"""PAM imaging on the closed-loop sample NanoMax (MAX311D via BPC303).

Derived from ``PAM_Main_Nanomax.py`` by removing everything this rig no longer has.
Kept deliberately identical to the parent script wherever the code is unchanged, so
run-log events and the ``.mat`` data contract stay compatible.

What was removed, and why
-------------------------
* **532 nm CBOX control** (``cbox_d2xx_controller``, ``PAM_532_*``): there is no
  532 nm laser path in this configuration. This also removes the blocked-beam
  standard-noise reference step, so saved files carry no ``noise_532`` fields.
* **TOPTICA CW laser control** (``toptica_dlc_controller``, ``PAM_TOPTICA_*``).
* **Open-loop probe NanoMax** (MDT693B / MAX312D): the whole ``probe_open_loop``
  scan target, its trajectory builder and its prealignment panel are gone.
  ``SCAN_TARGET`` is fixed to ``sample_closed_loop``; setting ``PAM_SCAN_TARGET`` to
  anything else now logs an override instead of switching modes.
* **NI-DAQ and LBTEK**: neither was reachable from the parent script, so there was
  nothing to delete here. (NI appears only in the unused ``NI_DAQ_based/`` tree and
  LBTEK only in ``PAM_Main_SDK.py`` / ``PAM_Main_LBTEK.py``.)

Because no laser code is reachable any more, ``Nanomax/no_laser_manager.NoLaserManager``
stands in for the laser manager that the shared terminal panels expect.

Everything else is retained: the persistent run log and previous-run inspection, the
startup X/Y zero-datum policy, the closed-loop prealignment panel, background DAQ
initialisation, the acquisition dashboard with progress/ETA, paused closed-loop Z
jogging, the live result preview, the timed save prompt, segmented return-to-start,
and the KeyboardInterrupt / exception cleanup paths.

Environment variables
---------------------
Same names as the parent script for every retained feature. The laser and probe
variables (``PAM_532_*``, ``PAM_TOPTICA_*``, ``PAM_PROBE_PREALIGN_*``,
``PAM_SCAN_TARGET=probe_open_loop``) are no longer read.
"""

import datetime
import gc
import os
import sys
import time
import traceback

import atsapi as ats

from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.BPC303NativeController import BPC303NativeController
from Nanomax.acquisition_panel import AcquisitionDashboard, PauseZMotionController
from Nanomax.daq_async import BackgroundDaqInit
from Nanomax.data_io import point_payload_is_empty, save_scan_data, save_scan_snapshot_data
from Nanomax.no_laser_manager import NoLaserManager
from Nanomax.prealign_panel import SamplePrealignConfig, run_sample_prealignment
from Nanomax.result_preview import PAMResultPreviewController
from Nanomax.run_log import RUN_LOG_PATH, append_run_log, inspect_previous_run, resolve_start_zero_policy, set_current_run_id
from Nanomax.runtime import find_other_pam_processes, return_to_start, run_bpc303_preflight, safe_return_to_start
from Nanomax.scan_speed_history import record_successful_scan_speed
from Nanomax.scan_utils import (
    NANOMAX_MANUAL_MIN_STEP_UM,
    NANOMAX_PIEZO_SCAN_LIMIT_UM,
    build_sample_trajectory,
    clamp_low_end_residual,
    resolve_scan_pattern,
    scan_shape_from_range,
    validate_sample_trajectory,
)


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


# Only this scan target is implemented now that the open-loop probe path is gone.
SCAN_TARGET = "sample_closed_loop"

# Sibling entry points that share the BPC303 controller or the Alazar board. Any of
# them running at the same time would contend for hardware, so the startup process
# check refuses to start rather than fighting over the controller.
SIBLING_PAM_ENTRY_POINTS = (
    "*PAM_Main_Nanomax_ClosedLoop.py*",
    "*PAM_Main_Prior.py*",
    "*PAM_Main_Manual.py*",
    "*PAM_Main_LBTEK.py*",
)


def refresh_terminal_for_acquisition():
    """Clear old setup output immediately before showing the acquisition progress bar."""
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(name, default):
    value = os.environ.get(name)
    return default if value is None else float(value)


def env_int(name, default):
    value = os.environ.get(name)
    return default if value is None else int(value)


def env_str(name, default):
    value = os.environ.get(name)
    return default if value is None else value


# Operator-editable defaults. Environment variables with the same names still
# override these values at runtime, but these are the script-level knobs to edit
# when the lab wants a different default behavior.
# False returns to the prealignment-selected scan start; True returns X/Y to 0 um.
DEFAULT_SAMPLE_RETURN_XY_TO_ZERO_AT_END = True


def main():
    # These startup decisions must be available before writing RUN_START or
    # explaining whether a sample zero-datum rebuild will be attempted.
    SAMPLE_START_ZERO_POLICY = env_str("PAM_SAMPLE_START_ZERO_POLICY", "auto")

    previous_run = inspect_previous_run()
    CURRENT_RUN_ID = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    set_current_run_id(CURRENT_RUN_ID)

    requested_scan_target = env_str("PAM_SCAN_TARGET", SCAN_TARGET)
    if requested_scan_target != SCAN_TARGET:
        print(
            f"\nPAM_SCAN_TARGET={requested_scan_target!r} is ignored: this program only "
            f"implements {SCAN_TARGET!r}. The open-loop probe path was removed."
        )

    try:
        SAMPLE_ZERO_XY_AT_START, SAMPLE_START_ZERO_REASON = resolve_start_zero_policy(
            SAMPLE_START_ZERO_POLICY,
            previous_run,
        )
    except ValueError as exc:
        append_run_log("RUN_END_ERROR", error=repr(exc), phase="startup_zero_policy_validation")
        raise SystemExit(f"Startup zero policy error: {exc}") from None
    append_run_log(
        "RUN_START",
        log_path=RUN_LOG_PATH,
        scan_target=SCAN_TARGET,
        requested_scan_target=requested_scan_target,
        laser_control="removed",
        sample_start_zero_policy=SAMPLE_START_ZERO_POLICY,
        sample_zero_at_start=SAMPLE_ZERO_XY_AT_START,
        sample_start_zero_reason=SAMPLE_START_ZERO_REASON,
        previous_status=previous_run["status"],
        previous_run_id=previous_run["run_id"],
        previous_event=previous_run["event"],
        previous_zero_datum_ready=previous_run["zero_datum_ready"],
        previous_final_cleanup_done=previous_run["final_cleanup_done"],
        previous_need_start_zero=previous_run["need_start_zero"],
        previous_zero_reason=previous_run["zero_reason"],
        cwd=os.getcwd(),
        pid=os.getpid(),
    )
    if SAMPLE_ZERO_XY_AT_START:
        print(
            "\nPrevious PAM run did not leave a trusted normal-cleanup + X/Y return/datum marker; "
            f"status={previous_run['status']}, run_id={previous_run['run_id']}, reason={previous_run['zero_reason']}. "
            f"Startup X/Y zero will be rebuilt (policy={SAMPLE_START_ZERO_POLICY})."
        )
        append_run_log("PREVIOUS_RUN_WARNING", previous_line=previous_run["line"])
    elif SAMPLE_START_ZERO_POLICY.strip().lower() == "never":
        print(
            "\nStartup X/Y zero rebuild is disabled by PAM_SAMPLE_START_ZERO_POLICY=never; "
            f"previous status={previous_run['status']}, run_id={previous_run['run_id']}, reason={previous_run['zero_reason']}. "
            "Use this only when the current sample datum is already valid."
        )
        append_run_log("START_ZERO_SKIPPED_BY_POLICY", previous_line=previous_run["line"])
    else:
        print(
            "\nPrevious PAM run completed normal cleanup and left a trusted X/Y return/datum marker; "
            f"status={previous_run['status']}, run_id={previous_run['run_id']}. "
            "Auto start-zero can be skipped."
        )
        append_run_log(
            "PREVIOUS_RUN_ZERO_READY",
            previous_zero_line=previous_run["zero_line"],
            previous_final_line=previous_run["final_line"],
        )

    # Closed-loop sample NanoMax: MAX311D on BPC303. User-confirmed axis mapping: channel 1/2/3 = X/Y/Z.
    BPC303_SERIAL_NO, BPC303_KINESIS_DIR = "71241834", r"C:\Program Files\Thorlabs\Kinesis"
    BPC303_AXIS_MAP, BPC303_SAFE_MAX_OUTPUT_VOLTAGE = {"x": 1, "y": 2, "z": 3}, 75.0
    BPC303_PREFLIGHT_ENABLE, BPC303_PREFLIGHT_TIMEOUT_S = False, 20.0
    # Disabled by default to avoid ~6 s startup overhead. Enable only when diagnosing Kinesis/USB hangs.
    # "open_close" does not enable channels, zero axes, or set position.
    BPC303_PREFLIGHT_MODE = "open_close"
    PANEL_AUTO_REFRESH_S = env_float("PAM_PANEL_AUTO_REFRESH_S", 5.0)

    # No laser control in this program. The shared terminal panels still expect a
    # laser-manager object, so they get one that reports the removal honestly.
    laser_manager = NoLaserManager(
        log_callback=append_run_log,
        reason="532 nm and CW laser control were removed from this program",
    )
    laser_manager.refresh_status()

    # User scan geometry. Ranges are the requested travel from first to last point.
    # Example: 20 um range with 1 um step gives 21 points: 0, 1, ..., 20 um.
    # The script also checks against the BPC303-reported maximum travel before acquisition.
    SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM = (
        env_float("PAM_SCAN_RANGE_X_UM", 18.75),
        env_float("PAM_SCAN_RANGE_Y_UM", 18.75),
        env_float("PAM_STEP_UM", 0.75),
    )  # X is actually up; Y is actually left.
    SAMPLE_X_DIRECTION, SAMPLE_Y_DIRECTION = 1.0, 1.0
    SAMPLE_PREALIGN_ENABLE, SAMPLE_PREALIGN_X_STEP_UM, SAMPLE_PREALIGN_Y_STEP_UM, SAMPLE_PREALIGN_Z_STEP_UM = env_bool("PAM_SAMPLE_PREALIGN_ENABLE", True), 0.1, 0.1, 0.1
    SAMPLE_PREALIGN_INTERVAL_S = 0.25
    # Startup zero policy:
    #   "auto": rebuild X/Y zero unless the previous log has a trusted low-end zero-datum marker.
    #   "always": rebuild X/Y zero every run.
    #   "never": never rebuild at start; only use this when you know the current datum is valid.
    # Z is intentionally excluded by default to avoid changing focus/clearance.
    SAMPLE_ZERO_AXES = ("x", "y")
    SAMPLE_LOW_END_RESIDUAL_TOLERANCE_UM = 0.01
    # False returns to the prealignment-selected start. Set True to return to 0,0.
    SAMPLE_RETURN_XY_TO_ZERO_AT_END = env_bool(
        "PAM_SAMPLE_RETURN_XY_TO_ZERO_AT_END",
        DEFAULT_SAMPLE_RETURN_XY_TO_ZERO_AT_END,
    )
    # False avoids a ~45-60 s BPC303 SetZero cycle at every normal end.
    # Set True only when you explicitly want the controller outputs forced to 0 V after each run.
    SAMPLE_ZERO_XY_AT_END = False
    # SCAN_PATTERN:
    #   "serpentine" or "s": S-shaped scan; odd rows reverse X direction.
    #   "raster" or "z": Z-shaped one-way rows; each row starts from low X.
    SCAN_PATTERN, SETTLE_MS = "serpentine", 120
    SAMPLE_POSITION_TOLERANCE_UM = env_float("PAM_SAMPLE_POSITION_TOLERANCE_UM", 0.02)
    SAMPLE_POSITION_TIMEOUT_S = env_float("PAM_SAMPLE_POSITION_TIMEOUT_S", 300.0)
    SAMPLE_POSITION_REISSUE_INTERVAL_S = env_float("PAM_SAMPLE_POSITION_REISSUE_INTERVAL_S", 1.0)
    SAMPLE_RETURN_STEP_UM = env_float("PAM_SAMPLE_RETURN_STEP_UM", 0.1)
    SAMPLE_RETURN_POSITION_TIMEOUT_S = env_float(
        "PAM_SAMPLE_RETURN_POSITION_TIMEOUT_S",
        min(float(SAMPLE_POSITION_TIMEOUT_S), 10.0),
    )
    DATA_SAVE_AUTO_TIMEOUT_S = env_float("PAM_DATA_SAVE_AUTO_TIMEOUT_S", 60.0)
    RESULT_PREVIEW_ENABLE = env_bool("PAM_RESULT_PREVIEW_ENABLE", True)
    RESULT_PREVIEW_OUTPUT_DIR = env_str("PAM_RESULT_PREVIEW_OUTPUT_DIR", r".\results\cache\pam_preview")
    RESULT_PREVIEW_SNAPSHOT_DIR = env_str("PAM_RESULT_PREVIEW_SNAPSHOT_DIR", r".\results\cache\pam_live_snapshots")
    RESULT_PREVIEW_TIMEOUT_S = env_float("PAM_RESULT_PREVIEW_TIMEOUT_S", 900.0)
    PROCESSING_SKILL_PATH = env_str("PAM_PROCESSING_SKILL_PATH", r"D:\Phd_training\skills\data-processing-skill")

    # DAQ parameters. DELAY is 0 since 2026-10-08; it was int(251*4) = 1004
    # (251 ns at 4 GS/s) and the record window now starts exactly at the trigger edge.
    DELAY, SAMPLES_REC, SAMPLE_RATE = 0, 4096, ats.SAMPLE_RATE_4000MSPS
    AVERAGE_ENABLE, RECORDS_PER_POINT, BUFFER_COUNT = True, 512, 4
    ACQ_TIMEOUT_MS = env_int("PAM_ACQ_TIMEOUT_MS", 1000)
    POINT_LOG_INTERVAL, USER_STOP_ENABLE, USER_STOP_KEY = 25, True, "q"

    if SAMPLE_POSITION_TOLERANCE_UM < float(NANOMAX_MANUAL_MIN_STEP_UM):
        append_run_log(
            "RUN_END_ERROR",
            error=(
                f"PAM_SAMPLE_POSITION_TOLERANCE_UM={SAMPLE_POSITION_TOLERANCE_UM:g} "
                f"is below NanoMax minimum step guard {float(NANOMAX_MANUAL_MIN_STEP_UM):g} um"
            ),
            phase="sample_position_tolerance_validation",
        )
        raise SystemExit(
            "Sample position tolerance is below the NanoMax minimum step guard "
            f"({float(NANOMAX_MANUAL_MIN_STEP_UM):g} um). "
            "Use PAM_SAMPLE_POSITION_TOLERANCE_UM >= that value."
        ) from None
    if SAMPLE_POSITION_TIMEOUT_S <= 0:
        append_run_log(
            "RUN_END_ERROR",
            error=f"PAM_SAMPLE_POSITION_TIMEOUT_S={SAMPLE_POSITION_TIMEOUT_S:g} must be positive",
            phase="sample_position_timeout_validation",
        )
        raise SystemExit("PAM_SAMPLE_POSITION_TIMEOUT_S must be positive.") from None
    if SAMPLE_POSITION_REISSUE_INTERVAL_S < 0:
        append_run_log(
            "RUN_END_ERROR",
            error=f"PAM_SAMPLE_POSITION_REISSUE_INTERVAL_S={SAMPLE_POSITION_REISSUE_INTERVAL_S:g} must be non-negative",
            phase="sample_position_reissue_validation",
        )
        raise SystemExit("PAM_SAMPLE_POSITION_REISSUE_INTERVAL_S must be non-negative.") from None
    if SAMPLE_RETURN_STEP_UM <= 0:
        append_run_log(
            "RUN_END_ERROR",
            error=f"PAM_SAMPLE_RETURN_STEP_UM={SAMPLE_RETURN_STEP_UM:g} must be positive",
            phase="sample_return_step_validation",
        )
        raise SystemExit("PAM_SAMPLE_RETURN_STEP_UM must be positive.") from None
    if SAMPLE_RETURN_POSITION_TIMEOUT_S <= 0:
        append_run_log(
            "RUN_END_ERROR",
            error=f"PAM_SAMPLE_RETURN_POSITION_TIMEOUT_S={SAMPLE_RETURN_POSITION_TIMEOUT_S:g} must be positive",
            phase="sample_return_position_timeout_validation",
        )
        raise SystemExit("PAM_SAMPLE_RETURN_POSITION_TIMEOUT_S must be positive.") from None
    if DATA_SAVE_AUTO_TIMEOUT_S < 0:
        append_run_log(
            "RUN_END_ERROR",
            error=f"PAM_DATA_SAVE_AUTO_TIMEOUT_S={DATA_SAVE_AUTO_TIMEOUT_S:g} must be non-negative",
            phase="data_save_timeout_validation",
        )
        raise SystemExit("PAM_DATA_SAVE_AUTO_TIMEOUT_S must be non-negative.") from None
    try:
        SCAN_W, SCAN_H = scan_shape_from_range(
            SCAN_RANGE_X_UM,
            SCAN_RANGE_Y_UM,
            STEP_UM,
            max_range_um=NANOMAX_PIEZO_SCAN_LIMIT_UM,
        )
        SERPENTINE_SCAN, SCAN_PATTERN_LABEL = resolve_scan_pattern(SCAN_PATTERN)
    except ValueError as exc:
        append_run_log("RUN_END_ERROR", error=repr(exc), phase="scan_parameter_validation")
        raise SystemExit(f"Scan parameter error: {exc}") from None
    append_run_log(
        "SCAN_CONFIG_INITIAL",
        scan_target=SCAN_TARGET,
        scan_range_x_um=SCAN_RANGE_X_UM,
        scan_range_y_um=SCAN_RANGE_Y_UM,
        step_um=STEP_UM,
        scan_w=SCAN_W,
        scan_h=SCAN_H,
        scan_pattern=SCAN_PATTERN_LABEL,
        sample_start_zero_policy=SAMPLE_START_ZERO_POLICY,
        sample_zero_at_start=SAMPLE_ZERO_XY_AT_START,
        sample_return_xy_to_zero_at_end=SAMPLE_RETURN_XY_TO_ZERO_AT_END,
        sample_start_zero_reason=SAMPLE_START_ZERO_REASON,
        sample_zero_at_end=SAMPLE_ZERO_XY_AT_END,
        sample_low_end_residual_tolerance_um=SAMPLE_LOW_END_RESIDUAL_TOLERANCE_UM,
        sample_position_tolerance_um=SAMPLE_POSITION_TOLERANCE_UM,
        sample_position_timeout_s=SAMPLE_POSITION_TIMEOUT_S,
        sample_position_reissue_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
        sample_return_step_um=SAMPLE_RETURN_STEP_UM,
        sample_return_position_timeout_s=SAMPLE_RETURN_POSITION_TIMEOUT_S,
        data_save_auto_timeout_s=DATA_SAVE_AUTO_TIMEOUT_S,
        result_preview_enable=RESULT_PREVIEW_ENABLE,
        result_preview_output_dir=RESULT_PREVIEW_OUTPUT_DIR,
        result_preview_snapshot_dir=RESULT_PREVIEW_SNAPSHOT_DIR,
        result_preview_timeout_s=RESULT_PREVIEW_TIMEOUT_S,
        processing_skill_path=PROCESSING_SKILL_PATH,
        point_log_interval=POINT_LOG_INTERVAL,
        user_stop_enable=USER_STOP_ENABLE,
        user_stop_key=USER_STOP_KEY,
        bpc303_preflight_enable=BPC303_PREFLIGHT_ENABLE,
        bpc303_preflight_mode=BPC303_PREFLIGHT_MODE,
        bpc303_preflight_timeout_s=BPC303_PREFLIGHT_TIMEOUT_S,
        sample_prealign_enable=SAMPLE_PREALIGN_ENABLE,
        panel_auto_refresh_s=PANEL_AUTO_REFRESH_S,
        laser_control="removed",
    )
    other_pam_processes = find_other_pam_processes(SIBLING_PAM_ENTRY_POINTS)
    if other_pam_processes:
        append_run_log(
            "RUN_END_ERROR",
            error="another_PAM_entry_point_is_running",
            active_processes=" ; ".join(other_pam_processes),
        )
        raise SystemExit(
            "Another PAM entry point is still running and may hold the controller or the Alazar board:\n"
            + "\n".join(other_pam_processes)
            + "\nClose that console/process or reboot the experiment PC before starting a new scan."
        )
    if BPC303_PREFLIGHT_ENABLE:
        run_bpc303_preflight(
            BPC303_SERIAL_NO,
            BPC303_KINESIS_DIR,
            timeout_s=BPC303_PREFLIGHT_TIMEOUT_S,
            mode=BPC303_PREFLIGHT_MODE,
        )

    stage = daq = daq_init = acquisition_dashboard = None
    all_data, START_X, START_Y, START_Z = [], None, None, None
    coordinate_unit, total_points, acquired_points, user_stop_requested = "um", 0, 0, False
    position_timeout_points = 0
    daq_empty_points, daq_empty_streak = 0, 0
    acquisition_loop_start_s = None
    data_save_done = False
    last_saved_mat_path = None
    preview_snapshot_serial = 0
    prealignment_started_acquisition = False
    preacquisition_quit_requested = False
    result_preview_controller = (
        PAMResultPreviewController(
            project_root=os.getcwd(),
            output_root=RESULT_PREVIEW_OUTPUT_DIR,
            processing_skill_path=PROCESSING_SKILL_PATH,
            python_executable=sys.executable,
            log_callback=append_run_log,
            timeout_s=RESULT_PREVIEW_TIMEOUT_S,
        )
        if RESULT_PREVIEW_ENABLE
        else None
    )

    # The open-loop probe NanoMax (MAX312D / MDT693B) is not part of this program, so the
    # .mat must not claim it. The sample identity is left at the data_io default, which is
    # the same MAX311D / BPC303 pair this program actually drives.
    STAGE_METADATA = {
        "probe_stage": "none",
        "probe_controller": "none",
    }
    append_run_log("MAT_STAGE_METADATA", **STAGE_METADATA)

    def save_data_once(reason):
        nonlocal data_save_done, last_saved_mat_path
        if data_save_done:
            append_run_log("DATA_SAVE_ONCE_SKIPPED_ALREADY_DONE", reason=reason)
            return None
        append_run_log("DATA_SAVE_ONCE_BEGIN", reason=reason, points=len(all_data))
        result = save_scan_data(
            all_data,
            SCAN_W,
            SCAN_H,
            STEP_UM,
            RECORDS_PER_POINT,
            SAMPLES_REC,
            AVERAGE_ENABLE,
            SCAN_TARGET,
            coordinate_unit,
            None,          # probe_step_v: open-loop probe path removed
            None,          # probe_um_per_v: open-loop probe path removed
            START_X,
            START_Y,
            START_Z,
            DELAY,
            save_prompt_timeout_s=DATA_SAVE_AUTO_TIMEOUT_S,
            stage_metadata=STAGE_METADATA,
        )
        status = result.get("status") if isinstance(result, dict) else None
        if status == "saved" and result.get("path"):
            last_saved_mat_path = result["path"]
            append_run_log("DATA_SAVE_CURRENT_FILE", reason=reason, path=last_saved_mat_path)
            data_save_done = True
        elif status in {"skipped", "discarded"}:
            data_save_done = True
        elif status == "failed":
            data_save_done = False
            append_run_log("DATA_SAVE_ONCE_RETRY_NEEDED", reason=reason, result=repr(result))
        else:
            data_save_done = bool(result)
        append_run_log("DATA_SAVE_ONCE_DONE", reason=reason, result=repr(result))
        return result

    def current_preview_mat_path():
        nonlocal preview_snapshot_serial, last_saved_mat_path
        if all_data:
            preview_snapshot_serial += 1
            snapshot = save_scan_snapshot_data(
                all_data,
                SCAN_W,
                SCAN_H,
                STEP_UM,
                RECORDS_PER_POINT,
                SAMPLES_REC,
                AVERAGE_ENABLE,
                SCAN_TARGET,
                coordinate_unit,
                None,      # probe_step_v: open-loop probe path removed
                None,      # probe_um_per_v: open-loop probe path removed
                START_X,
                START_Y,
                START_Z,
                output_dir=RESULT_PREVIEW_SNAPSHOT_DIR,
                label=f"{CURRENT_RUN_ID}-points-{len(all_data):06d}-{preview_snapshot_serial:03d}",
                stage_metadata=STAGE_METADATA,
            )
            if isinstance(snapshot, dict) and snapshot.get("status") == "saved" and snapshot.get("path"):
                last_saved_mat_path = snapshot["path"]
                return snapshot["path"]
            append_run_log("DATA_PREVIEW_SNAPSHOT_UNAVAILABLE", result=repr(snapshot))
        return last_saved_mat_path

    def stop_daq_best_effort(reason):
        if daq is None:
            return
        try:
            daq.stop_capture()
            append_run_log("DAQ_STOP_CAPTURE_DONE", reason=reason)
        except Exception as exc:
            append_run_log("DAQ_STOP_CAPTURE_FAILED", reason=reason, error=repr(exc))
            print(f"DAQ stop_capture failed during {reason}: {exc}")

    def attach_point_metadata(start_len, metadata):
        if len(all_data) <= start_len:
            append_run_log("ACQUISITION_POINT_METADATA_SKIPPED", reason="daq_did_not_append", metadata=repr(metadata))
            return
        point = all_data[-1]
        if len(point) < 3 or not isinstance(point[2], dict):
            point.append({})
        point[2].update(metadata)

    try:
        def daq_factory(step, daq_obj):
            if step == "create_system":
                return AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY, channel_A_range=ats.INPUT_RANGE_PM_200_MV)
            if step == "configure_board":
                daq_obj.configure_board(sample_rate=SAMPLE_RATE)
                return daq_obj
            if step == "prepare_acquisition":
                daq_obj.prepare_acquisition(
                    acq_channel=ats.CHANNEL_A,
                    samples_per_record=SAMPLES_REC,
                    records_per_buffer=RECORDS_PER_POINT,
                    buffer_count=BUFFER_COUNT,
                    records_per_point=RECORDS_PER_POINT,
                )
                return daq_obj
            raise ValueError(f"Unknown DAQ init step: {step}")

        print("Using BPC303 native closed-loop control for the MAX311D sample NanoMax...")
        append_run_log("STAGE_CONNECT_BEGIN", controller="BPC303", stage_model="MAX311D")
        stage = BPC303NativeController(
            serial_no=BPC303_SERIAL_NO,
            kinesis_dir=BPC303_KINESIS_DIR,
            channels=(1, 2, 3),
            axis_map=BPC303_AXIS_MAP,
            safe_max_output_voltage=BPC303_SAFE_MAX_OUTPUT_VOLTAGE,
            log_callback=append_run_log,
        )
        append_run_log(
            "STAGE_CONNECT_DONE",
            controller="BPC303",
            serial=BPC303_SERIAL_NO,
            max_travel_x_um=stage.get_max_travel("x"),
            max_travel_y_um=stage.get_max_travel("y"),
            max_travel_z_um=stage.get_max_travel("z"),
        )
        if SAMPLE_ZERO_XY_AT_START:
            print(
                "Zeroing sample X/Y at the low-voltage end before scan: "
                "output goes to 0 V and the selected-axis datum is rebuilt. "
                "Z is not zeroed by default."
            )
            append_run_log("ZERO_DATUM_REBUILD_BEGIN", axes=",".join(SAMPLE_ZERO_AXES), reason="scan_start")
            stage.set_zero_axes(SAMPLE_ZERO_AXES, wait=True, settle_time_ms=SETTLE_MS)
            append_run_log("ZERO_DATUM_REBUILT", axes=",".join(SAMPLE_ZERO_AXES), reason="scan_start")
        raw_values = stage.get_position_values()
        START_X, START_Y, START_Z = [float(v) for v in raw_values[:3]]
        if SAMPLE_ZERO_XY_AT_START:
            START_X, START_Y = 0.0, 0.0
        else:
            START_X = clamp_low_end_residual("x", START_X, SAMPLE_LOW_END_RESIDUAL_TOLERANCE_UM)
            START_Y = clamp_low_end_residual("y", START_Y, SAMPLE_LOW_END_RESIDUAL_TOLERANCE_UM)
        append_run_log(
            "START_POSITION",
            scan_target=SCAN_TARGET,
            x_um=f"{START_X:.4f}",
            y_um=f"{START_Y:.4f}",
            z_um=f"{START_Z:.4f}",
        )
        print(
            "Sample start position: "
            f"X={START_X:.4f} um, Y={START_Y:.4f} um, Z={START_Z:.4f} um; "
            f"travel X={stage.get_max_travel('x'):.1f} um, "
            f"Y={stage.get_max_travel('y'):.1f} um, Z={stage.get_max_travel('z'):.1f} um"
        )

        if SAMPLE_PREALIGN_ENABLE:
            daq_init = BackgroundDaqInit(daq_factory, log_callback=append_run_log)
            append_run_log("DAQ_INIT_BACKGROUND_REQUESTED", sample_rate=SAMPLE_RATE, records_per_point=RECORDS_PER_POINT, buffer_count=BUFFER_COUNT)
            daq_init.start()
            prealign_result = run_sample_prealignment(
                stage,
                SamplePrealignConfig(
                    scan_range_x_um=SCAN_RANGE_X_UM,
                    scan_range_y_um=SCAN_RANGE_Y_UM,
                    step_um=STEP_UM,
                    sample_x_direction=SAMPLE_X_DIRECTION,
                    sample_y_direction=SAMPLE_Y_DIRECTION,
                    scan_pattern=SCAN_PATTERN,
                    settle_ms=SETTLE_MS,
                    position_tolerance_um=SAMPLE_POSITION_TOLERANCE_UM,
                    position_timeout_s=SAMPLE_POSITION_TIMEOUT_S,
                    position_reissue_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
                    x_step_um=SAMPLE_PREALIGN_X_STEP_UM,
                    y_step_um=SAMPLE_PREALIGN_Y_STEP_UM,
                    z_step_um=SAMPLE_PREALIGN_Z_STEP_UM,
                    sample_interval_s=SAMPLE_PREALIGN_INTERVAL_S,
                    auto_refresh_s=PANEL_AUTO_REFRESH_S,
                ),
                log_callback=append_run_log,
                status_provider=daq_init.snapshot,
                display_params={
                    "LASER_MANAGER": laser_manager,
                    "SCAN_TARGET": SCAN_TARGET,
                    "SAMPLE_CONTROLLER": "BPC303",
                    "SAMPLE_STAGE_MODEL": "MAX311D",
                    "SAMPLE_CONNECTION": "connected",
                    "SAMPLE_SERIAL": BPC303_SERIAL_NO,
                    "SAMPLE_AXIS_MAP": "1/2/3=X/Y/Z",
                    "SHOW_PROBE_ROWS": False,
                    "DELAY": DELAY,
                    "SAMPLES_REC": SAMPLES_REC,
                    "SAMPLE_RATE": SAMPLE_RATE,
                    "AVERAGE_ENABLE": AVERAGE_ENABLE,
                    "ACQ_TIMEOUT_MS": ACQ_TIMEOUT_MS,
                    "RECORDS_PER_POINT": RECORDS_PER_POINT,
                    "BUFFER_COUNT": BUFFER_COUNT,
                    "POINT_LOG_INTERVAL": POINT_LOG_INTERVAL,
                    "USER_STOP_ENABLE": USER_STOP_ENABLE,
                    "USER_STOP_KEY": USER_STOP_KEY,
                    "SAMPLE_START_ZERO_POLICY": SAMPLE_START_ZERO_POLICY,
                    "SAMPLE_ZERO_XY_AT_END": SAMPLE_ZERO_XY_AT_END,
                    "PANEL_AUTO_REFRESH_S": PANEL_AUTO_REFRESH_S,
                },
            )
            if getattr(prealign_result, "next_action", "start") == "quit":
                preacquisition_quit_requested = True
                append_run_log(
                    "RUN_END_PREACQUISITION_QUIT",
                    phase="prealignment",
                    start_panel="sample",
                    acquired_points=0,
                    expected_points=0,
                )
                append_run_log(
                    "PREALIGN_QUIT_WITH_VALID_DATUM",
                    reason="operator_quit_before_acquisition",
                    sample_zero_at_start=SAMPLE_ZERO_XY_AT_START,
                    sample_start_zero_reason=SAMPLE_START_ZERO_REASON,
                )
                print("Prealignment quit requested before acquisition; closing controllers without starting DAQ acquisition.")
                return
            prealignment_started_acquisition = True
            # The shared panel returns a FLAT SamplePrealignResult
            # (x_um/y_um/z_um/scan_range_*/step_um/scan_pattern/next_action/scan_ok).
            # The original PAM_Main_Nanomax.py consumed a COMPOSITE result that nested
            # the sample payload under .sample_result (plus the removed open-loop
            # .probe_result). Accept either shape so this program keeps working if the
            # composite workflow wrapper is ever restored.
            sample_result = getattr(prealign_result, "sample_result", prealign_result)
            if sample_result is not None:
                START_X, START_Y, START_Z = sample_result.x_um, sample_result.y_um, sample_result.z_um
                SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM = sample_result.scan_range_x_um, sample_result.scan_range_y_um, sample_result.step_um
                SCAN_PATTERN = sample_result.scan_pattern
                SCAN_W, SCAN_H = scan_shape_from_range(SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM, max_range_um=NANOMAX_PIEZO_SCAN_LIMIT_UM)
                SERPENTINE_SCAN, SCAN_PATTERN_LABEL = resolve_scan_pattern(SCAN_PATTERN)
                append_run_log(
                    "PREALIGN_START_POSITION_SELECTED",
                    x_um=f"{START_X:.4f}",
                    y_um=f"{START_Y:.4f}",
                    z_um=f"{START_Z:.4f}",
                    scan_range_x_um=SCAN_RANGE_X_UM,
                    scan_range_y_um=SCAN_RANGE_Y_UM,
                    step_um=STEP_UM,
                    scan_pattern=SCAN_PATTERN_LABEL,
                    start_panel="sample",
                )
        append_run_log(
            "SCAN_CONFIG",
            scan_target=SCAN_TARGET,
            scan_range_x_um=SCAN_RANGE_X_UM,
            scan_range_y_um=SCAN_RANGE_Y_UM,
            step_um=STEP_UM,
            scan_w=SCAN_W,
            scan_h=SCAN_H,
            scan_pattern=SCAN_PATTERN_LABEL,
            sample_start_zero_policy=SAMPLE_START_ZERO_POLICY,
            sample_zero_at_start=SAMPLE_ZERO_XY_AT_START,
            sample_return_xy_to_zero_at_end=SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            sample_start_zero_reason=SAMPLE_START_ZERO_REASON,
            sample_zero_at_end=SAMPLE_ZERO_XY_AT_END,
            sample_prealign_enable=SAMPLE_PREALIGN_ENABLE,
        )
        trajectory = build_sample_trajectory(
            START_X,
            START_Y,
            SCAN_W,
            SCAN_H,
            STEP_UM,
            x_direction=SAMPLE_X_DIRECTION,
            y_direction=SAMPLE_Y_DIRECTION,
            serpentine=SERPENTINE_SCAN,
        )
        validate_sample_trajectory(stage, trajectory)
        xs = [point[0] for point in trajectory]
        ys = [point[1] for point in trajectory]
        total_points = len(trajectory)
        append_run_log(
            "TRAJECTORY_READY",
            scan_target=SCAN_TARGET,
            x_min_um=f"{min(xs):.4f}",
            x_max_um=f"{max(xs):.4f}",
            y_min_um=f"{min(ys):.4f}",
            y_max_um=f"{max(ys):.4f}",
            points=total_points,
            pattern=SCAN_PATTERN_LABEL,
        )
        print(
            "Closed-loop sample trajectory accepted: "
            f"X={min(xs):.4f}..{max(xs):.4f} um, "
            f"Y={min(ys):.4f}..{max(ys):.4f} um, "
            f"pattern={SCAN_PATTERN_LABEL}, points={len(trajectory)}."
        )
        coordinate_unit = "um"

        if daq_init is not None:
            snapshot = daq_init.snapshot()
            append_run_log("DAQ_INIT_WAIT_BEGIN", status=snapshot["status"], step=snapshot["step"], elapsed_s=f"{snapshot['elapsed_s']:.3f}")
            if snapshot["status"] != "ready":
                print(f"Waiting for background DAQ init: status={snapshot['status']}, step={snapshot['step']}...")
            daq = daq_init.result()
            snapshot = daq_init.snapshot()
            append_run_log("DAQ_INIT_DONE", mode="background", elapsed_s=f"{snapshot['elapsed_s']:.3f}", timings=snapshot["timings"])
        else:
            append_run_log("DAQ_INIT_BEGIN", mode="synchronous", sample_rate=SAMPLE_RATE, records_per_point=RECORDS_PER_POINT, buffer_count=BUFFER_COUNT)
            step_start = time.time()
            daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY, channel_A_range=ats.INPUT_RANGE_PM_200_MV)
            append_run_log("DAQ_INIT_STEP_DONE", mode="synchronous", step="create_system", duration_s=f"{time.time() - step_start:.3f}")
            step_start = time.time()
            daq.configure_board(sample_rate=SAMPLE_RATE)
            append_run_log("DAQ_INIT_STEP_DONE", mode="synchronous", step="configure_board", duration_s=f"{time.time() - step_start:.3f}")
            step_start = time.time()
            daq.prepare_acquisition(
                acq_channel=ats.CHANNEL_A,
                samples_per_record=SAMPLES_REC,
                records_per_buffer=RECORDS_PER_POINT,
                buffer_count=BUFFER_COUNT,
                records_per_point=RECORDS_PER_POINT,
            )
            append_run_log("DAQ_INIT_STEP_DONE", mode="synchronous", step="prepare_acquisition", duration_s=f"{time.time() - step_start:.3f}")
            append_run_log("DAQ_INIT_DONE", mode="synchronous")

        gc.disable()
        if prealignment_started_acquisition:
            append_run_log("USER_START_CONFIRMED", source="prealign_panel_start_command")
        else:
            append_run_log("WAITING_FOR_USER_START")
            input("Press Enter to START Experiment... (this program has no laser control)")
            append_run_log("USER_START_CONFIRMED", source="enter_prompt")
        progress_desc = "PAM sample closed-loop scan"
        laser_manager.refresh_status()
        if USER_STOP_ENABLE:
            append_run_log("USER_STOP_POLLING_ENABLED", stop_key=USER_STOP_KEY)
        scan_dashboard_items = [
            ("SCAN_TARGET", SCAN_TARGET, "frozen"),
            ("SCAN_PATTERN", SCAN_PATTERN_LABEL, "frozen"),
            ("SCAN_W", SCAN_W, "frozen"),
            ("SCAN_H", SCAN_H, "frozen"),
            ("TOTAL_POINTS", len(trajectory), "frozen"),
            ("SCAN_RANGE_X_UM", f"{SCAN_RANGE_X_UM:g}", "frozen"),
            ("SCAN_RANGE_Y_UM", f"{SCAN_RANGE_Y_UM:g}", "frozen"),
            ("STEP_UM", f"{STEP_UM:g}", "frozen"),
            ("START_X", f"{float(START_X):.4f}", f"{coordinate_unit}; frozen"),
            ("START_Y", f"{float(START_Y):.4f}", f"{coordinate_unit}; frozen"),
            ("START_Z", f"{float(START_Z):.4f}", f"{coordinate_unit}; frozen"),
            ("SETTLE_MS", SETTLE_MS, "frozen"),
            ("POS_TOL_UM", f"{SAMPLE_POSITION_TOLERANCE_UM:g}", "frozen"),
            ("POS_TIMEOUT_S", f"{SAMPLE_POSITION_TIMEOUT_S:g}", "frozen"),
            ("POS_REISSUE_S", f"{SAMPLE_POSITION_REISSUE_INTERVAL_S:g}", "frozen"),
            ("RETURN_STEP_UM", f"{SAMPLE_RETURN_STEP_UM:g}", "frozen"),
            ("RETURN_POS_TIMEOUT_S", f"{SAMPLE_RETURN_POSITION_TIMEOUT_S:g}", "frozen"),
        ]
        daq_dashboard_items = [
            ("DELAY", DELAY, "frozen"),
            ("SAMPLE_RATE", SAMPLE_RATE, "frozen"),
            ("SAMPLES_REC", SAMPLES_REC, "frozen"),
            ("RECORDS_PER_POINT", RECORDS_PER_POINT, "frozen"),
            ("BUFFER_COUNT", BUFFER_COUNT, "frozen"),
            ("AVERAGE_ENABLE", AVERAGE_ENABLE, "frozen"),
            ("ACQ_TIMEOUT_MS", ACQ_TIMEOUT_MS, "frozen"),
        ]
        runtime_dashboard_items = [
            ("POINT_LOG_INTERVAL", POINT_LOG_INTERVAL, "frozen"),
            ("USER_STOP_ENABLE", USER_STOP_ENABLE, "frozen"),
            ("USER_STOP_KEY", USER_STOP_KEY, "frozen"),
            ("SAMPLE_START_ZERO_POLICY", SAMPLE_START_ZERO_POLICY, "frozen"),
            ("SAMPLE_RETURN_XY_TO_ZERO_AT_END", SAMPLE_RETURN_XY_TO_ZERO_AT_END, "frozen"),
            ("SAMPLE_ZERO_XY_AT_END", SAMPLE_ZERO_XY_AT_END, "frozen"),
            ("DATA_SAVE_AUTO_TIMEOUT_S", f"{DATA_SAVE_AUTO_TIMEOUT_S:g}", "frozen"),
            ("RESULT_PREVIEW_ENABLE", RESULT_PREVIEW_ENABLE, "frozen"),
            ("RESULT_PREVIEW_OUTPUT", RESULT_PREVIEW_OUTPUT_DIR, "frozen"),
        ]
        refresh_terminal_for_acquisition()
        acquisition_dashboard = AcquisitionDashboard(
            desc=progress_desc,
            total=len(trajectory),
            laser_manager=laser_manager,
            stop_key=USER_STOP_KEY,
            stop_enabled=USER_STOP_ENABLE,
            scan_items=scan_dashboard_items,
            daq_items=daq_dashboard_items,
            runtime_items=runtime_dashboard_items,
            result_preview_controller=result_preview_controller,
            mat_path_provider=current_preview_mat_path,
            pause_z_controller=(
                PauseZMotionController(
                    stage,
                    step_um=SAMPLE_PREALIGN_Z_STEP_UM,
                    settle_ms=SETTLE_MS,
                    tolerance_um=SAMPLE_POSITION_TOLERANCE_UM,
                    timeout_s=SAMPLE_RETURN_POSITION_TIMEOUT_S,
                    reissue_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
                    min_step_um=NANOMAX_MANUAL_MIN_STEP_UM,
                    log_callback=append_run_log,
                )
                if stage is not None
                else None
            ),
            log_callback=append_run_log,
            laser_section_title="Laser Control",
            allowed_hint="none - this program has no laser control; use the stop key only",
            command_hint="no in-scan commands besides the stop key",
        )
        acquisition_dashboard.start()
        append_run_log("ACQUISITION_START", points=len(trajectory), desc=progress_desc)
        acquisition_loop_start_s = time.monotonic()

        for point_index, (tx, ty) in enumerate(trajectory, start=1):
            if acquisition_dashboard is not None and acquisition_dashboard.poll_commands():
                user_stop_requested = True
                append_run_log(
                    "ACQUISITION_USER_STOP_BEFORE_POINT",
                    index=point_index,
                    total=len(trajectory),
                    acquired_points=acquired_points,
                )
                break
            stage.set_position([tx, ty])
            position_settle_ok = True
            position_timeout = False
            actual_x, actual_y, actual_z = float(tx), float(ty), 0.0
            position_error_x, position_error_y, position_error_z = 0.0, 0.0, 0.0
            try:
                stage.wait_until_settled(
                    tx,
                    ty,
                    settle_time_ms=SETTLE_MS,
                    tolerance_step=SAMPLE_POSITION_TOLERANCE_UM,
                    timeout_s=SAMPLE_POSITION_TIMEOUT_S,
                    correction_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
                )
                try:
                    readback = stage.get_position_values()
                    actual_x, actual_y = float(readback[0]), float(readback[1])
                    actual_z = float(readback[2]) if len(readback) > 2 else actual_z
                    position_error_x = abs(actual_x - float(tx))
                    position_error_y = abs(actual_y - float(ty))
                except Exception as read_exc:
                    append_run_log(
                        "ACQUISITION_POSITION_READBACK_FAILED",
                        index=point_index,
                        total=len(trajectory),
                        target_x_um=f"{tx:.6f}",
                        target_y_um=f"{ty:.6f}",
                        error=repr(read_exc),
                    )
            except TimeoutError as exc:
                position_timeout_points += 1
                position_settle_ok = False
                position_timeout = True
                try:
                    readback = stage.get_position_values()
                    actual_x, actual_y = float(readback[0]), float(readback[1])
                    actual_z = float(readback[2]) if len(readback) > 2 else 0.0
                except Exception as read_exc:
                    append_run_log(
                        "ACQUISITION_POSITION_TIMEOUT_READBACK_FAILED",
                        index=point_index,
                        total=len(trajectory),
                        target_x_um=f"{tx:.6f}",
                        target_y_um=f"{ty:.6f}",
                        error=repr(read_exc),
                    )
                position_error_x = abs(actual_x - float(tx))
                position_error_y = abs(actual_y - float(ty))
                position_error_z = abs(actual_z - 0.0)
                append_run_log(
                    "ACQUISITION_POSITION_TIMEOUT_CONTINUE",
                    index=point_index,
                    total=len(trajectory),
                    target_x_um=f"{tx:.6f}",
                    target_y_um=f"{ty:.6f}",
                    actual_x_um=f"{actual_x:.6f}",
                    actual_y_um=f"{actual_y:.6f}",
                    error_x_um=f"{position_error_x:.6f}",
                    error_y_um=f"{position_error_y:.6f}",
                    tolerance_um=f"{SAMPLE_POSITION_TOLERANCE_UM:g}",
                    timeout_s=f"{SAMPLE_POSITION_TIMEOUT_S:g}",
                    exception=str(exc),
                )
                if acquisition_dashboard is not None:
                    acquisition_dashboard.update(
                        acquired_points,
                        current_position=(
                            f"TARGET X={tx:.4f}, Y={ty:.4f}; "
                            f"ACTUAL X={actual_x:.4f}, Y={actual_y:.4f}; collecting current signal"
                        ),
                        message="Position timeout; acquiring at current readback and continuing.",
                    )
            current_pos_str = f"{tx},{ty},0"
            point_metadata = {
                "target_pos_str": current_pos_str,
                "actual_pos_str": f"{actual_x},{actual_y},{actual_z}",
                "position_settle_ok": position_settle_ok,
                "position_timeout": position_timeout,
                "position_error_x_um": position_error_x,
                "position_error_y_um": position_error_y,
                "position_error_z_um": position_error_z,
            }
            data_len_before = len(all_data)
            daq.get_one_acquisition(
                all_data=all_data,
                curr_pos_str=current_pos_str,
                timeout_ms=ACQ_TIMEOUT_MS,
                Average_Enable=AVERAGE_ENABLE,
            )
            attach_point_metadata(data_len_before, point_metadata)
            if len(all_data) > data_len_before and point_payload_is_empty(all_data[-1][0]):
                daq_empty_points += 1
                daq_empty_streak += 1
                append_run_log(
                    "ACQUISITION_POINT_EMPTY",
                    index=point_index,
                    total=len(trajectory),
                    x_um=f"{tx:.4f}",
                    y_um=f"{ty:.4f}",
                    consecutive=daq_empty_streak,
                    daq_empty_points=daq_empty_points,
                    reason="daq_returned_no_buffer",
                )
            else:
                daq_empty_streak = 0
            acquired_points += 1
            if point_index == 1 or point_index == len(trajectory) or point_index % POINT_LOG_INTERVAL == 0:
                append_run_log(
                    "ACQUISITION_POINT_DONE",
                    index=point_index,
                    total=len(trajectory),
                    x_um=f"{tx:.4f}",
                    y_um=f"{ty:.4f}",
                    actual_x_um=f"{actual_x:.4f}",
                    actual_y_um=f"{actual_y:.4f}",
                    position_settle_ok=position_settle_ok,
                )
            if acquisition_dashboard is not None:
                acquisition_dashboard.update(
                    acquired_points,
                    current_position=(
                        f"X={tx:.4f} um, Y={ty:.4f} um, Z={actual_z:.4f} um"
                        if position_settle_ok
                        else f"TARGET X={tx:.4f}, Y={ty:.4f}; ACTUAL X={actual_x:.4f}, Y={actual_y:.4f}, Z={actual_z:.4f}"
                    ),
                )
            if acquisition_dashboard is not None and acquisition_dashboard.poll_commands():
                user_stop_requested = True
                append_run_log(
                    "ACQUISITION_USER_STOP_AFTER_POINT",
                    index=point_index,
                    total=len(trajectory),
                    acquired_points=acquired_points,
                    x_um=f"{tx:.4f}",
                    y_um=f"{ty:.4f}",
                )
                break

        acquisition_duration_s = time.monotonic() - acquisition_loop_start_s
        if acquisition_dashboard is not None:
            acquisition_dashboard.update(acquired_points, message="Acquisition loop finished; returning stages.")
            acquisition_dashboard.close()
        end_reason = "user_stop" if user_stop_requested else "completed"
        append_run_log(
            "ACQUISITION_DONE",
            acquired_points=acquired_points,
            expected_points=total_points,
            end_reason=end_reason,
            position_timeout_points=position_timeout_points,
            daq_empty_points=daq_empty_points,
        )
        if (
            end_reason == "completed"
            and acquired_points == total_points
            and len(all_data) >= acquired_points
            and position_timeout_points == 0
        ):
            record_successful_scan_speed(
                scan_target=SCAN_TARGET,
                step_um=STEP_UM,
                scan_range_x_um=SCAN_RANGE_X_UM,
                scan_range_y_um=SCAN_RANGE_Y_UM,
                scan_w=SCAN_W,
                scan_h=SCAN_H,
                points=acquired_points,
                acquisition_duration_s=acquisition_duration_s,
                scan_pattern=SCAN_PATTERN_LABEL,
                records_per_point=RECORDS_PER_POINT,
                samples_per_record=SAMPLES_REC,
                average_enable=AVERAGE_ENABLE,
                acq_timeout_ms=ACQ_TIMEOUT_MS,
            )
        else:
            append_run_log(
                "SCAN_SPEED_RECORD_SKIPPED",
                reason="not_full_success",
                end_reason=end_reason,
                acquired_points=acquired_points,
                expected_points=total_points,
                data_points=len(all_data),
                position_timeout_points=position_timeout_points,
            )

        return_to_start(
            SCAN_TARGET,
            stage,
            None,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            False,
            SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            SAMPLE_ZERO_XY_AT_END,
            SAMPLE_ZERO_AXES,
            sample_return_step_um=SAMPLE_RETURN_STEP_UM,
            sample_position_tolerance_um=SAMPLE_POSITION_TOLERANCE_UM,
            sample_position_timeout_s=SAMPLE_RETURN_POSITION_TIMEOUT_S,
            sample_position_reissue_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
        )
        append_run_log(
            "RUN_END_NORMAL",
            acquired_points=acquired_points,
            expected_points=total_points,
            end_reason=end_reason,
            daq_empty_points=daq_empty_points,
        )

    except KeyboardInterrupt:
        append_run_log("RUN_END_INTERRUPTED", acquired_points=acquired_points, expected_points=total_points, daq_empty_points=daq_empty_points)
        print("\nUser interrupted the scan.")
        stop_daq_best_effort("keyboard_interrupt")
        save_data_once("keyboard_interrupt")
        safe_return_to_start(
            SCAN_TARGET,
            stage,
            None,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            False,
            SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            SAMPLE_ZERO_XY_AT_END,
            SAMPLE_ZERO_AXES,
            sample_return_step_um=SAMPLE_RETURN_STEP_UM,
            sample_position_tolerance_um=SAMPLE_POSITION_TOLERANCE_UM,
            sample_position_timeout_s=SAMPLE_RETURN_POSITION_TIMEOUT_S,
            sample_position_reissue_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
        )
    except Exception as exc:
        append_run_log(
            "RUN_END_ERROR",
            error=repr(exc),
            acquired_points=acquired_points,
            expected_points=total_points,
            daq_empty_points=daq_empty_points,
            traceback=traceback.format_exc(limit=6),
        )
        print(f"\nExperiment error: {exc}")
        stop_daq_best_effort("exception")
        save_data_once("exception")
        safe_return_to_start(
            SCAN_TARGET,
            stage,
            None,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            False,
            SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            SAMPLE_ZERO_XY_AT_END,
            SAMPLE_ZERO_AXES,
            sample_return_step_um=SAMPLE_RETURN_STEP_UM,
            sample_position_tolerance_um=SAMPLE_POSITION_TOLERANCE_UM,
            sample_position_timeout_s=SAMPLE_RETURN_POSITION_TIMEOUT_S,
            sample_position_reissue_interval_s=SAMPLE_POSITION_REISSUE_INTERVAL_S,
        )
        append_run_log("RUN_END_ERROR_HANDLED", acquired_points=acquired_points, expected_points=total_points)
    finally:
        append_run_log("FINAL_CLEANUP_BEGIN")
        time.sleep(1)
        try:
            gc.enable()
            if daq is None and daq_init is not None and not preacquisition_quit_requested:
                try:
                    daq = daq_init.result()
                    append_run_log("DAQ_INIT_JOINED_DURING_CLEANUP", status=daq_init.snapshot()["status"])
                except Exception as exc:
                    append_run_log("DAQ_INIT_CLEANUP_JOIN_ERROR", error=repr(exc))
            elif daq is None and daq_init is not None and preacquisition_quit_requested:
                snapshot = daq_init.snapshot()
                append_run_log(
                    "DAQ_INIT_CLEANUP_JOIN_SKIPPED",
                    reason="preacquisition_quit",
                    status=snapshot.get("status", "-"),
                    step=snapshot.get("step", "-"),
                    elapsed_s=f"{float(snapshot.get('elapsed_s', 0.0)):.3f}",
                )
            stop_daq_best_effort("finally")
            if acquisition_dashboard is not None:
                acquisition_dashboard.close()
            if stage is not None:
                stage.close()
            append_run_log("FINAL_CLEANUP_DONE")
        except Exception as exc:
            append_run_log("FINAL_CLEANUP_ERROR", error=repr(exc))
            print(f"Cleanup error: {exc}")

        if not data_save_done and not preacquisition_quit_requested:
            save_data_once("finally")
        elif preacquisition_quit_requested:
            append_run_log("DATA_SAVE_SKIPPED", reason="preacquisition_quit_no_acquisition")


if __name__ == "__main__":
    main()
