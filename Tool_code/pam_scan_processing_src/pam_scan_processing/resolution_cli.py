from __future__ import annotations

import argparse
from pathlib import Path

from .resolution import parse_optional_slice, write_long_axis_resolution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compute PAM long-axis resolution with MATLAB plot_line.m style ERF/LSF fitting.")
    parser.add_argument("--input", required=True, type=Path, help="One PAM .mat file.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for resolution figure and tables.")
    parser.add_argument("--axis", choices=["x", "y"], default="x", help="Long axis used for the resolution profile.")
    parser.add_argument("--fixed-value", type=float, help="Fixed coordinate on the other axis, e.g. y=0 for --axis x.")
    parser.add_argument("--coordinate-tolerance", type=float, default=1e-9, help="Coordinate matching tolerance for --fixed-value.")
    parser.add_argument("--p2p-window", help="Optional START:STOP waveform slice for peak-to-peak values. Default uses full waveform like plot_line.m.")
    parser.add_argument("--matlab-nm-factor", type=float, default=20.0, help="Legacy plot_line.m reporting factor applied to FWHM axis units.")
    parser.add_argument("--fit-samples", type=int, default=2000, help="Number of points sampled from the fitted ERF before differentiating.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = write_long_axis_resolution(
        path=args.input,
        output_dir=args.output_dir,
        axis=args.axis,
        fixed_value=args.fixed_value,
        tolerance=args.coordinate_tolerance,
        p2p_window=parse_optional_slice(args.p2p_window),
        matlab_nm_factor=args.matlab_nm_factor,
        fit_samples=args.fit_samples,
    )
    print(result["outputs"]["figure"])
    print(result["outputs"]["peak_to_peak_3d"])
    print(result["outputs"]["peak_to_peak_3d_html"])
    print(result["outputs"]["profile_csv"])
    print(result["outputs"]["summary_json"])
    print(f"FWHM index width: {result['resolution']['fwhm_index']:.3f}")
    print(f"Axis-coordinate FWHM: {result['resolution']['fwhm_axis_units']:.6g}")
    print(f"Resolution if metadata step is in um: {result['resolution']['fwhm_nm_if_axis_is_um']:.3f} nm")
    print(f"Legacy plot_line display value: {result['resolution']['legacy_plot_line_display_nm']:.3f} nm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
