"""
PAM Interactive Imaging GUI (升级版)
- 增加防抖后台移动机制
- 增加多面板纯黑背景显示
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
# 数据管理核心 (逻辑保持不变，扩展掩码获取)
# ============================================================================
class MultiResolutionDataStore:
    def __init__(self):
        self.raw_data = {}
        self.masks = {}
        self.all_data_list = []
        
        self.origin_x = 0.0
        self.origin_y = 0.0
        self.scan_w = 65
        self.scan_h = 65
        self.step_um = 1.0
        
        self.cursor_gx = 0
        self.cursor_gy = 0

    def set_grid_params(self, origin_x, origin_y, scan_w, scan_h, step_um):
        self.origin_x = origin_x
        self.origin_y = origin_y
        self.scan_w = scan_w
        self.scan_h = scan_h
        self.step_um = step_um
        if step_um not in self.masks:
            self.masks[step_um] = {}

    def grid_to_world(self, gx, gy, dir_x=1, dir_y=1):
        wx = self.origin_x + gx * self.step_um * dir_x
        wy = self.origin_y + gy * self.step_um * dir_y
        return wx, wy

    def world_to_pos_str(self, wx, wy):
        return f"{wx},{wy},0"

    def store_data(self, gx, gy, waveform, pos_str):
        safe_key = sanitize_pos_to_key(pos_str)
        self.raw_data[safe_key] = waveform
        self.all_data_list.append((waveform, pos_str))
        if self.step_um not in self.masks:
            self.masks[self.step_um] = {}
        self.masks[self.step_um][(gx, gy)] = True

    def get_display_value(self, gx, gy, dir_x=1, dir_y=1):
        wx, wy = self.grid_to_world(gx, gy, dir_x, dir_y)
        pos_str = self.world_to_pos_str(wx, wy)
        safe_key = sanitize_pos_to_key(pos_str)
        
        if self.step_um in self.masks and (gx, gy) in self.masks[self.step_um]:
            if safe_key in self.raw_data:
                return np.ptp(self.raw_data[safe_key].astype(np.float32))
        
        values_from_finer = []
        for other_step, mask in self.masks.items():
            if other_step < self.step_um:
                ratio = self.step_um / other_step
                sub_gx_start, sub_gy_start = int(gx * ratio), int(gy * ratio)
                sub_gx_end, sub_gy_end = int((gx + 1) * ratio), int((gy + 1) * ratio)
                for sgx in range(sub_gx_start, sub_gx_end):
                    for sgy in range(sub_gy_start, sub_gy_end):
                        if (sgx, sgy) in mask:
                            swx, swy = self.grid_to_world(sgx, sgy, dir_x, dir_y)
                            s_pos_str = self.world_to_pos_str(swx, swy)
                            s_safe_key = sanitize_pos_to_key(s_pos_str)
                            if s_safe_key in self.raw_data:
                                values_from_finer.append(np.ptp(self.raw_data[s_safe_key].astype(np.float32)))
        if values_from_finer: return np.mean(values_from_finer)

        for other_step, mask in self.masks.items():
            if other_step > self.step_um:
                ratio = other_step / self.step_um
                parent_gx, parent_gy = int(gx // ratio), int(gy // ratio)
                if (parent_gx, parent_gy) in mask:
                    pwx, pwy = self.grid_to_world(parent_gx, parent_gy, dir_x, dir_y)
                    p_pos_str = self.world_to_pos_str(pwx, pwy)
                    p_safe_key = sanitize_pos_to_key(p_pos_str)
                    if p_safe_key in self.raw_data:
                        return np.ptp(self.raw_data[p_safe_key].astype(np.float32))
        return None

    def build_display_data(self, dir_x=1, dir_y=1):
        """返回 (渲染图像, 掩码图像)"""
        img = np.full((self.scan_h, self.scan_w), np.nan)
        mask_img = np.full((self.scan_h, self.scan_w), np.nan)
        current_mask_dict = self.masks.get(self.step_um, {})
        
        for gy in range(self.scan_h):
            for gx in range(self.scan_w):
                val = self.get_display_value(gx, gy, dir_x, dir_y)
                if val is not None:
                    img[gy, gx] = val
                # 构建掩码，已采集为1，未采集保持 nan(黑色)
                if (gx, gy) in current_mask_dict:
                    mask_img[gy, gx] = 1.0
                    
        return img, mask_img

    def get_latest_waveform_at(self, gx, gy, dir_x=1, dir_y=1):
        wx, wy = self.grid_to_world(gx, gy, dir_x, dir_y)
        safe_key = sanitize_pos_to_key(self.world_to_pos_str(wx, wy))
        if safe_key in self.raw_data:
            return self.raw_data[safe_key].astype(np.float32)
        return None


# ============================================================================
# 后台工作线程
# ============================================================================
class MoveWorker(QThread):
    """专门处理单纯移动的后台线程（防界面卡死）"""
    finished = pyqtSignal()
    def __init__(self, stage, target_x, target_y, start_x, start_y, offset):
        super().__init__()
        self.stage = stage
        self.target_x = target_x
        self.target_y = target_y
        self.start_x = start_x
        self.start_y = start_y
        self.offset = offset

    def run(self):
        try:
            go_to_position([self.start_x, self.start_y], [self.target_x, self.target_y], self.stage, self.offset)
        except Exception as e:
            print(f"移动线程错误: {e}")
        finally:
            self.finished.emit()


class AcquisitionWorker(QThread):
    finished = pyqtSignal(np.ndarray, str, int, int)
    error = pyqtSignal(str)
    def __init__(self, stage, daq, target_x, target_y, gx, gy, start_x, start_y, offset, average_enable, records_per_point):
        super().__init__()
        self.stage = stage; self.daq = daq; self.target_x = target_x; self.target_y = target_y
        self.gx = gx; self.gy = gy; self.start_x = start_x; self.start_y = start_y
        self.offset = offset; self.average_enable = average_enable; self.records_per_point = records_per_point

    def run(self):
        try:
            go_to_position([self.start_x, self.start_y], [self.target_x, self.target_y], self.stage, self.offset)
            pos_str = f"{self.target_x},{self.target_y},0"
            temp_data = []
            self.daq.get_one_acquisition(temp_data, pos_str, 500, self.average_enable)
            if temp_data:
                self.finished.emit(temp_data[-1][0], pos_str, self.gx, self.gy)
            else:
                self.error.emit("采集返回空数据")
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# 画布组件
# ============================================================================
class MapCanvas(FigureCanvas):
    def __init__(self, parent=None):
        self.fig = Figure(figsize=(7, 9), dpi=100)
        self.fig.patch.set_facecolor('#050505') # 极致黑色背景
        
        # 上图：成像结果
        self.ax_map = self.fig.add_subplot(211)
        self.ax_map.set_facecolor('#000000')
        self.ax_map.tick_params(colors='white')
        self.ax_map.set_title("Real-time MAP Reconstruction", color='white')
        
        # 下图：掩码
        self.ax_mask = self.fig.add_subplot(212)
        self.ax_mask.set_facecolor('#000000')
        self.ax_mask.tick_params(colors='white')
        self.ax_mask.set_title("Current Resolution Mask", color='white')

        super().__init__(self.fig)
        self.setParent(parent)
        
        # 配置 Hot 色图，缺失值(nan)强制设为黑色
        self.cmap_hot = plt.cm.hot.copy()
        self.cmap_hot.set_bad('black')
        
        # 配置灰度色图，缺失值(nan)强制设为黑色
        self.cmap_gray = plt.cm.gray.copy()
        self.cmap_gray.set_bad('black')

        self.img_data = np.full((65, 65), np.nan)
        self.mask_data = np.full((65, 65), np.nan)
        
        self.im_map = self.ax_map.imshow(self.img_data, cmap=self.cmap_hot, extent=[0, 65, 65, 0], interpolation='nearest')
        self.im_mask = self.ax_mask.imshow(self.mask_data, cmap=self.cmap_gray, extent=[0, 65, 65, 0], interpolation='nearest', vmin=0, vmax=1)
        
        self.cbar = self.fig.colorbar(self.im_map, ax=self.ax_map)
        self.cbar.ax.yaxis.set_tick_params(color='white')
        plt.setp(plt.getp(self.cbar.ax.axes, 'yticklabels'), color='white')
        
        self.cursor_rect_map = None
        self.cursor_rect_mask = None
        
        # 替代导致初始卡顿的 tight_layout
        self.fig.subplots_adjust(left=0.1, right=0.9, top=0.95, bottom=0.05, hspace=0.25)

    def update_map(self, img_data, mask_data, scan_w, scan_h, step_um, cursor_gx, cursor_gy):
        extent = [0, scan_w * step_um, scan_h * step_um, 0]
        
        # 更新上图 (MAP)
        self.im_map.set_data(img_data)
        self.im_map.set_extent(extent)
        valid = img_data[~np.isnan(img_data)]
        if len(valid) > 0: self.im_map.set_clim(vmin=0, vmax=np.max(valid) + 1)
        
        # 更新下图 (Mask)
        self.im_mask.set_data(mask_data)
        self.im_mask.set_extent(extent)
        
        # 统一处理边界，避免报错
        for ax in [self.ax_map, self.ax_mask]:
            ax.set_xlim(extent[0], extent[1])
            ax.set_ylim(extent[2], extent[3]) # Y轴倒置 (底为H，顶为0)
        
        # 绘制光标
        if self.cursor_rect_map: self.cursor_rect_map.remove()
        if self.cursor_rect_mask: self.cursor_rect_mask.remove()
        rect_x = cursor_gx * step_um
        rect_y = cursor_gy * step_um
        
        self.cursor_rect_map = plt.Rectangle((rect_x, rect_y), step_um, step_um, linewidth=2, edgecolor='cyan', facecolor='none')
        self.cursor_rect_mask = plt.Rectangle((rect_x, rect_y), step_um, step_um, linewidth=2, edgecolor='cyan', facecolor='none')
        self.ax_map.add_patch(self.cursor_rect_map)
        self.ax_mask.add_patch(self.cursor_rect_mask)
        
        self.draw_idle()

class SignalCanvas(FigureCanvas):
    # 保持原样... (为节省字数已折叠，请保留你原始代码中 SignalCanvas 的实现)
    pass # 替换为你原本的 SignalCanvas


# ============================================================================
# 主窗口
# ============================================================================
class PAMMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PAM Interactive Imaging System v2")
        self.setMinimumSize(1400, 850)
        
        # (保持原有的硬件参数设置)
        self.DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
        self.COM_PORT = "4"
        self.SAMPLES_REC, self.SAMPLE_RATE_HZ = 4096, 4e9
        self.SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
        self.AVERAGE_ENABLE, self.RECORDS_PER_POINT = True, 64
        self.SETTLE_MS, self.OFFSET = 100, 50
        
        self.init_hardware()
        self.data_store = MultiResolutionDataStore()
        
        raw_pos = self.stage.get_position()
        self.start_x, self.start_y = [float(v) for v in raw_pos.split(',')[:2]]
        self.data_store.set_grid_params(self.start_x, self.start_y, 65, 65, 1.0)
        
        # ==== 核心防抖移动 Timer ====
        self.move_timer = QTimer()
        self.move_timer.setSingleShot(True)
        self.move_timer.timeout.connect(self.execute_stage_move)
        
        self.worker = None
        self.is_acquiring = False
        self.is_moving = False
        
        self.build_ui()
        self.setup_shortcuts()
        self.refresh_map()

    # -- 初始化及构建 UI (截取修改部分) --
    def build_ui(self):
        # ... (保留原有的 Layout 构建)
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        
        self.map_canvas = MapCanvas()
        main_layout.addWidget(self.map_canvas, stretch=3)
        
        right_panel = QVBoxLayout()
        main_layout.addLayout(right_panel, stretch=2)
        
        self.signal_canvas = SignalCanvas(self.SAMPLES_REC, self.SAMPLE_RATE_HZ)
        right_panel.addWidget(self.signal_canvas, stretch=3)
        
        control_frame = QFrame()
        control_layout = QVBoxLayout(control_frame)
        right_panel.addWidget(control_frame, stretch=2)
        
        # 导航控制
        nav_group = QGroupBox("导航控制")
        nav_layout = QGridLayout(nav_group)
        self.lbl_cursor = QLabel("光标: (0, 0)")
        self.lbl_world_pos = QLabel("世界坐标: (0.0, 0.0)")
        nav_layout.addWidget(self.lbl_cursor, 0, 0, 1, 3, Qt.AlignCenter)
        nav_layout.addWidget(self.lbl_world_pos, 1, 0, 1, 3, Qt.AlignCenter)
        
        # --- 新增的跟随开关按钮 ---
        self.btn_sync_mode = QPushButton("🟢 实时跟随移动 (开)")
        self.btn_sync_mode.setCheckable(True)
        self.btn_sync_mode.setChecked(False) # 默认关闭按下状态 = 开启联动
        self.btn_sync_mode.setStyleSheet("background-color: #2e7d32; color: white; padding: 5px;")
        self.btn_sync_mode.toggled.connect(self.toggle_sync_mode)
        nav_layout.addWidget(self.btn_sync_mode, 2, 0, 1, 3)

        btn_up = QPushButton("▲"); btn_down = QPushButton("▼"); btn_left = QPushButton("◀"); btn_right = QPushButton("▶")
        self.btn_acquire = QPushButton("📡 采集 (Enter)")
        
        btn_up.clicked.connect(lambda: self.move_cursor(0, -1)); btn_down.clicked.connect(lambda: self.move_cursor(0, 1))
        btn_left.clicked.connect(lambda: self.move_cursor(-1, 0)); btn_right.clicked.connect(lambda: self.move_cursor(1, 0))
        self.btn_acquire.clicked.connect(self.acquire_current_point)
        
        nav_layout.addWidget(btn_up, 3, 1); nav_layout.addWidget(btn_left, 4, 0)
        nav_layout.addWidget(self.btn_acquire, 4, 1); nav_layout.addWidget(btn_right, 4, 2); nav_layout.addWidget(btn_down, 5, 1)
        
        self.lbl_status = QLabel("状态: 就绪")
        nav_layout.addWidget(self.lbl_status, 6, 0, 1, 3, Qt.AlignCenter)
        control_layout.addWidget(nav_group)
        
        # ... (保留下方的网格设置和保存按钮逻辑，保持原样)

    def toggle_sync_mode(self, checked):
        """处理联动跟随开关状态"""
        if checked:
            self.btn_sync_mode.setText("🔒 锁定平移台 (按Enter移动)")
            self.btn_sync_mode.setStyleSheet("background-color: #b71c1c; color: white;")
            self.move_timer.stop() # 切断正在计时的防抖
        else:
            self.btn_sync_mode.setText("🟢 实时跟随移动 (开)")
            self.btn_sync_mode.setStyleSheet("background-color: #2e7d32; color: white;")

    def move_cursor(self, dx, dy):
        """键盘或按钮移动光标事件"""
        if self.is_acquiring: return # 正在采集中完全锁定
        
        ds = self.data_store
        new_gx, new_gy = ds.cursor_gx + dx, ds.cursor_gy + dy
        
        if 0 <= new_gx < ds.scan_w and 0 <= new_gy < ds.scan_h:
            ds.cursor_gx = new_gx
            ds.cursor_gy = new_gy
            
            # --- 解析真实方向并更新界面 ---
            dir_x = int(self.spin_dir_x.value()) if hasattr(self, 'spin_dir_x') else 1
            dir_y = int(self.spin_dir_y.value()) if hasattr(self, 'spin_dir_y') else 1
            
            self.update_cursor_display(dir_x, dir_y)
            self.refresh_map()
            
            wf = ds.get_latest_waveform_at(new_gx, new_gy, dir_x, dir_y)
            if wf is not None: self.signal_canvas.update_signal(wf)
            
            # --- 防抖移动逻辑 ---
            if not self.btn_sync_mode.isChecked() and not self.is_moving:
                # 重新计时 300ms，期间如果继续按键，将重新计时（防抖）
                self.move_timer.start(300)

    def execute_stage_move(self):
        """防抖结束后实际执行移动 (通过后台线程)"""
        if self.is_acquiring: return
        
        self.is_moving = True
        self.btn_acquire.setEnabled(False) # 移动中禁用采集按钮
        self.lbl_status.setText("状态: 平移台移动中...")
        
        ds = self.data_store
        dir_x = int(self.spin_dir_x.value()) if hasattr(self, 'spin_dir_x') else 1
        dir_y = int(self.spin_dir_y.value()) if hasattr(self, 'spin_dir_y') else 1
        wx, wy = ds.grid_to_world(ds.cursor_gx, ds.cursor_gy, dir_x, dir_y)
        
        self.worker = MoveWorker(self.stage, wx, wy, self.start_x, self.start_y, self.OFFSET)
        self.worker.finished.connect(self.on_stage_moved)
        self.worker.start()

    def on_stage_moved(self):
        """移动完成回调"""
        self.is_moving = False
        self.btn_acquire.setEnabled(True)
        self.lbl_status.setText("状态: 就绪")

    def acquire_current_point(self):
        """Enter采集核心逻辑"""
        # 如果后台还在移动平台，忽略采集请求
        if self.is_acquiring or self.is_moving:
            self.statusBar().showMessage("正在执行后台动作，请稍后...")
            return
            
        ds = self.data_store
        gx, gy = ds.cursor_gx, ds.cursor_gy
        dir_x = int(self.spin_dir_x.value()) if hasattr(self, 'spin_dir_x') else 1
        dir_y = int(self.spin_dir_y.value()) if hasattr(self, 'spin_dir_y') else 1
        wx, wy = ds.grid_to_world(gx, gy, dir_x, dir_y)
        
        self.is_acquiring = True
        self.lbl_status.setText("状态: 采集中...")
        
        self.worker = AcquisitionWorker(
            self.stage, self.daq, wx, wy, gx, gy,
            self.start_x, self.start_y, self.OFFSET,
            self.AVERAGE_ENABLE, self.RECORDS_PER_POINT
        )
        self.worker.finished.connect(self.on_acquisition_done)
        self.worker.error.connect(self.on_acquisition_error)
        self.worker.start()

    # (保持原有的 refresh_map, save_data, closeEvent 等逻辑...)
    def refresh_map(self):
        ds = self.data_store
        dir_x = int(self.spin_dir_x.value()) if hasattr(self, 'spin_dir_x') else 1
        dir_y = int(self.spin_dir_y.value()) if hasattr(self, 'spin_dir_y') else 1
        
        img, mask = ds.build_display_data(dir_x, dir_y)
        self.map_canvas.update_map(img, mask, ds.scan_w, ds.scan_h, ds.step_um, ds.cursor_gx, ds.cursor_gy)

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
