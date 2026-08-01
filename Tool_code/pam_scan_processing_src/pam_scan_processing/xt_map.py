from __future__ import annotations

import json
from pathlib import Path
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from .core import load_pam_file, resolve_slice


def _slug_number(value: float) -> str:
    text = f"{float(value):g}"
    return re.sub(r"[^0-9A-Za-z]+", "p", text).strip("p") or "0"


def write_x_time_absolute_map(
    path: Path,
    output_dir: Path,
    y: float,
    display_window: tuple[int, int] = (0, -1),
    baseline: tuple[int, int] = (0, 100),
    time_step: int = 1,
    clip_percentile: float = 99.5,
    coordinate_tolerance: float = 1e-9,
    x_range: tuple[float, float] | None = None,
) -> dict:
    """Plot |ADC - baseline| versus X and sample index for one Y scan line."""
    data, meta, points = load_pam_file(path)
    unique_y = sorted({round(float(point.y), 12) for point in points})
    actual_y = min(unique_y, key=lambda value: abs(value - float(y)))
    if abs(actual_y - float(y)) > coordinate_tolerance:
        available = ", ".join(f"{value:g}" for value in unique_y)
        raise ValueError(f"No Y row within {coordinate_tolerance:g} of {y:g}; available Y values: {available}")

    line_points = sorted(
        [point for point in points if round(float(point.y), 12) == actual_y],
        key=lambda point: (point.x, point.z, point.index),
    )
    if x_range is not None:
        x0, x1 = sorted((float(x_range[0]), float(x_range[1])))
        line_points = [point for point in line_points if x0 <= point.x <= x1]
    if not line_points:
        raise ValueError(f"No scan points found at y={actual_y:g} in X range {x_range}")
    if time_step < 1:
        raise ValueError("time_step must be at least 1")

    waveform_length = min(len(np.asarray(data[point.key]).ravel()) for point in line_points)
    b0, b1 = resolve_slice(baseline, waveform_length)
    d0, d1 = resolve_slice(display_window, waveform_length)
    samples = np.arange(d0, d1, time_step, dtype=int)
    waveforms = np.stack(
        [np.asarray(data[point.key], dtype=float).ravel()[:waveform_length] for point in line_points]
    )
    baseline_medians = np.median(waveforms[:, b0:b1], axis=1)
    absolute_amplitude = np.abs(waveforms - baseline_medians[:, None])[:, samples].T
    xs = np.array([point.x for point in line_points], dtype=float)
    color_limit = float(np.percentile(absolute_amplitude, clip_percentile))
    if not np.isfinite(color_limit) or color_limit <= 0:
        color_limit = float(np.max(absolute_amplitude)) if absolute_amplitude.size else 1.0
    color_limit = max(color_limit, 1e-12)

    plt.rcParams.update(
        {
            "font.size": 12,
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )
    fig, ax = plt.subplots(figsize=(15, 8.5))
    image = ax.pcolormesh(
        xs,
        samples,
        absolute_amplitude,
        shading="auto",
        cmap="magma",
        vmin=0.0,
        vmax=color_limit,
        rasterized=True,
    )
    ax.set_title(f"{path.name}\ny={actual_y:g} um：X-时间绝对幅值图")
    ax.set_xlabel("X 坐标 (um)")
    ax.set_ylabel("采样点序号")
    ax.set_ylim(d0, d1 - 1 if d1 > d0 else d1)
    colorbar = fig.colorbar(image, ax=ax, pad=0.02)
    colorbar.set_label("绝对幅值 |ADC - 基线中位数|")
    ax.grid(False)
    fig.tight_layout()

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{path.stem}_y{_slug_number(actual_y)}"
    if x_range is not None:
        stem += f"_x{_slug_number(xs.min())}to{_slug_number(xs.max())}"
    if display_window[0] != 0 or display_window[1] >= 0:
        stem += f"_s{d0}to{d1}"
    stem += "_x_time_absolute"
    figure_path = output_dir / f"{stem}.png"
    fig.savefig(figure_path, dpi=220, bbox_inches="tight")
    plt.close(fig)

    summary = {
        "file": path.name,
        "source_path": str(path),
        "requested_y_um": float(y),
        "actual_y_um": float(actual_y),
        "x_point_count": len(line_points),
        "x_range_um": [float(xs.min()), float(xs.max())],
        "requested_x_range": list(x_range) if x_range is not None else None,
        "display_window": [int(d0), int(d1)],
        "time_step": int(time_step),
        "baseline": [int(b0), int(b1)],
        "amplitude_definition": "abs(waveform - per-point baseline median)",
        "clip_percentile": float(clip_percentile),
        "color_limit": color_limit,
        "absolute_amplitude_min": float(np.min(absolute_amplitude)),
        "absolute_amplitude_max": float(np.max(absolute_amplitude)),
        "scan": meta,
        "figure": str(figure_path),
    }
    summary_path = output_dir / f"{stem}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["summary"] = str(summary_path)
    return summary
