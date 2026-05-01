from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
import time
from pynput import keyboard

# ============================== 1. 参数设置 =================================
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"
STEP_SIZE = 100      # 每次按键移动的步长 (单位取决于你的设备设定)
SETTLE_MS = 150      # 位移后的稳定等待时间

# ============================== 2. 初始化 =================================
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
stage.cmd("controller.stage.ss.set 2") 
time.sleep(0.5)

def get_clean_pos(stage):
    raw = stage.get_position()
    return [int(x) for x in raw.split(',')]

print("=== 实时位移控制已启动 ===")
print("控制说明: [↑][↓][←][→] 控制移动 | [Esc] 退出程序")
print(f"当前位置: {get_clean_pos(stage)}")

def on_press(key):
    try:
        # 获取当前位置
        current_pos = get_clean_pos(stage)
        new_pos = list(current_pos)

        # 判断按键逻辑
        if key == keyboard.Key.up:
            new_pos[1] += STEP_SIZE  # Y轴正向
        elif key == keyboard.Key.down:
            new_pos[1] -= STEP_SIZE  # Y轴负向
        elif key == keyboard.Key.left:
            new_pos[0] -= STEP_SIZE  # X轴负向
        elif key == keyboard.Key.right:
            new_pos[0] += STEP_SIZE  # X轴正向
        elif key == keyboard.Key.esc:
            print("\n程序已退出")
            return False # 停止监听
        else:
            return True # 忽略其他按键

        # 执行移动
        stage.set_position([new_pos[0], new_pos[1]])
        
        # 等待稳定
        stage.wait_until_settled(new_pos[0], new_pos[1], settle_time_ms=SETTLE_MS)
        
        # 实时输出
        print(f"当前位置: {new_pos} (已稳定)", end='\r')

    except Exception as e:
        print(f"\n发生错误: {e}")

# 启动监听器
with keyboard.Listener(on_press=on_press) as listener:
    listener.join()