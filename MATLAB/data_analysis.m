function signalBrowser()
% 交互式信号浏览工具：6子图版（含线性与dB频谱）
% ========== 环境设置 ==========
set(0, 'DefaultFigureRenderer', 'painters');
try
    com.mathworks.services.Prefs.setBooleanPref('JavaFrameFeature加速', false);
catch
end
clear; close all; clc;

% --------------------- 1. 核心参数设置 ---------------------
Fs = 2e9;          % 采样率：2 GHz
T = 1/Fs;          % 采样周期

% --------------------- 2. 加载数据 ---------------------
try
    raw_data = evalin('base', 'raw_data');
catch
    error('在工作区中未找到变量 raw_data。');
end

numSignals = size(raw_data, 1);
allSignals = cell(numSignals, 1);
for i = 1:numSignals
    x = squeeze(raw_data(i, 1, :));
    x = double(x);
    x = (x - 32768) / 65536; 
    allSignals{i} = x(:);
end

% --------------------- 3. 图形初始化 ---------------------
currentIdx = 1;
fig = figure('Name', '信号浏览器 (左右键切换) - 6子图版', ...
             'NumberTitle', 'off', ...
             'KeyPressFcn', @keyPressCallback, ...
             'Position', [50, 50, 1400, 900], ...
             'Color', 'white');

% 布局改为 3行2列
% 第一行：时域
ax1 = subplot(3,2,1); h1 = plot(NaN, NaN); grid on;
xlabel('时间 (μs)'); ylabel('幅值'); title('原始时域信号');
ax2 = subplot(3,2,2); h2 = plot(NaN, NaN); grid on;
xlabel('时间 (μs)'); ylabel('幅值'); title('滤波后时域信号');

% 第二行：线性频域
ax3 = subplot(3,2,3); h3 = plot(NaN, NaN); grid on;
xlabel('频率 (MHz)'); ylabel('归一化幅度'); title('原始频谱 (线性)');
ax4 = subplot(3,2,4); h4 = plot(NaN, NaN); grid on;
xlabel('频率 (MHz)'); ylabel('归一化幅度'); title('滤波频谱 (线性)');

% 第三行：dB 频域 (新增)
ax5 = subplot(3,2,5); h5 = plot(NaN, NaN); grid on;
xlabel('频率 (MHz)'); ylabel('幅度 (dB)'); title('原始频谱 (dB)');
ax6 = subplot(3,2,6); h6 = plot(NaN, NaN); grid on;
xlabel('频率 (MHz)'); ylabel('幅度 (dB)'); title('滤波频谱 (dB)');

infoText = uicontrol('Style', 'text', 'Position', [20, 10, 200, 25], ...
                     'FontSize', 10, 'FontWeight', 'bold');

updatePlot();

% --------------------- 回调与更新函数 ---------------------
    function keyPressCallback(~, event)
        if strcmp(event.Key, 'leftarrow') && currentIdx > 1
            currentIdx = currentIdx - 1; updatePlot();
        elseif strcmp(event.Key, 'rightarrow') && currentIdx < numSignals
            currentIdx = currentIdx + 1; updatePlot();
        end
    end

    function updatePlot()
        x = allSignals{currentIdx};
        N = length(x);
        t = (0:N-1)' * T;
        x_filtered = x - mean(x);

        % 频谱计算
        X_orig = fft(x);
        X_filt = fft(x_filtered);
        
        if mod(N,2) == 0
            n_pos = 1:N/2+1;
            freq_pos = (0:N/2) * Fs / N;
        else
            n_pos = 1:(N+1)/2;
            freq_pos = (0:(N-1)/2) * Fs / N;
        end
        
        % 线性幅度
        amp_orig = abs(X_orig(n_pos)) / N;
        amp_filt = abs(X_filt(n_pos)) / N;
        
        % dB 幅度计算：20*log10，防止 log(0)
        % 0 dB 通常对应归一化幅度 1.0
        db_orig = 20 * log10(amp_orig + 1e-15); 
        db_filt = 20 * log10(amp_filt + 1e-15);

        % 更新数据
        set(h1, 'XData', t*1e6, 'YData', x);
        set(h2, 'XData', t*1e6, 'YData', x_filtered);
        
        set(h3, 'XData', freq_pos*1e-6, 'YData', amp_orig);
        set(h4, 'XData', freq_pos*1e-6, 'YData', amp_filt);
        
        set(h5, 'XData', freq_pos*1e-6, 'YData', db_orig);
        set(h6, 'XData', freq_pos*1e-6, 'YData', db_filt);

        % 坐标轴控制
        axis([ax1 ax2], 'tight');
        xlim([ax3 ax4 ax5 ax6], [0 1000]); % 限制频率范围到 1GHz
        
        % 线性频谱纵轴固定 (根据你之前的设置)
        ylim([ax3 ax4], [0 2e-5]); 
        
        % dB 频谱纵轴固定：通常在 -140dB 到 -80dB 左右
        ylim([ax5 ax6], [-160 -80]); 

        set(infoText, 'String', sprintf('信号索引: %d / %d', currentIdx, numSignals));
        drawnow;
    end
end