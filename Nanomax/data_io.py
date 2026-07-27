import datetime
import math
import os
import sys
import time
import traceback

import numpy as np
import scipy.io as sio

from Nanomax.run_log import append_run_log
from Tool_code.position_trans import sanitize_pos_to_key


def _timed_console_line(prompt, timeout_s=60.0, default_text=""):
    """Read a console line with a countdown on Windows; fall back to blocking input elsewhere."""
    if timeout_s is None or float(timeout_s) <= 0 or os.name != "nt" or not sys.stdin.isatty():
        return input(prompt), False

    try:
        import msvcrt
    except ImportError:
        return input(prompt), False

    prompt_text = str(prompt)
    prefix = ""
    while prompt_text.startswith("\n"):
        prefix += "\n"
        prompt_text = prompt_text[1:]
    if prefix:
        sys.stdout.write(prefix)

    deadline = time.monotonic() + float(timeout_s)
    chars = []
    last_render_len = 0

    def render():
        nonlocal last_render_len
        remaining = max(0, int(math.ceil(deadline - time.monotonic())))
        default_hint = f"; auto {default_text!r}" if default_text else ""
        line = f"{prompt_text} [timeout {remaining}s{default_hint}]: {''.join(chars)}"
        padding = " " * max(0, last_render_len - len(line))
        sys.stdout.write("\r" + line + padding)
        sys.stdout.flush()
        last_render_len = len(line)

    render()
    while True:
        if time.monotonic() >= deadline:
            chars.clear()
            chars.extend(str(default_text))
            render()
            sys.stdout.write("\n")
            sys.stdout.flush()
            return str(default_text), True

        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if char in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(chars), False
            if char in ("\b", "\x7f"):
                if chars:
                    chars.pop()
                    render()
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char.isprintable():
                chars.append(char)
                render()

        render()
        time.sleep(0.1)


def timed_choice(prompt, choices, default, timeout_s=60.0):
    deadline = time.monotonic() + float(timeout_s) if timeout_s is not None and float(timeout_s) > 0 else None
    normalized_choices = tuple(str(choice).lower() for choice in choices)
    while True:
        if deadline is None:
            text, auto_selected = input(prompt), False
        else:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return str(default).lower(), True
            text, auto_selected = _timed_console_line(prompt, remaining, default_text=str(default).lower())
        value = text.strip().lower()
        if auto_selected:
            return str(default).lower(), True
        if value in normalized_choices:
            return value, False
        print(f"Please enter one of: {', '.join(normalized_choices)}")


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
    save_prompt_timeout_s=60.0,
):
    if len(all_data) == 0:
        append_run_log("DATA_SAVE_SKIPPED", reason="no_valid_data")
        print("No valid data acquired; skipping save.")
        return {"status": "skipped", "reason": "no_valid_data"}

    append_run_log("DATA_SAVE_PROMPT", points=len(all_data), timeout_s=save_prompt_timeout_s, default="y")
    save_confirm, save_auto = timed_choice(
        f"\nExperiment stopped with {len(all_data)} acquired points. Save data? (y/n)",
        ("y", "n"),
        default="y",
        timeout_s=save_prompt_timeout_s,
    )
    if save_auto:
        append_run_log("DATA_SAVE_AUTO_CONFIRMED", points=len(all_data), timeout_s=save_prompt_timeout_s)
        print("No save response before timeout; automatically saving without a filename suffix.")
    if save_confirm == "n":
        append_run_log("DATA_DISCARDED_BY_USER", points=len(all_data))
        print("User chose not to save data; acquired data was discarded.")
        return {"status": "discarded", "points": len(all_data)}

    append_run_log("DATA_PACKAGING_BEGIN", points=len(all_data))
    print("Packaging and saving data...")
    mat_dict = {}
    index_to_pos = []
    skipped_pos = []
    actual_pos_list = []
    position_settle_ok = []
    position_timeout_list = []
    position_error_x_um = []
    position_error_y_um = []
    position_error_z_um = []

    try:
        for item in all_data:
            raw_data_content = item[0]
            original_pos_str = item[1]
            point_meta = item[2] if len(item) > 2 and isinstance(item[2], dict) else {}
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
            actual_pos_list.append(str(point_meta.get("actual_pos_str", original_pos_str)))
            position_settle_ok.append(int(bool(point_meta.get("position_settle_ok", True))))
            position_timeout_list.append(int(bool(point_meta.get("position_timeout", False))))
            position_error_x_um.append(float(point_meta.get("position_error_x_um", 0.0)))
            position_error_y_um.append(float(point_meta.get("position_error_y_um", 0.0)))
            position_error_z_um.append(float(point_meta.get("position_error_z_um", 0.0)))

        if not index_to_pos:
            append_run_log("DATA_SAVE_SKIPPED", reason="no_packageable_data", skipped_points=len(skipped_pos))
            print("No packageable acquisition data was available. This usually means the DAQ did not receive valid trigger buffers.")
            return {"status": "skipped", "reason": "no_packageable_data", "skipped_points": len(skipped_pos)}

        mat_dict["metadata"] = {
            "scan_shape": [scan_w, scan_h],
            "step_um": step_um,
            "pos_list": index_to_pos,
            "actual_pos_list": actual_pos_list,
            "position_settle_ok": position_settle_ok,
            "position_timeout": position_timeout_list,
            "position_error_x_um": position_error_x_um,
            "position_error_y_um": position_error_y_um,
            "position_error_z_um": position_error_z_um,
            "position_timeout_count": int(sum(position_timeout_list)),
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

        if save_auto:
            suffix = ""
        else:
            suffix_confirm = input("\nAdd an English filename suffix? (y/n): ").strip().lower()
            while suffix_confirm not in ("y", "n"):
                suffix_confirm = input("\nAdd an English filename suffix? (y/n): ").strip().lower()
            suffix = input("\nSuffix: ").strip().lower() if suffix_confirm == "y" else ""

        os.makedirs("./data", exist_ok=True)
        suffix_part = f"-{suffix}" if suffix else ""
        save_path = (
            f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
            f"-D-{delay}-AVER-{records_per_point}{suffix_part}.mat"
        )
        sio.savemat(save_path, mat_dict)
        append_run_log(
            "DATA_SAVED",
            path=save_path,
            points=len(index_to_pos),
            skipped_points=len(skipped_pos),
            position_timeout_count=int(sum(position_timeout_list)),
        )
        print(
            f"\nSaved {len(index_to_pos)} position points to {save_path}; skipped {len(skipped_pos)} invalid points. "
            f"Start position was {[start_x, start_y, start_z]} in {coordinate_unit}; stage return status is logged separately."
        )
        position_timeout_count = int(sum(position_timeout_list))
        if position_timeout_count:
            print(f"Position-timeout points saved with actual_pos metadata: {position_timeout_count}.")
        return {"status": "saved", "path": save_path, "points": len(index_to_pos), "skipped_points": len(skipped_pos)}
    except Exception as exc:
        append_run_log("DATA_PACKAGING_FAILED", error=repr(exc))
        print(f"Data packaging failed: {exc}")
        traceback.print_exc()
        return {"status": "failed", "error": repr(exc)}
