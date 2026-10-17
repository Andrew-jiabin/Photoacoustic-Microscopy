# -*- coding: utf-8 -*-
"""On-FPGA FFT spectrum acquisition for the ATS9373, without an external trigger.

Why this module exists
----------------------
``AlazarNPTSystem`` is the imaging path: it waits for the laser trigger and
returns raw A-lines.  An AFM-style approach scan needs the opposite trade-off --
a fast, trigger-free measurement of how much 10 MHz energy the probe receives,
repeated many times per second while the piezo creeps towards contact.

The ATS9373 carries an on-FPGA FFT block (ATS9373 User Manual V1-5, "On-FPGA
FFT", p.64):

* up to 4096-point FFT, one input channel only;
* user-programmable complex window applied before the transform;
* output as magnitude, float32 or 16-bit log (also U32/U16/U8 amplitude squared);
* optional simultaneous time-domain + FFT output ("RAW + FFT");
* ATS9373-A3 sustains 250,000 4096-point FFTs/s, the D6 variant 1,000,000/s.

Because the transform runs on the board, the host receives 2048 magnitudes per
record instead of 4096 raw samples, and the CPU never runs an FFT.  That is what
makes a per-approach-step readout cheap.

The DSP block is reachable only through AutoDMA NPT (ATS-SDK Guide 25.1,
section 4.1.2), so this module uses ``ADMA_NPT | ADMA_DSP`` and replaces the
usual ``waitAsyncBufferComplete`` / ``abortAsyncRead`` pair with
``dspGetBuffer`` / ``dspAbortCapture``.  ``AlazarFFTSetup`` must be called
before ``beforeAsyncRead`` and before the DMA buffers are allocated, because it
is the function that reports the output record size.

No trigger is used.  Both trigger engines are set to ``TRIG_DISABLE``, and the
trigger *timeout* is set to a small non-zero value -- that second part is what
actually makes it run.  ``AlazarSetTriggerTimeOut(0)`` means "wait forever for a
trigger", and this board keeps waiting even with the trigger disabled, so a zero
timeout just hangs until ``dspGetBuffer`` reports ``ApiWaitTimeout`` (measured on
the real board, no cable attached).  A non-zero timeout makes the board trigger
itself once the interval expires, i.e. a free-running source with a known,
adjustable period.  ``trigger_timeout_ticks`` is a parameter for that reason: one
tick is 10 us, so the default of 1 asks for the next record as soon as the
previous one finishes.

Units
-----
The FFT magnitude is in board units, not volts.  AlazarTech documents the
internal scaling (``AlazarFFTSetScalingAndSlicing``) only for FFT module
versions below 5, and the exact normalisation also depends on the window and
the FFT length, so this module reports *arbitrary units* (au) and provides
``scale_to_volts()`` for a one-point calibration against a known signal.  For an
approach loop only monotonicity and a threshold in the same units are needed.

Command line
------------
``python -m Alazar_imaging.AlazarFFTSpectrum --selftest``
    Exercises the band-power maths and the frequency mapping with no hardware.

``python -m Alazar_imaging.AlazarFFTSpectrum --probe``
    Opens the board, configures the FFT, free-runs for a few seconds and prints
    the 10 MHz amplitude.  No trigger, no stage motion.

Bundled Python wrapper caveat
-----------------------------
``atsapi.DMABuffer`` only allocates U8 and U16 buffers and raises
``ValueError("Invalid DMABuffer Type")`` for float32 / int32 / uint32 -- which is
why the vendor's own FFT sample defaults to ``U16_LOG``, and why its
``c_float`` branch would crash too.  ``make_dma_buffer()`` below allocates a U16
buffer of the required *byte* count and reinterprets the same memory as float32,
so ``FLOAT_AMP2`` is usable without touching the installed ``atsapi``.

The raw allocator (``AlazarAllocBufferU16``) is a member of the loaded DLL
object, i.e. ``ats.ats.AlazarAllocBufferU16``, and is *not* re-exported at module
level -- calling ``ats.AlazarAllocBufferU16`` raises ``AttributeError``.  Going
through ``DMABuffer`` avoids depending on that distinction, and lets the wrapper
own the matching free.

Freshness of the readout
------------------------
Only ``buffer_count`` DMA buffers are ever posted, so the board cannot run ahead
of the host: once they are full it stalls until one is returned.  That gives a
cheap way to get a fresh reading without restarting -- ``flush()`` reads and
discards exactly ``buffer_count`` buffers, after which the next read is
guaranteed to describe data acquired after the call.

Restarting per reading looks more direct but does not work here: after
``dspAbortCapture`` a second ``beforeAsyncRead`` does not re-arm the DSP path,
and the first buffer comes back mostly NaN (measured on hardware).  So the
stream is started once and kept running.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import numpy as np

import atsapi as ats


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

# 100 MS/s is the sweet spot for a 10 MHz carrier:
#   4096 samples  -> 40.96 us window -> 24.4 kHz bins -> 409.6 cycles of 10 MHz
#   Nyquist 50 MHz, so the first four harmonics of 10 MHz stay in band
#   gapless FFT even on the slower A3 module (4096 / 100 MS/s = 40.96 us
#   per record, versus 4 us of FPGA time per 4096-point FFT)
# At 4 GS/s the same 4096 points only span 10.24 cycles and 977 kHz bins, which
# is far too coarse to separate 10 MHz from its neighbours.
DEFAULT_SAMPLE_RATE = ats.SAMPLE_RATE_100MSPS
DEFAULT_SAMPLE_RATE_HZ = 100e6

DEFAULT_RECORD_LENGTH = 4096
DEFAULT_INPUT_RANGE = ats.INPUT_RANGE_PM_400_MV
DEFAULT_INPUT_RANGE_VOLTS = 0.4

# float32 amplitude squared: no log scaling, no integer slicing, one sqrt away
# from amplitude.  bytes/record = fft_length/2 * 4 = 8192 for a 4096-point FFT.
DEFAULT_OUTPUT_FORMAT = ats.FFT_OUTPUT_FORMAT_FLOAT_AMP2

# Trigger timeout in ticks of 10 us.  0 means "wait forever for a trigger",
# which stalls this board when no trigger cable is attached; 1 (= 10 us) lets
# the board trigger itself and record as fast as the record length allows.
DEFAULT_TRIGGER_TIMEOUT_TICKS = 1


def _sample_rate_map():
    """``{Hz: ats.SAMPLE_RATE_*}`` built from the constants this SDK exports.

    The clock is selected by an opaque SDK constant while every calculation here
    works in Hz, so the two have to be kept in step.  Deriving the map from the
    module avoids hard-coding a constant a given SDK build may not export.
    """
    mapping = {}
    for name in dir(ats):
        if not name.startswith("SAMPLE_RATE_"):
            continue
        match = re.match(r"^(\d+)([MK])SPS$", name[len("SAMPLE_RATE_"):])
        if not match:
            continue
        scale = 1e6 if match.group(2) == "M" else 1e3
        mapping[float(match.group(1)) * scale] = getattr(ats, name)
    return mapping


_SAMPLE_RATE_MAP = _sample_rate_map()


def sample_rate_constant(hz):
    """Return the ``ats.SAMPLE_RATE_*`` constant for a rate given in Hz."""
    target = float(hz)
    for rate, constant in _SAMPLE_RATE_MAP.items():
        if abs(rate - target) <= max(1.0, target * 1e-6):
            return constant
    raise ValueError(
        "%.6g Hz is not an internal sample rate this SDK offers; nearest are %s"
        % (target, ", ".join("%.6g" % r for r in sorted(_SAMPLE_RATE_MAP)[:6]))
    )

_OUTPUT_FORMAT_DTYPE = {
    ats.FFT_OUTPUT_FORMAT_U32: np.uint32,
    ats.FFT_OUTPUT_FORMAT_U16_LOG: np.uint16,
    ats.FFT_OUTPUT_FORMAT_U16_AMP2: np.uint16,
    ats.FFT_OUTPUT_FORMAT_U8_LOG: np.uint8,
    ats.FFT_OUTPUT_FORMAT_U8_AMP2: np.uint8,
    ats.FFT_OUTPUT_FORMAT_REAL_S32: np.int32,
    ats.FFT_OUTPUT_FORMAT_IMAG_S32: np.int32,
    ats.FFT_OUTPUT_FORMAT_FLOAT_AMP2: np.float32,
    ats.FFT_OUTPUT_FORMAT_FLOAT_LOG: np.float32,
}

_OUTPUT_FORMAT_LABEL = {
    ats.FFT_OUTPUT_FORMAT_U32: "U32_AMP2",
    ats.FFT_OUTPUT_FORMAT_U16_LOG: "U16_LOG",
    ats.FFT_OUTPUT_FORMAT_U16_AMP2: "U16_AMP2",
    ats.FFT_OUTPUT_FORMAT_U8_LOG: "U8_LOG",
    ats.FFT_OUTPUT_FORMAT_U8_AMP2: "U8_AMP2",
    ats.FFT_OUTPUT_FORMAT_REAL_S32: "S32_REAL",
    ats.FFT_OUTPUT_FORMAT_IMAG_S32: "S32_IMAG",
    ats.FFT_OUTPUT_FORMAT_FLOAT_AMP2: "FLOAT_AMP2",
    ats.FFT_OUTPUT_FORMAT_FLOAT_LOG: "FLOAT_LOG",
}

_LOG_FORMATS = {
    ats.FFT_OUTPUT_FORMAT_U16_LOG,
    ats.FFT_OUTPUT_FORMAT_U8_LOG,
    ats.FFT_OUTPUT_FORMAT_FLOAT_LOG,
}


# --------------------------------------------------------------------------- #
# Pure helpers (no hardware, unit-tested by --selftest)
# --------------------------------------------------------------------------- #

def frequency_axis(n_bins, fft_length, sample_rate_hz):
    """Bin centre frequencies in Hz for the half spectrum returned by the FPGA."""
    return np.arange(n_bins, dtype=np.float64) * (float(sample_rate_hz) / float(fft_length))


def band_mask(freqs, f0_hz, half_bandwidth_hz):
    """Bins that fall inside ``f0 +/- half_bandwidth``.

    A band rather than a single bin, because 10 MHz rarely lands exactly on a
    bin centre (at 100 MS/s it sits at bin 409.6) and a windowed tone leaks into
    its neighbours.  Summing a small band makes the reading insensitive to a
    percent or two of carrier-frequency error.
    """
    lo = float(f0_hz) - abs(float(half_bandwidth_hz))
    hi = float(f0_hz) + abs(float(half_bandwidth_hz))
    mask = (freqs >= lo) & (freqs <= hi)
    if not mask.any():
        raise ValueError(
            "no FFT bins inside %.3f..%.3f Hz; widen half_bandwidth or check the sample rate"
            % (lo, hi)
        )
    return mask


def band_amplitude(power, freqs, f0_hz, half_bandwidth_hz):
    """sqrt(sum of power in band) -- an RMS-like amplitude in board units."""
    mask = band_mask(freqs, f0_hz, half_bandwidth_hz)
    return float(np.sqrt(np.sum(np.asarray(power, dtype=np.float64)[mask])))


def noise_amplitude(power, freqs, f0_hz, half_bandwidth_hz, guard_bins=6):
    """Median per-bin power outside the signal band, returned as an amplitude.

    Used for a signal-to-noise readout so an approach can refuse to trust a
    threshold it cannot actually resolve.
    """
    mask = band_mask(freqs, f0_hz, half_bandwidth_hz)
    idx = np.flatnonzero(mask)
    lo = max(0, idx[0] - guard_bins)
    hi = min(len(power), idx[-1] + 1 + guard_bins)
    outside = np.ones(len(power), dtype=bool)
    outside[lo:hi] = False
    if not outside.any():
        return 0.0
    return float(np.sqrt(np.median(np.asarray(power, dtype=np.float64)[outside])))


# --------------------------------------------------------------------------- #
# Board driver
# --------------------------------------------------------------------------- #

class AlazarFFTSpectrum:
    """Free-running on-FPGA FFT acquisition on channel A.

    Typical use::

        fft = AlazarFFTSpectrum()
        fft.open()            # board + DSP module discovery
        fft.configure()       # clock, input, trigger off, window, fftSetup
        fft.start()           # free-running
        amp = fft.measure(10e6, 100e3)
        fft.stop()
        fft.close()
    """

    def __init__(
        self,
        system_id=1,
        board_id=1,
        sample_rate=None,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        record_length=DEFAULT_RECORD_LENGTH,
        fft_length=None,
        output_format=DEFAULT_OUTPUT_FORMAT,
        window=ats.DSP_WINDOW_HANNING,
        input_range=DEFAULT_INPUT_RANGE,
        input_range_volts=DEFAULT_INPUT_RANGE_VOLTS,
        records_per_buffer=8,
        buffer_count=4,
        timeout_ms=2000,
        trigger_timeout_ticks=DEFAULT_TRIGGER_TIMEOUT_TICKS,
        verbose=True,
        board=None,
    ):
        self.system_id = system_id
        self.board_id = board_id
        self._shared_board = board
        self.sample_rate = sample_rate_constant(sample_rate_hz) if sample_rate is None else sample_rate
        self.sample_rate_hz = float(sample_rate_hz)
        self.record_length = int(record_length)
        self.fft_length = int(fft_length) if fft_length else _next_power_of_two(self.record_length)
        self.output_format = output_format
        self.window = window
        self.input_range = input_range
        self.input_range_volts = float(input_range_volts)
        self.records_per_buffer = int(records_per_buffer)
        self.buffer_count = int(buffer_count)
        self.timeout_ms = int(timeout_ms)
        self.trigger_timeout_ticks = int(trigger_timeout_ticks)
        self.verbose = verbose

        self.board = None
        self.fft_module = None
        self.bytes_per_record = None
        self.n_bins = None
        self.freqs = None
        self.buffers = []
        self._posted = []
        self._running = False
        self._buffer_index = 0
        self._volts_per_unit = None
        self.module_info = None

    # -- lifecycle ---------------------------------------------------------- #

    def _log(self, message):
        if self.verbose:
            print("[FFT] " + str(message))

    def open(self):
        if self.board is not None:
            return self
        # A caller that needs to switch between FFT and time-domain modes can
        # hand in one board handle so both wrappers share it.
        self.board = self._shared_board or ats.Board(
            systemId=self.system_id, boardId=self.board_id
        )
        modules = self.board.dspGetModules()
        if not modules:
            raise RuntimeError(
                "this board reports no DSP modules; the on-FPGA FFT is unavailable "
                "(wrong board, or the FPGA image does not include the FFT block)"
            )

        # A board can expose several DSP modules (FFT, PCD, SSK, DIS); pick the
        # FFT one instead of assuming it is first.
        chosen = None
        seen = []
        for module in modules:
            module_id, major, minor, max_length = module.dspGetInfo()
            seen.append("0x%X v%d.%d max=%d" % (module_id, major, minor, max_length))
            if module_id == ats.DSP_MODULE_FFT and chosen is None:
                chosen = (module, module_id, major, minor, max_length)

        if chosen is None:
            raise RuntimeError(
                "no DSP_MODULE_FFT on this board; modules found: %s" % "; ".join(seen)
            )

        self.fft_module, module_id, major, minor, max_length = chosen
        self.module_info = {
            "module_id": module_id,
            "version": "%d.%d" % (major, minor),
            "max_length": max_length,
            "count": len(modules),
            "all_modules": seen,
        }
        if self.fft_length > max_length:
            raise RuntimeError(
                "requested FFT length %d exceeds the module maximum %d"
                % (self.fft_length, max_length)
            )
        self._log(
            "module id=0x%X version=%d.%d max_length=%d modules=%d"
            % (module_id, major, minor, max_length, len(modules))
        )
        return self

    def close(self):
        if self._running:
            self.stop()
        self.buffers = []
        self._posted = []
        self.board = None
        self.fft_module = None

    # -- configuration ------------------------------------------------------ #

    def configure(self):
        """Configure clock/input/trigger and the FFT block.  No trigger used."""
        if self.board is None:
            raise RuntimeError("open() first")

        board = self.board

        # Clock: internal, fixed rate.
        board.setCaptureClock(ats.INTERNAL_CLOCK, self.sample_rate, ats.CLOCK_EDGE_RISING, 0)

        # Channel A only: the DSP block can process a single input.
        board.inputControlEx(
            ats.CHANNEL_A, ats.DC_COUPLING, self.input_range, ats.IMPEDANCE_50_OHM
        )

        # Trigger disabled on both engines, plus a non-zero trigger timeout.
        # The timeout is what actually gets data flowing with no trigger cable:
        # a timeout of 0 means "wait forever", and this board honours that even
        # with the trigger disabled (verified on hardware: ApiWaitTimeout).
        board.setTriggerOperation(
            ats.TRIG_ENGINE_OP_J,
            ats.TRIG_ENGINE_J, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
            ats.TRIG_ENGINE_K, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
        )
        board.setTriggerDelay(0)
        board.setTriggerTimeOut(self.trigger_timeout_ticks)
        board.configureAuxIO(ats.AUX_OUT_TRIGGER, 0)

        # Window must be uploaded before fftSetup; it is not affected by
        # AlazarFFTSetup and survives until the next call.
        window = ats.dspGenerateWindowFunction(
            self.window,
            self.record_length,
            self.fft_length - self.record_length,
        )
        # ctypes will not convert a NumPy array to POINTER(c_float) on its own.
        import ctypes

        window_pointer = window.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        self.fft_module.fftSetWindowFunction(self.fft_length, window_pointer, None)

        # fftSetup reports the output record size, so it must run before the DMA
        # buffers are allocated and before beforeAsyncRead.
        self.bytes_per_record = int(
            self.fft_module.fftSetup(
                ats.CHANNEL_A,
                self.record_length,
                self.fft_length,
                self.output_format,
                ats.FFT_FOOTER_NONE,
                0,
            )
        )
        dtype = _OUTPUT_FORMAT_DTYPE[self.output_format]
        self.n_bins = self.bytes_per_record // np.dtype(dtype).itemsize
        self.freqs = frequency_axis(self.n_bins, self.fft_length, self.sample_rate_hz)

        # Sanity: a real-input FFT returns half the transform length, optionally
        # including the Nyquist bin.  Anything else means the format/size pair
        # was not understood and the frequency axis would be wrong.
        allowed = (self.fft_length // 2, self.fft_length // 2 + 1, self.fft_length)
        if self.n_bins not in allowed:
            raise RuntimeError(
                "unexpected FFT output size: %d bins for fft_length=%d, bytes/record=%d, "
                "format=%s (expected one of %s)"
                % (
                    self.n_bins,
                    self.fft_length,
                    self.bytes_per_record,
                    _OUTPUT_FORMAT_LABEL.get(self.output_format, hex(self.output_format)),
                    allowed,
                )
            )

        self._log(
            "configured fs=%.3f MS/s record=%d fft=%d bins=%d bytes/record=%d fmt=%s window=%d trig_timeout=%d ticks"
            % (
                self.sample_rate_hz / 1e6,
                self.record_length,
                self.fft_length,
                self.n_bins,
                self.bytes_per_record,
                _OUTPUT_FORMAT_LABEL.get(self.output_format, hex(self.output_format)),
                self.window,
                self.trigger_timeout_ticks,
            )
        )
        return self

    # -- acquisition -------------------------------------------------------- #

    def _ensure_buffers(self):
        """Allocate the DMA buffers once; they are reused across restarts."""
        if self.buffers:
            return
        dtype = _OUTPUT_FORMAT_DTYPE[self.output_format]
        bytes_per_buffer = self.bytes_per_record * self.records_per_buffer
        self.buffers = [
            make_dma_buffer(self.board, dtype, bytes_per_buffer)
            for _ in range(self.buffer_count)
        ]
        # Which buffers are currently owned by the board.  Posting the same
        # buffer twice corrupts the driver's bookkeeping and yields a partially
        # written buffer (observed on hardware as mostly-NaN float32), so every
        # post/unpost has to be tracked across the abort/restart cycles that
        # measure_fresh() performs.
        self._posted = [False] * len(self.buffers)

    def start(self):
        """Post buffers and start the free-running capture."""
        if self.bytes_per_record is None:
            raise RuntimeError("configure() first")
        if self._running:
            return self

        board = self.board
        self._ensure_buffers()

        # In DSP mode the third argument is the number of *bytes* per output
        # record, not samples -- see ATS-SDK Guide 25.1 section 4.1.3.
        board.beforeAsyncRead(
            ats.CHANNEL_A,
            0,
            self.bytes_per_record,
            self.records_per_buffer,
            0x7FFFFFFF,
            ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT | ats.ADMA_DSP,
        )

        for index, buffer in enumerate(self.buffers):
            if not self._posted[index]:
                board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
                self._posted[index] = True

        board.startCapture()
        self._running = True
        self._buffer_index = 0
        self._log("capture started (free-running, trigger disabled)")
        return self

    def read_spectrum(self, average=True):
        """Return ``(power, freqs)`` for one DMA buffer.

        ``power`` is amplitude squared, averaged over the records in the buffer
        when ``average`` is true.  The buffer is re-posted before returning, so
        consecutive calls stay in lock-step with the board.
        """
        if not self._running:
            raise RuntimeError("start() first")

        buffer = self.buffers[self._buffer_index % len(self.buffers)]
        index = self._buffer_index % len(self.buffers)
        self.board.dspGetBuffer(buffer.addr, self.timeout_ms)
        self._posted[index] = False
        self._buffer_index += 1

        dtype = _OUTPUT_FORMAT_DTYPE[self.output_format]
        view = np.frombuffer(buffer.buffer, dtype=dtype)
        usable = (view.size // self.n_bins) * self.n_bins
        if usable == 0:
            raise RuntimeError(
                "DMA buffer holds %d elements, fewer than one %d-bin record"
                % (view.size, self.n_bins)
            )
        records = view[:usable].reshape(-1, self.n_bins)

        if self.output_format in _LOG_FORMATS:
            # Log outputs are monotonic in power; use them as they are and let
            # the caller compare against a threshold in the same units.
            power = records.astype(np.float64).mean(axis=0) if average else records[-1].astype(np.float64)
        else:
            power = records.astype(np.float64).mean(axis=0) if average else records[-1].astype(np.float64)

        # Hand the buffer back to the board *after* the copy.
        self.board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
        self._posted[index] = True
        return power, self.freqs

    def measure(self, f0_hz, half_bandwidth_hz, buffers=1):
        """Mean 10 MHz-style amplitude over ``buffers`` DMA buffers (board units)."""
        total = 0.0
        for _ in range(max(1, int(buffers))):
            power, freqs = self.read_spectrum()
            total += band_amplitude(power, freqs, f0_hz, half_bandwidth_hz)
        return total / max(1, int(buffers))

    def read_analysis(self, f0_hz, half_bandwidth_hz):
        """One buffer -> ``(amplitude, snr, peak_hz)`` in board units."""
        power, freqs = self.read_spectrum()
        amp = band_amplitude(power, freqs, f0_hz, half_bandwidth_hz)
        noise = noise_amplitude(power, freqs, f0_hz, half_bandwidth_hz)
        snr = amp / noise if noise > 0 else float("inf")
        peak = float(freqs[int(np.argmax(power))])
        return amp, snr, peak

    def measure_with_snr(self, f0_hz, half_bandwidth_hz, buffers=1):
        """Return ``(amplitude, snr)`` with SNR = signal / median out-of-band noise."""
        amps, snrs = [], []
        for _ in range(max(1, int(buffers))):
            amp, snr, _ = self.read_analysis(f0_hz, half_bandwidth_hz)
            amps.append(amp)
            snrs.append(snr)
        return float(np.mean(amps)), float(np.mean(snrs))

    def flush(self, rounds=None):
        """Discard the buffers that were already filled before this call.

        Only ``buffer_count`` buffers are ever posted, so the board cannot run
        ahead of the host by more than that many records: once they are full it
        stalls until one is returned.  Reading and discarding exactly that many
        therefore guarantees the *next* read describes data acquired after this
        call.

        This is how freshness is obtained.  Restarting the capture per reading
        would seem more direct, but ``dspAbortCapture`` followed by a second
        ``beforeAsyncRead`` does not re-arm the DSP path on this board -- the
        first buffer after a restart comes back mostly NaN (measured on
        hardware), so the stream is left running instead.
        """
        if not self._running:
            raise RuntimeError("start() first")
        for _ in range(int(rounds if rounds is not None else len(self.buffers))):
            self.read_spectrum()

    def measure_fresh(self, f0_hz, half_bandwidth_hz, buffers=1):
        """One reading that post-dates this call.

        The stream keeps running; ``flush()`` drops whatever was already in
        flight, so the returned amplitude reflects the current probe position.
        Cost is one DMA buffer of latency, i.e.
        ``records_per_buffer * record_length / sample_rate`` (327 us at the
        default 100 MS/s settings), which is far below any piezo settle time.

        Returns ``(amplitude, snr, peak_hz)``.
        """
        if self.bytes_per_record is None:
            raise RuntimeError("configure() first")
        if not self._running:
            self.start()
        self.flush()
        amps, snrs, peaks = [], [], []
        for _ in range(max(1, int(buffers))):
            amp, snr, peak = self.read_analysis(f0_hz, half_bandwidth_hz)
            amps.append(amp)
            snrs.append(snr)
            peaks.append(peak)
        return float(np.mean(amps)), float(np.mean(snrs)), float(np.mean(peaks))

    def stop(self):
        """Abort the capture.  ``dspAbortCapture`` is mandatory in DSP mode."""
        if self.board is None:
            return
        try:
            if self._running:
                # A capture that already aborted itself (e.g. after a DMA
                # overrun) must not turn cleanup into a second failure.
                try:
                    self.board.dspAbortCapture()
                except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                    self._log("dspAbortCapture reported: %s" % (exc,))
        finally:
            self._running = False
            # An abort returns every posted buffer to the host.
            self._posted = [False] * len(self.buffers)
        self._log("capture stopped")

    # -- calibration -------------------------------------------------------- #

    def set_volts_per_unit(self, scale):
        """Set the factor that converts board units to volts (from a known signal)."""
        self._volts_per_unit = float(scale)
        return self

    def to_volts(self, amplitude):
        if self._volts_per_unit is None:
            raise RuntimeError("no calibration set; call set_volts_per_unit() first")
        return float(amplitude) * self._volts_per_unit


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def _next_power_of_two(value):
    power = 1
    while power < int(value):
        power *= 2
    return power


def _ctypes_type(dtype):
    import ctypes

    mapping = {
        np.dtype(np.uint8): ctypes.c_uint8,
        np.dtype(np.uint16): ctypes.c_uint16,
        np.dtype(np.uint32): ctypes.c_uint32,
        np.dtype(np.int32): ctypes.c_int32,
        np.dtype(np.float32): ctypes.c_float,
    }
    return mapping[np.dtype(dtype)]


class _TypedDMABuffer:
    """DMA buffer for output types ``atsapi.DMABuffer`` refuses.

    The Python wrapper bundled with the ATS-SDK only allocates U8 and U16
    buffers -- ``DMABuffer.__init__`` raises ``ValueError("Invalid DMABuffer
    Type")`` for float32, int32 and uint32.  That is why the vendor's own FFT
    sample defaults to ``FFT_OUTPUT_FORMAT_U16_LOG``; its ``c_float`` branch is
    dead code that would raise.

    ``AlazarAllocBufferU16`` takes a *byte* count, not a sample count, so a U16
    allocation of ``size_bytes`` backs any element type equally well.  This
    class asks ``DMABuffer`` for that allocation -- which also means ``DMABuffer``
    keeps ownership of the matching free -- and then builds the typed view the
    wrapper would have built.
    """

    def __init__(self, handle, c_type, np_dtype, size_bytes):
        import ctypes

        self.handle = handle
        self.size_bytes = int(size_bytes)
        self.c_sample_type = c_type
        # U16 purely as a byte allocator; the view below reinterprets it.
        self._raw = ats.DMABuffer(handle, ctypes.c_uint16, self.size_bytes)
        self.addr = self._raw.addr
        itemsize = np.dtype(np_dtype).itemsize
        if self.size_bytes % itemsize:
            raise ValueError(
                "buffer of %d bytes is not a whole number of %s elements"
                % (self.size_bytes, np.dtype(np_dtype).name)
            )
        array_type = c_type * (self.size_bytes // itemsize)
        self.ctypes_buffer = array_type.from_address(self.addr)
        self.buffer = np.frombuffer(self.ctypes_buffer, dtype=np_dtype)

    def __del__(self):
        # ``self._raw`` frees the allocation when it is collected.
        self._raw = None


def make_dma_buffer(board, np_dtype, size_bytes):
    """Allocate a DMA buffer for any FFT output format."""
    import ctypes

    c_type = _ctypes_type(np_dtype)
    if c_type in (ctypes.c_uint8, ctypes.c_uint16):
        return ats.DMABuffer(board.handle, c_type, size_bytes)
    return _TypedDMABuffer(board.handle, c_type, np_dtype, size_bytes)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _selftest():
    """Check the maths and the frequency mapping without touching hardware."""
    fs, fft_len, n_bins = 100e6, 4096, 2048
    freqs = frequency_axis(n_bins, fft_len, fs)

    problems = []

    def check(name, condition, detail=""):
        status = "ok" if condition else "FAIL"
        print("  %-42s %s %s" % (name, status, detail))
        if not condition:
            problems.append(name)

    print("selftest: frequency axis and band maths")

    df = fs / fft_len
    check("bin spacing", abs(df - 24414.0625) < 1e-6, "%.3f Hz" % df)
    check("10 MHz bin index", abs(10e6 / df - 409.6) < 0.01, "%.2f" % (10e6 / df))
    check("nyquist bin", abs(freqs[-1] - (fs / 2 - df)) < 1e-6, "%.1f Hz" % freqs[-1])

    # Synthetic tone at 10 MHz with a Hann-window-like 3-bin spread plus noise.
    rng = np.random.default_rng(12345)
    power = rng.uniform(1.0, 3.0, size=n_bins)
    centre = int(round(10e6 / df))
    for offset, weight in ((-1, 0.25), (0, 1.0), (1, 0.25)):
        power[centre + offset] += 1000.0 * weight

    amp = band_amplitude(power, freqs, 10e6, 100e3)
    expected = np.sqrt(1000.0 * 1.5 + 2 * 2.0)
    check("band amplitude recovers tone", abs(amp - expected) / expected < 0.05,
          "got %.1f expected %.1f" % (amp, expected))

    # A huge out-of-band tone must not change the in-band reading at all.
    flat = np.full(n_bins, 2.0)
    baseline = band_amplitude(flat, freqs, 10e6, 100e3)
    power2 = flat.copy()
    power2[int(round(12e6 / df))] = 1e6
    amp2 = band_amplitude(power2, freqs, 10e6, 100e3)
    check("12 MHz tone does not leak into the band",
          abs(amp2 - baseline) < 1e-9,
          "%.6f vs baseline %.6f" % (amp2, baseline))

    # Detuning tolerance: a 1 % carrier error still lands inside a 100 kHz band.
    mask = band_mask(freqs, 10.1e6, 100e3)
    check("1 % detune still in band", mask.any(), "%d bins" % int(mask.sum()))

    noise = noise_amplitude(power, freqs, 10e6, 100e3)
    check("noise estimate below signal", noise < amp, "noise %.2f amp %.1f" % (noise, amp))

    print("")
    if problems:
        print("SELFTEST FAILED: %s" % ", ".join(problems))
        return 1
    print("SELFTEST PASSED")
    return 0


def _probe(args):
    """Spectrum readout.  No trigger, no stage motion."""
    fft = AlazarFFTSpectrum(
        sample_rate_hz=args.sample_rate_hz,
        record_length=args.record_length,
        records_per_buffer=args.records_per_buffer,
        trigger_timeout_ticks=args.trigger_timeout_ticks,
        verbose=not args.quiet,
    )
    fft.open()
    print("")
    print("module: id=0x%X v%s max_length=%d (modules on board: %d)"
          % (
              fft.module_info["module_id"],
              fft.module_info["version"],
              fft.module_info["max_length"],
              fft.module_info["count"],
          ))
    fft.configure()
    try:
        mode = "free-running (stale by design)" if args.free_run else "flushed reading"
        print("")
        print("%s for %.1f s, f0 = %.3f MHz, band = +/-%.1f kHz"
              % (mode, args.duration, args.f0_hz / 1e6, args.half_bandwidth_hz / 1e3))
        print("%10s %14s %10s %10s" % ("t (s)", "amp (au)", "snr", "peak (MHz)"))

        if args.free_run:
            fft.start()

        started = time.time()
        readings = 0
        while time.time() - started < args.duration:
            if args.free_run:
                amp, snr, peak = fft.read_analysis(args.f0_hz, args.half_bandwidth_hz)
            else:
                amp, snr, peak = fft.measure_fresh(
                    args.f0_hz, args.half_bandwidth_hz, buffers=args.buffers
                )
            readings += 1
            print("%10.3f %14.4f %10.2f %10.3f"
                  % (time.time() - started, amp, snr, peak / 1e6))
            sys.stdout.flush()

        elapsed = time.time() - started
        print("")
        print("%d readings in %.2f s (%.1f Hz)" % (readings, elapsed, readings / elapsed))
    finally:
        fft.stop()
        fft.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ATS9373 on-FPGA FFT readout (no external trigger required)"
    )
    parser.add_argument("--selftest", action="store_true",
                        help="run the band-power maths check without hardware")
    parser.add_argument("--probe", action="store_true",
                        help="open the board and print the free-running spectrum")
    parser.add_argument("--f0-hz", type=float, default=10e6, help="carrier to track (Hz)")
    parser.add_argument("--half-bandwidth-hz", type=float, default=100e3,
                        help="half width of the integration band (Hz)")
    parser.add_argument("--duration", type=float, default=5.0, help="probe duration (s)")
    parser.add_argument("--sample-rate-hz", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    parser.add_argument("--record-length", type=int, default=DEFAULT_RECORD_LENGTH)
    parser.add_argument("--records-per-buffer", type=int, default=8)
    parser.add_argument("--buffers", type=int, default=1,
                        help="DMA buffers averaged per reading (fresh mode)")
    parser.add_argument("--free-run", action="store_true",
                        help="stream continuously instead of restarting per reading "
                             "(cheaper, but readings can lag the current signal)")
    parser.add_argument("--quiet", action="store_true", help="suppress driver messages")
    parser.add_argument("--trigger-timeout-ticks", type=int,
                        default=DEFAULT_TRIGGER_TIMEOUT_TICKS,
                        help="trigger timeout in 10 us ticks; 0 waits forever and "
                             "stalls when no trigger cable is attached")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.probe:
        return _probe(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
