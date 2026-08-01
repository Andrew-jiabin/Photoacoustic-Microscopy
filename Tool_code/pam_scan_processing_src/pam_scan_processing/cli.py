from __future__ import annotations

import argparse
from pathlib import Path

from .core import parse_slice, process_directory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate 3D PAM waveform and arrival-time views from Alazar .mat scans.")
    parser.add_argument("--input-dir", required=True, type=Path, help="Directory containing PAM .mat files.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for figures and tables.")
    parser.add_argument(
        "--arrival-window",
        "--target-window",
        dest="arrival_window",
        default="100:700",
        help="Early sample window used to build each scan-line arrival prior. --target-window is a compatibility alias.",
    )
    parser.add_argument("--display-window", default="0:end", help="Waveform samples drawn in 3D; use 0:end for the full record.")
    parser.add_argument("--baseline", default="0:100", help="Sample slice used for noise/baseline estimation.")
    parser.add_argument("--time-step", type=int, default=4, help="Sample stride for waveform line plotting.")
    parser.add_argument("--max-traces", type=int, default=700, help="Maximum waveform traces shown in waveform 3D figures.")
    parser.add_argument("--smooth-sigma", type=float, default=3.0, help="Gaussian smoothing sigma for the arrival envelope.")
    parser.add_argument("--threshold-sigma", type=float, default=5.0, help="Baseline-envelope sigma multiplier for the shared line-prior arrival threshold.")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="Minimum line-global confidence counted as a detected arrival.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    arrival_window = parse_slice(args.arrival_window, 100, 700)
    display_window = parse_slice(args.display_window, 0, -1)
    baseline = parse_slice(args.baseline, 0, 100)
    summaries = process_directory(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        arrival_window=arrival_window,
        display_window=display_window,
        baseline=baseline,
        time_step=args.time_step,
        max_traces=args.max_traces,
        smooth_sigma=args.smooth_sigma,
        threshold_sigma=args.threshold_sigma,
        min_confidence=args.min_confidence,
    )
    print(f"Done. Processed {len(summaries)} file(s).")
    print(f"Batch summary: {args.output_dir / 'batch_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
