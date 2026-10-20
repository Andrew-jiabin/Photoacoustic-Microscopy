# -*- coding: utf-8 -*-
"""Does the on-FPGA FFT really stop at 4096 points?  Ask the driver.

``AlazarFFTSpectrum`` refuses ``fft_length > 4096`` because the DSP module
reports ``maxLength = 4096``.  That is worth verifying rather than trusting,
because the cap decides something important: at 4 GS/s a 4096-point record is
1.024 us, so the bins are 976.6 kHz apart and the Hann main lobe is 1953 kHz --
two peaks 1 MHz apart merge into one hump.  If the cap were only on the
*transform* length, a longer record could be zero-padded and the answer would be
different (still no real resolution, but worth knowing).

So this asks in three ways and prints the driver's own error strings:

  1. every DSP module the board exposes, with its maxLength
  2. ``AlazarFFTSetup`` with record == fft == 8192 and 16384 (a real longer FFT)
  3. ``AlazarFFTSetup`` with a 4096 record and an 8192/16384 transform (padding)

Measured on the ATS9373 (PAM-NEW, 2026-10-20)::

    FFT module v5.32 maxLength=4096
      4096 points        -> ok, 8192 bytes/record, 2048 bins, 976.6 kHz
      8192 points        -> ApiFftSizeTooLarge
     16384 points        -> ApiFftSizeTooLarge
    record 4096, fft  8192 -> ApiFftSizeTooLarge
    record 4096, fft 16384 -> ApiFftSizeTooLarge

So the cap is real, it applies to both arguments, and there is no way around it
from the host.  Since the point count is fixed, the only lever left on resolution
is the clock -- which is why the scan program defaults the on-chip FFT to
2 GS/s (488.3 kHz bins, 976.6 kHz lobe, 0..1 GHz) rather than 4 GS/s.

ASCII output only: the remote console is GBK.
"""

from __future__ import annotations

import argparse
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_REPO, os.path.join(_REPO, "Alazar_imaging", "Samples_Python", "Library")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import atsapi as ats  # noqa: E402

from Alazar_imaging.AlazarBurstCapture import sample_rate_constant  # noqa: E402

MODULE_NAMES = {
    ats.DSP_MODULE_FFT: "FFT",
    ats.DSP_MODULE_PCD: "PCD",
    ats.DSP_MODULE_SSK: "SSK",
    ats.DSP_MODULE_DIS: "DIS",
}


def modules(board):
    """Every DSP module, as ``(index, id, name, version, max_length)``."""
    found = []
    for index, module in enumerate(board.dspGetModules()):
        module_id, major, minor, max_length = module.dspGetInfo()
        found.append((index, module_id, MODULE_NAMES.get(module_id, "?"),
                      "%d.%d" % (major, minor), max_length, module))
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check the on-FPGA FFT's maximum record length, from the driver.")
    parser.add_argument("--system-id", type=int, default=1)
    parser.add_argument("--board-id", type=int, default=1)
    parser.add_argument("--rate-msps", type=float, default=4000.0)
    parser.add_argument("--input-range", type=float, default=0.2,
                        help="only used to put the input in a known state")
    args = parser.parse_args(argv)

    board = ats.Board(systemId=args.system_id, boardId=args.board_id)
    print("board opened (system %d, board %d)" % (args.system_id, args.board_id))
    print("")

    found = modules(board)
    print("DSP modules: %d" % len(found))
    fft_module = None
    max_length = 0
    for index, module_id, name, version, limit, module in found:
        print("  [%d] id=0x%05X %-4s v%s maxLength=%d"
              % (index, module_id, name, version, limit))
        if module_id == ats.DSP_MODULE_FFT and fft_module is None:
            fft_module = module
            max_length = limit
    if fft_module is None:
        print("")
        print("no DSP_MODULE_FFT on this board; the on-FPGA FFT is unavailable")
        return 1
    print("")

    # A known input state, so the only thing under test is the size.
    # setCaptureClock wants the SDK's rate constant, not the number in Hz.
    rate_hz = int(round(args.rate_msps)) * 1000000
    board.setCaptureClock(ats.INTERNAL_CLOCK, sample_rate_constant(rate_hz),
                          ats.CLOCK_EDGE_RISING, 0)
    board.inputControlEx(ats.CHANNEL_A, ats.DC_COUPLING,
                         ats.INPUT_RANGE_PM_400_MV if args.input_range >= 0.4
                         else ats.INPUT_RANGE_PM_200_MV,
                         ats.IMPEDANCE_50_OHM)
    board.setTriggerOperation(
        ats.TRIG_ENGINE_OP_J,
        ats.TRIG_ENGINE_J, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
        ats.TRIG_ENGINE_K, ats.TRIG_DISABLE, ats.TRIGGER_SLOPE_POSITIVE, 128,
    )
    board.setTriggerDelay(0)
    board.setTriggerTimeOut(1)

    def attempt(record_length, fft_length):
        try:
            nbytes = fft_module.fftSetup(
                ats.CHANNEL_A, record_length, fft_length,
                ats.FFT_OUTPUT_FORMAT_FLOAT_AMP2, ats.FFT_FOOTER_NONE, 0)
        except Exception as exc:                              # noqa: BLE001
            message = str(exc)
            # The ctypes wrapper buries the driver string at the end; the part
            # after the last ':' is the only bit worth showing.
            short = message.rsplit(":", 1)[-1].strip().strip("b'\"")
            return None, short
        bins = nbytes // 4                 # FLOAT_AMP2 is one float per bin
        return bins, None

    print("AlazarFFTSetup, record length == transform length:")
    for length in (4096, 8192, 16384):
        bins, error = attempt(length, length)
        if error is None:
            print("  %6d points -> ok, %d bins, bin width %.1f kHz"
                  % (length, bins, (args.rate_msps * 1e6 / length) / 1e3))
        else:
            print("  %6d points -> %s" % (length, error))
    print("")

    print("AlazarFFTSetup, zero-padded (short record, long transform):")
    for record, fft_length in ((4096, 8192), (4096, 16384)):
        bins, error = attempt(record, fft_length)
        if error is None:
            print("  record %5d, fft %5d -> ok, %d bins" % (record, fft_length, bins))
        else:
            print("  record %5d, fft %5d -> %s" % (record, fft_length, error))
    print("")

    if max_length <= 4096:
        print("VERDICT: the on-FPGA FFT is capped at %d points." % max_length)
        print("")
        print("At %.0f MS/s that is a %.3f us window, so:" % (
            args.rate_msps, max_length / (args.rate_msps * 1e6) * 1e6))
        bin_hz = args.rate_msps * 1e6 / max_length
        print("  bin %.1f kHz, Hann main lobe %.1f kHz" % (bin_hz / 1e3, 2 * bin_hz / 1e3))
        print("")
        print("The cap is on the point count, so a longer time window needs either")
        print("a slower clock or a host-side transform.")
        print("  slower clock: 2 GS/s over the same %d points gives %.3f us and"
              % (max_length, max_length / 2e9 * 1e6))
        print("    488.3 kHz bins -- 1 MHz is then two bins apart, a notch (valley")
        print("    depth 0.37 after power averaging) rather than a clean valley.")
        print("    Rejected anyway: the board's Fs/4 spur then lands on 500 MHz, and")
        print("    AlazarSetCaptureClock blocks 87-193 ms while the PLL relocks, so")
        print("    switching per point costs 359 ms.")
        print("  host: 16384 points at 4 GS/s over numpy, 244.1 kHz bins, valley 0.99.")
        print("    Hann needs about four bins of separation, i.e. 244 kHz, and this")
        print("    module tops out at two.  So the on-chip FFT cannot cleanly resolve")
        print("    1 MHz at any usable clock; use PAM_AFM_FFT_BACKEND=host for that.")
        return 0

    print("VERDICT: maxLength=%d; the on-chip FFT can go beyond 4096." % max_length)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
