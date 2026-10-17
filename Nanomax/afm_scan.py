# -*- coding: utf-8 -*-
"""AFM-style approach/retract scan engine for the closed-loop sample NanoMax.

The idea
--------
A normal PAM scan keeps Z fixed and moves X/Y on a raster.  This engine does
what an atomic force microscope does instead:

    move X/Y to the next point
    bring Z to the setpoint on the feedback signal
    stop, take the A-line there
    back Z off by a retract distance
    repeat

The feedback signal is the *only* thing that defines the surface.  Nothing here
knows or assumes a Z datum: the surface position is re-discovered at every
point, so sample tilt and thermal drift are tracked instead of being a
systematic error.

Because the approach always starts from ``last_stop - retract_um``, the Z travel
per point is bounded by ``retract_um + max_approach_um`` no matter how long the
scan runs.  That is the property that keeps a 20 um piezo alive over thousands
of points.

Two control engines
-------------------
``control_mode = "pi"`` (default)
    A digital PI in velocity form, compiled in C++ (``Nanomax/afm_pi.cpp``,
    loaded through ctypes, see :mod:`Nanomax.afm_pi`).  It *tracks a setpoint*
    instead of creeping until a threshold is crossed, so it does not care
    whether the signal rises or falls with Z, and it can back off when it
    overshoots.  Use this unless you have a reason not to.

``control_mode = "stepped"``
    The original fixed-step creep: move ``coarse_step_um`` until the signal is
    within ``coarse_to_fine_ratio`` of the setpoint, then ``fine_step_um``,
    stop at the first crossing.  Purely one-directional -- if the starting Z is
    already past the surface it reports ``already_at_setpoint`` and never
    corrects.  Kept because it is trivial to reason about and is a useful
    fallback if the PI cannot be tuned on a given sample.

The setpoint is whatever the caller says it is
----------------------------------------------
``setpoint`` is compared against whatever ``measure()`` returns, in the same
unit.  The caller supplies both, so the Z criterion can be the 10 MHz
amplitude, a demodulated phase, a cantilever deflection, or anything else --
the engine only needs ``() -> (value, snr)``.

Separation of concerns
----------------------
This module contains no hardware calls and no file I/O.  It drives a *stage
object* (``move_axis`` / ``get_axis_position`` / ``wait_until_axis_settled``)
and a *measure callable*.  Both are injected, which is what makes the whole
state machine testable without a piezo or a digitizer -- see
``Tool_code/test_afm_offline.py``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

try:  # normal package import
    from .afm_pi import (
        PiConfig,
        PiController,
        STATUS_CONVERGED,
        STATUS_DIVERGED,
        STATUS_EXHAUSTED,
        STATUS_STALLED,
    )
except ImportError:  # run as a plain script
    from afm_pi import (  # type: ignore[no-redef]
        PiConfig,
        PiController,
        STATUS_CONVERGED,
        STATUS_DIVERGED,
        STATUS_EXHAUSTED,
        STATUS_STALLED,
    )


CONTROL_MODES = ("pi", "stepped")


# --------------------------------------------------------------------------- #
# Configuration / results
# --------------------------------------------------------------------------- #

@dataclass
class AfmApproachConfig:
    """Knobs for one approach."""

    #: Value the approach stops at, in whatever unit ``measure`` returns.
    setpoint: float = 0.0

    #: +1 if increasing Z brings the sample towards the probe, -1 otherwise.
    #: There is no way to infer this from software -- confirm it with a small
    #: manual move before the first real scan.  For the PI this is the only
    #: direction information it needs, so a signal that *falls* on approach is
    #: handled by setting -1, not by changing anything else.
    approach_sign: float = 1.0

    #: "pi" (default) or "stepped".
    control_mode: str = "pi"

    # -- shared geometry ---------------------------------------------------- #

    #: Hard budget for one approach, measured from the Z it started at.  In PI
    #: mode this becomes the Z window the controller may not leave, plus a
    #: cumulative-travel backstop.
    max_approach_um: float = 4.0
    #: Distance to back off after the setpoint is reached.
    retract_um: float = 0.500
    #: Hard budget for the retract, in case the surface moved.
    max_retract_um: float = 2.0

    #: Stop immediately if the feedback value exceeds this (probe crash guard).
    #: Set <= 0 to disable.  Must be above ``setpoint`` to be meaningful.
    safety_amplitude: float = 0.0

    #: Refuse to trust a reading below this signal-to-noise ratio.  Set <= 0 to
    #: disable.  A point that fails this is reported, not silently accepted.
    min_snr: float = 0.0

    #: Positioning tolerance passed to the stage.
    position_tolerance_um: float = 0.02
    #: Settle time for the *final* move, before the A-line is taken.
    settle_time_ms: int = 0
    #: Settle time for the intermediate PI steps.  Keep this short: the loop
    #: measures after every step, and a long settle here is the dominant cost
    #: of a point.  The final move still uses ``settle_time_ms``.
    pi_settle_ms: int = 0
    settle_timeout_s: float = 10.0

    #: Belt and braces: never issue a Z outside this window when it is given.
    z_min_um: Optional[float] = None
    z_max_um: Optional[float] = None

    # -- stepped engine ----------------------------------------------------- #

    #: Travel per step while far from the surface.
    coarse_step_um: float = 0.100
    #: Travel per step once the signal is close to the setpoint.
    fine_step_um: float = 0.020
    #: Fraction of the setpoint at which the coarse step gives way to the fine one.
    coarse_to_fine_ratio: float = 0.30
    #: Safety valve against a runaway stepped loop.
    max_steps: int = 4000

    # -- PI engine ---------------------------------------------------------- #

    #: um per unit of *change* in the feedback value.  This is the damping
    #: term: it reacts to how fast the signal is moving, not to how far off it
    #: is.  Rule of thumb: ``kp ~ 0.5 / sensitivity``, where sensitivity is the
    #: feedback change per um of Z (see :func:`suggest_gains`).
    kp: float = 0.003

    #: um per (feedback unit * second).  The creep term -- this is what closes
    #: the gap while the signal has not started moving yet.
    ki: float = 0.050

    #: Assumed period for the first step, before a real dt has been measured.
    #: Also the reference the measured dt is capped against (10x).
    pi_dt_s: float = 0.030

    #: Cap on one step's travel.  The main speed/safety trade-off: larger is
    #: faster but risks overshooting into the probe.
    dz_limit_um: float = 0.050

    #: Deadband as a fraction of the setpoint.  It must exceed the relative
    #: noise of the feedback signal, or the loop will never declare lock.
    #: Set to 0 to use ``deadband`` verbatim instead.
    deadband_frac: float = 0.020
    #: Absolute deadband, used when ``deadband_frac`` is 0.
    deadband: float = 0.0

    #: Extra Kp multiplier while |error| > ``boost_error_frac * setpoint``.
    #: A real probe is strongly nonlinear -- the sensitivity collapses as the
    #: gap opens -- so a bigger Kp far from the surface closes the gap in fewer
    #: steps without making the loop twitchy near the setpoint.
    kp_boost: float = 1.0
    #: Error above which the boost applies, as a fraction of the setpoint.
    #: 0 disables the boost.
    boost_error_frac: float = 0.0

    #: Feed-forward on setpoint changes (0 = off).  Only useful if the setpoint
    #: itself is ramped during a scan.
    ff_gain: float = 0.0

    #: Consecutive in-deadband readings required before a point is accepted.
    #: Two is enough to reject a single noisy sample.
    hold_steps: int = 2

    #: Iteration cap for one point (search phase included).
    pi_max_steps: int = 200

    #: How far the loop may travel *without the reading improving* before it
    #: gives up.  This is the one number that makes the stall guard practical:
    #: a saturated reading is flat, so backing out of saturation needs a blind
    #: allowance comfortably larger than ``retract_um``.  Converted to steps as
    #: ``stall_steps = ceil(pi_stall_um / dz_limit_um)``.
    pi_stall_um: float = 1.000

    # -- blind search ------------------------------------------------------- #
    #: Below this fraction of the setpoint the reading is indistinguishable from
    #: "no signal yet" (the sample is still far away), so the loop cannot learn
    #: anything from it.  While that holds, Z is advanced by ``search_step_um``
    #: in the approach direction until the signal appears; only then does the PI
    #: take over.  0 disables the search entirely.
    search_floor_frac: float = 0.10
    #: Travel per step during the search.  Deliberately coarse: there is no
    #: feedback to be gentle about yet, and the budget is what bounds it.
    search_step_um: float = 0.100

    #: Feedback sensitivity in units per um of Z travel.  Set it (>0) and the
    #: controller derives ``kp``/``ki`` from :func:`suggest_gains` instead of
    #: using the values above -- far more reliable than guessing the gains.
    feedback_sensitivity: float = 0.0

    #: Explicit path to ``afm_pi.dll``.  None means "next to afm_pi.py, then the
    #: repo root"; the Python reference is used if no library can be loaded.
    pi_dll: Optional[str] = None

    def validated(self):
        mode = str(self.control_mode).strip().lower()
        if mode not in CONTROL_MODES:
            raise ValueError(
                "control_mode must be one of %s, got %r" % (CONTROL_MODES, self.control_mode)
            )
        self.control_mode = mode
        return self


@dataclass
class AfmApproachResult:
    ok: bool
    z_um: float
    amplitude: float
    steps: int
    reason: str
    snr: float = 0.0
    last_step_um: float = 0.0
    elapsed_s: float = 0.0
    #: Amplitude just before the crossing step, for diagnosing a marginal setpoint.
    previous_amplitude: float = 0.0
    trace: list = field(default_factory=list)
    #: Which engine produced this result ("pi" / "stepped").
    control_mode: str = "stepped"
    #: Controller status name when ``control_mode == "pi"`` ("converged",
    #: "stalled", "diverged", "exhausted", "z_limit", ...).
    status: str = ""
    #: Controller backend ("c++" / "python"); empty for the stepped engine.
    backend: str = ""


# --------------------------------------------------------------------------- #
# Gain helper
# --------------------------------------------------------------------------- #

def suggest_gains(sensitivity_per_um, damping=0.5, creep_fraction=0.30, dt_s=0.030):
    """Starting gains for a plant with a known feedback sensitivity.

    ``sensitivity_per_um`` is how much the feedback value changes per um of Z
    travel near the setpoint -- measure it by moving Z a known small distance
    and watching the reading.  Getting this roughly right matters much more
    than any later fine-tuning.

    ``damping`` is the fraction of the previous step that Kp is allowed to
    cancel; above ~1 the loop oscillates.  ``creep_fraction`` is how much of the
    remaining distance one step should cover with the integral term alone.

    Returns ``(kp, ki)`` in the units :class:`AfmApproachConfig` expects.
    """
    sensitivity_per_um = float(sensitivity_per_um)
    if sensitivity_per_um <= 0:
        raise ValueError("sensitivity must be positive")
    kp = float(damping) / sensitivity_per_um
    ki = float(creep_fraction) / (sensitivity_per_um * float(dt_s))
    return kp, ki


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #

class AfmScanner:
    """Approach / measure / retract state machine.

    Parameters
    ----------
    stage
        Object with ``move_axis(axis, position, ...)``,
        ``get_axis_position(axis)`` and
        ``wait_until_axis_settled(axis, target, ...)`` -- i.e. the
        ``BPC303NativeController`` interface.
    measure
        ``() -> (value, snr)``.  Must be cheap: it is called after every step.
        The value is the Z criterion, whatever the caller decides that is.
    config
        An :class:`AfmApproachConfig`.
    log
        ``log(event, **fields)``; ``Nanomax.run_log.append_run_log`` fits.
    """

    def __init__(
        self,
        stage,
        measure: Callable[[], tuple],
        config: AfmApproachConfig,
        log: Optional[Callable[..., None]] = None,
        # perf_counter, not monotonic: on Windows time.monotonic() is backed by
        # GetTickCount64 and only resolves to ~15 ms, so two consecutive reads
        # inside one control step often return the same value.  dt then comes
        # out as 0, the controller falls back to its nominal period, and the
        # integral kick is silently much larger than the real elapsed time.
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.stage = stage
        # A plain callable, or anything exposing value() -- the ControlSource
        # protocol.  Duck-typed so a caller never has to subclass anything.
        if not callable(measure) and hasattr(measure, "value"):
            measure = measure.value
        self._measure = measure
        self.config = config.validated()
        self._log = log or (lambda event, **fields: None)
        self._clock = clock
        self._sign = 1.0 if float(config.approach_sign) >= 0 else -1.0

        # PI bookkeeping.  The controller is rebuilt when the per-point Z window
        # changes, which is every point, so what is cached here is the backend
        # decision and the identity strings that go into the scan metadata.
        self._pi_ctrl = None
        self._pi_window = None
        self._pi_prefer_python = False
        self._pi_backend = ""
        self._pi_version = ""
        self._pi_load_error = None
        if self.config.control_mode == "pi":
            # Build one up front so the run log can say which engine is live
            # before the first point is scanned.
            self._pi_ctrl = self._make_pi(None, None)

    # -- helpers ------------------------------------------------------------ #

    def _emit(self, event, **fields):
        try:
            self._log(event, **fields)
        except Exception:
            # Logging must never abort a scan.
            pass

    def measure(self):
        value = self._measure()
        if isinstance(value, tuple):
            amplitude, snr = value[0], value[1] if len(value) > 1 else 0.0
        else:
            amplitude, snr = value, 0.0
        return float(amplitude), float(snr)

    def _limits_allow(self, z):
        lo, hi = self.config.z_min_um, self.config.z_max_um
        if lo is not None and z < lo:
            return False
        if hi is not None and z > hi:
            return False
        return True

    def _move_z(self, z, settle_ms=None):
        self.stage.move_axis("z", z)
        self.stage.wait_until_axis_settled(
            "z",
            z,
            settle_time_ms=self.config.settle_time_ms if settle_ms is None else settle_ms,
            tolerance=self.config.position_tolerance_um,
            timeout_s=self.config.settle_timeout_s,
        )
        return float(self.stage.get_axis_position("z"))

    # -- PI plumbing -------------------------------------------------------- #

    def _deadband(self):
        cfg = self.config
        if cfg.deadband_frac > 0.0 and cfg.setpoint > 0:
            return float(cfg.deadband_frac) * float(cfg.setpoint)
        return float(cfg.deadband)

    def _make_pi(self, z_lo, z_hi):
        cfg = self.config
        boost_error = 0.0
        if cfg.boost_error_frac > 0.0 and cfg.setpoint > 0:
            boost_error = float(cfg.boost_error_frac) * float(cfg.setpoint)

        kp, ki = float(cfg.kp), float(cfg.ki)
        if cfg.feedback_sensitivity > 0.0:
            kp, ki = suggest_gains(cfg.feedback_sensitivity, dt_s=cfg.pi_dt_s)

        # Blind travel allowance, expressed in um and converted to steps here so
        # the caller never has to think about step counts.
        stall_steps = 8
        if cfg.dz_limit_um > 0 and cfg.pi_stall_um > 0:
            stall_steps = int(math.ceil(float(cfg.pi_stall_um) / float(cfg.dz_limit_um)))
            stall_steps = max(8, min(stall_steps, 400))

        pi_cfg = PiConfig(
            kp=kp,
            ki=ki,
            dt_s=float(cfg.pi_dt_s),
            dz_limit_um=float(cfg.dz_limit_um),
            deadband=self._deadband(),
            sign=self._sign,
            kp_boost=float(cfg.kp_boost),
            boost_error=boost_error,
            ff_gain=float(cfg.ff_gain),
            z_min_um=-1e308 if z_lo is None else float(z_lo),
            z_max_um=1e308 if z_hi is None else float(z_hi),
            hold_steps=int(cfg.hold_steps),
            max_steps=int(cfg.pi_max_steps),
            stall_steps=stall_steps,
        )
        ctrl = PiController(pi_cfg, dll_path=cfg.pi_dll,
                            prefer_python=self._pi_prefer_python)
        if ctrl.backend != "c++":
            # Do not retry a DLL that already failed to load on every point.
            self._pi_prefer_python = True
        self._pi_backend = ctrl.backend
        self._pi_version = ctrl.version
        self._pi_load_error = ctrl.load_error
        return ctrl

    def _pi(self, z_lo, z_hi):
        """Controller for this point's Z window, rebuilt only if the window moved."""
        window = (z_lo, z_hi)
        if self._pi_ctrl is not None and self._pi_window == window:
            return self._pi_ctrl
        if self._pi_ctrl is not None:
            self._pi_ctrl.close()
        self._pi_ctrl = self._make_pi(z_lo, z_hi)
        self._pi_window = window
        return self._pi_ctrl

    def describe(self):
        """Engine identity, for the scan metadata."""
        cfg = self.config
        info = {"control_mode": cfg.control_mode,
                "approach_sign": cfg.approach_sign,
                "setpoint": cfg.setpoint}
        if cfg.control_mode == "pi":
            kp, ki = float(cfg.kp), float(cfg.ki)
            if cfg.feedback_sensitivity > 0.0:
                kp, ki = suggest_gains(cfg.feedback_sensitivity, dt_s=cfg.pi_dt_s)
            info.update({
                "pi_backend": self._pi_backend or "unknown",
                "pi_version": self._pi_version,
                "pi_kp": kp,
                "pi_ki": ki,
                "pi_dz_limit_um": cfg.dz_limit_um,
                "pi_deadband": self._deadband(),
                "pi_hold_steps": cfg.hold_steps,
                "pi_max_steps": cfg.pi_max_steps,
                "pi_stall_um": cfg.pi_stall_um,
                "search_floor_frac": cfg.search_floor_frac,
                "search_step_um": cfg.search_step_um,
                "feedback_sensitivity": cfg.feedback_sensitivity,
            })
            if self._pi_load_error:
                info["pi_load_error"] = self._pi_load_error
        else:
            info.update({
                "coarse_step_um": cfg.coarse_step_um,
                "fine_step_um": cfg.fine_step_um,
            })
        return info

    def close(self):
        if self._pi_ctrl is not None:
            try:
                self._pi_ctrl.close()
            except Exception:
                pass
            self._pi_ctrl = None
            self._pi_window = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # -- approach: dispatch ------------------------------------------------- #

    def approach(self, z_from):
        """Bring Z to the setpoint from ``z_from``.

        Returns an :class:`AfmApproachResult`.  ``ok`` is false when the travel
        budget, a Z limit, the crash guard, the step limit or a progress guard
        was hit; the caller decides whether that is a skipped point or a fatal
        error.
        """
        if self.config.control_mode == "pi":
            return self._approach_pi(z_from)
        return self._approach_stepped(z_from)

    # -- approach: PI ------------------------------------------------------- #

    def _approach_pi(self, z_from):
        cfg = self.config
        started = self._clock()
        z = float(z_from)
        amplitude, snr = self.measure()

        trace = [{"step": 0, "z_um": round(z, 4), "amp": round(amplitude, 4)}]
        self._emit(
            "AFM_APPROACH_BEGIN",
            engine="pi",
            z_from_um="%.4f" % z,
            amplitude="%.4f" % amplitude,
            setpoint="%.4f" % cfg.setpoint,
            sign=cfg.approach_sign,
            kp=cfg.kp,
            ki=cfg.ki,
            dz_limit_um=cfg.dz_limit_um,
            deadband="%.6f" % self._deadband(),
            backend=self._pi_backend,
        )

        def fail(reason, steps, **extra):
            # Rate and travelled distance are what the user needs to tune Ki and
            # dz_limit: they say how fast the loop actually closed, not how many
            # steps it took.
            elapsed = self._clock() - started
            extra.setdefault("travelled_um", "%.4f" % travelled)
            extra.setdefault("rate_um_s", "%.4f" % (travelled / elapsed if elapsed > 0 else 0.0))
            self._emit("AFM_APPROACH_FAILED", engine="pi", reason=reason,
                       z_um="%.4f" % z, amplitude="%.4f" % amplitude, **extra)
            return AfmApproachResult(
                ok=False, z_um=z, amplitude=amplitude, steps=steps, reason=reason,
                snr=snr, last_step_um=0.0, elapsed_s=elapsed,
                trace=trace, control_mode="pi", status=reason,
                backend=self._pi_backend,
            )

        if cfg.setpoint <= 0:
            return fail("setpoint_not_configured", 0)

        budget = float(cfg.max_approach_um)
        if budget <= 0:
            return fail("travel_budget_exhausted", 0, travelled_um="0.0000")

        # The Z window *is* the travel budget, intersected with the caller's
        # absolute limits.  The controller clamps inside it and reports
        # limit_hit, so a surface outside the budget fails fast instead of
        # creeping to the end and then failing anyway.
        z_lo, z_hi = z - budget, z + budget
        if cfg.z_min_um is not None:
            z_lo = max(z_lo, float(cfg.z_min_um))
        if cfg.z_max_um is not None:
            z_hi = min(z_hi, float(cfg.z_max_um))
        if z_lo >= z_hi:
            return fail("z_limit", 0, z_min=cfg.z_min_um, z_max=cfg.z_max_um)

        previous_amplitude = amplitude
        travelled = 0.0
        index = 0

        # -- phase 1: blind search while the reading carries no information -- #
        # A sample that is still far away reads the same as a disconnected
        # probe, so the PI cannot tell which way to go and its stall guard would
        # (correctly) give up after a few steps.  Advance coarsely instead,
        # using the same direction the PI would use, until the signal appears.
        floor = float(cfg.search_floor_frac) * float(cfg.setpoint)
        search_step = float(cfg.search_step_um)
        if floor > 0.0 and search_step > 0.0 and amplitude < floor:
            self._emit("AFM_SEARCH_BEGIN", floor="%.4f" % floor,
                       step_um="%.4f" % search_step,
                       amplitude="%.4f" % amplitude)
            while amplitude < floor:
                if index >= int(cfg.pi_max_steps):
                    return fail("max_steps", index)
                if travelled + search_step > budget + 1e-9:
                    return fail("signal_not_found", index,
                                travelled_um="%.4f" % travelled)
                z_next = z + self._sign * search_step
                if not self._limits_allow(z_next):
                    return fail("z_limit", index, target_um="%.4f" % z_next,
                                z_min=cfg.z_min_um, z_max=cfg.z_max_um)
                try:
                    actual = self._move_z(z_next, settle_ms=cfg.pi_settle_ms)
                except Exception as exc:
                    return fail("stage_error", index, error=repr(exc))
                travelled += abs(actual - z)
                z = actual
                index += 1
                previous_amplitude = amplitude
                amplitude, snr = self.measure()
                trace.append({"step": index, "z_um": round(z, 4),
                              "amp": round(amplitude, 4), "search": True})
                if cfg.safety_amplitude > 0 and amplitude >= cfg.safety_amplitude:
                    self._emit("AFM_APPROACH_FAILED", engine="pi",
                               reason="safety_amplitude", z_um="%.4f" % z,
                               amplitude="%.4f" % amplitude,
                               safety="%.4f" % cfg.safety_amplitude)
                    return AfmApproachResult(
                        ok=False, z_um=z, amplitude=amplitude, steps=index,
                        reason="safety_amplitude", snr=snr,
                        previous_amplitude=previous_amplitude,
                        elapsed_s=self._clock() - started, trace=trace,
                        control_mode="pi", status="safety_amplitude",
                        backend=self._pi_backend,
                    )
            self._emit("AFM_SEARCH_HANDOVER", steps=index, z_um="%.4f" % z,
                       amplitude="%.4f" % amplitude, travelled_um="%.4f" % travelled)

        # -- phase 2: closed-loop PI ----------------------------------------- #
        pi = self._pi(z_lo, z_hi)
        pi.reset(z, amplitude)

        t_prev = self._clock()
        out = pi.state()
        prev_z = z

        while index < int(cfg.pi_max_steps):
            index += 1
            now = self._clock()
            dt = now - t_prev
            t_prev = now

            out = pi.step(cfg.setpoint, amplitude, dt)

            # Terminal conditions are decided on the reading we already have,
            # before any further move -- accepting a point means "the sample
            # sitting at this Z reads on target", not "the next Z would".
            if out.converged:
                return self._accept_pi(pi, z, amplitude, snr, index, out,
                                       previous_amplitude, trace, started, travelled)
            if out.status == STATUS_DIVERGED:
                return fail("diverged", index - 1, error="%.4f" % out.error)
            if out.status == STATUS_STALLED:
                return fail("stalled", index - 1, error="%.4f" % out.error,
                            travelled_um="%.4f" % travelled)
            if out.status == STATUS_EXHAUSTED:
                return fail("max_steps", index - 1)
            if out.limit_hit:
                return fail("travel_budget_exhausted", index - 1,
                            travelled_um="%.4f" % travelled)

            target = out.z_um
            try:
                actual = self._move_z(target, settle_ms=cfg.pi_settle_ms)
            except Exception as exc:
                return fail("stage_error", index - 1, error=repr(exc))

            travelled += abs(actual - prev_z)
            prev_z = actual
            z = actual

            previous_amplitude = amplitude
            amplitude, snr = self.measure()
            trace.append({"step": index, "z_um": round(z, 4),
                          "amp": round(amplitude, 4)})
            if len(trace) > 400:
                # Keep the log bounded on a long approach; the extremes matter.
                trace = trace[:200] + trace[-200:]

            # snr == 0 means "not measured" (the time-domain demodulator does
            # not produce one), so it must not be treated as a failing ratio.
            if cfg.min_snr > 0 and 0 < snr < cfg.min_snr:
                self._emit("AFM_SNR_LOW", step=index, z_um="%.4f" % z,
                           amplitude="%.4f" % amplitude, snr="%.2f" % snr,
                           min_snr="%.2f" % cfg.min_snr)

            if cfg.safety_amplitude > 0 and amplitude >= cfg.safety_amplitude:
                self._emit("AFM_APPROACH_FAILED", engine="pi",
                           reason="safety_amplitude", z_um="%.4f" % z,
                           amplitude="%.4f" % amplitude,
                           safety="%.4f" % cfg.safety_amplitude)
                return AfmApproachResult(
                    ok=False, z_um=z, amplitude=amplitude, steps=index,
                    reason="safety_amplitude", snr=snr,
                    last_step_um=abs(out.dz_um),
                    previous_amplitude=previous_amplitude,
                    elapsed_s=self._clock() - started, trace=trace,
                    control_mode="pi", status="safety_amplitude",
                    backend=self._pi_backend,
                )

            if travelled > budget:
                return fail("travel_budget_exhausted", index,
                            travelled_um="%.4f" % travelled)

        return fail("max_steps", int(cfg.pi_max_steps))

    def _accept_pi(self, pi, z, amplitude, snr, steps, out, previous_amplitude,
                   trace, started, travelled):
        """Finish a converged point: full settle, then confirm the reading."""
        cfg = self.config
        z_settled = z
        if cfg.settle_time_ms != cfg.pi_settle_ms:
            # The intermediate steps used the short settle; give the stage the
            # full one now, at the position we are about to sample.
            try:
                z_settled = self._move_z(z, settle_ms=cfg.settle_time_ms)
                fresh, snr = self.measure()
                shift = fresh - amplitude
                if abs(shift) > self._deadband():
                    self._emit("AFM_SETTLE_SHIFTED", z_um="%.4f" % z_settled,
                               before="%.4f" % amplitude, after="%.4f" % fresh,
                               deadband="%.6f" % self._deadband())
                amplitude = fresh
                trace.append({"step": steps, "z_um": round(z_settled, 4),
                              "amp": round(amplitude, 4), "settled": True})
            except Exception as exc:
                self._emit("AFM_SETTLE_FAILED", z_um="%.4f" % z,
                           error=repr(exc))

        elapsed = self._clock() - started
        self._emit(
            "AFM_SETPOINT_REACHED",
            engine="pi",
            steps=steps,
            z_um="%.4f" % z_settled,
            amplitude="%.4f" % amplitude,
            error="%.6f" % out.error,
            deadband="%.6f" % self._deadband(),
            travelled_um="%.4f" % travelled,
            rate_um_s="%.4f" % (travelled / elapsed if elapsed > 0 else 0.0),
            backend=self._pi_backend,
        )
        return AfmApproachResult(
            ok=True, z_um=z_settled, amplitude=amplitude, steps=steps,
            reason="setpoint_reached", snr=snr, last_step_um=abs(out.dz_um),
            previous_amplitude=previous_amplitude,
            elapsed_s=elapsed, trace=trace,
            control_mode="pi", status="converged", backend=self._pi_backend,
        )

    # -- approach: stepped -------------------------------------------------- #

    def _approach_stepped(self, z_from):
        """Creep Z from ``z_from`` until the value reaches the setpoint.

        One-directional by construction: it stops at the first crossing and
        never corrects downward.  Kept as a fallback; the PI is the default.
        """
        cfg = self.config
        started = self._clock()
        z = float(z_from)
        amplitude, snr = self.measure()

        trace = [{"step": 0, "z_um": round(z, 4), "amp": round(amplitude, 4)}]
        self._emit(
            "AFM_APPROACH_BEGIN",
            engine="stepped",
            z_from_um="%.4f" % z,
            amplitude="%.4f" % amplitude,
            setpoint="%.4f" % cfg.setpoint,
            sign=cfg.approach_sign,
        )

        if cfg.setpoint <= 0:
            return AfmApproachResult(
                ok=False, z_um=z, amplitude=amplitude, steps=0,
                reason="setpoint_not_configured", snr=snr,
                elapsed_s=self._clock() - started, trace=trace,
                control_mode="stepped",
            )

        if amplitude >= cfg.setpoint:
            self._emit("AFM_ALREADY_AT_SETPOINT", z_um="%.4f" % z,
                       amplitude="%.4f" % amplitude)
            return AfmApproachResult(
                ok=True, z_um=z, amplitude=amplitude, steps=0,
                reason="already_at_setpoint", snr=snr,
                elapsed_s=self._clock() - started, trace=trace,
                control_mode="stepped",
            )

        step = float(cfg.coarse_step_um)
        if step <= 0:
            step = float(cfg.fine_step_um)
        previous_amplitude = amplitude
        travelled = 0.0

        for index in range(1, int(cfg.max_steps) + 1):
            if travelled + step > float(cfg.max_approach_um) + 1e-9:
                self._emit(
                    "AFM_APPROACH_FAILED",
                    engine="stepped",
                    reason="travel_budget_exhausted",
                    travelled_um="%.4f" % travelled,
                    amplitude="%.4f" % amplitude,
                )
                return AfmApproachResult(
                    ok=False, z_um=z, amplitude=amplitude, steps=index - 1,
                    reason="travel_budget_exhausted", snr=snr,
                    previous_amplitude=previous_amplitude,
                    elapsed_s=self._clock() - started, trace=trace,
                    control_mode="stepped",
                )

            z_next = z + self._sign * step
            if not self._limits_allow(z_next):
                self._emit(
                    "AFM_APPROACH_FAILED",
                    engine="stepped",
                    reason="z_limit",
                    target_um="%.4f" % z_next,
                    z_min=cfg.z_min_um,
                    z_max=cfg.z_max_um,
                )
                return AfmApproachResult(
                    ok=False, z_um=z, amplitude=amplitude, steps=index - 1,
                    reason="z_limit", snr=snr,
                    previous_amplitude=previous_amplitude,
                    elapsed_s=self._clock() - started, trace=trace,
                    control_mode="stepped",
                )

            actual = self._move_z(z_next)
            travelled += abs(actual - z)
            z = actual

            previous_amplitude = amplitude
            amplitude, snr = self.measure()
            trace.append({"step": index, "z_um": round(z, 4), "amp": round(amplitude, 4)})
            if len(trace) > 400:
                # Keep the log bounded on a long approach; the extremes matter.
                trace = trace[:200] + trace[-200:]

            # snr == 0 means "not measured" (the time-domain demodulator does not
            # produce one), so it must not be treated as a failing ratio.
            if cfg.min_snr > 0 and 0 < snr < cfg.min_snr:
                self._emit(
                    "AFM_SNR_LOW",
                    step=index,
                    z_um="%.4f" % z,
                    amplitude="%.4f" % amplitude,
                    snr="%.2f" % snr,
                    min_snr="%.2f" % cfg.min_snr,
                )

            if cfg.safety_amplitude > 0 and amplitude >= cfg.safety_amplitude:
                self._emit(
                    "AFM_APPROACH_FAILED",
                    engine="stepped",
                    reason="safety_amplitude",
                    z_um="%.4f" % z,
                    amplitude="%.4f" % amplitude,
                    safety="%.4f" % cfg.safety_amplitude,
                )
                return AfmApproachResult(
                    ok=False, z_um=z, amplitude=amplitude, steps=index,
                    reason="safety_amplitude", snr=snr, last_step_um=step,
                    previous_amplitude=previous_amplitude,
                    elapsed_s=self._clock() - started, trace=trace,
                    control_mode="stepped",
                )

            if amplitude >= cfg.setpoint:
                self._emit(
                    "AFM_SETPOINT_REACHED",
                    engine="stepped",
                    steps=index,
                    z_um="%.4f" % z,
                    amplitude="%.4f" % amplitude,
                    step_um="%.3f" % step,
                    overshoot_ratio="%.3f" % (amplitude / cfg.setpoint),
                )
                return AfmApproachResult(
                    ok=True, z_um=z, amplitude=amplitude, steps=index,
                    reason="setpoint_reached", snr=snr, last_step_um=step,
                    previous_amplitude=previous_amplitude,
                    elapsed_s=self._clock() - started, trace=trace,
                    control_mode="stepped",
                )

            # Switch to the fine step as soon as the signal is in the neighbourhood.
            if (
                step > float(cfg.fine_step_um) > 0
                and amplitude >= cfg.coarse_to_fine_ratio * cfg.setpoint
            ):
                step = float(cfg.fine_step_um)
                self._emit("AFM_STEP_COARSE_TO_FINE", step_um="%.3f" % step,
                           amplitude="%.4f" % amplitude)

        self._emit("AFM_APPROACH_FAILED", engine="stepped", reason="max_steps",
                   steps=cfg.max_steps)
        return AfmApproachResult(
            ok=False, z_um=z, amplitude=amplitude, steps=int(cfg.max_steps),
            reason="max_steps", snr=snr,
            previous_amplitude=previous_amplitude,
            elapsed_s=self._clock() - started, trace=trace,
            control_mode="stepped",
        )

    # -- retract ------------------------------------------------------------ #

    def retract(self, z_from):
        """Back the sample away from the probe.  Best effort, never raises."""
        cfg = self.config
        z_from = float(z_from)
        target = z_from - self._sign * float(cfg.retract_um)

        # Never spend more than the retract budget, even if the surface moved.
        if abs(target - z_from) > float(cfg.max_retract_um):
            target = z_from - self._sign * float(cfg.max_retract_um)
        if not self._limits_allow(target):
            # Clamp to the nearest allowed Z rather than skipping the retract.
            lo, hi = cfg.z_min_um, cfg.z_max_um
            if lo is not None and target < lo:
                target = lo
            if hi is not None and target > hi:
                target = hi

        try:
            actual = self._move_z(target)
            self._emit("AFM_RETRACT", z_from_um="%.4f" % z_from,
                       z_um="%.4f" % actual, requested_um="%.4f" % target)
            return actual
        except Exception as exc:
            self._emit("AFM_RETRACT_FAILED", z_from_um="%.4f" % z_from,
                       requested_um="%.4f" % target, error=repr(exc))
            return z_from

    # -- one point ---------------------------------------------------------- #

    def scan_point(self, z_from):
        """Approach, and on success return ``(result, z_retract)``."""
        result = self.approach(z_from)
        if not result.ok:
            return result, z_from
        return result, self.retract(result.z_um)
