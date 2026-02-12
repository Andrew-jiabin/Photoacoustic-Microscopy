# PAM_Main_Controller.py
import time
import numpy as np
import scipy.io as sio # 用于保存 mat 文件
import matplotlib.pyplot as plt

 
# 导入模块
from instruments_class.PriorUnifiedStage import PriorUnifiedStage
from instruments_class.AlazarNPTSystem import AlazarNPTSystem
def main():
    # === 1. 参数设置 ===
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    
    # 扫描参数
    SCAN_W = 100       # 像素宽
    SCAN_H = 100       # 像素高
    STEP_UM = 10       # 步长 (um)
    EXPOSURE_MS = 1    # 每个点曝光/脉冲时间 (位移台参数)
    
    # DAQ 参数
    SAMPLES_REC = 4096
    RECORDS_BUF = 50   # 每个Buffer存50个激光脉冲数据 (降低主循环压力)
    
    # === 2. 初始化硬件 ===
    try:
        # 初始化位移台
        stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
        
        # 初始化采集卡
        daq = AlazarNPTSystem(systemId=1, boardId=1)
        daq.configure_board() # 80kHz 外部触发配置
        daq.prepare_acquisition(samples_per_record=SAMPLES_REC, 
                                records_per_buffer=RECORDS_BUF,
                                buffer_count=8) # 准备 DMA
        
        # === 3. 配置扫描 ===
        # 准备位移台 (此时未动)
        stage.prepare_scan_serial(SCAN_W, SCAN_H, STEP_UM, EXPOSURE_MS, 0)
        
        # 准备数据存储 (内存 RAM)
        # 注意: 如果数据量太大(>8GB), 列表会爆内存。
        # 这里假设采集 100x100 的图像，每个位置可能有多个激光trigger
        all_data = []      # 存 DAQ 数据
        pos_mapping = []   # 存 (X,Y, Buffer_Index)
        
        input("Press Enter to START Experiment... (确保激光器已开)")
        
        # === 4. 启动同步 ===
        # A. 开启 DAQ (进入等待触发状态)
        daq.start_capture()
        
        # B. 开启 位移台 (开始发出 TTL 触发 & 移动)
        stage.start_scan_motion()
        
        start_t = time.time()
        
        # === 5. 主循环 (Polling Loop) ===
        print("Starting Main Loop...")
        
        last_pos_str = ""
        total_buffers_captured = 0
        
        while True:
            # --- A. 获取 DAQ 数据 ---
            # 尝试拿一个 Buffer，timeout 设很短(2ms)，避免阻塞位置查询
            raw_data, success = daq.fetch_next_buffer(timeout_ms=2)
            
            if success:
                # 拿到了数据！
                # 记录数据 (reshape 为 records x samples)
                reshaped_data = raw_data.reshape(RECORDS_BUF, -1)
                
                # 为了节省内存，如果你只需要存 raw 数据，可以不 reshape，最后再处理
                # 这里为了演示，存入 list
                all_data.append(raw_data) 
                
                total_buffers_captured += 1
                
                # --- B. 获取当前位置 ---
                # 只有当采集到数据时，才去查位置，这样建立了 "数据->位置" 的映射
                # 或者你也可以无论有无数据都一直查位置
                curr_pos_str = stage.get_pos_fast() # 耗时约 4ms
                
                # 记录映射关系: 第N个Buffer 对应 哪个位置
                # 格式: [Buffer_Index, Position_String]
                pos_mapping.append((total_buffers_captured - 1, curr_pos_str))
                
                # --- C. 实时显示/处理 ---
                if curr_pos_str != last_pos_str:
                    # 位置变了，打印一下进度
                    print(f"\r📸 Buffers: {total_buffers_captured} | Pos: {curr_pos_str}  ", end="")
                    last_pos_str = curr_pos_str
                
            # --- D. 检查扫描是否结束 ---
            # 为了效率，不需要每次循环都查状态，可以每采集 N 个 Buffer 查一次
            if total_buffers_captured % 10 == 0:
                if not stage.is_scan_running():
                    print("\n✅ 位移台扫描完成！")
                    break

    except KeyboardInterrupt:
        print("\n🛑 用户强制停止！")
        stage.emergency_stop()
        
    finally:
        # === 6. 清理与保存 ===
        daq.stop_capture()
        
        # 确保回到 SDK 模式以便下次使用
        try: stage.connect_sdk() 
        except: pass

        duration = time.time() - start_t
        print(f"\n📊 实验结束。耗时: {duration:.2f}s")
        print(f"📦 采集总 Buffer 数: {len(all_data)}")
        
        if len(all_data) > 0:
            print("💾 正在保存数据至 data.mat ... (可能需要几秒)")
            
            # 拼接大数组 (内存警告!)
            # 假设 all_data 是 [Buffer1, Buffer2...]
            # 最终 save_data 形状: (Total_Records, Samples)
            try:
                # 将 List of Arrays 转换为大矩阵
                # 注意 uint16 占用内存较小
                big_data_matrix = np.concatenate(all_data) 
                big_data_matrix = big_data_matrix.reshape(-1, SAMPLES_REC)
                
                # 解析位置数据
                # pos_mapping 是 [(0, "0,0,0"), (1, "0,0,0")...]
                # 我们将其拆分为 buffer_idx 和 pos_str
                
                mat_dict = {
                    "raw_data": big_data_matrix,
                    "pos_map": pos_mapping, # 简单的 buffer-location 映射
                    "scan_params": [SCAN_W, SCAN_H, STEP_UM],
                    "daq_params": [SAMPLES_REC, RECORDS_BUF]
                }
                
                sio.savemat("data.mat", mat_dict, do_compression=True)
                print("✅ 文件保存成功: data.mat")
            except MemoryError:
                print("❌ 内存不足，无法拼接大数组保存！建议分块保存。")
        else:
            print("⚠️ 未采集到任何数据。")

if __name__ == "__main__":
    main()