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
        self.mode = 'OFFLINE'  # 可选: 'SDK', 'SERIAL', 'OFFLINE'
        self.ser = None        # 用于存储 serial 对象
        
        # --- 1. 加载 DLL ---
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
        # 无论程序如何退出，都会尝试停止电机
        atexit.register(self.emergency_stop)
        
        # --- 3. 初始连接 (默认使用 SDK 模式) ---
        self.connect_sdk()

    # =====================================================
    #  核心机制：模式切换 (解决端口独占问题)
    # =====================================================
    
    def connect_sdk(self):
        """切换到 SDK 控制模式 (常规移动/查询)"""
        if self.mode == 'SERIAL':
            self.disconnect_serial()
            
        if self.mode != 'SDK':
            print(f"🔌 [切换] 连接 SDK 模式 (Port {self.port_sdk_str})...")
            ret = self.cmd_sdk_raw(f"controller.connect {self.port_sdk_str}")
            if ret == 0:
                self.mode = 'SDK'
                print("✅ SDK 已连接")
            else:
                print(f"❌ SDK 连接失败, 错误码: {ret}")

    def disconnect_sdk(self):
        """断开 SDK 连接 (释放 COM 口给串口用)"""
        if self.mode == 'SDK':
            self.cmd_sdk_raw("controller.disconnect")
            self.mode = 'OFFLINE'
            time.sleep(0.5) # 给系统一点时间释放资源

    def connect_serial(self):
        """切换到 原生串口 模式 (用于 AutoScan)"""
        if self.mode == 'SDK':
            self.disconnect_sdk()
            
        if self.mode != 'SERIAL':
            print(f"🔌 [切换] 连接原生串口模式 ({self.port_serial_str})...")
            try:
                self.ser = serial.Serial(self.port_serial_str, self.baudrate, timeout=0.1)
                self.ser.flushInput()
                self.ser.flushOutput()
                
                # 激活标准模式并握手
                self._serial_send_wait("COMP,0")
                if not self._serial_send_wait("?"):
                    if not self._serial_send_wait("VERSION"):
                        raise Exception("握手无响应")
                        
                self.mode = 'SERIAL'
                print("✅ 串口已连接 (高速模式)")
            except Exception as e:
                print(f"❌ 串口连接失败: {e}")
                self.connect_sdk() # 尝试回滚到 SDK

    def disconnect_serial(self):
        """关闭串口连接"""
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.mode = 'OFFLINE'

    # =====================================================
    #  Part A: SDK 功能封装 (原有的功能)
    # =====================================================

    def cmd_sdk_raw(self, msg):
        """发送 SDK 指令 (内部使用)"""
        return self.SDKPrior.PriorScientificSDK_cmd(
            self.sessionID, create_string_buffer(msg.encode()), self.rx
        )

    def cmd(self, msg):
        """发送 SDK 指令并返回结果 (供用户调用)"""
        if self.mode != 'SDK': self.connect_sdk() # 自动切回 SDK
        ret = self.cmd_sdk_raw(msg)
        return ret, self.rx.value.decode()

    def get_position(self): 
        """获取当前坐标 (SDK)"""
        # 注意: 如果当前是 SERIAL 模式，这会自动切回 SDK 模式，速度较慢
        # 如果在扫描中途，千万不要调用这个！
        if self.mode != 'SDK': self.connect_sdk()
        
        self.cmd_sdk_raw("controller.stage.position.get")
        return self.rx.value.decode()
    
    def set_position(self, position: list): 
        """移动到指定位置 (SDK)"""
        if self.mode != 'SDK': self.connect_sdk()
        self.cmd_sdk_raw(f"controller.stage.goto-position {position[0]} {position[1]}")

    def get_sdk_version(self):
        return self.SDKPrior.PriorScientificSDK_Version(self.rx)

    # =====================================================
    #  Part B: 高速扫描功能 (整合 Solution B)
    # =====================================================

    def _serial_send_wait(self, cmd_text):
        """串口底层发送 (私有方法)"""
        if not self.ser: return ""
        try:
            self.ser.flushInput()
            self.ser.write((cmd_text + "\r").encode('ascii'))
            return self.ser.read_until(b'\r').decode('ascii', errors='ignore').strip()
        except Exception:
            return ""

    def perform_autoscan(self, width_px, height_px, step_um):
        """
        执行高速 AutoScan。
        注意：此函数会自动接管 COM 口，并在完成后自动归还给 SDK。
        """
        # 1. 切换环境
        print("\n=== 准备启动高速扫描任务 ===")
        self.connect_serial()
        if self.mode != 'SERIAL':
            print("❌ 无法进入串口模式，扫描中止")
            return

        total_points = width_px * height_px
        print(f"⚙️ 配置: {width_px}x{height_px} | 步长: {step_um}μm | 总点数: {total_points}")

        try:
            # 2. 配置参数
            self._serial_send_wait(f"N,{width_px-1},{height_px-1}")
            self._serial_send_wait(f"X,{step_um},{step_um}")
            # AS配置: 1ms曝光, 0ms稳定, TTL1, 高电平, 蛇形
            resp = self._serial_send_wait("AS,1,0,1,H,S")
            if "E" in resp:
                print(f"⚠️ 配置警告: {resp}")

            # 3. 启动并监控
            print("🚀 启动 AutoScan (硬件接管中)...")
            self._serial_send_wait("AS,1")
            
            start_time = time.perf_counter()
            
            while True:
                status = self._serial_send_wait("AS")
                # 使用 P 指令直接从串口读位置
                pos = self._serial_send_wait("P") 
                
                if not status: continue

                if status == "0":
                    end_time = time.perf_counter()
                    self._print_stats(start_time, end_time, total_points, pos)
                    break
                
                print(f"\r🔄 扫描中... 坐标: {pos.ljust(15)} | 状态: {status}   ", end="", flush=True)
                time.sleep(0.05)

        except KeyboardInterrupt:
            print("\n⚠️ 用户手动中止！")
            self._serial_send_wait("I") # 串口急停
        
        finally:
            # 4. 任务结束，切回 SDK 模式以供后续常规操作
            print("\n=== 扫描结束，恢复 SDK 连接 ===")
            self.connect_sdk()

    def _print_stats(self, start_t, end_t, total, final_pos):
        duration = end_t - start_t
        avg_step = (duration * 1000) / total if total > 0 else 0
        freq = 1000 / avg_step if avg_step > 0 else 0
        print("\n" + "-"*40)
        print(f"⏱️ 总耗时: {duration:.4f}s | 平均速度: {avg_step:.2f} ms/点 | 频率: {freq:.2f} Hz")
        print(f"📍 最终坐标: {final_pos}")
        print("-" * 40)

                

# =====================================================
#  主程序调用示例
# =====================================================

if __name__ == "__main__":
    dll_loc = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
    com_port = "4" # 用户只需提供数字，类内部会自动处理
    
    # 1. 初始化 (自动连接 SDK)
    stage = PriorUnifiedStage(dll_loc, com_port)
    
    # 2. 测试 SDK 功能
    print(f"🔍 [SDK] 当前位置: {stage.get_position()}")
    
    # 3. 移动到一个起点 (SDK)
    # stage.set_position([0, 0])
    
    # 4. 执行高速扫描 (自动切换到 串口 -> 扫描 -> 切回 SDK)
    # 参数: 宽50px, 高50px, 步长10um
    stage.perform_autoscan(50, 50, 1)
    
    # 5. 再次测试 SDK 功能 (验证是否切回来了)
    print(f"🔍 [SDK] 扫描后位置: {stage.get_position()}")
    
    # 程序结束时会自动触发 emergency_stop 并断开连接