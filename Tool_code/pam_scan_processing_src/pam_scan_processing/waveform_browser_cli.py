from __future__ import annotations

import argparse
from pathlib import Path


def _parse_slice(text: str) -> tuple[int, int]:
    if ":" not in text:
        raise argparse.ArgumentTypeError("Expected START:STOP")
    start_text, stop_text = text.split(":", 1)
    start = 0 if not start_text.strip() else int(start_text)
    stop_token = stop_text.strip().lower()
    stop = -1 if stop_token in ("", "end", "all") else int(stop_token)
    return start, stop


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Browse one PAM .mat scan with mouse clicks or arrow keys and show the selected point waveform plus spectrum."
    )
    parser.add_argument("--input", required=True, type=Path, help="One PAM .mat file.")
    parser.add_argument("--sample-rate-hz", type=float, default=4e9, help="DAQ sample rate for time and frequency axes.")
    parser.add_argument("--frequency-max-ghz", type=float, default=1.0, help="Right plot frequency range.")
    parser.add_argument("--baseline", default="0:100", help="Baseline slice used when pressing c or using --centered.")
    parser.add_argument("--p2p-window", type=_parse_slice, help="Optional waveform slice used for the left peak-to-peak map.")
    parser.add_argument("--centered", action="store_true", help="Start with waveform minus baseline median.")
    parser.add_argument("--initial-row", type=int, help="Initial grid row index.")
    parser.add_argument("--initial-col", type=int, help="Initial grid column index.")
    parser.add_argument("--initial-x", type=float, help="Initial X coordinate; nearest sampled X is used.")
    parser.add_argument("--initial-y", type=float, help="Initial Y coordinate; nearest sampled Y is used.")
    parser.add_argument("--backend", help="Optional Matplotlib backend, for example QtAgg or TkAgg.")
    parser.add_argument("--save-preview", type=Path, help="Save the initial browser view to this PNG path.")
    parser.add_argument("--no-show", action="store_true", help="Do not open the GUI window; useful with --save-preview.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import matplotlib

    if args.backend:
        matplotlib.use(args.backend, force=True)
    elif args.no_show:
        matplotlib.use("Agg", force=True)

    from .waveform_browser import launch_waveform_browser

    launch_waveform_browser(
        path=args.input,
        sample_rate_hz=args.sample_rate_hz,
        frequency_max_ghz=args.frequency_max_ghz,
        baseline=_parse_slice(args.baseline),
        p2p_window=args.p2p_window,
        centered=args.centered,
        initial_row=args.initial_row,
        initial_col=args.initial_col,
        initial_x=args.initial_x,
        initial_y=args.initial_y,
        save_preview=args.save_preview,
        show=not args.no_show,
    )
    if args.save_preview:
        print(args.save_preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
