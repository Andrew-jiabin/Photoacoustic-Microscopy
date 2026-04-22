from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage

# ============================== 1. 参数设置 =================================
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"
SETTLE_MS = 50     # 到位后的物理稳定时间 (根据位移台震动调整)


# === 2. 初始化硬件 ===
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)



# 使用建议：
# res = stage.upgrade_to_high_precision()
res = stage.cmd("controller.stage.steps-per-micron.get")

print(res)

stage.cmd("controller.stage.ss.set 1")
res = stage.cmd("controller.stage.steps-per-micron.get")

print(res)

# goto_position_precision(10.5, 20.75, res) # 这样就能实现 10.5微米的移动