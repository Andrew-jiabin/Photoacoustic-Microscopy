#!/usr/bin/env python3
"""Interactive CBOX-Micro payload discovery helper.

This script is for the case where the CBOX-Micro serial payloads are not
documented. It sends one candidate payload at a time and relies on the user to
observe the CBOX LCD/front panel.

Safety model:
- Start with Laser OFF discovery from physical/display state "Laser off".
- Do not continue scanning automatically after a visible state change.
- Emission discovery requires an explicit --allow-emission-test flag.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path


WRITE_CONFIRM_TOKEN = "LASER_RISK_ACCEPTED"


@dataclass(frozen=True)
class Candidate:
    label: str
    payload: bytes
    rationale: str


def default_candidates(button: str) -> list[Candidate]:
    # These are hypotheses only. They are ordered from the most likely/simple
    # UI index mapping to mnemonic ASCII candidates. Do not treat them as known.
    if button == "laser_off":
        return [
            Candidate("ascii_1", b"1", "front-panel item number 1 in the manual"),
            Candidate("ascii_L", b"L", "Laser OFF mnemonic"),
            Candidate("ascii_l", b"l", "Laser OFF mnemonic lowercase"),
            Candidate("ascii_O", b"O", "OFF mnemonic"),
            Candidate("ascii_o", b"o", "OFF mnemonic lowercase"),
            Candidate("byte_01", bytes([0x01]), "button index 1 as binary byte"),
        ]
    if button == "emission":
        return [
            Candidate("ascii_2", b"2", "front-panel item number 2 in the manual"),
            Candidate("ascii_E", b"E", "Emission mnemonic"),
            Candidate("ascii_e", b"e", "Emission mnemonic lowercase"),
            Candidate("byte_02", bytes([0x02]), "button index 2 as binary byte"),
        ]
    if button == "int_ext":
        return [
            Candidate("ascii_6", b"6", "front-panel item number 6 in the manual"),
            Candidate("ascii_I", b"I", "INT/EXT mnemonic"),
            Candidate("ascii_i", b"i", "INT/EXT mnemonic lowercase"),
            Candidate("ascii_X", b"X", "EXT mnemonic"),
            Candidate("ascii_x", b"x", "EXT mnemonic lowercase"),
            Candidate("byte_06", bytes([0x06]), "button index 6 as binary byte"),
        ]
    raise ValueError(f"Unsupported button {button!r}")


def serial_line_ending(name: str) -> bytes:
    return {
        "none": b"",
        "lf": b"\n",
        "cr": b"\r",
        "crlf": b"\r\n",
    }[name]


def expand_line_endings(candidates: list[Candidate]) -> list[Candidate]:
    endings = {
        "none": b"",
        "cr": b"\r",
        "lf": b"\n",
        "crlf": b"\r\n",
    }
    expanded: list[Candidate] = []
    for candidate in candidates:
        for name, suffix in endings.items():
            expanded.append(
                Candidate(
                    f"{candidate.label}_{name}",
                    candidate.payload + suffix,
                    f"{candidate.rationale}; line ending {name}",
                )
            )
    return expanded


def parse_payload(text: str) -> bytes:
    if text.startswith("hex:"):
        return bytes.fromhex(text[4:].strip())
    if text.startswith("text:"):
        return text[5:].encode("ascii")
    return text.encode("ascii")


def load_extra_candidates(path: str | None) -> list[Candidate]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("--extra-candidates must be a JSON list")
    result = []
    for item in raw:
        if not isinstance(item, dict) or "label" not in item or "payload" not in item:
            raise ValueError("Each extra candidate must contain label and payload")
        result.append(
            Candidate(
                str(item["label"]),
                parse_payload(str(item["payload"])),
                str(item.get("rationale", "user-supplied candidate")),
            )
        )
    return result


def write_jsonl(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_map(path: Path, button: str, payload: bytes) -> None:
    if path.exists():
        mapping = json.loads(path.read_text(encoding="utf-8"))
    else:
        mapping = {}
    mapping[button] = "hex:" + payload.hex(" ")
    path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")


def open_serial(args: argparse.Namespace):
    import serial  # type: ignore

    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = args.timeout
    ser.write_timeout = args.write_timeout
    ser.dtr = args.dtr
    ser.rts = args.rts
    ser.open()
    return ser


def read_for(ser, seconds: float) -> bytes:  # type: ignore[no-untyped-def]
    deadline = time.time() + max(seconds, 0.0)
    chunks: list[bytes] = []
    while time.time() < deadline:
        waiting = getattr(ser, "in_waiting", 0)
        if waiting:
            chunks.append(ser.read(waiting))
        else:
            time.sleep(0.05)
    return b"".join(chunks)


def safety_prompt(button: str) -> str:
    if button == "laser_off":
        return (
            "Before each candidate, set/confirm the display is 'Laser off'. "
            "Success means the display changes to 'Stand by'. Stop immediately after success."
        )
    if button == "emission":
        return (
            "Emission can make the laser lase. Use only after Laser OFF payload is known, "
            "pumping power is set to 0 if possible, beam path is safe, and goggles/interlocks are correct. "
            "Success means Stand by changes to LASER ON; then return to safe state."
        )
    return (
        "Use a non-lasing state if possible. Success means the LCD EXT/internal trigger indicator toggles."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("button", choices=("laser_off", "emission", "int_ext"))
    parser.add_argument("--port", default="COM10")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=0.2)
    parser.add_argument("--write-timeout", type=float, default=1.0)
    parser.add_argument("--line-ending", choices=("none", "lf", "cr", "crlf"), default="none")
    parser.add_argument(
        "--expand-line-endings",
        action="store_true",
        help="Expand each candidate into none/CR/LF/CRLF full-payload variants.",
    )
    parser.add_argument("--read-after", type=float, default=0.5)
    parser.add_argument("--step-delay", type=float, default=0.3)
    parser.add_argument("--dtr", action="store_true")
    parser.add_argument("--rts", action="store_true")
    parser.add_argument("--extra-candidates", help="JSON list of additional candidates.")
    parser.add_argument("--log", default="cbox_payload_discovery_log.jsonl")
    parser.add_argument("--output-map", default="cbox_discovered_command_map.json")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--confirm-write")
    parser.add_argument(
        "--allow-emission-test",
        action="store_true",
        help="Required for testing the emission button candidates.",
    )
    parser.add_argument(
        "--send-candidate",
        help="Send only one candidate label non-interactively, then exit after logging.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not (args.write and args.confirm_write == WRITE_CONFIRM_TOKEN):
        print(f"Refusing to send. Add --write --confirm-write {WRITE_CONFIRM_TOKEN}.")
        return 2
    if args.button == "emission" and not args.allow_emission_test:
        print("Refusing emission candidates without --allow-emission-test.")
        return 2

    try:
        import serial  # type: ignore  # noqa: F401
    except ImportError:
        print("Install pyserial first: python -m pip install pyserial")
        return 2

    candidates = default_candidates(args.button) + load_extra_candidates(args.extra_candidates)
    if args.expand_line_endings:
        candidates = expand_line_endings(candidates)
    if args.send_candidate:
        candidates = [item for item in candidates if item.label == args.send_candidate]
        if not candidates:
            print(f"Unknown candidate label for {args.button}: {args.send_candidate}")
            return 2
    log_path = Path(args.log)
    map_path = Path(args.output_map)
    suffix = b"" if args.expand_line_endings else serial_line_ending(args.line_ending)

    print(safety_prompt(args.button))
    line_ending_note = "expanded into candidate payloads" if args.expand_line_endings else args.line_ending
    print(f"Port={args.port}, baud={args.baud}, line-ending={line_ending_note}")
    print(f"Log={log_path.resolve()}")
    print(f"Output map={map_path.resolve()}")
    print()

    with open_serial(args) as ser:
        for index, candidate in enumerate(candidates, 1):
            print(f"[{index}/{len(candidates)}] {candidate.label}")
            print(f"  payload: {candidate.payload.hex(' ')}")
            print(f"  rationale: {candidate.rationale}")
            if args.send_candidate:
                answer = ""
            else:
                answer = input("Press Enter to send, s=skip, q=quit: ").strip().lower()
                if answer == "q":
                    break
                if answer == "s":
                    write_jsonl(log_path, {"button": args.button, "label": candidate.label, "result": "skipped"})
                    continue
            payload = candidate.payload + suffix
            ser.write(payload)
            ser.flush()
            time.sleep(args.step_delay)
            response = read_for(ser, args.read_after)
            print(f"  sent: {payload.hex(' ')}")
            if response:
                print(f"  read: {response.hex(' ')} | {response.decode('utf-8', errors='replace')!r}")
            if args.send_candidate:
                result = "sent_unobserved"
            else:
                result = input("Observed result? y=correct, n=no change/wrong, q=quit: ").strip().lower()
            row = {
                "button": args.button,
                "label": candidate.label,
                "payload_hex": candidate.payload.hex(" "),
                "sent_hex": payload.hex(" "),
                "read_hex": response.hex(" "),
                "result": result,
                "timestamp": time.time(),
            }
            write_jsonl(log_path, row)
            if args.send_candidate:
                print("Candidate was sent once. Observe the CBOX panel and record the result manually.")
                break
            if result == "y":
                write_map(map_path, args.button, candidate.payload)
                print(f"Recorded {args.button} -> hex:{candidate.payload.hex(' ')} in {map_path}")
                break
            if result == "q":
                break
            print("Restore the requested safe display state before the next candidate.")
            input("Press Enter after the CBOX display is safe for the next candidate...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
