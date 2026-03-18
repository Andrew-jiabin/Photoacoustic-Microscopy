import gc
import time
import datetime
import numpy as np
import scipy.io as sio
import matplotlib.pyplot as plt
import atsapi as ats
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Tool_code.position_trans import sanitize_pos_to_key

def main():
    # ============================== 1. 参数设置 =================================
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    SCAN_W, SCAN_H = 40, 40
    STEP_UM = 1
    SETTLE_MS = 100
    buffer_count = 1024
    # DAQ 参数
    SAMPLES_REC = 4096
    SAMPLE_RATE_HZ = 4e9 # 4 GSPS
    SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
    AVERAGE_ENABLE = True
    RECORDS_PER_POINT = 64 
    save_path = f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mat"

    # === 2. 初始化硬件 ===
    stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
    daq = AlazarNPTSystem(systemId=1, boardId=1)
    daq.configure_board(sample_rate=SAMPLE_RATE)
    
    # 注意：这里去掉了 num_points 参数
    daq.prepare_acquisition(acq_channel=ats.CHANNEL_A, 
                            samples_per_record=SAMPLES_REC, 
                            records_per_buffer=RECORDS_PER_POINT, 
                            buffer_count=4,  # 对于单点停顿采集，4个buffer游刃有余，不用1024
                            records_per_point=RECORDS_PER_POINT)

    # --- 实时绘图环境初始化 ---
    plt.ion()
    # 创建复合布局：左侧大图展示MAP，右侧两张小图展示波形和频谱
    fig = plt.figure(figsize=(14, 8))
    gs = fig.add_gridspec(2, 2)
    ax_map = fig.add_subplot(gs[:, 0])
    ax_time = fig.add_subplot(gs[0, 1])
    ax_freq = fig.add_subplot(gs[1, 1])

    # A. 2D MAP 初始化
    recon_img = np.zeros((SCAN_H, SCAN_W))
    im_display = ax_map.imshow(recon_img, cmap='hot', extent=[0, SCAN_W*STEP_UM, SCAN_H*STEP_UM, 0])
    plt.colorbar(im_display, ax=ax_map, label='P-P Intensity')
    ax_map.set_title("Real-time MAP Reconstruction")

    # B. 时域波形初始化
    time_axis = np.arange(SAMPLES_REC) / SAMPLE_RATE_HZ * 1e6 # 单位: us
    line_time, = ax_time.plot(time_axis, np.zeros(SAMPLES_REC), color='cyan', lw=1)
    ax_time.set_title("Latest Waveform")
    ax_time.set_xlabel("Time (us)")
    ax_time.set_ylabel("Amplitude")
    ax_time.grid(True)

    # C. 频域频谱初始化 (0-1 GHz)
    freqs = np.fft.rfftfreq(SAMPLES_REC, d=1/SAMPLE_RATE_HZ) / 1e9 # 单位: GHz
    idx_1ghz = np.where(freqs <= 1.0)[0][-1] # 找到 1GHz 对应的索引
    line_freq, = ax_freq.plot(freqs[:idx_1ghz], np.zeros(idx_1ghz), color='magenta', lw=1)
    ax_freq.set_title("Log-Power Spectrum (0-1 GHz)")
    ax_freq.set_xlabel("Frequency (GHz)")
    ax_freq.set_ylabel("Log Mag (dB)")
    ax_freq.set_ylim([-20, 100]) # 根据信号强度预设 dB 范围
    ax_freq.grid(True)

    fig.tight_layout()
    plt.show()

    # === 3. 生成轨迹 ===
    # === 3. 生成轨迹 (光栅型扫描) ===
    raw_pos = stage.get_position()
    START_X, START_Y = [float(v) for v in raw_pos.split(',')[:2]]
    trajectory = []

    for h in range(SCAN_H):
        # 每一行都从 0 到 SCAN_W，不再根据奇偶行翻转
        w_range = range(SCAN_W) 
        for w in w_range:
            target_x = START_X + (w * STEP_UM)
            target_y = START_Y + (h * STEP_UM)
            trajectory.append((target_x, target_y, w, h))
    # === 4. 开始实验 ===
    all_data = []
    gc.disable()
    input("Press Enter to START Experiment...")
    
    try:
        progress_manager.start(total=len(trajectory), desc="🚀 PAM Scanning")

        for i, (tx, ty, curr_w, curr_h) in enumerate(trajectory):
            stage.set_position([tx, ty])
            stage.wait_until_settled(tx, ty, settle_time_ms=SETTLE_MS)
            
            current_pos_str = f"{tx},{ty},0"
            daq.get_one_acquisition(all_data=all_data, curr_pos_str=current_pos_str, 
                                     timeout_ms=500, Average_Enable=AVERAGE_ENABLE)
            
            if len(all_data) > 0:
                latest_waveform = all_data[-1][0].astype(np.float32)
                
                # 1. 更新 MAP 数据
                val = np.ptp(latest_waveform)
                recon_img[curr_h, curr_w] = val

                # 2. 实时刷新绘图 (为了提高效率，可以每 N 个像素更新一次，这里设为 1)
                if i % 1 == 0:
                    # 更新 2D 图
                    im_display.set_data(recon_img)
                    im_display.set_clim(vmin=0, vmax=np.max(recon_img) + 1)
                    
                    # 更新时域图
                    line_time.set_ydata(latest_waveform)
                    ax_time.set_ylim([np.min(latest_waveform)-100, np.max(latest_waveform)+100])
                    
                    # 更新频域图 (FFT -> Log Magnitude)
                    fft_mag = np.abs(np.fft.rfft(latest_waveform))
                    log_spec = 20 * np.log10(fft_mag + 1e-6) # 转换为 dB 尺度
                    line_freq.set_ydata(log_spec[:idx_1ghz])
                    
                    plt.pause(0.001) 
            
            progress_manager.update(1)
            
    except Exception as e:
        print(f"\n❌ 错误: {e}")
    finally:
        # 清理与保存逻辑保持不变...
        stage.set_position([START_X, START_Y])
        plt.ioff()
        plt.savefig(save_path.replace(".mat", ".png"))
        save_mat_data(all_data, SCAN_W, SCAN_H, STEP_UM, AVERAGE_ENABLE, RECORDS_PER_POINT, save_path)

def save_mat_data(all_data, SCAN_W, SCAN_H, STEP_UM, AVERAGE_ENABLE, RECORDS_PER_POINT, save_path):
    # 此处封装你之前的保存逻辑，确保数据不丢失
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
    mat_dict["metadata"] = {"scan_shape": [SCAN_W, SCAN_H], "step_um": STEP_UM, "pos_list": index_to_pos}
    sio.savemat(save_path, mat_dict)

if __name__ == "__main__":
    main()
