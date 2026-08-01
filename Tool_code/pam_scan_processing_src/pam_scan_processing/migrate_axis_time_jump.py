from __future__ import annotations

import argparse
from pathlib import Path


JUMP_CONTROL_FRAGMENT = """
      <span id="jump-controls" class="group hidden">
        <span style="margin-left: 4px; color:#64748b;">跨模式跳转:</span>
        <button id="btn-jump-x">跳转到X-时间图</button>
        <button id="btn-jump-y">跳转到Y-时间图</button>
        <button id="btn-jump-xy">跳转到XY切面</button>
        <span id="jump-hint" style="font-size: 12px; color: #334155;"></span>
      </span>
"""

JUMP_PATCH_SCRIPT = r"""
  <script>
    (function () {
      if (window.__axisTimeCrossModeJumpPatched) return;
      window.__axisTimeCrossModeJumpPatched = true;

      if (typeof payload === "undefined" || typeof state === "undefined") return;
      const plotEl = document.getElementById("plot");
      if (!plotEl) return;

      const jumpControls = document.getElementById("jump-controls");
      const jumpHint = document.getElementById("jump-hint");
      const btnJumpX = document.getElementById("btn-jump-x");
      const btnJumpY = document.getElementById("btn-jump-y");
      const btnJumpXY = document.getElementById("btn-jump-xy");
      const modeButtons = [
        document.getElementById("btn-mode-x"),
        document.getElementById("btn-mode-y"),
        document.getElementById("btn-mode-xy"),
      ];
      if (!jumpControls || !jumpHint || !btnJumpX || !btnJumpY || !btnJumpXY) return;

      const sampleTimes = payload.sample_times_ns || [];
      const sampleIndices = payload.sample_indices || [];
      let jumpCandidate = null;

      function fmt(x) {
        const numeric = Number(x);
        return Number.isFinite(numeric) ? Number.parseFloat(numeric).toFixed(4) : "NaN";
      }

      function rawAtDisplayIndex(displayIndex) {
        const clamped = Math.max(0, Math.min(Math.round(displayIndex), sampleTimes.length - 1));
        return Number(sampleIndices[clamped]);
      }

      function displayIndexFromRawSample(rawSample) {
        const target = Number(rawSample);
        if (!Number.isFinite(target) || sampleIndices.length === 0) return 0;
        let best = 0;
        let bestDistance = Infinity;
        for (let i = 0; i < sampleIndices.length; i += 1) {
          const distance = Math.abs(sampleIndices[i] - target);
          if (distance < bestDistance) {
            bestDistance = distance;
            best = i;
          }
        }
        return best;
      }

      function nearestDisplayIndexFromTime(timeValue) {
        const target = Number(timeValue);
        if (!Number.isFinite(target) || sampleTimes.length === 0) return 0;
        let best = 0;
        let bestDistance = Infinity;
        for (let i = 0; i < sampleTimes.length; i += 1) {
          const distance = Math.abs(sampleTimes[i] - target);
          if (distance < bestDistance) {
            bestDistance = distance;
            best = i;
          }
        }
        return best;
      }

      function closestLineIndexByFixed(modeLines, targetValue) {
        if (!Array.isArray(modeLines) || modeLines.length === 0 || !Number.isFinite(targetValue)) return null;
        let bestIndex = 0;
        let bestDistance = Infinity;
        for (let i = 0; i < modeLines.length; i += 1) {
          const candidate = modeLines[i];
          const fixedValue = Number(candidate && candidate.fixed_value);
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
          jumpHint.textContent = "点击当前页中的一点，可选择跳转目标";
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
        jumpHint.textContent = `已选点: t=${fmt(candidate.sampleTimeNs)} ns，原始采样=${candidate.rawSample}`;
      }

      function buildCandidate(point, mode) {
        const pointNumber = Array.isArray(point.pointNumber) ? point.pointNumber : [];
        const ptX = Number(point.x);
        const ptY = Number(point.y);

        if ((mode === "x" || mode === "y") && Number.isFinite(ptY)) {
          const modeData = mode === "x" ? payload.x_time_mode : payload.y_time_mode;
          if (!modeData || !Array.isArray(modeData.lines) || modeData.lines.length === 0) return null;
          const line = modeData.lines[state.lineIndex];
          if (!line) return null;
          const fixedValue = Number(line.fixed_value);
          const scanCoord = Number.isFinite(ptX) ? ptX : null;
          if (!Number.isFinite(fixedValue) || !Number.isFinite(scanCoord)) return null;
          const displayIndex = pointNumber.length >= 2
            ? Math.max(0, Math.min(sampleTimes.length - 1, Math.round(mode === "x" || mode === "y" ? pointNumber[1] : pointNumber[0])))
            : nearestDisplayIndexFromTime(ptY);
          return {
            mode,
            rawSample: rawAtDisplayIndex(displayIndex),
            displayIndex,
            sampleTimeNs: sampleTimes[displayIndex],
            fixedX: mode === "x" ? scanCoord : fixedValue,
            fixedY: mode === "x" ? fixedValue : scanCoord,
          };
        }

        if (mode === "xy") {
          if (!Number.isFinite(ptX) || !Number.isFinite(ptY)) return null;
          return {
            mode,
            rawSample: rawAtDisplayIndex(state.xyCenterIndex),
            displayIndex: state.xyCenterIndex,
            sampleTimeNs: sampleTimes[Math.max(0, Math.min(sampleTimes.length - 1, state.xyCenterIndex))] || 0,
            fixedX: ptX,
            fixedY: ptY,
          };
        }
        return null;
      }

      function jumpToMode(modeTarget) {
        if (!jumpCandidate) return;
        const candidate = jumpCandidate;
        if (modeTarget === "xy") {
          state.mode = "xy";
          state.xyCenterIndex = displayIndexFromRawSample(candidate.rawSample);
          if (typeof setMode === "function") {
            setMode("xy");
          } else if (typeof setModeX === "function") {
            setModeX();
          }
          return;
        }

        if (modeTarget === "x") {
          const lineIndex = closestLineIndexByFixed(payload.x_time_mode && payload.x_time_mode.lines, candidate.fixedY);
          if (lineIndex === null) return;
          state.mode = "x";
          state.lineIndex = lineIndex;
        } else if (modeTarget === "y") {
          const lineIndex = closestLineIndexByFixed(payload.y_time_mode && payload.y_time_mode.lines, candidate.fixedX);
          if (lineIndex === null) return;
          state.mode = "y";
          state.lineIndex = lineIndex;
        } else {
          return;
        }

        const candidateIndex = displayIndexFromRawSample(candidate.rawSample);
        state.sampleStart = Math.max(0, candidateIndex - 2);
        state.sampleEnd = Math.min(sampleTimes.length - 1, candidateIndex + 3);
        if (typeof setMode === "function") {
          setMode(state.mode);
        }
      }

      function updateButtonStateFromMode() {
        if (!jumpCandidate) return;
        applyJumpCandidate(jumpCandidate);
      }

      btnJumpX.addEventListener("click", () => jumpToMode("x"));
      btnJumpY.addEventListener("click", () => jumpToMode("y"));
      btnJumpXY.addEventListener("click", () => jumpToMode("xy"));

      plotEl.on("plotly_click", (eventData) => {
        if (!eventData || !Array.isArray(eventData.points) || eventData.points.length === 0) return;
        const point = eventData.points[0];
        const candidate = buildCandidate(point, state.mode);
        if (!candidate) return;
        applyJumpCandidate(candidate);
      });

      if (typeof modeButtons[0] === "object" && modeButtons[0] !== null) {
        modeButtons.forEach((button) => {
          if (!button) return;
          button.addEventListener("click", () => {
            setTimeout(updateButtonStateFromMode, 0);
          }, { passive: true });
        });
      }
    })();
  </script>
"""


def inject_jump_controls(html_text: str) -> tuple[str, bool]:
    if 'id="btn-jump-x"' in html_text and 'id="jump-controls"' in html_text:
        return html_text, False
    if 'id="status"></span>' not in html_text:
        return html_text, False

    patched = html_text.replace('      <span id="status"></span>', JUMP_CONTROL_FRAGMENT + '\n      <span id="status"></span>', 1)
    if patched == html_text:
        return html_text, False

    if 'id="btn-jump-x"' not in patched:
        return html_text, False

    marker = patched.lower().rfind("</body>")
    if marker < 0:
        return html_text, False

    patched = patched[:marker] + JUMP_PATCH_SCRIPT + patched[marker:]
    return patched, True


def migrate_axis_time_results(results_root: Path) -> dict[str, int]:
    result = {
        "scanned": 0,
        "patched": 0,
        "already_ok": 0,
        "skipped": 0,
    }
    for html_path in sorted(results_root.rglob("*_axis_time_checker.html")):
        result["scanned"] += 1
        try:
            text = html_path.read_text(encoding="utf-8")
        except OSError:
            result["skipped"] += 1
            continue
        patched, changed = inject_jump_controls(text)
        if not changed:
            result["already_ok"] += 1
            continue
        html_path.write_text(patched, encoding="utf-8")
        result["patched"] += 1
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Patch existing axis-time checker HTML pages to enable cross-mode jump.")
    parser.add_argument(
        "--results-root",
        required=True,
        type=Path,
        help="Directory containing existing *_axis_time_checker.html outputs.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = migrate_axis_time_results(args.results_root.resolve())
    print(f"Scanned: {summary['scanned']}")
    print(f"Patched: {summary['patched']}")
    print(f"Already ready: {summary['already_ok']}")
    print(f"Skipped: {summary['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
