from __future__ import annotations

from pathlib import Path
import csv
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import gaussian_filter1d

from .core import analyze_arrivals_by_line, load_pam_file, template_correlation_curve


def _slug_number(value: float) -> str:
    text = f"{float(value):g}"
    return re.sub(r"[^0-9A-Za-z]+", "p", text).strip("p") or "0"


def _detection_arrays(waveform, baseline: tuple[int, int], target_window: tuple[int, int], smooth_sigma: float, threshold_sigma: float):
    w = np.asarray(waveform, dtype=float).ravel()
    b0, b1 = baseline
    baseline_values = w[b0:b1]
    baseline_median = float(np.median(baseline_values))
    centered = w - baseline_median
    envelope = gaussian_filter1d(np.abs(centered), smooth_sigma)
    baseline_env = envelope[b0:b1]
    threshold = float(np.median(baseline_env) + threshold_sigma * np.std(baseline_env))
    return centered, envelope, threshold


def _sliding_mean_zscore(waveform, baseline: tuple[int, int], window_size: int = 10) -> tuple[np.ndarray, float]:
    w = np.asarray(waveform, dtype=float).ravel()
    b0, b1 = baseline
    noise = w[b0:b1]
    noise_mean = float(np.mean(noise))
    noise_std = float(np.std(noise))
    if noise_std < 1e-12:
        noise_std = 1e-12
    stats = np.zeros(len(w), dtype=float)
    start = max(b1, 0)
    stop = len(w) - window_size + 1
    if stop <= start:
        return stats, noise_std
    scale = noise_std / np.sqrt(window_size)
    for i in range(start, stop):
        stats[i] = (float(np.mean(w[i : i + window_size])) - noise_mean) / scale
    return stats, noise_std


def _select_diagnostic_indices(rows: list[dict], count: int) -> list[int]:
    if len(rows) <= count:
        return list(range(len(rows)))
    arrivals = np.array([r["candidate_arrival_sample"] for r in rows], dtype=float)
    peaks = np.array([r["candidate_peak_sample"] for r in rows], dtype=float)
    wanted = set()
    for values in (arrivals, peaks):
        for pct in np.linspace(0, 100, count):
            target = np.percentile(values, pct)
            wanted.add(int(np.argmin(np.abs(values - target))))
    if len(wanted) < count:
        for idx in np.linspace(0, len(rows) - 1, count, dtype=int):
            wanted.add(int(idx))
    ordered = sorted(wanted, key=lambda i: (rows[i]["y_um"], rows[i]["x_um"]))
    return ordered[:count]


def _select_y_endpoints(points, y: float, tolerance: float):
    y_values = np.array([p.y for p in points], dtype=float)
    distances = np.abs(y_values - float(y))
    row = [p for p, d in zip(points, distances) if d <= tolerance]
    row = sorted(row, key=lambda p: p.x)
    if len(row) < 2:
        raise ValueError(f"Need at least two points near y={y:g}; found {len(row)}")
    return [row[0], row[-1]], float(row[0].y)


def _select_explicit_points(points, specs: list[tuple[float, float, float | None]], tolerance: float):
    selected = []
    for x, y, z in specs:
        best_point = None
        best_dist = float("inf")
        for point in points:
            dz = 0.0 if z is None else point.z - z
            dist = float(np.sqrt((point.x - x) ** 2 + (point.y - y) ** 2 + dz**2))
            if dist < best_dist:
                best_dist = dist
                best_point = point
        if best_point is None or best_dist > tolerance:
            raise ValueError(f"No point found within tolerance {tolerance:g} for ({x:g}, {y:g}, {z if z is not None else 0:g})")
        selected.append(best_point)
    return selected


def _write_selected_point_table(rows: list[dict], path: Path):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _plot_selected_points(
    data,
    selected_points,
    rows: list[dict],
    priors,
    path: Path,
    output_path: Path,
    baseline: tuple[int, int],
    arrival_window: tuple[int, int],
    smooth_sigma: float,
    threshold_sigma: float,
):
    plt.rcParams.update(
        {
            "font.size": 11,
            "axes.titlesize": 11,
            "axes.labelsize": 10,
            "legend.fontsize": 9,
            "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )
    n_points = len(selected_points)
    fig, axes = plt.subplots(n_points, 3, figsize=(21, max(5.0, 4.4 * n_points)), squeeze=False)
    b0, b1 = baseline
    t0, t1 = arrival_window

    for row_idx, (point, result_row) in enumerate(zip(selected_points, rows)):
        waveform = np.asarray(data[point.key], dtype=float).ravel()
        centered, envelope, threshold = _detection_arrays(waveform, baseline, arrival_window, smooth_sigma, threshold_sigma)
        samples = np.arange(len(centered))
        arrival = int(result_row["candidate_arrival_sample"])
        peak = int(result_row["candidate_peak_sample"])
        pointwise_arrival = int(result_row["pointwise_candidate_arrival_sample"])
        prior = priors[round(float(point.y), 12)]
        shifts, correlations = template_correlation_curve(centered, prior)
        template_start, template_stop = prior.template_window
        template = prior.reference_waveform[template_start:template_stop]
        point_segment = centered[template_start + result_row["arrival_shift"] : template_stop + result_row["arrival_shift"]]
        scale = float(np.dot(point_segment, template) / max(float(np.dot(template, template)), 1e-12))
        scaled_reference = prior.reference_waveform * scale
        label = f"x={point.x:g}, y={point.y:g}, z={point.z:g}"

        ax = axes[row_idx, 0]
        ax.plot(samples, centered, color="#3C4043", lw=0.75, label="去基线波形")
        ax.axvspan(b0, b1, color="#1A73E8", alpha=0.10, label="噪声基线")
        ax.axvspan(t0, t1, color="#F9AB00", alpha=0.12, label="首次到达搜索范围")
        ax.axvline(arrival, color="#D93025" if result_row["detected"] else "#777777", lw=1.6, ls="-" if result_row["detected"] else "--", label="首次到达候选")
        ax.axvline(
            pointwise_arrival,
            color="#F57C00" if result_row["pointwise_detected"] else "#C9A15A",
            lw=1.4,
            ls="-" if result_row["pointwise_detected"] else ":",
            label="单点内部到达候选",
        )
        ax.axvline(peak, color="#188038", lw=1.2, label="模板峰值位置")
        ax.grid(alpha=0.25)
        ax.set_title(f"{label}｜完整波形")
        ax.set_xlabel("采样点序号")
        ax.set_ylabel("去基线 ADC")
        ax.legend(loc="upper right")

        ax = axes[row_idx, 1]
        zoom0 = max(0, t0 - 30)
        zoom1 = min(len(centered), min(t1, prior.prior_peak_sample + 300))
        ax.plot(samples, centered, color="#3C4043", lw=0.85, label="该点波形")
        ax.plot(samples, scaled_reference, color="#1A73E8", lw=1.2, alpha=0.9, label="同一 Y 扫描线共享模板（按幅值缩放）")
        ax.plot(samples, envelope, color="#A56A00", lw=0.9, alpha=0.65, label="绝对值平滑包络")
        ax.axhline(threshold, color="#F9AB00", lw=1.1, ls="--", label="该点噪声阈值")
        ax.axvspan(t0, t1, color="#F9AB00", alpha=0.12)
        ax.axvline(prior.prior_arrival_sample, color="#7B1FA2", lw=1.4, ls=":", label=f"扫描线先验到达 {prior.prior_arrival_sample}")
        ax.axvline(arrival, color="#D93025" if result_row["detected"] else "#777777", lw=1.8, ls="-" if result_row["detected"] else "--", label=f"该点候选 {arrival}")
        ax.axvline(
            pointwise_arrival,
            color="#F57C00" if result_row["pointwise_detected"] else "#C9A15A",
            lw=1.6,
            ls="-" if result_row["pointwise_detected"] else ":",
            label=f"单点候选 {pointwise_arrival}",
        )
        ax.set_xlim(zoom0, zoom1)
        ax.grid(alpha=0.25)
        state = "有效到达" if result_row["detected"] else "未检出"
        ax.set_title(
            f"全局先验：{state}｜可信度={float(result_row['confidence']):.2f}；"
            f"单点可信度={float(result_row['pointwise_confidence']):.2f}"
        )
        ax.set_xlabel("采样点序号")
        ax.set_ylabel("ADC / 包络")
        ax.legend(loc="upper right")

        ax = axes[row_idx, 2]
        ax.plot(shifts, correlations, color="#7B1FA2", lw=1.2, label="与扫描线模板的相关性")
        ax.axvline(result_row["arrival_shift"], color="#D93025", lw=1.5, label=f"最佳平移 {result_row['arrival_shift']:+d}")
        ax.axhline(0.25, color="#777777", lw=1.0, ls="--", label="低相关参考线 0.25")
        ax.grid(alpha=0.25)
        ax.set_title(
            f"模板相关性={result_row['template_correlation']:.3f}｜"
            f"事件强度/噪声={result_row['event_strength_sigma']:.2f} σ｜"
            f"扫描线先验质量={result_row['line_prior_quality']:.2f}"
        )
        ax.set_xlabel("相对扫描线先验的采样点平移")
        ax.set_ylabel("归一化相关系数")
        ax.legend(loc="upper right")

    fig.suptitle(f"{path.name}：指定点首次到达诊断（线扫描全局先验）", fontsize=15, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(output_path, dpi=180)
    plt.close(fig)


def write_y_endpoint_arrival_diagnostics(
    path: Path,
    output_dir: Path,
    y: float,
    target_window: tuple[int, int] = (100, 700),
    baseline: tuple[int, int] = (0, 100),
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    tolerance: float = 1e-9,
) -> dict:
    data, meta, points = load_pam_file(path)
    selected_points, actual_y = _select_y_endpoints(points, y, tolerance)
    return write_selected_arrival_diagnostics(
        path=path,
        output_dir=output_dir,
        selected_points=selected_points,
        selection_name=f"y{_slug_number(actual_y)}_x_endpoints",
        target_window=target_window,
        baseline=baseline,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
        scan_meta=meta,
        data=data,
        all_points=points,
    )


def write_explicit_point_arrival_diagnostics(
    path: Path,
    output_dir: Path,
    point_specs: list[tuple[float, float, float | None]],
    target_window: tuple[int, int] = (100, 700),
    baseline: tuple[int, int] = (0, 100),
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    tolerance: float = 1e-9,
) -> dict:
    data, meta, points = load_pam_file(path)
    selected_points = _select_explicit_points(points, point_specs, tolerance)
    return write_selected_arrival_diagnostics(
        path=path,
        output_dir=output_dir,
        selected_points=selected_points,
        selection_name="selected_points",
        target_window=target_window,
        baseline=baseline,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
        scan_meta=meta,
        data=data,
        all_points=points,
    )


def write_selected_arrival_diagnostics(
    path: Path,
    output_dir: Path,
    selected_points,
    selection_name: str,
    target_window: tuple[int, int],
    baseline: tuple[int, int],
    smooth_sigma: float,
    threshold_sigma: float,
    scan_meta: dict | None = None,
    data=None,
    all_points=None,
) -> dict:
    if data is None or all_points is None:
        data, scan_meta, all_points = load_pam_file(path)
    output_dir.mkdir(parents=True, exist_ok=True)
    detections, priors = analyze_arrivals_by_line(
        data,
        all_points,
        baseline=baseline,
        arrival_window=target_window,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
    )
    rows = []
    for order, point in enumerate(selected_points):
        result = detections[point.key]
        rows.append(
            {
                "selection_order": order,
                "point_index": point.index,
                "key": point.key,
                "pos_text": point.pos_text,
                "x_um": point.x,
                "y_um": point.y,
                "z_um": point.z,
                "arrival_sample": result["arrival_sample"],
                "peak_sample": result["peak_sample"],
                "candidate_arrival_sample": result["candidate_arrival_sample"],
                "candidate_peak_sample": result["candidate_peak_sample"],
                "pointwise_arrival_sample": result["pointwise_arrival_sample"],
                "pointwise_candidate_arrival_sample": result["pointwise_candidate_arrival_sample"],
                "pointwise_peak_sample": result["pointwise_peak_sample"],
                "detected": result["detected"],
                "pointwise_detected": result["pointwise_detected"],
                "confidence": result["confidence"],
                "pointwise_confidence": result["pointwise_confidence"],
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
        )

    stem = path.stem
    table_path = output_dir / f"{stem}_{selection_name}_arrival_points.csv"
    figure_path = output_dir / f"{stem}_{selection_name}_arrival_diagnostics.png"
    _write_selected_point_table(rows, table_path)
    _plot_selected_points(
        data=data,
        selected_points=selected_points,
        rows=rows,
        priors=priors,
        path=path,
        output_path=figure_path,
        baseline=baseline,
        arrival_window=target_window,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
    )
    return {
        "figure": str(figure_path),
        "table": str(table_path),
        "selected_count": len(selected_points),
        "rows": rows,
        "scan": scan_meta,
    }


def write_arrival_diagnostics(
    path: Path,
    output_dir: Path,
    target_window: tuple[int, int] = (100, 700),
    baseline: tuple[int, int] = (0, 100),
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    point_count: int = 12,
) -> dict:
    data, meta, points = load_pam_file(path)
    detections, priors = analyze_arrivals_by_line(
        data,
        points,
        baseline=baseline,
        arrival_window=target_window,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
    )
    rows = []
    for point in points:
        result = detections[point.key]
        rows.append(
            {
                "point_index": point.index,
                "key": point.key,
                "pos_text": point.pos_text,
                "x_um": point.x,
                "y_um": point.y,
                "arrival_sample": result["arrival_sample"],
                "peak_sample": result["peak_sample"],
                "candidate_arrival_sample": result["candidate_arrival_sample"],
                "candidate_peak_sample": result["candidate_peak_sample"],
                "pointwise_arrival_sample": result["pointwise_arrival_sample"],
                "pointwise_candidate_arrival_sample": result["pointwise_candidate_arrival_sample"],
                "pointwise_peak_sample": result["pointwise_peak_sample"],
                "detected": result["detected"],
                "pointwise_detected": result["pointwise_detected"],
                "confidence": result["confidence"],
                "event_strength_sigma": result["event_strength_sigma"],
                "pointwise_confidence": result["pointwise_confidence"],
                "pointwise_event_strength_sigma": result["pointwise_event_strength_sigma"],
                "template_correlation": result["template_correlation"],
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem

    csv_path = output_dir / f"{stem}_diagnostic_points.csv"
    chosen = _select_diagnostic_indices(rows, point_count)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for idx in chosen:
            writer.writerow(rows[idx])

    profile_path = output_dir / f"{stem}_arrival_profile.png"
    fig, axes = plt.subplots(2, 1, figsize=(14, 9), sharex=True)
    unique_y = sorted({round(r["y_um"], 12) for r in rows})
    color_cycle = plt.cm.tab10(np.linspace(0, 1, max(1, len(unique_y))))
    for color, y in zip(color_cycle, unique_y):
        group = [r for r in rows if round(r["y_um"], 12) == y]
        xs = np.array([r["x_um"] for r in group], dtype=float)
        order = np.argsort(xs)
        arrivals = np.array([r["candidate_arrival_sample"] for r in group], dtype=float)[order]
        peaks = np.array([r["candidate_peak_sample"] for r in group], dtype=float)[order]
        detected = np.array([r["detected"] for r in group], dtype=bool)[order]
        pointwise_arrivals = np.array([r["pointwise_candidate_arrival_sample"] for r in group], dtype=float)[order]
        pointwise_detected = np.array([r["pointwise_detected"] for r in group], dtype=bool)[order]
        xs = xs[order]
        label = f"y={y:g} um"
        axes[0].plot(xs[detected], peaks[detected], ".", ms=4, color=color, label=label)
        axes[1].plot(xs[detected], arrivals[detected], ".", ms=4, color=color, label=label)
        axes[1].plot(xs[pointwise_detected], pointwise_arrivals[pointwise_detected], "^", ms=3.5, color="#F57C00", alpha=0.75)
        axes[0].plot(xs[~detected], peaks[~detected], "x", ms=3, color="#888888", alpha=0.45)
        axes[1].plot(xs[~detected], arrivals[~detected], "x", ms=3, color="#888888", alpha=0.45)
    for ax, title in zip(axes, ["共享模板峰值位置与 X", "首次到达候选与 X（叉号为未检出）"]):
        ax.axhspan(target_window[0], target_window[1], color="#F9AB00", alpha=0.12, label="首次到达搜索范围")
        ax.grid(alpha=0.28)
        ax.set_ylabel("采样点序号")
        ax.set_title(title)
    axes[1].set_xlabel("X 坐标 (um)")
    axes[0].legend(loc="upper right", fontsize=8, ncol=min(4, max(1, len(unique_y))))
    fig.suptitle(path.name)
    fig.tight_layout()
    fig.savefig(profile_path, dpi=170)
    plt.close(fig)

    points_path = output_dir / f"{stem}_diagnostic_waveforms.png"
    n_cols = 3
    n_rows = int(np.ceil(len(chosen) / n_cols))
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(17, 4.2 * n_rows), sharex=True)
    axes = np.asarray(axes).reshape(-1)
    t0, t1 = target_window
    for ax, idx in zip(axes, chosen):
        point = points[idx]
        centered, envelope, threshold = _detection_arrays(data[point.key], baseline, target_window, smooth_sigma, threshold_sigma)
        row = rows[idx]
        samples = np.arange(len(centered))
        ax.plot(samples, centered, color="#3C4043", lw=0.85, label="centered waveform")
        ax.plot(samples, envelope, color="#1A73E8", lw=1.05, alpha=0.9, label="abs envelope")
        ax.axhline(threshold, color="#F9AB00", lw=1.1, ls="--", label="envelope threshold")
        ax.axvspan(t0, t1, color="#F9AB00", alpha=0.10)
        ax.axvline(row["candidate_arrival_sample"], color="#D93025" if row["detected"] else "#777777", lw=1.2, ls="-" if row["detected"] else "--", label="首次到达候选")
        ax.axvline(row["candidate_peak_sample"], color="#188038", lw=1.2, label="共享模板峰值")
        ax.set_xlim(max(0, t0 - 180), min(len(centered), t1 + 180))
        ax.grid(alpha=0.25)
        ax.set_title(
            f"{point.pos_text} | 到达候选={row['candidate_arrival_sample']} "
            f"{'有效' if row['detected'] else '未检出'} conf={row['confidence']:.2f}",
            fontsize=10,
        )
    for ax in axes[len(chosen) :]:
        ax.axis("off")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.suptitle(f"{path.name}: selected waveform diagnostics", y=0.995, fontsize=15)
    fig.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.965), ncol=5, fontsize=10)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(points_path, dpi=170)
    plt.close(fig)

    return {
        "profile": str(profile_path),
        "waveforms": str(points_path),
        "table": str(csv_path),
        "selected_count": len(chosen),
        "scan": meta,
    }
