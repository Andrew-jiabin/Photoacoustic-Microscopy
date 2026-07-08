import datetime
import os
import traceback

import numpy as np
import scipy.io as sio

from Nanomax.run_log import append_run_log
from Tool_code.position_trans import sanitize_pos_to_key


def package_point_data_for_save(raw_data_content, average_enable, records_per_point, samples_per_record):
    """Normalize one acquired point into the waveform array saved in the .mat file."""
    if average_enable:
        if isinstance(raw_data_content, list):
            arrays = [np.asarray(item).reshape(-1) for item in raw_data_content if np.asarray(item).size > 0]
            if not arrays:
                return None, "empty_average_buffer_list"
            combined_raw = np.concatenate(arrays)
            if combined_raw.size % int(samples_per_record) != 0:
                raise ValueError(
                    f"Average buffer length {combined_raw.size} is not divisible by "
                    f"samples_per_record={samples_per_record}."
                )
            summed_data = np.sum(combined_raw.reshape(-1, int(samples_per_record)), axis=0, dtype=np.uint32)
            return (summed_data / int(records_per_point)).astype(np.uint16), "averaged_from_buffer_list"

        raw_array = np.asarray(raw_data_content)
        if raw_array.size == 0:
            return None, "empty_average_array"
        return (raw_array / int(records_per_point)).astype(np.uint16), "averaged_from_summed_array"

    if isinstance(raw_data_content, list):
        arrays = [np.asarray(item).reshape(-1) for item in raw_data_content if np.asarray(item).size > 0]
        if not arrays:
            return None, "empty_raw_buffer_list"
        return np.concatenate(arrays).astype(np.uint16), "raw_from_buffer_list"

    raw_array = np.asarray(raw_data_content)
    if raw_array.size == 0:
        return None, "empty_raw_array"
    return raw_array.astype(np.uint16), "raw_from_array"


def save_scan_data(
    all_data,
    scan_w,
    scan_h,
    step_um,
    records_per_point,
    samples_per_record,
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
        append_run_log("DATA_SAVE_SKIPPED", reason="no_valid_data")
        print("No valid data acquired; skipping save.")
        return

    append_run_log("DATA_SAVE_PROMPT", points=len(all_data))
    save_confirm = input(f"\nExperiment finished with {len(all_data)} points. Save data? (y/n): ").strip().lower()
    while save_confirm not in ("y", "n"):
        save_confirm = input(f"\nExperiment finished with {len(all_data)} points. Save data? (y/n): ").strip().lower()
    if save_confirm == "n":
        append_run_log("DATA_DISCARDED_BY_USER", points=len(all_data))
        print("User chose not to save data; acquired data was discarded.")
        return

    append_run_log("DATA_PACKAGING_BEGIN", points=len(all_data))
    print("Packaging and saving data...")
    mat_dict = {}
    index_to_pos = []
    skipped_pos = []

    try:
        for item in all_data:
            raw_data_content = item[0]
            original_pos_str = item[1]
            safe_key = sanitize_pos_to_key(original_pos_str)
            processed_data, package_reason = package_point_data_for_save(
                raw_data_content,
                average_enable,
                records_per_point,
                samples_per_record,
            )
            if processed_data is None:
                skipped_pos.append(original_pos_str)
                append_run_log("DATA_POINT_SKIPPED", pos=original_pos_str, reason=package_reason)
                continue

            mat_dict[safe_key] = processed_data
            index_to_pos.append(original_pos_str)

        if not index_to_pos:
            append_run_log("DATA_SAVE_SKIPPED", reason="no_packageable_data", skipped_points=len(skipped_pos))
            print("No packageable acquisition data was available. This usually means the DAQ did not receive valid trigger buffers.")
            return

        mat_dict["metadata"] = {
            "scan_shape": [scan_w, scan_h],
            "step_um": step_um,
            "pos_list": index_to_pos,
            "skipped_pos_list": skipped_pos,
            "is_averaged": int(average_enable),
            "records_per_point": records_per_point,
            "samples_per_record": samples_per_record,
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
        append_run_log("DATA_SAVED", path=save_path, points=len(index_to_pos), skipped_points=len(skipped_pos))
        print(
            f"\nSaved {len(index_to_pos)} position points to {save_path}; skipped {len(skipped_pos)} invalid points. "
            f"Start position was {[start_x, start_y, start_z]} in {coordinate_unit}; stage returned to start."
        )
    except Exception as exc:
        append_run_log("DATA_PACKAGING_FAILED", error=repr(exc))
        print(f"Data packaging failed: {exc}")
        traceback.print_exc()

