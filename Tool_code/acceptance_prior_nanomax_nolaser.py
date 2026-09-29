"""Hardware-free acceptance checks for the two laser-free PAM entry points.

Verifies ``PAM_Main_Nanomax_ClosedLoop.py`` and ``PAM_Main_Prior.py`` without touching
any hardware. The Alazar board is never opened, the Prior DLL is never loaded, no serial
port is opened and neither translation stage is commanded. Every controller interaction
goes through a fake object that only records the commands it was given.

Safe to run with the stages powered off, with the Alazar board unplugged, and with
``PAM_Main_*.py`` not running.

Checks
  1  syntax        py_compile every touched file
  2  imports       both entry points import cleanly, and the Prior program keeps its own
                   run log so a Prior run cannot be read as NanoMax zero-datum history
  3  step size     every PAM_PRIOR_SS_MODE (high / micron / legacy / value / none), the
                   leftover-ss hazard, and the refusal to guess when the controller will
                   not report steps-per-micron
  4  step grid     check_step_grid() accepts whole-unit steps and rejects the rest
  5  adapter       PriorStageAdapter against a fake stage: unit scaling, rounding,
                   travel widening, Z refusal, settle success and settle timeout
  6  no-laser      NoLaserManager status, panel items and command handling
  7  geometry      scan shape, pattern, trajectory (including flipped directions) and
                   travel validation
  8  panel parity  the shared panels still render the historical default strings, and
                   drop the probe rows only when the caller asks for it
  9  sweep         no removed hardware symbol is reachable from either entry point
 10  dry run       PAM_Main_Prior.main() runs end to end with every hardware seam faked,
                   in a scratch directory: the whole startup -> scan -> save -> return ->
                   cleanup path, the .mat it writes, and the run-log event chain

Exit codes
  0  every check passed
  1  at least one check failed
  2  the harness could not run (missing file, unimportable module, bad repo root)

Set ``PAM_ACCEPTANCE_REPO`` to point at a different checkout. Default is the repository
this file lives in.
"""

import importlib
import os
import py_compile
import subprocess
import sys
import tempfile
import time


REPO_ROOT = os.environ.get("PAM_ACCEPTANCE_REPO") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)

COMPILE_TARGETS = (
    "PAM_Main_Nanomax_ClosedLoop.py",
    "PAM_Main_Prior.py",
    "Alazar_imaging/PriorStageAdapter.py",
    "Nanomax/no_laser_manager.py",
    "Nanomax/acquisition_panel.py",
    "Nanomax/data_io.py",
    "Nanomax/prealign_panel.py",
    "Nanomax/runtime.py",
    "Nanomax/terminal_panel.py",
    "Nanomax/scan_utils.py",
    "Tool_code/acceptance_prior_nanomax_nolaser.py",
)

# Symbol names that must not be reachable from either new entry point.
FORBIDDEN_IN_ENTRY_POINTS = (
    "LBMover",
    "lbtek_wait_settled",
    "MDT_COMMAND_LIB",
    "moverLibrary.dll",
    "nidaqmx",
    "pyvisa",
    "emcvx",
)

# Removals that are only allowed to appear inside the module docstring, where they are
# documented rather than used.
DOCSTRING_ONLY_ALLOWED = ("LBTEK", "lbtek", "LBMover", "toptica", "TOPTICA", "cbox", "CBOX")


def safe(text):
    """Force a string to ASCII so a GBK console cannot raise UnicodeEncodeError."""
    return str(text).encode("ascii", "replace").decode("ascii")


class Harness:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def section(self, title):
        print("")
        print("=" * 74)
        print(safe(title))
        print("=" * 74)

    def ok(self, name, detail=""):
        self.passed += 1
        line = "  PASS  " + safe(name)
        if detail:
            line += "   [" + safe(detail) + "]"
        print(line)

    def fail(self, name, detail=""):
        self.failed += 1
        self.failures.append((safe(name), safe(detail)))
        line = "  FAIL  " + safe(name)
        if detail:
            line += "   [" + safe(detail) + "]"
        print(line)

    def expect(self, name, condition, detail=""):
        if condition:
            self.ok(name, detail)
        else:
            self.fail(name, detail)
        return bool(condition)


class FakePriorStage:
    """Stand-in for PriorUnifiedStage. No DLL, no serial port, no hardware.

    Only implements the surface PriorStageAdapter actually uses.
    """

    def __init__(self, x_units=100.0, y_units=20.0, steps_per_micron=50.0, ss=1.0, obey_moves=True):
        self.x_units = float(x_units)
        self.y_units = float(y_units)
        self.z_units = 0.0
        self.steps_per_micron = steps_per_micron
        self.ss = float(ss)
        self.obey_moves = bool(obey_moves)
        self.commands = []
        self.deinitialised = False
        self.stopped = False

    # -- readback ---------------------------------------------------------
    def get_position(self):
        return "%.0f,%.0f" % (self.x_units, self.y_units)

    # -- generic command channel ------------------------------------------
    def cmd(self, msg):
        text = str(msg).strip()
        self.commands.append(text)
        if text == "controller.stage.steps-per-micron.get":
            if self.steps_per_micron is None:
                return 0, "not available"
            return 0, "%.0f" % self.steps_per_micron
        if text == "controller.stage.ss.get":
            return 0, "%.0f" % self.ss
        if text == "controller.z.position.get":
            return 0, "%.0f" % self.z_units
        if text == "controller.stage.busy":
            return 0, "0"
        return 0, "0"

    def cmd_simple(self, msg):
        text = str(msg).strip()
        self.commands.append(text)
        if text.startswith("controller.stage.ss.set"):
            self.ss = float(text.split()[-1])
        elif text.startswith("controller.z.goto-position"):
            if self.obey_moves:
                self.z_units = float(text.split()[-1])
        return 0

    # -- absolute moves ---------------------------------------------------
    def set_position(self, position):
        values = [float(v) for v in position]
        self.commands.append("set_position:%d,%d" % (int(values[0]), int(values[1])))
        if self.obey_moves:
            self.x_units, self.y_units = values[0], values[1]

    def emergency_stop(self):
        self.stopped = True

    def stage_deinitial(self):
        self.deinitialised = True


class LineRecorder:
    """Swallows renderer output and keeps the lines for inspection."""

    def __init__(self):
        self.lines = []

    def render(self, lines):
        self.lines = list(lines)

    def show_cursor(self):
        pass

    def hide_cursor(self):
        pass

    def text(self):
        return "\n".join(str(line) for line in self.lines)


def run_child(code, timeout=90):
    """Run a snippet in a fresh interpreter rooted at the repository."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "ascii:replace"
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
    )
    return completed


# ---------------------------------------------------------------------------
# End-to-end dry run support
#
# ``main()`` is the largest untested surface in either program: sections 1-9 exercise the
# pieces it calls, but nothing executes the function itself. Section 10 runs
# ``PAM_Main_Prior.main()`` in a child interpreter with every hardware seam replaced, in a
# throwaway working directory, so the whole startup -> scan -> save -> return -> cleanup
# path is proven with both stages powered off.
#
# The fakes are injected through a ``sitecustomize.py`` placed first on ``PYTHONPATH``.
# That is the only hook early enough: ``PAM_Main_Prior`` binds ``PriorUnifiedStage`` and
# ``AlazarNPTSystem`` into its own namespace at import time, so patching the source modules
# afterwards would be too late.
# ---------------------------------------------------------------------------

DRY_RUN_SITECUSTOMIZE = r'''
"""Injected via PYTHONPATH for the PAM_Main_Prior.main() dry run.

Replaces every hardware seam with a recorder before the program is imported, and moves the
run log into the scratch directory so a test cannot pollute the repository's real log.
"""
import builtins
import os
import sys

REPO = os.environ["PAM_DRYRUN_REPO"]
WORK = os.environ["PAM_DRYRUN_WORK"]
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 1. Keep the dry run out of the repository's run log. PAM_Main_Prior derives its own
#    PAM_Main_Prior_run.log from whatever run_log.RUN_LOG_PATH holds at import time, so
#    moving it here redirects both.
from Nanomax import run_log as _run_log

_run_log.RUN_LOG_PATH = os.path.join(WORK, "run_logs", "PAM_Main_Nanomax_run.log")

# 2. Prior controller stand-in. Mirrors the surface PriorStageAdapter actually uses.
from Alazar_imaging import PriorUnifiedStage as _pus

COMMANDS = []


class DryRunPriorStage:
    def __init__(self, dll_path=None, com_port_number=None, baudrate=115200, **kwargs):
        COMMANDS.append("__init__")
        self.x_units = 100.0
        self.y_units = 40.0
        self.z_units = 0.0
        self.steps_per_micron = 50.0
        self.ss = 1.0
        self.deinitialised = False

    def get_position(self):
        return "%.0f,%.0f" % (self.x_units, self.y_units)

    def cmd(self, msg):
        text = str(msg).strip()
        COMMANDS.append(text)
        if text == "controller.stage.steps-per-micron.get":
            return 0, "%.0f" % self.steps_per_micron
        if text == "controller.stage.ss.get":
            return 0, "%.0f" % self.ss
        if text == "controller.z.position.get":
            return 0, "%.0f" % self.z_units
        return 0, "0"

    def cmd_simple(self, msg):
        text = str(msg).strip()
        COMMANDS.append(text)
        if text.startswith("controller.stage.ss.set"):
            self.ss = float(text.split()[-1])
        return 0

    def set_position(self, position):
        values = [float(value) for value in position]
        COMMANDS.append("set_position:%d,%d" % (int(values[0]), int(values[1])))
        self.x_units, self.y_units = values[0], values[1]

    def emergency_stop(self):
        COMMANDS.append("emergency_stop")

    def stage_deinitial(self):
        self.deinitialised = True
        COMMANDS.append("stage_deinitial")


_pus.PriorUnifiedStage = DryRunPriorStage

# 3. Digitizer stand-in: no board is opened, each point yields one full record.
from Alazar_imaging import AlazarNPTSystem as _npt


class DryRunDaq:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.prepared = None
        self.stopped = False
        self.points = 0

    def configure_board(self, **kwargs):
        self.board_kwargs = kwargs

    def prepare_acquisition(self, **kwargs):
        self.prepared = kwargs

    def get_one_acquisition(
        self, all_data=None, curr_pos_str=None, timeout_ms=None, Average_Enable=None
    ):
        self.points += 1
        samples = int(self.prepared["samples_per_record"])
        all_data.append([[0] * samples, curr_pos_str, {}])

    def stop_capture(self):
        self.stopped = True


_npt.AlazarNPTSystem = DryRunDaq

# 4. The only prompt left on the PREALIGN_ENABLE=0 path is the start gate.
builtins.input = lambda *args, **kwargs: ""

# 5. msvcrt.kbhit() needs a real console handle and raises when the suite runs over SSH
#    with piped stdio. This dry run is deliberately non-interactive, so the keyboard seam
#    reports "no key pressed" forever and the stop key is never seen.
try:
    import msvcrt

    msvcrt.kbhit = lambda: False
except Exception:
    pass
'''


DRY_RUN_DRIVER = r'''
"""Runs PAM_Main_Prior.main() with every hardware seam faked by sitecustomize.py."""
import glob
import os

import numpy as np

import PAM_Main_Prior as prog

prog.main()
print("DRYRUN_MAIN_RETURNED")

import Nanomax.run_log as run_log

print("DRYRUN_LOG_PATH=" + run_log.RUN_LOG_PATH)

mats = sorted(glob.glob(os.path.join("data", "*.mat")))
print("DRYRUN_MAT_COUNT=%d" % len(mats))
if mats:
    import scipy.io as sio

    data = sio.loadmat(mats[0])
    record = data["metadata"]
    if isinstance(record, np.ndarray) and record.shape == (1, 1):
        record = record[0, 0]

    def scalar_text(item):
        inner = np.asarray(item)
        if inner.dtype.kind in ("U", "S"):
            return "".join(str(part) for part in inner.reshape(-1))
        if inner.size == 1:
            return str(inner.reshape(-1)[0])
        return "|".join(str(part) for part in inner.reshape(-1))

    def emit(label, value):
        arr = np.asarray(value)
        if arr.dtype.kind in ("U", "S"):
            # A MATLAB char matrix: one continuous string, not one entry per character.
            print("%s=%s" % (label, "".join(str(part) for part in arr.reshape(-1))))
            return
        if arr.dtype.kind == "O":
            print("%s=%s" % (label, "|".join(scalar_text(item) for item in arr.reshape(-1))))
            return
        print("%s=%s" % (label, "|".join(str(part) for part in arr.reshape(-1))))

    def count_of(value):
        arr = np.asarray(value)
        if arr.dtype.kind in ("U", "S"):
            # A char matrix carries one row per original string.
            return int(arr.shape[0]) if arr.ndim == 2 else int(arr.size)
        return int(arr.size)

    emit("DRYRUN_META_SCAN_TARGET", record["scan_target"])
    emit("DRYRUN_META_SAMPLE_STAGE", record["sample_stage"])
    emit("DRYRUN_META_SAMPLE_CONTROLLER", record["sample_controller"])
    emit("DRYRUN_META_PROBE_STAGE", record["probe_stage"])
    emit("DRYRUN_META_PROBE_CONTROLLER", record["probe_controller"])
    emit("DRYRUN_META_UM_PER_UNIT", record["sample_unit_um_per_unit"])
    emit("DRYRUN_META_SS_SENT", record["sample_ss_sent"])
    emit("DRYRUN_META_SCAN_SHAPE", record["scan_shape"])
    emit("DRYRUN_META_STEP_UM", record["step_um"])
    emit("DRYRUN_META_POS_LIST_COUNT", [count_of(record["pos_list"])])
    emit(
        "DRYRUN_META_SETTLE_OK",
        [int(sum(int(value) for value in np.asarray(record["position_settle_ok"]).reshape(-1)))],
    )
    emit(
        "DRYRUN_META_TIMEOUTS",
        [int(sum(int(value) for value in np.asarray(record["position_timeout"]).reshape(-1)))],
    )
    # savemat adds __header__ / __version__ / __globals__ alongside the real keys, so count
    # only the waveform entries: every acquired point becomes one, plus "metadata".
    waveform_keys = [
        key for key in data.keys() if key != "metadata" and not key.startswith("__")
    ]
    print("DRYRUN_MAT_WAVEFORMS=%d" % len(waveform_keys))
    print("DRYRUN_MAT_KEYS=%d" % len(data.keys()))

import sitecustomize

commands = list(sitecustomize.COMMANDS)
print("DRYRUN_STAGE_COMMANDS=%d" % len(commands))
print(
    "DRYRUN_SS_SET="
    + "|".join(command for command in commands if command.startswith("controller.stage.ss.set"))
)
print(
    "DRYRUN_MOVES=%d"
    % len([command for command in commands if command.startswith("set_position:")])
)
print("DRYRUN_DEINIT=%s" % ("stage_deinitial" in commands))
'''


def run_dry_run_child(work_dir, env_overrides, timeout=240):
    """Run PAM_Main_Prior.main() in a child interpreter with the hardware seams faked."""
    site_dir = os.path.join(work_dir, "site")
    os.makedirs(site_dir, exist_ok=True)
    with open(
        os.path.join(site_dir, "sitecustomize.py"), "w", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(DRY_RUN_SITECUSTOMIZE)

    driver_path = os.path.join(work_dir, "dry_run_driver.py")
    with open(driver_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(DRY_RUN_DRIVER)

    env = dict(os.environ)
    env["PYTHONPATH"] = site_dir + os.pathsep + REPO_ROOT
    env["PYTHONIOENCODING"] = "ascii:replace"
    env["PAM_DRYRUN_REPO"] = REPO_ROOT
    env["PAM_DRYRUN_WORK"] = work_dir
    for key, value in env_overrides.items():
        env[str(key)] = str(value)

    return subprocess.run(
        [sys.executable, driver_path],
        cwd=work_dir,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )


def value_of(output, label):
    """Return the value printed as ``label=value``, or a placeholder when absent."""
    prefix = label + "="
    for line in (output or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return "no " + label + " line"


def int_of(output, label, default=0):
    """Return the integer printed as ``label=value``, or ``default`` when unusable.

    Keeps a missing line from turning into a harness-level ValueError, so the report shows
    the failed check rather than an aborted run.
    """
    try:
        return int(value_of(output, label))
    except (TypeError, ValueError):
        return default


def main():
    harness = Harness()

    print("PAM laser-free entry point acceptance (hardware-free)")
    print("repo root: " + safe(REPO_ROOT))
    print("python:    " + safe(sys.executable))
    print("note:      no board, no DLL, no serial port, no stage motion is attempted")

    if not os.path.isdir(REPO_ROOT):
        print("")
        print("ERROR: repository root does not exist: " + safe(REPO_ROOT))
        return 2

    missing = [name for name in COMPILE_TARGETS if not os.path.exists(os.path.join(REPO_ROOT, name))]
    if missing:
        print("")
        print("ERROR: required files are missing from the checkout:")
        for name in missing:
            print("  " + safe(name))
        return 2

    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    # scan_utils is used by the adapter tests in section 5 as well as the geometry tests in
    # section 7, so bind it once here. Importing it lazily inside section 7 made the earlier
    # references raise UnboundLocalError.
    try:
        scan_utils = importlib.import_module("Nanomax.scan_utils")
    except Exception as exc:
        print("")
        print("ERROR: could not import Nanomax.scan_utils: " + safe(repr(exc)))
        return 2

    # ---------------------------------------------------------------- 1 syntax
    harness.section("1. syntax")
    with tempfile.TemporaryDirectory() as cache_dir:
        for name in COMPILE_TARGETS:
            path = os.path.join(REPO_ROOT, name)
            cfile = os.path.join(cache_dir, name.replace("/", "__") + "c")
            try:
                py_compile.compile(path, cfile=cfile, doraise=True)
                harness.ok("py_compile " + name)
            except py_compile.PyCompileError as exc:
                harness.fail("py_compile " + name, str(exc))

    # --------------------------------------------------------------- 2 imports
    harness.section("2. imports and run-log separation")

    prior_code = (
        "import importlib\n"
        "m = importlib.import_module('PAM_Main_Prior')\n"
        "from Nanomax import run_log\n"
        "print('LOG=' + run_log.RUN_LOG_PATH)\n"
        "print('MAT_TARGET=' + m.MAT_SCAN_TARGET)\n"
        "print('RETURN_TARGET=' + m.RETURN_TO_START_TARGET)\n"
    )
    try:
        completed = run_child(prior_code)
        output = completed.stdout or ""
        harness.expect(
            "PAM_Main_Prior imports without touching hardware",
            completed.returncode == 0,
            (completed.stderr or "").strip().splitlines()[-1] if completed.returncode else "exit 0",
        )
        harness.expect(
            "PAM_Main_Prior writes its own run log",
            "LOG=" in output and output.split("LOG=")[1].splitlines()[0].strip().endswith("PAM_Main_Prior_run.log"),
            output.split("LOG=")[1].splitlines()[0].strip() if "LOG=" in output else "no LOG line",
        )
        harness.expect(
            "PAM_Main_Prior records prior_closed_loop in the .mat metadata",
            "MAT_TARGET=prior_closed_loop" in output,
        )
        harness.expect(
            "PAM_Main_Prior hands runtime.return_to_start a target it understands",
            "RETURN_TARGET=sample_closed_loop" in output,
        )
    except subprocess.TimeoutExpired:
        harness.fail("PAM_Main_Prior imports without touching hardware", "timed out")

    closed_loop_code = (
        "import importlib\n"
        "m = importlib.import_module('PAM_Main_Nanomax_ClosedLoop')\n"
        "from Nanomax import run_log\n"
        "print('LOG=' + run_log.RUN_LOG_PATH)\n"
        "print('SCAN_TARGET=' + m.SCAN_TARGET)\n"
    )
    try:
        completed = run_child(closed_loop_code)
        output = completed.stdout or ""
        harness.expect(
            "PAM_Main_Nanomax_ClosedLoop imports without touching hardware",
            completed.returncode == 0,
            (completed.stderr or "").strip().splitlines()[-1] if completed.returncode else "exit 0",
        )
        harness.expect(
            "PAM_Main_Nanomax_ClosedLoop keeps the shared NanoMax run log",
            "LOG=" in output and output.split("LOG=")[1].splitlines()[0].strip().endswith("PAM_Main_Nanomax_run.log"),
            output.split("LOG=")[1].splitlines()[0].strip() if "LOG=" in output else "no LOG line",
        )
        harness.expect(
            "PAM_Main_Nanomax_ClosedLoop scans the sample_closed_loop target",
            "SCAN_TARGET=sample_closed_loop" in output,
        )
    except subprocess.TimeoutExpired:
        harness.fail("PAM_Main_Nanomax_ClosedLoop imports without touching hardware", "timed out")

    # ----------------------------------------------------- 3/4 in-process imports
    harness.section("3. Prior step size and unit resolution")
    try:
        prior_module = importlib.import_module("PAM_Main_Prior")
        adapter_module = importlib.import_module("Alazar_imaging.PriorStageAdapter")
        PriorStageAdapter = adapter_module.PriorStageAdapter
    except Exception as exc:
        harness.fail("import PAM_Main_Prior and PriorStageAdapter in process", repr(exc))
        return 1

    resolve_prior_units = prior_module.resolve_prior_units
    check_step_grid = prior_module.check_step_grid
    resolve_ss_mode = prior_module.resolve_ss_mode

    # -- environment -> mode selection --------------------------------------
    for raw, ss_value, alias, expected in (
        ("", None, False, "high"),
        ("", 8.0, False, "value"),
        ("", None, True, "micron"),
        ("", 8.0, True, "value"),
        ("high", None, False, "high"),
        ("micron", 8.0, False, "micron"),
        ("legacy", None, False, "legacy"),
        ("value", 8.0, False, "value"),
        ("none", 8.0, False, "none"),
        ("  HIGH  ", None, False, "high"),
    ):
        try:
            got = resolve_ss_mode(raw, ss_value, alias)
        except ValueError as exc:
            got = "ValueError:" + str(exc)
        harness.expect(
            "ss mode from env (%r, ss=%s, alias=%s) -> %s" % (raw, ss_value, alias, expected),
            got == expected,
            "got %s" % got,
        )

    for raw, ss_value, alias in (("turbo", None, False), ("value", None, False)):
        try:
            got = resolve_ss_mode(raw, ss_value, alias)
            harness.fail(
                "ss mode %r is rejected" % raw,
                "returned %s" % got,
            )
        except ValueError:
            harness.ok("ss mode %r is rejected" % raw)

    harness.expect(
        "the shipped default mode is high, the stage's finest unit",
        resolve_ss_mode("", None, False) == "high",
    )

    def units_case(name, fake, ss_mode, ss_value=None, legacy_value=None,
                   explicit_um_per_unit=None, assume_one_unit_is_one_micron=False):
        """Call resolve_prior_units the way main() does, through a real adapter."""
        adapter = PriorStageAdapter(
            fake,
            um_per_unit=1.0,
            min_step_um=0.01,
            settle_default_ms=0,
            log_callback=None,
            label="Fake Prior",
        )
        try:
            um_per_unit, details = resolve_prior_units(
                adapter,
                ss_mode,
                ss_value,
                legacy_value,
                explicit_um_per_unit,
                assume_one_unit_is_one_micron,
                log_callback=None,
            )
        except SystemExit as exc:
            return {"exit": str(exc), "adapter": adapter, "fake": fake}
        except Exception as exc:
            harness.fail(name, "raised " + repr(exc))
            return None
        return {
            "um_per_unit": um_per_unit,
            "details": details,
            "adapter": adapter,
            "fake": fake,
        }

    def ss_set_commands(fake):
        return [c for c in fake.commands if c.startswith("controller.stage.ss.set")]

    # -- high: the stage's real high-precision mode ---------------------------
    case = units_case(
        "mode=high sets ss=1 for the finest unit",
        FakePriorStage(steps_per_micron=100.0, ss=100.0),
        ss_mode="high",
    )
    if case:
        harness.expect(
            "mode=high sets ss=1 for the finest unit",
            ss_set_commands(case["fake"]) == ["controller.stage.ss.set 1"],
            ",".join(case["fake"].commands),
        )
        harness.expect(
            "mode=high gives one unit = 1/steps_per_micron um",
            abs(case["um_per_unit"] - 0.01) < 1e-12,
            "um_per_unit=%g" % case["um_per_unit"],
        )
        harness.expect(
            "mode=high resolves a 10 nm smallest step on a 100 steps/micron stage",
            abs(case["adapter"].resolution_step_um() - 0.01) < 1e-12,
            "%g um" % case["adapter"].resolution_step_um(),
        )

    case = units_case(
        "mode=high on a 2000 steps/micron stage gives 0.0005 um/unit",
        FakePriorStage(steps_per_micron=2000.0, ss=2000.0),
        ss_mode="high",
    )
    if case:
        harness.expect(
            "mode=high on a 2000 steps/micron stage gives 0.0005 um/unit",
            abs(case["um_per_unit"] - 0.0005) < 1e-15,
            "um_per_unit=%g" % case["um_per_unit"],
        )

    # -- the leftover-ss hazard ---------------------------------------------
    case = units_case(
        "a leftover ss from another script cannot leak through",
        FakePriorStage(steps_per_micron=100.0, ss=2.0),
        ss_mode="high",
    )
    if case:
        harness.expect(
            "a leftover ss from another script cannot leak through",
            abs(case["um_per_unit"] - 0.01) < 1e-12 and case["fake"].ss == 1.0,
            "ss now %g, um_per_unit=%g" % (case["fake"].ss, case["um_per_unit"]),
        )
        harness.expect(
            "the leftover ss value is overwritten, not inherited",
            ss_set_commands(case["fake"]) == ["controller.stage.ss.set 1"],
            ",".join(case["fake"].commands),
        )

    # -- micron / legacy / value --------------------------------------------
    case = units_case(
        "mode=micron sets ss=steps_per_micron so one unit is 1 um",
        FakePriorStage(steps_per_micron=100.0, ss=2.0),
        ss_mode="micron",
    )
    if case:
        harness.expect(
            "mode=micron sets ss=steps_per_micron so one unit is 1 um",
            abs(case["um_per_unit"] - 1.0) < 1e-12 and case["fake"].ss == 100.0,
            "ss now %g, um_per_unit=%g" % (case["fake"].ss, case["um_per_unit"]),
        )

    case = units_case(
        "mode=legacy reproduces the old ss=50 default",
        FakePriorStage(steps_per_micron=100.0, ss=1.0),
        ss_mode="legacy",
        legacy_value=50.0,
    )
    if case:
        harness.expect(
            "mode=legacy reproduces the old ss=50 default",
            case["fake"].ss == 50.0 and abs(case["um_per_unit"] - 0.5) < 1e-12,
            "ss now %g, um_per_unit=%g" % (case["fake"].ss, case["um_per_unit"]),
        )
        harness.expect(
            "legacy ss=50 on a 100 steps/micron stage is 0.5 um per unit, not 1.0",
            abs(case["um_per_unit"] - 0.5) < 1e-12,
            "the old program treated this as 1.0 um per unit",
        )

    case = units_case(
        "mode=value honours an explicit ss",
        FakePriorStage(steps_per_micron=100.0, ss=1.0),
        ss_mode="value",
        ss_value=8.0,
    )
    if case:
        harness.expect(
            "mode=value honours an explicit ss",
            case["fake"].ss == 8.0 and abs(case["um_per_unit"] - 0.08) < 1e-12,
            "ss now %g, um_per_unit=%g" % (case["fake"].ss, case["um_per_unit"]),
        )

    case = units_case(
        "mode=none leaves ss untouched and reports the current unit",
        FakePriorStage(steps_per_micron=100.0, ss=2.0),
        ss_mode="none",
    )
    if case:
        harness.expect(
            "mode=none leaves ss untouched and reports the current unit",
            ss_set_commands(case["fake"]) == [] and abs(case["um_per_unit"] - 0.02) < 1e-12,
            "ss=%g, um_per_unit=%g" % (case["fake"].ss, case["um_per_unit"]),
        )

    # -- the details contract ------------------------------------------------
    # main() reads these keys directly and logs them, so a missing key is a startup crash
    # rather than a cosmetic problem. Every mode has to return the same shape.
    details_keys = (
        "mode",
        "steps_per_micron",
        "ss_requested",
        "ss_sent",
        "ss_value",
        "um_per_unit",
        "ok",
        "reason",
        "source",
    )
    for mode, extra in (
        ("high", {}),
        ("micron", {}),
        ("legacy", {"legacy_value": 50.0}),
        ("value", {"ss_value": 8.0}),
        ("none", {}),
    ):
        case = units_case(
            "mode=%s returns the full unit-resolution contract" % mode,
            FakePriorStage(steps_per_micron=100.0, ss=2.0),
            ss_mode=mode,
            **extra
        )
        if not isinstance(case, dict) or "details" not in case:
            continue
        missing = [key for key in details_keys if key not in case["details"]]
        harness.expect(
            "mode=%s returns the full unit-resolution contract" % mode,
            not missing,
            "missing: " + ", ".join(missing) if missing else "%d keys" % len(details_keys),
        )
        harness.expect(
            "mode=%s reports the ss actually in force" % mode,
            case["details"].get("ss_value") is not None,
            "ss_value=%s" % case["details"].get("ss_value"),
        )

    # The escape hatch bypasses apply_ss_mode, so it has to build the same contract itself.
    case = units_case(
        "the PAM_PRIOR_UM_PER_UNIT escape hatch returns the same contract",
        FakePriorStage(steps_per_micron=100.0, ss=2.0),
        ss_mode="high",
        explicit_um_per_unit=0.02,
    )
    if isinstance(case, dict) and "details" in case:
        missing = [key for key in details_keys if key not in case["details"]]
        harness.expect(
            "the PAM_PRIOR_UM_PER_UNIT escape hatch returns the same contract",
            not missing,
            "missing: " + ", ".join(missing) if missing else "%d keys" % len(details_keys),
        )
        harness.expect(
            "the escape hatch records the ss the controller was actually in",
            case["details"].get("ss_value") == 2.0,
            "ss_value=%s" % case["details"].get("ss_value"),
        )

    # -- refusal when the unit cannot be derived ----------------------------
    for mode in ("high", "micron", "legacy", "value", "none"):
        fake = FakePriorStage(steps_per_micron=None, ss=2.0)
        case = units_case(
            "mode=%s refuses when steps-per-micron is unreadable" % mode,
            fake,
            ss_mode=mode,
            ss_value=8.0,
            legacy_value=50.0,
        )
        if isinstance(case, dict) and "exit" in case:
            harness.ok(
                "mode=%s refuses when steps-per-micron is unreadable" % mode,
                "SystemExit",
            )
        elif case is None:
            pass  # already recorded as a failure
        else:
            harness.fail(
                "mode=%s refuses when steps-per-micron is unreadable" % mode,
                "returned um_per_unit=%s instead of stopping" % case["um_per_unit"],
            )

    case = units_case(
        "an unreadable unit is never silently assumed to be one micron",
        FakePriorStage(steps_per_micron=None, ss=2.0),
        ss_mode="high",
    )
    harness.expect(
        "an unreadable unit is never silently assumed to be one micron",
        isinstance(case, dict) and "exit" in case,
        "refused" if isinstance(case, dict) and "exit" in case else "guessed",
    )

    case = units_case(
        "PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1 is the explicit opt-in",
        FakePriorStage(steps_per_micron=None, ss=2.0),
        ss_mode="high",
        assume_one_unit_is_one_micron=True,
    )
    if case and "um_per_unit" in case:
        harness.expect(
            "PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1 is the explicit opt-in",
            abs(case["um_per_unit"] - 1.0) < 1e-12
            and case["details"]["source"] == "assumed_one_unit_is_one_micron_by_request",
            case["details"]["source"],
        )
    else:
        harness.fail(
            "PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1 is the explicit opt-in",
            "did not honour the opt-in",
        )

    case = units_case(
        "PAM_PRIOR_UM_PER_UNIT bypasses the controller entirely",
        FakePriorStage(steps_per_micron=None, ss=2.0),
        ss_mode="high",
        explicit_um_per_unit=0.5,
    )
    if case and "um_per_unit" in case:
        harness.expect(
            "PAM_PRIOR_UM_PER_UNIT bypasses the controller entirely",
            abs(case["um_per_unit"] - 0.5) < 1e-12
            and ss_set_commands(case["fake"]) == [],
            "um_per_unit=%g, ss commands=%d" % (case["um_per_unit"], len(ss_set_commands(case["fake"]))),
        )
    else:
        harness.fail(
            "PAM_PRIOR_UM_PER_UNIT bypasses the controller entirely",
            "did not honour the override",
        )

    case = units_case(
        "an unknown ss mode is rejected",
        FakePriorStage(steps_per_micron=100.0),
        ss_mode="turbo",
    )
    harness.expect(
        "an unknown ss mode is rejected",
        isinstance(case, dict) and "exit" in case,
        "refused" if isinstance(case, dict) and "exit" in case else "accepted",
    )

    case = units_case(
        "mode=value without a value is rejected",
        FakePriorStage(steps_per_micron=100.0),
        ss_mode="value",
        ss_value=None,
    )
    harness.expect(
        "mode=value without a value is rejected",
        isinstance(case, dict) and "exit" in case,
        "refused" if isinstance(case, dict) and "exit" in case else "accepted",
    )

    case = units_case(
        "the resolved unit matches the controller's own implied unit",
        FakePriorStage(steps_per_micron=100.0, ss=7.0),
        ss_mode="high",
    )
    if case and "um_per_unit" in case:
        identity = case["adapter"].probe_identity()
        harness.expect(
            "the resolved unit matches the controller's own implied unit",
            identity.get("implied_um_per_unit") is not None
            and abs(identity["implied_um_per_unit"] - case["um_per_unit"]) < 1e-12,
            "implied=%s resolved=%s" % (identity.get("implied_um_per_unit"), case["um_per_unit"]),
        )

    harness.section("4. step must be a whole number of stage units")
    for step_um, min_step_um, expected_ok in (
        (1.0, 1.0, True),
        (39.0, 1.0, True),
        (0.5, 0.025, True),
        (1.0, 0.025, True),
        (0.75, 1.0, False),
        (1.03, 0.025, False),
        (0.02, 0.025, False),
    ):
        ok, message, step_in_units = check_step_grid(step_um, min_step_um)
        harness.expect(
            "check_step_grid(step=%g, unit=%g) -> %s" % (step_um, min_step_um, "accept" if expected_ok else "reject"),
            ok is expected_ok,
            "units=%g" % step_in_units if ok is expected_ok else "message=%s" % safe(message.splitlines()[0]),
        )

    default_ok = all(
        check_step_grid(1.0, unit)[0] for unit in (1.0, 0.5, 0.25, 0.1, 0.05, 0.025, 0.02, 0.01)
    )
    harness.expect("shipped default STEP_UM=1.0 is legal for every sensible unit size", default_ok)

    # ---------------------------------------------------------------- 5 adapter
    harness.section("5. PriorStageAdapter against a fake stage")

    events = []
    fake = FakePriorStage(x_units=100.0, y_units=20.0, steps_per_micron=50.0)
    adapter = PriorStageAdapter(
        fake,
        um_per_unit=1.0,
        travel_um=100.0,
        min_step_um=0.01,
        settle_default_ms=0,
        default_timeout_s=1.0,
        poll_interval_s=0.002,
        log_callback=lambda event, **fields: events.append(event),
        label="Fake Prior",
    )

    harness.expect(
        "readback is scaled by um_per_unit",
        adapter.get_position_values() == [100.0, 20.0, 0.0],
        "values=%s" % adapter.get_position_values(),
    )

    harness.expect(
        "read_steps_per_micron returns the controller's hardware constant",
        adapter.read_steps_per_micron() == 50.0,
        "%s" % adapter.read_steps_per_micron(),
    )
    harness.expect(
        "read_steps_per_micron returns None instead of raising",
        PriorStageAdapter(FakePriorStage(steps_per_micron=None), log_callback=None).read_steps_per_micron() is None,
    )
    fake_ss = FakePriorStage(steps_per_micron=100.0, ss=2.0)
    ss_adapter = PriorStageAdapter(fake_ss, log_callback=None)
    sent = ss_adapter.set_step_size(4)
    harness.expect(
        "set_step_size sends the ss command",
        sent == 4 and fake_ss.ss == 4.0 and "controller.stage.ss.set 4" in fake_ss.commands,
        ",".join(fake_ss.commands),
    )
    try:
        PriorStageAdapter(FakePriorStage(), log_callback=None).set_step_size(0)
        harness.fail("set_step_size rejects ss below 1", "no exception")
    except ValueError:
        harness.ok("set_step_size rejects ss below 1")
    try:
        PriorStageAdapter(FakePriorStage(), log_callback=None).apply_ss_mode("turbo")
        harness.fail("apply_ss_mode rejects an unknown mode", "no exception")
    except ValueError:
        harness.ok("apply_ss_mode rejects an unknown mode")
    unit, detail = PriorStageAdapter(FakePriorStage(), log_callback=None).apply_ss_mode("value", ss_value=0)
    harness.expect(
        "apply_ss_mode reports failure instead of guessing",
        unit is None and detail["ok"] is False and detail["reason"] == "ss_target_below_one",
        "reason=%s" % detail["reason"],
    )
    unit, detail = PriorStageAdapter(
        FakePriorStage(steps_per_micron=100.0), log_callback=None
    ).apply_ss_mode("high")
    harness.expect(
        "apply_ss_mode records the mode it used",
        detail["mode"] == "high" and detail["ss_sent"] == 1 and abs(unit - 0.01) < 1e-12,
        "mode=%s ss=%s um_per_unit=%g" % (detail["mode"], detail["ss_sent"], unit),
    )

    harness.expect("resolution_step_um with 1 um/unit is 1.0", abs(adapter.resolution_step_um() - 1.0) < 1e-12)

    adapter_small = PriorStageAdapter(
        FakePriorStage(), um_per_unit=0.025, min_step_um=0.01, settle_default_ms=0, log_callback=None
    )
    harness.expect(
        "resolution_step_um follows a 0.025 um/unit controller",
        abs(adapter_small.resolution_step_um() - 0.025) < 1e-12,
        "%g" % adapter_small.resolution_step_um(),
    )

    fake.commands = []
    adapter.move_xyz(x=10.4, y=20.6, wait=False)
    harness.expect(
        "targets are rounded to whole stage units",
        "set_position:10,21" in fake.commands,
        ",".join(fake.commands),
    )

    fake2 = FakePriorStage()
    adapter_half = PriorStageAdapter(
        fake2, um_per_unit=0.5, min_step_um=0.01, settle_default_ms=0, log_callback=None
    )
    adapter_half.set_position([10.0, 20.0])
    harness.expect(
        "a 0.5 um/unit stage gets double the unit count",
        "set_position:20,40" in fake2.commands,
        ",".join(fake2.commands),
    )

    fake3 = FakePriorStage(x_units=150.0, y_units=20.0)
    widened_events = []
    adapter_wide = PriorStageAdapter(
        fake3,
        um_per_unit=1.0,
        travel_um=100.0,
        settle_default_ms=0,
        log_callback=lambda event, **fields: widened_events.append(event),
    )
    adapter_wide.refresh_limits()
    harness.expect(
        "a stage sitting beyond the window widens the window instead of moving backwards",
        abs(adapter_wide.get_max_travel("x") - 150.0) < 1e-9,
        "x limit=%g" % adapter_wide.get_max_travel("x"),
    )
    harness.expect(
        "the untouched axis keeps the configured travel",
        abs(adapter_wide.get_max_travel("y") - 100.0) < 1e-9,
        "y limit=%g" % adapter_wide.get_max_travel("y"),
    )
    harness.expect(
        "the widened window is logged",
        "PRIOR_TRAVEL_WIDENED" in widened_events,
        ",".join(widened_events) if widened_events else "no events",
    )

    # -- the signed working window ------------------------------------------
    # The Prior controller reports an absolute coordinate whose origin sits inside the
    # travel, so the real stage idles at a negative position. A [0, travel] window would
    # reject it, and the panel clamp would drag it 14 mm back to 0.
    signed = PriorStageAdapter(
        FakePriorStage(x_units=-14067.0, y_units=-1387.0),
        um_per_unit=1.0,
        travel_um=20000.0,
        settle_default_ms=0,
        log_callback=None,
    )
    signed.refresh_limits()
    harness.expect(
        "the default window is symmetric about the controller origin",
        abs(signed.get_min_travel("x") + 20000.0) < 1e-9
        and abs(signed.get_max_travel("x") - 20000.0) < 1e-9,
        "window=[%g, %g]" % (signed.get_min_travel("x"), signed.get_max_travel("x")),
    )
    harness.expect(
        "a stage parked at a negative coordinate is inside the window",
        signed.get_min_travel("x") <= -14067.0 <= signed.get_max_travel("x"),
        "x=%g window=[%g, %g]"
        % (-14067.0, signed.get_min_travel("x"), signed.get_max_travel("x")),
    )

    below = PriorStageAdapter(
        FakePriorStage(x_units=-500.0, y_units=0.0),
        um_per_unit=1.0,
        travel_um=100.0,
        travel_min_um=-100.0,
        settle_default_ms=0,
        log_callback=None,
    )
    below.refresh_limits()
    harness.expect(
        "a stage below the window widens the lower bound instead of being pulled up to it",
        abs(below.get_min_travel("x") + 500.0) < 1e-9 and abs(below.get_max_travel("x") - 100.0) < 1e-9,
        "window=[%g, %g]" % (below.get_min_travel("x"), below.get_max_travel("x")),
    )

    pinned = PriorStageAdapter(
        FakePriorStage(x_units=0.0, y_units=0.0),
        um_per_unit=1.0,
        travel_um=30000.0,
        travel_min_um=-25000.0,
        settle_default_ms=0,
        log_callback=None,
    )
    pinned.refresh_limits()
    harness.expect(
        "both ends of the window can be pinned explicitly",
        abs(pinned.get_min_travel("x") + 25000.0) < 1e-9
        and abs(pinned.get_max_travel("x") - 30000.0) < 1e-9,
        "window=[%g, %g]" % (pinned.get_min_travel("x"), pinned.get_max_travel("x")),
    )
    try:
        PriorStageAdapter(
            FakePriorStage(), um_per_unit=1.0, travel_um=100.0, travel_min_um=200.0,
            settle_default_ms=0, log_callback=None,
        )
        harness.fail("an inverted working window is rejected", "no exception")
    except ValueError:
        harness.ok("an inverted working window is rejected")

    # A stage with a zero datum (BPC303) exposes no lower bound, so it must keep [0, travel]
    # and the historical wording -- the Prior fix must not leak into the NanoMax program.
    class ZeroDatumStage:
        def get_max_travel(self, axis):
            return 20.0

    try:
        scan_utils.validate_sample_trajectory(
            ZeroDatumStage(), [(-1.0, 0.0)], limit_label="BPC303/MAX311D"
        )
        harness.fail("a zero-datum stage still rejects a negative target", "no exception")
    except ValueError as exc:
        harness.expect(
            "a zero-datum stage still rejects a negative target",
            "[0,20.0000]" in str(exc),
            safe(str(exc))[:80],
        )
    try:
        scan_utils.validate_sample_trajectory(ZeroDatumStage(), [(5.0, 5.0)])
        harness.ok("a zero-datum stage still accepts a positive target")
    except ValueError as exc:
        harness.fail("a zero-datum stage still accepts a positive target", safe(exc))

    fake4 = FakePriorStage()
    adapter_noz = PriorStageAdapter(
        fake4, um_per_unit=1.0, z_enable=False, settle_default_ms=0, log_callback=None
    )
    fake4.commands = []
    adapter_noz.move_xyz(x=5.0, y=6.0, z=1.0, wait=False)
    harness.expect(
        "a Z move on a Z-disabled stage is refused, not sent",
        not any("z.goto" in command for command in fake4.commands),
        ",".join(fake4.commands),
    )
    harness.expect("XY in the same call still moves", "set_position:5,6" in fake4.commands, ",".join(fake4.commands))

    fake5 = FakePriorStage()
    events5 = []
    adapter_zero = PriorStageAdapter(
        fake5,
        um_per_unit=1.0,
        settle_default_ms=0,
        log_callback=lambda event, **fields: events5.append(event),
    )
    returned = adapter_zero.set_zero_axes(["x", "y"])
    harness.expect("set_zero_axes reports it did nothing", returned is False)
    harness.expect("set_zero_axes logs the no-op", "PRIOR_ZERO_AXES_IGNORED" in events5, ",".join(events5))

    fake6 = FakePriorStage(x_units=0.0, y_units=0.0)
    adapter_settle = PriorStageAdapter(
        fake6,
        um_per_unit=1.0,
        min_step_um=0.01,
        settle_default_ms=0,
        default_timeout_s=1.0,
        poll_interval_s=0.002,
        log_callback=None,
    )
    try:
        adapter_settle.move_xyz(x=10.0, y=10.0, wait=True, settle_time_ms=0, tolerance=0.5, timeout_s=1.0)
        harness.ok("a reachable target settles without raising")
        harness.expect(
            "the stage actually received the move",
            abs(fake6.x_units - 10.0) < 1e-9 and abs(fake6.y_units - 10.0) < 1e-9,
            "units=%.1f,%.1f" % (fake6.x_units, fake6.y_units),
        )
    except Exception as exc:
        harness.fail("a reachable target settles without raising", repr(exc))

    fake7 = FakePriorStage(x_units=0.0, y_units=0.0, obey_moves=False)
    adapter_stuck = PriorStageAdapter(
        fake7,
        um_per_unit=1.0,
        min_step_um=0.01,
        settle_default_ms=0,
        default_timeout_s=0.05,
        poll_interval_s=0.002,
        log_callback=None,
    )
    started = time.monotonic()
    try:
        adapter_stuck.move_xyz(x=10.0, y=10.0, wait=True, settle_time_ms=0, tolerance=0.5, timeout_s=0.05)
        harness.fail("a stage that never arrives raises TimeoutError", "no exception")
    except TimeoutError as exc:
        harness.expect(
            "a stage that never arrives raises TimeoutError",
            True,
            "after %.2fs" % (time.monotonic() - started),
        )
    except Exception as exc:
        harness.fail("a stage that never arrives raises TimeoutError", "wrong type: " + repr(exc))

    identity = adapter.probe_identity()
    harness.expect(
        "probe_identity reports the controller unit metadata",
        identity.get("steps_per_micron") == 50.0 and abs((identity.get("implied_um_per_unit") or 0) - 0.02) < 1e-12,
        "steps=%s implied=%s" % (identity.get("steps_per_micron"), identity.get("implied_um_per_unit")),
    )
    harness.expect("describe() returns a readable line", "Fake Prior" in adapter.describe(), adapter.describe())

    adapter.close()
    harness.expect("close() deinitialises the stage", fake.deinitialised)
    adapter.emergency_stop()
    harness.expect("emergency_stop() reaches the stage", fake.stopped)

    # -------------------------------------------------------------- 6 no-laser
    harness.section("6. NoLaserManager")
    try:
        no_laser_module = importlib.import_module("Nanomax.no_laser_manager")
        NoLaserManager = no_laser_module.NoLaserManager
    except Exception as exc:
        harness.fail("import NoLaserManager", repr(exc))
        return 1

    logged = []
    manager = NoLaserManager(
        log_callback=lambda event, **fields: logged.append(event),
        reason="no laser in this program",
    )
    status = manager.refresh_status()
    harness.expect("refresh_status reports removal", isinstance(status, dict) and status.get("status") == "removed", str(status))
    harness.expect("prealign panel gets no laser rows", manager.panel_items(acquisition=False) == [])
    acquisition_items = manager.panel_items(acquisition=True)
    harness.expect(
        "dashboard gets an explicit not-used section",
        len(acquisition_items) > 0 and any(row[0] == "LASER_CONTROL" for row in acquisition_items),
        "%d rows" % len(acquisition_items),
    )
    harness.expect("prealign laser commands fall through to the panel", manager.execute_prealign_command(["532", "on"]) is None)
    message = manager.execute_acquisition_command(["532", "off"])
    harness.expect(
        "acquisition laser commands are answered with a message",
        isinstance(message, str) and "unavailable" in message.lower(),
        safe(message)[:60],
    )
    harness.expect("the ignored command is logged", "NO_LASER_COMMAND_IGNORED" in logged, ",".join(logged))

    # -------------------------------------------------------------- 7 geometry
    harness.section("7. scan geometry and travel validation")

    shape = scan_utils.scan_shape_from_range(39.0, 39.0, 1.0, max_range_um=None)
    harness.expect("the shipped Prior defaults give a 40x40 scan", shape == (40, 40), "shape=%s" % (shape,))

    shape_25 = scan_utils.scan_shape_from_range(20.0, 20.0, 0.5, max_range_um=None)
    harness.expect("20 um at 0.5 um step gives 41x41 points", shape_25 == (41, 41), "shape=%s" % (shape_25,))

    serpentine, label = scan_utils.resolve_scan_pattern("serpentine")
    harness.expect("serpentine resolves to an S-shaped scan", serpentine is True, label)
    raster, label_r = scan_utils.resolve_scan_pattern("raster")
    harness.expect("raster resolves to a Z-shaped scan", raster is False, label_r)
    try:
        scan_utils.resolve_scan_pattern("spiral")
        harness.fail("an unknown scan pattern is rejected", "no exception")
    except ValueError:
        harness.ok("an unknown scan pattern is rejected")

    try:
        scan_utils.validate_scan_step(0.005)
        harness.fail("a step below the piezo floor is rejected", "no exception")
    except ValueError:
        harness.ok("a step below the piezo floor is rejected")

    trajectory = scan_utils.build_sample_trajectory(0.0, 0.0, 40, 40, 1.0, serpentine=True)
    harness.expect("the trajectory has one point per pixel", len(trajectory) == 1600, "%d points" % len(trajectory))
    harness.expect("the first point is the start", trajectory[0] == (0.0, 0.0), str(trajectory[0]))
    harness.expect("row 1 runs forwards", trajectory[39] == (39.0, 0.0), str(trajectory[39]))
    harness.expect("row 2 runs backwards (serpentine)", trajectory[40] == (39.0, 1.0), str(trajectory[40]))
    harness.expect("the last point closes the raster", trajectory[-1] == (0.0, 39.0), str(trajectory[-1]))

    flipped_x = scan_utils.build_sample_trajectory(
        39.0, 0.0, 40, 2, 1.0, x_direction=-1.0, y_direction=1.0, serpentine=False
    )
    harness.expect(
        "a negative X direction mirrors the scan instead of leaving it unchanged",
        flipped_x[0] == (39.0, 0.0) and flipped_x[39] == (0.0, 0.0),
        "first=%s last_of_row=%s" % (flipped_x[0], flipped_x[39]),
    )
    flipped_y = scan_utils.build_sample_trajectory(
        0.0, 39.0, 1, 40, 1.0, x_direction=1.0, y_direction=-1.0, serpentine=False
    )
    harness.expect(
        "a negative Y direction mirrors the slow axis",
        flipped_y[0] == (0.0, 39.0) and flipped_y[-1] == (0.0, 0.0),
        "first=%s last=%s" % (flipped_y[0], flipped_y[-1]),
    )

    adapter_geom = PriorStageAdapter(
        FakePriorStage(), um_per_unit=1.0, travel_um=100.0, settle_default_ms=0, log_callback=None
    )
    adapter_geom.refresh_limits()
    try:
        scan_utils.validate_sample_trajectory(adapter_geom, trajectory, limit_label="Prior ProScan working-window")
        harness.ok("an in-window trajectory is accepted")
    except ValueError as exc:
        harness.fail("an in-window trajectory is accepted", safe(exc))

    try:
        far = scan_utils.build_sample_trajectory(0.0, 0.0, 40, 40, 1.0, serpentine=True)
        far = [(x + 500.0, y + 500.0) for x, y in far]
        scan_utils.validate_sample_trajectory(adapter_geom, far, limit_label="Prior ProScan working-window")
        harness.fail("an out-of-window trajectory is rejected", "no exception")
    except ValueError as exc:
        harness.expect(
            "an out-of-window trajectory is rejected",
            "Prior ProScan working-window" in str(exc),
            safe(str(exc).splitlines()[0])[:70],
        )

    default_label_kept = True
    try:
        scan_utils.validate_sample_trajectory(adapter_geom, far)
    except ValueError as exc:
        default_label_kept = "BPC303/MAX311D" in str(exc)
    harness.expect("omitting the label keeps the historical BPC303/MAX311D wording", default_label_kept)

    # ----------------------------------------------------------- 8 panel parity
    harness.section("8. shared panel parity")
    try:
        terminal_panel = importlib.import_module("Nanomax.terminal_panel")
        acquisition_panel = importlib.import_module("Nanomax.acquisition_panel")
        prealign_panel = importlib.import_module("Nanomax.prealign_panel")
    except Exception as exc:
        harness.fail("import the shared panels", repr(exc))
        return 1

    harness.expect("an empty section renders no lines", terminal_panel.format_section_lines("Empty", []) == [])
    one_row = terminal_panel.format_section_lines("One", [("A", "1", "")])
    harness.expect(
        "a one-row section still renders its header",
        len(one_row) == 2 and one_row[0] == "[One]",
        " | ".join(safe(line) for line in one_row),
    )

    dashboard = acquisition_panel.AcquisitionDashboard(
        desc="acceptance",
        total=1600,
        laser_manager=manager,
        scan_items=[("SCAN_W", 40, "frozen")],
        daq_items=[("DELAY", 1600, "frozen")],
        runtime_items=[("PRIOR_COM", "COM4", "frozen")],
        log_callback=None,
    )
    recorder = LineRecorder()
    dashboard.renderer = recorder
    dashboard.render()
    text = recorder.text()
    harness.expect(
        "dashboard default command hint is unchanged",
        "press ':' for laser close-at-end commands" in text,
    )
    harness.expect(
        "dashboard default allowed list is unchanged",
        ":532 close-at-end on/off" in text,
    )
    harness.expect("dashboard default laser section title is unchanged", "[Lasers]" in text)

    prior_dashboard = acquisition_panel.AcquisitionDashboard(
        desc="acceptance prior",
        total=1600,
        laser_manager=manager,
        scan_items=[("SCAN_W", 40, "frozen")],
        daq_items=[("DELAY", 1600, "frozen")],
        runtime_items=[("PRIOR_COM", "COM4", "frozen")],
        laser_section_title="Laser Control",
        allowed_hint="none - this program has no laser control; use the stop key only",
        command_hint="no in-scan commands besides the stop key",
    )
    prior_recorder = LineRecorder()
    prior_dashboard.renderer = prior_recorder
    prior_dashboard.render()
    prior_text = prior_recorder.text()
    harness.expect("the Prior dashboard renames the laser section", "[Laser Control]" in prior_text)
    harness.expect(
        "the Prior dashboard says there is no laser control",
        "none - this program has no laser control" in prior_text,
    )
    harness.expect(
        "the Prior dashboard hides the 532 command hint",
        "press ':' for laser close-at-end commands" not in prior_text,
    )

    config = prealign_panel.SamplePrealignConfig(
        scan_range_x_um=39.0,
        scan_range_y_um=39.0,
        step_um=1.0,
        x_step_um=1.0,
        y_step_um=1.0,
        z_step_um=1.0,
        min_step_um=1.0,
        position_tolerance_um=1.0,
        position_timeout_s=5.0,
        auto_refresh_s=5.0,
        allow_probe_switch=False,
    )
    panel_stage = PriorStageAdapter(
        FakePriorStage(), um_per_unit=1.0, travel_um=100.0, settle_default_ms=0, log_callback=None
    )
    panel_stage.refresh_limits()

    # The panel clamp is the dangerous half of the signed window: with a [0, travel] clamp a
    # stage parked at -14067 um would be yanked 14 mm forward by a single jog keypress.
    negative_panel = prealign_panel.SamplePrealignPanel(
        PriorStageAdapter(
            FakePriorStage(x_units=-14067.0, y_units=-1387.0),
            um_per_unit=1.0,
            travel_um=20000.0,
            settle_default_ms=0,
            log_callback=None,
        ),
        config,
        display_params={},
    )
    clamped_value, was_clamped = negative_panel.clamp_axis("x", -14066.0)
    harness.expect(
        "a negative coordinate inside the window is not clamped",
        abs(clamped_value + 14066.0) < 1e-9 and not was_clamped,
        "clamped=%s to %g" % (was_clamped, clamped_value),
    )
    outside_value, outside_clamped = negative_panel.clamp_axis("x", -99999.0)
    harness.expect(
        "a target outside the signed window is still clamped",
        outside_clamped and abs(outside_value + 20000.0) < 1e-9,
        "clamped=%s to %g" % (outside_clamped, outside_value),
    )

    default_panel = prealign_panel.SamplePrealignPanel(panel_stage, config, display_params={})
    default_recorder = LineRecorder()
    default_panel.renderer = default_recorder
    default_panel.render()
    default_text = default_recorder.text()
    harness.expect(
        "prealign default title is unchanged",
        default_panel.panel_title == "PAM closed-loop sample prealignment",
        default_panel.panel_title,
    )
    harness.expect(
        "prealign default help text is unchanged",
        default_panel.help_text == prealign_panel.HELP_TEXT,
    )
    harness.expect(
        "prealign default status header is unchanged",
        default_panel.status_header
        == "Closed-loop MAX311D/BPC303 prealignment phase - same PAM_Main_Nanomax.py process",
        default_panel.status_header,
    )
    harness.expect("prealign default keeps the probe rows", "PROBE_CTRL" in default_text)

    prior_panel = prealign_panel.SamplePrealignPanel(
        panel_stage,
        config,
        display_params={
            "PANEL_TITLE": "PAM Prior ProScan prealignment - closed-loop XY in um",
            "HELP_TEXT": prior_module.PRIOR_HELP_TEXT,
            "SHOW_PROBE_ROWS": False,
        },
    )
    prior_panel_recorder = LineRecorder()
    prior_panel.renderer = prior_panel_recorder
    prior_panel.render()
    prior_panel_text = prior_panel_recorder.text()
    harness.expect(
        "the Prior panel takes the custom title",
        "PAM Prior ProScan prealignment" in prior_panel_text,
    )
    harness.expect("the Prior panel drops the probe rows", "PROBE_CTRL" not in prior_panel_text)
    harness.expect(
        "the Prior panel keeps the stage rows",
        "SAMPLE_CTRL" in prior_panel_text,
        "SAMPLE_CTRL present",
    )

    # The .mat stage identity: data_io.py keeps the NanoMax values as defaults, and a caller
    # that overrides them must get exactly the keys it asked for.
    data_io = importlib.import_module("Nanomax.data_io")
    harness.expect(
        "data_io still defaults to the historical NanoMax stage identity",
        dict(data_io.DEFAULT_STAGE_METADATA)
        == {
            "sample_stage": "MAX311D",
            "sample_controller": "BPC303",
            "probe_stage": "MAX312D",
            "probe_controller": "MDT693B",
        },
        str(data_io.DEFAULT_STAGE_METADATA),
    )
    default_mat = data_io.build_scan_mat_dict(
        [[[0] * 8, "1.0,2.0,0", {}]],
        1,
        1,
        1.0,
        1,
        8,
        False,
        "sample_closed_loop",
        "um",
        None,
        None,
        0.0,
        0.0,
        0.0,
    )
    default_meta = default_mat[0]["metadata"] if default_mat[0] else {}
    harness.expect(
        "an unmodified caller still gets the four NanoMax stage keys",
        all(
            default_meta.get(key) == value
            for key, value in data_io.DEFAULT_STAGE_METADATA.items()
        ),
        str({key: default_meta.get(key) for key in data_io.DEFAULT_STAGE_METADATA}),
    )
    overridden_mat = data_io.build_scan_mat_dict(
        [[[0] * 8, "1.0,2.0,0", {}]],
        1,
        1,
        1.0,
        1,
        8,
        False,
        "prior_closed_loop",
        "um",
        None,
        None,
        0.0,
        0.0,
        0.0,
        stage_metadata={"probe_stage": "none", "sample_unit_um_per_unit": 0.02},
    )
    overridden_meta = overridden_mat[0]["metadata"] if overridden_mat[0] else {}
    harness.expect(
        "stage_metadata overrides only the keys it names and adds the rest",
        overridden_meta.get("probe_stage") == "none"
        and overridden_meta.get("sample_unit_um_per_unit") == 0.02
        and overridden_meta.get("sample_stage") == "MAX311D"
        and overridden_meta.get("sample_controller") == "BPC303",
        "probe=%s unit=%s sample=%s"
        % (
            overridden_meta.get("probe_stage"),
            overridden_meta.get("sample_unit_um_per_unit"),
            overridden_meta.get("sample_stage"),
        ),
    )

    # ------------------------------------------------------------ 9 static sweep
    harness.section("9. removed hardware is unreachable")
    for name in ("PAM_Main_Prior.py", "PAM_Main_Nanomax_ClosedLoop.py"):
        path = os.path.join(REPO_ROOT, name)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        code_lines = []
        in_docstring = False
        for raw in source.splitlines():
            stripped = raw.strip()
            if stripped.count('"""') == 1:
                in_docstring = not in_docstring
                continue
            if in_docstring or stripped.startswith("#"):
                continue
            # The sibling-process guard must name the other entry points, one of which is
            # PAM_Main_LBTEK.py. That is a process name, not a use of the LBTEK stage.
            if stripped.startswith('"*PAM_Main_'):
                continue
            code_lines.append(raw)
        code_text = "\n".join(code_lines)
        for symbol in FORBIDDEN_IN_ENTRY_POINTS:
            harness.expect(
                "%s does not use %s" % (name, symbol),
                symbol not in code_text,
            )
        for symbol in DOCSTRING_ONLY_ALLOWED:
            if symbol in code_text:
                harness.fail(
                    "%s only mentions %s in documentation" % (name, symbol),
                    "found outside the docstring",
                )
            else:
                harness.ok("%s only mentions %s in documentation" % (name, symbol))

    # ------------------------------------------------------- 10 main() dry run
    harness.section("10. PAM_Main_Prior.main() end-to-end dry run (no hardware)")

    # A dry run must not leave anything behind in the checkout: no scan in data/, no line
    # in the real run log. Both are asserted after the run below.
    repo_run_log = os.path.join(REPO_ROOT, "run_logs", "PAM_Main_Prior_run.log")
    repo_data_dir = os.path.join(REPO_ROOT, "data")

    def repo_run_log_size():
        try:
            return os.path.getsize(repo_run_log)
        except OSError:
            return None

    log_size_before = repo_run_log_size()
    data_before = sorted(os.listdir(repo_data_dir)) if os.path.isdir(repo_data_dir) else None

    with tempfile.TemporaryDirectory() as dry_run_dir:
        completed = None
        try:
            completed = run_dry_run_child(
                dry_run_dir,
                {
                    # ss=1 / steps-per-micron=50 makes one SDK unit 0.02 um, so a 1 um step
                    # is exactly 50 units: a legal step on the unit grid.
                    "PAM_PRIOR_SS_MODE": "high",
                    "PAM_PRIOR_COM": "4",
                    "PAM_SCAN_RANGE_X_UM": "2",
                    "PAM_SCAN_RANGE_Y_UM": "2",
                    "PAM_STEP_UM": "1",
                    "PAM_PRIOR_PREALIGN_ENABLE": "0",
                    "PAM_RESULT_PREVIEW_ENABLE": "0",
                    "PAM_DATA_SAVE_AUTO_TIMEOUT_S": "1",
                },
            )
        except subprocess.TimeoutExpired:
            harness.fail("PAM_Main_Prior.main() completes without hardware", "timed out")

        if completed is not None:
            output = completed.stdout or ""
            stderr_lines = [line for line in (completed.stderr or "").splitlines() if line.strip()]
            harness.expect(
                "PAM_Main_Prior.main() completes without hardware",
                completed.returncode == 0,
                stderr_lines[-1] if completed.returncode and stderr_lines else "exit 0",
            )
            if completed.returncode != 0:
                # A bare exit code is not diagnosable; show where the child died.
                print("  --- child stderr (last 12 lines) ---")
                for line in stderr_lines[-12:]:
                    print("  " + safe(line))
                print("  --- child stdout (last 12 lines) ---")
                for line in (output or "").splitlines()[-12:]:
                    print("  " + safe(line))
            harness.expect(
                "main() runs all the way to the end of the function",
                "DRYRUN_MAIN_RETURNED" in output,
            )
            harness.expect(
                "a 2 um range at a 1 um step is a 3x3 scan",
                value_of(output, "DRYRUN_META_SCAN_SHAPE") == "3|3",
                value_of(output, "DRYRUN_META_SCAN_SHAPE"),
            )
            harness.expect(
                "the dry run saves exactly one .mat",
                value_of(output, "DRYRUN_MAT_COUNT") == "1",
                value_of(output, "DRYRUN_MAT_COUNT"),
            )
            harness.expect(
                "the saved .mat holds one waveform per acquired point",
                value_of(output, "DRYRUN_MAT_WAVEFORMS") == "9",
                value_of(output, "DRYRUN_MAT_WAVEFORMS"),
            )
            harness.expect(
                "every point was acquired and settled inside tolerance",
                value_of(output, "DRYRUN_META_SETTLE_OK") == "9"
                and value_of(output, "DRYRUN_META_TIMEOUTS") == "0",
                "settled=%s timeouts=%s"
                % (
                    value_of(output, "DRYRUN_META_SETTLE_OK"),
                    value_of(output, "DRYRUN_META_TIMEOUTS"),
                ),
            )
            harness.expect(
                "the .mat records the Prior scan target",
                value_of(output, "DRYRUN_META_SCAN_TARGET") == "prior_closed_loop",
                value_of(output, "DRYRUN_META_SCAN_TARGET"),
            )
            harness.expect(
                "the .mat names the Prior stage, not the NanoMax one",
                value_of(output, "DRYRUN_META_SAMPLE_STAGE") == "Prior ProScan"
                and value_of(output, "DRYRUN_META_SAMPLE_CONTROLLER") == "PriorUnifiedStage",
                "%s / %s"
                % (
                    value_of(output, "DRYRUN_META_SAMPLE_STAGE"),
                    value_of(output, "DRYRUN_META_SAMPLE_CONTROLLER"),
                ),
            )
            harness.expect(
                "the .mat does not claim the probe stage this program removed",
                value_of(output, "DRYRUN_META_PROBE_STAGE") == "none"
                and value_of(output, "DRYRUN_META_PROBE_CONTROLLER") == "none",
                "%s / %s"
                % (
                    value_of(output, "DRYRUN_META_PROBE_STAGE"),
                    value_of(output, "DRYRUN_META_PROBE_CONTROLLER"),
                ),
            )
            harness.expect(
                "the .mat records the microns-per-unit the scan was taken in",
                value_of(output, "DRYRUN_META_UM_PER_UNIT") == "0.02",
                value_of(output, "DRYRUN_META_UM_PER_UNIT"),
            )
            harness.expect(
                "the .mat keeps one recorded position per point",
                value_of(output, "DRYRUN_META_POS_LIST_COUNT") == "9",
                value_of(output, "DRYRUN_META_POS_LIST_COUNT"),
            )
            harness.expect(
                "the program writes the controller step size instead of inheriting it",
                value_of(output, "DRYRUN_SS_SET") == "controller.stage.ss.set 1",
                value_of(output, "DRYRUN_SS_SET"),
            )
            harness.expect(
                "the program commanded the stage and released the controller at the end",
                int_of(output, "DRYRUN_MOVES") >= 10
                and value_of(output, "DRYRUN_DEINIT") == "True",
                "moves=%s deinit=%s"
                % (value_of(output, "DRYRUN_MOVES"), value_of(output, "DRYRUN_DEINIT")),
            )

        # The run log the dry run wrote, and the chain of events inside it.
        dry_run_log = os.path.join(dry_run_dir, "run_logs", "PAM_Main_Prior_run.log")
        log_text = ""
        if os.path.exists(dry_run_log):
            with open(dry_run_log, "r", encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()

        harness.expect(
            "the dry run writes its own run log inside the scratch directory",
            bool(log_text),
            "%d bytes" % len(log_text) if log_text else "missing",
        )

        events = set()
        for line in log_text.splitlines():
            for part in line.split(" | "):
                if part.startswith("event="):
                    events.add(part[len("event="):].strip())
                    break

        expected_chain = (
            "PRIOR_STAGE_OPEN_BEGIN",
            "PRIOR_STAGE_READY",
            "MAT_STAGE_METADATA",
            "RUN_START",
            "START_POSITION",
            "SCAN_CONFIG",
            "TRAJECTORY_READY",
            "USER_START_CONFIRMED",
            "ACQUISITION_START",
            "ACQUISITION_POINT_DONE",
            "ACQUISITION_DONE",
            "RETURN_TO_START_BEGIN",
            "RETURN_TO_START_DONE",
            "RUN_END_NORMAL",
            "FINAL_CLEANUP_DONE",
        )
        missing_events = [event for event in expected_chain if event not in events]
        harness.expect(
            "the run log records the whole startup-to-cleanup chain",
            not missing_events,
            "missing: " + ", ".join(missing_events) if missing_events else "15 events",
        )
        harness.expect(
            "the dry run never enters the error or interrupt path",
            "RUN_END_ERROR" not in events
            and "RUN_END_ERROR_HANDLED" not in events
            and "RUN_END_INTERRUPTED" not in events,
            "no error, no interrupt",
        )
        harness.expect(
            "the run log records the unit the scan was taken in",
            "PRIOR_UNIT_RESOLUTION" in events,
        )

        # Nothing may leak into the checkout.
        harness.expect(
            "the dry run leaves the repository's real run log untouched",
            repo_run_log_size() == log_size_before,
            "was %s, now %s" % (log_size_before, repo_run_log_size()),
        )
        data_after = sorted(os.listdir(repo_data_dir)) if os.path.isdir(repo_data_dir) else None
        harness.expect(
            "the dry run writes no scan into the repository's data directory",
            data_after == data_before,
            "%d entries before, %s after"
            % (
                len(data_before) if data_before is not None else -1,
                "unchanged" if data_after == data_before else "changed",
            ),
        )

    # ------------------------------------------------------------------ summary
    harness.section("summary")
    print("checks passed: %d" % harness.passed)
    print("checks failed: %d" % harness.failed)
    if harness.failures:
        print("")
        print("failures:")
        for name, detail in harness.failures:
            print("  - " + name + (("   [" + detail + "]") if detail else ""))
        print("")
        print("RESULT: FAIL")
        return 1
    print("")
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("")
        print("interrupted")
        sys.exit(2)
    except Exception as exc:  # harness could not run
        import traceback

        print("")
        print("HARNESS ERROR: " + safe(repr(exc)))
        traceback.print_exc()
        sys.exit(2)
