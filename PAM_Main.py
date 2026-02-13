# PAM_Main_Controller.py
import gc
gc.collect()   # 手动大扫除
gc.disable()   # 关掉自动回收（在此期间 Python 不会暂停）
import time
import numpy as np
import scipy.io as sio # 用于保存 mat 文件
import matplotlib.pyplot as plt
import atsapi as ats
import traceback
# 扫描开始前
# 导入模块

from instruments_class.PriorUnifiedStage import PriorUnifiedStage
from instruments_class.AlazarNPTSystem import AlazarNPTSystem
from instruments_class.AsyncProgress import progress_manager

def main():
    # ============================== 1. 参数设置 =================================
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    save_path = "./data.mat"
    # 扫描参数
    SCAN_W = 10       # 像素宽
    SCAN_H = 10       # 像素高
    STEP_UM = 1       # 步长 (um)
    EXPOSURE_MS =  100   # 每个点曝光时间 (位移台参数)
    
    # DAQ 参数
    SAMPLES_REC = 4096
    RECORDS_BUF = 16   # 每个Buffer存50个激光脉冲数据 (降低主循环压力)
    RECORDS_PER_POINT = 512 # 每个点记录多少个record，在平均的情况下，也不能大于1048832，否则uint32会溢出
    Buffer_Count = 4   # 用多少个buffer来收集数据，少了CPU可能忙不过来
    AVERAGE_ENABLE = True
    
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
        daq.configure_board() 
        daq.prepare_acquisition(num_points=SCAN_W*SCAN_H+1,
                                acq_channel=ats.CHANNEL_A, 
                                samples_per_record=SAMPLES_REC,
                                records_per_buffer=RECORDS_BUF,
                                buffer_count=Buffer_Count, 
                                records_per_point=RECORDS_PER_POINT,
                                preTriggerSamples=0) # 准备 DMA
        
        # === 3. 配置扫描 ===
        # 准备位移台 (此时未动)
        stage.prepare_scan_serial(SCAN_W, SCAN_H, STEP_UM, EXPOSURE_MS, 0)
        
        # 准备数据存储 (内存 RAM)
        # 注意: 如果数据量太大(>8GB), 列表会爆内存。
        # 这里假设采集 100x100 的图像，每个位置可能有多个激光trigger
        all_data = []      # 存 DAQ 数据
        pos_mapping = []   # 存 (X,Y, Buffer_Index)
        last_pos_str = ""
        positio_point_count = 0
        input("Press Enter to START Experiment... (确保激光器已开)\n\n")
        print("Starting Main Loop...")
        curr_pos_str = stage.get_pos_fast()
        progress_manager.start(total=SCAN_W*SCAN_H, desc=f"\033[31m📍 Pos: {curr_pos_str}\033[31m")
        progress_manager.set_colour("cyan") # 扫描开始，设为青色
        # === 4. 启动同步 ===
        # A. 开启 DAQ (进入等待触发状态)
        start_t = time.time()
        daq.start_capture()

        # B. 开启 位移台 (开始发出 TTL 触发 & 移动)
        stage.start_scan_motion()

        # === 5. 主循环 (Polling Loop) ===
        while True:
            while (curr_pos_str == last_pos_str):
                curr_pos_str = stage.get_pos_fast()
                time.sleep(0.001) # 给 CPU 和串口缓冲的时间

            daq.get_one_acquisition(all_data, pos_mapping, curr_pos_str, timeout_ms=int(EXPOSURE_MS*3/4), Average_Enable=AVERAGE_ENABLE)
            

            last_pos_str = curr_pos_str        
            progress_manager.update(1)
            progress_manager.set_description(f"📍 Pos: {curr_pos_str}",color="green") # 实时显示坐标
            positio_point_count += 1
        
            if positio_point_count >= SCAN_W * SCAN_H:
                break

    except StopIteration:
        progress_manager.set_colour("red")
        print("\n🛑 StopIteration！ 程序直接停止！")
        pass
    except TimeoutError:
        progress_manager.set_colour("red") 
        print("\n❌ 采集超时！可能是激光器没开，或者位移台触发线没接好。")
    except KeyboardInterrupt:
        progress_manager.set_colour("red")
        print("\n🛑 用户强制停止！")
        
    finally:
        # === 6. 清理与保存 ===
        # 立即停止硬件采集，防止 DMA 继续向已回收的内存写入
        daq.stop_capture()
        
        # 确保进度条完全停止并刷新终端，避免 UI 干扰接下来的打印
        progress_manager.set_colour("green")
        try: progress_manager.stop()
        except: pass
        
        # 恢复垃圾回收机制并尝试切换回 SDK 模式
        import gc
        gc.enable()
        try: stage.connect_sdk() 
        except: pass

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