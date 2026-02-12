import serial
import serial.tools.list_ports
import time

def scan_prior_controller():
    print("🔍 开始寻找 Prior 控制器...")
    
    # 1. 列出所有可用端口
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("❌ 未发现任何 COM 端口！请检查 USB 线连接。")
        return

    # Prior 支持的波特率列表
    # 手册说默认是 9600，但为了高速传输，很多人会改成 115200
    baud_rates = [9600, 115200, 38400, 19200]
    
    for p in ports:
        print(f"\n👉 正在检测端口: {p.device} ({p.description})")
        
        for baud in baud_rates:
            try:
                # 尝试连接
                ser = serial.Serial(p.device, baud, timeout=0.2)
                
                # 关键步骤：先发个回车清空缓冲区
                ser.write(b"\r")
                time.sleep(0.05)
                ser.flushInput()
                
                # 发送握手指令 '?' (查询系统信息)
                ser.write(b"?\r")
                
                # 读取响应
                response = ser.read_until(b'\r').decode('ascii', errors='ignore').strip()
                
                if len(response) > 2:  # 如果收到了有效字符
                    print(f"   ✅ 成功匹配！波特率: {baud}")
                    print(f"   📦 设备响应: {response}")
                    print("   ------------------------------------------------")
                    print(f"   🎉 请在主程序中使用: port='{p.device}', baudrate={baud}")
                    ser.close()
                    return p.device, baud
                else:
                    # 某些情况下设备可能处于兼容模式，不回显，尝试发送 'VERSION'
                    ser.write(b"VERSION\r")
                    resp_v = ser.read_until(b'\r').decode('ascii', errors='ignore').strip()
                    if "Version" in resp_v or len(resp_v) > 2:
                        print(f"   ✅ 成功匹配 (通过VERSION)！波特率: {baud}")
                        print(f"   📦 设备响应: {resp_v}")
                        ser.close()
                        return p.device, baud

                ser.close()
                print(f"   ...波特率 {baud} 无响应")
                
            except serial.SerialException:
                print(f"   ❌ 端口被占用或无法打开 (可能之前的程序没关?)")
            except Exception as e:
                print(f"   ⚠️ 异常: {e}")

    print("\n❌ 扫描结束，未找到 Prior 控制器。")
    print("建议：\n1. 拔掉 USB 线重插\n2. 重启控制器电源\n3. 确保之前的 Python 窗口已彻底关闭")

if __name__ == "__main__":
    scan_prior_controller()