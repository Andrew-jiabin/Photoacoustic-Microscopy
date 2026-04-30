from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
import time 
# ============================== 1. 参数设置 =================================
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"
SETTLE_MS = 5000     # 到位后的物理稳定时间 (根据位移台震动调整)

def get_clean_pos(stage):
    raw = stage.get_position()
    return [int(x) for x in raw.split(',')]
 

# === 低精度位移定位 ===
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
# stage.cmd("controller.stage.ss.set 2") # 设定为 20nm 步长
time.sleep(0.5)
base_pos = get_clean_pos(stage)
save_confirm = input(f"\n补充完成水? (y/n): ").strip().lower()
while(save_confirm!="y" and save_confirm!="n"):
    save_confirm = input(f"\n补充完成水? (y/n): ").strip().lower()
    if save_confirm == 'y':
        break
    else:
        continue

stage.set_position([base_pos[0], base_pos[1]])
stage.wait_until_settled(base_pos[0], base_pos[1], settle_time_ms=SETTLE_MS)
                
print("已归位")