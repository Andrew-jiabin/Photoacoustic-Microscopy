# PAM_Main_Controller.py
import time
import numpy as np
import scipy.io as sio # 用于保存 mat 文件
import matplotlib.pyplot as plt
import atsapi as ats
 
# 导入模块
from instruments_class.PriorUnifiedStage import PriorUnifiedStage
from instruments_class.AlazarNPTSystem import AlazarNPTSystem
from instruments_class.shared_progress import progress_manager
def main():
    # === 1. 参数设置 ===
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    save_path = "./data.mat"
    # 扫描参数
    SCAN_W = 20       # 像素宽
    SCAN_H = 20       # 像素高
    STEP_UM = 1       # 步长 (um)
    EXPOSURE_MS = 20    # 每个点曝光 (位移台参数)
    
    # DAQ 参数
    SAMPLES_REC = 4096
    RECORDS_BUF = 16   # 每个Buffer存50个激光脉冲数据 (降低主循环压力)
    RECORDS_PER_POINT = 1024 # 每个点记录多少个record
    Buffer_Count = 4   # 用多少个buffer来收集数据，少了CPU可能忙不过来
    progress_manager.start(total=SCAN_W*SCAN_H, desc="PAM Scan")

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
        temp_data=[]       # 暂存一次DAQ的数据
        last_pos_str = ""
        positio_point_count = 0
        input("Press Enter to START Experiment... (确保激光器已开)")
        
        # === 4. 启动同步 ===
        # A. 开启 DAQ (进入等待触发状态)
        start_t = time.time()
        print("Starting Main Loop...")
        daq.start_capture()
        # B. 开启 位移台 (开始发出 TTL 触发 & 移动)
        curr_pos_str = stage.get_pos_fast()
        stage.start_scan_motion()
        # === 5. 主循环 (Polling Loop) ===

        while True:
            while (curr_pos_str == last_pos_str):
                curr_pos_str = stage.get_pos_fast()

            daq.get_one_acquisition(all_data, pos_mapping, curr_pos_str, timeout_ms=int(EXPOSURE_MS*3/4))

            last_pos_str = curr_pos_str        
            progress_manager.update(1)
            positio_point_count += 1
        
            if positio_point_count >= SCAN_W * SCAN_H:
                print("\n✅ 所有预定点位采集完成！")
                break

    except StopIteration:
        pass
    except ats.ApiWaitTimeout:
        print("\n❌ 采集超时！可能是激光器没开，或者位移台触发线没接好。")
    except KeyboardInterrupt:
        print("\n🛑 用户强制停止！")
        
    finally:
        # === 6. 清理与保存 ===
        daq.stop_capture()
        try: stage.connect_sdk() 
        except: pass

        duration = time.time() - start_t
        
        # --- 新版解析逻辑 ---
        # 假设：
        # N_POINTS = len(all_data)
        # BUFS_PER_POINT = len(all_data[0]) if N_POINTS > 0 else 0
        # RECORDS_PER_BUF = daq.recordsPerBuffer
        # SAMPLES_PER_REC = daq.postTriggerSamples

        print(f"\n📊 实验耗时: {duration:.2f}s")
        print(f"📦 采集点数: {len(all_data)}")

        if len(all_data) > 0:
            print(f"💾 正在解析并保存数据至 {save_path} ... ")
            try:
                # 逻辑：对每个字符串按逗号分割，转为 float
                pos_numeric = np.array([[float(v) for v in s.split(',')] for s in pos_mapping])
            except Exception as e:
                print(f"⚠️ 坐标解析失败，可能存在非标格式: {e}")
                pos_numeric = np.array(pos_mapping) # 降级方案：存原始字符串

            try:
                # 1. 展平嵌套列表：从 [ [buf1, buf2], [buf3, buf4] ] 变成 [buf1, buf2, buf3, buf4]
                flattened_buffers = [buf for point_bufs in all_data for buf in point_bufs]
                
                # 2. 拼接为大矩阵 (shape: 总Buffer数 * 每个Buffer的采样点数)
                # 使用 np.vstack 比 np.concatenate 在处理 1D 数组时更稳健
                raw_matrix = np.vstack(flattened_buffers) 
                
                # 3. 重新塑形为四维或三维张量
                # 建议形状: (点数, 每个点的记录总数, 每个记录的采样点数)
                # 总记录数 = N_POINTS * (BUFS_PER_POINT * RECORDS_PER_BUF)
                final_data = raw_matrix.reshape(len(all_data), -1, SAMPLES_REC)
                
                # 4. 封装字典
                mat_dict = {
                    "raw_data": final_data,            # 维度: (Point, Record, Sample)
                    "pos_map": pos_numeric,  # 对应的坐标字符串列表
                    "scan_params": {
                        "width": SCAN_W,
                        "height": SCAN_H,
                        "step": STEP_UM
                    },
                    "daq_params": {
                        "samples_per_record": SAMPLES_REC,
                        "records_per_buffer": RECORDS_BUF,
                        "buffers_per_point": len(all_data[0])
                    }
                }
                
                # 5. 保存 (针对 PhD 大数据量，开启压缩)
                sio.savemat(save_path, mat_dict, do_compression=True)
                print(f"✅ 成功保存！矩阵维度: {final_data.shape}")

            except MemoryError:
                print("❌ 内存爆炸！建议降低每个点的 Buffer 数量或分块保存。")
        else:
            print("⚠️ 未采集到任何数据。")

if __name__ == "__main__":
    main()