# -*- coding: utf-8 -*-
r"""Read waveforms from a Yokogawa DLM3000-series scope over Ethernet.

Why this file exists
--------------------
The lab DLM3034 has to hand a *fresh* record to the PC: either as the Z
criterion of the AFM loop, or as a slow reference next to the ATS9373.
The scope speaks VXI-11 on the LAN (port 111 is open, the raw-socket port is
not), and pyvisa-py does work -- but it costs 0.3-0.8 s per 250 kB record
because every ``device_read`` is its own RPC round trip and each round trip
stalls ~28 ms.  This module speaks VXI-11 directly: one RPC per record,
``TCP_NODELAY`` on, no VISA, no pyvisa, no third-party dependency.

What the instrument actually does  (measured 2026-10-18, DLM3034, FW 2.00)
-----------------------------------------------------------------------
* ``:WAVEFORM:SEND?`` is the only waveform transfer command.  It answers with a
  definite-length block ``#9<9 digits><payload>``; WORD = 2 B/point.
* ``:WAVEFORM:TRACE`` selects the source (``1``..``4``, ``MATH1``).  It is a
  *selector*, not a query for data -- ``:WAVEFORM:TRACE?`` echoes the setting.
* ``:WAVEFORM:START`` / ``:WAVEFORM:END`` choose a window of the record.
* **The record is only readable while the acquisition is stopped.**  Running,
  ``:WAVEFORM:SEND?`` answers ``#9000000000`` -- a well-formed block of zero
  bytes, which is easy to mistake for "nothing configured".
* ``:STOP`` returns in ~2 ms, but the *first* ``:WAVEFORM:SEND?`` after it
  blocks ~320 ms before the payload appears.  That is the instrument having a
  record ready, and it behaves as a firmware constant: measured identical for
  window 1 000 / 10 000 / 50 000 / 125 000 points, for record length 1 250 /
  12 500 / 125 000 points, for TDIV 200 us / 2 ms / 20 ms, for holdoff 20 ns,
  and for AUTO vs NORMAL trigger with a valid trigger level.  One fresh record
  therefore costs ~0.45 s end to end: **~2.2 Hz is the ceiling**, and it is not
  a bug in this module.  For a 30 ms loop use the digitizer; for anything
  slower, drive it through :class:`Streamer` so the loop never blocks.
* ``:STOP`` **does not freeze the record.**  Read repeatedly after a single
  STOP and the payload keeps changing: the instrument goes on acquiring at its
  own ~2 Hz and hands over whatever is newest.  A burst of reads is cheap
  (~3 ms each) until the buffer has to be refilled, then one read blocks
  ~320 ms and one may come back as a legal zero-byte block.  So
  ``latest()`` is *not* guaranteed to repeat ``read()`` -- treat every read as
  "the newest record", and compare payloads if you need to be sure.
* Multi-channel costs almost nothing **provided the record is complete before
  :STOP** -- see :data:`MULTI_SETTLE_S` and :meth:`DlmScope.read_multi`:
  four traces cost ~40 ms more than one, and the four are then the same
  acquisition (verified 10/10 by re-reading CH1 after the sweep).
* ``:SINGle`` is not implemented on this firmware: the status queue answers
  ``113,"Undefined header;:SINGLE"`` and the command does nothing, so every
  read after it is the same cached record -- verified by changing CH1's offset
  and VDIV and watching the payload not move.  Only START -> STOP gives a new
  record.
* ``:SYSTem:ERRor?`` does not exist on this firmware; the error queue is
  ``:STATus:ERRor?``.  A bad header pushes 113 into that queue and leaves the
  query unanswered, which shows up here as a read timeout, not as an error.
* ``device_write`` must carry the END flag.  Without it the DLM accepts the
  bytes and never executes the command, and the next ``device_read`` dies with
  VXI-11 error 15 after the full io timeout.

Contract
--------
``DlmScope.read()``     -> ``Waveform`` (codes, volts, time base, state)
``DlmScope.latest()``   -> the record already latched, ~3 ms, no re-arm
``DlmScope.stream()``   -> iterator of ``Waveform``, ~2 Hz
``Streamer``            -> background grabber; ``snapshot()`` returns
                           ``(waveform, age_s)`` in microseconds
``DlmScope.measure()``  -> dict of pk-pk / mean / rms, codes and volts
``ScopeSource``         -> ``value()`` adapter for ``Nanomax.afm_control``

Units
-----
The payload is one signed 16-bit code per sample.  ``:WAVEFORM:VDIV?`` is *not*
a valid query on this firmware and the SCPI command reference (IM DLM3054-17EN)
is not in the manual set we own, so the code -> volt constant cannot be read
out of the instrument.  ``CODES_PER_DIV`` is the usual Yokogawa 16-bit
convention (8 vertical divisions across the code range).  ``read()`` always
returns the exact codes; ``Waveform.volts`` is that convention applied to
``:CHANnel<n>:VDIV?``.  Check the volt number once at the bench against the
on-screen amplitude -- the codes are exact either way, and a control loop only
needs a monotone proxy.
"""

from __future__ import absolute_import, division

import math
import socket
import struct
import time

__all__ = [
    "Vxi11Transport", "DlmScope", "Waveform", "Streamer", "ScopeSource",
    "RpcError", "VxiError", "ScopeError",
    "CODES_PER_DIV", "MULTI_SETTLE_S", "block_payload",
]

#: 16-bit codes per vertical division.  See the module docstring: verify once.
CODES_PER_DIV = 8192.0

#: Seconds to let the record run before :STOP when several traces are wanted.
#: Measured on the DLM3034: with 0.02 s a four-trace sweep costs 831 ms and only
#: 1 sweep in 4 is time-aligned; with 0.15 s it costs 593 ms and 4 in 4 are.
#: A single trace is cheapest at 0.02 s, so the default settle stays short and
#: only :meth:`DlmScope.read_multi` raises it.
MULTI_SETTLE_S = 0.15

# --------------------------------------------------------------------------- #
# VXI-11 (ONC RPC) transport
# --------------------------------------------------------------------------- #

PMAP_PROG, PMAP_VERS, PMAP_GETPORT = 100000, 2, 3
CORE_PROG, CORE_VERS = 0x0607AF, 1

CALL, REPLY = 0, 1
MSG_ACCEPTED = 0
SUCCESS = 0

#: Device_Flags bits (VXI-11 spec, table 5).  END is not optional on the DLM.
OP_FLAG_END = 0x08

CREATE_LINK, DEVICE_WRITE, DEVICE_READ = 10, 11, 12
DEVICE_CLEAR, DESTROY_LINK = 15, 23

#: VXI-11 error codes worth naming in a traceback.
VXI_ERRORS = {
    1: "syntax error", 3: "device not accessible", 4: "invalid link identifier",
    5: "parameter error", 6: "channel not established", 8: "operation not supported",
    9: "out of resources", 11: "device locked by another link", 12: "no lock held",
    15: "io timeout", 17: "io error", 21: "invalid address", 23: "abort",
    29: "channel already established",
}


class RpcError(Exception):
    """The ONC RPC layer failed (framing, xid, accept_stat)."""


class VxiError(Exception):
    """The instrument answered with a VXI-11 error code."""

    def __init__(self, code, where):
        self.code = code
        self.where = where
        Exception.__init__(self, "VXI-11 error %d (%s) in %s"
                           % (code, VXI_ERRORS.get(code, "?"), where))


class ScopeError(Exception):
    """The scope answered, but not with something we can use."""


def _pad(n):
    return (4 - (n % 4)) % 4


def _xstr(s):
    b = s.encode("ascii") if not isinstance(s, bytes) else s
    return struct.pack(">I", len(b)) + b + b"\x00" * _pad(len(b))


def _xopaque(b):
    return struct.pack(">I", len(b)) + b + b"\x00" * _pad(len(b))


class Vxi11Transport(object):
    """One VXI-11 core-channel link.  One instance per instrument."""

    def __init__(self, host, device="inst0", timeout=6.0):
        self.host = host
        self.device = device
        self.timeout = float(timeout)
        self.sock = None
        self.lid = None
        self.max_recv = 0
        self._xid = 0

    # -- plumbing ---------------------------------------------------------
    def _recv_exact(self, n, sock):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise RpcError("connection closed after %d/%d B" % (len(buf), n))
            buf += chunk
        return buf

    def _recv_record(self, sock):
        """Read one RPC record, reassembling every fragment of it."""
        body = b""
        while True:
            mark = struct.unpack(">I", self._recv_exact(4, sock))[0]
            body += self._recv_exact(mark & 0x7FFFFFFF, sock)
            if mark & 0x80000000:
                return body

    def _call(self, prog, vers, proc, args, timeout=None, sock=None):
        self._xid += 1
        xid = self._xid
        msg = struct.pack(">IIIIII", xid, CALL, 2, prog, vers, proc)
        msg += struct.pack(">II", 0, 0)     # AUTH_NULL credential
        msg += struct.pack(">II", 0, 0)     # AUTH_NULL verifier
        msg += args
        s = sock or self.sock
        s.settimeout(self.timeout if timeout is None else timeout)
        s.sendall(struct.pack(">I", 0x80000000 | len(msg)) + msg)
        body = self._recv_record(s)

        rxid, mtype, rstat = struct.unpack(">III", body[:12])
        if rxid != xid:
            raise RpcError("xid mismatch: %d != %d" % (rxid, xid))
        if mtype != REPLY:
            raise RpcError("not a reply (msg_type=%d)" % mtype)
        if rstat != MSG_ACCEPTED:
            raise RpcError("RPC denied (reply_stat=%d)" % rstat)
        off = 12
        _vflavor, vlen = struct.unpack(">II", body[off:off + 8])
        off += 8 + vlen + _pad(vlen)
        astat = struct.unpack(">I", body[off:off + 4])[0]
        off += 4
        if astat != SUCCESS:
            raise RpcError("accept_stat=%d (prog %d proc %d)" % (astat, prog, proc))
        return body[off:]

    def _getport(self, prog, vers):
        s = socket.create_connection((self.host, 111), timeout=self.timeout)
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            body = self._call(PMAP_PROG, PMAP_VERS, PMAP_GETPORT,
                              struct.pack(">IIII", prog, vers, 6, 0), sock=s)
            return struct.unpack(">I", body[:4])[0]
        finally:
            s.close()

    # -- public -----------------------------------------------------------
    def open(self):
        port = self._getport(CORE_PROG, CORE_VERS)
        if port == 0:
            raise RpcError("portmapper has no VXI-11 core channel")
        self.sock = socket.create_connection((self.host, port), timeout=self.timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        args = struct.pack(">III", 0x4E414D58, 0, 0) + _xstr(self.device)
        err, lid, _abort, max_recv = struct.unpack(
            ">iiII", self._call(CORE_PROG, CORE_VERS, CREATE_LINK, args)[:16])
        if err != 0:
            raise VxiError(err, "create_link(%s)" % self.device)
        self.lid = lid
        self.max_recv = max_recv
        return self

    def close(self):
        if self.sock is not None and self.lid is not None:
            try:
                self._call(CORE_PROG, CORE_VERS, DESTROY_LINK,
                           struct.pack(">i", self.lid), timeout=1.0)
            except Exception:
                pass
        self.lid = None
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def write(self, data, io_timeout=5000, flags=OP_FLAG_END):
        if isinstance(data, str):
            data = data.encode("ascii")
        args = struct.pack(">iIII", self.lid, io_timeout, 0, flags) + _xopaque(data)
        err, size = struct.unpack(
            ">iI", self._call(CORE_PROG, CORE_VERS, DEVICE_WRITE, args,
                              timeout=io_timeout / 1000.0 + 5.0)[:8])
        if err != 0:
            raise VxiError(err, "device_write")
        return size

    def read(self, size, io_timeout=5000, term=0):
        """One ``device_read``.  Returns ``(data, reason)``.

        ``reason`` bits: 1 = END, 2 = CHR, 4 = REQCNT.  The END bit is *not*
        trustworthy when ``size`` is smaller than the block -- the DLM truncates
        at ``size`` and still sets END -- so ask for at least the whole block.
        """
        args = struct.pack(">iIIII", self.lid, size, io_timeout, 0, 0)
        args += struct.pack(">I", term)
        body = self._call(CORE_PROG, CORE_VERS, DEVICE_READ, args,
                          timeout=io_timeout / 1000.0 + 5.0)
        err, reason, dlen = struct.unpack(">III", body[:12])
        if err != 0:
            raise VxiError(err, "device_read")
        return body[12:12 + dlen], reason


# --------------------------------------------------------------------------- #
# Block framing
# --------------------------------------------------------------------------- #

def block_payload(data):
    """Strip the IEEE 488.2 definite-length header.

    ``#9000020000`` + payload -> payload.  ``b""`` for the empty block
    ``#9000000000`` that the scope sends when there is nothing to transfer.
    """
    if not data:
        return b""
    if data[:1] != b"#":
        return data
    if len(data) < 2:
        raise ScopeError("truncated block header: %r" % data[:8])
    ndig = int(data[1:2])
    if len(data) < 2 + ndig:
        raise ScopeError("truncated block header: %r" % data[:8])
    n = int(data[2:2 + ndig])
    body = data[2 + ndig:2 + ndig + n]
    if len(body) != n:
        raise ScopeError("block declares %d B, got %d" % (n, len(body)))
    return body


# --------------------------------------------------------------------------- #
# Waveform
# --------------------------------------------------------------------------- #

class Waveform(object):
    """One record: exact codes plus everything needed to place it in time."""

    __slots__ = ("codes", "channel", "vdiv", "offset", "srate", "start_index",
                 "format", "codes_per_div", "acquired_at")

    def __init__(self, codes, channel=1, vdiv=1.0, offset=0.0, srate=0.0,
                 start_index=0, fmt="WORD", codes_per_div=CODES_PER_DIV,
                 acquired_at=None):
        self.codes = codes
        self.channel = channel
        self.vdiv = vdiv
        self.offset = offset
        self.srate = srate
        self.start_index = start_index
        self.format = fmt
        self.codes_per_div = codes_per_div
        self.acquired_at = acquired_at if acquired_at is not None else time.time()

    # -- shape ------------------------------------------------------------
    def __len__(self):
        return len(self.codes)

    @property
    def scale(self):
        """Volts per code."""
        return self.vdiv / self.codes_per_div

    def volts(self):
        """Codes -> volts (list, same length).  See the module docstring."""
        k = self.scale
        return [c * k for c in self.codes]

    def times(self):
        """Sample times in seconds, relative to the start of the window."""
        if not self.srate:
            return [float(i) for i in range(len(self.codes))]
        dt = 1.0 / self.srate
        return [i * dt for i in range(len(self.codes))]

    def to_numpy(self):
        """``(t, v)`` as numpy arrays.  Optional: numpy is not a dependency."""
        import numpy as np
        c = np.asarray(self.codes, dtype=np.float64)
        v = c * self.scale
        if self.srate:
            t = np.arange(len(c)) / float(self.srate)
        else:
            t = np.arange(len(c), dtype=np.float64)
        return t, v

    # -- statistics -------------------------------------------------------
    def summary(self):
        if not self.codes:
            return {"points": 0}
        c = self.codes
        n = float(len(c))
        mean = math.fsum(c) / n
        rms = math.sqrt(math.fsum(x * x for x in c) / n)
        lo, hi = min(c), max(c)
        k = self.scale
        return {
            "points": len(c),
            "channel": self.channel,
            "pkpk_codes": hi - lo,
            "min_codes": lo,
            "max_codes": hi,
            "mean_codes": mean,
            "rms_codes": rms,
            "pkpk_v": (hi - lo) * k,
            "mean_v": mean * k,
            "rms_v": rms * k,
            "vdiv": self.vdiv,
            "offset": self.offset,
            "srate": self.srate,
            "scale_v_per_code": k,
            "format": self.format,
        }

    def __repr__(self):
        return ("<Waveform ch%d %d pts %.3g V/div pk-pk %.4g V>"
                % (self.channel, len(self.codes), self.vdiv,
                   self.summary().get("pkpk_v", 0.0)))


# --------------------------------------------------------------------------- #
# The instrument
# --------------------------------------------------------------------------- #

class DlmScope(object):
    """A DLM3000-series scope on the LAN.

    ``channel`` picks the trace; ``codes_per_div`` is the volts convention;
    ``settle_s`` is how long to let a record run after ``:START`` before
    ``:STOP`` (the record length is ``:TIMebase``-dependent, 20 ms by default).
    """

    def __init__(self, host, channel=1, device="inst0", timeout=6.0,
                 codes_per_div=CODES_PER_DIV, settle_s=0.02, verbose=False):
        self.host = host
        self.channel = int(channel)
        self.device = device
        self.codes_per_div = float(codes_per_div)
        self.settle_s = float(settle_s)
        self.verbose = bool(verbose)
        self.transport = None
        self._start = 0
        self._end = None
        self._fmt = "WORD"
        self._byteorder = "LSBFirst"
        self._vdiv = 1.0
        self._offset = 0.0
        self._srate = 0.0
        self._vdivs = {}
        self._last = None
        self.stats = {"reads": 0, "rearm_s": 0.0, "transfer_s": 0.0, "empty": 0,
                      "misaligned": 0}

    # -- lifecycle --------------------------------------------------------
    def open(self):
        self.transport = Vxi11Transport(self.host, self.device)
        self.transport.open()
        self.transport.write(":WAVEFORM:FORMAT WORD\n")
        self.transport.write(":WAVEFORM:BYTEORDER LSBFirst\n")
        self.transport.write(":WAVEFORM:TRACE %d\n" % self.channel)
        self.refresh_state()
        return self

    def refresh_state(self):
        """Re-read the constants that turn codes into volts.  ~3 ms."""
        ch = self.channel
        self._vdivs = {}
        self._vdiv = self.query_float(":CHANnel%d:VDIV?" % ch, 1.0)
        self._vdivs[ch] = self._vdiv
        self._offset = self.query_float(":CHANnel%d:OFFSet?" % ch, 0.0)
        self._srate = self.query_float(":WAVEFORM:SRATE?", 0.0)
        return self._vdiv, self._offset, self._srate

    def ch_vdiv(self, ch):
        """V/div for one channel, cached until the next :meth:`refresh_state`."""
        ch = int(ch)
        if ch not in self._vdivs:
            self._vdivs[ch] = self.query_float(":CHANnel%d:VDIV?" % ch, 1.0)
        return self._vdivs[ch]

    def close(self):
        if self.transport is not None:
            try:
                self.transport.write(":START\n")   # never leave it stopped
            except Exception:
                pass
            self.transport.close()
            self.transport = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- SCPI -------------------------------------------------------------
    def write(self, text, io_timeout=5000):
        if not text.endswith("\n"):
            text += "\n"
        return self.transport.write(text, io_timeout=io_timeout)

    def query(self, text, io_timeout=5000, size=4096):
        if not text.endswith("\n"):
            text += "\n"
        self.transport.write(text, io_timeout=io_timeout)
        data, _reason = self.transport.read(size, io_timeout=io_timeout)
        return data.decode("ascii", "replace").strip()

    def query_float(self, text, default=float("nan")):
        try:
            return float(self.query(text).split()[-1])
        except (VxiError, RpcError, ScopeError, ValueError, IndexError):
            return default

    def errors(self, limit=8):
        """Drain the status queue.  ``:SYSTem:ERRor?`` does not exist here."""
        out = []
        for _ in range(limit):
            try:
                line = self.query(":STATus:ERRor?", io_timeout=2000)
            except (VxiError, RpcError):
                break
            if not line or line.startswith("0,"):
                break
            out.append(line)
        return out

    # -- identity and state ----------------------------------------------
    def idn(self):
        return self.query("*IDN?", io_timeout=3000)

    def state(self):
        """Everything we need to interpret a record, in one round of queries."""
        st = {"idn": self.idn()}
        st["channel"] = self.channel
        st["vdiv"] = self.query_float(":CHANnel%d:VDIV?" % self.channel)
        st["offset"] = self.query_float(":CHANnel%d:OFFSet?" % self.channel)
        st["srate"] = self.query_float(":WAVEFORM:SRATE?")
        st["record_length"] = self.query_float(":ACQuire:RLENgth?")
        st["tdiv"] = self.query_float(":TIMebase:TDIV?")
        st["format"] = self.query(":WAVEFORM:FORMAT?").split()[-1]
        st["byteorder"] = self.query(":WAVEFORM:BYTEORDER?").split()[-1]
        st["trace"] = self.query(":WAVEFORM:TRACE?").split()[-1]
        st["start"] = self.query_float(":WAVEFORM:START?")
        st["end"] = self.query_float(":WAVEFORM:END?")
        st["trigger_mode"] = self.query(":TRIGger:MODE?").split()[-1]
        st["acquire_mode"] = self.query(":ACQuire:MODE?").split()[-1]
        st["bits"] = self.query_float(":WAVEFORM:BITS?")
        return st

    # -- configuration ----------------------------------------------------
    def set_channel(self, channel):
        self.channel = int(channel)
        self.write(":WAVEFORM:TRACE %d\n" % self.channel)
        return self.channel

    def set_format(self, fmt="WORD", byteorder="LSBFirst"):
        fmt = str(fmt).upper()
        if fmt not in ("WORD", "BYTE"):
            raise ScopeError("format must be WORD or BYTE, not %r" % (fmt,))
        order = "MSBFirst" if str(byteorder).upper().startswith("MSB") else "LSBFirst"
        self.write(":WAVEFORM:FORMAT %s\n" % fmt)
        self.write(":WAVEFORM:BYTEORDER %s\n" % order)
        self._fmt, self._byteorder = fmt, order
        return fmt, order

    def set_window(self, start=0, end=None):
        """Transfer only record points ``start``..``end`` (inclusive).

        The window does not make a read cheaper -- the ~320 ms latch dominates
        -- but it does shrink the payload, which matters at 125 kpoints.
        """
        self._start = int(start)
        self.write(":WAVEFORM:START %d\n" % self._start)
        # END 499999999 is the instrument's own "to the end of the record"
        # sentinel; writing None must restore it, not leave the last window.
        self._end = 499999999 if end is None else int(end)
        self.write(":WAVEFORM:END %d\n" % self._end)
        return self._start, (None if end is None else self._end)

    def window(self):
        """``(start, end)`` with ``end=None`` meaning "to the end of the record"."""
        end = None if self._end in (None, 499999999) else self._end
        return self._start, end

    # -- acquisition ------------------------------------------------------
    def arm(self, settle=None):
        """Start a free-running acquisition and let it run for ``settle`` s."""
        self.write(":START\n")
        time.sleep(self.settle_s if settle is None else settle)

    def stop(self):
        self.write(":STOP\n")

    def _read_block(self, expect=None):
        """One ``:WAVEFORM:SEND?``, sized so the whole block arrives in one RPC."""
        if expect is None:
            expect = 2 * 300000
        self.transport.write(":WAVEFORM:SEND?\n", io_timeout=8000)
        data, _reason = self.transport.read(expect, io_timeout=8000)
        return block_payload(data)

    def _decode(self, body):
        if not body:
            return Waveform([], channel=self.channel, vdiv=self._vdiv,
                            srate=self._srate, fmt=self._fmt,
                            codes_per_div=self.codes_per_div)
        return Waveform(
            self._codes(body),
            channel=self.channel,
            vdiv=self._vdiv,
            offset=self._offset,
            srate=self._srate,
            start_index=self._start,
            fmt=self._fmt,
            codes_per_div=self.codes_per_div,
        )

    def read(self, arm=True, settle=None, window=None, refresh=False):
        """Grab a *fresh* record.  ~450 ms, almost all of it inside the scope.

        ``arm=False`` skips ``:START`` and re-reads whatever is latched (that is
        what ``latest()`` does); the record will not have changed.
        ``refresh=True`` re-reads VDIV/offset/sample rate first, for when
        somebody has turned a knob since the last call.
        """
        t0 = time.time()
        if refresh:
            self.refresh_state()
        if window is not None:
            self.set_window(*window)
        if arm:
            self.arm(settle)
            self.stop()
        t1 = time.time()
        body = self._read_block()
        t2 = time.time()
        self.stats["reads"] += 1
        self.stats["rearm_s"] += t1 - t0
        self.stats["transfer_s"] += t2 - t1
        if not body:
            self.stats["empty"] += 1
        wave = self._decode(body)
        self._last = wave
        return wave

    def latest(self):
        """Re-read the latched record without re-arming (~3 ms)."""
        return self.read(arm=False)

    def read_multi(self, channels=(1, 2, 3, 4), arm=True, settle=None,
                   window=None, refresh=False, verify=False):
        """All ``channels`` from **one** acquisition, for the price of one latch.

        ``:WAVEFORM:TRACE`` only picks which trace ``:WAVEFORM:SEND?`` hands
        over, so after one STOP the extra traces cost ~3 ms each -- **but only
        if the record was allowed to complete before :STOP**.  With the default
        0.02 s settle a four-trace sweep costs 831 ms and only 1 sweep in 4 is
        actually time-aligned; at 0.15 s the same sweep costs 593 ms (only ~40 ms
        more than one trace) and 4 sweeps in 4 are aligned.  Hence the raised
        default settle here; pass ``settle`` explicitly to override.

        The sweep has to be a **tight burst**: the transfer buffer only stays
        readable for a short while, and whatever command is in flight when the
        next record is due pays for it (measured: 130 ms + 320 ms).  So the
        V/div lookups are hoisted out of the loop -- an interleaved query can be
        the one that lands there.

        ``verify=True`` re-reads the first trace straight after the sweep and
        bumps ``stats["misaligned"]`` if the bytes moved, which is the only way
        to be certain all traces came from one acquisition.

        Returns ``{channel: Waveform}``.
        """
        t0 = time.time()
        if refresh:
            self.refresh_state()
        if window is not None:
            self.set_window(*window)
        # Before the burst, never inside it -- see the docstring.
        vdivs = dict((int(ch), self.ch_vdiv(ch)) for ch in channels)
        if arm:
            if settle is None:
                settle = max(self.settle_s, MULTI_SETTLE_S)
            self.arm(settle)
            self.stop()
        t1 = time.time()
        out = {}
        for ch in channels:
            self.set_channel(ch)
            body = self._read_block()
            out[ch] = Waveform(
                self._codes(body),
                channel=ch,
                vdiv=vdivs[int(ch)],
                offset=self._offset,
                srate=self._srate,
                start_index=self._start,
                fmt=self._fmt,
                codes_per_div=self.codes_per_div,
            )
            self.stats["reads"] += 1
            if not body:
                self.stats["empty"] += 1
        if verify and len(channels) > 1:
            first = int(channels[0])
            self.set_channel(first)
            if self._codes(self._read_block()) != out[first].codes:
                self.stats["misaligned"] += 1
        self.set_channel(self.channel)
        self.stats["rearm_s"] += t1 - t0
        self.stats["transfer_s"] += time.time() - t1
        self._last = out.get(self.channel, self._last)
        return out

    def _codes(self, body):
        """Payload bytes -> code list, honouring the format settings."""
        if not body:
            return []
        if self._fmt == "BYTE":
            return [b - 128 for b in bytearray(body)]
        n = len(body) // 2
        endian = "<" if self._byteorder == "LSBFirst" else ">"
        return list(struct.unpack("%s%dh" % (endian, n), body[:2 * n]))

    def stream(self, count=None, interval=0.0, window=None):
        """Yield fresh records at the instrument's own pace (~2 Hz).

        ``count=None`` streams until the caller breaks out.  ``interval`` adds a
        deliberate pause; the floor is set by the ~320 ms latch, not by us.
        """
        made = 0
        while count is None or made < count:
            t0 = time.time()
            yield self.read(arm=True, window=window)
            made += 1
            rest = interval - (time.time() - t0)
            if rest > 0:
                time.sleep(rest)

    # -- convenience ------------------------------------------------------
    def measure(self, wave=None, arm=True):
        """One record, reduced to the numbers a control loop wants."""
        if wave is None:
            wave = self.read(arm=arm)
        return wave.summary()


# --------------------------------------------------------------------------- #
# Background streaming
# --------------------------------------------------------------------------- #

class Streamer(object):
    """Keep the newest record(s) available without blocking the caller.

    The instrument gives a fresh acquisition roughly every 0.45 s (measured; see
    the module docstring), and nothing in the SCPI set makes that faster.  The
    way to *use* such a source in a fast loop is to decouple it: a background
    thread runs START/STOP/SEND as fast as the scope allows and publishes the
    newest data, while the control loop calls :meth:`snapshot` and gets an
    answer in microseconds -- the age of that answer is reported, so the loop
    can decide for itself whether it is stale.

    ``channels=None`` streams one ``Waveform`` (the scope's ``channel``).
    ``channels=(1, 2, 3, 4)`` streams ``{channel: Waveform}`` -- all four
    traces come from the *same* acquisition, because :meth:`DlmScope.read_multi`
    switches ``:WAVEFORM:TRACE`` after one latch (~3 ms per extra channel).

    ::

        with DlmScope(host) as scope:
            with Streamer(scope, channels=(1, 2), window=(0, 9999)) as s:
                while running:
                    data, age = s.snapshot()   # Waveform or {ch: Waveform}
                    ...
    """

    def __init__(self, scope, window=None, interval=0.0, autostart=True,
                 channels=None):
        self.scope = scope
        self.window = window
        self.interval = float(interval)
        self.channels = channels
        self._thread = None
        self._stop = None
        self._lock = None
        self._wave = None
        self._stamp = 0.0
        self._count = 0
        self._errors = []
        self._spans = []
        self._t0 = 0.0
        if autostart:
            self.start()

    # -- lifecycle --------------------------------------------------------
    def start(self):
        import threading
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._run, name="dlm-streamer")
        self._thread.daemon = True
        self._thread.start()
        return self

    def stop(self):
        if self._stop is not None:
            self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    # -- producer ---------------------------------------------------------
    def _run(self):
        import threading  # noqa: F401  (kept local so import stays cheap)
        while not self._stop.is_set():
            t0 = time.time()
            try:
                if self.channels:
                    payload = self.scope.read_multi(channels=self.channels,
                                                    arm=True, window=self.window)
                else:
                    payload = self.scope.read(arm=True, window=self.window)
            except Exception as exc:                 # keep streaming, record it
                with self._lock:
                    self._errors.append(repr(exc))
                    self._errors = self._errors[-5:]
                if self._stop.wait(0.05):
                    break
                continue
            with self._lock:
                self._wave = payload
                self._stamp = time.time()
                self._count += 1
                npts = (sum(len(w) for w in payload.values())
                        if isinstance(payload, dict) else len(payload))
                self._spans.append(npts)
                self._spans = self._spans[-32:]
            rest = self.interval - (time.time() - t0)
            if rest > 0 and self._stop.wait(rest):
                break

    # -- consumer ---------------------------------------------------------
    def latest(self):
        """The newest ``Waveform``, or ``None`` before the first one lands."""
        with self._lock:
            return self._wave

    def snapshot(self):
        """``(waveform, age_seconds)`` -- what a control loop actually wants."""
        with self._lock:
            if self._wave is None:
                return None, float("inf")
            return self._wave, time.time() - self._stamp

    def stats(self):
        with self._lock:
            n = self._count
            age = (time.time() - self._stamp) if self._stamp else float("inf")
            spans = list(self._spans)
            errors = list(self._errors)
            elapsed = time.time() - self._t0 if self._t0 else 0.0
        return {"records": n, "age_s": age, "points": spans[-1] if spans else 0,
                "errors": errors,
                "rate_hz": (n / elapsed) if elapsed > 0 else 0.0}


# --------------------------------------------------------------------------- #
# Adapter for Nanomax.afm_control
# --------------------------------------------------------------------------- #

class ScopeSource(object):
    """Present the scope as a ``value()`` source for the AFM Z loop.

    ``stat`` picks what the scalar is: ``pkpk`` (default), ``rms``, ``mean``,
    ``min`` or ``max``.  ``unit`` is ``V`` (volts, via CODES_PER_DIV) or
    ``code`` (raw).  Each ``value()`` costs one fresh record, ~0.45 s -- so this
    source is a *slow* reference, not a 30 ms loop sensor.  Use it for
    calibration runs, or as the second opinion next to the digitizer.
    """

    REQUIRED = ("value",)
    OPTIONAL = ("prepare", "finish", "describe", "ready", "unit")

    def __init__(self, scope, stat="pkpk", unit="V"):
        self.scope = scope
        self.stat = str(stat).lower()
        self.unit = "V" if str(unit).upper().startswith("V") else "code"
        self._opened_here = False
        self.last = None
        if self.scope.transport is None:
            self.scope.open()
            self._opened_here = True

    # -- contract ---------------------------------------------------------
    def value(self):
        wave = self.scope.read()
        self.last = wave
        s = wave.summary()
        if not s.get("points"):
            return None
        key = self.stat + ("_v" if self.unit == "V" else "_codes")
        raw = s.get(key)
        if raw is None:
            return None
        # SNR proxy: how far the peak-to-peak sits above one code of noise.
        snr = 0.0
        if s.get("pkpk_codes"):
            snr = float(s["pkpk_codes"]) / 1.0
        return float(raw), snr

    def describe(self):
        return "DLM3034 ch%d %s [%s]" % (self.scope.channel, self.stat, self.unit)

    def ready(self):
        return self.scope.transport is not None

    def prepare(self):
        if self.scope.transport is None:
            self.scope.open()
            self._opened_here = True

    def finish(self):
        if self._opened_here:
            self.scope.close()
            self._opened_here = False


# --------------------------------------------------------------------------- #
# Smoke test: python -m Nanomax.dlm_scope [host]
# --------------------------------------------------------------------------- #

def _main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    host = argv[0] if argv else "192.168.1.100"
    print("connecting to %s" % host)
    with DlmScope(host) as scope:
        print("idn:", scope.idn())
        st = scope.state()
        for k in sorted(st):
            print("  %-14s %s" % (k, st[k]))
        t0 = time.time()
        wave = scope.read()
        dt = (time.time() - t0) * 1e3
        print("read: %d pts in %.1f ms  (%s)" % (len(wave), dt, wave.format))
        s = wave.summary()
        for k in ("pkpk_codes", "pkpk_v", "mean_v", "rms_v", "scale_v_per_code"):
            print("  %-16s %.6g" % (k, s[k]))
        t0 = time.time()
        again = scope.latest()
        print("latest: %d pts in %.1f ms (same record: %s)"
              % (len(again), (time.time() - t0) * 1e3, again.codes == wave.codes))
        print("errors:", scope.errors())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
