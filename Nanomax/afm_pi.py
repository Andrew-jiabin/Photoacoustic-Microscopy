# -*- coding: utf-8 -*-
"""Digital PI controller for the AFM-style Z loop: C++ engine, Python fallback.

Two implementations of one algorithm
------------------------------------
``Nanomax/afm_pi.cpp`` is the real controller.  It is compiled to
``Nanomax/afm_pi.dll`` and loaded here through ``ctypes``; the loop then runs
entirely in native code with a stable C ABI, which is what makes the timing
deterministic and lets the same object be called from another language later.

``_PythonPi`` is a line-for-line mirror of the same arithmetic, used when the
DLL is missing (fresh clone, unsupported platform) so the scan never depends on
a binary being present.  ``--compare`` proves the two agree numerically; it is
run automatically by ``--selftest``.

The C ABI (kept stable on purpose)
----------------------------------
::

    int   afm_pi_default_config(afm_pi_config* cfg);
    void* afm_pi_create(const afm_pi_config* cfg);
    int   afm_pi_reset(void* h, double z0, double measurement0);
    int   afm_pi_step(void* h, double setpoint, double measurement,
                      double dt_s, afm_pi_output* out);
    int   afm_pi_get_state(void* h, afm_pi_output* out);
    int   afm_pi_set_gains(void* h, double kp, double ki);
    void  afm_pi_destroy(void* h);
    const char* afm_pi_version(void);
    int   afm_pi_selftest(double plant_gain, double* final_z, int* steps);

Why incremental PI
------------------
The stage takes an absolute Z, but the decision the loop has to make is "how
far to move next"::

    e[k]   = setpoint - y[k]
    dz[k]  = sign * ( Kp*(e[k]-e[k-1]) + Ki*dt*e[k] )
    z[k+1] = z[k] + clamp(dz[k], -dz_limit, +dz_limit)

There is no separate integral accumulator to wind up -- the integrator is the
accumulated position, which is already bounded by the Z window and the per-point
travel budget.  A setpoint change mid-scan is bumpless, and seeding ``e[-1]``
from the first measurement removes the derivative kick on step one.

Nothing assumes the measurement grows with Z.  ``sign`` says which way Z must
move for a positive error, so a signal that *falls* as the probe approaches
works just as well.  If the measurement does not respond to Z at all, one of two
progress guards fires -- ``diverged`` when the error keeps growing, ``stalled``
when it simply stops improving -- instead of grinding through the travel budget.

Units
-----
``kp`` is um per measurement unit, ``ki`` is um per (measurement unit * second).
Both are in the same unit the caller feeds to ``step()``, so a setpoint in volts
gives gains in um/V and um/(V*s), and a setpoint in board units gives um/au.
"""

from __future__ import annotations

import ctypes
import math
import os
import sys
from dataclasses import dataclass, asdict
from typing import Optional

# --------------------------------------------------------------------------- #
# Status codes (must match afm_pi.cpp)
# --------------------------------------------------------------------------- #

STATUS_OK = 0
STATUS_CONVERGED = 1
STATUS_SATURATED = 2
STATUS_Z_LIMIT = 3
STATUS_EXHAUSTED = 4
STATUS_DIVERGED = 5
STATUS_STALLED = 6
STATUS_BADARG = -1

_STATUS_NAMES = {
    STATUS_OK: "ok",
    STATUS_CONVERGED: "converged",
    STATUS_SATURATED: "saturated",
    STATUS_Z_LIMIT: "z_limit",
    STATUS_EXHAUSTED: "exhausted",
    STATUS_DIVERGED: "diverged",
    STATUS_STALLED: "stalled",
    STATUS_BADARG: "bad_argument",
}

#: Consecutive non-improving steps before the loop calls itself stalled.
#: Must match kStallSteps in afm_pi.cpp.
STALL_STEPS = 8
#: Consecutive growing-error steps before the loop calls itself diverged.
GROW_STEPS = 8

#: Reasons a scan point should be treated as failed rather than measured.
FATAL_STATUSES = (STATUS_EXHAUSTED, STATUS_DIVERGED, STATUS_STALLED, STATUS_BADARG)


def status_name(status):
    return _STATUS_NAMES.get(int(status), "unknown(%d)" % int(status))


# --------------------------------------------------------------------------- #
# Configuration / output
# --------------------------------------------------------------------------- #

@dataclass
class PiConfig:
    """Tunables for one control loop.  Units: um and seconds."""

    #: um per measurement unit.
    kp: float = 0.010
    #: um per (measurement unit * second).
    ki: float = 0.200
    #: Nominal period; only used when ``step()`` is not given a measured dt.
    dt_s: float = 0.010
    #: Cap on one step's travel.  This is the main speed/safety trade-off.
    dz_limit_um: float = 0.300
    #: |error| at or below this counts as on target.
    deadband: float = 0.005
    #: +1 if advancing Z raises the measurement, -1 if it lowers it.
    sign: float = 1.0
    #: Extra Kp multiplier while |error| > ``boost_error`` (1.0 = off).
    kp_boost: float = 1.0
    #: Error magnitude above which the boost applies (0 = disabled).
    boost_error: float = 0.0
    #: Feed-forward gain on setpoint changes (0 = disabled).
    ff_gain: float = 0.0
    #: Z window the controller refuses to leave.
    z_min_um: float = -1e308
    z_max_um: float = 1e308
    #: Consecutive in-deadband steps needed to declare lock.
    hold_steps: int = 2
    #: Hard cap on iterations for one point.
    max_steps: int = 200
    #: Consecutive non-improving steps before the loop declares itself stalled.
    #: The blind travel this allows is ``stall_steps * dz_limit_um``, so raise it
    #: when the reading can be flat for a while -- a saturated probe has to be
    #: able to back off far enough to get out of saturation.
    stall_steps: int = 8

    def validated(self):
        """Return a copy with the same normalisation the C++ side applies."""
        cfg = PiConfig(**asdict(self))
        if cfg.kp < 0 or cfg.ki < 0:
            raise ValueError("kp and ki must be non-negative")
        if not cfg.dz_limit_um > 0:
            raise ValueError("dz_limit_um must be positive")
        if cfg.sign == 0:
            raise ValueError("sign must be +1 or -1")
        if cfg.dt_s <= 0:
            cfg.dt_s = 1e-3
        if cfg.deadband < 0:
            cfg.deadband = 0.0
        if cfg.hold_steps < 1:
            cfg.hold_steps = 1
        if cfg.max_steps < 1:
            cfg.max_steps = 1
        if cfg.stall_steps < 1:
            cfg.stall_steps = STALL_STEPS
        if cfg.kp_boost < 1.0:
            cfg.kp_boost = 1.0
        if cfg.boost_error < 0:
            cfg.boost_error = 0.0
        if cfg.z_min_um > cfg.z_max_um:
            raise ValueError("z_min_um must not exceed z_max_um")
        return cfg


@dataclass
class PiOutput:
    """One step's result."""

    z_um: float
    dz_um: float
    error: float
    p_term: float
    i_term: float
    dt_s: float
    steps: int
    converged: bool
    saturated: bool
    limit_hit: bool
    exhausted: bool
    diverged: bool
    stalled: bool
    status: int

    @property
    def status_name(self):
        return status_name(self.status)

    @property
    def on_target(self):
        return bool(self.converged)

    @property
    def failed(self):
        return self.status in FATAL_STATUSES


# --------------------------------------------------------------------------- #
# ctypes mirror of the C structs
# --------------------------------------------------------------------------- #

class _CPiConfig(ctypes.Structure):
    _fields_ = [
        ("kp", ctypes.c_double),
        ("ki", ctypes.c_double),
        ("dt_s", ctypes.c_double),
        ("dz_limit_um", ctypes.c_double),
        ("deadband", ctypes.c_double),
        ("sign", ctypes.c_double),
        ("kp_boost", ctypes.c_double),
        ("boost_error", ctypes.c_double),
        ("ff_gain", ctypes.c_double),
        ("z_min_um", ctypes.c_double),
        ("z_max_um", ctypes.c_double),
        ("hold_steps", ctypes.c_int),
        ("max_steps", ctypes.c_int),
        ("stall_steps", ctypes.c_int),
    ]


class _CPiOutput(ctypes.Structure):
    _fields_ = [
        ("z_um", ctypes.c_double),
        ("dz_um", ctypes.c_double),
        ("error", ctypes.c_double),
        ("p_term", ctypes.c_double),
        ("i_term", ctypes.c_double),
        ("dt_s", ctypes.c_double),
        ("steps", ctypes.c_int),
        ("converged", ctypes.c_int),
        ("saturated", ctypes.c_int),
        ("limit_hit", ctypes.c_int),
        ("exhausted", ctypes.c_int),
        ("diverged", ctypes.c_int),
        ("stalled", ctypes.c_int),
        ("status", ctypes.c_int),
    ]


# --------------------------------------------------------------------------- #
# Backend: C++
# --------------------------------------------------------------------------- #

DLL_NAME = "afm_pi.dll" if os.name == "nt" else "libafm_pi.so"


def dll_candidates():
    """Where to look for the compiled controller, most specific first."""
    paths = []
    override = os.environ.get("PAM_AFM_PI_DLL")
    if override:
        paths.append(override)
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, DLL_NAME))
    paths.append(os.path.join(os.path.dirname(here), DLL_NAME))
    return paths


class _CppPi:
    """Native controller loaded from ``afm_pi.dll``."""

    backend = "c++"

    def __init__(self, config, dll_path=None):
        self.config = config.validated()
        self._lib = self._load(dll_path)
        self._bind()

        c_cfg = _CPiConfig(
            kp=self.config.kp,
            ki=self.config.ki,
            dt_s=self.config.dt_s,
            dz_limit_um=self.config.dz_limit_um,
            deadband=self.config.deadband,
            sign=self.config.sign,
            kp_boost=self.config.kp_boost,
            boost_error=self.config.boost_error,
            ff_gain=self.config.ff_gain,
            z_min_um=self.config.z_min_um,
            z_max_um=self.config.z_max_um,
            hold_steps=int(self.config.hold_steps),
            max_steps=int(self.config.max_steps),
            stall_steps=int(self.config.stall_steps),
        )
        handle = self._lib.afm_pi_create(ctypes.byref(c_cfg))
        if not handle:
            raise ValueError("afm_pi_create rejected the configuration")
        self._handle = handle
        self._out = _CPiOutput()
        self.reset(0.0, 0.0)

    @staticmethod
    def _load(dll_path):
        errors = []
        for path in ([dll_path] if dll_path else dll_candidates()):
            if not path or not os.path.exists(path):
                continue
            try:
                return ctypes.CDLL(path)
            except OSError as exc:  # wrong architecture, missing CRT, ...
                errors.append("%s: %s" % (path, exc))
        raise OSError(
            "afm_pi controller library not found or not loadable%s"
            % ((": " + "; ".join(errors)) if errors else "")
        )

    def _bind(self):
        lib = self._lib
        lib.afm_pi_create.argtypes = [ctypes.POINTER(_CPiConfig)]
        lib.afm_pi_create.restype = ctypes.c_void_p
        lib.afm_pi_reset.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
        lib.afm_pi_reset.restype = ctypes.c_int
        lib.afm_pi_step.argtypes = [
            ctypes.c_void_p, ctypes.c_double, ctypes.c_double,
            ctypes.c_double, ctypes.POINTER(_CPiOutput),
        ]
        lib.afm_pi_step.restype = ctypes.c_int
        lib.afm_pi_get_state.argtypes = [ctypes.c_void_p, ctypes.POINTER(_CPiOutput)]
        lib.afm_pi_get_state.restype = ctypes.c_int
        lib.afm_pi_set_gains.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_double]
        lib.afm_pi_set_gains.restype = ctypes.c_int
        # Optional: only in 1.3 and later.  An older DLL simply keeps its fixed
        # step cap and the speed limiter degrades to a per-point constant.
        self._has_set_dz_limit = hasattr(lib, "afm_pi_set_dz_limit")
        if self._has_set_dz_limit:
            lib.afm_pi_set_dz_limit.argtypes = [ctypes.c_void_p, ctypes.c_double]
            lib.afm_pi_set_dz_limit.restype = ctypes.c_int
        lib.afm_pi_destroy.argtypes = [ctypes.c_void_p]
        lib.afm_pi_destroy.restype = None
        lib.afm_pi_version.argtypes = []
        lib.afm_pi_version.restype = ctypes.c_char_p
        lib.afm_pi_selftest.argtypes = [
            ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int)
        ]
        lib.afm_pi_selftest.restype = ctypes.c_int

    @property
    def version(self):
        return self._lib.afm_pi_version().decode("ascii", "replace")

    def reset(self, z0, measurement0):
        rc = self._lib.afm_pi_reset(self._handle, float(z0), float(measurement0))
        if rc != STATUS_OK:
            raise ValueError("afm_pi_reset failed: %s" % status_name(rc))
        return self

    def step(self, setpoint, measurement, dt_s=0.0):
        rc = self._lib.afm_pi_step(
            self._handle, float(setpoint), float(measurement), float(dt_s),
            ctypes.byref(self._out),
        )
        if rc == STATUS_BADARG:
            raise ValueError("afm_pi_step rejected the arguments")
        return self._to_output(self._out)

    def state(self):
        self._lib.afm_pi_get_state(self._handle, ctypes.byref(self._out))
        return self._to_output(self._out)

    def set_gains(self, kp, ki):
        rc = self._lib.afm_pi_set_gains(self._handle, float(kp), float(ki))
        if rc != STATUS_OK:
            raise ValueError("afm_pi_set_gains rejected the gains")
        self.config.kp = float(kp)
        self.config.ki = float(ki)
        return self

    def set_dz_limit(self, dz_limit_um):
        """Change the per-step travel cap in place (bumpless).

        Used by the scanner to hold a *speed*: it passes
        ``speed_um_s * measured_dt`` every step, so the closing rate stays what
        was asked for even when a step takes much longer than nominal.
        """
        dz_limit_um = float(dz_limit_um)
        if not dz_limit_um > 0.0:
            raise ValueError("dz_limit_um must be positive")
        if self._has_set_dz_limit:
            rc = self._lib.afm_pi_set_dz_limit(self._handle, dz_limit_um)
            if rc != STATUS_OK:
                raise ValueError("afm_pi_set_dz_limit rejected the value")
        self.config.dz_limit_um = dz_limit_um
        return self

    def close(self):
        if getattr(self, "_handle", None):
            self._lib.afm_pi_destroy(self._handle)
            self._handle = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _to_output(c):
        return PiOutput(
            z_um=c.z_um, dz_um=c.dz_um, error=c.error,
            p_term=c.p_term, i_term=c.i_term, dt_s=c.dt_s,
            steps=int(c.steps),
            converged=bool(c.converged), saturated=bool(c.saturated),
            limit_hit=bool(c.limit_hit), exhausted=bool(c.exhausted),
            diverged=bool(c.diverged), stalled=bool(c.stalled),
            status=int(c.status),
        )


# --------------------------------------------------------------------------- #
# Backend: pure Python reference (same arithmetic, same order of operations)
# --------------------------------------------------------------------------- #

class _PythonPi:
    """Reference implementation.  Bit-comparable to the C++ engine."""

    backend = "python"
    version = "afm_pi 1.1 (incremental PI, Python reference)"

    def __init__(self, config, dll_path=None):
        self.config = config.validated()
        self.reset(0.0, 0.0)

    def reset(self, z0, measurement0):
        self.z = float(z0)
        self.steps = 0
        self.hold = 0
        self.growing = 0
        self.stall = 0
        self.have_prev = False
        self.e_prev = 0.0
        self.sp_prev = 0.0
        self.e_abs_start = 0.0
        self.e_best = float("inf")
        self._last = PiOutput(
            z_um=self.z, dz_um=0.0, error=0.0, p_term=0.0, i_term=0.0,
            dt_s=self.config.dt_s, steps=0, converged=False, saturated=False,
            limit_hit=False, exhausted=False, diverged=False, stalled=False,
            status=STATUS_OK,
        )
        return self

    def step(self, setpoint, measurement, dt_s=0.0):
        cfg = self.config
        setpoint = float(setpoint)
        measurement = float(measurement)
        if not math.isfinite(setpoint) or not math.isfinite(measurement):
            raise ValueError("setpoint and measurement must be finite")

        dt = float(dt_s) if (math.isfinite(dt_s) and dt_s > 0.0) else cfg.dt_s
        dt_cap = cfg.dt_s * 10.0
        if dt > dt_cap:
            dt = dt_cap

        e = setpoint - measurement

        if not self.have_prev:
            self.e_prev = e
            self.sp_prev = setpoint
            self.e_abs_start = abs(e)
            self.e_best = abs(e)
            self.have_prev = True

        kp = cfg.kp
        if cfg.boost_error > 0.0 and abs(e) > cfg.boost_error:
            kp *= cfg.kp_boost

        de = e - self.e_prev
        p_term = cfg.sign * kp * de
        i_term = cfg.sign * cfg.ki * dt * e

        ff = 0.0
        if cfg.ff_gain != 0.0:
            ff = cfg.sign * cfg.ff_gain * (setpoint - self.sp_prev)

        dz = p_term + i_term + ff
        dz_raw = dz
        if dz < -cfg.dz_limit_um:
            dz = -cfg.dz_limit_um
        elif dz > cfg.dz_limit_um:
            dz = cfg.dz_limit_um
        saturated = 1 if dz != dz_raw else 0

        z_next = self.z + dz
        limit_hit = 0
        if z_next < cfg.z_min_um:
            z_next = cfg.z_min_um
            limit_hit = 1
        elif z_next > cfg.z_max_um:
            z_next = cfg.z_max_um
            limit_hit = 1

        self.z = z_next
        self.e_prev = e
        self.sp_prev = setpoint
        self.steps += 1

        if abs(e) <= cfg.deadband:
            self.hold += 1
        else:
            self.hold = 0
        converged = 1 if self.hold >= cfg.hold_steps else 0

        # Progress guard, two failure modes.  A measurement that ignores Z either
        # lets the error grow (wrong sign, runaway) or never improves (probe far
        # away, no coupling at all).  Without both checks the loop would quietly
        # grind through the whole travel budget and report a bogus point.
        #
        # Only steps taken *towards* the surface count as evidence of a dead
        # loop.  A step away from it cannot be: when the probe starts inside the
        # sample (a retract that was too small for the next point's height) the
        # reading is clipped at its maximum, so "no improvement" is exactly what
        # a correct retreat looks like.  ``sign`` is the sign of
        # d(reading)/dZ, so the only steps that are *not* retreating have
        # ``dz * sign >= 0``; a zero-length step counts too, which is what keeps
        # a gains-off guard check honest.  (The weaker condition
        # ``dz * sign * e > 0`` is useless here: a correctly wired loop always
        # moves in the direction that reduces |e|.)
        e_abs = abs(e)
        toward = 1 if (dz * cfg.sign) >= 0.0 else 0
        if e_abs < self.e_best:
            self.e_best = e_abs      # monotone ratchet: noise cannot fake progress
            self.stall = 0
        elif toward:
            self.stall += 1

        if e_abs > self.e_abs_start * 1.5 + cfg.deadband:
            self.growing += 1
        else:
            self.growing = 0
        diverged = 1 if self.growing >= GROW_STEPS else 0
        stalled = 1 if self.stall >= cfg.stall_steps else 0

        exhausted = 1 if self.steps >= cfg.max_steps else 0

        if converged:
            status = STATUS_CONVERGED
        elif diverged:
            status = STATUS_DIVERGED
        elif stalled:
            status = STATUS_STALLED
        elif exhausted:
            status = STATUS_EXHAUSTED
        elif limit_hit:
            status = STATUS_Z_LIMIT
        elif saturated:
            status = STATUS_SATURATED
        else:
            status = STATUS_OK

        self._last = PiOutput(
            z_um=self.z, dz_um=dz, error=e, p_term=p_term, i_term=i_term,
            dt_s=dt, steps=self.steps,
            converged=bool(converged), saturated=bool(saturated),
            limit_hit=bool(limit_hit), exhausted=bool(exhausted),
            diverged=bool(diverged), stalled=bool(stalled), status=status,
        )
        return self._last

    def state(self):
        return self._last

    def set_gains(self, kp, ki):
        if kp < 0 or ki < 0:
            raise ValueError("kp and ki must be non-negative")
        self.config.kp = float(kp)
        self.config.ki = float(ki)
        return self

    def set_dz_limit(self, dz_limit_um):
        if not float(dz_limit_um) > 0.0:
            raise ValueError("dz_limit_um must be positive")
        self.config.dz_limit_um = float(dz_limit_um)
        return self

    def close(self):
        return None


# --------------------------------------------------------------------------- #
# Public facade
# --------------------------------------------------------------------------- #

class PiController:
    """One Z loop.  Uses the C++ engine when available, else the reference.

    The object is a drop-in for either backend -- ``backend`` reports which one
    is live, and ``describe()`` is what the run log records, so a scan file
    always says which controller produced it.
    """

    def __init__(self, config: Optional[PiConfig] = None, dll_path=None,
                 prefer_python=False, required=False):
        self.config = (config or PiConfig()).validated()
        self._impl = None
        self._load_error = None

        if not prefer_python:
            try:
                self._impl = _CppPi(self.config, dll_path=dll_path)
            except Exception as exc:  # noqa: BLE001 - fallback is the point
                self._load_error = str(exc)
                if required:
                    raise

        if self._impl is None:
            self._impl = _PythonPi(self.config)

    # -- introspection ------------------------------------------------------ #

    @property
    def backend(self):
        return self._impl.backend

    @property
    def version(self):
        return self._impl.version

    @property
    def load_error(self):
        return self._load_error

    def describe(self):
        if self.backend == "c++":
            return "digital PI, %s" % self.version
        return "digital PI, Python reference (C++ engine unavailable: %s)" % (
            self._load_error or "not requested"
        )

    # -- control ------------------------------------------------------------ #

    def reset(self, z0, measurement0):
        self._impl.reset(z0, measurement0)
        return self

    def step(self, setpoint, measurement, dt_s=0.0):
        return self._impl.step(setpoint, measurement, dt_s)

    def state(self):
        return self._impl.state()

    def set_gains(self, kp, ki):
        self._impl.set_gains(kp, ki)
        self.config.kp = float(kp)
        self.config.ki = float(ki)
        return self

    def set_dz_limit(self, dz_limit_um):
        self._impl.set_dz_limit(dz_limit_um)
        self.config.dz_limit_um = float(dz_limit_um)
        return self

    def close(self):
        self._impl.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


# --------------------------------------------------------------------------- #
# DLL self-check
# --------------------------------------------------------------------------- #

def dll_selftest(dll_path=None, plant_gain=1.0):
    """Run the controller's own closed-loop check inside the DLL."""
    lib = _CppPi._load(dll_path)
    lib.afm_pi_selftest.argtypes = [
        ctypes.c_double, ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_int)
    ]
    lib.afm_pi_selftest.restype = ctypes.c_int
    final_z = ctypes.c_double(0.0)
    steps = ctypes.c_int(0)
    rc = lib.afm_pi_selftest(float(plant_gain), ctypes.byref(final_z), ctypes.byref(steps))
    return rc, final_z.value, steps.value


def python_selftest(plant_gain=1.0):
    """Same plant and gains as ``afm_pi_selftest`` in the DLL.

    This is not a tuning example -- it is a fixed, reproducible trace whose only
    purpose is to be compared, term by term, with what the compiled controller
    computes.
    """
    cfg = PiConfig(kp=0.100, ki=700.0, dt_s=0.001, dz_limit_um=1.0,
                   deadband=0.0, hold_steps=1000, max_steps=40)
    pi = _PythonPi(cfg)
    pi.reset(0.0, 0.0)
    z = 0.0
    out = None
    for _ in range(40):
        out = pi.step(1.0, plant_gain * z, 0.001)
        z = out.z_um
    return z, out.steps


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def _demo():
    """Closed loop on a linear plant, printed step by step.

    The plant here is deliberately simple (``y = gain * z``) so the trace is
    readable.  A real probe is strongly nonlinear -- the sensitivity collapses
    as the gap opens -- which is what ``kp_boost`` and ``boost_error`` are for.
    """
    gain = 0.5
    setpoint = 1.0
    cfg = PiConfig(kp=0.20, ki=280.0, dt_s=0.005, dz_limit_um=1.0,
                   deadband=0.01, sign=1.0, max_steps=60)
    pi = PiController(cfg)
    print(pi.describe())
    print("plant: y = %.2f * z, setpoint %.2f -> z_target %.2f um"
          % (gain, setpoint, setpoint / gain))

    z = 0.0
    pi.reset(z, gain * z)
    print("%4s %10s %10s %10s %10s %8s" % ("k", "z (um)", "meas", "err", "dz (um)", "status"))
    for k in range(60):
        y = gain * z
        out = pi.step(setpoint, y, 0.005)
        z = out.z_um
        print("%4d %10.4f %10.4f %10.4f %10.4f %8s"
              % (k + 1, out.z_um, y, out.error, out.dz_um, out.status_name))
        if out.converged or out.failed:
            break
    pi.close()
    return 0


def _selftest():
    problems = []

    def check(name, ok, detail=""):
        print("  %-46s %s %s" % (name, "ok" if ok else "FAIL", detail))
        if not ok:
            problems.append(name)

    print("afm_pi selftest")

    # 1. The Python reference must close a unit-slope plant onto the setpoint.
    z_py, steps_py = python_selftest(1.0)
    check("python reference converges", abs(z_py - 1.0) < 0.05,
          "z=%.6f after %d steps" % (z_py, steps_py))

    # 2. If the DLL is present, it must agree with the reference exactly.
    try:
        rc, z_cpp, steps_cpp = dll_selftest()
        check("dll loads and runs", rc == 0, "rc=%d" % rc)
        check("dll matches python reference", abs(z_cpp - z_py) < 1e-12,
              "cpp=%.12f py=%.12f" % (z_cpp, z_py))
        check("dll step count matches", steps_cpp == steps_py,
              "%d vs %d" % (steps_cpp, steps_py))
        ctrl = PiController()
        check("facade picks the C++ engine", ctrl.backend == "c++", ctrl.version)
        ctrl.close()
    except OSError as exc:
        print("  %-46s %s" % ("dll present", "no (%s)" % str(exc)[:40]))
        ctrl = PiController()
        check("facade falls back to python", ctrl.backend == "python")
        ctrl.close()

    # 3. Deadband lock.
    cfg = PiConfig(kp=0.1, ki=1.0, deadband=0.01, hold_steps=2, max_steps=50)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    for _ in range(10):
        out = pi.step(1.0, 0.999, 0.01)
    check("holds inside the deadband", out.converged, out.status_name)
    pi.close()

    # 4. Negative sign: the loop must drive Z the other way.
    cfg = PiConfig(kp=0.05, ki=1.0, sign=-1.0, dz_limit_um=0.5, deadband=0.01,
                   max_steps=80)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    for _ in range(80):
        out = pi.step(1.0, 0.0, 0.01)   # never responds: only direction matters
        if out.dz_um:
            break
    check("negative sign moves Z negative", out.dz_um < 0, "dz=%.4f" % out.dz_um)
    pi.close()

    # 5. dz limit is never exceeded.
    cfg = PiConfig(kp=100.0, ki=100.0, dz_limit_um=0.05, max_steps=20)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    worst = 0.0
    for _ in range(20):
        out = pi.step(1.0, 0.0, 0.1)
        worst = max(worst, abs(out.dz_um))
    check("dz limit respected", worst <= 0.05 + 1e-12, "max |dz|=%.6f" % worst)
    pi.close()

    # 6. Z window is never left.
    cfg = PiConfig(kp=1.0, ki=1.0, dz_limit_um=1.0, z_min_um=-1.0, z_max_um=1.0,
                   max_steps=50)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    zs = []
    for _ in range(50):
        out = pi.step(10.0, 0.0, 0.1)
        zs.append(out.z_um)
    check("z window respected", min(zs) >= -1.0 - 1e-12 and max(zs) <= 1.0 + 1e-12,
          "z in [%.3f, %.3f]" % (min(zs), max(zs)))
    check("z window reports limit", out.limit_hit or out.exhausted,
          "limit_hit=%d exhausted=%d z=%.3f" % (out.limit_hit, out.exhausted, out.z_um))
    pi.close()

    # 7. A plant that ignores Z must be reported, not ground through the budget.
    # Either guard is acceptable here: a decoupled probe can fail to improve at
    # all (stalled) or run away (diverged).  What must never happen is quietly
    # spending the whole travel budget and reporting the result as a point.
    cfg = PiConfig(kp=0.05, ki=1.0, dz_limit_um=0.5, deadband=0.001, max_steps=500)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    flagged = False
    for _ in range(500):
        out = pi.step(1.0, 0.0, 0.01)   # measurement never moves
        if out.diverged or out.stalled:
            flagged = True
            break
    check("dead plant is flagged", flagged and out.failed,
          "%s after %d steps" % (out.status_name, out.steps))
    pi.close()

    # 7b. The stall guard must be a monotone ratchet.  Noise riding on a
    # constant reading may lower the best-so-far a few times, but it must not
    # keep the loop looking alive forever -- otherwise a probe that is nowhere
    # near the surface would grind through the whole travel budget.
    import random as _random

    rng = _random.Random(20261017)
    cfg = PiConfig(kp=0.0, ki=0.0, dz_limit_um=0.5, deadband=0.0, max_steps=500)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    first_flag = None
    for _ in range(500):
        noise = rng.uniform(-0.02, 0.02)       # zero-mean, no trend at all
        out = pi.step(1.0, noise, 0.01)
        if out.stalled:
            first_flag = out.steps
            break
    check("noise does not reset the stall guard",
          first_flag is not None and first_flag <= 100,
          "flagged at step %s (limit 100)" % first_flag)
    pi.close()

    # 8. dt is capped, so one long stall cannot produce a huge kick.
    cfg = PiConfig(kp=0.0, ki=1.0, dt_s=0.001, dz_limit_um=100.0, max_steps=5)
    pi = PiController(cfg)
    pi.reset(0.0, 0.0)
    out = pi.step(1.0, 0.0, 5.0)       # caller was blocked for 5 s
    check("dt capped at 10x nominal", abs(out.dt_s - 0.010) < 1e-15,
          "dt=%.6f" % out.dt_s)
    pi.close()

    # 9. Bumpless reset: first step has no derivative kick.
    cfg = PiConfig(kp=10.0, ki=0.0, dz_limit_um=10.0, max_steps=5)
    pi = PiController(cfg)
    pi.reset(0.0, 0.7)
    out = pi.step(1.0, 0.7, 0.01)
    check("no derivative kick on step one", abs(out.p_term) < 1e-15,
          "p=%.3e" % out.p_term)
    pi.close()

    print("")
    if problems:
        print("SELFTEST FAILED: %s" % ", ".join(problems))
        return 1
    print("SELFTEST PASSED")
    return 0


def _compare(trials=200):
    """Drive both backends with identical inputs and compare bit for bit."""
    import random

    rng = random.Random(20261017)
    cfg = PiConfig(kp=0.037, ki=0.91, dt_s=0.004, dz_limit_um=0.21,
                   deadband=0.002, sign=-1.0, kp_boost=2.5, boost_error=0.4,
                   ff_gain=0.1, z_min_um=-3.0, z_max_um=3.0,
                   hold_steps=2, max_steps=60)
    a = PiController(cfg, prefer_python=False)
    b = PiController(cfg, prefer_python=True)
    if a.backend != "c++":
        print("no DLL available, nothing to compare (%s)" % a.load_error)
        return 2

    a.reset(0.0, 0.0)
    b.reset(0.0, 0.0)
    worst = 0.0
    mismatches = 0
    for i in range(trials):
        sp = 1.0 + 0.2 * math.sin(i * 0.05)
        meas = 0.5 + 0.5 * math.cos(i * 0.11)
        dt = 0.001 + 0.004 * rng.random()
        oa = a.step(sp, meas, dt)
        ob = b.step(sp, meas, dt)
        for field in ("z_um", "dz_um", "error", "p_term", "i_term", "dt_s"):
            d = abs(getattr(oa, field) - getattr(ob, field))
            worst = max(worst, d)
        if (oa.status, oa.steps) != (ob.status, ob.steps):
            mismatches += 1
    a.close()
    b.close()

    print("compared %d steps: worst field difference %.3e, status mismatches %d"
          % (trials, worst, mismatches))
    ok = worst < 1e-12 and mismatches == 0
    print("COMPARE %s" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="AFM Z-loop digital PI controller (C++ engine + Python reference)"
    )
    parser.add_argument("--selftest", action="store_true",
                        help="check both backends and the loop invariants")
    parser.add_argument("--compare", action="store_true",
                        help="compare the C++ engine with the Python reference")
    parser.add_argument("--demo", action="store_true",
                        help="run a closed loop on a synthetic plant")
    parser.add_argument("--build", action="store_true",
                        help="compile Nanomax/afm_pi.cpp into afm_pi.dll")
    parser.add_argument("--info", action="store_true",
                        help="report which backend would be used")
    args = parser.parse_args(argv)

    if args.build:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "Tool_code"))
        from build_afm_pi import build
        return build()
    if args.compare:
        return _compare()
    if args.demo:
        return _demo()
    if args.info:
        ctrl = PiController()
        print("backend : %s" % ctrl.backend)
        print("version : %s" % ctrl.version)
        if ctrl.load_error:
            print("fallback: %s" % ctrl.load_error)
        print("config  : %s" % ctrl.config)
        ctrl.close()
        return 0
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
