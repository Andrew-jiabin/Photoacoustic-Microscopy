from __future__ import annotations

import argparse
from pathlib import Path

from .core import parse_slice
from .interactive import write_index_html, write_interactive_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate draggable browser-based 3D PAM waveform and arrival views.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input", type=Path, help="One PAM .mat file.")
    group.add_argument("--input-dir", type=Path, help="Directory containing PAM .mat files.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for interactive HTML files.")
    parser.add_argument(
        "--arrival-window",
        "--target-window",
        dest="arrival_window",
        default="100:700",
        help="Early sample window used to build each scan-line arrival prior. --target-window is a compatibility alias.",
    )
    parser.add_argument("--display-window", default="0:end", help="Waveform samples embedded in the 3D view.")
    parser.add_argument("--baseline", default="0:100", help="Sample slice used for noise/baseline estimation.")
    parser.add_argument("--time-step", type=int, default=8, help="Sample stride for interactive waveform points.")
    parser.add_argument("--max-traces", type=int, default=500, help="Maximum waveform traces shown as interactive samples.")
    parser.add_argument(
        "--max-waveform-points",
        type=int,
        default=150_000,
        help="Maximum total waveform samples embedded in the browser layer; the time axis is automatically decimated if needed.",
    )
    parser.add_argument("--smooth-sigma", type=float, default=3.0, help="Gaussian smoothing sigma for arrival detection.")
    parser.add_argument("--threshold-sigma", type=float, default=5.0, help="Baseline-envelope sigma multiplier for the shared line-prior arrival threshold.")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="Default-visible marker confidence cutoff; lower-confidence markers stay in an audit legend layer.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    arrival_window = parse_slice(args.arrival_window, 100, 700)
    display_window = parse_slice(args.display_window, 0, -1)
    baseline = parse_slice(args.baseline, 0, 100)

    if args.input:
        files = [args.input]
    else:
        files = sorted(args.input_dir.glob("*.mat"))
        if not files:
            raise FileNotFoundError(f"No .mat files found in {args.input_dir}")

    output_paths = []
    for path in files:
        print(f"Writing interactive 3D HTML for {path.name} ...")
        output_path = write_interactive_html(
            path=path,
            output_dir=args.output_dir,
            arrival_window=arrival_window,
            display_window=display_window,
            baseline=baseline,
            time_step=args.time_step,
            max_traces=args.max_traces,
            max_waveform_points=args.max_waveform_points,
            smooth_sigma=args.smooth_sigma,
            threshold_sigma=args.threshold_sigma,
            min_marker_confidence=args.min_confidence,
        )
        output_paths.append(output_path)
        print(output_path)
    if len(output_paths) > 1:
        index_path = write_index_html(args.output_dir, output_paths)
        print(f"Index: {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
