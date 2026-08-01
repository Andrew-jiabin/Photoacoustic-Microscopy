from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re


FILENAME_TIMESTAMP_RE = re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")


def parse_filename_timestamp(name: str) -> datetime | None:
    match = FILENAME_TIMESTAMP_RE.search(name)
    if not match:
        return None
    return datetime.strptime(match.group("stamp"), "%Y-%m-%d_%H-%M-%S")


def format_filename_timestamp(name: str) -> str | None:
    parsed = parse_filename_timestamp(name)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _relative_href(base_dir: Path, target: Path) -> str:
    return os.path.relpath(target, base_dir).replace("\\", "/")


def _dataset_stem_from_interactive(path: Path) -> str:
    return path.name.removesuffix("_interactive_3d.html")


def _dataset_stem_from_axis_summary(path: Path) -> str:
    return path.name.removesuffix("_axis_time_checker_summary.json")


def _artifact_priority(kind: str) -> int:
    order = {
        "axis_time_html": 0,
        "interactive_3d": 1,
        "x_time_png": 2,
        "workflow_manifest": 3,
        "axis_time_summary": 4,
    }
    return order.get(kind, 99)


def _artifact_label(kind: str, source_name: str) -> str:
    labels = {
        "axis_time_html": f"Axis-time 交互页 [{source_name}]",
        "interactive_3d": f"3D 交互页 [{source_name}]",
        "x_time_png": f"X-时间图 [{source_name}]",
        "workflow_manifest": f"Workflow manifest [{source_name}]",
        "axis_time_summary": f"Axis-time 摘要 [{source_name}]",
    }
    return labels.get(kind, f"{kind} [{source_name}]")


def _infer_tags(file_name: str) -> list[str]:
    tags: list[str] = []
    upper = file_name.upper()
    match_d = re.search(r"-D-(\d+)-", upper)
    if match_d:
        tags.append(f"D-{match_d.group(1)}")
    match_avg = re.search(r"-AVER-(\d+)", upper)
    if match_avg:
        tags.append(f"AVER-{match_avg.group(1)}")
    for token in ("TEST1", "TEST2", "TEST3", "TEST4", "TEST5"):
        if token in upper:
            tags.append(token.lower())
    if "NEAR-FIELD" in upper:
        tags.append("near-field")
    if "FAR-FIELD" in upper:
        tags.append("far-field")
    if "SUCCESS" in upper:
        tags.append("success")
    return tags


def _entry_sort_key(entry: dict) -> tuple:
    timestamp = entry.get("timestamp_sort") or ""
    return (timestamp, entry["file"])


def _ensure_entry(entries: dict[str, dict], file_name: str) -> dict:
    entry = entries.get(file_name)
    if entry is None:
        timestamp = parse_filename_timestamp(file_name)
        entry = {
            "id": file_name,
            "file": file_name,
            "stem": Path(file_name).stem,
            "display_time": format_filename_timestamp(file_name),
            "timestamp_sort": timestamp.isoformat() if timestamp else "",
            "tags": _infer_tags(file_name),
            "source_path": None,
            "scan_shape": None,
            "point_count": None,
            "step_um": None,
            "artifacts": [],
            "_seen_paths": set(),
        }
        entries[file_name] = entry
    return entry


def _add_artifact(entry: dict, kind: str, path: Path, index_dir: Path) -> None:
    resolved = str(path.resolve())
    if resolved in entry["_seen_paths"] or not path.exists():
        return
    source_name = path.parent.name
    artifact = {
        "kind": kind,
        "label": _artifact_label(kind, source_name),
        "path": resolved,
        "href": _relative_href(index_dir, path.resolve()),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        "source_name": source_name,
    }
    entry["artifacts"].append(artifact)
    entry["_seen_paths"].add(resolved)


def collect_result_entries(skill_root: Path, index_dir: Path | None = None) -> list[dict]:
    skill_root = Path(skill_root).resolve()
    results_root = skill_root / "workspace" / "results"
    index_dir = (index_dir or results_root / "pam-result-index").resolve()

    entries: dict[str, dict] = {}

    for summary_path in sorted(results_root.rglob("*_axis_time_checker_summary.json")):
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        file_name = str(summary.get("file") or f"{_dataset_stem_from_axis_summary(summary_path)}.mat")
        entry = _ensure_entry(entries, file_name)
        if summary.get("source_path"):
            entry["source_path"] = str(summary["source_path"])
        scan = summary.get("scan") or {}
        if scan:
            entry["scan_shape"] = scan.get("scan_shape") or entry["scan_shape"]
            entry["point_count"] = scan.get("valid_point_count") or scan.get("pos_count") or entry["point_count"]
            entry["step_um"] = scan.get("step_um") if scan.get("step_um") is not None else entry["step_um"]
        output_html = summary.get("output_html")
        if output_html:
            _add_artifact(entry, "axis_time_html", Path(output_html), index_dir)
        _add_artifact(entry, "axis_time_summary", summary_path, index_dir)

    for html_path in sorted(results_root.rglob("*_interactive_3d.html")):
        file_name = f"{_dataset_stem_from_interactive(html_path)}.mat"
        entry = _ensure_entry(entries, file_name)
        _add_artifact(entry, "interactive_3d", html_path, index_dir)

    for manifest_path in sorted(results_root.rglob("workflow_manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        outputs = manifest.get("outputs") or {}
        x_time_maps = outputs.get("x_time_maps") or []
        x_time_by_file: dict[str, Path] = {}
        for item in x_time_maps:
            file_name = str(item.get("file") or "")
            output_path = item.get("output_png")
            purpose = str(item.get("purpose") or "")
            if not file_name or not output_path:
                continue
            if purpose == "center" and file_name not in x_time_by_file:
                x_time_by_file[file_name] = Path(output_path)
            elif file_name not in x_time_by_file:
                x_time_by_file[file_name] = Path(output_path)

        for item in manifest.get("inputs") or []:
            source_path = item.get("source_path") or item.get("local_path")
            if not source_path:
                continue
            file_name = Path(source_path).name
            entry = _ensure_entry(entries, file_name)
            entry["source_path"] = str(source_path)
            _add_artifact(entry, "workflow_manifest", manifest_path, index_dir)
            if file_name in x_time_by_file:
                _add_artifact(entry, "x_time_png", x_time_by_file[file_name], index_dir)

    prepared: list[dict] = []
    for entry in entries.values():
        entry["artifacts"].sort(key=lambda item: (_artifact_priority(item["kind"]), item["path"]))
        preview = next(
            (item for item in entry["artifacts"] if item["kind"] in {"axis_time_html", "interactive_3d", "x_time_png"}),
            None,
        )
        entry["preview_href"] = preview["href"] if preview else None
        entry["artifact_count"] = len(entry["artifacts"])
        entry["display_scan"] = None
        if entry["scan_shape"]:
            shape = " x ".join(str(v) for v in entry["scan_shape"])
            step_text = f", step {entry['step_um']} um" if entry["step_um"] is not None else ""
            points_text = f", {entry['point_count']} points" if entry["point_count"] is not None else ""
            entry["display_scan"] = f"{shape}{points_text}{step_text}"
        del entry["_seen_paths"]
        prepared.append(entry)

    prepared.sort(key=_entry_sort_key, reverse=True)
    return prepared


def _build_index_html(entries: list[dict], generated_at: str) -> str:
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PAM 数据结果索引</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7fb;
      --card: #ffffff;
      --line: #d8deea;
      --text: #16324f;
      --muted: #5f7287;
      --accent: #2b6de5;
      --accent-soft: #e8f0ff;
      --good: #0a7f53;
    }
    * { box-sizing: border-box; }
    html, body {
      height: 100%;
    }
    body {
      margin: 0;
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      color: var(--text);
      background: var(--bg);
      overflow: hidden;
    }
    .layout {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      height: 100vh;
      overflow: hidden;
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background: #f8fbff;
      padding: 14px;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 12px;
      min-height: 0;
      overflow: hidden;
    }
    .main {
      padding: 14px;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: 12px;
      min-width: 0;
      min-height: 0;
      overflow: auto;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 22px;
    }
    p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
    }
    .meta-line {
      margin-top: 8px;
      font-size: 13px;
      color: var(--muted);
    }
    .toolbar, .detail-card, .preview-card {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(31, 55, 86, 0.06);
    }
    .toolbar {
      padding: 14px;
    }
    .search {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: 14px;
      background: #fff;
      margin-top: 10px;
    }
    .count {
      margin-top: 8px;
      font-size: 13px;
      color: var(--muted);
    }
    .list {
      display: grid;
      gap: 10px;
      min-height: 0;
      overflow: auto;
      align-content: start;
      padding-right: 4px;
      scrollbar-gutter: stable;
    }
    .item {
      width: 100%;
      text-align: left;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: var(--card);
      padding: 12px;
      cursor: pointer;
      transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
    }
    .item:hover {
      border-color: #a7c2ff;
      box-shadow: 0 6px 18px rgba(43, 109, 229, 0.10);
      transform: translateY(-1px);
    }
    .item.active {
      border-color: var(--accent);
      background: var(--accent-soft);
      box-shadow: 0 8px 18px rgba(43, 109, 229, 0.14);
    }
    .item-title {
      font-size: 14px;
      font-weight: 700;
      line-height: 1.4;
      color: var(--text);
      word-break: break-word;
    }
    .item-time {
      margin-top: 6px;
      font-size: 12px;
      color: var(--muted);
    }
    .item-tags {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      background: #eef3fb;
      color: #37516d;
    }
    .item-note {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
      max-height: 2.8em;
      overflow: hidden;
    }
    .detail-card {
      padding: 14px;
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(260px, 0.85fr);
      gap: 14px;
      align-items: start;
      min-width: 0;
    }
    .detail-main, .detail-side {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .detail-head {
      display: grid;
      gap: 10px;
    }
    .detail-title {
      margin: 0;
      font-size: 18px;
      line-height: 1.35;
      word-break: break-word;
    }
    .detail-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 8px;
    }
    .meta-chip {
      display: inline-flex;
      align-items: center;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      background: #f1f5fb;
      color: #39526f;
    }
    .detail-path {
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fbfdff;
      padding: 8px 10px;
      font-size: 12px;
      color: var(--muted);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .artifact-section {
      display: grid;
      gap: 8px;
    }
    .section-label {
      font-size: 13px;
      font-weight: 600;
      color: var(--muted);
    }
    .artifact-grid {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .artifact-btn {
      border: 1px solid #9eb8f0;
      background: #fff;
      color: var(--accent);
      border-radius: 10px;
      padding: 8px 12px;
      cursor: pointer;
      font-size: 13px;
    }
    .artifact-btn:hover {
      background: #f4f8ff;
    }
    .artifact-btn.active {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    .note-wrap {
      display: grid;
      gap: 8px;
      min-width: 0;
    }
    .note-label {
      font-size: 13px;
      font-weight: 600;
    }
    textarea {
      width: 100%;
      min-height: 108px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    .note-status {
      font-size: 12px;
      color: var(--good);
      min-height: 1.2em;
    }
    .preview-card {
      overflow: hidden;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      min-height: 560px;
    }
    .preview-top {
      padding: 10px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfdff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }
    .preview-label {
      min-width: 0;
      font-size: 13px;
      color: var(--muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      flex: 1 1 260px;
    }
    .preview-controls {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex: 0 0 auto;
    }
    .preview-control {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .preview-range {
      width: 150px;
      accent-color: var(--accent);
    }
    .preview-value {
      min-width: 42px;
      text-align: right;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .preview-body {
      min-height: 0;
      overflow: auto;
      padding: 12px;
      background: #fbfdff;
    }
    .preview-stage {
      position: relative;
      width: 100%;
      height: 100%;
      min-height: 0;
      display: flex;
      justify-content: center;
      align-items: stretch;
    }
    .preview-viewport {
      width: 100%;
      min-width: min(100%, 320px);
      max-width: 100%;
      height: 100%;
      overflow: hidden;
      resize: horizontal;
      border: 1px solid var(--line);
      border-radius: 10px;
      background: #fff;
      flex: 0 0 auto;
      transition: width 0.15s ease;
    }
    .preview-viewport.resizing {
      transition: none;
    }
    .preview-resize-handle {
      position: absolute;
      top: 0;
      width: 12px;
      height: 100%;
      cursor: ew-resize;
      z-index: 20;
      background: linear-gradient(to right, transparent, rgba(43, 109, 229, 0.18));
    }
    .preview-resize-handle::after {
      content: "";
      position: absolute;
      top: 50%;
      right: 3px;
      width: 3px;
      height: 44px;
      border-left: 1px solid rgba(43, 109, 229, 0.55);
      border-right: 1px solid rgba(43, 109, 229, 0.55);
      transform: translateY(-50%);
    }
    body.preview-resizing {
      cursor: ew-resize;
      user-select: none;
    }
    iframe {
      width: 100%;
      height: 100%;
      border: 0;
      background: #fff;
      display: block;
    }
    .empty {
      padding: 16px;
      color: var(--muted);
    }
    @media (max-width: 1100px) {
      body {
        overflow: auto;
      }
      .layout {
        grid-template-columns: 1fr;
        height: auto;
        overflow: visible;
      }
      .sidebar {
        border-right: 0;
        border-bottom: 1px solid var(--line);
        min-height: auto;
        overflow: visible;
      }
      .main {
        overflow: visible;
        min-height: auto;
      }
      .detail-card {
        grid-template-columns: 1fr;
      }
      .list {
        max-height: 42vh;
      }
      .preview-card {
        min-height: 720px;
      }
      .preview-range {
        width: 120px;
      }
      .preview-viewport {
        width: 100% !important;
      }
      .preview-resize-handle {
        display: none;
      }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside class="sidebar">
      <div class="toolbar">
        <h1>PAM 数据索引</h1>
        <p>按日期和文件名统一管理结果。点选后可直接打开交互页，并给每个数据写备注。</p>
        <div class="meta-line">索引更新时间：__GENERATED_AT__</div>
        <input id="search" class="search" type="search" placeholder="搜索日期、文件名、D 值、near/far-field、test..." />
        <div id="count" class="count"></div>
      </div>
      <div id="list" class="list"></div>
    </aside>
    <main class="main">
      <section id="detail" class="detail-card"></section>
      <section class="preview-card">
        <div class="preview-top">
          <div id="preview-label-text" class="preview-label">预览区</div>
          <div class="preview-controls">
            <label class="preview-control" for="preview-width-range">
              <span>预览窗口宽度</span>
              <input id="preview-width-range" class="preview-range" type="range" min="30" max="100" step="5" value="100" />
              <span id="preview-width-value" class="preview-value">100%</span>
            </label>
          </div>
        </div>
        <div class="preview-body">
          <div class="preview-stage">
            <div id="preview-viewport" class="preview-viewport">
              <iframe id="preview-frame" title="结果预览"></iframe>
            </div>
            <div id="preview-resize-handle" class="preview-resize-handle" title="拖动调整预览窗口宽度"></div>
          </div>
        </div>
      </section>
    </main>
  </div>
  <script>
    const datasets = __PAYLOAD__;
    const notesKey = "pam-result-index-notes-v1";
    const viewStateKey = "pam-result-index-view-v3";
    const searchInput = document.getElementById("search");
    const listEl = document.getElementById("list");
    const countEl = document.getElementById("count");
    const detailEl = document.getElementById("detail");
    const previewLabelEl = document.getElementById("preview-label-text");
    const previewFrame = document.getElementById("preview-frame");
    const previewWidthRange = document.getElementById("preview-width-range");
    const previewWidthValueEl = document.getElementById("preview-width-value");
    const previewViewportEl = document.getElementById("preview-viewport");
    const previewResizeHandle = document.getElementById("preview-resize-handle");
    const previewBodyEl = document.querySelector(".preview-body");

    let noteStore = {};
    let viewState = {};
    try {
      noteStore = JSON.parse(localStorage.getItem(notesKey) || "{}");
    } catch (error) {
      noteStore = {};
    }
    try {
      viewState = JSON.parse(localStorage.getItem(viewStateKey) || "{}");
    } catch (error) {
      viewState = {};
    }
    if (!viewState || typeof viewState !== "object") {
      viewState = {};
    }
    if (!viewState.datasets || typeof viewState.datasets !== "object") {
      viewState.datasets = {};
    }

    function datasetView(id) {
      if (!id) return {};
      if (!viewState.datasets[id] || typeof viewState.datasets[id] !== "object") {
        viewState.datasets[id] = {};
      }
      return viewState.datasets[id];
    }

    let selectedId = datasets.find((item) => item.id === viewState.selectedId)?.id || (datasets.length ? datasets[0].id : null);
    let activeArtifactPath = selectedId ? (datasetView(selectedId).artifactPath || null) : null;
    let previewWidthPercent = 100;

    function notePreview(text) {
      const trimmed = (text || "").trim().replace(/\\s+/g, " ");
      return trimmed.length > 52 ? trimmed.slice(0, 52) + "..." : trimmed;
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function renderTags(tags) {
      return (tags || []).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
    }

    function clampPreviewWidth(value) {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return 100;
      return Math.min(100, Math.max(30, Math.round(numeric / 5) * 5));
    }

    function persistNotes() {
      localStorage.setItem(notesKey, JSON.stringify(noteStore));
      renderList();
    }

    function persistViewState() {
      viewState.selectedId = selectedId;
      if (selectedId && activeArtifactPath) {
        datasetView(selectedId).artifactPath = activeArtifactPath;
      }
      viewState.previewWidthPercent = previewWidthPercent;
      localStorage.setItem(viewStateKey, JSON.stringify(viewState));
    }

    function syncPreviewHandlePosition() {
      previewResizeHandle.style.left = `calc(50% + ${previewWidthPercent / 2}% - 12px)`;
    }

    function applyPreviewWidth() {
      previewViewportEl.style.width = `${previewWidthPercent}%`;
      previewWidthRange.value = String(previewWidthPercent);
      previewWidthValueEl.textContent = `${previewWidthPercent}%`;
      syncPreviewHandlePosition();
      window.requestAnimationFrame(notifyPreviewResize);
    }

    function notifyPreviewResize() {
      try {
        if (previewFrame.contentWindow) {
          previewFrame.contentWindow.postMessage({ type: "pam-index-preview-resize" }, "*");
        }
      } catch (error) {
        // Local file previews can still work even when the browser blocks a resize message.
      }
    }

    function setPreviewWidth(value) {
      previewWidthPercent = clampPreviewWidth(value);
      applyPreviewWidth();
      persistViewState();
    }

    function filteredDatasets() {
      const query = searchInput.value.trim().toLowerCase();
      if (!query) return datasets;
      return datasets.filter((item) => {
        const haystack = [
          item.file,
          item.display_time || "",
          item.display_scan || "",
          ...(item.tags || []),
          noteStore[item.id] || "",
        ].join(" ").toLowerCase();
        return haystack.includes(query);
      });
    }

    function setPreview(artifact) {
      if (!artifact) {
        activeArtifactPath = null;
        previewLabelEl.textContent = "没有可预览的结果";
        previewLabelEl.removeAttribute("title");
        previewFrame.removeAttribute("src");
        persistViewState();
        return;
      }
      activeArtifactPath = artifact.path;
      if (selectedId) {
        datasetView(selectedId).artifactPath = artifact.path;
      }
      const sourceName = artifact.source_name ? ` | ${artifact.source_name}` : "";
      previewLabelEl.textContent = `${artifact.label}${sourceName}`;
      previewLabelEl.title = artifact.path || artifact.href || artifact.label;
      previewFrame.src = artifact.href;
      persistViewState();
    }

    function renderList() {
      const items = filteredDatasets();
      countEl.textContent = `当前显示 ${items.length} / ${datasets.length} 个数据条目`;
      listEl.innerHTML = items.map((item) => {
        const active = item.id === selectedId ? " active" : "";
        const tags = renderTags(item.tags);
        const note = escapeHtml(notePreview(noteStore[item.id] || ""));
        const displayTime = escapeHtml(item.display_time || "未从文件名解析到日期");
        return `
          <button class="item${active}" data-id="${escapeHtml(item.id)}">
            <div class="item-title">${escapeHtml(item.file)}</div>
            <div class="item-time">${displayTime} | ${item.artifact_count} 个结果</div>
            <div class="item-tags">${tags}</div>
            <div class="item-note">${note || "暂无备注"}</div>
          </button>
        `;
      }).join("");
      listEl.querySelectorAll(".item").forEach((button) => {
        button.addEventListener("click", () => {
          selectedId = button.dataset.id;
          activeArtifactPath = datasetView(selectedId).artifactPath || null;
          persistViewState();
          renderList();
          renderDetail();
        });
      });
    }

    function renderDetail() {
      const item = datasets.find((entry) => entry.id === selectedId);
      if (!item) {
        detailEl.innerHTML = '<div class="empty">没有可显示的数据。</div>';
        setPreview(null);
        return;
      }

      const tags = renderTags(item.tags);
      const meta = [
        `<span class="meta-chip">日期 ${escapeHtml(item.display_time || "未知")}</span>`,
        `<span class="meta-chip">${item.artifact_count} 个结果</span>`,
        item.display_scan ? `<span class="meta-chip">${escapeHtml(item.display_scan)}</span>` : "",
      ].filter(Boolean).join("");
      const sourcePath = item.source_path
        ? `<div class="detail-path" title="${escapeHtml(item.source_path)}">源文件：${escapeHtml(item.source_path)}</div>`
        : "";

      detailEl.innerHTML = `
        <div class="detail-main">
          <div class="detail-head">
            <div>
              <h2 class="detail-title">${escapeHtml(item.file)}</h2>
              <div class="detail-meta">${meta}</div>
              ${tags ? `<div class="item-tags">${tags}</div>` : ""}
            </div>
            ${sourcePath}
          </div>
          <div class="artifact-section">
            <div class="section-label">结果入口</div>
            <div class="artifact-grid" id="artifact-grid"></div>
          </div>
        </div>
        <div class="detail-side">
          <div class="note-wrap">
            <label class="note-label" for="note-box">备注</label>
            <textarea id="note-box" placeholder="在这里记录你对这组数据的判断、疑问或结论。重新打开本页后仍会保留。"></textarea>
            <div id="note-status" class="note-status"></div>
          </div>
        </div>
      `;

      const artifactGrid = document.getElementById("artifact-grid");
      item.artifacts.forEach((artifact, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "artifact-btn";
        button.textContent = artifact.label;
        if ((!activeArtifactPath && index === 0) || artifact.path === activeArtifactPath) {
          button.classList.add("active");
        }
        button.addEventListener("click", () => {
          artifactGrid.querySelectorAll(".artifact-btn").forEach((el) => el.classList.remove("active"));
          button.classList.add("active");
          setPreview(artifact);
        });
        artifactGrid.appendChild(button);
      });

      const noteBox = document.getElementById("note-box");
      const noteStatus = document.getElementById("note-status");
      noteBox.value = noteStore[item.id] || "";
      let saveTimer = null;

      function flushNote(statusText) {
        if (saveTimer) {
          window.clearTimeout(saveTimer);
          saveTimer = null;
        }
        noteStore[item.id] = noteBox.value;
        persistNotes();
        noteStatus.textContent = statusText;
      }

      noteBox.addEventListener("input", () => {
        noteStore[item.id] = noteBox.value;
        noteStatus.textContent = "编辑中，停止输入 3 秒后自动保存";
        if (saveTimer) window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => {
          saveTimer = null;
          persistNotes();
          noteStatus.textContent = "已保存到当前浏览器本地备注";
        }, 3000);
      });

      noteBox.addEventListener("blur", () => {
        if (saveTimer) {
          flushNote("已在离开输入框时保存");
        }
      });

      const rememberedArtifactPath = datasetView(item.id).artifactPath || activeArtifactPath;
      const preferred = item.artifacts.find((artifact) => artifact.path === rememberedArtifactPath) || item.artifacts[0];
      setPreview(preferred || null);
    }

    searchInput.addEventListener("input", () => {
      const visible = filteredDatasets();
      if (!visible.find((item) => item.id === selectedId)) {
        selectedId = visible.length ? visible[0].id : null;
        activeArtifactPath = selectedId ? (datasetView(selectedId).artifactPath || null) : null;
        persistViewState();
      }
      renderList();
      renderDetail();
    });

    previewWidthRange.addEventListener("input", (event) => {
      setPreviewWidth(event.target.value);
    });

    function startPreviewResize(startX) {
      const startWidth = previewWidthPercent;
      const bodyWidth = Math.max(1, previewBodyEl.getBoundingClientRect().width);
      previewViewportEl.classList.add("resizing");
      document.body.classList.add("preview-resizing");

      function moveTo(clientX) {
        const deltaPercent = ((clientX - startX) * 2 / bodyWidth) * 100;
        previewWidthPercent = clampPreviewWidth(startWidth + deltaPercent);
        applyPreviewWidth();
      }

      function finish() {
        previewViewportEl.classList.remove("resizing");
        document.body.classList.remove("preview-resizing");
        persistViewState();
      }

      return { moveTo, finish };
    }

    previewResizeHandle.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      const session = startPreviewResize(event.clientX);
      if (previewResizeHandle.setPointerCapture) {
        previewResizeHandle.setPointerCapture(event.pointerId);
      }

      function onPointerMove(moveEvent) {
        session.moveTo(moveEvent.clientX);
      }

      function onPointerUp(upEvent) {
        if (previewResizeHandle.releasePointerCapture) {
          previewResizeHandle.releasePointerCapture(upEvent.pointerId);
        }
        document.removeEventListener("pointermove", onPointerMove);
        document.removeEventListener("pointerup", onPointerUp);
        document.removeEventListener("pointercancel", onPointerUp);
        session.finish();
      }

      document.addEventListener("pointermove", onPointerMove);
      document.addEventListener("pointerup", onPointerUp);
      document.addEventListener("pointercancel", onPointerUp);
    });

    previewResizeHandle.addEventListener("mousedown", (event) => {
      if ("PointerEvent" in window) return;
      event.preventDefault();
      const session = startPreviewResize(event.clientX);

      function onMouseMove(moveEvent) {
        session.moveTo(moveEvent.clientX);
      }

      function onMouseUp() {
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
        session.finish();
      }

      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
    });

    if (window.ResizeObserver) {
      let resizePersistTimer = null;
      const previewResizeObserver = new ResizeObserver(() => {
        const bodyWidth = Math.max(1, previewBodyEl.getBoundingClientRect().width);
        const actualPercent = clampPreviewWidth((previewViewportEl.clientWidth / bodyWidth) * 100);
        if (Math.abs(actualPercent - previewWidthPercent) < 1) return;
        previewWidthPercent = actualPercent;
        previewWidthRange.value = String(previewWidthPercent);
        previewWidthValueEl.textContent = `${previewWidthPercent}%`;
        syncPreviewHandlePosition();
        notifyPreviewResize();
        if (resizePersistTimer) window.clearTimeout(resizePersistTimer);
        resizePersistTimer = window.setTimeout(persistViewState, 250);
      });
      previewResizeObserver.observe(previewViewportEl);
    }

    previewFrame.addEventListener("load", () => {
      notifyPreviewResize();
      window.setTimeout(notifyPreviewResize, 120);
      window.setTimeout(notifyPreviewResize, 500);
    });

    previewWidthPercent = clampPreviewWidth(viewState.previewWidthPercent ?? 100);
    applyPreviewWidth();

    renderList();
    renderDetail();
  </script>
</body>
</html>
"""
    return template.replace("__GENERATED_AT__", generated_at).replace("__PAYLOAD__", payload)


def write_result_index(skill_root: Path, output_dir: Path | None = None) -> dict:
    skill_root = Path(skill_root).resolve()
    results_root = skill_root / "workspace" / "results"
    output_dir = (output_dir or results_root / "pam-result-index").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    entries = collect_result_entries(skill_root, index_dir=output_dir)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html_text = _build_index_html(entries, generated_at)

    html_path = output_dir / "index.html"
    json_path = output_dir / "index_data.json"
    html_path.write_text(html_text, encoding="utf-8")
    json_path.write_text(json.dumps({"generated_at": generated_at, "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"html": str(html_path), "json": str(json_path), "count": len(entries)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a unified PAM result index with persistent browser-side notes.")
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--output-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = write_result_index(skill_root=args.skill_root, output_dir=args.output_dir)
    print(result["html"])
    print(result["json"])
    print(f"count={result['count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
