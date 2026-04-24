from Alazar_imaging.PriorUnifiedStage import PriorUnifiedStage
import time

# ============================== 实验参数设置 =================================
TEST_STEPS = [1, 10, 100, 1000, 5000]  # 位移跨度 (单位: 20nm 如果 ss=2)
CHECK_INTERVAL = 0.01                # 轮询频率 (10ms)
STABLE_THRESHOLD = 3                 # 连续 3 次坐标相同视为物理稳定
DLL_PATH = r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll"
COM_PORT = "4"
SETTLE_MS = 50     # 到位后的物理稳定时间 (根据位移台震动调整)
TIMEOUT = 2.0                         # 单次移动最大等待 2 秒
REPEATS = 100           # 每个步长重复测试次数

def get_clean_pos(stage):
    raw = stage.get_position()
    return [int(x) for x in raw.split(',')]

def calculate_stats(data):
    """计算平均值和方差"""
    if not data: return 0.0, 0.0
    mean = sum(data) / len(data)
    variance = sum((x - mean) ** 2 for x in data) / len(data)
    return mean, variance

# === 初始化硬件 ===
stage = PriorUnifiedStage(DLL_PATH, COM_PORT)
stage.cmd("controller.stage.ss.set 2") # 设定为 20nm 步长
time.sleep(1)
base_pos = get_clean_pos(stage)


print(f"### 位移台稳定性统计测试 (SS=2, Repeats={REPEATS})")
print("| Step | Mean Time (ms) | Variance | Success Rate | Final Status |")
print("| :--- | :--- | :--- | :--- | :--- |")

for step in TEST_STEPS:
    times = []
    success_count = 0
    
    for _ in range(REPEATS):
        # 1. 记录初始位置
        
        target_pos = [base_pos[0] + step, base_pos[1] + step]
        
        # 2. 执行位移
        stage.set_position(target_pos)
        start_time = time.time()
        
        
        # 3. 监测稳定过程
        last_pos = None
        stable_count = 0
        current_reach_time = None
        
        while (time.time() - start_time) < TIMEOUT:
            current_pos = get_clean_pos(stage)
            if current_pos == last_pos:
                stable_count += 1
            else:
                stable_count = 0
            
            if stable_count >= STABLE_THRESHOLD:
                current_reach_time = (time.time() - start_time) * 1000
                # 校验位置是否完全准确
                if current_pos == target_pos:
                    success_count += 1
                    times.append(current_reach_time)
                break
            
            last_pos = current_pos
            time.sleep(CHECK_INTERVAL)
        
        # 4. 强制复位，准备下一次重复测试
        stage.set_position(base_pos)
        time.sleep(2) # 给充足的复位稳定时间

    # 5. 计算统计结果
    mean_t, var_t = calculate_stats(times)
    success_rate = (success_count / REPEATS) * 100
    status = "✅ STABLE" if success_rate == 100 else "⚠️ UNSTABLE"
    
    print(f"| {step} | {mean_t:.2f} | {var_t:.2f} | {success_rate:.0f}% | {status} |")

print("\n> 注：Mean Time 仅计入成功到达 Target_Pos 的样本。")
stage.set_position(base_pos) 
print("所有测试结束，硬件已归位。")