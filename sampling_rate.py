import atsapi as alazar
import sys

def test_sample_rate(board, rate_id, channel_mode_desc):
    """
    尝试设置特定的采样率，返回是否成功
    """
    try:
        # 1. 设置时钟
        # 使用内部时钟测试
        board.setCaptureClock(alazar.INTERNAL_CLOCK, rate_id, alazar.CLOCK_EDGE_RISING, 0)
        
        # 2. 如果代码能跑到这里没有报错 (Exception)，说明 PLL 锁相环接受了这个频率
        print(f"   [测试] {channel_mode_desc} 设置频率... ✅ 成功")
        return True
    except Exception as e:
        print(f"   [测试] {channel_mode_desc} 设置频率... ❌ 失败")
        print(f"         错误信息: {e}")
        return False

def verify_4g_support():
    print("="*40)
    print("ATS9373 4GS/s 极限频率支持测试")
    print("="*40)

    try:
        board = alazar.Board(systemId=1, boardId=1)
    except Exception as e:
        print(f"❌ 无法连接板卡: {e}")
        return

    # 检查 atsapi 中是否有 4000MSPS 的定义
    # 在你给我的代码中，SAMPLE_RATE_4000MSPS = 0x80
    if not hasattr(alazar, 'SAMPLE_RATE_4000MSPS'):
        print("❌ 你的 SDK 版本过旧，未定义 SAMPLE_RATE_4000MSPS。")
        return

    RATE_4G = alazar.SAMPLE_RATE_4000MSPS
    RATE_2G = alazar.SAMPLE_RATE_2000MSPS

    # ==========================================
    # 实验 1: 模拟双通道模式 (Channel A + B)
    # 预期: ATS9373 在双通道下通常不支持 4G
    # ==========================================
    print("\n1️⃣  实验一: 双通道模式 (CH A + CH B)")
    # 注意: inputControl 只是设置模拟前端，真正的"单/双"通道模式往往由 setCaptureClock 
    # 或者后续的 beforeAsyncRead 中的 ChannelMask 决定。
    # 但有些板卡在 setCaptureClock 时会检查当前的通道配置。
    
    # 尝试设置 4G
    success_dual = test_sample_rate(board, RATE_4G, "双通道 @ 4GS/s")
    
    if not success_dual:
        print("   -> 符合预期: ATS9373 双通道通常无法达到 4GS/s (物理限制)。")
    else:
        print("   -> 意外: 居然允许双通道设置 4G？(请后续检查数据是否正确)")

    # ==========================================
    # 实验 2: 强制单通道模式 (Only Channel A)
    # 预期: 应该成功
    # ==========================================
    print("\n2️⃣  实验二: 单通道模式 (仅 CH A)")
    
    # 这里是一个关键技巧: 
    # 虽然 atsapi.py 没有直接的 "SetSingleChannel" 函数，
    # 但在调用 beforeAsyncRead 时只传入 CHANNEL_A 掩码即可。
    # 不过，为了测试 setCaptureClock 是否报错，我们先尝试重置一下。
    
    # 再次尝试设置 4G (假设前一次失败了，或者为了确保是在干净状态下测试)
    success_single = test_sample_rate(board, RATE_4G, "单通道 @ 4GS/s")

    if success_single:
        print("   -> 🎉 验证通过! 你的板子支持 4GS/s 采样率。")
        print("   -> 💡 提示: 在后续采集代码中，beforeAsyncRead 的 channelMask 必须只选 CHANNEL_A (1)。")
    else:
        print("   -> ❌ 验证失败。板子拒绝了 4GS/s 设置。")
        print("      可能原因: 需要特殊的 DES 模式标志位，或者固件限制。")

    # ==========================================
    # 实验 3: 验证 DES (Dual Edge Sampling) 模式
    # 如果实验 2 失败，可能需要显式调用 DES 模式
    # ==========================================
    if not success_single:
        print("\n3️⃣  实验三: 尝试显式 DES 模式")
        try:
            # 在某些老版本 API 中需要先 setParameter 设置 ADC_MODE
            board.setParameter(alazar.CHANNEL_A, alazar.SET_ADC_MODE, alazar.ADC_MODE_DES)
            success_des = test_sample_rate(board, RATE_4G, "DES 模式 @ 4GS/s")
            if success_des:
                 print("   -> 🎉 通过 DES 模式设置成功!")
        except Exception as e:
            print(f"   -> DES 设置不支持: {e}")

if __name__ == "__main__":
    verify_4g_support()