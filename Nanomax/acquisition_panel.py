from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from Nanomax.terminal_panel import TerminalPanelRenderer, format_section_lines, terminal_width


def _fmt(value, digits=4):
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def _format_duration(seconds):
    if seconds is None or math.isnan(seconds) or math.isinf(seconds):
        return "--:--"
    total_seconds = int(round(max(0.0, float(seconds))))
    days, remainder = divmod(total_seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass
class AcquisitionPanelState:
    acquired: int = 0
    current_position: str = "-"
    message: str = "Acquisition running. Use ':' commands for close-at-end toggles."
    command_mode: bool = False
    command_buffer: str = ""
    stop_confirm_mode: bool = False
    paused_since: Optional[float] = None
    stop_requested: bool = False
    start_time: float = field(default_factory=time.time)


class PauseZMotionController:
    """Closed-loop Z jog helper used only while acquisition is paused between points."""

    def __init__(
        self,
        stage,
        *,
        step_um: float = 0.1,
        settle_ms: int = 120,
        tolerance_um: float = 0.02,
        timeout_s: float = 10.0,
        reissue_interval_s: float = 1.0,
        min_step_um: float = 0.02,
        log_callback=None,
    ):
        self.stage = stage
        self.step_um = max(float(min_step_um), float(step_um))
        self.settle_ms = int(settle_ms)
        self.tolerance_um = float(tolerance_um)
        self.timeout_s = float(timeout_s)
        self.reissue_interval_s = float(reissue_interval_s)
        self.min_step_um = float(min_step_um)
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.current_z_um = None
        self.max_z_um = None
        self.last_error = ""
        try:
            self.refresh()
        except Exception as exc:
            self.last_error = str(exc)
            self.log("ACQUISITION_PAUSE_Z_INIT_FAILED", error=repr(exc))

    def available(self):
        return self.stage is not None and not self.last_error

    def refresh(self):
        if self.stage is None:
            return None
        try:
            values = self.stage.get_position_values()
            self.current_z_um = float(values[2]) if len(values) > 2 else None
            try:
                self.max_z_um = float(self.stage.get_max_travel("z"))
            except Exception:
                self.max_z_um = None
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            self.log("ACQUISITION_PAUSE_Z_REFRESH_FAILED", error=repr(exc))
            raise
        return self.current_z_um

    def clamp_z(self, value):
        value = float(value)
        low = 0.0
        high = self.max_z_um
        if high is None:
            return max(low, value), False
        clamped = max(low, min(float(high), value))
        return clamped, abs(clamped - value) > 1e-9

    def adjust_step(self, factor):
        old = float(self.step_um)
        self.step_um = max(float(self.min_step_um), old * float(factor))
        self.log("ACQUISITION_PAUSE_Z_STEP_CHANGED", old_step_um=f"{old:.6f}", new_step_um=f"{self.step_um:.6f}")
        return f"Pause Z step changed: {old:g} -> {self.step_um:g} um."

    def move(self, direction):
        current = self.refresh()
        if current is None:
            return "Pause Z move unavailable: could not read current Z."
        target, clamped = self.clamp_z(float(current) + float(direction) * float(self.step_um))
        if abs(target - float(current)) <= 1e-9:
            suffix = " at boundary" if clamped else ""
            return f"No Z move needed: Z={current:.4f} um{suffix}."
        self.log(
            "ACQUISITION_PAUSE_Z_MOVE_BEGIN",
            current_z_um=f"{current:.6f}",
            target_z_um=f"{target:.6f}",
            step_um=f"{self.step_um:.6f}",
            clamped=clamped,
        )
        self.stage.move_axis("z", target)
        self.stage.wait_until_axis_settled(
            "z",
            target,
            settle_time_ms=self.settle_ms,
            tolerance=self.tolerance_um,
            timeout_s=self.timeout_s,
            correction_interval_s=self.reissue_interval_s,
        )
        read_z = self.refresh()
        error = abs(float(read_z) - float(target)) if read_z is not None else float("nan")
        self.log(
            "ACQUISITION_PAUSE_Z_MOVE_DONE",
            target_z_um=f"{target:.6f}",
            read_z_um=f"{float(read_z):.6f}" if read_z is not None else "-",
            error_um=f"{error:.6f}" if not math.isnan(error) else "-",
            tolerance_um=f"{self.tolerance_um:g}",
        )
        suffix = " (clamped)" if clamped else ""
        if read_z is None:
            return f"Pause Z move command sent to target={target:.4f} um, but readback is unavailable{suffix}."
        return f"Pause Z moved to {float(read_z):.4f} um; target={target:.4f} um{suffix}."

    def panel_items(self):
        z = self.current_z_um
        limit = f"0..{self.max_z_um:.2f}" if self.max_z_um is not None else ">=0"
        if self.last_error:
            return [
                ("PAUSE_Z", "UNAVAILABLE", self.last_error),
                ("PAUSE_Z_STEP_UM", f"{self.step_um:g}", "[/] while paused"),
            ]
        return [
            ("PAUSE_Z", _fmt(z), "+/- while paused"),
            ("PAUSE_Z_STEP_UM", f"{self.step_um:g}", "[/] while paused"),
            ("PAUSE_Z_LIMIT_UM", limit, "closed-loop"),
            ("PAUSE_Z_TIMEOUT_S", f"{self.timeout_s:g}", "per jog"),
        ]


class AcquisitionDashboard:
    """Fixed acquisition dashboard with progress and nonblocking command input."""

    def __init__(
        self,
        *,
        desc: str,
        total: int,
        laser_manager,
        stop_key: str = "q",
        stop_enabled: bool = True,
        scan_items=None,
        daq_items=None,
        runtime_items=None,
        result_preview_controller=None,
        mat_path_provider=None,
        pause_z_controller=None,
        log_callback=None,
    ):
        self.desc = str(desc)
        self.total = int(total)
        self.laser_manager = laser_manager
        self.stop_key = str(stop_key or "q").lower()
        self.stop_enabled = bool(stop_enabled)
        self.scan_items = list(scan_items or [])
        self.daq_items = list(daq_items or [])
        self.runtime_items = list(runtime_items or [])
        self.result_preview_controller = result_preview_controller
        self.mat_path_provider = mat_path_provider
        self.pause_z_controller = pause_z_controller
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.renderer = TerminalPanelRenderer()
        self.state = AcquisitionPanelState()
        self._rate_ema = None
        self._last_rate_time = None
        self._last_rate_acquired = 0
        self._msvcrt = None
        if os.name == "nt":
            try:
                import msvcrt

                self._msvcrt = msvcrt
            except ImportError:
                self._msvcrt = None

    def start(self):
        now = time.time()
        self.state.start_time = now
        self._rate_ema = None
        self._last_rate_time = now
        self._last_rate_acquired = 0
        self.render()

    def close(self):
        self.renderer.show_cursor()

    def update(self, acquired: int, current_position: str = None, message: str = None):
        acquired = int(acquired)
        self._update_rate_estimate(acquired)
        self.state.acquired = acquired
        if current_position is not None:
            self.state.current_position = str(current_position)
        if message:
            self.state.message = str(message)
        self.render()

    def _update_rate_estimate(self, acquired: int):
        now = time.time()
        if self._last_rate_time is None or acquired < self._last_rate_acquired:
            self._last_rate_time = now
            self._last_rate_acquired = acquired
            self._rate_ema = None
            return
        delta_points = acquired - self._last_rate_acquired
        delta_time = now - self._last_rate_time
        if delta_points <= 0 or delta_time <= 0.05:
            return
        instant_rate = delta_points / delta_time
        if instant_rate > 0:
            # Low alpha damps point-to-point jitter while still following slow drift.
            alpha = 0.08
            if self._rate_ema is None:
                self._rate_ema = instant_rate
            else:
                self._rate_ema = alpha * instant_rate + (1.0 - alpha) * self._rate_ema
        self._last_rate_time = now
        self._last_rate_acquired = acquired

    def poll_commands(self) -> bool:
        if self._msvcrt is None:
            return False
        changed = False
        try:
            while self._msvcrt.kbhit():
                ch = self._msvcrt.getwch()
                changed = True
                if ch in ("\x00", "\xe0"):
                    if self._msvcrt.kbhit():
                        self._msvcrt.getwch()
                    continue
                if not self.state.command_mode:
                    if ch.lower() == self.stop_key:
                        if self.stop_enabled:
                            if self._pause_for_stop_confirmation():
                                return True
                        else:
                            self.state.message = f"Stop key '{self.stop_key}' is disabled; use ':' laser commands only."
                    elif ch == ":":
                        self.state.command_mode = True
                        self.state.command_buffer = ""
                        self.state.message = "Command mode: type laser command, Enter to run, Esc to cancel."
                    continue
                if ch in ("\r", "\n"):
                    self._execute_command(self.state.command_buffer)
                    self.state.command_mode = False
                    self.state.command_buffer = ""
                elif ch == "\x1b":
                    self.state.command_mode = False
                    self.state.command_buffer = ""
                    self.state.message = "Command cancelled."
                elif ch == "\b":
                    self.state.command_buffer = self.state.command_buffer[:-1]
                elif ch.isprintable():
                    self.state.command_buffer += ch
        except Exception as exc:
            self.state.message = f"Command input error: {exc}"
            self.log("ACQUISITION_PANEL_INPUT_ERROR", error=repr(exc))
            changed = True
        if changed:
            self.render()
        return self.state.stop_requested

    def _pause_for_stop_confirmation(self) -> bool:
        self.state.stop_confirm_mode = True
        self.state.paused_since = time.time()
        z_hint = ""
        if self.pause_z_controller is not None and self.pause_z_controller.available():
            z_hint = " '+/-' jog Z, '[]' change Z step;"
        self.state.message = (
            "Acquisition paused after current point. Press 'y' to stop; Esc to continue; "
            f"'p' preview all, 'a' axis-time, '3' 3D, 'i' index;{z_hint}"
        )
        self.log("ACQUISITION_PANEL_STOP_CONFIRM_REQUESTED", stop_key=self.stop_key)
        self.render()

        while True:
            if self._msvcrt is not None and self._msvcrt.kbhit():
                ch = self._msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    if self._msvcrt.kbhit():
                        self._msvcrt.getwch()
                    continue
                if ch.lower() == "y":
                    paused_for = time.time() - (self.state.paused_since or time.time())
                    self.state.stop_requested = True
                    self.state.stop_confirm_mode = False
                    self.state.paused_since = None
                    self.state.message = "Stop confirmed with 'y'. Acquisition will stop cleanly."
                    self.log("ACQUISITION_PANEL_STOP_CONFIRMED", paused_s=round(paused_for, 3))
                    self.render()
                    return True
                if ch.lower() in {"p", "a", "i"} or ch == "3":
                    preview_mode = {"p": "all", "a": "axis", "3": "3d", "i": "index"}.get(ch.lower(), "all")
                    self._run_preview(preview_mode)
                    self.render()
                    continue
                if ch in {"+", "-", "[", "]"}:
                    self._handle_pause_z_key(ch)
                    self.render()
                    continue
                if ch == "\x1b":
                    paused_for = time.time() - (self.state.paused_since or time.time())
                    self.state.stop_confirm_mode = False
                    self.state.paused_since = None
                    self.state.message = "Stop cancelled with Esc; acquisition continues."
                    self.log("ACQUISITION_PANEL_STOP_CANCELLED", paused_s=round(paused_for, 3))
                    self.render()
                    return False
                if ch.lower() == self.stop_key:
                    self.state.message = self._paused_help_message(already=True)
                    self.render()
                elif ch not in ("\r", "\n"):
                    self.state.message = self._paused_help_message(already=False)
                    self.render()
            time.sleep(0.05)

    def _paused_help_message(self, already=False):
        prefix = "Already paused." if already else "Paused."
        z_part = ""
        if self.pause_z_controller is not None and self.pause_z_controller.available():
            z_part = " +/- jog closed-loop Z; [/] change Z step;"
        return f"{prefix} Press 'y' to stop; Esc to continue; p/a/3/i preview;{z_part}"

    def _handle_pause_z_key(self, ch):
        if self.pause_z_controller is None or not self.pause_z_controller.available():
            self.state.message = "Paused Z control is unavailable for this scan target."
            self.log("ACQUISITION_PANEL_PAUSE_Z_SKIPPED", key=repr(ch), reason="not_available")
            return
        try:
            if ch == "+":
                self.state.message = self.pause_z_controller.move(+1)
            elif ch == "-":
                self.state.message = self.pause_z_controller.move(-1)
            elif ch == "[":
                self.state.message = self.pause_z_controller.adjust_step(0.5)
            elif ch == "]":
                self.state.message = self.pause_z_controller.adjust_step(2.0)
            self.log("ACQUISITION_PANEL_PAUSE_Z_COMMAND", key=repr(ch), message=self.state.message)
        except Exception as exc:
            self.state.message = f"Pause Z command failed: {exc}"
            self.log("ACQUISITION_PANEL_PAUSE_Z_ERROR", key=repr(ch), error=repr(exc))

    def _run_preview(self, mode: str) -> None:
        if self.result_preview_controller is None:
            self.state.message = "Result preview is not configured for this run."
            self.log("ACQUISITION_PANEL_PREVIEW_SKIPPED", reason="not_configured", mode=mode)
            return
        if not callable(self.mat_path_provider):
            self.state.message = "Result preview has no .mat path provider."
            self.log("ACQUISITION_PANEL_PREVIEW_SKIPPED", reason="missing_path_provider", mode=mode)
            return
        try:
            self.state.message = f"Generating {mode} preview from current saved/cache .mat..."
            self.render()
            mat_path = self.mat_path_provider()
            if not mat_path:
                self.state.message = "No saved or cache .mat is available yet for preview."
                self.log("ACQUISITION_PANEL_PREVIEW_SKIPPED", reason="no_mat_path", mode=mode)
                return
            result = self.result_preview_controller.generate(mat_path, mode=mode)
            if result.status == "ok":
                first_artifact = result.artifacts[0] if result.artifacts else result.output_dir
                self.state.message = f"Preview ready: {first_artifact}"
                self.log("ACQUISITION_PANEL_PREVIEW_DONE", mode=mode, input_path=mat_path, output_dir=result.output_dir)
            else:
                self.state.message = f"Preview failed/skipped: {result.error or result.status}"
                self.log("ACQUISITION_PANEL_PREVIEW_FAILED", mode=mode, input_path=mat_path, error=result.error)
        except Exception as exc:
            self.state.message = f"Preview error: {exc}"
            self.log("ACQUISITION_PANEL_PREVIEW_ERROR", mode=mode, error=repr(exc))

    def _execute_command(self, line: str):
        tokens = line.strip().split()
        if not tokens:
            self.state.message = "Empty command."
            return
        try:
            self.state.message = self.laser_manager.execute_acquisition_command(tokens)
            self.log("ACQUISITION_PANEL_COMMAND", command=line, message=self.state.message)
        except Exception as exc:
            self.state.message = f"Command failed: {exc}"
            self.log("ACQUISITION_PANEL_COMMAND_ERROR", command=line, error=repr(exc))

    def _progress_line(self):
        width = min(terminal_width() - 30, 70)
        width = max(20, width)
        total = max(1, self.total)
        acquired = max(0, min(self.state.acquired, total))
        fraction = acquired / total
        filled = int(round(fraction * width))
        bar = "█" * filled + "░" * max(0, width - filled)
        elapsed = max(0.001, time.time() - self.state.start_time)
        average_rate = acquired / elapsed if acquired else 0.0
        # Use a smoothed point-to-point rate after the first few points. Before
        # that, the whole-run average is less misleading than a one-point spike.
        eta_rate = self._rate_ema if acquired >= 5 and self._rate_ema else average_rate
        remaining = (total - acquired) / eta_rate if eta_rate > 0 else math.nan
        eta = _format_duration(remaining)
        elapsed_text = _format_duration(elapsed)
        return f"Progress: {100.0 * fraction:6.2f}%|{bar}| {acquired}/{total} [{elapsed_text}, {eta_rate:5.2f} pixel/s, ETA {eta}]"

    def _command_line(self):
        if self.state.stop_confirm_mode:
            paused_for = time.time() - (self.state.paused_since or time.time())
            z_part = ", +/- Z, [/] Z step" if self.pause_z_controller is not None and self.pause_z_controller.available() else ""
            return f"Command: PAUSED {_format_duration(paused_for)} - y stop, Esc continue, p/a/3/i preview{z_part}."
        if self.state.command_mode:
            return f"Command: :{self.state.command_buffer}"
        stop_hint = f"press {self.stop_key} to pause/confirm stop" if self.stop_enabled else "graceful stop key disabled"
        return f"Command: press ':' for laser close-at-end commands; {stop_hint}."

    def render(self):
        separator = "=" * min(terminal_width() - 1, 118)
        paused_controls = "y stop, Esc continue, p/a/3/i preview"
        if self.pause_z_controller is not None and self.pause_z_controller.available():
            paused_controls += ", +/- Z, [/] Z step"
        lines = [
            separator,
            f"PAM acquisition dashboard - {self.desc}",
            separator,
            self._progress_line(),
            self._command_line(),
            f"Current point: {self.state.current_position}",
            "Allowed during acquisition: "
            f":532 close-at-end on/off, :toptica close-at-end on/off, :laser refresh"
            f"{', ' + self.stop_key + ' pause; paused: ' + paused_controls if self.stop_enabled else ''}",
        ]
        lines += format_section_lines("Lasers", self.laser_manager.panel_items(acquisition=True))
        if self.pause_z_controller is not None and self.pause_z_controller.available():
            lines += format_section_lines("Paused Closed-Loop Z Control", self.pause_z_controller.panel_items())
        lines += format_section_lines("Frozen Scan Parameters", self.scan_items)
        lines += format_section_lines("Frozen DAQ Parameters", self.daq_items)
        lines += format_section_lines("Frozen Runtime Parameters", self.runtime_items)
        lines.append(f"Message: {self.state.message}")
        self.renderer.render(lines)
