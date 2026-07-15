from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass, field

from Nanomax.terminal_panel import TerminalPanelRenderer, format_section_lines, terminal_width


def _fmt(value, digits=4):
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


@dataclass
class AcquisitionPanelState:
    acquired: int = 0
    current_position: str = "-"
    message: str = "Acquisition running. Use ':' commands for close-at-end toggles."
    command_mode: bool = False
    command_buffer: str = ""
    stop_requested: bool = False
    start_time: float = field(default_factory=time.time)


class AcquisitionDashboard:
    """Fixed acquisition dashboard with progress and nonblocking command input."""

    def __init__(
        self,
        *,
        desc: str,
        total: int,
        laser_manager,
        stop_key: str = "q",
        scan_items=None,
        daq_items=None,
        runtime_items=None,
        log_callback=None,
    ):
        self.desc = str(desc)
        self.total = int(total)
        self.laser_manager = laser_manager
        self.stop_key = str(stop_key or "q").lower()
        self.scan_items = list(scan_items or [])
        self.daq_items = list(daq_items or [])
        self.runtime_items = list(runtime_items or [])
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.renderer = TerminalPanelRenderer()
        self.state = AcquisitionPanelState()
        self._msvcrt = None
        if os.name == "nt":
            try:
                import msvcrt

                self._msvcrt = msvcrt
            except ImportError:
                self._msvcrt = None

    def start(self):
        self.state.start_time = time.time()
        self.render()

    def close(self):
        self.renderer.show_cursor()

    def update(self, acquired: int, current_position: str = None, message: str = None):
        self.state.acquired = int(acquired)
        if current_position is not None:
            self.state.current_position = str(current_position)
        if message:
            self.state.message = str(message)
        self.render()

    def poll_commands(self) -> bool:
        if self._msvcrt is None:
            return False
        try:
            while self._msvcrt.kbhit():
                ch = self._msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    if self._msvcrt.kbhit():
                        self._msvcrt.getwch()
                    continue
                if not self.state.command_mode:
                    if ch.lower() == self.stop_key:
                        self.state.stop_requested = True
                        self.state.message = f"Graceful stop requested by '{self.stop_key}'. Current point will finish first."
                        self.log("ACQUISITION_PANEL_STOP_REQUESTED", stop_key=self.stop_key)
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
        self.render()
        return self.state.stop_requested

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
        rate = acquired / elapsed if acquired else 0.0
        remaining = (total - acquired) / rate if rate > 0 else math.nan
        eta = "--:--" if math.isnan(remaining) else time.strftime("%M:%S", time.gmtime(max(0, remaining)))
        return f"Progress: {100.0 * fraction:6.2f}%|{bar}| {acquired}/{total} [{elapsed:5.1f}s, {rate:5.2f} pixel/s, ETA {eta}]"

    def _command_line(self):
        if self.state.command_mode:
            return f"Command: :{self.state.command_buffer}"
        return "Command: press ':' for laser close-at-end commands; press q for graceful stop."

    def render(self):
        separator = "=" * min(terminal_width() - 1, 118)
        lines = [
            separator,
            f"PAM acquisition dashboard - {self.desc}",
            separator,
            self._progress_line(),
            self._command_line(),
            f"Current point: {self.state.current_position}",
            "Allowed during acquisition: :532 close-at-end on/off, :toptica close-at-end on/off, :laser refresh, q",
        ]
        lines += format_section_lines("Lasers", self.laser_manager.panel_items(acquisition=True))
        lines += format_section_lines("Frozen Scan Parameters", self.scan_items)
        lines += format_section_lines("Frozen DAQ Parameters", self.daq_items)
        lines += format_section_lines("Frozen Runtime Parameters", self.runtime_items)
        lines.append(f"Message: {self.state.message}")
        self.renderer.render(lines)
