import datetime
import json
import os
from pathlib import Path


PREALIGN_STATE_PATH = Path(__file__).resolve().parents[1] / "run_logs" / "nanomax_closed_loop_prealign_state.json"


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_prealign_state(x_um, y_um, z_um, config=None, path=PREALIGN_STATE_PATH):
    path = Path(path)
    payload = {
        "status": "ready",
        "timestamp": _now_iso(),
        "position_um": {"x": float(x_um), "y": float(y_um), "z": float(z_um)},
        "config": config or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def inspect_prealign_state(path=PREALIGN_STATE_PATH, newer_than_mtime=None, max_age_s=12 * 3600):
    path = Path(path)
    result = {
        "ready": False,
        "reason": "missing",
        "path": str(path),
        "position_um": {},
        "config": {},
        "timestamp": "",
    }
    if not path.exists():
        return result
    try:
        state_mtime = path.stat().st_mtime
        if newer_than_mtime is not None and state_mtime <= float(newer_than_mtime):
            result["reason"] = "older_than_last_pam_log"
            return result
        if max_age_s is not None and datetime.datetime.now().timestamp() - state_mtime > float(max_age_s):
            result["reason"] = "stale"
            return result
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result["reason"] = f"read_error:{exc!r}"
        return result

    if payload.get("status") != "ready":
        result["reason"] = f"status_{payload.get('status', 'unknown')}"
        return result
    position = payload.get("position_um") or {}
    if not all(axis in position for axis in ("x", "y", "z")):
        result["reason"] = "missing_position"
        return result
    result.update(
        ready=True,
        reason="prealign_current_position_ready",
        position_um=position,
        config=payload.get("config") or {},
        timestamp=str(payload.get("timestamp", "")),
    )
    return result
