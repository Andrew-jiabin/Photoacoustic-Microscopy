import ctypes
import os
import shutil
import sys
import time
from dataclasses import dataclass

from Nanomax.scan_utils import NANOMAX_MANUAL_MIN_STEP_UM, scan_shape_from_range


VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
KEY_ARROW_PREFIXES = ("\x00", "\xe0")


@dataclass
class SamplePrealignConfig:
    scan_range_x_um: float
    scan_range_y_um: float
    step_um: float
    sample_x_direction: float = 1.0
    sample_y_direction: float = 1.0
    scan_pattern: str = "serpentine"
    settle_ms: int = 120
    x_step_um: float = 0.1
    y_step_um: float = 0.1
    z_step_um: float = 0.1
    sample_interval_s: float = 0.25
    min_step_um: float = NANOMAX_MANUAL_MIN_STEP_UM


@dataclass
class SamplePrealignResult:
    x_um: float
    y_um: float
    z_um: float
    scan_range_x_um: float
    scan_range_y_um: float
    step_um: float
    scan_pattern: str


def clear_screen():
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def truncate_line(text):
    width = max(80, shutil.get_terminal_size((120, 30)).columns - 1)
    return text if len(text) <= width else text[: width - 3] + "..."


def validate_positive(name, value):
    value = float(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value:g}.")
    return value


def validate_manual_step(name, value, min_step_um=NANOMAX_MANUAL_MIN_STEP_UM):
    value = validate_positive(name, value)
    if value < float(min_step_um):
        raise ValueError(f"{name}={value:g} um is below the NanoMax minimum step guard {float(min_step_um):g} um.")
    return value


def is_key_down(vk_code):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)


def read_key(msvcrt_module):
    ch = msvcrt_module.getwch()
    if ch in KEY_ARROW_PREFIXES:
        return "arrow", msvcrt_module.getwch()
    return "char", ch


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


def normalized_scan_pattern(value):
    value = str(value).strip().lower()
    if value in ("serpentine", "s", "snake"):
        return "serpentine"
    if value in ("raster", "z", "unidirectional"):
        return "raster"
    raise ValueError("SCAN_PATTERN must be serpentine/s or raster/z.")


class SamplePrealignPanel:
    def __init__(self, stage, config, log_callback=None):
        self.stage = stage
        self.config = config
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.x_step_um = validate_manual_step("xstep", config.x_step_um, config.min_step_um)
        self.y_step_um = validate_manual_step("ystep", config.y_step_um, config.min_step_um)
        self.z_step_um = validate_manual_step("zstep", config.z_step_um, config.min_step_um)
        self.sample_interval_s = validate_positive("sample_interval_s", config.sample_interval_s)
        self.message = "Use hotkeys to set the start position, then ':' and 'start' to begin imaging."
        self.last_xyz = [float(value) for value in self.stage.get_position_values()]
        self.travel_um = {axis: float(self.stage.get_max_travel(axis)) for axis in ("x", "y", "z")}
        self.ready_to_start = False

    def refresh(self):
        self.last_xyz = [float(value) for value in self.stage.get_position_values()]
        return self.last_xyz

    def clamp_axis(self, axis, value):
        low, high = 0.0, self.travel_um[axis]
        clamped = max(low, min(high, float(value)))
        return clamped, abs(clamped - float(value)) > 1e-9

    def set_xyz(self, x=None, y=None, z=None, reason="manual"):
        current = dict(zip(("x", "y", "z"), self.last_xyz))
        requested = {
            "x": current["x"] if x is None else float(x),
            "y": current["y"] if y is None else float(y),
            "z": current["z"] if z is None else float(z),
        }
        target = {}
        clamped_axes = []
        for axis, value in requested.items():
            target[axis], clamped = self.clamp_axis(axis, value)
            if clamped:
                clamped_axes.append(axis.upper())

        move_kwargs = {axis: target[axis] if abs(target[axis] - current[axis]) > 1e-9 else None for axis in ("x", "y", "z")}
        if any(value is not None for value in move_kwargs.values()):
            self.stage.move_xyz(x=move_kwargs["x"], y=move_kwargs["y"], z=move_kwargs["z"], wait=False, settle_time_ms=0)
            if self.config.settle_ms > 0:
                time.sleep(float(self.config.settle_ms) / 1000.0)
        read_x, read_y, read_z = self.refresh()
        self.log(
            "PREALIGN_MOVE_XYZ",
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

    def move_delta(self, x_delta=0.0, y_delta=0.0, z_delta=0.0, reason="key"):
        x, y, z = self.last_xyz
        self.set_xyz(x=x + float(x_delta), y=y + float(y_delta), z=z + float(z_delta), reason=reason)

    def evaluate_scan(self, refresh=False):
        x, y, _ = self.refresh() if refresh else self.last_xyz
        try:
            scan_w, scan_h = scan_shape_from_range(self.config.scan_range_x_um, self.config.scan_range_y_um, self.config.step_um)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        end_x = float(x) + float(self.config.sample_x_direction) * float(self.config.scan_range_x_um)
        end_y = float(y) + float(self.config.sample_y_direction) * float(self.config.scan_range_y_um)
        min_x, max_x = sorted((float(x), end_x))
        min_y, max_y = sorted((float(y), end_y))
        errors = []
        if min_x < -1e-9 or max_x > self.travel_um["x"] + 1e-9:
            errors.append(f"SCAN_RANGE_X_UM makes X {min_x:.4f}..{max_x:.4f} um exceed [0,{self.travel_um['x']:.4f}]")
        if min_y < -1e-9 or max_y > self.travel_um["y"] + 1e-9:
            errors.append(f"SCAN_RANGE_Y_UM makes Y {min_y:.4f}..{max_y:.4f} um exceed [0,{self.travel_um['y']:.4f}]")
        return {
            "ok": not errors,
            "error": "; ".join(errors),
            "scan_w": scan_w,
            "scan_h": scan_h,
            "points": scan_w * scan_h,
            "x_min": min_x,
            "x_max": max_x,
            "y_min": min_y,
            "y_max": max_y,
        }

    def status_lines(self, refresh=False):
        x, y, z = self.refresh() if refresh else self.last_xyz
        scan = self.evaluate_scan(refresh=False)
        lines = [
            "Closed-loop MAX311D/BPC303 prealignment phase - same PAM_Main_Nanomax.py process",
            f"Position: X={x:8.4f} um (Up/Down, SCAN_RANGE_X_UM/up), Y={y:8.4f} um (Left/Right, SCAN_RANGE_Y_UM/left), Z={z:8.4f} um (+/-)",
            f"Manual step: xstep={self.x_step_um:g} um, ystep={self.y_step_um:g} um, zstep={self.z_step_um:g} um; interval={self.sample_interval_s:g} s",
            f"Scan preview: SCAN_RANGE_X_UM={self.config.scan_range_x_um:g} um, SCAN_RANGE_Y_UM={self.config.scan_range_y_um:g} um, STEP_UM={self.config.step_um:g} um, SCAN_PATTERN={self.config.scan_pattern}",
        ]
        if scan.get("ok"):
            lines.append(
                f"S trajectory: shape={scan['scan_w']} x {scan['scan_h']}, points={scan['points']}, "
                f"X={scan['x_min']:.4f}..{scan['x_max']:.4f} um, Y={scan['y_min']:.4f}..{scan['y_max']:.4f} um"
            )
            lines.append(f"Travel check: OK inside X[0,{self.travel_um['x']:.4f}], Y[0,{self.travel_um['y']:.4f}], Z[0,{self.travel_um['z']:.4f}] um")
        else:
            lines.append(f"Travel/step check: OUT OF RANGE - {scan.get('error')}")
            lines.append("Use ':' commands to change SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM, or move the start position.")
        lines.append(f"Message: {self.message}")
        return lines

    def render(self, refresh=True):
        clear_screen()
        print("=" * 78)
        print("PAM closed-loop sample prealignment")
        print("=" * 78)
        print(HELP_TEXT)
        print("=" * 78)
        for line in self.status_lines(refresh=refresh):
            print(truncate_line(line), flush=True)

    def set_scan_variable(self, name, value):
        name = normalize_scan_variable(name)
        if name in ("SCAN_RANGE_X_UM", "SCAN_RANGE_Y_UM"):
            value = float(value)
            if value < 0:
                raise ValueError(f"{name} must be >= 0 um.")
            if value > max(self.travel_um["x"], self.travel_um["y"]) + 1e-9:
                raise ValueError(f"{name}={value:g} um exceeds stage travel.")
            if name == "SCAN_RANGE_X_UM":
                self.config.scan_range_x_um = value
            else:
                self.config.scan_range_y_um = value
        elif name == "STEP_UM":
            value = float(value)
            self.config.step_um = validate_manual_step("STEP_UM", value, self.config.min_step_um)
        elif name == "SCAN_PATTERN":
            self.config.scan_pattern = normalized_scan_pattern(value)
        else:
            raise ValueError(f"Unsupported scan variable {name}.")
        scan = self.evaluate_scan(refresh=False)
        self.message = f"{name} set to {value}; check={'OK' if scan.get('ok') else scan.get('error')}"

    def execute_command(self, line):
        tokens = line.strip().split()
        if not tokens:
            self.message = "Empty command."
            return True
        cmd = tokens[0].lower()
        try:
            if cmd in ("q", "quit", "exit", "cancel"):
                raise KeyboardInterrupt("Prealignment cancelled by user.")
            if cmd in ("start", "run", "pam", "image", "scan"):
                scan = self.evaluate_scan(refresh=True)
                if not scan.get("ok"):
                    self.message = f"Cannot start: {scan.get('error')}"
                    self.log("PREALIGN_START_BLOCKED", reason=scan.get("error"))
                    return True
                self.ready_to_start = True
                self.message = "Starting acquisition from this closed-loop position."
                return False
            if cmd in ("h", "help", "?"):
                self.message = "Help refreshed."
            elif cmd in ("s", "status"):
                self.refresh()
                self.message = "Status refreshed."
            elif cmd in ("0", "zero", "home"):
                self.set_xyz(x=0.0, y=0.0, reason="command_move_xy_to_zero")
            elif cmd == "step" and len(tokens) == 2:
                value = validate_manual_step("step", tokens[1], self.config.min_step_um)
                self.x_step_um = self.y_step_um = self.z_step_um = value
                self.message = f"xstep/ystep/zstep set to {value:g} um."
            elif cmd == "xstep" and len(tokens) == 2:
                self.x_step_um = validate_manual_step("xstep", tokens[1], self.config.min_step_um)
                self.message = f"xstep set to {self.x_step_um:g} um."
            elif cmd == "ystep" and len(tokens) == 2:
                self.y_step_um = validate_manual_step("ystep", tokens[1], self.config.min_step_um)
                self.message = f"ystep set to {self.y_step_um:g} um."
            elif cmd == "zstep" and len(tokens) == 2:
                self.z_step_um = validate_manual_step("zstep", tokens[1], self.config.min_step_um)
                self.message = f"zstep set to {self.z_step_um:g} um."
            elif cmd in ("interval", "dt") and len(tokens) == 2:
                self.sample_interval_s = validate_positive("interval", tokens[1])
                self.message = f"interval set to {self.sample_interval_s:g} s."
            elif cmd == "set":
                self.execute_set(tokens[1:])
            elif normalize_scan_variable(cmd) in {"SCAN_RANGE_X_UM", "SCAN_RANGE_Y_UM", "STEP_UM"} and len(tokens) == 2:
                self.set_scan_variable(cmd, tokens[1])
            else:
                self.message = f"Unknown command: {line!r}."
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self.message = f"Command failed: {exc}"
            self.log("PREALIGN_COMMAND_ERROR", command=line, error=repr(exc))
        return True

    def execute_set(self, tokens):
        if not tokens:
            raise ValueError("Use set x/y/z/xy/xyz or set SCAN_RANGE_X_UM/SCAN_RANGE_Y_UM/STEP_UM.")
        target = tokens[0].lower()
        if normalize_scan_variable(target) in {"SCAN_RANGE_X_UM", "SCAN_RANGE_Y_UM", "STEP_UM"} and len(tokens) == 2:
            self.set_scan_variable(target, tokens[1])
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
            raise ValueError("Use set x <um>, set y <um>, set z <um>, set xy <X> <Y>, or set xyz <X> <Y> <Z>.")

    def sample_arrow_delta(self):
        x_delta, y_delta, reasons = 0.0, 0.0, []
        up, down, left, right = is_key_down(VK_UP), is_key_down(VK_DOWN), is_key_down(VK_LEFT), is_key_down(VK_RIGHT)
        if up and not down:
            x_delta += self.x_step_um
            reasons.append("x_plus_up")
        elif down and not up:
            x_delta -= self.x_step_um
            reasons.append("x_minus_down")
        if left and not right:
            y_delta += self.y_step_um
            reasons.append("y_plus_left")
        elif right and not left:
            y_delta -= self.y_step_um
            reasons.append("y_minus_right")
        return x_delta, y_delta, "+".join(reasons) if reasons else "idle"

    def result(self):
        x, y, z = self.refresh()
        return SamplePrealignResult(
            x_um=x,
            y_um=y,
            z_um=z,
            scan_range_x_um=float(self.config.scan_range_x_um),
            scan_range_y_um=float(self.config.scan_range_y_um),
            step_um=float(self.config.step_um),
            scan_pattern=str(self.config.scan_pattern),
        )


def normalize_scan_variable(text):
    normalized = str(text).strip().upper()
    aliases = {"XRANGE": "SCAN_RANGE_X_UM", "RANGEX": "SCAN_RANGE_X_UM", "YRANGE": "SCAN_RANGE_Y_UM", "RANGEY": "SCAN_RANGE_Y_UM"}
    return aliases.get(normalized, normalized)


HELP_TEXT = """
Hotkeys:
  Up / Down       X += xstep / X -= xstep  (SCAN_RANGE_X_UM direction, actually up)
  Left / Right    Y += ystep / Y -= ystep  (SCAN_RANGE_Y_UM direction, actually left)
  + / -           Z += zstep / Z -= zstep  (closed-loop position in um, not voltage)
  s               refresh status
  h / ?           redraw help
  0 / r           move X/Y to 0 um, keep Z; this is NOT PBC_SetZero
  :               command mode

Commands after ':' then Enter:
  start / run / pam / image / scan   start acquisition in this same PAM_Main_Nanomax.py process
  step <um>                          set xstep/ystep/zstep together
  xstep <um>, ystep <um>, zstep <um> set manual closed-loop move steps
  interval <sec>                     set key sampling interval
  SCAN_RANGE_X_UM <um>               set scan range along X/up for this run
  SCAN_RANGE_Y_UM <um>               set scan range along Y/left for this run
  STEP_UM <um>                       set image pixel step for this run
  set x/y/z/xy/xyz ...               set absolute closed-loop position(s) in um
  q / quit / cancel                  abort before acquisition
""".strip()


def run_sample_prealignment(stage, config, log_callback=None):
    if os.name != "nt":
        print("Prealignment keyboard panel requires a Windows console; using current closed-loop position.")
        return SamplePrealignPanel(stage, config, log_callback=log_callback).result()

    import msvcrt

    panel = SamplePrealignPanel(stage, config, log_callback=log_callback)
    panel.log(
        "PREALIGN_PANEL_START",
        scan_range_x_um=config.scan_range_x_um,
        scan_range_y_um=config.scan_range_y_um,
        step_um=config.step_um,
        x_step_um=panel.x_step_um,
        y_step_um=panel.y_step_um,
        z_step_um=panel.z_step_um,
    )
    panel.render(refresh=True)
    drain_keyboard_buffer(msvcrt)
    last_render = time.time()
    running = True
    while running:
        char = read_last_command_key(msvcrt)
        if char:
            if char in ("h", "H", "?"):
                panel.render(refresh=True)
            elif char in ("s", "S"):
                panel.message = "Status refreshed."
                panel.render(refresh=True)
            elif char in ("0", "r", "R"):
                panel.set_xyz(x=0.0, y=0.0, reason="hotkey_move_xy_to_zero")
                drain_keyboard_buffer(msvcrt)
                panel.render(refresh=True)
            elif char == "+":
                panel.move_delta(z_delta=panel.z_step_um, reason="hotkey_z_plus")
                drain_keyboard_buffer(msvcrt)
                panel.render(refresh=True)
            elif char == "-":
                panel.move_delta(z_delta=-panel.z_step_um, reason="hotkey_z_minus")
                drain_keyboard_buffer(msvcrt)
                panel.render(refresh=True)
            elif char == ":":
                sys.stdout.write("\ncmd> ")
                sys.stdout.flush()
                line = input()
                running = panel.execute_command(line)
                panel.render(refresh=True)

        if running:
            x_delta, y_delta, reason = panel.sample_arrow_delta()
            if x_delta or y_delta:
                panel.move_delta(x_delta=x_delta, y_delta=y_delta, reason=f"key_state_{reason}")
                drain_keyboard_buffer(msvcrt)
                panel.render(refresh=True)
                last_render = time.time()
            elif time.time() - last_render >= 1.0:
                panel.render(refresh=True)
                last_render = time.time()
        time.sleep(panel.sample_interval_s)

    result = panel.result()
    panel.log(
        "PREALIGN_PANEL_DONE",
        x_um=f"{result.x_um:.6f}",
        y_um=f"{result.y_um:.6f}",
        z_um=f"{result.z_um:.6f}",
        scan_range_x_um=result.scan_range_x_um,
        scan_range_y_um=result.scan_range_y_um,
        step_um=result.step_um,
        scan_pattern=result.scan_pattern,
    )
    return result
