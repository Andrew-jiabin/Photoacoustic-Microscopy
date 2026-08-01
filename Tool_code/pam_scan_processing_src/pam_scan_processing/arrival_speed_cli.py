from __future__ import annotations

import argparse
from pathlib import Path

from .arrival_speed import analyze_arrival_speed
from .core import parse_slice


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fit PAM first-arrival sample versus X and report an auditable apparent sound speed.")
    parser.add_argument("--input", required=True, type=Path, help="One PAM .mat file.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for point tables, fit summaries, and figures.")
    parser.add_argument("--sample-rate-hz", required=True, type=float, help="Acquisition sample rate in Hz; required because legacy MAT metadata omits it.")
    parser.add_argument("--sample-rate-source", default="explicit CLI value", help="Provenance text written to the summary JSON.")
    parser.add_argument("--y", type=float, help="Y row to analyze. If omitted, choose the row with the largest all-candidate OLS R-squared.")
    parser.add_argument("--baseline", default="0:100", help="Noise/baseline sample range.")
    parser.add_argument("--arrival-window", default="100:700", help="First-arrival search range.")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="Threshold separating trusted arrivals from audit-only candidates.")
    parser.add_argument("--coordinate-tolerance", type=float, default=1e-9, help="Tolerance for matching --y.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = analyze_arrival_speed(
        path=args.input,
        output_dir=args.output_dir,
        sample_rate_hz=args.sample_rate_hz,
        y=args.y,
        baseline=parse_slice(args.baseline, 0, 100),
        arrival_window=parse_slice(args.arrival_window, 100, 700),
        min_confidence=args.min_confidence,
        coordinate_tolerance=args.coordinate_tolerance,
        sample_rate_source=args.sample_rate_source,
    )
    fit = summary["fits"]["all_candidates_ols"]
    print(summary["outputs"]["summary"])
    print(summary["outputs"]["point_table"])
    print(summary["outputs"]["fit_figure"])
    print(f"All-candidate apparent speed: {fit.get('apparent_speed_m_per_s')} m/s")
    print(f"R-squared: {fit.get('r_squared')}")
    print(f"Validated physical speed: {summary['speed_validated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
