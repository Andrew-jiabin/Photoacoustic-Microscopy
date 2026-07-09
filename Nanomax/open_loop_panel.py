import ctypes
import os
import shutil
import sys
import time
from dataclasses import dataclass


VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
KEY_ARROW_PREFIXES = ("\x00", "\xe0")


@dataclass
class ProbePrealignConfig:
    safe_max_voltage: float = 75.0
    piezo_travel_um: float = 20.0
    piezo_travel_voltage: float = 75.0
    y_step_v: float = 1.0
    z_step_v: float = 1.0
    sample_interval_s: float = 0.25
    settle_ms: int = 80
    set_axis_max: bool = True
    allow_sample_switch: bool = False
    return_yz_zero_on_exit: bool = False


@dataclass
class ProbePrealignResult:
    x_v: float
    y_v: float
    z_v: float
    y_step_v: float
    z_step_v: float
    next_action: str = "start"


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


def clamp(value, low, high):
    clamped = max(low, min(high, float(value)))
    return clamped, abs(clamped - float(value)) > 1e-9


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


def section_lines(title, items):
    rows = [f"[{title}]"]
    width = max(80, shutil.get_terminal_size((120, 30)).columns - 1)
    columns = 2 if width < 115 else 3
    cell_width = max(28, min(42, (width - 2) // columns))
    cells = []
    for name, value, hint in items:
        text = f"{name}={value}"
        if hint:
            text = f"{text} ({hint})"
        cells.append(text[: cell_width - 1].ljust(cell_width))
    for index in range(0, len(cells), columns):
        rows.append(" ".join(cells[index:index + columns]).rstrip())
    return rows


class ProbePrealignPanel:
    def __init__(self, stage, config, log_callback=None, status_provider=None, display_params=None):
        self.stage = stage
        self.config = config
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.status_provider = status_provider
        self.display_params = display_params or {}
        self.safe_max_voltage = validate_positive("safe_max_voltage", config.safe_max_voltage)
        self.piezo_travel_um = validate_positive("piezo_travel_um", config.piezo_travel_um)
        self.piezo_travel_voltage = validate_positive("piezo_travel_voltage", config.piezo_travel_voltage)
        self.y_step_v = validate_positive("y_step_v", config.y_step_v)
        self.z_step_v = validate_positive("z_step_v", config.z_step_v)
        self.sample_interval_s = validate_positive("sample_interval_s", config.sample_interval_s)
        self.last_xyz = [float(value) for value in self.stage.get_voltage_xyz()]
        self.message = "Use hotkeys to set probe Y/Z, then ':' and 'start' to begin imaging."
        self.next_action = "start"

    def refresh(self):
        self.last_xyz = [float(value) for value in self.stage.get_voltage_xyz()]
        return self.last_xyz

    def voltage_to_um(self, voltage):
        return float(voltage) / self.piezo_travel_voltage * self.piezo_travel_um

    def set_safe_max(self, value):
        value = validate_positive("safe max voltage", value)
        device_limit = self.stage.limit_voltage
        if device_limit is not None and value > float(device_limit) + 1e-9:
            raise ValueError(f"safe max {value:g} V exceeds MDT device limit {float(device_limit):g} V")
        self.safe_max_voltage = value
        self.stage.safe_max_voltage = value
        warnings = []
        for axis in ("y", "z"):
            try:
                self.stage.set_axis_max_voltage(axis, value)
            except Exception as exc:
                warnings.append(f"{axis.upper()}MAX failed: {exc}")
        self.log("PROBE_PREALIGN_SAFE_MAX_SET", safe_max_voltage=f"{value:.6f}", warnings="; ".join(warnings))
        self.message = f"Safe max voltage set to {value:g} V" if not warnings else "; ".join(warnings)

    def set_yz(self, y=None, z=None, reason="manual"):
        _, current_y, current_z = self.last_xyz
        target_y = current_y if y is None else float(y)
        target_z = current_z if z is None else float(z)
        target_y, y_clamped = clamp(target_y, 0.0, self.safe_max_voltage)
        target_z, z_clamped = clamp(target_z, 0.0, self.safe_max_voltage)
        if abs(target_y - current_y) > 1e-9:
            self.stage.set_voltage_axis("y", target_y)
        if abs(target_z - current_z) > 1e-9:
            self.stage.set_voltage_axis("z", target_z)
        if self.config.settle_ms > 0:
            time.sleep(float(self.config.settle_ms) / 1000.0)
        x, read_y, read_z = self.refresh()
        self.log(
            "PROBE_PREALIGN_MOVE_YZ",
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
            f"Moved probe: Y={read_y:.3f} V ~= {self.voltage_to_um(read_y):.2f} um, "
            f"Z={read_z:.3f} V ~= {self.voltage_to_um(read_z):.2f} um{suffix}"
        )

    def move_delta(self, y_delta=0.0, z_delta=0.0, reason="key"):
        _, current_y, current_z = self.last_xyz
        self.set_yz(y=current_y + float(y_delta), z=current_z + float(z_delta), reason=reason)

    def sample_arrow_delta(self):
        y_delta, z_delta, reasons = 0.0, 0.0, []
        up, down, left, right = is_key_down(VK_UP), is_key_down(VK_DOWN), is_key_down(VK_LEFT), is_key_down(VK_RIGHT)
        if up and not down:
            z_delta += self.z_step_v
            reasons.append("z_plus_up")
        elif down and not up:
            z_delta -= self.z_step_v
            reasons.append("z_minus_down")
        if right and not left:
            y_delta += self.y_step_v
            reasons.append("y_plus_right")
        elif left and not right:
            y_delta -= self.y_step_v
            reasons.append("y_minus_left")
        return y_delta, z_delta, "+".join(reasons) if reasons else "idle"

    def execute_command(self, line):
        tokens = line.strip().split()
        if not tokens:
            self.message = "Empty command."
            return True
        cmd = tokens[0].lower()
        try:
            if cmd in ("q", "quit", "exit", "cancel"):
                raise KeyboardInterrupt("Probe prealignment cancelled by user.")
            if cmd in ("start", "run", "pam", "image", "scan"):
                self.next_action = "start"
                self.message = "Starting acquisition from the current positions."
                return False
            if cmd in ("sample", "closed", "closed-loop", "bpc", "bpc303"):
                if not self.config.allow_sample_switch:
                    self.message = "Closed-loop sample panel is not enabled for this run."
                    return True
                self.next_action = "sample"
                self.message = "Switching to closed-loop sample panel."
                return False
            if cmd in ("h", "help", "?"):
                self.message = "Help refreshed."
            elif cmd in ("s", "status"):
                self.refresh()
                self.message = "Status refreshed."
            elif cmd in ("0", "zero", "home"):
                self.set_yz(0.0, 0.0, reason="command_zero_yz")
            elif cmd == "step" and len(tokens) == 2:
                value = validate_positive("step", tokens[1])
                self.y_step_v = self.z_step_v = value
                self.message = f"Y/Z step set to {value:g} V."
            elif cmd == "ystep" and len(tokens) == 2:
                self.y_step_v = validate_positive("ystep", tokens[1])
                self.message = f"Y step set to {self.y_step_v:g} V."
            elif cmd == "zstep" and len(tokens) == 2:
                self.z_step_v = validate_positive("zstep", tokens[1])
                self.message = f"Z step set to {self.z_step_v:g} V."
            elif cmd in ("interval", "dt") and len(tokens) == 2:
                self.sample_interval_s = validate_positive("interval", tokens[1])
                self.message = f"interval set to {self.sample_interval_s:g} s."
            elif cmd in ("max", "safe", "limit") and len(tokens) == 2:
                self.set_safe_max(float(tokens[1]))
            elif cmd == "set":
                self.execute_set(tokens[1:])
            else:
                self.message = f"Unknown command: {line!r}."
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            self.message = f"Command failed: {exc}"
            self.log("PROBE_PREALIGN_COMMAND_ERROR", command=line, error=repr(exc))
        return True

    def execute_set(self, tokens):
        if not tokens:
            raise ValueError("Use: set y <V>, set z <V>, set yz <Y_V> <Z_V>, or set step <V>.")
        target = tokens[0].lower()
        if target == "y" and len(tokens) == 2:
            self.set_yz(y=float(tokens[1]), reason="command_set_y")
        elif target == "z" and len(tokens) == 2:
            self.set_yz(z=float(tokens[1]), reason="command_set_z")
        elif target == "yz" and len(tokens) == 3:
            self.set_yz(y=float(tokens[1]), z=float(tokens[2]), reason="command_set_yz")
        elif target == "step" and len(tokens) == 2:
            value = validate_positive("step", tokens[1])
            self.y_step_v = self.z_step_v = value
            self.message = f"Y/Z step set to {value:g} V."
        elif target == "ystep" and len(tokens) == 2:
            self.y_step_v = validate_positive("ystep", tokens[1])
            self.message = f"Y step set to {self.y_step_v:g} V."
        elif target == "zstep" and len(tokens) == 2:
            self.z_step_v = validate_positive("zstep", tokens[1])
            self.message = f"Z step set to {self.z_step_v:g} V."
        elif target in ("interval", "dt") and len(tokens) == 2:
            self.sample_interval_s = validate_positive("interval", tokens[1])
            self.message = f"interval set to {self.sample_interval_s:g} s."
        else:
            raise ValueError("Use set y <V>, set z <V>, set yz <Y> <Z>, set ystep <V>, or set zstep <V>.")

    def status_lines(self, refresh=False):
        x, y, z = self.refresh() if refresh else self.last_xyz
        daq_status = self.status_provider() if callable(self.status_provider) else {}
        position_items = [
            ("X_V", f"{x:.4f}", "read-only here"),
            ("Y_V", f"{y:.4f}", f"~= {self.voltage_to_um(y):.2f} um"),
            ("Z_V", f"{z:.4f}", f"~= {self.voltage_to_um(z):.2f} um"),
            ("SAFE_MAX_V", f"0..{self.safe_max_voltage:g}", "set max n"),
        ]
        motion_items = [
            ("ystep", f"{self.y_step_v:g}", f"~= {self.voltage_to_um(self.y_step_v):g} um"),
            ("zstep", f"{self.z_step_v:g}", f"~= {self.voltage_to_um(self.z_step_v):g} um"),
            ("interval", f"{self.sample_interval_s:g}", "set interval n"),
            ("SETTLE_MS", f"{self.config.settle_ms:g}", "fixed from config"),
        ]
        daq_items = [
            ("DAQ_STATUS", daq_status.get("status", "-"), ""),
            ("DAQ_STEP", daq_status.get("step", "-"), ""),
            ("DAQ_ELAPSED_S", f"{float(daq_status.get('elapsed_s', 0.0)):.2f}", ""),
            ("SCAN_TARGET", self.display_params.get("SCAN_TARGET", "-"), "read-only"),
            ("PROBE_SCAN_AXES", self.display_params.get("PROBE_SCAN_AXES", "-"), "read-only"),
        ]
        lines = ["Open-loop MAX312D/MDT693B probe prealignment phase - same PAM_Main_Nanomax.py process"]
        lines += section_lines("Probe Position", position_items)
        lines += section_lines("Motion / Hotkey Parameters", motion_items)
        lines += section_lines("DAQ / Runtime", daq_items)
        lines.append("Position display is an open-loop estimate, not closed-loop feedback.")
        lines.append(f"Message: {self.message}")
        return lines

    def render(self, refresh=True):
        clear_screen()
        width = max(80, shutil.get_terminal_size((120, 30)).columns)
        print("=" * min(width - 1, 118))
        print("PAM open-loop probe prealignment")
        print("=" * min(width - 1, 118))
        print(HELP_TEXT)
        print("=" * min(width - 1, 118))
        for line in self.status_lines(refresh=refresh):
            print(truncate_line(line), flush=True)

    def result(self):
        x, y, z = self.refresh()
        return ProbePrealignResult(
            x_v=x,
            y_v=y,
            z_v=z,
            y_step_v=float(self.y_step_v),
            z_step_v=float(self.z_step_v),
            next_action=self.next_action,
        )


HELP_TEXT = """
Hotkeys:
  Up / Down       probe Z += zstep / Z -= zstep  (MDT voltage)
  Right / Left    probe Y += ystep / Y -= ystep  (MDT voltage)
  s               refresh status
  h / ?           redraw help
  0 / r           set probe Y/Z to 0 V
  + / -           double / halve both Y and Z voltage steps
  :               command mode

Commands after ':' then Enter:
  start / run / pam / image / scan   start acquisition in this same PAM_Main_Nanomax.py process
  sample / closed / bpc303           switch to the closed-loop sample panel, if enabled
  set y <V>, set z <V>, set yz <Y> <Z>
  set ystep <V>, set zstep <V>, set step <V>
  set interval <sec>
  max <V>                            set program safe upper limit and MDT YMAX/ZMAX
  zero                               set Y/Z to 0 V
  q / quit / cancel                  abort before acquisition
""".strip()


def run_probe_prealignment(stage, config, log_callback=None, status_provider=None, display_params=None):
    panel = ProbePrealignPanel(
        stage,
        config,
        log_callback=log_callback,
        status_provider=status_provider,
        display_params=display_params,
    )
    panel.log(
        "PROBE_PREALIGN_PANEL_START",
        y_step_v=f"{panel.y_step_v:.6f}",
        z_step_v=f"{panel.z_step_v:.6f}",
        safe_max_voltage=f"{panel.safe_max_voltage:.6f}",
        set_axis_max=config.set_axis_max,
    )
    if config.set_axis_max:
        panel.set_safe_max(panel.safe_max_voltage)

    if os.name != "nt":
        print("Probe prealignment keyboard panel requires a Windows console; using current open-loop voltages.")
        return panel.result()

    import msvcrt

    panel.render(refresh=True)
    drain_keyboard_buffer(msvcrt)
    last_render = time.time()
    running = True
    while running:
        char = read_last_command_key(msvcrt)
        if char:
            if char in ("q", "Q"):
                raise KeyboardInterrupt("Probe prealignment cancelled by user.")
            if char in ("h", "H", "?"):
                panel.render(refresh=True)
            elif char in ("s", "S"):
                panel.message = "Status refreshed."
                panel.render(refresh=True)
            elif char in ("0", "r", "R"):
                panel.set_yz(0.0, 0.0, reason="hotkey_zero_yz")
                drain_keyboard_buffer(msvcrt)
                panel.render(refresh=True)
            elif char == "+":
                panel.y_step_v *= 2.0
                panel.z_step_v *= 2.0
                panel.message = f"Y/Z steps doubled to {panel.y_step_v:g} V."
                panel.render(refresh=True)
            elif char == "-":
                panel.y_step_v = max(panel.y_step_v / 2.0, 1e-6)
                panel.z_step_v = max(panel.z_step_v / 2.0, 1e-6)
                panel.message = f"Y/Z steps halved to {panel.y_step_v:g} V."
                panel.render(refresh=True)
            elif char == ":":
                sys.stdout.write("\ncmd> ")
                sys.stdout.flush()
                line = input()
                running = panel.execute_command(line)
                panel.render(refresh=True)

        if running:
            y_delta, z_delta, reason = panel.sample_arrow_delta()
            if y_delta or z_delta:
                panel.move_delta(y_delta=y_delta, z_delta=z_delta, reason=f"key_state_{reason}")
                drain_keyboard_buffer(msvcrt)
                panel.render(refresh=True)
                last_render = time.time()
            elif time.time() - last_render >= 1.0:
                panel.render(refresh=True)
                last_render = time.time()
        time.sleep(panel.sample_interval_s)

    if config.return_yz_zero_on_exit and panel.next_action != "start":
        panel.set_yz(0.0, 0.0, reason="prealign_exit_zero")

    result = panel.result()
    panel.log(
        "PROBE_PREALIGN_PANEL_DONE",
        x_v=f"{result.x_v:.6f}",
        y_v=f"{result.y_v:.6f}",
        z_v=f"{result.z_v:.6f}",
        y_step_v=f"{result.y_step_v:.6f}",
        z_step_v=f"{result.z_step_v:.6f}",
        next_action=result.next_action,
    )
    return result
