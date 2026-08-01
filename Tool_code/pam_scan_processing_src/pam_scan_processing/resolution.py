from __future__ import annotations

from pathlib import Path
import csv
import json
import math

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import plotly.graph_objects as go
from scipy.optimize import curve_fit

from .core import load_pam_file, parse_slice


def _logistic_step(x, a, b, c, d):
    c = np.clip(c, -80.0, 80.0)
    return a + (b - a) / (1.0 + np.exp(-c * (x - d)))


def _axis_values(points, axis: str) -> np.ndarray:
    if axis == "x":
        return np.array([p.x for p in points], dtype=float)
    if axis == "y":
        return np.array([p.y for p in points], dtype=float)
    raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")


def _fixed_values(points, axis: str) -> np.ndarray:
    if axis == "x":
        return np.array([p.y for p in points], dtype=float)
    if axis == "y":
        return np.array([p.x for p in points], dtype=float)
    raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")


def _select_profile_points(points, axis: str, fixed_value: float | None, tolerance: float):
    axis = axis.lower()
    fixed = _fixed_values(points, axis)
    if fixed_value is None:
        unique_values = sorted({round(v, 12) for v in fixed})
        best_group = []
        for value in unique_values:
            group = [p for p, v in zip(points, fixed) if abs(v - value) <= 1e-12]
            if len(group) > len(best_group):
                best_group = group
        selected = best_group
        actual_fixed = float(_fixed_values(selected, axis)[0])
    else:
        distances = np.abs(fixed - float(fixed_value))
        selected = [p for p, d in zip(points, distances) if d <= tolerance]
        if not selected:
            raise ValueError(f"No points found within tolerance {tolerance:g} for {('y' if axis == 'x' else 'x')}={fixed_value:g}")
        actual_fixed = float(_fixed_values(selected, axis)[0])
    selected = sorted(selected, key=lambda p: p.x if axis == "x" else p.y)
    if len(selected) < 6:
        raise ValueError(f"Need at least six points for a stable step fit; found {len(selected)}")
    return selected, actual_fixed


def _compute_profile(data, points, p2p_window: tuple[int, int] | None):
    values = []
    for point in points:
        waveform = np.asarray(data[point.key], dtype=float).ravel()
        if p2p_window is not None:
            w0, w1 = p2p_window
            waveform = waveform[max(0, w0) : min(len(waveform), w1)]
        values.append(float(np.max(waveform) - np.min(waveform)))
    return np.asarray(values, dtype=float)


def _p2p_axis_labels(meta: dict):
    y_label = "Y coordinate (display)"
    if meta.get("y_display_scale", 1.0) != 1.0:
        y_label += f"; expanded x{meta['y_display_scale']:.1f}"
    return "X coordinate (um)", y_label, "Peak-to-peak ADC"


def _p2p_camera(meta: dict, zoom_scale: float = 1.4) -> dict:
    base_eye = {
        "x": 1.7,
        "y": 1.25 if meta.get("unique_y_count", 1) > 1 else 0.55,
        "z": 0.95,
    }
    return {
        "eye": {axis: value / zoom_scale for axis, value in base_eye.items()},
        "up": {"x": 0.0, "y": 0.0, "z": 1.0},
        "center": {"x": 0.0, "y": 0.0, "z": -0.03},
        "projection": {"type": "perspective"},
    }


def write_peak_to_peak_interactive_html(
    path: Path,
    output_path: Path,
    meta: dict,
    all_points,
    all_p2p: np.ndarray,
    profile_points,
    profile_p2p: np.ndarray,
    fixed_axis: str,
    fixed_value: float,
) -> Path:
    x_label, y_label, z_label = _p2p_axis_labels(meta)
    xs = np.array([point.x for point in all_points], dtype=float)
    ys = np.array([point.display_y for point in all_points], dtype=float)
    zs = np.asarray(all_p2p, dtype=float)
    hover = [
        (
            f"位置={point.pos_text}<br>"
            f"x={point.x:.4g} um<br>"
            f"y={point.y:.4g} um<br>"
            f"峰峰值={value:.2f}"
        )
        for point, value in zip(all_points, zs)
    ]

    profile_x = np.array([point.x for point in profile_points], dtype=float)
    profile_y = np.array([point.display_y for point in profile_points], dtype=float)
    profile_z = np.asarray(profile_p2p, dtype=float)
    profile_hover = [
        (
            f"分辨率线位置={point.pos_text}<br>"
            f"x={point.x:.4g} um<br>"
            f"y={point.y:.4g} um<br>"
            f"峰峰值={value:.2f}"
        )
        for point, value in zip(profile_points, profile_z)
    ]

    fig = go.Figure()
    fig.add_trace(
        go.Scatter3d(
            x=xs,
            y=ys,
            z=zs,
            mode="markers",
            name="全部峰峰值散点",
            marker={
                "size": 4,
                "color": zs,
                "colorscale": "Viridis",
                "opacity": 0.88,
                "line": {"color": "#202020", "width": 0.6},
                "colorbar": {
                    "title": {"text": "峰峰值<br>ADC", "font": {"size": 16}},
                    "tickfont": {"size": 14},
                    "x": 1.03,
                    "len": 0.75,
                    "thickness": 18,
                },
            },
            text=hover,
            hovertemplate="%{text}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=profile_x,
            y=profile_y,
            z=profile_z,
            mode="lines+markers",
            name=f"分辨率拟合线（{fixed_axis}={fixed_value:g}）",
            line={"color": "#111111", "width": 6},
            marker={
                "size": 4,
                "color": "#D93025",
                "line": {"color": "#111111", "width": 1},
            },
            text=profile_hover,
            hovertemplate="%{text}<extra></extra>",
        )
    )

    scene = {
        "xaxis": {
            "title": {"text": "X 坐标 (um)", "font": {"size": 18}},
            "tickfont": {"size": 14},
            "showspikes": True,
            "spikecolor": "#455A64",
            "spikethickness": 2,
        },
        "yaxis": {
            "title": {"text": "Y 坐标（显示）" if "expanded" not in y_label else y_label.replace("Y coordinate (display)", "Y 坐标（显示）"), "font": {"size": 18}},
            "tickfont": {"size": 14},
            "showspikes": True,
            "spikecolor": "#455A64",
            "spikethickness": 2,
        },
        "zaxis": {
            "title": {"text": "峰峰值 ADC", "font": {"size": 18}},
            "tickfont": {"size": 14},
            "showspikes": True,
            "spikecolor": "#455A64",
            "spikethickness": 2,
        },
        "aspectmode": "manual",
        "aspectratio": {"x": 1.7, "y": 0.65 if meta.get("unique_y_count", 1) > 1 else 0.25, "z": 0.95},
        "camera": _p2p_camera(meta),
    }
    fig.update_layout(
        title={
            "text": (
                f"{path.name}：可交互 3D 峰峰值散点图<br>"
                f"<sup>彩色散点是全部扫描点的峰峰值；黑线红点高亮的是用于分辨率拟合的 {fixed_axis}={fixed_value:g} 这一条线。</sup>"
            ),
            "font": {"size": 21},
            "x": 0.02,
        },
        scene=scene,
        legend={
            "x": 0.02,
            "y": 0.96,
            "font": {"size": 16},
            "bgcolor": "rgba(255,255,255,0.85)",
            "bordercolor": "#DADCE0",
            "borderwidth": 1,
        },
        hoverlabel={"font": {"size": 16}},
        margin={"l": 0, "r": 155, "t": 88, "b": 0},
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(output_path, include_plotlyjs=True, full_html=True)
    return output_path


def plot_peak_to_peak_3d(
    path: Path,
    meta: dict,
    all_points,
    all_p2p: np.ndarray,
    profile_points,
    profile_p2p: np.ndarray,
    axis: str,
    fixed_axis: str,
    fixed_value: float,
):
    xs = np.array([point.x for point in all_points], dtype=float)
    ys = np.array([point.display_y for point in all_points], dtype=float)
    zs = np.asarray(all_p2p, dtype=float)

    fig = plt.figure(figsize=(15, 10), facecolor="white")
    ax = fig.add_subplot(111, projection="3d")
    scatter = ax.scatter(
        xs,
        ys,
        zs,
        c=zs,
        cmap="viridis",
        s=20,
        alpha=0.90,
        edgecolors="#202020",
        linewidths=0.22,
    )

    profile_x = np.array([point.x for point in profile_points], dtype=float)
    profile_y = np.array([point.display_y for point in profile_points], dtype=float)
    profile_z = np.asarray(profile_p2p, dtype=float)
    ax.plot(
        profile_x,
        profile_y,
        profile_z,
        color="#111111",
        linewidth=2.0,
        alpha=0.90,
        label=f"resolution profile ({fixed_axis}={fixed_value:g})",
    )
    ax.scatter(
        profile_x,
        profile_y,
        profile_z,
        s=34,
        facecolors="none",
        edgecolors="#D93025",
        linewidths=1.0,
        alpha=0.95,
        label="profile scatter points",
    )

    x_label, y_label, z_label = _p2p_axis_labels(meta)
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_zlabel(z_label)
    ax.set_title(f"{path.name}: 3D peak-to-peak scatter", pad=14)
    ax.view_init(elev=25, azim=-61)
    try:
        ax.set_box_aspect((1.7, 0.65 if meta.get("unique_y_count", 1) > 1 else 0.25, 0.95))
    except Exception:
        pass

    if len(xs):
        ax.set_xlim(float(xs.min()), float(xs.max()))
    if len(np.unique(ys)) == 1:
        ax.set_ylim(float(ys.min()) - 0.1, float(ys.max()) + 0.1)
    else:
        ax.set_ylim(float(ys.min()), float(ys.max()))
    if len(zs):
        z_pad = 0.04 * max(float(zs.max() - zs.min()), 1.0)
        ax.set_zlim(float(zs.min()) - z_pad, float(zs.max()) + z_pad)

    ax.legend(loc="upper left", framealpha=0.92)
    cbar = fig.colorbar(scatter, ax=ax, pad=0.08, shrink=0.72)
    cbar.set_label("Peak-to-peak ADC")
    fig.tight_layout()
    return fig


def _initial_guess(x_raw: np.ndarray, y_raw: np.ndarray):
    return [float(y_raw[0]), float(y_raw[-1]), 1.0, float(np.median(x_raw))]


def _fit_step_response(x_raw: np.ndarray, y_raw: np.ndarray):
    guess = _initial_guess(x_raw, y_raw)
    span = float(np.max(y_raw) - np.min(y_raw))
    if span <= 0:
        raise ValueError("Profile has no measurable amplitude span")
    candidates = []
    for c0 in (1.0, -1.0, 0.2, -0.2):
        start = [guess[0], guess[1], c0, guess[3]]
        try:
            popt, pcov = curve_fit(_logistic_step, x_raw, y_raw, p0=start, maxfev=100000)
            residual = y_raw - _logistic_step(x_raw, *popt)
            rmse = float(np.sqrt(np.mean(residual**2)))
            candidates.append((rmse, popt, pcov))
        except Exception:
            continue
    if not candidates:
        raise RuntimeError("Logistic step fit failed for all initial guesses")
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], candidates[0][0]


def _half_width_from_derivative(x_fit: np.ndarray, y_fit: np.ndarray):
    dy_fit = np.diff(y_fit) / np.diff(x_fit)
    x_der = x_fit[:-1] + np.diff(x_fit) / 2.0
    max_idx = int(np.argmax(np.abs(dy_fit)))
    peak_val = float(dy_fit[max_idx])
    half_max = peak_val / 2.0

    if peak_val > 0:
        idx1 = np.where(dy_fit[: max_idx + 1] >= half_max)[0]
        idx2_rel = np.where(dy_fit[max_idx:] <= half_max)[0]
    else:
        idx1 = np.where(dy_fit[: max_idx + 1] <= half_max)[0]
        idx2_rel = np.where(dy_fit[max_idx:] >= half_max)[0]

    left_cross = int(idx1[0]) if len(idx1) else 0
    def _crossing_x(i0: int, i1: int) -> float:
        y0 = float(dy_fit[i0])
        y1 = float(dy_fit[i1])
        x0 = float(x_der[i0])
        x1 = float(x_der[i1])
        if abs(y1 - y0) < 1e-12:
            return x1
        return x0 + (float(half_max) - y0) * (x1 - x0) / (y1 - y0)

    if left_cross <= 0:
        x_left = float(x_der[0])
    else:
        x_left = _crossing_x(left_cross - 1, left_cross)

    if len(idx2_rel):
        right_cross = int(idx2_rel[0]) + max_idx
        if right_cross <= 0:
            x_right = float(x_der[0])
        else:
            x_right = _crossing_x(right_cross - 1, right_cross)
    else:
        x_right = float(x_der[-1])

    return {
        "x_der": x_der,
        "dy_fit": dy_fit,
        "peak_index": max_idx,
        "peak_value": peak_val,
        "half_max": float(half_max),
        "x_left": x_left,
        "x_right": x_right,
        "fwhm_index": float(abs(x_right - x_left)),
    }


def _axis_step_size(points, axis: str, meta: dict) -> float:
    values = np.sort(np.unique(np.round(_axis_values(points, axis), 12)))
    diffs = np.diff(values)
    diffs = diffs[np.abs(diffs) > 1e-12]
    if len(diffs):
        return float(np.median(diffs))
    if meta.get("step_um") is not None:
        return float(meta["step_um"])
    return 1.0


def write_long_axis_resolution(
    path: Path,
    output_dir: Path,
    axis: str = "x",
    fixed_value: float | None = None,
    tolerance: float = 1e-9,
    p2p_window: tuple[int, int] | None = None,
    matlab_nm_factor: float = 20.0,
    fit_samples: int = 2000,
) -> dict:
    data, meta, points = load_pam_file(path)
    axis = axis.lower()
    selected, actual_fixed = _select_profile_points(points, axis, fixed_value, tolerance)
    profile = _compute_profile(data, selected, p2p_window)
    all_p2p = _compute_profile(data, points, p2p_window)
    valid_mask = np.isfinite(profile)
    selected = [p for p, ok in zip(selected, valid_mask) if ok]
    y_raw = profile[valid_mask].astype(float)
    x_raw = np.arange(1, len(y_raw) + 1, dtype=float)
    if len(y_raw) < 6:
        raise ValueError("Not enough valid profile points for resolution fitting")

    params, fit_rmse = _fit_step_response(x_raw, y_raw)
    x_fit = np.linspace(float(x_raw.min()), float(x_raw.max()), fit_samples)
    y_fit = _logistic_step(x_fit, *params)
    derivative = _half_width_from_derivative(x_fit, y_fit)

    step_size = _axis_step_size(selected, axis, meta)
    fwhm_axis_units = derivative["fwhm_index"] * step_size
    nm_per_index_if_step_unit_is_um = step_size * 1000.0
    fwhm_nm_if_axis_um = derivative["fwhm_index"] * nm_per_index_if_step_unit_is_um
    legacy_plot_line_display_nm = fwhm_axis_units * matlab_nm_factor

    output_dir.mkdir(parents=True, exist_ok=True)
    fixed_axis = "y" if axis == "x" else "x"
    selection_text = f"{fixed_axis}{actual_fixed:g}".replace(".", "p").replace("-", "n")
    stem = f"{path.stem}_{axis}_long_axis_{selection_text}"
    figure_path = output_dir / f"{stem}_resolution_fit.png"
    peak_to_peak_3d_path = output_dir / f"{stem}_peak_to_peak_3d.png"
    peak_to_peak_3d_html_path = output_dir / f"{stem}_peak_to_peak_3d.html"
    csv_path = output_dir / f"{stem}_profile.csv"
    summary_path = output_dir / f"{stem}_resolution_summary.json"

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["point_index", "axis_index", f"{axis}_coord", f"{fixed_axis}_coord", "peak_to_peak"])
        writer.writeheader()
        for idx, (point, value) in enumerate(zip(selected, y_raw), start=1):
            writer.writerow(
                {
                    "point_index": point.index,
                    "axis_index": idx,
                    f"{axis}_coord": point.x if axis == "x" else point.y,
                    f"{fixed_axis}_coord": point.y if axis == "x" else point.x,
                    "peak_to_peak": value,
                }
            )

    plt.rcParams.update({"font.size": 12, "axes.titlesize": 13, "axes.labelsize": 12, "legend.fontsize": 10})
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), facecolor="white")
    axes[0].plot(x_raw, y_raw, "k.", markersize=7, label="Raw")
    axes[0].plot(x_fit, y_fit, "r-", linewidth=2.0, label="Fit")
    axes[0].grid(alpha=0.30)
    axes[0].set_title("ERF (Step Response)")
    axes[0].set_xlabel(f"{axis.upper()} index")
    axes[0].set_ylabel("Peak-to-peak ADC")
    axes[0].legend(loc="best")

    x_der = derivative["x_der"]
    dy_fit = derivative["dy_fit"]
    half_max = derivative["half_max"]
    x_left = derivative["x_left"]
    x_right = derivative["x_right"]
    axes[1].plot(x_der, dy_fit, "b-", linewidth=2.0, label="LSF")
    axes[1].plot([x_left, x_right], [half_max, half_max], "ro-", linewidth=2.0, markerfacecolor="r", label="FWHM")
    axes[1].text(x_left, half_max, f"X: {x_left:.2f}  ", horizontalalignment="right", color="r", fontweight="bold")
    axes[1].text(x_right, half_max, f"  X: {x_right:.2f}", horizontalalignment="left", color="r", fontweight="bold")
    axes[1].grid(alpha=0.30)
    axes[1].set_title(f"FWHM: {fwhm_nm_if_axis_um:.2f} nm ({derivative['fwhm_index']:.2f} index)")
    axes[1].set_xlabel(f"{axis.upper()} index")
    axes[1].set_ylabel("Derivative")
    axes[1].legend(loc="best")

    fig.suptitle(f"{path.name} | {fixed_axis}={actual_fixed:g}, step={step_size:g}", fontsize=13)
    fig.tight_layout()
    fig.savefig(figure_path, dpi=180, bbox_inches="tight")
    plt.close(fig)

    p2p_fig = plot_peak_to_peak_3d(
        path=path,
        meta=meta,
        all_points=points,
        all_p2p=all_p2p,
        profile_points=selected,
        profile_p2p=y_raw,
        axis=axis,
        fixed_axis=fixed_axis,
        fixed_value=actual_fixed,
    )
    p2p_fig.savefig(peak_to_peak_3d_path, dpi=180, bbox_inches="tight")
    plt.close(p2p_fig)

    write_peak_to_peak_interactive_html(
        path=path,
        output_path=peak_to_peak_3d_html_path,
        meta=meta,
        all_points=points,
        all_p2p=all_p2p,
        profile_points=selected,
        profile_p2p=y_raw,
        fixed_axis=fixed_axis,
        fixed_value=actual_fixed,
    )

    summary = {
        "file": path.name,
        "axis": axis,
        "fixed_axis": fixed_axis,
        "requested_fixed_value": fixed_value,
        "actual_fixed_value": actual_fixed,
        "point_count": len(y_raw),
        "p2p_window": list(p2p_window) if p2p_window is not None else None,
        "fit": {
            "model": "a + (b-a) / (1 + exp(-c*(x-d)))",
            "a": float(params[0]),
            "b": float(params[1]),
            "c": float(params[2]),
            "d": float(params[3]),
            "rmse_adc": fit_rmse,
        },
        "resolution": {
            "left_index": x_left,
            "right_index": x_right,
            "fwhm_index": derivative["fwhm_index"],
            "axis_step": step_size,
            "fwhm_axis_units": fwhm_axis_units,
            "nm_per_index_if_step_unit_is_um": nm_per_index_if_step_unit_is_um,
            "fwhm_nm_if_axis_is_um": fwhm_nm_if_axis_um,
            "legacy_plot_line_nm_factor": matlab_nm_factor,
            "legacy_plot_line_display_nm": legacy_plot_line_display_nm,
            "interpretation_note": (
                "The remote plot_line.m script multiplies coordinate-width by a hard-coded 20 before printing nm. "
                "For this test5 dataset, the metadata step is 0.01; if that step is in um, then one index is 10 nm "
                "and the physically consistent FWHM is about 665 nm rather than the legacy 13.3 nm display value."
            ),
        },
        "outputs": {
            "figure": str(figure_path),
            "peak_to_peak_3d": str(peak_to_peak_3d_path),
            "peak_to_peak_3d_html": str(peak_to_peak_3d_html_path),
            "profile_csv": str(csv_path),
            "summary_json": str(summary_path),
        },
        "scan": meta,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def parse_optional_slice(text: str | None) -> tuple[int, int] | None:
    if not text:
        return None
    return parse_slice(text, 0, 0)
