"""
PAM Interactive Imaging GUI
基于 PyQt5 + Matplotlib 的交互式成像界面
保留原有数据保存方法不变
"""

import gc
import sys
import time
import datetime
import numpy as np
import scipy.io as sio
import matplotlib
matplotlib.use('Qt5Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.colors import Normalize

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QPushButton, QLabel, QSpinBox, QDoubleSpinBox,
    QGroupBox, QFrame, QSplitter, QStatusBar, QMessageBox, QShortcut
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QKeySequence

import atsapi as ats
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Tool_code.position_trans import sanitize_pos_to_key


# ============================================================================
# 数据管理核心：多精度数据存储与掩码管理
# ============================================================================
class MultiResolutionDataStore:
    """
    多精度数据存储管理器
    
    核心设计：
    - 所有数据以 键值对 "位置字符串" : "平均后的时序信号" 存储
    - 每个精度(step)有独立的掩码，标记该精度下哪些位点已经被实际采集
    - 切换精度时不删除任何已有数据
    """
    
    def __init__(self):
        # 核心数据存储: { "x,y,0": np.ndarray(waveform) }
        # 这是所有精度共享的原始数据池
        self.raw_data = {}
        
        # 按精度组织的掩码: { step_um: { (grid_x, grid_y): bool } }
        # True = 该精度下此点已实际采集过
        self.masks = {}
        
        # 用于保存的有序列表 (与原代码 all_data 兼容)
        self.all_data_list = []
        
        # 当前网格参数
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.scan_w = 50      # 网格宽度（格数）
        self.scan_h = 50      # 网格高度（格数）
        self.step_um = 2    # 当前步长
        
        # 当前光标位置 (网格索引)
        self.cursor_gx = 0
        self.cursor_gy = 0
    
    def set_grid_params(self, origin_x, origin_y, scan_w, scan_h, step_um):
        """设置/更新网格参数"""
        old_step = self.step_um
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.scan_w = scan_w
        self.scan_h = scan_h
        self.step_um = step_um
        
        # 确保当前精度有掩码
        if step_um not in self.masks:
            self.masks[step_um] = {}
    
    def grid_to_world(self, gx, gy):
        """网格坐标 -> 世界坐标"""
        wx = self.origin_x + gx * self.step_um
        wy = self.origin_y + gy * self.step_um
        return wx, wy
    
    def world_to_pos_str(self, wx, wy):
        """世界坐标 -> 位置字符串"""
        return f"{wx},{wy},0"
    
    def store_data(self, gx, gy, waveform, pos_str):
        """
        存储采集数据
        - waveform: 平均后的时序信号
        - 标记当前精度的掩码
        """
        # 存入原始数据池
        safe_key = sanitize_pos_to_key(pos_str)
        self.raw_data[safe_key] = waveform
        
        # 存入有序列表 (兼容原保存方法)
        self.all_data_list.append((waveform, pos_str))
        
        # 标记当前精度掩码
        if self.step_um not in self.masks:
            self.masks[self.step_um] = {}
        self.masks[self.step_um][(gx, gy)] = True
    
    def get_display_value(self, gx, gy):
        """
        获取某个网格点的显示值
        
        逻辑：
        1. 如果当前精度下该点有掩码(已采集) -> 返回该点数据
        2. 否则，查找更高精度(更小step)下覆盖该区域的数据点，返回平均值
        3. 如果当前精度比已有数据精度更高，且该点未采集 -> 
           查找更低精度(更大step)下包含该点的数据，返回该值
        4. 都没有 -> 返回 None
        """
        wx, wy = self.grid_to_world(gx, gy)
        pos_str = self.world_to_pos_str(wx, wy)
        safe_key = sanitize_pos_to_key(pos_str)
        
        # 情况1: 当前精度已采集
        if self.step_um in self.masks and (gx, gy) in self.masks[self.step_um]:
            if safe_key in self.raw_data:
                return np.ptp(self.raw_data[safe_key].astype(np.float32))
        
        # 情况2: 查找更高精度(更小step)的子点数据
        values_from_finer = []
        for other_step, mask in self.masks.items():
            if other_step < self.step_um:
                # 当前网格点在更高精度下对应的子区域
                ratio = self.step_um / other_step
                sub_gx_start = int(gx * ratio)
                sub_gy_start = int(gy * ratio)
                sub_gx_end = int((gx + 1) * ratio)
                sub_gy_end = int((gy + 1) * ratio)
                
                for sgx in range(sub_gx_start, sub_gx_end):
                    for sgy in range(sub_gy_start, sub_gy_end):
                        if (sgx, sgy) in mask:
                            swx = self.origin_x + sgx * other_step
                            swy = self.origin_y + sgy * other_step
                            s_pos_str = self.world_to_pos_str(swx, swy)
                            s_safe_key = sanitize_pos_to_key(s_pos_str)
                            if s_safe_key in self.raw_data:
                                values_from_finer.append(
                                    np.ptp(self.raw_data[s_safe_key].astype(np.float32))
                                )
        
        if values_from_finer:
            return np.mean(values_from_finer)
        
        # 情况3: 查找更低精度(更大step)的父点数据
        for other_step, mask in self.masks.items():
            if other_step > self.step_um:
                ratio = other_step / self.step_um
                parent_gx = int(gx // ratio)
                parent_gy = int(gy // ratio)
                
                if (parent_gx, parent_gy) in mask:
                    pwx = self.origin_x + parent_gx * other_step
                    pwy = self.origin_y + parent_gy * other_step
                    p_pos_str = self.world_to_pos_str(pwx, pwy)
                    p_safe_key = sanitize_pos_to_key(p_pos_str)
                    if p_safe_key in self.raw_data:
                        return np.ptp(self.raw_data[p_safe_key].astype(np.float32))
        
        return None
    
    def build_display_image(self):
        """构建当前精度下的完整显示图像"""
        img = np.full((self.scan_h, self.scan_w), np.nan)
        
        for gy in range(self.scan_h):
            for gx in range(self.scan_w):
                val = self.get_display_value(gx, gy)
                if val is not None:
                    img[gy, gx] = val
        
        return img
    
    def get_latest_waveform_at(self, gx, gy):
        """获取某个网格点的最新波形数据"""
        wx, wy = self.grid_to_world(gx, gy)
        pos_str = self.world_to_pos_str(wx, wy)
        safe_key = sanitize_pos_to_key(pos_str)
        if safe_key in self.raw_data:
            return self.raw_data[safe_key].astype(np.float32)
        return None


# ============================================================================
# 采集工作线程
# ============================================================================
class AcquisitionWorker(QThread):
    """在后台线程中执行单点采集，避免阻塞GUI"""
    finished = pyqtSignal(np.ndarray, str, int, int)  # waveform, pos_str, gx, gy
    error = pyqtSignal(str)
    
    def __init__(self, stage, daq, target_x, target_y, gx, gy,
                 start_x, start_y, offset, average_enable, records_per_point):
        super().__init__()
        self.stage = stage
        self.daq = daq
        self.target_x = target_x
        self.target_y = target_y
        self.gx = gx
        self.gy = gy
        self.start_x = start_x
        self.start_y = start_y
        self.offset = offset
        self.average_enable = average_enable
        self.records_per_point = records_per_point
    
    def run(self):
        try:
            # 移动到目标位置（带偏移校正）
            go_to_position(
                curr_position=[self.target_x, self.target_y],
                stage=self.stage,
            )
            
            pos_str = f"{self.target_x},{self.target_y},0"
            temp_data = []
            self.daq.get_one_acquisition(
                all_data=temp_data,
                curr_pos_str=pos_str,
                timeout_ms=500,
                Average_Enable=self.average_enable
            )
            
            if temp_data:
                waveform = temp_data[-1][0]
                self.finished.emit(waveform, pos_str, self.gx, self.gy)
            else:
                self.error.emit("采集返回空数据")
                
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# Matplotlib 画布组件
# ============================================================================
class MapCanvas(FigureCanvas):
    """左侧 MAP 图画布"""
    
    def __init__(self, parent=None):
        # 1. 明确设置 Figure 背景为黑色，避免边缘白边
        self.fig = Figure(figsize=(7, 7), dpi=100, facecolor='black')
        self.ax = self.fig.add_subplot(111)
        # 2. 设置坐标轴背景为黑色
        self.ax.set_facecolor('black')
        
        super().__init__(self.fig)
        self.setParent(parent)
        
        # 3. 获取 hot 色图并设置 nan 值的显示颜色为黑色
        self.my_cmap = plt.cm.get_cmap('hot').copy()
        self.my_cmap.set_bad(color='black')
        
        self.img_data = np.full((65, 65), np.nan)
        self.im = self.ax.imshow(
            self.img_data, 
            cmap=self.my_cmap, # 使用修改后的色图
            extent=[0, 65, 65, 0],
            interpolation='nearest'
        )
        
        # 4. 修改标题和坐标轴颜色，否则在黑底上看不见
        self.ax.set_title("Real-time MAP Reconstruction", color='white')
        self.ax.tick_params(axis='x', colors='white')
        self.ax.tick_params(axis='y', colors='white')
        
        # 颜色条也要处理标签颜色
        self.cbar = self.fig.colorbar(self.im, ax=self.ax)
        self.cbar.set_label('P-P Intensity', color='white')
        self.cbar.ax.yaxis.set_tick_params(color='white')
        plt.setp(plt.getp(self.cbar.ax.axes, 'yticklabels'), color='white')
        
        self.cursor_rect = None
        self.fig.tight_layout()

    def update_map(self, img_data, scan_w, scan_h, step_um, cursor_gx, cursor_gy):
        """更新MAP显示"""
        self.img_data = img_data
        self.im.set_data(img_data)
        
        # 更新范围
        self.im.set_extent([0, scan_w * step_um, scan_h * step_um, 0])
        
        # 动态颜色范围
        valid = img_data[~np.isnan(img_data)]
        if len(valid) > 0:
            v_min = np.min(valid)
            v_max = np.max(valid)
            # 如果最大最小值一样，略微撑开范围避免报错
            if v_min == v_max: v_max += 1
            self.im.set_clim(vmin=v_min, vmax=v_max)
        
        # 更新光标 (保持原逻辑)
        if self.cursor_rect is not None:
            self.cursor_rect.remove()
        
        rect_x = cursor_gx * step_um
        rect_y = cursor_gy * step_um
        self.cursor_rect = plt.Rectangle(
            (rect_x, rect_y), step_um, step_um,
            linewidth=2, edgecolor='cyan', facecolor='none', linestyle='--'
        )
        self.ax.add_patch(self.cursor_rect)
        
        self.draw_idle()

class SignalCanvas(FigureCanvas):
    """右上方 信号 + 频谱 画布"""
    
    def __init__(self, samples_per_record=4096, sample_rate_hz=4e9, parent=None):
        self.fig = Figure(figsize=(6, 6), dpi=100)
        self.ax_time = self.fig.add_subplot(211)
        self.ax_freq = self.fig.add_subplot(212)
        super().__init__(self.fig)
        self.setParent(parent)
        
        self.samples_per_record = samples_per_record
        self.sample_rate_hz = sample_rate_hz
        
        # 时域
        time_axis = np.arange(samples_per_record) / sample_rate_hz * 1e6
        self.line_time, = self.ax_time.plot(
            time_axis, np.zeros(samples_per_record), color='cyan', lw=1
        )
        self.ax_time.set_title("Latest Waveform")
        self.ax_time.set_xlabel("Time (μs)")
        self.ax_time.set_ylabel("Amplitude")
        self.ax_time.grid(True)
        
        # 频域 (0-1 GHz)
        freqs = np.fft.rfftfreq(samples_per_record, d=1/sample_rate_hz) / 1e9
        self.idx_1ghz = np.where(freqs <= 1.0)[0][-1]
        self.freqs_display = freqs[:self.idx_1ghz]
        self.line_freq, = self.ax_freq.plot(
            self.freqs_display, np.zeros(self.idx_1ghz), color='magenta', lw=1
        )
        self.ax_freq.set_title("Log-Power Spectrum (0-1 GHz)")
        self.ax_freq.set_xlabel("Frequency (GHz)")
        self.ax_freq.set_ylabel("Log Mag (dB)")
        self.ax_freq.set_ylim([-20, 100])
        self.ax_freq.grid(True)
        
        self.fig.tight_layout()
    
    def update_signal(self, waveform):
        """更新时域和频域显示"""
        if waveform is None:
            return
        
        wf = waveform.astype(np.float32)
        
        # 时域
        self.line_time.set_ydata(wf)
        self.ax_time.set_ylim([np.min(wf) - 100, np.max(wf) + 100])
        
        # 频域
        fft_mag = np.abs(np.fft.rfft(wf))
        log_spec = 20 * np.log10(fft_mag + 1e-6)
        self.line_freq.set_ydata(log_spec[:self.idx_1ghz])
        spec_min = np.min(log_spec[:self.idx_1ghz])
        spec_max = np.max(log_spec[:self.idx_1ghz])
        self.ax_freq.set_ylim(spec_min - 10, spec_max + 20)
        
        self.draw_idle()


# ============================================================================
# 主窗口
# ============================================================================
class PAMMainWindow(QMainWindow):
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PAM Interactive Imaging System")
        self.setMinimumSize(1400, 800)
        
        # ---- 硬件参数 ----
        self.DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
        self.COM_PORT = "4"
        self.SAMPLES_REC = 4096
        self.SAMPLE_RATE_HZ = 4e9
        self.SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
        self.AVERAGE_ENABLE = True
        self.RECORDS_PER_POINT = 64
        self.SETTLE_MS = 100
        self.OFFSET = 0
        
        # ---- 初始化硬件 ----
        self.init_hardware()
        
        # ---- 数据存储 ----
        self.data_store = MultiResolutionDataStore()
        
        # 读取初始位置
        raw_pos = self.stage.get_position()
        sx, sy = [float(v) for v in raw_pos.split(',')[:2]]
        self.start_x = sx
        self.start_y = sy
        
        # 初始化网格参数
        self.data_store.set_grid_params(
            origin_x=sx, origin_y=sy,
            scan_w=65, scan_h=65, step_um=1.0
        )
        
        # ---- 构建界面 ----
        self.build_ui()
        
        # ---- 键盘快捷键 ----
        self.setup_shortcuts()
        
        # ---- 工作线程 ----
        self.worker = None
        self.is_acquiring = False
        
        # ---- 初始刷新 ----
        self.refresh_map()
        
        self.statusBar().showMessage("就绪 | 使用方向键移动光标，Enter键采集当前点")
    
    def init_hardware(self):
        """初始化硬件设备"""
        self.stage = PriorUnifiedStage(self.DLL_PATH, self.COM_PORT)
        self.daq = AlazarNPTSystem(systemId=1, boardId=1)
        self.daq.configure_board(sample_rate=self.SAMPLE_RATE)
        self.daq.prepare_acquisition(
            acq_channel=ats.CHANNEL_A,
            samples_per_record=self.SAMPLES_REC,
            records_per_buffer=self.RECORDS_PER_POINT,
            buffer_count=4,
            records_per_point=self.RECORDS_PER_POINT
        )
    
    def build_ui(self):
        """构建完整的用户界面"""
        central = QWidget()
        self.setCentralWidget(central)
        
        main_layout = QHBoxLayout(central)
        
        # ============ 左侧：MAP 画布 ============
        self.map_canvas = MapCanvas()
        main_layout.addWidget(self.map_canvas, stretch=3)
        
        # ============ 右侧面板 ============
        right_panel = QVBoxLayout()
        main_layout.addLayout(right_panel, stretch=2)
        
        # ---- 右上：信号画布 ----
        self.signal_canvas = SignalCanvas(
            samples_per_record=self.SAMPLES_REC,
            sample_rate_hz=self.SAMPLE_RATE_HZ
        )
        right_panel.addWidget(self.signal_canvas, stretch=6)
        
        # ---- 右下：参数控制区 ----
        control_frame = QFrame()
        control_frame.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        control_layout = QVBoxLayout(control_frame)
        right_panel.addWidget(control_frame, stretch=2)
        
        # === 1) 方向控制 + 当前位置显示 ===
        nav_group = QGroupBox("导航控制 (方向键 / Enter采集)")
        nav_layout = QGridLayout(nav_group)
        
        self.lbl_cursor = QLabel("光标: (0, 0)")
        self.lbl_cursor.setStyleSheet("font-size: 14px; font-weight: bold; color: #00BFFF;")
        nav_layout.addWidget(self.lbl_cursor, 0, 0, 1, 3, Qt.AlignCenter)
        
        self.lbl_world_pos = QLabel("世界坐标: (0.0, 0.0)")
        self.lbl_world_pos.setStyleSheet("font-size: 12px; color: #888;")
        nav_layout.addWidget(self.lbl_world_pos, 1, 0, 1, 3, Qt.AlignCenter)
        
        btn_up = QPushButton("▲ 上")
        btn_down = QPushButton("▼ 下")
        btn_left = QPushButton("◀ 左")
        btn_right = QPushButton("▶ 右")
        btn_acquire = QPushButton("📡 采集 (Enter)")
        btn_acquire.setStyleSheet("background-color: #2196F3; color: white; font-size: 14px; padding: 8px;")
        
        btn_up.clicked.connect(lambda: self.move_cursor(0, -1))
        btn_down.clicked.connect(lambda: self.move_cursor(0, 1))
        btn_left.clicked.connect(lambda: self.move_cursor(-1, 0))
        btn_right.clicked.connect(lambda: self.move_cursor(1, 0))
        btn_acquire.clicked.connect(self.acquire_current_point)
        
        nav_layout.addWidget(btn_up, 2, 1)
        nav_layout.addWidget(btn_left, 3, 0)
        nav_layout.addWidget(btn_acquire, 3, 1)
        nav_layout.addWidget(btn_right, 3, 2)
        nav_layout.addWidget(btn_down, 4, 1)
        
        # 状态标签
        self.lbl_status = QLabel("状态: 就绪")
        self.lbl_status.setStyleSheet("color: green; font-weight: bold;")
        nav_layout.addWidget(self.lbl_status, 5, 0, 1, 3, Qt.AlignCenter)
        
        control_layout.addWidget(nav_group)

        # === 3) 精度/步长设置 ===
        res_group = QGroupBox("精度设置 (步长 μm)")
        res_layout = QHBoxLayout(res_group)
        
        res_layout.addWidget(QLabel("步长:"))
        self.spin_step = QDoubleSpinBox()
        self.spin_step.setRange(0.01, 1000)
        self.spin_step.setDecimals(2)
        self.spin_step.setSingleStep(0.1)
        self.spin_step.setValue(1.0)
        res_layout.addWidget(self.spin_step)
        
        res_layout.addWidget(QLabel("μm"))
        
        control_layout.addWidget(res_group)

        # === 2) 网格范围设置 ===
        grid_group = QGroupBox("成像网格设置")
        grid_layout = QGridLayout(grid_group)
        
        grid_layout.addWidget(QLabel("起点 X:"), 0, 0)
        self.spin_origin_x = QDoubleSpinBox()
        self.spin_origin_x.setRange(-1e6, 1e6)
        self.spin_origin_x.setDecimals(2)
        self.spin_origin_x.setValue(self.start_x)
        grid_layout.addWidget(self.spin_origin_x, 0, 1)
        
        grid_layout.addWidget(QLabel("起点 Y:"), 0, 2)
        self.spin_origin_y = QDoubleSpinBox()
        self.spin_origin_y.setRange(-1e6, 1e6)
        self.spin_origin_y.setDecimals(2)
        self.spin_origin_y.setValue(self.start_y)
        grid_layout.addWidget(self.spin_origin_y, 0, 3)
        
        grid_layout.addWidget(QLabel("宽 (格数):"), 1, 0)
        self.spin_w = QSpinBox()
        self.spin_w.setRange(1, 10000)
        self.spin_w.setValue(65)
        grid_layout.addWidget(self.spin_w, 1, 1)
        
        grid_layout.addWidget(QLabel("高 (格数):"), 1, 2)
        self.spin_h = QSpinBox()
        self.spin_h.setRange(1, 10000)
        self.spin_h.setValue(65)
        grid_layout.addWidget(self.spin_h, 1, 3)
        
        # 注意：宽高可以是负数代表反向，但格数始终正数
        # 用 "方向" 来控制正负
        grid_layout.addWidget(QLabel("X方向:"), 2, 0)
        self.spin_dir_x = QDoubleSpinBox()
        self.spin_dir_x.setRange(-1, 1)
        self.spin_dir_x.setSingleStep(2)
        self.spin_dir_x.setDecimals(0)
        self.spin_dir_x.setValue(1)  # 1=正向, -1=反向
        self.spin_dir_x.setToolTip("1=正向(往右), -1=反向(往左)")
        grid_layout.addWidget(self.spin_dir_x, 2, 1)
        
        grid_layout.addWidget(QLabel("Y方向:"), 2, 2)
        self.spin_dir_y = QDoubleSpinBox()
        self.spin_dir_y.setRange(-1, 1)
        self.spin_dir_y.setSingleStep(2)
        self.spin_dir_y.setDecimals(0)
        self.spin_dir_y.setValue(1)
        self.spin_dir_y.setToolTip("1=正向(往下), -1=反向(往上)")
        grid_layout.addWidget(self.spin_dir_y, 2, 3)
        
        control_layout.addWidget(grid_group)
        

        
        # === 4) 应用参数 + 保存按钮 ===
        btn_layout = QHBoxLayout()
        
        btn_apply = QPushButton("🔄 应用网格参数")
        btn_apply.setStyleSheet("background-color: #FF9800; color: white; font-size: 13px; padding: 6px;")
        btn_apply.clicked.connect(self.apply_grid_params)
        btn_layout.addWidget(btn_apply)
        
        btn_save = QPushButton("💾 保存数据")
        btn_save.setStyleSheet("background-color: #4CAF50; color: white; font-size: 13px; padding: 6px;")
        btn_save.clicked.connect(self.save_data)
        btn_layout.addWidget(btn_save)
        
        btn_auto_scan = QPushButton("🚀 自动扫描全部")
        btn_auto_scan.setStyleSheet("background-color: #9C27B0; color: white; font-size: 13px; padding: 6px;")
        btn_auto_scan.clicked.connect(self.auto_scan_all)
        btn_layout.addWidget(btn_auto_scan)
        
        control_layout.addLayout(btn_layout)
    
    def setup_shortcuts(self):
        """设置键盘快捷键"""
        # 方向键
        QShortcut(QKeySequence(Qt.Key_Up), self).activated.connect(
            lambda: self.move_cursor(0, -1))
        QShortcut(QKeySequence(Qt.Key_Down), self).activated.connect(
            lambda: self.move_cursor(0, 1))
        QShortcut(QKeySequence(Qt.Key_Left), self).activated.connect(
            lambda: self.move_cursor(-1, 0))
        QShortcut(QKeySequence(Qt.Key_Right), self).activated.connect(
            lambda: self.move_cursor(1, 0))
        
        # Enter 采集
        QShortcut(QKeySequence(Qt.Key_Return), self).activated.connect(
            self.acquire_current_point)
        QShortcut(QKeySequence(Qt.Key_Enter), self).activated.connect(
            self.acquire_current_point)
    
    def move_cursor(self, dx, dy):
        """移动光标"""
        ds = self.data_store
        new_gx = ds.cursor_gx + dx
        new_gy = ds.cursor_gy + dy
        
        # 边界检查
        if 0 <= new_gx < ds.scan_w and 0 <= new_gy < ds.scan_h:
            ds.cursor_gx = new_gx
            ds.cursor_gy = new_gy
            self.update_cursor_display()
            self.refresh_map()
            
            # 如果该点有波形数据，更新信号显示
            wf = ds.get_latest_waveform_at(new_gx, new_gy)
            if wf is not None:
                self.signal_canvas.update_signal(wf)
    
    def update_cursor_display(self):
        """更新光标位置标签"""
        ds = self.data_store
        wx, wy = ds.grid_to_world(ds.cursor_gx, ds.cursor_gy)
        self.lbl_cursor.setText(f"光标: ({ds.cursor_gx}, {ds.cursor_gy})")
        self.stage.set_position([wx, wy])
        self.lbl_world_pos.setText(f"世界坐标: ({wx:.2f}, {wy:.2f})")
    
    def acquire_current_point(self):
        """采集当前光标所在点"""
        if self.is_acquiring:
            self.statusBar().showMessage("正在采集中，请等待...")
            return
        
        ds = self.data_store
        gx, gy = ds.cursor_gx, ds.cursor_gy
        
        # 计算世界坐标（考虑方向）
        dir_x = int(self.spin_dir_x.value()) if self.spin_dir_x.value() != 0 else 1
        dir_y = int(self.spin_dir_y.value()) if self.spin_dir_y.value() != 0 else 1
        
        wx = ds.origin_x + gx * ds.step_um * dir_x
        wy = ds.origin_y + gy * ds.step_um * dir_y
        
        self.is_acquiring = True
        self.lbl_status.setText("状态: 采集中...")
        self.lbl_status.setStyleSheet("color: orange; font-weight: bold;")
        
        self.worker = AcquisitionWorker(
            stage=self.stage, daq=self.daq,
            target_x=wx, target_y=wy,
            gx=gx, gy=gy,
            start_x=self.start_x, start_y=self.start_y,
            offset=self.OFFSET,
            average_enable=self.AVERAGE_ENABLE,
            records_per_point=self.RECORDS_PER_POINT
        )
        self.worker.finished.connect(self.on_acquisition_done)
        self.worker.error.connect(self.on_acquisition_error)
        self.worker.start()
    
    def on_acquisition_done(self, waveform, pos_str, gx, gy):
        """采集完成回调"""
        self.is_acquiring = False
        
        # 存储数据
        self.data_store.store_data(gx, gy, waveform, pos_str)
        
        # 更新显示
        self.refresh_map()
        self.signal_canvas.update_signal(waveform.astype(np.float32))
        
        self.lbl_status.setText(f"状态: 采集完成 ({gx},{gy})")
        self.lbl_status.setStyleSheet("color: green; font-weight: bold;")
        self.statusBar().showMessage(
            f"已采集点 ({gx},{gy}) | 位置: {pos_str} | P-P: {np.ptp(waveform.astype(np.float32)):.1f}"
        )
    
    def on_acquisition_error(self, error_msg):
        """采集错误回调"""
        self.is_acquiring = False
        self.lbl_status.setText(f"状态: 错误!")
        self.lbl_status.setStyleSheet("color: red; font-weight: bold;")
        self.statusBar().showMessage(f"采集错误: {error_msg}")
    
    def apply_grid_params(self):
        """应用新的网格参数"""
        new_origin_x = self.spin_origin_x.value()
        new_origin_y = self.spin_origin_y.value()
        new_w = self.spin_w.value()
        new_h = self.spin_h.value()
        new_step = self.spin_step.value()
        
        self.data_store.set_grid_params(
            origin_x=new_origin_x,
            origin_y=new_origin_y,
            scan_w=new_w,
            scan_h=new_h,
            step_um=new_step
        )
        
        # 确保光标在新范围内
        if self.data_store.cursor_gx >= new_w:
            self.data_store.cursor_gx = new_w - 1
        if self.data_store.cursor_gy >= new_h:
            self.data_store.cursor_gy = new_h - 1
        
        self.update_cursor_display()
        self.refresh_map()
        
        self.statusBar().showMessage(
            f"网格参数已更新: 起点({new_origin_x:.1f},{new_origin_y:.1f}) "
            f"大小{new_w}x{new_h} 步长{new_step}μm"
        )
    
    def refresh_map(self):
        """刷新MAP显示"""
        ds = self.data_store
        img = ds.build_display_image()
        self.map_canvas.update_map(
            img, ds.scan_w, ds.scan_h, ds.step_um,
            ds.cursor_gx, ds.cursor_gy
        )
    
    def auto_scan_all(self):
        """自动扫描所有未采集的点"""
        if self.is_acquiring:
            self.statusBar().showMessage("正在采集中，请等待...")
            return
        
        ds = self.data_store
        # 收集所有未采集的点
        self.scan_queue = []
        for gy in range(ds.scan_h):
            line_gx = list(range(ds.scan_w))
            if gy % 2 == 1:
                line_gx.reverse() 
            for gx in line_gx:
                if ds.step_um not in ds.masks or (gx, gy) not in ds.masks[ds.step_um]:
                    self.scan_queue.append((gx, gy))
        
        if not self.scan_queue:
            QMessageBox.information(self, "提示", "当前精度下所有点均已采集")
            return
        
        self.scan_total = len(self.scan_queue)
        self.scan_index = 0
        self.statusBar().showMessage(
            f"开始自动扫描: 共 {self.scan_total} 个点待采集"
        )
        self.auto_scan_next()
    
    def auto_scan_next(self):
        """自动扫描下一个点"""
        if self.scan_index >= len(self.scan_queue):
            self.statusBar().showMessage("自动扫描完成!")
            self.lbl_status.setText("状态: 扫描完成")
            self.lbl_status.setStyleSheet("color: green; font-weight: bold;")
            return
        
        gx, gy = self.scan_queue[self.scan_index]
        self.data_store.cursor_gx = gx
        self.data_store.cursor_gy = gy
        self.update_cursor_display()
        
        self.lbl_status.setText(
            f"自动扫描: {self.scan_index+1}/{self.scan_total}"
        )
        
        # 启动采集 (复用单点采集逻辑)
        ds = self.data_store
        dir_x = int(self.spin_dir_x.value()) if self.spin_dir_x.value() != 0 else 1
        dir_y = int(self.spin_dir_y.value()) if self.spin_dir_y.value() != 0 else 1
        wx = ds.origin_x + gx * ds.step_um * dir_x
        wy = ds.origin_y + gy * ds.step_um * dir_y
        
        self.is_acquiring = True
        self.worker = AcquisitionWorker(
            stage=self.stage, daq=self.daq,
            target_x=wx, target_y=wy,
            gx=gx, gy=gy,
            start_x=self.start_x, start_y=self.start_y,
            offset=self.OFFSET,
            average_enable=self.AVERAGE_ENABLE,
            records_per_point=self.RECORDS_PER_POINT
        )
        self.worker.finished.connect(self.on_auto_scan_point_done)
        self.worker.error.connect(self.on_acquisition_error)
        self.worker.start()
    
    def on_auto_scan_point_done(self, waveform, pos_str, gx, gy):
        """自动扫描单点完成"""
        self.is_acquiring = False
        self.data_store.store_data(gx, gy, waveform, pos_str)
        self.refresh_map()
        self.signal_canvas.update_signal(waveform.astype(np.float32))
        
        self.scan_index += 1
        self.statusBar().showMessage(
            f"自动扫描进度: {self.scan_index}/{self.scan_total} | "
            f"当前点({gx},{gy}) P-P: {np.ptp(waveform.astype(np.float32)):.1f}"
        )
        
        # 继续下一个 (用 QTimer 避免递归过深)
        QTimer.singleShot(10, self.auto_scan_next)
    
    def save_data(self):
        """保存数据 (保持与原代码完全兼容的格式)"""
        save_path = f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mat"
        
        ds = self.data_store
        all_data = ds.all_data_list
        
        save_mat_data(
            all_data=all_data,
            SCAN_W=ds.scan_w,
            SCAN_H=ds.scan_h,
            STEP_UM=ds.step_um,
            AVERAGE_ENABLE=self.AVERAGE_ENABLE,
            RECORDS_PER_POINT=self.RECORDS_PER_POINT,
            save_path=save_path
        )
        
        # 同时保存MAP图片
        self.map_canvas.fig.savefig(save_path.replace(".mat", "_map.png"), dpi=150)
        
        self.statusBar().showMessage(f"数据已保存至 {save_path}")
        QMessageBox.information(self, "保存成功", f"数据已保存至:\n{save_path}")
    
    def closeEvent(self, event):
        """窗口关闭时归位"""
        reply = QMessageBox.question(
            self, "确认退出",
            "是否保存数据并退出？\n(平台将归位至初始位置)",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel
        )
        
        if reply == QMessageBox.Save:
            self.save_data()
            self.stage.set_position([self.start_x, self.start_y])
            event.accept()
        elif reply == QMessageBox.Discard:
            self.stage.set_position([self.start_x, self.start_y])
            event.accept()
        else:
            event.ignore()


# ============================================================================
# 辅助函数 (保持与原代码一致)
# ============================================================================
def go_to_position(curr_position: list, stage: PriorUnifiedStage):
    stage.set_position([curr_position[0], curr_position[1]])
    stage.wait_until_settled(curr_position[0], curr_position[1], settle_time_ms=400)


def save_mat_data(all_data, SCAN_W, SCAN_H, STEP_UM, AVERAGE_ENABLE, RECORDS_PER_POINT, save_path):
    print(f"💾 正在保存原始数据至 {save_path}...")
    mat_dict = {}
    index_to_pos = []
    for item in all_data:
        raw_data = item[0]
        pos_str = item[1]
        safe_key = sanitize_pos_to_key(pos_str)
        processed_data = (raw_data / RECORDS_PER_POINT).astype(np.uint16) if AVERAGE_ENABLE else raw_data.astype(np.uint16)
        mat_dict[safe_key] = processed_data
        index_to_pos.append(pos_str)
    mat_dict["metadata"] = {
        "scan_shape": [SCAN_W, SCAN_H],
        "step_um": STEP_UM,
        "pos_list": index_to_pos
    }
    sio.savemat(save_path, mat_dict)
    print(f"✅ 保存完成，共 {len(index_to_pos)} 个数据点")


# ============================================================================
# 程序入口
# ============================================================================
def main():
    app = QApplication(sys.argv)
    
    # 设置全局样式
    app.setStyleSheet("""
        QMainWindow { background-color: #1a1a2e; }
        QWidget { background-color: #16213e; color: #e0e0e0; }
        QGroupBox { 
            border: 1px solid #0f3460; 
            border-radius: 5px; 
            margin-top: 10px; 
            padding-top: 15px;
            font-weight: bold;
            color: #e0e0e0;
        }
        QGroupBox::title { 
            subcontrol-origin: margin; 
            left: 10px; 
            padding: 0 5px; 
        }
        QPushButton { 
            background-color: #0f3460; 
            color: white; 
            border: none; 
            border-radius: 4px; 
            padding: 6px 12px;
            font-size: 12px;
        }
        QPushButton:hover { background-color: #533483; }
        QPushButton:pressed { background-color: #e94560; }
        QSpinBox, QDoubleSpinBox { 
            background-color: #1a1a2e; 
            color: #00d2ff; 
            border: 1px solid #0f3460; 
            border-radius: 3px; 
            padding: 3px;
        }
        QLabel { color: #e0e0e0; }
        QStatusBar { background-color: #0f3460; color: #00d2ff; }
    """)
    
    window = PAMMainWindow()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
