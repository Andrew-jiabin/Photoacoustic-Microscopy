function signalBrowser()
% 交互式信号浏览工具，通过左右方向键切换100个信号并实时绘图

% ========== 新增：强制设置图形渲染器 ==========
% 先重置图形设置
set(0, 'DefaultFigureRenderer', 'painters');
% 关闭Java加速（部分版本兼容问题）
com.mathworks.services.Prefs.setBooleanPref('JavaFrameFeature加速', false);
% =============================================

clear; close all; clc;

% --------------------- 1. 核心参数设置 ---------------------
Fs = 2e9;          % 采样率：2 GHz
T = 1/Fs;          % 采样周期

% --------------------- 2. 从工作区加载 raw_data ---------------------
% 尝试从基础工作区获取 raw_data 变量
try
    raw_data = evalin('base', 'raw_data');
catch
    error('在工作区中未找到变量 raw_data。请先加载数据。');
end

% 检查维度
expectedSize = [100, 1, 2048];
if ~isequal(size(raw_data), expectedSize)
    error('raw_data 的维度应为 [100, 1, 2048]，但实际为 [%s]', num2str(size(raw_data)));
end

numSignals = size(raw_data, 1);  % 应为100
allSignals = cell(numSignals, 1);
for i = 1:numSignals
    x = squeeze(raw_data(i, 1, :));  % 提取第i个信号，2048x1 uint16
    x = double(x);                    % 转换为 double
    x = (x - 32768) / 65536;          % 归一化到约 [-0.5, 0.5]
    allSignals{i} = x(:);              % 确保为列向量
end
% -----------------------------------------------------------------

% 初始化当前索引
currentIdx = 1;

% ========== 优化：显式设置图形可见性 ==========
fig = figure('Name', '信号浏览器 (左右键切换)', ...
             'NumberTitle', 'off', ...
             'KeyPressFcn', @keyPressCallback, ...
             'Position', [100, 100, 1200, 800], ...
             'Visible', 'on', ...  % 强制可见
             'Renderer', 'painters');  % 强制渲染器
% =============================================

% 预先创建四个子图的坐标轴句柄，方便更新数据而不重复创建
ax1 = subplot(2,2,1);
h1 = plot(NaN, NaN);  % 占位线条
xlabel('时间 (μs)'); ylabel('幅值'); title('原始时域信号（含直流）'); grid on;

ax2 = subplot(2,2,2);
h2 = plot(NaN, NaN);
xlabel('时间 (μs)'); ylabel('幅值'); title('滤波后时域信号（无直流）'); grid on;

ax3 = subplot(2,2,3);
h3 = plot(NaN, NaN);
xlabel('频率 (MHz)'); ylabel('归一化幅度'); title('原始信号频域（正频率）'); grid on;

ax4 = subplot(2,2,4);
h4 = plot(NaN, NaN);
xlabel('频率 (MHz)'); ylabel('归一化幅度'); title('滤波后信号频域（正频率）'); grid on;

% 添加一个文本显示当前索引
infoText = uicontrol('Style', 'text', ...
                     'String', sprintf('信号 %d / %d', currentIdx, numSignals), ...
                     'Position', [50, 20, 150, 20], ...
                     'BackgroundColor', 'white');

% 首次绘制
updatePlot();

% --------------------- 嵌套函数：键盘回调 ---------------------
    function keyPressCallback(~, event)
        switch event.Key
            case 'leftarrow'
                if currentIdx > 1
                    currentIdx = currentIdx - 1;
                    updatePlot();
                end
            case 'rightarrow'
                if currentIdx < numSignals
                    currentIdx = currentIdx + 1;
                    updatePlot();
                end
        end
    end

% --------------------- 嵌套函数：更新绘图 ---------------------
    function updatePlot()
        % 获取当前信号
        x = allSignals{currentIdx};
        x = x(:);  % 确保列向量
        N = length(x);
        t = (0:N-1)' * T;  % 时间轴列向量

        % 滤除直流
        x_filtered = x - mean(x);

        % 傅里叶变换（不进行fftshift，只取正频率）
        X_orig = fft(x);
        X_filt = fft(x_filtered);

        % 生成正频率轴及对应幅度
        if mod(N,2) == 0
            n_pos = 1:N/2+1;
            freq_pos = (0:N/2) * Fs / N;
        else
            n_pos = 1:(N+1)/2;
            freq_pos = (0:(N-1)/2) * Fs / N;
        end
        X_orig_amp = abs(X_orig(n_pos)) / N;
        X_filt_amp = abs(X_filt(n_pos)) / N;

        % --- 更新图形数据 ---
        % 时域原始信号
        set(h1, 'XData', t*1e6, 'YData', x);
        axis(ax1, 'tight');

        % 时域滤波后信号
        set(h2, 'XData', t*1e6, 'YData', x_filtered);
        axis(ax2, 'tight');

        % 频域原始信号
        set(h3, 'XData', freq_pos*1e-6, 'YData', X_orig_amp);
        xlim(ax3, [0 1000]);  % 固定X轴为0~1000 MHz
        ylim(ax3, [0 2e-5]);  % ========== 修复：原代码这里写错了，是ax3不是ax4 ==========

        % 频域滤波后信号
        set(h4, 'XData', freq_pos*1e-6, 'YData', X_filt_amp);
        xlim(ax4, [0 1000]);
        ylim(ax4, [0 2e-5]);

        % 更新索引显示
        set(infoText, 'String', sprintf('信号 %d / %d', currentIdx, numSignals));

        % 刷新图形
        drawnow;
    end

end