import gc
import time
from Tool_code.data_saving import save_experiment_data
import datetime
import atsapi as ats
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Tool_code.position_trans import sanitize_pos_to_key
import numpy as np
import scipy.io as sio # 用于保存 mat 文件
def main():
    # ============================== 1. 参数设置 =================================
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"
    
    # 扫描参数 (数值格式，方便计算)
    SCAN_W = 150    # 像素宽
    SCAN_H = 100     # 像素高
    STEP_UM = 1        # 步长
    SETTLE_MS = 50     # 到位后的物理稳定时间 (根据位移台震动调整)
    
    # DAQ 参数 (Alazar)
    SAMPLES_REC = 4096
    SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
    RECORDS_PER_POINT = 64 
    AVERAGE_ENABLE = True
    SAMPLE_RATE_str = "4G"
    RECORDS_BUF = 64 
    RECORDS_PER_POINT = 64 # 每个点记录多少个record，在平均的情况下，也不能大于1048832，否则uint32会溢出
    Buffer_Count = 4   # 对于单点停顿采集，4个buffer游刃有余，不用1024
    save_path = f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mat"

    # === 2. 初始化硬件 ===
    stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
    daq = AlazarNPTSystem(systemId=1, boardId=1)
    daq.configure_board(sample_rate=SAMPLE_RATE)
    
    # 注意：这里去掉了 num_points 参数
    daq.prepare_acquisition(acq_channel=ats.CHANNEL_A, 
                            samples_per_record=SAMPLES_REC, 
                            records_per_buffer=RECORDS_PER_POINT, 
                            buffer_count=Buffer_Count,  
                            records_per_point=RECORDS_PER_POINT)

    gc.disable()
    input("Press Enter to START Experiment... (确保激光器已开)")


    # === 3. 生成扫描轨迹 (蛇形扫描逻辑) ===
    # 获取初始位置作为 0,0 点
    raw_pos = stage.get_position()
    START_X, START_Y = [float(v) for v in raw_pos.split(',')[:2]]
    
    trajectory = []
    for h in range(SCAN_H):
        w_range = range(SCAN_W) if h % 2 == 0 else reversed(range(SCAN_W))
        for w in w_range:
            target_x = START_X + (w * STEP_UM)
            target_y = START_Y + (h * STEP_UM)
            trajectory.append((target_x, target_y, 0))

    # === 4. 开始实验 ===
    all_data = []
    pos_mapping = []
    
    try:
        progress_manager.start(total=len(trajectory), desc="🚀 PAM Scanning")
        start_time = time.time()

        for i, (tx, ty, _) in enumerate(trajectory):

            # A. 指令位移台移动
            stage.set_position([tx, ty])
            
            # B. 核心握手：等待物理到位
            stage.wait_until_settled(tx, ty, settle_time_ms=SETTLE_MS)
            current_pos_str = f"{tx},{ty},0"
            daq.get_one_acquisition(all_data=all_data, curr_pos_str=current_pos_str, 
                                     timeout_ms=500, Average_Enable=AVERAGE_ENABLE)
            # D. 触发 Alazar 采集
            # 注意：daq.get_one_acquisition 内部应包含对激光触发脉冲的等待
            current_pos_str = f"{tx},{ty},0"
            progress_manager.update(1)
            if i % 10 == 0:
                progress_manager.set_description(f"📍 X:{tx:.1f} Y:{ty:.1f}", color="green")
            
    except KeyboardInterrupt:
        print("\n🛑 用户终止")
        stage.set_position([START_X, START_Y])
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        stage.set_position([START_X, START_Y])
    finally:
        stage.set_position([START_X, START_Y])
        # === 5. 清理与保存 ===
        try:
            gc.enable()
            daq.stop_capture()
            progress_manager.set_colour("green")
            progress_manager.stop()
            stage.connect_sdk() # 确保释放串口
        except Exception as e:
            print(f"\n❌ 发生错误: {e}")
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
