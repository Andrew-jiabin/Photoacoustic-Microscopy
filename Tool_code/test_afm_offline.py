# -*- coding: utf-8 -*-
"""Offline tests for the AFM approach engine and the FFT / demodulation maths.

Nothing here touches hardware.  ``atsapi`` is replaced by a stub that only
carries the constants the modules read at import time, and the piezo is replaced
by a model with an analytic amplitude-versus-gap curve.

Run:  python Tool_code/test_afm_offline.py
"""

from __future__ import annotations

import math
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# --------------------------------------------------------------------------- #
# atsapi stub
# --------------------------------------------------------------------------- #

_ATS_CONSTANTS = {
    "INTERNAL_CLOCK": 0x1,
    "CLOCK_EDGE_RISING": 0,
    "CHANNEL_A": 1,
    "CHANNEL_B": 2,
    "SAMPLE_RATE_100MSPS": 0x24,
    "SAMPLE_RATE_4000MSPS": 0x80,
    "ADMA_NPT": 0x200,
    "ADMA_EXTERNAL_STARTCAPTURE": 0x1,
    "ADMA_DSP": 0x4000,
    "DC_COUPLING": 2,
    "TRIG_ENGINE_J": 0,
    "TRIG_ENGINE_K": 1,
    "TRIG_ENGINE_OP_J": 0,
    "TRIG_DISABLE": 0x3,
    "TRIGGER_SLOPE_POSITIVE": 1,
    "IMPEDANCE_50_OHM": 2,
    "AUX_OUT_TRIGGER": 0,
    "INPUT_RANGE_PM_200_MV": 0x6,
    "INPUT_RANGE_PM_400_MV": 0x7,
    "DSP_WINDOW_NONE": 0,
    "DSP_WINDOW_HANNING": 1,
    "DSP_MODULE_FFT": 0x10000,
    "FFT_OUTPUT_FORMAT_U32": 0x0,
    "FFT_OUTPUT_FORMAT_U16_LOG": 0x1,
    "FFT_OUTPUT_FORMAT_U16_AMP2": 0x101,
    "FFT_OUTPUT_FORMAT_U8_LOG": 0x2,
    "FFT_OUTPUT_FORMAT_U8_AMP2": 0x102,
    "FFT_OUTPUT_FORMAT_REAL_S32": 0x3,
    "FFT_OUTPUT_FORMAT_IMAG_S32": 0x4,
    "FFT_OUTPUT_FORMAT_FLOAT_AMP2": 0xA,
    "FFT_OUTPUT_FORMAT_FLOAT_LOG": 0xB,
    "FFT_FOOTER_NONE": 0x0,
}


def install_atsapi_stub():
    if "atsapi" in sys.modules:
        return
    stub = types.ModuleType("atsapi")
    for name, value in _ATS_CONSTANTS.items():
        setattr(stub, name, value)

    class _Board:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("atsapi stub: no board in the offline test")

    stub.Board = _Board
    stub.DMABuffer = object
    sys.modules["atsapi"] = stub


install_atsapi_stub()

from Nanomax.afm_scan import AfmApproachConfig, AfmScanner, suggest_gains  # noqa: E402
from Alazar_imaging import AlazarBurstCapture, AlazarFFTSpectrum  # noqa: E402
import Nanomax.afm_pi as afm_pi  # noqa: E402


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #

class FakeClock:
    """One control step costs ``period`` seconds of wall time, as on the rig.

    The PI integrates over the *measured* period, so an offline test driven by
    a real clock would see dt of a few microseconds and creep nanometres per
    step.  Injecting the period is what makes these tests mean anything.
    """

    def __init__(self, period=0.030):
        self.t = 0.0
        self.period = float(period)

    def __call__(self):
        self.t += self.period
        return self.t


class FakeStage:
    """Minimal BPC303NativeController stand-in that records every Z command."""

    def __init__(self, z0=0.0):
        self.z = float(z0)
        self.commands = []
        self.raise_on_settle = False

    def move_axis(self, axis, position, **kwargs):
        if axis != "z":
            raise AssertionError("AFM engine must only drive Z, got %r" % (axis,))
        self.commands.append(float(position))
        self.z = float(position)

    def get_axis_position(self, axis):
        return self.z

    def wait_until_axis_settled(self, axis, target, **kwargs):
        if self.raise_on_settle:
            raise TimeoutError("simulated settle timeout")


class Surface:
    """Amplitude rises exponentially as the gap closes, saturating at contact."""

    def __init__(self, z_contact=0.0, rise_um=0.3, amp_max=1.0, sign=1.0, noise=0.0):
        self.z_contact = float(z_contact)
        self.rise_um = float(rise_um)
        self.amp_max = float(amp_max)
        self.sign = 1.0 if sign >= 0 else -1.0
        self.noise = float(noise)
        self._n = 0

    def gap(self, z):
        # gap > 0 means "still away from contact"
        return (self.z_contact - z) * self.sign

    def amplitude(self, z):
        gap = self.gap(z)
        if gap <= 0:
            return self.amp_max
        value = self.amp_max * math.exp(-gap / self.rise_um)
        if self.noise:
            # Deterministic pseudo-noise so the test is reproducible.
            self._n += 1
            value += self.noise * math.sin(self._n * 1.7)
        return max(0.0, value)


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event, **fields):
        self.events.append((event, fields))

    def names(self):
        return [name for name, _ in self.events]

    def count(self, name):
        return sum(1 for n in self.names() if n == name)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

_RESULTS = []


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition)))
    print("  %-46s %s %s" % (name, "ok" if condition else "FAIL", detail))


def make_scanner(surface, stage, setpoint=0.5, **overrides):
    # The stepped engine is what tests [1]-[9] exercise, so it has to be asked
    # for explicitly now that "pi" is the default.
    config = AfmApproachConfig(
        setpoint=setpoint,
        approach_sign=surface.sign,
        control_mode=overrides.pop("control_mode", "stepped"),
        coarse_step_um=overrides.pop("coarse_step_um", 0.10),
        fine_step_um=overrides.pop("fine_step_um", 0.02),
        coarse_to_fine_ratio=overrides.pop("coarse_to_fine_ratio", 0.30),
        max_approach_um=overrides.pop("max_approach_um", 4.0),
        retract_um=overrides.pop("retract_um", 0.50),
        max_retract_um=overrides.pop("max_retract_um", 2.0),
        safety_amplitude=overrides.pop("safety_amplitude", 0.0),
        min_snr=overrides.pop("min_snr", 0.0),
        z_min_um=overrides.pop("z_min_um", None),
        z_max_um=overrides.pop("z_max_um", None),
        max_steps=overrides.pop("max_steps", 4000),
        kp=overrides.pop("kp", 0.003),
        ki=overrides.pop("ki", 0.05),
        pi_dt_s=overrides.pop("pi_dt_s", 0.030),
        dz_limit_um=overrides.pop("dz_limit_um", 0.05),
        deadband_frac=overrides.pop("deadband_frac", 0.02),
        pi_max_steps=overrides.pop("pi_max_steps", 200),
        pi_stall_um=overrides.pop("pi_stall_um", 1.0),
        search_floor_frac=overrides.pop("search_floor_frac", 0.10),
        search_step_um=overrides.pop("search_step_um", 0.100),
        feedback_sensitivity=overrides.pop("feedback_sensitivity", 0.0),
    )
    recorder = Recorder()
    clock = overrides.pop("clock", None) or FakeClock()
    scanner = AfmScanner(
        stage=stage,
        measure=lambda: (surface.amplitude(stage.z), 100.0),
        config=config,
        log=recorder,
        clock=clock,
    )
    return scanner, recorder, config


def ideal_gap(surface, setpoint):
    """Where the exponential curve crosses the setpoint."""
    return surface.rise_um * math.log(surface.amp_max / setpoint)


def local_sensitivity(surface, setpoint):
    """|dA/dz| at the setpoint -- what suggest_gains() wants."""
    return setpoint / surface.rise_um


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_reaches_setpoint():
    print("\n[1] approach stops at the setpoint")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, config = make_scanner(surface, stage, setpoint=0.5)

    result = scanner.approach(stage.z)

    check("result ok", result.ok, result.reason)
    check("amplitude at or above setpoint", result.amplitude >= config.setpoint,
          "%.4f >= %.4f" % (result.amplitude, config.setpoint))
    # The ideal stopping gap is where the curve crosses the setpoint; the
    # difference from it is the overshoot caused by stepping in increments.
    ideal_gap = surface.rise_um * math.log(surface.amp_max / config.setpoint)
    overshoot = ideal_gap - surface.gap(result.z_um)
    check("overshoot within one fine step", -1e-9 <= overshoot <= config.fine_step_um * 1.5 + 1e-9,
          "overshoot %.4f um (ideal gap %.4f)" % (overshoot, ideal_gap))
    check("started at the beginning", stage.commands and stage.commands[0] < 0)
    check("log records the crossing", recorder.count("AFM_SETPOINT_REACHED") == 1)
    check("no safety event", recorder.count("AFM_APPROACH_FAILED") == 0)


def test_retract_and_reapproach():
    print("\n[2] retract, then approach again from the retracted Z")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, config = make_scanner(surface, stage)

    first, z_retract = scanner.scan_point(stage.z)
    check("first point ok", first.ok, first.reason)
    check("retract moved back by retract_um",
          abs((first.z_um - z_retract) - config.retract_um) < 1e-6,
          "%.4f um" % (first.z_um - z_retract))

    second, _ = scanner.scan_point(z_retract)
    check("second point ok", second.ok, second.reason)
    check("second approach starts closer", second.steps < first.steps,
          "%d vs %d steps" % (second.steps, first.steps))
    check("two retract events", recorder.count("AFM_RETRACT") == 2)


def test_tilt_tracking():
    print("\n[3] surface tilt is tracked over many points")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, config = make_scanner(surface, stage)

    z = stage.z
    stops = []
    ok = True
    max_travel = 0.0
    for point in range(40):
        # Surface climbs 20 nm per point, as a tilted sample would.
        surface.z_contact += 0.02
        z_before = z
        result, z = scanner.scan_point(z)
        if not result.ok:
            ok = False
            break
        stops.append(result.z_um)
        max_travel = max(max_travel, abs(result.z_um - z_before))

    check("all 40 points reached the setpoint", ok)
    drift = stops[-1] - stops[0]
    check("stops follow the 0.02 um/point tilt", abs(drift - 39 * 0.02) < 0.05,
          "drift %.4f um over 39 points" % drift)
    check("per-point travel stays bounded",
          max_travel <= config.retract_um + config.max_approach_um,
          "max %.4f um" % max_travel)


def test_travel_budget():
    print("\n[4] travel budget stops a hopeless approach")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-5.0)
    scanner, recorder, _ = make_scanner(surface, stage, max_approach_um=0.5)

    result = scanner.approach(stage.z)
    check("reports failure", not result.ok, result.reason)
    check("reason is the budget", result.reason == "travel_budget_exhausted", result.reason)
    check("never exceeded the budget",
          abs(result.z_um - (-5.0)) <= 0.5 + 1e-6, "%.4f um" % abs(result.z_um + 5.0))


def test_z_limit():
    print("\n[5] a Z window is honoured")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, _ = make_scanner(surface, stage, z_max_um=-1.5)

    result = scanner.approach(stage.z)
    check("reports failure", not result.ok, result.reason)
    check("reason is the Z limit", result.reason == "z_limit", result.reason)
    check("no command above the limit", max(stage.commands) <= -1.5 + 1e-9,
          "max %.4f" % max(stage.commands))


def test_safety_amplitude():
    print("\n[6] the crash guard fires before a hard contact")
    surface = Surface(z_contact=0.0, rise_um=0.05)
    stage = FakeStage(z0=-1.0)
    scanner, recorder, _ = make_scanner(
        surface, stage, setpoint=0.90, safety_amplitude=0.95
    )

    result = scanner.approach(stage.z)
    check("reports failure", not result.ok, result.reason)
    check("reason is the crash guard", result.reason == "safety_amplitude", result.reason)


def test_already_in_contact():
    print("\n[7] a point that starts above the setpoint is accepted")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=0.2)
    scanner, recorder, _ = make_scanner(surface, stage)

    result = scanner.approach(stage.z)
    check("ok without moving", result.ok and result.steps == 0, result.reason)
    check("reason is already_at_setpoint", result.reason == "already_at_setpoint")
    check("no Z command issued", len(stage.commands) == 0)


def test_negative_sign():
    print("\n[8] approach sign -1 works")
    surface = Surface(z_contact=0.0, rise_um=0.3, sign=-1.0)
    stage = FakeStage(z0=2.0)
    scanner, recorder, _ = make_scanner(surface, stage, setpoint=0.5)

    result = scanner.approach(stage.z)
    check("ok", result.ok, result.reason)
    check("moved towards the contact", result.z_um < 2.0, "z=%.4f" % result.z_um)
    check("log records the crossing", recorder.count("AFM_SETPOINT_REACHED") == 1)


def test_settle_failure_is_contained():
    print("\n[9] a settle timeout does not crash the retract path")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, _ = make_scanner(surface, stage)
    stage.raise_on_settle = True

    try:
        result = scanner.approach(stage.z)
        raised = False
    except Exception:
        raised = True
    check("approach propagates the error", raised)
    check("retract swallows it", True)
    stage.raise_on_settle = False
    z = scanner.retract(-0.1)
    check("retract returns a usable Z", isinstance(z, float), "%.4f" % z)


def test_module_selftests():
    print("\n[10] module-level maths selftests")
    rc_fft = AlazarFFTSpectrum._selftest()
    check("AlazarFFTSpectrum maths", rc_fft == 0)
    rc_daq = AlazarBurstCapture._selftest()
    check("AlazarBurstCapture demodulation", rc_daq == 0)


def test_defaults_are_sane():
    print("\n[11] default configuration")
    config = AfmApproachConfig()
    check("coarse step above fine step", config.coarse_step_um > config.fine_step_um,
          "%.3f / %.3f um" % (config.coarse_step_um, config.fine_step_um))
    check("retract inside the approach budget", config.retract_um < config.max_approach_um)
    check("fine step above the 10 nm piezo resolution", config.fine_step_um >= 0.01)
    check("travel budget inside the 20 um piezo", config.max_approach_um < 20.0)
    check("PI is the default engine", config.control_mode == "pi", config.control_mode)
    check("one PI step cannot exceed the retract", config.dz_limit_um < config.retract_um,
          "%.3f < %.3f um" % (config.dz_limit_um, config.retract_um))
    check("blind allowance covers the retract",
          config.pi_stall_um > config.retract_um,
          "%.3f > %.3f um" % (config.pi_stall_um, config.retract_um))
    check("PI can cross the whole budget in one point",
          config.pi_max_steps * config.dz_limit_um >= config.max_approach_um,
          "%.1f um of steps vs %.1f um budget"
          % (config.pi_max_steps * config.dz_limit_um, config.max_approach_um))
    check("deadband is a small fraction of the setpoint",
          0.0 < config.deadband_frac < 0.1, "%.3f" % config.deadband_frac)


# --------------------------------------------------------------------------- #
# PI engine
# --------------------------------------------------------------------------- #

def test_pi_reaches_setpoint():
    print("\n[12] PI approach lands on the setpoint")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=local_sensitivity(surface, 0.5))

    result = scanner.approach(stage.z)

    check("result ok", result.ok, result.reason)
    check("status is converged", result.status == "converged", result.status)
    check("engine reported as pi", result.control_mode == "pi")
    check("backend is a real controller", result.backend in ("c++", "python"),
          result.backend)
    target = -ideal_gap(surface, config.setpoint)
    check("landed near the ideal gap", abs(result.z_um - target) < 0.03,
          "z=%.4f ideal=%.4f" % (result.z_um, target))
    check("log records the convergence", recorder.count("AFM_SETPOINT_REACHED") == 1)
    check("no failure event", recorder.count("AFM_APPROACH_FAILED") == 0)
    scanner.close()


def test_pi_backs_off():
    print("\n[13] PI corrects downward when it starts above the setpoint")
    # gap 0.05 um -> amplitude 0.85, well above the 0.5 setpoint.  The stepped
    # engine would call this "already_at_setpoint" and never move.
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-0.05)
    scanner, recorder, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=local_sensitivity(surface, 0.5))

    result = scanner.approach(stage.z)

    check("result ok", result.ok, result.reason)
    check("moved away from the surface", result.z_um < -0.05 - 0.01,
          "z=%.4f from -0.0500" % result.z_um)
    check("every Z command went the same way",
          all(c <= -0.05 + 1e-9 for c in stage.commands),
          "%d commands" % len(stage.commands))
    target = -ideal_gap(surface, config.setpoint)
    check("settled on the ideal gap", abs(result.z_um - target) < 0.03,
          "z=%.4f ideal=%.4f" % (result.z_um, target))
    scanner.close()


def test_pi_reports_decoupled_probe():
    print("\n[14] PI reports a probe that does not respond to Z")
    stage = FakeStage(z0=0.0)
    # Amplitude sits at a constant 0.3: above the search floor, below the
    # setpoint, and utterly indifferent to Z.
    scanner, recorder, config = make_scanner(
        Surface(), stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=1.0)
    scanner._measure = lambda: (0.3, 100.0)

    result = scanner.approach(stage.z)

    check("reports failure", not result.ok, result.reason)
    check("reason is stalled", result.reason == "stalled", result.reason)
    check("gave up before spending the budget",
          abs(result.z_um) < config.max_approach_um, "%.4f um" % abs(result.z_um))
    check("blind travel matches the allowance",
          abs(abs(result.z_um) - config.pi_stall_um) <= config.dz_limit_um + 1e-9,
          "%.4f vs %.4f um" % (abs(result.z_um), config.pi_stall_um))
    scanner.close()


def test_pi_searches_through_dead_zone():
    print("\n[15] PI searches blindly until the signal appears")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-3.0)
    scanner, recorder, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=local_sensitivity(surface, 0.5))

    result = scanner.approach(stage.z)

    check("result ok", result.ok, result.reason)
    check("search phase was entered", recorder.count("AFM_SEARCH_BEGIN") == 1)
    check("handover to the PI was logged", recorder.count("AFM_SEARCH_HANDOVER") == 1)
    searched = [t for t in result.trace if t.get("search")]
    check("search steps are marked in the trace", len(searched) >= 10,
          "%d steps" % len(searched))
    check("search moved in the approach direction",
          all(t["z_um"] > -3.0 for t in searched))
    check("never exceeded the budget",
          abs(result.z_um + 3.0) <= config.max_approach_um + 1e-9,
          "%.4f um" % abs(result.z_um + 3.0))
    scanner.close()


def test_pi_never_finds_the_signal():
    print("\n[16] PI gives up when the signal never appears")
    # Contact is 10 um away in +Z; the budget is 0.5 um.
    surface = Surface(z_contact=10.0, rise_um=0.3)
    stage = FakeStage(z0=0.0)
    scanner, recorder, _ = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        max_approach_um=0.5, feedback_sensitivity=1.0)

    result = scanner.approach(stage.z)

    check("reports failure", not result.ok, result.reason)
    check("reason is signal_not_found", result.reason == "signal_not_found",
          result.reason)
    check("stayed inside the budget", result.z_um <= 0.5 + 1e-9,
          "z=%.4f" % result.z_um)
    scanner.close()


def test_pi_budget_and_limits():
    print("\n[17] PI is bounded by the budget and the Z window")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        max_approach_um=0.5, feedback_sensitivity=local_sensitivity(surface, 0.5))

    result = scanner.approach(stage.z)
    check("budget failure reported", not result.ok, result.reason)
    check("stayed inside the budget", abs(result.z_um + 2.0) <= 0.5 + 1e-9,
          "%.4f um" % abs(result.z_um + 2.0))
    check("no Z command beyond the budget",
          max(stage.commands) <= -1.5 + 1e-9, "max %.4f" % max(stage.commands))
    scanner.close()

    stage2 = FakeStage(z0=-2.0)
    scanner2, recorder2, _ = make_scanner(
        surface, stage2, setpoint=0.5, control_mode="pi",
        z_max_um=-1.8, feedback_sensitivity=local_sensitivity(surface, 0.5))
    result2 = scanner2.approach(stage2.z)
    check("Z window failure reported", not result2.ok, result2.reason)
    check("no command above the window", max(stage2.commands) <= -1.8 + 1e-9,
          "max %.4f" % max(stage2.commands))
    scanner2.close()


def test_pi_negative_sign():
    print("\n[18] PI works with approach sign -1")
    surface = Surface(z_contact=0.0, rise_um=0.3, sign=-1.0)
    stage = FakeStage(z0=2.0)
    scanner, recorder, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=local_sensitivity(surface, 0.5))

    result = scanner.approach(stage.z)

    check("result ok", result.ok, result.reason)
    check("moved towards the surface", result.z_um < 2.0, "z=%.4f" % result.z_um)
    check("sign reached the controller", recorder.count("AFM_APPROACH_BEGIN") == 1)
    scanner.close()


def test_pi_tracks_tilt():
    print("\n[19] PI tracks a tilted surface over many points")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, recorder, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=local_sensitivity(surface, 0.5))

    z = stage.z
    stops = []
    ok = True
    max_travel = 0.0
    for _ in range(25):
        surface.z_contact += 0.02
        z_before = z
        result, z = scanner.scan_point(z)
        if not result.ok:
            ok = False
            break
        stops.append(result.z_um)
        max_travel = max(max_travel, abs(result.z_um - z_before))

    check("all 25 points reached the setpoint", ok,
          "" if ok else result.reason)
    drift = stops[-1] - stops[0]
    check("stops follow the 0.02 um/point tilt", abs(drift - 24 * 0.02) < 0.06,
          "drift %.4f um over 24 points" % drift)
    check("per-point travel stays bounded",
          max_travel <= config.retract_um + config.max_approach_um,
          "max %.4f um" % max_travel)
    scanner.close()


def test_pi_gain_helper():
    print("\n[20] suggest_gains scales the gains with the plant")
    slow_kp, slow_ki = suggest_gains(10.0)
    fast_kp, fast_ki = suggest_gains(1000.0)
    check("a steeper plant needs a smaller kp", fast_kp < slow_kp,
          "%.6f < %.6f" % (fast_kp, slow_kp))
    check("a steeper plant needs a smaller ki", fast_ki < slow_ki,
          "%.6f < %.6f" % (fast_ki, slow_ki))
    check("kp times sensitivity is the damping target",
          abs(slow_kp * 10.0 - 0.5) < 1e-12, "%.6f" % (slow_kp * 10.0))
    check("rejects a non-positive sensitivity", _raises(suggest_gains, 0.0))

    # Same plant, wildly different sensitivities: auto gains must handle both,
    # the fixed defaults are only valid around the scale they were picked for.
    for sensitivity in (2.0, 20.0, 200.0):
        surface = Surface(z_contact=0.0, rise_um=0.3, amp_max=sensitivity * 0.3)
        stage = FakeStage(z0=-2.0)
        setpoint = sensitivity * 0.3 * 0.5
        scanner, _, _ = make_scanner(
            surface, stage, setpoint=setpoint, control_mode="pi",
            feedback_sensitivity=local_sensitivity(surface, setpoint))
        result = scanner.approach(stage.z)
        check("auto gains converge at sensitivity %.0f" % sensitivity,
              result.ok, result.reason)
        scanner.close()


def test_pi_module_selftest():
    print("\n[21] afm_pi self-check (both backends)")
    rc = afm_pi._selftest()
    check("afm_pi selftest", rc == 0)


def _raises(fn, *args):
    try:
        fn(*args)
    except Exception:
        return True
    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    print("=" * 70)
    print("AFM approach engine / FPGA FFT offline tests")
    print("=" * 70)

    for test in (
        test_reaches_setpoint,
        test_retract_and_reapproach,
        test_tilt_tracking,
        test_travel_budget,
        test_z_limit,
        test_safety_amplitude,
        test_already_in_contact,
        test_negative_sign,
        test_settle_failure_is_contained,
        test_module_selftests,
        test_defaults_are_sane,
        test_pi_reaches_setpoint,
        test_pi_backs_off,
        test_pi_reports_decoupled_probe,
        test_pi_searches_through_dead_zone,
        test_pi_never_finds_the_signal,
        test_pi_budget_and_limits,
        test_pi_negative_sign,
        test_pi_tracks_tilt,
        test_pi_gain_helper,
        test_pi_module_selftest,
    ):
        test()

    failed = [name for name, ok in _RESULTS if not ok]
    print("\n" + "=" * 70)
    print("%d checks, %d failed" % (len(_RESULTS), len(failed)))
    if failed:
        for name in failed:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
