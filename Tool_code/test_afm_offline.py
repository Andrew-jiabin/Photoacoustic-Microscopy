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

from Nanomax.afm_scan import (  # noqa: E402
    AfmApproachConfig,
    AfmScanner,
    check_config,
    suggest_gains,
)
from Nanomax import afm_control, afm_retract  # noqa: E402
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
        # speed / retract controls
        approach_speed_um_s=overrides.pop("approach_speed_um_s", 1.0),
        final_speed_um_s=overrides.pop("final_speed_um_s", 0.4),
        final_band_frac=overrides.pop("final_band_frac", 0.15),
        max_approach_s=overrides.pop("max_approach_s", 15.0),
        search_speed_um_s=overrides.pop("search_speed_um_s", 3.0),
        retract_policy=overrides.pop("retract_policy", "auto"),
        retract_margin_um=overrides.pop("retract_margin_um", 0.05),
        retract_map=overrides.pop("retract_map", None),
    )
    recorder = Recorder()
    clock = overrides.pop("clock", None) or FakeClock()
    measure = overrides.pop("measure", None)
    if measure is None:
        measure = lambda: (surface.amplitude(stage.z), 100.0)  # noqa: E731
    scanner = AfmScanner(
        stage=stage,
        measure=measure,
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
    # Speed and retract defaults, now that both are first-class.
    check("default closing speed is cautious but not absurd",
          0.1 <= config.approach_speed_um_s <= 5.0,
          "%.2f um/s" % config.approach_speed_um_s)
    check("final band is slower than the general approach",
          0 < config.final_speed_um_s <= config.approach_speed_um_s,
          "%.2f <= %.2f um/s" % (config.final_speed_um_s, config.approach_speed_um_s))
    check("default retract policy is adaptive", config.retract_policy == "auto",
          config.retract_policy)
    check("default config has no warnings", not check_config(config),
          "; ".join(check_config(config)))


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
# The Z criterion is an interface
# --------------------------------------------------------------------------- #

def test_control_source_interface():
    print("\n[22] the Z criterion is a pluggable interface")

    # adopt() accepts all three shapes the code base actually uses.
    plain = afm_control.adopt(lambda: 2.0)
    check("adopts a bare callable", plain.value() == 2.0, "%.3f" % plain.value())

    class OnlyMeasure:
        unit = "V"

        def measure(self):
            return 3.0, 12.0

    adopted = afm_control.adopt(OnlyMeasure())
    check("adopts an object with measure()", adopted.value() == (3.0, 12.0))
    check("keeps the unit through adoption", adopted.unit == "V", adopted.unit)

    class Full:
        unit = "au"

        def value(self):
            return 4.0

    check("adopting a source is a no-op", afm_control.adopt(Full()).value() == 4.0)
    check("check_source accepts a valid source", afm_control.check_source(Full()) == [])
    check("check_source reports a missing value()",
          afm_control.check_source(object()) != [],
          "; ".join(afm_control.check_source(object())))

    # Adapters.
    const = afm_control.ConstantSource(1.25)
    check("constant source", const.value() == 1.25)
    scaled = afm_control.ScaledSource(afm_control.ConstantSource(2.0), gain=3.0, offset=1.0)
    check("scaled source applies gain and offset", scaled.value() == (7.0, 0.0),
          "%r" % (scaled.value(),))

    # EMA smooths, median rejects a spike.
    ema = afm_control.SmoothSource(afm_control.ConstantSource(0.0), alpha=1.0)
    check("alpha=1 passes the signal through", ema.value()[0] == 0.0)
    median = afm_control.SmoothSource(
        afm_control.CallableSource(lambda: next(feed)), mode="median", window=5)
    feed = iter([1.0, 1.0, 1.0, 1.0, 50.0])
    for _ in range(5):
        out = median.value()
    check("median rejects a single spike", abs(out[0] - 1.0) < 1e-9, "%.3f" % out[0])

    # "No reading" is a first-class answer, not a crash.
    check("None becomes NaN",
          math.isnan(afm_control.read_value(afm_control.CallableSource(lambda: None))[0]))
    check("NaN from a source becomes NaN",
          math.isnan(afm_control.read_value(
              afm_control.CallableSource(lambda: float("nan")))[0]))

    # Loading by spec.
    check("const spec", afm_control.load_source("const:2.5").value() == 2.5)
    check("'feedback' must be resolved by the caller",
          _raises(afm_control.load_source, "feedback"))

    import tempfile
    path = os.path.join(tempfile.gettempdir(), "_pam_afm_src_%d.py" % os.getpid())
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "UNIT = 'V'\n"
            "def create_source(config):\n"
            "    return lambda: 7.5\n"
        )
    try:
        loaded = afm_control.load_source("file:" + path)
        check("loads a user module from a file", loaded.value() == 7.5)
        check("describes the loaded source",
              "callable" in afm_control.describe_source(loaded),
              afm_control.describe_source(loaded))
    finally:
        os.remove(path)

    # A source that drops a reading must fail the point, not move on garbage.
    class Flaky:
        unit = "au"

        def __init__(self):
            self.n = 0

        def value(self):
            self.n += 1
            return None if self.n == 2 else 1.0

    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, _, _ = make_scanner(surface, stage, setpoint=0.5, control_mode="pi",
                                 measure=Flaky())
    result = scanner.approach(stage.z)
    check("a dropped reading stops the point", result.reason == "no_reading",
          result.reason)


# --------------------------------------------------------------------------- #
# Speed: careful on approach, fast on retract
# --------------------------------------------------------------------------- #

def test_approach_speed_is_a_rate():
    print("\n[23] closing speed is um/s, not um/step")
    surface = Surface(z_contact=0.0, rise_um=0.3)
    gains = local_sensitivity(surface, 0.5)

    for period in (0.030, 0.100):
        surface = Surface(z_contact=0.0, rise_um=0.3)
        stage = FakeStage(z0=-2.0)
        scanner, _, config = make_scanner(
            surface, stage, setpoint=0.5, control_mode="pi",
            feedback_sensitivity=gains, dz_limit_um=10.0,   # no absolute cap
            approach_speed_um_s=1.0, final_speed_um_s=0.0,
            # No blind search: this test is about the PI's rate, and the search
            # phase is deliberately faster (it is checked separately below).
            search_floor_frac=0.0,
            max_approach_s=0.0, clock=FakeClock(period))
        result = scanner.approach(stage.z)
        moves = [-2.0] + list(stage.commands)
        deltas = [abs(b - a) for a, b in zip(moves, moves[1:])]
        cap = 1.0 * period
        check("step <= %.4f um at a %.0f ms loop" % (cap, period * 1e3),
              result.ok and max(deltas) <= cap * 1.02,
              "max %.5f um, %d steps" % (max(deltas), len(deltas)))

    # The blind search may be quicker, but only up to search_speed_um_s.
    for period in (0.030, 0.100):
        surface = Surface(z_contact=0.0, rise_um=0.3)
        stage = FakeStage(z0=-2.0)
        scanner, _, config = make_scanner(
            surface, stage, setpoint=0.5, control_mode="pi",
            feedback_sensitivity=gains, dz_limit_um=10.0,
            approach_speed_um_s=1.0, final_speed_um_s=0.0,
            search_floor_frac=0.10, search_step_um=0.5,
            search_speed_um_s=3.0, max_approach_s=0.0, clock=FakeClock(period))
        result = scanner.approach(stage.z)
        search = [t for t in result.trace if t.get("search")]
        if search:
            zs = [search[0]["z_um"]]
            zs.extend(t["z_um"] for t in search[1:])
            deltas = [abs(b - a) for a, b in zip(zs, zs[1:])]
            cap = 3.0 * period
            check("search step <= %.4f um at a %.0f ms loop" % (cap, period * 1e3),
                  max(deltas) <= cap * 1.02,
                  "max %.5f um over %d steps" % (max(deltas), len(deltas)))
        else:
            check("search step capped at %.3f um/s" % 3.0, False, "no search phase ran")

    # The last band gets the slower rate.
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, _, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=gains, dz_limit_um=10.0,
        approach_speed_um_s=1.0, final_speed_um_s=0.4, final_band_frac=0.15,
        max_approach_s=0.0)
    result = scanner.approach(stage.z)
    last = abs(result.last_step_um)
    check("the final band is slower than the approach",
          result.ok and last <= 0.4 * 0.030 * 1.05,
          "last step %.5f um <= %.5f" % (last, 0.4 * 0.030))

    # A slow setting must still land, given enough steps.
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, _, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=gains, dz_limit_um=10.0,
        approach_speed_um_s=0.5, final_speed_um_s=0.0,
        pi_max_steps=400, max_approach_s=0.0)
    result = scanner.approach(stage.z)
    check("0.5 um/s still converges", result.ok, "%s in %d steps"
          % (result.reason, result.steps))

    # "Not too slow": the time budget is the bound a step count cannot give.
    surface = Surface(z_contact=0.0, rise_um=0.3)
    stage = FakeStage(z0=-2.0)
    scanner, _, config = make_scanner(
        surface, stage, setpoint=0.5, control_mode="pi",
        feedback_sensitivity=gains, dz_limit_um=10.0,
        approach_speed_um_s=0.05, final_speed_um_s=0.0,
        pi_max_steps=4000, max_approach_s=0.5)
    result = scanner.approach(stage.z)
    check("a crawl is cut off by the time budget", result.reason == "time_budget",
          "%s after %.1f s" % (result.reason, result.elapsed_s))

    # ... and the configuration check warns before the scan starts.
    slow = AfmApproachConfig(setpoint=0.5, approach_speed_um_s=0.001,
                             max_approach_um=4.0, pi_max_steps=200)
    problems = check_config(slow)
    check("an unusably slow speed is reported", len(problems) > 0,
          problems[0] if problems else "")
    fast = AfmApproachConfig(setpoint=0.5, approach_speed_um_s=2.0,
                             max_approach_um=4.0, pi_max_steps=200)
    check("a sane speed produces no warning", not check_config(fast),
          "; ".join(check_config(fast)))


# --------------------------------------------------------------------------- #
# Retract height
# --------------------------------------------------------------------------- #

class TwoLevelSurface:
    """Flat, then one step up at ``step_x`` -- a resolution target.

    ``height`` is *protrusion*: how much closer to the probe the surface is past
    the step.  With ``approach_sign = +1`` the Z at which contact happens is
    therefore ``-height``, which is the whole point -- a taller feature is met at
    a *lower* stage Z, so staying at the old Z means the probe is already inside
    the sample.
    """

    def __init__(self, step_x=2.0, height=1.5, rise_um=0.3, amp_max=1.0, sign=1.0):
        self.step_x = float(step_x)
        self.height = float(height)
        self.rise_um = float(rise_um)
        self.amp_max = float(amp_max)
        self.sign = 1.0 if sign >= 0 else -1.0
        self.x = 0.0

    def protrusion(self, x=None):
        x = self.x if x is None else float(x)
        return self.height if x >= self.step_x else 0.0

    def contact_z(self, x=None):
        return -self.sign * self.protrusion(x)

    def amplitude(self, z):
        gap = (self.contact_z() - z) * self.sign
        if gap <= 0:
            return self.amp_max
        return self.amp_max * math.exp(-gap / self.rise_um)


class DomeSurface:
    """A Gaussian bump -- a cell.

    ``height`` is the apex protrusion and ``sigma`` its 1/e radius, in the same
    *protrusion* convention as :class:`TwoLevelSurface`: the top of the bump is
    met at a *lower* stage Z, so staying put while XY moves onto the flank means
    the probe is already inside the sample.
    """

    def __init__(self, height=5.0, sigma=4.0, rise_um=0.3, amp_max=1.0, sign=1.0):
        self.height = float(height)
        self.sigma = float(sigma)
        self.rise_um = float(rise_um)
        self.amp_max = float(amp_max)
        self.sign = 1.0 if sign >= 0 else -1.0
        self.x = 0.0

    def protrusion(self, x=None):
        x = self.x if x is None else float(x)
        return self.height * math.exp(-0.5 * (x / self.sigma) ** 2)

    def contact_z(self, x=None):
        return -self.sign * self.protrusion(x)

    def amplitude(self, z):
        gap = (self.contact_z() - z) * self.sign
        if gap <= 0:
            return self.amp_max
        return self.amp_max * math.exp(-gap / self.rise_um)


def _scan_line(surface, policy, xs, retract_um=0.5, map_fn=None, **extra):
    """Walk a line of points the way run_scan() does; return (results, lifts)."""
    stage = FakeStage(z0=-1.0)
    surface.x = xs[0]
    config = AfmApproachConfig(
        setpoint=0.5, approach_sign=1.0, control_mode="pi",
        feedback_sensitivity=0.5 / surface.rise_um,
        max_approach_um=4.0, retract_um=retract_um, max_retract_um=2.0,
        retract_policy=policy, retract_map=map_fn,
        approach_speed_um_s=1.0, final_speed_um_s=0.0,
        pi_max_steps=300, max_approach_s=0.0,
        **extra
    )
    scanner = AfmScanner(stage, lambda: (surface.amplitude(stage.z), 50.0),
                         config, clock=FakeClock())
    results, lifts, cursors = [], [], []
    z_cursor = stage.z
    for i, x in enumerate(xs):
        surface.x = x
        next_xy = (xs[i + 1], 0.0) if i + 1 < len(xs) else None
        result, z_retract = scanner.scan_point(z_cursor, here_xy=(x, 0.0),
                                               next_xy=next_xy)
        results.append(result)
        lifts.append(abs(z_retract - result.z_um) if result.ok else 0.0)
        cursors.append(z_retract)
        z_cursor = z_retract
    scanner.close()
    return results, lifts, cursors


def test_retract_policies():
    print("\n[24] retract height follows the surface, not a constant")

    # A two-level target: only the point that climbs needs the step height.
    xs = [0.0, 1.0, 2.0, 3.0, 4.0]
    surface = TwoLevelSurface(step_x=2.0, height=1.5)
    height_map = lambda x, y: 1.5 if x >= 2.0 else 0.0  # noqa: E731

    results, lifts, cursors = _scan_line(surface, "map", xs, retract_um=0.5,
                                        map_fn=height_map)
    check("all points reached the setpoint", all(r.ok for r in results),
          ",".join(r.reason for r in results))
    # Point x=1 is followed by x=2, which is 1.5 um higher.
    check("the climbing point lifts by the step height",
          lifts[1] >= 1.5, "lift %.3f um at x=1" % lifts[1])
    check("flat points keep the minimum lift", abs(lifts[0] - 0.5) < 1e-6,
          "lift %.3f um at x=0" % lifts[0])
    # The real property: at the moment XY moves, Z clears the next surface.
    clears = []
    for i, z_retract in enumerate(cursors[:-1]):
        clears.append(z_retract <= surface.contact_z(xs[i + 1]) - 0.05 + 1e-9)
    check("Z clears the next point before the XY move", all(clears),
          "%d/%d clear" % (sum(clears), len(clears)))

    # Without a map, a fixed retract cannot clear the step -- that is exactly
    # why the policy exists.
    results_fixed, lifts_fixed, cursors_fixed = _scan_line(surface, "fixed", xs,
                                                           retract_um=0.5)
    fixed_clears = [cursors_fixed[i] <= surface.contact_z(xs[i + 1]) - 0.05 + 1e-9
                    for i in range(len(cursors_fixed) - 1)]
    check("a fixed retract does NOT clear the step",
          not all(fixed_clears), "%d/%d clear" % (sum(fixed_clears), len(fixed_clears)))

    # A running max remembers the high level and lifts from the low one.
    policy = afm_retract.RunningMaxLift(margin_um=0.05, floor_um=0.5, max_um=2.0)
    policy.observe(0.0)
    policy.observe(1.5)          # the high level has been visited
    lift_low = policy.lift_um(0.0, (0.0, 0.0), (1.0, 0.0), [], 1.0)
    check("running max lifts a low point clear of the high level",
          abs(lift_low - 1.55) < 1e-6, "%.3f um" % lift_low)
    check("running max forgets on reset",
          policy.reset() is None and
          abs(policy.lift_um(0.0, (0, 0), (1, 0), [], 1.0) - 0.5) < 1e-6,
          "%.3f um after reset" % policy.lift_um(0.0, (0, 0), (1, 0), [], 1.0))

    # A dome: the slope policy lifts more at the rim than on top, with no map.
    slope = afm_retract.SlopeLift(margin_um=0.05, floor_um=0.0, window=5, safety=2.0)
    radius = 3.0
    h = lambda x: math.sqrt(max(0.0, radius * radius - x * x))  # noqa: E731
    flank_history = [(-3.0, 0.0, h(-3.0)), (-2.5, 0.0, h(-2.5)),
                     (-2.0, 0.0, h(-2.0))]
    lift_flank = slope.lift_um(h(-2.0), (-2.0, 0.0), (-1.5, 0.0), flank_history, 1.0)
    top_history = [(-1.0, 0.0, h(-1.0)), (-0.5, 0.0, h(-0.5))]
    lift_top = slope.lift_um(h(0.0), (0.0, 0.0), (0.5, 0.0), top_history, 1.0)
    check("the slope policy lifts more at the rim than on top",
          lift_flank > 2.0 * lift_top,
          "rim %.3f um vs top %.3f um" % (lift_flank, lift_top))

    # Composite never returns less than its parts.
    composite = afm_retract.make_policy("auto", base_um=0.5, margin_um=0.05, max_um=2.0)
    check("auto is at least the fixed floor",
          composite.lift_um(0.0, (0, 0), (1, 0), [], 1.0) >= 0.5,
          "%.3f um" % composite.lift_um(0.0, (0, 0), (1, 0), [], 1.0))
    check("auto describes its parts", "max of" in composite.describe(),
          composite.describe())

    # The map loader: a CSV grid, which is what a profilometer dump looks like.
    import tempfile
    csv_path = os.path.join(tempfile.gettempdir(), "_pam_afm_map_%d.csv" % os.getpid())
    with open(csv_path, "w", encoding="utf-8") as handle:
        handle.write("y/x,0,1,2\n0,0.0,0.0,1.5\n")
    try:
        loaded = afm_retract.csv_map(csv_path)
        check("csv map reads a grid", loaded(2.0, 0.0) == 1.5 and loaded(1.0, 0.0) == 0.0,
              "%.2f / %.2f" % (loaded(2.0, 0.0), loaded(1.0, 0.0)))
        built = afm_retract.make_map_policy("file:" + csv_path, base_um=0.5,
                                            margin_um=0.05, max_um=2.0)
        check("make_map_policy lifts over the step",
              abs(built.lift_um(0.0, (1.0, 0.0), (2.0, 0.0), [], 1.0) - 1.55) < 1e-6,
              "%.3f um" % built.lift_um(0.0, (1.0, 0.0), (2.0, 0.0), [], 1.0))
    finally:
        os.remove(csv_path)

    # A policy that raises must not stop the scan.
    class Broken(afm_retract.RetractPolicy):
        name = "broken"

        def lift_um(self, *args, **kwargs):
            raise RuntimeError("policy exploded")

    stage = FakeStage(z0=-1.0)
    surface = Surface(z_contact=0.0, rise_um=0.3)
    config = AfmApproachConfig(setpoint=0.5, control_mode="pi",
                               feedback_sensitivity=0.5 / 0.3,
                               retract_policy="fixed", retract_um=0.5)
    scanner = AfmScanner(stage, lambda: (surface.amplitude(stage.z), 50.0), config,
                         clock=FakeClock())
    scanner._policy = Broken()
    result, z_retract = scanner.scan_point(-1.0, here_xy=(0.0, 0.0),
                                           next_xy=(1.0, 0.0))
    check("a broken policy falls back to the minimum lift",
          result.ok and abs(z_retract - (result.z_um - 0.5)) < 1e-6,
          "%.4f -> %.4f" % (result.z_um, z_retract))
    scanner.close()


# --------------------------------------------------------------------------- #
# The two sample shapes the user actually scans
# --------------------------------------------------------------------------- #

def test_two_sample_archetypes():
    print("\n[25] a cell dome and a two-level target need different answers")

    # A cell: 5 um high, 1/e radius 4 um, scanned on a 2 um pitch.  The model
    # does not simulate a crash -- it measures whether Z was already inside the
    # *next* point at the moment XY moved, which is the event that breaks a
    # probe.
    dome = DomeSurface(height=5.0, sigma=4.0)
    xs = [float(v) for v in range(-12, 14, 2)]      # 13 points, -12 .. +12

    rises = [dome.protrusion(xs[i + 1]) - dome.protrusion(xs[i])
             for i in range(len(xs) - 1)]
    worst = max(rises)
    check("the dome climbs %.2f um between neighbouring points" % worst,
          1.0 < worst < 2.0, "rises %s" % " ".join("%.2f" % r for r in rises))

    # The conservative analytic bound is the number a user should feed to
    # retract_um: it assumes the steepest part of the bump falls between two
    # sample points, so it is never optimistic.
    bound = afm_retract.dome_rise(5.0, 4.0, 2.0)
    check("dome_rise bounds the sampled climb", bound >= worst - 1e-9,
          "bound %.3f um vs sampled %.3f um" % (bound, worst))
    check("dome_rise rejects a zero radius",
          _raises(afm_retract.dome_rise, 5.0, 0.0, 2.0))
    check("dome_rise rejects a zero pitch",
          _raises(afm_retract.dome_rise, 5.0, 4.0, 0.0))

    def _misses(cursors, surface, xs_line):
        """Indices where Z was NOT clear of the next point when XY moved."""
        return [i for i, z in enumerate(cursors[:-1])
                if not (z <= surface.contact_z(xs_line[i + 1]) - 0.05 + 1e-9)]

    def _run(policy, surface, xs_line, retract_um=0.5, map_fn=None):
        results, _lifts, cursors = _scan_line(surface, policy, xs_line,
                                              retract_um=retract_um,
                                              map_fn=map_fn)
        return results, _misses(cursors, surface, xs_line)

    # 1. The old constant: fine on a flat surface, blind on a dome.
    results_fixed, miss_fixed = _run("fixed", dome, xs, retract_um=0.5)
    check("every dome point still reaches the setpoint", all(r.ok for r in results_fixed),
          ",".join(r.reason for r in results_fixed))
    check("a fixed 0.5 um lift does not clear a cell dome",
          len(miss_fixed) >= 3,
          "%d/%d moves inside the sample: %s"
          % (len(miss_fixed), len(xs) - 1, miss_fixed))

    # 2. The reactive slope policy: on a convex flank the least-squares slope
    #    underestimates the *next* rise, so the lift is systematically one point
    #    late.  It must not be sold as a cure -- only lookahead is.
    _results_slope, miss_slope = _run("slope", dome, xs, retract_um=0.5)
    check("the slope policy still leaves points inside the sample",
          len(miss_slope) >= 1,
          "%d still inside: %s" % (len(miss_slope), miss_slope))
    check("its misses are all on the ascending flank",
          all(xs[i] < 0.0 for i in miss_slope),
          "misses at x = %s" % [xs[i] for i in miss_slope])

    # 3. A map: exact, because the lift is computed from the *next* point.
    results_map, miss_map = _run("map", dome, xs, retract_um=0.5,
                                 map_fn=lambda x, _y: dome.protrusion(x))
    check("a height map clears every move over the dome",
          all(r.ok for r in results_map) and not miss_map,
          "%d misses" % len(miss_map))

    # 4. No map, but retract_um set from dome_rise: also clears, because the
    #    lift is now bigger than the worst climb the surface can present.
    _results_roomy, miss_roomy = _run("fixed", dome, xs, retract_um=bound + 0.05)
    check("retract_um from dome_rise clears every move",
          not miss_roomy, "%d misses" % len(miss_roomy))

    # 5. The other archetype.  A two-level target is a step, not a flank: a
    #    slope policy never sees it coming, and the only inputs that work are
    #    the step height (known) or a map.
    step = TwoLevelSurface(step_x=2.0, height=1.5)
    xs_step = [0.0, 1.0, 2.0, 3.0, 4.0]
    _results_step_slope, miss_step_slope = _run("slope", step, xs_step, retract_um=0.5)
    check("the slope policy cannot clear a step", len(miss_step_slope) >= 1,
          "%d/%d inside" % (len(miss_step_slope), len(xs_step) - 1))
    _results_step_tall, miss_step_tall = _run("fixed", step, xs_step, retract_um=1.55)
    check("a lift equal to the step height clears a two-level target",
          not miss_step_tall, "%d misses" % len(miss_step_tall))

    # 6. auto is the default.  It beats a fixed lift only because running_max
    #    remembers the apex for the descent; it still cannot see the ascent.
    _results_auto, miss_auto = _run("auto", dome, xs, retract_um=0.5)
    check("auto beats a fixed lift on the dome",
          len(miss_auto) < len(miss_fixed),
          "auto %d vs fixed %d" % (len(miss_auto), len(miss_fixed)))
    check("but auto still leaves the first steep step exposed",
          len(miss_auto) >= 1, "%d still inside" % len(miss_auto))


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
        test_control_source_interface,
        test_approach_speed_is_a_rate,
        test_retract_policies,
        test_two_sample_archetypes,
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
