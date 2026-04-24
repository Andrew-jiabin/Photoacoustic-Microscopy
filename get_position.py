from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
import time

# ============================== 实验参数设置 =================================
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"


def get_clean_pos(stage):
    raw = stage.get_position()
    return [int(x) for x in raw.split(',')]


# === 初始化硬件 ===
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
stage.cmd("controller.stage.ss.set 2") # 设定为 20nm 步长
time.sleep(1)
base_pos = get_clean_pos(stage)
print(base_pos)


