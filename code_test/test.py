import nidaqmx
from nidaqmx.constants import Edge, AcquisitionType
import threading
import queue
import time
import matplotlib.pyplot as plt
import numpy as np
from ctypes import WinDLL, create_string_buffer
import os
import sys

# ===================== 采集参数 =====================
DEVICE = "Dev1/ai0"
SAMPLE_RATE = 160000        # 采样率 (Hz)
SAMPLES_PER_READ = 1000     # 每次读取样本数
THRESHOLD = 4.99            # 脉冲检测阈值
PLOT_INTERVAL = 3           # 每隔多少秒刷新一次图
MAX_POINTS_TO_SHOW = 20     # 图中最多显示多少点
INIT_PULSE_SKIP = 10        # 初始化时丢弃的脉冲数
DEAD_TIME_SEC = 0.001       # 脉冲死区时间 (秒)
PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"

# ====================================================

if os.path.exists(PATH):
    SDKPrior = WinDLL(PATH)
else:
    raise RuntimeError("DLL could not be loaded.")
rx = create_string_buffer(1000)

def cmd_simple(msg):
    SDKPrior.PriorScientificSDK_cmd(
        sessionID, create_string_buffer(msg.encode()), rx
    )

ret = SDKPrior.PriorScientificSDK_Initialise()
if ret:
    print(f"Error initialising {ret}")
    sys.exit()
sessionID = SDKPrior.PriorScientificSDK_OpenNewSession()
cmd_simple("controller.connect 4")
cmd_simple("controller.stage.position.set 0 0")

pulse_queue = queue.Queue()
pulse_count = 0
init_done = False

# ---------- 数据采集线程 ----------
def acquisition_thread():
    global init_done
    init_skip_count = 0

    dead_time_samples = int(DEAD_TIME_SEC * SAMPLE_RATE)
    sample_index = 0
    last_pulse_sample = -dead_time_samples

    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(DEVICE, min_val=-10.0, max_val=10.0)
        task.timing.cfg_samp_clk_timing(
            rate=SAMPLE_RATE,
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=SAMPLES_PER_READ
        )

        while True:
            data = np.array(task.read(number_of_samples_per_channel=SAMPLES_PER_READ))
            pulse_indices = np.where(data > THRESHOLD)[0]

            for idx in pulse_indices:
                global_index = sample_index + idx
                if global_index - last_pulse_sample >= dead_time_samples:
                    if init_done:
                        t_event = global_index / SAMPLE_RATE
                        pulse_queue.put(t_event)
                    else:
                        init_skip_count += 1
                        if init_skip_count >= INIT_PULSE_SKIP:
                            init_done = True
                            print(f"初始化完成，跳过前 {INIT_PULSE_SKIP} 个脉冲")
                    last_pulse_sample = global_index

            sample_index += SAMPLES_PER_READ

# ---------- 任务处理线程 ----------
def processing_thread():
    global pulse_count
    while True:
        try:
            t_event = pulse_queue.get(timeout=1)
        except queue.Empty:
            continue
        pulse_count += 1
        print(f"脉冲计数: {pulse_count}，时间: {t_event:.6f} s")

        # ====== 这里写你的任务 ======
        # 在这里执行针对每个脉冲的任务
        # 注意任务应尽量短，以免影响采样性能
        # ===========================

# ---------- 启动线程 ----------
t_acq = threading.Thread(target=acquisition_thread, daemon=True)
t_proc = threading.Thread(target=processing_thread, daemon=True)
t_acq.start()
t_proc.start()

# 主线程保持运行
while True:
    time.sleep(1)
