import ctypes
import math
import os
import sys
import time
from dataclasses import dataclass

from Nanomax.scan_utils import NANOMAX_MANUAL_MIN_STEP_UM, scan_shape_from_range
from Nanomax.scan_speed_history import estimate_scan_time, format_duration
from Nanomax.terminal_panel import TerminalPanelRenderer, format_section_lines, terminal_width


VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
KEY_ARROW_PREFIXES = ("\x00", "\xe0")
DEFAULT_AUTO_REFRESH_S = 5.0


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
    auto_refresh_s: float = DEFAULT_AUTO_REFRESH_S
    min_step_um: float = NANOMAX_MANUAL_MIN_STEP_UM
    position_tolerance_um: float = 0.02
    position_timeout_s: float = 300.0
    position_reissue_interval_s: float = 1.0
    allow_probe_switch: bool = False
    # Longest single leg used when the panel has to travel back to the scan start corner.
    # Marking the corners can leave the stage at the far one, so the return is issued as a
    # chain of short closed-loop moves rather than one jump.
    rect_return_step_um: float = 1.0


@dataclass
class SamplePrealignResult:
    x_um: float
    y_um: float
    z_um: float
    scan_range_x_um: float
    scan_range_y_um: float
    step_um: float
    scan_pattern: str
    next_action: str = "start"
    scan_ok: bool = True
    scan_error: str = ""

def validate_positive(name, value):
    value = float(value)
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value:g}.")
    return value


def validate_nonnegative(name, value):
    value = float(value)
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value:g}.")
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
    def __init__(self, stage, config, log_callback=None, status_provider=None, display_params=None):
        self.stage = stage
        self.config = config
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.status_provider = status_provider
        self.display_params = display_params or {}
        self.debug_mode = self.display_params.get("SCAN_TARGET") == "nanomax_motion_debug"
        self.x_step_um = validate_manual_step("xstep", config.x_step_um, config.min_step_um)
        self.y_step_um = validate_manual_step("ystep", config.y_step_um, config.min_step_um)
        self.z_step_um = validate_manual_step("zstep", config.z_step_um, config.min_step_um)
        self.sample_interval_s = validate_positive("sample_interval_s", config.sample_interval_s)
        self.auto_refresh_s = validate_positive("auto_refresh_s", config.auto_refresh_s)
        self.config.position_tolerance_um = validate_manual_step(
            "position_tolerance_um",
            config.position_tolerance_um,
            config.min_step_um,
        )
        self.config.position_timeout_s = validate_positive("position_timeout_s", config.position_timeout_s)
        self.config.position_reissue_interval_s = validate_nonnegative(
            "position_reissue_interval_s",
            config.position_reissue_interval_s,
        )
        if self.debug_mode:
            self.message = "NanoMax motion debug only: use hotkeys/commands to move stages; q exits without imaging."
        else:
            self.message = "Use hotkeys to set the start position, then ':' and 'start' to begin imaging."
        self.last_xyz = [float(value) for value in self.stage.get_position_values()]
        self.travel_um = {axis: float(self.stage.get_max_travel(axis)) for axis in ("x", "y", "z")}
        # A piezo with a zero datum can only go up from 0; a controller with absolute
        # coordinates can sit at a negative one. Ask only if the stage offers the lower
        # bound, so the BPC303 panel keeps its historical [0, travel] clamp exactly.
        min_travel = getattr(self.stage, "get_min_travel", None)
        self.travel_min_um = {
            axis: (float(min_travel(axis)) if callable(min_travel) else 0.0)
            for axis in ("x", "y", "z")
        }
        self.ready_to_start = False
        self.next_action = "start"
        self.renderer = TerminalPanelRenderer()
        self.laser_manager = self.display_params.get("LASER_MANAGER")
        # Presentation overrides so a non-NanoMax stage (for example the Prior
        # adapter) can reuse this panel without showing NanoMax-specific labels.
        # Every default reproduces the historical strings exactly.
        self.help_text = str(self.display_params.get("HELP_TEXT") or HELP_TEXT)
        self.panel_title = str(
            self.display_params.get("PANEL_TITLE") or "PAM closed-loop sample prealignment"
        )
        self.status_header = str(
            self.display_params.get("STATUS_HEADER")
            or "Closed-loop MAX311D/BPC303 prealignment phase - same PAM_Main_Nanomax.py process"
        )
        self.show_probe_rows = bool(self.display_params.get("SHOW_PROBE_ROWS", True))
        # Two-point rectangle scan: the operator marks two opposite corners and the panel
        # derives the scan rectangle from them, instead of hand-placing the scan start and
        # then typing the two ranges. Each corner is read from the live stage position, so
        # it does not matter whether the stage was moved with the panel hotkeys, the
        # terminal commands, or the controller's own hand pad.
        self.corner_a = None
        self.corner_b = None
        self.rect_return_step_um = validate_manual_step(
            "rect_return_step_um",
            config.rect_return_step_um,
            config.min_step_um,
        )

    def refresh(self):
        self.last_xyz = [float(value) for value in self.stage.get_position_values()]
        return self.last_xyz

    def status_signature(self):
        daq_status = self.status_provider() if callable(self.status_provider) else {}
        return (
            daq_status.get("status", "-"),
            daq_status.get("step", "-"),
            daq_status.get("message", ""),
        )

    def refresh_lasers(self):
        if self.laser_manager is None:
            return
        self.laser_manager.refresh_status()

    def axis_window(self, axis):
        """Return ``(low, high)`` for one axis, in microns.

        A piezo with a zero datum reports ``(0, travel)``; a controller with absolute
        coordinates reports a signed window. Everything that validates or displays a
        limit goes through here, so the BPC303 path keeps its historical
        ``0..travel`` wording and behaviour exactly.
        """
        return float(self.travel_min_um[axis]), float(self.travel_um[axis])

    def clamp_axis(self, axis, value):
        low, high = self.axis_window(axis)
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
        moved_axes = [axis for axis, value in move_kwargs.items() if value is not None]
        if not moved_axes:
            if not str(reason).startswith(("key", "hotkey")):
                suffix = f"; already at {','.join(clamped_axes)} boundary" if clamped_axes else ""
                self.message = (
                    f"No closed-loop move needed: X={current['x']:.4f} um, "
                    f"Y={current['y']:.4f} um, Z={current['z']:.4f} um{suffix}."
                )
            return False

        self.stage.move_xyz(
            x=move_kwargs["x"],
            y=move_kwargs["y"],
            z=move_kwargs["z"],
            wait=True,
            settle_time_ms=self.config.settle_ms,
            tolerance=self.config.position_tolerance_um,
            timeout_s=self.config.position_timeout_s,
            correction_interval_s=self.config.position_reissue_interval_s,
        )
        read_x, read_y, read_z = self.refresh()
        readback = {"x": read_x, "y": read_y, "z": read_z}
        errors = {axis: abs(readback[axis] - target[axis]) for axis in ("x", "y", "z")}
        max_moved_error = max((errors[axis] for axis in moved_axes), default=0.0)
        self.log(
            "PREALIGN_MOVE_XYZ",
            reason=reason,
            target_x_um=f"{target['x']:.6f}",
            target_y_um=f"{target['y']:.6f}",
            target_z_um=f"{target['z']:.6f}",
            read_x_um=f"{read_x:.6f}",
            read_y_um=f"{read_y:.6f}",
            read_z_um=f"{read_z:.6f}",
            error_x_um=f"{errors['x']:.6f}",
            error_y_um=f"{errors['y']:.6f}",
            error_z_um=f"{errors['z']:.6f}",
            max_moved_error_um=f"{max_moved_error:.6f}",
            tolerance_um=f"{self.config.position_tolerance_um:.6f}",
            timeout_s=f"{self.config.position_timeout_s:g}",
            reissue_interval_s=f"{self.config.position_reissue_interval_s:g}",
            clamped=",".join(clamped_axes) if clamped_axes else "none",
        )
        suffix = f" (clamped {','.join(clamped_axes)})" if clamped_axes else ""
        self.message = (
            f"Moved to X={read_x:.4f} um, Y={read_y:.4f} um, Z={read_z:.4f} um; "
            f"max moved-axis error={max_moved_error:.4f} um "
            f"(tol={self.config.position_tolerance_um:g} um){suffix}"
        )
        return True

    def move_delta(self, x_delta=0.0, y_delta=0.0, z_delta=0.0, reason="key"):
        x, y, z = self.last_xyz
        return self.set_xyz(x=x + float(x_delta), y=y + float(y_delta), z=z + float(z_delta), reason=reason)

    def move_segmented_to(self, target_x, target_y, reason="segmented_move", step_um=None):
        """Travel to an absolute X,Y as a chain of short closed-loop legs, not one jump.

        Marking the two corners can leave the stage at the far corner, so returning to the
        scan start may be a long travel. Each leg is at most ``rect_return_step_um`` long
        and waits for the closed loop to settle before the next one is issued, so the stage
        never takes the whole distance in a single command.

        Returns ``(moved, legs, distance_um)``.
        """
        start_x, start_y, _ = self.last_xyz
        delta_x = float(target_x) - float(start_x)
        delta_y = float(target_y) - float(start_y)
        distance = math.hypot(delta_x, delta_y)
        segment = float(self.rect_return_step_um if step_um is None else step_um)
        if segment <= 0:
            raise ValueError(f"Return segment must be positive, got {segment:g} um.")
        if distance <= segment + 1e-9:
            return bool(self.set_xyz(x=target_x, y=target_y, reason=reason)), 1, distance
        legs = int(math.ceil(distance / segment - 1e-9))
        moved = False
        for index in range(1, legs + 1):
            if index == legs:
                leg_x, leg_y = float(target_x), float(target_y)
            else:
                fraction = float(index) / float(legs)
                leg_x = float(start_x) + delta_x * fraction
                leg_y = float(start_y) + delta_y * fraction
            moved = bool(self.set_xyz(x=leg_x, y=leg_y, reason=f"{reason}_leg{index}of{legs}")) or moved
        return moved, legs, distance

    # ------------------------------------------------------------------ two-point rect
    def mark_corner(self, which):
        """Record the live stage position as one corner of the scan rectangle.

        The position is read from the stage right now, so whatever moved it last -- the
        panel arrow keys, a ``set x/y`` command, or the controller's hand pad -- is
        already reflected here. Marking the second corner applies the rectangle.
        """
        axis = str(which).strip().lower()
        if axis not in ("a", "b"):
            raise ValueError("Corner must be 'a' or 'b'.")
        x, y, _ = self.refresh()
        point = (float(x), float(y))
        if axis == "a":
            self.corner_a = point
        else:
            self.corner_b = point
        label = "A" if axis == "a" else "B"
        self.log("PREALIGN_CORNER_MARKED", corner=label, x_um=f"{point[0]:.6f}", y_um=f"{point[1]:.6f}")
        if self.corner_a is not None and self.corner_b is not None:
            self.apply_corner_rectangle()
            return True
        other = "B" if axis == "a" else "A"
        self.message = (
            f"Corner {label} marked at X={point[0]:.4f} um, Y={point[1]:.4f} um. "
            f"Move the stage to corner {other} and mark it too, then the rectangle is applied."
        )
        return True

    def clear_corners(self):
        self.corner_a = None
        self.corner_b = None
        self.message = "Two-point corners cleared."
        self.log("PREALIGN_CORNERS_CLEARED")
        return True

    def corner_status_text(self):
        def fmt(point):
            return "-" if point is None else f"{point[0]:.4f},{point[1]:.4f}"

        return fmt(self.corner_a), fmt(self.corner_b)

    def apply_corner_rectangle(self):
        """Derive the scan rectangle from the two marked corners and move to its start.

        The start corner is chosen from the configured scan directions so the shared
        trajectory builder, which walks ``+direction * range`` from the start, covers
        exactly the marked rectangle. The hand-placed span is snapped to the nearest whole
        number of ``STEP_UM`` steps, because the scan grid has to be a whole number of
        steps and a hand-placed rectangle almost never lands on one.
        """
        if self.corner_a is None or self.corner_b is None:
            raise ValueError("Mark both corners first: p1 then p2, or 'rect x1 y1 x2 y2'.")
        (x1, y1), (x2, y2) = self.corner_a, self.corner_b
        step = float(self.config.step_um)
        span_x = abs(float(x2) - float(x1))
        span_y = abs(float(y2) - float(y1))
        steps_x = int(round(span_x / step))
        steps_y = int(round(span_y / step))
        range_x = steps_x * step
        range_y = steps_y * step
        dir_x = float(self.config.sample_x_direction)
        dir_y = float(self.config.sample_y_direction)
        start_x = min(x1, x2) if dir_x >= 0 else max(x1, x2)
        start_y = min(y1, y2) if dir_y >= 0 else max(y1, y2)
        end_x = start_x + dir_x * range_x
        end_y = start_y + dir_y * range_y
        low_x, high_x = self.axis_window("x")
        low_y, high_y = self.axis_window("y")
        errors = []
        if min(start_x, end_x) < low_x - 1e-9 or max(start_x, end_x) > high_x + 1e-9:
            errors.append(
                f"X {min(start_x, end_x):.4f}..{max(start_x, end_x):.4f} um exceeds [{low_x:g},{high_x:.4f}]"
            )
        if min(start_y, end_y) < low_y - 1e-9 or max(start_y, end_y) > high_y + 1e-9:
            errors.append(
                f"Y {min(start_y, end_y):.4f}..{max(start_y, end_y):.4f} um exceeds [{low_y:g},{high_y:.4f}]"
            )
        if errors:
            raise ValueError("Two-point rectangle is outside the stage travel window: " + "; ".join(errors))
        # Returning to the start corner can cross the whole rectangle, so it is issued as a
        # chain of short legs instead of one jump.
        moved, legs, return_distance = self.move_segmented_to(
            start_x,
            start_y,
            reason="command_rect_start_corner",
        )
        self.config.scan_range_x_um = range_x
        self.config.scan_range_y_um = range_y
        # The two-point feature is the S-shaped scan; keep it explicit so a previous
        # 'set SCAN_PATTERN raster' cannot silently turn it back into a raster scan.
        self.config.scan_pattern = "serpentine"
        scan = self.evaluate_scan(refresh=True)
        self.log(
            "PREALIGN_RECT_READY",
            corner_a_x_um=f"{x1:.6f}",
            corner_a_y_um=f"{y1:.6f}",
            corner_b_x_um=f"{x2:.6f}",
            corner_b_y_um=f"{y2:.6f}",
            start_x_um=f"{start_x:.6f}",
            start_y_um=f"{start_y:.6f}",
            scan_range_x_um=f"{range_x:.6f}",
            scan_range_y_um=f"{range_y:.6f}",
            step_um=f"{step:g}",
            snap_residual_x_um=f"{span_x - range_x:.6f}",
            snap_residual_y_um=f"{span_y - range_y:.6f}",
            scan_w=scan.get("scan_w"),
            scan_h=scan.get("scan_h"),
            points=scan.get("points"),
            scan_ok=scan.get("ok"),
            start_corner_moved=moved,
            return_distance_um=f"{return_distance:.6f}",
            return_legs=legs,
            return_step_um=f"{self.rect_return_step_um:g}",
        )
        if not scan.get("ok"):
            self.message = f"Two-point rectangle applied but the check failed: {scan.get('error')}"
            return True
        residual = []
        if abs(span_x - range_x) > 1e-9:
            residual.append(f"X span {span_x:.4f}->{range_x:.4f} um")
        if abs(span_y - range_y) > 1e-9:
            residual.append(f"Y span {span_y:.4f}->{range_y:.4f} um")
        residual_text = ("; snapped to STEP_UM: " + ", ".join(residual)) if residual else ""
        return_text = ""
        if legs > 1:
            return_text = (
                f" Returned to the start corner in {legs} legs of <= {self.rect_return_step_um:g} um "
                f"({return_distance:.4f} um travelled)."
            )
        self.message = (
            f"Two-point rectangle ready: A=({x1:.4f},{y1:.4f}) B=({x2:.4f},{y2:.4f}) um -> "
            f"start=({start_x:.4f},{start_y:.4f}), range={range_x:g} x {range_y:g} um, "
            f"shape={scan['scan_w']} x {scan['scan_h']}, points={scan['points']}, "
            f"pattern=serpentine (S-shaped){residual_text}.{return_text} Type start to begin."
        )
        return True

    def execute_rect(self, tokens):
        if not tokens:
            self.apply_corner_rectangle()
            return
        if len(tokens) == 1 and tokens[0].lower() in ("clear", "reset", "none"):
            self.clear_corners()
            return
        if len(tokens) == 4:
            self.corner_a = (float(tokens[0]), float(tokens[1]))
            self.corner_b = (float(tokens[2]), float(tokens[3]))
            self.log(
                "PREALIGN_CORNER_MARKED",
                corner="A+B",
                x_um=f"{self.corner_a[0]:.6f}",
                y_um=f"{self.corner_a[1]:.6f}",
                corner_b_x_um=f"{self.corner_b[0]:.6f}",
                corner_b_y_um=f"{self.corner_b[1]:.6f}",
            )
            self.apply_corner_rectangle()
            return
        raise ValueError(
            "Use 'rect' to apply the marked corners, 'rect clear' to drop them, "
            "or 'rect x1 y1 x2 y2' to set both corners numerically."
        )

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
        low_x, high_x = self.axis_window("x")
        low_y, high_y = self.axis_window("y")
        if min_x < low_x - 1e-9 or max_x > high_x + 1e-9:
            errors.append(
                f"SCAN_RANGE_X_UM makes X {min_x:.4f}..{max_x:.4f} um exceed "
                f"[{low_x:g},{high_x:.4f}]"
            )
        if min_y < low_y - 1e-9 or max_y > high_y + 1e-9:
            errors.append(
                f"SCAN_RANGE_Y_UM makes Y {min_y:.4f}..{max_y:.4f} um exceed "
                f"[{low_y:g},{high_y:.4f}]"
            )
        estimate = estimate_scan_time("sample_closed_loop", self.config.step_um, scan_w * scan_h)
        return {
            "ok": not errors,
            "error": "; ".join(errors),
            "scan_w": scan_w,
            "scan_h": scan_h,
            "points": scan_w * scan_h,
            "time_estimate": estimate,
            "x_min": min_x,
            "x_max": max_x,
            "y_min": min_y,
            "y_max": max_y,
        }

    def status_lines(self, refresh=False):
        x, y, z = self.refresh() if refresh else self.last_xyz
        scan = self.evaluate_scan(refresh=False)
        daq_status = self.status_provider() if callable(self.status_provider) else {}
        daq_state = str(daq_status.get("status", "not_started"))
        daq_ready = daq_state == "ready"
        scan_ok = bool(scan.get("ok"))
        start_allowed = scan_ok
        if self.debug_mode:
            start_allowed = True
            start_hint = "DEBUG - type ':' then start/run to close this motion panel without imaging."
        elif not scan_ok:
            start_hint = f"NO - fix scan range/step first: {scan.get('error')}"
        elif daq_ready:
            start_hint = "YES - type ':' then start/run/pam to begin acquisition."
        else:
            start_hint = f"YES - type ':' then start/run/pam; acquisition will wait for DAQ status={daq_state}."
        connection_items = [
            ("SAMPLE_CTRL", self.display_params.get("SAMPLE_CONTROLLER", "BPC303"), self.display_params.get("SAMPLE_CONNECTION", "connected")),
            ("SAMPLE_SERIAL", self.display_params.get("SAMPLE_SERIAL", "-"), self.display_params.get("SAMPLE_STAGE_MODEL", "MAX311D")),
            ("SAMPLE_AXES", self.display_params.get("SAMPLE_AXIS_MAP", "1/2/3=X/Y/Z"), "closed-loop um"),
        ]
        if self.show_probe_rows:
            connection_items += [
                ("PROBE_CTRL", self.display_params.get("PROBE_CONTROLLER", "MDT693B"), self.display_params.get("PROBE_CONNECTION", "unknown")),
                ("PROBE_SERIAL", self.display_params.get("PROBE_SERIAL", "-"), f"port={self.display_params.get('PROBE_PORT', '-')}, backend={self.display_params.get('PROBE_BACKEND', '-')}"),
                ("PROBE_PANEL", "available" if self.config.allow_probe_switch else "unavailable", "use :probe" if self.config.allow_probe_switch else self.display_params.get("PROBE_CONNECT_ERROR", "-")),
            ]
        low_x, high_x = self.axis_window("x")
        low_y, high_y = self.axis_window("y")
        low_z, high_z = self.axis_window("z")
        position_items = [
            ("X_um", f"{x:.4f}", "Up/Down"),
            ("Y_um", f"{y:.4f}", "Left/Right"),
            ("Z_um", f"{z:.4f}", "+/-"),
            ("X_LIMIT", f"{low_x:g}..{high_x:.2f}", "um"),
            ("Y_LIMIT", f"{low_y:g}..{high_y:.2f}", "um"),
            ("Z_LIMIT", f"{low_z:g}..{high_z:.2f}", "um"),
        ]
        corner_a_text, corner_b_text = self.corner_status_text()
        scan_items = [
            ("SCAN_RANGE_X_UM", f"{self.config.scan_range_x_um:g}", "set SCAN_RANGE_X_UM n"),
            ("SCAN_RANGE_Y_UM", f"{self.config.scan_range_y_um:g}", "set SCAN_RANGE_Y_UM n"),
            ("STEP_UM", f"{self.config.step_um:g}", "set STEP_UM n"),
            ("SCAN_PATTERN", self.config.scan_pattern, "set SCAN_PATTERN s|z"),
            ("CORNER_A", corner_a_text, "p1: mark current X,Y"),
            ("CORNER_B", corner_b_text, "p2: mark, then rectangle applies"),
        ]
        motion_items = [
            ("xstep", f"{self.x_step_um:g}", "set xstep n"),
            ("ystep", f"{self.y_step_um:g}", "set ystep n"),
            ("zstep", f"{self.z_step_um:g}", "set zstep n"),
            ("RECT_RETURN_STEP_UM", f"{self.rect_return_step_um:g}", "set return_step n"),
            ("interval", f"{self.sample_interval_s:g}", "set interval n"),
            ("refresh", f"{self.auto_refresh_s:g}", "set refresh n"),
            ("SETTLE_MS", f"{self.config.settle_ms:g}", "set SETTLE_MS n"),
            ("POS_TOL_UM", f"{self.config.position_tolerance_um:g}", "set tolerance n"),
            ("POS_TIMEOUT_S", f"{self.config.position_timeout_s:g}", "set timeout n"),
            ("POS_REISSUE_S", f"{self.config.position_reissue_interval_s:g}", "set reissue n"),
        ]
        daq_items = [
            ("DAQ_STATUS", daq_status.get("status", "-"), ""),
            ("DAQ_STEP", daq_status.get("step", "-"), ""),
            ("DAQ_READY", "YES" if daq_ready else "NO", "background init complete"),
            ("DAQ_ELAPSED_S", f"{float(daq_status.get('elapsed_s', 0.0)):.2f}", ""),
            ("DELAY", self.display_params.get("DELAY", "-"), "read-only after init starts"),
            ("SAMPLE_RATE", self.display_params.get("SAMPLE_RATE", "-"), "read-only after init starts"),
            ("SAMPLES_REC", self.display_params.get("SAMPLES_REC", "-"), "read-only after init starts"),
            ("RECORDS_PER_POINT", self.display_params.get("RECORDS_PER_POINT", "-"), "read-only after init starts"),
            ("BUFFER_COUNT", self.display_params.get("BUFFER_COUNT", "-"), "read-only after init starts"),
            ("AVERAGE_ENABLE", self.display_params.get("AVERAGE_ENABLE", "-"), "read-only"),
        ]
        runtime_items = [
            ("POINT_LOG_INTERVAL", self.display_params.get("POINT_LOG_INTERVAL", "-"), "read-only"),
            ("USER_STOP_ENABLE", self.display_params.get("USER_STOP_ENABLE", "-"), "read-only"),
            ("USER_STOP_KEY", self.display_params.get("USER_STOP_KEY", "-"), "read-only"),
            ("SAMPLE_START_ZERO_POLICY", self.display_params.get("SAMPLE_START_ZERO_POLICY", "-"), "read-only"),
            ("SAMPLE_ZERO_XY_AT_END", self.display_params.get("SAMPLE_ZERO_XY_AT_END", "-"), "read-only"),
        ]
        laser_items = []
        if self.laser_manager is not None:
            laser_items = self.laser_manager.panel_items(acquisition=False)
        start_items = [
            ("START_ALLOWED", "DEBUG_EXIT" if self.debug_mode else ("YES" if start_allowed else "NO"), "command=:start/:run/:pam"),
            ("SCAN_CHECK", "DEBUG_ONLY" if self.debug_mode else ("OK" if scan_ok else "BLOCKED"), "" if self.debug_mode else scan.get("error", "")),
            ("DAQ_CHECK", "READY" if daq_ready else f"WAIT:{daq_state}", daq_status.get("step", "")),
        ]
        if self.debug_mode:
            lines = ["Closed-loop MAX311D/BPC303 NanoMax motion debug - DAQ and lasers are not initialized"]
        else:
            lines = [self.status_header]
        lines += section_lines("Connections", connection_items)
        lines += section_lines("Position", position_items)
        lines += section_lines("Scan Parameters", scan_items)
        if scan.get("ok"):
            lines.append(f"Trajectory: shape={scan['scan_w']} x {scan['scan_h']}, points={scan['points']}, X={scan['x_min']:.4f}..{scan['x_max']:.4f} um, Y={scan['y_min']:.4f}..{scan['y_max']:.4f} um")
            estimate = scan.get("time_estimate")
            if estimate:
                lines.append(
                    "Time estimate: "
                    f"{format_duration(estimate['estimated_s'])} "
                    f"at {estimate['speed_pps']:.3f} px/s "
                    f"(STEP_UM={self.config.step_um:g}, records={estimate['records_used']}/{estimate['history_records']}, "
                    f"last={estimate['last_timestamp']})"
                )
            else:
                lines.append(f"Time estimate: unavailable until a complete successful STEP_UM={self.config.step_um:g} scan is recorded.")
            lines.append(
                f"Travel check: OK inside X[{low_x:g},{high_x:.4f}], "
                f"Y[{low_y:g},{high_y:.4f}], Z[{low_z:g},{high_z:.4f}] um"
            )
        else:
            lines.append(f"Travel/step check: OUT OF RANGE - {scan.get('error')}")
            lines.append("Use ':' commands to change SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM, or move the start position.")
        lines += section_lines("Motion / Hotkey Parameters", motion_items)
        if laser_items:
            lines += section_lines("Lasers", laser_items)
        lines += section_lines("DAQ Background Init", daq_items)
        lines += section_lines("Start Gate", start_items)
        lines.append(f"Start prompt: {start_hint}")
        lines += section_lines("Runtime Parameters", runtime_items)
        if daq_status.get("message"):
            lines.append(f"DAQ message: {daq_status.get('message')}")
        if daq_status.get("timings"):
            timings = daq_status["timings"]
            lines.append("DAQ timings: " + ", ".join(f"{key}={float(value):.2f}s" for key, value in timings.items()))
        lines.append(f"Message: {self.message}")
        return lines

    def render(self, refresh=True):
        width = terminal_width()
        separator = "=" * min(width - 1, 118)
        lines = [
            separator,
            self.panel_title,
            separator,
            self.help_text,
            separator,
        ]
        lines.extend(self.status_lines(refresh=refresh))
        self.renderer.render(lines)

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
            if self.laser_manager is not None:
                laser_message = self.laser_manager.execute_prealign_command(tokens)
                if laser_message is not None:
                    self.message = laser_message
                    return True
            if cmd in ("q", "quit", "exit", "cancel"):
                self.ready_to_start = False
                self.next_action = "quit"
                self.message = "Leaving prealignment before acquisition; no scan will be started."
                self.log("PREALIGN_QUIT_REQUESTED", command=line)
                return False
            if cmd in ("start", "run", "pam", "image", "scan", "go"):
                if self.debug_mode:
                    self.ready_to_start = False
                    self.next_action = "quit"
                    self.message = "Closing NanoMax motion debug panel without imaging."
                    self.log("PREALIGN_DEBUG_EXIT_REQUESTED", command=line)
                    return False
                scan = self.evaluate_scan(refresh=True)
                if not scan.get("ok"):
                    self.message = f"Cannot start: {scan.get('error')}"
                    self.log("PREALIGN_START_BLOCKED", reason=scan.get("error"))
                    return True
                self.ready_to_start = True
                self.next_action = "start"
                self.message = "Starting acquisition from this closed-loop position."
                return False
            if cmd in ("probe", "open", "open-loop", "mdt", "mdt693b"):
                if not self.config.allow_probe_switch:
                    self.message = "Open-loop probe panel is not enabled for this run."
                    return True
                self.next_action = "probe"
                self.message = "Switching to open-loop probe panel."
                return False
            if cmd in ("p1", "a", "mark1", "point1", "m1"):
                self.mark_corner("a")
            elif cmd in ("p2", "b", "mark2", "point2", "m2"):
                self.mark_corner("b")
            elif cmd in ("rect", "rectangle", "twopoint", "two-point"):
                self.execute_rect(tokens[1:])
            elif cmd in ("clear", "reset", "unmark"):
                self.clear_corners()
            elif cmd in ("h", "help", "?"):
                self.message = "Help refreshed."
            elif cmd in ("s", "status"):
                self.refresh()
                self.refresh_lasers()
                self.message = "Status refreshed."
            elif cmd in ("0", "zero", "home"):
                self.set_xyz(x=0.0, y=0.0, reason="command_move_xy_to_zero")
            elif cmd == "set":
                self.execute_set(tokens[1:])
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
            elif cmd in ("refresh", "redraw") and len(tokens) == 2:
                self.auto_refresh_s = validate_positive("refresh", tokens[1])
                self.message = f"auto refresh set to {self.auto_refresh_s:g} s."
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
            raise ValueError("Use fixed form: set PARAM value, for example set STEP_UM 0.02.")
        target = tokens[0].lower()
        normalized_target = normalize_scan_variable(target)
        if normalized_target in {"SCAN_RANGE_X_UM", "SCAN_RANGE_Y_UM", "STEP_UM", "SCAN_PATTERN"} and len(tokens) == 2:
            self.set_scan_variable(target, tokens[1])
        elif target == "step" and len(tokens) == 2:
            value = validate_manual_step("step", tokens[1], self.config.min_step_um)
            self.x_step_um = self.y_step_um = self.z_step_um = value
            self.message = f"xstep/ystep/zstep set to {value:g} um."
        elif target == "xstep" and len(tokens) == 2:
            self.x_step_um = validate_manual_step("xstep", tokens[1], self.config.min_step_um)
            self.message = f"xstep set to {self.x_step_um:g} um."
        elif target == "ystep" and len(tokens) == 2:
            self.y_step_um = validate_manual_step("ystep", tokens[1], self.config.min_step_um)
            self.message = f"ystep set to {self.y_step_um:g} um."
        elif target == "zstep" and len(tokens) == 2:
            self.z_step_um = validate_manual_step("zstep", tokens[1], self.config.min_step_um)
            self.message = f"zstep set to {self.z_step_um:g} um."
        elif target in ("return_step", "returnstep", "rect_return_step", "rect_return_step_um", "return_step_um") and len(tokens) == 2:
            self.rect_return_step_um = validate_manual_step(
                "rect_return_step_um",
                tokens[1],
                self.config.min_step_um,
            )
            self.message = f"rect_return_step_um set to {self.rect_return_step_um:g} um."
        elif target in ("interval", "dt") and len(tokens) == 2:
            self.sample_interval_s = validate_positive("interval", tokens[1])
            self.message = f"interval set to {self.sample_interval_s:g} s."
        elif target in ("refresh", "redraw") and len(tokens) == 2:
            self.auto_refresh_s = validate_positive("refresh", tokens[1])
            self.message = f"auto refresh set to {self.auto_refresh_s:g} s."
        elif target == "settle_ms" and len(tokens) == 2:
            self.config.settle_ms = int(validate_positive("SETTLE_MS", tokens[1]))
            self.message = f"SETTLE_MS set to {self.config.settle_ms:g}."
        elif target in ("tolerance", "position_tolerance", "position_tolerance_um", "pos_tol", "pos_tol_um") and len(tokens) == 2:
            self.config.position_tolerance_um = validate_manual_step(
                "position_tolerance_um",
                tokens[1],
                self.config.min_step_um,
            )
            self.message = f"position_tolerance_um set to {self.config.position_tolerance_um:g} um."
        elif target in ("timeout", "position_timeout", "position_timeout_s", "pos_timeout", "pos_timeout_s") and len(tokens) == 2:
            self.config.position_timeout_s = validate_positive("position_timeout_s", tokens[1])
            self.message = f"position_timeout_s set to {self.config.position_timeout_s:g} s."
        elif target in ("reissue", "reissue_interval", "position_reissue", "position_reissue_interval_s", "pos_reissue", "pos_reissue_s") and len(tokens) == 2:
            self.config.position_reissue_interval_s = validate_nonnegative("position_reissue_interval_s", tokens[1])
            self.message = f"position_reissue_interval_s set to {self.config.position_reissue_interval_s:g} s."
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
            raise ValueError(
                "Use set x <um>, set y <um>, set z <um>, set xy <X> <Y>, set xyz <X> <Y> <Z>, "
                "set tolerance <um>, set timeout <s>, or set reissue <s>."
            )

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
        scan = self.evaluate_scan(refresh=False)
        return SamplePrealignResult(
            x_um=x,
            y_um=y,
            z_um=z,
            scan_range_x_um=float(self.config.scan_range_x_um),
            scan_range_y_um=float(self.config.scan_range_y_um),
            step_um=float(self.config.step_um),
            scan_pattern=str(self.config.scan_pattern),
            next_action=self.next_action,
            scan_ok=bool(scan.get("ok")),
            scan_error=str(scan.get("error", "")),
        )


def normalize_scan_variable(text):
    normalized = str(text).strip().upper()
    aliases = {"XRANGE": "SCAN_RANGE_X_UM", "RANGEX": "SCAN_RANGE_X_UM", "YRANGE": "SCAN_RANGE_Y_UM", "RANGEY": "SCAN_RANGE_Y_UM", "PATTERN": "SCAN_PATTERN"}
    return aliases.get(normalized, normalized)


def section_lines(title, items):
    return format_section_lines(title, items)


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
  probe / open / mdt693b             switch to the open-loop probe panel, if enabled
  p1 / a                             mark the current X,Y as corner A of the scan rectangle
  p2 / b                             mark corner B; once both corners exist the rectangle is applied
  rect x1 y1 x2 y2                   set both corners numerically and apply the rectangle
  rect                               re-apply the rectangle from the marked corners
  rect clear                         drop the marked corners
  set SCAN_RANGE_X_UM <um>           set scan range along X/up for this run
  set SCAN_RANGE_Y_UM <um>           set scan range along Y/left for this run
  set STEP_UM <um>                   set image pixel step for this run
  set xstep/ystep/zstep <um>         set manual closed-loop move steps
  set return_step <um>               longest leg used to travel back to the scan start corner
  set x/y/z/xy/xyz ...               set absolute closed-loop position(s) in um
  set interval <sec>                 hotkey polling interval
  set refresh <sec>                  automatic screen redraw interval
  set SETTLE_MS <ms>
  set tolerance <um>                 closed-loop readback tolerance before reporting move done
  set timeout <sec>                  closed-loop move timeout
  set reissue <sec>                  resend target position while outside tolerance; 0 disables resend
  laser refresh                      read 532/TOPTICA status only
  532 emission on/off                explicitly change CBOX emission
  532 trigger ext/int                explicitly change CBOX trigger source
  532 close-at-end on/off            choose whether final cleanup closes 532 emission
  toptica cc/pc/external/scan on/off explicitly change TOPTICA controls
  toptica close-at-end on/off        choose whether final cleanup runs TOPTICA safe off
  q / quit / cancel                  abort before acquisition

Two-point rectangle scan:
  Move the stage to one corner of the region you want (arrow keys, 'set x/y', or the
  controller's hand pad), type p1, move to the opposite corner, type p2. The panel then
  snaps the span to a whole number of STEP_UM steps, sets the scan ranges, forces the
  S-shaped (serpentine) pattern, moves to the start corner, and shows the shape. Type
  start to begin. The start corner is the low corner for the configured scan directions.
  If the corners leave the stage at the far end, the travel back to the start corner is
  split into legs of at most RECT_RETURN_STEP_UM (default 1 um) so the stage never makes
  one long jump; each leg settles before the next one is issued.
""".strip()


def run_sample_prealignment(stage, config, log_callback=None, status_provider=None, display_params=None):
    if os.name != "nt":
        print("Prealignment keyboard panel requires a Windows console; using current closed-loop position.")
        return SamplePrealignPanel(stage, config, log_callback=log_callback, status_provider=status_provider, display_params=display_params).result()

    import msvcrt

    panel = SamplePrealignPanel(stage, config, log_callback=log_callback, status_provider=status_provider, display_params=display_params)
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
    last_status_signature = panel.status_signature()

    def redraw(refresh=True):
        nonlocal last_render, last_status_signature
        panel.render(refresh=refresh)
        last_render = time.time()
        last_status_signature = panel.status_signature()

    running = True
    while running:
        char = read_last_command_key(msvcrt)
        if char:
            if char in ("q", "Q"):
                panel.ready_to_start = False
                panel.next_action = "quit"
                panel.message = "Leaving prealignment before acquisition; no scan will be started."
                panel.log("PREALIGN_QUIT_REQUESTED", source="hotkey_q")
                running = False
                redraw(refresh=True)
                continue
            if char in ("h", "H", "?"):
                redraw(refresh=True)
            elif char in ("s", "S"):
                panel.refresh_lasers()
                panel.message = "Status refreshed."
                redraw(refresh=True)
            elif char in ("0", "r", "R"):
                if panel.set_xyz(x=0.0, y=0.0, reason="hotkey_move_xy_to_zero"):
                    drain_keyboard_buffer(msvcrt)
                    redraw(refresh=True)
            elif char == "+":
                if panel.move_delta(z_delta=panel.z_step_um, reason="hotkey_z_plus"):
                    drain_keyboard_buffer(msvcrt)
                    redraw(refresh=True)
            elif char == "-":
                if panel.move_delta(z_delta=-panel.z_step_um, reason="hotkey_z_minus"):
                    drain_keyboard_buffer(msvcrt)
                    redraw(refresh=True)
            elif char == ":":
                panel.renderer.show_cursor()
                sys.stdout.write("\ncmd> ")
                sys.stdout.flush()
                line = input()
                running = panel.execute_command(line)
                redraw(refresh=True)

        if running:
            x_delta, y_delta, reason = panel.sample_arrow_delta()
            if x_delta or y_delta:
                if panel.move_delta(x_delta=x_delta, y_delta=y_delta, reason=f"key_state_{reason}"):
                    drain_keyboard_buffer(msvcrt)
                    redraw(refresh=True)
            else:
                status_signature = panel.status_signature()
                if status_signature != last_status_signature:
                    redraw(refresh=False)
                elif time.time() - last_render >= panel.auto_refresh_s:
                    redraw(refresh=True)
        time.sleep(panel.sample_interval_s)

    panel.renderer.show_cursor()
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
        next_action=result.next_action,
        scan_ok=result.scan_ok,
        scan_error=result.scan_error,
    )
    return result
