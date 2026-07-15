"""Stable import path for the BrightSolutions CBOX-Micro D2XX controller.

The recovered implementation is kept compatible with the Tool_code scripts,
while PAM_Main_Nanomax imports it from Alazar_imaging.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_tool_module():
    tool_path = Path(__file__).resolve().parents[1] / "Tool_code" / "cbox_d2xx_controller.py"
    spec = importlib.util.spec_from_file_location("_pam_tool_cbox_d2xx_controller", tool_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load CBOX controller implementation from {tool_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_tool = _load_tool_module()

DEFAULT_FTDI_SERIAL = _tool.DEFAULT_FTDI_SERIAL
CboxResponse = _tool.CboxResponse


class CboxD2xxController(_tool.CboxD2xxController):
    """Alazar_imaging-facing CBOX controller class."""


__all__ = ["DEFAULT_FTDI_SERIAL", "CboxD2xxController", "CboxResponse"]
