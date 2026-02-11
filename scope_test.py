import atsapi as alazar
import numpy as np
import matplotlib.pyplot as plt
import time
import ctypes

# ================= 配置参数 =================
SYSTEM_ID = 1
BOARD_ID = 1

# 光声成像通常参数
SAMPLE_RATE = alazar.SAMPLE_RATE_2000MSPS  # 2 GS/s
RECORD_LENGTH = 2048         # A-line 长度 (Samples)
FFT_LENGTH = 2048            # FFT 点数 (必须是2的幂)
TRIGGER_DELAY = 0            # 触发延迟

# 触发设置 (假设使用激光器的同步信号作为触发)
TRIGGER_SOURCE = alazar.TRIG_EXTERNAL
TRIGGER_LEVEL_VOLTS = 1.0    # 1V 触发电平
TRIGGER_RANGE = alazar.ETR_2V5 # 外部触发量程

# ===========================================

def configure_board(board):
    """配置板卡基础参数：时钟、触发、通道"""
    # 1. 设置时钟 (使用内部时钟测试，实际实验建议用 EXTERNAL_CLOCK_10MHz_REF 同步)
    board.setCaptureClock(alazar.INTERNAL_CLOCK, SAMPLE_RATE, alazar.CLOCK_EDGE_RISING, 0)
    
    # 2. 设置通道 A 输入 (光声信号通常在 CH A)
    board.inputControl(alazar.CHANNEL_A, alazar.DC_COUPLING, alazar.INPUT_RANGE_PM_400_MV, alazar.IMPEDANCE_50_OHM)
    board.setBWLimit(alazar.CHANNEL_A, 0) # 0 = 禁用带宽限制 (全带宽)

    # 3. 设置触发 (外部触发模式)
    # 计算触发电平代码: Level = 128 + 127 * (Volts / Range)
    trig_level_code = int(128 + 127 * (TRIGGER_LEVEL_VOLTS / 2.5))
    
    board.setExternalTrigger(alazar.DC_COUPLING, TRIGGER_RANGE)
    board.setTriggerOperation(alazar.TRIG_ENGINE_OP_J,
                              alazar.TRIG_ENGINE_J, TRIGGER_SOURCE, alazar.TRIGGER_SLOPE_POSITIVE, trig_level_code,
                              alazar.TRIG_ENGINE_K, alazar.TRIG_DISABLE, alazar.TRIGGER_SLOPE_POSITIVE, 128)
    
    # 4. 设置触发延迟
    board.setTriggerDelay(TRIGGER_DELAY)

def configure_dsp_fft(board, record_length, fft_length):
    """配置板载 DSP 进行 FFT + Raw Data 输出"""
    
    # 1. 获取 DSP 模块列表
    dsp_modules = board.dspGetModules()
    if not dsp_modules:
        raise Exception("未检测到 DSP 模块！请确认该板卡支持板载 FFT。")
    
    dsp = dsp_modules[0] # 通常使用第一个 DSP 模块
    print(f"DSP 模块已加载: ID={dsp.dspGetInfo()[0]}")

    # 2. 生成窗函数 (Hanning Window 适合非周期信号)
    # 注意：atsapi.py 中 dspGenerateWindowFunction 是全局函数
    window = alazar.dspGenerateWindowFunction(alazar.DSP_WINDOW_HANNING, record_length, 0)
    
    # 将窗函数上传到板卡 (实部和虚部，这里只设实部，虚部为0)
    # 创建全0的虚部数组
    zeros = np.zeros(record_length, dtype=np.float32)
    dsp.fftSetWindowFunction(record_length, window.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), 
                             zeros.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))

    # 3. 设置 FFT 模式
    # 关键点：使用 RAW_PLUS_FFT 标志位
    # 输出格式：U16_LOG (对数幅度，适合显示 DB)，同时包含原始数据
    output_format = alazar.FFT_OUTPUT_FORMAT_U16_LOG | alazar.FFT_OUTPUT_FORMAT_RAW_PLUS_FFT
    
    # Footer 设置 (这里设为 None 以简化数据解析)
    footer = alazar.FFT_FOOTER_NONE
    
    # Setup 并获取"输出记录"的字节大小
    # 通道掩码 1 = Channel A
    bytes_per_out_record = dsp.fftSetup(1, record_length, fft_length, output_format, footer, 0)
    
    print(f"DSP 配置完成。单次输出记录大小: {bytes_per_out_record} Bytes")
    return dsp, bytes_per_out_record

def acquire_4g_shot(board, bytes_per_record):
    # 定义无限记录常量
    ADMA_RECORDS_INFINITE = 0x7FFFFFFF
    
    buffer = alazar.DMABuffer(board.handle, alazar.c_uint8, bytes_per_record)
    
    # ADMA_DSP (16384) | ADMA_NPT (512) | ADMA_ALLOC_BUFFERS (32) | ADMA_EXTERNAL_STARTCAPTURE (1) = 16929
    flags = alazar.ADMA_DSP | alazar.ADMA_NPT | alazar.ADMA_EXTERNAL_STARTCAPTURE | alazar.ADMA_ALLOC_BUFFERS
    
    # 修改点：将倒数第二个参数从 0 改为 0x7FFFFFFF
    board.beforeAsyncRead(alazar.CHANNEL_A, 0, RECORD_LENGTH, 
                          1, ADMA_RECORDS_INFINITE, flags) 
    
    print(f"等待触发 (4G模式, 长度 {RECORD_LENGTH})...", end="", flush=True)
    board.startCapture()
    
    try:
        # 这里会阻塞直到激光器触发并完成数据传输
        board.dspGetNextBuffer(buffer.addr, bytes_per_record, 2000)
        print(" 采集成功!")
    except Exception as e:
        print(f"\n❌ 采集超时或失败: {e}")
        return None
    finally:
        # 因为设置了无限采集，拿到数据后必须手动停止板卡
        board.abortCapture()
    
    return buffer

def parse_and_plot(buffer_obj, record_len, fft_len, bytes_total):
    """解析混合数据并绘图"""
    
    # 将 DMA 缓冲区转为 numpy 数组 (uint16)
    # 因为 ATS9373 数据是 12/14 bit 也是按 16bit 存储的
    # buffer_obj.buffer 是 uint8 类型的视图，我们需要将其 cast 成 uint16
    raw_bytes = np.frombuffer(buffer_obj.buffer, dtype=np.uint8)
    data_u16 = raw_bytes.view(np.uint16)
    
    # --- 数据拆分逻辑 ---
    # 在 RAW_PLUS_FFT 模式下，数据布局通常是：[时域数据 (RecordLen)] + [频域数据 (FFTLen/2)]
    # 注意：FFT 输出如果是 U16_LOG 格式，长度通常是 FFT_LENGTH / 2 (即奈奎斯特频率内)
    
    # 1. 提取时域信号
    time_domain_data = data_u16[0 : record_len]
    
    # 2. 提取频域信号
    # 时域数据之后就是频域数据
    freq_domain_start_idx = record_len
    # FFT 有效点数通常是 FFT_LENGTH / 2
    valid_fft_points = fft_len // 2
    freq_domain_data = data_u16[freq_domain_start_idx : freq_domain_start_idx + valid_fft_points]
    
    # --- 转换电压单位 (假设 400mV 量程) ---
    # ATS9373 是 12-bit，数据左对齐到 16-bit，或者右对齐？
    # 通常 Alazar 原始数据是无符号的。
    # 简单的归一化显示：
    code_zero = 32768 # 中点
    volt_scale = 0.4 / 32768.0 
    time_volts = (time_domain_data.astype(float) - code_zero) * volt_scale
    
    # --- 绘图 ---
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))
    
    # 时域图
    t_axis = np.arange(record_len)
    ax1.plot(t_axis, time_volts, 'b-')
    ax1.set_title(f"Time Domain (CH A) - Record: {record_len} pts")
    ax1.set_ylabel("Amplitude (V)")
    ax1.set_xlabel("Sample Index")
    ax1.grid(True)
    
    # 频域图
    # 简单的频率轴生成 (0 到 Nyquist)
    f_axis = np.linspace(0, SAMPLE_RATE_HZ/2/1e6, len(freq_domain_data)) # MHz
    ax2.plot(f_axis, freq_domain_data, 'r-')
    ax2.set_title("On-Board FFT (Log Magnitude)")
    ax2.set_ylabel("Log Magnitude (Arbitrary Units)")
    ax2.set_xlabel("Frequency (MHz)")
    ax2.grid(True)
    
    plt.tight_layout()
    plt.show()

# SAMPLE_RATE 常量对应的数值 (用于画图)
# 0x3F = 2000 MSPS
SAMPLE_RATE_HZ = 2000e6 

def main():
    # 1. 初始化
    board = alazar.Board(SYSTEM_ID, BOARD_ID)
    print("已连接板卡")
    
    # 2. 配置
    configure_board(board)
    dsp_handle, bytes_per_record = configure_dsp_fft(board, RECORD_LENGTH, FFT_LENGTH)
    
    # 3. 采集 (循环或单次，这里演示单次)
    print("\n--- 开始光声示波器采集 ---")
    print("请触发你的激光器/信号源...")
    
    dma_buffer = acquire_4g_shot(board, bytes_per_record)
    
    if dma_buffer:
        # 4. 绘图
        parse_and_plot(dma_buffer, RECORD_LENGTH, FFT_LENGTH, bytes_per_record)
    
    print("程序结束")

if __name__ == "__main__":
    main()