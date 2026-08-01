from __future__ import annotations

import argparse
from pathlib import Path

from .core import parse_slice
from .workflow import (
    DEFAULT_REMOTE_DATA_DIR,
    DEFAULT_REMOTE_HOST,
    run_workflow,
)


def _parse_window_list(text: str) -> tuple[tuple[int, int], ...]:
    windows = []
    for item in text.split(","):
        item = item.strip()
        if item:
            windows.append(parse_slice(item, 0, -1))
    if not windows:
        raise argparse.ArgumentTypeError("At least one sample window is required")
    return tuple(windows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the reusable PAM workflow: source resolution, global/pointwise arrivals, 3D views, diagnostics, X-time maps, and a manifest."
    )
    parser.add_argument("--input", action="append", default=[], help="Local or remote .mat path; repeat for multiple files, e.g. ./data/file.mat.")
    parser.add_argument("--input-dir", help="Local or remote directory containing .mat files, e.g. ./data.")
    parser.add_argument("--run-id", help="Workspace batch name; reuse it to add data to an existing project batch.")
    parser.add_argument("--skill-root", type=Path, default=None, help="Skill root; defaults to the installed package's data-processing-skill root.")
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST, help="SSH host alias for remote data.")
    parser.add_argument("--remote-data-dir", default=DEFAULT_REMOTE_DATA_DIR, help="Remote Windows data directory used for ./data references.")
    parser.add_argument("--arrival-window", default="100:700", help="Early arrival search window START:STOP.")
    parser.add_argument("--display-window", default="0:end", help="Waveform display window START:STOP.")
    parser.add_argument("--baseline", default="0:100", help="Baseline window START:STOP.")
    parser.add_argument("--time-step", type=int, default=4, help="Temporal stride for 3D waveform displays.")
    parser.add_argument("--max-traces", type=int, default=700, help="Maximum spatial waveform lines displayed.")
    parser.add_argument("--max-waveform-points", type=int, default=150_000, help="Maximum waveform samples embedded in interactive HTML.")
    parser.add_argument("--min-confidence", type=float, default=0.6, help="Confidence threshold used by both reported methods.")
    parser.add_argument("--x-time-windows", type=_parse_window_list, default=((0, -1), (0, 800)), help="Comma-separated X-time sample windows, e.g. 0:end,0:800.")
    parser.add_argument("--no-browser-preview", action="store_true", help="Skip the static preview of the arrow/mouse waveform browser.")
    parser.add_argument("--axis-time-map", action="store_true", help="Generate the interactive axis-time/XY checker map after standard outputs.")
    parser.add_argument(
        "--axis-time-display-window",
        default="0:4000",
        help="Sample window for axis-time map generation. 0:4000 covers 1 us at 4 GHz.",
    )
    parser.add_argument(
        "--axis-time-baseline",
        default="0:100",
        help="Baseline slice used for centering waveform amplitudes in axis-time map.",
    )
    parser.add_argument(
        "--axis-time-step",
        type=int,
        default=1,
        help="Sampling stride inside axis-time map.",
    )
    parser.add_argument(
        "--axis-time-clip-percentile",
        type=float,
        default=99.5,
        help="Upper color percentile used for axis-time map contrast.",
    )
    parser.add_argument(
        "--axis-time-mode",
        choices=("x", "y", "xy"),
        default="x",
        help="Default axis-time mode in the generated checker: x=fix-Y scan lines, y=fix-X scan lines, xy=XY projection.",
    )
    parser.add_argument(
        "--axis-time-hilbert",
        action="store_true",
        help="Use Hilbert envelope (abs(hilbert(centered_waveform))) instead of pointwise absolute waveform.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    skill_root = args.skill_root or Path(__file__).resolve().parents[4]
    manifest = run_workflow(
        skill_root=skill_root,
        input_specs=args.input,
        input_dir=args.input_dir,
        run_id=args.run_id,
        remote_host=args.remote_host,
        remote_data_dir=args.remote_data_dir,
        arrival_window=parse_slice(args.arrival_window, 100, 700),
        display_window=parse_slice(args.display_window, 0, -1),
        baseline=parse_slice(args.baseline, 0, 100),
        time_step=args.time_step,
        max_traces=args.max_traces,
        max_waveform_points=args.max_waveform_points,
        min_confidence=args.min_confidence,
        x_time_windows=args.x_time_windows,
        axis_time_map=args.axis_time_map,
        axis_time_display_window=parse_slice(args.axis_time_display_window, 0, 4000),
        axis_time_baseline=parse_slice(args.axis_time_baseline, 0, 100),
        axis_time_step=args.axis_time_step,
        axis_time_clip_percentile=args.axis_time_clip_percentile,
        axis_time_mode=args.axis_time_mode,
        axis_time_hilbert=args.axis_time_hilbert,
        browser_preview=not args.no_browser_preview,
    )
    print(f"Workflow complete: {manifest['run_id']}")
    print(manifest["outputs"]["result_root"])
    print(manifest["outputs"]["interactive_index"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
