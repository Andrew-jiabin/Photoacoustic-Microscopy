"""PAM imaging on the Prior ProScan stage, with the same terminal UI as the NanoMax program.

Derived from ``PAM_Main_SDK.py``. The acquisition maths, the DAQ configuration and the
``.mat`` data contract are unchanged; what changed is the control surface and the
hardware that is still supported.

What was removed
----------------
* **LBTEK / LBMover**: the whole ``LB_MOVER`` branch, its DLL path, its serial-port
  handling and ``lbtek_wait_settled`` are gone. This program drives the Prior stage only.
* **NI-DAQ**: not reachable from the parent script, so there was nothing to delete.
  (NI appears only in the unused ``NI_DAQ_based/`` tree.)

What was added
--------------
The parent script had no control interface at all: it printed a few lines, waited for
one ``Enter``, then ran to completion with a single ``progress_manager`` bar. This
program uses the same terminal UI as ``PAM_Main_Nanomax.py``:

* a **prealignment panel** -- arrow-key X/Y jogging, ``+``/``-`` Z jogging, live
  position readback, live scan-range/step editing and a start gate that refuses to
  launch a scan which would leave the stage travel;
* **background DAQ initialisation** so the board is ready while the operator aligns;
* an **acquisition dashboard** with progress bar, smoothed rate, ETA, current point,
  frozen parameter sections and a non-blocking stop key;
* **paused closed-loop Z jogging** from inside the dashboard (only when Z is enabled);
* **live result preview** of the current ``.mat`` cache;
* a **timed save prompt** with an optional filename suffix;
* a **persistent run log** under ``run_logs/PAM_Main_Prior_run.log``;
* **segmented return-to-start** and the same KeyboardInterrupt / exception cleanup.

The Prior stage is presented to those panels through
``Alazar_imaging/PriorStageAdapter.py``, which maps the BPC303 closed-loop interface
onto ``PriorUnifiedStage``. See that module for the unit-conversion rules.

Units -- read this before trusting a scan size
----------------------------------------------
``controller.stage.ss.set`` rescales the SDK unit: ``resolution_um = ss / steps_per_micron``,
where ``steps-per-micron`` is a hardware constant (microsteps per micron) and ``ss`` is how
many microsteps make up one SDK unit. So ``ss = 1`` is the finest the controller can
address, and ``ss = steps_per_micron`` is one unit per micron.

``ss`` is a **controller setting that persists across programs**, so a program which does
not set it inherits whatever the last script left behind -- scripts in this repo set it to
1, 2, 50 and 64. The parent script set ``ss`` to 50 and then treated the readback as
microns, which is only correct when ``steps-per-micron == 50``. Nothing in this repo
records the real value.

So this program **always writes ``ss`` explicitly** (except in mode ``none``) and derives
``um_per_unit`` from the controller's own ``steps-per-micron``, re-reading it after the
write. Choose the mode with ``PAM_PRIOR_SS_MODE``:

``high`` (default)
    ``ss = 1`` -- one SDK unit is one microstep, the finest the controller can address.
    This is what ``PriorUnifiedStage.upgrade_to_high_precision()`` does. Nothing is lost
    by using it: every distance in this program is expressed in microns, so the physical
    scan is the same, only the achievable step gets finer.

``micron``
    ``ss = steps_per_micron`` -- one SDK unit is exactly one micron. This is the
    controller's own default unit, so SDK numbers stay small and human-readable.

``legacy``
    ``ss = PAM_PRIOR_SS_LEGACY_VALUE`` (default 50) -- reproduces the parent script's
    hardcoded ``HIGH_PRECISION_VALUE``. Only correct when the controller's
    ``steps-per-micron`` actually equals that value; use it to reproduce an old scan.

``value``
    ``ss = PAM_PRIOR_SS_VALUE`` -- whatever the operator asks for.

``none``
    Leave ``ss`` alone and report the unit the controller is already in. Only safe if
    nothing else has changed ``ss`` since power-up.

Two escape hatches bypass the controller entirely:

* ``PAM_PRIOR_UM_PER_UNIT=<n>`` -- the operator states the answer outright.
* ``PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1`` -- fall back to the parent script's
  assumption when ``steps-per-micron`` cannot be read.

**If the unit cannot be established, this program refuses to start.** It does not guess,
because a wrong microns-per-unit produces a mis-scaled dataset that looks entirely normal.

⚠️ **Setting ``ss`` resets the controller's ``hostdirection``** (Prior SDK). Direction
cannot be verified without moving the stage, so it is exposed as ``PAM_PRIOR_X_DIRECTION``
/ ``PAM_PRIOR_Y_DIRECTION`` (default ``+1``), printed in the banner, and recorded in the
run log. Confirm it with a small jog on the prealignment panel before the first scan.

The smallest move the stage can make is one SDK unit, published as the adapter's
``resolution_um``. It becomes the panel's minimum scan step, so the UI can never be
asked for a step the stage cannot take. ``STEP_UM`` must also be a **whole number of
those units**; the program refuses to start otherwise, because every target is rounded
to the nearest unit and a fractional step would make the ``.mat`` pixel size disagree
with where the stage actually went.

Working window
--------------
The Prior controller reports an **absolute** coordinate whose origin sits somewhere inside
the mechanical travel, so a perfectly reachable position is often **negative** -- the stage
in this lab idles at about X = -14 mm. The working window is therefore **signed**:
``[PAM_PRIOR_TRAVEL_MIN_UM, PAM_PRIOR_TRAVEL_MAX_UM]``, defaulting to a symmetric
``[-PAM_PRIOR_TRAVEL_UM, +PAM_PRIOR_TRAVEL_UM]``. Both the panel clamp and the trajectory
guard use it, so a stage parked at a negative coordinate is neither rejected nor dragged
back to 0. If the stage reads outside the configured window at startup the window is
**widened to include it** (``PRIOR_TRAVEL_WIDENED``) rather than clamping the stage
backwards. Pin both ends once you know the real travel.

Environment variables
---------------------
``PAM_PRIOR_SDK_DLL``, ``PAM_PRIOR_COM``, ``PAM_PRIOR_TRAVEL_UM``,
``PAM_PRIOR_TRAVEL_MIN_UM``, ``PAM_PRIOR_TRAVEL_MAX_UM``, ``PAM_PRIOR_Z_ENABLE``,
``PAM_PRIOR_Z_TRAVEL_UM``, ``PAM_PRIOR_SS_MODE``, ``PAM_PRIOR_SS_VALUE``,
``PAM_PRIOR_SS_LEGACY_VALUE``, ``PAM_PRIOR_SS_AUTO`` (deprecated alias for mode=micron),
``PAM_PRIOR_UM_PER_UNIT``, ``PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON``,
``PAM_PRIOR_X_DIRECTION``, ``PAM_PRIOR_Y_DIRECTION``, ``PAM_PRIOR_PREALIGN_ENABLE``,
``PAM_PRIOR_RETURN_XY_TO_ZERO_AT_END``,
plus the shared ``PAM_SCAN_RANGE_X_UM`` / ``PAM_SCAN_RANGE_Y_UM`` / ``PAM_STEP_UM`` /
``PAM_PANEL_AUTO_REFRESH_S`` / ``PAM_DATA_SAVE_AUTO_TIMEOUT_S`` / ``PAM_RESULT_PREVIEW_*`` /
``PAM_ACQ_TIMEOUT_MS``.
"""

import gc
import os
import sys
import time
import traceback

import atsapi as ats

from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.PriorStageAdapter import PriorStageAdapter
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Nanomax import run_log as _run_log
from Nanomax.acquisition_panel import AcquisitionDashboard, PauseZMotionController
from Nanomax.daq_async import BackgroundDaqInit
from Nanomax.data_io import save_scan_data, save_scan_snapshot_data
from Nanomax.no_laser_manager import NoLaserManager
from Nanomax.prealign_panel import SamplePrealignConfig, run_sample_prealignment
from Nanomax.result_preview import PAMResultPreviewController
from Nanomax.runtime import find_other_pam_processes, return_to_start, safe_return_to_start
from Nanomax.scan_utils import (
    NANOMAX_MANUAL_MIN_STEP_UM,
    build_sample_trajectory,
    resolve_scan_pattern,
    scan_shape_from_range,
    validate_sample_trajectory,
)


for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


# Keep this program's history out of PAM_Main_Nanomax_run.log: the NanoMax program reads
# that file to decide whether its own X/Y zero datum can be trusted, and a Prior run
# says nothing about the NanoMax piezo datum.
_run_log.RUN_LOG_PATH = os.path.join(
    os.path.dirname(_run_log.RUN_LOG_PATH),
    "PAM_Main_Prior_run.log",
)
RUN_LOG_PATH = _run_log.RUN_LOG_PATH
append_run_log = _run_log.append_run_log
set_current_run_id = _run_log.set_current_run_id


# The .mat metadata records which controller produced the data. runtime.return_to_start
# only knows the NanoMax scan targets, and the Prior closed-loop XY geometry is the same
# raster as "sample_closed_loop", so the return-to-start helpers get that value.
MAT_SCAN_TARGET = "prior_closed_loop"
RETURN_TO_START_TARGET = "sample_closed_loop"

# Entry points that share the Alazar board. The startup check refuses to start while any
# of them is running, because two captures on one board will corrupt both datasets.
SIBLING_PAM_ENTRY_POINTS = (
    "*PAM_Main_Prior.py*",
    "*PAM_Main_Nanomax_ClosedLoop.py*",
    "*PAM_Main_Nanomax.py*",
    "*PAM_Main_Manual.py*",
    "*PAM_Main_LBTEK.py*",
)


def repo_root():
    return os.path.dirname(os.path.abspath(__file__))


def prior_dll_candidates():
    """Search order for PriorScientificSDK.dll, mirroring Tool_code/acceptance_nanomax_prior.py."""
    root = repo_root()
    parent = os.path.dirname(root)
    return [
        os.path.join(parent, "PAM", "PriorSDK 2.0.0", "x64", "PriorScientificSDK.dll"),
        os.path.join(parent, "Labview_development", "nanoscan", "python", "PriorSDK 2.0.0", "x64", "PriorScientificSDK.dll"),
        os.path.join(root, "PriorSDK 2.0.0", "x64", "PriorScientificSDK.dll"),
        os.path.join(parent, "PAM", "PriorSDK 2.0.0", "PriorScientificSDK.dll"),
    ]


def resolve_prior_dll():
    explicit = os.environ.get("PAM_PRIOR_SDK_DLL")
    if explicit:
        if not os.path.exists(explicit):
            raise SystemExit(f"PAM_PRIOR_SDK_DLL points at a missing file: {explicit}")
        return explicit
    searched = prior_dll_candidates()
    for candidate in searched:
        if os.path.exists(candidate):
            return candidate
    raise SystemExit(
        "PriorScientificSDK.dll was not found. Set PAM_PRIOR_SDK_DLL to its full path.\n"
        "Searched:\n  " + "\n  ".join(searched)
    )


def refresh_terminal_for_acquisition():
    """Clear old setup output immediately before showing the acquisition progress bar."""
    if not sys.stdout.isatty():
        return
    os.system("cls" if os.name == "nt" else "clear")


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def env_float(name, default):
    value = os.environ.get(name)
    return default if value is None else float(value)


def env_int(name, default):
    value = os.environ.get(name)
    return default if value is None else int(value)


def env_str(name, default):
    value = os.environ.get(name)
    return default if value is None else value


def env_optional_float(name):
    value = os.environ.get(name)
    if value is None or str(value).strip() == "":
        return None
    return float(value)


def resolve_prior_units(
    stage,
    ss_mode,
    ss_value,
    legacy_value,
    explicit_um_per_unit,
    assume_one_unit_is_one_micron,
    log_callback=None,
):
    """Decide microns per SDK unit, and make the controller agree with that decision.

    ``stage`` is a :class:`PriorStageAdapter` (it owns the SDK conversation and the
    ``steps-per-micron`` read).

    Returns ``(um_per_unit, details)``. ``um_per_unit`` is **None** when the unit could
    not be established; the caller must then stop. Guessing here is what makes a scan
    come out mis-scaled while looking completely normal.

    The controller's ``ss`` step size persists across programs, so this never trusts
    whatever a previous script left behind -- it always writes ``ss`` (except in the
    ``none`` mode) and re-reads the controller's own ``steps-per-micron`` afterwards.
    """
    def log(event, **fields):
        if log_callback is not None:
            try:
                log_callback(event, **fields)
            except Exception:
                pass

    # An operator who states the number wins outright: it needs no controller reply.
    if explicit_um_per_unit is not None:
        um_per_unit = float(explicit_um_per_unit)
        if um_per_unit <= 0:
            raise SystemExit(
                f"PAM_PRIOR_UM_PER_UNIT must be positive, got {explicit_um_per_unit!r}."
            )
        details = {
            "mode": "um_per_unit",
            "steps_per_micron": stage.read_steps_per_micron(),
            "ss_requested": None,
            "ss_sent": None,
            "ss_value": stage.read_ss_value(),
            "um_per_unit": um_per_unit,
            "ok": True,
            "reason": None,
            "source": "explicit_um_per_unit",
        }
        log("PRIOR_UNIT_RESOLUTION", **details)
        stage.um_per_unit = um_per_unit
        stage.resolution_um = um_per_unit
        return um_per_unit, details

    try:
        um_per_unit, mode_details = stage.apply_ss_mode(ss_mode, ss_value=ss_value, legacy_value=legacy_value)
    except ValueError as exc:
        raise SystemExit(f"PAM_PRIOR_SS_MODE error: {exc}") from None

    details = dict(mode_details)
    details["source"] = "ss_mode_" + str(mode_details.get("mode"))

    if um_per_unit is not None:
        log("PRIOR_UNIT_RESOLUTION", **details)
        return um_per_unit, details

    # The unit could not be derived from the controller. Refuse to guess unless the
    # operator has explicitly asked for the one-unit-is-one-micron assumption.
    if assume_one_unit_is_one_micron:
        um_per_unit = 1.0
        details.update(
            um_per_unit=um_per_unit,
            ok=True,
            source="assumed_one_unit_is_one_micron_by_request",
        )
        log("PRIOR_UNIT_RESOLUTION", **details)
        print(
            "WARNING: the controller did not report steps-per-micron, so one SDK unit is "
            "being assumed to be exactly one micron because "
            "PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1. This is the old PAM_Main_SDK.py "
            "assumption; if it is wrong the scan size on disk is wrong too."
        )
        stage.um_per_unit = um_per_unit
        stage.resolution_um = um_per_unit
        return um_per_unit, details

    log("PRIOR_UNIT_RESOLUTION_FAILED", **details)
    raise SystemExit(
        "Could not work out how many microns one Prior SDK unit is, so the scan size "
        "would be a guess. The controller did not report 'controller.stage."
        "steps-per-micron.get' (reason: "
        + str(details.get("reason"))
        + ").\n"
        "Fix the readback (check the COM port and that no other program holds the "
        "controller), or state the answer yourself with one of:\n"
        "  set PAM_PRIOR_UM_PER_UNIT=<microns per SDK unit>\n"
        "  set PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1   (only if ss == steps-per-micron)\n"
        "The old PAM_Main_SDK.py silently assumed the second one, which is only correct "
        "when steps-per-micron happens to equal the ss value it set."
    )


SS_MODES = ("high", "micron", "legacy", "value", "none")


def resolve_ss_mode(raw_mode, ss_value, ss_auto_alias=False):
    """Turn the environment into a controller step-size mode.

    Returns one of ``SS_MODES``. Raises ``ValueError`` for an unusable combination, so
    the choice can be tested without starting the program.

    ``PAM_PRIOR_SS_MODE`` wins when set. Otherwise the first cut of this program's
    variables are still honoured (``PAM_PRIOR_SS_VALUE`` -> ``value``,
    ``PAM_PRIOR_SS_AUTO`` -> ``micron``), and the default is ``high``: the finest unit
    the controller can address.
    """
    mode = str(raw_mode or "").strip().lower()
    if not mode:
        if ss_value is not None:
            mode = "value"
        elif ss_auto_alias:
            mode = "micron"
        else:
            mode = "high"
    if mode not in SS_MODES:
        raise ValueError(
            f"PAM_PRIOR_SS_MODE={mode!r} is not recognised. "
            "Use high, micron, legacy, value or none."
        )
    if mode == "value" and ss_value is None:
        raise ValueError(
            "PAM_PRIOR_SS_MODE=value needs PAM_PRIOR_SS_VALUE=<microsteps per unit>."
        )
    return mode


def check_step_grid(step_um, min_step_um, tolerance=1e-6):
    """Check that a scan step is a whole number of stage units.

    Returns ``(ok, message, step_in_units)``. ``message`` is empty when ``ok`` is True.

    The Prior controller only lands on exact multiples of one SDK unit, and the adapter
    rounds every target to the nearest unit. A step that is not a whole number of units
    would therefore move the stage on one grid while the ``.mat`` metadata recorded a
    different pixel size -- the saved image would be silently mis-scaled.
    """
    min_step_um = float(min_step_um)
    step_um = float(step_um)
    if min_step_um <= 0:
        return True, "", 0.0
    step_in_units = step_um / min_step_um
    nearest_units = max(1, int(round(step_in_units)))
    if abs(step_in_units - nearest_units) <= float(tolerance):
        return True, "", step_in_units

    if step_in_units > nearest_units:
        legal_low = nearest_units * min_step_um
        legal_high = (nearest_units + 1) * min_step_um
    else:
        legal_low = max(min_step_um, (nearest_units - 1) * min_step_um)
        legal_high = nearest_units * min_step_um
    message = (
        f"STEP_UM={step_um:g} um is not a whole number of Prior units. "
        f"One Prior unit is {min_step_um:g} um here, so the stage can only land on "
        f"multiples of it; the nearest usable steps are {legal_low:g} um or "
        f"{legal_high:g} um.\n"
        "Fix STEP_UM (PAM_STEP_UM) and SCAN_RANGE_X_UM/SCAN_RANGE_Y_UM "
        "(PAM_SCAN_RANGE_X_UM/PAM_SCAN_RANGE_Y_UM) so the travel is an exact multiple "
        "of the step, or set PAM_PRIOR_SS_AUTO=1 / PAM_PRIOR_SS_VALUE so one unit "
        "really is the micron size you expect."
    )
    return False, message, step_in_units


PRIOR_HELP_TEXT = """
Hotkeys:
  Up / Down       X += xstep / X -= xstep
  Left / Right    Y += ystep / Y -= ystep
  + / -           Z += zstep / Z -= zstep        (only when Z is enabled)
  s               refresh status
  h / ?           redraw help
  0 / r           move X/Y to 0 um (the controller's own origin, not a datum rebuild)
  :               command mode

Commands after ':' then Enter:
  start / run / pam / image / scan   close this panel and begin acquisition
  p1 / a                             mark the current X,Y as corner A of the scan rectangle
  p2 / b                             mark corner B; once both corners exist the rectangle is applied
  rect x1 y1 x2 y2                   set both corners numerically and apply the rectangle
  rect                               re-apply the rectangle from the marked corners
  rect clear                         drop the marked corners
  set SCAN_RANGE_X_UM <um>           scan travel along X for this run
  set SCAN_RANGE_Y_UM <um>           scan travel along Y for this run
  set STEP_UM <um>                   image pixel step for this run
  set xstep/ystep/zstep <um>         manual move steps
  set return_step <um>               longest leg used to travel back to the scan start corner
  set x/y/z/xy/xyz ...               set absolute position(s) in um
  set interval <sec>                 hotkey polling interval
  set refresh <sec>                  automatic screen redraw interval
  set SETTLE_MS <ms>                 physical settle wait after each move
  set tolerance <um>                 readback tolerance before reporting a move done
  set timeout <sec>                  move timeout
  set reissue <sec>                  resend the target while outside tolerance; 0 disables
  q / quit / cancel                  abort before acquisition

Two-point rectangle scan:
  Move the stage to one corner of the region you want -- arrow keys, 'set x/y', or the
  controller's own hand pad -- type p1, move to the opposite corner, type p2. The panel
  then snaps the span to a whole number of STEP_UM steps, sets the scan ranges, forces
  the S-shaped (serpentine) pattern, moves to the start corner and shows the shape.
  Type start to begin.
  If marking the corners leaves the stage at the far end, the travel back to the start
  corner is split into legs of at most RECT_RETURN_STEP_UM (default 1 um) so the stage
  never makes one long jump; each leg settles before the next one is issued.
  RECT_RETURN_STEP_UM is the panel's own setting; it is not RETURN_STEP_UM, which is the
  line step the program uses when it returns the stage at the very end of a scan.

This program has no laser and no open-loop probe control; those commands are not
available. Scan travel is limited to the configured Prior working window.

Before the first scan: jog X/Y with the arrow keys and confirm the stage really moves
the way the readout says. Setting the controller's step size (ss) resets its
hostdirection, and direction can only be checked by watching the hardware move. If it
runs the wrong way, exit and set PAM_PRIOR_X_DIRECTION / PAM_PRIOR_Y_DIRECTION to -1.
""".strip()


def main():
    prior_dll_path = resolve_prior_dll()
    prior_com_port = env_str("PAM_PRIOR_COM", "4")

    # ------------------------------------------------------- stage step size
    # The controller's `ss` (microsteps per SDK unit) is a *persistent controller
    # setting*: it survives disconnect/reconnect, so a previous script leaves it behind.
    # Scripts in this repo set ss to 1, 2, 50 and 64. A program that does not set ss
    # inherits whatever was there, so this program always writes it explicitly.
    try:
        SS_MODE = resolve_ss_mode(
            env_str("PAM_PRIOR_SS_MODE", ""),
            env_optional_float("PAM_PRIOR_SS_VALUE"),
            env_bool("PAM_PRIOR_SS_AUTO", False),
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    SS_VALUE = env_optional_float("PAM_PRIOR_SS_VALUE")
    SS_LEGACY_VALUE = env_float("PAM_PRIOR_SS_LEGACY_VALUE", 50.0)
    UM_PER_UNIT_OVERRIDE = env_optional_float("PAM_PRIOR_UM_PER_UNIT")
    ASSUME_ONE_UNIT_IS_ONE_MICRON = env_bool("PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON", False)

    PRIOR_TRAVEL_UM = env_float("PAM_PRIOR_TRAVEL_UM", 20000.0)
    # The Prior controller reports ABSOLUTE coordinates and its origin sits somewhere inside
    # the mechanical travel, so a perfectly reachable position is often NEGATIVE (this stage
    # sits at about -14 mm). A [0, travel] window would reject real positions -- and the panel
    # clamp would drag the stage back to 0. The window is therefore signed; by default it is
    # symmetric about the controller origin, and both ends can be pinned once the real travel
    # is known. PAM_PRIOR_TRAVEL_UM is kept as the shorthand for a symmetric window.
    PRIOR_TRAVEL_MIN_UM = env_optional_float("PAM_PRIOR_TRAVEL_MIN_UM")
    PRIOR_TRAVEL_MAX_UM = env_optional_float("PAM_PRIOR_TRAVEL_MAX_UM")
    if PRIOR_TRAVEL_MIN_UM is None:
        PRIOR_TRAVEL_MIN_UM = -abs(PRIOR_TRAVEL_UM)
    if PRIOR_TRAVEL_MAX_UM is not None:
        PRIOR_TRAVEL_UM = PRIOR_TRAVEL_MAX_UM
    PRIOR_Z_ENABLE = env_bool("PAM_PRIOR_Z_ENABLE", False)
    PRIOR_Z_TRAVEL_UM = env_float("PAM_PRIOR_Z_TRAVEL_UM", 0.0)

    # User scan geometry. Ranges are the requested travel from first to last point.
    # Example: 20 um range with 1 um step gives 21 points: 0, 1, ..., 20 um.
    # Defaults reproduce the original PAM_Main_SDK.py scan: MOVE_RATE=8 -> 40 points per
    # axis with STEP_UM=1 SDK unit, i.e. 39 units of travel. They are also a whole number
    # of units for every sensible unit resolution, so the default config starts cleanly.
    SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM = (
        env_float("PAM_SCAN_RANGE_X_UM", 39.0),
        env_float("PAM_SCAN_RANGE_Y_UM", 39.0),
        env_float("PAM_STEP_UM", 1.0),
    )
    # The old PAM_Main_SDK.py moved in the positive direction only. Setting `ss` resets
    # `hostdirection` per the Prior SDK, so after an ss change the real direction has to be
    # confirmed on the hardware. It cannot be checked without moving the stage, so it is
    # exposed here and logged: verify it with the arrow keys on the prealignment panel and
    # flip these if the stage runs the wrong way.
    SAMPLE_X_DIRECTION = env_float("PAM_PRIOR_X_DIRECTION", 1.0)
    SAMPLE_Y_DIRECTION = env_float("PAM_PRIOR_Y_DIRECTION", 1.0)
    PREALIGN_ENABLE = env_bool("PAM_PRIOR_PREALIGN_ENABLE", True)
    PREALIGN_X_STEP_UM, PREALIGN_Y_STEP_UM, PREALIGN_Z_STEP_UM = 1.0, 1.0, 1.0
    PREALIGN_INTERVAL_S = 0.25
    PANEL_AUTO_REFRESH_S = env_float("PAM_PANEL_AUTO_REFRESH_S", 5.0)
    # False returns to the prealignment-selected start. True would send the stage to the
    # controller's absolute 0,0, which on a Prior can be a very long travel.
    RETURN_XY_TO_ZERO_AT_END = env_bool("PAM_PRIOR_RETURN_XY_TO_ZERO_AT_END", False)
    ZERO_AXES_AT_END = False
    ZERO_AXES = ("x", "y")
    # SCAN_PATTERN:
    #   "serpentine" or "s": S-shaped scan; odd rows reverse X direction.
    #   "raster" or "z": Z-shaped one-way rows; each row starts from low X.
    SCAN_PATTERN, SETTLE_MS = "serpentine", 120
    POSITION_TOLERANCE_UM = env_float("PAM_PRIOR_POSITION_TOLERANCE_UM", 1.0)
    POSITION_TIMEOUT_S = env_float("PAM_PRIOR_POSITION_TIMEOUT_S", 60.0)
    POSITION_REISSUE_INTERVAL_S = env_float("PAM_PRIOR_POSITION_REISSUE_INTERVAL_S", 1.0)
    RETURN_STEP_UM = env_float("PAM_PRIOR_RETURN_STEP_UM", 10.0)
    RETURN_POSITION_TIMEOUT_S = env_float(
        "PAM_PRIOR_RETURN_POSITION_TIMEOUT_S",
        min(float(POSITION_TIMEOUT_S), 60.0),
    )
    DATA_SAVE_AUTO_TIMEOUT_S = env_float("PAM_DATA_SAVE_AUTO_TIMEOUT_S", 60.0)
    RESULT_PREVIEW_ENABLE = env_bool("PAM_RESULT_PREVIEW_ENABLE", True)
    RESULT_PREVIEW_OUTPUT_DIR = env_str("PAM_RESULT_PREVIEW_OUTPUT_DIR", r".\results\cache\pam_preview")
    RESULT_PREVIEW_SNAPSHOT_DIR = env_str("PAM_RESULT_PREVIEW_SNAPSHOT_DIR", r".\results\cache\pam_live_snapshots")
    RESULT_PREVIEW_TIMEOUT_S = env_float("PAM_RESULT_PREVIEW_TIMEOUT_S", 900.0)
    PROCESSING_SKILL_PATH = env_str("PAM_PROCESSING_SKILL_PATH", r"D:\Phd_training\skills\data-processing-skill")

    # DAQ parameters, unchanged from PAM_Main_SDK.py.
    DELAY, SAMPLES_REC, SAMPLE_RATE = 1600, 4096, ats.SAMPLE_RATE_4000MSPS
    AVERAGE_ENABLE, RECORDS_PER_POINT, BUFFER_COUNT = True, 256, 4
    # Host-side wait for one acquisition. Aligned with the build that actually ran on the
    # Prior rig: PAM_Main_SDK.py used timeout_ms=1000 on its Prior/closed-loop branch
    # (its 2500 sat in the removed LBTEK branch). This is a host-side wait only -- it is
    # not a board configuration value and does not change what is sampled or how.
    ACQ_TIMEOUT_MS = env_int("PAM_ACQ_TIMEOUT_MS", 1000)
    POINT_LOG_INTERVAL, USER_STOP_ENABLE, USER_STOP_KEY = 25, True, "q"

    # ---------------------------------------------------------------- run log
    CURRENT_RUN_ID = time.strftime("%Y%m%d_%H%M%S")
    set_current_run_id(CURRENT_RUN_ID)

    # No laser control in this program; the shared panels still need the object.
    laser_manager = NoLaserManager(
        log_callback=append_run_log,
        reason="532 nm and CW laser control are not part of the Prior program",
    )
    laser_manager.refresh_status()

    # ------------------------------------------------------------ stage setup
    print(f"Opening Prior stage: DLL={prior_dll_path}, COM={prior_com_port}")
    append_run_log(
        "PRIOR_STAGE_OPEN_BEGIN",
        dll=prior_dll_path,
        com_port=prior_com_port,
    )
    prior_stage = PriorUnifiedStage(prior_dll_path, prior_com_port)

    # The adapter is built before the unit is known: `resolve_prior_units` writes `ss`
    # through it and then reads back the controller's own steps-per-micron to derive
    # microns-per-unit. um_per_unit=1.0 here is provisional and is overwritten.
    stage = PriorStageAdapter(
        prior_stage,
        um_per_unit=1.0,
        travel_um=PRIOR_TRAVEL_UM,
        travel_min_um=PRIOR_TRAVEL_MIN_UM,
        z_enable=PRIOR_Z_ENABLE,
        z_travel_um=PRIOR_Z_TRAVEL_UM,
        min_step_um=NANOMAX_MANUAL_MIN_STEP_UM,
        settle_default_ms=SETTLE_MS,
        default_timeout_s=POSITION_TIMEOUT_S,
        default_reissue_s=POSITION_REISSUE_INTERVAL_S,
        log_callback=append_run_log,
        label="Prior ProScan",
    )

    um_per_unit, unit_details = resolve_prior_units(
        stage,
        SS_MODE,
        SS_VALUE,
        SS_LEGACY_VALUE,
        UM_PER_UNIT_OVERRIDE,
        ASSUME_ONE_UNIT_IS_ONE_MICRON,
        log_callback=append_run_log,
    )
    identity = stage.probe_identity()
    stage.refresh_limits()

    # The stage cannot move in less than one SDK unit, so that is the smallest legal step.
    # The tolerance is raised to at least one unit for the same reason: a target is rounded
    # to the nearest unit, so a tolerance finer than a unit could report a perfectly good
    # move as a timeout.
    MIN_STEP_UM = stage.resolution_step_um()
    POSITION_TOLERANCE_UM = max(float(POSITION_TOLERANCE_UM), MIN_STEP_UM)
    PREALIGN_X_STEP_UM = max(PREALIGN_X_STEP_UM, MIN_STEP_UM)
    PREALIGN_Y_STEP_UM = max(PREALIGN_Y_STEP_UM, MIN_STEP_UM)
    PREALIGN_Z_STEP_UM = max(PREALIGN_Z_STEP_UM, MIN_STEP_UM)

    # The controller only lands on exact multiples of one SDK unit, so STEP_UM has to be a
    # whole number of units. If it is not, every target would be rounded to the nearest unit
    # while the .mat metadata kept the requested value -- the pixel size on disk would then
    # disagree with where the stage actually went. Refuse instead of scanning a lie.
    step_grid_ok, step_grid_message, step_grid_units = check_step_grid(STEP_UM, MIN_STEP_UM)
    if not step_grid_ok:
        append_run_log(
            "RUN_END_ERROR",
            error="step_um_not_a_whole_number_of_stage_units",
            phase="unit_grid_validation",
            step_um=STEP_UM,
            min_step_um=MIN_STEP_UM,
            um_per_unit=um_per_unit,
            step_in_units=step_grid_units,
        )
        raise SystemExit(step_grid_message)

    print("=" * 78)
    print("PAM Prior ProScan acquisition")
    print("=" * 78)
    print(stage.describe())
    print(
        f"Step size: mode={unit_details.get('mode')}, source={unit_details['source']}, "
        f"ss_sent={unit_details.get('ss_sent')}, "
        f"steps_per_micron={unit_details['steps_per_micron']}, "
        f"controller_implied_um_per_unit={identity.get('implied_um_per_unit')}"
    )
    print(
        f"One SDK unit = {um_per_unit:g} um; smallest legal step = {MIN_STEP_UM:g} um; "
        f"STEP_UM={STEP_UM:g} um = {step_grid_units:g} unit(s)."
    )
    print(
        f"Scan direction: X={SAMPLE_X_DIRECTION:+g}, Y={SAMPLE_Y_DIRECTION:+g} "
        "(set PAM_PRIOR_X_DIRECTION / PAM_PRIOR_Y_DIRECTION to flip)."
    )
    print(
        "NOTE: setting ss resets the controller's hostdirection. Jog with the arrow keys "
        "on the prealignment panel and confirm the stage moves the way the readout says "
        "before you press ':start'."
    )
    if unit_details["source"] == "assumed_one_unit_is_one_micron_by_request":
        print(
            "WARNING: one SDK unit is being assumed to be exactly one micron because "
            "PAM_PRIOR_ASSUME_ONE_UNIT_IS_ONE_MICRON=1 was set. That is the old "
            "PAM_Main_SDK.py assumption; if it is wrong the saved scan size is wrong too."
        )
    if unit_details.get("mode") == "none":
        print(
            "NOTE: PAM_PRIOR_SS_MODE=none left the controller's ss untouched, so the unit "
            "above is whatever the previous program left behind. Use mode=high unless you "
            "are deliberately continuing another program's setting."
        )
    print("=" * 78)

    append_run_log(
        "PRIOR_STAGE_READY",
        um_per_unit=um_per_unit,
        unit_source=unit_details["source"],
        ss_mode=unit_details.get("mode"),
        ss_sent=unit_details.get("ss_sent"),
        steps_per_micron=unit_details["steps_per_micron"],
        ss_value=unit_details["ss_value"],
        min_step_um=MIN_STEP_UM,
        step_um=STEP_UM,
        step_in_units=step_grid_units,
        x_direction=SAMPLE_X_DIRECTION,
        y_direction=SAMPLE_Y_DIRECTION,
        travel_x_um=stage.get_max_travel("x"),
        travel_y_um=stage.get_max_travel("y"),
        z_enable=PRIOR_Z_ENABLE,
    )

    # What the .mat should say about the hardware that produced it. data_io.py defaults to
    # the NanoMax identity (MAX311D/BPC303 sample, MAX312D/MDT693B probe); this program has
    # neither, so it overrides all four and adds the unit provenance. Without this a Prior
    # scan would be saved claiming a BPC303 and a probe stage it never touched -- and the
    # microns-per-unit, which is the one number that makes the scan size checkable later,
    # would not be recorded anywhere in the file.
    STAGE_METADATA = {
        "sample_stage": "Prior ProScan",
        "sample_controller": "PriorUnifiedStage",
        "probe_stage": "none",
        "probe_controller": "none",
        "sample_unit_um_per_unit": float(um_per_unit),
        "sample_unit_source": unit_details["source"],
        "sample_ss_mode": unit_details.get("mode"),
        "sample_ss_sent": -1 if unit_details.get("ss_sent") is None else unit_details["ss_sent"],
        "sample_steps_per_micron": unit_details["steps_per_micron"],
        "sample_min_step_um": float(MIN_STEP_UM),
        "sample_com_port": f"COM{prior_com_port}",
        "sample_x_direction": float(SAMPLE_X_DIRECTION),
        "sample_y_direction": float(SAMPLE_Y_DIRECTION),
        "sample_travel_min_um": float(stage.get_min_travel("x")),
        "sample_travel_max_um": float(stage.get_max_travel("x")),
    }
    append_run_log("MAT_STAGE_METADATA", **STAGE_METADATA)

    try:
        SCAN_W, SCAN_H = scan_shape_from_range(
            SCAN_RANGE_X_UM,
            SCAN_RANGE_Y_UM,
            STEP_UM,
            max_range_um=None,
        )
        SERPENTINE_SCAN, SCAN_PATTERN_LABEL = resolve_scan_pattern(SCAN_PATTERN)
    except ValueError as exc:
        append_run_log("RUN_END_ERROR", error=repr(exc), phase="scan_parameter_validation")
        raise SystemExit(f"Scan parameter error: {exc}") from None

    other_pam_processes = find_other_pam_processes(SIBLING_PAM_ENTRY_POINTS)
    if other_pam_processes:
        append_run_log(
            "RUN_END_ERROR",
            error="another_PAM_entry_point_is_running",
            active_processes=" ; ".join(other_pam_processes),
        )
        raise SystemExit(
            "Another PAM entry point is still running and may hold the Alazar board or a controller:\n"
            + "\n".join(other_pam_processes)
            + "\nClose that console/process before starting a new scan."
        )

    append_run_log(
        "RUN_START",
        log_path=RUN_LOG_PATH,
        scan_target=MAT_SCAN_TARGET,
        laser_control="removed",
        stage_controller="PriorUnifiedStage",
        prior_dll=prior_dll_path,
        prior_com_port=prior_com_port,
        um_per_unit=um_per_unit,
        unit_source=unit_details["source"],
        scan_range_x_um=SCAN_RANGE_X_UM,
        scan_range_y_um=SCAN_RANGE_Y_UM,
        step_um=STEP_UM,
        scan_w=SCAN_W,
        scan_h=SCAN_H,
        scan_pattern=SCAN_PATTERN_LABEL,
        sample_position_tolerance_um=POSITION_TOLERANCE_UM,
        sample_position_timeout_s=POSITION_TIMEOUT_S,
        sample_position_reissue_interval_s=POSITION_REISSUE_INTERVAL_S,
        sample_return_step_um=RETURN_STEP_UM,
        sample_return_position_timeout_s=RETURN_POSITION_TIMEOUT_S,
        data_save_auto_timeout_s=DATA_SAVE_AUTO_TIMEOUT_S,
        result_preview_enable=RESULT_PREVIEW_ENABLE,
        point_log_interval=POINT_LOG_INTERVAL,
        user_stop_enable=USER_STOP_ENABLE,
        user_stop_key=USER_STOP_KEY,
        prealign_enable=PREALIGN_ENABLE,
        panel_auto_refresh_s=PANEL_AUTO_REFRESH_S,
        cwd=os.getcwd(),
        pid=os.getpid(),
    )

    daq = daq_init = acquisition_dashboard = None
    all_data, START_X, START_Y, START_Z = [], None, None, None
    coordinate_unit, total_points, acquired_points, user_stop_requested = "um", 0, 0, False
    position_timeout_points = 0
    acquisition_loop_start_s = None
    data_save_done = False
    last_saved_mat_path = None
    preview_snapshot_serial = 0
    preacquisition_quit_requested = False
    result_preview_controller = (
        PAMResultPreviewController(
            project_root=os.getcwd(),
            output_root=RESULT_PREVIEW_OUTPUT_DIR,
            processing_skill_path=PROCESSING_SKILL_PATH,
            python_executable=sys.executable,
            log_callback=append_run_log,
            timeout_s=RESULT_PREVIEW_TIMEOUT_S,
        )
        if RESULT_PREVIEW_ENABLE
        else None
    )

    def save_data_once(reason):
        nonlocal data_save_done, last_saved_mat_path
        if data_save_done:
            append_run_log("DATA_SAVE_ONCE_SKIPPED_ALREADY_DONE", reason=reason)
            return None
        append_run_log("DATA_SAVE_ONCE_BEGIN", reason=reason, points=len(all_data))
        result = save_scan_data(
            all_data,
            SCAN_W,
            SCAN_H,
            STEP_UM,
            RECORDS_PER_POINT,
            SAMPLES_REC,
            AVERAGE_ENABLE,
            MAT_SCAN_TARGET,
            coordinate_unit,
            None,          # probe_step_v: no open-loop probe path in this program
            None,          # probe_um_per_v: no open-loop probe path in this program
            START_X,
            START_Y,
            START_Z,
            DELAY,
            save_prompt_timeout_s=DATA_SAVE_AUTO_TIMEOUT_S,
            stage_metadata=STAGE_METADATA,
        )
        status = result.get("status") if isinstance(result, dict) else None
        if status == "saved" and result.get("path"):
            last_saved_mat_path = result["path"]
            append_run_log("DATA_SAVE_CURRENT_FILE", reason=reason, path=last_saved_mat_path)
            data_save_done = True
        elif status in {"skipped", "discarded"}:
            data_save_done = True
        elif status == "failed":
            data_save_done = False
            append_run_log("DATA_SAVE_ONCE_RETRY_NEEDED", reason=reason, result=repr(result))
        else:
            data_save_done = bool(result)
        append_run_log("DATA_SAVE_ONCE_DONE", reason=reason, result=repr(result))
        return result

    def current_preview_mat_path():
        nonlocal preview_snapshot_serial, last_saved_mat_path
        if all_data:
            preview_snapshot_serial += 1
            snapshot = save_scan_snapshot_data(
                all_data,
                SCAN_W,
                SCAN_H,
                STEP_UM,
                RECORDS_PER_POINT,
                SAMPLES_REC,
                AVERAGE_ENABLE,
                MAT_SCAN_TARGET,
                coordinate_unit,
                None,      # probe_step_v
                None,      # probe_um_per_v
                START_X,
                START_Y,
                START_Z,
                output_dir=RESULT_PREVIEW_SNAPSHOT_DIR,
                label=f"{CURRENT_RUN_ID}-points-{len(all_data):06d}-{preview_snapshot_serial:03d}",
                stage_metadata=STAGE_METADATA,
            )
            if isinstance(snapshot, dict) and snapshot.get("status") == "saved" and snapshot.get("path"):
                last_saved_mat_path = snapshot["path"]
                return snapshot["path"]
            append_run_log("DATA_PREVIEW_SNAPSHOT_UNAVAILABLE", result=repr(snapshot))
        return last_saved_mat_path

    def stop_daq_best_effort(reason):
        if daq is None:
            return
        try:
            daq.stop_capture()
            append_run_log("DAQ_STOP_CAPTURE_DONE", reason=reason)
        except Exception as exc:
            append_run_log("DAQ_STOP_CAPTURE_FAILED", reason=reason, error=repr(exc))
            print(f"DAQ stop_capture failed during {reason}: {exc}")

    def attach_point_metadata(start_len, metadata):
        if len(all_data) <= start_len:
            append_run_log("ACQUISITION_POINT_METADATA_SKIPPED", reason="daq_did_not_append", metadata=repr(metadata))
            return
        point = all_data[-1]
        if len(point) < 3 or not isinstance(point[2], dict):
            point.append({})
        point[2].update(metadata)

    try:
        def daq_factory(step, daq_obj):
            if step == "create_system":
                return AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY, channel_A_range=ats.INPUT_RANGE_PM_200_MV)
            if step == "configure_board":
                daq_obj.configure_board(sample_rate=SAMPLE_RATE)
                return daq_obj
            if step == "prepare_acquisition":
                daq_obj.prepare_acquisition(
                    acq_channel=ats.CHANNEL_A,
                    samples_per_record=SAMPLES_REC,
                    records_per_buffer=RECORDS_PER_POINT,
                    buffer_count=BUFFER_COUNT,
                    records_per_point=RECORDS_PER_POINT,
                )
                return daq_obj
            raise ValueError(f"Unknown DAQ init step: {step}")

        raw_values = stage.get_position_values()
        START_X, START_Y, START_Z = [float(value) for value in raw_values[:3]]
        append_run_log(
            "START_POSITION",
            scan_target=MAT_SCAN_TARGET,
            x_um=f"{START_X:.4f}",
            y_um=f"{START_Y:.4f}",
            z_um=f"{START_Z:.4f}",
        )
        print(
            "Prior start position: "
            f"X={START_X:.4f} um, Y={START_Y:.4f} um, Z={START_Z:.4f} um"
        )

        if PREALIGN_ENABLE:
            daq_init = BackgroundDaqInit(daq_factory, log_callback=append_run_log)
            append_run_log("DAQ_INIT_BACKGROUND_REQUESTED", sample_rate=SAMPLE_RATE, records_per_point=RECORDS_PER_POINT, buffer_count=BUFFER_COUNT)
            daq_init.start()
            prealign_result = run_sample_prealignment(
                stage,
                SamplePrealignConfig(
                    scan_range_x_um=SCAN_RANGE_X_UM,
                    scan_range_y_um=SCAN_RANGE_Y_UM,
                    step_um=STEP_UM,
                    sample_x_direction=SAMPLE_X_DIRECTION,
                    sample_y_direction=SAMPLE_Y_DIRECTION,
                    scan_pattern=SCAN_PATTERN,
                    settle_ms=SETTLE_MS,
                    position_tolerance_um=POSITION_TOLERANCE_UM,
                    position_timeout_s=POSITION_TIMEOUT_S,
                    position_reissue_interval_s=POSITION_REISSUE_INTERVAL_S,
                    x_step_um=PREALIGN_X_STEP_UM,
                    y_step_um=PREALIGN_Y_STEP_UM,
                    z_step_um=PREALIGN_Z_STEP_UM,
                    sample_interval_s=PREALIGN_INTERVAL_S,
                    auto_refresh_s=PANEL_AUTO_REFRESH_S,
                    min_step_um=MIN_STEP_UM,
                ),
                log_callback=append_run_log,
                status_provider=daq_init.snapshot,
                display_params={
                    "LASER_MANAGER": laser_manager,
                    "SCAN_TARGET": MAT_SCAN_TARGET,
                    "PANEL_TITLE": "PAM Prior ProScan prealignment - closed-loop XY in um",
                    "STATUS_HEADER": "Prior ProScan prealignment phase - same PAM_Main_Prior.py process",
                    "HELP_TEXT": PRIOR_HELP_TEXT,
                    "SHOW_PROBE_ROWS": False,
                    "SAMPLE_CONTROLLER": "PriorUnifiedStage",
                    "SAMPLE_STAGE_MODEL": "Prior ProScan",
                    "SAMPLE_CONNECTION": f"COM{prior_com_port}",
                    "SAMPLE_SERIAL": f"COM{prior_com_port}",
                    "SAMPLE_AXIS_MAP": "x/y/z in um",
                    "DELAY": DELAY,
                    "SAMPLES_REC": SAMPLES_REC,
                    "SAMPLE_RATE": SAMPLE_RATE,
                    "AVERAGE_ENABLE": AVERAGE_ENABLE,
                    "ACQ_TIMEOUT_MS": ACQ_TIMEOUT_MS,
                    "RECORDS_PER_POINT": RECORDS_PER_POINT,
                    "BUFFER_COUNT": BUFFER_COUNT,
                    "POINT_LOG_INTERVAL": POINT_LOG_INTERVAL,
                    "USER_STOP_ENABLE": USER_STOP_ENABLE,
                    "USER_STOP_KEY": USER_STOP_KEY,
                    "SAMPLE_START_ZERO_POLICY": "not_applicable_prior",
                    "SAMPLE_ZERO_XY_AT_END": ZERO_AXES_AT_END,
                    "PANEL_AUTO_REFRESH_S": PANEL_AUTO_REFRESH_S,
                },
            )
            if getattr(prealign_result, "next_action", "start") == "quit":
                preacquisition_quit_requested = True
                append_run_log(
                    "RUN_END_PREACQUISITION_QUIT",
                    phase="prealignment",
                    start_panel="sample",
                    acquired_points=0,
                    expected_points=0,
                )
                print("Prealignment quit requested before acquisition; closing the controller without starting DAQ acquisition.")
                return
            # The shared panel returns a FLAT SamplePrealignResult
            # (x_um/y_um/z_um/scan_range_*/step_um/scan_pattern/next_action/scan_ok).
            # The original PAM_Main_Nanomax.py consumed a COMPOSITE result that nested
            # the sample payload under .sample_result (plus the removed open-loop
            # .probe_result). Accept either shape so this program keeps working if the
            # composite workflow wrapper is ever restored.
            sample_result = getattr(prealign_result, "sample_result", prealign_result)
            if sample_result is not None:
                START_X, START_Y, START_Z = sample_result.x_um, sample_result.y_um, sample_result.z_um
                SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM = sample_result.scan_range_x_um, sample_result.scan_range_y_um, sample_result.step_um
                SCAN_PATTERN = sample_result.scan_pattern
                SCAN_W, SCAN_H = scan_shape_from_range(SCAN_RANGE_X_UM, SCAN_RANGE_Y_UM, STEP_UM, max_range_um=None)
                SERPENTINE_SCAN, SCAN_PATTERN_LABEL = resolve_scan_pattern(SCAN_PATTERN)
                append_run_log(
                    "PREALIGN_START_POSITION_SELECTED",
                    x_um=f"{START_X:.4f}",
                    y_um=f"{START_Y:.4f}",
                    z_um=f"{START_Z:.4f}",
                    scan_range_x_um=SCAN_RANGE_X_UM,
                    scan_range_y_um=SCAN_RANGE_Y_UM,
                    step_um=STEP_UM,
                    scan_pattern=SCAN_PATTERN_LABEL,
                    start_panel="sample",
                )

        append_run_log(
            "SCAN_CONFIG",
            scan_target=MAT_SCAN_TARGET,
            scan_range_x_um=SCAN_RANGE_X_UM,
            scan_range_y_um=SCAN_RANGE_Y_UM,
            step_um=STEP_UM,
            scan_w=SCAN_W,
            scan_h=SCAN_H,
            scan_pattern=SCAN_PATTERN_LABEL,
            prealign_enable=PREALIGN_ENABLE,
            return_xy_to_zero_at_end=RETURN_XY_TO_ZERO_AT_END,
        )
        trajectory = build_sample_trajectory(
            START_X,
            START_Y,
            SCAN_W,
            SCAN_H,
            STEP_UM,
            x_direction=SAMPLE_X_DIRECTION,
            y_direction=SAMPLE_Y_DIRECTION,
            serpentine=SERPENTINE_SCAN,
        )
        validate_sample_trajectory(stage, trajectory, limit_label="Prior ProScan working-window")
        xs = [point[0] for point in trajectory]
        ys = [point[1] for point in trajectory]
        total_points = len(trajectory)
        append_run_log(
            "TRAJECTORY_READY",
            scan_target=MAT_SCAN_TARGET,
            x_min_um=f"{min(xs):.4f}",
            x_max_um=f"{max(xs):.4f}",
            y_min_um=f"{min(ys):.4f}",
            y_max_um=f"{max(ys):.4f}",
            points=total_points,
            pattern=SCAN_PATTERN_LABEL,
        )
        print(
            "Prior trajectory accepted: "
            f"X={min(xs):.4f}..{max(xs):.4f} um, "
            f"Y={min(ys):.4f}..{max(ys):.4f} um, "
            f"pattern={SCAN_PATTERN_LABEL}, points={len(trajectory)}."
        )
        coordinate_unit = "um"

        if daq_init is not None:
            snapshot = daq_init.snapshot()
            append_run_log("DAQ_INIT_WAIT_BEGIN", status=snapshot["status"], step=snapshot["step"], elapsed_s=f"{snapshot['elapsed_s']:.3f}")
            if snapshot["status"] != "ready":
                print(f"Waiting for background DAQ init: status={snapshot['status']}, step={snapshot['step']}...")
            daq = daq_init.result()
            snapshot = daq_init.snapshot()
            append_run_log("DAQ_INIT_DONE", mode="background", elapsed_s=f"{snapshot['elapsed_s']:.3f}", timings=snapshot["timings"])
        else:
            append_run_log("DAQ_INIT_BEGIN", mode="synchronous", sample_rate=SAMPLE_RATE, records_per_point=RECORDS_PER_POINT, buffer_count=BUFFER_COUNT)
            step_start = time.time()
            daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY, channel_A_range=ats.INPUT_RANGE_PM_200_MV)
            append_run_log("DAQ_INIT_STEP_DONE", mode="synchronous", step="create_system", duration_s=f"{time.time() - step_start:.3f}")
            step_start = time.time()
            daq.configure_board(sample_rate=SAMPLE_RATE)
            append_run_log("DAQ_INIT_STEP_DONE", mode="synchronous", step="configure_board", duration_s=f"{time.time() - step_start:.3f}")
            step_start = time.time()
            daq.prepare_acquisition(
                acq_channel=ats.CHANNEL_A,
                samples_per_record=SAMPLES_REC,
                records_per_buffer=RECORDS_PER_POINT,
                buffer_count=BUFFER_COUNT,
                records_per_point=RECORDS_PER_POINT,
            )
            append_run_log("DAQ_INIT_STEP_DONE", mode="synchronous", step="prepare_acquisition", duration_s=f"{time.time() - step_start:.3f}")
            append_run_log("DAQ_INIT_DONE", mode="synchronous")

        gc.disable()
        if PREALIGN_ENABLE:
            append_run_log("USER_START_CONFIRMED", source="prealign_panel_start_command")
        else:
            append_run_log("WAITING_FOR_USER_START")
            input("Press Enter to START Experiment... (this program has no laser control)")
            append_run_log("USER_START_CONFIRMED", source="enter_prompt")
        progress_desc = "PAM Prior ProScan scan"
        laser_manager.refresh_status()
        if USER_STOP_ENABLE:
            append_run_log("USER_STOP_POLLING_ENABLED", stop_key=USER_STOP_KEY)

        scan_dashboard_items = [
            ("SCAN_TARGET", MAT_SCAN_TARGET, "frozen"),
            ("SCAN_PATTERN", SCAN_PATTERN_LABEL, "frozen"),
            ("SCAN_W", SCAN_W, "frozen"),
            ("SCAN_H", SCAN_H, "frozen"),
            ("TOTAL_POINTS", len(trajectory), "frozen"),
            ("SCAN_RANGE_X_UM", f"{SCAN_RANGE_X_UM:g}", "frozen"),
            ("SCAN_RANGE_Y_UM", f"{SCAN_RANGE_Y_UM:g}", "frozen"),
            ("STEP_UM", f"{STEP_UM:g}", "frozen"),
            ("START_X", f"{float(START_X):.4f}", f"{coordinate_unit}; frozen"),
            ("START_Y", f"{float(START_Y):.4f}", f"{coordinate_unit}; frozen"),
            ("START_Z", f"{float(START_Z):.4f}", f"{coordinate_unit}; frozen"),
            ("SETTLE_MS", SETTLE_MS, "frozen"),
            ("POS_TOL_UM", f"{POSITION_TOLERANCE_UM:g}", "frozen"),
            ("POS_TIMEOUT_S", f"{POSITION_TIMEOUT_S:g}", "frozen"),
            ("POS_REISSUE_S", f"{POSITION_REISSUE_INTERVAL_S:g}", "frozen"),
            ("RETURN_STEP_UM", f"{RETURN_STEP_UM:g}", "frozen"),
            ("RETURN_POS_TIMEOUT_S", f"{RETURN_POSITION_TIMEOUT_S:g}", "frozen"),
        ]
        daq_dashboard_items = [
            ("DELAY", DELAY, "frozen"),
            ("SAMPLE_RATE", SAMPLE_RATE, "frozen"),
            ("SAMPLES_REC", SAMPLES_REC, "frozen"),
            ("RECORDS_PER_POINT", RECORDS_PER_POINT, "frozen"),
            ("BUFFER_COUNT", BUFFER_COUNT, "frozen"),
            ("AVERAGE_ENABLE", AVERAGE_ENABLE, "frozen"),
            ("ACQ_TIMEOUT_MS", ACQ_TIMEOUT_MS, "frozen"),
        ]
        runtime_dashboard_items = [
            ("PRIOR_COM", f"COM{prior_com_port}", "frozen"),
            ("UM_PER_UNIT", f"{um_per_unit:g}", unit_details["source"]),
            ("MIN_STEP_UM", f"{MIN_STEP_UM:g}", "one SDK unit"),
            ("PRIOR_TRAVEL_X_UM", f"{stage.get_max_travel('x'):.1f}", "working window"),
            ("PRIOR_TRAVEL_Y_UM", f"{stage.get_max_travel('y'):.1f}", "working window"),
            ("PRIOR_Z_ENABLE", PRIOR_Z_ENABLE, "frozen"),
            ("POINT_LOG_INTERVAL", POINT_LOG_INTERVAL, "frozen"),
            ("USER_STOP_ENABLE", USER_STOP_ENABLE, "frozen"),
            ("USER_STOP_KEY", USER_STOP_KEY, "frozen"),
            ("RETURN_XY_TO_ZERO_AT_END", RETURN_XY_TO_ZERO_AT_END, "frozen"),
            ("DATA_SAVE_AUTO_TIMEOUT_S", f"{DATA_SAVE_AUTO_TIMEOUT_S:g}", "frozen"),
            ("RESULT_PREVIEW_ENABLE", RESULT_PREVIEW_ENABLE, "frozen"),
            ("RESULT_PREVIEW_OUTPUT", RESULT_PREVIEW_OUTPUT_DIR, "frozen"),
        ]
        refresh_terminal_for_acquisition()
        acquisition_dashboard = AcquisitionDashboard(
            desc=progress_desc,
            total=len(trajectory),
            laser_manager=laser_manager,
            stop_key=USER_STOP_KEY,
            stop_enabled=USER_STOP_ENABLE,
            scan_items=scan_dashboard_items,
            daq_items=daq_dashboard_items,
            runtime_items=runtime_dashboard_items,
            result_preview_controller=result_preview_controller,
            mat_path_provider=current_preview_mat_path,
            pause_z_controller=(
                PauseZMotionController(
                    stage,
                    step_um=PREALIGN_Z_STEP_UM,
                    settle_ms=SETTLE_MS,
                    tolerance_um=POSITION_TOLERANCE_UM,
                    timeout_s=RETURN_POSITION_TIMEOUT_S,
                    reissue_interval_s=POSITION_REISSUE_INTERVAL_S,
                    min_step_um=MIN_STEP_UM,
                    log_callback=append_run_log,
                )
                if PRIOR_Z_ENABLE
                else None
            ),
            log_callback=append_run_log,
            laser_section_title="Laser Control",
            allowed_hint="none - this program has no laser control; use the stop key only",
            command_hint="no in-scan commands besides the stop key",
        )
        acquisition_dashboard.start()
        append_run_log("ACQUISITION_START", points=len(trajectory), desc=progress_desc)
        acquisition_loop_start_s = time.monotonic()

        for point_index, (tx, ty) in enumerate(trajectory, start=1):
            if acquisition_dashboard is not None and acquisition_dashboard.poll_commands():
                user_stop_requested = True
                append_run_log(
                    "ACQUISITION_USER_STOP_BEFORE_POINT",
                    index=point_index,
                    total=len(trajectory),
                    acquired_points=acquired_points,
                )
                break
            position_settle_ok = True
            position_timeout = False
            actual_x, actual_y, actual_z = float(tx), float(ty), 0.0
            position_error_x, position_error_y, position_error_z = 0.0, 0.0, 0.0
            try:
                stage.move_xyz(
                    x=tx,
                    y=ty,
                    wait=True,
                    settle_time_ms=SETTLE_MS,
                    tolerance=POSITION_TOLERANCE_UM,
                    timeout_s=POSITION_TIMEOUT_S,
                    correction_interval_s=POSITION_REISSUE_INTERVAL_S,
                )
                try:
                    readback = stage.get_position_values()
                    actual_x, actual_y = float(readback[0]), float(readback[1])
                    actual_z = float(readback[2]) if len(readback) > 2 else actual_z
                    position_error_x = abs(actual_x - float(tx))
                    position_error_y = abs(actual_y - float(ty))
                except Exception as read_exc:
                    append_run_log(
                        "ACQUISITION_POSITION_READBACK_FAILED",
                        index=point_index,
                        total=len(trajectory),
                        target_x_um=f"{tx:.6f}",
                        target_y_um=f"{ty:.6f}",
                        error=repr(read_exc),
                    )
            except TimeoutError as exc:
                position_timeout_points += 1
                position_settle_ok = False
                position_timeout = True
                try:
                    readback = stage.get_position_values()
                    actual_x, actual_y = float(readback[0]), float(readback[1])
                    actual_z = float(readback[2]) if len(readback) > 2 else 0.0
                except Exception as read_exc:
                    append_run_log(
                        "ACQUISITION_POSITION_TIMEOUT_READBACK_FAILED",
                        index=point_index,
                        total=len(trajectory),
                        target_x_um=f"{tx:.6f}",
                        target_y_um=f"{ty:.6f}",
                        error=repr(read_exc),
                    )
                position_error_x = abs(actual_x - float(tx))
                position_error_y = abs(actual_y - float(ty))
                position_error_z = abs(actual_z - 0.0)
                append_run_log(
                    "ACQUISITION_POSITION_TIMEOUT_CONTINUE",
                    index=point_index,
                    total=len(trajectory),
                    target_x_um=f"{tx:.6f}",
                    target_y_um=f"{ty:.6f}",
                    actual_x_um=f"{actual_x:.6f}",
                    actual_y_um=f"{actual_y:.6f}",
                    error_x_um=f"{position_error_x:.6f}",
                    error_y_um=f"{position_error_y:.6f}",
                    tolerance_um=f"{POSITION_TOLERANCE_UM:g}",
                    timeout_s=f"{POSITION_TIMEOUT_S:g}",
                    exception=str(exc),
                )
                if acquisition_dashboard is not None:
                    acquisition_dashboard.update(
                        acquired_points,
                        current_position=(
                            f"TARGET X={tx:.4f}, Y={ty:.4f}; "
                            f"ACTUAL X={actual_x:.4f}, Y={actual_y:.4f}; collecting current signal"
                        ),
                        message="Position timeout; acquiring at current readback and continuing.",
                    )
            current_pos_str = f"{tx},{ty},0"
            point_metadata = {
                "target_pos_str": current_pos_str,
                "actual_pos_str": f"{actual_x},{actual_y},{actual_z}",
                "position_settle_ok": position_settle_ok,
                "position_timeout": position_timeout,
                "position_error_x_um": position_error_x,
                "position_error_y_um": position_error_y,
                "position_error_z_um": position_error_z,
            }
            data_len_before = len(all_data)
            daq.get_one_acquisition(
                all_data=all_data,
                curr_pos_str=current_pos_str,
                timeout_ms=ACQ_TIMEOUT_MS,
                Average_Enable=AVERAGE_ENABLE,
            )
            attach_point_metadata(data_len_before, point_metadata)
            acquired_points += 1
            if point_index == 1 or point_index == len(trajectory) or point_index % POINT_LOG_INTERVAL == 0:
                append_run_log(
                    "ACQUISITION_POINT_DONE",
                    index=point_index,
                    total=len(trajectory),
                    x_um=f"{tx:.4f}",
                    y_um=f"{ty:.4f}",
                    actual_x_um=f"{actual_x:.4f}",
                    actual_y_um=f"{actual_y:.4f}",
                    position_settle_ok=position_settle_ok,
                )
            if acquisition_dashboard is not None:
                acquisition_dashboard.update(
                    acquired_points,
                    current_position=(
                        f"X={tx:.4f} um, Y={ty:.4f} um, Z={actual_z:.4f} um"
                        if position_settle_ok
                        else f"TARGET X={tx:.4f}, Y={ty:.4f}; ACTUAL X={actual_x:.4f}, Y={actual_y:.4f}, Z={actual_z:.4f}"
                    ),
                )
            if acquisition_dashboard is not None and acquisition_dashboard.poll_commands():
                user_stop_requested = True
                append_run_log(
                    "ACQUISITION_USER_STOP_AFTER_POINT",
                    index=point_index,
                    total=len(trajectory),
                    acquired_points=acquired_points,
                    x_um=f"{tx:.4f}",
                    y_um=f"{ty:.4f}",
                )
                break

        acquisition_duration_s = time.monotonic() - acquisition_loop_start_s
        if acquisition_dashboard is not None:
            acquisition_dashboard.update(acquired_points, message="Acquisition loop finished; returning the stage.")
            acquisition_dashboard.close()
        end_reason = "user_stop" if user_stop_requested else "completed"
        append_run_log(
            "ACQUISITION_DONE",
            acquired_points=acquired_points,
            expected_points=total_points,
            end_reason=end_reason,
            position_timeout_points=position_timeout_points,
            acquisition_duration_s=f"{acquisition_duration_s:.3f}",
        )

        return_to_start(
            RETURN_TO_START_TARGET,
            stage,
            None,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            False,
            RETURN_XY_TO_ZERO_AT_END,
            ZERO_AXES_AT_END,
            ZERO_AXES,
            sample_return_step_um=RETURN_STEP_UM,
            sample_position_tolerance_um=POSITION_TOLERANCE_UM,
            sample_position_timeout_s=RETURN_POSITION_TIMEOUT_S,
            sample_position_reissue_interval_s=POSITION_REISSUE_INTERVAL_S,
        )
        append_run_log(
            "RUN_END_NORMAL",
            acquired_points=acquired_points,
            expected_points=total_points,
            end_reason=end_reason,
        )

    except KeyboardInterrupt:
        append_run_log("RUN_END_INTERRUPTED", acquired_points=acquired_points, expected_points=total_points)
        print("\nUser interrupted the scan.")
        stop_daq_best_effort("keyboard_interrupt")
        save_data_once("keyboard_interrupt")
        safe_return_to_start(
            RETURN_TO_START_TARGET,
            stage,
            None,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            False,
            RETURN_XY_TO_ZERO_AT_END,
            ZERO_AXES_AT_END,
            ZERO_AXES,
            sample_return_step_um=RETURN_STEP_UM,
            sample_position_tolerance_um=POSITION_TOLERANCE_UM,
            sample_position_timeout_s=RETURN_POSITION_TIMEOUT_S,
            sample_position_reissue_interval_s=POSITION_REISSUE_INTERVAL_S,
        )
    except Exception as exc:
        append_run_log(
            "RUN_END_ERROR",
            error=repr(exc),
            acquired_points=acquired_points,
            expected_points=total_points,
            traceback=traceback.format_exc(limit=6),
        )
        print(f"\nExperiment error: {exc}")
        stop_daq_best_effort("exception")
        save_data_once("exception")
        safe_return_to_start(
            RETURN_TO_START_TARGET,
            stage,
            None,
            START_X,
            START_Y,
            START_Z,
            SETTLE_MS,
            False,
            RETURN_XY_TO_ZERO_AT_END,
            ZERO_AXES_AT_END,
            ZERO_AXES,
            sample_return_step_um=RETURN_STEP_UM,
            sample_position_tolerance_um=POSITION_TOLERANCE_UM,
            sample_position_timeout_s=RETURN_POSITION_TIMEOUT_S,
            sample_position_reissue_interval_s=POSITION_REISSUE_INTERVAL_S,
        )
        append_run_log("RUN_END_ERROR_HANDLED", acquired_points=acquired_points, expected_points=total_points)
    finally:
        append_run_log("FINAL_CLEANUP_BEGIN")
        time.sleep(1)
        try:
            gc.enable()
            if daq is None and daq_init is not None and not preacquisition_quit_requested:
                try:
                    daq = daq_init.result()
                    append_run_log("DAQ_INIT_JOINED_DURING_CLEANUP", status=daq_init.snapshot()["status"])
                except Exception as exc:
                    append_run_log("DAQ_INIT_CLEANUP_JOIN_ERROR", error=repr(exc))
            elif daq is None and daq_init is not None and preacquisition_quit_requested:
                snapshot = daq_init.snapshot()
                append_run_log(
                    "DAQ_INIT_CLEANUP_JOIN_SKIPPED",
                    reason="preacquisition_quit",
                    status=snapshot.get("status", "-"),
                    step=snapshot.get("step", "-"),
                    elapsed_s=f"{float(snapshot.get('elapsed_s', 0.0)):.3f}",
                )
            stop_daq_best_effort("finally")
            if acquisition_dashboard is not None:
                acquisition_dashboard.close()
            if stage is not None:
                stage.close()
            append_run_log("FINAL_CLEANUP_DONE")
        except Exception as exc:
            append_run_log("FINAL_CLEANUP_ERROR", error=repr(exc))
            print(f"Cleanup error: {exc}")

        if not data_save_done and not preacquisition_quit_requested:
            save_data_once("finally")
        elif preacquisition_quit_requested:
            append_run_log("DATA_SAVE_SKIPPED", reason="preacquisition_quit_no_acquisition")


if __name__ == "__main__":
    main()
