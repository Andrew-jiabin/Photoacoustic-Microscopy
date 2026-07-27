#!/usr/bin/env python
"""BrightSolutions CBOX-Micro FTDI D2XX control helper.

The frame format and command IDs were recovered from the vendor
``2016_08_05 USBLaserController.exe``:

    ASCII_HEX(cmd, arg1, arg2, arg3, xor) + CR

where xor is cmd ^ arg1 ^ arg2 ^ arg3.  The controller responds with the same
11-byte ASCII-hex frame format.

Default operations are read-only. State-changing commands require:

    --write --confirm-write LASER_RISK_ACCEPTED
"""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from ctypes import byref, c_ubyte, c_uint32, c_ushort, c_void_p, create_string_buffer
from typing import Any

FT_OK = 0
FT_OPEN_BY_SERIAL_NUMBER = 1
FT_PURGE_RX = 1
FT_PURGE_TX = 2
FT_BITS_8 = 8
FT_STOP_BITS_1 = 0
FT_PARITY_NONE = 0
FT_FLOW_NONE = 0

DEFAULT_SERIAL = "BS7VJICA"
DEFAULT_BAUD_RATE = 9600

QUERY_COMMANDS = {
    "flags": (0x90, 0x00, 0x00, 0x00),
    "cmon": (0x92, 0x00, 0x00, 0x00),
    "photo_diode": (0x94, 0x00, 0x00, 0x00),
    "model": (0x98, 0x00, 0x00, 0x00),
    "freq": (0x9A, 0x00, 0x00, 0x00),
    "serial": (0x9E, 0x00, 0x00, 0x00),
    "pulse_width": (0xA4, 0x00, 0x00, 0x00),
}

WRITE_COMMANDS = {
    "trigger_int": (0x82, 0x00, 0x00, 0x00),
    "trigger_ext": (0x82, 0x01, 0x00, 0x00),
    "emission_off": (0x84, 0x00, 0x00, 0x00),
    "emission_on": (0x84, 0x01, 0x00, 0x00),
}


def make_frame(cmd: int, arg1: int = 0, arg2: int = 0, arg3: int = 0) -> bytes:
    vals = [cmd & 0xFF, arg1 & 0xFF, arg2 & 0xFF, arg3 & 0xFF]
    vals.append(vals[0] ^ vals[1] ^ vals[2] ^ vals[3])
    return ("".join(f"{v:02X}" for v in vals) + "\r").encode("ascii")


def parse_response(raw: bytes) -> dict[str, Any]:
    text = raw.decode("ascii", errors="replace")
    clean = text.strip("\r\n")
    if len(clean) != 10:
        return {"text": text, "error": f"unexpected payload length {len(clean)}"}
    vals = [int(clean[i : i + 2], 16) for i in range(0, 10, 2)]
    xor = vals[0] ^ vals[1] ^ vals[2] ^ vals[3]
    out: dict[str, Any] = {
        "text": text,
        "bytes": vals,
        "checksum_ok": xor == vals[4],
    }
    if vals[0] == 0xE1:
        out["controller_error_code"] = vals[1]
    return out


def decode_measurement(action: str, parsed: dict[str, Any]) -> dict[str, Any]:
    vals = parsed.get("bytes")
    if not isinstance(vals, list) or len(vals) < 5 or not parsed.get("checksum_ok"):
        return {}
    b1, b2, b3 = vals[1], vals[2], vals[3]
    if action == "serial":
        # This matches the vendor GUI logic and gives e.g. 244362.
        return {"laser_serial_number": f"{b3:02d}{b2:02X}{b1:02d}"}
    if action == "freq":
        return {"repetition_rate_hz": (b1 << 16) + (b2 << 8) + b3}
    if action == "cmon":
        return {"current_monitor_a": b1, "raw_low_bytes": [b2, b3]}
    if action == "photo_diode":
        return {"photo_diode_v": ((b2 << 8) + b3) / 1000.0}
    if action == "pulse_width":
        return {"pulse_width_us": ((b2 << 8) + b3) / 10.0}
    if action == "flags":
        return {
            "flags_byte_1": b1,
            "flags_byte_2": b2,
            "flags_byte_3": b3,
            "flags_hex": f"{b1:02X}{b2:02X}{b3:02X}",
        }
    if action == "model":
        return {"model_chunk_byte": b2, "model_chunk_ascii": chr(b2) if 32 <= b2 <= 126 else None}
    return {}


def load_d2xx() -> Any:
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
    dll.FT_SetBaudRate.argtypes = [c_void_p, c_uint32]
    dll.FT_SetBaudRate.restype = c_uint32
    dll.FT_SetDataCharacteristics.argtypes = [c_void_p, c_ubyte, c_ubyte, c_ubyte]
    dll.FT_SetDataCharacteristics.restype = c_uint32
    dll.FT_SetFlowControl.argtypes = [c_void_p, c_ushort, c_ubyte, c_ubyte]
    dll.FT_SetFlowControl.restype = c_uint32
    dll.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]
    dll.FT_SetTimeouts.restype = c_uint32
    return dll


def check(status: int, action: str) -> None:
    if status != FT_OK:
        raise RuntimeError(f"{action} failed: FT_STATUS={status}")


def transact(serial: str, frame: bytes, timeout_s: float) -> dict[str, Any]:
    dll = load_d2xx()
    handle = c_void_p()
    serial_buf = create_string_buffer(serial.encode("ascii"))
    check(dll.FT_OpenEx(serial_buf, FT_OPEN_BY_SERIAL_NUMBER, byref(handle)), f"FT_OpenEx({serial})")
    try:
        check(dll.FT_SetBaudRate(handle, DEFAULT_BAUD_RATE), "FT_SetBaudRate")
        check(
            dll.FT_SetDataCharacteristics(handle, FT_BITS_8, FT_STOP_BITS_1, FT_PARITY_NONE),
            "FT_SetDataCharacteristics",
        )
        check(dll.FT_SetFlowControl(handle, FT_FLOW_NONE, 0, 0), "FT_SetFlowControl")
        check(dll.FT_SetTimeouts(handle, int(timeout_s * 1000), int(timeout_s * 1000)), "FT_SetTimeouts")
        check(dll.FT_Purge(handle, FT_PURGE_RX | FT_PURGE_TX), "FT_Purge")

        written = c_uint32(0)
        tx = create_string_buffer(frame)
        check(dll.FT_Write(handle, tx, len(frame), byref(written)), "FT_Write")

        deadline = time.time() + timeout_s
        queued = c_uint32(0)
        while time.time() < deadline:
            check(dll.FT_GetQueueStatus(handle, byref(queued)), "FT_GetQueueStatus")
            if queued.value >= len(frame):
                break
            time.sleep(0.02)

        if queued.value == 0:
            return {
                "tx": frame.decode("ascii", errors="replace"),
                "written": written.value,
                "rx_hex": "",
                "parsed": {"error": "timeout/no response"},
            }

        nread = c_uint32(0)
        rx = create_string_buffer(queued.value)
        check(dll.FT_Read(handle, rx, queued.value, byref(nread)), "FT_Read")
        raw = bytes(rx.raw[: nread.value])
        return {
            "tx": frame.decode("ascii", errors="replace"),
            "written": written.value,
            "rx_hex": raw.hex(" "),
            "parsed": parse_response(raw),
        }
    finally:
        dll.FT_Close(handle)


def build_arg_parser() -> argparse.ArgumentParser:
    actions = sorted(set(QUERY_COMMANDS) | set(WRITE_COMMANDS) | {"status", "frames"})
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=actions, help="Read-only query or guarded state-changing command")
    parser.add_argument("--serial", default=DEFAULT_SERIAL, help="FTDI serial number")
    parser.add_argument("--timeout", type=float, default=2.5)
    parser.add_argument("--write", action="store_true", help="Actually send state-changing commands")
    parser.add_argument("--confirm-write", default="", help="Must be LASER_RISK_ACCEPTED for writes")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    return parser


def run_single(action: str, serial: str, timeout: float) -> dict[str, Any]:
    if action in QUERY_COMMANDS:
        frame = make_frame(*QUERY_COMMANDS[action])
    else:
        frame = make_frame(*WRITE_COMMANDS[action])
    result = transact(serial, frame, timeout_s=timeout)
    result["action"] = action
    result["decoded"] = decode_measurement(action, result["parsed"])
    return result


def main() -> int:
    args = build_arg_parser().parse_args()

    if args.action == "frames":
        payload = {
            "queries": {name: make_frame(*cmd).decode("ascii") for name, cmd in QUERY_COMMANDS.items()},
            "writes": {name: make_frame(*cmd).decode("ascii") for name, cmd in WRITE_COMMANDS.items()},
        }
        print(json.dumps(payload, indent=2))
        return 0

    if args.action in WRITE_COMMANDS and not (args.write and args.confirm_write == "LASER_RISK_ACCEPTED"):
        frame = make_frame(*WRITE_COMMANDS[args.action]).decode("ascii")
        raise SystemExit(
            f"Refusing to send {args.action} ({frame!r}) without "
            "--write --confirm-write LASER_RISK_ACCEPTED"
        )

    if args.action == "status":
        results = [run_single(action, args.serial, args.timeout) for action in ("serial", "flags", "freq", "cmon", "photo_diode", "pulse_width")]
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            for item in results:
                print(f"{item['action']}: tx={item['tx']!r} rx={item['parsed'].get('text')!r} decoded={item['decoded']}")
        return 0

    result = run_single(args.action, args.serial, args.timeout)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"action={result['action']}")
        print(f"tx={result['tx']!r}")
        print(f"written={result['written']}")
        print(f"rx_hex={result['rx_hex']}")
        print(f"parsed={result['parsed']}")
        print(f"decoded={result['decoded']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
