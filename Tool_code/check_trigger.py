# -*- coding: utf-8 -*-
"""Is a trigger pulse actually arriving on TRIG IN?

With TRIG IN as the trigger source the board replaces the LSB of every sample
with the live state of the trigger input (ATS9373 User Manual V1-5 p.45).  A
long, slow record is therefore a 1-bit copy of the trigger waveform, clocked by
the board's own crystal -- no scope, no extra hardware.

So this reads one 1 MS/s record and counts transitions of bit 4 (the 12-bit
LSB, since samples are left-aligned in the 16-bit word):

* no transitions  -> nothing is connected, or the source is idle
* regular edges   -> trigger present; the period is printed and compared with
                     the timeout, which is the number that decides whether the
                     payload records can lock

Usage::

    python -m Tool_code.check_trigger

ASCII output only: the remote console is GBK.
"""

from __future__ import annotations

import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (REPO_ROOT, os.path.join(REPO_ROOT, "Alazar_imaging", "Samples_Python", "Library")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import numpy as np  # noqa: E402

from Alazar_imaging.AlazarBurstCapture import (  # noqa: E402
    DEFAULT_TRIGGER_TIMEOUT_TICKS,
    AlazarBurstCapture,
)

RATE_HZ = 1e6
SAMPLES = 2_000_000          # 2 s window
TRIGGER_BIT = 4              # 12-bit LSB after the 4-bit left shift


def check(rate_hz=RATE_HZ, samples=SAMPLES,
          timeout_ticks=DEFAULT_TRIGGER_TIMEOUT_TICKS, verbose=True):
    """Return ``dict`` describing the trigger line.  Never raises on no-trigger."""
    daq = AlazarBurstCapture(
        sample_rate_hz=rate_hz,
        samples_per_record=int(samples),
        input_range_volts=0.2,
        trigger="external",
        trigger_timeout_ticks=int(timeout_ticks),
        records_per_buffer=1,
        verbose=False,
    )
    daq.open()
    try:
        daq.configure()
        started = time.time()
        data = daq.capture(1)
        elapsed = time.time() - started
    finally:
        daq.close()

    row = data[0].astype(np.uint32)
    bits = (row >> TRIGGER_BIT) & 1
    edges = int(np.count_nonzero(np.diff(bits)))

    result = {
        "samples": int(row.size),
        "elapsed_s": elapsed,
        "left_aligned": not bool(np.any(row & 0x0F)),
        "duty_pct": 100.0 * float(bits.sum()) / bits.size,
        "edges": edges,
        "period_us": None,
        "freq_hz": None,
        "jitter_ppm": None,
        "timeout_us": timeout_ticks * 10.0,
        "locked": False,
    }

    if edges == 0:
        if verbose:
            print("captured %d samples in %.2f s" % (row.size, elapsed))
            print("  low bits 0..3 constant : %s"
                  % ("yes" if result["left_aligned"] else "no"))
            print("  trigger bit high       : %.2f %% duty" % result["duty_pct"])
            print("  transitions            : 0")
            print("")
            print("VERDICT: no trigger pulse seen.")
            print("  Nothing is driving TRIG IN.  The board falls back to the")
            print("  %d-tick timeout, so approach readings still work but the"
                  % timeout_ticks)
            print("  payload records are NOT laser-locked.")
        return result

    idx = np.flatnonzero(np.diff(bits))
    spacing = np.diff(idx)
    period_s = float(np.median(spacing)) / rate_hz
    result["period_us"] = period_s * 1e6
    result["freq_hz"] = 1.0 / period_s
    result["jitter_ppm"] = 1e6 * float(np.std(spacing)) / float(np.mean(spacing))
    result["locked"] = period_s * 1e6 < result["timeout_us"]

    if verbose:
        print("captured %d samples in %.2f s" % (row.size, elapsed))
        print("  low bits 0..3 constant : %s"
              % ("yes" if result["left_aligned"] else "no"))
        print("  trigger bit high       : %.2f %% duty" % result["duty_pct"])
        print("  transitions            : %d" % edges)
        print("")
        print("VERDICT: trigger present.")
        print("  frequency : %.3f Hz" % result["freq_hz"])
        print("  period    : %.1f us" % result["period_us"])
        print("  jitter    : %.1f ppm" % result["jitter_ppm"])
        print("")
        if result["locked"]:
            margin = 100.0 * (1.0 - result["period_us"] / result["timeout_us"])
            print("  OK: the timeout is %.0f %% longer than the period, so every"
                  % margin)
            print("  record waits for a pulse instead of timing out.")
        else:
            print("  WARNING: the period is longer than the timeout (%.1f ms)."
                  % (result["timeout_us"] / 1000.0))
            print("  The board will time out before every pulse and the payload")
            print("  will not lock.  Set PAM_AFM_LASER_PERIOD_US=%.0f"
                  % result["period_us"])
    return result


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Read the TRIG IN line through the sample LSB (read-only)")
    parser.add_argument("--rate-hz", type=float, default=RATE_HZ)
    parser.add_argument("--samples", type=int, default=SAMPLES)
    parser.add_argument("--timeout-ticks", type=int, default=DEFAULT_TRIGGER_TIMEOUT_TICKS)
    args = parser.parse_args(argv)

    print("reading the trigger line through the sample LSB")
    print("  rate    : %.0f MS/s" % (args.rate_hz / 1e6))
    print("  window  : %.1f s" % (args.samples / args.rate_hz))
    print("  timeout : %d ticks (%.1f ms) -- must exceed the laser period"
          % (args.timeout_ticks, args.timeout_ticks * 0.01))
    print("")
    result = check(args.rate_hz, args.samples, args.timeout_ticks)
    return 0 if result["edges"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
