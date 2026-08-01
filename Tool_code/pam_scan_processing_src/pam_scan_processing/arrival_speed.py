from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import linregress, theilslopes

from .core import analyze_arrivals_by_line, load_pam_file, template_correlation_curve


def _slug_number(value: float) -> str:
    text = f"{float(value):g}"
    return re.sub(r"[^0-9A-Za-z]+", "p", text).strip("p") or "0"


def coordinate_scale_to_m(unit: str | None) -> float:
    normalized = (unit or "").strip().lower().replace("μ", "u").replace("µ", "u")
    scales = {"m": 1.0, "mm": 1e-3, "um": 1e-6, "nm": 1e-9}
    if normalized not in scales:
        raise ValueError(f"Unsupported or missing coordinate unit {unit!r}; expected m, mm, um, or nm")
    return scales[normalized]


def _subsample_template_shift(waveform: np.ndarray, prior) -> tuple[float, float, int]:
    shifts, correlations = template_correlation_curve(waveform, prior)
    peak_index = int(np.nanargmax(correlations))
    integer_shift = int(shifts[peak_index])
    delta = 0.0
    if 0 < peak_index < len(correlations) - 1:
        y_minus, y_zero, y_plus = correlations[peak_index - 1 : peak_index + 2]
        denominator = float(y_minus - 2.0 * y_zero + y_plus)
        if np.isfinite([y_minus, y_zero, y_plus]).all() and abs(denominator) > 1e-12:
            delta = float(np.clip(0.5 * (y_minus - y_plus) / denominator, -1.0, 1.0))
    return integer_shift + delta, float(correlations[peak_index]), integer_shift


def _speed_from_slope(slope_samples_per_unit: float, sample_rate_hz: float, coordinate_scale_m: float) -> float | None:
    if not math.isfinite(slope_samples_per_unit) or abs(slope_samples_per_unit) <= 1e-12:
        return None
    return float(sample_rate_hz * coordinate_scale_m / abs(slope_samples_per_unit))


def linear_speed_fit(
    x: np.ndarray,
    arrival_samples: np.ndarray,
    sample_rate_hz: float,
    coordinate_scale_m: float,
) -> dict:
    x = np.asarray(x, dtype=float)
    arrival_samples = np.asarray(arrival_samples, dtype=float)
    finite = np.isfinite(x) & np.isfinite(arrival_samples)
    x, arrival_samples = x[finite], arrival_samples[finite]
    if len(x) < 3 or float(np.ptp(x)) <= 0:
        return {"n": int(len(x)), "valid": False}

    fit = linregress(x, arrival_samples)
    predicted = fit.intercept + fit.slope * x
    residuals = arrival_samples - predicted
    slope_ci = [
        float(fit.slope - 1.96 * fit.stderr),
        float(fit.slope + 1.96 * fit.stderr),
    ]
    speed_ci = None
    if slope_ci[0] * slope_ci[1] > 0:
        speeds = [
            _speed_from_slope(slope_ci[0], sample_rate_hz, coordinate_scale_m),
            _speed_from_slope(slope_ci[1], sample_rate_hz, coordinate_scale_m),
        ]
        if all(speed is not None for speed in speeds):
            speed_ci = [float(min(speeds)), float(max(speeds))]

    return {
        "n": int(len(x)),
        "valid": True,
        "x_min": float(np.min(x)),
        "x_max": float(np.max(x)),
        "slope_samples_per_coordinate_unit": float(fit.slope),
        "slope_time_ns_per_coordinate_unit": float(fit.slope / sample_rate_hz * 1e9),
        "slope_stderr": float(fit.stderr),
        "slope_ci95_samples_per_coordinate_unit": slope_ci,
        "intercept_sample": float(fit.intercept),
        "r_squared": float(fit.rvalue**2),
        "p_value": float(fit.pvalue),
        "residual_rmse_samples": float(np.sqrt(np.mean(residuals**2))),
        "apparent_speed_m_per_s": _speed_from_slope(fit.slope, sample_rate_hz, coordinate_scale_m),
        "apparent_speed_ci95_m_per_s": speed_ci,
    }


def _theil_sen_fit(
    x: np.ndarray,
    arrival_samples: np.ndarray,
    sample_rate_hz: float,
    coordinate_scale_m: float,
) -> dict:
    x = np.asarray(x, dtype=float)
    arrival_samples = np.asarray(arrival_samples, dtype=float)
    finite = np.isfinite(x) & np.isfinite(arrival_samples)
    x, arrival_samples = x[finite], arrival_samples[finite]
    if len(x) < 3 or float(np.ptp(x)) <= 0:
        return {"n": int(len(x)), "valid": False}
    fit = theilslopes(arrival_samples, x, 0.95)
    return {
        "n": int(len(x)),
        "valid": True,
        "slope_samples_per_coordinate_unit": float(fit.slope),
        "intercept_sample": float(fit.intercept),
        "slope_ci95_samples_per_coordinate_unit": [float(fit.low_slope), float(fit.high_slope)],
        "apparent_speed_m_per_s": _speed_from_slope(fit.slope, sample_rate_hz, coordinate_scale_m),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _plot_selected_line(
    path: Path,
    output_path: Path,
    rows: list[dict],
    all_fit: dict,
    detected_fit: dict,
    coordinate_unit: str,
    y_value: float,
) -> None:
    plt.rcParams.update(
        {
            "font.size": 11,
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )
    x = np.array([row["x"] for row in rows], dtype=float)
    arrival = np.array([row["arrival_subsample"] for row in rows], dtype=float)
    confidence = np.array([row["confidence"] for row in rows], dtype=float)
    detected = np.array([row["detected"] for row in rows], dtype=bool)
    strength = np.array([row["event_strength_sigma"] for row in rows], dtype=float)

    fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True)
    ax = axes[0]
    scatter = ax.scatter(
        x[detected],
        arrival[detected],
        c=confidence[detected],
        cmap="viridis",
        vmin=0,
        vmax=1,
        s=24,
        edgecolors="#202020",
        linewidths=0.25,
        label="可信到达",
    )
    ax.scatter(x[~detected], arrival[~detected], c="#858585", marker="x", s=24, alpha=0.7, label="低可信候选")
    fit_x = np.linspace(float(x.min()), float(x.max()), 400)
    if all_fit.get("valid"):
        fit_y = all_fit["intercept_sample"] + all_fit["slope_samples_per_coordinate_unit"] * fit_x
        ax.plot(fit_x, fit_y, color="#111111", lw=1.8, label="全部候选 OLS")
    if detected_fit.get("valid"):
        fit_y = detected_fit["intercept_sample"] + detected_fit["slope_samples_per_coordinate_unit"] * fit_x
        ax.plot(fit_x, fit_y, color="#E57A19", lw=1.8, ls="--", label="仅可信点 OLS")
    speed = all_fit.get("apparent_speed_m_per_s")
    ax.set_title(
        f"{path.name}｜y={y_value:g} {coordinate_unit}｜候选表观速度={speed:.1f} m/s，"
        f"R²={all_fit.get('r_squared', float('nan')):.3f}"
        if speed is not None
        else f"{path.name}｜y={y_value:g} {coordinate_unit}｜斜率接近 0，无法换算速度"
    )
    ax.set_ylabel("亚采样模板到达位置")
    ax.grid(alpha=0.25)
    ax.legend(loc="best")
    fig.colorbar(scatter, ax=ax, pad=0.01, label="全局先验可信度")

    ax = axes[1]
    if all_fit.get("valid"):
        predicted = all_fit["intercept_sample"] + all_fit["slope_samples_per_coordinate_unit"] * x
        residuals = arrival - predicted
        ax.scatter(x[detected], residuals[detected], c="#1F8A70", s=18, label="可信点残差")
        ax.scatter(x[~detected], residuals[~detected], c="#858585", marker="x", s=20, alpha=0.7, label="低可信候选残差")
    ax.axhline(0.0, color="#333333", lw=1.0)
    ax.set_ylabel("全部候选拟合残差（采样点）")
    ax.grid(alpha=0.25)
    ax.legend(loc="best")

    ax = axes[2]
    ax.plot(x, confidence, color="#2A6FBB", lw=1.5, label="全局先验可信度")
    ax.axhline(0.6, color="#777777", lw=1.0, ls="--", label="默认可信阈值 0.6")
    ax.set_ylim(-0.03, 1.05)
    ax.set_ylabel("可信度")
    ax.set_xlabel(f"X 坐标 ({coordinate_unit})")
    ax.grid(alpha=0.25)
    strength_ax = ax.twinx()
    strength_ax.plot(x, strength, color="#B43C3C", lw=1.0, alpha=0.75, label="事件强度/噪声")
    strength_ax.set_ylabel("事件强度/噪声 (σ)")
    handles, labels = ax.get_legend_handles_labels()
    handles2, labels2 = strength_ax.get_legend_handles_labels()
    ax.legend(handles + handles2, labels + labels2, loc="best")

    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def _plot_x_time_overlay(
    path: Path,
    output_path: Path,
    data,
    line_points,
    rows: list[dict],
    all_fit: dict,
    baseline: tuple[int, int],
    arrival_window: tuple[int, int],
    coordinate_unit: str,
    y_value: float,
) -> None:
    x = np.array([point.x for point in line_points], dtype=float)
    waveform_length = min(len(np.asarray(data[point.key]).ravel()) for point in line_points)
    b0, b1 = max(0, baseline[0]), min(waveform_length, baseline[1])
    waveforms = np.stack([np.asarray(data[point.key], dtype=float).ravel()[:waveform_length] for point in line_points])
    centered = waveforms - np.median(waveforms[:, b0:b1], axis=1, keepdims=True)
    arrival = np.array([row["arrival_subsample"] for row in rows], dtype=float)
    detected = np.array([row["detected"] for row in rows], dtype=bool)
    start = max(arrival_window[0], int(np.floor(np.min(arrival))) - 35)
    stop = min(arrival_window[1], int(np.ceil(max(row["line_prior_peak_sample"] for row in rows))) + 45)
    samples = np.arange(start, stop, dtype=int)
    amplitude = np.abs(centered[:, samples]).T
    color_limit = float(np.percentile(amplitude, 99.5))

    plt.rcParams.update(
        {
            "font.size": 11,
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )
    fig, ax = plt.subplots(figsize=(15, 8.5))
    image = ax.pcolormesh(x, samples, amplitude, shading="auto", cmap="magma", vmin=0, vmax=color_limit, rasterized=True)
    ax.scatter(x[detected], arrival[detected], c="#45E6D1", s=12, edgecolors="#101010", linewidths=0.2, label="可信到达")
    ax.scatter(x[~detected], arrival[~detected], c="#D0D0D0", marker="x", s=18, label="低可信候选")
    if all_fit.get("valid"):
        fit_x = np.linspace(float(x.min()), float(x.max()), 400)
        fit_y = all_fit["intercept_sample"] + all_fit["slope_samples_per_coordinate_unit"] * fit_x
        ax.plot(fit_x, fit_y, color="#00E5FF", lw=1.6, label="全部候选线性拟合")
    ax.set_title(f"{path.name}｜y={y_value:g} {coordinate_unit}｜X-时间绝对幅值与到达候选")
    ax.set_xlabel(f"X 坐标 ({coordinate_unit})")
    ax.set_ylabel("采样点序号")
    ax.legend(loc="upper right")
    colorbar = fig.colorbar(image, ax=ax, pad=0.02)
    colorbar.set_label("绝对幅值 |ADC - 基线中位数|")
    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def _plot_all_y_comparison(path: Path, output_path: Path, summaries: list[dict], coordinate_unit: str) -> None:
    ys = np.array([row["y"] for row in summaries], dtype=float)
    speeds = np.array([row["all_candidate_apparent_speed_m_per_s"] for row in summaries], dtype=float)
    r2_all = np.array([row["all_candidate_r_squared"] for row in summaries], dtype=float)
    r2_detected = np.array([row["detected_only_r_squared"] for row in summaries], dtype=float)
    valid_fraction = np.array([row["detected_fraction"] for row in summaries], dtype=float)

    fig, axes = plt.subplots(1, 3, figsize=(16, 5.2))
    axes[0].plot(ys, speeds, "o-", color="#2A6FBB")
    axes[0].set_ylabel("全部候选表观速度 (m/s)")
    axes[1].plot(ys, r2_all, "o-", color="#1F8A70", label="全部候选")
    axes[1].plot(ys, r2_detected, "s--", color="#E57A19", label="仅可信点")
    axes[1].set_ylabel("线性拟合 R²")
    axes[1].legend()
    axes[2].plot(ys, valid_fraction, "o-", color="#7B4AB5")
    axes[2].set_ylabel("可信点比例")
    axes[2].set_ylim(0, 1.05)
    for ax in axes:
        ax.set_xlabel(f"Y 坐标 ({coordinate_unit})")
        ax.grid(alpha=0.25)
    fig.suptitle(f"{path.name}：各 Y 扫描线的候选速度与线性一致性", fontsize=14)
    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def analyze_arrival_speed(
    path: Path,
    output_dir: Path,
    sample_rate_hz: float,
    y: float | None = None,
    baseline: tuple[int, int] = (0, 100),
    arrival_window: tuple[int, int] = (100, 700),
    min_confidence: float = 0.6,
    coordinate_tolerance: float = 1e-9,
    sample_rate_source: str = "explicit CLI value",
) -> dict:
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    data, meta, points = load_pam_file(path)
    coordinate_unit = meta.get("coordinate_unit")
    coordinate_scale_m = coordinate_scale_to_m(coordinate_unit)
    detections, priors = analyze_arrivals_by_line(
        data,
        points,
        baseline=baseline,
        arrival_window=arrival_window,
        min_confidence=min_confidence,
    )

    all_rows: list[dict] = []
    for point in points:
        result = detections[point.key]
        prior = priors[round(float(point.y), 12)]
        waveform = np.asarray(data[point.key], dtype=float).ravel()
        b0, b1 = max(0, baseline[0]), min(len(waveform), baseline[1])
        centered = waveform - np.median(waveform[b0:b1])
        shift_subsample, peak_correlation, integer_shift = _subsample_template_shift(centered, prior)
        arrival_subsample = float(prior.prior_arrival_sample + shift_subsample)
        all_rows.append(
            {
                "point_index": point.index,
                "key": point.key,
                "pos_text": point.pos_text,
                "x": point.x,
                "y": point.y,
                "coordinate_unit": coordinate_unit,
                "arrival_subsample": arrival_subsample,
                "arrival_time_ns_from_record_start": arrival_subsample / sample_rate_hz * 1e9,
                "integer_candidate_arrival": result["candidate_arrival_sample"],
                "template_shift_subsample": shift_subsample,
                "template_shift_integer": integer_shift,
                "template_correlation": peak_correlation,
                "event_strength_sigma": result["event_strength_sigma"],
                "confidence": result["confidence"],
                "detected": result["detected"],
                "line_prior_arrival_sample": result["line_prior_arrival_sample"],
                "line_prior_peak_sample": result["line_prior_peak_sample"],
                "line_prior_quality": result["line_prior_quality"],
            }
        )

    y_values = sorted({round(float(row["y"]), 12) for row in all_rows})
    all_y_summaries: list[dict] = []
    y_fit_cache: dict[float, dict] = {}
    for y_value in y_values:
        line = [row for row in all_rows if round(float(row["y"]), 12) == y_value]
        x_values = np.array([row["x"] for row in line], dtype=float)
        arrivals = np.array([row["arrival_subsample"] for row in line], dtype=float)
        detected_mask = np.array([bool(row["detected"]) for row in line], dtype=bool)
        all_fit = linear_speed_fit(x_values, arrivals, sample_rate_hz, coordinate_scale_m)
        detected_fit = linear_speed_fit(x_values[detected_mask], arrivals[detected_mask], sample_rate_hz, coordinate_scale_m)
        y_fit_cache[y_value] = {"all": all_fit, "detected": detected_fit}
        all_y_summaries.append(
            {
                "y": y_value,
                "coordinate_unit": coordinate_unit,
                "point_count": len(line),
                "detected_count": int(detected_mask.sum()),
                "detected_fraction": float(detected_mask.mean()),
                "all_candidate_slope_samples_per_coordinate_unit": all_fit.get("slope_samples_per_coordinate_unit"),
                "all_candidate_r_squared": all_fit.get("r_squared"),
                "all_candidate_apparent_speed_m_per_s": all_fit.get("apparent_speed_m_per_s"),
                "detected_only_slope_samples_per_coordinate_unit": detected_fit.get("slope_samples_per_coordinate_unit"),
                "detected_only_r_squared": detected_fit.get("r_squared"),
                "detected_only_apparent_speed_m_per_s": detected_fit.get("apparent_speed_m_per_s"),
            }
        )

    if y is None:
        selected_y = max(
            y_values,
            key=lambda value: y_fit_cache[value]["all"].get("r_squared", float("-inf")),
        )
        selection_reason = "auto-selected highest all-candidate OLS R-squared"
    else:
        selected_y = min(y_values, key=lambda value: abs(value - float(y)))
        if abs(selected_y - float(y)) > coordinate_tolerance:
            available = ", ".join(f"{value:g}" for value in y_values)
            raise ValueError(f"No Y row within {coordinate_tolerance:g} of {y:g}; available: {available}")
        selection_reason = "explicit Y coordinate"

    selected_rows = sorted(
        [row for row in all_rows if round(float(row["y"]), 12) == selected_y],
        key=lambda row: row["x"],
    )
    x_values = np.array([row["x"] for row in selected_rows], dtype=float)
    arrivals = np.array([row["arrival_subsample"] for row in selected_rows], dtype=float)
    detected_mask = np.array([bool(row["detected"]) for row in selected_rows], dtype=bool)
    high_confidence_mask = np.array([float(row["confidence"]) >= max(0.9, min_confidence) for row in selected_rows])
    all_fit = linear_speed_fit(x_values, arrivals, sample_rate_hz, coordinate_scale_m)
    detected_fit = linear_speed_fit(x_values[detected_mask], arrivals[detected_mask], sample_rate_hz, coordinate_scale_m)
    high_confidence_fit = linear_speed_fit(
        x_values[high_confidence_mask], arrivals[high_confidence_mask], sample_rate_hz, coordinate_scale_m
    )
    theil_fit = _theil_sen_fit(x_values, arrivals, sample_rate_hz, coordinate_scale_m)

    slopes_consistent = False
    if all_fit.get("valid") and detected_fit.get("valid"):
        all_slope = all_fit["slope_samples_per_coordinate_unit"]
        detected_slope = detected_fit["slope_samples_per_coordinate_unit"]
        slopes_consistent = (
            all_slope * detected_slope > 0
            and abs(all_slope - detected_slope) / max(abs(all_slope), 1e-12) <= 0.25
        )
    speed_validated = bool(
        all_fit.get("r_squared", 0.0) >= 0.9
        and detected_fit.get("r_squared", 0.0) >= 0.8
        and slopes_consistent
    )
    interpretation = (
        "validated linear time-of-flight speed"
        if speed_validated
        else "apparent speed only; low-confidence points and amplitude-dependent delay dominate the full-line slope"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{path.stem}_y{_slug_number(selected_y)}"
    points_path = output_dir / f"{stem}_arrival_speed_points.csv"
    all_y_path = output_dir / f"{path.stem}_all_y_arrival_speed_summary.csv"
    fit_figure_path = output_dir / f"{stem}_arrival_vs_x_speed_fit.png"
    overlay_path = output_dir / f"{stem}_x_time_arrival_overlay.png"
    comparison_path = output_dir / f"{path.stem}_all_y_fit_comparison.png"
    summary_path = output_dir / f"{stem}_arrival_speed_summary.json"

    _write_csv(points_path, selected_rows)
    _write_csv(all_y_path, all_y_summaries)
    _plot_selected_line(path, fit_figure_path, selected_rows, all_fit, detected_fit, coordinate_unit, selected_y)
    line_points = sorted(
        [point for point in points if round(float(point.y), 12) == selected_y],
        key=lambda point: point.x,
    )
    _plot_x_time_overlay(
        path,
        overlay_path,
        data,
        line_points,
        selected_rows,
        all_fit,
        baseline,
        arrival_window,
        coordinate_unit,
        selected_y,
    )
    _plot_all_y_comparison(path, comparison_path, all_y_summaries, coordinate_unit)

    summary = {
        "file": path.name,
        "source_path": str(path),
        "selected_y": selected_y,
        "selection_reason": selection_reason,
        "coordinate_unit": coordinate_unit,
        "coordinate_scale_m": coordinate_scale_m,
        "sample_rate_hz": float(sample_rate_hz),
        "sample_period_ps": float(1e12 / sample_rate_hz),
        "sample_rate_source": sample_rate_source,
        "baseline": list(baseline),
        "arrival_window": list(arrival_window),
        "min_confidence": min_confidence,
        "point_count": len(selected_rows),
        "detected_count": int(detected_mask.sum()),
        "detected_fraction": float(detected_mask.mean()),
        "fits": {
            "all_candidates_ols": all_fit,
            "detected_only_ols": detected_fit,
            "high_confidence_ols": high_confidence_fit,
            "all_candidates_theil_sen": theil_fit,
        },
        "speed_validated": speed_validated,
        "slopes_consistent": slopes_consistent,
        "interpretation": interpretation,
        "important_caveat": (
            "The MAT metadata contains coordinate_unit but no sample-rate field. "
            "The speed conversion is valid only if the explicit sample rate matches the acquisition script."
        ),
        "outputs": {
            "point_table": str(points_path),
            "all_y_summary": str(all_y_path),
            "fit_figure": str(fit_figure_path),
            "x_time_overlay": str(overlay_path),
            "all_y_comparison": str(comparison_path),
            "summary": str(summary_path),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
