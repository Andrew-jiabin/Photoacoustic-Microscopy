# -*- coding: utf-8 -*-
r"""Command line front end for ``Nanomax.dlm_scope``.

All output is plain ASCII: the lab PCs are driven over ssh from a cmd.exe
console whose code page is GBK, and a stray non-ASCII byte there turns into
mojibake or a UnicodeEncodeError.

    python -m Tool_code.dlm_scope_cli --check
    python -m Tool_code.dlm_scope_cli --bench 10
    python -m Tool_code.dlm_scope_cli --stream 5
    python -m Tool_code.dlm_scope_cli --save rec.csv --window 0 9999
    python -m Tool_code.dlm_scope_cli --source pkpk

Default host is 192.168.1.100 (what the DLM3034 reports on the lab router).
"""

from __future__ import absolute_import, division, print_function

import argparse
import os
import statistics
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from Nanomax.dlm_scope import (DlmScope, ScopeSource, Streamer,
                                   VxiError, RpcError)
else:
    from Nanomax.dlm_scope import (DlmScope, ScopeSource, Streamer,
                                   VxiError, RpcError)

DEFAULT_HOST = "192.168.1.100"


def _fmt_state(st):
    order = ("idn", "channel", "vdiv", "offset", "srate", "record_length", "tdiv",
             "format", "byteorder", "trace", "start", "end", "trigger_mode",
             "acquire_mode", "bits")
    lines = []
    for k in order:
        if k in st:
            lines.append("  %-14s %s" % (k, st[k]))
    return "\n".join(lines)


def _fmt_summary(s):
    keys = ("points", "pkpk_codes", "min_codes", "max_codes", "mean_codes",
            "pkpk_v", "mean_v", "rms_v", "vdiv", "srate", "scale_v_per_code")
    return "\n".join("  %-16s %.6g" % (k, s[k]) for k in keys if k in s)


def do_check(scope, args):
    print("host %s" % args.host)
    st = scope.state()
    print("state:")
    print(_fmt_state(st))
    t0 = time.time()
    wave = scope.read(window=_window(args))
    dt = (time.time() - t0) * 1e3
    print("read: %d points in %.1f ms (%s)" % (len(wave), dt, wave.format))
    print("summary:")
    print(_fmt_summary(wave.summary()))
    t0 = time.time()
    again = scope.latest()
    print("latest: %d points in %.1f ms, same record: %s"
          % (len(again), (time.time() - t0) * 1e3, again.codes == wave.codes))
    errs = scope.errors()
    print("error queue: %s" % (errs if errs else "empty"))


def do_bench(scope, args):
    n = max(1, args.bench)
    _apply_window(scope, args)
    scope.read()                      # prime the latch
    rows, spans, sigs = [], [], []
    print("bench: %d fresh records, window %s" % (n, _window(args)))
    for i in range(n):
        t0 = time.time()
        wave = scope.read()
        dt = (time.time() - t0) * 1e3
        rows.append(dt)
        s = wave.summary()
        spans.append(s.get("pkpk_codes", 0))
        sigs.append(hash(tuple(wave.codes[:64])))
        print("  %2d  %7.1f ms  %6d pts  pkpk %8.1f codes"
              % (i, dt, len(wave), s.get("pkpk_codes", 0)))
    print()
    print("latency  min %.1f  median %.1f  max %.1f ms"
          % (min(rows), statistics.median(rows), max(rows)))
    print("rate     %.2f Hz (ceiling of the instrument, see the report)"
          % (1000.0 / statistics.median(rows)))
    print("distinct records %d / %d" % (len(set(sigs)), len(sigs)))
    print("pk-pk range %.1f .. %.1f codes" % (min(spans), max(spans)))


def do_stream(scope, args):
    n = args.stream
    print("stream: %d records, window %s (Ctrl-C to stop)" % (n, _window(args)))
    print("  #   t_rel    points      pkpk_codes     pkpk_v")
    t_start = time.time()
    made = 0
    try:
        for wave in scope.stream(count=n, window=_window(args)):
            s = wave.summary()
            print("  %2d  %6.2f  %7d  %12.1f  %10.6f"
                  % (made, time.time() - t_start, len(wave),
                     s.get("pkpk_codes", 0), s.get("pkpk_v", 0.0)))
            made += 1
    except KeyboardInterrupt:
        print("interrupted after %d records" % made)
    if made:
        print("average %.2f Hz" % (made / (time.time() - t_start)))


def do_save(scope, args):
    _apply_window(scope, args)
    t0 = time.time()
    wave = scope.read()
    dt = (time.time() - t0) * 1e3
    t, v = wave.to_numpy()
    path = args.save
    with open(path, "w") as fh:
        fh.write("# host=%s idn=%s channel=%d\n" % (args.host, scope.idn(), wave.channel))
        fh.write("# vdiv=%g offset=%g srate=%g codes_per_div=%g\n"
                 % (wave.vdiv, wave.offset, wave.srate, wave.codes_per_div))
        fh.write("# read_ms=%.1f points=%d\n" % (dt, len(wave)))
        fh.write("time_s,volt,code\n")
        for i in range(len(wave)):
            fh.write("%.9g,%.9g,%d\n" % (t[i], v[i], wave.codes[i]))
    print("wrote %s: %d points in %.1f ms" % (path, len(wave), dt))
    print(_fmt_summary(wave.summary()))


def do_source(scope, args):
    stat = args.source
    for unit in ("V", "code"):
        src = ScopeSource(scope, stat=stat, unit=unit)
        t0 = time.time()
        val = src.value()
        print("  %-28s -> %r  (%.0f ms)" % (src.describe(), val, (time.time() - t0) * 1e3))


def do_follow(scope, args):
    """Sustained streaming through a background thread.

    The grabber runs at the instrument's own pace (~2 Hz); the loop below reads
    the newest record in microseconds, so a fast control loop can sit next to it
    and simply skip stale samples.
    """
    secs = float(args.follow)
    print("follow: %.1f s, window %s" % (secs, _window(args)))
    with Streamer(scope, window=_window(args)) as streamer:
        t0 = time.time()
        next_report = 0.0
        lat = []
        while True:
            now = time.time() - t0
            if now >= secs:
                break
            a = time.time()
            wave, age = streamer.snapshot()
            lat.append((time.time() - a) * 1e6)
            if wave is not None and now >= next_report:
                s = wave.summary()
                print("  t=%6.2f  age %6.3f s  %6d pts  pkpk %8.1f codes"
                      % (now, age, len(wave), s.get("pkpk_codes", 0.0)))
                next_report = now + max(0.5, secs / 8.0)
            time.sleep(0.01)
        st = streamer.stats()
        print()
        print("records   %d in %.1f s -> %.2f Hz" % (st["records"], secs, st["rate_hz"]))
        print("snapshot  median %.0f us  max %.0f us (the loop never waits on the scope)"
              % (statistics.median(lat), max(lat)))
        print("age       last sample %.3f s old" % st["age_s"])
        if st["errors"]:
            print("errors    %s" % st["errors"])


def _window(args):
    if args.window is None:
        return None
    start, end = args.window
    return (start, end)


def _apply_window(scope, args):
    """Push --window onto the scope, if it was given (None means "leave it")."""
    win = _window(args)
    if win is not None:
        scope.set_window(*win)
    return win


def build_parser():
    p = argparse.ArgumentParser(
        description="Read waveforms from a Yokogawa DLM3000 over VXI-11.")
    p.add_argument("--host", default=DEFAULT_HOST, help="instrument IP (default %s)" % DEFAULT_HOST)
    p.add_argument("--channel", type=int, default=1, help="trace to read (1..4, default 1)")
    p.add_argument("--window", type=int, nargs=2, metavar=("START", "END"),
                   help="transfer only record points START..END")
    p.add_argument("--check", action="store_true", help="identity, state and one record")
    p.add_argument("--bench", type=int, metavar="N", help="time N fresh records")
    p.add_argument("--stream", type=int, metavar="N", help="print N fresh records")
    p.add_argument("--save", metavar="CSV", help="save one record as time,volt,code")
    p.add_argument("--follow", type=float, metavar="SECONDS",
                   help="stream continuously for SECONDS through a background thread")
    p.add_argument("--source", metavar="STAT",
                   help="exercise the afm_control adapter: pkpk/rms/mean/min/max")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not any((args.check, args.bench, args.stream, args.save, args.source,
                args.follow is not None)):
        args.check = True
    try:
        with DlmScope(args.host, channel=args.channel) as scope:
            if args.check:
                do_check(scope, args)
            if args.bench:
                do_bench(scope, args)
            if args.stream:
                do_stream(scope, args)
            if args.save:
                do_save(scope, args)
            if args.follow is not None:
                do_follow(scope, args)
            if args.source:
                do_source(scope, args)
    except (VxiError, RpcError, OSError) as exc:
        print("FAILED: %s: %s" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
