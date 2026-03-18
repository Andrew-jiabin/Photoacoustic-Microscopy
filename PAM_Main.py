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
import datetime
from Tool_code.position_trans import sanitize_pos_to_key
from Tool_code.record_position import write_position_to_txt
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Alazar_imaging.Alazar_imaging_tools import get_expected_trajectory
def main():
    # ============================== 1. 参数设置 =================================
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    
    # 扫描参数
    SCAN_W = 50       # 像素宽,对应了向上的距离
    SCAN_H = 50       # 像素高,对应了向左的距离
    STEP_UM = 1       # 步长 (um)
    EXPOSURE_MS =  500   # 每个点曝光时间 (位移台参数)
    
    # DAQ 参数
    SAMPLES_REC = 4096
    SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
    SAMPLE_RATE_str = "4G"
    RECORDS_BUF = 64   # 每个Buffer存50个激光脉冲数据 (降低主循环压力)
    RECORDS_PER_POINT = 64 # 每个点记录多少个record，在平均的情况下，也不能大于1048832，否则uint32会溢出
    Buffer_Count = 8   # 用多少个buffer来收集数据，太少了可能双DMA会受限制
    SETTLE_MS = int(EXPOSURE_MS/10)

    AVERAGE_ENABLE = True
    save_path = f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}-W({SCAN_W})-H({SCAN_H})-"+"S_"+str(SAMPLE_RATE_str)+"-EXPOSURE_MS_"+str(EXPOSURE_MS)+".mat"
    txt_save_path = save_path.replace(".mat", "_position_log.txt")
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
                        daq.get_one_acquisition(all_data=all_data, curr_pos_str=raw_pos,
                                                 timeout_ms=int(EXPOSURE_MS*4/5), Average_Enable=AVERAGE_ENABLE)
                        verify_pos = stage.get_pos_fast()
                        if verify_pos != target_str:
                            raise ValueError("曝光速度过短，无法正确采集信号")
                        break
                    else:
                        pass
                time.sleep(SETTLE_MS/2000.)

            
                  
            progress_manager.update(1)
            progress_manager.set_description(f"📍 Pos: {raw_pos}",color="green") # 实时显示坐标
            positio_point_count += 1
        
            if positio_point_count >= SCAN_W * SCAN_H:
                break
    except ValueError as e:
        print(e)

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
            print(f"💾 正在按位点建立映射并保存至 {save_path}")
            
            mat_dict = {}
            # 预留一个辅助列表，用于在 MATLAB 中方便地按索引访问坐标
            index_to_pos = [] 

            try:
                for idx, item in enumerate(all_data):
                    # 根据你 get_one_acquisition 的改版：
                    # item[0] 是数据 (summed_data 或 buffers列表)
                    # item[1] 是原始坐标字符串 "X,Y,Z"
                    raw_data_content = item[0]
                    original_pos_str = item[1]

                    # 1. 生成合法的 MATLAB 变量名
                    safe_key = sanitize_pos_to_key(original_pos_str)
                    
                    # 2. 处理数据体
                    if AVERAGE_ENABLE:
                        # summed_data 已经是 uint32 求和结果，转换为 uint16 节省空间
                        # 注意：如果 RECORDS_PER_POINT 很大，请检查是否会溢出
                        processed_data = (raw_data_content / RECORDS_PER_POINT).astype(np.uint16)
                    else:
                        # 如果是原始 buffer 列表，进行拼接,这里还存在很多处理逻辑上的问题
                        if isinstance(raw_data_content, list):
                            processed_data = np.concatenate(raw_data_content).astype(np.uint16)
                        else:
                            processed_data = raw_data_content.astype(np.uint16)

                    # 3. 存入字典：坐标 -> 数据
                    mat_dict[safe_key] = processed_data
                    
                    # 4. 存入辅助索引映射
                    index_to_pos.append(original_pos_str)

                # 5. 添加元数据，方便后续追溯
                mat_dict["metadata"] = {
                    "scan_shape": [SCAN_W, SCAN_H],
                    "step_um": STEP_UM,
                    "pos_list": index_to_pos, # 这样你在 MATLAB 里可以用这个列表找到所有的 Key
                    "is_averaged": int(AVERAGE_ENABLE)
                }

                # 6. 最终保存
                sio.savemat(save_path, mat_dict)
                print(f"✅ 成功保存！共计 {len(mat_dict)-1} 个坐标位点数据。")

            except Exception as e:
                print(f"❌ 数据封装失败: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("⚠️ 未采集到任何有效数据，跳过保存。")

if __name__ == "__main__":
    main()