%% 1. 全量加载数据（保持不变）
mat_path = "./data/2026-03-21_17-59-32.mat";
fprintf('正在将数据全量加载至内存...\n');
tic;
S = load(mat_path); 
load_time = toc;
fprintf('加载完成，耗时: %.2f 秒\n', load_time);

%% 2. 提取元数据与预分配网格
meta = S.metadata;
scan_w = double(meta.scan_shape(1)); % X方向点数
scan_h = double(meta.scan_shape(2)); % Y方向点数
pos_list = cellstr(meta.pos_list);   % 格式如 "0.0, 0.5"

dx=1;
dy=1;

% 【关键修改】：预分配二维网格图像
% 初始化为 NaN 或 0，方便观察是否有漏采点
MAP_img = zeros(scan_h, scan_w, 'single'); 

%% 3. 基于坐标映射的重建 (改进版)
% --- 自动计算偏移量以防止索引 <= 0 ---
all_coords = zeros(length(pos_list), 2);
for i = 1:length(pos_list)
    all_coords(i, :) = sscanf(pos_list{i}, '%f, %f')';
end
min_x = min(all_coords(:, 1));
min_y = min(all_coords(:, 2));

fprintf('检测到坐标范围: X=[%.2f, %.2f], Y=[%.2f, %.2f]\n', ...
    min_x, max(all_coords(:, 1)), min_y, max(all_coords(:, 2)));

% 重新开始循环填充
for i = 1:length(pos_list)
    raw_pos_str = pos_list{i};
    coords = all_coords(i, :);
    curr_x = coords(1);
    curr_y = coords(2);
    
    % 使用相对偏移量计算索引，确保最小值为 1
    col = round((curr_x - min_x) / dx) + 1;
    row = round((curr_y - min_y) / dy) + 1;
    
    % 检查是否越界（防御性编程）
    if row < 1 || col < 1 || row > scan_h || col > scan_w
        warning('索引越界: pos(%f,%f) -> idx(%d,%d). 请检查 dx/dy 或 scan_shape', ...
            curr_x, curr_y, row, col);
        continue;
    end
    
    safe_key = sanitize_key_matlab(raw_pos_str);
    if isfield(S, safe_key)
        waveform = single(S.(safe_key));
        MAP_img(row, col) = max(waveform) - min(waveform);
    end
end

recon_time = toc;
fprintf('网格填充重建完成，耗时: %.2f 秒\n', recon_time);

%% 4. 绘图与保存
% 由于是直接按坐标填入，不再需要手动处理“蛇形翻转”逻辑
fig = figure('Visible', 'off', 'Color', 'w');
imagesc(MAP_img); 
colormap('hot'); colorbar; axis image;
xlabel('X (pixels)'); ylabel('Y (pixels)');
title('Grid-based MAP Reconstruction');
saveas(fig, "D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\result_grid_recon.png");

%% 辅助函数 (保持不变)
function safe_key = sanitize_key_matlab(pos_str)
    clean = strrep(pos_str, ' ', '');
    clean = strrep(clean, '.', 'p');
    clean = strrep(clean, '-', 'n');
    clean = strrep(clean, ',', '_');
    safe_key = ['P_', clean];
end