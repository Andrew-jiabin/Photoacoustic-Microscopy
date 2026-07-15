#!/usr/bin/env python3
"""CBOX-Micro serial test utility for Bright Solutions pulsed lasers.

The local CBOX-Micro manual documents front-panel behavior but does not document
the USB serial command bytes. This script therefore separates source-backed
state sequences from the transport-specific button command map.

Default behavior never writes to the serial port. Any serial write requires:

  --write --confirm-write LASER_RISK_ACCEPTED

If the vendor command map is later found, pass it as JSON with --command-map.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


WRITE_CONFIRM_TOKEN = "LASER_RISK_ACCEPTED"
DEFAULT_BAUD = 9600


MANUAL_SOURCE = "CBOX-Micro User's Manual 2014-04-14.pdf"


BUTTON_LABELS = {
    "laser_off": "Laser OFF",
    "emission": "Emission",
    "down": "Down",
    "up": "Up",
    "lf": "L/F",
    "int_ext": "INT/EXT",
    "cw_qs": "CW/QS",
    "aim_beam": "Aim Beam",
}


@dataclass(frozen=True)
class ButtonStep:
    button: str
    reason: str
    repeat: int = 1


def cbox_manual_map() -> dict[str, object]:
    return {
        "source": MANUAL_SOURCE,
        "manual_pages": {
            "front_panel": "pages 5-6",
            "start_sequence": "pages 6-7",
            "shutdown_sequence": "page 7",
            "external_trigger": "page 8",
        },
        "states": {
            "off": "Laser status OFF / Laser off.",
            "standby": "Laser status Stand by. Laser OFF switch is latched in the innermost position.",
            "on": "Laser status LASER ON. Lasing LED is illuminated; laser emission can be present.",
        },
        "buttons": {
            "laser_off": "Switches OFF -> Stand By and Stand By/ON -> OFF.",
            "emission": "Switches Stand By -> ON and ON -> Stand By.",
            "int_ext": "Toggles internal trigger and external TTL trigger source.",
            "down": "Decreases power or frequency depending on L/F selection.",
            "up": "Increases power or frequency depending on L/F selection.",
            "lf": "Selects whether Up/Down edit Level or Frequency.",
            "cw_qs": "Sets Continuous Q-switch mode on supported laser models.",
            "aim_beam": "Toggles red aiming beam; on LUCE SB OEM, hold over 2 s.",
        },
        "safety": [
            "The manual does not document serial command bytes.",
            "The manual does not document a readback command for current state.",
            "Laser OFF and Emission are toggle-like controls, so scripted operation requires a trusted assumed state.",
            "The manual shutdown sequence says to decrease pumping power to 0 before Emission off and Laser OFF.",
            "External trigger requires a suitable TTL pulse train on the External Trigger Input BNC.",
        ],
    }


def sequence_for_mode(
    target: str,
    assumed_state: str,
    zero_power_down_count: int = 0,
) -> list[ButtonStep]:
    if target == "standby":
        if assumed_state == "off":
            return [ButtonStep("laser_off", "manual page 7: OFF -> Stand by")]
        if assumed_state == "on":
            return [ButtonStep("emission", "manual page 5: Emission switches ON -> Stand by")]
        return []

    if target == "on":
        if assumed_state == "off":
            return [
                ButtonStep("laser_off", "manual page 7: first enter Stand by from OFF"),
                ButtonStep("emission", "manual page 7: Stand by -> LASER ON; emission can be present"),
            ]
        if assumed_state == "standby":
            return [ButtonStep("emission", "manual page 7: Stand by -> LASER ON; emission can be present")]
        return []

    if target == "off":
        steps: list[ButtonStep] = []
        if zero_power_down_count > 0:
            steps.append(
                ButtonStep(
                    "down",
                    "manual page 7: decrease pumping power until it reaches 0",
                    repeat=zero_power_down_count,
                )
            )
        if assumed_state == "on":
            steps.extend(
                [
                    ButtonStep("emission", "manual page 7: temporarily switch off emission"),
                    ButtonStep("laser_off", "manual page 7: disable the laser"),
                ]
            )
        elif assumed_state == "standby":
            steps.append(ButtonStep("laser_off", "manual page 7: disable the laser"))
        return steps

    raise ValueError(f"Unsupported target mode {target!r}")


def sequence_for_trigger(target: str, assumed_trigger: str) -> list[ButtonStep]:
    if target == assumed_trigger:
        return []
    return [
        ButtonStep(
            "int_ext",
            "manual pages 6-8: INT/EXT toggles internal and external TTL trigger source",
        )
    ]


def list_serial_ports() -> list[dict[str, str]]:
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        return list_serial_ports_windows_fallback()
    ports = []
    for port in list_ports.comports():
        ports.append(
            {
                "device": str(port.device),
                "description": str(port.description),
                "hwid": str(port.hwid),
            }
        )
    return ports


def list_serial_ports_windows_fallback() -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.Name -match 'COM|FTDI|USB Serial|CBOX|Bright|LUCE|Laser' "
            "-or $_.PNPDeviceID -match 'FTDI|VID_0403' } | "
            "Select-Object Name,PNPDeviceID,Status | ConvertTo-Json -Compress"
        ),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except Exception:
        return []
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    ports: list[dict[str, str]] = []
    for item in payload:
        name = str(item.get("Name", ""))
        pnp = str(item.get("PNPDeviceID", ""))
        if "COM" not in name and "FTDI" not in pnp and "VID_0403" not in pnp:
            continue
        device = ""
        marker = "(COM"
        if marker in name:
            start = name.find(marker) + 1
            end = name.find(")", start)
            if end > start:
                device = name[start:end]
        ports.append({"device": device, "description": name, "hwid": pnp})
    return ports


def load_command_map(path: str | None) -> dict[str, bytes]:
    if not path:
        return {}
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("--command-map must be a JSON object")
    return {str(key): parse_payload(value) for key, value in raw.items()}


def parse_payload(value: object) -> bytes:
    if isinstance(value, list):
        return bytes(int(item) & 0xFF for item in value)
    if not isinstance(value, str):
        raise ValueError(f"Command payload must be string or byte list, got {value!r}")
    if value.startswith("hex:"):
        return bytes.fromhex(value[4:].strip())
    if value.startswith("text:"):
        return value[5:].encode("ascii")
    return value.encode("ascii")


def render_steps(steps: list[ButtonStep]) -> str:
    if not steps:
        return "No button action needed for the provided assumed state."
    rows = []
    for index, step in enumerate(steps, 1):
        label = BUTTON_LABELS.get(step.button, step.button)
        suffix = f" x{step.repeat}" if step.repeat != 1 else ""
        rows.append(f"{index}. {label}{suffix} ({step.button})")
        rows.append(f"   reason: {step.reason}")
    return "\n".join(rows)


def serial_line_ending(name: str) -> bytes:
    return {
        "none": b"",
        "lf": b"\n",
        "cr": b"\r",
        "crlf": b"\r\n",
    }[name]


def open_serial(args: argparse.Namespace):
    import serial  # type: ignore

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = args.timeout
    ser.write_timeout = getattr(args, "write_timeout", 1.0)
    ser.dsrdtr = args.dsrdtr
    ser.rtscts = args.rtscts
    # Keep modem-control lines inactive unless explicitly requested. Some
    # USB-serial devices use DTR/RTS as reset or control lines.
    ser.dtr = bool(getattr(args, "dtr", False))
    ser.rts = bool(getattr(args, "rts", False))
    ser.open()
    return ser


def require_write_confirmation(args: argparse.Namespace) -> bool:
    return bool(args.write and args.confirm_write == WRITE_CONFIRM_TOKEN)


def execute_steps(args: argparse.Namespace, steps: list[ButtonStep]) -> int:
    print("Planned CBOX-Micro button sequence:")
    print(render_steps(steps))
    if not steps:
        return 0

    command_map = load_command_map(args.command_map)
    missing = sorted({step.button for step in steps if step.button not in command_map})
    if missing:
        print()
        print("No serial write was sent.")
        print("Missing command-map entries for:", ", ".join(missing))
        print("The CBOX-Micro manual documents button behavior but not serial bytes.")
        return 0

    if not require_write_confirmation(args):
        print()
        print("No serial write was sent. To execute the mapped sequence, add:")
        print(f"  --write --confirm-write {WRITE_CONFIRM_TOKEN}")
        return 0

    if not args.port:
        print("--port is required for serial writes.", file=sys.stderr)
        return 2

    try:
        import serial  # type: ignore
    except ImportError:
        print("Serial writes require pyserial: python -m pip install pyserial", file=sys.stderr)
        return 2

    with open_serial(args) as ser:
        for step in steps:
            payload = command_map[step.button] + serial_line_ending(args.line_ending)
            for repeat_index in range(step.repeat):
                ser.write(payload)
                ser.flush()
                print(f"Sent {step.button} repeat {repeat_index + 1}/{step.repeat}: {payload.hex(' ')}")
                time.sleep(args.step_delay)
        if args.read_after > 0:
            data = read_serial_for(ser, args.read_after)
            print_serial_payload("Read after sequence", data)
    return 0


def read_serial_for(ser, seconds: float) -> bytes:  # type: ignore[no-untyped-def]
    deadline = time.time() + max(0.0, seconds)
    chunks: list[bytes] = []
    while time.time() < deadline:
        waiting = getattr(ser, "in_waiting", 0)
        if waiting:
            chunks.append(ser.read(waiting))
        else:
            time.sleep(0.05)
    return b"".join(chunks)


def print_serial_payload(label: str, data: bytes) -> None:
    print(f"{label}: {len(data)} bytes")
    if not data:
        return
    print("  hex :", data.hex(" "))
    print("  text:", data.decode("utf-8", errors="replace"))


def handle_map(args: argparse.Namespace) -> int:
    print(json.dumps(cbox_manual_map(), indent=2))
    return 0


def handle_ports(args: argparse.Namespace) -> int:
    print(json.dumps(list_serial_ports(), indent=2))
    return 0


def handle_passive_read(args: argparse.Namespace) -> int:
    if not args.port:
        print("--port is required for passive-read.", file=sys.stderr)
        return 2
    try:
        import serial  # type: ignore
    except ImportError:
        print("Passive serial reads require pyserial: python -m pip install pyserial", file=sys.stderr)
        return 2
    with open_serial(args) as ser:
        print(f"Opened {args.port} at {args.baud} baud for passive read only.")
        print_serial_payload("Passive read", read_serial_for(ser, args.seconds))
    return 0


def handle_connect_test(args: argparse.Namespace) -> int:
    if not args.port:
        print("--port is required for connect-test.", file=sys.stderr)
        return 2
    try:
        import serial  # type: ignore  # noqa: F401
    except ImportError:
        print("Connection test requires pyserial: python -m pip install pyserial", file=sys.stderr)
        return 2
    started = time.time()
    with open_serial(args) as ser:
        elapsed_ms = (time.time() - started) * 1000.0
        print("CBOX_SERIAL_CONNECT_OK")
        print(f"  port={ser.port}")
        print(f"  baud={ser.baudrate}")
        print(f"  open_elapsed_ms={elapsed_ms:.1f}")
        print(f"  dtr={ser.dtr}")
        print(f"  rts={ser.rts}")
        for name, getter in (
            ("cts", lambda: ser.cts),
            ("dsr", lambda: ser.dsr),
            ("ri", lambda: ser.ri),
            ("cd", lambda: ser.cd),
        ):
            try:
                value = getter()
            except Exception as exc:  # noqa: BLE001
                value = f"ERROR:{exc}"
            print(f"  {name}={value}")
        try:
            waiting = ser.in_waiting
        except Exception as exc:  # noqa: BLE001
            waiting = f"ERROR:{exc}"
        print(f"  in_waiting={waiting}")
        if args.seconds > 0:
            print_serial_payload("Passive read", read_serial_for(ser, args.seconds))
    print("CBOX_SERIAL_CLOSED_OK")
    return 0


def handle_send(args: argparse.Namespace) -> int:
    if args.text is None and args.hex is None:
        print("Provide --text or --hex.", file=sys.stderr)
        return 2
    if not args.port:
        print("--port is required for send.", file=sys.stderr)
        return 2
    if not require_write_confirmation(args):
        print("No serial write was sent. To execute the user-provided payload, add:")
        print(f"  --write --confirm-write {WRITE_CONFIRM_TOKEN}")
        return 0
    try:
        import serial  # type: ignore
    except ImportError:
        print("Serial writes require pyserial: python -m pip install pyserial", file=sys.stderr)
        return 2
    payload = (args.text.encode(args.encoding) if args.text is not None else bytes.fromhex(args.hex))
    payload += serial_line_ending(args.line_ending)
    with open_serial(args) as ser:
        ser.write(payload)
        ser.flush()
        print_serial_payload("Sent", payload)
        if args.read_after > 0:
            print_serial_payload("Read after send", read_serial_for(ser, args.read_after))
    return 0


def handle_mode(args: argparse.Namespace) -> int:
    steps = sequence_for_mode(args.target_mode, args.assume_state, args.zero_power_down_count)
    return execute_steps(args, steps)


def handle_trigger(args: argparse.Namespace) -> int:
    steps = sequence_for_trigger(args.target_trigger, args.assume_trigger)
    return execute_steps(args, steps)


def add_serial_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--port", help="Serial port, for example COM10.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--timeout", type=float, default=0.2)
    parser.add_argument("--write-timeout", type=float, default=1.0)
    parser.add_argument("--dsrdtr", action="store_true", help="Enable DSR/DTR flow control.")
    parser.add_argument("--rtscts", action="store_true", help="Enable RTS/CTS flow control.")
    parser.add_argument("--dtr", action="store_true", help="Assert DTR after opening the serial port.")
    parser.add_argument("--rts", action="store_true", help="Assert RTS after opening the serial port.")


def add_write_guard(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--write", action="store_true", help="Actually send serial bytes.")
    parser.add_argument("--confirm-write", help=f"Must equal {WRITE_CONFIRM_TOKEN}.")


def add_sequence_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--command-map", help="JSON mapping button keys to payloads, e.g. {'emission':'text:E'}.")
    parser.add_argument("--line-ending", choices=("none", "lf", "cr", "crlf"), default="crlf")
    parser.add_argument("--step-delay", type=float, default=0.5)
    parser.add_argument("--read-after", type=float, default=1.0)
    add_serial_options(parser)
    add_write_guard(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manual = subparsers.add_parser("manual-map", help="Print source-backed CBOX-Micro behavior map.")
    manual.set_defaults(func=handle_map)

    ports = subparsers.add_parser("list-ports", help="List serial/FTDI ports without opening them.")
    ports.set_defaults(func=handle_ports)

    passive = subparsers.add_parser("passive-read", help="Open a port and read without sending bytes.")
    add_serial_options(passive)
    passive.add_argument("--seconds", type=float, default=2.0)
    passive.set_defaults(func=handle_passive_read)

    connect = subparsers.add_parser("connect-test", help="Actually open a serial port and report connection status without sending bytes.")
    add_serial_options(connect)
    connect.add_argument("--seconds", type=float, default=1.0, help="Passive read duration after opening.")
    connect.set_defaults(func=handle_connect_test)

    send = subparsers.add_parser("send", help="Send a user-provided payload after explicit write confirmation.")
    add_serial_options(send)
    add_write_guard(send)
    send.add_argument("--text")
    send.add_argument("--hex")
    send.add_argument("--encoding", default="ascii")
    send.add_argument("--line-ending", choices=("none", "lf", "cr", "crlf"), default="crlf")
    send.add_argument("--read-after", type=float, default=1.0)
    send.set_defaults(func=handle_send)

    mode = subparsers.add_parser("mode", help="Plan or execute OFF/Standby/ON button sequence.")
    add_sequence_options(mode)
    mode.add_argument("target_mode", choices=("off", "standby", "on"))
    mode.add_argument("--assume-state", choices=("off", "standby", "on"), required=True)
    mode.add_argument(
        "--zero-power-down-count",
        type=int,
        default=0,
        help="For target off, optionally press Down this many times before emission off.",
    )
    mode.set_defaults(func=handle_mode)

    trigger = subparsers.add_parser("trigger", help="Plan or execute INT/EXT trigger toggle sequence.")
    add_sequence_options(trigger)
    trigger.add_argument("target_trigger", choices=("internal", "external"))
    trigger.add_argument("--assume-trigger", choices=("internal", "external"), required=True)
    trigger.set_defaults(func=handle_trigger)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
