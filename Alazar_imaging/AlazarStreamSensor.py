# -*- coding: utf-8 -*-
"""Continuous 4 GS/s stream on one input, with a 10 MHz amplitude readout.

Why this module exists
----------------------
``AlazarBurstCapture`` aborts and re-arms the board for every reading.  On the
ATS9373 that cycle costs ~46 ms, which is where the old "20 readings/s" figure
came from -- it was never the board's record rate.

Leaving the stream running changes the picture completely.  Measured on the
board at 4 GS/s, 4096 points, 8 records per DMA buffer, host-side demodulation
of the newest record in each buffer:

    trigger timeout   records/s   readings/s   demod
    10 us             196 962      24 620       5.7 us
    100 us             22 180       2 772       6.2 us
    1 ms                2 016         252      11.9 us
    10 ms                 200          25      22.6 us

So one continuous stream covers both jobs in an AFM-style scan: the approach
feedback and the imaging payload come from the same records, and no channel,
clock or trigger reconfiguration is needed between points.

Trigger
-------
``TRIG_EXTERNAL`` with a non-zero timeout.  With the laser off (approach) the
board self-triggers when the timeout expires; with the laser on (imaging) the
laser wins as long as the timeout is longer than the laser period.  That single
setting is what makes the "no switching" claim true.

Channel rules measured on the board (ATS9373 User Manual V1-5 p.46, p.66, and
the driver's own error strings):

* 4 GS/s exists only in single-channel DES mode;
* asking for A+B at 4 GS/s fails with ``ApiDESOnlySupportedInSingleChannelMode``;
* asking for B alone at 4 GS/s fails with ``ApiInconsistentChannel``.

There is therefore no way to keep A at 4 GS/s and record a second channel at the
same time.  The answer is not a second channel, it is a single continuous stream.

Command line
------------
``python -m Alazar_imaging.AlazarStreamSensor --selftest``
    Checks the demodulator with no hardware.

``python -m Alazar_imaging.AlazarStreamSensor --probe``
    Opens the board, streams for a few seconds and prints the amplitude and the
    achieved reading rate.  No trigger cable, no stage motion.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from collections import namedtuple

import numpy as np

import atsapi as ats


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
DEFAULT_SAMPLE_RATE_HZ = 4e9
DEFAULT_RECORD_LENGTH = 4096
DEFAULT_INPUT_RANGE = ats.INPUT_RANGE_PM_200_MV
DEFAULT_INPUT_RANGE_VOLTS = 0.2
DEFAULT_F0_HZ = 10e6

#: 150 ticks = 1.5 ms.  The timeout must stay longer than the laser period during
#: imaging, otherwise the board self-triggers between laser pulses and the
#: payload records are no longer laser-synchronised.  Measured laser: 1050 Hz,
#: 952 us period, so 1.5 ms leaves ~57 % margin instead of the 5 % a 1 ms
#: timeout would leave.  With the laser off this is the approach period, which
#: still gives ~170 readings/s -- ample at 1 um/s.
DEFAULT_TIMEOUT_TICKS = 150

DEFAULT_RECORDS_PER_BUFFER = 8
DEFAULT_BUFFER_COUNT = 4
DEFAULT_TIMEOUT_MS = 2000

# 12-bit samples arrive left-aligned in a 16-bit word.
_COUNTS_PER_FULL_SCALE = 2048.0
_SAMPLE_SHIFT = 4

Reading = namedtuple("Reading", "amplitude_v interval_s sequence")


def mask_of(value):
    """``"A"``, ``"B"``, ``"A+B"`` or an SDK mask -> a channel mask.

    The board has exactly two analog inputs.  AUX I/O is a 3.3 V TTL digital
    input (``GET_AUX_INPUT_LEVEL`` returns 0 or 1) and TRIG IN is a comparator
    that "cannot be digitized" (ATS9373 User Manual V1-5 p.45), so there is no
    third analog path to fall back on.
    """
    if isinstance(value, str):
        text = value.strip().upper().replace(" ", "")
        resolved = {
            "A": ats.CHANNEL_A,
            "B": ats.CHANNEL_B,
            "A+B": ats.CHANNEL_A | ats.CHANNEL_B,
            "B+A": ats.CHANNEL_A | ats.CHANNEL_B,
            "AB": ats.CHANNEL_A | ats.CHANNEL_B,
        }.get(text)
        if resolved is None:
            raise ValueError("channels must be 'A', 'B' or 'A+B', got %r" % (value,))
        return resolved
    mask = int(value)
    if mask not in (ats.CHANNEL_A, ats.CHANNEL_B,
                    ats.CHANNEL_A | ats.CHANNEL_B):
        raise ValueError("channels must be A, B or A+B, got mask 0x%X" % mask)
    return mask


def _mask_name(mask):
    return {
        ats.CHANNEL_A: "A",
        ats.CHANNEL_B: "B",
        ats.CHANNEL_A | ats.CHANNEL_B: "A+B",
    }.get(mask, "0x%X" % mask)


def channel_mask(value):
    """Like :func:`mask_of`, but only one channel (used for the signal input)."""
    mask = mask_of(value)
    if mask == (ats.CHANNEL_A | ats.CHANNEL_B):
        raise ValueError("a signal channel must be A or B, not A+B")
    return mask


# --------------------------------------------------------------------------- #
# Demodulation (pure, no hardware)
# --------------------------------------------------------------------------- #

def counts_to_volts(samples, input_range_volts=DEFAULT_INPUT_RANGE_VOLTS):
    """Raw uint16 codes (12-bit, left aligned) to volts."""
    codes = np.asarray(samples, dtype=np.float64) / float(1 << _SAMPLE_SHIFT)
    return (codes - _COUNTS_PER_FULL_SCALE) / _COUNTS_PER_FULL_SCALE * float(input_range_volts)


def make_demod(n_samples, sample_rate_hz, f0_hz):
    """Precompute the windowed phasor so a reading costs one dot product.

    The window matters: 4096 points at 4 GS/s hold 10.24 cycles of 10 MHz, so a
    rectangular window would leak badly.  Building the phasor once and reusing
    it is what brings the per-reading cost down to ~6 us.
    """
    t = np.arange(int(n_samples), dtype=np.float64) / float(sample_rate_hz)
    window = np.hanning(int(n_samples))
    phasor = window * np.exp(-2j * np.pi * float(f0_hz) * t)
    return phasor, float(window.sum())


def demod_amplitude(samples, phasor, norm, input_range_volts=DEFAULT_INPUT_RANGE_VOLTS):
    """Peak amplitude of the carrier in volts, for one record."""
    volts = counts_to_volts(samples, input_range_volts)
    return float(2.0 * abs(np.dot(volts, phasor)) / norm)


# --------------------------------------------------------------------------- #
# Continuous stream
# --------------------------------------------------------------------------- #

class AlazarStreamSensor(object):
    """One free-running 4 GS/s stream, read as fresh 10 MHz amplitudes.

    Typical use::

        sensor = AlazarStreamSensor(timeout_ticks=100)
        sensor.open()
        sensor.configure()
        sensor.start()
        try:
            while running:
                reading = sensor.read()
                controller.update(reading.amplitude_v)
        finally:
            sensor.stop()
            sensor.close()

    ``reading.interval_s`` is the time since the previous reading, which is also
    the worst-case staleness of the value: the newest record in a buffer is at
    most one buffer interval old.
    """

    def __init__(
        self,
        system_id=1,
        board_id=1,
        sample_rate=None,
        sample_rate_hz=DEFAULT_SAMPLE_RATE_HZ,
        channel="A",
        channels=None,
        signal_channel=None,
        record_length=DEFAULT_RECORD_LENGTH,
        f0_hz=DEFAULT_F0_HZ,
        input_range=DEFAULT_INPUT_RANGE,
        input_range_volts=DEFAULT_INPUT_RANGE_VOLTS,
        records_per_buffer=DEFAULT_RECORDS_PER_BUFFER,
        buffer_count=DEFAULT_BUFFER_COUNT,
        timeout_ticks=DEFAULT_TIMEOUT_TICKS,
        timeout_ms=DEFAULT_TIMEOUT_MS,
        verbose=True,
        board=None,
    ):
        self.system_id = system_id
        self.board_id = board_id
        self._shared_board = board
        self.sample_rate = (
            sample_rate if sample_rate is not None
            else _sample_rate_constant(sample_rate_hz)
        )
        self.sample_rate_hz = float(sample_rate_hz)
        self.channel_mask = mask_of(channels if channels is not None else channel)
        self.channel_count = 2 if self.channel_mask == (
            ats.CHANNEL_A | ats.CHANNEL_B) else 1
        if signal_channel is None:
            # Single input: the amplitude comes from that input.  Dual: default
            # to A, which is the imaging channel.
            self.signal_channel = (
                self.channel_mask if self.channel_count == 1 else ats.CHANNEL_A
            )
        else:
            self.signal_channel = channel_mask(signal_channel)
            if not (self.channel_mask & self.signal_channel):
                raise ValueError(
                    "signal channel %s is not among the acquired channels"
                    % ("A" if self.signal_channel == ats.CHANNEL_A else "B")
                )
        self.record_length = int(record_length)
        self.f0_hz = float(f0_hz)
        self.input_range = input_range
        self.input_range_volts = float(input_range_volts)
        self.records_per_buffer = int(records_per_buffer)
        self.buffer_count = int(buffer_count)
        self.timeout_ticks = int(timeout_ticks)
        self.timeout_ms = int(timeout_ms)
        self.verbose = verbose

        self.board = None
        self.buffers = []
        self._posted = []
        self._index = 0
        self._running = False
        self._sequence = 0
        self._t_prev = None
        self._phasor = None
        self._norm = None
        self._t_start = None
        self.readings = 0
        self.last_interval = 0.0
        self.errors = []

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def running(self):
        return self._running

    def _log(self, message):
        if self.verbose:
            print("[STREAM] " + str(message))

    def open(self):
        if self.board is not None:
            return self
        self.board = self._shared_board or ats.Board(
            systemId=self.system_id, boardId=self.board_id
        )
        return self

    def close(self):
        self.stop()
        self.buffers = []
        self._posted = []
        if self._shared_board is None:
            self.board = None

    def configure(self):
        """Clock, input and trigger.  Safe to call once per scan."""
        if self.board is None:
            raise RuntimeError("open() first")
        board = self.board

        # Measured on the board: 4 GS/s exists only in single-channel DES mode,
        # and DES is wired to A.  Fail here with a sentence instead of at
        # startCapture with a driver code.
        if self.sample_rate_hz > 2e9:
            if self.channel_count == 2:
                raise ValueError(
                    "A+B is limited to 2 GS/s: 4 GS/s exists only in "
                    "single-channel DES mode "
                    "(ApiDESOnlySupportedInSingleChannelMode)"
                )
            if self.channel_mask == ats.CHANNEL_B:
                raise ValueError(
                    "channel B tops out at 2 GS/s on the ATS9373; 4 GS/s is "
                    "single-channel DES mode and is only available on A"
                )

        board.setCaptureClock(ats.INTERNAL_CLOCK, self.sample_rate,
                              ats.CLOCK_EDGE_RISING, 0)
        for ch in (ats.CHANNEL_A, ats.CHANNEL_B):
            if self.channel_mask & ch:
                board.inputControlEx(ch, ats.DC_COUPLING, self.input_range,
                                     ats.IMPEDANCE_50_OHM)

        # External trigger plus a timeout: the laser wins when it is present,
        # the timeout keeps data flowing when it is not.
        board.setTriggerOperation(
            ats.TRIG_ENGINE_OP_J,
            ats.TRIG_ENGINE_J, ats.TRIG_EXTERNAL, ats.TRIGGER_SLOPE_POSITIVE, 128,
            ats.TRIG_ENGINE_K, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
        )
        board.setTriggerDelay(0)
        board.setTriggerTimeOut(self.timeout_ticks)

        self._phasor, self._norm = make_demod(
            self.record_length, self.sample_rate_hz, self.f0_hz
        )
        self._log(
            "configured %.3f GS/s, channels %s (signal %s), %d points, timeout %d ticks"
            % (self.sample_rate_hz / 1e9, _mask_name(self.channel_mask),
               "A" if self.signal_channel == ats.CHANNEL_A else "B",
               self.record_length, self.timeout_ticks)
        )
        return self

    def _ensure_buffers(self):
        if self.buffers:
            return
        bytes_per_buffer = (2 * self.record_length * self.records_per_buffer
                            * self.channel_count)
        self.buffers = [
            ats.DMABuffer(self.board.handle, ctypes.c_uint16, bytes_per_buffer)
            for _ in range(self.buffer_count)
        ]
        self._posted = [False] * len(self.buffers)

    def start(self):
        """Arm the stream once.  Nothing here is repeated per point."""
        if self._running:
            return self
        if self._phasor is None:
            raise RuntimeError("configure() first")

        board = self.board
        self._ensure_buffers()
        board.beforeAsyncRead(
            self.channel_mask, 0, self.record_length, self.records_per_buffer,
            0x7FFFFFFF,
            ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT,
        )
        for index, buffer in enumerate(self.buffers):
            if not self._posted[index]:
                board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
                self._posted[index] = True
        board.startCapture()
        self._running = True
        self._index = 0
        self._sequence = 0
        self._t_prev = None
        self._t_start = time.time()
        self._log("stream started")
        return self

    def stop(self):
        if not self._running:
            return self
        try:
            self.board.abortAsyncRead()
        finally:
            self._running = False
            self._posted = [False] * len(self.buffers)
        return self

    # -- reading ------------------------------------------------------------ #

    def _next_buffer(self):
        """Wait for the next DMA buffer and return ``(view, index)``.

        The caller owns the buffer until it calls ``_release``; posting it back
        before the copy is done would let the board overwrite the data.
        """
        index = self._index % len(self.buffers)
        self.board.waitAsyncBufferComplete(self.buffers[index].addr,
                                           timeout_ms=self.timeout_ms)
        self._posted[index] = False
        self._index += 1
        view = np.frombuffer(self.buffers[index].buffer, dtype=np.uint16)
        return view, index

    def _release(self, index):
        self.board.postAsyncBuffer(self.buffers[index].addr,
                                   self.buffers[index].size_bytes)
        self._posted[index] = True

    def _records(self, view):
        """Split one DMA buffer into per-channel ``(n, record_length)`` arrays.

        Alazar interleaves enabled channels sample by sample: A0 B0 A1 B1 ...
        so the even samples belong to A and the odd ones to B.
        """
        width = self.record_length * self.channel_count
        usable = (view.size // width) * width
        block = view[:usable].reshape(-1, width)
        if self.channel_count == 1:
            return {self.channel_mask: block}
        return {
            ats.CHANNEL_A: block[:, 0::2],
            ats.CHANNEL_B: block[:, 1::2],
        }

    def _read_amplitudes(self):
        """Demodulate the newest record of every enabled channel."""
        view, index = self._next_buffer()
        channels = self._records(view)
        amps = dict(
            (ch, demod_amplitude(arr[-1], self._phasor, self._norm,
                                 self.input_range_volts))
            for ch, arr in channels.items()
        )
        self._release(index)

        now = time.time()
        interval = 0.0 if self._t_prev is None else (now - self._t_prev)
        self._t_prev = now
        self.last_interval = interval
        self._sequence += 1
        self.readings += 1
        return amps, interval

    def read(self):
        """Return the newest amplitude of ``signal_channel`` as a :class:`Reading`."""
        if not self._running:
            raise RuntimeError("start() first")
        amps, interval = self._read_amplitudes()
        return Reading(amps[self.signal_channel], interval, self._sequence)

    def read_channels(self):
        """``{CHANNEL_A: volts, CHANNEL_B: volts}`` for the newest record."""
        if not self._running:
            raise RuntimeError("start() first")
        amps, _ = self._read_amplitudes()
        return amps

    def read_amplitude(self):
        """``read().amplitude_v`` for a tight control loop."""
        return self.read().amplitude_v

    def latest(self, records=1):
        """Return the most recent ``records`` raw records.

        Single channel: ``(n, record_length)`` uint16.  Dual channel: a dict
        ``{CHANNEL_A: (n, len), CHANNEL_B: (n, len)}``.  This is the payload
        path: after the probe touches down, the last few records of the same
        stream are already laser-triggered, so no separate acquisition is
        needed.
        """
        if not self._running:
            raise RuntimeError("start() first")
        records = max(1, int(records))
        collected = []
        have = 0
        while have < records:
            view, index = self._next_buffer()
            channels = self._records(view)
            take = min(records - have, channels[self.signal_channel].shape[0])
            collected.append(dict((ch, arr[-take:].copy())
                                  for ch, arr in channels.items()))
            have += take
            self._release(index)

        out = {}
        for ch in collected[0]:
            out[ch] = np.concatenate([block[ch] for block in collected],
                                     axis=0)[-records:]
        if self.channel_count == 1:
            return out[self.channel_mask]
        return out

    # -- diagnostics -------------------------------------------------------- #

    def stats(self):
        elapsed = (time.time() - self._t_start) if self._t_start else 0.0
        return {
            "readings": self.readings,
            "rate_hz": (self.readings / elapsed) if elapsed > 0 else 0.0,
            "errors": list(self.errors),
        }


def _sample_rate_constant(hz):
    """Map a rate in Hz to the SDK constant this build actually exports."""
    target = int(round(float(hz)))
    for name in dir(ats):
        if not name.startswith("SAMPLE_RATE_"):
            continue
        if name.endswith("MSPS"):
            body = name[len("SAMPLE_RATE_"):-len("MSPS")]
            try:
                value = int(float(body) * 1e6)
            except ValueError:
                continue
            if value == target:
                return getattr(ats, name)
    raise ValueError("no SAMPLE_RATE_* constant for %d Hz in this SDK" % target)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _selftest():
    problems = []

    def check(name, condition, detail=""):
        print("  %-44s %s %s" % (name, "ok" if condition else "FAIL", detail))
        if not condition:
            problems.append(name)

    print("selftest: stream demodulator (no hardware)")

    fs, n, f0 = 4e9, 4096, 10e6
    phasor, norm = make_demod(n, fs, f0)
    t = np.arange(n) / fs

    for amplitude_v in (0.05, 0.1):
        signal = amplitude_v * np.sin(2 * np.pi * f0 * t + 0.3)
        codes = (signal / DEFAULT_INPUT_RANGE_VOLTS * _COUNTS_PER_FULL_SCALE
                 + _COUNTS_PER_FULL_SCALE)
        codes = np.clip(np.round(codes), 0, 4095).astype(np.uint16) << _SAMPLE_SHIFT
        measured = demod_amplitude(codes, phasor, norm)
        check("recovers %.3f V tone" % amplitude_v,
              abs(measured - amplitude_v) / amplitude_v < 0.02,
              "got %.4f V" % measured)

    silence = np.full(n, int(_COUNTS_PER_FULL_SCALE), dtype=np.uint16) << _SAMPLE_SHIFT
    check("silence reads zero", demod_amplitude(silence, phasor, norm) < 1e-4)

    check("channel A resolves", channel_mask("A") == ats.CHANNEL_A)
    check("channel B resolves", channel_mask("B") == ats.CHANNEL_B)
    check("A+B resolves to both",
          mask_of("A+B") == (ats.CHANNEL_A | ats.CHANNEL_B))
    try:
        channel_mask("A+B")
        check("A+B is rejected as a signal channel", False)
    except ValueError:
        check("A+B is rejected as a signal channel", True)

    print("")
    if problems:
        print("SELFTEST FAILED: %s" % ", ".join(problems))
        return 1
    print("SELFTEST PASSED")
    return 0


def _probe(args):
    channels = args.channels or args.channel
    rate = args.sample_rate_hz
    if rate is None:
        rate = 2e9 if channels == "A+B" else DEFAULT_SAMPLE_RATE_HZ

    sensor = AlazarStreamSensor(
        sample_rate_hz=rate,
        channels=channels,
        signal_channel=args.signal_channel,
        record_length=args.record_length,
        f0_hz=args.f0_hz,
        records_per_buffer=args.records_per_buffer,
        timeout_ticks=args.timeout_ticks,
        verbose=not args.quiet,
    )
    sensor.open()
    sensor.configure()
    sensor.start()
    dual = sensor.channel_count == 2
    print("")
    if dual:
        print("%10s %12s %12s %10s" % ("t (s)", "A (mV)", "B (mV)", "dt (ms)"))
    else:
        print("%10s %14s %12s" % ("t (s)", "amp (mV)", "dt (ms)"))
    started = time.time()
    count = 0
    try:
        while time.time() - started < args.duration:
            amps = sensor.read_channels()
            count += 1
            if count <= 10 or count % 50 == 0:
                if dual:
                    print("%10.3f %12.3f %12.3f %10.3f"
                          % (time.time() - started,
                             amps[ats.CHANNEL_A] * 1e3,
                             amps[ats.CHANNEL_B] * 1e3,
                             sensor.last_interval * 1e3))
                else:
                    print("%10.3f %14.3f %12.3f"
                          % (time.time() - started,
                             amps[sensor.signal_channel] * 1e3,
                             sensor.last_interval * 1e3))
                sys.stdout.flush()
    finally:
        sensor.stop()
        sensor.close()
    st = sensor.stats()
    print("")
    print("%d readings, %.1f Hz" % (st["readings"], st["rate_hz"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Continuous 4 GS/s stream with a 10 MHz amplitude readout"
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--channel", default="A", choices=("A", "B"),
                        help="single input (shorthand for --channels)")
    parser.add_argument("--channels", default=None, choices=("A", "B", "A+B"),
                        help="inputs to acquire; A+B runs at 2 GS/s")
    parser.add_argument("--signal-channel", default=None, choices=("A", "B"),
                        help="input the amplitude is taken from (default A)")
    parser.add_argument("--f0-hz", type=float, default=DEFAULT_F0_HZ)
    parser.add_argument("--sample-rate-hz", type=float, default=None,
                        help="default 4 GS/s for one channel, 2 GS/s for A+B")
    parser.add_argument("--record-length", type=int, default=DEFAULT_RECORD_LENGTH)
    parser.add_argument("--records-per-buffer", type=int,
                        default=DEFAULT_RECORDS_PER_BUFFER)
    parser.add_argument("--timeout-ticks", type=int, default=DEFAULT_TIMEOUT_TICKS,
                        help="trigger timeout in 10 us ticks; also sets the "
                             "approach feedback rate when no laser is present")
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    if args.probe:
        return _probe(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
