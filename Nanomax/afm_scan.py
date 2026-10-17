# -*- coding: utf-8 -*-
"""AFM-style approach/retract scan engine for the closed-loop sample NanoMax.

The idea
--------
A normal PAM scan keeps Z fixed and moves X/Y on a raster.  This engine does
what an atomic force microscope does instead:

    move X/Y to the next point
    creep Z towards the probe until the 10 MHz amplitude reaches a setpoint
    stop, take the A-line there
    back Z off by a retract distance
    repeat

The 10 MHz amplitude is the *only* feedback.  Nothing here knows or assumes a
Z datum: the surface position is re-discovered at every point, so sample tilt
and thermal drift are tracked instead of being a systematic error.

Because the approach always starts from ``last_stop - retract_um``, the Z travel
per point is bounded by ``retract_um + max_approach_um`` no matter how long the
scan runs.  That is the property that keeps a 20 um piezo alive over thousands
of points.

Separation of concerns
----------------------
This module contains no hardware calls and no file I/O.  It drives a *stage
object* (``move_axis`` / ``get_axis_position`` / ``wait_until_axis_settled``)
and a *measure callable* (``() -> (amplitude, snr)``).  Both are injected, which
is what makes the whole state machine testable without a piezo or a digitizer --
see ``Tool_code/test_afm_scan_offline.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# --------------------------------------------------------------------------- #
# Configuration / results
# --------------------------------------------------------------------------- #

@dataclass
class AfmApproachConfig:
    """Knobs for one approach."""

    #: Amplitude the approach stops at, in whatever unit ``measure`` returns.
    setpoint: float = 0.0

    #: +1 if increasing Z brings the sample towards the probe, -1 otherwise.
    #: There is no way to infer this from software -- confirm it with a small
    #: manual move before the first real scan.
    approach_sign: float = 1.0

    #: Travel per step while far from the surface.
    coarse_step_um: float = 0.100
    #: Travel per step once the signal is close to the setpoint.
    fine_step_um: float = 0.020
    #: Fraction of the setpoint at which the coarse step gives way to the fine one.
    coarse_to_fine_ratio: float = 0.30

    #: Hard budget for one approach, measured from the Z it started at.
    max_approach_um: float = 4.0
    #: Distance to back off after the setpoint is reached.
    retract_um: float = 0.500
    #: Hard budget for the retract, in case the surface moved.
    max_retract_um: float = 2.0

    #: Stop immediately if the amplitude exceeds this (probe crash guard).
    #: Set <= 0 to disable.  Must be above ``setpoint`` to be meaningful.
    safety_amplitude: float = 0.0

    #: Refuse to trust a reading below this signal-to-noise ratio.  Set <= 0 to
    #: disable.  A point that fails this is reported, not silently accepted.
    min_snr: float = 0.0

    #: Positioning tolerance passed to the stage.
    position_tolerance_um: float = 0.02
    settle_time_ms: int = 0
    settle_timeout_s: float = 10.0

    #: Belt and braces: never issue a Z outside this window when it is given.
    z_min_um: Optional[float] = None
    z_max_um: Optional[float] = None

    #: Safety valve against a runaway loop.
    max_steps: int = 4000


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
        ``() -> (amplitude, snr)``.  Must be cheap: it is called after every step.
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
        clock: Callable[[], float] = time.monotonic,
    ):
        self.stage = stage
        self._measure = measure
        self.config = config
        self._log = log or (lambda event, **fields: None)
        self._clock = clock
        self._sign = 1.0 if float(config.approach_sign) >= 0 else -1.0

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

    def _move_z(self, z):
        self.stage.move_axis("z", z)
        self.stage.wait_until_axis_settled(
            "z",
            z,
            settle_time_ms=self.config.settle_time_ms,
            tolerance=self.config.position_tolerance_um,
            timeout_s=self.config.settle_timeout_s,
        )
        return float(self.stage.get_axis_position("z"))

    # -- approach ----------------------------------------------------------- #

    def approach(self, z_from):
        """Creep Z from ``z_from`` until the amplitude reaches the setpoint.

        Returns an :class:`AfmApproachResult`.  ``ok`` is false when the travel
        budget, a Z limit, the crash guard or the step limit was hit; the caller
        decides whether that is a skipped point or a fatal error.
        """
        cfg = self.config
        started = self._clock()
        z = float(z_from)
        amplitude, snr = self.measure()

        trace = [{"step": 0, "z_um": round(z, 4), "amp": round(amplitude, 4)}]
        self._emit(
            "AFM_APPROACH_BEGIN",
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
            )

        if amplitude >= cfg.setpoint:
            self._emit("AFM_ALREADY_AT_SETPOINT", z_um="%.4f" % z, amplitude="%.4f" % amplitude)
            return AfmApproachResult(
                ok=True, z_um=z, amplitude=amplitude, steps=0,
                reason="already_at_setpoint", snr=snr,
                elapsed_s=self._clock() - started, trace=trace,
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
                    reason="travel_budget_exhausted",
                    travelled_um="%.4f" % travelled,
                    amplitude="%.4f" % amplitude,
                )
                return AfmApproachResult(
                    ok=False, z_um=z, amplitude=amplitude, steps=index - 1,
                    reason="travel_budget_exhausted", snr=snr,
                    previous_amplitude=previous_amplitude,
                    elapsed_s=self._clock() - started, trace=trace,
                )

            z_next = z + self._sign * step
            if not self._limits_allow(z_next):
                self._emit(
                    "AFM_APPROACH_FAILED",
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
                )

            actual = self._move_z(z_next)
            travelled += abs(actual - z)
            z = actual

            previous_amplitude = amplitude
            amplitude, snr = self.measure()
            trace.append({"step": index, "z_um": round(z, 4), "amp": round(amplitude, 4)})
            if len(trace) > 400:
                # Keep the log bounded on a long approach; the extremes are what matter.
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
                )

            if amplitude >= cfg.setpoint:
                self._emit(
                    "AFM_SETPOINT_REACHED",
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
                )

            # Switch to the fine step as soon as the signal is in the neighbourhood.
            if (
                step > float(cfg.fine_step_um) > 0
                and amplitude >= cfg.coarse_to_fine_ratio * cfg.setpoint
            ):
                step = float(cfg.fine_step_um)
                self._emit("AFM_STEP_COARSE_TO_FINE", step_um="%.3f" % step,
                           amplitude="%.4f" % amplitude)

        self._emit("AFM_APPROACH_FAILED", reason="max_steps", steps=cfg.max_steps)
        return AfmApproachResult(
            ok=False, z_um=z, amplitude=amplitude, steps=int(cfg.max_steps),
            reason="max_steps", snr=snr,
            previous_amplitude=previous_amplitude,
            elapsed_s=self._clock() - started, trace=trace,
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
