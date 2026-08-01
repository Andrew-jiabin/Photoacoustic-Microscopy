from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math

import numpy as np
from scipy.io import loadmat


@dataclass(frozen=True)
class BrowserPoint:
    index: int
    key: str
    pos_text: str
    x: float
    y: float
    z: float
    row: int
    col: int


@dataclass
class BrowserScan:
    path: Path
    data: dict
    points: list[BrowserPoint]
    x_values: np.ndarray
    y_values: np.ndarray
    point_grid: np.ndarray
    p2p_image: np.ndarray
    waveform_length: int
    meta: dict


def safe_key_from_pos(pos_text: str) -> str:
    clean = str(pos_text).strip()
    clean = clean.replace(" ", "")
    clean = clean.replace(".", "p")
    clean = clean.replace("-", "n")
    clean = clean.replace(",", "_")
    return "P_" + clean


def parse_pos_text(pos_text: str) -> tuple[float, float, float]:
    parts = [part.strip() for part in str(pos_text).split(",") if part.strip()]
    values = [float(part) for part in parts]
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
    item = arr.ravel()[0]
    return item.item() if hasattr(item, "item") else item


def _string_list(value) -> list[str]:
    return [str(item).strip() for item in np.asarray(value).ravel()]


def _rounded(value: float) -> float:
    return round(float(value), 12)


def _step(values: np.ndarray, fallback: float = 1.0) -> float:
    values = np.asarray(values, dtype=float)
    if len(values) < 2:
        return fallback
    diffs = np.diff(np.sort(values))
    diffs = diffs[np.isfinite(diffs) & (np.abs(diffs) > 1e-12)]
    if diffs.size == 0:
        return fallback
    return float(np.median(np.abs(diffs)))


def load_browser_scan(path: Path, p2p_window: tuple[int, int] | None = None) -> BrowserScan:
    data = loadmat(path, squeeze_me=True, struct_as_record=False)
    meta_obj = data["metadata"]
    pos_list = _string_list(_meta_value(meta_obj, "pos_list"))
    scan_shape = [int(x) for x in np.asarray(_meta_value(meta_obj, "scan_shape")).ravel()]
    step_um = _scalar(_meta_value(meta_obj, "step_um"), None)
    if step_um is None:
        step_um = _scalar(_meta_value(meta_obj, "step_size"), None)
    coordinate_unit = _scalar(_meta_value(meta_obj, "coordinate_unit"), None)

    raw_coords: list[tuple[int, str, str, float, float, float]] = []
    for index, pos_text in enumerate(pos_list):
        key = safe_key_from_pos(pos_text)
        if key not in data:
            continue
        x, y, z = parse_pos_text(pos_text)
        raw_coords.append((index, key, pos_text, x, y, z))
    if not raw_coords:
        raise ValueError(f"No waveform fields matching metadata.pos_list were found in {path}")

    x_values = np.array(sorted({_rounded(item[3]) for item in raw_coords}), dtype=float)
    y_values = np.array(sorted({_rounded(item[4]) for item in raw_coords}), dtype=float)
    x_lookup = {value: idx for idx, value in enumerate(x_values.tolist())}
    y_lookup = {value: idx for idx, value in enumerate(y_values.tolist())}
    point_grid = np.full((len(y_values), len(x_values)), -1, dtype=int)
    p2p_image = np.full((len(y_values), len(x_values)), np.nan, dtype=float)

    points: list[BrowserPoint] = []
    waveform_length = math.inf
    for item in raw_coords:
        index, key, pos_text, x, y, z = item
        row = y_lookup[_rounded(y)]
        col = x_lookup[_rounded(x)]
        point_index = len(points)
        point_grid[row, col] = point_index
        waveform = np.asarray(data[key]).ravel()
        waveform_length = min(waveform_length, len(waveform))
        if p2p_window is None:
            segment = waveform
        else:
            start, stop = p2p_window
            start = max(0, int(start))
            stop = len(waveform) if int(stop) < 0 else min(len(waveform), int(stop))
            segment = waveform[start:stop]
        p2p_image[row, col] = float(np.ptp(segment.astype(np.float32))) if segment.size else np.nan
        points.append(
            BrowserPoint(
                index=index,
                key=key,
                pos_text=pos_text,
                x=float(x),
                y=float(y),
                z=float(z),
                row=row,
                col=col,
            )
        )

    coords = np.array([(point.x, point.y, point.z) for point in points], dtype=float)
    meta = {
        "scan_shape": scan_shape,
        "step_um": float(step_um) if step_um is not None else None,
        "coordinate_unit": str(coordinate_unit).strip() if coordinate_unit is not None else None,
        "valid_point_count": len(points),
        "x_range": [float(np.min(coords[:, 0])), float(np.max(coords[:, 0]))],
        "y_range": [float(np.min(coords[:, 1])), float(np.max(coords[:, 1]))],
        "z_range": [float(np.min(coords[:, 2])), float(np.max(coords[:, 2]))],
    }
    return BrowserScan(
        path=path,
        data=data,
        points=points,
        x_values=x_values,
        y_values=y_values,
        point_grid=point_grid,
        p2p_image=p2p_image,
        waveform_length=int(waveform_length),
        meta=meta,
    )


def _resolve_initial_cell(
    scan: BrowserScan,
    initial_row: int | None = None,
    initial_col: int | None = None,
    initial_x: float | None = None,
    initial_y: float | None = None,
) -> tuple[int, int]:
    if initial_x is not None:
        col = int(np.argmin(np.abs(scan.x_values - float(initial_x))))
    elif initial_col is not None:
        col = int(initial_col)
    else:
        col = len(scan.x_values) // 2

    if initial_y is not None:
        row = int(np.argmin(np.abs(scan.y_values - float(initial_y))))
    elif initial_row is not None:
        row = int(initial_row)
    else:
        row = len(scan.y_values) // 2

    row = int(np.clip(row, 0, len(scan.y_values) - 1))
    col = int(np.clip(col, 0, len(scan.x_values) - 1))
    if scan.point_grid[row, col] >= 0:
        return row, col

    valid = np.argwhere(scan.point_grid >= 0)
    distances = (valid[:, 0] - row) ** 2 + (valid[:, 1] - col) ** 2
    nearest = valid[int(np.argmin(distances))]
    return int(nearest[0]), int(nearest[1])


def _resolve_click_cell(scan: BrowserScan, x: float, y: float) -> tuple[int, int]:
    """Map a mouse coordinate to the nearest acquired grid cell."""
    col = int(np.argmin(np.abs(scan.x_values - float(x))))
    row = int(np.argmin(np.abs(scan.y_values - float(y))))
    if scan.point_grid[row, col] >= 0:
        return row, col

    valid = np.argwhere(scan.point_grid >= 0)
    if valid.size == 0:
        return row, col
    valid_x = scan.x_values[valid[:, 1]]
    valid_y = scan.y_values[valid[:, 0]]
    nearest = int(np.argmin((valid_x - float(x)) ** 2 + (valid_y - float(y)) ** 2))
    return int(valid[nearest, 0]), int(valid[nearest, 1])


class WaveformBrowser:
    def __init__(
        self,
        scan: BrowserScan,
        sample_rate_hz: float = 4e9,
        frequency_max_ghz: float = 1.0,
        baseline: tuple[int, int] = (0, 100),
        centered: bool = False,
        initial_row: int | None = None,
        initial_col: int | None = None,
        initial_x: float | None = None,
        initial_y: float | None = None,
        cmap: str = "viridis",
    ):
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle

        self.plt = plt
        self.Rectangle = Rectangle
        self.scan = scan
        self.sample_rate_hz = float(sample_rate_hz)
        self.frequency_max_ghz = float(frequency_max_ghz)
        self.baseline = baseline
        self.centered = bool(centered)
        self.row, self.col = _resolve_initial_cell(scan, initial_row, initial_col, initial_x, initial_y)

        plt.rcParams.update(
            {
                "font.size": 11,
                "font.sans-serif": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
                "axes.unicode_minus": False,
            }
        )
        self.fig = plt.figure(figsize=(14, 8), constrained_layout=True)
        grid = self.fig.add_gridspec(2, 2, width_ratios=[1.05, 1.35], height_ratios=[1, 1])
        self.ax_map = self.fig.add_subplot(grid[:, 0])
        self.ax_time = self.fig.add_subplot(grid[0, 1])
        self.ax_freq = self.fig.add_subplot(grid[1, 1])

        self._setup_map(cmap)
        (self.time_line,) = self.ax_time.plot([], [], color="#0891b2", lw=1.0)
        (self.freq_line,) = self.ax_freq.plot([], [], color="#c026d3", lw=1.0)

        self.ax_time.set_xlabel("时间 (us)")
        self.ax_time.set_ylabel("ADC 幅值")
        self.ax_time.grid(True, alpha=0.25)
        self.ax_freq.set_xlabel("频率 (GHz)")
        self.ax_freq.set_ylabel("Log Mag (dB)")
        self.ax_freq.grid(True, alpha=0.25)
        self.fig.canvas.mpl_connect("key_press_event", self.on_key)
        self.fig.canvas.mpl_connect("button_press_event", self.on_click)
        self.update_plots()

    def _setup_map(self, cmap: str) -> None:
        scan = self.scan
        step_x = _step(scan.x_values, fallback=float(scan.meta.get("step_um") or 1.0))
        step_y = _step(scan.y_values, fallback=step_x)
        x0 = float(scan.x_values.min() - step_x / 2)
        x1 = float(scan.x_values.max() + step_x / 2)
        y0 = float(scan.y_values.min() - step_y / 2)
        y1 = float(scan.y_values.max() + step_y / 2)
        if len(scan.y_values) == 1:
            y0 = float(scan.y_values[0] - step_y / 2)
            y1 = float(scan.y_values[0] + step_y / 2)

        finite = scan.p2p_image[np.isfinite(scan.p2p_image)]
        vmax = float(np.percentile(finite, 99.5)) if finite.size else 1.0
        if not np.isfinite(vmax) or vmax <= 0:
            vmax = float(np.nanmax(scan.p2p_image)) if finite.size else 1.0
        self.map_image = self.ax_map.imshow(
            scan.p2p_image,
            origin="lower",
            extent=[x0, x1, y0, y1],
            aspect="auto" if len(scan.y_values) == 1 else "equal",
            cmap=cmap,
            vmin=0,
            vmax=vmax,
            interpolation="nearest",
        )
        self.fig.colorbar(self.map_image, ax=self.ax_map, fraction=0.046, pad=0.04, label="峰峰值 ADC")
        self.ax_map.set_xlabel("X 坐标 (um)")
        self.ax_map.set_ylabel("Y 坐标 (um)")
        self.ax_map.set_title("空间峰峰值图：点击或方向键选择点")
        self.cursor_rect = self.Rectangle(
            (0, 0),
            step_x,
            step_y,
            linewidth=2.0,
            edgecolor="#22d3ee",
            facecolor="none",
            linestyle="--",
        )
        self.cursor_step_x = step_x
        self.cursor_step_y = step_y
        self.ax_map.add_patch(self.cursor_rect)

    def current_point(self) -> BrowserPoint:
        point_index = int(self.scan.point_grid[self.row, self.col])
        return self.scan.points[point_index]

    def current_waveform(self) -> np.ndarray:
        point = self.current_point()
        return np.asarray(self.scan.data[point.key], dtype=np.float32).ravel()

    def displayed_waveform(self, waveform: np.ndarray) -> np.ndarray:
        if not self.centered:
            return waveform
        b0 = max(0, int(self.baseline[0]))
        b1 = min(len(waveform), int(self.baseline[1]))
        if b1 <= b0:
            return waveform - float(np.median(waveform))
        return waveform - float(np.median(waveform[b0:b1]))

    def update_plots(self) -> None:
        point = self.current_point()
        waveform = self.current_waveform()
        shown = self.displayed_waveform(waveform)
        samples = np.arange(len(shown), dtype=float)
        time_us = samples / self.sample_rate_hz * 1e6

        self.time_line.set_data(time_us, shown)
        self.ax_time.set_xlim(float(time_us[0]), float(time_us[-1]) if len(time_us) else 1.0)
        y_min = float(np.min(shown))
        y_max = float(np.max(shown))
        pad = max(1.0, 0.08 * (y_max - y_min))
        self.ax_time.set_ylim(y_min - pad, y_max + pad)

        freqs = np.fft.rfftfreq(len(shown), d=1.0 / self.sample_rate_hz) / 1e9
        spectrum = 20.0 * np.log10(np.abs(np.fft.rfft(shown)) + 1e-6)
        mask = freqs <= self.frequency_max_ghz
        self.freq_line.set_data(freqs[mask], spectrum[mask])
        self.ax_freq.set_xlim(0.0, self.frequency_max_ghz)
        spec = spectrum[mask]
        s_min = float(np.min(spec))
        s_max = float(np.max(spec))
        s_pad = max(1.0, 0.08 * (s_max - s_min))
        self.ax_freq.set_ylim(s_min - s_pad, s_max + s_pad)

        x_left = float(point.x - self.cursor_step_x / 2)
        y_bottom = float(point.y - self.cursor_step_y / 2)
        self.cursor_rect.set_xy((x_left, y_bottom))

        mode = "去基线" if self.centered else "原始"
        self.ax_time.set_title(f"时域信号 ({mode}) | row={self.row}, col={self.col}, x={point.x:.6g}, y={point.y:.6g}")
        self.ax_freq.set_title(f"频谱 0-{self.frequency_max_ghz:g} GHz | {Path(self.scan.path).name}")
        self.fig.suptitle("鼠标点击或方向键选择点；c 切换原始/去基线；q 关闭", fontsize=13)
        self.fig.canvas.draw_idle()

    def move(self, d_row: int, d_col: int) -> None:
        row = int(np.clip(self.row + d_row, 0, len(self.scan.y_values) - 1))
        col = int(np.clip(self.col + d_col, 0, len(self.scan.x_values) - 1))
        if self.scan.point_grid[row, col] < 0:
            return
        self.row = row
        self.col = col
        self.update_plots()

    def on_key(self, event) -> None:
        key = event.key
        if key == "left":
            self.move(0, -1)
        elif key == "right":
            self.move(0, 1)
        elif key == "up":
            self.move(1, 0)
        elif key == "down":
            self.move(-1, 0)
        elif key == "c":
            self.centered = not self.centered
            self.update_plots()
        elif key in {"q", "escape"}:
            self.plt.close(self.fig)

    def on_click(self, event) -> None:
        if event.inaxes is not self.ax_map or event.xdata is None or event.ydata is None:
            return
        if getattr(event, "button", 1) != 1:
            return
        self.row, self.col = _resolve_click_cell(self.scan, event.xdata, event.ydata)
        self.update_plots()


def launch_waveform_browser(
    path: Path,
    sample_rate_hz: float = 4e9,
    frequency_max_ghz: float = 1.0,
    baseline: tuple[int, int] = (0, 100),
    p2p_window: tuple[int, int] | None = None,
    centered: bool = False,
    initial_row: int | None = None,
    initial_col: int | None = None,
    initial_x: float | None = None,
    initial_y: float | None = None,
    save_preview: Path | None = None,
    show: bool = True,
) -> WaveformBrowser:
    scan = load_browser_scan(path, p2p_window=p2p_window)
    browser = WaveformBrowser(
        scan=scan,
        sample_rate_hz=sample_rate_hz,
        frequency_max_ghz=frequency_max_ghz,
        baseline=baseline,
        centered=centered,
        initial_row=initial_row,
        initial_col=initial_col,
        initial_x=initial_x,
        initial_y=initial_y,
    )
    if save_preview is not None:
        save_preview.parent.mkdir(parents=True, exist_ok=True)
        browser.fig.savefig(save_preview, dpi=180, bbox_inches="tight")
    if show:
        browser.plt.show()
    return browser
