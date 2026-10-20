# -*- coding: utf-8 -*-
"""Host-side FFT at 4 GS/s, long enough to resolve 1 MHz.

Why not just use the on-FPGA FFT
--------------------------------
``AlazarFFTSpectrum`` uses the ATS9373's DSP module, and that module reports
``maxLength = 4096``.  At 4 GS/s a 4096-point record is 1.024 us, so:

    bin      = 4000 MS/s / 4096 = 976.6 kHz
    Hann main lobe = 2 bins     = 1953.1 kHz

Two peaks 1 MHz apart fall inside one main lobe and merge into a single hump,
so the on-FPGA path cannot answer "is there a feature at 500 MHz *and* another
at 501 MHz".  Nothing about the FFT's arithmetic is wrong -- it is the record
length that sets the resolution, and the record length is capped in hardware.

Doubling the record to 8192 points halves both numbers:

    bin      = 488.3 kHz
    main lobe = 976.6 kHz   < 1 MHz

which is exactly the resolution the measurement needs.  16384 points buys more
margin (lobe 488 kHz) for the same capture time; the FFT is not the bottleneck.

What it costs
-------------
The on-FPGA path ships *bins*: 2048 of them, 4 KB per record.  The host path
ships *samples*: 8192 of them, 16 KB per record -- 4x the DMA traffic and a
numpy rfft afterwards.  Measured on the board (64 records, 4 GS/s, trigger off):

    samples      bin       lobe    capture      fft     bytes      bins
       4096  976.6 kHz  1953.1 kHz   48.1 ms   9.5 ms   2048 KB     2049
       8192  488.3 kHz   976.6 kHz   46.6 ms   4.1 ms   4096 KB     4097
      16384  244.1 kHz   488.3 kHz   48.3 ms  11.0 ms   8192 KB     8193

Capture time is flat because the record *period* is set by the trigger timeout
(952 us at 1050 Hz), not by the record length: 64 records take 61 ms either way.
So the 8192-point path is not measurably slower than the 4096-point one.  It is
the FFT that grows, and 4.1 ms for 4097 bins is nothing next to a 61 ms capture.

Amplitude convention
--------------------
The on-FPGA FFT reports ``FFT_OUTPUT_FORMAT_FLOAT_AMP2`` -- a power, in board
units.  Here the returned value is an **amplitude in raw sample units**, i.e.
the same scale as the uint16 samples the board hands over (12-bit codes shifted
left by 4), normalised so a pure tone of amplitude ``A`` reads ``A`` at its bin
centre:

    amplitude = 2 * |rfft(x * w)| / (N * mean(w))

``mean(w)`` is the window's coherent gain (0.5 for Hann), so for a bin-centred
tone the factor of two and the gain cancel and the number reads as a sine
amplitude.  Two useful consequences: it is directly comparable with the sample
values the demodulator works on, and a full-scale sine (32768 raw units) still
fits in uint16, so a spectrum can be stored in the same dtype as an A-line.
:func:`amplitude_to_volts` converts it; note that ``counts_to_volts`` is *not*
the right conversion, because that one subtracts the mid-scale offset that a
sample has and an amplitude does not.

The DC term is removed per record before windowing: the board's offset is large
compared with a photoacoustic signal and a Hann window leaks it into the first
few bins.

Averaging is done on power, then square-rooted, because consecutive records are
not phase-coherent -- averaging amplitudes of noisy bins biases the result high,
averaging powers does not.
"""

from __future__ import annotations

import argparse
import sys
import time

import numpy as np

from Alazar_imaging.AlazarBurstCapture import (
    DEFAULT_INPUT_RANGE_VOLTS,
    AlazarBurstCapture,
    counts_to_volts,
)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# 4 GS/s is the whole point: the band of interest reaches 1 GHz, so the record
# must span 0..2 GHz, and 4 GS/s is the fastest the board offers (single channel).
DEFAULT_SAMPLE_RATE_HZ = 4e9

# 8192 points at 4 GS/s: 2.048 us window, 488.3 kHz bins, 976.6 kHz main lobe.
# The smallest length that separates peaks 1 MHz apart.
DEFAULT_RECORD_LENGTH = 8192

DEFAULT_WINDOW = "hann"
DEFAULT_INPUT_RANGE = DEFAULT_INPUT_RANGE_VOLTS

# Same trigger policy as the burst capture path: follow the laser when it is
# there, self-trigger on the timeout when it is not.  See AlazarBurstCapture.
DEFAULT_TRIGGER = "external"
DEFAULT_TRIGGER_TIMEOUT_TICKS = 150
DEFAULT_TRIGGER_LEVEL = 153

WINDOWS = ("hann", "hamming", "blackman", "rect", "boxcar", "none")


# --------------------------------------------------------------------------- #
# Pure helpers (no hardware)
# --------------------------------------------------------------------------- #

def window_values(name, length):
    """Window array for ``name``, normalised so ``mean()`` is its coherent gain."""
    key = str(name).strip().lower()
    length = int(length)
    if key in ("rect", "boxcar", "none"):
        return np.ones(length, dtype=np.float64)
    if key == "hann":
        return np.hanning(length)
    if key == "hamming":
        return np.hamming(length)
    if key == "blackman":
        return np.blackman(length)
    raise ValueError("window must be one of %s, got %r" % (", ".join(WINDOWS), name))


def frequency_axis(record_length, sample_rate_hz):
    """Bin centres in Hz for the half spectrum, 0 .. fs/2 inclusive."""
    return np.fft.rfftfreq(int(record_length), d=1.0 / float(sample_rate_hz))


def resolution_hz(record_length, sample_rate_hz):
    """Bin spacing, i.e. how far apart two *bins* are."""
    return float(sample_rate_hz) / float(record_length)


# Half-power width of the main lobe, in bins.  The full lobe reaches its first
# zero at twice this, but two tones separated by the half-power width are the
# closest pair that still shows up as two humps, which is the number that
# matters when someone asks "can this resolve 1 MHz?".
LOBE_BINS = {
    "hann": 2.0,
    "hamming": 2.0,
    "blackman": 3.0,
    "rect": 1.0,
    "boxcar": 1.0,
    "none": 1.0,
}


def main_lobe_hz(record_length, sample_rate_hz, window=DEFAULT_WINDOW):
    """Width of the window's main lobe -- the real separability limit.

    A Hann window's main lobe is four bins wide to its first zero but two bins
    wide at half power, and two tones closer than that merge into one hump.
    Reporting the half-power width is the honest number and the one this module
    is sized against: at 4 GS/s it is 1953.1 kHz for 4096 points (too wide for
    1 MHz) and 976.6 kHz for 8192 points (just enough).
    """
    factor = LOBE_BINS.get(str(window).strip().lower(), 2.0)
    return factor * resolution_hz(record_length, sample_rate_hz)


def amplitude_spectrum(samples, window=None, sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ):
    """Windowed amplitude spectrum of ``(records, n)`` raw uint16 samples.

    Returns ``(amplitude, freqs)`` with ``amplitude`` of shape
    ``(records, n/2 + 1)`` in raw sample units, normalised so a bin-centred tone
    of amplitude ``A`` raw units reads ``A``.
    """
    block = np.asarray(samples)
    if block.ndim == 1:
        block = block[None, :]
    if block.size == 0:
        raise ValueError("no samples")
    n = block.shape[1]

    values = block.astype(np.float64)
    values -= values.mean(axis=1, keepdims=True)

    if window is None:
        window = window_values(DEFAULT_WINDOW, n)
    else:
        window = np.asarray(window, dtype=np.float64).ravel()
        if window.size != n:
            raise ValueError("window has %d taps but the record has %d" % (window.size, n))

    gain = float(window.mean())
    if gain <= 0:
        raise ValueError("window has non-positive coherent gain")

    transformed = np.fft.rfft(values * window[None, :], axis=1)
    amplitude = 2.0 * np.abs(transformed) / (n * gain)
    # Bin 0 and, for even n, the Nyquist bin have no mirror image to fold, so the
    # factor of two above overstates them by 2.  Halve them back.
    amplitude[:, 0] *= 0.5
    if n % 2 == 0:
        amplitude[:, -1] *= 0.5
    return amplitude, frequency_axis(n, sample_rate_hz)


def amplitude_to_volts(amplitude, input_range_volts=DEFAULT_INPUT_RANGE):
    """Convert a spectrum amplitude in raw sample units to volts.

    ``counts_to_volts`` cannot be reused here: it converts a *sample*, so it
    subtracts the mid-scale offset.  An amplitude has no offset -- full scale is
    ``input_range`` volts, i.e. raw 32768 (2048 codes shifted left by 4).
    """
    return np.asarray(amplitude, dtype=np.float64) * (
        float(input_range_volts) / 32768.0
    )


def average_amplitude(amplitude, average=True):
    """Collapse ``(records, bins)`` to ``(bins,)``.

    Averaging happens on power (amplitude squared) and is square-rooted at the
    end, which is the unbiased way to combine records that are not phase
    coherent.  ``average=False`` returns the last record, for a live display.
    """
    block = np.asarray(amplitude, dtype=np.float64)
    if block.ndim == 1:
        return block
    if not average:
        return block[-1]
    power = np.square(block).mean(axis=0)
    return np.sqrt(power)


# --------------------------------------------------------------------------- #
# Board driver
# --------------------------------------------------------------------------- #

class AlazarHostSpectrum:
    """Capture time-domain records at 4 GS/s and FFT them on the host.

    Deliberately thin: the trigger policy, the DMA buffers and the timeout
    fallback all live in :class:`AlazarBurstCapture`, which is already exercised
    on the board.  This class only adds the window, the transform and the
    frequency axis, so there is one place where "how a record is acquired" is
    decided and one place where "how a record becomes a spectrum" is decided.
    """

    def __init__(
        self,
        system_id=1,
        board_id=1,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        record_length=DEFAULT_RECORD_LENGTH,
        input_range_volts=DEFAULT_INPUT_RANGE,
        window=DEFAULT_WINDOW,
        records_per_buffer=8,
        buffer_count=4,
        timeout_ms=20000,
        trigger=DEFAULT_TRIGGER,
        trigger_timeout_ticks=DEFAULT_TRIGGER_TIMEOUT_TICKS,
        trigger_level=DEFAULT_TRIGGER_LEVEL,
        verbose=True,
        board=None,
        capture=None,
    ):
        self.sample_rate_hz = float(sample_rate_hz)
        self.record_length = int(record_length)
        if self.record_length < 2:
            raise ValueError("record_length must be at least 2")
        self.input_range_volts = float(input_range_volts)
        self.window_name = str(window)
        self.verbose = verbose

        # Built once: rebuilding a 8192-tap Hann window per point is small but
        # pointless, and keeping it makes the normalisation auditable.
        self.window = window_values(self.window_name, self.record_length)

        if capture is None:
            capture = AlazarBurstCapture(
                system_id=system_id,
                board_id=board_id,
                sample_rate_hz=self.sample_rate_hz,
                samples_per_record=self.record_length,
                input_range_volts=self.input_range_volts,
                records_per_buffer=records_per_buffer,
                buffer_count=buffer_count,
                timeout_ms=timeout_ms,
                trigger=trigger,
                trigger_timeout_ticks=trigger_timeout_ticks,
                trigger_level=trigger_level,
                verbose=False,
                board=board,
            )
        self.capture = capture

    # -- geometry ----------------------------------------------------------- #

    @property
    def board(self):
        return self.capture.board

    @property
    def n_bins(self):
        return self.record_length // 2 + 1

    @property
    def bin_width_hz(self):
        return resolution_hz(self.record_length, self.sample_rate_hz)

    @property
    def lobe_hz(self):
        return main_lobe_hz(self.record_length, self.sample_rate_hz, self.window_name)

    @property
    def max_frequency_hz(self):
        return self.sample_rate_hz / 2.0

    def freqs(self):
        return frequency_axis(self.record_length, self.sample_rate_hz)

    def describe(self):
        return (
            "host FFT, %.0f MS/s x %d points -> %d bins of %.1f kHz "
            "(main lobe %.1f kHz, 0..%.0f MHz)"
            % (self.sample_rate_hz / 1e6, self.record_length, self.n_bins,
               self.bin_width_hz / 1e3, self.lobe_hz / 1e3,
               self.max_frequency_hz / 1e6)
        )

    def _log(self, message):
        if self.verbose:
            print("[HOSTFFT] " + str(message))

    # -- lifecycle ---------------------------------------------------------- #

    def open(self):
        self.capture.open()
        self._log(self.describe())
        return self

    def configure(self):
        self.capture.configure()
        return self

    def close(self):
        self.capture.close()

    def __enter__(self):
        self.open()
        self.configure()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # -- acquisition -------------------------------------------------------- #

    def read_spectrum(self, records=1, average=True):
        """One windowed amplitude spectrum, ``(amplitude, freqs)``.

        ``amplitude`` is ``(n_bins,)`` in raw sample units, averaged over
        ``records`` acquisitions in the power domain.
        """
        records = max(1, int(records))
        raw = self.capture.capture(records)
        amplitude, freqs = amplitude_spectrum(
            raw, window=self.window, sample_rate_hz=self.sample_rate_hz
        )
        return average_amplitude(amplitude, average=average), freqs

    def read_spectrum_volts(self, records=1, average=True):
        """Same as :meth:`read_spectrum` but scaled to volts of sine amplitude."""
        amplitude, freqs = self.read_spectrum(records=records, average=average)
        return amplitude_to_volts(amplitude, self.input_range_volts), freqs

    def band_amplitude(self, lo_hz, hi_hz, records=1, reduce="max"):
        """One number for a band, e.g. the 10 MHz monitor used for the approach.

        ``max`` by default: the point of a narrow-band reading during an
        approach is "is there anything at this frequency", and the tone sits
        between bins when the bin width is 488 kHz and the carrier is 10 MHz
        (bin 20.48).  Taking the maximum of the few bins the band touches is the
        reading least sensitive to that.
        """
        amplitude, freqs = self.read_spectrum(records=records)
        mask = (freqs >= float(lo_hz)) & (freqs <= float(hi_hz))
        if not mask.any():
            nearest = int(np.argmin(np.abs(freqs - 0.5 * (float(lo_hz) + float(hi_hz)))))
            mask = np.zeros(freqs.size, dtype=bool)
            mask[max(0, nearest - 1):nearest + 2] = True
        block = amplitude[mask]
        if reduce == "max":
            return float(block.max())
        if reduce == "mean":
            return float(block.mean())
        if reduce == "sum":
            return float(block.sum())
        raise ValueError("reduce must be 'max', 'mean' or 'sum'")


# --------------------------------------------------------------------------- #
# Self-test: no hardware, checks the normalisation and the resolution claim
# --------------------------------------------------------------------------- #

def _dip_depth(row, freqs, lo_hz, hi_hz):
    """How deep the valley between the two strongest peaks is, as a fraction.

    0 means the pair merged into one hump, 1 means the valley reaches the
    baseline.  This is the measurement that answers "can it resolve 1 MHz":
    two tones separated by less than the main-lobe width leave a shoulder
    rather than a valley, and no amount of peak counting changes that.
    """
    band = (freqs >= float(lo_hz)) & (freqs <= float(hi_hz))
    values = np.asarray(row, dtype=np.float64)[band]
    if values.size < 5:
        return 0.0
    peaks = [i for i in range(1, values.size - 1)
             if values[i] > values[i - 1] and values[i] >= values[i + 1]]
    if len(peaks) < 2:
        return 0.0
    peaks.sort(key=lambda i: values[i], reverse=True)
    a, b = sorted(peaks[:2])
    valley = float(values[a:b + 1].min())
    smaller = float(min(values[a], values[b]))
    if smaller <= 0:
        return 0.0
    return 1.0 - valley / smaller


def _selftest():
    failures = []

    def check(name, condition, detail=""):
        if condition:
            print("  ok   %s %s" % (name, detail))
        else:
            print("  FAIL %s %s" % (name, detail))
            failures.append(name)

    print("host FFT self-test")
    print("")

    # A tone at a bin centre must read back as its own amplitude.  Everything is
    # in raw sample units: 12-bit codes shifted left by 4, full scale 65520.
    n = 8192
    fs = 4e9
    tone_hz = 250e6
    amplitude_raw = 8000.0          # = 500 codes = 0.0488 V at the 0.2 V range
    t = np.arange(n) / fs
    signal = amplitude_raw * np.cos(2 * np.pi * tone_hz * t) + 32768.0
    codes = np.clip(np.round(signal), 0, 65535).astype(np.uint16)

    amplitude, freqs = amplitude_spectrum(codes[None, :], sample_rate_hz=fs)
    peak = int(np.argmax(amplitude[0]))
    check("bin centre", abs(freqs[peak] - tone_hz) < 1.0,
          "%.1f Hz" % freqs[peak])
    check("amplitude", abs(amplitude[0, peak] - amplitude_raw) < 0.02 * amplitude_raw,
          "%.1f raw units (expected %.0f)" % (amplitude[0, peak], amplitude_raw))

    # Resolution: two equal tones 1 MHz apart.  The phases must be independent:
    # two cosines that both start at zero phase beat into one amplitude-modulated
    # hump sitting at the centre of the window, which reads as a single peak no
    # matter how long the record is.  Averaging over random phase draws is what a
    # pair of incoherent photoacoustic sources actually looks like.
    print("")
    print("  %8s %10s %10s %10s %10s" % ("samples", "bin", "lobe", "dF/bin", "dip"))
    print("  " + "-" * 52)
    rng = np.random.RandomState(20261020)
    for length in (4096, 8192, 16384):
        t = np.arange(length) / fs
        dips = []
        for _ in range(16):
            phase = rng.uniform(0, 2 * np.pi, 2)
            two = (np.cos(2 * np.pi * 250e6 * t + phase[0])
                   + np.cos(2 * np.pi * 251e6 * t + phase[1])) * 4000.0 + 32768.0
            two_codes = np.clip(np.round(two), 0, 65535).astype(np.uint16)
            amp, fr = amplitude_spectrum(two_codes[None, :], sample_rate_hz=fs)
            dips.append(_dip_depth(amp[0], fr, 247e6, 254e6))
        dip = float(np.median(dips))
        separation = 1e6 / resolution_hz(length, fs)
        print("  %8d %8.1f kHz %8.1f kHz %9.2f %9.2f" %
              (length, resolution_hz(length, fs) / 1e3,
               main_lobe_hz(length, fs) / 1e3, separation, dip))
        if length == 4096:
            check("1 MHz merged at 4096", dip < 0.25, "dip %.2f" % dip)
        if length == 8192:
            check("1 MHz partly resolved at 8192", dip > 0.35, "dip %.2f" % dip)
        if length == 16384:
            check("1 MHz resolved at 16384", dip > 0.7, "dip %.2f" % dip)

    print("")
    print("  dip 0 = one hump, 1 = valley down to the baseline.  Hann needs about")
    print("  four bins between the tones before the valley is unambiguous, so")
    print("  1 MHz is comfortable at 16384 points (244 kHz bins) and marginal at")
    print("  8192 (488 kHz bins, two bins apart: a shoulder, not a valley).")

    print("")
    # Averaging on power, not amplitude.
    amp = np.array([[1.0, 1.0], [3.0, 3.0]])
    check("power average", abs(average_amplitude(amp)[0] - np.sqrt(5.0)) < 1e-9,
          "%.4f (expected sqrt(5))" % average_amplitude(amp)[0])

    # Volts scaling: 2048 codes is mid-scale, i.e. 0 V.
    mid = counts_to_volts(np.array([2048 << 4], dtype=np.uint16))
    check("counts_to_volts", abs(mid[0]) < 1e-9, "mid-scale reads %.6f V" % mid[0])

    # A tone at full scale must come back as the input range in volts.  The tone
    # has to sit on a bin centre or leakage alone costs ~10 % of the peak.
    n = 8192
    t = np.arange(n) / fs
    tone_hz = 400 * resolution_hz(n, fs)
    signal = 2048.0 * 16.0 * np.cos(2 * np.pi * tone_hz * t) + 32768.0
    codes = np.clip(np.round(signal), 0, 65535).astype(np.uint16)
    amp, fr = amplitude_spectrum(codes[None, :], sample_rate_hz=fs)
    volts = float(amplitude_to_volts(amp[0, int(np.argmax(amp[0]))]))
    check("full scale is the input range", abs(volts - 0.2) < 0.005,
          "%.4f V (expected 0.2000)" % volts)

    print("")
    if failures:
        print("FAILED: %s" % ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


def _probe(args):
    fft = AlazarHostSpectrum(
        sample_rate_hz=args.rate_msps * 1e6,
        record_length=args.record_length,
        input_range_volts=args.input_range,
        trigger=args.trigger,
        trigger_timeout_ticks=args.timeout_ticks,
        verbose=True,
    )
    print("opening...")
    fft.open()
    try:
        fft.configure()
        print("reading %d record(s)..." % args.records)
        started = time.time()
        amplitude, freqs = fft.read_spectrum(records=args.records)
        elapsed = time.time() - started
        peak = int(np.argmax(amplitude[1:])) + 1
        print("")
        print("read %.1f ms  (%d records, %.1f records/s)"
              % (elapsed * 1e3, args.records, args.records / max(elapsed, 1e-9)))
        print("peak   : %.3f MHz  %.1f counts" % (freqs[peak] / 1e6, amplitude[peak]))
        print("noise  : %.1f counts (median)" % np.median(amplitude))
        print("bins   : %d, %.1f kHz apart, main lobe %.1f kHz"
              % (freqs.size, fft.bin_width_hz / 1e3, fft.lobe_hz / 1e3))
    finally:
        fft.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Host-side FFT at 4 GS/s, long enough to resolve 1 MHz.")
    parser.add_argument("--selftest", action="store_true",
                        help="check the normalisation and the resolution claim, no hardware")
    parser.add_argument("--probe", action="store_true",
                        help="read one spectrum from the board")
    parser.add_argument("--rate-msps", type=float, default=DEFAULT_SAMPLE_RATE_HZ / 1e6)
    parser.add_argument("--record-length", type=int, default=DEFAULT_RECORD_LENGTH)
    parser.add_argument("--records", type=int, default=64)
    parser.add_argument("--input-range", type=float, default=DEFAULT_INPUT_RANGE)
    parser.add_argument("--trigger", choices=("external", "disable"), default=DEFAULT_TRIGGER)
    parser.add_argument("--timeout-ticks", type=int, default=DEFAULT_TRIGGER_TIMEOUT_TICKS)
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.probe:
        return _probe(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
