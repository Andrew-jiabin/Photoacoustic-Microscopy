# -*- coding: utf-8 -*-
"""Spectrum payload: one amplitude spectrum per pixel.

Why a separate type
-------------------
The time-domain payload is a ``(1, 4096)`` uint16 A-line -- fixed length, fixed
dtype, summed over records.  A spectrum is none of those things: the bin count
follows the FFT length, the values are amplitudes, and what makes the data
meaningful is the *frequency axis*, which the time-domain contract has no place
to store.  Cramming a spectrum into the same array would lose that axis and
force every reader to guess the sample rate back out of the metadata.

Layout on disk (``.npz``)
-------------------------
=========================  ================  ==============================
key                        shape             meaning
=========================  ================  ==============================
``amplitude``              (n, bins) float32 amplitude per bin, board units
``freqs_hz``               (bins,)   float32 bin centres
``pos_um``                 (n, 2)    float32 x, y of each point
``records``                (n,)      int32   spectra averaged into that point
``meta``                   scalar str        JSON: scan shape, step, rate
=========================  ================  ==============================

A ``.mat`` sidecar is written next to it with one variable per point named the
way ``MATLAB/PAM_Reconstruction_4.m`` expects (``P_<x>_<y>``), so the existing
MATLAB tooling can still open a spectrum scan -- it will just be reading a
spectrum instead of a waveform.

Reduction
---------
A pixel value is the amplitude averaged (or summed, or maxed) over a chosen
band.  ``SpectrumDataset.image()`` does that for an arbitrary ``[lo, hi]``, which
is what makes it possible to look at "the 10 MHz band" and "everything from 100
to 400 MHz" from the same acquisition without re-scanning.
"""

from __future__ import annotations

import json
import os

import numpy as np


class SpectrumDataset(object):
    """A grid of amplitude spectra plus the geometry needed to draw it."""

    def __init__(self, amplitude, freqs_hz, pos_um, records=None, meta=None):
        self.amplitude = np.asarray(amplitude, dtype=np.float32)
        if self.amplitude.ndim != 2:
            raise ValueError("amplitude must be (n_points, n_bins)")
        self.freqs_hz = np.asarray(freqs_hz, dtype=np.float32).ravel()
        if self.freqs_hz.size != self.amplitude.shape[1]:
            raise ValueError(
                "freqs_hz has %d bins but amplitude has %d"
                % (self.freqs_hz.size, self.amplitude.shape[1])
            )
        self.pos_um = np.asarray(pos_um, dtype=np.float32).reshape(-1, 2)
        if self.pos_um.shape[0] != self.amplitude.shape[0]:
            raise ValueError("pos_um and amplitude disagree on point count")
        self.records = (
            np.asarray(records, dtype=np.int32).ravel()
            if records is not None
            else np.ones(self.amplitude.shape[0], dtype=np.int32)
        )
        self.meta = dict(meta or {})

    # -- basic geometry ----------------------------------------------------- #

    @property
    def n_points(self):
        return self.amplitude.shape[0]

    @property
    def n_bins(self):
        return self.amplitude.shape[1]

    @property
    def bin_width_hz(self):
        if self.n_bins < 2:
            return float("nan")
        return float(self.freqs_hz[1] - self.freqs_hz[0])

    @property
    def scan_shape(self):
        """``(width, height)`` in pixels, from the metadata or inferred."""
        shape = self.meta.get("scan_shape")
        if shape and len(shape) == 2:
            return int(shape[0]), int(shape[1])
        return self._infer_shape()

    @property
    def main_lobe_hz(self):
        """Width of the window's main lobe -- the real separability limit.

        Two bins for the Hann window both producers use, so this is twice
        :attr:`bin_width_hz`.  It is the number that answers "can this scan
        separate 1 MHz?", and it is deliberately not stored on disk: it follows
        from the sample rate and the FFT length, and storing it would let a file
        claim a resolution its own axis contradicts.
        """
        return 2.0 * self.bin_width_hz

    def to_volts(self):
        """A copy whose amplitudes are volts instead of ADC units.

        The host FFT path records ``input_range_volts`` in the metadata, where
        full scale (32768 ADC units) is that many volts, so the conversion is
        exact.  For a scan written by the on-FPGA path the scale of the DSP
        module's output has not been measured against the input range, so this
        raises rather than inventing a number.
        """
        volts = self.meta.get("input_range_volts")
        if not volts:
            raise ValueError(
                "this dataset has no input_range_volts, so its ADC units cannot "
                "be converted; only the host FFT path records the scale"
            )
        scaled = SpectrumDataset(
            self.amplitude.astype(np.float32) * (float(volts) / 32768.0),
            self.freqs_hz, self.pos_um, self.records,
            dict(self.meta, units="V"),
        )
        return scaled

    def _infer_shape(self):
        """Guess the grid from the coordinates when the metadata is silent."""
        if self.n_points == 0:
            return 0, 0
        xs = np.unique(np.round(self.pos_um[:, 0], 4))
        ys = np.unique(np.round(self.pos_um[:, 1], 4))
        return len(xs), len(ys)

    # -- band selection ----------------------------------------------------- #

    def band_mask(self, lo_hz=None, hi_hz=None, min_bins=1):
        """Boolean mask of bins inside ``[lo_hz, hi_hz]``.

        Defaults cover the whole spectrum.  ``min_bins`` guarantees at least one
        bin is selected, so a band narrower than one bin still returns something
        instead of an empty image.
        """
        lo = -np.inf if lo_hz is None else float(lo_hz)
        hi = np.inf if hi_hz is None else float(hi_hz)
        if hi < lo:
            lo, hi = hi, lo
        mask = (self.freqs_hz >= lo) & (self.freqs_hz <= hi)
        if not mask.any() and min_bins > 0:
            nearest = int(np.argmin(np.abs(self.freqs_hz - 0.5 * (lo + hi))))
            mask = np.zeros(self.n_bins, dtype=bool)
            first = max(0, nearest - (int(min_bins) - 1) // 2)
            mask[first:first + int(min_bins)] = True
        return mask

    def band_amplitude(self, lo_hz=None, hi_hz=None, reduce="mean"):
        """One number per point: how strong the band is there."""
        mask = self.band_mask(lo_hz, hi_hz)
        block = self.amplitude[:, mask]
        if reduce == "mean":
            return block.mean(axis=1)
        if reduce == "sum":
            return block.sum(axis=1)
        if reduce == "max":
            return block.max(axis=1)
        raise ValueError("reduce must be 'mean', 'sum' or 'max', got %r" % (reduce,))

    def spectrum(self, index):
        """``(freqs, amplitude)`` for one point, for the "show me this spectrum" view."""
        index = int(index)
        if not 0 <= index < self.n_points:
            raise IndexError("point %d out of range 0..%d" % (index, self.n_points - 1))
        return self.freqs_hz, self.amplitude[index]

    def nearest_point(self, x_um, y_um):
        """Index of the point closest to ``(x_um, y_um)``."""
        if self.n_points == 0:
            raise ValueError("empty dataset")
        d2 = ((self.pos_um[:, 0] - float(x_um)) ** 2
              + (self.pos_um[:, 1] - float(y_um)) ** 2)
        return int(np.argmin(d2))

    # -- imaging ------------------------------------------------------------ #

    def image(self, lo_hz=None, hi_hz=None, reduce="mean", fill=np.nan):
        """2D array ``(height, width)`` of the band amplitude.

        Unvisited pixels stay ``fill`` (NaN by default) so a missing point is
        visible rather than silently reading as zero.
        """
        width, height = self.scan_shape
        if width <= 0 or height <= 0:
            raise ValueError("cannot determine scan shape")
        values = self.band_amplitude(lo_hz, hi_hz, reduce=reduce)
        out = np.full((height, width), fill, dtype=np.float32)

        step = float(self.meta.get("step_um") or 0.0)
        if step <= 0:
            # Fall back to the spacing actually present in the data.
            xs = np.unique(np.round(self.pos_um[:, 0], 4))
            step = float(xs[1] - xs[0]) if xs.size > 1 else 1.0

        x0 = float(self.pos_um[:, 0].min())
        y0 = float(self.pos_um[:, 1].min())
        for i in range(self.n_points):
            col = int(round((self.pos_um[i, 0] - x0) / step))
            row = int(round((self.pos_um[i, 1] - y0) / step))
            if 0 <= row < height and 0 <= col < width:
                out[row, col] = values[i]
        return out

    # -- persistence -------------------------------------------------------- #

    def save(self, path, mat_sidecar=True):
        """Write ``.npz`` (+ optional ``.mat``) and return the paths used."""
        path = os.path.abspath(path)
        root, ext = os.path.splitext(path)
        if ext.lower() != ".npz":
            path = root + ".npz"
            root = os.path.splitext(path)[0]

        np.savez_compressed(
            path,
            amplitude=self.amplitude,
            freqs_hz=self.freqs_hz,
            pos_um=self.pos_um,
            records=self.records,
            meta=json.dumps(self.meta),
        )
        written = [path]

        if mat_sidecar:
            try:
                from scipy.io import savemat
            except ImportError:
                savemat = None
            if savemat is not None:
                mat_path = root + ".mat"
                payload = {
                    "metadata": {
                        "scan_shape": np.asarray(self.scan_shape, dtype=np.float64),
                        "pos_list": np.asarray(
                            ["%.4f,%.4f" % (p[0], p[1]) for p in self.pos_um],
                            dtype=object,
                        ),
                        "step_size": float(self.meta.get("step_um") or 0.0),
                        "payload_kind": "spectrum",
                        "freqs_hz": self.freqs_hz.astype(np.float64),
                        "records_per_point": int(self.records.max()) if self.n_points else 0,
                        "sample_rate_hz": float(self.meta.get("sample_rate_hz") or 0.0),
                        "fft_length": int(self.meta.get("fft_length") or 0),
                    },
                }
                for i in range(self.n_points):
                    payload[_mat_key(self.pos_um[i])] = self.amplitude[i].reshape(1, -1)
                savemat(mat_path, payload, do_compression=True)
                written.append(mat_path)
        return written

    @classmethod
    def load(cls, path):
        """Read back a ``.npz`` written by :meth:`save`."""
        with np.load(path, allow_pickle=False) as data:
            meta = json.loads(str(data["meta"])) if "meta" in data else {}
            return cls(
                amplitude=data["amplitude"],
                freqs_hz=data["freqs_hz"],
                pos_um=data["pos_um"],
                records=data["records"] if "records" in data else None,
                meta=meta,
            )


def _mat_key(pos):
    """Same naming as ``sanitize_key_matlab`` in PAM_Reconstruction_4.m."""
    clean = ("%.4f,%.4f" % (float(pos[0]), float(pos[1]))).replace(" ", "")
    clean = clean.replace(".", "p").replace("-", "n").replace(",", "_")
    return "P_" + clean


def from_scan_points(points, freqs_hz, meta=None):
    """Build a dataset from ``[(pos_um, amplitude, records), ...]``.

    ``points`` is what the scan loop accumulates: a list of
    ``((x, y), amplitude_array, records_averaged)``.
    """
    if not points:
        raise ValueError("no points")
    amplitude = np.vstack([np.asarray(p[1], dtype=np.float32).ravel() for p in points])
    pos = np.asarray([p[0] for p in points], dtype=np.float32)
    records = np.asarray([p[2] if len(p) > 2 else 1 for p in points], dtype=np.int32)
    return SpectrumDataset(amplitude, freqs_hz, pos, records, meta)


def frequency_axis(n_bins, fft_length, sample_rate_hz):
    """Bin centres for a spectrum of ``n_bins`` produced from ``fft_length``.

    ``n * fs / fft_length`` rather than ``np.fft.rfftfreq``, because the two
    producers of these spectra disagree on the last bin: the ATS9373's DSP module
    returns ``fft_length / 2`` bins (no Nyquist bin) and the host FFT returns
    ``fft_length / 2 + 1``.  The linear rule is right for both, and matches
    ``AlazarFFTSpectrum.frequency_axis``.
    """
    return (np.arange(int(n_bins), dtype=np.float64)
            * float(sample_rate_hz) / float(fft_length))


def synthetic(nx=11, ny=11, n_bins=2048, sample_rate_hz=4e9, fft_length=4096):
    """A stand-in dataset, so the reconstruction can be exercised without a scan.

    Three blobs with different spectral content, so a band selection actually
    changes the picture -- which is the whole point of storing spectra.
    """
    freqs = frequency_axis(n_bins, fft_length, sample_rate_hz).astype(np.float32)
    pos, amps = [], []
    for iy in range(ny):
        for ix in range(nx):
            x = float(ix)
            y = float(iy)
            base = 40.0 + 5.0 * np.random.randn(n_bins)
            for cx, cy, f_center, width, height in (
                (2.0, 2.0, 10e6, 3e6, 900.0),
                (5.0, 5.0, 120e6, 30e6, 600.0),
                (8.0, 8.0, 400e6, 60e6, 750.0),
            ):
                r2 = (x - cx) ** 2 + (y - cy) ** 2
                weight = np.exp(-r2 / 6.0)
                if weight < 1e-3:
                    continue
                base = base + height * weight * np.exp(
                    -0.5 * ((freqs - f_center) / width) ** 2
                )
            pos.append((x, y))
            amps.append(np.maximum(base, 0.0).astype(np.float32))
    return SpectrumDataset(
        np.vstack(amps), freqs, np.asarray(pos, dtype=np.float32),
        records=np.full(len(pos), 64, dtype=np.int32),
        meta={"scan_shape": [nx, ny], "step_um": 1.0,
              "sample_rate_hz": sample_rate_hz, "fft_length": fft_length,
              "payload_kind": "spectrum"},
    )
