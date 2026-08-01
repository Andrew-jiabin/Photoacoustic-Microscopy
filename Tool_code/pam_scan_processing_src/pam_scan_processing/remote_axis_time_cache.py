from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

from .result_index import write_result_index
from .core import parse_slice
from .interactive import write_interactive_html
from .time_axis_map import write_axis_time_checker
from .workflow import DEFAULT_REMOTE_DATA_DIR, DEFAULT_REMOTE_HOST


FILENAME_TIMESTAMP_RE = re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})")
DEFAULT_INCREMENTAL_SINCE = "2026-07-20_02-56-04"
LATEST_SINCE_MARKERS = {"", "auto", "latest", "latest-processed", "latest_processed"}
DEFAULT_AXIS_TIME_DISPLAY_WINDOW = (0, 4000)


def _parse_filename_time(name: str) -> datetime | None:
    match = FILENAME_TIMESTAMP_RE.search(name)
    if not match:
        return None
    return datetime.strptime(match.group("stamp"), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)


def _parse_cutoff(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    filename_time = _parse_filename_time(text)
    if filename_time is not None:
        return filename_time
    normalized = text.replace("T", " ").replace("_", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H-%M-%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(normalized, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse cutoff timestamp: {value}")


def _latest_processed_cutoff(previous_files: dict[str, dict]) -> datetime | None:
    latest: datetime | None = None
    for record in previous_files.values():
        has_cached_outputs = (
            record.get("axis_time_html")
            or record.get("axis_time_status") in {"existing", "generated"}
            or record.get("interactive_3d_html")
            or record.get("interactive_3d_status") in {"existing", "generated"}
        )
        if not has_cached_outputs:
            continue
        filename_time = record.get("filename_time_utc")
        if filename_time:
            current = datetime.fromisoformat(str(filename_time).replace("Z", "+00:00"))
        else:
            current = _parse_filename_time(str(record.get("name") or ""))
        if current is None:
            continue
        current = current.astimezone(timezone.utc) if current.tzinfo else current.replace(tzinfo=timezone.utc)
        latest = current if latest is None or current > latest else latest
    return latest


def _resolve_effective_since(since: str | None, previous_files: dict[str, dict]) -> tuple[str | None, datetime | None]:
    text = "" if since is None else str(since).strip()
    if text.lower() in LATEST_SINCE_MARKERS:
        latest = _latest_processed_cutoff(previous_files)
        if latest is not None:
            return "latest-processed", latest
        fallback = _parse_cutoff(DEFAULT_INCREMENTAL_SINCE)
        return DEFAULT_INCREMENTAL_SINCE, fallback
    return text or None, _parse_cutoff(text or None)


def _safe_cache_name(host: str, remote_data_dir: str) -> str:
    seed = f"{host}_{remote_data_dir}".replace("\\", "_").replace("/", "_").replace(":", "")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", seed).strip("._")
    return cleaned or "remote_data"


def _remote_json_listing(host: str, remote_data_dir: str) -> list[dict]:
    escaped = remote_data_dir.replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$items = Get-ChildItem -LiteralPath '{escaped}' -Filter '*.mat' -File | "
        "Sort-Object Name | ForEach-Object { "
        "[PSCustomObject]@{"
        "Name=$_.Name;"
        "FullName=$_.FullName;"
        "Length=[Int64]$_.Length;"
        "LastWriteTimeUtc=$_.LastWriteTimeUtc.ToString('o')"
        "} }; "
        "$items | ConvertTo-Json -Compress"
    )
    command = f'powershell -NoProfile -Command "{script}"'
    result = subprocess.run(
        ["ssh", host, command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Remote listing failed with exit code {result.returncode}.\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    text = result.stdout.strip()
    if not text:
        return []
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        return [parsed]
    return list(parsed)


def _scp_remote_file(host: str, remote_file: str, destination: Path, overwrite: bool = False) -> None:
    if destination.exists() and not overwrite:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    remote_file_posix = remote_file.replace("\\", "/")
    scp_source = f"{host}:{remote_file_posix}"
    subprocess.run(["scp", scp_source, str(destination)], check=True)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def sync_remote_axis_time_cache(
    skill_root: Path,
    remote_host: str = DEFAULT_REMOTE_HOST,
    remote_data_dir: str = DEFAULT_REMOTE_DATA_DIR,
    since: str | None = "latest-processed",
    output_run_id: str = "remote_axis_time_incremental",
    display_window: tuple[int, int] = DEFAULT_AXIS_TIME_DISPLAY_WINDOW,
    baseline: tuple[int, int] = (0, 100),
    time_step: int = 1,
    clip_percentile: float = 99.5,
    mode: str = "xy",
    use_hilbert: bool = True,
    generate_interactive_3d: bool = True,
    overwrite_cache: bool = False,
    overwrite_html: bool = False,
) -> dict:
    skill_root = Path(skill_root).resolve()
    cache_name = _safe_cache_name(remote_host, remote_data_dir)
    cache_root = skill_root / "workspace" / "data" / "remote_cache" / cache_name
    raw_dir = cache_root / "raw"
    manifest_path = cache_root / "remote_file_cache_manifest.json"
    output_root = skill_root / "workspace" / "results" / output_run_id
    axis_output_dir = output_root / "axis_time_map"
    interactive_output_dir = output_root / "interactive_3d"

    previous = _read_json(manifest_path)
    previous_files = previous.get("files", {}) if isinstance(previous.get("files", {}), dict) else {}
    effective_since_text, cutoff = _resolve_effective_since(since, previous_files)
    remote_items = _remote_json_listing(remote_host, remote_data_dir)

    files: dict[str, dict] = {}
    selected: list[dict] = []
    for item in remote_items:
        name = str(item["Name"])
        file_time = _parse_filename_time(name)
        record = dict(previous_files.get(name, {}))
        record.update(
            {
                "name": name,
                "remote_path": str(item["FullName"]),
                "length": int(item["Length"]),
                "remote_last_write_utc": str(item["LastWriteTimeUtc"]),
                "filename_time_utc": file_time.isoformat() if file_time else None,
            }
        )
        local_path = raw_dir / name
        record["cache_path"] = str(local_path)
        files[name] = record
        if file_time is None:
            continue
        if cutoff is not None and file_time <= cutoff:
            continue
        selected.append(record)

    processed: list[dict] = []
    skipped: list[dict] = []
    for record in sorted(selected, key=lambda r: (r.get("filename_time_utc") or "", r["name"])):
        local_path = raw_dir / record["name"]
        _scp_remote_file(remote_host, record["remote_path"], local_path, overwrite=overwrite_cache)
        generated_any = False

        html_path = axis_output_dir / f"{local_path.stem}_axis_time_checker.html"
        if html_path.exists() and not overwrite_html:
            record["axis_time_html"] = str(html_path)
            record["axis_time_status"] = "existing"
        else:
            result = write_axis_time_checker(
                input_spec=str(local_path),
                output_dir=axis_output_dir,
                display_window=display_window,
                baseline=baseline,
                time_step=time_step,
                clip_percentile=clip_percentile,
                initial_mode=mode,
                use_hilbert=use_hilbert,
                remote_host=remote_host,
                remote_data_dir=remote_data_dir,
            )
            record["axis_time_html"] = result["html"]
            record["axis_time_summary"] = result["summary"]
            record["axis_time_status"] = "generated"
            generated_any = True

        interactive_html_path = interactive_output_dir / f"{local_path.stem}_interactive_3d.html"
        if generate_interactive_3d:
            if interactive_html_path.exists() and not overwrite_html:
                record["interactive_3d_html"] = str(interactive_html_path)
                record["interactive_3d_status"] = "existing"
            else:
                output_path = write_interactive_html(
                    path=local_path,
                    output_dir=interactive_output_dir,
                    display_window=display_window,
                    baseline=baseline,
                    time_step=max(1, time_step),
                )
                record["interactive_3d_html"] = str(output_path)
                record["interactive_3d_status"] = "generated"
                generated_any = True

        if generated_any:
            processed.append(record)
        else:
            skipped.append(
                {
                    "name": record["name"],
                    "reason": "artifacts_exist",
                    "axis_time_html": record.get("axis_time_html"),
                    "interactive_3d_html": record.get("interactive_3d_html"),
                }
            )
        files[record["name"]] = record

    manifest = {
        "updated_at": datetime.now().astimezone().isoformat(),
        "remote_host": remote_host,
        "remote_data_dir": remote_data_dir,
        "cache_root": str(cache_root),
        "raw_dir": str(raw_dir),
        "output_dir": str(output_root),
        "axis_time_output_dir": str(axis_output_dir),
        "interactive_output_dir": str(interactive_output_dir),
        "since": effective_since_text,
        "requested_since": since,
        "since_utc": cutoff.isoformat() if cutoff else None,
        "parameters": {
            "display_window": list(display_window),
            "baseline": list(baseline),
            "time_step": time_step,
            "clip_percentile": clip_percentile,
            "mode": mode,
            "use_hilbert": use_hilbert,
            "generate_interactive_3d": generate_interactive_3d,
            "overwrite_cache": overwrite_cache,
            "overwrite_html": overwrite_html,
        },
        "remote_file_count": len(remote_items),
        "selected_count": len(selected),
        "processed_count": len(processed),
        "skipped_count": len(skipped),
        "processed": processed,
        "skipped": skipped,
        "files": dict(sorted(files.items())),
    }
    cache_root.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    index_result = write_result_index(skill_root)
    return {**manifest, "manifest_path": str(manifest_path), "index_html": index_result["html"], "index_json": index_result["json"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cache remote PAM data directory files and generate axis-time and 3D HTML for new .mat files."
    )
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-data-dir", default=DEFAULT_REMOTE_DATA_DIR)
    parser.add_argument(
        "--since",
        default="latest-processed",
        help="Process files whose filename timestamp is later than this value, or use latest-processed.",
    )
    parser.add_argument("--output-run-id", default="remote_axis_time_incremental")
    parser.add_argument(
        "--display-window",
        default="0:4000",
        help="Sample window embedded in each HTML page. 0:4000 covers 1 us at 4 GHz.",
    )
    parser.add_argument("--baseline", default="0:100")
    parser.add_argument("--time-step", type=int, default=1)
    parser.add_argument("--clip-percentile", type=float, default=99.5)
    parser.add_argument("--mode", choices=("x", "y", "xy"), default="xy")
    parser.add_argument("--hilbert", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--interactive-3d", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--overwrite-cache", action="store_true")
    parser.add_argument("--overwrite-html", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = sync_remote_axis_time_cache(
        skill_root=args.skill_root,
        remote_host=args.remote_host,
        remote_data_dir=args.remote_data_dir,
        since=args.since,
        output_run_id=args.output_run_id,
        display_window=parse_slice(args.display_window, *DEFAULT_AXIS_TIME_DISPLAY_WINDOW),
        baseline=parse_slice(args.baseline, 0, 100),
        time_step=args.time_step,
        clip_percentile=args.clip_percentile,
        mode=args.mode,
        use_hilbert=bool(args.hilbert),
        generate_interactive_3d=bool(args.interactive_3d),
        overwrite_cache=args.overwrite_cache,
        overwrite_html=args.overwrite_html,
    )
    print(result["manifest_path"])
    print(result["output_dir"])
    print(result["index_html"])
    print(f"selected={result['selected_count']} processed={result['processed_count']} skipped={result['skipped_count']}")
    for record in result["processed"]:
        for key in ("axis_time_html", "interactive_3d_html"):
            if record.get(key):
                print(record[key])
    for record in result["skipped"]:
        for key in ("axis_time_html", "interactive_3d_html"):
            if record.get(key):
                print(record[key])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
