import nidaqmx
from matplotlib import pyplot as plt
from nidaqmx.constants import Edge, AcquisitionType

with nidaqmx.Task() as task:
    task.ai_channels.add_ai_voltage_chan("Dev1/ai1", min_val=-10.0, max_val=10.0)
    task.timing.cfg_samp_clk_timing(
        rate=10000,                       # 样本率（示例：10 kS/s）
        source="",                         # 默认内部时钟
        active_edge=Edge.RISING,
        sample_mode=AcquisitionType.CONTINUOUS,
        samps_per_chan=1000               # 缓冲区大小示例
    )
    data = task.read(number_of_samples_per_channel=1000)

    # 创建图形和坐标轴
    plt.figure(figsize=(10, 4))

    # 绘制折线图
    plt.plot(data, marker='o', linestyle='-', color='b', label='数组值')

    # 添加标题和标签
    plt.title('一维数组可视化', fontsize=14)
    plt.xlabel('索引位置', fontsize=12)
    plt.ylabel('数组值', fontsize=12)

    # 添加网格线
    plt.grid(True, linestyle='--', alpha=0.7)

    # 添加图例
    plt.legend()

    # 调整布局
    plt.tight_layout()

    # 显示图形
    plt.show()