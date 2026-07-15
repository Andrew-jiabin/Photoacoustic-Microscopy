#!/usr/bin/env python
"""Read-only FTDI D2XX probe for BrightSolutions CBOX-Micro.

This implements the ASCII-hex frame format recovered from the vendor
USBLaserController .NET executable. By default it only sends read/query
commands and does not change laser state.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
from ctypes import byref, c_char_p, c_uint32, c_void_p, create_string_buffer

FT_OK = 0
FT_OPEN_BY_SERIAL_NUMBER = 1
FT_PURGE_RX = 1
FT_PURGE_TX = 2

QUERY_COMMANDS = {
    "flags": 0x90,
    "serial": 0x9E,
    "cmon": 0x92,
    "photo_diode": 0x94,
    "model": 0x98,
    "freq": 0x9A,
    "pulse_width": 0xA4,
}

WRITE_COMMANDS = {
    "emission_off": (0x84, 0, 0, 0),
    "emission_on": (0x84, 1, 0, 0),
    "trigger_int": (0x82, 0, 0, 0),
    "trigger_ext": (0x82, 1, 0, 0),
}


def load_dll():
    try:
        dll = ctypes.WinDLL("ftd2xx.dll")
    except OSError as exc:
        raise RuntimeError(f"Cannot load ftd2xx.dll: {exc}") from exc
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
    if hasattr(dll, "FT_SetBaudRate"):
        dll.FT_SetBaudRate.argtypes = [c_void_p, c_uint32]
        dll.FT_SetBaudRate.restype = c_uint32
    if hasattr(dll, "FT_SetDataCharacteristics"):
        dll.FT_SetDataCharacteristics.argtypes = [c_void_p, ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte]
        dll.FT_SetDataCharacteristics.restype = c_uint32
    if hasattr(dll, "FT_SetFlowControl"):
        dll.FT_SetFlowControl.argtypes = [c_void_p, ctypes.c_ushort, ctypes.c_ubyte, ctypes.c_ubyte]
        dll.FT_SetFlowControl.restype = c_uint32
    if hasattr(dll, "FT_SetTimeouts"):
        dll.FT_SetTimeouts.argtypes = [c_void_p, c_uint32, c_uint32]
        dll.FT_SetTimeouts.restype = c_uint32
    return dll


def check(status: int, action: str) -> None:
    if status != FT_OK:
        raise RuntimeError(f"{action} failed: FT_STATUS={status}")


def make_frame(cmd: int, a1: int = 0, a2: int = 0, a3: int = 0) -> bytes:
    vals = [cmd & 0xFF, a1 & 0xFF, a2 & 0xFF, a3 & 0xFF]
    vals.append(vals[0] ^ vals[1] ^ vals[2] ^ vals[3])
    return ("".join(f"{v:02X}" for v in vals) + "\r").encode("ascii")


def parse_response(raw: bytes):
    text = raw.decode("ascii", errors="replace")
    clean = text.strip("\r\n")
    if len(clean) != 10:
        return {"text": text, "error": f"unexpected payload length {len(clean)}"}
    vals = [int(clean[i:i+2], 16) for i in range(0, 10, 2)]
    xor = vals[0] ^ vals[1] ^ vals[2] ^ vals[3]
    return {"text": text, "bytes": vals, "checksum_ok": xor == vals[4], "error_code": vals[1] if vals[0] == 0xE1 else None}


def transact(serial: str, frame: bytes, timeout_s: float = 2.5, baud: int | None = None):
    dll = load_dll()
    handle = c_void_p()
    serial_buf = create_string_buffer(serial.encode("ascii"))
    check(dll.FT_OpenEx(serial_buf, FT_OPEN_BY_SERIAL_NUMBER, byref(handle)), f"FT_OpenEx({serial})")
    try:
        if baud is not None and hasattr(dll, "FT_SetBaudRate"):
            check(dll.FT_SetBaudRate(handle, baud), f"FT_SetBaudRate({baud})")
        if hasattr(dll, "FT_SetDataCharacteristics"):
            check(dll.FT_SetDataCharacteristics(handle, 8, 0, 0), "FT_SetDataCharacteristics(8N1)")
        if hasattr(dll, "FT_SetFlowControl"):
            check(dll.FT_SetFlowControl(handle, 0, 0, 0), "FT_SetFlowControl(none)")
        if hasattr(dll, "FT_SetTimeouts"):
            check(dll.FT_SetTimeouts(handle, int(timeout_s * 1000), int(timeout_s * 1000)), "FT_SetTimeouts")
        dll.FT_Purge(handle, FT_PURGE_RX | FT_PURGE_TX)
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
            return {"frame": frame.decode("ascii", "replace"), "written": written.value, "queued": 0, "raw": b"", "parsed": {"error": "timeout/no response"}}
        nread = c_uint32(0)
        rx = create_string_buffer(queued.value)
        check(dll.FT_Read(handle, rx, queued.value, byref(nread)), "FT_Read")
        raw = bytes(rx.raw[:nread.value])
        return {"frame": frame.decode("ascii", "replace"), "written": written.value, "queued": queued.value, "raw": raw, "parsed": parse_response(raw)}
    finally:
        dll.FT_Close(handle)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", nargs="?", default="serial", choices=sorted(QUERY_COMMANDS | WRITE_COMMANDS))
    ap.add_argument("--serial", default="BS7VJICA", help="FTDI serial number")
    ap.add_argument("--timeout", type=float, default=2.5)
    ap.add_argument("--baud", type=int, default=None, help="Optional FTDI baud rate, normally not needed")
    ap.add_argument("--write", action="store_true", help="Allow state-changing commands")
    ap.add_argument("--confirm-write", default="")
    args = ap.parse_args()

    if args.action in QUERY_COMMANDS:
        frame = make_frame(QUERY_COMMANDS[args.action])
    else:
        if not args.write or args.confirm_write != "LASER_RISK_ACCEPTED":
            raise SystemExit("Refusing state-changing command without --write --confirm-write LASER_RISK_ACCEPTED")
        frame = make_frame(*WRITE_COMMANDS[args.action])

    result = transact(args.serial, frame, timeout_s=args.timeout, baud=args.baud)
    print("TX", result["frame"].encode("ascii").hex(" "), repr(result["frame"]))
    print("written", result["written"], "queued", result["queued"])
    print("RX", result["raw"].hex(" "), repr(result["raw"]))
    print("parsed", result["parsed"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

