"""
Open-loop MAX312D NanoMax YZ full-range voltage scan test.

The MAX312D stage has no position feedback. MDT693B commands are voltages, not
measured positions. The NanoMax manual specifies 20 um piezo travel at a nominal
maximum input voltage of 75 V, so this script labels voltage targets with only an
approximate displacement.

Default scan:
    Y/Z voltages cover 0..75 V, about 0..20 um of NanoMax piezo travel.
    Step is about 5 um, equivalent to 18.75 V, for 25 total YZ points.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


MAX312D_PIEZO_TRAVEL_UM = 20.0
MAX312D_NOMINAL_MAX_INPUT_V = 75.0
DEFAULT_MDT_SERIAL_NO = "2201287140-09"
DEFAULT_DLL_PATH = REPO_ROOT / "Alazar_imaging" / "MDT_COMMAND_LIB_x64.dll"


def build_axis_values(start_v: float, stop_v: float, step_v: float) -> list[float]:
    if step_v <= 0:
        raise ValueError(f"step_v must be positive, got {step_v}")
    if stop_v < start_v:
        raise ValueError(f"stop_v must be >= start_v, got {stop_v} < {start_v}")

    values = []
    current = float(start_v)
    stop = float(stop_v)
    while current < stop - 1e-9:
        values.append(round(current, 6))
        current += step_v
    if not values or abs(values[-1] - stop) > 1e-9:
        values.append(round(stop, 6))
    return values


def resolve_pattern(pattern: str) -> str:
    normalized = pattern.strip().lower()
    if normalized in ("raster", "z", "unidirectional"):
        return "raster"
    if normalized in ("serpentine", "s", "snake"):
        return "serpentine"
    raise ValueError("pattern must be raster/z or serpentine/s")


def build_yz_trajectory(y_values: list[float], z_values: list[float], pattern: str) -> list[tuple[float, float]]:
    resolved = resolve_pattern(pattern)
    trajectory = []
    for row_index, z_v in enumerate(z_values):
        row_y = y_values
        if resolved == "serpentine" and row_index % 2 == 1:
            row_y = list(reversed(y_values))
        for y_v in row_y:
            trajectory.append((y_v, z_v))
    return trajectory


def voltage_to_um(voltage_v: float, max_voltage_v: float) -> float:
    return float(voltage_v) / float(max_voltage_v) * MAX312D_PIEZO_TRAVEL_UM


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan the open-loop MAX312D NanoMax probe stage over the full YZ piezo range."
    )
    parser.add_argument("--serial", default=DEFAULT_MDT_SERIAL_NO, help="MDT693B serial number.")
    parser.add_argument("--dll", default=str(DEFAULT_DLL_PATH), help="Path to MDT_COMMAND_LIB_x64.dll.")
    parser.add_argument("--max-voltage", type=float, default=MAX312D_NOMINAL_MAX_INPUT_V)
    parser.add_argument("--step-um", type=float, default=5.0, help="Approximate Y/Z step in um.")
    parser.add_argument("--step-v", type=float, default=None, help="Voltage step; overrides --step-um.")
    parser.add_argument(
        "--pattern",
        default="raster",
        choices=("raster", "z", "unidirectional", "serpentine", "s", "snake"),
        help="Raster keeps each Y row in the same direction; serpentine alternates rows.",
    )
    parser.add_argument("--settle-ms", type=int, default=250, help="Delay after each voltage set.")
    parser.add_argument("--dwell-ms", type=int, default=500, help="Extra dwell at each YZ point.")
    parser.add_argument("--dry-run", action="store_true", help="Print the trajectory without connecting or moving.")
    parser.add_argument("--yes", action="store_true", help="Skip the RUN confirmation prompt.")
    parser.add_argument("--leave-at-last", action="store_true", help="Do not return Y/Z to 0 V at the end.")
    parser.add_argument(
        "--keep-axis-limits",
        action="store_true",
        help="Do not raise MDT Y/Z axis max limits to --max-voltage before scanning.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_voltage = float(args.max_voltage)
    if max_voltage <= 0 or max_voltage > MAX312D_NOMINAL_MAX_INPUT_V:
        raise SystemExit(f"--max-voltage must be in (0, {MAX312D_NOMINAL_MAX_INPUT_V}] V")

    step_v = args.step_v
    if step_v is None:
        step_v = float(args.step_um) / MAX312D_PIEZO_TRAVEL_UM * max_voltage
    step_v = float(step_v)

    y_values = build_axis_values(0.0, max_voltage, step_v)
    z_values = build_axis_values(0.0, max_voltage, step_v)
    pattern = resolve_pattern(args.pattern)
    trajectory = build_yz_trajectory(y_values, z_values, pattern)

    print("Open-loop MAX312D YZ full-range test")
    print(f"NanoMax piezo travel: about {MAX312D_PIEZO_TRAVEL_UM:g} um per axis")
    print(f"Voltage range: 0..{max_voltage:g} V")
    print(f"Step: {step_v:g} V, about {voltage_to_um(step_v, max_voltage):g} um")
    print(f"Pattern: {pattern}")
    print(f"Points: {len(trajectory)} ({len(y_values)} Y targets x {len(z_values)} Z targets)")
    print("First targets:")
    for index, (y_v, z_v) in enumerate(trajectory[: min(10, len(trajectory))], start=1):
        print(
            f"  {index:02d}: Y={y_v:7.3f} V ({voltage_to_um(y_v, max_voltage):6.2f} um approx), "
            f"Z={z_v:7.3f} V ({voltage_to_um(z_v, max_voltage):6.2f} um approx)"
        )
    if len(trajectory) > 10:
        print(f"  ... {len(trajectory) - 10} more targets")

    if args.dry_run:
        print("Dry run only; no MDT693B connection was opened and no voltage was changed.")
        return

    if not args.yes:
        answer = input("This will move open-loop Y/Z over 0..75 V. Type RUN to continue: ").strip()
        if answer != "RUN":
            print("Cancelled; no voltage was changed.")
            return

    from Alazar_imaging.MDT693BController import MDT693BController

    stage = MDT693BController(serial_no=args.serial, dll_path=args.dll, safe_max_voltage=max_voltage)
    start_x, start_y, start_z = [float(value) for value in stage.get_voltage_xyz()]
    print(f"Start voltages: X={start_x:.3f} V, Y={start_y:.3f} V, Z={start_z:.3f} V")
    try:
        y_axis_max = stage.get_axis_max_voltage("y")
        z_axis_max = stage.get_axis_max_voltage("z")
        print(f"Initial MDT axis max limits: YMAX={y_axis_max:.3f} V, ZMAX={z_axis_max:.3f} V")
        if not args.keep_axis_limits:
            if y_axis_max < max_voltage - 0.25:
                y_axis_max = stage.set_axis_max_voltage("y", max_voltage)
                print(f"Raised YMAX to {y_axis_max:.3f} V")
            if z_axis_max < max_voltage - 0.25:
                z_axis_max = stage.set_axis_max_voltage("z", max_voltage)
                print(f"Raised ZMAX to {z_axis_max:.3f} V")
        if min(y_axis_max, z_axis_max) < max_voltage - 0.25:
            raise RuntimeError(
                f"Y/Z axis max limits are too low for {max_voltage:g} V scan: "
                f"YMAX={y_axis_max:.3f} V, ZMAX={z_axis_max:.3f} V"
            )
    except AttributeError:
        print("MDT axis max limit check is unavailable for this controller backend.")
    print("Holding X at its starting voltage and scanning Y/Z.")

    try:
        stage.set_voltage_xyz(y=0.0, z=0.0, wait=True, settle_time_ms=args.settle_ms)
        for point_index, (y_v, z_v) in enumerate(trajectory, start=1):
            stage.set_voltage_xyz(y=y_v, z=z_v, wait=True, settle_time_ms=args.settle_ms)
            print(
                f"{point_index:04d}/{len(trajectory)}: "
                f"Y={y_v:7.3f} V ({voltage_to_um(y_v, max_voltage):6.2f} um approx), "
                f"Z={z_v:7.3f} V ({voltage_to_um(z_v, max_voltage):6.2f} um approx)"
            )
            if args.dwell_ms > 0:
                time.sleep(args.dwell_ms / 1000.0)
    finally:
        if not args.leave_at_last:
            try:
                print("Returning open-loop Y/Z to 0 V; X is restored to its starting voltage.")
                stage.set_voltage_xyz(x=start_x, y=0.0, z=0.0, wait=True, settle_time_ms=args.settle_ms)
                final_x, final_y, final_z = [float(value) for value in stage.get_voltage_xyz()]
                print(f"Final voltages: X={final_x:.3f} V, Y={final_y:.3f} V, Z={final_z:.3f} V")
            except Exception as exc:
                print(f"Return-to-low-end failed: {exc}")
        stage.close()


if __name__ == "__main__":
    main()
