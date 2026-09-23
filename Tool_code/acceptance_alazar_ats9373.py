# -*- coding: utf-8 -*-
"""Acceptance check for the Alazar ATS9373 acquisition path.

Purpose
-------
Verify that the ATSApi runtime and the project's own DAQ wrapper
(``Alazar_imaging.AlazarNPTSystem``) are functional on this machine.

Two modes
---------
1. Default (config-only, completely safe)
   Runs up to ``prepare_acquisition()`` and stops. ``start_capture()`` is
   never called, so nothing is armed and no DMA happens.

2. ``--capture`` (one real acquisition, still no laser required)
   Goes one step further: NPT mode plus a **non-zero trigger timeout** makes
   the board self-trigger when no signal is present. That exercises the whole
   ``arm -> trigger -> wait -> read buffer -> re-post -> abort`` DMA path
   without a laser or an external trigger source.

Usage
-----
Run from the repository root::

    python Tool_code\\acceptance_alazar_ats9373.py
    python Tool_code\\acceptance_alazar_ats9373.py --capture

Exit codes
----------
0 = pass, 1 = ``atsapi`` import failed, 2 = wrapper/init failed,
3 = real acquisition failed.

Notes
-----
* Output is deliberately ASCII-only. A Chinese Windows console is GBK, so
  printing ``um`` / an em dash / a check mark raises ``UnicodeEncodeError``.
* Sample encoding: the 12-bit code sits in the **high 12 bits** of the 16-bit
  word, so 0 V reads back as 32768 and the signed value is ``raw - 32768``.
"""
import ctypes
import inspect
import os
import sys
import time

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

DO_CAPTURE = "--capture" in sys.argv

# --- acquisition parameters for the laser-free test -------------------------
POST = 2048                 # samples per record
RECORDS_PER_BUFFER = 10
BUFFERS = 2
TIMEOUT_TICKS = 100000      # non-zero -> board auto-triggers with no signal
WAIT_MS = 20000


def sep(title):
    print("")
    print("=== %s ===" % title)


def as_tuple(value):
    for fn in (lambda v: tuple(x.value for x in v),
               lambda v: tuple(bytes(v))):
        try:
            return fn(value)
        except Exception:
            pass
    return value


# --------------------------------------------------------- 1. atsapi binding
sep("1. atsapi pure bind")
try:
    import atsapi as ats
except Exception as exc:
    print("  import atsapi          : FAIL %r" % (exc,))
    sys.exit(1)
print("  import atsapi          : OK")
print("  atsapi file            : %s" % getattr(ats, "__file__", "?"))

for name in ("getSDKVersion", "getDriverVersion"):
    try:
        print("  %-22s : %s" % (name, as_tuple(getattr(ats, name)())))
    except Exception as exc:
        print("  %-22s : FAIL %r" % (name, exc))

try:
    print("  numOfSystems           : %s" % (ats.numOfSystems(),))
except Exception as exc:
    print("  numOfSystems           : FAIL %r" % (exc,))

try:
    print("  boardsInSystemByID(1)  : %s" % (ats.boardsInSystemBySystemID(1),))
except Exception as exc:
    print("  boardsInSystemByID(1)  : FAIL %r" % (exc,))

# ------------------------------------------------------------ 2. board identity
sep("2. board identity")
try:
    board = ats.Board(systemId=1, boardId=1)
    print("  ats.Board(systemId=1, boardId=1) : OK")
except Exception as exc:
    print("  ats.Board(...)         : FAIL %r" % (exc,))
    sys.exit(1)

try:
    print("  getBoardKind()         : %s   (ATS9373 = %s)"
          % (board.getBoardKind(), ats.ATS9373))
except Exception as exc:
    print("  getBoardKind()         : FAIL %r" % (exc,))

for pname in ("GET_SERIAL_NUMBER", "GET_CHANNELS_PER_BOARD", "GET_FPGA_TEMPERATURE"):
    try:
        print("  getParameter(0,%-22s): %r"
              % (pname, board.getParameter(0, getattr(ats, pname))))
    except Exception as exc:
        print("  getParameter(0,%-22s): FAIL %r" % (pname, exc))

try:
    # First element is an "unsupported on this model" sentinel on some boards;
    # the meaningful value is bitsPerSample.
    print("  getChannelInfo()       : %r" % (board.getChannelInfo(),))
except Exception as exc:
    print("  getChannelInfo()       : FAIL %r" % (exc,))

# --------------------------------------- 3. wrapper init path (no acquisition)
sep("3. DAQ wrapper init path (no capture)")
try:
    from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
    print("  import AlazarNPTSystem : OK")
except Exception as exc:
    print("  import AlazarNPTSystem : FAIL %r" % (exc,))
    sys.exit(2)

try:
    print("  configure_board sig    : %s"
          % (inspect.signature(AlazarNPTSystem.configure_board),))
    print("  prepare_acq sig        : %s"
          % (inspect.signature(AlazarNPTSystem.prepare_acquisition),))
except Exception:
    pass

try:
    daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=1004,
                          channel_A_range=ats.INPUT_RANGE_PM_200_MV)
    print("  construct              : OK")
except Exception as exc:
    print("  construct              : FAIL %r" % (exc,))
    sys.exit(2)

t0 = time.time()
try:
    daq.configure_board(ats.SAMPLE_RATE_4000MSPS)
    print("  configure_board(4000MSPS): OK  %.3f s" % (time.time() - t0))
except Exception as exc:
    print("  configure_board(4000MSPS): FAIL %r" % (exc,))
    sys.exit(2)

t0 = time.time()
try:
    daq.prepare_acquisition()
    print("  prepare_acquisition()  : OK  %.3f s" % (time.time() - t0))
except Exception as exc:
    print("  prepare_acquisition()  : FAIL %r" % (exc,))
    sys.exit(2)

print("  start_capture()        : NOT CALLED (config-only mode)")

if not DO_CAPTURE:
    sep("RESULT: PASS (config-only)")
    print("  Re-run with --capture for a real acquisition (still no laser).")
    sys.exit(0)

# ------------------------------- 4. real acquisition, laser-free (trigger timeout)
sep("4. REAL acquisition, laser-free (NPT + trigger timeout)")

import numpy as np


def show(name, value):
    print("  %-38s: %s" % (name, value))


try:
    board.setCaptureClock(ats.INTERNAL_CLOCK, ats.SAMPLE_RATE_4000MSPS,
                          ats.CLOCK_EDGE_RISING, 0)
    show("setCaptureClock(INTERNAL, 4000MSPS)", "OK")

    board.inputControlEx(ats.CHANNEL_A, ats.DC_COUPLING,
                         ats.INPUT_RANGE_PM_400_MV, ats.IMPEDANCE_50_OHM)
    show("inputControlEx(CHA, DC, 400mV, 50ohm)", "OK")

    board.setTriggerOperation(ats.TRIG_ENGINE_OP_J,
                              ats.TRIG_ENGINE_J, ats.TRIG_CHAN_A,
                              ats.TRIGGER_SLOPE_POSITIVE, 150,
                              ats.TRIG_ENGINE_K, ats.TRIG_DISABLE,
                              ats.TRIGGER_SLOPE_POSITIVE, 128)
    show("setTriggerOperation(J=CHA pos @150)", "OK")

    board.setTriggerDelay(0)
    # The key line: a non-zero timeout makes the board trigger on its own.
    board.setTriggerTimeOut(TIMEOUT_TICKS)
    show("setTriggerTimeOut(%d)" % TIMEOUT_TICKS,
         "OK (auto-trigger, no signal)")

    try:
        board.configureAuxIO(ats.AUX_OUT_TRIGGER, 0)
        show("configureAuxIO(AUX_OUT_TRIGGER)", "OK")
    except Exception as exc:
        show("configureAuxIO(AUX_OUT_TRIGGER)", "SKIPPED %r" % (exc,))

    info = board.getChannelInfo()
    bits = int(info[1].value) if hasattr(info[1], "value") else int(info[1])
    bytes_per_sample = (bits + 7) // 8
    bytes_per_buffer = bytes_per_sample * POST * RECORDS_PER_BUFFER
    show("bitsPerSample / bytesPerSample", "%d / %d" % (bits, bytes_per_sample))
    show("bytesPerBuffer", bytes_per_buffer)

    sample_type = ctypes.c_uint8 if bytes_per_sample == 1 else ctypes.c_uint16
    buffers = [ats.DMABuffer(board.handle, sample_type, bytes_per_buffer)
               for _ in range(4)]
    show("DMA buffers allocated",
         "%d x %d B = %.2f MB" % (len(buffers), bytes_per_buffer,
                                  len(buffers) * bytes_per_buffer / 1048576.0))

    board.setRecordSize(0, POST)
    board.beforeAsyncRead(ats.CHANNEL_A, 0, POST, RECORDS_PER_BUFFER,
                          RECORDS_PER_BUFFER * BUFFERS,
                          ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT
                          | ats.ADMA_FIFO_ONLY_STREAMING)
    show("beforeAsyncRead(NPT, CHA)", "OK")

    for buf in buffers:
        board.postAsyncBuffer(buf.addr, buf.size_bytes)
    show("postAsyncBuffer x %d" % len(buffers), "OK")

    t0 = time.time()
    board.startCapture()
    show("startCapture()", "OK (armed)")

    done = 0
    total_bytes = 0
    first_record = None
    try:
        while done < BUFFERS:
            buf = buffers[done % len(buffers)]
            t1 = time.time()
            board.waitAsyncBufferComplete(buf.addr, WAIT_MS)
            dt = time.time() - t1
            done += 1
            total_bytes += buf.size_bytes
            arr = np.asarray(buf.buffer).ravel()
            if first_record is None:
                first_record = arr[:POST].copy()
            show("buffer %d complete (%.3f s)" % (done, dt),
                 "n=%d min=%d max=%d mean=%.1f"
                 % (arr.size, arr.min(), arr.max(), float(arr.mean())))
            board.postAsyncBuffer(buf.addr, buf.size_bytes)
    finally:
        board.abortAsyncRead()
        show("abortAsyncRead()", "OK")

    elapsed = time.time() - t0
    show("TOTAL", "%d buffers / %d records / %d bytes in %.3f s"
         % (done, done * RECORDS_PER_BUFFER, total_bytes, elapsed))

    if first_record is not None:
        signed = first_record.astype(np.int32) - 32768
        show("record 0 (signed, 12-bit<<4)",
             "min=%d max=%d std=%.1f"
             % (signed.min(), signed.max(), float(signed.std())))
        show("record 0 nonzero samples",
             "%d / %d" % (int(np.count_nonzero(signed)), signed.size))

    sep("RESULT: %s"
        % ("PASS (config + real acquisition)" if done == BUFFERS else "INCOMPLETE"))
except Exception as exc:
    print("  REAL acquisition       : FAIL %r" % (exc,))
    sep("RESULT: FAIL")
    sys.exit(3)
