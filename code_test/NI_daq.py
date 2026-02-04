import nidaqmx
from nidaqmx.constants import Edge, AcquisitionType
import threading
import queue
import time
import matplotlib.pyplot as plt
import numpy as np

# ===================== 采集参数 =====================
DEVICE = "Dev1/ai0"
SAMPLE_RATE = 100_000       # 采样率 (Hz)
SAMPLES_PER_READ = 1000     # 每次读取样本数
THRESHOLD = 5.0             # 脉冲检测阈值
PLOT_INTERVAL = 5           # 每隔多少秒刷新一次图
MAX_POINTS_TO_SHOW = 2000   # 图中最多显示多少点
# ====================================================

pulse_queue = queue.Queue()

# ---------- 数据采集线程 ----------
def acquisition_thread():
    sample_index = 0  # 记录采样点总索引
    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(DEVICE, min_val=-10.0, max_val=10.0)
        task.timing.cfg_samp_clk_timing(
            rate=SAMPLE_RATE,
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=SAMPLES_PER_READ
        )

        while True:
            data = task.read(number_of_samples_per_channel=SAMPLES_PER_READ)
            data = np.array(data)

            # 检测超过阈值的点
            pulse_indices = np.where(data > THRESHOLD)[0]
            for idx in pulse_indices:
                # 将采样索引换算为时间（秒）
                t_event = (sample_index + idx) / SAMPLE_RATE
                pulse_queue.put(t_event)

            sample_index += SAMPLES_PER_READ

# ---------- 数据处理 + 实时绘图线程 ----------
def processing_thread():
    # pulse_times = []
    # last_plot_time = time.time()
    conunt=0
    # plt.ion()
    # fig, ax = plt.subplots(figsize=(10, 4))

    while True:
        try:
            t_event = pulse_queue.get(timeout=1)
        except queue.Empty:
            continue

        # ====== 这里写你的任务 ======
        # 注意：任务要尽量短，避免影响采样
        conunt=conunt + 1
        print(conunt)
        # ===========================

# ---------- 启动线程 ----------
t_acq = threading.Thread(target=acquisition_thread, daemon=True)
t_proc = threading.Thread(target=processing_thread, daemon=True)

t_acq.start()
t_proc.start()

# 主线程保持运行
while True:
    time.sleep(1)
