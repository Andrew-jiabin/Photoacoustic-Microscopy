import atsapi as alazar

def check_my_ats9373():
    try:
        # 1. 初始化 Board 对象（这会自动调用 AlazarGetBoardBySystemID）
        # 默认就是 systemId=1, boardId=1
        board = alazar.Board(systemId=1, boardId=1)
        
        print("✅ 成功连接到 ATS9373 采集卡")
        
        # 2. 查询 PCIe 链路信息
        # 注意：在你的源码中，这些常量定义在全局空间，而 queryCapability 是 Board 的方法
        # 修正后的逻辑判断
        speed = board.queryCapability(alazar.GET_PCIE_LINK_SPEED).value
        width = board.queryCapability(alazar.GET_PCIE_LINK_WIDTH).value

        if speed >= 3 and width >= 8:
            print("🚀 状态：完美！硬件链路已满载 (Gen3 x8)")
        
        # 3. 查询序列号 (SN) —— 解决你刚才找序列号的问题
        sn = board.queryCapability(alazar.GET_SERIAL_NUMBER)
        
        print(f"\n--- 硬件信息报告 ---")
        print(f"板卡序列号 (S/N): {sn}")
        print(f"PCIe 协商速率: Gen {speed}")
        print(f"PCIe 协商宽度: x{width}")
        
        # 4. 性能判定
        if speed == 3 and width == 8:
            print("\n🚀 状态：完美！已达到理论最大带宽 (Gen3 x8)。")
        else:
            print("\n⚠️ 状态：受限！建议检查主板插槽是否支持 Gen3 x8。")
            
    except Exception as e:
        print(f"❌ 运行失败: {e}")

if __name__ == "__main__":
    check_my_ats9373()