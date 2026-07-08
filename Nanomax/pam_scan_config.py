import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SampleScanConfig:
    scan_range_x_um: float
    scan_range_y_um: float
    step_um: float
    sample_x_direction: float = 1.0
    sample_y_direction: float = 1.0
    scan_pattern: str = "serpentine"


def _literal_value(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        value = _literal_value(node.operand)
        if isinstance(value, (int, float)):
            return -value
    raise ValueError(f"Unsupported non-literal config expression: {ast.unparse(node)}")


def _collect_main_assignments(tree):
    main_node = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"), None)
    if main_node is None:
        raise ValueError("Could not find main() in PAM_Main_Nanomax.py")

    values = {}
    for node in ast.walk(main_node):
        if not isinstance(node, ast.Assign):
            continue
        targets = node.targets
        if len(targets) != 1:
            continue
        target = targets[0]
        if isinstance(target, ast.Name):
            try:
                values[target.id] = _literal_value(node.value)
            except ValueError:
                continue
        elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
            if len(target.elts) != len(node.value.elts):
                continue
            for name_node, value_node in zip(target.elts, node.value.elts):
                if isinstance(name_node, ast.Name):
                    try:
                        values[name_node.id] = _literal_value(value_node)
                    except ValueError:
                        continue
    return values


def load_sample_scan_config(pam_main_path):
    path = Path(pam_main_path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values = _collect_main_assignments(tree)
    required = ("SCAN_RANGE_X_UM", "SCAN_RANGE_Y_UM", "STEP_UM")
    missing = [name for name in required if name not in values]
    if missing:
        raise ValueError(f"Missing scan config values in {path}: {', '.join(missing)}")

    return SampleScanConfig(
        scan_range_x_um=float(values["SCAN_RANGE_X_UM"]),
        scan_range_y_um=float(values["SCAN_RANGE_Y_UM"]),
        step_um=float(values["STEP_UM"]),
        sample_x_direction=float(values.get("SAMPLE_X_DIRECTION", 1.0)),
        sample_y_direction=float(values.get("SAMPLE_Y_DIRECTION", 1.0)),
        scan_pattern=str(values.get("SCAN_PATTERN", "serpentine")),
    )
