
%% 1. 加载数据
file_path = "D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\data\rec_data\2026-04-30_17-54-45-D-1600-reconstructed_cube.mat";
load(file_path, 'Data_Cube', 'meta');

% 获取维度信息 [H, W, Time]
[scan_h, scan_w, ~] = size(Data_Cube);

%% 2. 参数设置：选择要分析的行或列
target_row = 100;    % 设置你想查看的行号
target_col = 1;    % 设置你想查看的列号
mode = 'col';        % 选项: 'row' 或 'col'

%% 3. 计算特征值并提取剖面 (Profile)
% 这里的函数暂时设定为峰峰值 (Peak-to-Peak)
% 我们先对整个 Cube 进行维度运算以提高效率

% 计算所有点的峰峰值
P2P_Map = max(Data_Cube, [], 3) - min(Data_Cube, [], 3);

% 处理“跳过空数值点”逻辑：将全零点（未采集点）标记为 NaN
% 这样在绘制折线图时，这些点会自动断开，不会出现下坠到 0 的直线
is_empty = all(Data_Cube == 0, 3);
P2P_Map(is_empty) = NaN;

if strcmpi(mode, 'row')
    profile_data = P2P_Map(target_row, :);
    axis_label = 'Column Index (X)';
    plot_title = sprintf('Row %d Profile (Peak-to-Peak)', target_row);
else
    profile_data = P2P_Map(:, target_col);
    axis_label = 'Row Index (Y)';
    plot_title = sprintf('Column %d Profile (Peak-to-Peak)', target_col);
end

%% 4. 绘图
figure('Color', 'w');
plot(profile_data, '-o', 'MarkerSize', 4, 'LineWidth', 1.5);
grid on;
xlabel(axis_label);
ylabel('Peak-to-Peak Amplitude');
title(plot_title);

% 如果你想检查是否存在由于跳过点导致的空隙
fprintf('该断面总点数: %d, 有效数据点: %d\n', length(profile_data), sum(~isnan(profile_data)));