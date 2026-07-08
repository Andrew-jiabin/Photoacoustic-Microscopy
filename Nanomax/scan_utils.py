from Nanomax.run_log import append_run_log


NANOMAX_MANUAL_MIN_STEP_UM = 0.01  # MAX300 manual: piezo resolution is approximately 10 nm.
NANOMAX_PIEZO_SCAN_LIMIT_UM = 20.0  # MAX311D/MAX312D built-in piezo travel used for imaging.


def resolve_probe_step_v(step_um, probe_step_v, probe_um_per_v):
    """Return the open-loop probe scan step in volts per pixel."""
    if probe_step_v is not None:
        return float(probe_step_v)
    if probe_um_per_v is not None:
        return float(step_um) / float(probe_um_per_v)
    raise ValueError(
        "Probe open-loop scan requires PROBE_STEP_V, or PROBE_UM_PER_V "
        "so STEP_UM can be converted to volts."
    )


def validate_scan_step(step_um, min_step_um=NANOMAX_MANUAL_MIN_STEP_UM):
    step_um = float(step_um)
    if step_um <= 0:
        raise ValueError(f"STEP_UM must be positive, got {step_um}.")
    if step_um < min_step_um:
        raise ValueError(
            f"STEP_UM={step_um:g} um is below the NanoMax manual piezo resolution "
            f"of approximately {min_step_um:g} um (10 nm). Increase STEP_UM."
        )
    return step_um


def scan_shape_from_range(scan_range_x_um, scan_range_y_um, step_um, max_range_um=None):
    """Convert user requested scan travel to point counts, including both endpoints."""
    step_um = validate_scan_step(step_um)
    ranges = {"SCAN_RANGE_X_UM": float(scan_range_x_um), "SCAN_RANGE_Y_UM": float(scan_range_y_um)}
    max_range = None if max_range_um is None else float(max_range_um)
    for name, value in ranges.items():
        if value < 0:
            raise ValueError(f"{name} must be >= 0 um, got {value}.")
        if max_range is not None and value > max_range + 1e-9:
            raise ValueError(
                f"{name}={value:g} um exceeds the NanoMax built-in piezo imaging limit "
                f"of {max_range:g} um. Use the coarse/manual travel to reposition, then scan 0-{max_range:g} um."
            )

    shape = []
    for name, value in ranges.items():
        interval_count = value / step_um
        rounded = round(interval_count)
        if abs(interval_count - rounded) > 1e-9:
            raise ValueError(
                f"{name}={value:g} um must be an integer multiple of STEP_UM={step_um:g} um. "
                "This avoids silently scanning a smaller or uneven range."
            )
        shape.append(int(rounded) + 1)
    return shape[0], shape[1]


def resolve_scan_pattern(scan_pattern):
    """Return whether to use S-shaped serpentine scanning."""
    normalized = str(scan_pattern).strip().lower()
    if normalized in ("serpentine", "s", "snake"):
        return True, "serpentine/S-shaped"
    if normalized in ("raster", "z", "unidirectional"):
        return False, "raster/Z-shaped"
    raise ValueError("SCAN_PATTERN must be 'serpentine'/'s' or 'raster'/'z'.")


def build_sample_trajectory(start_x, start_y, scan_w, scan_h, step_um, x_direction=1.0, y_direction=1.0, serpentine=False):
    """Build closed-loop sample-stage targets in microns."""
    trajectory = []
    for h in range(scan_h):
        w_range = range(scan_w)
        if serpentine and h % 2 == 1:
            w_range = reversed(range(scan_w))
        for w in w_range:
            target_x = start_x + x_direction * w * step_um
            target_y = start_y + y_direction * h * step_um
            trajectory.append((target_x, target_y))
    return trajectory


def build_probe_trajectory(start_x, start_y, start_z, scan_w, scan_h, probe_step_v, x_direction=1.0, y_direction=1.0, serpentine=False):
    """Build open-loop probe-controller voltage targets."""
    trajectory = []
    for h in range(scan_h):
        w_range = range(scan_w)
        if serpentine and h % 2 == 1:
            w_range = reversed(range(scan_w))
        for w in w_range:
            target_x = start_x + x_direction * w * probe_step_v
            target_y = start_y + y_direction * h * probe_step_v
            trajectory.append((target_x, target_y, start_z))
    return trajectory


def validate_sample_trajectory(stage, trajectory):
    """Fail early if any closed-loop MAX311D target is outside native travel."""
    if not trajectory:
        raise ValueError("Empty sample trajectory.")
    max_x = float(stage.get_max_travel("x"))
    max_y = float(stage.get_max_travel("y"))
    violations = [(x, y) for x, y in trajectory if x < 0.0 or x > max_x or y < 0.0 or y > max_y]
    if violations:
        first_x, first_y = violations[0]
        raise ValueError(
            "Closed-loop sample scan exceeds the current BPC303/MAX311D travel limit. "
            f"First invalid target: X={first_x:.4f} um, Y={first_y:.4f} um; "
            f"valid ranges are X=[0,{max_x:.4f}] um, Y=[0,{max_y:.4f}] um. "
            "Reduce SCAN_RANGE_X_UM/SCAN_RANGE_Y_UM/STEP_UM, change scan direction, "
            "or move the stage start position."
        )


def clamp_low_end_residual(axis_name, value_um, tolerance_um):
    """Treat tiny closed-loop readback residuals after low-end zero as numeric noise."""
    value = float(value_um)
    tolerance = abs(float(tolerance_um))
    if abs(value) <= tolerance:
        if abs(value) > 0.0:
            append_run_log(
                "LOW_END_RESIDUAL_CLAMPED",
                axis=axis_name,
                raw_um=f"{value:.6f}",
                clamped_um="0.000000",
                tolerance_um=f"{tolerance:.6f}",
            )
        return 0.0
    append_run_log("LOW_END_RESIDUAL_TOO_LARGE", axis=axis_name, raw_um=f"{value:.6f}", tolerance_um=f"{tolerance:.6f}")
    return value


def validate_probe_trajectory(probe_stage, trajectory):
    """Fail early if any open-loop MDT693B target voltage is unsafe."""
    if not trajectory:
        raise ValueError("Empty probe trajectory.")
    limit_candidates = []
    if probe_stage.limit_voltage is not None:
        limit_candidates.append(float(probe_stage.limit_voltage))
    if probe_stage.safe_max_voltage is not None:
        limit_candidates.append(float(probe_stage.safe_max_voltage))
    max_voltage = min(limit_candidates) if limit_candidates else None
    if max_voltage is None:
        return

    violations = [
        (x, y, z)
        for x, y, z in trajectory
        if x < 0.0 or y < 0.0 or z < 0.0 or x > max_voltage or y > max_voltage or z > max_voltage
    ]
    if violations:
        first_x, first_y, first_z = violations[0]
        raise ValueError(
            "Open-loop probe scan exceeds MDT693B voltage range. "
            f"First invalid target: X={first_x:.4f} V, Y={first_y:.4f} V, Z={first_z:.4f} V; "
            f"valid voltage range is [0,{max_voltage:.4f}] V."
        )


def sample_scan_summary(start_x, start_y, scan_range_x_um, scan_range_y_um, step_um, x_direction=1.0, y_direction=1.0, serpentine=True, max_range_um=NANOMAX_PIEZO_SCAN_LIMIT_UM):
    scan_w, scan_h = scan_shape_from_range(scan_range_x_um, scan_range_y_um, step_um, max_range_um=max_range_um)
    trajectory = build_sample_trajectory(start_x, start_y, scan_w, scan_h, step_um, x_direction=x_direction, y_direction=y_direction, serpentine=serpentine)
    xs = [point[0] for point in trajectory]
    ys = [point[1] for point in trajectory]
    return {"scan_w": scan_w, "scan_h": scan_h, "points": len(trajectory), "x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys), "trajectory": trajectory}


def validate_sample_bounds_from_position(start_x, start_y, scan_range_x_um, scan_range_y_um, step_um, max_x, max_y, x_direction=1.0, y_direction=1.0, serpentine=True):
    summary = sample_scan_summary(start_x, start_y, scan_range_x_um, scan_range_y_um, step_um, x_direction=x_direction, y_direction=y_direction, serpentine=serpentine, max_range_um=None)
    errors = []
    if summary["x_min"] < 0.0 or summary["x_max"] > float(max_x):
        errors.append(f"SCAN_RANGE_X_UM gives X range [{summary['x_min']:.4f}, {summary['x_max']:.4f}] um outside [0,{float(max_x):.4f}] um")
    if summary["y_min"] < 0.0 or summary["y_max"] > float(max_y):
        errors.append(f"SCAN_RANGE_Y_UM gives Y range [{summary['y_min']:.4f}, {summary['y_max']:.4f}] um outside [0,{float(max_y):.4f}] um")
    return summary, errors

