import datetime
import gc
import os
import sys
import time
import traceback

import atsapi as ats

from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Alazar_imaging.BPC303NativeController import BPC303NativeController
from Alazar_imaging.MDT693BController import MDT693BController
from Nanomax.daq_async import BackgroundDaqInit
from Nanomax.data_io import save_scan_data
from Nanomax.open_loop_panel import ProbePrealignConfig
from Nanomax.prealign_panel import SamplePrealignConfig
from Nanomax.prealignment_workflow import run_nanomax_prealignment
from Nanomax.run_log import RUN_LOG_PATH, append_run_log, inspect_previous_run, resolve_start_zero_policy, set_current_run_id
from Nanomax.runtime import find_other_pam_processes, poll_user_stop_request, return_to_start, run_bpc303_preflight, safe_return_to_start
from Nanomax.scan_utils import (
    NANOMAX_PIEZO_SCAN_LIMIT_UM,
    build_probe_trajectory,
    build_sample_trajectory,
    clamp_low_end_residual,
    resolve_probe_step_v,
    resolve_scan_pattern,
    scan_shape_from_range,
    validate_probe_trajectory,
    validate_sample_trajectory,
)


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def refresh_terminal_for_acquisition():
    """Clear old setup output immediately before showing the acquisition progress bar."""
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def main():
    previous_run = inspect_previous_run()
    CURRENT_RUN_ID = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    set_current_run_id(CURRENT_RUN_ID)
    append_run_log(
        "RUN_START",
        log_path=RUN_LOG_PATH,
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
    if previous_run["need_start_zero"]:
        print(
            "\nPrevious PAM run did not leave a trusted normal-cleanup + low-end X/Y zero-datum marker; "
            f"status={previous_run['status']}, run_id={previous_run['run_id']}, reason={previous_run['zero_reason']}. "
            "Startup X/Y zero will be rebuilt when sample_closed_loop is used."
        )
        append_run_log("PREVIOUS_RUN_WARNING", previous_line=previous_run["line"])
    else:
        print(
            "\nPrevious PAM run completed normal cleanup and left a trusted low-end X/Y datum marker; "
            f"status={previous_run['status']}, run_id={previous_run['run_id']}. "
            "Auto start-zero can be skipped."
        )
        append_run_log(
            "PREVIOUS_RUN_ZERO_READY",
            previous_zero_line=previous_run["zero_line"],
            previous_final_line=previous_run["final_line"],
        )

    # Stage selection:
    #   sample_closed_loop: move the MAX311D sample stage with BPC303 in microns.
    #   probe_open_loop: keep the sample fixed and move the MAX312D probe stage by MDT693B voltages.
    SCAN_TARGET = "sample_closed_loop"

    # Closed-loop sample NanoMax: MAX311D on BPC303. User-confirmed axis mapping: channel 1/2/3 = X/Y/Z.
    BPC303_SERIAL_NO, BPC303_KINESIS_DIR = "71241834", r"C:\Program Files\Thorlabs\Kinesis"
    BPC303_AXIS_MAP, BPC303_SAFE_MAX_OUTPUT_VOLTAGE = {"x": 1, "y": 2, "z": 3}, 75.0
    BPC303_PREFLIGHT_ENABLE, BPC303_PREFLIGHT_TIMEOUT_S = False, 20.0
    # Disabled by default to avoid ~6 s startup overhead. Enable only when diagnosing Kinesis/USB hangs.
    # "open_close" does not enable channels, zero axes, or set position.
    BPC303_PREFLIGHT_MODE = "open_close"

    # Open-loop probe NanoMax: MAX312D on MDT693B.
    PROBE_MDT_SERIAL_NO, PROBE_MDT_SERIAL_PORT, PROBE_MDT_BACKEND = "2201287140-09", None, "serial"
    PROBE_MDT_DLL_PATH = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\Alazar_imaging\MDT_COMMAND_LIB_x64.dll"
    PROBE_SAFE_MAX_VOLTAGE, PROBE_PIEZO_TRAVEL_UM, PROBE_PIEZO_TRAVEL_VOLTAGE = 75.0, 20.0, 75.0
    PROBE_STEP_V, PROBE_UM_PER_V = None, PROBE_PIEZO_TRAVEL_UM / PROBE_PIEZO_TRAVEL_VOLTAGE
    PROBE_SCAN_FAST_AXIS, PROBE_SCAN_SLOW_AXIS = "y", "z"
    PROBE_FAST_DIRECTION, PROBE_SLOW_DIRECTION, PROBE_Z_HOLD_V, PROBE_RETURN_TO_START = 1.0, 1.0, None, True
    PROBE_PREALIGN_ENABLE, PROBE_PREALIGN_Y_STEP_V, PROBE_PREALIGN_Z_STEP_V = True, 1.0, 1.0
    PROBE_PREALIGN_INTERVAL_S, PROBE_PREALIGN_SET_AXIS_MAX = 0.25, True
    PROBE_PREALIGN_REQUIRE_CONTROLLER = False

    # User scan geometry. Ranges are the requested travel from first to last point.
    # Example: 20 um range with 1 um step gives 21 points: 0, 1, ..., 20 um.
    # The script also checks against the BPC303-reported maximum travel before acquisition.
    SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM = 2.5, 2.5, 0.01  # X is actually up; Y is actually left.
    SAMPLE_X_DIRECTION, SAMPLE_Y_DIRECTION = 1.0, 1.0
    SAMPLE_PREALIGN_ENABLE, SAMPLE_PREALIGN_X_STEP_UM, SAMPLE_PREALIGN_Y_STEP_UM, SAMPLE_PREALIGN_Z_STEP_UM = True, 0.1, 0.1, 0.1
    SAMPLE_PREALIGN_INTERVAL_S = 0.25
    # Startup zero policy:
    #   "auto": rebuild X/Y zero unless the previous log has a trusted low-end zero-datum marker.
    #   "always": rebuild X/Y zero every run.
    #   "never": never rebuild at start; only use this when you know the current datum is valid.
    # Z is intentionally excluded by default to avoid changing focus/clearance.
    SAMPLE_START_ZERO_POLICY, SAMPLE_ZERO_XY_AT_START, SAMPLE_START_ZERO_REASON = "auto", True, "not_resolved"
    SAMPLE_ZERO_AXES, SAMPLE_LOW_END_RESIDUAL_TOLERANCE_UM, SAMPLE_RETURN_XY_TO_ZERO_AT_END = ("x", "y"), 0.01, True
    # False avoids a ~45-60 s BPC303 SetZero cycle at every normal end.
    # Set True only when you explicitly want the controller outputs forced to 0 V after each run.
    SAMPLE_ZERO_XY_AT_END = False
    # SCAN_PATTERN:
    #   "serpentine" or "s": S-shaped scan; odd rows reverse X direction.
    #   "raster" or "z": Z-shaped one-way rows; each row starts from low X.
    SCAN_PATTERN, SETTLE_MS = "serpentine", 120

    # DAQ parameters.
    DELAY, SAMPLES_REC, SAMPLE_RATE = 1320, 4096, ats.SAMPLE_RATE_4000MSPS
    AVERAGE_ENABLE, RECORDS_PER_POINT, BUFFER_COUNT = True, 256, 4
    POINT_LOG_INTERVAL, USER_STOP_ENABLE, USER_STOP_KEY = 25, True, "q"

    if SCAN_TARGET not in ("sample_closed_loop", "probe_open_loop"):
        append_run_log("RUN_END_ERROR", error=f"invalid SCAN_TARGET {SCAN_TARGET}")
        raise ValueError("SCAN_TARGET must be 'sample_closed_loop' or 'probe_open_loop'.")
    try:
        SCAN_W, SCAN_H = scan_shape_from_range(
            SCAN_RANGE_X_UM,
            SCAN_RANGE_Y_UM,
            STEP_UM,
            max_range_um=NANOMAX_PIEZO_SCAN_LIMIT_UM,
        )
        SERPENTINE_SCAN, SCAN_PATTERN_LABEL = resolve_scan_pattern(SCAN_PATTERN)
        SAMPLE_ZERO_XY_AT_START, SAMPLE_START_ZERO_REASON = resolve_start_zero_policy(
            SAMPLE_START_ZERO_POLICY,
            previous_run,
        )
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
        sample_start_zero_reason=SAMPLE_START_ZERO_REASON,
        sample_zero_at_end=SAMPLE_ZERO_XY_AT_END,
        sample_low_end_residual_tolerance_um=SAMPLE_LOW_END_RESIDUAL_TOLERANCE_UM,
        point_log_interval=POINT_LOG_INTERVAL,
        user_stop_enable=USER_STOP_ENABLE,
        user_stop_key=USER_STOP_KEY,
        bpc303_preflight_enable=BPC303_PREFLIGHT_ENABLE,
        bpc303_preflight_mode=BPC303_PREFLIGHT_MODE,
        bpc303_preflight_timeout_s=BPC303_PREFLIGHT_TIMEOUT_S,
        sample_prealign_enable=SAMPLE_PREALIGN_ENABLE,
        probe_prealign_enable=PROBE_PREALIGN_ENABLE,
        probe_scan_fast_axis=PROBE_SCAN_FAST_AXIS,
        probe_scan_slow_axis=PROBE_SCAN_SLOW_AXIS,
    )
    other_pam_processes = find_other_pam_processes()
    if other_pam_processes:
        append_run_log("RUN_END_ERROR", error="another_PAM_Main_Nanomax_process_is_running", active_processes=" ; ".join(other_pam_processes))
        raise SystemExit(
            "Another PAM_Main_Nanomax.py process is still running and may hold the controller:\n"
            + "\n".join(other_pam_processes)
            + "\nClose that console/process or reboot the experiment PC before starting a new scan."
        )
    if SCAN_TARGET == "sample_closed_loop" and BPC303_PREFLIGHT_ENABLE:
        run_bpc303_preflight(
            BPC303_SERIAL_NO,
            BPC303_KINESIS_DIR,
            timeout_s=BPC303_PREFLIGHT_TIMEOUT_S,
            mode=BPC303_PREFLIGHT_MODE,
        )

    stage = probe_stage = probe_step_v = daq = daq_init = None
    all_data, START_X, START_Y, START_Z = [], None, None, None
    coordinate_unit, total_points, acquired_points, user_stop_requested = "um", 0, 0, False
    prealignment_started_acquisition = False
    probe_connect_error = ""

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

        if SCAN_TARGET == "sample_closed_loop":
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
            if PROBE_PREALIGN_ENABLE:
                print("Opening MDT693B open-loop probe control for optional prealignment...")
                append_run_log("STAGE_CONNECT_BEGIN", controller="MDT693B", stage_model="MAX312D", reason="probe_prealignment")
                try:
                    probe_stage = MDT693BController(
                        serial_no=PROBE_MDT_SERIAL_NO,
                        dll_path=PROBE_MDT_DLL_PATH,
                        safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
                        um_per_volt=PROBE_UM_PER_V,
                        backend=PROBE_MDT_BACKEND,
                        serial_port=PROBE_MDT_SERIAL_PORT,
                    )
                    append_run_log(
                        "STAGE_CONNECT_DONE",
                        controller="MDT693B",
                        serial=probe_stage.serial_no,
                        serial_port=probe_stage.serial_port,
                        active_backend=getattr(probe_stage, "_active_backend", "-"),
                        device_id=probe_stage.device_id,
                        limit_voltage=probe_stage.limit_voltage,
                        safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
                        reason="probe_prealignment",
                    )
                except Exception as exc:
                    probe_connect_error = repr(exc)
                    append_run_log("PROBE_PREALIGN_UNAVAILABLE", error=repr(exc), required=PROBE_PREALIGN_REQUIRE_CONTROLLER)
                    print(f"Probe prealignment unavailable; continuing with closed-loop sample panel only: {exc}")
                    if PROBE_PREALIGN_REQUIRE_CONTROLLER:
                        raise
                    probe_stage = None
            if SAMPLE_PREALIGN_ENABLE or (PROBE_PREALIGN_ENABLE and probe_stage is not None):
                daq_init = BackgroundDaqInit(daq_factory, log_callback=append_run_log)
                append_run_log("DAQ_INIT_BACKGROUND_REQUESTED", sample_rate=SAMPLE_RATE, records_per_point=RECORDS_PER_POINT, buffer_count=BUFFER_COUNT)
                daq_init.start()
                prealign_result = run_nanomax_prealignment(
                    sample_stage=stage if SAMPLE_PREALIGN_ENABLE else None,
                    sample_config=SamplePrealignConfig(
                        scan_range_x_um=SCAN_RANGE_X_UM,
                        scan_range_y_um=SCAN_RANGE_Y_UM,
                        step_um=STEP_UM,
                        sample_x_direction=SAMPLE_X_DIRECTION,
                        sample_y_direction=SAMPLE_Y_DIRECTION,
                        scan_pattern=SCAN_PATTERN,
                        settle_ms=SETTLE_MS,
                        x_step_um=SAMPLE_PREALIGN_X_STEP_UM,
                        y_step_um=SAMPLE_PREALIGN_Y_STEP_UM,
                        z_step_um=SAMPLE_PREALIGN_Z_STEP_UM,
                        sample_interval_s=SAMPLE_PREALIGN_INTERVAL_S,
                    ),
                    probe_stage=probe_stage if (PROBE_PREALIGN_ENABLE and probe_stage is not None) else None,
                    probe_config=ProbePrealignConfig(
                        safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
                        piezo_travel_um=PROBE_PIEZO_TRAVEL_UM,
                        piezo_travel_voltage=PROBE_PIEZO_TRAVEL_VOLTAGE,
                        y_step_v=PROBE_PREALIGN_Y_STEP_V,
                        z_step_v=PROBE_PREALIGN_Z_STEP_V,
                        sample_interval_s=PROBE_PREALIGN_INTERVAL_S,
                        settle_ms=SETTLE_MS,
                        set_axis_max=PROBE_PREALIGN_SET_AXIS_MAX,
                    ),
                    initial_panel="sample" if SAMPLE_PREALIGN_ENABLE else "probe",
                    log_callback=append_run_log,
                    status_provider=daq_init.snapshot,
                    display_params={
                        "SCAN_TARGET": SCAN_TARGET,
                        "SAMPLE_CONTROLLER": "BPC303",
                        "SAMPLE_STAGE_MODEL": "MAX311D",
                        "SAMPLE_CONNECTION": "connected",
                        "SAMPLE_SERIAL": BPC303_SERIAL_NO,
                        "SAMPLE_AXIS_MAP": "1/2/3=X/Y/Z",
                        "PROBE_CONTROLLER": "MDT693B",
                        "PROBE_STAGE_MODEL": "MAX312D",
                        "PROBE_CONNECTION": "connected" if probe_stage is not None else ("unavailable" if PROBE_PREALIGN_ENABLE else "disabled"),
                        "PROBE_SERIAL": getattr(probe_stage, "serial_no", PROBE_MDT_SERIAL_NO or "-") if probe_stage is not None else (PROBE_MDT_SERIAL_NO or "-"),
                        "PROBE_PORT": getattr(probe_stage, "serial_port", PROBE_MDT_SERIAL_PORT or "-") if probe_stage is not None else (PROBE_MDT_SERIAL_PORT or "-"),
                        "PROBE_BACKEND": getattr(probe_stage, "_active_backend", PROBE_MDT_BACKEND) if probe_stage is not None else PROBE_MDT_BACKEND,
                        "PROBE_DEVICE_ID": getattr(probe_stage, "device_id", "-") if probe_stage is not None else "-",
                        "PROBE_LIMIT_V": getattr(probe_stage, "limit_voltage", "-") if probe_stage is not None else "-",
                        "PROBE_CONNECT_ERROR": probe_connect_error,
                        "DELAY": DELAY,
                        "SAMPLES_REC": SAMPLES_REC,
                        "SAMPLE_RATE": SAMPLE_RATE,
                        "AVERAGE_ENABLE": AVERAGE_ENABLE,
                        "RECORDS_PER_POINT": RECORDS_PER_POINT,
                        "BUFFER_COUNT": BUFFER_COUNT,
                        "POINT_LOG_INTERVAL": POINT_LOG_INTERVAL,
                        "USER_STOP_ENABLE": USER_STOP_ENABLE,
                        "USER_STOP_KEY": USER_STOP_KEY,
                        "SAMPLE_START_ZERO_POLICY": SAMPLE_START_ZERO_POLICY,
                        "SAMPLE_ZERO_XY_AT_END": SAMPLE_ZERO_XY_AT_END,
                        "PROBE_SCAN_AXES": f"{PROBE_SCAN_FAST_AXIS}/{PROBE_SCAN_SLOW_AXIS}",
                    },
                )
                prealignment_started_acquisition = True
                if prealign_result.sample_result is not None:
                    sample_result = prealign_result.sample_result
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
                        start_panel=prealign_result.start_panel,
                    )
                if prealign_result.probe_result is not None:
                    probe_result = prealign_result.probe_result
                    append_run_log(
                        "PROBE_PREALIGN_POSITION_SELECTED",
                        x_v=f"{probe_result.x_v:.4f}",
                        y_v=f"{probe_result.y_v:.4f}",
                        z_v=f"{probe_result.z_v:.4f}",
                        y_step_v=f"{probe_result.y_step_v:.4f}",
                        z_step_v=f"{probe_result.z_step_v:.4f}",
                        start_panel=prealign_result.start_panel,
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

        else:
            probe_step_v = resolve_probe_step_v(STEP_UM, PROBE_STEP_V, PROBE_UM_PER_V)
            print(f"Using MDT693B open-loop probe scan, step={probe_step_v} V/pixel...")
            append_run_log("STAGE_CONNECT_BEGIN", controller="MDT693B", stage_model="MAX312D")
            probe_stage = MDT693BController(
                serial_no=PROBE_MDT_SERIAL_NO,
                dll_path=PROBE_MDT_DLL_PATH,
                safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
                um_per_volt=PROBE_UM_PER_V,
                backend=PROBE_MDT_BACKEND,
                serial_port=PROBE_MDT_SERIAL_PORT,
            )
            append_run_log(
                "STAGE_CONNECT_DONE",
                controller="MDT693B",
                serial=probe_stage.serial_no,
                serial_port=probe_stage.serial_port,
                active_backend=getattr(probe_stage, "_active_backend", "-"),
                device_id=probe_stage.device_id,
                limit_voltage=probe_stage.limit_voltage,
                safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
            )
            START_X, START_Y, START_Z = [float(v) for v in probe_stage.get_voltage_xyz()]
            if PROBE_Z_HOLD_V is not None:
                START_Z = float(PROBE_Z_HOLD_V)
                probe_stage.set_voltage_xyz(z=START_Z, wait=True, settle_time_ms=SETTLE_MS)
            append_run_log(
                "START_POSITION",
                scan_target=SCAN_TARGET,
                x_v=f"{START_X:.4f}",
                y_v=f"{START_Y:.4f}",
                z_v=f"{START_Z:.4f}",
            )
            if PROBE_PREALIGN_ENABLE:
                daq_init = BackgroundDaqInit(daq_factory, log_callback=append_run_log)
                append_run_log("DAQ_INIT_BACKGROUND_REQUESTED", sample_rate=SAMPLE_RATE, records_per_point=RECORDS_PER_POINT, buffer_count=BUFFER_COUNT)
                daq_init.start()
                prealign_result = run_nanomax_prealignment(
                    probe_stage=probe_stage,
                    probe_config=ProbePrealignConfig(
                        safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
                        piezo_travel_um=PROBE_PIEZO_TRAVEL_UM,
                        piezo_travel_voltage=PROBE_PIEZO_TRAVEL_VOLTAGE,
                        y_step_v=PROBE_PREALIGN_Y_STEP_V,
                        z_step_v=PROBE_PREALIGN_Z_STEP_V,
                        sample_interval_s=PROBE_PREALIGN_INTERVAL_S,
                        settle_ms=SETTLE_MS,
                        set_axis_max=PROBE_PREALIGN_SET_AXIS_MAX,
                    ),
                    initial_panel="probe",
                    log_callback=append_run_log,
                    status_provider=daq_init.snapshot,
                    display_params={
                        "SCAN_TARGET": SCAN_TARGET,
                        "SAMPLE_CONTROLLER": "BPC303",
                        "SAMPLE_STAGE_MODEL": "MAX311D",
                        "SAMPLE_CONNECTION": "not-opened",
                        "SAMPLE_SERIAL": BPC303_SERIAL_NO,
                        "SAMPLE_AXIS_MAP": "1/2/3=X/Y/Z",
                        "PROBE_CONTROLLER": "MDT693B",
                        "PROBE_STAGE_MODEL": "MAX312D",
                        "PROBE_CONNECTION": "connected",
                        "PROBE_SERIAL": probe_stage.serial_no,
                        "PROBE_PORT": probe_stage.serial_port,
                        "PROBE_BACKEND": getattr(probe_stage, "_active_backend", PROBE_MDT_BACKEND),
                        "PROBE_DEVICE_ID": probe_stage.device_id,
                        "PROBE_LIMIT_V": probe_stage.limit_voltage,
                        "PROBE_SCAN_AXES": f"{PROBE_SCAN_FAST_AXIS}/{PROBE_SCAN_SLOW_AXIS}",
                    },
                )
                if prealign_result.probe_result is not None:
                    prealignment_started_acquisition = True
                    probe_result = prealign_result.probe_result
                    START_X, START_Y, START_Z = probe_result.x_v, probe_result.y_v, probe_result.z_v
                    append_run_log(
                        "PROBE_PREALIGN_START_POSITION_SELECTED",
                        x_v=f"{START_X:.4f}",
                        y_v=f"{START_Y:.4f}",
                        z_v=f"{START_Z:.4f}",
                        y_step_v=f"{probe_result.y_step_v:.4f}",
                        z_step_v=f"{probe_result.z_step_v:.4f}",
                        start_panel=prealign_result.start_panel,
                    )
            trajectory = build_probe_trajectory(
                START_X,
                START_Y,
                START_Z,
                SCAN_W,
                SCAN_H,
                probe_step_v,
                x_direction=PROBE_FAST_DIRECTION,
                y_direction=PROBE_SLOW_DIRECTION,
                serpentine=SERPENTINE_SCAN,
                fast_axis=PROBE_SCAN_FAST_AXIS,
                slow_axis=PROBE_SCAN_SLOW_AXIS,
            )
            validate_probe_trajectory(probe_stage, trajectory)
            coordinate_unit = "V"
            total_points = len(trajectory)
            xs = [point[0] for point in trajectory]
            ys = [point[1] for point in trajectory]
            zs = [point[2] for point in trajectory]
            append_run_log(
                "TRAJECTORY_READY",
                scan_target=SCAN_TARGET,
                points=total_points,
                pattern=SCAN_PATTERN_LABEL,
                probe_step_v=probe_step_v,
                probe_scan_fast_axis=PROBE_SCAN_FAST_AXIS,
                probe_scan_slow_axis=PROBE_SCAN_SLOW_AXIS,
                x_min_v=f"{min(xs):.4f}",
                x_max_v=f"{max(xs):.4f}",
                y_min_v=f"{min(ys):.4f}",
                y_max_v=f"{max(ys):.4f}",
                z_min_v=f"{min(zs):.4f}",
                z_max_v=f"{max(zs):.4f}",
            )
            print(
                f"Probe start voltage: X={START_X:.4f} V, Y={START_Y:.4f} V, Z={START_Z:.4f} V; "
                f"axes={PROBE_SCAN_FAST_AXIS}/{PROBE_SCAN_SLOW_AXIS}, "
                f"pattern={SCAN_PATTERN_LABEL}, points={len(trajectory)}."
            )

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
            input("Press Enter to START Experiment... (make sure the laser is enabled)")
            append_run_log("USER_START_CONFIRMED", source="enter_prompt")
        progress_desc = "PAM sample closed-loop scan" if SCAN_TARGET == "sample_closed_loop" else "PAM probe open-loop scan"
        refresh_terminal_for_acquisition()
        print(f"{progress_desc} starting: {len(trajectory)} points.")
        if USER_STOP_ENABLE:
            print(f"During acquisition, press '{USER_STOP_KEY}' to stop gracefully after the current point. Enter is optional.")
            append_run_log("USER_STOP_POLLING_ENABLED", stop_key=USER_STOP_KEY)

        progress_manager.start(total=len(trajectory), desc=progress_desc)
        append_run_log("ACQUISITION_START", points=len(trajectory), desc=progress_desc)

        if SCAN_TARGET == "sample_closed_loop":
            for point_index, (tx, ty) in enumerate(trajectory, start=1):
                stage.set_position([tx, ty])
                stage.wait_until_settled(tx, ty, settle_time_ms=SETTLE_MS)
                current_pos_str = f"{tx},{ty},0"
                daq.get_one_acquisition(
                    all_data=all_data,
                    curr_pos_str=current_pos_str,
                    timeout_ms=1000,
                    Average_Enable=AVERAGE_ENABLE,
                )
                acquired_points += 1
                if point_index == 1 or point_index == len(trajectory) or point_index % POINT_LOG_INTERVAL == 0:
                    append_run_log(
                        "ACQUISITION_POINT_DONE",
                        index=point_index,
                        total=len(trajectory),
                        x_um=f"{tx:.4f}",
                        y_um=f"{ty:.4f}",
                    )
                progress_manager.update(1)
                if poll_user_stop_request(USER_STOP_ENABLE, USER_STOP_KEY, notify=progress_manager.write):
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
        else:
            for point_index, (vx, vy, vz) in enumerate(trajectory, start=1):
                probe_stage.set_voltage_xyz(x=vx, y=vy, z=vz, wait=True, settle_time_ms=SETTLE_MS)
                current_pos_str = f"{vx},{vy},{vz}"
                daq.get_one_acquisition(
                    all_data=all_data,
                    curr_pos_str=current_pos_str,
                    timeout_ms=1000,
                    Average_Enable=AVERAGE_ENABLE,
                )
                acquired_points += 1
                if point_index == 1 or point_index == len(trajectory) or point_index % POINT_LOG_INTERVAL == 0:
                    append_run_log(
                        "ACQUISITION_POINT_DONE",
                        index=point_index,
                        total=len(trajectory),
                        x_v=f"{vx:.4f}",
                        y_v=f"{vy:.4f}",
                        z_v=f"{vz:.4f}",
                    )
                progress_manager.update(1)
                if poll_user_stop_request(USER_STOP_ENABLE, USER_STOP_KEY, notify=progress_manager.write):
                    user_stop_requested = True
                    append_run_log(
                        "ACQUISITION_USER_STOP_AFTER_POINT",
                        index=point_index,
                        total=len(trajectory),
                        acquired_points=acquired_points,
                        x_v=f"{vx:.4f}",
                        y_v=f"{vy:.4f}",
                        z_v=f"{vz:.4f}",
                    )
                    break
        progress_manager.stop()
        end_reason = "user_stop" if user_stop_requested else "completed"
        append_run_log(
            "ACQUISITION_DONE",
            acquired_points=acquired_points,
            expected_points=total_points,
            end_reason=end_reason,
        )

        return_to_start(
            SCAN_TARGET,
            stage,
            probe_stage,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            PROBE_RETURN_TO_START,
            SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            SAMPLE_ZERO_XY_AT_END,
            SAMPLE_ZERO_AXES,
        )
        append_run_log(
            "RUN_END_NORMAL",
            acquired_points=acquired_points,
            expected_points=total_points,
            end_reason=end_reason,
        )

    except KeyboardInterrupt:
        append_run_log("RUN_END_INTERRUPTED", acquired_points=acquired_points, expected_points=total_points)
        print("\nUser interrupted the scan.")
        safe_return_to_start(
            SCAN_TARGET,
            stage,
            probe_stage,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            PROBE_RETURN_TO_START,
            SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            SAMPLE_ZERO_XY_AT_END,
            SAMPLE_ZERO_AXES,
        )
    except Exception as exc:
        append_run_log(
            "RUN_END_ERROR",
            error=repr(exc),
            acquired_points=acquired_points,
            expected_points=total_points,
            traceback=traceback.format_exc(limit=6),
        )
        print(f"\nExperiment error: {exc}")
        safe_return_to_start(
            SCAN_TARGET,
            stage,
            probe_stage,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            PROBE_RETURN_TO_START,
            SAMPLE_RETURN_XY_TO_ZERO_AT_END,
            SAMPLE_ZERO_XY_AT_END,
            SAMPLE_ZERO_AXES,
        )
        raise
    finally:
        append_run_log("FINAL_CLEANUP_BEGIN")
        time.sleep(1)
        try:
            gc.enable()
            if daq is None and daq_init is not None:
                try:
                    daq = daq_init.result()
                    append_run_log("DAQ_INIT_JOINED_DURING_CLEANUP", status=daq_init.snapshot()["status"])
                except Exception as exc:
                    append_run_log("DAQ_INIT_CLEANUP_JOIN_ERROR", error=repr(exc))
            if daq is not None:
                daq.stop_capture()
            progress_manager.set_colour("green")
            progress_manager.stop()
            if probe_stage is not None:
                probe_stage.close()
            if stage is not None:
                stage.close()
            append_run_log("FINAL_CLEANUP_DONE")
        except Exception as exc:
            append_run_log("FINAL_CLEANUP_ERROR", error=repr(exc))
            print(f"Cleanup error: {exc}")

        save_scan_data(
            all_data,
            SCAN_W,
            SCAN_H,
            STEP_UM,
            RECORDS_PER_POINT,
            SAMPLES_REC,
            AVERAGE_ENABLE,
            SCAN_TARGET,
            coordinate_unit,
            probe_step_v,
            PROBE_UM_PER_V,
            START_X,
            START_Y,
            START_Z,
            DELAY,
        )


if __name__ == "__main__":
    main()
