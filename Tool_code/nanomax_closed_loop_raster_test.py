"""
Closed-loop NanoMax movement test for BPC303 + MAX311D.

This script is intentionally standalone. It imports the existing
Alazar_imaging.BPC303NativeController wrapper. The default pattern jumps
between the four vertices of a 10 um x 10 um square so the movement is easier
to see by eye. Z is held at its current position.

Safety:
  - The default square is 10 um x 10 um, centered on the current X/Y position.
  - No motion is performed when this module is imported.
  - Running the script requires an Enter confirmation unless CONFIRM_BEFORE_MOVE
    is changed or --no-confirm is passed.
  - The script validates reported travel limits before the first move.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Alazar_imaging.BPC303NativeController import BPC303NativeController


# ========================= User-adjustable parameters =========================

SERIAL_NO = "71241834"
KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"

# Closed-loop sample stage wiring confirmed on 2026-07-01:
# X -> BPC303 CH1, Y -> BPC303 CH2, Z -> BPC303 CH3.
CHANNELS = (1, 2, 3)
AXIS_MAP = {"x": 1, "y": 2, "z": 3}

# Pattern geometry. The default "corners" pattern jumps between the four
# vertices of a square. The old raster pattern is still available with
# --pattern raster.
SCAN_PATTERN = "corners"  # "corners" or "raster"
SQUARE_SIZE_UM = 10.0

# Raster geometry. Used only when SCAN_PATTERN is "raster".
# SCAN_RANGE_UM is the full width/height, not half range.
SCAN_RANGE_X_UM = 4.0
SCAN_RANGE_Y_UM = 4.0
STEP_UM = 0.5
LOOP_COUNT = 1
CONTINUOUS_LOOP = False  # True: repeat raster loops until Ctrl+C.
SERPENTINE = True

# Use the current closed-loop position as the scan center. If this is False,
# CENTER_X_UM and CENTER_Y_UM must be set.
CENTER_ON_CURRENT_POSITION = True
CENTER_X_UM = None
CENTER_Y_UM = None

# Motion-rate controls. BPC303 position commands do not expose a normal motor
# velocity setting, so scan speed is controlled by command spacing, small
# interpolated steps, settle time, and dwell time.
MAX_COMMAND_STEP_UM = 0.25
COMMAND_INTERVAL_MS = 20
SETTLE_MS = 120
DWELL_MS = 1000
LOOP_DELAY_MS = 250
TOLERANCE_UM = 0.05
MOVE_TIMEOUT_S = 10.0

RETURN_TO_START = True
CONFIRM_BEFORE_MOVE = True
ADJUST_WINDOW_TO_TRAVEL = True

# MAX311D NanoMax piezo input limit is 75 V according to Thorlabs 10997-D02.
# If Kinesis reports a larger accessible output range, this script refuses to
# move until the controller/stage configuration has been checked.
SAFE_MAX_OUTPUT_VOLTAGE = 75.0
ENFORCE_SAFE_MAX_OUTPUT_VOLTAGE = True

# Set DRY_RUN = True to print the planned path without connecting to hardware.
DRY_RUN = False
DRY_RUN_START_X_UM = 10.0
DRY_RUN_START_Y_UM = 10.0
DRY_RUN_START_Z_UM = 10.0


# =============================== Implementation ===============================


@dataclass(frozen=True)
class ScanConfig:
    serial_no: str
    kinesis_dir: str
    scan_pattern: str
    square_size_um: float
    scan_range_x_um: float
    scan_range_y_um: float
    step_um: float
    loop_count: int
    continuous_loop: bool
    serpentine: bool
    center_on_current_position: bool
    center_x_um: Optional[float]
    center_y_um: Optional[float]
    max_command_step_um: float
    command_interval_ms: float
    settle_ms: float
    dwell_ms: float
    loop_delay_ms: float
    tolerance_um: float
    move_timeout_s: float
    return_to_start: bool
    confirm_before_move: bool
    adjust_window_to_travel: bool
    safe_max_output_voltage: float
    enforce_safe_max_output_voltage: bool
    dry_run: bool


def build_config(args: argparse.Namespace) -> ScanConfig:
    return ScanConfig(
        serial_no=args.serial_no,
        kinesis_dir=args.kinesis_dir,
        scan_pattern=args.pattern,
        square_size_um=args.square_size_um,
        scan_range_x_um=args.range_x_um,
        scan_range_y_um=args.range_y_um,
        step_um=args.step_um,
        loop_count=args.loops,
        continuous_loop=args.continuous_loop,
        serpentine=not args.no_serpentine,
        center_on_current_position=not args.absolute_center,
        center_x_um=args.center_x_um,
        center_y_um=args.center_y_um,
        max_command_step_um=args.max_command_step_um,
        command_interval_ms=args.command_interval_ms,
        settle_ms=args.settle_ms,
        dwell_ms=args.dwell_ms,
        loop_delay_ms=args.loop_delay_ms,
        tolerance_um=args.tolerance_um,
        move_timeout_s=args.move_timeout_s,
        return_to_start=not args.no_return,
        confirm_before_move=not args.no_confirm,
        adjust_window_to_travel=not args.no_adjust_window_to_travel,
        safe_max_output_voltage=args.safe_max_output_voltage,
        enforce_safe_max_output_voltage=not args.no_voltage_limit_check,
        dry_run=args.dry_run,
    )


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def non_negative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be >= 0")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be > 0")
    return parsed


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a small closed-loop XY raster on BPC303/MAX311D.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--serial-no", default=SERIAL_NO)
    parser.add_argument("--kinesis-dir", default=KINESIS_DIR)
    parser.add_argument("--pattern", choices=("corners", "raster"), default=SCAN_PATTERN)
    parser.add_argument("--square-size-um", type=positive_float, default=SQUARE_SIZE_UM)
    parser.add_argument("--range-x-um", type=positive_float, default=SCAN_RANGE_X_UM)
    parser.add_argument("--range-y-um", type=positive_float, default=SCAN_RANGE_Y_UM)
    parser.add_argument("--step-um", type=positive_float, default=STEP_UM)
    parser.add_argument("--loops", type=positive_int, default=LOOP_COUNT)
    loop_mode = parser.add_mutually_exclusive_group()
    loop_mode.add_argument(
        "--continuous-loop",
        dest="continuous_loop",
        action="store_true",
        default=CONTINUOUS_LOOP,
        help="Repeat the raster continuously until Ctrl+C.",
    )
    loop_mode.add_argument(
        "--single-run",
        dest="continuous_loop",
        action="store_false",
        help="Run exactly --loops raster loops.",
    )
    parser.add_argument("--no-serpentine", action="store_true")
    parser.add_argument(
        "--absolute-center",
        action="store_true",
        help="Use --center-x-um/--center-y-um instead of current position.",
    )
    parser.add_argument("--center-x-um", type=float, default=CENTER_X_UM)
    parser.add_argument("--center-y-um", type=float, default=CENTER_Y_UM)
    parser.add_argument("--max-command-step-um", type=positive_float, default=MAX_COMMAND_STEP_UM)
    parser.add_argument("--command-interval-ms", type=non_negative_float, default=COMMAND_INTERVAL_MS)
    parser.add_argument("--settle-ms", type=non_negative_float, default=SETTLE_MS)
    parser.add_argument("--dwell-ms", type=non_negative_float, default=DWELL_MS)
    parser.add_argument("--loop-delay-ms", type=non_negative_float, default=LOOP_DELAY_MS)
    parser.add_argument("--tolerance-um", type=positive_float, default=TOLERANCE_UM)
    parser.add_argument("--move-timeout-s", type=positive_float, default=MOVE_TIMEOUT_S)
    parser.add_argument("--safe-max-output-voltage", type=positive_float, default=SAFE_MAX_OUTPUT_VOLTAGE)
    parser.add_argument("--no-voltage-limit-check", action="store_true")
    parser.add_argument("--no-return", action="store_true")
    parser.add_argument("--no-confirm", action="store_true")
    parser.add_argument(
        "--no-adjust-window-to-travel",
        action="store_true",
        help="Do not shift the 4 um scan window back inside the reported travel range.",
    )
    parser.add_argument("--dry-run", action="store_true", default=DRY_RUN)
    return parser.parse_args(argv)


def axis_values(center_um: float, scan_range_um: float, step_um: float) -> List[float]:
    start = center_um - scan_range_um / 2.0
    end = center_um + scan_range_um / 2.0
    count = int(math.floor(scan_range_um / step_um)) + 1
    values = [start + i * step_um for i in range(count)]
    if not math.isclose(values[-1], end, abs_tol=1e-9):
        values.append(end)
    return [round(v, 6) for v in values]


def generate_raster(
    center_x_um: float,
    center_y_um: float,
    range_x_um: float,
    range_y_um: float,
    step_um: float,
    serpentine: bool,
) -> List[Tuple[float, float]]:
    xs = axis_values(center_x_um, range_x_um, step_um)
    ys = axis_values(center_y_um, range_y_um, step_um)
    points: List[Tuple[float, float]] = []
    for row_index, y_um in enumerate(ys):
        row_xs: Iterable[float]
        if serpentine and row_index % 2 == 1:
            row_xs = reversed(xs)
        else:
            row_xs = xs
        for x_um in row_xs:
            points.append((x_um, y_um))
    return points


def generate_square_corners(center_x_um: float, center_y_um: float, square_size_um: float) -> List[Tuple[float, float]]:
    half_size = square_size_um / 2.0
    x_min = round(center_x_um - half_size, 6)
    x_max = round(center_x_um + half_size, 6)
    y_min = round(center_y_um - half_size, 6)
    y_max = round(center_y_um + half_size, 6)
    return [
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
    ]


def pattern_ranges(config: ScanConfig) -> Tuple[float, float]:
    if config.scan_pattern == "corners":
        return config.square_size_um, config.square_size_um
    if config.scan_pattern == "raster":
        return config.scan_range_x_um, config.scan_range_y_um
    raise ValueError(f"Unknown scan pattern: {config.scan_pattern}")


def generate_points(config: ScanConfig, center_x_um: float, center_y_um: float) -> List[Tuple[float, float]]:
    if config.scan_pattern == "corners":
        return generate_square_corners(center_x_um, center_y_um, config.square_size_um)
    if config.scan_pattern == "raster":
        return generate_raster(
            center_x_um,
            center_y_um,
            config.scan_range_x_um,
            config.scan_range_y_um,
            config.step_um,
            config.serpentine,
        )
    raise ValueError(f"Unknown scan pattern: {config.scan_pattern}")


def fit_center_to_travel(center_um: float, scan_range_um: float, max_travel_um: float) -> float:
    if scan_range_um > max_travel_um:
        raise ValueError(f"scan range {scan_range_um} um exceeds max travel {max_travel_um} um")
    half_range = scan_range_um / 2.0
    return min(max(center_um, half_range), max_travel_um - half_range)


def parse_position(position_text: str) -> Tuple[float, float, float]:
    values = [float(item) for item in position_text.split(",")[:3]]
    if len(values) < 3:
        raise RuntimeError(f"Expected x,y,z position string, got: {position_text!r}")
    return values[0], values[1], values[2]


def validate_config(config: ScanConfig) -> None:
    if config.scan_pattern == "raster" and config.step_um > max(config.scan_range_x_um, config.scan_range_y_um):
        raise ValueError("STEP_UM is larger than both scan ranges")
    if config.scan_pattern == "corners" and config.square_size_um <= 0:
        raise ValueError("SQUARE_SIZE_UM must be > 0")
    if config.max_command_step_um <= 0:
        raise ValueError("MAX_COMMAND_STEP_UM must be > 0")
    if not config.center_on_current_position:
        if config.center_x_um is None or config.center_y_um is None:
            raise ValueError("--absolute-center requires --center-x-um and --center-y-um")


def validate_limits(
    points: Sequence[Tuple[float, float]],
    max_travel_x_um: float,
    max_travel_y_um: float,
) -> None:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    problems = []
    if min_x < 0 or max_x > max_travel_x_um:
        problems.append(f"X scan [{min_x}, {max_x}] um outside travel [0, {max_travel_x_um}] um")
    if min_y < 0 or max_y > max_travel_y_um:
        problems.append(f"Y scan [{min_y}, {max_y}] um outside travel [0, {max_travel_y_um}] um")
    if problems:
        raise RuntimeError("; ".join(problems))


def validate_voltage_limits(stage: BPC303NativeController, config: ScanConfig) -> None:
    if not config.enforce_safe_max_output_voltage:
        return
    reported = {}
    for axis in ("x", "y", "z"):
        reported[axis] = stage.get_max_output_voltage(axis)
    too_high = {
        axis: voltage
        for axis, voltage in reported.items()
        if voltage > config.safe_max_output_voltage + 1e-9
    }
    if too_high:
        raise RuntimeError(
            "BPC303 reports max output above MAX311D safe limit. "
            f"reported={reported}, safe_limit={config.safe_max_output_voltage} V. "
            "Check controller voltage-range configuration before moving."
        )


def controlled_xy_move(
    stage: BPC303NativeController,
    current_xy: Tuple[float, float],
    target_xy: Tuple[float, float],
    config: ScanConfig,
) -> Tuple[float, float]:
    current_x, current_y = current_xy
    target_x, target_y = target_xy
    dx = target_x - current_x
    dy = target_y - current_y
    segment_count = max(1, int(math.ceil(max(abs(dx), abs(dy)) / config.max_command_step_um)))

    for segment_index in range(1, segment_count + 1):
        fraction = segment_index / segment_count
        intermediate_x = current_x + dx * fraction
        intermediate_y = current_y + dy * fraction
        stage.set_position([intermediate_x, intermediate_y])
        if config.command_interval_ms:
            time.sleep(config.command_interval_ms / 1000.0)

    stage.wait_until_settled(
        target_x,
        target_y,
        settle_time_ms=config.settle_ms,
        tolerance_step=config.tolerance_um,
        timeout_s=config.move_timeout_s,
    )
    if config.dwell_ms:
        time.sleep(config.dwell_ms / 1000.0)
    return target_x, target_y


def print_plan(
    config: ScanConfig,
    start_xyz: Tuple[float, float, float],
    points: Sequence[Tuple[float, float]],
) -> None:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    print("Closed-loop NanoMax movement test plan")
    print(f"  serial: {config.serial_no}")
    print(f"  pattern: {config.scan_pattern}")
    print(f"  start position: X={start_xyz[0]:.6f} um, Y={start_xyz[1]:.6f} um, Z={start_xyz[2]:.6f} um")
    print(f"  X range: {min(xs):.6f} .. {max(xs):.6f} um")
    print(f"  Y range: {min(ys):.6f} .. {max(ys):.6f} um")
    if config.scan_pattern == "corners":
        print(f"  square_size_um: {config.square_size_um}")
    print(f"  points per loop: {len(points)}")
    if config.continuous_loop:
        print("  loops: continuous until Ctrl+C")
        print("  total points: unbounded")
    else:
        print(f"  loops: {config.loop_count}")
        print(f"  total points: {len(points) * config.loop_count}")
    if config.scan_pattern == "raster":
        print(f"  step_um: {config.step_um}")
    print(f"  max_command_step_um: {config.max_command_step_um}")
    print(f"  command_interval_ms: {config.command_interval_ms}")
    print(f"  settle_ms: {config.settle_ms}, dwell_ms: {config.dwell_ms}")
    print(f"  serpentine: {config.serpentine}")
    print(f"  return_to_start: {config.return_to_start}")
    print(f"  adjust_window_to_travel: {config.adjust_window_to_travel}")


def run_scan(config: ScanConfig) -> None:
    validate_config(config)

    if config.dry_run:
        start_xyz = (DRY_RUN_START_X_UM, DRY_RUN_START_Y_UM, DRY_RUN_START_Z_UM)
        center_x, center_y = start_xyz[0], start_xyz[1]
        range_x_um, range_y_um = pattern_ranges(config)
        if config.adjust_window_to_travel:
            center_x = fit_center_to_travel(center_x, range_x_um, 20.0)
            center_y = fit_center_to_travel(center_y, range_y_um, 20.0)
        points = generate_points(config, center_x, center_y)
        print_plan(config, start_xyz, points)
        print("DRY_RUN=True: no hardware connection or movement was performed.")
        return

    stage = BPC303NativeController(
        serial_no=config.serial_no,
        kinesis_dir=config.kinesis_dir,
        channels=CHANNELS,
        axis_map=AXIS_MAP,
    )

    start_xyz = (0.0, 0.0, 0.0)
    have_start_position = False
    motion_started = False
    try:
        start_xyz = parse_position(stage.get_position())
        have_start_position = True
        max_travel_x = stage.get_max_travel("x")
        max_travel_y = stage.get_max_travel("y")
        max_travel_z = stage.get_max_travel("z")
        validate_voltage_limits(stage, config)

        if config.center_on_current_position:
            center_x, center_y = start_xyz[0], start_xyz[1]
        else:
            center_x = float(config.center_x_um)
            center_y = float(config.center_y_um)

        requested_center = (center_x, center_y)
        range_x_um, range_y_um = pattern_ranges(config)
        if config.adjust_window_to_travel:
            center_x = fit_center_to_travel(center_x, range_x_um, max_travel_x)
            center_y = fit_center_to_travel(center_y, range_y_um, max_travel_y)
            if (center_x, center_y) != requested_center:
                print(
                    "Adjusted scan center to keep targets inside travel: "
                    f"requested=({requested_center[0]:.6f}, {requested_center[1]:.6f}) um, "
                    f"used=({center_x:.6f}, {center_y:.6f}) um"
                )

        points = generate_points(config, center_x, center_y)

        validate_limits(points, max_travel_x, max_travel_y)

        print(f"Reported travel: X={max_travel_x} um, Y={max_travel_y} um, Z={max_travel_z} um")
        print_plan(config, start_xyz, points)

        if config.confirm_before_move:
            response = input("Press Enter to start movement, or type q then Enter to abort: ").strip().lower()
            if response == "q":
                print("Aborted before motion.")
                return

        current_xy = (start_xyz[0], start_xyz[1])
        motion_started = True
        loop_index = 0
        while config.continuous_loop or loop_index < config.loop_count:
            loop_index += 1
            loop_label = f"{loop_index}/continuous" if config.continuous_loop else f"{loop_index}/{config.loop_count}"
            print(f"Starting loop {loop_label}")
            for point_index, target_xy in enumerate(points, start=1):
                current_xy = controlled_xy_move(stage, current_xy, target_xy, config)
                print(
                    f"loop {loop_label}, "
                    f"point {point_index}/{len(points)}: "
                    f"X={target_xy[0]:.6f} um, Y={target_xy[1]:.6f} um"
                )
            if config.loop_delay_ms and (config.continuous_loop or loop_index < config.loop_count):
                time.sleep(config.loop_delay_ms / 1000.0)

        if config.return_to_start:
            print("Returning to start X/Y position...")
            controlled_xy_move(stage, current_xy, (start_xyz[0], start_xyz[1]), config)
        print("Raster test finished.")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        if config.return_to_start and have_start_position and motion_started:
            print("Returning to start X/Y position after interrupt...")
            controlled_xy_move(stage, parse_position(stage.get_position())[:2], (start_xyz[0], start_xyz[1]), config)
    except Exception:
        if config.return_to_start and have_start_position and motion_started:
            print("Error during scan. Returning to start X/Y position before re-raising...")
            controlled_xy_move(stage, parse_position(stage.get_position())[:2], (start_xyz[0], start_xyz[1]), config)
        raise
    finally:
        stage.close()


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = parse_args(argv)
    config = build_config(args)
    run_scan(config)


if __name__ == "__main__":
    main()
