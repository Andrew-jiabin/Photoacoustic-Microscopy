"""
Realtime keyboard control for the open-loop MAX312D NanoMax probe stage.

Arrow keys control MDT693B voltages:
    Up/Down    -> Z axis +/- z_step_v
    Right/Left -> Y axis +/- y_step_v

The MDT693B is open-loop: these are voltage targets, not measured positions.
All commanded voltages are clamped to [0, safe_max_voltage].
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as _dt
import os
import shutil
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_MDT_SERIAL_NO = "2201287140-09"
DEFAULT_SAFE_MAX_VOLTAGE = 75.0
DEFAULT_PIEZO_TRAVEL_UM = 20.0
DEFAULT_PIEZO_TRAVEL_VOLTAGE = 75.0
DEFAULT_STEP_V = 1.0
DEFAULT_SAMPLE_INTERVAL_S = 0.25
DEFAULT_LOG_PATH = REPO_ROOT / "run_logs" / "nanomax_open_loop_realtime_control.log"

KEY_ARROW_PREFIXES = ("\x00", "\xe0")
KEY_UP = "H"
KEY_DOWN = "P"
KEY_LEFT = "K"
KEY_RIGHT = "M"
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def clamp(value: float, low: float, high: float) -> tuple[float, bool]:
    clamped = max(low, min(high, float(value)))
    return clamped, abs(clamped - float(value)) > 1e-9


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Realtime arrow-key voltage control for the open-loop MAX312D/MDT693B probe stage."
    )
    parser.add_argument("--serial", default=DEFAULT_MDT_SERIAL_NO, help="MDT693B serial number.")
    parser.add_argument("--serial-port", default=None, help="Override the detected MDT serial port, e.g. COM7.")
    parser.add_argument("--safe-max-voltage", type=float, default=DEFAULT_SAFE_MAX_VOLTAGE)
    parser.add_argument("--piezo-travel-um", type=float, default=DEFAULT_PIEZO_TRAVEL_UM)
    parser.add_argument("--piezo-travel-voltage", type=float, default=DEFAULT_PIEZO_TRAVEL_VOLTAGE)
    parser.add_argument("--y-step-v", type=float, default=DEFAULT_STEP_V)
    parser.add_argument("--z-step-v", type=float, default=DEFAULT_STEP_V)
    parser.add_argument("--sample-interval-s", type=float, default=DEFAULT_SAMPLE_INTERVAL_S)
    parser.add_argument("--settle-ms", type=int, default=80)
    parser.add_argument("--backend", default="serial", choices=("serial", "auto", "dll"))
    parser.add_argument("--yes", action="store_true", help="Skip the RUN confirmation prompt.")
    parser.add_argument("--status-only", action="store_true", help="Connect, print current status, and exit.")
    parser.add_argument("--no-set-axis-max", action="store_true", help="Do not set MDT YMAX/ZMAX to safe max.")
    parser.add_argument("--return-yz-zero-on-exit", action="store_true", help="Return Y/Z to 0 V when quitting.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Text log path.")
    return parser.parse_args()


def ensure_windows_keyboard() -> None:
    if os.name != "nt":
        raise SystemExit("Realtime arrow-key control currently requires a Windows console with msvcrt.")


def validate_positive(name: str, value: float) -> float:
    value = float(value)
    if value <= 0:
        raise SystemExit(f"{name} must be positive, got {value}")
    return value


class RealtimeProbeControl:
    def __init__(self, stage, args: argparse.Namespace):
        self.stage = stage
        self.safe_max_voltage = float(args.safe_max_voltage)
        self.piezo_travel_um = float(args.piezo_travel_um)
        self.piezo_travel_voltage = float(args.piezo_travel_voltage)
        self.y_step_v = float(args.y_step_v)
        self.z_step_v = float(args.z_step_v)
        self.sample_interval_s = float(args.sample_interval_s)
        self.settle_ms = int(args.settle_ms)
        self.log_path = Path(args.log_path)
        self.last_xyz = [float(value) for value in stage.get_voltage_xyz()]
        self.message = "Ready"

    def log(self, event: str, **fields) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            parts = [f"ts={now_iso()}", f"event={event}"]
            for key, value in fields.items():
                text = str(value).replace("\n", "\\n").replace("\r", "\\r").replace("|", "/")
                parts.append(f"{key}={text}")
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(" | ".join(parts) + "\n")
        except Exception:
            pass

    def refresh(self) -> list[float]:
        self.last_xyz = [float(value) for value in self.stage.get_voltage_xyz()]
        return self.last_xyz

    def status_line(self, refresh: bool = False) -> str:
        x, y, z = self.refresh() if refresh else self.last_xyz
        y_um = self.voltage_to_um(y)
        z_um = self.voltage_to_um(z)
        y_step_um = self.voltage_to_um(self.y_step_v)
        z_step_um = self.voltage_to_um(self.z_step_v)
        return (
            f"X={x:7.3f} V | "
            f"Y={y:7.3f} V ~= {y_um:6.2f} um, Z={z:7.3f} V ~= {z_um:6.2f} um | "
            f"Y step={self.y_step_v:g} V ~= {y_step_um:g} um, Z step={self.z_step_v:g} V ~= {z_step_um:g} um | "
            f"interval={self.sample_interval_s:g} s | safe=[0,{self.safe_max_voltage:g}] V"
        )

    def print_status(self) -> None:
        print(self.status_line(refresh=True), flush=True)

    def voltage_to_um(self, voltage: float) -> float:
        return float(voltage) / self.piezo_travel_voltage * self.piezo_travel_um

    def set_yz(self, y: float | None = None, z: float | None = None, reason: str = "manual") -> None:
        _, current_y, current_z = self.last_xyz
        target_y = current_y if y is None else float(y)
        target_z = current_z if z is None else float(z)
        target_y, y_clamped = clamp(target_y, 0.0, self.safe_max_voltage)
        target_z, z_clamped = clamp(target_z, 0.0, self.safe_max_voltage)
        if abs(target_y - current_y) > 1e-9:
            self.stage.set_voltage_axis("y", target_y)
        if abs(target_z - current_z) > 1e-9:
            self.stage.set_voltage_axis("z", target_z)
        if self.settle_ms > 0:
            time.sleep(self.settle_ms / 1000.0)
        x, read_y, read_z = self.refresh()
        self.log(
            "MOVE_YZ",
            reason=reason,
            target_y_v=f"{target_y:.6f}",
            target_z_v=f"{target_z:.6f}",
            read_x_v=f"{x:.6f}",
            read_y_v=f"{read_y:.6f}",
            read_z_v=f"{read_z:.6f}",
            y_clamped=y_clamped,
            z_clamped=z_clamped,
        )
        suffix = " (clamped)" if y_clamped or z_clamped else ""
        self.message = (
            f"Moved: Y={read_y:.3f} V ~= {self.voltage_to_um(read_y):.2f} um, "
            f"Z={read_z:.3f} V ~= {self.voltage_to_um(read_z):.2f} um{suffix}"
        )
        return self.message

    def move_delta(self, y_delta: float = 0.0, z_delta: float = 0.0, reason: str = "key") -> None:
        _, current_y, current_z = self.last_xyz
        self.set_yz(current_y + float(y_delta), current_z + float(z_delta), reason=reason)

    def set_safe_max(self, value: float) -> None:
        value = validate_positive("safe max voltage", value)
        device_limit = self.stage.limit_voltage
        if device_limit is not None and value > float(device_limit):
            raise ValueError(f"safe max {value} V exceeds MDT device limit {device_limit} V")
        self.safe_max_voltage = value
        self.stage.safe_max_voltage = value
        for axis in ("y", "z"):
            try:
                self.stage.set_axis_max_voltage(axis, value)
            except Exception as exc:
                self.message = f"Warning: failed to set {axis.upper()}MAX={value:g} V: {exc}"
        self.log("SAFE_MAX_SET", safe_max_voltage=value)
        self.message = f"Safe max voltage set to {value:g} V"

    def execute_command(self, line: str) -> bool:
        tokens = line.strip().split()
        if not tokens:
            return True
        cmd = tokens[0].lower()
        try:
            if cmd in ("q", "quit", "exit"):
                return False
            if cmd in ("h", "help", "?"):
                self.message = "Help is shown above"
            elif cmd in ("s", "status"):
                self.message = "Status refreshed"
            elif cmd in ("zero", "home", "0"):
                self.set_yz(0.0, 0.0, reason="command_zero")
            elif cmd == "step" and len(tokens) == 2:
                value = validate_positive("step", float(tokens[1]))
                self.y_step_v = value
                self.z_step_v = value
                self.log("STEP_SET", y_step_v=value, z_step_v=value)
                self.message = f"Y/Z step set to {value:g} V"
            elif cmd == "ystep" and len(tokens) == 2:
                self.y_step_v = validate_positive("ystep", float(tokens[1]))
                self.log("YSTEP_SET", y_step_v=self.y_step_v)
                self.message = f"Y step set to {self.y_step_v:g} V"
            elif cmd == "zstep" and len(tokens) == 2:
                self.z_step_v = validate_positive("zstep", float(tokens[1]))
                self.log("ZSTEP_SET", z_step_v=self.z_step_v)
                self.message = f"Z step set to {self.z_step_v:g} V"
            elif cmd in ("interval", "dt") and len(tokens) == 2:
                self.sample_interval_s = validate_positive("sample interval", float(tokens[1]))
                self.log("INTERVAL_SET", sample_interval_s=self.sample_interval_s)
                self.message = f"Sample interval set to {self.sample_interval_s:g} s"
            elif cmd in ("max", "safe", "limit") and len(tokens) == 2:
                self.set_safe_max(float(tokens[1]))
            elif cmd == "set" and len(tokens) >= 3:
                self._execute_set(tokens[1:])
            else:
                self.message = f"Unknown command: {line!r}. Type h for help."
        except Exception as exc:
            self.log("COMMAND_ERROR", command=line, error=repr(exc))
            self.message = f"Command failed: {exc}"
        return True

    def _execute_set(self, tokens: list[str]) -> None:
        target = tokens[0].lower()
        if target == "y" and len(tokens) == 2:
            self.set_yz(y=float(tokens[1]), reason="command_set_y")
        elif target == "z" and len(tokens) == 2:
            self.set_yz(z=float(tokens[1]), reason="command_set_z")
        elif target == "yz" and len(tokens) == 3:
            self.set_yz(y=float(tokens[1]), z=float(tokens[2]), reason="command_set_yz")
        else:
            raise ValueError("Use: set y <V>, set z <V>, or set yz <Y_V> <Z_V>")


HELP_TEXT = """
Hotkeys:
  Up / Down       Z += z_step / Z -= z_step
  Right / Left    Y += y_step / Y -= y_step
  s               print current status
  h               print this help
  0 or r          set Y/Z to 0 V
  + / -           double / halve both Y and Z steps
  :               command mode
  q               quit and close the controller

Commands after ':' then Enter:
  step <V>        set both Y/Z step sizes
  ystep <V>       set Y step
  zstep <V>       set Z step
  interval <sec>  set keyboard sampling interval
  max <V>         set program safe upper limit and MDT YMAX/ZMAX
  set y <V>       move Y to absolute voltage
  set z <V>       move Z to absolute voltage
  set yz <Y> <Z>  move Y/Z to absolute voltages
  zero            set Y/Z to 0 V
  status          print current status
  quit            exit

Position display:
  Open-loop estimate only: um ~= voltage / 75 V * 20 um.
""".strip()


def clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def print_help() -> None:
    print(HELP_TEXT, flush=True)


def render_dashboard(control: RealtimeProbeControl, message: str | None = None) -> None:
    clear_screen()
    print("Realtime open-loop MAX312D/MDT693B Y/Z control")
    print("=" * 62)
    print_help()
    print("=" * 62)
    print("Status:")
    update_status_line(control, message=message, refresh=True)


def update_status_line(control: RealtimeProbeControl, message: str | None = None, refresh: bool = False) -> None:
    text = control.status_line(refresh=refresh)
    msg = control.message if message is None else message
    if msg:
        text = f"{text} | {msg}"
    width = max(80, shutil.get_terminal_size((120, 24)).columns - 1)
    if len(text) > width:
        text = text[: width - 3] + "..."
    sys.stdout.write("\r" + text.ljust(width))
    sys.stdout.flush()


def read_key(msvcrt_module):
    ch = msvcrt_module.getwch()
    if ch in KEY_ARROW_PREFIXES:
        return ("arrow", msvcrt_module.getwch())
    return ("char", ch)


def drain_keyboard_buffer(msvcrt_module):
    while msvcrt_module.kbhit():
        read_key(msvcrt_module)


def read_last_command_key(msvcrt_module):
    command = None
    while msvcrt_module.kbhit():
        kind, value = read_key(msvcrt_module)
        if kind == "char":
            command = value
    return command


def is_key_down(vk_code: int) -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)


def sample_arrow_delta(control: RealtimeProbeControl) -> tuple[float, float, str]:
    y_delta = 0.0
    z_delta = 0.0
    reasons = []

    up = is_key_down(VK_UP)
    down = is_key_down(VK_DOWN)
    right = is_key_down(VK_RIGHT)
    left = is_key_down(VK_LEFT)

    if up and not down:
        z_delta += control.z_step_v
        reasons.append("z_plus")
    elif down and not up:
        z_delta -= control.z_step_v
        reasons.append("z_minus")

    if right and not left:
        y_delta += control.y_step_v
        reasons.append("y_plus")
    elif left and not right:
        y_delta -= control.y_step_v
        reasons.append("y_minus")

    return y_delta, z_delta, "+".join(reasons) if reasons else "idle"


def main() -> None:
    args = parse_args()
    ensure_windows_keyboard()
    args.safe_max_voltage = validate_positive("safe max voltage", args.safe_max_voltage)
    args.piezo_travel_um = validate_positive("piezo travel", args.piezo_travel_um)
    args.piezo_travel_voltage = validate_positive("piezo travel voltage", args.piezo_travel_voltage)
    args.y_step_v = validate_positive("Y step", args.y_step_v)
    args.z_step_v = validate_positive("Z step", args.z_step_v)
    args.sample_interval_s = validate_positive("sample interval", args.sample_interval_s)

    from Alazar_imaging.MDT693BController import MDT693BController

    stage = MDT693BController(
        serial_no=args.serial,
        serial_port=args.serial_port,
        safe_max_voltage=args.safe_max_voltage,
        backend=args.backend,
    )
    control = RealtimeProbeControl(stage, args)

    try:
        if not args.no_set_axis_max:
            control.set_safe_max(args.safe_max_voltage)

        if args.status_only:
            print("Realtime open-loop MAX312D/MDT693B Y/Z control")
            control.print_status()
            return

        print("Realtime open-loop MAX312D/MDT693B Y/Z control")
        control.print_status()
        print_help()
        if not args.yes:
            answer = input("This enables live voltage control. Type RUN to continue: ").strip()
            if answer != "RUN":
                print("Cancelled; no live control started.")
                return

        import msvcrt

        control.log(
            "REALTIME_START",
            y_step_v=control.y_step_v,
            z_step_v=control.z_step_v,
            sample_interval_s=control.sample_interval_s,
            safe_max_voltage=control.safe_max_voltage,
        )
        render_dashboard(control, message="Live control started")
        drain_keyboard_buffer(msvcrt)
        running = True
        while running:
            char = read_last_command_key(msvcrt)
            if char:
                char = char.lower()
                if char == "q":
                    running = False
                    break
                if char in ("h", "?"):
                    control.message = "Help is shown above"
                    render_dashboard(control)
                elif char == "s":
                    control.message = "Status refreshed"
                    update_status_line(control, refresh=True)
                elif char in ("0", "r"):
                    control.set_yz(0.0, 0.0, reason="hotkey_zero")
                    drain_keyboard_buffer(msvcrt)
                    update_status_line(control)
                elif char == "+":
                    control.execute_command(f"step {control.y_step_v * 2.0}")
                    update_status_line(control)
                elif char == "-":
                    control.execute_command(f"step {max(control.y_step_v / 2.0, 1e-6)}")
                    update_status_line(control)
                elif char == ":":
                    sys.stdout.write("\ncmd> ")
                    sys.stdout.flush()
                    line = input()
                    running = control.execute_command(line)
                    render_dashboard(control)

            if running:
                y_delta, z_delta, reason = sample_arrow_delta(control)
                if y_delta or z_delta:
                    control.move_delta(y_delta=y_delta, z_delta=z_delta, reason=f"key_state_{reason}")
                    drain_keyboard_buffer(msvcrt)
                    update_status_line(control)
                else:
                    update_status_line(control)
            time.sleep(control.sample_interval_s)
    finally:
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
            if args.return_yz_zero_on_exit and not args.status_only:
                print("Returning Y/Z to 0 V before exit.", flush=True)
                control.set_yz(0.0, 0.0, reason="exit_zero")
            control.log("REALTIME_END", status=control.status_line())
        finally:
            stage.close()
            print("MDT693B controller closed.", flush=True)


if __name__ == "__main__":
    main()
