# -*- coding: utf-8 -*-
"""AFM-style approach scan on the closed-loop sample NanoMax (MAX311D / BPC303).

What this program does that the raster programs do not
------------------------------------------------------
The raster entry points keep Z fixed and trust that the sample stays in the
focal plane.  This one treats Z as the measurement: at every XY point the sample
is driven towards a fixed probe until the 10 MHz ultrasound amplitude picked up
by that probe reaches a setpoint, the A-line is taken there, and the sample is
backed off before moving on.  That is contact-mode AFM with an acoustic
setpoint, and it gives two extra channels per pixel for free -- the Z at which
the surface was found, and the amplitude at that Z.

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

Neither path uses a trigger.  Both trigger engines are disabled, which is what
the current hardware requires: the laser trigger is not connected.

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
from Alazar_imaging.BPC303NativeController import BPC303NativeController
from Nanomax.afm_scan import AfmApproachConfig, AfmScanner
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
        if self.feedback not in ("demod", "fft"):
            raise ValueError("PAM_AFM_FEEDBACK must be 'demod' or 'fft'")
        self.f0_hz = env_float("PAM_AFM_F0_HZ", 10e6)
        self.band_hz = env_float("PAM_AFM_BAND_HZ", 100e3)
        self.feedback_records = env_int("PAM_AFM_FEEDBACK_RECORDS", 8)
        # 100 MS/s for both modes: a 4096-sample record then spans 40.96 us, i.e.
        # 409.6 cycles of a 10 MHz carrier, instead of the 10.24 cycles a 4 GS/s
        # record holds.  The longer window is what makes the amplitude reading
        # repeatable; the FFT path additionally gets 24.4 kHz bins instead of
        # 977 kHz ones.  Verified on the board: both modes run trigger-free.
        self.feedback_rate_msps = env_float("PAM_AFM_FEEDBACK_RATE_MSPS", 100.0)
        self.input_range_volts = env_float("PAM_AFM_INPUT_RANGE_V", DEFAULT_INPUT_RANGE_VOLTS)

        # Setpoint.
        self.setpoint = env_float("PAM_AFM_SETPOINT", 0.0)
        self.setpoint_unit = env_str(
            "PAM_AFM_SETPOINT_UNIT", "V" if self.feedback == "demod" else "au"
        )

        # Approach.
        self.approach_sign = env_float("PAM_AFM_APPROACH_SIGN", 1.0)
        self.coarse_step_um = env_float("PAM_AFM_COARSE_STEP_UM", 0.100)
        self.fine_step_um = env_float("PAM_AFM_FINE_STEP_UM", 0.020)
        self.coarse_to_fine_ratio = env_float("PAM_AFM_COARSE_TO_FINE", 0.30)
        self.max_approach_um = env_float("PAM_AFM_MAX_APPROACH_UM", 4.0)
        self.retract_um = env_float("PAM_AFM_RETRACT_UM", 0.50)
        self.max_retract_um = env_float("PAM_AFM_MAX_RETRACT_UM", 2.0)
        self.safety_amplitude = env_float("PAM_AFM_SAFETY_AMP", 0.0)
        self.min_snr = env_float("PAM_AFM_MIN_SNR", 0.0)
        self.z_min_um = env_float("PAM_AFM_Z_MIN_UM", float("nan"))
        self.z_max_um = env_float("PAM_AFM_Z_MAX_UM", float("nan"))
        self.position_tolerance_um = env_float("PAM_AFM_POSITION_TOLERANCE_UM", 0.02)
        self.settle_time_ms = env_int("PAM_AFM_SETTLE_MS", 0)
        self.settle_timeout_s = env_float("PAM_AFM_SETTLE_TIMEOUT_S", 10.0)

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

    def approach_config(self):
        lo, hi = self.z_window()
        return AfmApproachConfig(
            setpoint=self.setpoint,
            approach_sign=self.approach_sign,
            coarse_step_um=self.coarse_step_um,
            fine_step_um=self.fine_step_um,
            coarse_to_fine_ratio=self.coarse_to_fine_ratio,
            max_approach_um=self.max_approach_um,
            retract_um=self.retract_um,
            max_retract_um=self.max_retract_um,
            safety_amplitude=self.safety_amplitude,
            min_snr=self.min_snr,
            position_tolerance_um=self.position_tolerance_um,
            settle_time_ms=self.settle_time_ms,
            settle_timeout_s=self.settle_timeout_s,
            z_min_um=lo,
            z_max_um=hi,
        )

    def describe(self):
        lines = [
            "feedback        : %s (%.0f MS/s, %d records, f0 = %.3f MHz, band +/-%.1f kHz)"
            % (self.feedback, self.feedback_rate_msps, self.feedback_records,
               self.f0_hz / 1e6, self.band_hz / 1e3),
            "setpoint        : %.4f %s" % (self.setpoint, self.setpoint_unit),
            "approach        : sign %+.0f, coarse %.3f um, fine %.3f um, budget %.2f um"
            % (self.approach_sign, self.coarse_step_um, self.fine_step_um, self.max_approach_um),
            "retract         : %.2f um (max %.2f um)" % (self.retract_um, self.max_retract_um),
            "scan            : %.2f x %.2f um, step %.3f um, %s"
            % (self.scan_range_x_um, self.scan_range_y_um, self.step_um,
               "serpentine" if self.serpentine else "raster"),
            "records/point   : %d" % self.records_per_point,
            "on failure      : %s" % self.on_fail,
        ]
        lo, hi = self.z_window()
        if lo is not None or hi is not None:
            lines.append("Z window        : %s .. %s um" % (lo, hi))
        return "\n".join("  " + line for line in lines)


# --------------------------------------------------------------------------- #
# Feedback sources
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

    def finish(self):
        return None

    def describe(self):
        return "time-domain demodulation at %.0f MS/s" % (self.sample_rate_hz / 1e6)


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

    def finish(self):
        return None

    def describe(self):
        return "synthetic surface at z=%.3f um, rise %.2f um" % (self.z_contact, self.rise_um)


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
        setpoint=config.setpoint,
        setpoint_unit=config.setpoint_unit,
        f0_hz=config.f0_hz,
        band_hz=config.band_hz,
        coarse_step_um=config.coarse_step_um,
        fine_step_um=config.fine_step_um,
        retract_um=config.retract_um,
        max_approach_um=config.max_approach_um,
        records_per_point=config.records_per_point,
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
    daq = fft = feedback = None
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
        if config.feedback == "demod":
            daq = AlazarBurstCapture(
                sample_rate_hz=config.feedback_rate_msps * 1e6,
                input_range_volts=config.input_range_volts,
                verbose=False,
            )
            feedback = DemodFeedback(daq, config)
        elif config.feedback == "fft":
            fft = AlazarFFTSpectrum(
                sample_rate_hz=config.feedback_rate_msps * 1e6,
                input_range_volts=config.input_range_volts,
                verbose=False,
            )
            feedback = FftFeedback(fft, config)
        else:
            raise ValueError("unknown feedback %r" % config.feedback)

        print("  feedback        : %s" % feedback.describe())
        feedback.prepare()

        scanner = AfmScanner(
            stage=stage,
            measure=feedback.measure,
            config=config.approach_config(),
            log=append_run_log,
        )

        z_cursor = start_z
        for index, (target_x, target_y) in enumerate(trajectory, start=1):
            append_run_log(
                "AFM_POINT_BEGIN", index=index, total=total_points,
                x_um="%.4f" % target_x, y_um="%.4f" % target_y, z_from_um="%.4f" % z_cursor,
            )
            stage.move_xyz(x=target_x, y=target_y)
            stage.wait_until_settled(
                target_x, target_y,
                settle_time_ms=config.settle_time_ms,
                tolerance_step=config.position_tolerance_um,
                timeout_s=config.settle_timeout_s,
            )

            if config.feedback == "fft":
                # The FFT stream was stopped by the previous point's payload
                # capture; restart it before approaching.
                feedback.prepare()

            result, z_retract = scanner.scan_point(z_cursor)

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

            # Payload at the setpoint.  In FFT mode the board must be switched
            # back to 4 GS/s for this, which is why the stream is stopped first.
            if config.feedback == "fft":
                feedback.finish()
                if daq is None:
                    daq = AlazarBurstCapture(
                        sample_rate_hz=4e9,
                        input_range_volts=config.input_range_volts,
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
            "afm_max_approach_um": config.max_approach_um,
            "afm_feedback_records": config.feedback_records,
            "afm_failed_points": z_failures,
            "afm_z_um": [round(float(item[1].split(",")[2]), 4) for item in all_data],
        }

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
            if feedback is not None:
                feedback.finish()
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

        if config.feedback == "demod":
            daq = AlazarBurstCapture(
                sample_rate_hz=config.feedback_rate_msps * 1e6,
                input_range_volts=config.input_range_volts,
                verbose=False,
            )
            feedback = DemodFeedback(daq, config)
        else:
            fft = AlazarFFTSpectrum(
                sample_rate_hz=config.feedback_rate_msps * 1e6,
                input_range_volts=config.input_range_volts,
                verbose=False,
            )
            feedback = FftFeedback(fft, config)
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
    )

    z_cursor = stage.get_axis_position("z")
    stops, steps_used, failures = [], [], 0
    for index, (x, y) in enumerate(trajectory, start=1):
        stage.move_axis("x", x)
        stage.move_axis("y", y)
        surface.z_contact = (index - 1) * tilt_per_point
        result, z_retract = scanner.scan_point(z_cursor)
        if result.ok:
            stops.append(result.z_um)
            steps_used.append(result.steps)
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
