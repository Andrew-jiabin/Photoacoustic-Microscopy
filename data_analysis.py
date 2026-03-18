import numpy as np
import matplotlib.pyplot as plt
import scipy.io as sio

# 加载文件
data = sio.loadmat('W(40)-H(40)-2026-03-16_21-40-27-S_4G-EXPOSURE_MS_2000_position_log.txt')

# 从字典中提取变量
# 注意：MATLAB 变量名即为字典的键
raw_data = data['raw_data']
def save_signal_analysis(raw_data, fs=2e9, filename='signal_analysis.png'):
    """
    针对 raw_data 中的第一个信号点生成 6子图分析图并保存
    """
    # --------------------- 1. 数据预处理 ---------------------
    # 提取第一个信号 [1, 1, :] -> Python 索引为 [0, 0, :]
    x_raw = raw_data[0, 0, :]
    x_raw = x_raw.astype(np.float64)
    
    # 归一化 (对应 MATLAB: (x - 32768) / 65536)
    x = (x_raw - 32768) / 65536
    
    # 滤波 (去直流分量)
    x_filtered = x - np.mean(x)
    
    # 时间轴 (微秒)
    n = len(x)
    t = np.arange(n) / fs
    t_us = t * 1e6
    
    # --------------------- 2. 频谱计算 ---------------------
    # 计算 FFT
    freqs = np.fft.rfftfreq(n, d=1/fs)
    freq_mhz = freqs * 1e-6
    
    # 线性幅度 (归一化幅度)
    amp_orig = np.abs(np.fft.rfft(x)) / n
    amp_filt = np.abs(np.fft.rfft(x_filtered)) / n
    
    # dB 幅度 (增加 epsilon 防止 log10(0))
    db_orig = 20 * np.log10(amp_orig + 1e-15)
    db_filt = 20 * np.log10(amp_filt + 1e-15)
    
    # --------------------- 3. 绘图 ---------------------
    fig, axes = plt.subplots(3, 2, figsize=(14, 10), constrained_layout=True)
    fig.patch.set_facecolor('white')

    # 第一行：时域信号
    axes[0, 0].plot(t_us, x)
    axes[0, 0].set_title('Original Time Domain')
    axes[0, 1].plot(t_us, x_filtered)
    axes[0, 1].set_title('Filtered Time Domain (Mean Removed)')
    for ax in axes[0, :]: ax.set_xlabel('Time (μs)'); ax.set_ylabel('Amplitude'); ax.grid(True)

    # 第二行：线性频谱
    axes[1, 0].plot(freq_mhz, amp_orig)
    axes[1, 0].set_title('Original Spectrum (Linear)')
    axes[1, 1].plot(freq_mhz, amp_filt)
    axes[1, 1].set_title('Filtered Spectrum (Linear)')
    for ax in axes[1, :]: 
        ax.set_xlabel('Frequency (MHz)'); ax.set_ylabel('Linear Amp')
        ax.set_xlim(0, 1000)   # 限制 1GHz
        ax.set_ylim(0, 2e-5)   # 对应 MATLAB ylim
        ax.grid(True)

    # 第三行：dB 频谱
    axes[2, 0].plot(freq_mhz, db_orig)
    axes[2, 0].set_title('Original Spectrum (dB)')
    axes[2, 1].plot(freq_mhz, db_filt)
    axes[2, 1].set_title('Filtered Spectrum (dB)')
    for ax in axes[2, :]: 
        ax.set_xlabel('Frequency (MHz)'); ax.set_ylabel('Amplitude (dB)')
        ax.set_xlim(0, 1000)   # 限制 1GHz
        ax.set_ylim(-160, -80) # 对应 MATLAB ylim
        ax.grid(True)

    # 保存图片
    plt.savefig(filename, dpi=300)
    plt.close()
    print(f"图像已保存至: {filename}")

# ========== 使用示例 ==========
# 假设你的 raw_data 是一个 numpy 数组
# 如果是从 .mat 加载，通常使用 scipy.io.loadmat
if __name__ == "__main__":

    save_signal_analysis(raw_data)