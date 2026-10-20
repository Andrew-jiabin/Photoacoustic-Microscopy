# -*- coding: utf-8 -*-
"""Draw a spectrum scan: any band, or the whole thing, from one acquisition.

What this is for
----------------
``MATLAB/PAM_Reconstruction_4.m`` turns a scan into one image by taking
``max(waveform) - min(waveform)`` per pixel.  That is the right reduction for an
A-line, where the whole record is one pulse, and the wrong one for a spectrum:
the interesting content of a photoacoustic spectrum is *where* the energy sits,
and ``max - min`` collapses a 4097-bin spectrum into a single number that mixes
every frequency together.

This script keeps the frequency axis and reduces over a band you choose, so the
same scan can be looked at as "the 10 MHz image", "the 100-400 MHz image" and
"everything" without re-acquiring.  The layout, the position keys and the
serpentine ordering are the same as the MATLAB script, so a ``.mat`` written by
``PAM_Main_Nanomax_AFM.py`` opens here too.

Usage
-----
    # what is in the file, and can it resolve 1 MHz?
    python PAM_Spectrum_Reconstruction.py scan.npz --list

    # the whole band
    python PAM_Spectrum_Reconstruction.py scan.npz

    # three bands side by side, written to disk
    python PAM_Spectrum_Reconstruction.py scan.npz \\
        --band 5 15 --band 100 400 --band 400 800 --save out

    # one pixel's spectrum, with the bands drawn on top
    python PAM_Spectrum_Reconstruction.py scan.npz --point 5.0 5.0

    # drag a slider through the spectrum
    python PAM_Spectrum_Reconstruction.py scan.npz --interactive

    # no data yet
    python PAM_Spectrum_Reconstruction.py --synthetic --band 5 15 --band 100 400

``--band`` is in MHz by default; ``--unit GHz`` or ``--unit Hz`` also work.
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np

from spectrum_io import SpectrumDataset


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_dataset(path):
    """Read a ``.npz`` written by ``SpectrumDataset.save``, or a ``.mat``.

    The ``.mat`` route exists because that is what the scan program has always
    written and what MATLAB reads.  It only works if the file carries
    ``metadata.freqs_hz`` -- without a frequency axis a set of columns is not a
    spectrum, and guessing the axis from the column count would silently produce
    a plausible but wrong picture.
    """
    if path.lower().endswith(".npz"):
        return SpectrumDataset.load(path)

    from scipy.io import loadmat

    mat = loadmat(path)
    metadata = mat.get("metadata")
    if metadata is None:
        raise ValueError("%s has no metadata struct" % path)
    metadata = metadata[0, 0]

    if "freqs_hz" not in metadata.dtype.names:
        raise ValueError(
            "%s has no metadata.freqs_hz, so its columns cannot be placed on a "
            "frequency axis; re-run the scan with PAM_AFM_PAYLOAD=spectrum" % path
        )
    freqs = np.asarray(metadata["freqs_hz"], dtype=np.float64).ravel()

    keys = sorted(k for k in mat if k.startswith("P_"))
    if not keys:
        raise ValueError("%s has no P_<x>_<y> variables" % path)

    rows, positions = [], []
    for key in keys:
        values = np.asarray(mat[key], dtype=np.float64).ravel()
        if values.size != freqs.size:
            raise ValueError(
                "%s has %d bins but metadata.freqs_hz has %d"
                % (key, values.size, freqs.size)
            )
        rows.append(values)
        positions.append(_parse_key_position(key))

    meta = {}
    for name in ("scan_shape", "step_size", "sample_rate_hz", "fft_length"):
        if name in metadata.dtype.names:
            value = np.asarray(metadata[name]).ravel()
            meta["step_um" if name == "step_size" else name] = (
                [int(v) for v in value] if name == "scan_shape" else float(value[0])
            )
    meta.setdefault("payload_kind", "spectrum")
    if "records_per_point" in metadata.dtype.names:
        records = int(np.asarray(metadata["records_per_point"]).ravel()[0])
    else:
        records = 1

    return SpectrumDataset(
        np.vstack(rows), freqs, np.asarray(positions, dtype=np.float32),
        records=np.full(len(rows), records, dtype=np.int32), meta=meta,
    )


def _parse_key_position(key):
    """Inverse of ``spectrum_io._mat_key``: ``P_n0p5000_1p0000`` -> (-0.5, 1.0)."""
    body = key[2:] if key.startswith("P_") else key
    parts = body.split("_")
    if len(parts) != 2:
        return (0.0, 0.0)
    return tuple(float(p.replace("p", ".").replace("n", "-")) for p in parts)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def to_hz(value, unit):
    factor = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}[unit.lower()]
    return float(value) * factor


def fmt_hz(hz):
    hz = float(hz)
    if abs(hz) >= 1e9:
        return "%.3f GHz" % (hz / 1e9)
    if abs(hz) >= 1e6:
        return "%.3f MHz" % (hz / 1e6)
    if abs(hz) >= 1e3:
        return "%.1f kHz" % (hz / 1e3)
    return "%.0f Hz" % hz


def band_label(lo_hz, hi_hz):
    if lo_hz is None and hi_hz is None:
        return "all bins"
    lo = "0" if lo_hz is None else fmt_hz(lo_hz)
    hi = fmt_hz(hi_hz) if hi_hz is not None else "top"
    return "%s - %s" % (lo, hi)


def describe(dataset):
    print("points        : %d  (%d x %d)" % (
        dataset.n_points, dataset.scan_shape[0], dataset.scan_shape[1]))
    print("bins          : %d, %.3f kHz apart" % (
        dataset.n_bins, dataset.bin_width_hz / 1e3))
    print("frequency     : %s .. %s" % (
        fmt_hz(dataset.freqs_hz[0]), fmt_hz(dataset.freqs_hz[-1])))
    lobe = dataset.main_lobe_hz
    verdict = "yes" if lobe <= 1e6 else "NO"
    print("main lobe     : %s  -> resolves 1 MHz: %s" % (fmt_hz(lobe), verdict))
    print("records/point : %d" % int(dataset.records.max() if dataset.n_points else 0))
    print("units         : %s" % dataset.meta.get("units", "adc"))
    if dataset.meta.get("fft_backend"):
        print("fft backend   : %s, length %s" % (
            dataset.meta["fft_backend"], dataset.meta.get("fft_length")))
    values = dataset.amplitude
    print("amplitude     : min %.3g, median %.3g, max %.3g"
          % (float(values.min()), float(np.median(values)), float(values.max())))


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #

def _colour_limits(image, vmax_percentile, log):
    finite = image[np.isfinite(image)]
    if finite.size == 0:
        return 0.0, 1.0
    if log:
        positive = finite[finite > 0]
        low = float(np.percentile(positive, 1)) if positive.size else 1e-12
        high = float(np.percentile(positive, vmax_percentile)) if positive.size else 1.0
        return max(low, high * 1e-6), max(high, low * 10.0)
    return float(finite.min()), float(np.percentile(finite, vmax_percentile))


def draw(dataset, bands, reduce, log, vmax_percentile, points, dpi, save_dir,
         show, interactive):
    import matplotlib
    if not show and not interactive:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    unit = dataset.meta.get("units", "adc")
    width, height = dataset.scan_shape
    extent = _extent(dataset)

    n_panels = max(1, len(bands))
    n_spectra = len(points)
    fig = plt.figure(figsize=(4.2 * n_panels, 3.9 * (1 + (1 if n_spectra else 0))),
                     constrained_layout=True)
    grid = fig.add_gridspec(1 + (1 if n_spectra else 0), n_panels)

    if not bands:
        bands = [(None, None)]

    for panel, (lo_hz, hi_hz) in enumerate(bands):
        axis = fig.add_subplot(grid[0, panel])
        image = dataset.image(lo_hz, hi_hz, reduce=reduce)
        low, high = _colour_limits(image, vmax_percentile, log)
        shown = np.log10(np.clip(image, low, None)) if log else image
        if log:
            low, high = np.log10(low), np.log10(high)
        artist = axis.imshow(shown, origin="lower", extent=extent, aspect="equal",
                             cmap="hot", vmin=low, vmax=high, interpolation="nearest")
        axis.set_title(band_label(lo_hz, hi_hz), fontsize=10)
        axis.set_xlabel("x (um)")
        if panel == 0:
            axis.set_ylabel("y (um)")
        bar = fig.colorbar(artist, ax=axis, shrink=0.85)
        bar.set_label(("log10 " if log else "") + unit, fontsize=8)

        for x_um, y_um in points:
            axis.plot([x_um], [y_um], marker="+", color="#00ffff",
                      markersize=10, markeredgewidth=1.4)

    if n_spectra:
        axis = fig.add_subplot(grid[1, :])
        for x_um, y_um in points:
            index = dataset.nearest_point(x_um, y_um)
            freqs, amplitude = dataset.spectrum(index)
            axis.plot(freqs / 1e6, amplitude, lw=0.9,
                      label="(%.3f, %.3f) um, point %d"
                            % (dataset.pos_um[index, 0], dataset.pos_um[index, 1], index))
        for lo_hz, hi_hz in bands:
            if lo_hz is not None:
                axis.axvline(lo_hz / 1e6, color="0.6", ls=":", lw=0.8)
            if hi_hz is not None:
                axis.axvline(hi_hz / 1e6, color="0.6", ls=":", lw=0.8)
        if log:
            axis.set_yscale("log")
        axis.set_xlabel("frequency (MHz)")
        axis.set_ylabel("amplitude (%s)" % unit)
        axis.set_title("spectra at the marked points", fontsize=10)
        axis.legend(fontsize=8, loc="upper right")
        axis.grid(alpha=0.25)

    fig.suptitle("%d x %d points, %d bins of %s, lobe %s"
                 % (width, height, dataset.n_bins,
                    fmt_hz(dataset.bin_width_hz), fmt_hz(dataset.main_lobe_hz)),
                 fontsize=9)

    written = []
    if save_dir:
        os.makedirs(save_dir, exist_ok=True)
        target = os.path.join(save_dir, "spectrum_bands.png")
        fig.savefig(target, dpi=dpi)
        written.append(target)
        print("wrote %s" % target)

    if interactive:
        _interactive(fig, dataset, reduce, log, vmax_percentile, extent, unit, dpi, save_dir)

    if show or not save_dir:
        plt.show()
    plt.close(fig)
    return written


def _extent(dataset):
    """``imshow`` extent in um, from the positions actually visited."""
    step = float(dataset.meta.get("step_um") or 0.0)
    if step <= 0:
        xs = np.unique(np.round(dataset.pos_um[:, 0], 4))
        step = float(xs[1] - xs[0]) if xs.size > 1 else 1.0
    x0 = float(dataset.pos_um[:, 0].min()) - step / 2.0
    y0 = float(dataset.pos_um[:, 1].min()) - step / 2.0
    width, height = dataset.scan_shape
    return (x0, x0 + width * step, y0, y0 + height * step)


def _interactive(fig, dataset, reduce, log, vmax_percentile, extent, unit, dpi, save_dir):
    """A frequency slider: one image, swept through the spectrum.

    This is the "selectively show a band" view.  The slider sets the centre of a
    window whose width is a second slider, because the question is usually "where
    does the contrast appear as I move up in frequency", and that is easier to
    answer by dragging than by typing bands.
    """
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Slider, Button

    span_hz = float(dataset.freqs_hz[-1] - dataset.freqs_hz[0])
    if span_hz <= 0:
        return

    # Shrink the image axes to make room for the controls.
    fig.set_layout_engine("none")
    base = fig.axes[0].get_position()
    image_axis = fig.axes[0]
    image_axis.set_position([base.x0, base.y0 + 0.12, base.width, base.height - 0.12])

    centre_axis = fig.add_axes([0.12, 0.075, 0.68, 0.025])
    width_axis = fig.add_axes([0.12, 0.035, 0.68, 0.025])
    save_axis = fig.add_axes([0.84, 0.035, 0.10, 0.03])

    centre = Slider(centre_axis, "centre (MHz)", 0.0, span_hz / 1e6,
                    valinit=span_hz / 2e6)
    width_s = Slider(width_axis, "window (MHz)", 0.5, max(1.0, span_hz / 1e6),
                     valinit=min(20.0, span_hz / 1e6))
    button = Button(save_axis, "save")

    image = dataset.image(None, None, reduce=reduce)
    low, high = _colour_limits(image, vmax_percentile, log)
    shown = np.log10(np.clip(image, low, None)) if log else image
    artist = image_axis.imshow(
        shown, origin="lower", extent=extent, aspect="equal", cmap="hot",
        vmin=np.log10(low) if log else low,
        vmax=np.log10(high) if log else high, interpolation="nearest")
    image_axis.set_title(band_label(None, None), fontsize=10)

    def update(_value=None):
        lo = max(0.0, (centre.val - width_s.val / 2.0) * 1e6)
        hi = (centre.val + width_s.val / 2.0) * 1e6
        block = dataset.image(lo, hi, reduce=reduce)
        low_now, high_now = _colour_limits(block, vmax_percentile, log)
        artist.set_data(np.log10(np.clip(block, low_now, None)) if log else block)
        artist.set_clim(np.log10(low_now) if log else low_now,
                        np.log10(high_now) if log else high_now)
        image_axis.set_title(band_label(lo, hi), fontsize=10)
        fig.canvas.draw_idle()

    def save(_event):
        if not save_dir:
            print("no --save DIR given, nothing written")
            return
        os.makedirs(save_dir, exist_ok=True)
        lo = max(0.0, (centre.val - width_s.val / 2.0) * 1e6)
        hi = (centre.val + width_s.val / 2.0) * 1e6
        target = os.path.join(save_dir, "band_%07.2f_%07.2fMHz.png"
                              % (lo / 1e6, hi / 1e6))
        fig.savefig(target, dpi=dpi)
        print("wrote %s" % target)

    centre.on_changed(update)
    width_s.on_changed(update)
    button.on_clicked(save)
    update()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Draw a spectrum scan: any band, or the whole thing.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="--band takes two numbers in --unit units (MHz by default).",
    )
    parser.add_argument("input", nargs="?", help="scan .npz (or .mat); omit with --synthetic")
    parser.add_argument("--band", nargs=2, action="append", type=float,
                        metavar=("LO", "HI"),
                        help="frequency band to image, repeatable")
    parser.add_argument("--unit", default="MHz", choices=("Hz", "kHz", "MHz", "GHz"),
                        help="unit for --band (default MHz)")
    parser.add_argument("--reduce", default="mean", choices=("mean", "sum", "max"),
                        help="how to collapse the band to one number per pixel")
    parser.add_argument("--point", nargs=2, action="append", type=float,
                        metavar=("X", "Y"), help="draw the spectrum at this point, repeatable")
    parser.add_argument("--point-index", type=int, action="append",
                        help="draw the spectrum at this point index, repeatable")
    parser.add_argument("--log", action="store_true", help="log colour scale")
    parser.add_argument("--vmax", type=float, default=99.0,
                        help="clip the colour scale at this percentile (default 99)")
    parser.add_argument("--save", metavar="DIR", help="write PNGs into DIR instead of showing")
    parser.add_argument("--show", action="store_true", help="open a window even with --save")
    parser.add_argument("--interactive", action="store_true",
                        help="add a frequency slider")
    parser.add_argument("--dpi", type=int, default=140)
    parser.add_argument("--list", action="store_true",
                        help="print the geometry and exit")
    parser.add_argument("--synthetic", action="store_true",
                        help="use the built-in stand-in dataset, no file needed")
    args = parser.parse_args(argv)

    if args.synthetic:
        from spectrum_io import synthetic as make_synthetic
        dataset = make_synthetic()
        print("using the synthetic dataset (three blobs at 10, 120 and 400 MHz)")
    elif args.input:
        dataset = load_dataset(args.input)
    else:
        parser.error("give an input file, or --synthetic")
        return 2

    describe(dataset)
    print("")

    points = [tuple(p) for p in (args.point or [])]
    for index in (args.point_index or []):
        if not 0 <= index < dataset.n_points:
            parser.error("--point-index %d out of range 0..%d"
                         % (index, dataset.n_points - 1))
        points.append((float(dataset.pos_um[index, 0]), float(dataset.pos_um[index, 1])))

    if args.list:
        if points:
            for x_um, y_um in points:
                index = dataset.nearest_point(x_um, y_um)
                print("point %d at (%.4f, %.4f) um"
                      % (index, dataset.pos_um[index, 0], dataset.pos_um[index, 1]))
        return 0

    bands = [(to_hz(lo, args.unit), to_hz(hi, args.unit)) for lo, hi in (args.band or [])]

    draw(dataset, bands, args.reduce, args.log, args.vmax, points,
         args.dpi, args.save, args.show, args.interactive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
