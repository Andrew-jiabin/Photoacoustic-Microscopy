import nidaqmx
from nidaqmx.constants import Edge, AcquisitionType
import threading
import queue
import time
import pyvisa
from tqdm import tqdm
from scipy.io import savemat
from utils.scan_initial import get_scan_initial
import numpy as np
from ctypes import WinDLL, create_string_buffer
import os
import sys
import re

# ===================== 采集参数 =====================
debug=True                  # 出问题debug时候的flag False or True
# DEVICE = "Dev1/ai0"          # 采集卡的数据名称
# SAMPLE_RATE = 160000        # 采样率 (Hz)
# expected_Hz= 80e3            # 预期信号的频率
# dead_time= 1/expected_Hz  # 死区时间（秒），需要和信号的频率相匹配
# SAMPLES_PER_READ = 1000     # 每次读取样本数,一般任何频率的触发信号不改
# THRESHOLD = 1.5           # 脉冲检测阈值，该阈值测试过可以正常计数
PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll" # DLL 地址,注意需要修改
Scope_address = "USB0::0x2A8D::0x9046::MY63410110::0::INSTR"
visa32_dll_path = "C:/Windows/System32/visa32.dll"
# USB0::0x2A8D::0x9046::MY63410110::0::INSTR
DATA_save_path = r"D:\LJB\PAM\data_csv"
position_time_waiting_saving = 1
position_time_waiting_recording = 2
Scan_step_micron=[100,100]    # 扫描距离
average_times=1           # 示波器内部的平均信号次数
average_times_code=1       # 重复记录信号次数
# pulse_skip=10               # 初始化时丢弃的脉冲数
# pulse_count=0              # 全局变量 脉冲次数
initial_conut_flag=False    # 全局变量 初始化标识
scan_horizontal_pixel=70    # 横向移动的步长
scan_vertical_pixel=40      # 纵向移动的步长
point_index = 0             # 扫描到第几个点了
# 则实际扫描范围是 (scan_horizontal_pixel+1)*(scan_vertical_pixel+1)个像素
scan_horizontal_back=False  # 从头扫到尾部，下一行就从尾部往头部扫
COM_port = "4"              # 位移台 COM 口序号 TODO
position_now = [0,0]        # 当前位置  x-y
init_done = False           # 最开始是没有初始化的
double_flag = False         # 在左右拐的时候，上下有两个点分别要扫，所以要判断这是先扫了还是后扫的
scan_finished = False       # 扫描完成，结束ch
data_save_method=2          # 2 是指保存在示波器上，1 是保存在电脑上,之前行不通好像是因为示波器本身出了问题
Whole_data=[]               # 存放采样数据的数组
# ====================================================



# ================ 纳米平移台初始化及其command函数 ==========
get_scan_initial(PATH)
if os.path.exists(PATH):
    SDKPrior = WinDLL(PATH)
else:
    raise RuntimeError("DLL could not be loaded.")
rx = create_string_buffer(1000)

def cmd(msg):
    if debug:print(msg)
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
def cmd_for_get_position(msg):
    SDKPrior.PriorScientificSDK_cmd(
        sessionID, create_string_buffer(msg.encode()), rx
    )

    return rx.value.decode()

ret = SDKPrior.PriorScientificSDK_Initialise()
if debug:
    if ret:
        print(f"Error initialising {ret}")
        sys.exit()
    else:
        print(f"Ok initialising {ret}")
ret = SDKPrior.PriorScientificSDK_Version(rx)
if debug:
    print(f"dll version api ret={ret}, version={rx.value.decode()}")

sessionID = SDKPrior.PriorScientificSDK_OpenNewSession()
if debug:
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

print("test over, Connecting..., resetting position and now is in:")
cmd_simple("controller.connect "+COM_port)
cmd_simple("controller.stage.position.set 0 0")
cmd("controller.stage.position.get")
pulse_queue = queue.Queue()
# ================ 纳米平移台初始化及其command函数 ==========
