import nidaqmx
from nidaqmx.constants import Edge, AcquisitionType
import threading
import queue
import time
from utils.scan_initial import get_scan_initial
import matplotlib.pyplot as plt
import numpy as np
from ctypes import WinDLL, create_string_buffer
import os
import sys
import keyboard  # 用于监听键盘事件

from numpy.ma.extras import average

# ===================== 采集参数 =====================
DEVICE = "Dev1/ai1"
SAMPLE_RATE = 160000  # 采样率 (Hz)
expected_Hz = 50  # 预期信号的频率
dead_time = 0.5 / expected_Hz  # 死区时间（秒），需要和信号的频率相匹配
SAMPLES_PER_READ = 1000  # 每次读取样本数
THRESHOLD = 4.99  # 脉冲检测阈值，该阈值测试过可以正常计数
PLOT_INTERVAL = 3  # 每隔多少秒刷新一次图
MAX_POINTS_TO_SHOW = 20  # 图中最多显示多少点
PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"  # DLL 地址,注意需要修改
Scan_step_micron = [10, 10]  # 扫描距离
average_times = 50  # 示波器的平均信号次数
pulse_skip = 10  # 初始化时丢弃的脉冲数
pulse_count = 0  # 全局变量 脉冲次数
initial_conut_flag = False  # 全局变量 初始化标识
move_velocity_x_y = [100, 100]  # 扫描移动的速度,移动的时候并不考虑脉冲
scan_horizontal_pixel = 10  # 横向移动的步长  x-to-right-side
scan_horizontal_back = False  # 从头扫到尾部，下一行就从尾部往头部扫
scan_vertical_pixel = 10  # 纵向移动的步长  y-to-down-side
COM_port = "4"  # com口序号
position_now = [0, 0]  # 当前位置  x-y
init_done = False  # 最开始是没有初始化的
exit_flag = False  # 退出标志，用于控制线程退出


# ====================================================

def exit_program():
    """退出程序时执行的特定语句"""
    global exit_flag
    exit_flag = True
    print("\n检测到退出信号，正在停止所有线程...")

    # 执行特定的退出操作
    print("执行退出前的清理工作...")
    # 1. 停止运动控制器
    if 'sessionID' in globals():
        cmd_simple("controller.stage.stop")
        cmd_simple("controller.disconnect")
        SDKPrior.PriorScientificSDK_CloseSession(sessionID)
        SDKPrior.PriorScientificSDK_Shutdown()
    # 2. 保存当前位置信息等操作
    print(f"最后位置: X={position_now[0]}, Y={position_now[1]}")
    print(f"总脉冲计数: {pulse_count}")
    print("程序已安全退出")
    sys.exit(0)


# 注册按键事件，按'q'键退出程序
keyboard.add_hotkey('q', exit_program)
print("程序运行中，按 'q' 键退出...")

get_scan_initial(PATH)
if os.path.exists(PATH):
    SDKPrior = WinDLL(PATH)
else:
    raise RuntimeError("DLL could not be loaded.")
rx = create_string_buffer(1000)


def cmd(msg):
    print(msg)
    ret = SDKPrior.PriorScientificSDK_cmd(
        sessionID, create_string_buffer(msg.encode()), rx
    )
    if ret:
        print(f"Api error {ret}")
    else:
        print(f"OK {rx.value.decode()}")

    return ret, rx.value.decode()


def cmd_simple(msg):
    SDKPrior.PriorScientificSDK_cmd(
        sessionID, create_string_buffer(msg.encode()), rx
    )


ret = SDKPrior.PriorScientificSDK_Initialise()
if ret:
    print(f"Error initialising {ret}")
    sys.exit()
else:
    print(f"Ok initialising {ret}")
ret = SDKPrior.PriorScientificSDK_Version(rx)
print(f"dll version api ret={ret}, version={rx.value.decode()}")

sessionID = SDKPrior.PriorScientificSDK_OpenNewSession()
if sessionID < 0:
    print(f"Error getting sessionID {ret}")
else:
    print(f"SessionID = {sessionID}")
ret = SDKPrior.PriorScientificSDK_cmd(
    sessionID, create_string_buffer(b"dll.apitest 33 goodresponse"), rx
)
print(f"api response {ret}, rx = {rx.value.decode()}")
ret = SDKPrior.PriorScientificSDK_cmd(
    sessionID, create_string_buffer(b"dll.apitest -300 stillgoodresponse"), rx
)
print(f"api response {ret}, rx = {rx.value.decode()}")

'''这一行代码的意义就是向 PriorScientificSDK.dll 发送一条测试命令 
"dll.apitest -300 stillgoodresponse"，并把返回的结果放到 rx 中，
同时获得执行状态码 ret，用于检查 API 调用链路和参数传递是否正常。'''
print("test over, Connecting..., resetting position")
cmd_simple("controller.connect " + COM_port)
cmd_simple("controller.stage.position.set 0 0")
cmd_simple("controller.stage.move-at-velocity " + str(move_velocity_x_y[0]) + " " + str(move_velocity_x_y[1]))
cmd("controller.stage.position.get")

pulse_queue = queue.Queue()


def send_information_and_clear_scope():
    pass


# ---------- 数据采集线程 ----------
def acquisition_thread(
        dead_time_sec=dead_time,  # 死区时间（秒）
        init_pulse_skip=pulse_skip,  # 初始化时丢弃的脉冲数
        sample_rate=SAMPLE_RATE,  # 采样率（Hz）
        samples_per_read=SAMPLES_PER_READ,
        threshold=THRESHOLD
):
    global init_done, exit_flag
    init_skip_count = 0

    # 死区时间对应的采样点数
    dead_time_samples = int(dead_time_sec * sample_rate)
    sample_index = 0
    last_pulse_sample = -dead_time_samples  # 初始化为负，保证第一脉冲能计入

    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(DEVICE, min_val=-10.0, max_val=10.0)
        task.timing.cfg_samp_clk_timing(
            rate=sample_rate,
            active_edge=Edge.RISING,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=samples_per_read
        )

        while not exit_flag:  # 检查退出标志
            data = np.array(task.read(number_of_samples_per_channel=samples_per_read))
            pulse_indices = np.where(data > threshold)[0]

            for idx in pulse_indices:
                global_index = sample_index + idx
                # 检查死区时间
                if global_index - last_pulse_sample >= dead_time_samples:
                    if init_done:
                        t_event = global_index / sample_rate
                        pulse_queue.put(t_event)
                    else:
                        init_skip_count += 1
                        if init_skip_count >= init_pulse_skip:
                            init_done = True
                            print(f"初始化完成，跳过前 {init_pulse_skip} 个脉冲")
                    last_pulse_sample = global_index

            sample_index += samples_per_read
        print("数据采集线程已退出")


# ---------- 读取脉冲次数 ----------
def processing_thread():
    global position_now, scan_horizontal_back, pulse_count, exit_flag
    while not exit_flag:  # 检查退出标志
        try:
            t_event = pulse_queue.get(timeout=1)
        except queue.Empty:
            continue
        # ====== 这里写你的任务 ======
        # 注意：任务要尽量短，避免影响采样
        pulse_count += 1
        print(pulse_count)

        if pulse_count >= average_times:
            send_information_and_clear_scope()

            if not scan_horizontal_back:
                position_now[0] += Scan_step_micron[0]
                if position_now[0] >= Scan_step_micron[0] * scan_horizontal_pixel:
                    print(Scan_step_micron[0] * scan_horizontal_pixel)
                    scan_horizontal_back = True
            else:
                position_now[0] -= Scan_step_micron[0]
                if position_now[0] <= 0:
                    scan_horizontal_back = False
                    position_now[1] += Scan_step_micron[1]

            if position_now[1] >= Scan_step_micron[1] * scan_horizontal_pixel:
                exit_program()  # 完成扫描，调用退出程序

            cmd_simple(f"controller.stage.goto-position {position_now[0]} {position_now[1]}")

            # 等待到位
            flag_temp = True
            while flag_temp and not exit_flag:  # 检查退出标志
                a, b = cmd("controller.stage.position.get")
                curr_x, curr_y = map(float, b.split(','))
                if abs(curr_x - position_now[0]) < 0.001 and abs(curr_y - position_now[1]) < 0.001:
                    flag_temp = False

            print(f"{position_now[0]}, {position_now[1]}")
            time.sleep(1)
            pulse_count = 0

        # ===========================
    print("脉冲处理线程已退出")


# ---------- 初始化脉冲记录,舍去前10个点 ----------
def initial_conut():
    global pulse_count, initial_conut_flag, exit_flag  # 计数多少次脉冲
    while not exit_flag:  # 检查退出标志
        try:
            t_event = pulse_queue.get(timeout=1)
        except queue.Empty:
            continue
        # ====== 这里写你的任务 ======
        # 注意：任务要尽量短，避免影响采样
        pulse_count += 1
        # ===========================
        if pulse_count == 10:
            pulse_count = 0
            initial_conut_flag = True
            break
    print("初始化计数线程已退出")


# ---------- 启动线程 ----------
t_acq = threading.Thread(target=acquisition_thread, daemon=True)
t_initial_conut = threading.Thread(target=initial_conut, daemon=True)
t_acq.start()
t_initial_conut.start()
time.sleep(1)

t_proc = None
if initial_conut_flag:
    t_proc = threading.Thread(target=processing_thread, daemon=True)
    t_proc.start()

# 主线程保持运行，直到退出标志被设置
try:
    while not exit_flag:
        time.sleep(1)
except KeyboardInterrupt:
    exit_program()
