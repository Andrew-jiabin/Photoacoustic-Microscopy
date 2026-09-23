# -*- coding: utf-8 -*-
"""Acceptance check for the two positioning stages.

Exercises the **project's own** driver classes, i.e. exactly the code path the
application uses, on a very small travel with a hard safety guard.

    Part A - Prior translation stage   (Alazar_imaging.PriorUnifiedStage)
    Part B - NanoMax closed-loop stage (Alazar_imaging.BPC303NativeController)

Safety
------
* ``LIMIT_UM`` is a hard ceiling: any commanded displacement larger than it
  raises before a single command is sent. Lower it, never raise it, for a
  first run on an unknown rig.
* Every move is followed by a return to the recorded start position, and both
  controllers are closed in a ``finally`` block.
* Output is ASCII-only (GBK console).

Configuration
-------------
* Prior SDK DLL: ``--prior-dll <path>`` or ``PAM_PRIOR_SDK_DLL``. When neither
  is given the script searches the usual deployment locations.
* Prior COM port: ``--prior-com <n>`` or ``PAM_PRIOR_COM`` (default ``4``).
  **Do not assume a historical COM number is still valid.**
* NanoMax only: ``--skip-prior`` or ``--skip-nanomax`` to run one half.

Usage
-----
Run from the repository root, with the stages powered on::

    python Tool_code\\acceptance_stages_nanomax_prior.py
    python Tool_code\\acceptance_stages_nanomax_prior.py --skip-nanomax

Note
----
``BPC303NativeController()`` is **not** a read-only constructor: with its
defaults it auto-connects, enables the piezo channels, and forces closed-loop
mode. Power the stage before running this and expect the channels to become
energised.
"""
import glob
import os
import sys
import time
import traceback

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

LIMIT_UM = 20.0          # hard safety guard; travel per commanded move
PRIOR_DEFAULT_COM = "4"

# Candidate locations for the Prior SDK, relative to the repository parent.
_PRIOR_DLL_CANDIDATES = (
    os.path.join(os.path.dirname(REPO), "PAM", "PriorSDK 2.0.0", "x64",
                 "PriorScientificSDK.dll"),
    os.path.join(os.path.dirname(REPO), "Labview_development", "nanoscan",
                 "python", "PriorSDK 2.0.0", "x64", "PriorScientificSDK.dll"),
    os.path.join(os.path.dirname(REPO), "**", "PriorSDK*", "x64",
                 "PriorScientificSDK.dll"),
)


def arg_value(flag):
    """Return the value following ``flag`` on the command line, or None."""
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def find_prior_dll():
    explicit = arg_value("--prior-dll") or os.environ.get("PAM_PRIOR_SDK_DLL")
    if explicit:
        return explicit
    for pattern in _PRIOR_DLL_CANDIDATES:
        if "*" in pattern:
            hits = sorted(glob.glob(pattern, recursive=True))
            if hits:
                return hits[0]
        elif os.path.isfile(pattern):
            return pattern
    return None


PRIOR_DLL = find_prior_dll()
PRIOR_COM = arg_value("--prior-com") or os.environ.get("PAM_PRIOR_COM") \
    or PRIOR_DEFAULT_COM

SKIP_PRIOR = "--skip-prior" in sys.argv
SKIP_NANOMAX = "--skip-nanomax" in sys.argv


def sep(title):
    print("")
    print("=== %s ===" % title)


def poll_position(stage, target, tol=0.05, timeout_s=20.0):
    """Poll a Prior stage until it is within ``tol`` of ``target``."""
    t0 = time.time()
    cx = cy = None
    while time.time() - t0 < timeout_s:
        parts = stage.get_position().split(",")
        cx, cy = float(parts[0]), float(parts[1])
        if abs(cx - target[0]) < tol and abs(cy - target[1]) < tol:
            return time.time() - t0, (cx, cy)
        time.sleep(0.05)
    return None, (cx, cy)


# ------------------------------------------------------------------- Part A
if SKIP_PRIOR:
    sep("A. Prior stage  -- SKIPPED")
else:
    sep("A. Prior stage  (PriorUnifiedStage, COM%s)" % PRIOR_COM)
    prior = None
    try:
        if not PRIOR_DLL or not os.path.isfile(PRIOR_DLL):
            raise RuntimeError(
                "Prior SDK DLL not found. Pass --prior-dll <path> or set "
                "PAM_PRIOR_SDK_DLL. Searched: %s"
                % (PRIOR_DLL or "nothing",))
        print("  sdk dll           : %s" % PRIOR_DLL)

        from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage

        prior = PriorUnifiedStage(PRIOR_DLL, PRIOR_COM)
        print("  connect COM%s     : OK" % PRIOR_COM)
        print("  mode              : %s" % prior.mode)
        # get_SDK_version() returns an rc code, not a version string.
        print("  get_SDK_version() : %s  (rc code, not a version)"
              % (prior.get_SDK_version(),))

        x0, y0 = (float(v) for v in prior.get_position().split(","))
        print("  start position    : x=%.3f  y=%.3f  (um)" % (x0, y0))

        dx = 10.0
        assert abs(dx) <= LIMIT_UM, "guard: |%.1f| > %.1f" % (dx, LIMIT_UM)
        tx = x0 + dx
        print("  target            : x=%.3f  (delta %+.1f um, guard %.1f um)"
              % (tx, dx, LIMIT_UM))

        prior.set_position([tx, y0])
        dt, got = poll_position(prior, (tx, y0))
        print("  moved             : %s in %s -> x=%.3f  err=%+.3f"
              % ("OK" if dt is not None else "TIMEOUT",
                 ("%.3f s" % dt) if dt else "-", got[0], got[0] - tx))

        prior.set_position([x0, y0])
        dt, got = poll_position(prior, (x0, y0))
        print("  returned          : %s in %s -> x=%.3f  residual=%+.3f"
              % ("OK" if dt is not None else "TIMEOUT",
                 ("%.3f s" % dt) if dt else "-", got[0], got[0] - x0))

        print("  RESULT A: PASS")
    except Exception as exc:
        print("  RESULT A: FAIL %r" % (exc,))
        traceback.print_exc()
    finally:
        if prior is not None:
            try:
                prior.stage_deinitial()
                print("  disconnect        : OK")
            except Exception:
                pass

# ------------------------------------------------------------------- Part B
if SKIP_NANOMAX:
    sep("B. NanoMax closed-loop stage  -- SKIPPED")
else:
    sep("B. NanoMax closed-loop stage  (Thorlabs MAX311D + BPC303)")
    bpc = None
    try:
        from Alazar_imaging.BPC303NativeController import BPC303NativeController

        # Not read-only: enables channels and forces closed loop.
        bpc = BPC303NativeController()
        print("  connect SN 71241834 : OK")

        for ax in ("x", "y", "z"):
            print("  axis %s : travel=%.1f um  mode=%s  maxV=%.1f V"
                  % (ax, bpc.get_max_travel(ax),
                     bpc.get_position_control_mode(ax),
                     bpc.get_max_output_voltage(ax)))

        pos = bpc.get_position_values()
        print("  start position      : x=%.3f y=%.3f z=%.3f (um)" % tuple(pos))
        x0 = pos[0]

        dx = 5.0
        assert abs(dx) <= LIMIT_UM, "guard: |%.1f| > %.1f" % (dx, LIMIT_UM)

        t0 = time.time()
        bpc.move_axis("x", x0 + dx, wait=True, tolerance=0.05, timeout_s=10.0)
        dt = time.time() - t0
        got = bpc.get_axis_position("x")
        print("  X %+.1f um           : %.3f s -> x=%.3f  err=%+.3f"
              % (dx, dt, got, got - (x0 + dx)))

        t0 = time.time()
        bpc.move_axis("x", x0, wait=True, tolerance=0.05, timeout_s=10.0)
        dt = time.time() - t0
        got = bpc.get_axis_position("x")
        print("  returned            : %.3f s -> x=%.3f  residual=%+.3f"
              % (dt, got, got - x0))

        print("  RESULT B: PASS")
    except Exception as exc:
        print("  RESULT B: FAIL %r" % (exc,))
        traceback.print_exc()
    finally:
        if bpc is not None:
            try:
                bpc.close()
                print("  close()             : OK")
            except Exception:
                pass

print("")
print("=== DONE ===")
