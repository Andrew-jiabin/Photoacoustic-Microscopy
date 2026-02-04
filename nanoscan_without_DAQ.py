# import nidaqmx
# from nidaqmx.constants import Edge, AcquisitionType
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
COM_port = "4"              # 位移台 COM 口序号
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


# ===================== scope 初始化以及相关函数 =====================
rm = pyvisa.ResourceManager(visa32_dll_path)
Scope = rm.open_resource(Scope_address)


def send_information_and_clear_scope(Iteratinon,average_times_code_temp,path):
    if (data_save_method == 1) or (data_save_method == 3):  # 保存到电脑 CSV
        # --- 1. 公共部分：先获取参数和原始数据 ---
        # 必须先获取这些缩放参数，后续无论是存CSV还是存内存都需要用到
        # 注意：Scope.query 返回的是字符串，必须转为 float 才能参与计算
        x_increment = float(Scope.query(":WAVeform:XINCrement?"))
        x_origin    = float(Scope.query(":WAVeform:XORigin?"))
        y_increment = float(Scope.query(":WAVeform:YINCrement?"))
        y_origin    = float(Scope.query(":WAVeform:YORigin?"))

        # 获取原始波形数据字符串
        Scope.write(":WAVeform:STReaming OFF")
        raw_data_str = Scope.query(":WAVeform:DATA?")

        # 正则解析数据
        pattern = re.compile(r'[-+]?\d*\.\d+E[-+]?\d+')
        tt = pattern.findall(raw_data_str)

        # 转换为 numpy 数组 (这是 Raw Data，还未物理还原)
        # 使用 float32 节省内存，适合深度学习
        raw_datas = np.array([float(ttt) for ttt in tt], dtype=np.float32)
        nLength = len(raw_datas)
        if debug:
            print(f"Number of data values: {nLength}")
        # --- 2. 核心计算：物理量还原 (向量化处理) ---
        # 这一步替换了之前的 for 循环，直接对整个数组进行数学运算
        # Voltage = (Raw * Y_Inc) + Y_Org
        voltages = (raw_datas * y_increment) + y_origin
        # Time = X_Org + (Index * X_Inc)
        # 创建一个从 0 到 nLength-1 的索引数组
        indices = np.arange(nLength, dtype=np.float32)
        times = x_origin + (indices * x_increment)
    elif data_save_method == 2:  # 保存在示波器本地
        pass
    else:
        print("ERROR: data_save_method must be 1, 2, or 3.")

    # --- 3. 根据保存方式分流 ---

    if data_save_method == 1:  # 保存到电脑 CSV
        filename = f"{path}\\data_{Iteratinon}_{average_times_code_temp}.csv"
        
        # 之前你的代码在这里有个 bug (用了 data_words[i] 而非 datas[i])
        # 现在我们使用 numpy 的 column_stack 将时间与电压合并，然后一次性写入
        # 格式：第一列 Time，第二列 Voltage
        save_data = np.column_stack((times, voltages))
        
        # 使用 numpy 直接保存，比 for 循环 f.write 快得多
        with open(filename, "a+") as f:
            np.savetxt(f, save_data, fmt='%E, %f', delimiter=', ')
            
        if debug:
            print("Waveform data written to CSV (Vectorized).")

    elif data_save_method == 2:  # 保存在示波器本地
        Scope.write(f":DISK:SAVE:WAVeform CHANnel3,\"{Iteratinon}_{average_times_code_temp}\",CSV,ON")

    elif data_save_method == 3:  # 保存到内存 (用于后续 EEG/AI 分析)
        # 这里是修复的重点：
        # 1. 我们现在存入的是计算好的 voltages (真实电压值)，而不是 raw_datas
        # 2. 如果你的 AI 模型只需要电压数据（通常如此），这样就够了
        # 3. 如果你的模型是 Time-Series 强相关且采样率不固定，你可能需要把 times 也存进去
        
        Whole_data.append(voltages)
        
        if debug:
            print(f"Appended processed voltage data shape: {voltages.shape}")
# ===================== scope 初始化以及相关函数 =====================



def processing_thread():
    global position_now,scan_horizontal_back,scan_finished,point_index,DATA_save_path,average_times_code
    average_times_code_temp=average_times_code
    with tqdm(total=(scan_horizontal_pixel + 1) * (scan_vertical_pixel + 1),
              unit="pixel", leave=True, dynamic_ncols=True, colour="blue") as pbar:
        pbar.set_description(f"Position: [{position_now[0]},{position_now[1]}]")
        pbar.update(1)
        while True:
            time.sleep(position_time_waiting_recording)
            send_information_and_clear_scope(point_index,average_times_code_temp,DATA_save_path)
            time.sleep(position_time_waiting_saving)
            average_times_code_temp-=1
            if average_times_code_temp==0:
                if not scan_horizontal_back:
                    position_now[0] += Scan_step_micron[0]
                    if position_now[0] >= (Scan_step_micron[0]+1) * scan_horizontal_pixel:
                        position_now[0] -= Scan_step_micron[0]
                        position_now[1] += Scan_step_micron[1]
                        scan_horizontal_back = True
                else:
                    position_now[0] -= Scan_step_micron[0]
                    if position_now[0] <= -scan_horizontal_pixel:
                        position_now[0] += Scan_step_micron[0]
                        position_now[1] += Scan_step_micron[1]
                        scan_horizontal_back = False

                if position_now[1] >= (scan_vertical_pixel+1) * Scan_step_micron[1]:
                    cmd_simple(f"controller.stage.goto-position 0 0")
                    flag_temp = True
                    while flag_temp:
                        b = cmd_for_get_position("controller.stage.position.get")
                        curr_x, curr_y = map(float, b.split(','))
                        if abs(curr_x - 0) < 0.001 and abs(curr_y - 0) < 0.001:
                            flag_temp = False
                            scan_finished=True
                    print("back to origin " + b)
                    # Scope.close()
                    exit(0)
                point_index+=1
                cmd_simple(f"controller.stage.goto-position {position_now[0]} {position_now[1]}")

                # 等待到位
                flag_temp = True
                while flag_temp:
                    b = ""
                    while(b=="") or (len(b.split(','))==1):
                        b = cmd_for_get_position("controller.stage.position.get")
                    try:
                        curr_x, curr_y = map(float, b.split(','))
                    except ValueError:
                        continue  # 跳过这一帧，继续下一次循环
                    if abs(curr_x - position_now[0]) < 0.001 and abs(curr_y - position_now[1]) < 0.001:
                        flag_temp = False
                        pbar.set_description(f"Position: [{curr_x},{curr_y}], Data_len: {len(Whole_data)}")
                if debug:print(f"{curr_x}, {curr_y}")
                pbar.update(1)
                average_times_code_temp = average_times_code

        # ===========================



 
# ---------- 启动线程 ----------
t_proc = threading.Thread(target=processing_thread, daemon=True)
t_proc.start()




# 主线程保持运行
while True:
    time.sleep(1)
    if scan_finished==True:
        if data_save_method==3:
            Whole_data=np.stack(Whole_data, axis=0)
            savemat("result.mat", {"data": Whole_data})
        break
    # Scope.close()
    # cmd_simple(f"controller.stage.goto-position 0 0")