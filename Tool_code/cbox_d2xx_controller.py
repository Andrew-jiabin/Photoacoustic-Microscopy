#!/usr/bin/env python
"""Reusable BrightSolutions CBOX-Micro controller class.

This module wraps the command format recovered from the vendor
``2016_08_05 USBLaserController.exe``.  It uses FTDI D2XX directly, matching
the vendor program; it does not use the VCP/COM-port serial API.

The CBOX front-panel ``Laser OFF`` standby/off latch is still a physical
toggle.  The software-visible emission command here is the vendor GUI's
``Laser Emission ON/OFF`` button, which works after the box is manually put in
Stand By.
"""

from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass
from typing import Any, ClassVar
from ctypes import byref, c_uint32, c_void_p, create_string_buffer

FT_OK = 0
FT_OPEN_BY_SERIAL_NUMBER = 1
FT_PURGE_RX = 1
FT_PURGE_TX = 2

DEFAULT_FTDI_SERIAL = "BS7VJICA"


@dataclass(frozen=True)
class CboxResponse:
    """Parsed 11-byte ASCII-hex response from CBOX-Micro."""

    tx_text: str
    rx_text: str
    rx_hex: str
    bytes: tuple[int, int, int, int, int] | None
    checksum_ok: bool
    controller_error_code: int | None = None

    @property
    def ok(self) -> bool:
        return self.bytes is not None and self.checksum_ok and self.controller_error_code is None

    @property
    def payload(self) -> tuple[int, int, int] | None:
        if self.bytes is None:
            return None
        return self.bytes[1], self.bytes[2], self.bytes[3]


class CboxD2xxController:
    """Controller class for the BrightSolutions CBOX-Micro FTDI protocol.

    The underlying vendor frame is:

        ASCII_HEX(cmd, arg1, arg2, arg3, xor) + CR

    where ``xor = cmd ^ arg1 ^ arg2 ^ arg3``.

    ``send_message`` exposes the full protocol surface found in the EXE.  The
    named methods below cover the current main-panel functions that have been
    verified or directly recovered from `USBLaserController.FormMain`.
    """

    QUERY_COMMANDS: ClassVar[dict[str, tuple[int, int, int, int]]] = {
        "flags": (0x90, 0x00, 0x00, 0x00),
        "current_monitor": (0x92, 0x00, 0x00, 0x00),
        "photo_diode": (0x94, 0x00, 0x00, 0x00),
        "model_chunk": (0x98, 0x00, 0x00, 0x00),
        "frequency": (0x9A, 0x00, 0x00, 0x00),
        "operation_hours": (0x9C, 0x00, 0x00, 0x00),
        "serial_number": (0x9E, 0x00, 0x00, 0x00),
        "pulse_width": (0xA4, 0x00, 0x00, 0x00),
    }

    MAIN_PANEL_COMMANDS: ClassVar[dict[str, tuple[int, int, int, int]]] = {
        "trigger_internal": (0x82, 0x00, 0x00, 0x00),
        "trigger_external": (0x82, 0x01, 0x00, 0x00),
        "emission_off": (0x84, 0x00, 0x00, 0x00),
        "emission_on": (0x84, 0x01, 0x00, 0x00),
        "aiming_beam_off": (0x86, 0x00, 0x00, 0x00),
        "aiming_beam_on": (0x86, 0x01, 0x00, 0x00),
        "cw_qs_off": (0x8C, 0x00, 0x00, 0x00),
        "cw_qs_on": (0x8C, 0x01, 0x00, 0x00),
        "reset": (0xA0, 0x01, 0x00, 0x00),
    }

    # Fixed service/menu commands recovered from the EXE. These are not called
    # by convenience methods because several are EEPROM, flash, firmware, or
    # service operations. Use send_known() or send_message() only deliberately.
    EXE_COMMAND_MAP: ClassVar[dict[str, tuple[int, int, int, int]]] = {
        **QUERY_COMMANDS,
        **MAIN_PANEL_COMMANDS,
        "rtc_refresh": (0x9C, 0xFE, 0x00, 0x00),
        "service_firmware_update": (0xA0, 0x03, 0x00, 0x00),
        "service_eeprom_download": (0xD4, 0x10, 0x08, 0xFF),
        "service_editor_save": (0xC4, 0x00, 0x00, 0x00),
        "service_save_to_flash": (0x5B, 0x01, 0x00, 0x00),
        "service_fpk_test_1": (0xC8, 0x00, 0x04, 0x00),
        "service_fpk_test_2": (0xC8, 0x02, 0x00, 0x00),
        "service_fpk_test_pulse_width": (0xA4, 0x01, 0x00, 0x10),
        "service_fpk_test_freq_80khz": (0xA6, 0x01, 0x38, 0x80),
        "service_fpk_test_freq_10khz": (0xA6, 0x00, 0x27, 0x10),
        "pcbatr_setpoint_0": (0x88, 0x04, 0x00, 0x00),
        "pcbatr_setpoint_50": (0x88, 0x04, 0x00, 0x32),
        "pcbatr_setpoint_100": (0x88, 0x04, 0x00, 0x64),
    }

    def __init__(self, serial: str = DEFAULT_FTDI_SERIAL, timeout_s: float = 2.5) -> None:
        self.serial = serial
        self.timeout_s = timeout_s

    @staticmethod
    def make_frame(cmd: int, arg1: int = 0, arg2: int = 0, arg3: int = 0) -> bytes:
        vals = [cmd & 0xFF, arg1 & 0xFF, arg2 & 0xFF, arg3 & 0xFF]
        vals.append(vals[0] ^ vals[1] ^ vals[2] ^ vals[3])
        return ("".join(f"{v:02X}" for v in vals) + "\r").encode("ascii")

    @staticmethod
    def parse_response(tx_text: str, raw: bytes) -> CboxResponse:
        rx_text = raw.decode("ascii", errors="replace")
        clean = rx_text.strip("\r\n")
        if len(clean) != 10:
            return CboxResponse(tx_text, rx_text, raw.hex(" "), None, False)
        vals = tuple(int(clean[i : i + 2], 16) for i in range(0, 10, 2))
        checksum_ok = (vals[0] ^ vals[1] ^ vals[2] ^ vals[3]) == vals[4]
        error_code = vals[1] if vals[0] == 0xE1 else None
        return CboxResponse(tx_text, rx_text, raw.hex(" "), vals, checksum_ok, error_code)

    @staticmethod
    def _load_d2xx() -> Any:
        dll = ctypes.WinDLL("ftd2xx.dll")
        dll.FT_OpenEx.argtypes = [c_void_p, c_uint32, ctypes.POINTER(c_void_p)]
        dll.FT_OpenEx.restype = c_uint32
        dll.FT_Close.argtypes = [c_void_p]
        dll.FT_Close.restype = c_uint32
        dll.FT_Write.argtypes = [c_void_p, c_void_p, c_uint32, ctypes.POINTER(c_uint32)]
        dll.FT_Write.restype = c_uint32
        dll.FT_Read.argtypes = [c_void_p, c_void_p, c_uint32, ctypes.POINTER(c_uint32)]
        dll.FT_Read.restype = c_uint32
        dll.FT_GetQueueStatus.argtypes = [c_void_p, ctypes.POINTER(c_uint32)]
        dll.FT_GetQueueStatus.restype = c_uint32
        dll.FT_Purge.argtypes = [c_void_p, c_uint32]
        dll.FT_Purge.restype = c_uint32
        dll.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]
        dll.FT_SetTimeouts.restype = c_uint32
        return dll

    @staticmethod
    def _check(status: int, action: str) -> None:
        if status != FT_OK:
            raise RuntimeError(f"{action} failed: FT_STATUS={status}")

    def send_message(self, cmd: int, arg1: int = 0, arg2: int = 0, arg3: int = 0) -> CboxResponse:
        """Send one vendor frame and return the parsed response."""

        frame = self.make_frame(cmd, arg1, arg2, arg3)
        tx_text = frame.decode("ascii")
        dll = self._load_d2xx()
        handle = c_void_p()
        serial_buf = create_string_buffer(self.serial.encode("ascii"))
        self._check(dll.FT_OpenEx(serial_buf, FT_OPEN_BY_SERIAL_NUMBER, byref(handle)), f"FT_OpenEx({self.serial})")
        try:
            timeout_ms = int(self.timeout_s * 1000)
            self._check(dll.FT_SetTimeouts(handle, timeout_ms, timeout_ms), "FT_SetTimeouts")
            dll.FT_Purge(handle, FT_PURGE_RX | FT_PURGE_TX)

            written = c_uint32(0)
            tx_buf = create_string_buffer(frame)
            self._check(dll.FT_Write(handle, tx_buf, len(frame), byref(written)), "FT_Write")
            if written.value != len(frame):
                raise RuntimeError(f"short FT_Write: wrote {written.value} of {len(frame)} bytes")

            deadline = time.time() + self.timeout_s
            queued = c_uint32(0)
            while time.time() < deadline:
                self._check(dll.FT_GetQueueStatus(handle, byref(queued)), "FT_GetQueueStatus")
                if queued.value >= len(frame):
                    break
                time.sleep(0.02)

            if queued.value == 0:
                return CboxResponse(tx_text, "", "", None, False)

            read_count = c_uint32(0)
            rx_buf = create_string_buffer(queued.value)
            self._check(dll.FT_Read(handle, rx_buf, queued.value, byref(read_count)), "FT_Read")
            return self.parse_response(tx_text, bytes(rx_buf.raw[: read_count.value]))
        finally:
            dll.FT_Close(handle)

    def send_known(self, name: str) -> CboxResponse:
        """Send a named command from ``EXE_COMMAND_MAP``."""

        try:
            cmd = self.EXE_COMMAND_MAP[name]
        except KeyError as exc:
            known = ", ".join(sorted(self.EXE_COMMAND_MAP))
            raise KeyError(f"unknown command {name!r}; known commands: {known}") from exc
        return self.send_message(*cmd)

    def set_trigger_source(self, source: str) -> CboxResponse:
        normalized = source.strip().lower()
        if normalized in {"internal", "int", "0"}:
            return self.send_known("trigger_internal")
        if normalized in {"external", "ext", "1"}:
            return self.send_known("trigger_external")
        raise ValueError("source must be 'internal' or 'external'")

    def set_emission(self, enabled: bool) -> CboxResponse:
        return self.send_known("emission_on" if enabled else "emission_off")

    def set_aiming_beam(self, enabled: bool) -> CboxResponse:
        return self.send_known("aiming_beam_on" if enabled else "aiming_beam_off")

    def set_cw_qs(self, enabled: bool) -> CboxResponse:
        return self.send_known("cw_qs_on" if enabled else "cw_qs_off")

    def set_frequency_code(self, code: int) -> CboxResponse:
        """Send the vendor GUI frequency-code setter, not a direct Hz setter."""

        return self.send_message(0x8A, code & 0xFF, 0x00, 0x00)

    def set_level_raw(self, channel: int, value: int) -> CboxResponse:
        """Send the vendor GUI raw diode-level setter.

        This mirrors the EXE's ``SetLevel(channel, value)`` frame and should not
        be interpreted as a calibrated physical unit without additional proof.
        """

        return self.send_message(0x88, channel & 0xFF, (value * 8) & 0xFF, value & 0xFF)

    def set_pulse_width_raw(self, value: int, save: bool = False) -> CboxResponse:
        """Set pulse width using the EXE's raw two-byte value path."""

        arg1 = 0x02 if save else 0x01
        return self.send_message(0xA4, arg1, (value >> 8) & 0xFF, value & 0xFF)

    def reset(self) -> CboxResponse:
        """Send the vendor GUI RESET button command."""

        return self.send_known("reset")

    def get_flags(self) -> dict[str, Any]:
        response = self.send_known("flags")
        b1, b2, b3 = self._payload_or_raise(response)
        return {
            "raw": response.rx_text.strip(),
            "byte1": b1,
            "byte2": b2,
            "byte3": b3,
            "hex": f"{b1:02X}{b2:02X}{b3:02X}",
            # These two booleans are inferred from live tests on 2026-07-15:
            # trigger_int -> 0x02, trigger_ext -> 0x03, emission_on -> 0x0B.
            "trigger_external_inferred": bool(b1 & 0x01),
            "emission_on_inferred": bool(b1 & 0x08),
        }

    def get_laser_serial_number(self) -> str:
        response = self.send_known("serial_number")
        b1, b2, b3 = self._payload_or_raise(response)
        return f"{b3:02d}{b2:02X}{b1:02d}"

    def get_repetition_rate_hz(self) -> int:
        response = self.send_known("frequency")
        b1, b2, b3 = self._payload_or_raise(response)
        return (b1 << 16) + (b2 << 8) + b3

    def get_current_monitor(self) -> dict[str, Any]:
        response = self.send_known("current_monitor")
        b1, b2, b3 = self._payload_or_raise(response)
        return {"current_monitor_a_gui": b1, "raw_low_bytes": [b2, b3], "raw": response.rx_text.strip()}

    def get_photo_diode_voltage(self) -> float:
        response = self.send_known("photo_diode")
        _, b2, b3 = self._payload_or_raise(response)
        return ((b2 << 8) + b3) / 1000.0

    def get_pulse_width_us(self) -> float:
        response = self.send_known("pulse_width")
        _, b2, b3 = self._payload_or_raise(response)
        return ((b2 << 8) + b3) / 10.0

    def get_operation_hours(self, refresh: bool = True) -> int:
        if refresh:
            self.send_known("rtc_refresh")
        response = self.send_known("operation_hours")
        b1, b2, b3 = self._payload_or_raise(response)
        if b1 == b2 == b3 == 0xFF:
            return -1
        return (b1 << 16) + (b2 << 8) + b3

    def get_model_string(self, max_chars: int = 10) -> str:
        chars: list[str] = []
        for index in range(max_chars):
            response = self.send_message(0x98, index, 0x00, 0x00)
            _, value, _ = self._payload_or_raise(response)
            if value == 0:
                break
            chars.append(chr(value))
        return "".join(chars)

    def status(self) -> dict[str, Any]:
        return {
            "serial_number": self.get_laser_serial_number(),
            "flags": self.get_flags(),
            "repetition_rate_hz": self.get_repetition_rate_hz(),
            "current_monitor": self.get_current_monitor(),
            "photo_diode_v": self.get_photo_diode_voltage(),
            "pulse_width_us": self.get_pulse_width_us(),
        }

    @staticmethod
    def _payload_or_raise(response: CboxResponse) -> tuple[int, int, int]:
        if not response.ok or response.payload is None:
            raise RuntimeError(f"CBOX command failed: {response}")
        return response.payload

