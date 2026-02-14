% =========================================================================
% PAM 数据读取与逐点重建主程序
% =========================================================================
clear; clc; close all;
set(0, 'DefaultFigureVisible', 'off'); % 禁止弹出窗口，防止远程报错
%% 1. 加载数据
filepath = '.\data.mat';
fprintf('⏳ 正在加载文件: %s ...\n', filepath);
data = load(filepath);

% 提取变量 (注意: Python savemat 存的参数在 MATLAB 中会自动转为 double)
raw_data = data.raw_data;       % 维度: [N_points, Records, Samples] 或 [N_points, 1, Samples]
pos_map = data.pos_map;         % 维度: [N_points, 3] -> [X, Y, Z]
is_averaged = double(data.daq_params.is_averaged) > 0;
step_um = double(data.scan_params.step);

N_points = size(raw_data, 1);
fprintf('✅ 加载成功！共 %d 个像素点。当前数据状态: %s\n', ...
    N_points, logical2str(is_averaged));

%% 2. 坐标解析与网格分配 (完美解决蛇形扫描和机械抖动)
X = pos_map(:, 1);
Y = pos_map(:, 2);

% 将物理坐标归一化为矩阵索引 (1 到 W, 1 到 H)
% 即使位移台有微小的机械抖动，round() 也能确保它们对齐到正确的网格
X_idx = round((X - min(X)) / step_um) + 1;
Y_idx = round((Y - min(Y)) / step_um) + 1;

% 获取最终图像的实际物理宽和高
W = max(X_idx);
H = max(Y_idx);
img_matrix = zeros(H, W);

%% 3. 逐点解析与计算 (核心循环)
fprintf('⚙️ 正在逐点计算像素值...\n');

% [可选] 如果你想看进度条，可以取消注释下面这行
% wbar = waitbar(0, 'Processing...');

for i = 1:N_points
    % 提取当前点的 A-line 数据
    % squeeze 会移除长度为 1 的维度:
    % - 平均后: [1, 1, Samples] -> [Samples, 1]
    % - 未平均: [1, Records, Samples] -> [Records, Samples]
    point_data = squeeze(raw_data(i, :, :));
    
    % 调用你的自定义函数计算单点特征值
    pixel_val = calculate_pixel_value(point_data, is_averaged);
    
    % 填入二维网格对应的位置
    img_matrix(Y_idx(i), X_idx(i)) = pixel_val;
    
    % waitbar(i/N_points, wbar);
end
% if exist('wbar','var'), close(wbar); end

%% 4. 图像绘制
figure('Name', 'PAM Reconstruction', 'Color', 'w');
% 使用 imagesc 绘制，并将其映射到实际物理坐标轴上
x_axis_physical = min(X) : step_um : max(X);
y_axis_physical = min(Y) : step_um : max(Y);

imagesc(x_axis_physical, y_axis_physical, img_matrix);
colormap('hot'); % 光声成像常用热力图
colorbar;
axis image;      % 保证 X 和 Y 比例 1:1，防止图像拉伸变形
xlabel('X Position (\mu m)', 'FontWeight', 'bold');
ylabel('Y Position (\mu m)', 'FontWeight', 'bold');
title(sprintf('PAM Maximum Amplitude Projection (Averaged: %d)', is_averaged));
% --- 保存为可编辑的 MATLAB 格式 ---
% savefig('result_reconstruction.fig'); 

% --- 同时保存为可直接查看的图片格式 (方便在 VS Code 里直接预览) ---
saveas(gcf, 'result_reconstruction.png'); 

fprintf('✅ 图像已保存至当前目录。\n');
% =========================================================================
% 辅助函数区
% =========================================================================

function str = logical2str(tf)
    if tf, str = '已硬件平均'; else, str = '保留所有记录 (Raw)'; end
end

%% ⭐ 你的自定义信号处理函数 ⭐
function val = calculate_pixel_value(point_data, is_averaged)
    % 输入说明：
    %   point_data : 单个像素点的数据。
    %                如果 is_averaged == true，尺寸为 [Samples, 1]
    %                如果 is_averaged == false，尺寸为 [Records, Samples]
    %   is_averaged: 布尔值，标识数据是否在采集端被平均过
    
    % 1. 数据类型转换 (关键：从 uint16 转为 double 防止计算溢出)
    point_data = double(point_data);
    
    if is_averaged
        % -----------------------------------------------------------------
        % 模式 A: 硬件已经平均好的数据 [维度: Samples x 1]
        % -----------------------------------------------------------------
        % 这里写你的单线处理逻辑。例如最简单的最大振幅投影 (MAP):
        
        % a. 去除直流偏置 (基线漂移)
        signal = point_data - mean(point_data); 
        
        % b. 你可以在这里加入带通滤波代码 (类似 EEG 处理那套)
        % [b, a] = butter(4, [1e6 50e6]/(2e9/2), 'bandpass');
        % signal = filtfilt(b, a, signal);
        
        % c. 提取特征值 (例如最大包络值)
        val = max(abs(signal));
        
    else
        % -----------------------------------------------------------------
        % 模式 B: 未平均的原始数据 [维度: Records x Samples]
        % -----------------------------------------------------------------
        % 此时你可以进行更复杂的统计操作，比如中值滤波剔除异常脉冲
        
        % a. 在软件层执行平均操作 (沿第 1 维度 Records 求均值)
        % 你可以改为 median(point_data, 1) 来剔除激光闪烁的极端值
        software_averaged_signal = mean(point_data, 1); 
        
        % b. 将其转为列向量以对齐模式 A
        signal = software_averaged_signal(:) - mean(software_averaged_signal);
        
        % c. 提取特征值
        val = max(abs(signal));
        
    end
end