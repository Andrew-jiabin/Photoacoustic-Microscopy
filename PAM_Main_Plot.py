# PAM_Main_Controller.py
import gc
gc.collect()   # 手动大扫除
gc.disable()   # 关掉自动回收（在此期间 Python 不会暂停）
import time
import os
import numpy as np
import scipy.io as sio # 用于保存 mat 文件
import matplotlib.pyplot as plt
import atsapi as ats
import traceback
# 扫描开始前
# 导入模块
import datetime
from Tool_code.record_position import write_position_to_txt
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Alazar_imaging.Alazar_imaging_tools import get_expected_trajectory

def plot_and_save_realtime(raw_signal_1d, pos_str, fs, save_dir):
    """
    实时绘制单点原始信号与 dB 频谱并保存为 PNG
    """
    # --------------------- 1. 数据预处理 ---------------------
    x_raw = raw_signal_1d.astype(np.float64)
    
    # 归一化 (对应 MATLAB: (x - 32768) / 65536)
    x = (x_raw - 32768) / 65536
    
    # 时间轴 (微秒)
    n = len(x)
    t = np.arange(n) / fs
    t_us = t * 1e6
    
    # --------------------- 2. 频谱计算 ---------------------
    # 计算 FFT
    freqs = np.fft.rfftfreq(n, d=1/fs)
    freq_mhz = freqs * 1e-6
    
    # dB 幅度 (增加 epsilon 防止 log10(0))
    amp_orig = np.abs(np.fft.rfft(x)) / n
    db_orig = 20 * np.log10(amp_orig + 1e-15)
    
    # --------------------- 3. 绘图 ---------------------
    # 只绘制 1行2列：左边时域，右边频谱
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), constrained_layout=True)
    fig.patch.set_facecolor('white')

    # 左图：时域信号
    axes[0].plot(t_us, x)
    axes[0].set_title(f'Original Time Domain [{pos_str}]')
    axes[0].set_xlabel('Time (μs)')
    axes[0].set_ylabel('Amplitude')
    axes[0].grid(True)

    # 右图：dB 频谱
    axes[1].plot(freq_mhz, db_orig)
    axes[1].set_title(f'Original Spectrum (dB) [{pos_str}]')
    axes[1].set_xlabel('Frequency (MHz)')
    axes[1].set_ylabel('Amplitude (dB)')
    axes[1].set_xlim(0, 1000)   # 限制 1GHz
    axes[1].set_ylim(-160, -80) # 对应 MATLAB ylim
    axes[1].grid(True)

    # 保存图片
    # 将坐标字符串中的逗号替换为下划线，作为合法文件名
    safe_pos_str = pos_str.replace(',', '_').replace(' ', '')
    filename = os.path.join(save_dir, f"signal_pos_{safe_pos_str}.png")
    
    plt.savefig(filename, dpi=100) # 降低 dpi 加快实时保存速度
    plt.close(fig) # 务必 close，防止内存泄漏


def main():
    # ============================== 1. 参数设置 =================================
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    
    # 扫描参数
    SCAN_W = 40       # 像素宽
    SCAN_H = 40       # 像素高
    STEP_UM = 1       # 步长 (um)
    EXPOSURE_MS =  2000   # 每个点曝光时间 (位移台参数)
    
    # DAQ 参数
    SAMPLES_REC = 4096
    SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
    # 根据 atsapi 对应实际采样率数值 (4GHz)
    FS_HZ = 4e9 
    SAMPLE_RATE_str = "4G"
    RECORDS_BUF = 128   # 每个Buffer存50个激光脉冲数据 (降低主循环压力)
    RECORDS_PER_POINT = 128 # 每个点记录多少个record，在平均的情况下，也不能大于1048832，否则uint32会溢出
    Buffer_Count = 16   # 用多少个buffer来收集数据，太少了可能双DMA会受限制
    SETTLE_MS = int(EXPOSURE_MS/10)

    AVERAGE_ENABLE = True
    
    # 路径与文件夹创建
    time_str = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    base_name = f"{time_str}-W({SCAN_W})-H({SCAN_H})-S_{SAMPLE_RATE_str}-EXPOSURE_MS_{EXPOSURE_MS}"
    save_path = f"./data/{base_name}.mat"
    txt_save_path = save_path.replace(".mat", "_position_log.txt")
    
    # 为实时图片创建专属文件夹
    img_save_dir = f"./{base_name}_images"
    os.makedirs(img_save_dir, exist_ok=True)
    
    # 数据量计算与内存使用分析：
    # 1. 基础扫描范围数据量：
    # 20 × 20 (点) × 1024 (Rec/点) × 4096 (Sample/Rec) × 2 (Bytes) ≈ 3.3 GB 
    # 该数据量对于16GB内存是安全的。

    # 2. 扩大扫描范围后的风险：
    # 若扫描范围扩大到 100 × 100，数据量将达到 83 GB，程序会直接崩溃。

    # 3. 优化建议：
    # 如果未来需要做大图扫描，必须在 get_one_acquisition 函数中做实时平均（Averaging），
    # 将 1024 次数据平均成 1 次，可使数据量缩小 1024 倍。
    # ============================== 1. 参数设置 =================================

    

    # === 2. 初始化硬件 ===
    try:
        # 初始化位移台 & 采集卡
        stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
        daq = AlazarNPTSystem(systemId=1, boardId=1)
        daq.configure_board(sample_rate=SAMPLE_RATE) 
        daq.prepare_acquisition(num_points=SCAN_W*SCAN_H,
                                acq_channel=ats.CHANNEL_A, 
                                samples_per_record=SAMPLES_REC,
                                records_per_buffer=RECORDS_BUF,
                                buffer_count=Buffer_Count, 
                                records_per_point=RECORDS_PER_POINT,
                                preTriggerSamples=0) # 准备 DMA
        
        # === 3. 配置扫描 ===
        # 准备位移台 (此时未动)
        stage.prepare_scan_serial(width_px=SCAN_W, height_px=SCAN_H,
                                step_um=STEP_UM, exposure_ms=EXPOSURE_MS,
                                settle_ms=SETTLE_MS, ttl_pin=0)
        
        # 准备数据存储 (内存 RAM)
        # 注意: 如果数据量太大(>8GB), 列表会爆内存。
        # 这里假设采集 100x100 的图像，每个位置可能有多个激光trigger
        all_data = []      # 存 DAQ 数据
        pos_mapping = []   # 存 (X,Y, Buffer_Index)
        positio_point_count = 0
        input("Press Enter to START Experiment... (确保激光器已开)\n\n")
        print("Starting Main Loop...")
        raw_pos = stage.get_pos_fast()

        START_X, START_Y, _= [int(v) for v in raw_pos.split(',')]
        expected_trajectory_str = get_expected_trajectory(SCAN_W, SCAN_H, STEP_UM, START_X, START_Y)

        progress_manager.start(total=SCAN_W*SCAN_H, desc=f"\033[31m📍 Pos: {raw_pos}\033[31m")
        progress_manager.set_colour("cyan") # 扫描开始，设为青色
        # === 4. 启动同步 ===
        # A. 开启 DAQ (进入等待触发状态)
        start_t = time.time()
        daq.start_capture()

        # B. 开启 位移台 (开始发出 TTL 触发 & 移动)
        stage.start_scan_motion()

        # === 5. 主循环 (Polling Loop) ===
        for target_str in expected_trajectory_str:
            # print(expected_trajectory_str)
            while True:
                # 1. 快速查询并去空格
                raw_pos = stage.get_pos_fast()
                # write_position_to_txt(txt_save_path, raw_pos, 0)
                # 2. 第一重判定：是否到达目标字符串
                if raw_pos == target_str:
                    
                    # 3. 停稳等待 (settle_ms)
                    time.sleep(SETTLE_MS/1000.)
                    
                    # 4. 第二重确认：再次读取，如果还是 target_str，说明真的稳了
                    verify_pos = stage.get_pos_fast()
                    if verify_pos == target_str:
                        break
                    else:
                        pass
                time.sleep(SETTLE_MS/2000.)

            daq.get_one_acquisition(all_data, pos_mapping, raw_pos, timeout_ms=int(EXPOSURE_MS*4/5), Average_Enable=AVERAGE_ENABLE)
                  
            # ================= 实时提取数据并绘图 =================
            try:
                # 取出当前位点最新的缓冲列表
                latest_buffers = all_data[-1] 
                if len(latest_buffers) > 0:
                    # 获取第一块数据 (1D array)
                    current_raw = latest_buffers[0]
                    
                    # 只有一条 Record 用来画图就够了，截取 SAMPLES_REC 长度
                    plot_raw = current_raw[:SAMPLES_REC] 
                    
                    if AVERAGE_ENABLE:
                        # 如果是累加的 uint32 数据，需要做除法并转回 uint16
                        plot_raw = (plot_raw / RECORDS_PER_POINT).astype(np.uint16)
                    
                    # 调用实时绘图函数
                    plot_and_save_realtime(plot_raw, raw_pos, FS_HZ, img_save_dir)
            except Exception as e:
                print(f"\n[WARNING] 实时绘图异常，但采集继续: {e}")
            # ======================================================

            progress_manager.update(1)
            progress_manager.set_description(f"📍 Pos: {raw_pos}",color="green") # 实时显示坐标
            positio_point_count += 1
        
            if positio_point_count >= SCAN_W * SCAN_H:
                break


    except StopIteration:
        import traceback
        progress_manager.set_colour("red")
        print(traceback.format_exc())
        print("\n🛑 StopIteration！ 程序直接停止！")
        pass
    except TimeoutError:
        import traceback
        progress_manager.set_colour("red") 
        print(traceback.format_exc())
        print("\n❌ 采集超时！可能是激光器没开，或者位移台触发线没接好。")
    except KeyboardInterrupt:
        import traceback
        progress_manager.set_colour("red")
        print(traceback.format_exc())
        print("\n🛑 用户强制停止！")
        
    finally:
        # === 6. 清理与保存 ===
        # 1. 停止采集卡（增加ApiWaitTimeout容错）
        try:
            daq.stop_capture()
        except Exception as e:
            print(f"[WARNING] 停止采集卡失败: {e}")

        # 2. 停止进度条
        try:
            progress_manager.set_colour("green")
            progress_manager.stop()
        except Exception as e:
            print(f"[WARNING] 停止进度条失败: {e}")

        # 3. 恢复垃圾回收
        try:
            gc.enable()
        except:
            pass

        # 4. 位移台清理（修复stop_scan不存在）
        try:
            # 替换为位移台实际的停止方法，若无则删除这行
            # stage.stop_motion()  # 示例：如果有该方法则保留
            stage.connect_sdk()
        except AttributeError as e:
            print(f"[INFO] 位移台清理: {e}（无需处理）")
        except Exception as e:
            print(f"[WARNING] 位移台清理失败: {e}")

        # 后续的耗时统计、数据保存逻辑...（保留原代码）

        duration = time.time() - start_t
        print(f"\n📊 实验耗时: {duration:.2f}s")
        print(f"📦 采集点数: {len(all_data)}")

        if len(all_data) > 0:
            print(f"💾 正在解析并保存数据至 {save_path} ... ")
            
            # --- 坐标解析 (数值化) ---
            try:
                # 将坐标字符串解析为 (N, 3) 的 float64 矩阵，方便 MATLAB 直接处理
                pos_numeric = np.array([[float(v) for v in s.split(',')] for s in pos_mapping])
            except Exception as e:
                print(f"⚠️ 坐标解析失败: {e}")
                pos_numeric = np.array(pos_mapping) 

            # --- 数据重塑与平均逻辑 ---
            try:
                # 1. 展平嵌套列表
                # 如果开启了 Average_Enable，每个子列表里现在只有 1 个 summed_data 数组
                flattened_buffers = [buf for point_bufs in all_data for buf in point_bufs]
                
                # 2. 垂直堆叠为大矩阵 (Point, Samples)
                raw_matrix = np.vstack(flattened_buffers) 
                
                if AVERAGE_ENABLE:
                    # 计算公式: Final_Data = sum(Records) / RECORDS_ACQ
                    # 此时 raw_matrix 的 dtype 是 uint32，除法会自动处理精度
                    final_data = (raw_matrix / RECORDS_PER_POINT).astype(np.uint16)
                    # 重新塑形为 (点数, 1, 采样点数) 以符合你的 3D 维度要求
                    final_data = final_data.reshape(len(all_data), 1, SAMPLES_REC)
                else:
                    # 原始非平均模式
                    final_data = raw_matrix.reshape(len(all_data), -1, SAMPLES_REC)
                
                # 4. 封装字典
                mat_dict = {
                    "raw_data": final_data,
                    "pos_map": pos_numeric,
                    "scan_params": {
                        "width": SCAN_W,
                        "height": SCAN_H,
                        "step": STEP_UM
                    },
                    "daq_params": {
                        "samples_per_record": SAMPLES_REC,
                        "records_per_point": RECORDS_PER_POINT,
                        "is_averaged": int(AVERAGE_ENABLE)
                    }
                }
                
                # 5. 保存文件 (如果不追求文件大小，do_compression=False 可以让保存瞬间完成)
                sio.savemat(save_path, mat_dict, do_compression=True)
                print(f"✅ 成功保存！最终矩阵维度: {final_data.shape}")

            except MemoryError:
                print("❌ 内存爆炸！可能是由于 raw_matrix 展平时申请了过大的连续空间。")
            except Exception as e:
                import traceback
                print(f"❌ 数据处理发生意外错误:\n{traceback.format_exc()}")
        else:
            print("⚠️ 未采集到任何有效数据，跳过保存。")

if __name__ == "__main__":
    main()