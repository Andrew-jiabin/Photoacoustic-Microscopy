import datetime
import math
import os
import re
import sys
import time
import traceback

import numpy as np
import scipy.io as sio

from Nanomax.run_log import append_run_log
from Tool_code.position_trans import sanitize_pos_to_key


SAFE_SUFFIX_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _timed_console_line(prompt, timeout_s=60.0, default_text=""):
    """Read a console line with a countdown, using defaults when countdown input is unavailable."""
    if timeout_s is None or float(timeout_s) <= 0:
        return input(prompt), False
    if os.name != "nt" or not sys.stdin.isatty():
        print(f"{prompt} [non-interactive; auto {default_text!r}]")
        return str(default_text), True

    try:
        import msvcrt
    except ImportError:
        print(f"{prompt} [timed input unavailable; auto {default_text!r}]")
        return str(default_text), True

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


def sanitize_filename_suffix(suffix, max_length=80):
    text = SAFE_SUFFIX_RE.sub("-", str(suffix).strip().lower())
    text = text.strip(".-_")
    return text[:max_length].strip(".-_")


def _unique_path(path):
    base, ext = os.path.splitext(path)
    if not os.path.exists(path):
        return path
    for index in range(2, 10000):
        candidate = f"{base}-{index}{ext}"
        if not os.path.exists(candidate):
            return candidate
    raise FileExistsError(f"Could not find a unique filename for {path}")


def _rename_with_suffix_no_overwrite(default_path, suffix):
    safe_suffix = sanitize_filename_suffix(suffix)
    if not safe_suffix:
        return default_path, False, "empty_suffix"
    base, ext = os.path.splitext(default_path)
    target_path = _unique_path(f"{base}-{safe_suffix}{ext}")
    os.rename(default_path, target_path)
    return target_path, True, "renamed"


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


def _prepare_532_noise_reference(noise_532_reference, average_enable, records_per_point, samples_per_record):
    if noise_532_reference is None:
        return None, "not_collected"
    noise_data, package_reason = package_point_data_for_save(
        noise_532_reference,
        average_enable,
        records_per_point,
        samples_per_record,
    )
    if noise_data is None:
        append_run_log("DATA_532_NOISE_REFERENCE_SKIPPED", reason=package_reason)
        return None, package_reason
    return np.asarray(noise_data), package_reason


def build_scan_mat_dict(
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
    noise_532_reference=None,
    noise_532_metadata=None,
):
    if len(all_data) == 0:
        return None, [], [], 0

    mat_dict = {}
    index_to_pos = []
    skipped_pos = []
    actual_pos_list = []
    position_settle_ok = []
    position_timeout_list = []
    position_error_x_um = []
    position_error_y_um = []
    position_error_z_um = []
    noise_532_subtracted = []
    noise_532_subtraction_skipped_pos = []

    try:
        noise_532_processed, noise_532_reason = _prepare_532_noise_reference(
            noise_532_reference,
            average_enable,
            records_per_point,
            samples_per_record,
        )
        if noise_532_processed is not None:
            mat_dict["noise_532_reference"] = noise_532_processed
            append_run_log(
                "DATA_532_NOISE_REFERENCE_READY",
                reason=noise_532_reason,
                shape="x".join(str(dim) for dim in noise_532_processed.shape),
                dtype=str(noise_532_processed.dtype),
            )

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

            if noise_532_processed is not None:
                processed_array = np.asarray(processed_data)
                if processed_array.shape == noise_532_processed.shape:
                    processed_data = processed_array.astype(np.int32) - noise_532_processed.astype(np.int32)
                    noise_532_subtracted.append(1)
                else:
                    noise_532_subtracted.append(0)
                    noise_532_subtraction_skipped_pos.append(original_pos_str)
                    append_run_log(
                        "DATA_532_NOISE_SUBTRACTION_SKIPPED",
                        pos=original_pos_str,
                        reason="shape_mismatch",
                        signal_shape="x".join(str(dim) for dim in processed_array.shape),
                        noise_shape="x".join(str(dim) for dim in noise_532_processed.shape),
                    )
            else:
                noise_532_subtracted.append(0)

            mat_dict[safe_key] = processed_data
            index_to_pos.append(original_pos_str)
            actual_pos_list.append(str(point_meta.get("actual_pos_str", original_pos_str)))
            position_settle_ok.append(int(bool(point_meta.get("position_settle_ok", True))))
            position_timeout_list.append(int(bool(point_meta.get("position_timeout", False))))
            position_error_x_um.append(float(point_meta.get("position_error_x_um", 0.0)))
            position_error_y_um.append(float(point_meta.get("position_error_y_um", 0.0)))
            position_error_z_um.append(float(point_meta.get("position_error_z_um", 0.0)))

        if not index_to_pos:
            return mat_dict, index_to_pos, skipped_pos, 0

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
            "noise_532_reference_available": int(noise_532_processed is not None),
            "noise_532_reference_package_reason": noise_532_reason,
            "noise_532_reference_shape": [] if noise_532_processed is None else list(noise_532_processed.shape),
            "noise_532_subtracted": noise_532_subtracted,
            "noise_532_subtracted_count": int(sum(noise_532_subtracted)),
            "noise_532_subtraction_skipped_pos_list": noise_532_subtraction_skipped_pos,
            "noise_532_metadata": dict(noise_532_metadata or {}),
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
        position_timeout_count = int(sum(position_timeout_list))
        return mat_dict, index_to_pos, skipped_pos, position_timeout_count
    except Exception:
        raise


def _default_data_save_path(delay, records_per_point, output_dir="./data"):
    os.makedirs(output_dir, exist_ok=True)
    return _unique_path(
        os.path.join(
            output_dir,
            f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}-D-{delay}-AVER-{records_per_point}.mat",
        )
    )


def _default_snapshot_save_path(output_dir, label):
    os.makedirs(output_dir, exist_ok=True)
    safe_label = sanitize_filename_suffix(label) or "snapshot"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return _unique_path(os.path.join(output_dir, f"{timestamp}-{safe_label}.mat"))


def save_scan_snapshot_data(
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
    output_dir,
    label="live-preview",
    noise_532_reference=None,
    noise_532_metadata=None,
):
    if len(all_data) == 0:
        append_run_log("DATA_PREVIEW_SNAPSHOT_SKIPPED", reason="no_valid_data")
        return {"status": "skipped", "reason": "no_valid_data"}
    append_run_log("DATA_PREVIEW_SNAPSHOT_PACKAGING_BEGIN", points=len(all_data), output_dir=output_dir)
    try:
        mat_dict, index_to_pos, skipped_pos, position_timeout_count = build_scan_mat_dict(
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
            noise_532_reference=noise_532_reference,
            noise_532_metadata=noise_532_metadata,
        )
        if not index_to_pos:
            append_run_log("DATA_PREVIEW_SNAPSHOT_SKIPPED", reason="no_packageable_data", skipped_points=len(skipped_pos))
            return {"status": "skipped", "reason": "no_packageable_data", "skipped_points": len(skipped_pos)}
        save_path = _default_snapshot_save_path(output_dir, label)
        sio.savemat(save_path, mat_dict)
        append_run_log(
            "DATA_PREVIEW_SNAPSHOT_SAVED",
            path=save_path,
            points=len(index_to_pos),
            skipped_points=len(skipped_pos),
            position_timeout_count=position_timeout_count,
            noise_532_reference_available=int(mat_dict.get("metadata", {}).get("noise_532_reference_available", 0)),
            noise_532_subtracted_count=int(mat_dict.get("metadata", {}).get("noise_532_subtracted_count", 0)),
        )
        return {"status": "saved", "path": save_path, "points": len(index_to_pos), "skipped_points": len(skipped_pos)}
    except Exception as exc:
        append_run_log("DATA_PREVIEW_SNAPSHOT_FAILED", error=repr(exc))
        return {"status": "failed", "error": repr(exc)}


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
    noise_532_reference=None,
    noise_532_metadata=None,
):
    if len(all_data) == 0:
        append_run_log("DATA_SAVE_SKIPPED", reason="no_valid_data")
        print("No valid data acquired; skipping save.")
        return {"status": "skipped", "reason": "no_valid_data"}

    append_run_log("DATA_PACKAGING_BEGIN", points=len(all_data))
    print("Packaging and saving data before any suffix prompt...")

    try:
        mat_dict, index_to_pos, skipped_pos, position_timeout_count = build_scan_mat_dict(
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
            noise_532_reference=noise_532_reference,
            noise_532_metadata=noise_532_metadata,
        )

        if not index_to_pos:
            append_run_log("DATA_SAVE_SKIPPED", reason="no_packageable_data", skipped_points=len(skipped_pos))
            print("No packageable acquisition data was available. This usually means the DAQ did not receive valid trigger buffers.")
            return {"status": "skipped", "reason": "no_packageable_data", "skipped_points": len(skipped_pos)}

        default_save_path = _default_data_save_path(delay, records_per_point)
        sio.savemat(default_save_path, mat_dict)
        append_run_log(
            "DATA_SAVED_DEFAULT",
            path=default_save_path,
            points=len(index_to_pos),
            skipped_points=len(skipped_pos),
            position_timeout_count=position_timeout_count,
            noise_532_reference_available=int(mat_dict.get("metadata", {}).get("noise_532_reference_available", 0)),
            noise_532_subtracted_count=int(mat_dict.get("metadata", {}).get("noise_532_subtracted_count", 0)),
        )
        print(
            f"\nSaved default data file: {default_save_path}\n"
            f"Saved {len(index_to_pos)} position points; skipped {len(skipped_pos)} invalid points. "
            f"Start position was {[start_x, start_y, start_z]} in {coordinate_unit}; stage return status is logged separately."
        )
        if position_timeout_count:
            print(f"Position-timeout points saved with actual_pos metadata: {position_timeout_count}.")

        final_save_path = default_save_path
        try:
            suffix_confirm, suffix_auto = timed_choice(
                "\nAdd an English filename suffix by renaming the saved file? (y/n)",
                ("y", "n"),
                default="n",
                timeout_s=save_prompt_timeout_s,
            )
        except KeyboardInterrupt:
            suffix_confirm, suffix_auto = "n", False
            append_run_log("DATA_SUFFIX_PROMPT_INTERRUPTED", path=default_save_path)
            print(f"\nSuffix prompt interrupted; keeping default filename: {default_save_path}")
        if suffix_auto:
            append_run_log("DATA_SUFFIX_PROMPT_AUTO_SKIPPED", path=default_save_path, timeout_s=save_prompt_timeout_s)
            print(f"No suffix response before timeout; keeping default filename: {default_save_path}")
        elif suffix_confirm == "y":
            try:
                suffix_text, suffix_input_auto = _timed_console_line(
                    "\nSuffix",
                    save_prompt_timeout_s,
                    default_text="",
                )
            except KeyboardInterrupt:
                suffix_text, suffix_input_auto = "", False
                append_run_log("DATA_SUFFIX_INPUT_INTERRUPTED", path=default_save_path)
                print(f"\nSuffix entry interrupted; keeping default filename: {default_save_path}")
            safe_suffix = sanitize_filename_suffix(suffix_text)
            if suffix_input_auto or not safe_suffix:
                append_run_log("DATA_SUFFIX_RENAME_SKIPPED", path=default_save_path, reason="empty_or_timeout_suffix")
                print(f"No valid suffix was entered; keeping default filename: {default_save_path}")
            else:
                try:
                    renamed_path, renamed, reason = _rename_with_suffix_no_overwrite(default_save_path, safe_suffix)
                    final_save_path = renamed_path
                    append_run_log(
                        "DATA_RENAMED_WITH_SUFFIX",
                        old_path=default_save_path,
                        new_path=renamed_path,
                        suffix=safe_suffix,
                        renamed=renamed,
                        reason=reason,
                    )
                    print(f"Final data filename after safe rename: {final_save_path}")
                except Exception as rename_exc:
                    append_run_log(
                        "DATA_SUFFIX_RENAME_FAILED",
                        original_path=default_save_path,
                        suffix=safe_suffix,
                        error=repr(rename_exc),
                    )
                    print(f"Suffix rename failed: {rename_exc}")
                    print(f"Original default file is preserved: {default_save_path}")
                    final_save_path = default_save_path
        else:
            append_run_log("DATA_SUFFIX_RENAME_SKIPPED", path=default_save_path, reason="user_declined")
            print(f"Final data filename: {default_save_path}")

        append_run_log(
            "DATA_SAVED",
            path=final_save_path,
            default_path=default_save_path,
            points=len(index_to_pos),
            skipped_points=len(skipped_pos),
            position_timeout_count=position_timeout_count,
            noise_532_reference_available=int(mat_dict.get("metadata", {}).get("noise_532_reference_available", 0)),
            noise_532_subtracted_count=int(mat_dict.get("metadata", {}).get("noise_532_subtracted_count", 0)),
        )
        return {
            "status": "saved",
            "path": final_save_path,
            "default_path": default_save_path,
            "points": len(index_to_pos),
            "skipped_points": len(skipped_pos),
        }
    except Exception as exc:
        append_run_log("DATA_PACKAGING_FAILED", error=repr(exc))
        print(f"Data packaging failed: {exc}")
        traceback.print_exc()
        return {"status": "failed", "error": repr(exc)}
