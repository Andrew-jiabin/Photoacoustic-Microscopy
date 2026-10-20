# -*- coding: utf-8 -*-
"""AFM-style approach scan on the closed-loop sample NanoMax (MAX311D / BPC303).

What this program does that the raster programs do not
------------------------------------------------------
The raster entry points keep Z fixed and trust that the sample stays in the
focal plane.  This one treats Z as the measurement: at every XY point the sample
is driven until the feedback signal reaches a setpoint, the A-line is taken
there, and the sample is backed off before moving on.  That is contact-mode AFM
with an acoustic setpoint, and it gives two extra channels per pixel for free --
the Z at which the surface was found, and the value at that Z.

The Z loop is a digital PI in velocity form, compiled in C++ (``afm_pi.cpp``,
loaded through ctypes, with a bit-exact Python reference as a fallback).  It
tracks a setpoint rather than creeping until a threshold is crossed, so it does
not care whether the signal rises or falls with Z, and it corrects in either
direction.  ``PAM_AFM_CONTROL_MODE=stepped`` restores the old one-way creep.

Three things are deliberately interfaces rather than constants
--------------------------------------------------------------
``PAM_AFM_CONTROL_SOURCE``
    What the Z decision is based on.  Default is the digitizer amplitude; any
    module implementing ``value()`` can replace it (``Nanomax.afm_control``), so
    a processed signal from another instrument can drive Z without touching the
    scan engine.

``PAM_AFM_APPROACH_SPEED_UM_S`` / ``PAM_AFM_FINAL_SPEED_UM_S``
    Closing speed in **um/s**, converted to a per-step distance with the
    measured loop period, so the rate is the same at any host speed.  The last
    ``PAM_AFM_FINAL_BAND_FRAC`` of the error is approached more slowly.  Moving
    *away* from the probe is a single command at full stage speed: it is the one
    motion that cannot break anything.

``PAM_AFM_RETRACT_POLICY`` / ``PAM_AFM_RETRACT_MAP``
    How far to lift before the XY move (``Nanomax.afm_retract``).  A cell is a
    dome and a resolution target is two levels, so one constant cannot be right
    for both: ``auto`` takes the max of the fixed floor, a running maximum and
    the local slope; ``map`` uses a supplied height map and is the only one that
    clears a step before stepping onto it.

Why a separate entry point
--------------------------
``PAM_Main_Nanomax_ClosedLoop.py`` is a 1100-line raster state machine whose
whole structure assumes "move XY, wait, acquire N triggered records".  Folding an
approach/retract loop into it would route every ordinary imaging run through
AFM code paths.  This program reuses the same building blocks instead -- the
BPC303 controller, ``Nanomax.scan_utils`` for geometry, ``Nanomax.data_io`` for
the ``.mat`` contract, ``Nanomax.run_log`` for events -- so a file written here
is read by ``MATLAB/PAM_Reconstruction_4.m`` without any change: one ``(1, 4096)``
uint16 variable per pixel plus a ``metadata`` struct.

Two feedback paths
------------------
``PAM_AFM_FEEDBACK=demod`` (default)
    4 GS/s, 4096-sample records, 10 MHz extracted by Hann-windowed quadrature
    demodulation.  Same sample rate and same records as the point payload, so
    the board is configured once.  Reports **volts**, calibrated from the ADC
    range.  Frequency selectivity is only ~3.9 MHz (the Hann main lobe of a
    1.024 us record).

``PAM_AFM_FEEDBACK=fft``
    100 MS/s, on-FPGA 4096-point FFT (see ``AlazarFFTSpectrum``).  Reports
    **board units** but with ~98 kHz selectivity and no host-side FFT.  Costs one
    board reconfiguration per point, because the point payload is still taken at
    4 GS/s to keep the file contract.

``PAM_AFM_FEEDBACK=stream``
    100 MS/s continuous stream on ``PAM_AFM_FEEDBACK_CHANNEL`` (default ``B``).
    Reports **volts**.  Unlike ``demod`` it never aborts and re-arms, so a
    reading costs ~4 ms instead of ~46 ms (~250 Hz against ~20 Hz), and the
    approach signal can sit on a cable that is not the imaging input.  The board
    still switches to 4 GS/s on A for the payload, but that switch is started
    before the XY move so it overlaps the stage travel.

``PAM_AFM_FEEDBACK=relay``
    A stays at **4 GS/s for the whole scan** and a two-way signal relay on the
    AUX I/O digital output selects which cable A sees: the 10 MHz monitor while
    approaching, the transducer while taking the point.  This is the only path
    that keeps A at 4 GS/s *and* takes the approach reading from its own cable,
    because the board has two analog inputs and no more -- AUX is a 3.3 V TTL
    digital port, not a third channel, and ``TRIG IN`` cannot be digitized.
    Costs a relay instead of 231 ms + 217 ms of reconfiguration per point.

None of these paths uses a trigger.  Both trigger engines are disabled, which is
what the current hardware requires: the laser trigger is not connected.

Usage
-----
    python PAM_Main_Nanomax_AFM.py --dry-run          # no hardware, simulates
    python PAM_Main_Nanomax_AFM.py --probe-z          # Z sweep, pick a setpoint
    python PAM_Main_Nanomax_AFM.py                    # the real scan

Every knob is an environment variable; the defaults are the ones the lab uses.
"""

from __future__ import annotations

import argparse
import datetime
import math
import os
import sys
import time
import traceback

import numpy as np

from Alazar_imaging.AlazarBurstCapture import (
    DEFAULT_INPUT_RANGE_VOLTS,
    AlazarBurstCapture,
    demod_amplitude_mean,
)
from Alazar_imaging.AlazarFFTSpectrum import AlazarFFTSpectrum
from Alazar_imaging.AlazarStreamSensor import AlazarStreamSensor
from Alazar_imaging.AlazarAuxRelay import AuxRelay
from Alazar_imaging.BPC303NativeController import BPC303NativeController
from Nanomax.afm_scan import (
    AfmApproachConfig,
    AfmScanner,
    check_config,
    suggest_gains,
)
from Nanomax import afm_control, afm_retract
from Nanomax.data_io import save_scan_data
from Nanomax.run_log import append_run_log, set_current_run_id
from Nanomax.scan_utils import (
    NANOMAX_MANUAL_MIN_STEP_UM,
    NANOMAX_PIEZO_SCAN_LIMIT_UM,
    build_sample_trajectory,
    scan_shape_from_range,
    validate_sample_trajectory,
)


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


# --------------------------------------------------------------------------- #
# Fixed hardware identity (matches PAM_Main_Nanomax_ClosedLoop.py)
# --------------------------------------------------------------------------- #

BPC303_SERIAL_NO = "71241834"
BPC303_KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"
BPC303_AXIS_MAP = {"x": 1, "y": 2, "z": 3}
BPC303_SAFE_MAX_OUTPUT_VOLTAGE = 75.0

SCAN_TARGET = "sample_closed_loop_afm"
SAMPLES_PER_RECORD = 4096          # the MATLAB contract: 4096 points at 4 GS/s
COORDINATE_UNIT = "um"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(name, default):
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else float(value)


def env_int(name, default):
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else int(value)


def env_str(name, default):
    value = os.environ.get(name)
    return default if value is None else value.strip()


class RunConfig:
    """Everything the scan needs, resolved from the environment once."""

    def __init__(self):
        # Geometry.
        self.scan_range_x_um = env_float("PAM_SCAN_RANGE_X_UM", 10.0)
        self.scan_range_y_um = env_float("PAM_SCAN_RANGE_Y_UM", 10.0)
        self.step_um = env_float("PAM_STEP_UM", 1.0)
        self.serpentine = env_str("PAM_SCAN_PATTERN", "serpentine").lower() in (
            "serpentine", "s", "snake"
        )

        # Feedback.
        self.feedback = env_str("PAM_AFM_FEEDBACK", "demod").lower()
        if self.feedback not in ("demod", "fft", "stream", "relay"):
            raise ValueError(
                "PAM_AFM_FEEDBACK must be 'demod', 'fft', 'stream' or 'relay'"
            )
        self.f0_hz = env_float("PAM_AFM_F0_HZ", 10e6)
        self.band_hz = env_float("PAM_AFM_BAND_HZ", 100e3)
        self.feedback_records = env_int("PAM_AFM_FEEDBACK_RECORDS", 8)
        # 100 MS/s for both modes: a 4096-sample record then spans 40.96 us, i.e.
        # 409.6 cycles of a 10 MHz carrier, instead of the 10.24 cycles a 4 GS/s
        # record holds.  The longer window is what makes the amplitude reading
        # repeatable; the FFT path additionally gets 24.4 kHz bins instead of
        # 977 kHz ones.  Verified on the board: both modes run trigger-free.
        self.feedback_rate_msps = env_float("PAM_AFM_FEEDBACK_RATE_MSPS", 100.0)
        # "stream" mode only.  The approach signal lives on its own cable, so it
        # needs its own input; B is the one A does not use.  The timeout sets the
        # feedback rate while no laser is present: 100 ticks = 1 ms -> ~250 Hz.
        self.feedback_channel = env_str("PAM_AFM_FEEDBACK_CHANNEL", "B").upper()
        # The laser period sets the minimum trigger timeout.  The board waits on
        # TRIG IN and falls back to the timeout, so a timeout shorter than one
        # laser period means it fires before every pulse, never locks, and the
        # summed payload is built from records at random phases of the laser
        # cycle.  Measured laser: 1050 Hz -> 952 us; 1.3x that is 124 ticks,
        # which still leaves the approach loop reading ~200 Hz.
        self.laser_period_us = env_float("PAM_AFM_LASER_PERIOD_US", 952.0)
        _auto_ticks = int(math.ceil(1.3 * self.laser_period_us / 10.0))
        self.feedback_timeout_ticks = env_int(
            "PAM_AFM_FEEDBACK_TIMEOUT_TICKS", max(_auto_ticks, 10)
        )
        # "relay" mode: time the relay contact needs after switching to the
        # imaging input before the payload is taken, so the transient is not
        # included in the A-line.
        self.relay_settle_s = env_float("PAM_AFM_RELAY_SETTLE_MS", 2.0) / 1000.0
        self.input_range_volts = env_float("PAM_AFM_INPUT_RANGE_V", DEFAULT_INPUT_RANGE_VOLTS)

        # Setpoint.
        self.setpoint = env_float("PAM_AFM_SETPOINT", 0.0)
        # "relay" also reads a 10 MHz amplitude off the digitizer, so it is in
        # volts as well; only the on-FPGA FFT reports arbitrary units.
        self.setpoint_unit = env_str(
            "PAM_AFM_SETPOINT_UNIT",
            "V" if self.feedback in ("demod", "stream", "relay") else "au",
        )

        # Control engine.  "pi" tracks a setpoint and can correct in either
        # direction; "stepped" is the original one-way creep, kept as a fallback.
        self.control_mode = env_str("PAM_AFM_CONTROL_MODE", "pi").lower()
        if self.control_mode not in ("pi", "stepped"):
            raise ValueError("PAM_AFM_CONTROL_MODE must be 'pi' or 'stepped'")

        # Approach geometry (shared).
        self.approach_sign = env_float("PAM_AFM_APPROACH_SIGN", 1.0)
        self.max_approach_um = env_float("PAM_AFM_MAX_APPROACH_UM", 4.0)
        self.max_approach_s = env_float("PAM_AFM_MAX_APPROACH_S", 15.0)
        self.retract_um = env_float("PAM_AFM_RETRACT_UM", 0.50)
        self.max_retract_um = env_float("PAM_AFM_MAX_RETRACT_UM", 2.0)
        self.retract_settle_ms = env_int("PAM_AFM_RETRACT_SETTLE_MS", 0)
        self.safety_amplitude = env_float("PAM_AFM_SAFETY_AMP", 0.0)
        self.min_snr = env_float("PAM_AFM_MIN_SNR", 0.0)
        self.z_min_um = env_float("PAM_AFM_Z_MIN_UM", float("nan"))
        self.z_max_um = env_float("PAM_AFM_Z_MAX_UM", float("nan"))
        self.position_tolerance_um = env_float("PAM_AFM_POSITION_TOLERANCE_UM", 0.02)
        self.settle_time_ms = env_int("PAM_AFM_SETTLE_MS", 0)
        self.settle_timeout_s = env_float("PAM_AFM_SETTLE_TIMEOUT_S", 10.0)

        # Speed.  Away from the probe the only limit is the stage; closing in,
        # the rate is what protects the probe, and the final band gets its own
        # slower rate.  All three are um/s, not um/step, so they mean the same
        # thing at any loop period.
        self.approach_speed_um_s = env_float("PAM_AFM_APPROACH_SPEED_UM_S", 1.000)
        self.final_speed_um_s = env_float("PAM_AFM_FINAL_SPEED_UM_S", 0.400)
        self.final_band_frac = env_float("PAM_AFM_FINAL_BAND_FRAC", 0.150)
        self.retract_speed_um_s = env_float("PAM_AFM_RETRACT_SPEED_UM_S", 20.0)
        self.search_speed_um_s = env_float("PAM_AFM_SEARCH_SPEED_UM_S", 3.000)

        # How high to lift between points.  A dome and a two-level target need
        # different answers, so this is a policy, not a number.
        self.retract_policy = env_str("PAM_AFM_RETRACT_POLICY", "auto").lower()
        self.retract_margin_um = env_float("PAM_AFM_RETRACT_MARGIN_UM", 0.050)
        self.retract_slope_window = env_int("PAM_AFM_RETRACT_SLOPE_WINDOW", 5)
        self.retract_slope_safety = env_float("PAM_AFM_RETRACT_SLOPE_SAFETY", 2.0)
        # "file:surface.csv" / "file:surface.py" / "module:name"
        self.retract_map = env_str("PAM_AFM_RETRACT_MAP", "")

        # Stepped engine only.
        self.coarse_step_um = env_float("PAM_AFM_COARSE_STEP_UM", 0.100)
        self.fine_step_um = env_float("PAM_AFM_FINE_STEP_UM", 0.020)
        self.coarse_to_fine_ratio = env_float("PAM_AFM_COARSE_TO_FINE", 0.30)
        self.max_steps = env_int("PAM_AFM_MAX_STEPS", 4000)

        # PI engine.
        # The single most useful knob: if you know how much the feedback value
        # changes per um of Z, set PAM_AFM_SENSITIVITY and the gains are derived
        # from it.  Leaving it at 0 falls back to the explicit kp/ki below.
        self.feedback_sensitivity = env_float("PAM_AFM_SENSITIVITY", 0.0)
        self.kp = env_float("PAM_AFM_KP", 0.003)
        self.ki = env_float("PAM_AFM_KI", 0.050)
        self.pi_dt_s = env_float("PAM_AFM_PI_DT_S", 0.030)
        self.dz_limit_um = env_float("PAM_AFM_DZ_LIMIT_UM", 0.050)
        self.deadband_frac = env_float("PAM_AFM_DEADBAND_FRAC", 0.020)
        self.deadband = env_float("PAM_AFM_DEADBAND", 0.0)
        self.kp_boost = env_float("PAM_AFM_KP_BOOST", 1.0)
        self.boost_error_frac = env_float("PAM_AFM_BOOST_ERROR_FRAC", 0.0)
        self.ff_gain = env_float("PAM_AFM_FF_GAIN", 0.0)
        self.hold_steps = env_int("PAM_AFM_HOLD_STEPS", 2)
        self.pi_max_steps = env_int("PAM_AFM_PI_MAX_STEPS", 200)
        self.pi_stall_um = env_float("PAM_AFM_PI_STALL_UM", 1.0)
        self.search_floor_frac = env_float("PAM_AFM_SEARCH_FLOOR_FRAC", 0.10)
        self.search_step_um = env_float("PAM_AFM_SEARCH_STEP_UM", 0.100)
        self.pi_settle_ms = env_int("PAM_AFM_PI_SETTLE_MS", 0)
        self.pi_dll = env_str("PAM_AFM_PI_DLL", "") or None

        # Where the Z criterion comes from.  "feedback" (default) is the
        # digitizer; "file:<path.py>" or "<module>" loads a module that defines
        # create_source(config) -> object with value().
        self.control_source = env_str("PAM_AFM_CONTROL_SOURCE", "feedback")

        # Point payload.
        self.records_per_point = env_int("PAM_AFM_RECORDS_PER_POINT", 256)

        # What to do when an approach fails: skip the point, or stop the scan.
        self.on_fail = env_str("PAM_AFM_ON_FAIL", "skip").lower()
        if self.on_fail not in ("skip", "stop"):
            raise ValueError("PAM_AFM_ON_FAIL must be 'skip' or 'stop'")

        # Optional explicit start Z; otherwise the current position is used.
        self.start_z_um = env_float("PAM_AFM_START_Z_UM", float("nan"))

    def z_window(self):
        lo = None if math.isnan(self.z_min_um) else self.z_min_um
        hi = None if math.isnan(self.z_max_um) else self.z_max_um
        return lo, hi

    def effective_gains(self):
        """The kp/ki the controller will actually use."""
        if self.feedback_sensitivity > 0:
            return suggest_gains(self.feedback_sensitivity, dt_s=self.pi_dt_s)
        return self.kp, self.ki

    def approach_config(self):
        lo, hi = self.z_window()
        return AfmApproachConfig(
            setpoint=self.setpoint,
            approach_sign=self.approach_sign,
            control_mode=self.control_mode,
            coarse_step_um=self.coarse_step_um,
            fine_step_um=self.fine_step_um,
            coarse_to_fine_ratio=self.coarse_to_fine_ratio,
            max_approach_um=self.max_approach_um,
            retract_um=self.retract_um,
            max_retract_um=self.max_retract_um,
            retract_settle_ms=self.retract_settle_ms,
            retract_policy=self.retract_policy,
            retract_margin_um=self.retract_margin_um,
            retract_slope_window=self.retract_slope_window,
            retract_slope_safety=self.retract_slope_safety,
            retract_map=self.retract_map or None,
            approach_speed_um_s=self.approach_speed_um_s,
            final_speed_um_s=self.final_speed_um_s,
            final_band_frac=self.final_band_frac,
            retract_speed_um_s=self.retract_speed_um_s,
            search_speed_um_s=self.search_speed_um_s,
            max_approach_s=self.max_approach_s,
            safety_amplitude=self.safety_amplitude,
            min_snr=self.min_snr,
            position_tolerance_um=self.position_tolerance_um,
            settle_time_ms=self.settle_time_ms,
            pi_settle_ms=self.pi_settle_ms,
            settle_timeout_s=self.settle_timeout_s,
            z_min_um=lo,
            z_max_um=hi,
            max_steps=self.max_steps,
            kp=self.kp,
            ki=self.ki,
            pi_dt_s=self.pi_dt_s,
            dz_limit_um=self.dz_limit_um,
            deadband_frac=self.deadband_frac,
            deadband=self.deadband,
            kp_boost=self.kp_boost,
            boost_error_frac=self.boost_error_frac,
            ff_gain=self.ff_gain,
            hold_steps=self.hold_steps,
            pi_max_steps=self.pi_max_steps,
            pi_stall_um=self.pi_stall_um,
            search_floor_frac=self.search_floor_frac,
            search_step_um=self.search_step_um,
            feedback_sensitivity=self.feedback_sensitivity,
            pi_dll=self.pi_dll,
        )

    def describe(self):
        # "relay" always runs A at 4 GS/s, whatever PAM_AFM_FEEDBACK_RATE_MSPS
        # says -- that variable belongs to the B-channel stream.
        rate_msps = 4000.0 if self.feedback == "relay" else self.feedback_rate_msps
        feedback_line = (
            "feedback        : %s (%.0f MS/s, %d records, f0 = %.3f MHz, band +/-%.1f kHz)"
            % (self.feedback, rate_msps, self.feedback_records,
               self.f0_hz / 1e6, self.band_hz / 1e3)
        )
        if self.feedback == "stream":
            feedback_line += ", channel %s, timeout %d ticks" % (
                self.feedback_channel, self.feedback_timeout_ticks)
        elif self.feedback == "relay":
            feedback_line += ", channel A, AUX relay, settle %.1f ms" % (
                self.relay_settle_s * 1e3)
        lines = [
            feedback_line,
            "setpoint        : %.4f %s" % (self.setpoint, self.setpoint_unit),
        ]
        if self.control_mode == "pi":
            kp, ki = self.effective_gains()
            lines.append(
                "control         : digital PI, kp %.6f um/unit, ki %.6f um/(unit s), "
                "step <= %.3f um" % (kp, ki, self.dz_limit_um)
            )
            lines.append(
                "PI guards       : deadband %.2f%% of setpoint, hold %d, "
                "blind allowance %.2f um, %d steps max"
                % (self.deadband_frac * 100.0, self.hold_steps,
                   self.pi_stall_um, self.pi_max_steps)
            )
            if self.feedback_sensitivity > 0:
                lines.append("gain source     : derived from sensitivity %.4f unit/um"
                             % self.feedback_sensitivity)
            else:
                lines.append("gain source     : PAM_AFM_KP / PAM_AFM_KI "
                             "(set PAM_AFM_SENSITIVITY to derive them instead)")
        else:
            lines.append(
                "control         : stepped, coarse %.3f um, fine %.3f um"
                % (self.coarse_step_um, self.fine_step_um)
            )
        lines.extend([
            "approach        : sign %+.0f, budget %.2f um" % (self.approach_sign, self.max_approach_um),
            "speed           : closing <= %.2f um/s, final band %.0f%% at <= %.2f um/s, "
            "retract %.0f um/s"
            % (self.approach_speed_um_s, self.final_band_frac * 100.0,
               self.final_speed_um_s, self.retract_speed_um_s),
            "retract         : >= %.2f um (max %.2f um), policy %s"
            % (self.retract_um, self.max_retract_um, self.retract_policy),
            "scan            : %.2f x %.2f um, step %.3f um, %s"
            % (self.scan_range_x_um, self.scan_range_y_um, self.step_um,
               "serpentine" if self.serpentine else "raster"),
            "records/point   : %d" % self.records_per_point,
            "on failure      : %s" % self.on_fail,
        ])
        if self.retract_map:
            lines.append("height map      : %s" % self.retract_map)
        # The cost of the speed settings, before a long scan rather than after.
        try:
            cfg = self.approach_config()
            eta = cfg.approach_eta_s()
            if cfg.control_mode == "pi" and eta > 0:
                lines.append(
                    "approach cost   : <= %.1f s per point worst case "
                    "(%d steps to cross %.1f um at %.2f um/s)"
                    % (eta, cfg.steps_needed(), self.max_approach_um,
                       self.approach_speed_um_s)
                )
            for problem in check_config(cfg):
                lines.append("WARNING         : %s" % problem)
        except Exception as exc:  # configuration errors surface at run time
            lines.append("WARNING         : %r" % (exc,))
        if self.control_source not in ("", "feedback", "auto"):
            lines.append("control source  : %s" % self.control_source)
        lo, hi = self.z_window()
        if lo is not None or hi is not None:
            lines.append("Z window        : %s .. %s um" % (lo, hi))
        return "\n".join("  " + line for line in lines)


# --------------------------------------------------------------------------- #
# Feedback sources
#
# Each one exposes ``value()``, which is the interface the Z loop reads (see
# Nanomax.afm_scan.ControlSource).  Swapping in a different Z criterion -- a
# demodulated phase, a lock-in output, a reading from another instrument --
# means writing one more class with ``value()`` and pointing
# PAM_AFM_CONTROL_SOURCE at it, without touching the scan engine.
# --------------------------------------------------------------------------- #

class DemodFeedback:
    """10 MHz amplitude in volts from the 4 GS/s time-domain records."""

    unit = "V"

    def __init__(self, daq, config):
        self.daq = daq
        self.config = config
        self.sample_rate_hz = config.feedback_rate_msps * 1e6
        self.records = config.feedback_records

    def prepare(self):
        self.daq.configure()

    def measure(self):
        data = self.daq.capture(self.records)
        amplitude = demod_amplitude_mean(
            data,
            self.sample_rate_hz,
            self.config.f0_hz,
            self.config.input_range_volts,
        )
        # The demodulator does not produce a meaningful SNR; 0 means "unknown"
        # and is not enforced by the approach engine.
        return amplitude, 0.0

    def value(self):
        return self.measure()

    def finish(self):
        return None

    def describe(self):
        return "time-domain demodulation at %.0f MS/s" % (self.sample_rate_hz / 1e6)


class StreamFeedback:
    """10 MHz amplitude in volts from a continuous stream on one input.

    ``DemodFeedback`` aborts and re-arms the board for every reading, which
    costs ~46 ms and caps the approach loop at ~20 Hz.  This one leaves the
    stream running and takes the newest record of each DMA buffer: ~4 ms per
    reading at a 1 ms trigger timeout, i.e. ~250 Hz, with no reconfiguration.

    The input is normally B, because A has to stay at 4 GS/s for the payload
    and the two signals are on separate cables.
    """

    unit = "V"

    def __init__(self, sensor, config):
        self.sensor = sensor
        self.config = config

    def prepare(self):
        if self.sensor.board is None:
            self.sensor.open()
        if not self.sensor.running:
            self.sensor.configure()
            self.sensor.start()

    def measure(self):
        return self.sensor.read().amplitude_v, 0.0

    def value(self):
        return self.measure()

    def finish(self):
        self.sensor.stop()

    def describe(self):
        return "continuous stream on channel %s at %.0f MS/s" % (
            self.config.feedback_channel, self.config.feedback_rate_msps)


class RelayFeedback:
    """A stays at 4 GS/s; an AUX-driven relay selects the input cable.

    The two signals cannot share an input, and the board has no third analog
    channel, so a two-way signal relay on CH A is the only way to keep 4 GS/s
    for the whole scan.  The digitizer streams A at 4 GS/s from the first point
    to the last; the relay decides whether A sees the 10 MHz monitor (approach)
    or the PA transducer (imaging).

    Switching cost is the relay's own time (a reed relay closes in ~1 ms),
    against 231 ms + 217 ms measured for reconfiguring the board between
    B(100 MS/s) and A(4 GS/s).
    """

    unit = "V"

    def __init__(self, sensor, relay, config):
        self.sensor = sensor
        self.relay = relay
        self.config = config

    def prepare(self):
        if self.sensor.board is None:
            self.sensor.open()
        self.relay.open()
        if not self.sensor.running:
            self.sensor.configure()
            self.sensor.start()
        self.relay.select("approach")

    def measure(self):
        return self.sensor.read().amplitude_v, 0.0

    def value(self):
        return self.measure()

    def payload(self, records):
        """Switch to the imaging input, then sum the newest ``records`` records."""
        self.relay.select("imaging")
        time.sleep(self.config.relay_settle_s)
        data = self.sensor.latest(records)
        if data.size == 0:
            return np.zeros((1, self.sensor.record_length), dtype=np.uint32)
        # dtype=uint32 is explicit: on Windows numpy promotes a uint32 sum to
        # uint64, and the .mat contract wants one uint32 row per pixel.
        return data.astype(np.uint32).sum(axis=0, keepdims=True, dtype=np.uint32)

    def finish(self):
        # Stop the stream, then park the relay on the imaging input: with the
        # scan over, the transducer is the input a later acquisition expects.
        # prepare() switches back to the approach cable.
        self.sensor.stop()
        self.relay.select("imaging")

    def describe(self):
        return "continuous stream on A at 4 GS/s, AUX relay selects the input"


class FftFeedback:
    """10 MHz amplitude in board units from the on-FPGA FFT."""

    unit = "au"

    def __init__(self, fft, config):
        self.fft = fft
        self.config = config

    def prepare(self):
        # Configure once and leave the stream running.  Restarting the capture
        # per point does not re-arm the DSP path on this board -- the first
        # buffer after an abort comes back mostly NaN (measured on hardware) --
        # so freshness comes from flush() inside measure_fresh() instead.
        if self.fft.board is None:
            self.fft.open()
            self.fft.configure()
            self.fft.start()
        elif not getattr(self.fft, "_running", False):
            self.fft.start()

    def measure(self):
        amplitude, snr, _peak = self.fft.measure_fresh(
            self.config.f0_hz, self.config.band_hz
        )
        return amplitude, snr

    def value(self):
        return self.measure()

    def finish(self):
        self.fft.stop()

    def describe(self):
        info = self.fft.module_info or {}
        return "on-FPGA FFT at %.0f MS/s (module v%s)" % (
            self.config.feedback_rate_msps, info.get("version", "?")
        )


class SyntheticFeedback:
    """Dry-run stand-in: amplitude rises exponentially as the gap closes."""

    unit = "V"

    def __init__(self, config, z_contact=0.0, rise_um=0.30, amp_max=1.0):
        self.config = config
        self.z_contact = float(z_contact)
        self.rise_um = float(rise_um)
        self.amp_max = float(amp_max)
        self.z_um = 0.0

    def prepare(self):
        return None

    def measure(self):
        gap = (self.z_contact - self.z_um) * (1.0 if self.config.approach_sign >= 0 else -1.0)
        if gap <= 0:
            return self.amp_max, 50.0
        return self.amp_max * math.exp(-gap / self.rise_um), 50.0

    def value(self):
        return self.measure()

    def finish(self):
        return None

    def describe(self):
        return "synthetic surface at z=%.3f um, rise %.2f um" % (self.z_contact, self.rise_um)


def build_feedback(config):
    """Create the feedback source named by ``PAM_AFM_FEEDBACK``.

    Kept in one place so the scan and the Z sweep cannot drift apart.
    """
    if config.feedback == "demod":
        daq = AlazarBurstCapture(
            sample_rate_hz=config.feedback_rate_msps * 1e6,
            input_range_volts=config.input_range_volts,
            trigger_timeout_ticks=config.feedback_timeout_ticks,
            verbose=False,
        )
        return DemodFeedback(daq, config)
    if config.feedback == "fft":
        fft = AlazarFFTSpectrum(
            sample_rate_hz=config.feedback_rate_msps * 1e6,
            input_range_volts=config.input_range_volts,
            verbose=False,
        )
        return FftFeedback(fft, config)
    if config.feedback == "stream":
        sensor = AlazarStreamSensor(
            sample_rate_hz=config.feedback_rate_msps * 1e6,
            channels=config.feedback_channel,
            signal_channel=config.feedback_channel,
            record_length=4096,
            f0_hz=config.f0_hz,
            records_per_buffer=config.feedback_records,
            timeout_ticks=config.feedback_timeout_ticks,
            input_range_volts=config.input_range_volts,
            verbose=False,
        )
        return StreamFeedback(sensor, config)
    if config.feedback == "relay":
        sensor = AlazarStreamSensor(
            sample_rate_hz=4e9,
            channels="A",
            signal_channel="A",
            record_length=4096,
            f0_hz=config.f0_hz,
            records_per_buffer=config.feedback_records,
            timeout_ticks=config.feedback_timeout_ticks,
            input_range_volts=config.input_range_volts,
            verbose=False,
        )
        return RelayFeedback(sensor, AuxRelay(verbose=False), config)
    raise ValueError("unknown feedback %r" % config.feedback)


def load_external_source(spec, config):
    r"""Load a user-supplied Z criterion (Nanomax.afm_control.load_source).

    ``PAM_AFM_CONTROL_SOURCE`` accepts::

        feedback                     the digitizer (default)
        const:1.25                   a fixed reading, for bench tests
        file:D:\path\my_source.py    load that file
        my_package.my_source         import that module

    The module (or file) must define ``create_source(config)`` returning an
    object with a ``value()`` method -- a bare callable works too.  ``value()``
    may return a number, ``(number, snr)``, or ``None`` for "no reading";
    ``prepare()`` / ``finish()`` / ``describe()`` / ``unit`` are optional.  The
    number is compared against ``PAM_AFM_SETPOINT`` in the same unit, so nothing
    here converts anything -- the caller owns the units.

    This is the seam that makes the Z criterion swappable: the 10 MHz amplitude
    is only the default implementation, and the scan engine never sees which one
    is in use.
    """
    return afm_control.load_source(spec, config)


# --------------------------------------------------------------------------- #
# Dry-run stage
# --------------------------------------------------------------------------- #

class DryRunStage:
    """Accepts Z/X/Y commands and reports them back instantly."""

    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.position = {"x": float(x), "y": float(y), "z": float(z)}
        self.commands = []

    def move_axis(self, axis, position, **kwargs):
        self.commands.append((axis, float(position)))
        self.position[axis] = float(position)

    def move_xyz(self, x=None, y=None, z=None, **kwargs):
        for axis, value in (("x", x), ("y", y), ("z", z)):
            if value is not None:
                self.move_axis(axis, value)

    def get_axis_position(self, axis):
        return self.position[axis]

    def get_position_values(self):
        return [self.position["x"], self.position["y"], self.position["z"]]

    def wait_until_axis_settled(self, axis, target, **kwargs):
        return True

    def ensure_closed_loop_axis(self, axis, timeout_s=15.0):
        return True

    def get_max_travel(self, axis):
        return NANOMAX_PIEZO_SCAN_LIMIT_UM

    def close(self):
        return None


# --------------------------------------------------------------------------- #
# Scan
# --------------------------------------------------------------------------- #

def _fmt_xyz(x, y, z):
    return "%.2f,%.2f,%.2f" % (float(x), float(y), float(z))


def run_scan(config, args):
    """The real thing: stage + digitizer + file."""
    started = time.time()
    run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    set_current_run_id(run_id)
    append_run_log(
        "AFM_SCAN_BEGIN",
        run_id=run_id,
        feedback=config.feedback,
        control_mode=config.control_mode,
        setpoint=config.setpoint,
        setpoint_unit=config.setpoint_unit,
        f0_hz=config.f0_hz,
        band_hz=config.band_hz,
        coarse_step_um=config.coarse_step_um,
        fine_step_um=config.fine_step_um,
        retract_um=config.retract_um,
        max_approach_um=config.max_approach_um,
        records_per_point=config.records_per_point,
        kp=config.effective_gains()[0],
        ki=config.effective_gains()[1],
        dz_limit_um=config.dz_limit_um,
        feedback_sensitivity=config.feedback_sensitivity,
    )

    print("\nAFM approach scan")
    print(config.describe())
    print("")

    stage = BPC303NativeController(
        serial_no=BPC303_SERIAL_NO,
        kinesis_dir=BPC303_KINESIS_DIR,
        axis_map=BPC303_AXIS_MAP,
        safe_max_output_voltage=BPC303_SAFE_MAX_OUTPUT_VOLTAGE,
        log_callback=append_run_log,
    )
    daq = fft = feedback = source = scanner = None
    external = False
    all_data = []
    z_failures = 0
    try:
        stage.ensure_closed_loop_axes(("x", "y", "z"))
        max_travel = {
            axis: float(stage.get_max_travel(axis)) for axis in ("x", "y", "z")
        }
        print("  stage travel    : x %.2f um, y %.2f um, z %.2f um"
              % (max_travel["x"], max_travel["y"], max_travel["z"]))

        positions = stage.get_position_values()
        start_x, start_y = float(positions[0]), float(positions[1])
        start_z = (
            float(positions[2]) if math.isnan(config.start_z_um) else float(config.start_z_um)
        )

        scan_w, scan_h = scan_shape_from_range(
            config.scan_range_x_um,
            config.scan_range_y_um,
            config.step_um,
            max_range_um=NANOMAX_PIEZO_SCAN_LIMIT_UM,
        )
        trajectory = build_sample_trajectory(
            start_x, start_y, scan_w, scan_h, config.step_um, serpentine=config.serpentine
        )
        validate_sample_trajectory(stage, trajectory, limit_label="AFM scan")

        total_points = len(trajectory)
        print("  grid            : %d x %d = %d points, step %.3f um"
              % (scan_w, scan_h, total_points, config.step_um))
        print("  start           : x=%.3f y=%.3f z=%.3f um" % (start_x, start_y, start_z))
        print("")

        board = None
        feedback = build_feedback(config)
        # In "demod" the approach DAQ doubles as the payload DAQ; in "fft" and
        # "stream" the payload needs its own 4 GS/s acquisition on A.
        daq = getattr(feedback, "daq", None)

        print("  feedback        : %s" % feedback.describe())
        feedback.prepare()

        # Where the Z criterion comes from.  Default is the digitizer; anything
        # else is a user module, which keeps the scan engine untouched.
        source = feedback
        external = config.control_source not in ("", "feedback", "auto")
        if external:
            source = load_external_source(config.control_source, config)
            if hasattr(source, "prepare"):
                source.prepare()
            label = source.describe() if hasattr(source, "describe") else config.control_source
            print("  control source  : %s" % label)
            unit = getattr(source, "unit", "") or ""
            if unit and unit != config.setpoint_unit:
                print("  setpoint unit   : %s (taken from the control source)" % unit)
                config.setpoint_unit = unit
            append_run_log("AFM_CONTROL_SOURCE", kind=config.control_source,
                           describe=str(label), unit=config.setpoint_unit)

        scanner = AfmScanner(
            stage=stage,
            measure=source,
            config=config.approach_config(),
            log=append_run_log,
        )
        print("  controller      : %s" % scanner.describe().get("pi_backend", "stepped"))

        z_cursor = start_z
        retract_lifts = []
        for index, (target_x, target_y) in enumerate(trajectory, start=1):
            # Where we are going after this point -- the retract policy needs it
            # to decide how high to lift before the XY move (a dome rises at the
            # rim, a two-level target steps up in one place).
            next_xy = trajectory[index] if index < total_points else None
            append_run_log(
                "AFM_POINT_BEGIN", index=index, total=total_points,
                x_um="%.4f" % target_x, y_um="%.4f" % target_y, z_from_um="%.4f" % z_cursor,
            )
            # Restart the approach stream before the move.  The clock change and
            # the arm cost are then paid while the stage is travelling instead
            # of after it has settled.  "relay" keeps its stream running across
            # the whole scan, so this only flips the input back to the approach
            # cable -- a millisecond, not a reconfiguration.
            if config.feedback in ("fft", "stream", "relay"):
                feedback.prepare()

            stage.move_xyz(x=target_x, y=target_y)
            stage.wait_until_settled(
                target_x, target_y,
                settle_time_ms=config.settle_time_ms,
                tolerance_step=config.position_tolerance_um,
                timeout_s=config.settle_timeout_s,
            )

            result, z_retract = scanner.scan_point(
                z_cursor, here_xy=(target_x, target_y), next_xy=next_xy
            )
            retract_lifts.append(round(abs(z_retract - result.z_um), 4) if result.ok else 0.0)

            if not result.ok:
                z_failures += 1
                append_run_log(
                    "AFM_POINT_FAILED", index=index, x_um="%.4f" % target_x,
                    y_um="%.4f" % target_y, reason=result.reason,
                    z_um="%.4f" % result.z_um, amplitude="%.4f" % result.amplitude,
                )
                print("  [%d/%d] FAILED %s at z=%.3f (amp %.4f)"
                      % (index, total_points, result.reason, result.z_um, result.amplitude))
                z_cursor = z_retract
                if config.on_fail == "stop":
                    print("  stopping: PAM_AFM_ON_FAIL=stop")
                    break
                continue

            # Payload at the setpoint.  In FFT and stream modes the board must be
            # switched back to 4 GS/s on A for this, which is why the approach
            # stream is stopped first.  "relay" never leaves 4 GS/s: the relay
            # moves A from the approach cable to the transducer and the same
            # stream keeps delivering records, so nothing is reconfigured.
            if config.feedback == "relay":
                payload = feedback.payload(config.records_per_point)
            else:
                if config.feedback in ("fft", "stream"):
                    feedback.finish()
                    if daq is None:
                        daq = AlazarBurstCapture(
                            sample_rate_hz=4e9,
                            input_range_volts=config.input_range_volts,
                            trigger_timeout_ticks=config.feedback_timeout_ticks,
                            verbose=False,
                        )
                        daq.open()
                    daq.configure()

                payload = daq.capture_summed(config.records_per_point)
            if payload.size == 0:
                z_failures += 1
                append_run_log("AFM_POINT_FAILED", index=index, reason="no_payload")
                z_cursor = z_retract
                if config.on_fail == "stop":
                    break
                continue

            pos_str = _fmt_xyz(target_x, target_y, result.z_um)
            meta = {
                "actual_pos_str": pos_str,
                "position_settle_ok": True,
                "position_timeout": False,
                "position_error_x_um": 0.0,
                "position_error_y_um": 0.0,
                "position_error_z_um": 0.0,
            }
            all_data.append([payload, pos_str, meta])

            append_run_log(
                "AFM_POINT_DONE", index=index, x_um="%.4f" % target_x,
                y_um="%.4f" % target_y, z_um="%.4f" % result.z_um,
                amplitude="%.4f" % result.amplitude, steps=result.steps,
                approach_reason=result.reason,
            )
            if index == 1 or index % 10 == 0 or index == total_points:
                elapsed = time.time() - started
                rate = index / elapsed if elapsed > 0 else 0.0
                remaining = (total_points - index) / rate if rate > 0 else 0.0
                print("  [%d/%d] z=%.3f amp=%.4f %s steps=%d  %.1f pt/s  eta %.0f s"
                      % (index, total_points, result.z_um, result.amplitude,
                         config.setpoint_unit, result.steps, rate, remaining))

            z_cursor = z_retract

        print("")
        print("  acquired %d of %d points, %d approach failures"
              % (len(all_data), total_points, z_failures))

        if not all_data:
            print("  nothing to save")
            append_run_log("AFM_SCAN_END", points=0, failures=z_failures)
            return 1

        stage_metadata = {
            "probe_stage": "none",
            "probe_controller": "none",
            "sample_unit_um_per_unit": 0.01,
            "sample_min_step_um": NANOMAX_MANUAL_MIN_STEP_UM,
            "afm_mode": 1,
            "afm_feedback": config.feedback,
            "afm_setpoint": config.setpoint,
            "afm_setpoint_unit": config.setpoint_unit,
            "afm_f0_hz": config.f0_hz,
            "afm_band_hz": config.band_hz,
            "afm_approach_sign": config.approach_sign,
            "afm_coarse_step_um": config.coarse_step_um,
            "afm_fine_step_um": config.fine_step_um,
            "afm_retract_um": config.retract_um,
            "afm_retract_policy": config.retract_policy,
            "afm_retract_lifts": retract_lifts,
            "afm_approach_speed_um_s": config.approach_speed_um_s,
            "afm_final_speed_um_s": config.final_speed_um_s,
            "afm_control_source": config.control_source,
            "afm_max_approach_um": config.max_approach_um,
            "afm_feedback_records": config.feedback_records,
            "afm_failed_points": z_failures,
            "afm_z_um": [round(float(item[1].split(",")[2]), 4) for item in all_data],
        }
        # Which engine actually ran, and with which gains -- so a file can be
        # reproduced months later without guessing.
        for key, value in scanner.describe().items():
            if isinstance(value, (int, float, str, bool)):
                stage_metadata["afm_" + key] = value

        result = save_scan_data(
            all_data,
            scan_w,
            scan_h,
            config.step_um,
            config.records_per_point,
            SAMPLES_PER_RECORD,
            True,
            SCAN_TARGET,
            COORDINATE_UNIT,
            None,
            None,
            start_x,
            start_y,
            start_z,
            0,
            save_prompt_timeout_s=30.0,
            stage_metadata=stage_metadata,
        )
        path = result.get("path") if isinstance(result, dict) else None
        append_run_log("AFM_SCAN_END", points=len(all_data), failures=z_failures, path=path)
        if path:
            print("  saved: %s" % path)
        return 0

    except KeyboardInterrupt:
        print("\n  interrupted")
        append_run_log("AFM_SCAN_INTERRUPTED", points=len(all_data))
        try:
            if all_data:
                save_scan_data(
                    all_data, 1, len(all_data), config.step_um, config.records_per_point,
                    SAMPLES_PER_RECORD, True, SCAN_TARGET, COORDINATE_UNIT,
                    None, None, 0.0, 0.0, 0.0, 0, save_prompt_timeout_s=5.0,
                )
        except Exception:
            traceback.print_exc()
        return 130
    finally:
        try:
            if external and source is not None and hasattr(source, "finish"):
                source.finish()
        except Exception:
            pass
        try:
            if feedback is not None:
                feedback.finish()
        except Exception:
            pass
        try:
            if scanner is not None:
                scanner.close()
        except Exception:
            pass
        try:
            stage.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Z sweep: pick a setpoint
# --------------------------------------------------------------------------- #

def probe_z(config, args):
    """Step Z over a bounded range and print the amplitude at every step."""
    stage = BPC303NativeController(
        serial_no=BPC303_SERIAL_NO,
        kinesis_dir=BPC303_KINESIS_DIR,
        axis_map=BPC303_AXIS_MAP,
        safe_max_output_voltage=BPC303_SAFE_MAX_OUTPUT_VOLTAGE,
        log_callback=append_run_log,
    )
    feedback = None
    try:
        stage.ensure_closed_loop_axes(("x", "y", "z"))
        z0 = float(stage.get_axis_position("z"))
        sign = 1.0 if config.approach_sign >= 0 else -1.0

        feedback = build_feedback(config)
        feedback.prepare()

        print("\nZ sweep from %.4f um, %+.3f um per step, %d steps"
              % (z0, sign * args.z_step_um, args.z_steps))
        print("%6s %12s %14s" % ("step", "z (um)", "amp (%s)" % feedback.unit))
        readings = []
        for step in range(args.z_steps + 1):
            z = z0 + sign * step * args.z_step_um
            stage.move_axis("z", z)
            stage.wait_until_axis_settled(
                "z", z,
                settle_time_ms=config.settle_time_ms,
                tolerance=config.position_tolerance_um,
                timeout_s=config.settle_timeout_s,
            )
            amplitude, _ = feedback.measure()
            actual = float(stage.get_axis_position("z"))
            readings.append((actual, amplitude))
            print("%6d %12.4f %14.5f" % (step, actual, amplitude))
            sys.stdout.flush()

        best = max(readings, key=lambda pair: pair[1])
        print("\n  peak %.5f %s at z=%.4f um" % (best[1], feedback.unit, best[0]))
        if best[1] > 0:
            print("  suggested setpoint (50%% of peak): %.5f %s"
                  % (0.5 * best[1], feedback.unit))
        print("  Z left at %.4f um -- retract manually before an imaging run" % readings[-1][0])
        return 0
    finally:
        try:
            if feedback is not None:
                feedback.finish()
        except Exception:
            pass
        try:
            stage.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Dry run
# --------------------------------------------------------------------------- #

def dry_run(config, args):
    """Full loop against a synthetic surface.  No hardware, no file."""
    stage = DryRunStage(z=-1.0)
    surface = SyntheticFeedback(config)
    surface.z_contact = 0.0
    config.setpoint = config.setpoint if config.setpoint > 0 else 0.5
    if config.feedback_sensitivity <= 0:
        # The synthetic surface has a known slope, so derive the gains from it.
        # Without this the dry run would use the au-scale defaults against a
        # 0..1 V signal and creep a few nanometres per step.
        config.feedback_sensitivity = config.setpoint / surface.rise_um

    # A dry run has no stage and no digitizer, so a real clock would measure a
    # dt of microseconds and the PI would integrate almost nothing.  Pretend
    # each control step costs what it costs on the rig.
    class DryRunClock:
        def __init__(self, period_s=0.030):
            self.t = 0.0
            self.period = float(period_s)

        def __call__(self):
            self.t += self.period
            return self.t

    print("\nDRY RUN -- synthetic surface, no hardware")
    print(config.describe())
    print("")

    scan_w, scan_h = scan_shape_from_range(
        config.scan_range_x_um, config.scan_range_y_um, config.step_um,
        max_range_um=NANOMAX_PIEZO_SCAN_LIMIT_UM,
    )
    trajectory = build_sample_trajectory(
        0.0, 0.0, scan_w, scan_h, config.step_um, serpentine=config.serpentine
    )
    print("  grid: %d x %d = %d points" % (scan_w, scan_h, len(trajectory)))

    # Make the synthetic surface tilt so the Z tracking is visible.
    tilt_per_point = args.dry_tilt_um

    def measure():
        # The synthetic surface must see the Z the stage is actually at.
        surface.z_um = stage.get_axis_position("z")
        return surface.measure()

    scanner = AfmScanner(
        stage=stage,
        measure=measure,
        config=config.approach_config(),
        log=lambda event, **fields: None,
        clock=DryRunClock(),
    )
    print("  controller      : %s" % scanner.describe().get("pi_backend", "stepped"))

    z_cursor = stage.get_axis_position("z")
    stops, steps_used, failures = [], [], 0
    lifts = []
    for index, (x, y) in enumerate(trajectory, start=1):
        next_xy = trajectory[index] if index < len(trajectory) else None
        stage.move_axis("x", x)
        stage.move_axis("y", y)
        surface.z_contact = (index - 1) * tilt_per_point
        result, z_retract = scanner.scan_point(z_cursor, here_xy=(x, y), next_xy=next_xy)
        if result.ok:
            stops.append(result.z_um)
            steps_used.append(result.steps)
            lifts.append(abs(z_retract - result.z_um))
        else:
            failures += 1
        z_cursor = z_retract

    print("  points ok      : %d" % len(stops))
    print("  failures       : %d" % failures)
    if stops:
        print("  Z span         : %.4f .. %.4f um" % (min(stops), max(stops)))
        print("  Z follows tilt : %.4f um expected, %.4f um observed"
              % ((len(trajectory) - 1) * tilt_per_point, stops[-1] - stops[0]))
        print("  steps per point: min %d, mean %.1f, max %d"
              % (min(steps_used), sum(steps_used) / len(steps_used), max(steps_used)))
    if lifts:
        print("  retract lift   : min %.3f, mean %.3f, max %.3f um (%s)"
              % (min(lifts), sum(lifts) / len(lifts), max(lifts),
                 scanner.describe().get("retract_policy", "?")))
    print("  config checks  : %s"
          % (", ".join(check_config(config.approach_config())) or "none"))
    scanner.close()
    print("")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="AFM-style approach scan for the closed-loop sample NanoMax"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="simulate the scan against a synthetic surface")
    parser.add_argument("--dry-tilt-um", type=float, default=0.02,
                        help="synthetic surface tilt per point for --dry-run")
    parser.add_argument("--probe-z", action="store_true",
                        help="step Z and print the 10 MHz amplitude (pick a setpoint)")
    parser.add_argument("--z-steps", type=int, default=40, help="--probe-z steps")
    parser.add_argument("--z-step-um", type=float, default=0.05,
                        help="--probe-z step size in um")
    args = parser.parse_args(argv)

    config = RunConfig()

    if args.dry_run:
        return dry_run(config, args)
    if args.probe_z:
        return probe_z(config, args)
    if config.setpoint <= 0:
        print("PAM_AFM_SETPOINT is not set; run --probe-z first to choose one.")
        return 2
    return run_scan(config, args)


if __name__ == "__main__":
    raise SystemExit(main())
