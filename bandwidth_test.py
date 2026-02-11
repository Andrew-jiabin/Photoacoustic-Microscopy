import atsapi as alazar

def get_ats9373_bandwidth(board):
    """
    查询 ATS9373 的实际模拟带宽限制
    """
    # 1. 首先确认板卡型号是否为 ATS9373
    # ATS9373 的型号代码通常是 26 (十进制)
    board_kind = board.getBoardKind()
    if board_kind != alazar.ATS9373:
        return "错误：当前板卡不是 ATS9373。"

    # 2. 查询板卡的硬件选项 (Board Options Low)
    # OPTION_WIDEBAND_输入对应的位通常在板卡手册中定义
    # 在 ATS9373 中，宽带升级是一个特定的硬件标志位
    GET_BOARD_OPTIONS_LOW = 0x10000037  # 能力代码
    OPTION_WIDEBAND = (1 << 11)         # 假设位（注：不同固件版本可能略有差异）

    try:
        options = board.queryCapability(GET_BOARD_OPTIONS_LOW)
        # 判断是否安装了宽带升级选件
        # 注意：AlazarTech 官方文档中建议直接根据是否有此 Option 位来判断
        has_wideband = (options.value & OPTION_WIDEBAND) != 0
        
        if has_wideband:
            return {
                "版本": "宽带升级版 (Wideband Upgrade)",
                "非 DES 模式带宽": "1.9 GHz",
                "DES 模式带宽": "1.7 GHz",
                "输入电压范围": "固定 ±400 mV",
                "输入阻抗": "固定 50 Ω"
            }
        else:
            return {
                "版本": "标准版 (Standard)",
                "标称带宽": "1.0 GHz",
                "输入电压范围": "固定 ±400 mV",
                "输入阻抗": "固定 50 Ω"
            }
            
    except Exception as e:
        return f"查询失败: {str(e)}"

# 使用示例
board = alazar.Board(systemId=1, boardId=1)
print(get_ats9373_bandwidth(board))