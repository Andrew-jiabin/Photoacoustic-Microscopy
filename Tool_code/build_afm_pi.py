# -*- coding: utf-8 -*-
"""Build Nanomax/afm_pi.cpp into Nanomax/afm_pi.dll.

No Visual Studio installation is required.  The script tries, in order:

1. ``zig c++``          -- Zig on PATH, bundles its own libc/libc++
2. ``python -m ziglang``-- the ``ziglang`` PyPI wheel (same compiler, no install)
3. ``cl``               -- MSVC, only if it is already on PATH
4. ``g++``              -- MinGW-w64

The ``ziglang`` wheel is the route this repository was built with, because the
imaging PC has neither MSVC nor MinGW::

    pip download ziglang --no-deps -d D:\\LJB\\_stage\\zig
    pip install --no-deps --target D:\\LJB\\_stage\\zigpkg D:\\LJB\\_stage\\zig\\ziglang-*.whl
    set PYTHONPATH=D:\\LJB\\_stage\\zigpkg
    python Tool_code\\build_afm_pi.py

The build is reproducible and the output is a self-contained DLL: the C++ side
uses only ``<cmath>``, ``<cstring>`` and ``<new>``, and the Windows build links
the runtime statically so the target machine needs no redistributable.

Usage::

    python Tool_code/build_afm_pi.py            # build and verify
    python Tool_code/build_afm_pi.py --check    # verify an existing DLL only
    python Tool_code/build_afm_pi.py --compiler zig
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SRC = os.path.join(REPO_ROOT, "Nanomax", "afm_pi.cpp")
OUT = os.path.join(REPO_ROOT, "Nanomax",
                   "afm_pi.dll" if os.name == "nt" else "libafm_pi.so")


def _run(cmd, cwd=None, timeout=300):
    proc = subprocess.run(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _ziglang_available():
    try:
        import ziglang  # noqa: F401
        return True
    except Exception:
        return False


def candidates():
    """Yield ``(name, argv_prefix)`` for each compiler we can try."""
    zig = shutil.which("zig")
    if zig:
        yield "zig", [zig, "c++"]
    if _ziglang_available():
        yield "ziglang", [sys.executable, "-m", "ziglang", "c++"]
    cl = shutil.which("cl")
    if cl:
        yield "msvc", [cl]
    for name in ("g++", "clang++"):
        found = shutil.which(name)
        if found:
            yield name, [found]


def _command(name, prefix, out_path):
    if name in ("zig", "ziglang"):
        # windows-gnu keeps the DLL free of any MSVC runtime dependency.
        cmd = prefix + ["-target", "x86_64-windows-gnu", "-O2", "-shared",
                        "-o", out_path, SRC]
        return cmd
    if name == "msvc":
        return prefix + ["/nologo", "/O2", "/EHsc", "/LD", "/MT",
                         "/Fe:" + out_path, SRC]
    return prefix + ["-O2", "-shared", "-static", "-o", out_path, SRC]


def verify(dll_path=OUT):
    """Load the DLL and compare it with the Python reference."""
    sys.path.insert(0, REPO_ROOT)
    from Nanomax import afm_pi

    rc, z_cpp, steps_cpp = afm_pi.dll_selftest(dll_path=dll_path)
    z_py, steps_py = afm_pi.python_selftest()
    ok = (rc == 0
          and abs(z_cpp - z_py) < 1e-12
          and steps_cpp == steps_py)
    print("  dll selftest   rc=%d  z=%.12f  steps=%d" % (rc, z_cpp, steps_cpp))
    print("  py  reference         z=%.12f  steps=%d" % (z_py, steps_py))
    print("  agreement      %s" % ("EXACT" if ok else "MISMATCH"))
    return ok


def build(only=None, out_path=OUT):
    if not os.path.exists(SRC):
        print("source not found: %s" % SRC)
        return 1

    print("building %s" % os.path.basename(out_path))
    print("  source %s" % SRC)

    tried = []
    for name, prefix in candidates():
        if only and name != only:
            continue
        cmd = _command(name, prefix, out_path)
        print("  trying %s: %s" % (name, " ".join(cmd[:3]) + " ..."))
        try:
            rc, output = _run(cmd, cwd=REPO_ROOT)
        except Exception as exc:  # noqa: BLE001
            tried.append((name, "spawn failed: %s" % exc))
            continue
        if rc == 0 and os.path.exists(out_path):
            size = os.path.getsize(out_path)
            print("  built with %s -> %s (%d bytes)" % (name, out_path, size))
            print("")
            if verify(out_path):
                print("")
                print("BUILD OK")
                return 0
            print("")
            print("BUILD FAILED: the DLL was produced but does not agree with the reference")
            return 1
        tail = "\n".join(output.strip().splitlines()[-6:])
        tried.append((name, "exit %d: %s" % (rc, tail)))

    print("  no usable compiler")
    for name, why in tried:
        print("    %-8s %s" % (name, why))
    if not tried:
        print("    none found on PATH and ziglang is not importable")
        print("    install one of:")
        print("      pip download ziglang --no-deps -d D:\\LJB\\_stage\\zig")
        print("      pip install --no-deps --target D:\\LJB\\_stage\\zigpkg D:\\LJB\\_stage\\zig\\ziglang-*.whl")
        print("      set PYTHONPATH=D:\\LJB\\_stage\\zigpkg")
    print("")
    print("BUILD FAILED")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the AFM PI controller DLL")
    parser.add_argument("--compiler", default=None,
                        choices=["zig", "ziglang", "msvc", "g++", "clang++"],
                        help="force a specific compiler")
    parser.add_argument("--check", action="store_true",
                        help="only verify an existing DLL")
    args = parser.parse_args(argv)

    if args.check:
        if not os.path.exists(OUT):
            print("no DLL at %s" % OUT)
            return 1
        return 0 if verify(OUT) else 1
    return build(only=args.compiler)


if __name__ == "__main__":
    raise SystemExit(main())
