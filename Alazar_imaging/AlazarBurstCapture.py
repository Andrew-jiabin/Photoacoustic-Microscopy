# -*- coding: utf-8 -*-
"""Burst capture on the ATS9373, plus a 10 MHz amplitude demodulator.

Why this is not ``AlazarNPTSystem``
-----------------------------------
``AlazarNPTSystem`` is the imaging path and it is shared by every entry point:
it arms the board on the laser trigger and returns one A-line per pulse.  It is
deliberately left untouched.  This module is the other mode -- the caller asks
for exactly the number of records it needs, and the board may or may not have a
trigger cable attached.

Trigger: follow the laser if it is there, self-trigger if it is not
-------------------------------------------------------------------
``TRIG_EXTERNAL`` plus a non-zero timeout does both, with no reconfiguration and
no mode switch:

* laser sync pulse present on TRIG IN -> the board waits for it, so every record
  starts on a laser pulse and the A-lines are phase-locked.  This is what makes
  summing ``records_per_point`` records meaningful.
* no pulse (laser off, cable unplugged) -> the timeout expires and the board
  triggers itself, so the approach loop still gets readings.

The one hard constraint is **the timeout must be longer than the laser period**.
If it is shorter, the board times out before every pulse, never locks, and the
sum is taken over records that land at random phases of the laser cycle.  At
1050 Hz the period is 952 us, so the default is 150 ticks (1.5 ms).

``trigger="disable"`` restores the old behaviour: TRIG IN is ignored and the
board always self-triggers.  Use it only when there is no laser to follow.

Two jobs:

1. **Point record.**  At the approach setpoint, grab ``records_per_point``
   A-lines and sum them, so the saved payload keeps the exact contract the
   MATLAB scripts expect: ``(1, 4096)`` uint16 at 4 GS/s, ``metadata`` with
   ``records_per_point`` / ``is_averaged`` / ``step_size``.

2. **Calibrated amplitude.**  ``demod_amplitude()`` turns a raw record into the
   peak amplitude of the 10 MHz component *in volts*, which is what makes an
   AFM-style threshold interpretable.  The on-FPGA FFT path in
   ``AlazarFFTSpectrum`` reports board units; comparing the two on the same
   signal is the calibration procedure.
"""

from __future__ import annotations

import argparse
import re
import sys

import numpy as np

import atsapi as ats


DEFAULT_SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
DEFAULT_SAMPLE_RATE_HZ = 4e9
DEFAULT_SAMPLES_PER_RECORD = 4096
DEFAULT_INPUT_RANGE = ats.INPUT_RANGE_PM_200_MV
DEFAULT_INPUT_RANGE_VOLTS = 0.2

# 12-bit samples arrive left-aligned in a 16-bit word.
_COUNTS_PER_FULL_SCALE = 2048.0
_SAMPLE_SHIFT = 4

# Trigger source.  "external" follows the laser sync pulse on TRIG IN and falls
# back to the timeout when none arrives; "disable" ignores TRIG IN entirely.
DEFAULT_TRIGGER = "external"

# Trigger timeout in ticks of 10 us.  Zero means "wait forever for a trigger",
# which stalls this board when no trigger cable is attached (verified on
# hardware: ApiWaitTimeout).  A non-zero value is what lets the board carry on
# without a trigger cable.
#
# With trigger="external" this value is also the fallback period, so it MUST be
# longer than the laser period -- otherwise the board times out before every
# pulse and the records are never phase-locked.  Measured laser: 1050 Hz,
# period 952 us.  150 ticks = 1.5 ms leaves ~57 % margin.
DEFAULT_TRIGGER_TIMEOUT_TICKS = 150

# External trigger front end: DC coupling, +/-2.5 V range, positive slope,
# threshold level 153 of 255 = +0.49 V.  These are the settings the trigger was
# characterised with (PAM_触发频率实测_20261006.md); the sync pulse is ~4 us wide
# and sits well above 0.49 V.
#
# getattr because the offline test replaces atsapi with a stub that carries only
# the constants it needs; the numeric values are the SDK's own.
DEFAULT_TRIGGER_COUPLING = getattr(ats, "DC_COUPLING", 1)
DEFAULT_TRIGGER_RANGE = getattr(ats, "ETR_2V5", 3)
DEFAULT_TRIGGER_LEVEL = 153


def _sample_rate_map():
    """``{Hz: ats.SAMPLE_RATE_*}`` built from the constants this SDK actually has.

    The SDK identifies sample rates by opaque constant, not by a number, so the
    rate in Hz and the constant must be kept in step.  Deriving the map from the
    module avoids hard-coding a constant that a given SDK build might not
    export.
    """
    mapping = {}
    for name in dir(ats):
        if not name.startswith("SAMPLE_RATE_"):
            continue
        body = name[len("SAMPLE_RATE_"):]
        match = re.match(r"^(\d+)([MK])SPS$", body)
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


class AlazarBurstCapture:
    """Free-running NPT capture of a fixed number of records."""

    def __init__(
        self,
        system_id=1,
        board_id=1,
        sample_rate=None,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        samples_per_record=DEFAULT_SAMPLES_PER_RECORD,
        input_range=DEFAULT_INPUT_RANGE,
        input_range_volts=DEFAULT_INPUT_RANGE_VOLTS,
        records_per_buffer=64,
        buffer_count=4,
        timeout_ms=5000,
        trigger_timeout_ticks=DEFAULT_TRIGGER_TIMEOUT_TICKS,
        trigger=DEFAULT_TRIGGER,
        trigger_level=DEFAULT_TRIGGER_LEVEL,
        verbose=True,
        board=None,
    ):
        self.system_id = system_id
        self.board_id = board_id
        self._shared_board = board
        self.sample_rate = sample_rate_constant(sample_rate_hz) if sample_rate is None else sample_rate
        self.sample_rate_hz = float(sample_rate_hz)
        self.samples_per_record = int(samples_per_record)
        self.input_range = input_range
        self.input_range_volts = float(input_range_volts)
        self.records_per_buffer = int(records_per_buffer)
        self.buffer_count = int(buffer_count)
        self.timeout_ms = int(timeout_ms)
        self.trigger_timeout_ticks = int(trigger_timeout_ticks)
        self.trigger = str(trigger).strip().lower()
        if self.trigger not in ("external", "disable"):
            raise ValueError("trigger must be 'external' or 'disable', got %r" % (trigger,))
        self.trigger_level = int(trigger_level)
        self.verbose = verbose

        self.board = None
        self.buffers = []
        self._buffer_index = 0

    def _log(self, message):
        if self.verbose:
            print("[DAQ] " + str(message))

    # -- lifecycle ---------------------------------------------------------- #

    def open(self):
        if self.board is None:
            self.board = self._shared_board or ats.Board(
                systemId=self.system_id, boardId=self.board_id
            )
            self._log("board opened")
        return self

    def close(self):
        self.buffers = []
        self.board = None

    def configure(self):
        """Clock, input and trigger.

        With ``trigger="external"`` the board waits on TRIG IN and falls back to
        the timeout, which is what makes the same configuration work both with
        the laser running and with it off.
        """
        if self.board is None:
            raise RuntimeError("open() first")
        board = self.board
        board.setCaptureClock(ats.INTERNAL_CLOCK, self.sample_rate, ats.CLOCK_EDGE_RISING, 0)
        board.inputControlEx(
            ats.CHANNEL_A, ats.DC_COUPLING, self.input_range, ats.IMPEDANCE_50_OHM
        )
        if self.trigger == "external":
            board.setExternalTrigger(DEFAULT_TRIGGER_COUPLING, DEFAULT_TRIGGER_RANGE)
            board.setTriggerOperation(
                ats.TRIG_ENGINE_OP_J,
                ats.TRIG_ENGINE_J, ats.TRIG_EXTERNAL, ats.TRIGGER_SLOPE_POSITIVE,
                self.trigger_level,
                ats.TRIG_ENGINE_K, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
            )
        else:
            board.setTriggerOperation(
                ats.TRIG_ENGINE_OP_J,
                ats.TRIG_ENGINE_J, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
                ats.TRIG_ENGINE_K, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
            )
        board.setTriggerDelay(0)
        # A non-zero timeout is what makes this work with no trigger cable: the
        # board waits `trigger_timeout_ticks` x 10 us and then triggers itself.
        # Zero means "wait forever", and this board does exactly that.
        board.setTriggerTimeOut(self.trigger_timeout_ticks)
        board.configureAuxIO(ats.AUX_OUT_TRIGGER, 0)
        self._log(
            "configured fs=%.3f GS/s record=%d trigger=%s timeout=%d ticks (%.1f ms)"
            % (self.sample_rate_hz / 1e9, self.samples_per_record, self.trigger,
               self.trigger_timeout_ticks, self.trigger_timeout_ticks * 0.01)
        )
        return self

    # -- acquisition -------------------------------------------------------- #

    def capture(self, records):
        """Capture ``records`` A-lines and return them as ``(records, samples)`` uint16."""
        if self.board is None:
            raise RuntimeError("open() first")
        records = int(records)
        if records <= 0:
            raise ValueError("records must be positive")

        board = self.board
        per_buffer = min(self.records_per_buffer, records)
        bytes_per_buffer = 2 * self.samples_per_record * per_buffer

        self.buffers = [
            ats.DMABuffer(board.handle, _ctypes_uint16(), bytes_per_buffer)
            for _ in range(self.buffer_count)
        ]

        board.beforeAsyncRead(
            ats.CHANNEL_A,
            0,
            self.samples_per_record,
            per_buffer,
            records,
            ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT,
        )
        for buffer in self.buffers:
            board.postAsyncBuffer(buffer.addr, buffer.size_bytes)

        collected = []
        try:
            board.startCapture()
            remaining = records
            self._buffer_index = 0
            while remaining > 0:
                buffer = self.buffers[self._buffer_index % len(self.buffers)]
                self._buffer_index += 1
                board.waitAsyncBufferComplete(buffer.addr, timeout_ms=self.timeout_ms)
                view = np.frombuffer(buffer.buffer, dtype=np.uint16)
                usable = (view.size // self.samples_per_record) * self.samples_per_record
                block = view[:usable].reshape(-1, self.samples_per_record)
                take = min(remaining, block.shape[0])
                collected.append(block[:take].copy())
                remaining -= take
                board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
        finally:
            board.abortAsyncRead()

        data = np.concatenate(collected, axis=0) if collected else np.zeros(
            (0, self.samples_per_record), dtype=np.uint16
        )
        if data.shape[0] < records:
            self._log("warning: asked for %d records, got %d" % (records, data.shape[0]))
        return data

    def capture_summed(self, records):
        """Capture and sum ``records`` A-lines along the record axis.

        Returns ``(1, samples)`` uint32.  Summing (not averaging) is what
        ``Nanomax.data_io.package_point_data_for_save`` expects when
        ``average_enable`` is true: it divides by ``records_per_point`` itself.
        """
        data = self.capture(records)
        if data.size == 0:
            return np.zeros((1, self.samples_per_record), dtype=np.uint32)
        # dtype must be given explicitly: numpy's sum() promotes uint32 to the
        # platform's default integer (uint64 on Windows), which breaks the
        # documented contract and, in data_io, the uint16 cast downstream.
        summed = data.astype(np.uint32).sum(axis=0, keepdims=True, dtype=np.uint32)
        return summed

    def capture_averaged(self, records):
        """Capture and average, returned as ``(1, samples)`` uint16."""
        summed = self.capture_summed(records)
        return (summed / max(1, int(records))).astype(np.uint16)


# --------------------------------------------------------------------------- #
# Signal processing (pure, no hardware)
# --------------------------------------------------------------------------- #

def counts_to_volts(samples, input_range_volts=DEFAULT_INPUT_RANGE_VOLTS):
    """Convert raw uint16 codes (12-bit, left aligned) to volts."""
    codes = np.asarray(samples, dtype=np.float64) / float(1 << _SAMPLE_SHIFT)
    return (codes - _COUNTS_PER_FULL_SCALE) / _COUNTS_PER_FULL_SCALE * float(input_range_volts)


def demod_resolution_hz(sample_rate_hz, n_samples):
    """Smallest frequency gap this demodulator can separate, in Hz.

    A Hann window has a four-bin-wide main lobe, so the passband is about
    ``4 * fs / n``.  At 4 GS/s with 4096 samples that is 3.9 MHz -- wide enough
    to measure the amplitude of a clean 10 MHz carrier, but nowhere near enough
    to tell 10.0 MHz from 10.5 MHz.  The on-FPGA FFT at 100 MS/s has a ~98 kHz
    main lobe instead; that selectivity is the real reason to prefer it when
    interference or harmonics are present.
    """
    return 4.0 * float(sample_rate_hz) / float(n_samples)


def demod_amplitude(samples, sample_rate_hz, f0_hz, input_range_volts=DEFAULT_INPUT_RANGE_VOLTS):
    """Peak amplitude of the ``f0`` component, in volts.

    Hann-windowed quadrature demodulation over one record.  A window is required
    because 4096 points at 4 GS/s hold 10.24 cycles of 10 MHz -- not an integer
    number, so a rectangular window would leak badly.

    The factor of two converts the one-sided complex amplitude back to the peak
    amplitude of the real sinusoid.
    """
    x = counts_to_volts(samples, input_range_volts)
    n = x.size
    if n < 8:
        return 0.0
    window = np.hanning(n)
    t = np.arange(n, dtype=np.float64) / float(sample_rate_hz)
    phasor = np.exp(-2j * np.pi * float(f0_hz) * t)
    windowed = x * window
    coefficient = np.sum(windowed * phasor)
    return float(2.0 * np.abs(coefficient) / np.sum(window))


def demod_amplitude_mean(records, sample_rate_hz, f0_hz, input_range_volts=DEFAULT_INPUT_RANGE_VOLTS):
    """Mean demodulated amplitude over a ``(records, samples)`` array."""
    data = np.asarray(records)
    if data.ndim == 1:
        data = data[None, :]
    if data.shape[0] == 0:
        return 0.0
    values = [
        demod_amplitude(row, sample_rate_hz, f0_hz, input_range_volts) for row in data
    ]
    return float(np.mean(values))


def _ctypes_uint16():
    import ctypes

    return ctypes.c_uint16


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _selftest():
    problems = []

    def check(name, condition, detail=""):
        print("  %-42s %s %s" % (name, "ok" if condition else "FAIL", detail))
        if not condition:
            problems.append(name)

    print("selftest: counts/volts and 10 MHz demodulation")

    fs, n = 4e9, 4096
    t = np.arange(n) / fs

    # Full-scale sine at 10 MHz -> 0.2 V peak on the +/-200 mV range.
    amplitude_v = 0.2
    signal = amplitude_v * np.sin(2 * np.pi * 10e6 * t + 0.7)
    volts = signal / DEFAULT_INPUT_RANGE_VOLTS * _COUNTS_PER_FULL_SCALE + _COUNTS_PER_FULL_SCALE
    codes = np.clip(np.round(volts), 0, 4095).astype(np.uint16) << _SAMPLE_SHIFT

    measured = demod_amplitude(codes, fs, 10e6)
    check("recovers 0.200 V tone", abs(measured - amplitude_v) / amplitude_v < 0.02,
          "got %.4f V" % measured)

    # Half amplitude, and a detuned carrier inside the passband.
    half = (amplitude_v * 0.5) * np.sin(2 * np.pi * 10e6 * t)
    codes_half = np.clip(
        np.round(half / DEFAULT_INPUT_RANGE_VOLTS * _COUNTS_PER_FULL_SCALE + _COUNTS_PER_FULL_SCALE),
        0, 4095,
    ).astype(np.uint16) << _SAMPLE_SHIFT
    measured_half = demod_amplitude(codes_half, fs, 10e6)
    check("recovers 0.100 V tone", abs(measured_half - 0.1) / 0.1 < 0.02,
          "got %.4f V" % measured_half)

    # A 10.5 MHz tone sits inside the 3.9 MHz main lobe of a 1.024 us record, so
    # it is *not* rejected.  This is a documented property, not a defect: assert
    # it so a future change to the window or record length cannot silently alter
    # the selectivity the AFM threshold depends on.
    off = amplitude_v * np.sin(2 * np.pi * 10.5e6 * t)
    codes_off = np.clip(
        np.round(off / DEFAULT_INPUT_RANGE_VOLTS * _COUNTS_PER_FULL_SCALE + _COUNTS_PER_FULL_SCALE),
        0, 4095,
    ).astype(np.uint16) << _SAMPLE_SHIFT
    measured_off = demod_amplitude(codes_off, fs, 10e6)
    check("10.5 MHz stays inside the main lobe", measured_off > 0.05 * amplitude_v,
          "got %.4f V (expected, resolution %.2f MHz)"
          % (measured_off, demod_resolution_hz(fs, n) / 1e6))

    # 10 MHz away is far outside it and must vanish.
    far = amplitude_v * np.sin(2 * np.pi * 20e6 * t)
    codes_far = np.clip(
        np.round(far / DEFAULT_INPUT_RANGE_VOLTS * _COUNTS_PER_FULL_SCALE + _COUNTS_PER_FULL_SCALE),
        0, 4095,
    ).astype(np.uint16) << _SAMPLE_SHIFT
    measured_far = demod_amplitude(codes_far, fs, 10e6)
    check("20 MHz rejected at 10 MHz", measured_far < 0.02 * amplitude_v,
          "got %.5f V" % measured_far)

    resolution = demod_resolution_hz(fs, n)
    check("resolution is 4*fs/n", abs(resolution - 3.90625e6) < 1.0,
          "%.3f MHz at 4 GS/s" % (resolution / 1e6))
    check("100 MS/s FFT is far more selective",
          demod_resolution_hz(100e6, 4096) < 0.1 * resolution,
          "%.1f kHz vs %.2f MHz"
          % (demod_resolution_hz(100e6, 4096) / 1e3, resolution / 1e6))

    # DC must not bias the reading.
    dc_codes = np.full(n, int(round(2048 + 500)), dtype=np.uint16) << _SAMPLE_SHIFT
    check("DC rejected", demod_amplitude(dc_codes, fs, 10e6) < 1e-3,
          "got %.6f V" % demod_amplitude(dc_codes, fs, 10e6))

    print("")
    if problems:
        print("SELFTEST FAILED: %s" % ", ".join(problems))
        return 1
    print("SELFTEST PASSED")
    return 0


def _probe(args):
    daq = AlazarBurstCapture(sample_rate_hz=args.sample_rate_hz)
    daq.open()
    daq.configure()
    try:
        print("")
        print("free-running burst capture, %d records per read" % args.records)
        print("%10s %14s %10s" % ("t (s)", "amp (mV)", "codes p-p"))
        import time

        started = time.time()
        while time.time() - started < args.duration:
            data = daq.capture(args.records)
            amp = demod_amplitude_mean(data, args.sample_rate_hz, args.f0_hz)
            pp = int(data.max()) - int(data.min())
            print("%10.3f %14.3f %10d" % (time.time() - started, amp * 1e3, pp))
            sys.stdout.flush()
    finally:
        daq.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="ATS9373 trigger-free burst capture and 10 MHz demodulation"
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--f0-hz", type=float, default=10e6)
    parser.add_argument("--records", type=int, default=32)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--sample-rate-hz", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.probe:
        return _probe(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
