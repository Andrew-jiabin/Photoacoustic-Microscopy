import datetime
import json
import os
import statistics
import tempfile

from Nanomax.run_log import RUN_LOG_PATH, append_run_log


HISTORY_PATH = os.path.join(os.path.dirname(RUN_LOG_PATH), "pam_scan_speed_history.json")
MAX_RECORDS_PER_KEY = 20
ESTIMATE_RECENT_COUNT = 5


def _step_key(step_um):
    return f"{float(step_um):.6f}"


def _history_key(scan_target, step_um):
    return f"{str(scan_target)}|step_um={_step_key(step_um)}"


def _load_history():
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"version": 1, "records": {}}
    except Exception as exc:
        append_run_log("SCAN_SPEED_HISTORY_LOAD_FAILED", path=HISTORY_PATH, error=repr(exc))
        return {"version": 1, "records": {}}
    if not isinstance(data, dict):
        return {"version": 1, "records": {}}
    records = data.get("records")
    if not isinstance(records, dict):
        data["records"] = {}
    data.setdefault("version", 1)
    return data


def _atomic_write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".scan_speed_", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=True, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def record_successful_scan_speed(
    *,
    scan_target,
    step_um,
    scan_range_x_um,
    scan_range_y_um,
    scan_w,
    scan_h,
    points,
    acquisition_duration_s,
    scan_pattern,
    records_per_point,
    samples_per_record,
    average_enable,
    acq_timeout_ms,
):
    points = int(points)
    acquisition_duration_s = float(acquisition_duration_s)
    if points <= 0 or acquisition_duration_s <= 0:
        append_run_log(
            "SCAN_SPEED_RECORD_SKIPPED",
            reason="invalid_points_or_duration",
            points=points,
            acquisition_duration_s=f"{acquisition_duration_s:.6f}",
        )
        return None

    speed_pps = points / acquisition_duration_s
    record = {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "scan_target": str(scan_target),
        "step_um": float(step_um),
        "scan_range_x_um": float(scan_range_x_um),
        "scan_range_y_um": float(scan_range_y_um),
        "scan_w": int(scan_w),
        "scan_h": int(scan_h),
        "points": points,
        "acquisition_duration_s": acquisition_duration_s,
        "speed_pps": speed_pps,
        "seconds_per_point": acquisition_duration_s / points,
        "scan_pattern": str(scan_pattern),
        "records_per_point": int(records_per_point),
        "samples_per_record": int(samples_per_record),
        "average_enable": bool(average_enable),
        "acq_timeout_ms": int(acq_timeout_ms),
    }

    data = _load_history()
    key = _history_key(scan_target, step_um)
    bucket = data.setdefault("records", {}).setdefault(key, [])
    bucket.append(record)
    del bucket[:-MAX_RECORDS_PER_KEY]
    _atomic_write_json(HISTORY_PATH, data)
    append_run_log(
        "SCAN_SPEED_RECORDED",
        path=HISTORY_PATH,
        scan_target=scan_target,
        step_um=f"{float(step_um):.6f}",
        points=points,
        duration_s=f"{acquisition_duration_s:.3f}",
        speed_pps=f"{speed_pps:.6f}",
        records=len(bucket),
    )
    return record


def estimate_scan_time(scan_target, step_um, points):
    data = _load_history()
    records = data.get("records", {}).get(_history_key(scan_target, step_um), [])
    valid_records = [
        record
        for record in records
        if isinstance(record, dict) and float(record.get("speed_pps", 0.0)) > 0
    ]
    if not valid_records:
        return None

    recent = valid_records[-ESTIMATE_RECENT_COUNT:]
    speeds = [float(record["speed_pps"]) for record in recent]
    speed_pps = statistics.median(speeds)
    points = int(points)
    if speed_pps <= 0 or points <= 0:
        return None
    estimated_s = points / speed_pps
    return {
        "estimated_s": estimated_s,
        "speed_pps": speed_pps,
        "seconds_per_point": 1.0 / speed_pps,
        "records_used": len(recent),
        "history_records": len(valid_records),
        "last_timestamp": recent[-1].get("timestamp", "-"),
        "history_path": HISTORY_PATH,
    }


def format_duration(seconds):
    seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
