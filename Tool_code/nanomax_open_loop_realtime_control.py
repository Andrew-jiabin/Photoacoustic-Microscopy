"""
Standalone wrapper for the open-loop MAX312D/MDT693B probe prealignment panel.

The shared implementation lives in Nanomax.open_loop_panel so this tool and
PAM_Main_Nanomax.py use the same keyboard controls, safety limits, and status
display.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_MDT_SERIAL_NO = "2201287140-09"
DEFAULT_SERIAL_PORT = None
DEFAULT_SAFE_MAX_VOLTAGE = 75.0
DEFAULT_PIEZO_TRAVEL_UM = 20.0
DEFAULT_PIEZO_TRAVEL_VOLTAGE = 75.0
DEFAULT_STEP_V = 1.0
DEFAULT_SAMPLE_INTERVAL_S = 0.25
DEFAULT_LOG_PATH = REPO_ROOT / "run_logs" / "nanomax_open_loop_realtime_control.log"


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Realtime arrow-key voltage control for the open-loop MAX312D/MDT693B probe stage."
    )
    parser.add_argument("--serial", default=DEFAULT_MDT_SERIAL_NO, help="MDT693B serial number.")
    parser.add_argument("--serial-port", default=DEFAULT_SERIAL_PORT, help="Override the detected MDT serial port, e.g. COM7.")
    parser.add_argument("--safe-max-voltage", type=float, default=DEFAULT_SAFE_MAX_VOLTAGE)
    parser.add_argument("--piezo-travel-um", type=float, default=DEFAULT_PIEZO_TRAVEL_UM)
    parser.add_argument("--piezo-travel-voltage", type=float, default=DEFAULT_PIEZO_TRAVEL_VOLTAGE)
    parser.add_argument("--y-step-v", type=float, default=DEFAULT_STEP_V)
    parser.add_argument("--z-step-v", type=float, default=DEFAULT_STEP_V)
    parser.add_argument("--sample-interval-s", type=float, default=DEFAULT_SAMPLE_INTERVAL_S)
    parser.add_argument("--settle-ms", type=int, default=80)
    parser.add_argument("--backend", default="serial", choices=("serial", "auto", "dll"))
    parser.add_argument("--yes", action="store_true", help="Skip the RUN confirmation prompt.")
    parser.add_argument("--status-only", action="store_true", help="Connect, print current status, and exit.")
    parser.add_argument("--no-set-axis-max", action="store_true", help="Do not set MDT YMAX/ZMAX to safe max.")
    parser.add_argument("--return-yz-zero-on-exit", action="store_true", help="Return Y/Z to 0 V when quitting.")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Text log path.")
    return parser.parse_args()


def append_tool_log(path, event, **fields):
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        parts = [f"ts={now_iso()}", f"event={event}"]
        for key, value in fields.items():
            text = str(value).replace("\n", "\\n").replace("\r", "\\r").replace("|", "/")
            parts.append(f"{key}={text}")
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(" | ".join(parts) + "\n")
    except Exception:
        pass


def ensure_windows_keyboard() -> None:
    if os.name != "nt":
        raise SystemExit("Realtime arrow-key control currently requires a Windows console with msvcrt.")


def main() -> None:
    args = parse_args()
    ensure_windows_keyboard()

    from Alazar_imaging.MDT693BController import MDT693BController
    from Nanomax.open_loop_panel import ProbePrealignConfig, ProbePrealignPanel, run_probe_prealignment

    stage = MDT693BController(
        serial_no=args.serial,
        serial_port=args.serial_port,
        safe_max_voltage=args.safe_max_voltage,
        backend=args.backend,
    )
    config = ProbePrealignConfig(
        safe_max_voltage=args.safe_max_voltage,
        piezo_travel_um=args.piezo_travel_um,
        piezo_travel_voltage=args.piezo_travel_voltage,
        y_step_v=args.y_step_v,
        z_step_v=args.z_step_v,
        sample_interval_s=args.sample_interval_s,
        settle_ms=args.settle_ms,
        set_axis_max=not args.no_set_axis_max,
        return_yz_zero_on_exit=args.return_yz_zero_on_exit,
    )
    log = lambda event, **fields: append_tool_log(args.log_path, event, **fields)

    try:
        if args.status_only:
            panel = ProbePrealignPanel(stage, config, log_callback=log)
            print("Realtime open-loop MAX312D/MDT693B Y/Z control")
            for line in panel.status_lines(refresh=True):
                print(line)
            return

        print("Realtime open-loop MAX312D/MDT693B Y/Z control")
        if not args.yes:
            answer = input("This enables live voltage control. Type RUN to continue: ").strip()
            if answer != "RUN":
                print("Cancelled; no live control started.")
                return

        try:
            run_probe_prealignment(stage, config, log_callback=log)
        except KeyboardInterrupt as exc:
            print(f"\n{exc}")
    finally:
        stage.close()
        print("MDT693B controller closed.", flush=True)


if __name__ == "__main__":
    main()
