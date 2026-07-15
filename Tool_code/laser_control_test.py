#!/usr/bin/env python3
"""Conservative test utility for the lab TOPTICA DLC pro and legacy LUCE serial paths.

Default TOPTICA actions are read-only. Any command that changes a controller
parameter requires both --write and --confirm-write LASER_RISK_ACCEPTED.

For the current 532 nm CBOX-Micro controller, use cbox_micro_test.py instead.
This legacy LUCE branch only lists ports, opens/reads passively, or sends a
user-provided command after the same explicit write confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Iterable


DEFAULT_TOPTICA_HOST = "192.168.1.11"
DEFAULT_TOPTICA_COMMAND_PORT = 1998
WRITE_CONFIRM_TOKEN = "LASER_RISK_ACCEPTED"


@dataclass(frozen=True)
class TopticaControl:
    key: str
    label: str
    param: str
    source: str
    meaning_true: str
    meaning_false: str
    notes: str = ""


TOPTICA_CONTROLS: tuple[TopticaControl, ...] = (
    TopticaControl(
        key="cc",
        label="CC - Current Control Enable",
        param="laser1:dl:cc:enabled",
        source="DLCpro-Command-Reference.pdf page 50",
        meaning_true="laser emission on",
        meaning_false="laser emission off",
        notes="This is an emission-affecting setting.",
    ),
    TopticaControl(
        key="pc",
        label="PC - Piezo Control Enable",
        param="laser1:dl:pc:enabled",
        source="DLCpro-Command-Reference.pdf page 60",
        meaning_true="piezo HV output enabled",
        meaning_false="piezo HV output disabled",
    ),
    TopticaControl(
        key="scan",
        label="SC - Scan Control Enable",
        param="laser1:scan:enabled",
        source="DLCpro-Command-Reference.pdf page 118",
        meaning_true="scan signal generator enabled",
        meaning_false="scan signal generator disabled",
    ),
    TopticaControl(
        key="pc_external",
        label="PC Analog Remote Control Enable",
        param="laser1:dl:pc:external-input:enabled",
        source="DLCpro-Command-Reference.pdf pages 52 and 60; live read-only probe",
        meaning_true="piezo voltage controlled by external analog input",
        meaning_false="piezo external analog input disabled",
        notes="This matches the highlighted button under the PC - Piezo Control panel.",
    ),
    TopticaControl(
        key="ctl_remote",
        label="CTL wavelength Analog Remote Control Enable",
        param="laser1:ctl:remote-control:enabled",
        source="DLCpro-Command-Reference.pdf page 99",
        meaning_true="CTL wavelength controlled by analog remote input",
        meaning_false="CTL analog wavelength remote control disabled",
        notes="Not the highlighted PC-panel button, but similarly named in the CTL command tree.",
    ),
)


TOPTICA_STATUS_PARAMS: tuple[str, ...] = (
    "serial-number",
    "system-model",
    "emission",
    "interlock-open",
    "frontkey-locked",
    "laser1:product-name",
    "laser1:emission",
    "laser1:dl:cc:enabled",
    "laser1:dl:cc:current-set",
    "laser1:dl:cc:current-act",
    "laser1:dl:cc:external-input:enabled",
    "laser1:dl:cc:external-input:signal",
    "laser1:dl:cc:external-input:factor",
    "laser1:dl:pc:enabled",
    "laser1:dl:pc:voltage-set",
    "laser1:dl:pc:voltage-act",
    "laser1:dl:pc:voltage-min",
    "laser1:dl:pc:voltage-max",
    "laser1:dl:pc:external-input:enabled",
    "laser1:dl:pc:external-input:signal",
    "laser1:dl:pc:external-input:factor",
    "laser1:scan:enabled",
    "laser1:scan:hold",
    "laser1:scan:signal-type",
    "laser1:scan:frequency",
    "laser1:scan:amplitude",
    "laser1:scan:offset",
    "laser1:scan:start",
    "laser1:scan:end",
    "laser1:scan:output-channel",
    "laser1:scan:unit",
    "laser1:ctl:remote-control:enabled",
    "laser1:ctl:remote-control:signal",
    "laser1:ctl:remote-control:factor",
)


TOPTICA_CONNECT_TEST_PARAMS: tuple[str, ...] = (
    "serial-number",
    "system-model",
    "emission",
    "interlock-open",
    "laser1:product-name",
    "laser1:emission",
)


BOOLEAN_SETTERS: dict[str, str] = {control.key: control.param for control in TOPTICA_CONTROLS}
BOOLEAN_SETTERS.update(
    {
        "emission_button": "emission-button-enabled",
        "scan_hold": "laser1:scan:hold",
        "pc_voltage_dither": "laser1:dl:pc:voltage-set-dithering",
        "cc_external": "laser1:dl:cc:external-input:enabled",
    }
)


NUMERIC_SETTERS: dict[str, str] = {
    "pc_voltage": "laser1:dl:pc:voltage-set",
    "pc_external_signal": "laser1:dl:pc:external-input:signal",
    "pc_external_factor": "laser1:dl:pc:external-input:factor",
    "ctl_remote_signal": "laser1:ctl:remote-control:signal",
    "ctl_remote_factor": "laser1:ctl:remote-control:factor",
    "scan_signal_type": "laser1:scan:signal-type",
    "scan_frequency": "laser1:scan:frequency",
    "scan_amplitude": "laser1:scan:amplitude",
    "scan_offset": "laser1:scan:offset",
    "scan_start": "laser1:scan:start",
    "scan_end": "laser1:scan:end",
    "scan_output_channel": "laser1:scan:output-channel",
}


class DlcProClient:
    """Small DLC pro command-port client.

    It intentionally implements only plain command-line request/response access.
    """

    def __init__(
        self,
        host: str,
        port: int,
        timeout: float,
        ssh_host: str | None = None,
        ssh_user: str | None = None,
        ssh_password_env: str | None = None,
        ssh_port: int = 22,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_password_env = ssh_password_env
        self.ssh_port = ssh_port
        self._sock = None
        self._ssh_client = None

    def __enter__(self) -> "DlcProClient":
        if self.ssh_host:
            self._connect_via_ssh()
        else:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        self.read_until_prompt()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        try:
            if self._sock is not None:
                self._sock.close()
        finally:
            if self._ssh_client is not None:
                self._ssh_client.close()

    def _connect_via_ssh(self) -> None:
        try:
            import paramiko  # type: ignore
        except ImportError as exc:
            raise RuntimeError("SSH tunnel mode requires: python -m pip install paramiko") from exc
        if not self.ssh_user:
            raise ValueError("--ssh-user is required with --ssh-host")
        password = os.environ.get(self.ssh_password_env or "")
        if self.ssh_password_env and password is None:
            raise ValueError(f"Environment variable {self.ssh_password_env!r} is not set")
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            self.ssh_host,
            port=self.ssh_port,
            username=self.ssh_user,
            password=password,
            timeout=self.timeout,
            banner_timeout=self.timeout,
            auth_timeout=self.timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        channel = client.get_transport().open_channel(  # type: ignore[union-attr]
            "direct-tcpip",
            (self.host, self.port),
            ("127.0.0.1", 0),
            timeout=self.timeout,
        )
        channel.settimeout(self.timeout)
        self._ssh_client = client
        self._sock = channel

    def _recv(self, size: int) -> bytes:
        if self._sock is None:
            raise RuntimeError("Client is not connected")
        return self._sock.recv(size)

    def _sendall(self, data: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("Client is not connected")
        self._sock.sendall(data)

    def read_until_prompt(self) -> str:
        chunks: list[bytes] = []
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                data = self._recv(4096)
            except TimeoutError:
                break
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            joined = b"".join(chunks)
            if re.search(br"(?m)^>\s*$", joined) or joined.rstrip().endswith(b">"):
                break
        return _clean_dlc_text(b"".join(chunks))

    def command(self, command_text: str) -> str:
        self._sendall(command_text.encode("utf-8") + b"\n")
        return self.read_until_prompt()

    def param_ref(self, name: str) -> str:
        return parse_dlc_response(self.command(f"(param-ref '{name})"))

    def param_set(self, name: str, value: str) -> str:
        return parse_dlc_response(self.command(f"(param-set! '{name} {value})"))

    def exec_command(self, name: str, args: Iterable[str] = ()) -> str:
        joined = " ".join(args)
        suffix = f" {joined}" if joined else ""
        return parse_dlc_response(self.command(f"(exec '{name}{suffix})"))

    def param_disp(self, name: str) -> str:
        return parse_dlc_response(self.command(f"(param-disp '{name})"))


def _clean_dlc_text(data: bytes) -> str:
    # Some DLC pro command ports start with telnet negotiation bytes. They are
    # irrelevant for this line-oriented use case.
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\x00", "")
    return text


def parse_dlc_response(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == ">":
            continue
        if line.startswith(">"):
            line = line[1:].strip()
            if not line:
                continue
        lines.append(line)
    return "\n".join(lines).strip()


def bool_value(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "#t", "on", "enable", "enabled", "yes"}:
        return "#t"
    if normalized in {"0", "false", "f", "#f", "off", "disable", "disabled", "no"}:
        return "#f"
    raise argparse.ArgumentTypeError(f"Expected boolean/on/off value, got {value!r}")


def dlc_value(value: str) -> str:
    if value.startswith("raw:"):
        return value[4:]
    try:
        bool_token = bool_value(value)
    except argparse.ArgumentTypeError:
        pass
    else:
        return bool_token
    try:
        float(value)
        return value
    except ValueError:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'


def render_toptica_map() -> str:
    rows = []
    for control in TOPTICA_CONTROLS:
        rows.append(
            {
                "key": control.key,
                "label": control.label,
                "param": control.param,
                "true": control.meaning_true,
                "false": control.meaning_false,
                "source": control.source,
                "notes": control.notes,
            }
        )
    return json.dumps(rows, indent=2)


def connect_dlc(args: argparse.Namespace) -> DlcProClient:
    return DlcProClient(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_password_env=args.ssh_password_env,
        ssh_port=args.ssh_port,
    )


def collect_writes(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    writes: list[tuple[str, str, str]] = []
    for key, param in BOOLEAN_SETTERS.items():
        value = getattr(args, f"set_{key}", None)
        if value is not None:
            writes.append((param, bool_value(value), f"--set-{key.replace('_', '-')}"))
    for key, param in NUMERIC_SETTERS.items():
        value = getattr(args, f"set_{key}", None)
        if value is not None:
            writes.append((param, dlc_value(value), f"--set-{key.replace('_', '-')}"))
    for item in args.set_param or []:
        if "=" not in item:
            raise ValueError("--set-param must use NAME=VALUE")
        name, value = item.split("=", 1)
        writes.append((name.strip(), dlc_value(value.strip()), "--set-param"))
    if args.safe_off:
        writes.extend(
            [
                ("laser1:scan:enabled", "#f", "--safe-off"),
                ("laser1:dl:pc:external-input:enabled", "#f", "--safe-off"),
                ("laser1:dl:pc:enabled", "#f", "--safe-off"),
                ("laser1:dl:cc:enabled", "#f", "--safe-off"),
            ]
        )
    return writes


def handle_toptica(args: argparse.Namespace) -> int:
    if args.map:
        print(render_toptica_map())

    writes = collect_writes(args)
    read_params = list(TOPTICA_STATUS_PARAMS if args.status else ())
    if args.connect_test:
        read_params.extend(TOPTICA_CONNECT_TEST_PARAMS)
    read_params.extend(args.read or [])
    disp_sections = args.disp or []
    exec_commands = args.exec or []

    if writes:
        print("Planned TOPTICA writes:")
        for param, value, origin in writes:
            print(f"  {origin}: (param-set! '{param} {value})")
        if not (args.write and args.confirm_write == WRITE_CONFIRM_TOKEN):
            print()
            print("No write was sent. To execute, add:")
            print(f"  --write --confirm-write {WRITE_CONFIRM_TOKEN}")
            if not read_params and not disp_sections and not exec_commands:
                return 0

    if not read_params and not disp_sections and not exec_commands and not writes and not args.map:
        read_params = list(TOPTICA_STATUS_PARAMS)

    if not read_params and not disp_sections and not exec_commands and not (writes and args.write):
        return 0

    with connect_dlc(args) as client:
        if args.connect_test:
            print("TOPTICA_CONNECT_OK")
        if read_params:
            print("TOPTICA read-only status:")
            for param in read_params:
                try:
                    value = client.param_ref(param)
                except Exception as exc:  # noqa: BLE001
                    value = f"ERROR: {exc}"
                print(f"  {param} = {value}")
        for section in disp_sections:
            print(f"TOPTICA param-disp {section}:")
            print(client.param_disp(section))
        for command_name in exec_commands:
            print(f"TOPTICA exec {command_name}:")
            print(client.exec_command(command_name))
        if writes and args.write and args.confirm_write == WRITE_CONFIRM_TOKEN:
            print("Executing TOPTICA writes:")
            for param, value, origin in writes:
                response = client.param_set(param, value)
                print(f"  {origin}: {param} <- {value}; response: {response or '(empty)'}")
                if args.verify_after_write:
                    verify = client.param_ref(param)
                    print(f"    verify: {param} = {verify}")
    return 0


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
            "Get-CimInstance Win32_SerialPort | "
            "Select-Object DeviceID,Name,PNPDeviceID | ConvertTo-Json -Compress"
        ),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
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
    ports = []
    for item in payload:
        ports.append(
            {
                "device": str(item.get("DeviceID", "")),
                "description": str(item.get("Name", "")),
                "hwid": str(item.get("PNPDeviceID", "")),
            }
        )
    return ports


def serial_line_ending(name: str) -> bytes:
    return {
        "none": b"",
        "lf": b"\n",
        "cr": b"\r",
        "crlf": b"\r\n",
    }[name]


def handle_luce(args: argparse.Namespace) -> int:
    if args.list_ports:
        ports = list_serial_ports()
        print(json.dumps(ports, indent=2))
        if not ports:
            print("No ports found by pyserial or Windows CIM fallback.")
        return 0

    if not args.port:
        print("LUCE serial mode needs --port, or use --list-ports first.", file=sys.stderr)
        return 2

    try:
        import serial  # type: ignore
    except ImportError:
        print("LUCE serial mode requires pyserial: python -m pip install pyserial", file=sys.stderr)
        return 2

    if (args.send is not None or args.send_hex is not None) and not (
        args.write and args.confirm_write == WRITE_CONFIRM_TOKEN
    ):
        print("No LUCE serial write was sent. To execute user-provided bytes, add:")
        print(f"  --write --confirm-write {WRITE_CONFIRM_TOKEN}")
        return 0

    with serial.Serial(args.port, args.baud, timeout=args.timeout) as ser:
        print(f"Opened {args.port} at {args.baud} baud.")
        if args.open_read > 0:
            data = read_serial_for(ser, args.open_read)
            print_serial_payload("Passive read", data)
        payload = b""
        if args.send is not None:
            payload = args.send.encode(args.encoding) + serial_line_ending(args.line_ending)
        elif args.send_hex is not None:
            payload = bytes.fromhex(args.send_hex) + serial_line_ending(args.line_ending)
        if payload:
            ser.write(payload)
            ser.flush()
            print_serial_payload("Sent", payload)
            data = read_serial_for(ser, args.read_after)
            print_serial_payload("Read after send", data)
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


def add_bool_setter(parser: argparse.ArgumentParser, key: str, help_text: str) -> None:
    parser.add_argument(
        f"--set-{key.replace('_', '-')}",
        dest=f"set_{key}",
        metavar="on|off",
        help=help_text,
    )


def add_numeric_setter(parser: argparse.ArgumentParser, key: str, help_text: str) -> None:
    parser.add_argument(
        f"--set-{key.replace('_', '-')}",
        dest=f"set_{key}",
        metavar="VALUE",
        help=help_text,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="target", required=True)

    top = subparsers.add_parser("toptica", help="TOPTICA DLC pro TCP command-port tests")
    top.add_argument("--host", default=DEFAULT_TOPTICA_HOST)
    top.add_argument("--port", type=int, default=DEFAULT_TOPTICA_COMMAND_PORT)
    top.add_argument("--timeout", type=float, default=5.0)
    top.add_argument("--ssh-host", help="Optional SSH jump host for reaching the laser LAN.")
    top.add_argument("--ssh-port", type=int, default=22)
    top.add_argument("--ssh-user", help="SSH username for --ssh-host.")
    top.add_argument(
        "--ssh-password-env",
        help="Environment variable containing the SSH password. Do not put passwords in commands.",
    )
    top.add_argument("--map", action="store_true", help="Print screenshot button to command mapping.")
    top.add_argument("--connect-test", action="store_true", help="Actually connect to the command port and read minimal status.")
    top.add_argument("--status", action="store_true", help="Read a broad read-only status snapshot.")
    top.add_argument("--read", action="append", help="Read an additional parameter by name.")
    top.add_argument("--disp", action="append", help="Run read-only param-disp on a section.")
    top.add_argument("--exec", action="append", help="Run a DLC pro exec command. Avoid unsafe commands.")
    top.add_argument("--set-param", action="append", help="Generic write as NAME=VALUE. Prefix VALUE with raw: to bypass quoting.")
    top.add_argument("--safe-off", action="store_true", help="Plan/write scan off, PC external off, PC off, CC off.")
    top.add_argument("--write", action="store_true", help="Actually send planned write commands.")
    top.add_argument("--confirm-write", help=f"Must equal {WRITE_CONFIRM_TOKEN} for writes.")
    top.add_argument("--no-verify-after-write", dest="verify_after_write", action="store_false")
    top.set_defaults(verify_after_write=True, func=handle_toptica)

    add_bool_setter(top, "cc", "Set laser1:dl:cc:enabled. This affects emission.")
    add_bool_setter(top, "pc", "Set laser1:dl:pc:enabled.")
    add_bool_setter(top, "scan", "Set laser1:scan:enabled.")
    add_bool_setter(top, "pc_external", "Set laser1:dl:pc:external-input:enabled.")
    add_bool_setter(top, "ctl_remote", "Set laser1:ctl:remote-control:enabled.")
    add_bool_setter(top, "emission_button", "Set emission-button-enabled.")
    add_bool_setter(top, "scan_hold", "Set laser1:scan:hold.")
    add_bool_setter(top, "pc_voltage_dither", "Set laser1:dl:pc:voltage-set-dithering.")
    add_bool_setter(top, "cc_external", "Set laser1:dl:cc:external-input:enabled.")

    add_numeric_setter(top, "pc_voltage", "Set laser1:dl:pc:voltage-set in V.")
    add_numeric_setter(top, "pc_external_signal", "Set laser1:dl:pc:external-input:signal.")
    add_numeric_setter(top, "pc_external_factor", "Set laser1:dl:pc:external-input:factor.")
    add_numeric_setter(top, "ctl_remote_signal", "Set laser1:ctl:remote-control:signal.")
    add_numeric_setter(top, "ctl_remote_factor", "Set laser1:ctl:remote-control:factor.")
    add_numeric_setter(top, "scan_signal_type", "Set laser1:scan:signal-type: 0 sine, 1 triangle, 2 rounded triangle.")
    add_numeric_setter(top, "scan_frequency", "Set laser1:scan:frequency in Hz.")
    add_numeric_setter(top, "scan_amplitude", "Set laser1:scan:amplitude in current scan unit.")
    add_numeric_setter(top, "scan_offset", "Set laser1:scan:offset in current scan unit.")
    add_numeric_setter(top, "scan_start", "Set laser1:scan:start in current scan unit.")
    add_numeric_setter(top, "scan_end", "Set laser1:scan:end in current scan unit.")
    add_numeric_setter(top, "scan_output_channel", "Set laser1:scan:output-channel.")

    luce = subparsers.add_parser("luce", help="Legacy LUCE USB serial discovery tests; use cbox_micro_test.py for CBOX-Micro")
    luce.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    luce.add_argument("--port", help="Serial port, for example COM7.")
    luce.add_argument("--baud", type=int, default=9600, help="Baud rate. Protocol is not confirmed; override as needed.")
    luce.add_argument("--timeout", type=float, default=0.2)
    luce.add_argument("--open-read", type=float, default=2.0, help="Seconds to read passively after opening.")
    luce.add_argument("--send", help="User-provided serial text to send. No built-in LUCE commands are assumed.")
    luce.add_argument("--send-hex", help="User-provided hex bytes to send, for example '3f 0d'.")
    luce.add_argument("--line-ending", choices=("none", "lf", "cr", "crlf"), default="crlf")
    luce.add_argument("--encoding", default="ascii")
    luce.add_argument("--read-after", type=float, default=1.0)
    luce.add_argument("--write", action="store_true", help="Actually send user-provided serial bytes.")
    luce.add_argument("--confirm-write", help=f"Must equal {WRITE_CONFIRM_TOKEN} for serial writes.")
    luce.set_defaults(func=handle_luce)
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
