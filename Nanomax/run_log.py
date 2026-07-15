import datetime
import os


RUN_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "run_logs",
    "PAM_Main_Nanomax_run.log",
)
CURRENT_RUN_ID = None


def set_current_run_id(run_id):
    global CURRENT_RUN_ID
    CURRENT_RUN_ID = run_id


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _format_log_value(value):
    text = str(value)
    return text.replace("\n", "\\n").replace("\r", "\\r").replace("|", "/")


def append_run_log(event, message="", **fields):
    """Append a non-fatal text event to the persistent PAM run log."""
    try:
        os.makedirs(os.path.dirname(RUN_LOG_PATH), exist_ok=True)
        parts = [
            f"ts={_format_log_value(_now_iso())}",
            f"run_id={_format_log_value(CURRENT_RUN_ID or '-')}",
            f"event={_format_log_value(event)}",
        ]
        if message:
            parts.append(f"message={_format_log_value(message)}")
        for key, value in fields.items():
            parts.append(f"{key}={_format_log_value(value)}")
        with open(RUN_LOG_PATH, "a", encoding="utf-8") as log_file:
            log_file.write(" | ".join(parts) + "\n")
    except Exception:
        pass


def parse_log_fields(line):
    parsed = {}
    for part in line.strip().split(" | "):
        if "=" in part:
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
    return parsed


def inspect_previous_run(log_path=RUN_LOG_PATH):
    if not os.path.exists(log_path):
        return {
            "status": "no_log",
            "run_id": "-",
            "event": "-",
            "line": "",
            "zero_datum_ready": False,
            "final_cleanup_done": False,
            "need_start_zero": True,
            "zero_reason": "log_missing",
            "zero_line": "",
            "final_line": "",
        }

    current_run = None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as log_file:
            for raw_line in log_file:
                fields = parse_log_fields(raw_line)
                event = fields.get("event")
                if event == "RUN_START":
                    current_run = {
                        "run_id": fields.get("run_id", "-"),
                        "start_line": raw_line.strip(),
                        "last_line": raw_line.strip(),
                        "last_event": event,
                        "terminal_event": None,
                        "terminal_line": "",
                        "zero_line": "",
                        "trusted_zero_line": "",
                        "trusted_start_line": "",
                        "final_line": "",
                        "return_failed_line": "",
                        "final_error_line": "",
                        "acquisition_started_line": "",
                    }
                elif current_run is not None:
                    current_run["last_line"] = raw_line.strip()
                    current_run["last_event"] = event or "-"
                    if event in ("RUN_END_NORMAL", "RUN_END_INTERRUPTED", "RUN_END_ERROR"):
                        current_run["terminal_event"] = event
                        current_run["terminal_line"] = raw_line.strip()
                    elif event == "ZERO_DATUM_REBUILT" and fields.get("reason") == "return_to_start":
                        current_run["zero_line"] = raw_line.strip()
                    elif event == "ZERO_DATUM_TRUSTED_AFTER_LOW_END_RETURN":
                        current_run["trusted_zero_line"] = raw_line.strip()
                    elif event == "POSITION_TRUSTED_AFTER_START_RETURN":
                        current_run["trusted_start_line"] = raw_line.strip()
                    elif event == "FINAL_CLEANUP_DONE":
                        current_run["final_line"] = raw_line.strip()
                    elif event == "RETURN_TO_START_FAILED":
                        current_run["return_failed_line"] = raw_line.strip()
                    elif event == "FINAL_CLEANUP_ERROR":
                        current_run["final_error_line"] = raw_line.strip()
                    elif event == "ACQUISITION_START":
                        current_run["acquisition_started_line"] = raw_line.strip()
    except Exception as exc:
        return {
            "status": "log_read_error",
            "run_id": "-",
            "event": "-",
            "line": str(exc),
            "zero_datum_ready": False,
            "final_cleanup_done": False,
            "need_start_zero": True,
            "zero_reason": "log_read_error",
            "zero_line": "",
            "final_line": "",
        }

    if current_run is None:
        return {
            "status": "no_run_start",
            "run_id": "-",
            "event": "-",
            "line": "",
            "zero_datum_ready": False,
            "final_cleanup_done": False,
            "need_start_zero": True,
            "zero_reason": "no_run_start",
            "zero_line": "",
            "final_line": "",
        }

    event = current_run["terminal_event"] or current_run["last_event"]
    status = {
        "RUN_END_NORMAL": "normal",
        "RUN_END_INTERRUPTED": "interrupted",
        "RUN_END_ERROR": "error",
    }.get(event, "unfinished_or_abnormal")
    zero_datum_ready = bool(
        current_run["zero_line"]
        or current_run["trusted_zero_line"]
        or current_run["trusted_start_line"]
    )
    final_cleanup_done = bool(current_run["final_line"])
    if current_run["return_failed_line"]:
        need_start_zero = True
        zero_reason = "return_to_start_failed"
    elif current_run["final_error_line"]:
        need_start_zero = True
        zero_reason = "final_cleanup_error"
    elif status == "interrupted":
        need_start_zero = True
        zero_reason = "interrupted_requires_rebuild"
    elif status == "error" and current_run["acquisition_started_line"]:
        need_start_zero = True
        zero_reason = "acquisition_error_requires_rebuild"
    elif status == "error" and zero_datum_ready and final_cleanup_done:
        need_start_zero = False
        zero_reason = "pre_acquisition_error_low_end_zero_rebuilt"
    elif status != "normal":
        need_start_zero = True
        zero_reason = f"{status}_requires_rebuild"
    elif not zero_datum_ready:
        need_start_zero = True
        zero_reason = "normal_without_trusted_xy_return"
    elif not final_cleanup_done:
        need_start_zero = True
        zero_reason = "normal_without_final_cleanup_done"
    else:
        need_start_zero = False
        if current_run["zero_line"]:
            zero_reason = "normal_low_end_zero_rebuilt"
        elif current_run["trusted_zero_line"]:
            zero_reason = "normal_low_end_returned_existing_datum_trusted"
        else:
            zero_reason = "normal_returned_to_prealign_start_trusted"
    trusted_return_line = (
        current_run["zero_line"]
        or current_run["trusted_zero_line"]
        or current_run["trusted_start_line"]
    )
    return {
        "status": status,
        "run_id": current_run["run_id"],
        "event": event,
        "line": current_run["final_line"] or current_run["terminal_line"] or current_run["last_line"],
        "zero_datum_ready": zero_datum_ready,
        "final_cleanup_done": final_cleanup_done,
        "need_start_zero": need_start_zero,
        "zero_reason": zero_reason,
        "zero_line": trusted_return_line,
        "final_line": current_run["final_line"],
    }


def resolve_start_zero_policy(policy, previous_run):
    normalized = str(policy).strip().lower()
    if normalized == "auto":
        return bool(previous_run["need_start_zero"]), previous_run["zero_reason"]
    if normalized == "always":
        return True, "policy_always"
    if normalized == "never":
        return False, "policy_never"
    raise ValueError("SAMPLE_START_ZERO_POLICY must be 'auto', 'always', or 'never'.")
