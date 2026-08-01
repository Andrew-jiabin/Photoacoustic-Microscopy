from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import csv
import json
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import colors
from mpl_toolkits.mplot3d.art3d import Line3DCollection
import numpy as np
from scipy.io import loadmat
from scipy.ndimage import gaussian_filter1d

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


@dataclass(frozen=True)
class PamPoint:
    index: int
    key: str
    pos_text: str
    x: float
    y: float
    z: float
    display_y: float


@dataclass
class LineArrivalPrior:
    y_value: float
    point_keys: list[str]
    reference_waveform: np.ndarray
    reference_envelope: np.ndarray
    arrival_window: tuple[int, int]
    template_window: tuple[int, int]
    prior_arrival_sample: int
    prior_peak_sample: int
    reference_trace_count: int
    prior_quality: float
    noise_strength_anchor: float
    signal_strength_anchor: float
    shift_limit: int


def safe_key_from_pos(pos_text: str) -> str:
    clean = str(pos_text).strip()
    clean = clean.replace(" ", "")
    clean = clean.replace(".", "p")
    clean = clean.replace("-", "n")
    clean = clean.replace(",", "_")
    return "P_" + clean


def parse_pos_text(pos_text: str) -> tuple[float, float, float]:
    parts = [p.strip() for p in str(pos_text).strip().split(",") if p.strip()]
    values = [float(p) for p in parts]
    while len(values) < 3:
        values.append(0.0)
    return values[0], values[1], values[2]


def _meta_value(meta: object, name: str, default=None):
    if hasattr(meta, name):
        return getattr(meta, name)
    return default


def _scalar(value, default=None):
    if value is None:
        return default
    arr = np.asarray(value)
    if arr.size == 0:
        return default
    return arr.ravel()[0].item() if hasattr(arr.ravel()[0], "item") else arr.ravel()[0]


def _string_list(value) -> list[str]:
    arr = np.asarray(value).ravel()
    return [str(x).strip() for x in arr]


def load_pam_file(path: Path):
    data = loadmat(path, squeeze_me=True, struct_as_record=False)
    meta = data["metadata"]
    scan_shape = [int(x) for x in np.asarray(_meta_value(meta, "scan_shape")).ravel()]
    step_um = _scalar(_meta_value(meta, "step_um"), None)
    if step_um is None:
        step_um = _scalar(_meta_value(meta, "step_size"), None)
    coordinate_unit = _scalar(_meta_value(meta, "coordinate_unit"), None)
    pos_list = _string_list(_meta_value(meta, "pos_list"))

    coords = np.array([parse_pos_text(p) for p in pos_list], dtype=float)
    y_values = coords[:, 1] if len(coords) else np.array([0.0])
    unique_y = np.unique(np.round(y_values, 12))
    x_range = float(coords[:, 0].max() - coords[:, 0].min()) if len(coords) else 0.0
    y_range = float(y_values.max() - y_values.min()) if len(coords) else 0.0

    y_display_scale = 1.0
    if len(unique_y) > 1 and y_range > 0:
        desired_y_span = max(x_range * 0.35, y_range)
        y_display_scale = max(1.0, desired_y_span / y_range)

    points: list[PamPoint] = []
    for i, pos_text in enumerate(pos_list):
        x, y, z = parse_pos_text(pos_text)
        key = safe_key_from_pos(pos_text)
        if key not in data:
            continue
        points.append(
            PamPoint(
                index=i,
                key=key,
                pos_text=pos_text,
                x=x,
                y=y,
                z=z,
                display_y=(y - float(y_values.min())) * y_display_scale,
            )
        )

    return data, {
        "scan_shape": scan_shape,
        "step_um": float(step_um) if step_um is not None else None,
        "coordinate_unit": str(coordinate_unit).strip() if coordinate_unit is not None else None,
        "pos_count": len(pos_list),
        "valid_point_count": len(points),
        "x_range": [float(coords[:, 0].min()), float(coords[:, 0].max())] if len(coords) else [0.0, 0.0],
        "y_range": [float(coords[:, 1].min()), float(coords[:, 1].max())] if len(coords) else [0.0, 0.0],
        "z_range": [float(coords[:, 2].min()), float(coords[:, 2].max())] if len(coords) else [0.0, 0.0],
        "unique_y_count": int(len(unique_y)),
        "y_display_scale": float(y_display_scale),
    }, points


def parse_slice(text: str, default_start: int, default_stop: int) -> tuple[int, int]:
    if not text:
        return default_start, default_stop
    if ":" not in text:
        raise ValueError(f"Expected START:STOP, got {text!r}")
    start_s, stop_s = text.split(":", 1)
    start = default_start if not start_s.strip() else int(start_s)
    stop_text = stop_s.strip().lower()
    stop = default_stop if stop_text in ("", "end", "all") else int(stop_text)
    return start, stop


def resolve_slice(window: tuple[int, int], length: int) -> tuple[int, int]:
    start = max(0, int(window[0]))
    stop = length if int(window[1]) < 0 else min(length, int(window[1]))
    if stop <= start:
        raise ValueError(f"slice {window!r} is empty for length {length}")
    return start, stop


def _robust_sigma(values: np.ndarray, axis=None) -> np.ndarray:
    values = np.asarray(values, dtype=float)
    median = np.median(values, axis=axis, keepdims=True)
    sigma = 1.4826 * np.median(np.abs(values - median), axis=axis)
    fallback = np.std(values, axis=axis)
    return np.where(sigma > 1e-9, sigma, np.maximum(fallback, 1e-9))


def _normalized_correlation(left: np.ndarray, right: np.ndarray) -> float:
    left = np.asarray(left, dtype=float) - float(np.mean(left))
    right = np.asarray(right, dtype=float) - float(np.mean(right))
    denom = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denom <= 1e-12:
        return 0.0
    return float(np.dot(left, right) / denom)


def template_correlation_curve(waveform, prior: LineArrivalPrior) -> tuple[np.ndarray, np.ndarray]:
    waveform = np.asarray(waveform, dtype=float).ravel()
    t0, t1 = prior.template_window
    template = prior.reference_waveform[t0:t1]
    shifts = np.arange(-prior.shift_limit, prior.shift_limit + 1, dtype=int)
    correlations = np.full(len(shifts), np.nan, dtype=float)
    for idx, shift in enumerate(shifts):
        start, stop = t0 + int(shift), t1 + int(shift)
        if start < 0 or stop > len(waveform):
            continue
        correlations[idx] = _normalized_correlation(template, waveform[start:stop])
    return shifts, correlations


def analyze_arrivals_by_line(
    data: dict,
    points: list[PamPoint],
    baseline: tuple[int, int] = (0, 100),
    arrival_window: tuple[int, int] = (100, 700),
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    min_confidence: float = 0.6,
    reference_fraction: float = 0.25,
    shift_limit: int = 35,
) -> tuple[dict[str, dict], dict[float, LineArrivalPrior]]:
    """Detect first arrivals using one shared waveform prior per Y scan line."""
    groups: dict[float, list[PamPoint]] = {}
    for point in points:
        groups.setdefault(round(float(point.y), 12), []).append(point)

    results: dict[str, dict] = {}
    priors: dict[float, LineArrivalPrior] = {}
    for y_value, line_points in sorted(groups.items()):
        line_points = sorted(line_points, key=lambda point: (point.x, point.z, point.index))
        line_length = min(len(np.asarray(data[point.key]).ravel()) for point in line_points)
        b0, b1 = resolve_slice(baseline, line_length)
        a0, a1 = resolve_slice(arrival_window, line_length)
        if a0 < b1:
            a0 = b1
        if a1 <= a0:
            raise ValueError("arrival window must start after the baseline and contain samples")

        waveforms = np.stack(
            [np.asarray(data[point.key], dtype=float).ravel()[:line_length] for point in line_points]
        )
        baseline_medians = np.median(waveforms[:, b0:b1], axis=1)
        centered = waveforms - baseline_medians[:, None]
        noise_sigma = _robust_sigma(centered[:, b0:b1], axis=1)
        envelopes = gaussian_filter1d(np.abs(centered), smooth_sigma, axis=1)
        strengths = np.max(envelopes[:, a0:a1], axis=1) / np.maximum(noise_sigma, 1e-9)

        reference_count = min(
            len(line_points),
            max(5, int(math.ceil(len(line_points) * max(0.05, min(1.0, reference_fraction))))),
        )
        reference_indices = np.argsort(strengths)[-reference_count:]
        reference_waveform = np.median(centered[reference_indices], axis=0)
        reference_envelope = gaussian_filter1d(np.abs(reference_waveform), smooth_sigma)
        prior_peak = int(a0 + np.argmax(reference_envelope[a0:a1]))
        baseline_env = reference_envelope[b0:b1]
        threshold = float(np.median(baseline_env) + threshold_sigma * _robust_sigma(baseline_env))
        left = prior_peak
        while left > a0 and reference_envelope[left] > threshold:
            left -= 1
        prior_arrival = int(left + 1 if left < prior_peak else prior_peak)

        template_start = max(a0, prior_arrival - 10, shift_limit)
        template_stop = min(a1, prior_peak + 220, line_length - shift_limit)
        if template_stop - template_start < 40:
            template_start = max(a0, prior_peak - 30, shift_limit)
            template_stop = min(a1, prior_peak + 80, line_length - shift_limit)

        reference_strength = float(np.median(strengths[reference_indices]))
        prior_quality = float(np.clip((reference_strength - 3.0) / 9.0, 0.0, 1.0))
        noise_anchor, signal_anchor = np.percentile(strengths, [15, 75]).astype(float)
        prior = LineArrivalPrior(
            y_value=float(y_value),
            point_keys=[point.key for point in line_points],
            reference_waveform=reference_waveform,
            reference_envelope=reference_envelope,
            arrival_window=(a0, a1),
            template_window=(template_start, template_stop),
            prior_arrival_sample=prior_arrival,
            prior_peak_sample=prior_peak,
            reference_trace_count=reference_count,
            prior_quality=prior_quality,
            noise_strength_anchor=float(noise_anchor),
            signal_strength_anchor=float(signal_anchor),
            shift_limit=int(shift_limit),
        )
        priors[y_value] = prior

        for row_idx, point in enumerate(line_points):
            pointwise = detect_arrival(
                waveforms[row_idx],
                baseline=(b0, b1),
                target_window=(a0, a1),
                smooth_sigma=smooth_sigma,
                threshold_sigma=threshold_sigma,
                min_confidence=min_confidence,
            )
            shifts, correlations = template_correlation_curve(centered[row_idx], prior)
            finite = np.isfinite(correlations)
            if finite.any():
                best_curve_idx = int(np.nanargmax(correlations))
                best_shift = int(shifts[best_curve_idx])
                correlation = float(correlations[best_curve_idx])
            else:
                best_shift = 0
                correlation = 0.0

            candidate_arrival = int(np.clip(prior_arrival + best_shift, a0, a1 - 1))
            candidate_peak = int(np.clip(prior_peak + best_shift, a0, a1 - 1))
            event_start = max(a0, candidate_peak - 30)
            event_stop = min(a1, candidate_peak + 31)
            peak_abs = float(np.max(envelopes[row_idx, event_start:event_stop]))
            event_strength = peak_abs / max(float(noise_sigma[row_idx]), 1e-9)

            strength_score = float(np.clip((event_strength - 3.0) / 9.0, 0.0, 1.0))
            correlation_score = float(np.clip((correlation - 0.25) / 0.65, 0.0, 1.0))
            temporal_score = float(np.exp(-0.5 * (best_shift / max(1.0, 0.75 * shift_limit)) ** 2))
            confidence = float(
                np.clip(prior_quality * math.sqrt(strength_score * correlation_score) * temporal_score, 0.0, 1.0)
            )
            detected = bool(confidence >= min_confidence)

            point_baseline_env = envelopes[row_idx, b0:b1]
            point_threshold = float(
                np.median(point_baseline_env) + threshold_sigma * _robust_sigma(point_baseline_env)
            )
            results[point.key] = {
                "arrival_sample": candidate_arrival if detected else -1,
                "onset_sample": candidate_arrival if detected else -1,
                "peak_sample": candidate_peak if detected else -1,
                "candidate_arrival_sample": candidate_arrival,
                "candidate_peak_sample": candidate_peak,
                "detected": detected,
                "confidence": confidence,
                "baseline_median": float(baseline_medians[row_idx]),
                "noise_std": float(noise_sigma[row_idx]),
                "threshold": point_threshold,
                "peak_abs_adc": peak_abs,
                "window_peak_to_peak": float(np.ptp(centered[row_idx, a0:a1])),
                "event_strength_sigma": float(event_strength),
                "template_correlation": correlation,
                "arrival_shift": best_shift,
                "line_prior_arrival_sample": prior_arrival,
                "line_prior_peak_sample": prior_peak,
                "line_prior_quality": prior_quality,
                "line_reference_trace_count": reference_count,
                "pointwise_arrival_sample": pointwise["arrival_sample"] if pointwise["detected"] else -1,
                "pointwise_candidate_arrival_sample": pointwise["arrival_sample"],
                "pointwise_peak_sample": pointwise["peak_sample"],
                "pointwise_detected": bool(pointwise["detected"]),
                "pointwise_confidence": float(pointwise["confidence"]),
                "pointwise_event_strength_sigma": float(pointwise["event_strength_sigma"]),
                "pointwise_threshold": float(pointwise["threshold"]),
            }

    return results, priors


def detect_arrival(
    waveform,
    baseline: tuple[int, int] = (0, 100),
    target_window: tuple[int, int] = (100, 700),
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    min_confidence: float = 0.6,
) -> dict:
    w = np.asarray(waveform, dtype=float).ravel()
    n = len(w)
    b0, b1 = max(0, baseline[0]), min(n, baseline[1])
    t0, t1 = max(0, target_window[0]), min(n, target_window[1])
    if b1 <= b0:
        raise ValueError("baseline slice is empty")
    if t1 <= t0:
        raise ValueError("target window is empty")

    baseline_values = w[b0:b1]
    baseline_median = float(np.median(baseline_values))
    centered = w - baseline_median
    envelope = gaussian_filter1d(np.abs(centered), smooth_sigma)
    baseline_env = envelope[b0:b1]
    noise_sigma = max(float(_robust_sigma(baseline_values)), 1e-9)
    threshold = float(np.median(baseline_env) + threshold_sigma * _robust_sigma(baseline_env))

    peak_sample = int(t0 + np.argmax(envelope[t0:t1]))
    left = peak_sample
    while left > t0 and envelope[left] > threshold:
        left -= 1
    onset_sample = int(left + 1 if left < peak_sample else peak_sample)

    peak_abs = float(envelope[peak_sample])
    threshold_span = max(threshold - float(np.median(baseline_env)), 1e-12)
    confidence = max(0.0, min(1.0, (peak_abs - threshold) / (4.0 * threshold_span) + 0.5))
    if not math.isfinite(confidence):
        confidence = 0.0
    detected = bool(confidence >= float(min_confidence))

    return {
        "arrival_sample": onset_sample,
        "onset_sample": onset_sample,
        "peak_sample": peak_sample,
        "confidence": float(confidence),
        "detected": detected,
        "baseline_median": baseline_median,
        "noise_std": noise_sigma,
        "event_strength_sigma": float(peak_abs / noise_sigma),
        "threshold": threshold,
        "peak_abs_adc": peak_abs,
        "window_peak_to_peak": float(np.max(centered[t0:t1]) - np.min(centered[t0:t1])),
        "centered": centered,
    }


def select_indices(count: int, max_count: int) -> np.ndarray:
    if max_count <= 0:
        return np.arange(count, dtype=int)
    if count <= max_count:
        return np.arange(count, dtype=int)
    return np.unique(np.linspace(0, count - 1, max_count, dtype=int))


def _axis_setup(ax, title: str, meta: dict):
    ax.set_title(title, pad=14)
    ax.set_xlabel("X 坐标 (um)")
    y_label = "Y 坐标（显示值）"
    if meta.get("y_display_scale", 1.0) != 1.0:
        y_label += f"；仅为显示放大 x{meta['y_display_scale']:.1f}"
    ax.set_ylabel(y_label)
    ax.set_zlabel("采样点序号")
    ax.view_init(elev=25, azim=-62)
    try:
        ax.set_box_aspect((1.7, 0.65 if meta.get("unique_y_count", 1) > 1 else 0.25, 1.1))
    except Exception:
        pass


def plot_waveform_3d(
    path: Path,
    data: dict,
    meta: dict,
    points: list[PamPoint],
    display_window: tuple[int, int],
    baseline: tuple[int, int],
    time_step: int,
    max_traces: int,
):
    chosen = select_indices(len(points), max_traces)
    waveform_length = max(len(np.asarray(data[point.key]).ravel()) for point in points)
    t0, t1 = resolve_slice(display_window, waveform_length)
    sample_idx = np.arange(t0, t1, time_step, dtype=int)

    segments = []
    values = []
    for idx in chosen:
        point = points[int(idx)]
        w = np.asarray(data[point.key], dtype=float).ravel()
        b0, b1 = baseline
        centered = w - np.median(w[b0:b1])
        y = point.display_y
        x = point.x
        z = sample_idx[sample_idx < len(centered)]
        if len(z) < 2:
            continue
        coords = np.column_stack([np.full_like(z, x, dtype=float), np.full_like(z, y, dtype=float), z.astype(float)])
        segments.extend(np.stack([coords[:-1], coords[1:]], axis=1))
        values.extend(((centered[z[:-1]] + centered[z[1:]]) / 2.0).tolist())

    fig = plt.figure(figsize=(15, 10))
    ax = fig.add_subplot(111, projection="3d")
    values_arr = np.asarray(values, dtype=float)
    limit = float(np.nanpercentile(np.abs(values_arr), 98)) if values_arr.size else 1.0
    norm = colors.TwoSlopeNorm(vmin=-limit, vcenter=0.0, vmax=limit)
    collection = Line3DCollection(segments, cmap="coolwarm", norm=norm, linewidths=0.75, alpha=0.9)
    collection.set_array(values_arr)
    ax.add_collection3d(collection)

    xs = np.array([points[int(i)].x for i in chosen])
    ys = np.array([points[int(i)].display_y for i in chosen])
    ax.set_xlim(float(xs.min()), float(xs.max()))
    if len(np.unique(ys)) == 1:
        ax.set_ylim(float(ys.min()) - 0.1, float(ys.max()) + 0.1)
    else:
        ax.set_ylim(float(ys.min()), float(ys.max()))
    ax.set_zlim(float(t0), float(t1))
    _axis_setup(ax, f"{path.name}：3D 时域波形（{len(chosen)}/{len(points)} 条空间时间线）", meta)
    cbar = fig.colorbar(collection, ax=ax, pad=0.08, shrink=0.72)
    cbar.set_label("去基线 ADC 振幅")
    fig.tight_layout()
    return fig


def plot_arrival_3d(
    path: Path,
    meta: dict,
    points: list[PamPoint],
    rows: list[dict],
    display_window: tuple[int, int],
    waveform_length: int,
):
    candidate_arrivals = np.array([r["candidate_arrival_sample"] for r in rows], dtype=float)
    detected = np.array([bool(r["detected"]) for r in rows], dtype=bool)
    pointwise_arrivals = np.array([r["pointwise_candidate_arrival_sample"] for r in rows], dtype=float)
    pointwise_detected = np.array([bool(r["pointwise_detected"]) for r in rows], dtype=bool)
    t0, t1 = resolve_slice(display_window, waveform_length)
    xs = np.array([p.x for p in points])
    ys = np.array([p.display_y for p in points])
    conf = np.array([r["confidence"] for r in rows], dtype=float)

    fig = plt.figure(figsize=(15, 10))
    ax = fig.add_subplot(111, projection="3d")
    line_segments = [
        [[float(p.x), float(p.display_y), float(t0)], [float(p.x), float(p.display_y), float(t1)]]
        for p in points
    ]
    ax.add_collection3d(Line3DCollection(line_segments, colors="#B8B8B8", linewidths=0.28, alpha=0.23))
    sizes = 14 + 42 * np.clip(conf[detected], 0.0, 1.0)
    scatter = ax.scatter(
        xs[detected],
        ys[detected],
        candidate_arrivals[detected],
        c=candidate_arrivals[detected],
        s=sizes,
        cmap="viridis",
        edgecolors="#202020",
        linewidths=0.25,
        label="全局先验支持的首次到达",
    )
    if (~detected).any():
        ax.scatter(
            xs[~detected],
            ys[~detected],
            candidate_arrivals[~detected],
            c="#8A8A8A",
            marker="x",
            s=12,
            alpha=0.45,
            label="低可信候选（未检出）",
        )

    ax.scatter(
        xs[pointwise_detected],
        ys[pointwise_detected],
        pointwise_arrivals[pointwise_detected],
        c="#F57C00",
        marker="^",
        s=22,
        alpha=0.82,
        edgecolors="#5D2600",
        linewidths=0.3,
        label="单点内部估计的首次到达",
    )
    if (~pointwise_detected).any():
        ax.scatter(
            xs[~pointwise_detected],
            ys[~pointwise_detected],
            pointwise_arrivals[~pointwise_detected],
            c="#D6A15C",
            marker="+",
            s=10,
            alpha=0.28,
            label="单点估计低可信（审查用）",
        )

    ax.set_xlim(float(xs.min()), float(xs.max()))
    if len(np.unique(ys)) == 1:
        ax.set_ylim(float(ys.min()) - 0.1, float(ys.max()) + 0.1)
    else:
        ax.set_ylim(float(ys.min()), float(ys.max()))
    ax.set_zlim(float(t0), float(t1))
    _axis_setup(
        ax,
        f"{path.name}：全局先验与单点首次到达（全局 {int(detected.sum())}/{len(points)}；单点 {int(pointwise_detected.sum())}/{len(points)}）",
        meta,
    )
    color_source = scatter
    if not detected.any():
        color_source = plt.cm.ScalarMappable(norm=colors.Normalize(vmin=t0, vmax=t1), cmap="viridis")
    cbar = fig.colorbar(color_source, ax=ax, pad=0.08, shrink=0.72)
    cbar.set_label("首次到达采样点")
    ax.legend(loc="upper left")
    fig.tight_layout()
    return fig


def process_file(
    path: Path,
    output_dir: Path,
    arrival_window: tuple[int, int],
    display_window: tuple[int, int],
    baseline: tuple[int, int],
    time_step: int,
    max_traces: int,
    smooth_sigma: float,
    threshold_sigma: float,
    min_confidence: float,
):
    data, meta, points = load_pam_file(path)
    detections, priors = analyze_arrivals_by_line(
        data,
        points,
        baseline=baseline,
        arrival_window=arrival_window,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
        min_confidence=min_confidence,
    )
    rows: list[dict] = []
    for point in points:
        result = detections[point.key]
        row = {
            "file": path.name,
            "point_index": point.index,
            "key": point.key,
            "pos_text": point.pos_text,
            "x_um": point.x,
            "y_um": point.y,
            "z_um": point.z,
            "display_y": point.display_y,
            "arrival_sample": result["arrival_sample"],
            "onset_sample": result["onset_sample"],
            "peak_sample": result["peak_sample"],
            "candidate_arrival_sample": result["candidate_arrival_sample"],
            "candidate_peak_sample": result["candidate_peak_sample"],
            "pointwise_arrival_sample": result["pointwise_arrival_sample"],
            "pointwise_candidate_arrival_sample": result["pointwise_candidate_arrival_sample"],
            "pointwise_peak_sample": result["pointwise_peak_sample"],
            "pointwise_detected": result["pointwise_detected"],
            "pointwise_confidence": result["pointwise_confidence"],
            "pointwise_event_strength_sigma": result["pointwise_event_strength_sigma"],
            "pointwise_threshold": result["pointwise_threshold"],
            "detected": result["detected"],
            "confidence": result["confidence"],
            "baseline_median": result["baseline_median"],
            "noise_std": result["noise_std"],
            "threshold": result["threshold"],
            "peak_abs_adc": result["peak_abs_adc"],
            "window_peak_to_peak": result["window_peak_to_peak"],
            "event_strength_sigma": result["event_strength_sigma"],
            "template_correlation": result["template_correlation"],
            "arrival_shift": result["arrival_shift"],
            "line_prior_arrival_sample": result["line_prior_arrival_sample"],
            "line_prior_peak_sample": result["line_prior_peak_sample"],
            "line_prior_quality": result["line_prior_quality"],
            "line_reference_trace_count": result["line_reference_trace_count"],
        }
        rows.append(row)

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    table_path = output_dir / f"{stem}_arrival_table.csv"
    with table_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    waveform_fig = plot_waveform_3d(path, data, meta, points, display_window, baseline, time_step, max_traces)
    waveform_path = output_dir / f"{stem}_waveform_3d.png"
    waveform_fig.savefig(waveform_path, dpi=220, bbox_inches="tight")
    plt.close(waveform_fig)

    waveform_length = max(len(np.asarray(data[point.key]).ravel()) for point in points)
    arrival_fig = plot_arrival_3d(path, meta, points, rows, display_window, waveform_length)
    arrival_path = output_dir / f"{stem}_arrival_3d.png"
    arrival_fig.savefig(arrival_path, dpi=220, bbox_inches="tight")
    plt.close(arrival_fig)

    detected_rows = [row for row in rows if row["detected"]]
    arrivals = np.array([r["arrival_sample"] for r in detected_rows], dtype=float)
    peaks = np.array([r["peak_sample"] for r in detected_rows], dtype=float)
    pointwise_rows = [row for row in rows if row["pointwise_detected"]]
    pointwise_arrivals = np.array([r["pointwise_arrival_sample"] for r in pointwise_rows], dtype=float)
    prior_summaries = [
        {
            "y_um": prior.y_value,
            "arrival_window": list(prior.arrival_window),
            "template_window": list(prior.template_window),
            "prior_arrival_sample": prior.prior_arrival_sample,
            "prior_peak_sample": prior.prior_peak_sample,
            "reference_trace_count": prior.reference_trace_count,
            "prior_quality": prior.prior_quality,
            "noise_strength_anchor": prior.noise_strength_anchor,
            "signal_strength_anchor": prior.signal_strength_anchor,
        }
        for prior in priors.values()
    ]
    summary = {
        "file": path.name,
        "source_path": str(path),
        "scan": meta,
        "detection": {
            "baseline": list(baseline),
            "method": "line-global template prior",
            "additional_method": "independent pointwise envelope threshold and backtracking",
            "arrival_window": list(arrival_window),
            "display_window": list(display_window),
            "smooth_sigma": smooth_sigma,
            "threshold_sigma": threshold_sigma,
            "min_confidence": min_confidence,
            "detected_count": len(detected_rows),
            "detected_fraction": len(detected_rows) / max(1, len(rows)),
            "arrival_sample_percentiles": np.percentile(arrivals, [0, 10, 50, 90, 100]).round(3).tolist() if arrivals.size else [],
            "peak_sample_percentiles": np.percentile(peaks, [0, 10, 50, 90, 100]).round(3).tolist() if peaks.size else [],
            "pointwise_detected_count": len(pointwise_rows),
            "pointwise_detected_fraction": len(pointwise_rows) / max(1, len(rows)),
            "pointwise_arrival_sample_percentiles": np.percentile(pointwise_arrivals, [0, 10, 50, 90, 100]).round(3).tolist() if pointwise_arrivals.size else [],
            "line_priors": prior_summaries,
        },
        "outputs": {
            "arrival_table": str(table_path),
            "waveform_3d": str(waveform_path),
            "arrival_3d": str(arrival_path),
        },
    }
    summary_path = output_dir / f"{stem}_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def process_directory(
    input_dir: Path,
    output_dir: Path,
    arrival_window: tuple[int, int] = (100, 700),
    display_window: tuple[int, int] = (0, -1),
    baseline: tuple[int, int] = (0, 100),
    time_step: int = 4,
    max_traces: int = 700,
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    min_confidence: float = 0.6,
):
    mat_files = sorted(input_dir.glob("*.mat"))
    if not mat_files:
        raise FileNotFoundError(f"No .mat files found in {input_dir}")
    summaries = []
    for path in mat_files:
        print(f"Processing {path.name} ...")
        summaries.append(
            process_file(
                path=path,
                output_dir=output_dir,
                arrival_window=arrival_window,
                display_window=display_window,
                baseline=baseline,
                time_step=time_step,
                max_traces=max_traces,
                smooth_sigma=smooth_sigma,
                threshold_sigma=threshold_sigma,
                min_confidence=min_confidence,
            )
        )
    manifest = output_dir / "batch_summary.json"
    manifest.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    return summaries
