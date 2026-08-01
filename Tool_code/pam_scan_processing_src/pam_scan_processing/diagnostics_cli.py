from __future__ import annotations

import argparse
from pathlib import Path

from .core import parse_slice
from .diagnostics import write_arrival_diagnostics, write_explicit_point_arrival_diagnostics, write_y_endpoint_arrival_diagnostics


def _parse_point_spec(text: str) -> tuple[float, float, float | None]:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) not in (2, 3):
        raise argparse.ArgumentTypeError("Point specs must be x,y or x,y,z")
    values = [float(part) for part in parts]
    if len(values) == 2:
        values.append(None)
    return values[0], values[1], values[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate 1D diagnostic plots for PAM arrival-time detection.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="One PAM .mat file.")
    group.add_argument("--input-dir", type=Path, help="Directory containing PAM .mat files.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for diagnostic plots.")
    parser.add_argument(
        "--arrival-window",
        "--target-window",
        dest="arrival_window",
        default="100:700",
        help="Early sample window used to build each scan-line arrival prior. --target-window is a compatibility alias.",
    )
    parser.add_argument("--baseline", default="0:100", help="Sample slice used for noise/baseline estimation.")
    parser.add_argument("--smooth-sigma", type=float, default=3.0, help="Gaussian smoothing sigma for arrival envelope.")
    parser.add_argument("--threshold-sigma", type=float, default=5.0, help="Baseline-envelope sigma multiplier for the shared line-prior arrival threshold.")
    parser.add_argument("--points", type=int, default=12, help="Number of representative point waveforms to plot per file.")
    parser.add_argument("--y-endpoints", type=float, help="Instead of representative points, plot the first and last X points near this Y coordinate.")
    parser.add_argument("--point", action="append", type=_parse_point_spec, help="Instead of representative points, plot an explicit x,y or x,y,z point. Can be repeated.")
    parser.add_argument("--coordinate-tolerance", type=float, default=1e-9, help="Coordinate matching tolerance for --y-endpoints and --point.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    target_window = parse_slice(args.arrival_window, 100, 700)
    baseline = parse_slice(args.baseline, 0, 100)
    if args.input:
        files = [args.input]
    else:
        files = sorted(args.input_dir.glob("*.mat"))
        if not files:
            raise FileNotFoundError(f"No .mat files found in {args.input_dir}")

    for path in files:
        print(f"Writing diagnostics for {path.name} ...")
        if args.y_endpoints is not None and args.point:
            raise ValueError("Use either --y-endpoints or --point, not both.")
        if args.y_endpoints is not None:
            result = write_y_endpoint_arrival_diagnostics(
                path=path,
                output_dir=args.output_dir,
                y=args.y_endpoints,
                target_window=target_window,
                baseline=baseline,
                smooth_sigma=args.smooth_sigma,
                threshold_sigma=args.threshold_sigma,
                tolerance=args.coordinate_tolerance,
            )
            print(result["figure"])
        elif args.point:
            result = write_explicit_point_arrival_diagnostics(
                path=path,
                output_dir=args.output_dir,
                point_specs=args.point,
                target_window=target_window,
                baseline=baseline,
                smooth_sigma=args.smooth_sigma,
                threshold_sigma=args.threshold_sigma,
                tolerance=args.coordinate_tolerance,
            )
            print(result["figure"])
        else:
            result = write_arrival_diagnostics(
                path=path,
                output_dir=args.output_dir,
                target_window=target_window,
                baseline=baseline,
                smooth_sigma=args.smooth_sigma,
                threshold_sigma=args.threshold_sigma,
                point_count=args.points,
            )
            print(result["profile"])
            print(result["waveforms"])
        print(result["table"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
