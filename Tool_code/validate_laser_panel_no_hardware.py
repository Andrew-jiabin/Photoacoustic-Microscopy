#!/usr/bin/env python
"""No-hardware validation for the PAM laser panel integration.

This script is intentionally limited to static checks, py_compile, and disabled
controller command parsing. It must not open BPC303, CBOX/FTDI, TOPTICA/TCP, or
DAQ connections. Use it before real lab validation when ports are occupied.
"""

from __future__ import annotations

import ast
import py_compile
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

TARGETS = [
    "PAM_Main_Nanomax.py",
    "Nanomax/terminal_panel.py",
    "Nanomax/prealign_panel.py",
    "Nanomax/open_loop_panel.py",
    "Nanomax/acquisition_panel.py",
    "Alazar_imaging/BPC303NativeController.py",
    "Alazar_imaging/cbox_d2xx_controller.py",
    "Alazar_imaging/toptica_dlc_controller.py",
    "Alazar_imaging/laser_runtime.py",
    "Tool_code/cbox_d2xx_control.py",
    "Tool_code/laser_control_test.py",
]

HARDWARE_WRITE_CALLS = {
    "set_emission",
    "set_trigger_source",
    "enable_button",
    "disable_button",
    "safe_off_lifo",
}


def parse_module(relative_path: str) -> ast.Module:
    return ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"), filename=relative_path)


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def calls_in_function(function: ast.FunctionDef) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Call):
            calls.append((node.lineno, call_name(node)))
    return calls


def find_method(module: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"Missing method {class_name}.{method_name}")


def py_compile_targets() -> None:
    for relative in TARGETS:
        py_compile.compile(str(REPO_ROOT / relative), doraise=True)
    print(f"py_compile OK for {len(TARGETS)} files")


def audit_no_implicit_laser_writes() -> None:
    main = parse_module("PAM_Main_Nanomax.py")
    main_write_calls = []
    for node in ast.walk(main):
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name in HARDWARE_WRITE_CALLS or name == "finalize_close_at_end":
                main_write_calls.append((node.lineno, name))
    illegal_main = [(line, name) for line, name in main_write_calls if name != "finalize_close_at_end"]
    if illegal_main:
        raise AssertionError(f"PAM_Main_Nanomax has unexpected direct laser write calls: {illegal_main}")
    if not any(name == "finalize_close_at_end" for _, name in main_write_calls):
        raise AssertionError("PAM_Main_Nanomax must call finalize_close_at_end during cleanup")

    laser = parse_module("Alazar_imaging/laser_runtime.py")
    refresh = find_method(laser, "PamLaserManager", "refresh_status")
    refresh_writes = [(line, name) for line, name in calls_in_function(refresh) if name in HARDWARE_WRITE_CALLS]
    if refresh_writes:
        raise AssertionError(f"refresh_status must remain read-only, found writes: {refresh_writes}")

    acquisition = find_method(laser, "PamLaserManager", "execute_acquisition_command")
    acquisition_writes = [(line, name) for line, name in calls_in_function(acquisition) if name in HARDWARE_WRITE_CALLS]
    if acquisition_writes:
        raise AssertionError(f"acquisition commands must not write hardware, found: {acquisition_writes}")
    print("AST no-implicit-write audit OK")


def audit_disabled_hardware_logic() -> None:
    sys.path.insert(0, str(REPO_ROOT))
    from Alazar_imaging.laser_runtime import LaserRunOptions, PamLaserManager
    from Nanomax.acquisition_panel import AcquisitionDashboard

    manager = PamLaserManager(LaserRunOptions(cbox_enabled=False, toptica_enabled=False))
    status = manager.refresh_status()
    assert status.values["cbox_connection"] == "DISABLED"
    assert status.values["toptica_connection"] == "DISABLED"

    assert manager.execute_prealign_command("laser refresh".split()) == "Laser status refreshed."
    assert "set to ON" in manager.execute_prealign_command("532 close-at-end on".split())
    assert "set to ON" in manager.execute_prealign_command("toptica close-at-end on".split())
    assert "FROZEN" in manager.execute_acquisition_command("532 emission on".split())
    assert "FROZEN" in manager.execute_acquisition_command("532 trigger ext".split())
    assert "FROZEN" in manager.execute_acquisition_command("toptica scan off".split())
    assert "set to OFF" in manager.execute_acquisition_command("532 close-at-end off".split())
    assert "set to OFF" in manager.execute_acquisition_command("toptica close-at-end off".split())

    frozen_items = manager.panel_items(acquisition=True)
    assert any(name == "CBOX_EMISSION" and "frozen" in hint for name, _, hint in frozen_items)
    assert any(name == "TOPTICA_SCAN" and "frozen" in hint for name, _, hint in frozen_items)

    dashboard = AcquisitionDashboard(
        desc="dryrun",
        total=1,
        laser_manager=manager,
        stop_key="q",
        stop_enabled=False,
    )
    assert "disabled" in dashboard._command_line()
    assert " q" not in dashboard._command_line()
    print("disabled-hardware command logic OK")


def main() -> int:
    py_compile_targets()
    audit_no_implicit_laser_writes()
    audit_disabled_hardware_logic()
    print("NO-HARDWARE LASER PANEL VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
