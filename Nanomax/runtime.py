import math
import os
import subprocess
import sys

from Nanomax.run_log import RUN_LOG_PATH, append_run_log


def find_other_pam_processes():
    """Return other running PAM/BPC helper processes that may hold the controller."""
    if os.name != "nt":
        return []
    current_pid = os.getpid()
    command = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and "
        f"$_.ProcessId -ne {current_pid} -and "
        "($_.CommandLine -like '*PAM_Main_Nanomax.py*' -or "
        "$_.CommandLine -like '*_bpc303_preflight_child.py*') } | "
        "ForEach-Object { "
        "'pid=' + $_.ProcessId + ' creation=' + $_.CreationDate + "
        "' command=' + ($_.CommandLine -replace '\\r|\\n', ' ') }"
    )
    try:
        output = subprocess.check_output(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except Exception as exc:
        append_run_log("PROCESS_PRECHECK_SKIPPED", error=repr(exc))
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def run_bpc303_preflight(serial_no, kinesis_dir, timeout_s=20.0, mode="open_close"):
    """
    Probe BPC303 discovery in a child process before the main process touches the stage.

    The child only builds the Kinesis device list, enumerates BPC30x devices, and
    optionally opens/checks/closes the controller. It does not enable channels,
    zero axes, or set any position/voltage.
    """
    if not serial_no:
        raise ValueError("BPC303 preflight requires a serial number.")
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in ("enumerate", "open_close"):
        raise ValueError("BPC303_PREFLIGHT_MODE must be 'enumerate' or 'open_close'.")

    child_path = os.path.join(os.path.dirname(RUN_LOG_PATH), "_bpc303_preflight_child.py")
    child_code = r'''
import ctypes
import json
import os
import sys
from ctypes import c_bool, c_char_p, c_int, c_short, create_string_buffer

def emit(event, **fields):
    payload = {"event": event}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=True), flush=True)

def bind(dll, name, restype, argtypes):
    func = getattr(dll, name)
    func.restype = restype
    func.argtypes = argtypes
    return func

def main():
    kinesis_dir = sys.argv[1]
    serial_no = str(sys.argv[2])
    mode = str(sys.argv[3]).lower()
    serial_bytes = serial_no.encode("ascii")
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(kinesis_dir)
    dll_path = os.path.join(kinesis_dir, "Thorlabs.MotionControl.Benchtop.Piezo.dll")
    emit("child_load_library_begin", dll_path=dll_path)
    dll = ctypes.CDLL(dll_path)
    emit("child_load_library_done")

    build_device_list = bind(dll, "TLI_BuildDeviceList", c_short, [])
    get_by_type = bind(dll, "TLI_GetDeviceListByTypeExt", c_short, [ctypes.c_char_p, ctypes.c_ulong, c_int])
    pbc_open = bind(dll, "PBC_Open", c_short, [c_char_p])
    pbc_close = bind(dll, "PBC_Close", None, [c_char_p])
    pbc_check_connection = bind(dll, "PBC_CheckConnection", c_bool, [c_char_p])

    emit("child_build_device_list_begin")
    ret = int(build_device_list())
    emit("child_build_device_list_done", result=ret)
    if ret != 0:
        raise SystemExit(10)

    emit("child_enumerate_begin", device_type=71)
    buffer = create_string_buffer(1024)
    ret = int(get_by_type(buffer, 1024, 71))
    raw = buffer.value.decode("ascii", errors="replace")
    serials = [item for item in raw.split(",") if item]
    emit("child_enumerate_done", result=ret, serials=serials)
    if ret != 0:
        raise SystemExit(11)
    if serial_no not in serials:
        raise SystemExit(12)

    if mode == "open_close":
        emit("child_open_begin", serial=serial_no)
        ret = int(pbc_open(serial_bytes))
        emit("child_open_done", serial=serial_no, result=ret)
        if ret != 0:
            raise SystemExit(13)
        try:
            emit("child_check_connection_begin", serial=serial_no)
            connected = bool(pbc_check_connection(serial_bytes))
            emit("child_check_connection_done", serial=serial_no, connected=connected)
            if not connected:
                raise SystemExit(14)
        finally:
            emit("child_close_begin", serial=serial_no)
            pbc_close(serial_bytes)
            emit("child_close_done", serial=serial_no)

    emit("child_preflight_done", serial=serial_no, mode=mode)

if __name__ == "__main__":
    main()
'''
    os.makedirs(os.path.dirname(child_path), exist_ok=True)
    with open(child_path, "w", encoding="utf-8") as child_file:
        child_file.write(child_code)

    command = [sys.executable, child_path, kinesis_dir, str(serial_no), normalized_mode]
    append_run_log("BPC_PREFLIGHT_BEGIN", serial=serial_no, mode=normalized_mode, timeout_s=timeout_s, child_path=child_path)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    try:
        stdout, stderr = process.communicate(timeout=float(timeout_s))
    except subprocess.TimeoutExpired:
        append_run_log("BPC_PREFLIGHT_TIMEOUT", serial=serial_no, mode=normalized_mode, timeout_s=timeout_s)
        try:
            process.kill()
        except Exception as exc:
            append_run_log("BPC_PREFLIGHT_KILL_FAILED", error=repr(exc))
        raise RuntimeError(
            f"BPC303 preflight timed out after {timeout_s} s. "
            "Kinesis/USB may be stuck; close old Python processes or reboot the experiment PC."
        ) from None

    compact_stdout = stdout.strip().replace("\n", " || ")
    compact_stderr = stderr.strip().replace("\n", " || ")
    append_run_log(
        "BPC_PREFLIGHT_DONE",
        serial=serial_no,
        mode=normalized_mode,
        returncode=process.returncode,
        stdout=compact_stdout[-1800:],
        stderr=compact_stderr[-800:],
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"BPC303 preflight failed with code {process.returncode}. "
            f"stdout={compact_stdout} stderr={compact_stderr}"
        )
    return stdout


def poll_user_stop_request(enabled=True, stop_key="q", notify=None):
    """Check once for a graceful stop key without competing with later input prompts."""
    if not enabled:
        return False
    normalized_key = str(stop_key).strip().lower()
    if not normalized_key:
        append_run_log("USER_STOP_POLL_SKIPPED", reason="empty_stop_key")
        return False
    if os.name != "nt":
        return False

    try:
        import msvcrt
    except ImportError:
        return False

    requested = False
    try:
        while msvcrt.kbhit():
            char = msvcrt.getwch()
            if char in ("\x00", "\xe0"):
                if msvcrt.kbhit():
                    msvcrt.getwch()
                continue
            if char.lower() == normalized_key:
                requested = True
    except Exception as exc:
        append_run_log("USER_STOP_POLL_ERROR", error=repr(exc))
        return False

    if requested:
        append_run_log("USER_STOP_REQUESTED", stop_key=normalized_key)
        message = (
            f"Graceful stop requested by '{normalized_key}'. "
            "The current point will finish, then the stage will return and close normally."
        )
        if notify is not None:
            try:
                notify(message)
            except Exception as exc:
                append_run_log("USER_STOP_NOTIFY_FAILED", error=repr(exc))
                print(message, flush=True)
        else:
            print(message, flush=True)
    return requested


def _linear_segment_points(start_values, target_values, max_step):
    start = [float(value) for value in start_values]
    target = [float(value) for value in target_values]
    if len(start) != len(target):
        raise ValueError("start_values and target_values must have the same length")
    max_step = float(max_step)
    if max_step <= 0:
        raise ValueError(f"Return step must be positive, got {max_step:g}.")
    distance = math.sqrt(sum((target[i] - start[i]) ** 2 for i in range(len(start))))
    if distance <= 1e-12:
        return [], distance
    steps = max(1, int(math.ceil(distance / max_step)))
    points = []
    for step_index in range(1, steps + 1):
        fraction = step_index / steps
        points.append([start[i] + (target[i] - start[i]) * fraction for i in range(len(start))])
    return points, distance


def _return_sample_xy_segmented(
    stage,
    target_x,
    target_y,
    settle_ms,
    return_target,
    step_um=0.1,
    tolerance_um=0.02,
    timeout_s=1800.0,
    reissue_interval_s=1.0,
):
    current = stage.get_position_values()
    current_x, current_y = float(current[0]), float(current[1])
    points, distance = _linear_segment_points((current_x, current_y), (target_x, target_y), step_um)
    append_run_log(
        "RETURN_TO_START_SEGMENTED_BEGIN",
        scan_target="sample_closed_loop",
        return_target=return_target,
        current_x_um=f"{current_x:.6f}",
        current_y_um=f"{current_y:.6f}",
        target_x_um=f"{target_x:.6f}",
        target_y_um=f"{target_y:.6f}",
        distance_um=f"{distance:.6f}",
        step_um=f"{float(step_um):g}",
        segments=len(points),
    )
    if not points:
        stage.wait_until_settled(
            target_x,
            target_y,
            settle_time_ms=settle_ms,
            tolerance_step=tolerance_um,
            timeout_s=timeout_s,
            correction_interval_s=reissue_interval_s,
        )
        return

    for index, (next_x, next_y) in enumerate(points, start=1):
        stage.set_position([next_x, next_y])
        stage.wait_until_settled(
            next_x,
            next_y,
            settle_time_ms=settle_ms if index == len(points) else 0,
            tolerance_step=tolerance_um,
            timeout_s=timeout_s,
            correction_interval_s=reissue_interval_s,
        )


def _return_probe_xyz_segmented(probe_stage, target_x, target_y, target_z, settle_ms, step_um=0.1, um_per_v=None):
    current = [float(value) for value in probe_stage.get_voltage_xyz()]
    target = [float(target_x), float(target_y), float(target_z)]
    if um_per_v is None or float(um_per_v) <= 0:
        append_run_log(
            "RETURN_TO_START_SEGMENTED_SKIPPED",
            scan_target="probe_open_loop",
            reason="missing_um_per_v_calibration",
        )
        probe_stage.set_voltage_xyz(x=target[0], y=target[1], z=target[2], wait=True, settle_time_ms=settle_ms)
        return

    step_v = float(step_um) / float(um_per_v)
    points, distance_v = _linear_segment_points(current, target, step_v)
    append_run_log(
        "RETURN_TO_START_SEGMENTED_BEGIN",
        scan_target="probe_open_loop",
        current_x_v=f"{current[0]:.6f}",
        current_y_v=f"{current[1]:.6f}",
        current_z_v=f"{current[2]:.6f}",
        target_x_v=f"{target[0]:.6f}",
        target_y_v=f"{target[1]:.6f}",
        target_z_v=f"{target[2]:.6f}",
        distance_v=f"{distance_v:.6f}",
        step_um=f"{float(step_um):g}",
        step_v=f"{step_v:.6f}",
        segments=len(points),
    )
    if not points:
        probe_stage.wait_until_voltage_settled(target, settle_time_ms=settle_ms)
        return

    for index, (next_x, next_y, next_z) in enumerate(points, start=1):
        probe_stage.set_voltage_xyz(
            x=next_x,
            y=next_y,
            z=next_z,
            wait=True,
            settle_time_ms=settle_ms if index == len(points) else 0,
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
    sample_return_xy_to_zero=False,
    sample_zero_xy_after_return=False,
    sample_zero_axes=("x", "y"),
    sample_return_step_um=0.1,
    sample_position_tolerance_um=0.02,
    sample_position_timeout_s=1800.0,
    sample_position_reissue_interval_s=1.0,
    probe_um_per_v=None,
    probe_return_step_um=0.1,
):
    if scan_target == "sample_closed_loop" and stage is not None:
        return_target = "low_end_zero" if sample_return_xy_to_zero else "prealign_start"
        if sample_return_xy_to_zero:
            target_x, target_y = 0.0, 0.0
        elif start_x is not None and start_y is not None:
            target_x, target_y = start_x, start_y
        else:
            append_run_log("RETURN_TO_START_SKIPPED", scan_target=scan_target, reason="missing_start_xy")
            return
        append_run_log(
            "RETURN_TO_START_BEGIN",
            scan_target=scan_target,
            return_target=return_target,
            target_x_um=f"{target_x:.4f}",
            target_y_um=f"{target_y:.4f}",
            zero_after_return=sample_zero_xy_after_return,
        )
        print(
            f"Returning sample X/Y to {return_target}: X={target_x:.4f} um, Y={target_y:.4f} um "
            f"in <= {float(sample_return_step_um):g} um line steps."
        )
        _return_sample_xy_segmented(
            stage,
            target_x,
            target_y,
            settle_ms,
            return_target,
            step_um=sample_return_step_um,
            tolerance_um=sample_position_tolerance_um,
            timeout_s=sample_position_timeout_s,
            reissue_interval_s=sample_position_reissue_interval_s,
        )
        append_run_log(
            "RETURN_TO_START_DONE",
            scan_target=scan_target,
            return_target=return_target,
            target_x_um=f"{target_x:.4f}",
            target_y_um=f"{target_y:.4f}",
        )
        if sample_zero_xy_after_return:
            append_run_log("ZERO_DATUM_REBUILD_BEGIN", axes=",".join(sample_zero_axes), reason="return_to_start")
            print("Zeroing sample X/Y after return: output goes to 0 V and the datum is rebuilt.")
            stage.set_zero_axes(sample_zero_axes, wait=True, settle_time_ms=settle_ms)
            append_run_log("ZERO_DATUM_REBUILT", axes=",".join(sample_zero_axes), reason="return_to_start")
        elif sample_return_xy_to_zero and abs(target_x) <= 1e-6 and abs(target_y) <= 1e-6:
            append_run_log(
                "ZERO_DATUM_TRUSTED_AFTER_LOW_END_RETURN",
                axes=",".join(sample_zero_axes),
                reason="return_to_start_without_rebuild",
            )
        else:
            append_run_log(
                "POSITION_TRUSTED_AFTER_START_RETURN",
                axes=",".join(sample_zero_axes),
                reason="return_to_prealign_start_without_zero_rebuild",
            )
    elif (
        scan_target == "probe_open_loop"
        and probe_return_to_start
        and probe_stage is not None
        and start_x is not None
        and start_y is not None
        and start_z is not None
    ):
        append_run_log(
            "RETURN_TO_START_BEGIN",
            scan_target=scan_target,
            target_x_v=f"{start_x:.4f}",
            target_y_v=f"{start_y:.4f}",
            target_z_v=f"{start_z:.4f}",
        )
        print(
            f"Returning probe voltages to start in <= {float(probe_return_step_um):g} um-equivalent line steps."
        )
        _return_probe_xyz_segmented(
            probe_stage,
            start_x,
            start_y,
            start_z,
            settle_ms,
            step_um=probe_return_step_um,
            um_per_v=probe_um_per_v,
        )
        append_run_log("RETURN_TO_START_DONE", scan_target=scan_target)
    else:
        append_run_log("RETURN_TO_START_SKIPPED", scan_target=scan_target, reason="conditions_not_met")


def safe_return_to_start(*args, **kwargs):
    """Best-effort return used while handling interruptions/errors."""
    try:
        return_to_start(*args, **kwargs)
        return True
    except Exception as exc:
        append_run_log("RETURN_TO_START_FAILED", error=repr(exc))
        print(f"Return-to-start cleanup failed: {exc}")
        return False
