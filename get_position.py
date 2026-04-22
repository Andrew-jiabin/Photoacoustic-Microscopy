from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage

# ============================== 1. 参数设置 =================================
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"
SETTLE_MS = 50     # 到位后的物理稳定时间 (根据位移台震动调整)


# === 2. 初始化硬件 ===
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)


raw_pos = stage.get_position()
print(raw_pos)

# 成像位点：-18115,1771

# 滴水位点: -17934,-26632


# 成像范围： -18187,1645 到 -18119,1818