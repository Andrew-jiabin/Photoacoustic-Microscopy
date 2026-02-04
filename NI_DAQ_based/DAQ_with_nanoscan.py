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
debug=False                  # 出问题debug时候的flag False or True
DEVICE = "Dev1/ai0"          # 采集卡的数据名称
SAMPLE_RATE = 160000        # 采样率 (Hz)
expected_Hz= 80e3            # 预期信号的频率
dead_time= 1/expected_Hz  # 死区时间（秒），需要和信号的频率相匹配
SAMPLES_PER_READ = 1000     # 每次读取样本数,一般任何频率的触发信号不改
THRESHOLD = 1.5           # 脉冲检测阈值，该阈值测试过可以正常计数
PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll" # DLL 地址,注意需要修改
Scope_address = "USB0::0x2A8D::0x9046::MY63410110::0::INSTR"
# USB0::0x2A8D::0x9046::MY63410110::0::INSTR
DATA_save_path = r"D:\LJB\PAM\data_csv"
Scan_step_micron=[100,100]    # 扫描距离
average_times=1           # 示波器内部的平均信号次数
average_times_code=1       # 重复记录信号次数
pulse_skip=10               # 初始化时丢弃的脉冲数
pulse_count=0              # 全局变量 脉冲次数
initial_conut_flag=False    # 全局变量 初始化标识
scan_horizontal_pixel=70    # 横向移动的步长  x-to-right-side
scan_vertical_pixel=40      # 纵向移动的步长  y-to-down-side
point_index = 0             # 扫描到第几个点了
# 则实际扫描范围是 (scan_horizontal_pixel+1)*(scan_vertical_pixel+1)个像素
scan_horizontal_back=False  # 从头扫到尾部，下一行就从尾部往头部扫
COM_port = "4"              # com口序号
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
rm = pyvisa.ResourceManager('C:/Windows/System32/visa32.dll')
Scope = rm.open_resource('USB0::0x2A8D::0x9046::MY63410110::0::INSTR')


def send_information_and_clear_scope(Iteratinon,average_times_code_temp,path):
    if data_save_method==1:
        x_increment = Scope.query(":WAVeform:XINCrement?")
        x_origin = Scope.query(":WAVeform:XORigin?")
        y_increment = Scope.query(":WAVeform:YINCrement?")
        y_origin = Scope.query(":WAVeform:YORigin?")

        # Get waveform data
        Scope.write(":WAVeform:STReaming OFF")
        data_words = Scope.query(":WAVeform:DATA?")
        pattern=re.compile(r'[-+]?\d*\.\d+E[-+]?\d+')
        tt=pattern.findall(data_words)
        datas=[float(ttt) for ttt in tt]
        nLength = len(datas)
        if debug:
            print("Number of data values: %d" % nLength)

        # Save CSV
        with open(path+"\\data_"+str(Iteratinon)+"_"+str(average_times_code_temp)+".csv", "a+") as f:
            for i in range(0, nLength - 1):
                time_val = x_origin + (i * x_increment)
                voltage = (data_words[i] * y_increment) + y_origin
                f.write("%E, %f\n" % (time_val, voltage))
        if debug:
            print("Waveform format WORD data written to waveform_data.csv.")
    elif data_save_method == 2:
        Scope.write(":DISK:SAVE:WAVeform CHANnel3,\""+str(Iteratinon)+"_"+str(average_times_code_temp)+"\",CSV,ON")
    elif data_save_method == 3:
        # Get waveform data
        Scope.write(":WAVeform:STReaming OFF")
        data_words=Scope.query(":WAVeform:DATA?")
        pattern=re.compile(r'[-+]?\d*\.\d+E[-+]?\d+')
        tt=pattern.findall(data_words)
        datas=[float(ttt) for ttt in tt]
        data_words = np.array(datas, dtype=np.float32)
        if debug:
            nLength = len(data_words)
            print("Number of data values: %d" % nLength)
        Whole_data.append(data_words)

    else:
        print("ERROR: data_save_method must be 1,2,3.")
        pass
# ===================== scope 初始化以及相关函数 =====================





# ---------- 数据采集线程 ----------
def acquisition_thread(
    dead_time_sec=dead_time,       # 死区时间（秒）
    init_pulse_skip=pulse_skip,        # 初始化时丢弃的脉冲数
    sample_rate=SAMPLE_RATE,   # 采样率（Hz）
    samples_per_read=SAMPLES_PER_READ,
    threshold=THRESHOLD
):
    global init_done
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

        while True:
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
                            if debug:
                                print(f"初始化完成，跳过前 {init_pulse_skip} 个脉冲")
                    last_pulse_sample = global_index

            sample_index += samples_per_read

# ---------- 读取脉冲次数 ----------
def processing_thread():
    global position_now,scan_horizontal_back,pulse_count,scan_finished,point_index,DATA_save_path,average_times_code
    average_times_code_temp=average_times_code
    with tqdm(total=(scan_horizontal_pixel + 1) * (scan_vertical_pixel + 1),
              unit="pixel", leave=True, dynamic_ncols=True, colour="blue") as pbar:
        pbar.set_description(f"Position: [{position_now[0]},{position_now[1]}]")
        pbar.update(1)
        while True:
            try:
                t_event = pulse_queue.get(timeout=1)
            except queue.Empty:
                continue
            # ====== 这里写你的任务 ======
            # 注意：任务要尽量短，避免影响采样
            pulse_count += 1
            if pulse_count >= average_times:
                pulse_count = 0
                send_information_and_clear_scope(point_index,average_times_code_temp,DATA_save_path)
                time.sleep(0.25)
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
t_acq = threading.Thread(target=acquisition_thread, daemon=True)
t_acq.start()
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