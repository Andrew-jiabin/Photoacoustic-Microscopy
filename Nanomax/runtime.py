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


def poll_user_stop_request(enabled=True, stop_key="q"):
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
        print(
            f"\nGraceful stop requested by '{normalized_key}'. "
            "The current point will finish, then the stage will return and close normally."
        )
    return requested


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
):
    if scan_target == "sample_closed_loop" and stage is not None:
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
            target_x_um=f"{target_x:.4f}",
            target_y_um=f"{target_y:.4f}",
            zero_after_return=sample_zero_xy_after_return,
        )
        print(f"Returning sample X/Y to low-end target: X={target_x:.4f} um, Y={target_y:.4f} um.")
        stage.set_position([target_x, target_y])
        stage.wait_until_settled(target_x, target_y, settle_time_ms=settle_ms)
        append_run_log("RETURN_TO_START_DONE", scan_target=scan_target, target_x_um=f"{target_x:.4f}", target_y_um=f"{target_y:.4f}")
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
        probe_stage.set_voltage_xyz(x=start_x, y=start_y, z=start_z, wait=True, settle_time_ms=settle_ms)
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
