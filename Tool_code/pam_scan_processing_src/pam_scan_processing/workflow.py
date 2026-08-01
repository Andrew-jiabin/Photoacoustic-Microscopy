from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess

import numpy as np

from .core import load_pam_file, parse_slice, process_file
from .diagnostics import write_arrival_diagnostics
from .interactive import write_index_html, write_interactive_html
from .result_index import write_result_index
from .waveform_browser import launch_waveform_browser
from .xt_map import write_x_time_absolute_map
from .time_axis_map import write_axis_time_checker


DEFAULT_REMOTE_HOST = "PAM"
DEFAULT_REMOTE_PROJECT_ROOT = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy"
DEFAULT_REMOTE_DATA_DIR = DEFAULT_REMOTE_PROJECT_ROOT + r"\data"


@dataclass
class SourceRecord:
    input_spec: str
    source_kind: str
    source_path: str
    local_path: str


def _safe_run_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    cleaned = cleaned.strip("._")
    if not cleaned:
        raise ValueError("run_id must contain at least one alphanumeric character")
    return cleaned


def _remote_suffix(spec: str, remote_data_dir: str) -> str | None:
    text = str(spec).strip().strip('"').replace("\\", "/")
    lower = text.lower()
    if lower in {"./data", ".", "data"}:
        return ""
    for prefix in ("./data/", "data/"):
        if lower.startswith(prefix):
            return text[len(prefix) :]
    remote_prefix = remote_data_dir.replace("\\", "/").rstrip("/") + "/"
    if lower.startswith(remote_prefix.lower()):
        return text[len(remote_prefix) :]
    return None


def remote_path_for_spec(spec: str, remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR) -> str | None:
    """Resolve a project-relative data reference to a remote Windows path."""
    text = str(spec).strip().strip('"')
    suffix = _remote_suffix(text, remote_data_dir)
    if suffix is not None:
        return str(PureWindowsPath(remote_data_dir) / PureWindowsPath(suffix)) if suffix else remote_data_dir
    normalized = text.replace("/", "\\")
    if re.match(r"^[A-Za-z]:\\", normalized):
        return normalized
    return None


def _remote_list_mat_files(host: str, remote_dir: str) -> list[str]:
    escaped = remote_dir.replace("'", "''")
    command = (
        "powershell -NoProfile -Command "
        f"\"Get-ChildItem -LiteralPath '{escaped}' -Filter '*.mat' -File | "
        "ForEach-Object { $_.FullName }\""
    )
    result = subprocess.run(
        ["ssh", host, command],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip().lower().endswith(".mat")]


def _scp_remote_file(host: str, remote_file: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    remote_file_posix = remote_file.replace("\\", "/")
    scp_source = f"{host}:{remote_file_posix}"
    subprocess.run(["scp", scp_source, str(destination)], check=True)


def _expand_local_spec(spec: str) -> list[Path]:
    path = Path(spec)
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.glob("*.mat"))
    return []


def _resolve_source_specs(
    input_specs: list[str],
    input_dir: str | None,
    remote_host: str,
    remote_data_dir: str,
) -> list[tuple[str, str, str]]:
    """Return (input_spec, source_kind, source_path) before local materialization."""
    resolved: list[tuple[str, str, str]] = []
    for spec in input_specs:
        local_matches = _expand_local_spec(spec)
        if local_matches:
            resolved.extend((spec, "local", str(path)) for path in local_matches)
            continue
        remote_file = remote_path_for_spec(spec, remote_data_dir)
        if remote_file is None or remote_file.endswith(("\\", "/")):
            raise FileNotFoundError(f"Input does not exist locally and is not a supported remote data reference: {spec}")
        resolved.append((spec, "remote", remote_file))

    if input_dir:
        local_matches = _expand_local_spec(input_dir)
        if local_matches:
            resolved.extend((input_dir, "local", str(path)) for path in local_matches)
        else:
            remote_dir = remote_path_for_spec(input_dir, remote_data_dir)
            if remote_dir is None:
                raise FileNotFoundError(f"Input directory does not exist locally and is not a supported remote reference: {input_dir}")
            remote_files = _remote_list_mat_files(remote_host, remote_dir)
            if not remote_files:
                raise FileNotFoundError(f"No .mat files found in remote directory: {remote_dir}")
            resolved.extend((input_dir, "remote", path) for path in remote_files)

    if not resolved:
        raise ValueError("Provide at least one --input or --input-dir")
    return resolved


def materialize_sources(
    resolved: list[tuple[str, str, str]],
    raw_dir: Path,
    remote_host: str,
) -> tuple[list[Path], list[SourceRecord]]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    local_files: list[Path] = []
    records: list[SourceRecord] = []
    seen_destinations: set[Path] = set()
    for input_spec, source_kind, source_path in resolved:
        filename = Path(source_path).name
        destination = raw_dir / filename
        if destination in seen_destinations:
            continue
        if source_kind == "remote":
            _scp_remote_file(remote_host, source_path, destination)
        else:
            source = Path(source_path).resolve()
            if source != destination.resolve():
                shutil.copy2(source, destination)
        seen_destinations.add(destination)
        local_files.append(destination)
        records.append(SourceRecord(input_spec, source_kind, source_path, str(destination)))
    return sorted(local_files), records


def choose_representative_y(path: Path) -> tuple[float, float]:
    """Return center Y and the row with the largest median full-waveform peak-to-peak."""
    data, _, points = load_pam_file(path)
    y_values = sorted({round(float(point.y), 12) for point in points})
    center_y = float(y_values[len(y_values) // 2])
    medians: dict[float, float] = {}
    for y_value in y_values:
        p2p = [float(np.ptp(np.asarray(data[point.key], dtype=np.float32))) for point in points if round(float(point.y), 12) == y_value]
        medians[y_value] = float(np.median(p2p)) if p2p else 0.0
    strongest_y = max(medians, key=medians.get)
    return center_y, float(strongest_y)


def run_workflow(
    skill_root: Path,
    input_specs: list[str],
    input_dir: str | None = None,
    run_id: str | None = None,
    remote_host: str = DEFAULT_REMOTE_HOST,
    remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR,
    arrival_window: tuple[int, int] = (100, 700),
    display_window: tuple[int, int] = (0, -1),
    baseline: tuple[int, int] = (0, 100),
    time_step: int = 4,
    max_traces: int = 700,
    max_waveform_points: int = 150_000,
    min_confidence: float = 0.6,
    x_time_windows: tuple[tuple[int, int], ...] = ((0, -1), (0, 800)),
    axis_time_map: bool = False,
    axis_time_display_window: tuple[int, int] = (0, 4000),
    axis_time_baseline: tuple[int, int] = (0, 100),
    axis_time_step: int = 1,
    axis_time_clip_percentile: float = 99.5,
    axis_time_mode: str = "x",
    axis_time_hilbert: bool = False,
    browser_preview: bool = True,
) -> dict:
    skill_root = Path(skill_root).resolve()
    resolved = _resolve_source_specs(input_specs, input_dir, remote_host, remote_data_dir)
    final_run_id = _safe_run_id(run_id or f"pam_workflow_{datetime.now():%Y%m%d_%H%M%S}")
    raw_dir = skill_root / "workspace" / "data" / "raw" / final_run_id
    result_root = skill_root / "workspace" / "results" / final_run_id
    local_files, source_records = materialize_sources(resolved, raw_dir, remote_host)

    static_dir = result_root / "global_prior" / "static"
    interactive_dir = result_root / "global_prior" / "interactive"
    diagnostics_dir = result_root / "global_prior" / "diagnostics"
    x_time_dir = result_root / "global_prior" / "x_time"
    axis_time_dir = result_root / "global_prior" / "axis_time_map"
    browser_dir = result_root / "waveform_browser"
    summaries = []
    interactive_files = []
    diagnostics = []
    x_time_outputs = []
    browser_outputs = []
    axis_time_outputs: list[dict[str, str | int | float | tuple | bool]] = []
    representatives = {}

    for path in local_files:
        summaries.append(
            process_file(
                path=path,
                output_dir=static_dir,
                arrival_window=arrival_window,
                display_window=display_window,
                baseline=baseline,
                time_step=time_step,
                max_traces=max_traces,
                smooth_sigma=3.0,
                threshold_sigma=5.0,
                min_confidence=min_confidence,
            )
        )
        interactive_files.append(
            write_interactive_html(
                path=path,
                output_dir=interactive_dir,
                arrival_window=arrival_window,
                display_window=display_window,
                baseline=baseline,
                time_step=time_step,
                max_traces=max_traces,
                max_waveform_points=max_waveform_points,
                smooth_sigma=3.0,
                threshold_sigma=5.0,
                min_marker_confidence=min_confidence,
            )
        )
        diagnostics.append(
            write_arrival_diagnostics(
                path=path,
                output_dir=diagnostics_dir,
                target_window=arrival_window,
                baseline=baseline,
                smooth_sigma=3.0,
                threshold_sigma=5.0,
                point_count=12,
            )
        )
        center_y, strongest_y = choose_representative_y(path)
        representatives[path.name] = {"center_y_um": center_y, "strongest_y_um": strongest_y}
        row_requests = [(center_y, "center")]
        if not np.isclose(strongest_y, center_y):
            row_requests.append((strongest_y, "strongest"))
        for y_value, purpose in row_requests:
            for window in x_time_windows:
                output = write_x_time_absolute_map(
                    path=path,
                    output_dir=x_time_dir,
                    y=y_value,
                    display_window=window,
                    baseline=baseline,
                )
                output["purpose"] = purpose
                x_time_outputs.append(output)
        if axis_time_map:
            axis_output = write_axis_time_checker(
                input_spec=str(path),
                output_dir=axis_time_dir,
                display_window=axis_time_display_window,
                baseline=axis_time_baseline,
                time_step=axis_time_step,
                clip_percentile=axis_time_clip_percentile,
                initial_mode=axis_time_mode,
                use_hilbert=axis_time_hilbert,
                remote_host=remote_host,
                remote_data_dir=remote_data_dir,
            )
            axis_time_outputs.append(axis_output)
        if browser_preview:
            import matplotlib

            matplotlib.use("Agg", force=True)
            preview = browser_dir / f"{path.stem}_browser_preview.png"
            launch_waveform_browser(
                path=path,
                baseline=baseline,
                centered=True,
                save_preview=preview,
                show=False,
            )
            browser_outputs.append(str(preview))

    interactive_index = write_index_html(interactive_dir, interactive_files)
    batch_summary = static_dir / "batch_summary.json"
    batch_summary.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(),
        "run_id": final_run_id,
        "remote": {"host": remote_host, "data_dir": remote_data_dir},
        "inputs": [asdict(record) for record in source_records],
        "parameters": {
            "arrival_window": list(arrival_window),
            "display_window": list(display_window),
            "baseline": list(baseline),
            "time_step": time_step,
            "max_traces": max_traces,
            "max_waveform_points": max_waveform_points,
            "min_confidence": min_confidence,
            "x_time_windows": [list(window) for window in x_time_windows],
            "axis_time_map": axis_time_map,
            "axis_time_display_window": list(axis_time_display_window),
            "axis_time_baseline": list(axis_time_baseline),
            "axis_time_step": axis_time_step,
            "axis_time_clip_percentile": axis_time_clip_percentile,
            "axis_time_mode": axis_time_mode,
            "axis_time_hilbert": axis_time_hilbert,
            "browser_preview": browser_preview,
        },
        "representative_rows": representatives,
        "outputs": {
            "raw_dir": str(raw_dir),
            "result_root": str(result_root),
            "static_dir": str(static_dir),
            "interactive_index": str(interactive_index),
            "diagnostics_dir": str(diagnostics_dir),
            "x_time_dir": str(x_time_dir),
            "x_time_maps": x_time_outputs,
            "axis_time_dir": str(axis_time_dir),
            "axis_time_maps": axis_time_outputs,
            "browser_previews": browser_outputs,
            "batch_summary": str(batch_summary),
            "file_summaries": [summary["outputs"] for summary in summaries],
            "interactive_files": [str(path) for path in interactive_files],
            "diagnostic_summaries": diagnostics,
        },
    }
    manifest_path = result_root / "workflow_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    index_result = write_result_index(skill_root)
    manifest["outputs"]["global_index_html"] = index_result["html"]
    manifest["outputs"]["global_index_json"] = index_result["json"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
