# -*- coding: utf-8 -*-
r"""Offline checks for ``Nanomax.dlm_scope`` -- no instrument, no network.

The transport is replaced by a scripted fake, so everything below the socket
(waveform framing, decoding, statistics, the afm_control adapter) is exercised
on a laptop.  The live path is covered by ``Tool_code/dlm_scope_cli.py --bench``.

    python -m Tool_code.test_dlm_scope_offline
"""

from __future__ import absolute_import, division, print_function

import os
import struct
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Nanomax import dlm_scope as ds

FAILED = []
COUNT = [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, ("(" + str(detail) + ")") if detail else ""))
        FAILED.append(name)


def eq(name, got, want):
    check(name, got == want, "got %r want %r" % (got, want))


# --------------------------------------------------------------------------- #
# A scripted stand-in for the VXI-11 link
# --------------------------------------------------------------------------- #

class FakeTransport(object):
    """Answers SCPI queries from a dict and hands out scripted blocks."""

    def __init__(self, answers=None, blocks=None):
        self.answers = dict(answers or {})
        self.blocks = list(blocks or [])
        self.written = []
        self.blocks_served = 0

    def write(self, data, io_timeout=5000, flags=0):
        text = data.decode("ascii") if isinstance(data, bytes) else data
        self.written.append(text.strip())
        return len(text)

    def read(self, size, io_timeout=5000, term=0):
        last = self.written[-1] if self.written else ""
        if last != ":WAVEFORM:SEND?" and last.endswith("?"):
            if last not in self.answers:
                raise ds.ScopeError("fake has no answer for %r" % last)
            return self.answers[last].encode("ascii"), 1
        # :WAVEFORM:SEND?  -> next scripted block
        if self.blocks_served < len(self.blocks):
            block = self.blocks[self.blocks_served]
            self.blocks_served += 1
            return block, 1
        return b"#9000000000", 4

    def close(self):
        pass


def make_scope(blocks, answers=None, codes_per_div=8192.0):
    scope = ds.DlmScope("127.0.0.1", channel=1, codes_per_div=codes_per_div)
    base = {
        ":CHANnel1:VDIV?": ":CHAN1:VDIV 50.00E-03",
        ":CHANnel1:OFFSet?": ":CHAN1:OFFS 50.000000E-03",
        ":WAVEFORM:SRATE?": ":WAV:SRAT 6.250E+06",
    }
    base.update(answers or {})
    scope.transport = FakeTransport(base, blocks)
    scope.refresh_state()
    return scope


def word_block(codes, order="<"):
    body = struct.pack("%s%dh" % (order, len(codes)), *codes)
    return b"#9%09d" % len(body) + body


# --------------------------------------------------------------------------- #
def test_framing():
    print("[1] block framing")
    body = b"\x01\x02\x03\x04"
    eq("strips a definite-length header", ds.block_payload(b"#9000000004" + body), body)
    eq("empty block -> b''", ds.block_payload(b"#9000000000"), b"")
    eq("empty input -> b''", ds.block_payload(b""), b"")
    eq("passes a bare payload through", ds.block_payload(b"YOKOGAWA"), b"YOKOGAWA")
    try:
        ds.block_payload(b"#9000000010short")
        check("short block raises", False, "no exception")
    except ds.ScopeError:
        check("short block raises", True)
    try:
        ds.block_payload(b"#9")
        check("truncated header raises", False, "no exception")
    except ds.ScopeError:
        check("truncated header raises", True)


def test_xdr():
    print("[2] XDR encoding")
    eq("string length prefix", ds._xstr("inst0")[:4], struct.pack(">I", 5))
    eq("string padding to 4 bytes", len(ds._xstr("inst0")), 12)
    eq("opaque padding", len(ds._xopaque(b"*IDN?\n")), 12)
    eq("no padding when aligned", len(ds._xopaque(b"abcd")), 8)
    eq("pad helper", (ds._pad(0), ds._pad(1), ds._pad(4)), (0, 3, 0))


def test_decode():
    print("[3] decoding")
    codes = [0, 100, -100, 32767, -32768]
    scope = make_scope([word_block(codes)])
    wave = scope.read()
    eq("WORD little endian", wave.codes, codes)
    eq("point count", len(wave), 5)
    eq("channel tag", wave.channel, 1)
    eq("vdiv read from the instrument", wave.vdiv, 0.05)
    eq("sample rate read from the instrument", wave.srate, 6250000.0)
    eq("scale is vdiv/codes_per_div", wave.scale, 0.05 / 8192.0)

    scope = make_scope([word_block(codes, order=">")])
    scope.set_format("WORD", "MSBFirst")
    wave = scope.read()
    eq("WORD big endian", wave.codes, codes)

    scope = make_scope([b"#9000000004" + bytes(bytearray([0, 128, 255, 64]))])
    scope.set_format("BYTE", "LSBFirst")
    wave = scope.read()
    eq("BYTE is offset binary around 128", wave.codes, [-128, 0, 127, -64])

    scope = make_scope([b"#9000000000"])
    wave = scope.read()
    eq("empty block -> zero points", len(wave), 0)
    eq("empty block is counted", scope.stats["empty"], 1)


def test_statistics():
    print("[4] statistics")
    scope = make_scope([word_block([0, 8192, -8192, 16384])])
    s = scope.read().summary()
    eq("points", s["points"], 4)
    eq("pk-pk codes", s["pkpk_codes"], 16384 + 8192)
    eq("min codes", s["min_codes"], -8192)
    eq("max codes", s["max_codes"], 16384)
    eq("pk-pk volts", round(s["pkpk_v"], 9), round((16384 + 8192) * 0.05 / 8192.0, 9))
    eq("mean codes", s["mean_codes"], (0 + 8192 - 8192 + 16384) / 4.0)
    check("rms is positive", s["rms_codes"] > 0, s["rms_codes"])
    empty = make_scope([b"#9000000000"]).read().summary()
    eq("empty summary has no points", empty["points"], 0)

    w = ds.Waveform([0, 1000, 2000], srate=1000.0, vdiv=0.1, codes_per_div=1000.0)
    eq("time axis uses the sample rate", [round(x, 6) for x in w.times()], [0.0, 0.001, 0.002])
    eq("volts scale", [round(x, 6) for x in w.volts()], [0.0, 0.1, 0.2])
    eq("times fall back to index without a rate", ds.Waveform([0, 1], srate=0).times(), [0.0, 1.0])


def test_windowing():
    print("[5] window handling")
    scope = make_scope([word_block([1, 2]), word_block([3, 4])])
    eq("window start/end written", scope.set_window(0, 999), (0, 999))
    check("START sent", ":WAVEFORM:START 0" in scope.transport.written)
    check("END sent", ":WAVEFORM:END 999" in scope.transport.written)
    scope.set_window(0, None)
    eq("end None restores the sentinel", scope.window(), (0, None))
    check("END sentinel written", ":WAVEFORM:END 499999999" in scope.transport.written)
    eq("start_index lands on the waveform", scope.read().start_index, 0)


def test_arm_sequence():
    print("[6] the read sequence")
    scope = make_scope([word_block([1, 2, 3])])
    scope.read(settle=0.0)
    seq = [c for c in scope.transport.written if c in (":START", ":STOP", ":WAVEFORM:SEND?")]
    eq("START then STOP then SEND?", seq, [":START", ":STOP", ":WAVEFORM:SEND?"])
    scope.read(arm=False)
    eq("arm=False skips START/STOP", scope.transport.written[-1], ":WAVEFORM:SEND?")
    eq("two reads counted", scope.stats["reads"], 2)
    check("timing buckets filled", scope.stats["transfer_s"] >= 0.0)


def test_stream():
    print("[7] stream")
    scope = make_scope([word_block([1]), word_block([2]), word_block([3])])
    waves = list(scope.stream(count=3, interval=0.0))
    eq("stream yields the requested count", len(waves), 3)
    eq("records are distinct", [w.codes for w in waves], [[1], [2], [3]])
    eq("stream stops at the script end", list(scope.stream(count=2))[0].codes, [])


def test_source_adapter():
    print("[8] afm_control adapter")
    from Nanomax import afm_control

    scope = make_scope([word_block([0, 8192, -8192])])
    src = ds.ScopeSource(scope, stat="pkpk", unit="V")
    adopted = afm_control.adopt(src)
    check("adopt() accepts the scope source", adopted is src)
    value, snr = afm_control.read_value(src)
    eq("pk-pk in volts", round(value, 9), round(16384 * 0.05 / 8192.0, 9))
    check("snr is positive", snr > 0, snr)

    scope = make_scope([word_block([0, 8192, -8192])])
    src = ds.ScopeSource(scope, stat="pkpk", unit="code")
    eq("pk-pk in codes", afm_control.read_value(src)[0], 16384.0)

    scope = make_scope([b"#9000000000"])
    src = ds.ScopeSource(scope, stat="pkpk", unit="V")
    value, snr = afm_control.read_value(src)
    check("an empty record reads as nan", value != value and snr == 0.0, (value, snr))

    scope = make_scope([word_block([0, 100])])
    src = ds.ScopeSource(scope, stat="rms", unit="V")
    check("describe names model, channel and stat",
          src.describe().startswith("DLM3034 ch1 rms"), src.describe())
    eq("optional contract fields are present",
       sorted(set(ds.ScopeSource.OPTIONAL) - set(dir(src))), [])


def test_error_paths():
    print("[9] error paths")
    scope = make_scope([])
    try:
        scope.query(":NO:SUCH:QUERY?")
        check("unknown query raises from the fake", False)
    except ds.ScopeError:
        check("unknown query raises from the fake", True)
    eq("query_float falls back on a bad answer", scope.query_float(":NO:SUCH:QUERY?", -1.0), -1.0)
    err = ds.VxiError(15, "device_read")
    check("VxiError names the code", "io timeout" in str(err), str(err))
    eq("format rejects nonsense", isinstance(ds.ScopeError("x"), Exception), True)
    try:
        scope.set_format("LONG")
        check("set_format rejects unknown formats", False)
    except ds.ScopeError:
        check("set_format rejects unknown formats", True)


def test_cli_window_helper():
    """``--window`` is optional; the CLI must not unpack None into set_window."""
    print("[10] cli window helper")
    from Tool_code import dlm_scope_cli as cli

    class Args(object):
        def __init__(self, window):
            self.window = window

    scope = make_scope([])
    written_before = list(scope.transport.written)
    eq("no --window leaves the scope alone",
       cli._apply_window(scope, Args(None)), None)
    eq("no --window writes nothing",
       scope.transport.written, written_before)

    scope2 = make_scope([])
    eq("--window is forwarded", cli._apply_window(scope2, Args([7, 1234])), (7, 1234))
    check("--window reaches :WAVEFORM:END",
          any(":WAVEFORM:END 1234" in w for w in scope2.transport.written),
          scope2.transport.written)


def test_multi_settle():
    """A multi-trace sweep must wait long enough for one aligned acquisition."""
    print("[11] multi-channel settle")
    scope = make_scope([word_block([1, 2, 3]) for _ in range(5)])

    seen = []
    real_arm = scope.arm

    def spy_arm(settle=None):
        seen.append(settle)
        return real_arm(settle)

    scope.arm = spy_arm
    scope.read()
    eq("a single read keeps the short settle", seen[-1], None)

    out = scope.read_multi(channels=(1, 2))
    eq("read_multi raises the settle", seen[-1], ds.MULTI_SETTLE_S)
    eq("read_multi returns every trace", sorted(out), [1, 2])
    eq("each trace decoded", [len(out[1].codes), len(out[2].codes)], [3, 3])

    scope.read_multi(channels=(1, 2), settle=0.4)
    eq("an explicit settle wins", seen[-1], 0.4)


def main():
    test_framing()
    test_xdr()
    test_decode()
    test_statistics()
    test_windowing()
    test_arm_sequence()
    test_stream()
    test_source_adapter()
    test_error_paths()
    test_cli_window_helper()
    test_multi_settle()
    print()
    if FAILED:
        print("SELFTEST FAILED: %d of %d checks failed" % (len(FAILED), COUNT[0]))
        for name in FAILED:
            print("  - %s" % name)
        return 1
    print("all %d checks passed" % COUNT[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
