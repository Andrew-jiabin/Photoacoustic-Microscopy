"""Stable import path for TOPTICA DLC pro TCP control."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_tool_module():
    tool_path = Path(__file__).resolve().parents[1] / "Tool_code" / "toptica_dlc_controller.py"
    spec = importlib.util.spec_from_file_location("_pam_tool_toptica_dlc_controller", tool_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load TOPTICA controller implementation from {tool_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_tool = _load_tool_module()

DEFAULT_TOPTICA_HOST = _tool.DEFAULT_TOPTICA_HOST
DEFAULT_TOPTICA_PORT = _tool.DEFAULT_TOPTICA_PORT
SAFE_CLOSE_ORDER = _tool.SAFE_CLOSE_ORDER
TOPTICA_BUTTONS = _tool.TOPTICA_BUTTONS
TopticaButton = _tool.TopticaButton


class TopticaDlcProController(_tool.TopticaDlcProController):
    """Alazar_imaging-facing TOPTICA DLC pro controller class."""


__all__ = [
    "DEFAULT_TOPTICA_HOST",
    "DEFAULT_TOPTICA_PORT",
    "SAFE_CLOSE_ORDER",
    "TOPTICA_BUTTONS",
    "TopticaButton",
    "TopticaDlcProController",
]
