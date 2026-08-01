from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_PROCESSING_SKILL_PATH = r"D:\Phd_training\skills\data-processing-skill"
DEFAULT_OUTPUT_ROOT = r".\results\cache\pam_preview"


@dataclass
class PreviewRunResult:
    status: str
    input_path: str
    output_dir: str
    mode: str
    artifacts: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    error: str = ""


class PAMResultPreviewController:
    """Bridge from the acquisition UI to the PAM data-processing package.

    The acquisition program deliberately calls the processing package through a
    subprocess with a temporary PYTHONPATH. That keeps heavy plotting/scipy code
    out of the hardware-control startup path and makes failures non-fatal.
    """

    def __init__(
        self,
        *,
        project_root: str | os.PathLike[str] = ".",
        output_root: str | os.PathLike[str] = DEFAULT_OUTPUT_ROOT,
        processing_skill_path: str | os.PathLike[str] | None = None,
        python_executable: str | os.PathLike[str] | None = None,
        log_callback=None,
        timeout_s: float = 900.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.output_root = (self.project_root / Path(output_root)).resolve() if not Path(output_root).is_absolute() else Path(output_root).resolve()
        self.processing_skill_path = Path(
            processing_skill_path
            or os.environ.get("PAM_PROCESSING_SKILL_PATH")
            or DEFAULT_PROCESSING_SKILL_PATH
        ).resolve()
        self.python_executable = str(python_executable or sys.executable)
        self.timeout_s = float(timeout_s)
        self.log = log_callback or (lambda *args, **kwargs: None)

    @property
    def processing_src(self) -> Path:
        return self.processing_skill_path / "scripts" / "pam_scan_processing" / "src"

    def available(self) -> tuple[bool, str]:
        package_dir = self.processing_src / "pam_scan_processing"
        if not package_dir.exists():
            return False, f"pam_scan_processing source not found at {package_dir}"
        return True, str(package_dir)

    def output_dir_for(self, input_path: str | os.PathLike[str]) -> Path:
        path = Path(input_path)
        return self.output_root / path.stem

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        src = str(self.processing_src)
        env["PYTHONPATH"] = src if not existing else src + os.pathsep + existing
        return env

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            cwd=str(self.project_root),
            env=self._env(),
            text=True,
            capture_output=True,
            timeout=self.timeout_s,
            check=False,
        )

    def _append_completed(self, result: PreviewRunResult, label: str, completed: subprocess.CompletedProcess) -> None:
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout:
            result.messages.append(f"{label} stdout: {stdout[-1200:]}")
        if stderr:
            result.messages.append(f"{label} stderr: {stderr[-1200:]}")
        if completed.returncode != 0:
            raise RuntimeError(f"{label} failed with exit code {completed.returncode}: {stderr or stdout}")

    def _write_project_index(self, output_dir: Path) -> Path:
        html_files = sorted(output_dir.glob("*.html"))
        cards = []
        for html_file in html_files:
            cards.append(f'<li><a href="{html_file.name}">{html_file.name}</a></li>')
        index_html = output_dir / "index.html"
        index_html.write_text(
            "<!doctype html><meta charset=\"utf-8\"><title>PAM preview</title>"
            "<h1>PAM preview artifacts</h1><ul>" + "\n".join(cards) + "</ul>",
            encoding="utf-8",
        )
        return index_html

    def generate(
        self,
        input_path: str | os.PathLike[str] | None,
        *,
        mode: str = "all",
        display_window: str = "0:4000",
        baseline: str = "0:100",
        time_step: int = 1,
        axis_mode: str = "xy",
        hilbert: bool = True,
    ) -> PreviewRunResult:
        if not input_path:
            return PreviewRunResult(status="skipped", input_path="", output_dir="", mode=mode, error="No .mat file is available yet.")

        ok, detail = self.available()
        input_path = str(input_path)
        output_dir = self.output_dir_for(input_path)
        result = PreviewRunResult(status="running", input_path=input_path, output_dir=str(output_dir), mode=mode)
        if not ok:
            result.status = "failed"
            result.error = detail
            return result

        output_dir.mkdir(parents=True, exist_ok=True)
        normalized_mode = str(mode).strip().lower()
        try:
            if normalized_mode in {"all", "axis", "axis-time", "time"}:
                axis_args = [
                    self.python_executable,
                    "-m",
                    "pam_scan_processing.time_axis_map",
                    "--input",
                    input_path,
                    "--output-dir",
                    str(output_dir),
                    "--display-window",
                    display_window,
                    "--baseline",
                    baseline,
                    "--time-step",
                    str(int(time_step)),
                    "--mode",
                    axis_mode,
                ]
                if hilbert:
                    axis_args.append("--hilbert")
                self._append_completed(result, "axis-time", self._run(axis_args))

            if normalized_mode in {"all", "3d", "interactive"}:
                interactive_args = [
                    self.python_executable,
                    "-m",
                    "pam_scan_processing.interactive_cli",
                    "--input",
                    input_path,
                    "--output-dir",
                    str(output_dir),
                    "--display-window",
                    display_window,
                    "--baseline",
                    baseline,
                    "--time-step",
                    str(int(time_step)),
                ]
                self._append_completed(result, "interactive-3d", self._run(interactive_args))

            index_path = self._write_project_index(output_dir)
            artifacts = [str(path) for path in sorted(output_dir.glob("*.html"))]
            if str(index_path) not in artifacts:
                artifacts.append(str(index_path))
            result.artifacts = artifacts
            result.status = "ok"
            self.log(
                "PAM_RESULT_PREVIEW_DONE",
                mode=normalized_mode,
                input_path=input_path,
                output_dir=str(output_dir),
                artifacts=json.dumps(result.artifacts, ensure_ascii=True),
            )
            return result
        except Exception as exc:
            result.status = "failed"
            result.error = str(exc)
            self.log("PAM_RESULT_PREVIEW_FAILED", mode=normalized_mode, input_path=input_path, error=repr(exc))
            return result
