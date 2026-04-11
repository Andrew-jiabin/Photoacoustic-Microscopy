import gc
import time
from Tool_code.data_saving import save_experiment_data
import datetime
import atsapi as ats
from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Alazar_imaging.LBMover import LBMover
from Tool_code.position_trans import sanitize_pos_to_key
import numpy as np
import scipy.io as sio # 用于保存 mat 文件
from Alazar_imaging.Alazar_imaging_tools import lbtek_wait_settled
def main():
    # ============================== 1. 参数设置 =================================
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    LB_DLL_PATH = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\LBTEK_SDK\x64\moverLibrary.dll"
    MODEL_NAME = b"EM-LSS65-13C1"
    COM_PORT = "COM6" # 你的位移台串口
    # 如果不使用一维位移台, 则不要设置长宽任意为 1
    SCAN_W = 140    
    SCAN_H = 1      # 设置为 1 实现 1 维扫描
    STEP_UM = 10    # 步长 (注意单位，麓邦通常是 mm，如果是 1um 请填 0.001)
    # 扫描参数 (数值格式，方便计算)
    SETTLE_MS = 50     # 到位后的物理稳定时间 (根据位移台震动调整)
    offset = 5   # um 偏置，克服机械位移差
    # DAQ 参数 (Alazar)
    SAMPLES_REC = 4096
    SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS   # 如果使用 B 通道, 则只能使用2000MSPS
    RECORDS_PER_POINT = 64 
    AVERAGE_ENABLE = True
    # SAMPLE_RATE_str = "4G"
    # RECORDS_BUF = 64 
    RECORDS_PER_POINT = 64 # 每个点记录多少个record，在平均的情况下，也不能大于1048832，否则uint32会溢出
    Buffer_Count = 4   # 对于单点停顿采集，4个buffer游刃有余，不用1024
    save_path = f"./data/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.mat"

    if SCAN_W == 1:
        print("如果使用1维位移台, 只能设置 SCAN_H 为1, 不能设置 SCAN_W 为1 - ljb")
        exit(0)

    if SCAN_H == 1:
        print("检测到 1D 扫描模式，切换至 LBMover 控制器...")
        stage = LBMover(LB_DLL_PATH)
        handle = stage.openEmcvx(COM_PORT.encode('utf-8'))
        if handle < 0:
            raise RuntimeError("无法打开麓邦位移台串口")
        stage.handle = handle
        
        # 初始化轴
        # 参数：句柄, 轴ID, 型号, 总轴数为1
        res = stage.initAxis(handle, 0, MODEL_NAME, 1)
        if res == 0:
            print(f"✅ 型号 {MODEL_NAME.decode()} 初始化成功")
            stage.setAxisEnable(handle, 0, 1)
        else:
            # 如果返回非0，说明 DLL 内部不认识这个型号名，需要调用 getAllModels 检查
            print(f"❌ 初始化失败，错误码: {res}") 

    else:
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
    if SCAN_H == 1:
        # 获取当前位置
        start_pos, _ = stage.get_pos(0)
        trajectory = []
        for w in range(SCAN_W):
            target_x = start_pos + (w * STEP_UM / 1000.0) # 假设 STEP_UM 是微米，转为毫米
            trajectory.append(target_x)
    else:
        raw_pos = stage.get_position()
        START_X, START_Y = [float(v) for v in raw_pos.split(',')[:2]]
        
        trajectory = []
        for h in range(SCAN_H):
            # w_range = range(SCAN_W) if h % 2 == 0 else reversed(range(SCAN_W))
            w_range = reversed(range(SCAN_W))
            
            for w in w_range:
                target_x = START_X + (w * STEP_UM)
                target_y = START_Y + (h * STEP_UM)
                if w == (SCAN_W-1):
                    trajectory.append((target_x, target_y, 1))
                else:
                    trajectory.append((target_x, target_y, 0))
            
    # === 生成轨迹 (快轴改为 Y 轴，S型扫描) ===
    # for w in range(SCAN_W):
    #     # 根据列号 w 的奇偶性决定 Y 轴是正向还是反向扫描
    #     h_range = range(SCAN_H) if w % 2 == 0 else reversed(range(SCAN_H))
        
    #     for h in h_range:
    #         target_x = START_X + (w * STEP_UM)
    #         target_y = START_Y + (h * STEP_UM)
            
    #         # 根据当前列的扫描方向标记状态 (可选，保留你原代码的逻辑)
    #         if w % 2 == 1:
    #             trajectory.append((target_x, target_y, 1))
    #         else:
    #             trajectory.append((target_x, target_y, 0))

    # === 4. 开始实验 ===
    all_data = []
    try:
        if SCAN_H == 1:
            progress_manager.start(total=len(trajectory), desc="🚀 1D PAM Scanning")

            for i, tx in enumerate(trajectory):
                # A. 指令位移台移动 (LBMover 逻辑)
                if SCAN_H == 1:
                    # 1. 设置绝对目标位置
                    stage.setAbsoluteDisp(stage.handle, 0, tx)
                    # 2. 发送“绝对移动”指令 (0x06)
                    stage.moveEmcvx(stage.handle, 0, 0x06)
                    
                    # B. 等待到位
                    lbtek_wait_settled(stage, stage.handle, 0)
                    time.sleep(SETTLE_MS / 1000.0) # 物理稳定时间
                    
                    current_pos_str = f"{tx:.4f},0,0" # 格式化坐标字符串用于保存
                
                # C. 采集数据 (DAQ 逻辑不变)
                daq.get_one_acquisition(all_data=all_data, curr_pos_str=current_pos_str, 
                                        timeout_ms=500, Average_Enable=AVERAGE_ENABLE)
                
                progress_manager.update(1)
            
            # 回到起点
            stage.setAbsoluteDisp(stage.handle, 0, start_pos)
            stage.moveEmcvx(stage.handle, 0, 0x06)
            lbtek_wait_settled(stage, stage.handle, 0)
            # 关闭串口
            stage.closeEmcvx(stage.handle)

        else:
            progress_manager.start(total=len(trajectory), desc="🚀 PAM Scanning")

            for i, (tx, ty, flag) in enumerate(trajectory):
                if flag==1:
                    # A. 指令位移台移动
                    stage.set_position([tx+offset, ty])
                    # B. 核心握手：等待物理到位
                    stage.wait_until_settled(tx+offset, ty, settle_time_ms=SETTLE_MS)
                    current_pos_str = f"{tx+offset},{ty},0"
                else:
                    pass
                # A. 指令位移台移动
                stage.set_position([tx, ty])
                time.sleep(0.01)
                # B. 核心握手：等待物理到位
                stage.wait_until_settled(tx, ty, settle_time_ms=SETTLE_MS)
                current_pos_str = f"{tx},{ty},0"
                
                
                daq.get_one_acquisition(all_data=all_data, curr_pos_str=current_pos_str, 
                                        timeout_ms=500, Average_Enable=AVERAGE_ENABLE)

                current_pos_str = f"{tx},{ty},0"
                progress_manager.update(1)
            # 回到起点    
            stage.set_position([START_X, START_Y])


    except KeyboardInterrupt:
        print("\n🛑 用户终止")
        stage.set_position([START_X, START_Y])
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        stage.set_position([START_X, START_Y])
    finally:
        time.sleep(5)
        # === 5. 清理与保存 ===
        try:
            gc.enable()
            daq.stop_capture()
            progress_manager.set_colour("green")
            progress_manager.stop()
            if SCAN_H!=1: stage.connect_sdk() # 确保释放串口
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
                    "is_averaged": int(AVERAGE_ENABLE),
                    "step_size":STEP_UM
                }

                # 6. 最终保存
                sio.savemat(save_path, mat_dict)
                
                print(f"✅ 成功保存！共计 {len(mat_dict)-1} 个坐标位点数据，注意初始位置为 {[START_X, START_Y]}")
            
            except Exception as e:
                print(f"❌ 数据封装失败: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("⚠️ 未采集到任何有效数据，跳过保存。")

if __name__ == "__main__":
    main()
