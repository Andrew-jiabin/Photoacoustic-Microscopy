#!/usr/bin/env python
"""Reusable TOPTICA DLC pro TCP controller class.

The controller uses the DLC pro command-line port, normally
``192.168.1.11:1998`` on the remote experiment PC LAN.

For the four TOPAS GUI buttons tested on 2026-07-15:

    CC - Current Control Enable       -> laser1:dl:cc:enabled
    PC - Piezo Control Enable         -> laser1:dl:pc:enabled
    SC - Scan Control Enable          -> laser1:scan:enabled
    PC Analog Remote Control Enable   -> laser1:dl:pc:external-input:enabled

Closing should be LIFO/dependency ordered.  In practice, do not directly turn
off CC while upper layers are still active.  Use ``safe_off_lifo()`` or
``close_open_stack()``.
"""

from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass
from typing import Iterable

DEFAULT_TOPTICA_HOST = "192.168.1.11"
DEFAULT_TOPTICA_PORT = 1998


@dataclass(frozen=True)
class TopticaButton:
    key: str
    label: str
    param: str
    depends_on: tuple[str, ...] = ()
    emission_affecting: bool = False


TOPTICA_BUTTONS: dict[str, TopticaButton] = {
    "cc": TopticaButton(
        key="cc",
        label="CC - Current Control Enable",
        param="laser1:dl:cc:enabled",
        emission_affecting=True,
    ),
    "pc": TopticaButton(
        key="pc",
        label="PC - Piezo Control Enable",
        param="laser1:dl:pc:enabled",
        depends_on=("cc",),
    ),
    "pc_external": TopticaButton(
        key="pc_external",
        label="PC Analog Remote Control Enable",
        param="laser1:dl:pc:external-input:enabled",
        depends_on=("cc", "pc"),
    ),
    "scan": TopticaButton(
        key="scan",
        label="SC - Scan Control Enable",
        param="laser1:scan:enabled",
        depends_on=("cc",),
    ),
}

# Conservative close order used if the current process did not create the
# active stack itself.  Higher-level controls are closed before CC.
SAFE_CLOSE_ORDER = ("scan", "pc_external", "pc", "cc")


class TopticaDlcProController:
    """Small DLC pro command-port client with dependency-stack button control."""

    def __init__(self, host: str = DEFAULT_TOPTICA_HOST, port: int = DEFAULT_TOPTICA_PORT, timeout_s: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._open_stack: list[str] = []

    def __enter__(self) -> "TopticaDlcProController":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()

    def connect(self) -> None:
        if self._sock is not None:
            return
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        self._sock.settimeout(self.timeout_s)
        self._read_until_prompt()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _sendall(self, data: bytes) -> None:
        if self._sock is None:
            raise RuntimeError("TOPTICA controller is not connected")
        self._sock.sendall(data)

    def _recv(self, size: int) -> bytes:
        if self._sock is None:
            raise RuntimeError("TOPTICA controller is not connected")
        return self._sock.recv(size)

    def _read_until_prompt(self) -> str:
        chunks: list[bytes] = []
        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            try:
                data = self._recv(4096)
            except socket.timeout:
                break
            if not data:
                break
            chunks.append(data)
            joined = b"".join(chunks)
            if re.search(br"(?m)^>\s*$", joined) or joined.rstrip().endswith(b">"):
                break
        return self._clean_response(b"".join(chunks))

    @staticmethod
    def _clean_response(data: bytes) -> str:
        text = data.decode("utf-8", errors="replace").replace("\x00", "")
        lines: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line == ">":
                continue
            if line.startswith(">"):
                line = line[1:].strip()
            if line:
                lines.append(line)
        return "\n".join(lines).strip()

    def command(self, command_text: str) -> str:
        self._sendall(command_text.encode("utf-8") + b"\n")
        return self._read_until_prompt()

    def param_ref(self, name: str) -> str:
        return self.command(f"(param-ref '{name})")

    def param_set(self, name: str, value: str | bool | int | float) -> str:
        return self.command(f"(param-set! '{name} {self._format_value(value)})")

    def exec_command(self, name: str, args: Iterable[str] = ()) -> str:
        joined = " ".join(args)
        suffix = f" {joined}" if joined else ""
        return self.command(f"(exec '{name}{suffix})")

    @staticmethod
    def _format_value(value: str | bool | int | float) -> str:
        if isinstance(value, bool):
            return "#t" if value else "#f"
        if isinstance(value, (int, float)):
            return str(value)
        normalized = value.strip().lower()
        if normalized in {"#t", "#f"}:
            return normalized
        if normalized in {"on", "true", "1", "enabled", "enable", "yes"}:
            return "#t"
        if normalized in {"off", "false", "0", "disabled", "disable", "no"}:
            return "#f"
        if value.startswith("raw:"):
            return value[4:]
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    @staticmethod
    def _as_bool(value: str) -> bool:
        normalized = value.strip().lower()
        if normalized == "#t":
            return True
        if normalized == "#f":
            return False
        raise ValueError(f"not a DLC boolean response: {value!r}")

    def button_state(self, key: str) -> bool:
        button = TOPTICA_BUTTONS[key]
        return self._as_bool(self.param_ref(button.param))

    def set_button(self, key: str, enabled: bool, *, verify: bool = True, track_stack: bool = True) -> str:
        """Set a tested TOPAS GUI button.

        Enabling pushes the button key onto this object's open stack when the
        operation succeeds.  Disabling removes it.  Prefer ``enable_button`` and
        ``close_open_stack``/``safe_off_lifo`` for acquisition workflows.
        """

        button = TOPTICA_BUTTONS[key]
        response = self.param_set(button.param, enabled)
        if verify:
            actual = self.button_state(key)
            if actual != enabled:
                raise RuntimeError(f"{key} verify failed: expected {enabled}, got {actual}; response={response!r}")
        if track_stack:
            if enabled and key not in self._open_stack:
                self._open_stack.append(key)
            if not enabled:
                self._open_stack = [item for item in self._open_stack if item != key]
        return response

    def enable_button(self, key: str, *, ensure_dependencies: bool = True) -> str:
        if ensure_dependencies:
            for dep in TOPTICA_BUTTONS[key].depends_on:
                if not self.button_state(dep):
                    self.enable_button(dep, ensure_dependencies=True)
        return self.set_button(key, True)

    def disable_button(self, key: str) -> str:
        return self.set_button(key, False)

    def close_open_stack(self) -> list[tuple[str, str, bool]]:
        """Close buttons opened by this object in reverse order."""

        results: list[tuple[str, str, bool]] = []
        while self._open_stack:
            key = self._open_stack.pop()
            response = self.set_button(key, False, track_stack=False)
            results.append((key, response, self.button_state(key)))
        return results

    def safe_off_lifo(self) -> list[tuple[str, str, bool]]:
        """Close the tested buttons in a conservative LIFO/dependency order.

        Use this when the acquisition process did not create the open stack, or
        when recovering after an exception.  It checks each control first and
        only sends an OFF command for active controls.
        """

        results: list[tuple[str, str, bool]] = []
        for key in SAFE_CLOSE_ORDER:
            if self.button_state(key):
                response = self.set_button(key, False, track_stack=False)
                results.append((key, response, self.button_state(key)))
        self._open_stack.clear()
        return results

    def status(self) -> dict[str, str]:
        params = (
            "serial-number",
            "system-model",
            "emission",
            "laser1:emission",
            "laser1:dl:cc:enabled",
            "laser1:dl:pc:enabled",
            "laser1:dl:pc:external-input:enabled",
            "laser1:scan:enabled",
            "laser1:dl:cc:current-set",
            "laser1:dl:cc:current-act",
            "laser1:dl:pc:voltage-set",
            "laser1:dl:pc:voltage-act",
            "laser1:scan:frequency",
            "laser1:scan:amplitude",
            "laser1:scan:offset",
            "laser1:scan:unit",
        )
        return {param: self.param_ref(param) for param in params}

