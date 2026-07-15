from __future__ import annotations

import datetime as _datetime
from dataclasses import dataclass, field
from typing import Any

from Alazar_imaging.cbox_d2xx_controller import CboxD2xxController, DEFAULT_FTDI_SERIAL
from Alazar_imaging.toptica_dlc_controller import (
    DEFAULT_TOPTICA_HOST,
    DEFAULT_TOPTICA_PORT,
    SAFE_CLOSE_ORDER,
    TopticaDlcProController,
)


def parse_on_off(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"on", "true", "1", "yes", "y", "enable", "enabled"}:
        return True
    if normalized in {"off", "false", "0", "no", "n", "disable", "disabled"}:
        return False
    raise ValueError("value must be on/off")


def bool_text(value: bool) -> str:
    return "ON" if bool(value) else "OFF"


def _dlc_bool_text(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized == "#t":
        return "ON"
    if normalized == "#f":
        return "OFF"
    return str(value)


@dataclass
class LaserRunOptions:
    cbox_enabled: bool = True
    cbox_serial: str = DEFAULT_FTDI_SERIAL
    cbox_close_at_end: bool = False
    toptica_enabled: bool = True
    toptica_host: str = DEFAULT_TOPTICA_HOST
    toptica_port: int = DEFAULT_TOPTICA_PORT
    toptica_close_at_end: bool = False


@dataclass
class LaserStatus:
    values: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    refreshed_at: str = "-"


class PamLaserManager:
    """Read, display, and explicitly control the PAM lasers.

    Startup and acquisition status refreshes are read-only.  State-changing
    commands happen only through explicit panel commands or final close-at-end.
    """

    def __init__(self, options: LaserRunOptions | None = None, log_callback=None):
        self.options = options or LaserRunOptions()
        self.log = log_callback or (lambda *args, **kwargs: None)
        self.status = LaserStatus()

    def _log(self, event: str, **fields: Any) -> None:
        try:
            self.log(event, **fields)
        except Exception:
            pass

    def refresh_status(self) -> LaserStatus:
        values: dict[str, Any] = {}
        errors: dict[str, str] = {}

        if self.options.cbox_enabled:
            try:
                cbox = CboxD2xxController(serial=self.options.cbox_serial)
                flags = cbox.get_flags()
                serial_number = cbox.get_laser_serial_number()
                values.update(
                    {
                        "cbox_connection": "CONNECTED",
                        "cbox_ftdi_serial": self.options.cbox_serial,
                        "cbox_laser_serial": serial_number,
                        "cbox_trigger": "EXTERNAL" if flags.get("trigger_external_inferred") else "INTERNAL",
                        "cbox_emission": "ON" if flags.get("emission_on_inferred") else "OFF",
                        "cbox_flags_hex": flags.get("hex", "-"),
                    }
                )
            except Exception as exc:
                values.update(
                    {
                        "cbox_connection": "ERROR",
                        "cbox_ftdi_serial": self.options.cbox_serial,
                        "cbox_laser_serial": "-",
                        "cbox_trigger": "UNKNOWN",
                        "cbox_emission": "UNKNOWN",
                        "cbox_flags_hex": "-",
                    }
                )
                errors["cbox"] = repr(exc)
        else:
            values.update(
                {
                    "cbox_connection": "DISABLED",
                    "cbox_ftdi_serial": self.options.cbox_serial,
                    "cbox_laser_serial": "-",
                    "cbox_trigger": "-",
                    "cbox_emission": "-",
                    "cbox_flags_hex": "-",
                }
            )

        if self.options.toptica_enabled:
            try:
                with TopticaDlcProController(
                    host=self.options.toptica_host,
                    port=self.options.toptica_port,
                ) as toptica:
                    raw_status = toptica.status()
                values.update(
                    {
                        "toptica_connection": "CONNECTED",
                        "toptica_host": f"{self.options.toptica_host}:{self.options.toptica_port}",
                        "toptica_model": raw_status.get("system-model", "-"),
                        "toptica_emission": _dlc_bool_text(raw_status.get("emission", "-")),
                        "toptica_cc": _dlc_bool_text(raw_status.get("laser1:dl:cc:enabled", "-")),
                        "toptica_pc": _dlc_bool_text(raw_status.get("laser1:dl:pc:enabled", "-")),
                        "toptica_pc_external": _dlc_bool_text(raw_status.get("laser1:dl:pc:external-input:enabled", "-")),
                        "toptica_scan": _dlc_bool_text(raw_status.get("laser1:scan:enabled", "-")),
                    }
                )
            except Exception as exc:
                values.update(
                    {
                        "toptica_connection": "ERROR",
                        "toptica_host": f"{self.options.toptica_host}:{self.options.toptica_port}",
                        "toptica_model": "-",
                        "toptica_emission": "UNKNOWN",
                        "toptica_cc": "UNKNOWN",
                        "toptica_pc": "UNKNOWN",
                        "toptica_pc_external": "UNKNOWN",
                        "toptica_scan": "UNKNOWN",
                    }
                )
                errors["toptica"] = repr(exc)
        else:
            values.update(
                {
                    "toptica_connection": "DISABLED",
                    "toptica_host": f"{self.options.toptica_host}:{self.options.toptica_port}",
                    "toptica_model": "-",
                    "toptica_emission": "-",
                    "toptica_cc": "-",
                    "toptica_pc": "-",
                    "toptica_pc_external": "-",
                    "toptica_scan": "-",
                }
            )

        self.status = LaserStatus(
            values=values,
            errors=errors,
            refreshed_at=_datetime.datetime.now().strftime("%H:%M:%S"),
        )
        self._log(
            "LASER_STATUS_REFRESH",
            cbox_connection=values.get("cbox_connection"),
            cbox_trigger=values.get("cbox_trigger"),
            cbox_emission=values.get("cbox_emission"),
            toptica_connection=values.get("toptica_connection"),
            toptica_cc=values.get("toptica_cc"),
            toptica_pc=values.get("toptica_pc"),
            toptica_pc_external=values.get("toptica_pc_external"),
            toptica_scan=values.get("toptica_scan"),
            errors="; ".join(f"{k}={v}" for k, v in errors.items()),
        )
        return self.status

    def panel_items(self, *, acquisition: bool = False) -> list[tuple[str, str, str]]:
        values = self.status.values
        editable = "frozen" if acquisition else "editable"
        items = [
            ("CBOX_532", values.get("cbox_connection", "UNKNOWN"), f"ftdi={values.get('cbox_ftdi_serial', self.options.cbox_serial)}"),
            ("CBOX_LASER_SN", values.get("cbox_laser_serial", "-"), "actual readback"),
            ("CBOX_TRIGGER", values.get("cbox_trigger", "UNKNOWN"), "actual readback" if acquisition else ":532 trigger ext/int"),
            ("CBOX_EMISSION", values.get("cbox_emission", "UNKNOWN"), "actual readback" if acquisition else ":532 emission on/off"),
            ("CBOX_FLAGS", values.get("cbox_flags_hex", "-"), "actual readback"),
            ("532_CLOSE_AT_END", bool_text(self.options.cbox_close_at_end), ":532 close-at-end on/off"),
            ("TOPTICA", values.get("toptica_connection", "UNKNOWN"), values.get("toptica_host", "-")),
            ("TOPTICA_EMISSION", values.get("toptica_emission", "UNKNOWN"), "actual readback"),
            ("TOPTICA_CC", values.get("toptica_cc", "UNKNOWN"), "actual" if acquisition else ":toptica cc on/off"),
            ("TOPTICA_PC", values.get("toptica_pc", "UNKNOWN"), "actual" if acquisition else ":toptica pc on/off"),
            ("TOPTICA_PC_EXTERNAL", values.get("toptica_pc_external", "UNKNOWN"), "actual" if acquisition else ":toptica external on/off"),
            ("TOPTICA_SCAN", values.get("toptica_scan", "UNKNOWN"), "actual; normally OFF during imaging" if acquisition else ":toptica scan on/off"),
            ("TOPTICA_CLOSE_AT_END", bool_text(self.options.toptica_close_at_end), ":toptica close-at-end on/off"),
            ("LASER_REFRESH", self.status.refreshed_at, ":laser refresh"),
        ]
        if acquisition:
            frozen_keys = {"CBOX_TRIGGER", "CBOX_EMISSION", "TOPTICA_CC", "TOPTICA_PC", "TOPTICA_PC_EXTERNAL", "TOPTICA_SCAN"}
            items = [
                (name, value, f"{hint}; {editable}" if name in frozen_keys else hint)
                for name, value, hint in items
            ]
        if self.status.errors:
            for key, error in self.status.errors.items():
                items.append((f"{key.upper()}_ERROR", "ERROR", error))
        return items

    def execute_prealign_command(self, tokens: list[str]) -> str | None:
        if not tokens:
            return None
        head = tokens[0].lower()
        if head == "laser":
            if len(tokens) == 2 and tokens[1].lower() in {"refresh", "status"}:
                self.refresh_status()
                return "Laser status refreshed."
            return None
        if head in {"532", "cbox", "cbox532"}:
            return self._execute_cbox_command(tokens[1:])
        if head in {"toptica", "dlc"}:
            return self._execute_toptica_command(tokens[1:])
        return None

    def execute_acquisition_command(self, tokens: list[str]) -> str:
        if not tokens:
            return "Empty command."
        head = tokens[0].lower()
        if head == "laser" and len(tokens) == 2 and tokens[1].lower() in {"refresh", "status"}:
            self.refresh_status()
            return "Laser status refreshed."
        if head in {"532", "cbox", "cbox532"}:
            if len(tokens) == 3 and tokens[1].lower() in {"close", "close-at-end", "close_at_end"}:
                self.options.cbox_close_at_end = parse_on_off(tokens[2])
                self._log("LASER_OPTION_CHANGED_DURING_ACQ", name="532_CLOSE_AT_END", value=self.options.cbox_close_at_end)
                return f"532_CLOSE_AT_END set to {bool_text(self.options.cbox_close_at_end)}."
            return "FROZEN during acquisition: only :532 close-at-end on/off is allowed."
        if head in {"toptica", "dlc"}:
            if len(tokens) == 3 and tokens[1].lower() in {"close", "close-at-end", "close_at_end"}:
                self.options.toptica_close_at_end = parse_on_off(tokens[2])
                self._log("LASER_OPTION_CHANGED_DURING_ACQ", name="TOPTICA_CLOSE_AT_END", value=self.options.toptica_close_at_end)
                return f"TOPTICA_CLOSE_AT_END set to {bool_text(self.options.toptica_close_at_end)}."
            return "FROZEN during acquisition: only :toptica close-at-end on/off is allowed."
        return "Unknown or frozen acquisition command."

    def _execute_cbox_command(self, tokens: list[str]) -> str:
        if not tokens:
            raise ValueError("Use :532 emission on/off, :532 trigger ext/int, or :532 close-at-end on/off.")
        action = tokens[0].lower()
        if action in {"close", "close-at-end", "close_at_end"} and len(tokens) == 2:
            self.options.cbox_close_at_end = parse_on_off(tokens[1])
            self._log("LASER_OPTION_CHANGED", name="532_CLOSE_AT_END", value=self.options.cbox_close_at_end)
            return f"532_CLOSE_AT_END set to {bool_text(self.options.cbox_close_at_end)}."
        if not self.options.cbox_enabled:
            raise RuntimeError("CBOX control is disabled.")
        cbox = CboxD2xxController(serial=self.options.cbox_serial)
        if action == "emission" and len(tokens) == 2:
            enabled = parse_on_off(tokens[1])
            response = cbox.set_emission(enabled)
            self._log("CBOX_COMMAND", action="emission", enabled=enabled, ok=response.ok)
            self.refresh_status()
            return f"CBOX emission set to {bool_text(enabled)}; ok={response.ok}."
        if action == "trigger" and len(tokens) == 2:
            source = tokens[1].lower()
            if source in {"ext", "external"}:
                source = "external"
            elif source in {"int", "internal"}:
                source = "internal"
            else:
                raise ValueError("trigger must be ext/int")
            response = cbox.set_trigger_source(source)
            self._log("CBOX_COMMAND", action="trigger", source=source, ok=response.ok)
            self.refresh_status()
            return f"CBOX trigger set to {source.upper()}; ok={response.ok}."
        if action in {"refresh", "status"}:
            self.refresh_status()
            return "CBOX status refreshed."
        raise ValueError("Use :532 emission on/off, :532 trigger ext/int, or :532 close-at-end on/off.")

    def _execute_toptica_command(self, tokens: list[str]) -> str:
        if not tokens:
            raise ValueError("Use :toptica cc|pc|external|scan on/off or :toptica close-at-end on/off.")
        action = tokens[0].lower().replace("-", "_")
        if action in {"close", "close_at_end"} and len(tokens) == 2:
            self.options.toptica_close_at_end = parse_on_off(tokens[1])
            self._log("LASER_OPTION_CHANGED", name="TOPTICA_CLOSE_AT_END", value=self.options.toptica_close_at_end)
            return f"TOPTICA_CLOSE_AT_END set to {bool_text(self.options.toptica_close_at_end)}."
        aliases = {"external": "pc_external", "pc_external": "pc_external", "pcanalog": "pc_external"}
        key = aliases.get(action, action)
        if key not in {"cc", "pc", "pc_external", "scan"} or len(tokens) != 2:
            if action in {"refresh", "status"}:
                self.refresh_status()
                return "TOPTICA status refreshed."
            raise ValueError("Use :toptica cc|pc|external|scan on/off or :toptica close-at-end on/off.")
        if not self.options.toptica_enabled:
            raise RuntimeError("TOPTICA control is disabled.")
        enabled = parse_on_off(tokens[1])
        with TopticaDlcProController(host=self.options.toptica_host, port=self.options.toptica_port) as toptica:
            if enabled:
                response = toptica.enable_button(key)
            elif key == "cc":
                results = toptica.safe_off_lifo()
                response = ", ".join(f"{name}->{state}" for name, _, state in results) or "already off"
            elif key == "pc":
                responses = []
                if toptica.button_state("pc_external"):
                    responses.append(("pc_external", toptica.disable_button("pc_external")))
                responses.append(("pc", toptica.disable_button("pc")))
                response = ", ".join(f"{name}:{resp}" for name, resp in responses)
            else:
                response = toptica.disable_button(key)
        self._log("TOPTICA_COMMAND", action=key, enabled=enabled)
        self.refresh_status()
        return f"TOPTICA {key} set to {bool_text(enabled)}; response={response!r}."

    def finalize_close_at_end(self) -> list[str]:
        messages: list[str] = []
        if self.options.cbox_enabled and self.options.cbox_close_at_end:
            try:
                response = CboxD2xxController(serial=self.options.cbox_serial).set_emission(False)
                message = f"532 emission OFF sent; ok={response.ok}."
                self._log("CBOX_CLOSE_AT_END_DONE", ok=response.ok)
                messages.append(message)
            except Exception as exc:
                self._log("CBOX_CLOSE_AT_END_ERROR", error=repr(exc))
                messages.append(f"532 close-at-end failed: {exc}")
        else:
            self._log("CBOX_CLOSE_AT_END_SKIPPED", enabled=self.options.cbox_enabled, close_at_end=self.options.cbox_close_at_end)

        if self.options.toptica_enabled and self.options.toptica_close_at_end:
            try:
                with TopticaDlcProController(host=self.options.toptica_host, port=self.options.toptica_port) as toptica:
                    results = toptica.safe_off_lifo()
                compact = ", ".join(f"{key}->{state}" for key, _, state in results) or "already off"
                self._log("TOPTICA_CLOSE_AT_END_DONE", order=",".join(SAFE_CLOSE_ORDER), results=compact)
                messages.append(f"TOPTICA safe_off_lifo done: {compact}.")
            except Exception as exc:
                self._log("TOPTICA_CLOSE_AT_END_ERROR", error=repr(exc))
                messages.append(f"TOPTICA close-at-end failed: {exc}")
        else:
            self._log("TOPTICA_CLOSE_AT_END_SKIPPED", enabled=self.options.toptica_enabled, close_at_end=self.options.toptica_close_at_end)

        try:
            self.refresh_status()
        except Exception:
            pass
        return messages
