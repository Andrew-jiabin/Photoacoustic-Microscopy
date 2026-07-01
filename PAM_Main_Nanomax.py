import datetime
import gc
import os
import time

import atsapi as ats
import numpy as np
import scipy.io as sio

from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Alazar_imaging.BPC303NativeController import BPC303NativeController
from Alazar_imaging.MDT693BController import MDT693BController
from Tool_code.position_trans import sanitize_pos_to_key


def resolve_probe_step_v(step_um, probe_step_v, probe_um_per_v):
    """Return the open-loop probe scan step in volts per pixel."""
    if probe_step_v is not None:
        return float(probe_step_v)
    if probe_um_per_v is not None:
        return float(step_um) / float(probe_um_per_v)
    raise ValueError(
        "Probe open-loop scan requires PROBE_STEP_V, or PROBE_UM_PER_V "
        "so STEP_UM can be converted to volts."
    )


def build_sample_trajectory(start_x, start_y, scan_w, scan_h, step_um, x_direction=1.0, y_direction=1.0, serpentine=False):
    """Build closed-loop sample-stage targets in microns."""
    trajectory = []
    for h in range(scan_h):
        w_range = range(scan_w)
        if serpentine and h % 2 == 1:
            w_range = reversed(range(scan_w))
        for w in w_range:
            target_x = start_x + x_direction * w * step_um
            target_y = start_y + y_direction * h * step_um
            trajectory.append((target_x, target_y))
    return trajectory


def build_probe_trajectory(
    start_x,
    start_y,
    start_z,
    scan_w,
    scan_h,
    probe_step_v,
    x_direction=1.0,
    y_direction=1.0,
    serpentine=False,
):
    """Build open-loop probe-controller voltage targets."""
    trajectory = []
    for h in range(scan_h):
        w_range = range(scan_w)
        if serpentine and h % 2 == 1:
            w_range = reversed(range(scan_w))
        for w in w_range:
            target_x = start_x + x_direction * w * probe_step_v
            target_y = start_y + y_direction * h * probe_step_v
            trajectory.append((target_x, target_y, start_z))
    return trajectory


def validate_sample_trajectory(stage, trajectory):
    """Fail early if any closed-loop MAX311D target is outside native travel."""
    if not trajectory:
        raise ValueError("Empty sample trajectory.")
    max_x = float(stage.get_max_travel("x"))
    max_y = float(stage.get_max_travel("y"))
    violations = [
        (x, y)
        for x, y in trajectory
        if x < 0.0 or x > max_x or y < 0.0 or y > max_y
    ]
    if violations:
        first_x, first_y = violations[0]
        raise ValueError(
            "Closed-loop sample scan exceeds MAX311D travel. "
            f"First invalid target: X={first_x:.4f} um, Y={first_y:.4f} um; "
            f"valid ranges are X=[0,{max_x:.4f}] um, Y=[0,{max_y:.4f}] um. "
            "Reduce SCAN_W/SCAN_H/STEP_UM or move the stage start position."
        )


def validate_probe_trajectory(probe_stage, trajectory):
    """Fail early if any open-loop MDT693B target voltage is unsafe."""
    if not trajectory:
        raise ValueError("Empty probe trajectory.")
    limit_candidates = []
    if probe_stage.limit_voltage is not None:
        limit_candidates.append(float(probe_stage.limit_voltage))
    if probe_stage.safe_max_voltage is not None:
        limit_candidates.append(float(probe_stage.safe_max_voltage))
    max_voltage = min(limit_candidates) if limit_candidates else None
    if max_voltage is None:
        return

    violations = [
        (x, y, z)
        for x, y, z in trajectory
        if x < 0.0 or y < 0.0 or z < 0.0 or x > max_voltage or y > max_voltage or z > max_voltage
    ]
    if violations:
        first_x, first_y, first_z = violations[0]
        raise ValueError(
            "Open-loop probe scan exceeds MDT693B voltage range. "
            f"First invalid target: X={first_x:.4f} V, Y={first_y:.4f} V, Z={first_z:.4f} V; "
            f"valid voltage range is [0,{max_voltage:.4f}] V."
        )


def return_to_start(
    scan_target,
    stage,
    probe_stage,
    start_x,
    start_y,
    start_z,
    settle_ms,
    probe_return_to_start,
):
    if scan_target == "sample_closed_loop" and stage is not None and start_x is not None and start_y is not None:
        stage.set_position([start_x, start_y])
        stage.wait_until_settled(start_x, start_y, settle_time_ms=settle_ms)
    elif (
        scan_target == "probe_open_loop"
        and probe_return_to_start
        and probe_stage is not None
        and start_x is not None
        and start_y is not None
        and start_z is not None
    ):
        probe_stage.set_voltage_xyz(x=start_x, y=start_y, z=start_z, wait=True, settle_time_ms=settle_ms)


def save_scan_data(
    all_data,
    scan_w,
    scan_h,
    step_um,
    records_per_point,
    average_enable,
    scan_target,
    coordinate_unit,
    probe_step_v,
    probe_um_per_v,
    start_x,
    start_y,
    start_z,
    delay,
):
    if len(all_data) == 0:
        print("No valid data acquired; skipping save.")
        return

    save_confirm = input(f"\nExperiment finished with {len(all_data)} points. Save data? (y/n): ").strip().lower()
    while save_confirm not in ("y", "n"):
        save_confirm = input(f"\nExperiment finished with {len(all_data)} points. Save data? (y/n): ").strip().lower()
    if save_confirm == "n":
        print("User chose not to save data; acquired data was discarded.")
        return

    print("Packaging and saving data...")
    mat_dict = {}
    index_to_pos = []

    try:
        for item in all_data:
            raw_data_content = item[0]
            original_pos_str = item[1]
            safe_key = sanitize_pos_to_key(original_pos_str)

            if average_enable:
                processed_data = (raw_data_content / records_per_point).astype(np.uint16)
            else:
                if isinstance(raw_data_content, list):
                    processed_data = np.concatenate(raw_data_content).astype(np.uint16)
                else:
                    processed_data = raw_data_content.astype(np.uint16)

            mat_dict[safe_key] = processed_data
            index_to_pos.append(original_pos_str)

        mat_dict["metadata"] = {
            "scan_shape": [scan_w, scan_h],
            "step_um": step_um,
            "pos_list": index_to_pos,
            "is_averaged": int(average_enable),
            "scan_target": scan_target,
            "coordinate_unit": coordinate_unit,
            "step_size": probe_step_v if scan_target == "probe_open_loop" else step_um,
            "probe_step_v": -1 if probe_step_v is None else probe_step_v,
            "probe_um_per_v": -1 if probe_um_per_v is None else probe_um_per_v,
            "start_xyz": [start_x, start_y, 0 if start_z is None else start_z],
            "sample_stage": "MAX311D",
            "sample_controller": "BPC303",
            "probe_stage": "MAX312D",
            "probe_controller": "MDT693B",
        }

        suffix_confirm = input("\nAdd an English filename suffix? (y/n): ").strip().lower()
        while suffix_confirm not in ("y", "n"):
            suffix_confirm = input("\nAdd an English filename suffix? (y/n): ").strip().lower()
        suffix = input("\nSuffix: ").strip().lower() if suffix_confirm == "y" else ""

        os.makedirs("./data", exist_ok=True)
        save_path = (
            f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
            f"-D-{delay}-AVER-{records_per_point}-{suffix}.mat"
        )
        sio.savemat(save_path, mat_dict)
        print(
            f"\nSaved {len(mat_dict) - 1} position points to {save_path}. "
            f"Start position was {[start_x, start_y, start_z]} in {coordinate_unit}; stage returned to start."
        )
    except Exception as exc:
        print(f"Data packaging failed: {exc}")
        import traceback

        traceback.print_exc()


def main():
    # Stage selection:
    #   sample_closed_loop: move the MAX311D sample stage with BPC303 in microns.
    #   probe_open_loop: keep the sample fixed and move the MAX312D probe stage by MDT693B voltages.
    SCAN_TARGET = "sample_closed_loop"

    # Closed-loop sample NanoMax: MAX311D on BPC303. User-confirmed axis mapping:
    # BPC303 channel 1/2/3 = X/Y/Z.
    BPC303_SERIAL_NO = "71241834"
    BPC303_KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"
    BPC303_AXIS_MAP = {"x": 1, "y": 2, "z": 3}
    BPC303_SAFE_MAX_OUTPUT_VOLTAGE = 75.0

    # Open-loop probe NanoMax: MAX312D on MDT693B.
    PROBE_MDT_SERIAL_NO = None
    PROBE_MDT_DLL_PATH = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\Alazar_imaging\MDT_COMMAND_LIB_x64.dll"
    PROBE_SAFE_MAX_VOLTAGE = 75.0
    PROBE_STEP_V = None
    PROBE_UM_PER_V = None
    PROBE_X_DIRECTION = 1.0
    PROBE_Y_DIRECTION = 1.0
    PROBE_Z_HOLD_V = None
    PROBE_RETURN_TO_START = True

    # Scan geometry. For MAX311D the full closed-loop travel is 20 um per axis,
    # so sample scans are checked before acquisition starts.
    SCAN_W = 10
    SCAN_H = 10
    STEP_UM = 1.0
    SAMPLE_X_DIRECTION = 1.0
    SAMPLE_Y_DIRECTION = 1.0
    SERPENTINE_SCAN = False
    SETTLE_MS = 120

    # DAQ parameters.
    DELAY = 1600
    SAMPLES_REC = 4096
    SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
    AVERAGE_ENABLE = True
    RECORDS_PER_POINT = 256
    BUFFER_COUNT = 4

    if SCAN_TARGET not in ("sample_closed_loop", "probe_open_loop"):
        raise ValueError("SCAN_TARGET must be 'sample_closed_loop' or 'probe_open_loop'.")

    stage = None
    probe_stage = None
    probe_step_v = None
    daq = None
    all_data = []
    START_X = None
    START_Y = None
    START_Z = None
    coordinate_unit = "um"

    try:
        if SCAN_TARGET == "sample_closed_loop":
            print("Using BPC303 native closed-loop control for the MAX311D sample NanoMax...")
            stage = BPC303NativeController(
                serial_no=BPC303_SERIAL_NO,
                kinesis_dir=BPC303_KINESIS_DIR,
                channels=(1, 2, 3),
                axis_map=BPC303_AXIS_MAP,
                safe_max_output_voltage=BPC303_SAFE_MAX_OUTPUT_VOLTAGE,
            )
            raw_values = stage.get_position_values()
            START_X, START_Y, START_Z = [float(v) for v in raw_values[:3]]
            print(
                "Sample start position: "
                f"X={START_X:.4f} um, Y={START_Y:.4f} um, Z={START_Z:.4f} um; "
                f"travel X={stage.get_max_travel('x'):.1f} um, "
                f"Y={stage.get_max_travel('y'):.1f} um, Z={stage.get_max_travel('z'):.1f} um"
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
            coordinate_unit = "um"

        else:
            probe_step_v = resolve_probe_step_v(STEP_UM, PROBE_STEP_V, PROBE_UM_PER_V)
            print(f"Using MDT693B open-loop probe scan, step={probe_step_v} V/pixel...")
            probe_stage = MDT693BController(
                serial_no=PROBE_MDT_SERIAL_NO,
                dll_path=PROBE_MDT_DLL_PATH,
                safe_max_voltage=PROBE_SAFE_MAX_VOLTAGE,
                um_per_volt=PROBE_UM_PER_V,
            )
            START_X, START_Y, START_Z = [float(v) for v in probe_stage.get_voltage_xyz()]
            if PROBE_Z_HOLD_V is not None:
                START_Z = float(PROBE_Z_HOLD_V)
                probe_stage.set_voltage_xyz(z=START_Z, wait=True, settle_time_ms=SETTLE_MS)
            trajectory = build_probe_trajectory(
                START_X,
                START_Y,
                START_Z,
                SCAN_W,
                SCAN_H,
                probe_step_v,
                x_direction=PROBE_X_DIRECTION,
                y_direction=PROBE_Y_DIRECTION,
                serpentine=SERPENTINE_SCAN,
            )
            validate_probe_trajectory(probe_stage, trajectory)
            coordinate_unit = "V"
            print(f"Probe start voltage: X={START_X:.4f} V, Y={START_Y:.4f} V, Z={START_Z:.4f} V")

        daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY, channel_A_range=ats.INPUT_RANGE_PM_200_MV)
        daq.configure_board(sample_rate=SAMPLE_RATE)
        daq.prepare_acquisition(
            acq_channel=ats.CHANNEL_A,
            samples_per_record=SAMPLES_REC,
            records_per_buffer=RECORDS_PER_POINT,
            buffer_count=BUFFER_COUNT,
            records_per_point=RECORDS_PER_POINT,
        )

        gc.disable()
        input("Press Enter to START Experiment... (make sure the laser is enabled)")

        progress_desc = "PAM sample closed-loop scan" if SCAN_TARGET == "sample_closed_loop" else "PAM probe open-loop scan"
        progress_manager.start(total=len(trajectory), desc=progress_desc)

        if SCAN_TARGET == "sample_closed_loop":
            for tx, ty in trajectory:
                stage.set_position([tx, ty])
                stage.wait_until_settled(tx, ty, settle_time_ms=SETTLE_MS)
                current_pos_str = f"{tx},{ty},0"
                daq.get_one_acquisition(
                    all_data=all_data,
                    curr_pos_str=current_pos_str,
                    timeout_ms=1000,
                    Average_Enable=AVERAGE_ENABLE,
                )
                progress_manager.update(1)
        else:
            for vx, vy, vz in trajectory:
                probe_stage.set_voltage_xyz(x=vx, y=vy, z=vz, wait=True, settle_time_ms=SETTLE_MS)
                current_pos_str = f"{vx},{vy},{vz}"
                daq.get_one_acquisition(
                    all_data=all_data,
                    curr_pos_str=current_pos_str,
                    timeout_ms=1000,
                    Average_Enable=AVERAGE_ENABLE,
                )
                progress_manager.update(1)

        return_to_start(
            SCAN_TARGET,
            stage,
            probe_stage,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            PROBE_RETURN_TO_START,
        )

    except KeyboardInterrupt:
        print("\nUser interrupted the scan.")
        return_to_start(
            SCAN_TARGET,
            stage,
            probe_stage,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            PROBE_RETURN_TO_START,
        )
    except Exception as exc:
        print(f"\nExperiment error: {exc}")
        return_to_start(
            SCAN_TARGET,
            stage,
            probe_stage,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            PROBE_RETURN_TO_START,
        )
        raise
    finally:
        time.sleep(1)
        try:
            gc.enable()
            if daq is not None:
                daq.stop_capture()
            progress_manager.set_colour("green")
            progress_manager.stop()
            if probe_stage is not None:
                probe_stage.close()
            if stage is not None:
                stage.close()
        except Exception as exc:
            print(f"Cleanup error: {exc}")

        save_scan_data(
            all_data,
            SCAN_W,
            SCAN_H,
            STEP_UM,
            RECORDS_PER_POINT,
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
