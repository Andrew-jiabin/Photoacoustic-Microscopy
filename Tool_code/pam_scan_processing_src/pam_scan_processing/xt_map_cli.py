from __future__ import annotations

import argparse
from pathlib import Path

from .core import parse_slice
from .xt_map import write_x_time_absolute_map


def _parse_float_range(text: str) -> tuple[float, float]:
    if ":" not in text:
        raise argparse.ArgumentTypeError("Expected START:STOP")
    start, stop = text.split(":", 1)
    return float(start), float(stop)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot a non-negative X-time absolute-amplitude map for one PAM Y scan line.")
    parser.add_argument("--input", required=True, type=Path, help="One PAM .mat file.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for the PNG and JSON summary.")
    parser.add_argument("--y", required=True, type=float, help="Y coordinate of the scan line.")
    parser.add_argument("--display-window", default="0:end", help="Sample range to draw, for example 0:end or 0:800.")
    parser.add_argument("--baseline", default="0:100", help="Per-point baseline slice removed before taking absolute values.")
    parser.add_argument("--time-step", type=int, default=1, help="Sample stride along the time axis.")
    parser.add_argument("--clip-percentile", type=float, default=99.5, help="Upper color-limit percentile; values remain non-negative.")
    parser.add_argument("--coordinate-tolerance", type=float, default=1e-9, help="Tolerance for matching the requested Y coordinate.")
    parser.add_argument("--x-range", type=_parse_float_range, help="Optional inclusive X range, for example 1.5:2.5.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_x_time_absolute_map(
        path=args.input,
        output_dir=args.output_dir,
        y=args.y,
        display_window=parse_slice(args.display_window, 0, -1),
        baseline=parse_slice(args.baseline, 0, 100),
        time_step=args.time_step,
        clip_percentile=args.clip_percentile,
        coordinate_tolerance=args.coordinate_tolerance,
        x_range=args.x_range,
    )
    print(result["figure"])
    print(result["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
