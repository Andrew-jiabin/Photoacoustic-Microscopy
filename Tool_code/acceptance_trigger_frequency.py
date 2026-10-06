# -*- coding: utf-8 -*-
"""Measure the external trigger frequency seen by the ATS9373.

Purpose
-------
Answer one question with hard numbers: *what rate is the external trigger
actually running at?*  Useful whenever a scan looks slower or faster than it
should be, or after changing the trigger source.

This script is READ-ONLY with respect to the rig: it opens the digitizer only.
No stage is opened, nothing moves, and no acquisition parameter of the imaging
programs is touched.

Two independent methods
-----------------------
The two methods share no code path and no assumption, so agreement between
them is real evidence rather than a restatement.

**Method A - digitise the trigger signal on the sample LSB.**
The ATS9373 User Manual (p.45) states: "When External Trigger Input is used as
the trigger source, the least significant bit (LSB) of each 12-bit sample is
replaced by the state of the external trigger signal source."  One long record
is therefore a 1-bit sampled copy of the trigger waveform, clocked by the
board's own +/-2 ppm oscillator.  Counting rising edges in that bit and
dividing by the exact record duration gives the frequency without involving
the Windows timer at all.  It also yields the pulse width and the jitter.

**Method B - count how many records the trigger produces.**
Arm N records using the imaging programs' exact trigger settings and time it,
for several N, then least-squares fit ``t = T0 + N / r``.  The fit removes the
fixed arming overhead - which is what makes the naive "records / elapsed"
figure read 780 Hz - and the residuals show whether the trigger is regular.

Method B is pulse-width agnostic, so it is the tie-breaker when method A sees
nothing (e.g. a trigger pulse far narrower than the chosen sample period).

Usage
-----
Run from the repository root::

    python Tool_code\\acceptance_trigger_frequency.py
    python Tool_code\\acceptance_trigger_frequency.py --expect 1000

Exit codes
----------
0 = measured, 1 = ``atsapi``/``numpy`` import failed, 2 = board setup failed,
3 = no external trigger detected within the host timeout.

Notes
-----
* Output is deliberately ASCII-only.  A Chinese Windows console is GBK, so
  printing ``um`` / an em dash / a check mark raises ``UnicodeEncodeError``.
* ``atsapi.SAMPLE_RATE_*`` constants are internal CODES, not Hz
  (``SAMPLE_RATE_1MSPS == 20``, ``SAMPLE_RATE_4000MSPS == 128``).  They are the
  correct argument for ``setCaptureClock()`` but must never be used in
  arithmetic; the real Hz value is carried separately here.
* The 12-bit sample is LEFT-JUSTIFIED in the 16-bit word: bits 3..0 are always
  0 and the 12-bit LSB sits in bit 4.  A "look at bit 0" scan therefore sees
  nothing, which is why the bit scan below covers bits 0..11.
"""

import ctypes
import sys
import time

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

try:
    import atsapi as ats
    import numpy as np
except Exception as exc:                                # pragma: no cover
    print("import atsapi/numpy : FAIL %r" % (exc,))
    sys.exit(1)

# --- the imaging programs' external-trigger configuration -------------------
# Keep in sync with Alazar_imaging/AlazarNPTSystem.py configure_board().
TRIG_LEVEL = 153            # 8-bit bipolar: (153 - 128) * 2.5 / 127 = 0.490 V
TRIG_RANGE_V = 2.5          # ETR_2V5
SAMPLES_PER_RECORD = 4096   # both imaging programs use this

# --- host-side timeouts -----------------------------------------------------
# The board is configured with setTriggerTimeOut(0), i.e. "wait forever", which
# is what the imaging programs do.  A host-side wall clock is therefore the only
# thing preventing an indefinite hang when nothing is connected.
LSB_HOST_TIMEOUT_S = 25.0
COUNT_HOST_TIMEOUT_S = 60.0

# (label, setCaptureClock code, real Hz, samples per record) for method A.
LSB_PLAN = (
    ("1 MS/s", ats.SAMPLE_RATE_1MSPS, 1000000.0, 10000000),
    ("10 MS/s", ats.SAMPLE_RATE_10MSPS, 10000000.0, 20000000),
    ("100 MS/s", ats.SAMPLE_RATE_100MSPS, 100000000.0, 50000000),
)

# Record counts for method B.  The loop stops at the first timeout, so a slow
# trigger simply produces fewer fit points instead of a very long run.
COUNT_N = (128, 512, 2048)

EXPECT_HZ = None
if "--expect" in sys.argv:
    try:
        EXPECT_HZ = float(sys.argv[sys.argv.index("--expect") + 1])
    except (IndexError, ValueError):
        print("--expect needs a numeric value, e.g. --expect 1000")
        sys.exit(1)


def sep(title):
    print("")
    print("=" * 74)
    print(title)
    print("=" * 74)


def sub(title):
    print("")
    print("-" * 74)
    print(title)
    print("-" * 74)


def configure(board, rate_code, level=TRIG_LEVEL, delay=0):
    board.setCaptureClock(ats.INTERNAL_CLOCK, rate_code, ats.CLOCK_EDGE_RISING, 0)
    board.inputControlEx(ats.CHANNEL_A, ats.DC_COUPLING,
                         ats.INPUT_RANGE_PM_400_MV, ats.IMPEDANCE_50_OHM)
    board.setExternalTrigger(ats.DC_COUPLING, ats.ETR_2V5)
    board.setTriggerOperation(ats.TRIG_ENGINE_OP_J,
                              ats.TRIG_ENGINE_J, ats.TRIG_EXTERNAL,
                              ats.TRIGGER_SLOPE_POSITIVE, level,
                              ats.TRIG_ENGINE_K, ats.TRIG_DISABLE,
                              ats.TRIGGER_SLOPE_POSITIVE, 128)
    board.setTriggerDelay(delay)
    board.setTriggerTimeOut(0)      # wait forever; the host enforces its own cap


def bytes_per_sample(board):
    info = board.getChannelInfo()
    second = info[1]
    bits = int(second.value) if hasattr(second, "value") else int(second)
    return (bits + 7) // 8


def capture(board, samples_per_record, records, host_timeout_s):
    """Arm `records` records. Returns (elapsed_s, raw_uint16_or_None, err)."""
    bps = bytes_per_sample(board)
    size = bps * samples_per_record * records
    sample_type = ctypes.c_uint8 if bps == 1 else ctypes.c_uint16
    buf = ats.DMABuffer(board.handle, sample_type, size)
    board.setRecordSize(0, samples_per_record)
    board.beforeAsyncRead(ats.CHANNEL_A, 0, samples_per_record, records, records,
                          ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT
                          | ats.ADMA_FIFO_ONLY_STREAMING)
    board.postAsyncBuffer(buf.addr, size)
    t0 = time.perf_counter()
    err = None
    try:
        board.startCapture()
        board.waitAsyncBufferComplete(buf.addr, int(host_timeout_s * 1000))
    except Exception as exc:
        err = exc
    dt = time.perf_counter() - t0
    try:
        board.abortAsyncRead()
    except Exception:
        pass
    if err is not None:
        del buf
        return dt, None, err
    raw = np.array(np.asarray(buf.buffer).ravel(), dtype=np.uint16, copy=True)
    del buf
    return dt, raw, None


def bit_edges(raw, bit):
    """Rising / falling edge count and duty cycle of one bit of the stream."""
    prev = (raw[:-1] >> bit) & 1
    cur = (raw[1:] >> bit) & 1
    rising = int(np.count_nonzero((prev == 0) & (cur == 1)))
    falling = int(np.count_nonzero((prev == 1) & (cur == 0)))
    duty = float(((raw >> bit) & 1).mean())
    return rising, falling, duty


def pulse_stats(raw, bit, rate_hz):
    """High-time widths and edge-to-edge periods of one bit, in seconds."""
    b = ((raw >> bit) & 1).astype(np.int8)
    padded = np.concatenate((np.zeros(1, np.int8), b, np.zeros(1, np.int8)))
    d = np.diff(padded)
    rise = np.flatnonzero(d == 1)
    fall = np.flatnonzero(d == -1)
    widths = (fall - rise) / rate_hz if rise.size and fall.size else np.array([])
    periods = np.diff(rise) / rate_hz if rise.size >= 2 else np.array([])
    return widths, periods


def fit_rate(points):
    """Least-squares fit of t = T0 + N / r over (N, t) samples."""
    if len(points) < 2:
        return None
    ns = np.array([p[0] for p in points], dtype=float)
    ts = np.array([p[1] for p in points], dtype=float)
    design = np.vstack([ns, np.ones_like(ns)]).T
    slope, intercept = np.linalg.lstsq(design, ts, rcond=None)[0]
    if slope <= 0:
        return None
    residual = ts - (design @ np.array([slope, intercept]))
    return 1.0 / slope, intercept, residual


# ---------------------------------------------------------------------------
sep("external trigger frequency measurement (read-only, no stage motion)")
print("  trigger  : TRIG_EXTERNAL, ETR_2V5 (+/-2.5 V), POSITIVE slope, level %d"
      % TRIG_LEVEL)
print("             level %d = %.3f V  [V = (level-128)*%.1f/127]"
      % (TRIG_LEVEL, (TRIG_LEVEL - 128) * TRIG_RANGE_V / 127.0, TRIG_RANGE_V))
print("  timeout  : board waits forever; the host gives up after %.0f s"
      % LSB_HOST_TIMEOUT_S)
print("  motion   : none - no stage is opened")

try:
    board = ats.Board(systemId=1, boardId=1)
except Exception as exc:
    print("  ats.Board(...) : FAIL %r" % (exc,))
    sep("RESULT: FAIL (board setup)")
    sys.exit(2)

# ---------------------------------------------------------------------------
# Method A
# ---------------------------------------------------------------------------
sub("METHOD A - sample-LSB capture of the trigger signal")
print("  ATS9373 manual p.45: with TRIG IN as the trigger source, the LSB of")
print("  every sample carries the live state of the trigger signal.  One long")
print("  record is therefore a 1-bit copy of the trigger waveform.")

lsb_answer = None
for label, code, rate_hz, nsamples in LSB_PLAN:
    window_s = nsamples / rate_hz
    configure(board, code)
    dt, raw, err = capture(board, nsamples, 1, LSB_HOST_TIMEOUT_S)
    if err is not None:
        print("")
        print("  %-9s -> NO TRIGGER within %.0f s (%s)"
              % (label, LSB_HOST_TIMEOUT_S, type(err).__name__))
        print("             no trigger ever arrived - skipping the remaining rates")
        break
    print("")
    print("  %-9s window %6.3f s  armed+read in %5.2f s  (%d samples)"
          % (label, window_s, dt, raw.size))

    rows = [(bit,) + bit_edges(raw, bit) for bit in range(12)]
    active = [row for row in rows if row[1] or row[2]]
    silent = [row[0] for row in rows if not row[1] and not row[2]]
    if silent:
        const = int(((raw >> silent[0]) & 1)[0])
        print("             bits %s are constant (%d) -> the 12-bit sample is"
              % (silent, const))
        print("             left-justified in the 16-bit word")
    if not active:
        print("             no bit moves - no trigger state is visible")
        del raw
        continue

    print("             bit  rising    falling    duty      freq (Hz)")
    for bit, r, f, duty in active:
        print("             %3d  %8d  %8d   %6.2f%%   %11.3f"
              % (bit, r, f, duty * 100.0, r / window_s))

    # The trigger bit is the one whose edge rate is orders of magnitude below
    # the noise bits; a noise bit toggles at roughly a quarter of the sample
    # rate.  Taking the minimum is therefore a safe discriminator.
    bit, r, _, duty = min(active, key=lambda row: row[1])
    freq = r / window_s
    widths, periods = pulse_stats(raw, bit, rate_hz)
    print("")
    print("  -> trigger bit = bit %d" % bit)
    print("     rising edges = %d over %.3f s  =>  %.4f Hz" % (r, window_s, freq))
    if widths.size:
        print("     pulse high time : mean %8.3f us  std %7.3f us  min %.3f  max %.3f"
              % (widths.mean() * 1e6, widths.std() * 1e6,
                 widths.min() * 1e6, widths.max() * 1e6))
    if periods.size >= 2:
        print("     period          : mean %10.6f s  std %.3e s"
              % (periods.mean(), periods.std()))
        print("     jitter (std/mean) = %.4f %%"
              % (100.0 * periods.std() / periods.mean()))
    if lsb_answer is None:
        lsb_answer = freq
    del raw

# ---------------------------------------------------------------------------
# Method B
# ---------------------------------------------------------------------------
sub("METHOD B - count how many records the trigger produces")
print("  Imaging programs' exact settings: %.0f MS/s, %d samples/record."
      % (4000.0, SAMPLES_PER_RECORD))
print("  Fit t = T0 + N / r across several N; the arming overhead T0 cancels.")

configure(board, ats.SAMPLE_RATE_4000MSPS)
points = []
for n in COUNT_N:
    dt, raw, err = capture(board, SAMPLES_PER_RECORD, n, COUNT_HOST_TIMEOUT_S)
    if err is not None:
        print("")
        print("  %6d records -> TIMEOUT after %.1f s  (rate below %.2f Hz)"
              % (n, dt, n / COUNT_HOST_TIMEOUT_S))
        del raw
        break
    print("")
    print("  %6d records -> %8.4f s   (naive %.2f Hz, overhead not yet removed)"
          % (n, dt, n / dt))
    points.append((n, dt))
    del raw

count_answer = None
result = fit_rate(points)
if result is None:
    print("")
    print("  not enough completed runs to fit a rate")
else:
    rate, intercept, residual = result
    count_answer = rate
    print("")
    print("  fit: t = %.6f s + N / %.4f Hz" % (intercept, rate))
    print("       arming overhead T0 = %.4f s" % intercept)
    print("       residuals (s) = %s" % np.array2string(residual, precision=6))
    if len(points) >= 3:
        worst = float(np.max(np.abs(residual)))
        print("       worst residual = %.6f s" % worst)
        print("       linearity: %s"
              % ("GOOD - the trigger is regular" if worst < 0.05
                 else "POOR - the trigger interval is irregular"))
    else:
        print("       linearity: NOT TESTED - a straight line always fits 2 points")

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
sep("VERDICT")
if lsb_answer is None and count_answer is None:
    print("  RESULT: FAIL (no external trigger detected).")
    print("  The board never received a trigger on TRIG IN within the host timeout.")
    print("  Check, in order:")
    print("    1) the source is wired to TRIG IN (the EXT TRIG SMA), not a channel input")
    print("    2) it is a POSITIVE pulse with amplitude above %.3f V" % (
        (TRIG_LEVEL - 128) * TRIG_RANGE_V / 127.0))
    print("    3) the source is actually pulsing")
    try:
        del board
    except Exception:
        pass
    sys.exit(3)

if count_answer is None:
    print("  method A (sample LSB)      : %.4f Hz" % lsb_answer)
    print("  method B (record counting) : not enough completed runs")
    print("  RESULT: PASS (provisional - method B did not confirm)")
else:
    print("  method A (sample LSB)      : %.4f Hz" % (lsb_answer if lsb_answer else -1))
    print("  method B (record counting) : %.4f Hz" % count_answer)
    if lsb_answer:
        print("  agreement                  : %.3f %%"
              % (abs(lsb_answer - count_answer) / count_answer * 100.0))
    if EXPECT_HZ is not None:
        err = abs(count_answer - EXPECT_HZ) / EXPECT_HZ * 100.0
        print("  expected                   : %.4f Hz  (deviation %.3f %%)"
              % (EXPECT_HZ, err))
    print("")
    print("  NOTE: each record needs one trigger and each scan point needs 256")
    print("        (Prior) or 512 (NanoMax) records, so the trigger rate sets the")
    print("        scan speed directly: %.1f Hz -> %.3f s / %.3f s per point."
          % (count_answer, 256.0 / count_answer, 512.0 / count_answer))
    print("  RESULT: PASS")

try:
    del board
except Exception:
    pass

print("")
print("=== DONE (no stage was opened, nothing moved) ===")
