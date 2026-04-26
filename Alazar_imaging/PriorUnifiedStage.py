from ctypes import WinDLL, create_string_buffer
import os
import serial
import time
import sys
import atexit

class PriorUnifiedStage:
    def __init__(self, dll_path, com_port_number, baudrate=115200):
        """
        初始化 Prior 显微镜控制系统。
        :param dll_path: PriorScientificSDK.dll 的绝对路径
        :param com_port_number: 端口号字符串 (例如 "4" 代表 COM4)
        :param baudrate: 串口波特率 (默认 115200)
        """
        self.dll_path = dll_path
        self.port_sdk_str = str(com_port_number)           # SDK 格式: "4"
        self.port_serial_str = f"COM{com_port_number}"     # Pyserial 格式: "COM4"
        self.baudrate = baudrate
        
        # 状态标志
        self.mode = 'OFFLINE'  # 'SDK', 'SERIAL', 'OFFLINE'
        self.ser = None        # 存储 serial 对象
        
        # --- 1. 加载 SDK DLL ---
        if os.path.exists(dll_path):
            self.SDKPrior = WinDLL(dll_path)
        else:
            raise RuntimeError(f"DLL not found at: {dll_path}")
            
        self.SDKPrior.PriorScientificSDK_Initialise()
        self.sessionID = self.SDKPrior.PriorScientificSDK_OpenNewSession()
        
        if self.sessionID < 0:
            raise RuntimeError(f"Error getting sessionID: {self.sessionID}")
            
        self.rx = create_string_buffer(5000) # 加大缓冲区防止溢出
        
        # --- 2. 注册安全急停 ---
        atexit.register(self.emergency_stop)
        
        # --- 3. 初始连接 (默认进入 SDK 模式) ---
        self.connect_sdk()

    # =====================================================
    #  核心机制：模式切换 (自动管理端口独占)
    # =====================================================
    
    def connect_sdk(self):
        """切换到 SDK 控制模式"""
        # 如果当前占着串口，先断开
        if self.mode == 'SERIAL':
            self.disconnect_serial()
            
        if self.mode != 'SDK':
            # print(f"🔌 [切换] 连接 SDK 模式...")
            ret = self.cmd_sdk_raw(f"controller.connect {self.port_sdk_str}")
            if ret == 0:
                self.mode = 'SDK'
            else:
                print(f"❌ SDK 连接失败, 错误码: {ret}")

    def disconnect_sdk(self):
        """断开 SDK 连接 (释放 COM 口)"""
        if self.mode == 'SDK':
            self.cmd_sdk_raw("controller.disconnect")
            self.mode = 'OFFLINE'
            time.sleep(0.1) 

    def connect_serial(self):
        """切换到 原生串口 模式"""
        # 如果当前占着 SDK，先断开
        if self.mode == 'SDK':
            self.disconnect_sdk()
            
        if self.mode != 'SERIAL':
            try:
                # print(f"🔌 [切换] 连接原生串口模式...")
                self.ser = serial.Serial(self.port_serial_str, self.baudrate, timeout=0.05)
                self.ser.flushInput()
                self._serial_send_wait("COMP,0") # 确保标准模式
                self.mode = 'SERIAL'
            except Exception as e:
                print(f"❌ 串口连接失败: {e}")
                self.connect_sdk() # 失败则回滚到 SDK

    def disconnect_serial(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.mode = 'OFFLINE'

    # =====================================================
    #  Part A: 原有 SDK 功能 (已全部复原)
    # =====================================================

    def cmd_sdk_raw(self, msg):
        """(内部函数) 直接调用 DLL 发送指令"""
        return self.SDKPrior.PriorScientificSDK_cmd(
            self.sessionID, create_string_buffer(msg.encode()), self.rx
        )

    def cmd(self, msg):
        """
        [原函数复原] 发送指令并返回 (错误码, 响应字符串)
        会自动检查并切换到 SDK 模式。
        """
        if self.mode != 'SDK': self.connect_sdk()
        ret = self.cmd_sdk_raw(msg)
        return ret, self.rx.value.decode()

    def cmd_simple(self, msg):
        """
        [原函数复原] 仅发送指令，不返回结果 (但会更新 self.rx)
        """
        if self.mode != 'SDK': self.connect_sdk()
        self.cmd_sdk_raw(msg)
        
    def get_ID(self):
        """[原函数复原] 获取 Session ID"""
        return self.sessionID
    
    def get_SDK_version(self): # 原名 get_SDK_vision，修正了拼写
        """[原函数复原] 获取 SDK 版本"""
        return self.SDKPrior.PriorScientificSDK_Version(self.rx)

    def get_position(self): 
        """[原函数复原] 获取当前位置 (SDK方式)"""
        if self.mode != 'SDK': self.connect_sdk()
        self.cmd_sdk_raw("controller.stage.position.get")
        return self.rx.value.decode()
    
    def set_position(self, position: list): 
        """[原函数复原] 移动到指定位置 (SDK方式)"""
        if self.mode != 'SDK': self.connect_sdk()
        self.cmd_sdk_raw(f"controller.stage.goto-position {position[0]} {position[1]}")
    
    def stage_deinitial(self):
        """[原函数复原] 断开控制器连接"""
        return self.disconnect_sdk()
    
    # 请添加在 PriorUnifiedStage 类内部
    def is_busy(self):
        """检查位移台是否还在运动 (SDK 模式)"""
        ret, resp = self.cmd("controller.stage.busy")
        return resp == "1"

    def wait_until_settled(self, target_x, target_y, settle_time_ms, tolerance_step=0.05):
        flag = False
        time_count= False
        while not flag:
            if time_count==True:
                flag = True
            pos_str = self.get_position() # 获取 SDK 坐标
            parts = pos_str.split(',')
            curr_x, curr_y = float(parts[0]), float(parts[1])
            if abs(curr_x - target_x) < tolerance_step and abs(curr_y - target_y) < tolerance_step:
                time.sleep(settle_time_ms/1000) # 短暂休眠，避免占用 100% CPU
                pos_str = self.get_position() # 获取 SDK 坐标
                parts = pos_str.split(',')
                curr_x, curr_y = float(parts[0]), float(parts[1])
                if abs(curr_x - target_x) < tolerance_step and abs(curr_y - target_y) < tolerance_step:
                    break
                else:
                    time_count=True
                    continue
            elif (abs(curr_x - target_x) > tolerance_step and abs(curr_y - target_y) > tolerance_step) and time_count:
                    print("settle time too short!")

        return False


    def upgrade_to_high_precision(self):
        """验证并开启亚微米精度模式"""
        if self.mode != 'SDK': self.connect_sdk()
        
        # 1. 获取硬件物理极限（每微米包含多少个微步）
        self.cmd_sdk_raw("controller.stage.steps-per-micron.get")
        steps_per_um = int(self.rx.value.decode())
        
        # 2. 设置用户单位为 1 个微步 (最高精度)
        # 注意：更改 ss 会重置 hostdirection [cite: 547]
        self.cmd_sdk_raw("controller.stage.ss.set 1")
        
        # 3. 计算当前的单位分辨率
        resolution = 1.0 / steps_per_um
        print(f"模式已切换！当前SDK 1个单位 = {resolution:.4f} 微米")
        return resolution

    def goto_position_precision(self, x_um, y_um, resolution):
        """移动到指定位置（输入为微米，自动转换为SDK单位）"""
        # 将微米坐标转换为 SDK 的整数单位
        x_steps = int(x_um / resolution)
        y_steps = int(y_um / resolution)
        
        self.cmd_sdk_raw(f"controller.stage.goto-position {x_steps} {y_steps}")


    def _serial_send_wait(self, cmd_text):
        """(内部函数) 串口发送并等待回复"""
        if not self.ser: return ""
        try:
            # self.ser.flushInput()
            self.ser.write((cmd_text + "\r").encode('ascii'))
            return self.ser.read_until(b'\r').decode('ascii', errors='ignore').strip()
        except Exception:
            return ""

    def prepare_scan_serial(self, width_px, height_px, step_um, 
                            exposure_ms, settle_ms, ttl_pin=1):
        """
        配置扫描参数 (不启动运动)。
        会自动切换到串口模式。
        """
        self.connect_serial()
        # print(f"⚙️ [Stage] 配置扫描: {width_px}x{height_px}, 步长{step_um}um")
        
        # 发送网格参数
        self._serial_send_wait(f"N,{width_px-1},{height_px-1}")
        self._serial_send_wait(f"X,{step_um},{step_um}")
        
        # 发送 AutoScan 参数
        # AS, 曝光, 稳定, TTL脚, 高电平触发(H), 蛇形扫描(S)
        cfg_str = f"AS,{exposure_ms},{settle_ms},{ttl_pin},H,S"
        resp = self._serial_send_wait(cfg_str)
        if "E" in resp: print(f"⚠️ Stage配置警告: {resp}")

    def start_scan_motion(self):
        """发送启动指令 (AS,1)"""
        # print("🚀 [Stage] 启动物理运动 (AS,1)")
        self._serial_send_wait("AS,1")

    def get_pos_fast(self):
        """
        高速读取位置 (仅串口模式下可用)。
        返回字符串 "X,Y,Z"
        """
        return self._serial_send_wait("P")

    def is_scan_running(self):
        """检查 AutoScan 是否还在运行 (返回 True/False)"""
        status = self._serial_send_wait("AS")
        return status != "0"

    # =====================================================
    #  Part C: 安全急停
    # =====================================================

    def emergency_stop(self):
        """
        [安全急停] 无论在什么模式，尝试一切手段停止电机。
        """
        # 1. 如果在串口模式，发送 I 和 K
        try:
            if self.mode == 'SERIAL' and self.ser:
                self.ser.write(b"I\r"); time.sleep(0.05)
                self.ser.write(b"K\r")
        except:
            pass

        # 2. 尝试通过 SDK 停止
        try:
            if self.SDKPrior:
                self.cmd_sdk_raw("controller.stop.abruptly")
        except:
            pass

# =====================================================
#  测试代码
# =====================================================
if __name__ == "__main__":
    DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    COM_PORT = "4"

    stage = PriorUnifiedStage(DLL_PATH, COM_PORT)

    print("--- 测试原 SDK 功能 ---")
    print(f"SDK Version: {stage.get_SDK_version()}")
    print(f"Session ID: {stage.get_ID()}")
    print(f"Current Pos: {stage.get_position()}")
    
    # 测试 cmd_simple
    stage.cmd_simple("controller.z.position.get")
    print(f"Z Pos (via rx): {stage.rx.value.decode()}")

    print("\n--- 测试高速扫描功能 ---")
    stage.prepare_scan_serial(10, 10, 10, 100, 0)
    stage.start_scan_motion()
    
    while stage.is_scan_running():
        print(f"\rScanning... {stage.get_pos_fast()}", end="")
        time.sleep(0.1)
    
    print("\n\n--- 测试切回 SDK ---")
    print(f"Final Pos: {stage.get_position()}")