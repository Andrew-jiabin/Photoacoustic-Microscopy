from __future__ import annotations

import argparse
from pathlib import Path
from pathlib import PureWindowsPath
import json
import math
import subprocess
import re

import numpy as np
from scipy.signal import hilbert

from .core import load_pam_file, parse_slice, resolve_slice
from .result_index import write_result_index

DEFAULT_REMOTE_HOST = "PAM"
DEFAULT_REMOTE_PROJECT_ROOT = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy"
DEFAULT_REMOTE_DATA_DIR = DEFAULT_REMOTE_PROJECT_ROOT + r"\data"


def _remote_suffix(spec: str, remote_data_dir: str) -> str | None:
    text = str(spec).strip().strip('"').replace("\\", "/")
    lower = text.lower()
    if lower in {"./data", ".", "data"}:
        return ""
    for prefix in ("./data/", "data/"):
        if lower.startswith(prefix):
            return text[len(prefix):]
    remote_prefix = remote_data_dir.replace("\\", "/").rstrip("/") + "/"
    if lower.startswith(remote_prefix.lower()):
        return text[len(remote_prefix):]
    return None


def remote_path_for_spec(spec: str, remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR) -> str | None:
    text = str(spec).strip().strip('"')
    suffix = _remote_suffix(text, remote_data_dir)
    if suffix is not None:
        return str(PureWindowsPath(remote_data_dir) / PureWindowsPath(suffix)) if suffix else remote_data_dir
    normalized = text.replace("/", "\\")
    if re.match(r"^[A-Za-z]:\\", normalized):
        return normalized
    return None

SAMPLE_RATE_GHZ = 4.0
SAMPLE_INTERVAL_NS = 0.25


def _remote_suffix_for_local_data(path_text: str, remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR) -> str | None:
    return remote_path_for_spec(str(path_text).strip().strip('"'), remote_data_dir)


def _scp_remote_mat(host: str, remote_file: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    remote_file_posix = remote_file.replace("\\", "/")
    remote = f"{host}:{remote_file_posix}"
    subprocess.run(["scp", remote, str(destination)], check=True)
    return destination


def _resolve_input_path(
    input_spec: str,
    output_dir: Path,
    remote_host: str,
    remote_data_dir: str,
) -> tuple[Path, str]:
    local_guess = Path(input_spec)
    if local_guess.exists():
        return local_guess, "local"

    remote_path = _remote_suffix_for_local_data(input_spec, remote_data_dir)
    if remote_path is None:
        raise FileNotFoundError(f"Cannot find input locally and cannot parse remote reference: {input_spec}")

    remote_name = Path(remote_path).name
    if not remote_name.lower().endswith(".mat"):
        raise FileNotFoundError(f"Remote reference is not a .mat file: {input_spec}")

    cached = output_dir / "remote_cache" / remote_name
    if not cached.exists():
        _scp_remote_mat(remote_host, remote_path, cached)
    return cached, "remote"


def _to_js_matrix(matrix: np.ndarray) -> list[list[float | None]]:
    out: list[list[float | None]] = []
    for row in np.asarray(matrix, dtype=float).tolist():
        out.append([float(f"{float(v):.6g}") if (v is not None and math.isfinite(float(v))) else None for v in row])
    return out


def _group_lines(points, key_name: str) -> dict[float, list]:
    groups: dict[float, list] = {}
    for point in points:
        fixed = round(float(getattr(point, key_name)), 12)
        groups.setdefault(fixed, []).append(point)
    return groups


def _collect_point_amplitudes(points, data: dict, baseline: tuple[int, int], use_hilbert: bool):
    b0, b1 = baseline
    point_amplitudes: dict = {}
    for point in points:
        waveform = np.asarray(data[point.key], dtype=float).ravel()
        if waveform.size == 0:
            point_amplitudes[point.key] = np.array([], dtype=float)
            continue
        baseline_start = int(max(0, min(b0, waveform.size)))
        baseline_stop = int(max(baseline_start + 1, min(b1, waveform.size)))
        centered = waveform - float(np.median(waveform[baseline_start:baseline_stop]))
        if use_hilbert:
            centered = np.abs(hilbert(centered))
        point_amplitudes[point.key] = np.abs(centered)
    return point_amplitudes


def _build_line_payload(points, point_amplitudes, sample_indices: np.ndarray, fixed_axis: str, scan_axis: str):
    groups = _group_lines(points, fixed_axis)
    payload: list[dict] = []
    for fixed_value in sorted(groups):
        line_points = sorted(groups[fixed_value], key=lambda p: float(getattr(p, scan_axis)))
        scan_coords = [float(getattr(point, scan_axis)) for point in line_points]
        matrix = np.full((len(sample_indices), len(line_points)), np.nan, dtype=float)

        for col, point in enumerate(line_points):
            amp = point_amplitudes.get(point.key, np.array([], dtype=float))
            if amp.size == 0:
                continue
            valid = sample_indices < amp.size
            target_rows = np.where(valid)[0]
            matrix[target_rows, col] = amp[sample_indices[valid]]

        payload.append(
            {
                "fixed_value": float(fixed_value),
                "scan_axis_coords": scan_coords,
                "matrix": _to_js_matrix(matrix),
                "point_count": int(len(line_points)),
            }
        )
    return payload


def _build_xy_payload(points, sample_indices: np.ndarray):
    if not points:
        return {
            "x_coords": [],
            "y_coords": [],
            "sample_count": int(len(sample_indices)),
            "point_count": 0,
            "matrix_shape": [0, 0],
        }

    x_values = sorted({round(float(p.x), 12) for p in points})
    y_values = sorted({round(float(p.y), 12) for p in points})

    return {
        "x_coords": [float(v) for v in x_values],
        "y_coords": [float(v) for v in y_values],
        "sample_count": int(len(sample_indices)),
        "point_count": len(points),
        "matrix_shape": [len(y_values), len(x_values)],
    }


def _build_payload(
    path: Path,
    display_window: tuple[int, int],
    baseline: tuple[int, int],
    time_step: int,
    clip_percentile: float,
    use_hilbert: bool,
) -> tuple[dict, dict]:
    data, meta, points = load_pam_file(path)
    if not points:
        raise ValueError(f"No valid waveform points in {path}")

    waveform_length = max(len(np.asarray(data[point.key]).ravel()) for point in points)
    d0, d1 = resolve_slice(display_window, waveform_length)
    b0, b1 = resolve_slice(baseline, waveform_length)
    if time_step < 1:
        raise ValueError("time-step must be >= 1")

    sample_indices = np.arange(d0, d1, time_step, dtype=int)
    if sample_indices.size < 2:
        raise ValueError("display range is too small for interactive plotting")

    point_amplitudes = _collect_point_amplitudes(points, data, (b0, b1), use_hilbert=use_hilbert)
    x_mode_payload = _build_line_payload(points, point_amplitudes, sample_indices, fixed_axis="y", scan_axis="x")
    y_mode_payload = _build_line_payload(points, point_amplitudes, sample_indices, fixed_axis="x", scan_axis="y")
    if not x_mode_payload:
        raise ValueError(f"No Y lines found in {path}")
    if not y_mode_payload:
        raise ValueError(f"No X lines found in {path}")

    xy_payload = _build_xy_payload(points, sample_indices)

    all_values: list[float] = []
    for mode in (x_mode_payload, y_mode_payload):
        for line in mode:
            for row in line["matrix"]:
                for value in row:
                    if value is not None:
                        all_values.append(float(value))
    if not all_values:
        raise ValueError(f"No finite amplitude in {path} for requested window {display_window}")

    arr = np.asarray(all_values, dtype=float)
    color_limit = float(np.nanpercentile(arr, clip_percentile))
    if not math.isfinite(color_limit) or color_limit <= 0:
        color_limit = float(np.nanmax(arr))
    color_limit = max(color_limit, 1e-12)

    sample_times = (sample_indices.astype(float) * SAMPLE_INTERVAL_NS).tolist()

    payload = {
        "file": path.name,
        "source_path": str(path),
        "display_window": [int(d0), int(d1)],
        "baseline": [int(b0), int(b1)],
        "time_step": int(time_step),
        "clip_percentile": float(clip_percentile),
        "sample_rate_ghz": SAMPLE_RATE_GHZ,
        "sample_interval_ns": SAMPLE_INTERVAL_NS,
        "color_limit": float(color_limit),
        "use_hilbert": bool(use_hilbert),
        "sample_indices": sample_indices.astype(int).tolist(),
        "sample_times_ns": sample_times,
        "x_time_mode": {
            "label": "X-时间绝对值图",
            "fixed_axis": "Y (um)",
            "scan_axis": "X (um)",
            "lines": x_mode_payload,
        },
        "y_time_mode": {
            "label": "Y-时间绝对值图",
            "fixed_axis": "X (um)",
            "scan_axis": "Y (um)",
            "lines": y_mode_payload,
        },
        "xy_mode": xy_payload,
        "scan": meta,
    }
    return payload, meta


def _build_check_html(payload: dict, initial_mode: str) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    initial_mode = "x" if initial_mode not in {"x", "y", "xy"} else initial_mode
    title = f"{payload['file']} - X/Y-时间图与XY切面检查器"

    html = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>""" + title + """</title>
  <script src="https://cdn.plot.ly/plotly-2.34.0.min.js"></script>
  <style>
    :root { font-family: "Segoe UI", Tahoma, Arial, sans-serif; color: #1f2937; }
    body { margin: 0; background: #f6f7fb; }
    .toolbar {
      display: grid;
      gap: 8px;
      padding: 10px 14px;
      background: #ffffff;
      border-bottom: 1px solid #d9dde3;
    }
    .group { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .group label { margin-right: 4px; font-size: 14px; }
    .group input, .group select { font-family: inherit; }
    button {
      border: 1px solid #5c6ac4;
      background: #e2e8f0;
      color: #1f2a44;
      border-radius: 6px;
      padding: 6px 10px;
      cursor: pointer;
    }
    button.active { background: #2563eb; color: #ffffff; border-color: #1d4ed8; }
    input[type=number] { width: 100px; padding: 4px 6px; }
    input[type=range] { width: 260px; }
    #status { font-size: 14px; margin-left: 8px; }
    .plot-width-value {
      min-width: 46px;
      text-align: right;
      font-variant-numeric: tabular-nums;
      color: #334155;
    }
    #plot-shell {
      width: 100%;
      height: calc(100vh - 235px);
      min-height: 360px;
      padding: 0 14px 14px;
      display: flex;
      justify-content: center;
      align-items: stretch;
    }
    #plot-frame {
      width: 100%;
      min-width: min(100%, 320px);
      transition: width 0.15s ease;
    }
    #plot { width: 100%; height: 100%; }
    .hint { font-size: 12px; color: #475569; line-height: 1.4; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="toolbar">
    <div class="group">
      <button id="btn-mode-x" class="active">X-时间图</button>
      <button id="btn-mode-y">Y-时间图</button>
      <button id="btn-mode-xy">XY切面</button>
      <span id="agg-controls">
        <span style="margin-left: 4px; color:#64748b;">XY聚合:</span>
        <button id="btn-agg-instant" class="active">当前采样点</button>
        <button id="btn-agg-max">前后邻域最大值</button>
        <button id="btn-agg-mean">前后邻域平均值</button>
        <span style="margin-left: 8px; color:#64748b;">色标范围:</span>
        <button id="btn-color-global">全局归一化</button>
        <button id="btn-color-slice" class="active">切面内归一化</button>
      </span>
      <span id="jump-controls" class="group hidden">
        <span style="margin-left: 4px; color:#64748b;">跨模式跳转:</span>
        <button id="btn-jump-x">跳转到X-时间图</button>
        <button id="btn-jump-y">跳转到Y-时间图</button>
        <button id="btn-jump-xy">跳转到XY切面</button>
        <span id="jump-hint" style="font-size: 12px; color: #334155;"></span>
      </span>
      <span id="status"></span>
    </div>
    <div class="group" id="plot-width-controls">
      <label>图像宽度</label>
      <input id="plot-width" type="range" min="30" max="100" value="45" step="5" />
      <span id="plot-width-label" class="plot-width-value">45%</span>
    </div>
    <div class="group" id="line-controls">
      <label>固定扫描线索引（1-based）</label>
      <input id="line-index" type="range" min="0" max="0" value="0" step="1" />
      <input id="line-index-input" type="number" min="0" max="0" value="0" />
      <span id="line-label"></span>
    </div>
    <div class="group" id="range-controls">
      <label>采样起点（原始索引）</label>
      <input id="start-index" type="range" min="0" max="0" value="0" step="1" />
      <input id="start-index-input" type="number" min="0" max="0" value="0" />
      <label>采样终点（原始索引）</label>
      <input id="end-index" type="range" min="0" max="0" value="0" step="1" />
      <input id="end-index-input" type="number" min="0" max="0" value="0" />
      <span id="range-label"></span>
    </div>
    <div class="group" id="xy-controls">
      <label>XY中心采样点（原始索引）</label>
      <input id="xy-sample-index" type="range" min="0" max="0" value="0" step="1" />
      <input id="xy-sample-index-input" type="number" min="0" max="0" value="0" />
      <label>邻域半宽（预载切片数，最大/平均时>=1）</label>
      <input id="xy-neighborhood" type="range" min="0" max="0" value="0" step="1" />
      <input id="xy-neighborhood-input" type="number" min="0" max="0" value="0" />
      <span id="xy-label"></span>
    </div>
    <div class="hint">
      说明：X-时间图/ Y-时间图表示每一条扫描线“横轴空间坐标，纵轴时间(ns)”。XY切面基于某一个采样点（及其前后邻域）在XY平面上重建幅值图。
    </div>
  </div>
  <div id="plot-shell">
    <div id="plot-frame">
      <div id="plot"></div>
    </div>
  </div>
  <script>
const payload = PAYLOAD_JSON;
const useHilbert = !!payload.use_hilbert;
const sampleTimes = payload.sample_times_ns;
const sampleCount = sampleTimes.length;

const plot = document.getElementById('plot');
const plotFrame = document.getElementById('plot-frame');
const plotWidthSlider = document.getElementById('plot-width');
const plotWidthLabel = document.getElementById('plot-width-label');
const status = document.getElementById('status');
const lineControls = document.getElementById('line-controls');
const rangeControls = document.getElementById('range-controls');
const xyControls = document.getElementById('xy-controls');
const aggControls = document.getElementById('agg-controls');
const lineSlider = document.getElementById('line-index');
const lineInput = document.getElementById('line-index-input');
const startSlider = document.getElementById('start-index');
const startInput = document.getElementById('start-index-input');
const endSlider = document.getElementById('end-index');
const endInput = document.getElementById('end-index-input');
const sampleCenterSlider = document.getElementById('xy-sample-index');
const sampleCenterInput = document.getElementById('xy-sample-index-input');
const neighborhoodSlider = document.getElementById('xy-neighborhood');
const neighborhoodInput = document.getElementById('xy-neighborhood-input');
const jumpControls = document.getElementById('jump-controls');
const jumpHint = document.getElementById('jump-hint');
const btnJumpX = document.getElementById('btn-jump-x');
const btnJumpY = document.getElementById('btn-jump-y');
const btnJumpXY = document.getElementById('btn-jump-xy');

const lineLabel = document.getElementById('line-label');
const rangeLabel = document.getElementById('range-label');
const xyLabel = document.getElementById('xy-label');

const btnModeX = document.getElementById('btn-mode-x');
const btnModeY = document.getElementById('btn-mode-y');
const btnModeXY = document.getElementById('btn-mode-xy');
const btnInstant = document.getElementById('btn-agg-instant');
const btnMax = document.getElementById('btn-agg-max');
const btnMean = document.getElementById('btn-agg-mean');
const btnColorGlobal = document.getElementById('btn-color-global');
const btnColorSlice = document.getElementById('btn-color-slice');

const state = {
  mode: REPLACE_INITIAL_MODE,
  lineIndex: 0,
  sampleStart: 0,
  sampleEnd: Math.max(0, sampleCount - 1),
  xyCenterIndex: 0,
  neighborhood: 1,
  aggregate: "max",
  xyColorMode: "global",
};
let jumpCandidate = null;
let plotClickHandlerInstalled = false;

const pageStateKey = `pam-axis-time-state-v3:${payload.source_path || payload.file}`;
let plotWidthPercent = 45;

function fmt(x) {
  return Number.parseFloat(x).toFixed(4);
}

function rawSampleAt(displayIndex) {
  const clamped = Math.max(0, Math.min(Math.round(displayIndex), sampleCount - 1));
  return Number(payload.sample_indices[clamped]);
}

function nearestDisplayIndexFromRaw(rawValue) {
  const numeric = Number(rawValue);
  if (!Number.isFinite(numeric)) return 0;
  let bestIndex = 0;
  let bestDistance = Infinity;
  for (let i = 0; i < payload.sample_indices.length; i += 1) {
    const distance = Math.abs(Number(payload.sample_indices[i]) - numeric);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = i;
    }
  }
  return bestIndex;
}

function nearestDisplayIndexFromTime(timeValue) {
  const numeric = Number(timeValue);
  if (!Number.isFinite(numeric)) return 0;
  let bestIndex = 0;
  let bestDistance = Infinity;
  for (let i = 0; i < sampleTimes.length; i += 1) {
    const distance = Math.abs(sampleTimes[i] - numeric);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = i;
    }
  }
  return bestIndex;
}

function preloadedSliceIntervalNs() {
  return Number(payload.time_step || 1) * Number(payload.sample_interval_ns || 0);
}

function preloadedSliceIntervalText() {
  const step = Math.max(1, Number(payload.time_step || 1));
  if (step === 1) return "每个原始采样点均已预载为可选切面";
  return `当前页每 ${step} 个原始采样点预载 1 个可选切面（切片间隔 ${fmt(preloadedSliceIntervalNs())} ns）`;
}

function clampPlotWidth(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 45;
  return Math.min(100, Math.max(30, Math.round(numeric / 5) * 5));
}

function normalizeWholeNumber(value, fallback) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.round(numeric) : fallback;
}

function buildJumpCandidateByMode(mode, point) {
  const pointNumber = Array.isArray(point.pointNumber) ? point.pointNumber : [];
  if (mode === "x" || mode === "y") {
    const modeData = mode === "x" ? payload.x_time_mode : payload.y_time_mode;
    const line = modeData.lines[state.lineIndex];
    if (!line) return null;
    const start = Math.min(state.sampleStart, state.sampleEnd - 1);
    const clickedRow = Number.isInteger(pointNumber[1]) ? pointNumber[1] : -1;
    const displayIndex = clickedRow >= 0 ? start + clickedRow : nearestDisplayIndexFromTime(point.y);
    if (!Number.isFinite(displayIndex)) return null;
    const sampleIndex = Math.max(0, Math.min(sampleCount - 1, Math.round(displayIndex)));
    const scanCoord = Number(point.x);
    const fixedCoord = Number(line.fixed_value);
    if (!Number.isFinite(scanCoord) || !Number.isFinite(fixedCoord)) return null;
    return {
      sampleIndex,
      fixedX: mode === "x" ? scanCoord : fixedCoord,
      fixedY: mode === "x" ? fixedCoord : scanCoord,
      originMode: mode,
      sampleTime: sampleTimes[sampleIndex],
    };
  }

  if (mode === "xy" && payload.xy_mode) {
    const fixedX = Number(point.x);
    const fixedY = Number(point.y);
    if (!Number.isFinite(fixedX) || !Number.isFinite(fixedY)) return null;
    return {
      sampleIndex: Math.max(0, Math.min(sampleCount - 1, Math.round(state.xyCenterIndex))),
      fixedX,
      fixedY,
      originMode: "xy",
      sampleTime: sampleTimes[Math.max(0, Math.min(sampleCount - 1, state.xyCenterIndex))],
    };
  }

  return null;
}

function closestLineIndexByFixed(modeLines, targetValue) {
  if (!Array.isArray(modeLines) || modeLines.length === 0 || !Number.isFinite(targetValue)) return null;
  let bestIndex = 0;
  let bestDistance = Infinity;
  for (let i = 0; i < modeLines.length; i += 1) {
    const line = modeLines[i];
    const fixedValue = Number(line.fixed_value);
    if (!Number.isFinite(fixedValue)) continue;
    const distance = Math.abs(fixedValue - targetValue);
    if (distance < bestDistance) {
      bestDistance = distance;
      bestIndex = i;
    }
  }
  return bestIndex;
}

function applyJumpCandidate(candidate) {
  jumpCandidate = candidate;
  if (!candidate) {
    jumpControls.classList.add("hidden");
    jumpHint.textContent = "点击时域/XY图中的任一点后，可选择跳转";
    btnJumpX.classList.add("hidden");
    btnJumpY.classList.add("hidden");
    btnJumpXY.classList.add("hidden");
    return;
  }
  jumpControls.classList.remove("hidden");
  if (state.mode === "x") {
    btnJumpX.classList.add("hidden");
    btnJumpY.classList.remove("hidden");
    btnJumpXY.classList.remove("hidden");
  } else if (state.mode === "y") {
    btnJumpY.classList.add("hidden");
    btnJumpX.classList.remove("hidden");
    btnJumpXY.classList.remove("hidden");
  } else {
    btnJumpXY.classList.add("hidden");
    btnJumpX.classList.remove("hidden");
    btnJumpY.classList.remove("hidden");
  }
  jumpHint.textContent = `已选取点: t=${fmt(candidate.sampleTime)} ns, 原始采样=${rawSampleAt(candidate.sampleIndex)}`;
}

function jumpToMode(modeTarget) {
  if (!jumpCandidate) return;
  const candidate = jumpCandidate;
  if (modeTarget === "xy") {
    state.mode = "xy";
    state.xyCenterIndex = candidate.sampleIndex;
    setMode("xy");
    return;
  }
  if (modeTarget === "x") {
    const lineIndex = closestLineIndexByFixed(payload.x_time_mode.lines, candidate.fixedY);
    if (lineIndex === null) return;
    state.mode = "x";
    state.lineIndex = lineIndex;
  } else if (modeTarget === "y") {
    const lineIndex = closestLineIndexByFixed(payload.y_time_mode.lines, candidate.fixedX);
    if (lineIndex === null) return;
    state.mode = "y";
    state.lineIndex = lineIndex;
  }
  state.sampleStart = Math.max(0, candidate.sampleIndex - 2);
  state.sampleEnd = Math.min(sampleCount - 1, candidate.sampleIndex + 3);
  setMode(state.mode);
}

function restorePageState() {
  try {
    const saved = JSON.parse(localStorage.getItem(pageStateKey) || "{}");
    if (!saved || typeof saved !== "object") return;
    if (saved.mode === "x" || saved.mode === "y" || saved.mode === "xy") state.mode = saved.mode;
    state.lineIndex = normalizeWholeNumber(saved.lineIndex, state.lineIndex);
    state.sampleStart = normalizeWholeNumber(saved.sampleStart, state.sampleStart);
    state.sampleEnd = normalizeWholeNumber(saved.sampleEnd, state.sampleEnd);
    state.xyCenterIndex = normalizeWholeNumber(saved.xyCenterIndex, state.xyCenterIndex);
    state.neighborhood = normalizeWholeNumber(saved.neighborhood, state.neighborhood);
    if (saved.aggregate === "instant" || saved.aggregate === "max" || saved.aggregate === "mean") {
      state.aggregate = saved.aggregate;
    }
    if (saved.xyColorMode === "global" || saved.xyColorMode === "slice") {
      state.xyColorMode = saved.xyColorMode;
    }
    plotWidthPercent = clampPlotWidth(saved.plotWidthPercent ?? plotWidthPercent);
  } catch (error) {
    plotWidthPercent = 45;
  }
}

function persistPageState() {
  localStorage.setItem(pageStateKey, JSON.stringify({
    mode: state.mode,
    lineIndex: state.lineIndex,
    sampleStart: state.sampleStart,
    sampleEnd: state.sampleEnd,
    xyCenterIndex: state.xyCenterIndex,
    neighborhood: state.neighborhood,
    aggregate: state.aggregate,
    xyColorMode: state.xyColorMode,
    plotWidthPercent: plotWidthPercent,
  }));
}

function applyPlotWidth() {
  plotFrame.style.width = `${plotWidthPercent}%`;
  plotWidthSlider.value = String(plotWidthPercent);
  plotWidthLabel.textContent = `${plotWidthPercent}%`;
  window.requestAnimationFrame(() => {
    if (window.Plotly && plot.data) {
      Plotly.Plots.resize(plot);
    }
  });
}

function ensurePlotClickHandler() {
  if (plotClickHandlerInstalled || typeof plot.on !== "function") return;
  plot.on("plotly_click", (eventData) => {
    if (!eventData || !Array.isArray(eventData.points) || eventData.points.length === 0) return;
    const point = eventData.points[0];
    const candidate = buildJumpCandidateByMode(state.mode, point);
    if (!candidate) return;
    applyJumpCandidate(candidate);
    draw();
  });
  plotClickHandlerInstalled = true;
}

function resizePlotSoon() {
  window.requestAnimationFrame(() => {
    if (window.Plotly && plot.data) {
      Plotly.Plots.resize(plot);
    }
  });
}

restorePageState();

function clampState() {
  const modeData = state.mode === "x" ? payload.x_time_mode : (state.mode === "y" ? payload.y_time_mode : null);
  const lineCount = modeData ? modeData.lines.length : 0;
  state.lineIndex = Math.max(0, Math.min(state.lineIndex, Math.max(0, lineCount - 1)));

  startSlider.min = String(rawSampleAt(0));
  endSlider.min = String(rawSampleAt(Math.min(1, sampleCount - 1)));
  startSlider.max = String(rawSampleAt(Math.max(0, sampleCount - 2)));
  endSlider.max = String(rawSampleAt(Math.max(1, sampleCount - 1)));
  startInput.min = startSlider.min;
  endInput.min = endSlider.min;
  startInput.max = startSlider.max;
  endInput.max = endSlider.max;

  if (state.sampleStart < 0) state.sampleStart = 0;
  if (state.sampleStart > sampleCount - 2) state.sampleStart = Math.max(0, sampleCount - 2);
  if (state.sampleEnd < 1) state.sampleEnd = 1;
  if (state.sampleEnd > sampleCount - 1) state.sampleEnd = sampleCount - 1;
  if (state.sampleStart >= state.sampleEnd) state.sampleStart = Math.max(0, state.sampleEnd - 1);

  startSlider.step = String(Math.max(1, Number(payload.time_step || 1)));
  endSlider.step = String(Math.max(1, Number(payload.time_step || 1)));
  startInput.step = String(Math.max(1, Number(payload.time_step || 1)));
  endInput.step = String(Math.max(1, Number(payload.time_step || 1)));
  startSlider.value = String(rawSampleAt(state.sampleStart));
  startInput.value = String(rawSampleAt(state.sampleStart));
  endSlider.value = String(rawSampleAt(state.sampleEnd));
  endInput.value = String(rawSampleAt(state.sampleEnd));

  if (state.mode === "xy") {
    lineControls.classList.add("hidden");
    rangeControls.classList.add("hidden");
    xyControls.classList.remove("hidden");
    aggControls.classList.remove("hidden");
  } else {
    lineControls.classList.remove("hidden");
    rangeControls.classList.remove("hidden");
    xyControls.classList.add("hidden");
    aggControls.classList.add("hidden");
    lineSlider.max = String(Math.max(0, lineCount - 1));
    lineInput.max = String(Math.max(1, lineCount));
    lineSlider.value = String(state.lineIndex);
    lineInput.value = String(state.lineIndex + 1);
  }

  sampleCenterSlider.min = String(rawSampleAt(0));
  sampleCenterSlider.max = String(rawSampleAt(Math.max(0, sampleCount - 1)));
  sampleCenterInput.min = sampleCenterSlider.min;
  sampleCenterInput.max = sampleCenterSlider.max;
  sampleCenterSlider.step = String(Math.max(1, Number(payload.time_step || 1)));
  sampleCenterInput.step = String(Math.max(1, Number(payload.time_step || 1)));
  if (state.xyCenterIndex < 0) state.xyCenterIndex = 0;
  if (state.xyCenterIndex > sampleCount - 1) state.xyCenterIndex = sampleCount - 1;
  sampleCenterSlider.value = String(rawSampleAt(state.xyCenterIndex));
  sampleCenterInput.value = String(rawSampleAt(state.xyCenterIndex));

  const maxNeighborhood = Math.max(1, sampleCount - 1);
  if (state.aggregate === "instant") {
    state.neighborhood = 0;
  } else if (state.neighborhood < 1) {
    state.neighborhood = 1;
  }
  if (state.neighborhood > maxNeighborhood) state.neighborhood = maxNeighborhood;
  neighborhoodSlider.min = state.aggregate === "instant" ? "0" : "1";
  neighborhoodInput.min = state.aggregate === "instant" ? "0" : "1";
  neighborhoodSlider.max = String(maxNeighborhood);
  neighborhoodInput.max = String(maxNeighborhood);
  neighborhoodSlider.disabled = state.aggregate === "instant";
  neighborhoodInput.disabled = state.aggregate === "instant";
  neighborhoodSlider.value = String(state.neighborhood);
  neighborhoodInput.value = String(state.neighborhood);
}

function setMode(mode) {
  state.mode = mode;
  btnModeX.classList.toggle("active", mode === "x");
  btnModeY.classList.toggle("active", mode === "y");
  btnModeXY.classList.toggle("active", mode === "xy");
  btnInstant.classList.toggle("active", state.aggregate === "instant");
  btnMax.classList.toggle("active", state.aggregate === "max");
  btnMean.classList.toggle("active", state.aggregate === "mean");
  btnColorGlobal.classList.toggle("active", state.xyColorMode === "global");
  btnColorSlice.classList.toggle("active", state.xyColorMode === "slice");
  applyJumpCandidate(jumpCandidate);
  clampState();
  draw();
}

function setAggregate(kind) {
  state.aggregate = kind;
  if (kind !== "instant" && state.neighborhood < 1) state.neighborhood = 1;
  btnInstant.classList.toggle("active", kind === "instant");
  btnMax.classList.toggle("active", kind === "max");
  btnMean.classList.toggle("active", kind === "mean");
  clampState();
  draw();
}

function setXYColorMode(mode) {
  state.xyColorMode = mode === "global" ? "global" : "slice";
  btnColorGlobal.classList.toggle("active", state.xyColorMode === "global");
  btnColorSlice.classList.toggle("active", state.xyColorMode === "slice");
  draw();
}

function toTime(idx) {
  return sampleTimes[idx];
}

function aggregationWindowSummary(kind, centerIndex, halfWindow) {
  if (kind === "instant") return `当前采样点（原始采样=${rawSampleAt(centerIndex)}，时间=${fmt(toTime(centerIndex))} ns）`;
  const effectiveHalfWindow = Math.max(1, halfWindow);
  const start = Math.max(0, centerIndex - effectiveHalfWindow);
  const end = Math.min(sampleCount - 1, centerIndex + effectiveHalfWindow);
  const pointCount = end - start + 1;
  const windowSpanNs = Math.max(0, pointCount - 1) * preloadedSliceIntervalNs();
  const prefix = kind === "max" ? "邻域最大值" : "邻域平均值";
  return `${prefix} ±${effectiveHalfWindow}（实际 ${pointCount} 个预载切片，时间跨度 ${fmt(windowSpanNs)} ns）`;
}

function getLineData(modeName) {
  const modeData = modeName === "x" ? payload.x_time_mode : payload.y_time_mode;
  const line = modeData.lines[state.lineIndex];
  if (!line) return {trace: null, layout: null};
  const start = Math.min(state.sampleStart, state.sampleEnd - 1);
  const end = Math.max(state.sampleStart + 1, state.sampleEnd);
  const y = sampleTimes.slice(start, end);
  const z = line.matrix.slice(start, end);
  return {
    trace: {
      type: "heatmap",
      x: line.scan_axis_coords,
      y: y,
      z: z,
      colorscale: "Magma",
      zmin: 0,
      zmax: payload.color_limit,
      colorbar: { title: "幅值(ADC)" },
      hovertemplate: `${modeData.scan_axis}=%{x:.6g}<br>时间(ns)=%{y:.6f}<br>幅值=%{z:.4g}<extra></extra>`,
    },
    layout: {
      title: { text: `${payload.file} | ${modeData.label}（固定${modeData.fixed_axis}）`, x: 0.02 },
      xaxis: { title: modeData.scan_axis },
      yaxis: { title: "时间 (ns)" },
      margin: { l: 60, r: 18, t: 48, b: 48 },
    },
    line: line,
    modeData,
    selectedStart: start,
    selectedEnd: end
  };
}

function aggregatePoint(values, center, halfWindow, kind) {
  if (!Array.isArray(values) || values.length === 0) return null;
  const n = values.length;
  let start = Math.max(0, center - halfWindow);
  let end = Math.min(n - 1, center + halfWindow);
  let hasFinite = false;
  let maxVal = -Infinity;
  let sum = 0.0;
  let count = 0;
  for (let i = start; i <= end; i += 1) {
    const v = values[i];
    if (v === null || typeof v !== "number" || !Number.isFinite(v)) continue;
    if (kind === "max" && v > maxVal) maxVal = v;
    hasFinite = true;
    if (kind === "mean") {
      sum += v;
      count += 1;
    }
  }
  if (kind === "max") return hasFinite ? maxVal : null;
  if (kind === "mean") return count === 0 ? null : sum / count;
  const centerValue = values[center];
  return (centerValue === null || typeof centerValue !== "number" || !Number.isFinite(centerValue)) ? null : centerValue;
}

function computeColorLimit(values, percentile, fallback) {
  const finite = values.filter((value) => typeof value === "number" && Number.isFinite(value));
  if (!finite.length) return fallback;
  const sorted = finite.slice().sort((a, b) => a - b);
  const pct = Math.min(100, Math.max(0, Number(percentile ?? 99.5)));
  const rank = (pct / 100) * (sorted.length - 1);
  const low = Math.floor(rank);
  const high = Math.ceil(rank);
  const blend = rank - low;
  const value = low === high ? sorted[low] : (sorted[low] * (1 - blend) + sorted[high] * blend);
  if (Number.isFinite(value) && value > 0) return value;
  const maxValue = sorted[sorted.length - 1];
  return Number.isFinite(maxValue) && maxValue > 0 ? maxValue : fallback;
}

function getXYData() {
  const payloadXY = payload.xy_mode;
  const yCount = payloadXY.matrix_shape[0];
  const xCount = payloadXY.matrix_shape[1];
  const effectiveNeighborhood = state.aggregate === "instant" ? 0 : Math.max(1, state.neighborhood);
  const z = Array.from({ length: yCount }, () => Array(xCount).fill(null));
  const finiteValues = [];
  for (let yi = 0; yi < payload.x_time_mode.lines.length; yi += 1) {
    const line = payload.x_time_mode.lines[yi];
    for (let xi = 0; xi < line.scan_axis_coords.length; xi += 1) {
      const values = line.matrix.map(row => row[xi]);
      const value = aggregatePoint(values, state.xyCenterIndex, effectiveNeighborhood, state.aggregate);
      if (value !== null && yi >= 0 && yi < yCount && xi >= 0 && xi < xCount) {
        z[yi][xi] = value;
        finiteValues.push(value);
      }
    }
  }
  const aggText = aggregationWindowSummary(state.aggregate, state.xyCenterIndex, effectiveNeighborhood);
  const t = toTime(state.xyCenterIndex);
  const xyColorLimit = state.xyColorMode === "slice"
    ? computeColorLimit(finiteValues, payload.clip_percentile, payload.color_limit)
    : payload.color_limit;
  const colorModeText = state.xyColorMode === "slice" ? "切面内归一化" : "全局归一化";
  return {
    trace: {
      type: "heatmap",
      x: payloadXY.x_coords,
      y: payloadXY.y_coords,
      z: z,
      colorscale: "Magma",
      zmin: 0,
      zmax: xyColorLimit,
      colorbar: { title: "幅值(ADC)" },
      hovertemplate: `X=%{x:.6g} um<br>Y=%{y:.6g} um<br>幅值=%{z:.4g}<extra></extra>`
    },
    layout: {
      title: { text: `${payload.file} | XY切面 | 时间=${fmt(t)} ns | ${aggText} | ${colorModeText}`, x: 0.02 },
      xaxis: { title: "X (um)" },
      yaxis: { title: "Y (um)" },
      margin: { l: 60, r: 18, t: 48, b: 48 },
    },
  };
}

function draw() {
  clampState();
  let result = null;
  if (state.mode === "x" || state.mode === "y") {
    result = getLineData(state.mode);
  } else {
    result = getXYData();
  }
  if (!result || !result.trace) return;
  if (!window.Plotly) {
    status.textContent = "Plotly 未加载完成，请稍等或刷新页面";
    return;
  }
  const plotPromise = Plotly.react(plot, [result.trace], result.layout, {responsive: true, displayModeBar: true});
  Promise.resolve(plotPromise).then(ensurePlotClickHandler).catch((error) => {
    status.textContent = `Plotly 绘图失败: ${error && error.message ? error.message : error}`;
  });

  if (state.mode === "x" || state.mode === "y") {
    const line = result.line;
    const modeData = result.modeData;
    const labelValue = `${modeData.fixed_axis}=${fmt(line.fixed_value)}`;
    lineLabel.textContent = `${labelValue}（第 ${state.lineIndex + 1} / ${modeData.lines.length} 条线）`;
    xyLabel.textContent = "";
    const s = state.sampleStart;
    const e = state.sampleEnd;
    const t0 = toTime(s);
    const t1 = toTime(e);
    rangeLabel.textContent = `原始采样索引 [${rawSampleAt(s)}, ${rawSampleAt(e)}]，对应时间 [${fmt(t0)} ns, ${fmt(t1)} ns]`;
  } else {
    const payloadXY = payload.xy_mode;
    lineLabel.textContent = "";
    rangeLabel.textContent = "";
    const effectiveNeighborhood = state.aggregate === "instant" ? 0 : Math.max(1, state.neighborhood);
    const neighborhoodText = aggregationWindowSummary(state.aggregate, state.xyCenterIndex, effectiveNeighborhood);
    const colorModeText = state.xyColorMode === "slice" ? "切面内归一化" : "全局归一化";
    xyLabel.textContent = `XY网格：${payloadXY.matrix_shape[1]} × ${payloadXY.matrix_shape[0]}（X × Y）；中心原始采样=${rawSampleAt(state.xyCenterIndex)}，时间=${fmt(toTime(state.xyCenterIndex))} ns；${neighborhoodText}；${colorModeText}`;
  }
  status.textContent = `当前模式：${state.mode.toUpperCase()} | 可选时间切片数：${payload.sample_indices.length} | 采样率：${payload.sample_rate_ghz.toFixed(1)} GHz | 原始每点间隔：${payload.sample_interval_ns.toFixed(3)} ns | ${preloadedSliceIntervalText()} | ${useHilbert ? "Hilbert包络" : "时域绝对值"}`;
  persistPageState();
}

btnModeX.addEventListener("click", () => setMode("x"));
btnModeY.addEventListener("click", () => setMode("y"));
btnModeXY.addEventListener("click", () => setMode("xy"));
btnInstant.addEventListener("click", () => setAggregate("instant"));
btnMax.addEventListener("click", () => setAggregate("max"));
btnMean.addEventListener("click", () => setAggregate("mean"));
btnColorGlobal.addEventListener("click", () => setXYColorMode("global"));
btnColorSlice.addEventListener("click", () => setXYColorMode("slice"));
btnJumpX.addEventListener("click", () => jumpToMode("x"));
btnJumpY.addEventListener("click", () => jumpToMode("y"));
btnJumpXY.addEventListener("click", () => jumpToMode("xy"));

lineSlider.addEventListener("input", (e) => {
  state.lineIndex = Number(e.target.value);
  draw();
});
lineInput.addEventListener("input", (e) => {
  const max = Number(lineInput.max || "0");
  const value = Number(e.target.value);
  if (!Number.isFinite(value)) return;
  state.lineIndex = Math.max(1, Math.min(Math.round(value), max)) - 1;
  clampState();
  draw();
});
startSlider.addEventListener("input", (e) => {
  state.sampleStart = nearestDisplayIndexFromRaw(e.target.value);
  if (state.sampleStart >= state.sampleEnd) state.sampleStart = Math.max(0, state.sampleEnd - 1);
  draw();
});
startInput.addEventListener("input", (e) => {
  const value = Number(e.target.value);
  if (!Number.isFinite(value)) return;
  state.sampleStart = Math.max(0, Math.min(nearestDisplayIndexFromRaw(value), sampleCount - 2));
  if (state.sampleStart >= state.sampleEnd) state.sampleStart = Math.max(0, state.sampleEnd - 1);
  clampState();
  draw();
});
endSlider.addEventListener("input", (e) => {
  state.sampleEnd = nearestDisplayIndexFromRaw(e.target.value);
  if (state.sampleEnd <= state.sampleStart) state.sampleEnd = Math.min(sampleCount - 1, state.sampleStart + 1);
  draw();
});
endInput.addEventListener("input", (e) => {
  const value = Number(e.target.value);
  if (!Number.isFinite(value)) return;
  state.sampleEnd = Math.min(Math.max(1, nearestDisplayIndexFromRaw(value)), sampleCount - 1);
  if (state.sampleEnd <= state.sampleStart) state.sampleStart = Math.max(0, state.sampleEnd - 1);
  clampState();
  draw();
});
sampleCenterSlider.addEventListener("input", (e) => {
  state.xyCenterIndex = nearestDisplayIndexFromRaw(e.target.value);
  draw();
});
sampleCenterInput.addEventListener("input", (e) => {
  const value = Number(e.target.value);
  if (!Number.isFinite(value)) return;
  state.xyCenterIndex = Math.max(0, Math.min(nearestDisplayIndexFromRaw(value), sampleCount - 1));
  clampState();
  draw();
});
neighborhoodSlider.addEventListener("input", (e) => {
  state.neighborhood = Number(e.target.value);
  draw();
});
neighborhoodInput.addEventListener("input", (e) => {
  const value = Number(e.target.value);
  if (!Number.isFinite(value)) return;
  state.neighborhood = Math.max(0, Math.min(Math.round(value), sampleCount - 1));
  clampState();
  draw();
});

plotWidthSlider.addEventListener("input", (e) => {
  plotWidthPercent = clampPlotWidth(e.target.value);
  applyPlotWidth();
  persistPageState();
});

window.addEventListener("message", (event) => {
  const data = event.data || {};
  if (data.type === "pam-index-preview-resize") {
    resizePlotSoon();
  }
});

window.addEventListener("resize", resizePlotSoon);

if (window.ResizeObserver) {
  const plotResizeObserver = new ResizeObserver(resizePlotSoon);
  plotResizeObserver.observe(plotFrame);
}

function isEditableTarget(target) {
  return target instanceof HTMLElement
    && target.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""]') !== null;
}

document.addEventListener("keydown", (e) => {
  if (isEditableTarget(e.target)) return;
  if (e.key === "x" || e.key === "X") { setMode("x"); return; }
  if (e.key === "y" || e.key === "Y") { setMode("y"); return; }
  if (e.key === "z" || e.key === "Z") { setMode("xy"); return; }
  const lineData = state.mode === "x" ? payload.x_time_mode.lines : payload.y_time_mode.lines;
  if (e.key === "ArrowLeft") {
    if (state.mode === "xy") {
      state.xyCenterIndex = Math.max(0, state.xyCenterIndex - 1);
    } else {
      state.lineIndex = Math.max(0, state.lineIndex - 1);
    }
    clampState();
    draw();
    return;
  }
  if (e.key === "ArrowRight") {
    if (state.mode === "xy") {
      state.xyCenterIndex = Math.min(sampleCount - 1, state.xyCenterIndex + 1);
    } else {
      state.lineIndex = Math.min(Math.max(0, lineData.length - 1), state.lineIndex + 1);
    }
    clampState();
    draw();
    return;
  }
  if (e.key === "ArrowUp" && state.mode === "xy" && state.aggregate !== "instant") {
    state.neighborhood = Math.min(sampleCount - 1, state.neighborhood + 1);
    clampState();
    draw();
    return;
  }
  if (e.key === "ArrowDown" && state.mode === "xy" && state.aggregate !== "instant") {
    state.neighborhood = Math.max(1, state.neighborhood - 1);
    clampState();
    draw();
    return;
  }
});

applyPlotWidth();
setMode(state.mode || """ + "REPLACE_INITIAL_MODE" + """);
  </script>
</body>
</html>
"""
    return html.replace("PAYLOAD_JSON", payload_json).replace("REPLACE_INITIAL_MODE", f'"{initial_mode}"')


def write_axis_time_checker(
    input_spec: str,
    output_dir: Path,
    display_window: tuple[int, int] = (0, -1),
    baseline: tuple[int, int] = (0, 100),
    time_step: int = 1,
    clip_percentile: float = 99.5,
    initial_mode: str = "x",
    use_hilbert: bool = False,
    remote_host: str = DEFAULT_REMOTE_HOST,
    remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR,
) -> dict:
    output_dir = output_dir.resolve()
    input_path, source = _resolve_input_path(
        input_spec=input_spec,
        output_dir=output_dir,
        remote_host=remote_host,
        remote_data_dir=remote_data_dir,
    )
    payload, meta = _build_payload(
        path=input_path,
        display_window=display_window,
        baseline=baseline,
        time_step=time_step,
        clip_percentile=clip_percentile,
        use_hilbert=use_hilbert,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    html = _build_check_html(payload, initial_mode=initial_mode)
    html_path = output_dir / f"{input_path.stem}_axis_time_checker.html"
    html_path.write_text(html, encoding="utf-8")

    summary = {
        "file": input_path.name,
        "source_path": str(input_path),
        "source_kind": source,
        "output_html": str(html_path),
        "initial_mode": initial_mode,
        "display_window": [int(display_window[0]), int(display_window[1])],
        "baseline": [int(baseline[0]), int(baseline[1])],
        "time_step": int(time_step),
        "clip_percentile": float(clip_percentile),
        "use_hilbert": bool(use_hilbert),
        "color_limit": float(payload["color_limit"]),
        "sample_count": len(payload["sample_indices"]),
        "line_counts": {
            "x_mode": len(payload["x_time_mode"]["lines"]),
            "y_mode": len(payload["y_time_mode"]["lines"]),
        },
        "xy_points": payload["xy_mode"]["point_count"],
        "xy_shape": payload["xy_mode"]["matrix_shape"],
        "scan": meta,
    }
    summary_path = output_dir / f"{input_path.stem}_axis_time_checker_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"html": str(html_path), "summary": str(summary_path), **summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate interactive X/Y-time checker and XY time-slice projection."
    )
    parser.add_argument("--input", required=True, help="Local path or remote reference like ./data/file.mat.")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for HTML and summary outputs.")
    parser.add_argument("--display-window", default="0:end", help="Plot sample window, e.g. 0:end or 0:4000.")
    parser.add_argument("--baseline", default="0:100", help="Median baseline window for centering each waveform.")
    parser.add_argument("--time-step", type=int, default=1, help="Stride of sampling points used for the map.")
    parser.add_argument("--clip-percentile", type=float, default=99.5, help="Upper color percentile.")
    parser.add_argument("--mode", choices=("x", "y", "xy"), default="x", help="Default mode.")
    parser.add_argument("--hilbert", action="store_true", help="Use Hilbert envelope instead of raw absolute waveform.")
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST, help="SSH alias for PAM remote host.")
    parser.add_argument(
        "--remote-data-dir",
        default=DEFAULT_REMOTE_DATA_DIR,
        help="Remote PAM data root used by ./data references.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_axis_time_checker(
        input_spec=str(args.input),
        output_dir=args.output_dir,
        display_window=parse_slice(args.display_window, 0, -1),
        baseline=parse_slice(args.baseline, 0, 100),
        time_step=args.time_step,
        clip_percentile=args.clip_percentile,
        initial_mode=args.mode,
        use_hilbert=args.hilbert,
        remote_host=args.remote_host,
        remote_data_dir=args.remote_data_dir,
    )
    index_result = write_result_index(Path(__file__).resolve().parents[4])
    print(result["html"])
    print(result["summary"])
    print(index_result["html"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
