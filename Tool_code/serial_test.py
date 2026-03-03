import serial
import time
import sys
import atexit  # [关键] 用于注册程序退出时的清理函数

class PriorPAMScannerSafe:
    def __init__(self, port="COM4", baudrate=115200):
        self.ser = None
        self.total_points = 0
        self.is_connected = False
        
        try:
            print(f"🔌 [系统] 正在连接 {port} (波特率: {baudrate})...")
            self.ser = serial.Serial(port, baudrate, timeout=0.1)
            self.is_connected = True
            
            # [关键安全机制] 注册退出函数：无论程序怎么死，死前都要运行 self.emergency_stop
            atexit.register(self.emergency_stop)
            
            # 1. 切换到标准模式
            self.send_cmd("COMP,0")
            
            # 2. 握手测试
            info = self.send_cmd("?")
            if not info:
                info = self.send_cmd("VERSION")
                if not info:
                    raise Exception("设备无响应！")
            
            print(f"✅ [安全] 握手成功！急停守护已激活。")
            
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            sys.exit()

    def emergency_stop(self):
        """
        [安全核心] 发送急停指令 'I'。
        该函数会在程序退出、报错或被关闭时自动触发。
        """
        if self.is_connected and self.ser and self.ser.is_open:
            print("\n\n🛑 [系统] 正在触发安全急停...")
            try:
                # 发送 'I' (Interrupt): 停止移动并清空指令队列
                self.ser.write(b"I\r")
                time.sleep(0.05)
                # 可选：发送 'K' (Kill): 立即断电刹车（更暴力，防撞用）
                # self.ser.write(b"K\r") 
                self.ser.close()
                print("✅ [系统] 硬件已制动，串口已安全关闭。")
            except Exception as e:
                print(f"⚠️ 急停发送失败 (可能串口已断): {e}")

    def send_cmd(self, cmd_text):
        try:
            self.ser.flushInput()
            full_cmd = (cmd_text + "\r").encode('ascii')
            self.ser.write(full_cmd)
            raw_response = self.ser.read_until(b'\r')
            return raw_response.decode('ascii', errors='ignore').strip()
        except Exception:
            return ""

    def get_live_pos(self):
        return self.send_cmd("P")

    def configure_scan(self, width_px, height_px, step_um):
        self.total_points = width_px * height_px
        print(f"⚙️ [配置] 网格: {width_px}x{height_px} | 步长: {step_um}μm")
        print(f"📊 [预计] 总采样点数: {self.total_points}")
        
        self.send_cmd(f"N,{width_px-1},{height_px-1}")
        self.send_cmd(f"X,{step_um},{step_um}")
        
        # AS配置: 1ms脉冲, 0ms等待, TTL1, 高电平, 蛇形扫描
        resp = self.send_cmd("AS,1,0,1,H,S")
        if "E" in resp:
            print(f"⚠️ 配置警告: {resp}")
        else:
            print("✅ 参数已下发至控制器")

    def run_scan_task(self):
        print("\n🚀 [启动] AutoScan 硬件自动扫描中...")
        print("-" * 50)
        
        # --- 计时器启动 ---
        start_time = time.perf_counter()
        
        # 发送启动指令
        self.send_cmd("AS,1")
        
        try:
            while True:
                # 1. 监控状态
                status = self.send_cmd("AS")
                # 2. 监控位置
                pos = self.get_live_pos()
                
                if not status: continue

                # 状态 '0' 代表完成
                if status == "0":
                    end_time = time.perf_counter() # 计时结束
                    self.print_report(start_time, end_time, pos)
                    break
                
                # 实时刷新显示
                print(f"\r🔄 扫描进行中... 坐标: {pos.ljust(15)} | 状态: {status}   ", end="", flush=True)
                time.sleep(0.05)
                
        except KeyboardInterrupt:
            # 用户按 Ctrl+C 时，emergency_stop 会被 atexit 再次调用作为双重保险
            print("\n⚠️ 用户手动中止！")
            
        # 注意：这里不需要手动调用 self.close()，atexit 会自动处理

    def print_report(self, start_t, end_t, final_pos):
        duration = end_t - start_t
        # 计算平均每一步的耗时 (ms)
        avg_step_time = (duration * 1000) / self.total_points if self.total_points > 0 else 0
        # 计算有效频率 (Hz)
        freq = 1000 / avg_step_time if avg_step_time > 0 else 0
        
        print("\n" + "=" * 50)
        print("🏁 扫描任务完成报告")
        print("=" * 50)
        print(f"⏱️  总耗时     : {duration:.4f} 秒")
        print(f"📍 停止坐标   : {final_pos}")
        print("-" * 50)
        print(f"⚡ 平均速度   : {avg_step_time:.2f} ms/点")
        print(f"📡 有效帧率   : {freq:.2f} Hz (Points per Second)")
        print("=" * 50)

# --- 主程序 ---
if __name__ == "__main__":
    # 请确保 COM 口和波特率正确
    scanner = PriorPAMScannerSafe(port="COM4", baudrate=115200)
    
    # 🧪 测试参数
    scanner.configure_scan(width_px=20, height_px=20, step_um=1)
    
    # ▶️ 开始
    scanner.run_scan_task()
    
    # 程序结束时，atexit 会自动打印 "硬件已制动..."