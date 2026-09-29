"""Expose a Prior ProScan stage through the NanoMax closed-loop stage protocol.

Why this module exists
----------------------
The NanoMax terminal UI -- ``Nanomax/prealign_panel.py``, ``Nanomax/acquisition_panel.py``
and ``Nanomax/runtime.py`` -- was written against the BPC303 / MAX311D closed-loop
controller. Rather than fork roughly 1300 lines of panel code for the Prior stage,
this adapter maps that controller interface onto :class:`PriorUnifiedStage`, so
``PAM_Main_Prior.py`` can reuse the same prealignment panel, the same acquisition
dashboard and the same return-to-start logic as ``PAM_Main_Nanomax.py``.

Interface the NanoMax UI expects
--------------------------------
::

    get_position_values()                     -> [x, y, z] in um
    get_max_travel(axis)                      -> float um
    get_min_travel(axis)                      -> float um
    move_xyz(x=, y=, z=, wait=, settle_time_ms=, tolerance=, timeout_s=,
             correction_interval_s=)
    move_axis(axis, target)
    wait_until_axis_settled(axis, target, settle_time_ms=, tolerance=,
                            timeout_s=, correction_interval_s=)
    set_position([x, y])                      # used by return_to_start
    wait_until_settled(x, y, settle_time_ms=, tolerance_step=, timeout_s=,
                       correction_interval_s=)
    set_zero_axes(axes, wait=, settle_time_ms=)

Units -- read this before trusting a scan size
----------------------------------------------
``PriorUnifiedStage`` speaks in SDK units. The Prior SDK reference notes in
``code_test/prior_interface.py`` describe ``controller.stage.position.get`` as
returning "default units of microns", but ``controller.stage.ss.set <n>`` rescales
that unit::

    resolution_um = ss / steps_per_micron

``steps-per-micron`` is a hardware constant (microsteps per micron);
``ss`` is how many of those microsteps make up one SDK unit. So ``ss = 1`` is the
finest the controller can address, ``ss = steps_per_micron`` is one unit per micron
(the controller's default), and any other ``ss`` scales linearly.

**``ss`` persists across programs.** It is a controller setting, not a process
setting, so it survives disconnect/reconnect. Scripts in this repo set it to
different values (``ss=1``, ``ss=2``, ``ss=50``, ``ss=64``), which means a program
that does not set ``ss`` inherits whatever the previous script left behind. Reading
positions back in microns would then be wrong by that ratio, silently.

Therefore :meth:`PriorStageAdapter.apply_ss_mode` **always** sets ``ss`` explicitly
and derives ``um_per_unit`` from the controller's own ``steps-per-micron`` report,
re-reading it after the write. If ``steps-per-micron`` cannot be read the adapter
returns ``um_per_unit = None`` and the caller is expected to stop, because guessing
here produces a mis-scaled dataset that looks perfectly normal.

Modes (see :meth:`apply_ss_mode`): ``high`` (``ss=1``, finest -- this is what
``PriorUnifiedStage.upgrade_to_high_precision`` does), ``micron`` (one unit = 1 um),
``legacy`` (reproduces ``PAM_Main_SDK.py``'s hardcoded ``HIGH_PRECISION_VALUE``),
``value`` (operator-specified ``ss``) and ``none`` (do not touch ``ss``).

The original ``PAM_Main_SDK.py`` set ``ss`` to 50 and then treated the readback as
microns, which is only correct when ``steps-per-micron == 50``. Nothing in this repo
records the real value, so the adapter never assumes it.

Because ``controller.stage.goto-position`` takes integer SDK units, the smallest
move the stage can make is one SDK unit, i.e. ``um_per_unit`` microns. That value is
published as :attr:`PriorStageAdapter.resolution_um` and the caller is expected to
use it as the minimum scan step, so the UI can never be asked for a step the stage
cannot physically take.

⚠️ **``ss.set`` resets ``hostdirection``.** The Prior SDK notes this, and it is why
axis directions must be re-verified against the hardware after any ``ss`` change.
The adapter cannot verify direction without moving the stage, so it exposes the
direction to the caller (``PAM_PRIOR_X_DIRECTION`` / ``PAM_PRIOR_Y_DIRECTION`` in
``PAM_Main_Prior.py``) and logs it; confirm it with a small jog on the prealignment
panel before the first real scan.

Z axis
------
``PRIOR_Z_ENABLE`` is off by default because the XY ProScan body on this rig has no
verified Z drive. When it is off, Z reads back as 0.0, ``get_max_travel("z")``
returns 0.0 and every Z move is refused, so the prealignment panel simply reports a
zero-width Z range and Z jogging is a no-op. Refusing a Z move drops only the Z leg of
the call: a combined X/Y/Z move still performs its X and Y part, because the
prealignment panel always passes all three axes and silently swallowing the X/Y half
would stall a jog. When Z is enabled, it is read with ``controller.z.position.get``
and moved with ``controller.z.goto-position`` -- that move command was **not**
verified against hardware, so confirm it before relying on Z motion.
"""

from __future__ import annotations

import time


AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

# Matches Nanomax.scan_utils.NANOMAX_MANUAL_MIN_STEP_UM so the shared panels accept
# the value without special-casing.
DEFAULT_MIN_STEP_UM = 0.01
DEFAULT_TRAVEL_UM = 20000.0
DEFAULT_SETTLE_MS = 120
DEFAULT_TIMEOUT_S = 60.0
DEFAULT_REISSUE_S = 1.0
DEFAULT_POLL_S = 0.02


class PriorStageAdapter:
    """Drive a :class:`PriorUnifiedStage` with the NanoMax stage interface."""

    def __init__(
        self,
        stage,
        *,
        um_per_unit=1.0,
        travel_um=DEFAULT_TRAVEL_UM,
        travel_min_um=None,
        z_enable=False,
        z_travel_um=0.0,
        min_step_um=DEFAULT_MIN_STEP_UM,
        settle_default_ms=DEFAULT_SETTLE_MS,
        default_timeout_s=DEFAULT_TIMEOUT_S,
        default_reissue_s=DEFAULT_REISSUE_S,
        poll_interval_s=DEFAULT_POLL_S,
        log_callback=None,
        label="Prior ProScan",
    ):
        self.stage = stage
        self.um_per_unit = float(um_per_unit)
        if self.um_per_unit <= 0:
            raise ValueError(f"um_per_unit must be positive, got {self.um_per_unit!r}.")
        self.travel_um = float(travel_um)
        # The Prior controller reports an ABSOLUTE coordinate whose origin sits somewhere
        # inside the mechanical travel, so a perfectly reachable position can be negative.
        # A [0, travel] window would therefore reject real positions -- and, worse, the panel
        # clamp would drag the stage back to 0. The window is signed; the default is
        # symmetric about the controller origin, and the operator can pin both ends.
        self.travel_min_um = (
            -abs(self.travel_um) if travel_min_um is None else float(travel_min_um)
        )
        if self.travel_min_um > self.travel_um:
            raise ValueError(
                f"travel_min_um ({self.travel_min_um}) must not exceed travel_um "
                f"({self.travel_um})."
            )
        self.z_enable = bool(z_enable)
        self.z_travel_um = float(z_travel_um)
        self.min_step_um = float(min_step_um)
        self.settle_default_ms = int(settle_default_ms)
        self.default_timeout_s = float(default_timeout_s)
        self.default_reissue_s = float(default_reissue_s)
        self.poll_interval_s = float(poll_interval_s)
        self.log_callback = log_callback
        self.label = str(label)

        # Effective travel window, widened at startup so the panel clamp can never
        # command a move *backwards* onto a stage sitting outside the nominal range.
        self.effective_travel_um = {"x": None, "y": None, "z": None}
        self.effective_travel_min_um = {"x": None, "y": None, "z": None}
        self.steps_per_micron = None
        self.ss_value = None
        self.ss_mode = None
        self.resolution_um = self.um_per_unit
        self.last_position_um = None

    # ------------------------------------------------------------------ helpers

    def _log(self, event, **fields):
        if self.log_callback is not None:
            try:
                self.log_callback(event, **fields)
            except Exception:
                pass

    @staticmethod
    def _parse_numbers(raw):
        """Parse an SDK reply such as '1234,5678' into floats, or None."""
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        for separator in (";", "\t", " "):
            text = text.replace(separator, ",")
        values = []
        for chunk in text.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                values.append(float(chunk))
            except ValueError:
                return None
        return values or None

    def _to_um(self, units):
        return float(units) * self.um_per_unit

    def _to_units(self, um):
        return int(round(float(um) / self.um_per_unit))

    # -------------------------------------------------------------- readback

    def _read_xy_units(self):
        raw = self.stage.get_position()
        values = self._parse_numbers(raw)
        if values is None or len(values) < 2:
            raise RuntimeError(
                f"{self.label} position readback was not usable: {raw!r}. "
                "Check that the Prior controller is connected and that no other "
                "program is holding the serial port."
            )
        return values[0], values[1]

    def _read_xy_um(self):
        x_units, y_units = self._read_xy_units()
        return self._to_um(x_units), self._to_um(y_units)

    def _read_z_um(self):
        if not self.z_enable:
            return 0.0
        try:
            _ret, response = self.stage.cmd("controller.z.position.get")
        except Exception as exc:
            self._log("PRIOR_Z_READ_FAILED", error=repr(exc))
            return 0.0
        values = self._parse_numbers(response)
        return self._to_um(values[0]) if values else 0.0

    def get_position_values(self):
        """Return [x, y, z] in microns."""
        x_um, y_um = self._read_xy_um()
        z_um = self._read_z_um()
        self.last_position_um = [x_um, y_um, z_um]
        return [x_um, y_um, z_um]

    def get_max_travel(self, axis):
        """Return the working-window upper bound in microns for one axis."""
        axis = str(axis).strip().lower()
        if axis not in AXIS_INDEX:
            raise ValueError(f"Unknown axis {axis!r}; expected one of x, y, z.")
        if axis == "z":
            return self.z_travel_um if self.z_enable else 0.0
        cached = self.effective_travel_um[axis]
        if cached is None:
            cached = self._refresh_axis_limit(axis)
        return float(cached)

    def get_min_travel(self, axis):
        """Return the working-window lower bound in microns for one axis.

        Negative is normal: the controller's coordinates are absolute and its origin lies
        somewhere inside the travel. The shared panel and trajectory guard use this when the
        stage provides it, so a stage sitting at a negative coordinate is not dragged to 0.
        """
        axis = str(axis).strip().lower()
        if axis not in AXIS_INDEX:
            raise ValueError(f"Unknown axis {axis!r}; expected one of x, y, z.")
        if axis == "z":
            return 0.0 if self.z_enable else 0.0
        cached = self.effective_travel_min_um[axis]
        if cached is None:
            cached = self._refresh_axis_limit(axis)
        return float(cached)

    def _refresh_axis_limit(self, axis):
        index = AXIS_INDEX[axis]
        current_um = None
        try:
            current_um = float(self.get_position_values()[index])
        except Exception as exc:
            self._log("PRIOR_LIMIT_READ_FAILED", axis=axis, error=repr(exc))
        low = float(self.travel_min_um)
        high = float(self.travel_um)
        if current_um is not None and (current_um < low or current_um > high):
            self._log(
                "PRIOR_TRAVEL_WIDENED",
                axis=axis,
                configured_min_um=f"{low:.4f}",
                configured_max_um=f"{high:.4f}",
                current_um=f"{current_um:.4f}",
            )
            print(
                f"NOTE: {self.label} {axis.upper()} reads {current_um:.4f} um, outside the "
                f"configured window [{low:.4f}, {high:.4f}] um. The window was widened to "
                f"include it so the panel clamp can never pull the stage backwards. Set "
                "PAM_PRIOR_TRAVEL_MIN_UM / PAM_PRIOR_TRAVEL_MAX_UM to the real window to "
                "silence this."
            )
            low = min(low, current_um)
            high = max(high, current_um)
        self.effective_travel_min_um[axis] = low
        self.effective_travel_um[axis] = high
        return high

    def refresh_limits(self):
        """Re-read the effective travel window. Call once at startup."""
        for axis in ("x", "y"):
            self._refresh_axis_limit(axis)
        self.effective_travel_um["z"] = self.z_travel_um if self.z_enable else 0.0
        self.effective_travel_min_um["z"] = 0.0
        return dict(self.effective_travel_um)

    # --------------------------------------------------------------- commands

    def _command_axes_um(self, x=None, y=None, z=None):
        """Command an absolute move for the given axes only (microns)."""
        if x is not None or y is not None:
            x_units = self._to_units(x) if x is not None else self._to_units(self._read_xy_um()[0])
            y_units = self._to_units(y) if y is not None else self._to_units(self._read_xy_um()[1])
            self.stage.set_position([x_units, y_units])
        if z is not None:
            if not self.z_enable:
                raise RuntimeError(
                    "A Z move was requested but Z is disabled for this Prior stage. "
                    "Set PRIOR_Z_ENABLE=1 only after confirming the controller has a Z drive."
                )
            self.stage.cmd_simple(f"controller.z.goto-position {self._to_units(z)}")

    def move_xyz(
        self,
        x=None,
        y=None,
        z=None,
        wait=True,
        settle_time_ms=None,
        tolerance=None,
        timeout_s=None,
        correction_interval_s=None,
    ):
        """Absolute move in microns for any subset of axes, then optionally wait."""
        targets = {}
        if x is not None:
            targets["x"] = float(x)
        if y is not None:
            targets["y"] = float(y)
        if z is not None:
            if not self.z_enable:
                # Drop only the Z leg. Abandoning the whole call here would silently
                # swallow a perfectly good X/Y move whenever a caller passes all three
                # axes at once, which is exactly what the prealignment panel does.
                self._log("PRIOR_Z_MOVE_REFUSED", reason="z_disabled")
                z = None
            else:
                targets["z"] = float(z)
        if not targets:
            return
        self._command_axes_um(**targets)
        if wait:
            self._wait_settle(
                targets,
                settle_time_ms=settle_time_ms,
                tolerance_um=tolerance,
                timeout_s=timeout_s,
                reissue_s=correction_interval_s,
            )

    def move_axis(self, axis, target_um, wait=True, **kwargs):
        axis = str(axis).strip().lower()
        self.move_xyz(**{axis: float(target_um)}, wait=wait, **kwargs)

    def wait_until_axis_settled(
        self,
        axis,
        target_um,
        settle_time_ms=None,
        tolerance=None,
        timeout_s=None,
        correction_interval_s=None,
    ):
        axis = str(axis).strip().lower()
        return self._wait_settle(
            {axis: float(target_um)},
            settle_time_ms=settle_time_ms,
            tolerance_um=tolerance,
            timeout_s=timeout_s,
            reissue_s=correction_interval_s,
        )

    def set_position(self, position):
        """Absolute XY move in microns. Signature matches the BPC303 controller."""
        self._command_axes_um(x=float(position[0]), y=float(position[1]))

    def wait_until_settled(
        self,
        target_x_um,
        target_y_um,
        settle_time_ms=None,
        tolerance_step=None,
        timeout_s=None,
        correction_interval_s=None,
    ):
        """Wait until XY is within tolerance. Raises TimeoutError on expiry."""
        return self._wait_settle(
            {"x": float(target_x_um), "y": float(target_y_um)},
            settle_time_ms=settle_time_ms,
            tolerance_um=tolerance_step,
            timeout_s=timeout_s,
            reissue_s=correction_interval_s,
        )

    def set_zero_axes(self, axes, wait=True, settle_time_ms=None):
        """No-op on Prior.

        The NanoMax closed-loop controller can rebuild its low-end zero datum by
        driving the piezo to 0 V. A Prior stage has no equivalent software datum:
        its coordinates are the controller's own absolute frame. Rather than fake a
        datum shift, this logs and returns. Use the panel's ``0`` / ``r`` hotkey or
        ``controller.stage.position.set`` if you want to re-reference the origin.
        """
        axis_text = ",".join(str(axis) for axis in axes)
        self._log("PRIOR_ZERO_AXES_IGNORED", axes=axis_text, reason="prior_has_no_software_datum")
        print(
            f"Zero-datum rebuild is not applicable to the {self.label}; "
            f"axes {axis_text} were left at their absolute controller coordinates."
        )
        return False

    # --------------------------------------------------------------- settling

    def _wait_settle(self, targets, settle_time_ms=None, tolerance_um=None, timeout_s=None, reissue_s=None):
        tolerance = self.min_step_um if tolerance_um is None else abs(float(tolerance_um))
        timeout = self.default_timeout_s if timeout_s is None else float(timeout_s)
        settle_ms = self.settle_default_ms if settle_time_ms is None else max(0, int(settle_time_ms))
        reissue = self.default_reissue_s if reissue_s is None else float(reissue_s)

        deadline = time.monotonic() + max(0.001, timeout)
        next_reissue = time.monotonic() + reissue if reissue > 0 else None
        stable_since = None
        last_error = float("nan")
        last_values = None

        while True:
            values = self.get_position_values()
            last_values = values
            last_error = max(
                abs(float(values[AXIS_INDEX[axis]]) - float(target))
                for axis, target in targets.items()
            )
            now = time.monotonic()

            if last_error <= tolerance:
                if settle_ms <= 0:
                    return True
                if stable_since is None:
                    stable_since = now
                elif (now - stable_since) * 1000.0 >= settle_ms:
                    return True
            else:
                stable_since = None
                if next_reissue is not None and now >= next_reissue:
                    self._command_axes_um(**targets)
                    next_reissue = now + max(0.05, reissue)

            if now >= deadline:
                readback = " ".join(
                    f"{axis.upper()}={float(last_values[AXIS_INDEX[axis]]):.4f}"
                    for axis in targets
                )
                wanted = " ".join(f"{axis.upper()}={float(v):.4f}" for axis, v in targets.items())
                raise TimeoutError(
                    f"{self.label} did not settle within {timeout:g} s. "
                    f"Target {wanted} um, readback {readback} um, "
                    f"worst error {last_error:.4f} um, tolerance {tolerance:g} um."
                )
            time.sleep(self.poll_interval_s)

    # ----------------------------------------------------------- introspection

    def probe_identity(self):
        """Read the controller's own unit metadata and log it.

        Returns a dict for the startup banner. Never raises: an unreadable field is
        reported as None so a missing reply cannot stop the run.
        """
        steps_per_micron = self.read_steps_per_micron()
        ss_value = self._read_ss_value()

        self.steps_per_micron = steps_per_micron
        self.ss_value = ss_value
        implied = None
        if steps_per_micron and ss_value:
            implied = float(ss_value) / float(steps_per_micron)

        self._log(
            "PRIOR_IDENTITY",
            steps_per_micron=steps_per_micron,
            ss=ss_value,
            implied_um_per_unit=implied,
            configured_um_per_unit=self.um_per_unit,
            resolution_um=self.resolution_um,
            z_enable=int(self.z_enable),
        )
        return {
            "steps_per_micron": steps_per_micron,
            "ss": ss_value,
            "implied_um_per_unit": implied,
            "configured_um_per_unit": self.um_per_unit,
            "resolution_um": self.resolution_um,
        }

    def read_steps_per_micron(self):
        """Read the controller's hardware microsteps-per-micron. None if unreadable.

        This is a hardware constant, not a setting: it is what makes the ``ss`` step
        size convertible to microns. Never raises.
        """
        try:
            _ret, response = self.stage.cmd("controller.stage.steps-per-micron.get")
        except Exception as exc:
            self._log("PRIOR_STEPS_PER_MICRON_READ_FAILED", error=repr(exc))
            return None
        values = self._parse_numbers(response)
        if not values:
            self._log("PRIOR_STEPS_PER_MICRON_READ_FAILED", reason="unparsable", response=repr(response))
            return None
        value = float(values[0])
        if value <= 0:
            self._log("PRIOR_STEPS_PER_MICRON_READ_FAILED", reason="not_positive", value=value)
            return None
        return value

    def set_step_size(self, ss_value):
        """Send ``controller.stage.ss.set <n>`` and return the integer that was sent.

        ``ss`` is the number of microsteps that make up one SDK unit, so
        ``um_per_unit = ss / steps_per_micron``. It is a controller setting that
        **persists across programs**, which is why this program always sets it
        explicitly instead of trusting whatever the last script left behind.
        """
        ss_value = int(round(float(ss_value)))
        if ss_value < 1:
            raise ValueError(f"ss must be at least 1 microstep per unit, got {ss_value}.")
        self.stage.cmd_simple(f"controller.stage.ss.set {ss_value}")
        self._log("PRIOR_SS_SET", ss=ss_value)
        return ss_value

    def apply_ss_mode(self, mode, ss_value=None, legacy_value=None):
        """Set the step size so one SDK unit has a known size in microns.

        Returns ``(um_per_unit, details)``. ``um_per_unit`` is None when the unit could
        not be derived -- in that case the caller must stop rather than guess, because a
        wrong microns-per-unit silently mis-scales every saved image.

        ``details`` always carries the same seven keys, so a caller can read any of them
        without a membership test and log a whole failed attempt in one call:
        ``mode``, ``steps_per_micron``, ``ss_requested``, ``ss_sent``, ``ss_value``
        (the ``ss`` actually in force afterwards), ``um_per_unit``, ``ok``, ``reason``.

        Modes:
          ``high``   -- ``ss = 1``: one unit is one microstep, the finest the controller
                        can address. This is what
                        ``PriorUnifiedStage.upgrade_to_high_precision`` does.
          ``micron`` -- ``ss = steps_per_micron``: one unit is exactly one micron, which
                        is the controller's own default unit.
          ``legacy`` -- ``ss = legacy_value``: reproduces the old ``PAM_Main_SDK.py``,
                        which hardcoded ``HIGH_PRECISION_VALUE``. Only meaningful when
                        ``legacy_value == steps_per_micron``.
          ``value``  -- ``ss = ss_value``: whatever the operator asked for.
          ``none``   -- do not touch ``ss``; report the unit the controller is already
                        in. Only safe if nothing else changed ``ss`` since power-up.
        """
        mode = str(mode).strip().lower()
        steps_per_micron = self.read_steps_per_micron()

        details = {
            "mode": mode,
            "steps_per_micron": steps_per_micron,
            "ss_requested": None,
            "ss_sent": None,
            "ss_value": None,
            "um_per_unit": None,
            "ok": False,
            "reason": None,
        }

        if mode == "none":
            if steps_per_micron is None:
                details["reason"] = "steps_per_micron_unreadable_and_ss_not_touched"
                self._log("PRIOR_SS_MODE_FAILED", **details)
                return None, details
            ss_now = self._read_ss_value()
            if ss_now is None:
                details["reason"] = "ss_unreadable_and_ss_not_touched"
                self._log("PRIOR_SS_MODE_FAILED", **details)
                return None, details
            details["ss_sent"] = ss_now
            details["ss_value"] = ss_now
            um_per_unit = float(ss_now) / float(steps_per_micron)
            details.update(um_per_unit=um_per_unit, ok=True)
            self.um_per_unit = um_per_unit
            self.resolution_um = um_per_unit
            self.ss_value = ss_now
            self.ss_mode = mode
            self._log("PRIOR_SS_MODE_APPLIED", **details)
            return um_per_unit, details

        if mode == "high":
            if steps_per_micron is None:
                details["reason"] = "steps_per_micron_unreadable"
                self._log("PRIOR_SS_MODE_FAILED", **details)
                return None, details
            ss_target = 1
        elif mode == "micron":
            if steps_per_micron is None:
                details["reason"] = "steps_per_micron_unreadable"
                self._log("PRIOR_SS_MODE_FAILED", **details)
                return None, details
            ss_target = int(round(steps_per_micron))
        elif mode == "legacy":
            if legacy_value is None:
                details["reason"] = "legacy_value_missing"
                self._log("PRIOR_SS_MODE_FAILED", **details)
                return None, details
            ss_target = int(round(float(legacy_value)))
        elif mode == "value":
            if ss_value is None:
                details["reason"] = "ss_value_missing"
                self._log("PRIOR_SS_MODE_FAILED", **details)
                return None, details
            ss_target = int(round(float(ss_value)))
        else:
            raise ValueError(
                f"Unknown ss mode {mode!r}; expected one of high, micron, legacy, value, none."
            )

        details["ss_requested"] = ss_target
        if ss_target < 1:
            details["reason"] = "ss_target_below_one"
            self._log("PRIOR_SS_MODE_FAILED", **details)
            return None, details

        ss_sent = self.set_step_size(ss_target)
        details["ss_sent"] = ss_sent
        details["ss_value"] = ss_sent

        # Re-read after the write: the controller is the authority on what it accepted,
        # and a silently clamped value would otherwise corrupt every later conversion.
        steps_after = self.read_steps_per_micron()
        if steps_after is None:
            details["reason"] = "steps_per_micron_unreadable_after_ss_set"
            self._log("PRIOR_SS_MODE_FAILED", **details)
            return None, details
        if steps_after != steps_per_micron:
            self._log(
                "PRIOR_STEPS_PER_MICRON_CHANGED",
                before=steps_per_micron,
                after=steps_after,
            )
        details["steps_per_micron"] = steps_after

        um_per_unit = float(ss_sent) / float(steps_after)
        details.update(um_per_unit=um_per_unit, ok=True)
        self.um_per_unit = um_per_unit
        self.resolution_um = um_per_unit
        self.ss_value = ss_sent
        self.steps_per_micron = steps_after
        self.ss_mode = mode
        self._log("PRIOR_SS_MODE_APPLIED", **details)
        return um_per_unit, details

    def read_ss_value(self):
        """Read the controller's current ``ss`` (microsteps per SDK unit). None if unreadable.

        Public counterpart of :meth:`read_steps_per_micron`: that one is the hardware
        constant, this one is the operator-settable step size. A caller that bypasses
        :meth:`apply_ss_mode` still needs this to record which ``ss`` the scan was taken in.
        """
        return self._read_ss_value()

    def _read_ss_value(self):
        """Read the controller's current ``ss``. None if unreadable."""
        try:
            _ret, response = self.stage.cmd("controller.stage.ss.get")
        except Exception as exc:
            self._log("PRIOR_SS_READ_FAILED", error=repr(exc))
            return None
        values = self._parse_numbers(response)
        if not values:
            return None
        value = float(values[0])
        return value if value > 0 else None

    def resolution_step_um(self):
        """Smallest move the stage can make, in microns."""
        if self.resolution_um is None:
            return float(self.min_step_um)
        return max(self.min_step_um, float(self.resolution_um))

    def describe(self):
        """Human-readable one-liner for the startup banner."""
        unit_text = "unknown" if self.um_per_unit is None else f"{self.um_per_unit:g}"
        ss_text = "?" if self.ss_value is None else f"{self.ss_value:g}"
        spm_text = "?" if self.steps_per_micron is None else f"{self.steps_per_micron:g}"
        return (
            f"{self.label}: um_per_unit={unit_text} (ss={ss_text}, "
            f"steps_per_micron={spm_text}, mode={self.ss_mode or '-'}), "
            f"smallest step={self.resolution_step_um():g} um, "
            f"X window=[{self.get_min_travel('x'):.1f}, {self.get_max_travel('x'):.1f}] um, "
            f"Y window=[{self.get_min_travel('y'):.1f}, {self.get_max_travel('y'):.1f}] um, "
            f"Z={'enabled' if self.z_enable else 'disabled'}"
        )

    # --------------------------------------------------------------- teardown

    def emergency_stop(self):
        try:
            self.stage.emergency_stop()
        except Exception as exc:
            self._log("PRIOR_EMERGENCY_STOP_FAILED", error=repr(exc))

    def close(self):
        try:
            self.stage.stage_deinitial()
            self._log("PRIOR_STAGE_CLOSED")
        except Exception as exc:
            self._log("PRIOR_STAGE_CLOSE_FAILED", error=repr(exc))
