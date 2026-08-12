import argparse
import datetime
import os
import sys
import traceback
from dataclasses import dataclass

from Alazar_imaging.BPC303NativeController import BPC303NativeController
from Alazar_imaging.MDT693BController import MDT693BController
from Nanomax.open_loop_panel import ProbePrealignConfig
from Nanomax.prealign_panel import SamplePrealignConfig
from Nanomax.prealignment_workflow import run_nanomax_prealignment
from Nanomax.run_log import append_run_log, set_current_run_id


DEFAULT_BPC303_SERIAL_NO = "71241834"
DEFAULT_BPC303_KINESIS_DIR = r"C:\Program Files\Thorlabs\Kinesis"
DEFAULT_BPC303_AXIS_MAP = {"x": 1, "y": 2, "z": 3}
DEFAULT_BPC303_SAFE_MAX_OUTPUT_VOLTAGE = 75.0

DEFAULT_PROBE_MDT_SERIAL_NO = "2201287140-09"
DEFAULT_PROBE_MDT_SERIAL_PORT = None
DEFAULT_PROBE_MDT_BACKEND = "serial"
DEFAULT_PROBE_MDT_DLL_PATH = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\Alazar_imaging\MDT_COMMAND_LIB_x64.dll"
DEFAULT_PROBE_SAFE_MAX_VOLTAGE = 75.0
DEFAULT_PROBE_PIEZO_TRAVEL_UM = 20.0
DEFAULT_PROBE_PIEZO_TRAVEL_VOLTAGE = 75.0


@dataclass
class NanoMaxMotionDebugOptions:
    sample_enabled: bool = True
    probe_enabled: bool = True
    initial_panel: str = "sample"
    bpc303_serial_no: str = DEFAULT_BPC303_SERIAL_NO
    bpc303_kinesis_dir: str = DEFAULT_BPC303_KINESIS_DIR
    probe_mdt_serial_no: str = DEFAULT_PROBE_MDT_SERIAL_NO
    probe_mdt_serial_port: str = DEFAULT_PROBE_MDT_SERIAL_PORT
    probe_mdt_backend: str = DEFAULT_PROBE_MDT_BACKEND
    probe_mdt_dll_path: str = DEFAULT_PROBE_MDT_DLL_PATH
    sample_x_step_um: float = 0.1
    sample_y_step_um: float = 0.1
    sample_z_step_um: float = 0.1
    probe_y_step_v: float = 1.0
    probe_z_step_v: float = 1.0
    sample_interval_s: float = 0.25
    auto_refresh_s: float = 5.0
    settle_ms: int = 120
    sample_position_tolerance_um: float = 0.02
    sample_position_timeout_s: float = 300.0
    sample_position_reissue_interval_s: float = 1.0
    probe_safe_max_voltage: float = DEFAULT_PROBE_SAFE_MAX_VOLTAGE
    probe_piezo_travel_um: float = DEFAULT_PROBE_PIEZO_TRAVEL_UM
    probe_piezo_travel_voltage: float = DEFAULT_PROBE_PIEZO_TRAVEL_VOLTAGE
    probe_set_axis_max: bool = True


class NanoMaxMotionDebugSession:
    """Interactive NanoMax-only motion panel without DAQ or laser initialization."""

    def __init__(self, options=None, log_callback=None):
        self.options = options or NanoMaxMotionDebugOptions()
        self.log = log_callback or append_run_log
        self.sample_stage = None
        self.probe_stage = None
        self.probe_connect_error = ""

    def _disabled_daq_status(self):
        return {
            "status": "disabled",
            "step": "nanomax_motion_debug",
            "message": "DAQ is intentionally not initialized in this NanoMax-only debug script.",
            "elapsed_s": 0.0,
            "timings": {},
            "error": "",
        }

    def connect(self):
        if self.options.sample_enabled:
            self.log("MOTION_DEBUG_STAGE_CONNECT_BEGIN", controller="BPC303", stage_model="MAX311D")
            self.sample_stage = BPC303NativeController(
                serial_no=self.options.bpc303_serial_no,
                kinesis_dir=self.options.bpc303_kinesis_dir,
                channels=(1, 2, 3),
                axis_map=DEFAULT_BPC303_AXIS_MAP,
                safe_max_output_voltage=DEFAULT_BPC303_SAFE_MAX_OUTPUT_VOLTAGE,
                log_callback=self.log,
            )
            self.log(
                "MOTION_DEBUG_STAGE_CONNECT_DONE",
                controller="BPC303",
                serial=self.options.bpc303_serial_no,
                max_travel_x_um=self.sample_stage.get_max_travel("x"),
                max_travel_y_um=self.sample_stage.get_max_travel("y"),
                max_travel_z_um=self.sample_stage.get_max_travel("z"),
            )

        if self.options.probe_enabled:
            self.log("MOTION_DEBUG_STAGE_CONNECT_BEGIN", controller="MDT693B", stage_model="MAX312D")
            try:
                self.probe_stage = MDT693BController(
                    serial_no=self.options.probe_mdt_serial_no,
                    dll_path=self.options.probe_mdt_dll_path,
                    safe_max_voltage=self.options.probe_safe_max_voltage,
                    um_per_volt=self.options.probe_piezo_travel_um / self.options.probe_piezo_travel_voltage,
                    backend=self.options.probe_mdt_backend,
                    serial_port=self.options.probe_mdt_serial_port,
                )
                self.log(
                    "MOTION_DEBUG_STAGE_CONNECT_DONE",
                    controller="MDT693B",
                    serial=self.probe_stage.serial_no,
                    serial_port=self.probe_stage.serial_port,
                    active_backend=getattr(self.probe_stage, "_active_backend", "-"),
                    device_id=self.probe_stage.device_id,
                    limit_voltage=self.probe_stage.limit_voltage,
                    safe_max_voltage=self.options.probe_safe_max_voltage,
                )
            except Exception as exc:
                self.probe_connect_error = repr(exc)
                self.log("MOTION_DEBUG_PROBE_CONNECT_FAILED", error=repr(exc))
                print(f"Open-loop probe controller unavailable: {exc}")
                self.probe_stage = None

        if self.sample_stage is None and self.probe_stage is None:
            raise RuntimeError("No NanoMax controller was connected; cannot open debug panel.")

    def run(self):
        self.connect()
        try:
            result = run_nanomax_prealignment(
                sample_stage=self.sample_stage,
                sample_config=(
                    SamplePrealignConfig(
                        scan_range_x_um=0.0,
                        scan_range_y_um=0.0,
                        step_um=max(0.02, float(self.options.sample_x_step_um)),
                        settle_ms=self.options.settle_ms,
                        position_tolerance_um=self.options.sample_position_tolerance_um,
                        position_timeout_s=self.options.sample_position_timeout_s,
                        position_reissue_interval_s=self.options.sample_position_reissue_interval_s,
                        x_step_um=self.options.sample_x_step_um,
                        y_step_um=self.options.sample_y_step_um,
                        z_step_um=self.options.sample_z_step_um,
                        sample_interval_s=self.options.sample_interval_s,
                        auto_refresh_s=self.options.auto_refresh_s,
                    )
                    if self.sample_stage is not None
                    else None
                ),
                probe_stage=self.probe_stage,
                probe_config=(
                    ProbePrealignConfig(
                        safe_max_voltage=self.options.probe_safe_max_voltage,
                        piezo_travel_um=self.options.probe_piezo_travel_um,
                        piezo_travel_voltage=self.options.probe_piezo_travel_voltage,
                        y_step_v=self.options.probe_y_step_v,
                        z_step_v=self.options.probe_z_step_v,
                        sample_interval_s=self.options.sample_interval_s,
                        auto_refresh_s=self.options.auto_refresh_s,
                        settle_ms=self.options.settle_ms,
                        set_axis_max=self.options.probe_set_axis_max,
                    )
                    if self.probe_stage is not None
                    else None
                ),
                initial_panel=self.options.initial_panel,
                log_callback=self.log,
                status_provider=self._disabled_daq_status,
                display_params={
                    "SCAN_TARGET": "nanomax_motion_debug",
                    "SAMPLE_CONTROLLER": "BPC303",
                    "SAMPLE_STAGE_MODEL": "MAX311D",
                    "SAMPLE_CONNECTION": "connected" if self.sample_stage is not None else "disabled",
                    "SAMPLE_SERIAL": self.options.bpc303_serial_no if self.sample_stage is not None else "-",
                    "SAMPLE_AXIS_MAP": "1/2/3=X/Y/Z",
                    "PROBE_CONTROLLER": "MDT693B",
                    "PROBE_STAGE_MODEL": "MAX312D",
                    "PROBE_CONNECTION": "connected" if self.probe_stage is not None else "disabled",
                    "PROBE_SERIAL": getattr(self.probe_stage, "serial_no", self.options.probe_mdt_serial_no) if self.probe_stage is not None else "-",
                    "PROBE_PORT": getattr(self.probe_stage, "serial_port", self.options.probe_mdt_serial_port or "-") if self.probe_stage is not None else "-",
                    "PROBE_BACKEND": getattr(self.probe_stage, "_active_backend", self.options.probe_mdt_backend) if self.probe_stage is not None else "-",
                    "PROBE_DEVICE_ID": getattr(self.probe_stage, "device_id", "-") if self.probe_stage is not None else "-",
                    "PROBE_LIMIT_V": getattr(self.probe_stage, "limit_voltage", "-") if self.probe_stage is not None else "-",
                    "PROBE_CONNECT_ERROR": self.probe_connect_error,
                    "DELAY": "-",
                    "SAMPLES_REC": "-",
                    "SAMPLE_RATE": "-",
                    "AVERAGE_ENABLE": "-",
                    "ACQ_TIMEOUT_MS": "-",
                    "RECORDS_PER_POINT": "-",
                    "BUFFER_COUNT": "-",
                    "POINT_LOG_INTERVAL": "-",
                    "USER_STOP_ENABLE": "panel-only",
                    "USER_STOP_KEY": "q",
                    "SAMPLE_START_ZERO_POLICY": "not used",
                    "SAMPLE_ZERO_XY_AT_END": "not used",
                    "PANEL_AUTO_REFRESH_S": self.options.auto_refresh_s,
                    "PROBE_SCAN_AXES": "debug Y/Z only",
                },
            )
            self.log(
                "MOTION_DEBUG_PANEL_DONE",
                next_action=getattr(result, "next_action", "-"),
                start_panel=getattr(result, "start_panel", "-"),
            )
            print("NanoMax motion debug panel closed. No DAQ or laser device was initialized.")
            return result
        finally:
            self.close()

    def close(self):
        if self.probe_stage is not None:
            try:
                self.probe_stage.close()
                self.log("MOTION_DEBUG_STAGE_CLOSED", controller="MDT693B")
            except Exception as exc:
                self.log("MOTION_DEBUG_STAGE_CLOSE_FAILED", controller="MDT693B", error=repr(exc))
        if self.sample_stage is not None:
            try:
                self.sample_stage.close()
                self.log("MOTION_DEBUG_STAGE_CLOSED", controller="BPC303")
            except Exception as exc:
                self.log("MOTION_DEBUG_STAGE_CLOSE_FAILED", controller="BPC303", error=repr(exc))


def _env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name, default):
    value = os.environ.get(name)
    return float(default if value is None or value == "" else value)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Open a NanoMax-only motion debug panel. This script does not initialize DAQ or lasers."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--sample-only", action="store_true", help="connect only BPC303/MAX311D closed-loop sample stage")
    group.add_argument("--probe-only", action="store_true", help="connect only MDT693B/MAX312D open-loop probe stage")
    parser.add_argument("--initial-panel", choices=("sample", "probe"), default=os.environ.get("PAM_NANOMAX_DEBUG_INITIAL_PANEL", "sample"))
    parser.add_argument("--bpc303-serial", default=os.environ.get("PAM_BPC303_SERIAL", DEFAULT_BPC303_SERIAL_NO))
    parser.add_argument("--bpc303-kinesis-dir", default=os.environ.get("PAM_BPC303_KINESIS_DIR", DEFAULT_BPC303_KINESIS_DIR))
    parser.add_argument("--mdt-serial", default=os.environ.get("PAM_MDT693B_SERIAL", DEFAULT_PROBE_MDT_SERIAL_NO))
    parser.add_argument("--mdt-port", default=os.environ.get("PAM_MDT693B_PORT", DEFAULT_PROBE_MDT_SERIAL_PORT))
    parser.add_argument("--mdt-backend", default=os.environ.get("PAM_MDT693B_BACKEND", DEFAULT_PROBE_MDT_BACKEND))
    parser.add_argument("--mdt-dll", default=os.environ.get("PAM_MDT693B_DLL", DEFAULT_PROBE_MDT_DLL_PATH))
    parser.add_argument("--sample-step", type=float, default=_env_float("PAM_NANOMAX_DEBUG_SAMPLE_STEP_UM", 0.1))
    parser.add_argument("--probe-step", type=float, default=_env_float("PAM_NANOMAX_DEBUG_PROBE_STEP_V", 1.0))
    parser.add_argument("--interval", type=float, default=_env_float("PAM_NANOMAX_DEBUG_INTERVAL_S", 0.25))
    parser.add_argument("--refresh", type=float, default=_env_float("PAM_NANOMAX_DEBUG_REFRESH_S", 5.0))
    parser.add_argument("--settle-ms", type=int, default=int(_env_float("PAM_NANOMAX_DEBUG_SETTLE_MS", 120)))
    parser.add_argument("--probe-safe-max", type=float, default=_env_float("PAM_NANOMAX_DEBUG_PROBE_SAFE_MAX_V", DEFAULT_PROBE_SAFE_MAX_VOLTAGE))
    parser.add_argument("--no-probe-set-axis-max", action="store_true", help="do not send MDT YMAX/ZMAX on startup")
    return parser.parse_args(argv)


def options_from_args(args):
    sample_enabled = not args.probe_only
    probe_enabled = not args.sample_only
    return NanoMaxMotionDebugOptions(
        sample_enabled=sample_enabled,
        probe_enabled=probe_enabled,
        initial_panel=args.initial_panel,
        bpc303_serial_no=args.bpc303_serial,
        bpc303_kinesis_dir=args.bpc303_kinesis_dir,
        probe_mdt_serial_no=args.mdt_serial,
        probe_mdt_serial_port=args.mdt_port,
        probe_mdt_backend=args.mdt_backend,
        probe_mdt_dll_path=args.mdt_dll,
        sample_x_step_um=args.sample_step,
        sample_y_step_um=args.sample_step,
        sample_z_step_um=args.sample_step,
        probe_y_step_v=args.probe_step,
        probe_z_step_v=args.probe_step,
        sample_interval_s=args.interval,
        auto_refresh_s=args.refresh,
        settle_ms=args.settle_ms,
        probe_safe_max_voltage=args.probe_safe_max,
        probe_set_axis_max=not args.no_probe_set_axis_max,
    )


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    run_id = "nanomax_debug_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    set_current_run_id(run_id)
    args = parse_args(argv)
    options = options_from_args(args)
    append_run_log(
        "MOTION_DEBUG_RUN_START",
        sample_enabled=options.sample_enabled,
        probe_enabled=options.probe_enabled,
        initial_panel=options.initial_panel,
        pid=os.getpid(),
        cwd=os.getcwd(),
    )
    try:
        NanoMaxMotionDebugSession(options).run()
        append_run_log("MOTION_DEBUG_RUN_END_NORMAL")
    except KeyboardInterrupt:
        append_run_log("MOTION_DEBUG_RUN_END_INTERRUPTED")
        print("\nNanoMax motion debug interrupted by user.")
    except Exception as exc:
        append_run_log("MOTION_DEBUG_RUN_END_ERROR", error=repr(exc), traceback=traceback.format_exc(limit=6))
        raise


if __name__ == "__main__":
    main()
