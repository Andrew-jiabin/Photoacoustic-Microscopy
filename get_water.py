from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage

# ============================== 1. 参数设置 =================================
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"
SETTLE_MS = 50     # 到位后的物理稳定时间 (根据位移台震动调整)


# === 2. 初始化硬件 ===
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)


stage.set_position([-16442, -27939])



# B. 核心握手：等待物理到位
stage.wait_until_settled(-16442, -27939, settle_time_ms=SETTLE_MS)
                
