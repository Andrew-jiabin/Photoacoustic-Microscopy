from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import plotly.graph_objects as go

from .core import analyze_arrivals_by_line, load_pam_file, resolve_slice, select_indices


def _axis_layout(meta: dict) -> dict:
    y_title = "Y 坐标"
    if meta.get("y_display_scale", 1.0) != 1.0:
        y_title += f"（仅为显示放大 x{meta['y_display_scale']:.1f}）"
    return {
        "xaxis": {
            "title": {"text": "X 坐标 (um)", "font": {"size": 18}},
            "tickfont": {"size": 14},
            "showspikes": True,
            "spikecolor": "#455A64",
            "spikethickness": 2,
        },
        "yaxis": {
            "title": {"text": y_title, "font": {"size": 18}},
            "tickfont": {"size": 14},
            "showspikes": True,
            "spikecolor": "#455A64",
            "spikethickness": 2,
        },
        "zaxis": {
            "title": {"text": "采样点序号", "font": {"size": 18}},
            "tickfont": {"size": 14},
            "showspikes": True,
            "spikecolor": "#455A64",
            "spikethickness": 2,
        },
        "aspectmode": "manual",
        "aspectratio": {"x": 1.7, "y": 0.65 if meta.get("unique_y_count", 1) > 1 else 0.25, "z": 1.1},
    }


def _default_camera(meta: dict, zoom_scale: float = 1.45) -> dict:
    base_eye = {
        "x": 1.7,
        "y": 1.25 if meta.get("unique_y_count", 1) > 1 else 0.55,
        "z": 0.92,
    }
    return {
        "eye": {axis: value / zoom_scale for axis, value in base_eye.items()},
        "up": {"x": 0.0, "y": 0.0, "z": 1.0},
        "center": {"x": 0.0, "y": 0.0, "z": -0.02},
        "projection": {"type": "perspective"},
    }


def build_interactive_figure(
    path: Path,
    arrival_window: tuple[int, int],
    display_window: tuple[int, int],
    baseline: tuple[int, int],
    time_step: int,
    max_traces: int,
    max_waveform_points: int,
    smooth_sigma: float,
    threshold_sigma: float,
    min_marker_confidence: float = 0.6,
) -> go.Figure:
    data, meta, points = load_pam_file(path)
    chosen = select_indices(len(points), max_traces)
    waveform_length = max(len(np.asarray(data[point.key]).ravel()) for point in points)
    d0, d1 = resolve_slice(display_window, waveform_length)
    a0, a1 = resolve_slice(arrival_window, waveform_length)
    sample_idx = np.arange(d0, d1, time_step, dtype=int)
    requested_time_step = int(time_step)
    if max_waveform_points > 0 and len(chosen) > 0:
        total_waveform_points = int(len(chosen) * len(sample_idx))
        if total_waveform_points > max_waveform_points:
            extra_stride = int(np.ceil(total_waveform_points / max_waveform_points))
            sample_idx = sample_idx[::extra_stride]
            time_step = int(requested_time_step * extra_stride)
    detections, priors = analyze_arrivals_by_line(
        data,
        points,
        baseline=baseline,
        arrival_window=arrival_window,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
        min_confidence=min_marker_confidence,
    )

    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    values: list[float] = []

    for idx in chosen:
        point = points[int(idx)]
        waveform = np.asarray(data[point.key], dtype=float).ravel()
        b0, b1 = baseline
        centered = waveform - np.median(waveform[b0:b1])
        valid_samples = sample_idx[sample_idx < len(centered)]
        xs.extend([point.x] * len(valid_samples))
        ys.extend([point.display_y] * len(valid_samples))
        zs.extend(valid_samples.astype(float).tolist())
        values.extend(centered[valid_samples].astype(float).tolist())

    values_arr = np.asarray(values, dtype=float)
    color_limit = float(np.nanpercentile(np.abs(values_arr), 98)) if values_arr.size else 1.0

    guide_x: list[float | None] = []
    guide_y: list[float | None] = []
    guide_z: list[float | None] = []
    arrival_x: list[float] = []
    arrival_y: list[float] = []
    arrival_z: list[float] = []
    peak_z: list[float] = []
    arrival_conf: list[float] = []
    arrival_detected: list[bool] = []
    arrival_text: list[str] = []
    peak_text: list[str] = []
    pointwise_z: list[float] = []
    pointwise_conf: list[float] = []
    pointwise_detected: list[bool] = []
    pointwise_text: list[str] = []

    guide_indices = set(int(idx) for idx in chosen)
    for point_idx, point in enumerate(points):
        result = detections[point.key]
        if point_idx in guide_indices:
            guide_x.extend([point.x, point.x, None])
            guide_y.extend([point.display_y, point.display_y, None])
            guide_z.extend([float(d0), float(d1), None])
        arrival_x.append(point.x)
        arrival_y.append(point.display_y)
        arrival_z.append(float(result["candidate_arrival_sample"]))
        peak_z.append(float(result["candidate_peak_sample"]))
        arrival_conf.append(float(result["confidence"]))
        arrival_detected.append(bool(result["detected"]))
        pointwise_z.append(float(result["pointwise_candidate_arrival_sample"]))
        pointwise_conf.append(float(result["pointwise_confidence"]))
        pointwise_detected.append(bool(result["pointwise_detected"]))
        arrival_text.append(
            f"位置={point.pos_text}<br>x={point.x:.4g} um<br>y={point.y:.4g} um<br>"
            f"首次到达候选={result['candidate_arrival_sample']}<br>"
            f"线扫描先验到达={result['line_prior_arrival_sample']}<br>"
            f"模板相关性={result['template_correlation']:.3f}<br>"
            f"事件强度/噪声={result['event_strength_sigma']:.2f} σ<br>"
            f"全局先验可信度={result['confidence']:.2f}<br>"
            f"单点内部到达={result['pointwise_candidate_arrival_sample']}，可信度={result['pointwise_confidence']:.2f}<br>"
            f"判定={'有效到达' if result['detected'] else '未检出（仅保留候选）'}"
        )
        peak_text.append(
            f"位置={point.pos_text}<br>x={point.x:.4g} um<br>y={point.y:.4g} um<br>"
            f"模板峰值候选={result['candidate_peak_sample']}<br>"
            f"首次到达候选={result['candidate_arrival_sample']}<br>"
            f"全局先验可信度={result['confidence']:.2f}<br>"
            f"单点内部到达候选={result['pointwise_candidate_arrival_sample']}"
        )
        pointwise_text.append(
            f"位置={point.pos_text}<br>x={point.x:.4g} um<br>y={point.y:.4g} um<br>"
            f"单点内部首次到达候选={result['pointwise_candidate_arrival_sample']}<br>"
            f"单点内部峰值={result['pointwise_peak_sample']}<br>"
            f"单点内部可信度={result['pointwise_confidence']:.2f}<br>"
            f"事件强度/噪声={result['pointwise_event_strength_sigma']:.2f} σ<br>"
            f"判定={'有效到达' if result['pointwise_detected'] else '未检出（仅保留候选）'}"
        )

    fig = go.Figure()
    fig.add_trace(
        go.Scatter3d(
            x=guide_x,
            y=guide_y,
            z=guide_z,
            mode="lines",
            name=f"波形位置参考线（显示 {len(chosen)}/{len(points)} 条）",
            line={"color": "rgba(120,120,120,0.22)", "width": 1},
            hoverinfo="skip",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=xs,
            y=ys,
            z=zs,
            mode="markers",
            name=f"波形振幅采样点（显示 {len(chosen)}/{len(points)} 条，时间步长 {time_step}）",
            marker={
                "size": 2,
                "color": values,
                "colorscale": "RdBu",
                "cmin": -color_limit,
                "cmax": color_limit,
                "opacity": 0.78,
                "colorbar": {
                    "title": {"text": "波形振幅<br>去基线 ADC", "font": {"size": 16}},
                    "tickfont": {"size": 14},
                    "x": 1.03,
                    "y": 0.73,
                    "len": 0.44,
                    "thickness": 18,
                },
            },
            hovertemplate=(
                "x=%{x:.4g} um<br>Y 显示坐标=%{y:.4g}<br>采样点=%{z}<br>"
                "去基线 ADC=%{marker.color:.2f}<extra></extra>"
            ),
        )
    )
    confident = [i for i, detected in enumerate(arrival_detected) if detected]
    low_conf = [i for i, detected in enumerate(arrival_detected) if not detected]

    def pick(values: list, indices: list[int]) -> list:
        return [values[i] for i in indices]

    fig.add_trace(
        go.Scatter3d(
            x=pick(arrival_x, confident),
            y=pick(arrival_y, confident),
            z=pick(arrival_z, confident),
            mode="markers",
            name=f"有效首次到达（全局先验可信度 >= {min_marker_confidence:g}）",
            marker={
                "size": [4 + 6 * max(0.0, min(1.0, c)) for c in pick(arrival_conf, confident)],
                "color": pick(arrival_z, confident),
                "colorscale": "Viridis",
                "opacity": 0.94,
                "symbol": "circle",
                "line": {"color": "black", "width": 1},
                "colorbar": {
                    "title": {"text": "首次到达<br>采样点", "font": {"size": 16}},
                    "tickfont": {"size": 14},
                    "x": 1.03,
                    "y": 0.24,
                    "len": 0.44,
                    "thickness": 18,
                },
            },
            text=pick(arrival_text, confident),
            hovertemplate="%{text}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=pick(arrival_x, low_conf),
            y=pick(arrival_y, low_conf),
            z=pick(arrival_z, low_conf),
            mode="markers",
            name=f"低可信候选（< {min_marker_confidence:g}，默认隐藏）",
            visible="legendonly",
            marker={
                "size": [3 + 5 * max(0.0, min(1.0, c)) for c in pick(arrival_conf, low_conf)],
                "color": "#6E6E6E",
                "opacity": 0.68,
                "symbol": "x",
                "line": {"color": "#444444", "width": 1},
            },
            text=pick(arrival_text, low_conf),
            hovertemplate="%{text}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=pick(arrival_x, confident),
            y=pick(arrival_y, confident),
            z=pick(peak_z, confident),
            mode="markers",
            name="模板峰值位置（审查用）",
            visible="legendonly",
            marker={
                "size": [3 + 5 * max(0.0, min(1.0, c)) for c in pick(arrival_conf, confident)],
                "color": pick(peak_z, confident),
                "colorscale": "Magma",
                "opacity": 0.92,
                "symbol": "circle",
                "line": {"color": "black", "width": 0.5},
            },
            text=pick(peak_text, confident),
            hovertemplate="%{text}<extra></extra>",
        )
    )
    pointwise_valid = [i for i, detected in enumerate(pointwise_detected) if detected]
    pointwise_low = [i for i, detected in enumerate(pointwise_detected) if not detected]
    fig.add_trace(
        go.Scatter3d(
            x=pick(arrival_x, pointwise_valid),
            y=pick(arrival_y, pointwise_valid),
            z=pick(pointwise_z, pointwise_valid),
            mode="markers",
            name=f"单点内部首次到达（可信度 >= {min_marker_confidence:g}）",
            marker={
                "size": [4 + 5 * max(0.0, min(1.0, c)) for c in pick(pointwise_conf, pointwise_valid)],
                "color": "#F57C00",
                "opacity": 0.86,
                "symbol": "diamond",
                "line": {"color": "#5D2600", "width": 1},
            },
            text=pick(pointwise_text, pointwise_valid),
            hovertemplate="%{text}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter3d(
            x=pick(arrival_x, pointwise_low),
            y=pick(arrival_y, pointwise_low),
            z=pick(pointwise_z, pointwise_low),
            mode="markers",
            name=f"单点估计低可信（< {min_marker_confidence:g}，默认隐藏）",
            visible="legendonly",
            marker={
                "size": [3 + 4 * max(0.0, min(1.0, c)) for c in pick(pointwise_conf, pointwise_low)],
                "color": "#D6A15C",
                "opacity": 0.60,
                "symbol": "x",
                "line": {"color": "#7A4B00", "width": 1},
            },
            text=pick(pointwise_text, pointwise_low),
            hovertemplate="%{text}<extra></extra>",
        )
    )
    scene = _axis_layout(meta)
    early_stop = min(d1, max(800, a1))
    arrival_local_start = max(d0, min(prior.prior_arrival_sample for prior in priors.values()) - 80)
    arrival_local_stop = min(d1, max(prior.prior_peak_sample for prior in priors.values()) + 300)
    scene["zaxis"]["range"] = [d0, early_stop]
    fig.update_layout(
        title={
            "text": (
                f"{path.name}：可交互 3D 波形与到达时间视图<br>"
                "<sup>波形从采样点 0 开始；彩色圆点 = 每条扫描线共享先验支持的首次到达。"
                "全局先验圆点与单点内部菱形分别代表两种独立估计；低可信候选和后续模板峰值默认隐藏，可在图例中打开审查。"
                f"为保证浏览器流畅，波形层最多显示约 {max_waveform_points:g} 个采样点。</sup>"
            ),
            "font": {"size": 21},
            "x": 0.02,
        },
        scene={**scene, "camera": _default_camera(meta)},
        updatemenus=[
            {
                "type": "dropdown",
                "direction": "down",
                "x": 0.01,
                "y": 1.03,
                "showactive": True,
                "active": 0,
                "font": {"size": 15},
                "buttons": [
                    {"label": f"早期信号 {d0}:{early_stop}", "method": "relayout", "args": [{"scene.zaxis.range": [d0, early_stop]}]},
                    {
                        "label": f"首次到达局部 {arrival_local_start}:{arrival_local_stop}",
                        "method": "relayout",
                        "args": [{"scene.zaxis.range": [arrival_local_start, arrival_local_stop]}],
                    },
                    {"label": f"完整波形 {d0}:{d1}", "method": "relayout", "args": [{"scene.zaxis.range": [d0, d1]}]},
                ],
            }
        ],
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
    return fig


def write_interactive_html(
    path: Path,
    output_dir: Path,
    arrival_window: tuple[int, int] = (100, 700),
    display_window: tuple[int, int] = (0, -1),
    baseline: tuple[int, int] = (0, 100),
    time_step: int = 8,
    max_traces: int = 500,
    max_waveform_points: int = 150_000,
    smooth_sigma: float = 3.0,
    threshold_sigma: float = 5.0,
    min_marker_confidence: float = 0.6,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    fig = build_interactive_figure(
        path=path,
        arrival_window=arrival_window,
        display_window=display_window,
        baseline=baseline,
        time_step=time_step,
        max_traces=max_traces,
        max_waveform_points=max_waveform_points,
        smooth_sigma=smooth_sigma,
        threshold_sigma=threshold_sigma,
        min_marker_confidence=min_marker_confidence,
    )
    output_path = output_dir / f"{path.stem}_interactive_3d.html"
    fig.write_html(output_path, include_plotlyjs=True, full_html=True)
    return output_path


def write_index_html(output_dir: Path, html_files: list[Path]) -> Path:
    cards = []
    for html_file in html_files:
        label = html_file.name.replace("_interactive_3d.html", "")
        cards.append(
            "      <a href=\"{href}\">\n"
            "        <span class=\"name\">{label}</span>\n"
                "        <span class=\"meta\">完整时域波形 + 全局先验与单点内部首次到达</span>\n"
            "      </a>".format(
                href=html.escape(html_file.name, quote=True),
                label=html.escape(label),
            )
        )

    page = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PAM 交互式 3D 结果</title>
  <style>
    body {{ margin: 0; font-family: Arial, sans-serif; color: #202124; background: #f7f8fa; }}
    main {{ max-width: 980px; margin: 0 auto; padding: 32px 24px; }}
    h1 {{ margin: 0 0 8px; font-size: 28px; font-weight: 700; }}
    p {{ margin: 0 0 24px; color: #5f6368; line-height: 1.5; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }}
    a {{ display: block; padding: 18px; border: 1px solid #dfe3e8; border-radius: 8px; background: #fff; color: #174ea6; text-decoration: none; box-shadow: 0 1px 2px rgba(60, 64, 67, 0.08); }}
    a:hover {{ border-color: #8ab4f8; box-shadow: 0 2px 8px rgba(60, 64, 67, 0.16); }}
    .name {{ display: block; color: #202124; font-weight: 700; margin-bottom: 8px; }}
    .meta {{ display: block; color: #5f6368; font-size: 13px; }}
  </style>
</head>
<body>
  <main>
    <h1>PAM 交互式 3D 结果</h1>
    <p>打开下面任意一个结果后，可以拖动旋转、滚轮缩放；用时间范围菜单切换完整波形或首次到达局部，并通过图例区分全局先验、单点内部估计、低可信候选和模板峰值。</p>
    <div class="grid">
{cards}
    </div>
  </main>
</body>
</html>
""".format(cards="\n".join(cards))
    output_path = output_dir / "index.html"
    output_path.write_text(page, encoding="utf-8")
    return output_path
