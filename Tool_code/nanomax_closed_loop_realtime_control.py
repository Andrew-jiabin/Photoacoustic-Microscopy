"""
Realtime pre-alignment control for the closed-loop MAX311D/BPC303 NanoMax stage.

Hotkey mapping follows the physical directions used by PAM_Main_Nanomax.py:
    Up/Down     -> sample X in um, matching SCAN_RANGE_X_UM ("actually up")
    Left/Right  -> sample Y in um, matching SCAN_RANGE_Y_UM ("actually left")
    + / -       -> sample Z in um

This script never calls PBC_SetZero and never returns axes to zero on exit.
Quitting only closes the controller connection, so the current NanoMax position
is preserved for the following PAM_Main_Nanomax.py run.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from Nanomax.prealign_state import PREALIGN_STATE_PATH, write_prealign_state


DEFAULT_MAIN_FILE = REPO_ROOT / "PAM_Main_Nanomax.py"
DEFAULT_BPC_SERIAL_NO = "71241834"
DEFAULT_KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"
DEFAULT_SAFE_MAX_OUTPUT_VOLTAGE = 75.0
DEFAULT_PIEZO_TRAVEL_UM = 20.0
DEFAULT_MIN_STEP_UM = 0.01
DEFAULT_MANUAL_STEP_UM = 0.10
DEFAULT_SAMPLE_INTERVAL_S = 0.25
DEFAULT_LOG_PATH = REPO_ROOT / "run_logs" / "nanomax_closed_loop_realtime_control.log"

KEY_ARROW_PREFIXES = ("\x00", "\xe0")
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28

PAM_CONFIG_NAMES = {
    "SCAN_RANGE_X_UM",
    "SCAN_RANGE_Y_UM",
    "STEP_UM",
    "SAMPLE_X_DIRECTION",
    "SAMPLE_Y_DIRECTION",
    "SCAN_PATTERN",
    "BPC303_SERIAL_NO",
    "BPC303_KINESIS_DIR",
    "BPC303_AXIS_MAP",
    "BPC303_SAFE_MAX_OUTPUT_VOLTAGE",
    "SETTLE_MS",
}


@dataclass
class PamScanConfig:
    main_file: Path
    scan_range_x_um: float
    scan_range_y_um: float
    step_um: float
    sample_x_direction: float
    sample_y_direction: float
    scan_pattern: str
    bpc303_serial_no: str
    bpc303_kinesis_dir: str
    bpc303_axis_map: dict[str, int]
    bpc303_safe_max_output_voltage: float
    settle_ms: int
    scan_range_x_comment: str
    scan_range_y_comment: str


@dataclass
class ScanEvaluation:
    ok: bool
    error: str
    scan_w: int = 0
    scan_h: int = 0
    points: int = 0
    min_x: float = 0.0
    max_x: float = 0.0
    min_y: float = 0.0
    max_y: float = 0.0
    pattern_label: str = "unknown"
    overflow: str = ""


class DryRunStage:
    def __init__(self, x: float, y: float, z: float, travel_um: float):
        self.xyz = [float(x), float(y), float(z)]
        self.travel_um = float(travel_um)

    def get_position_values(self) -> list[float]:
        return list(self.xyz)

    def get_max_travel(self, axis: str) -> float:
        return self.travel_um

    def move_xyz(self, x=None, y=None, z=None, wait=False, settle_time_ms=0, tolerance=0.05, timeout_s=10.0):
        for index, value in enumerate((x, y, z)):
            if value is not None:
                self.xyz[index] = float(value)

    def close(self) -> None:
        return


class InitTracker:
    def __init__(self, enabled: bool = True, log_path: Path | None = None):
        self.enabled = bool(enabled)
        self.log_path = log_path
        self.events: list[str] = []

    def __call__(self, event: str, **fields: Any) -> None:
        compact = event
        if fields:
            visible = []
            for key in ("serial", "channel", "result", "connected", "started", "travel_um", "max_output_voltage"):
                if key in fields:
                    visible.append(f"{key}={fields[key]}")
            if visible:
                compact += " | " + ", ".join(visible)
        self.events.append(compact)
        self.events = self.events[-8:]
        self._write_log(event, fields)
        if self.enabled:
            self.render("BPC303 initialization")

    def _write_log(self, event: str, fields: dict[str, Any]) -> None:
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            parts = [f"ts={now_iso()}", f"event={event}"]
            for key, value in fields.items():
                parts.append(f"{key}={sanitize_log_value(value)}")
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(" | ".join(parts) + "\n")
        except Exception:
            pass

    def render(self, title: str) -> None:
        clear_screen()
        print("Realtime closed-loop MAX311D/BPC303 X/Y/Z pre-alignment")
        print("=" * 74)
        print(title)
        print("-" * 74)
        for line in self.events:
            print(line)
        print("-" * 74)
        print("No zeroing or position return is performed by this tool.", flush=True)


class RealtimeClosedLoopControl:
    def __init__(self, stage, args: argparse.Namespace, config: PamScanConfig):
        self.stage = stage
        self.config = config
        self.x_step_um = validate_manual_step("xstep", args.x_step_um)
        self.y_step_um = validate_manual_step("ystep", args.y_step_um)
        self.z_step_um = validate_manual_step("zstep", args.z_step_um)
        self.sample_interval_s = validate_positive("sample interval", args.sample_interval_s)
        self.settle_ms = int(args.settle_ms if args.settle_ms is not None else config.settle_ms)
        self.wait_after_move = bool(args.wait_after_move)
        self.move_tolerance_um = float(args.move_tolerance_um)
        self.move_timeout_s = float(args.move_timeout_s)
        self.log_path = Path(args.log_path)
        self.message = "Ready. Exit preserves the current NanoMax position."
        self.next_action: str | None = None
        self.last_xyz = [float(value) for value in self.stage.get_position_values()]
        self.travel_um = {axis: float(self.stage.get_max_travel(axis)) for axis in ("x", "y", "z")}

    def log(self, event: str, **fields: Any) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            parts = [f"ts={now_iso()}", f"event={event}"]
            for key, value in fields.items():
                parts.append(f"{key}={sanitize_log_value(value)}")
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(" | ".join(parts) + "\n")
        except Exception:
            pass

    def refresh(self) -> list[float]:
        self.last_xyz = [float(value) for value in self.stage.get_position_values()]
        return self.last_xyz

    def evaluate_scan(self, refresh: bool = False) -> ScanEvaluation:
        x, y, _ = self.refresh() if refresh else self.last_xyz
        return evaluate_scan_from_start(
            start_x=x,
            start_y=y,
            config=self.config,
            travel_x_um=self.travel_um["x"],
            travel_y_um=self.travel_um["y"],
        )

    def status_lines(self, refresh: bool = False) -> list[str]:
        x, y, z = self.refresh() if refresh else self.last_xyz
        evaluation = self.evaluate_scan(refresh=False)
        x_comment = f", {self.config.scan_range_x_comment}" if self.config.scan_range_x_comment else ""
        y_comment = f", {self.config.scan_range_y_comment}" if self.config.scan_range_y_comment else ""
        lines = [
            f"Position: X={x:8.4f} um (Up/Down), Y={y:8.4f} um (Left/Right), Z={z:8.4f} um (+/-)",
            f"Manual step: xstep={self.x_step_um:g} um, ystep={self.y_step_um:g} um, zstep={self.z_step_um:g} um, interval={self.sample_interval_s:g} s",
            f"PAM config: SCAN_RANGE_X_UM={self.config.scan_range_x_um:g} um{x_comment}; SCAN_RANGE_Y_UM={self.config.scan_range_y_um:g} um{y_comment}; STEP_UM={self.config.step_um:g} um",
        ]
        if evaluation.error:
            lines.append(f"Scan check: INVALID - {evaluation.error}")
        else:
            lines.append(
                f"S trajectory: pattern={evaluation.pattern_label}, shape={evaluation.scan_w} x {evaluation.scan_h}, "
                f"points={evaluation.points}, X={evaluation.min_x:.4f}..{evaluation.max_x:.4f} um, "
                f"Y={evaluation.min_y:.4f}..{evaluation.max_y:.4f} um"
            )
            if evaluation.ok:
                lines.append(
                    f"Travel check: OK inside X[0,{self.travel_um['x']:.4f}] um and Y[0,{self.travel_um['y']:.4f}] um"
                )
            else:
                lines.append(f"Travel check: OUT OF RANGE - {evaluation.overflow}")
                lines.append("Press ':' and set SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, or STEP_UM for this preview.")
        lines.append(f"Message: {self.message}")
        return lines

    def print_status(self, refresh: bool = True) -> None:
        for line in self.status_lines(refresh=refresh):
            print(line, flush=True)

    def write_prealign_marker(self) -> None:
        x, y, z = self.refresh()
        config_payload = {
            "SCAN_RANGE_X_UM": self.config.scan_range_x_um,
            "SCAN_RANGE_Y_UM": self.config.scan_range_y_um,
            "STEP_UM": self.config.step_um,
            "SCAN_PATTERN": self.config.scan_pattern,
        }
        write_prealign_state(x, y, z, config=config_payload, path=PREALIGN_STATE_PATH)
        self.log(
            "PREALIGN_MARKER_WRITTEN",
            path=PREALIGN_STATE_PATH,
            x_um=f"{x:.6f}",
            y_um=f"{y:.6f}",
            z_um=f"{z:.6f}",
            scan_range_x_um=self.config.scan_range_x_um,
            scan_range_y_um=self.config.scan_range_y_um,
            step_um=self.config.step_um,
        )

    def move_delta(self, x_delta: float = 0.0, y_delta: float = 0.0, z_delta: float = 0.0, reason: str = "key") -> None:
        x, y, z = self.last_xyz
        self.set_xyz(x=x + float(x_delta), y=y + float(y_delta), z=z + float(z_delta), reason=reason)

    def set_xyz(self, x=None, y=None, z=None, reason: str = "manual") -> None:
        current = dict(zip(("x", "y", "z"), self.last_xyz))
        requested = {"x": current["x"] if x is None else float(x), "y": current["y"] if y is None else float(y), "z": current["z"] if z is None else float(z)}
        target: dict[str, float] = {}
        clamped_axes = []
        for axis, value in requested.items():
            clamped, changed = clamp(value, 0.0, self.travel_um[axis])
            target[axis] = clamped
            if changed:
                clamped_axes.append(axis.upper())

        move_kwargs = {}
        for axis in ("x", "y", "z"):
            move_kwargs[axis] = target[axis] if abs(target[axis] - current[axis]) > 1e-9 else None
        if any(value is not None for value in move_kwargs.values()):
            self.stage.move_xyz(
                x=move_kwargs["x"],
                y=move_kwargs["y"],
                z=move_kwargs["z"],
                wait=self.wait_after_move,
                settle_time_ms=self.settle_ms,
                tolerance=self.move_tolerance_um,
                timeout_s=self.move_timeout_s,
            )
            if not self.wait_after_move and self.settle_ms > 0:
                time.sleep(self.settle_ms / 1000.0)
        read_x, read_y, read_z = self.refresh()
        self.log(
            "MOVE_XYZ",
            reason=reason,
            target_x_um=f"{target['x']:.6f}",
            target_y_um=f"{target['y']:.6f}",
            target_z_um=f"{target['z']:.6f}",
            read_x_um=f"{read_x:.6f}",
            read_y_um=f"{read_y:.6f}",
            read_z_um=f"{read_z:.6f}",
            clamped=",".join(clamped_axes) if clamped_axes else "none",
        )
        suffix = f" (clamped {','.join(clamped_axes)})" if clamped_axes else ""
        self.message = f"Moved to X={read_x:.4f} um, Y={read_y:.4f} um, Z={read_z:.4f} um{suffix}"

    def execute_command(self, line: str) -> bool:
        tokens = line.strip().split()
        if not tokens:
            self.message = "Empty command."
            return True
        cmd = tokens[0].lower()
        try:
            if cmd in ("q", "quit", "exit"):
                return False
            if cmd in ("pam", "image", "imaging", "scan", "start", "run"):
                self.next_action = "pam"
                self.message = "Starting PAM_Main_Nanomax.py after closing BPC303 control."
                self.log("PAM_LAUNCH_REQUESTED", command=line)
                return False
            if cmd in ("h", "help", "?"):
                self.message = "Help is shown in the fixed panel."
            elif cmd in ("s", "status"):
                self.refresh()
                self.message = "Status refreshed."
            elif cmd == "step" and len(tokens) == 2:
                value = validate_manual_step("step", tokens[1])
                self.x_step_um = self.y_step_um = self.z_step_um = value
                self.message = f"Manual X/Y/Z step set to {value:g} um."
            elif cmd == "xstep" and len(tokens) == 2:
                self.x_step_um = validate_manual_step("xstep", tokens[1])
                self.message = f"xstep set to {self.x_step_um:g} um."
            elif cmd == "ystep" and len(tokens) == 2:
                self.y_step_um = validate_manual_step("ystep", tokens[1])
                self.message = f"ystep set to {self.y_step_um:g} um."
            elif cmd == "zstep" and len(tokens) == 2:
                self.z_step_um = validate_manual_step("zstep", tokens[1])
                self.message = f"zstep set to {self.z_step_um:g} um."
            elif cmd in ("interval", "dt") and len(tokens) == 2:
                self.sample_interval_s = validate_positive("sample interval", tokens[1])
                self.message = f"Keyboard sampling interval set to {self.sample_interval_s:g} s."
            elif command_is_scan_variable(cmd):
                self._execute_scan_variable_command(tokens)
            elif cmd == "set":
                self._execute_set_command(tokens[1:])
            else:
                self.message = f"Unknown command: {line!r}. Type ':' then help."
        except Exception as exc:
            self.log("COMMAND_ERROR", command=line, error=repr(exc))
            self.message = f"Command failed: {exc}"
        return True

    def _execute_scan_variable_command(self, tokens: list[str]) -> None:
        variable = normalize_scan_variable(tokens[0])
        if len(tokens) == 1:
            self.message = f"{variable}={get_scan_variable(self.config, variable):g} um."
            return
        if len(tokens) != 2:
            raise ValueError(f"Use: {variable} <um>")
        self._set_scan_variable(variable, tokens[1])

    def _execute_set_command(self, tokens: list[str]) -> None:
        if len(tokens) < 1:
            raise ValueError("Use: set x <um>, set y <um>, set z <um>, set xyz <X> <Y> <Z>, or set SCAN_RANGE_X_UM <um>")
        target = tokens[0].lower()
        if command_is_scan_variable(target):
            if len(tokens) != 2:
                raise ValueError(f"Use: set {normalize_scan_variable(target)} <um>")
            self._set_scan_variable(normalize_scan_variable(target), tokens[1])
        elif target == "x" and len(tokens) == 2:
            self.set_xyz(x=float(tokens[1]), reason="command_set_x")
        elif target == "y" and len(tokens) == 2:
            self.set_xyz(y=float(tokens[1]), reason="command_set_y")
        elif target == "z" and len(tokens) == 2:
            self.set_xyz(z=float(tokens[1]), reason="command_set_z")
        elif target == "xy" and len(tokens) == 3:
            self.set_xyz(x=float(tokens[1]), y=float(tokens[2]), reason="command_set_xy")
        elif target == "xyz" and len(tokens) == 4:
            self.set_xyz(x=float(tokens[1]), y=float(tokens[2]), z=float(tokens[3]), reason="command_set_xyz")
        else:
            raise ValueError("Use: set x <um>, set y <um>, set z <um>, set xy <X> <Y>, or set xyz <X> <Y> <Z>")

    def _set_scan_variable(self, variable: str, value_text: str) -> None:
        value = float(value_text)
        if variable == "SCAN_RANGE_X_UM":
            validate_scan_range(variable, value)
            self.config.scan_range_x_um = value
        elif variable == "SCAN_RANGE_Y_UM":
            validate_scan_range(variable, value)
            self.config.scan_range_y_um = value
        elif variable == "STEP_UM":
            self.config.step_um = validate_scan_step(value)
        else:
            raise ValueError(f"Unsupported scan variable: {variable}")
        evaluation = self.evaluate_scan(refresh=False)
        state = "OK" if evaluation.ok else "needs adjustment"
        detail = evaluation.overflow or evaluation.error or "within travel"
        self.message = f"{variable} set to {value:g} um for preview only; scan check {state}: {detail}."
        self.log("SCAN_PREVIEW_SET", variable=variable, value_um=value, check=state, detail=detail)


HELP_TEXT = """
Hotkeys:
  Up / Down        X += xstep / X -= xstep  (SCAN_RANGE_X_UM direction, actually up)
  Left / Right     Y += ystep / Y -= ystep  (SCAN_RANGE_Y_UM direction, actually left)
  + / -            Z += zstep / Z -= zstep
  s                refresh status
  h or ?           redraw this help
  0 or r           move X/Y to 0 um, keep Z; this is NOT PBC_SetZero
  :                command mode
  q                quit; position is preserved and marked ready for PAM_Main_Nanomax.py

Commands after ':' then Enter:
  step <um>                 set xstep/ystep/zstep together
  xstep <um>                set manual X step
  ystep <um>                set manual Y step
  zstep <um>                set manual Z step
  interval <sec>            set keyboard sampling interval
  SCAN_RANGE_X_UM <um>      set preview scan range along X/up
  SCAN_RANGE_Y_UM <um>      set preview scan range along Y/left
  STEP_UM <um>              set preview pixel step
  set x <um>                move X to an absolute closed-loop position
  set y <um>                move Y to an absolute closed-loop position
  set z <um>                move Z to an absolute closed-loop position
  set xyz <X> <Y> <Z>       move all three axes
  pam / image / scan        close pre-alignment control, then launch PAM_Main_Nanomax.py
  status                   refresh status
  quit                     exit

Preview settings are not written back to PAM_Main_Nanomax.py, but a normal quit
writes them into the pre-alignment marker consumed by the next PAM run.
No command in this tool calls PBC_SetZero.
""".strip()


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def sanitize_log_value(value: Any) -> str:
    return str(value).replace("\n", "\\n").replace("\r", "\\r").replace("|", "/")


def clamp(value: float, low: float, high: float) -> tuple[float, bool]:
    clamped = max(low, min(high, float(value)))
    return clamped, abs(clamped - float(value)) > 1e-9


def validate_positive(name: str, value: Any) -> float:
    result = float(value)
    if result <= 0:
        raise ValueError(f"{name} must be positive, got {result:g}.")
    return result


def validate_manual_step(name: str, value: Any) -> float:
    result = validate_positive(name, value)
    if result < DEFAULT_MIN_STEP_UM:
        raise ValueError(f"{name}={result:g} um is below the NanoMax 0.01 um practical resolution guard.")
    return result


def validate_scan_step(step_um: Any) -> float:
    result = validate_positive("STEP_UM", step_um)
    if result < DEFAULT_MIN_STEP_UM:
        raise ValueError(f"STEP_UM={result:g} um is below the NanoMax 0.01 um practical resolution guard.")
    return result


def validate_scan_range(name: str, value: Any) -> float:
    result = float(value)
    if result < 0:
        raise ValueError(f"{name} must be >= 0 um, got {result:g}.")
    if result > DEFAULT_PIEZO_TRAVEL_UM + 1e-9:
        raise ValueError(f"{name}={result:g} um exceeds the NanoMax piezo travel guard of {DEFAULT_PIEZO_TRAVEL_UM:g} um.")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Realtime micron-position pre-alignment for the closed-loop MAX311D/BPC303 NanoMax stage."
    )
    parser.add_argument("--main-file", default=str(DEFAULT_MAIN_FILE), help="PAM_Main_Nanomax.py path to read scan defaults from.")
    parser.add_argument("--serial", default=None, help="Override BPC303 serial number from PAM_Main_Nanomax.py.")
    parser.add_argument("--kinesis-dir", default=None, help="Override Thorlabs Kinesis directory.")
    parser.add_argument("--safe-max-output-voltage", type=float, default=None, help="Override safe BPC output-voltage guard.")
    parser.add_argument("--x-step-um", type=float, default=DEFAULT_MANUAL_STEP_UM)
    parser.add_argument("--y-step-um", type=float, default=DEFAULT_MANUAL_STEP_UM)
    parser.add_argument("--z-step-um", type=float, default=DEFAULT_MANUAL_STEP_UM)
    parser.add_argument("--sample-interval-s", type=float, default=DEFAULT_SAMPLE_INTERVAL_S)
    parser.add_argument("--settle-ms", type=int, default=None)
    parser.add_argument("--move-tolerance-um", type=float, default=0.05)
    parser.add_argument("--move-timeout-s", type=float, default=10.0)
    parser.add_argument("--wait-after-move", action="store_true", help="Wait until BPC reports the commanded position after each manual move.")
    parser.add_argument("--yes", action="store_true", help="Skip the RUN confirmation prompt before live control.")
    parser.add_argument("--status-only", action="store_true", help="Connect, read current position and travel, print status, then exit.")
    parser.add_argument("--dry-run-config", action="store_true", help="Parse PAM scan settings and preview trajectory without connecting hardware.")
    parser.add_argument("--dry-run-x", type=float, default=0.0)
    parser.add_argument("--dry-run-y", type=float, default=0.0)
    parser.add_argument("--dry-run-z", type=float, default=0.0)
    parser.add_argument("--travel-um", type=float, default=DEFAULT_PIEZO_TRAVEL_UM, help="Dry-run travel used for preview checks.")
    parser.add_argument("--launch-dry-run", action="store_true", help="For testing: print the PAM launch command but do not execute it.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Text log path.")
    return parser.parse_args()


def ensure_windows_keyboard() -> None:
    if os.name != "nt":
        raise SystemExit("Realtime keyboard control requires a Windows console with msvcrt.")


def load_pam_scan_config(main_file: Path) -> PamScanConfig:
    source = main_file.read_text(encoding="utf-8")
    values: dict[str, Any] = {
        "SCAN_RANGE_X_UM": 0.0,
        "SCAN_RANGE_Y_UM": 0.0,
        "STEP_UM": 1.0,
        "SAMPLE_X_DIRECTION": 1.0,
        "SAMPLE_Y_DIRECTION": 1.0,
        "SCAN_PATTERN": "serpentine",
        "BPC303_SERIAL_NO": DEFAULT_BPC_SERIAL_NO,
        "BPC303_KINESIS_DIR": DEFAULT_KINESIS_DIR,
        "BPC303_AXIS_MAP": {"x": 1, "y": 2, "z": 3},
        "BPC303_SAFE_MAX_OUTPUT_VOLTAGE": DEFAULT_SAFE_MAX_OUTPUT_VOLTAGE,
        "SETTLE_MS": 120,
    }
    tree = ast.parse(source, filename=str(main_file))
    assignments = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in PAM_CONFIG_NAMES:
                try:
                    assignments.append((node.lineno, target.id, ast.literal_eval(node.value)))
                except Exception:
                    pass
            elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                if len(target.elts) != len(node.value.elts):
                    continue
                for name_node, value_node in zip(target.elts, node.value.elts):
                    if isinstance(name_node, ast.Name) and name_node.id in PAM_CONFIG_NAMES:
                        try:
                            assignments.append((node.lineno, name_node.id, ast.literal_eval(value_node)))
                        except Exception:
                            pass
    for _, name, value in sorted(assignments, key=lambda item: item[0]):
        values[name] = value

    comments = extract_assignment_comments(source)
    axis_map = values["BPC303_AXIS_MAP"]
    if not isinstance(axis_map, dict):
        axis_map = {"x": 1, "y": 2, "z": 3}
    return PamScanConfig(
        main_file=main_file,
        scan_range_x_um=validate_scan_range("SCAN_RANGE_X_UM", values["SCAN_RANGE_X_UM"]),
        scan_range_y_um=validate_scan_range("SCAN_RANGE_Y_UM", values["SCAN_RANGE_Y_UM"]),
        step_um=validate_scan_step(values["STEP_UM"]),
        sample_x_direction=float(values["SAMPLE_X_DIRECTION"]),
        sample_y_direction=float(values["SAMPLE_Y_DIRECTION"]),
        scan_pattern=str(values["SCAN_PATTERN"]),
        bpc303_serial_no=str(values["BPC303_SERIAL_NO"]),
        bpc303_kinesis_dir=str(values["BPC303_KINESIS_DIR"]),
        bpc303_axis_map={str(key).lower(): int(value) for key, value in axis_map.items()},
        bpc303_safe_max_output_voltage=float(values["BPC303_SAFE_MAX_OUTPUT_VOLTAGE"]),
        settle_ms=int(values["SETTLE_MS"]),
        scan_range_x_comment=comments.get("SCAN_RANGE_X_UM", ""),
        scan_range_y_comment=comments.get("SCAN_RANGE_Y_UM", ""),
    )


def extract_assignment_comments(source: str) -> dict[str, str]:
    comments: dict[str, str] = {}
    pattern = re.compile(r"^\s*(SCAN_RANGE_X_UM|SCAN_RANGE_Y_UM)\s*=.*?(?:#\s*(.*))?$")
    tuple_pattern = re.compile(r"^\s*SCAN_RANGE_X_UM\s*,\s*SCAN_RANGE_Y_UM\s*,\s*STEP_UM\s*=.*?(?:#\s*(.*))?$")
    for line in source.splitlines():
        tuple_match = tuple_pattern.match(line)
        if tuple_match and tuple_match.group(1):
            comment = tuple_match.group(1).strip()
            parts = [part.strip() for part in re.split(r";|,", comment) if part.strip()]
            comments["SCAN_RANGE_X_UM"] = next((part for part in parts if part.lower().startswith("x")), comment)
            comments["SCAN_RANGE_Y_UM"] = next((part for part in parts if part.lower().startswith("y")), comment)
            continue
        match = pattern.match(line)
        if match and match.group(2):
            comments[match.group(1)] = match.group(2).strip()
    return comments


def scan_shape_from_range(scan_range_x_um: float, scan_range_y_um: float, step_um: float) -> tuple[int, int]:
    step = validate_scan_step(step_um)
    shape = []
    for name, value in (("SCAN_RANGE_X_UM", scan_range_x_um), ("SCAN_RANGE_Y_UM", scan_range_y_um)):
        range_um = validate_scan_range(name, value)
        interval_count = range_um / step
        rounded = round(interval_count)
        if abs(interval_count - rounded) > 1e-9:
            raise ValueError(f"{name}={range_um:g} um must be an integer multiple of STEP_UM={step:g} um.")
        shape.append(int(rounded) + 1)
    return shape[0], shape[1]


def resolve_scan_pattern(scan_pattern: str) -> tuple[bool, str]:
    normalized = str(scan_pattern).strip().lower()
    if normalized in ("serpentine", "s", "snake"):
        return True, "serpentine/S-shaped"
    if normalized in ("raster", "z", "unidirectional"):
        return False, "raster/Z-shaped"
    raise ValueError("SCAN_PATTERN must be 'serpentine'/'s' or 'raster'/'z'.")


def evaluate_scan_from_start(start_x: float, start_y: float, config: PamScanConfig, travel_x_um: float, travel_y_um: float) -> ScanEvaluation:
    try:
        scan_w, scan_h = scan_shape_from_range(config.scan_range_x_um, config.scan_range_y_um, config.step_um)
        _, pattern_label = resolve_scan_pattern(config.scan_pattern)
    except Exception as exc:
        return ScanEvaluation(ok=False, error=str(exc))

    end_x = float(start_x) + float(config.sample_x_direction) * float(config.scan_range_x_um)
    end_y = float(start_y) + float(config.sample_y_direction) * float(config.scan_range_y_um)
    min_x, max_x = sorted((float(start_x), end_x))
    min_y, max_y = sorted((float(start_y), end_y))
    overflows = []
    if min_x < -1e-9 or max_x > float(travel_x_um) + 1e-9:
        overflows.append(f"X preview {min_x:.4f}..{max_x:.4f} um exceeds [0,{float(travel_x_um):.4f}]")
    if min_y < -1e-9 or max_y > float(travel_y_um) + 1e-9:
        overflows.append(f"Y preview {min_y:.4f}..{max_y:.4f} um exceeds [0,{float(travel_y_um):.4f}]")
    return ScanEvaluation(
        ok=not overflows,
        error="",
        scan_w=scan_w,
        scan_h=scan_h,
        points=scan_w * scan_h,
        min_x=min_x,
        max_x=max_x,
        min_y=min_y,
        max_y=max_y,
        pattern_label=pattern_label,
        overflow="; ".join(overflows),
    )


def command_is_scan_variable(text: str) -> bool:
    return normalize_scan_variable(text) in {"SCAN_RANGE_X_UM", "SCAN_RANGE_Y_UM", "STEP_UM"}


def normalize_scan_variable(text: str) -> str:
    normalized = text.strip().upper()
    aliases = {"RANGEX": "SCAN_RANGE_X_UM", "XRANGE": "SCAN_RANGE_X_UM", "RANGEY": "SCAN_RANGE_Y_UM", "YRANGE": "SCAN_RANGE_Y_UM"}
    return aliases.get(normalized, normalized)


def get_scan_variable(config: PamScanConfig, variable: str) -> float:
    if variable == "SCAN_RANGE_X_UM":
        return config.scan_range_x_um
    if variable == "SCAN_RANGE_Y_UM":
        return config.scan_range_y_um
    if variable == "STEP_UM":
        return config.step_um
    raise ValueError(f"Unsupported scan variable: {variable}")


def clear_screen() -> None:
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def print_help() -> None:
    print(HELP_TEXT, flush=True)


def render_dashboard(control: RealtimeClosedLoopControl, message: str | None = None, refresh: bool = True) -> None:
    clear_screen()
    print("Realtime closed-loop MAX311D/BPC303 X/Y/Z pre-alignment")
    print("=" * 74)
    print_help()
    print("=" * 74)
    if message is not None:
        control.message = message
    for line in control.status_lines(refresh=refresh):
        print(truncate_line(line), flush=True)


def truncate_line(text: str) -> str:
    width = max(80, shutil.get_terminal_size((120, 28)).columns - 1)
    if len(text) > width:
        return text[: width - 3] + "..."
    return text


def read_key(msvcrt_module):
    ch = msvcrt_module.getwch()
    if ch in KEY_ARROW_PREFIXES:
        return "arrow", msvcrt_module.getwch()
    return "char", ch


def drain_keyboard_buffer(msvcrt_module) -> None:
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


def sample_arrow_delta(control: RealtimeClosedLoopControl) -> tuple[float, float, str]:
    x_delta = 0.0
    y_delta = 0.0
    reasons = []

    up = is_key_down(VK_UP)
    down = is_key_down(VK_DOWN)
    left = is_key_down(VK_LEFT)
    right = is_key_down(VK_RIGHT)

    if up and not down:
        x_delta += control.x_step_um
        reasons.append("x_plus_up")
    elif down and not up:
        x_delta -= control.x_step_um
        reasons.append("x_minus_down")

    if left and not right:
        y_delta += control.y_step_um
        reasons.append("y_plus_left")
    elif right and not left:
        y_delta -= control.y_step_um
        reasons.append("y_minus_right")

    return x_delta, y_delta, "+".join(reasons) if reasons else "idle"


def apply_cli_overrides(args: argparse.Namespace, config: PamScanConfig) -> PamScanConfig:
    if args.serial:
        config.bpc303_serial_no = str(args.serial)
    if args.kinesis_dir:
        config.bpc303_kinesis_dir = str(args.kinesis_dir)
    if args.safe_max_output_voltage is not None:
        config.bpc303_safe_max_output_voltage = float(args.safe_max_output_voltage)
    if args.settle_ms is not None:
        config.settle_ms = int(args.settle_ms)
    return config


def connect_stage(args: argparse.Namespace, config: PamScanConfig, tracker: InitTracker):
    from Alazar_imaging.BPC303NativeController import BPC303NativeController

    return BPC303NativeController(
        serial_no=config.bpc303_serial_no,
        kinesis_dir=config.bpc303_kinesis_dir,
        channels=(1, 2, 3),
        axis_map=config.bpc303_axis_map,
        safe_max_output_voltage=config.bpc303_safe_max_output_voltage,
        log_callback=tracker,
    )


def launch_pam_main(main_file: Path, dry_run: bool = False) -> int:
    main_file = Path(main_file).resolve()
    command = [sys.executable, str(main_file)]
    print("\nLaunching PAM imaging from pre-alignment:")
    print(f"  cwd: {main_file.parent}")
    print(f"  command: {command[0]} {command[1]}")
    if dry_run:
        print("Launch dry-run enabled; PAM_Main_Nanomax.py was not executed.")
        return 0
    completed = subprocess.run(command, cwd=str(main_file.parent))
    return int(completed.returncode)


def dry_run_config(args: argparse.Namespace, config: PamScanConfig) -> None:
    stage = DryRunStage(args.dry_run_x, args.dry_run_y, args.dry_run_z, args.travel_um)
    control = RealtimeClosedLoopControl(stage, args, config)
    print("Dry-run closed-loop NanoMax preview; hardware was not connected.")
    print(f"Main file: {config.main_file}")
    control.print_status(refresh=True)


def run_status_only(args: argparse.Namespace, config: PamScanConfig) -> None:
    tracker = InitTracker(enabled=True, log_path=Path(args.log_path))
    stage = connect_stage(args, config, tracker)
    try:
        control = RealtimeClosedLoopControl(stage, args, config)
        clear_screen()
        print("Closed-loop MAX311D/BPC303 status-only check")
        print("=" * 74)
        control.print_status(refresh=True)
    finally:
        stage.close()
        print("BPC303 controller closed; position was not changed.", flush=True)


def run_live_control(args: argparse.Namespace, config: PamScanConfig) -> None:
    ensure_windows_keyboard()
    tracker = InitTracker(enabled=True, log_path=Path(args.log_path))
    stage = connect_stage(args, config, tracker)
    control = RealtimeClosedLoopControl(stage, args, config)
    normal_exit = False
    launch_after_close = False
    try:
        clear_screen()
        print("Realtime closed-loop MAX311D/BPC303 X/Y/Z pre-alignment")
        control.print_status(refresh=True)
        print_help()
        if not args.yes:
            answer = input("This enables live micron-position control. Type RUN to continue: ").strip()
            if answer != "RUN":
                print("Cancelled; no live control started. Position was not changed by hotkeys.")
                return

        import msvcrt

        control.log(
            "REALTIME_START",
            x_step_um=control.x_step_um,
            y_step_um=control.y_step_um,
            z_step_um=control.z_step_um,
            sample_interval_s=control.sample_interval_s,
            scan_range_x_um=control.config.scan_range_x_um,
            scan_range_y_um=control.config.scan_range_y_um,
            step_um=control.config.step_um,
        )
        render_dashboard(control, message="Live control started; q exits without zeroing or return.", refresh=True)
        drain_keyboard_buffer(msvcrt)
        running = True
        last_render = time.time()
        while running:
            char = read_last_command_key(msvcrt)
            if char:
                if char in ("q", "Q"):
                    running = False
                    break
                if char in ("h", "H", "?"):
                    render_dashboard(control, message="Help refreshed.", refresh=True)
                elif char in ("s", "S"):
                    render_dashboard(control, message="Status refreshed.", refresh=True)
                elif char in ("0", "r", "R"):
                    control.set_xyz(x=0.0, y=0.0, reason="hotkey_move_xy_to_zero")
                    drain_keyboard_buffer(msvcrt)
                    render_dashboard(control, refresh=True)
                elif char == "+":
                    control.move_delta(z_delta=control.z_step_um, reason="hotkey_z_plus")
                    drain_keyboard_buffer(msvcrt)
                    render_dashboard(control, refresh=True)
                elif char == "-":
                    control.move_delta(z_delta=-control.z_step_um, reason="hotkey_z_minus")
                    drain_keyboard_buffer(msvcrt)
                    render_dashboard(control, refresh=True)
                elif char == ":":
                    sys.stdout.write("\ncmd> ")
                    sys.stdout.flush()
                    line = input()
                    running = control.execute_command(line)
                    render_dashboard(control, refresh=True)

            if running:
                x_delta, y_delta, reason = sample_arrow_delta(control)
                if x_delta or y_delta:
                    control.move_delta(x_delta=x_delta, y_delta=y_delta, reason=f"key_state_{reason}")
                    drain_keyboard_buffer(msvcrt)
                    render_dashboard(control, refresh=True)
                    last_render = time.time()
                elif time.time() - last_render >= 1.0:
                    render_dashboard(control, refresh=True)
                    last_render = time.time()
            time.sleep(control.sample_interval_s)
        normal_exit = True
        launch_after_close = control.next_action == "pam"
    finally:
        try:
            control.refresh()
            if normal_exit:
                control.write_prealign_marker()
            control.log("REALTIME_END", final_x_um=f"{control.last_xyz[0]:.6f}", final_y_um=f"{control.last_xyz[1]:.6f}", final_z_um=f"{control.last_xyz[2]:.6f}")
        finally:
            stage.close()
            marker_text = f" Pre-alignment marker: {PREALIGN_STATE_PATH}" if normal_exit else " No ready marker was written."
            print(f"\nBPC303 controller closed; current NanoMax position was preserved.{marker_text}", flush=True)
    if launch_after_close:
        return_code = launch_pam_main(Path(args.main_file), dry_run=args.launch_dry_run)
        if return_code != 0:
            raise SystemExit(return_code)


def main() -> None:
    args = parse_args()
    config = apply_cli_overrides(args, load_pam_scan_config(Path(args.main_file)))
    validate_manual_step("xstep", args.x_step_um)
    validate_manual_step("ystep", args.y_step_um)
    validate_manual_step("zstep", args.z_step_um)
    validate_positive("sample interval", args.sample_interval_s)
    validate_positive("move timeout", args.move_timeout_s)
    validate_positive("move tolerance", args.move_tolerance_um)
    if args.dry_run_config:
        dry_run_config(args, config)
    elif args.status_only:
        run_status_only(args, config)
    else:
        run_live_control(args, config)


if __name__ == "__main__":
    main()
