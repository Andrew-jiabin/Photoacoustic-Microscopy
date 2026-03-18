%% 1. 加载数据
mat_path = "./data/2026-03-18_12-14-45-4G.mat";
raw_obj = matfile(mat_path);
vars = who(raw_obj);
save_name = "D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\result_reconstruction.png";
%% 2. 提取信息 (自适应模式)
% 过滤出所有以 'P_' 开头的数据变量
data_vars = vars(startsWith(vars, 'P_'));
if isempty(data_vars)
    error('文件中未找到以 P_ 开头的数据变量，请检查文件内容。');
end

% 尝试寻找元数据
meta_idx = find(contains(vars, 'metadata', 'IgnoreCase', true), 1);

if ~isempty(meta_idx)
    % --- 模式 A：从元数据读取（速度快） ---
    fprintf('检测到元数据，正在解析...\n');
    meta = raw_obj.(vars{meta_idx});
    pos_list = cellstr(meta.pos_list);
    step_um = double(meta.step_um);
    is_averaged = double(meta.is_averaged);
    
    % 解析坐标
    coords = zeros(length(pos_list), 2);
    for i = 1:length(pos_list)
        tmp = str2num(pos_list{i});
        coords(i, :) = tmp(1:2);
    end
else
    % --- 模式 B：无元数据模式（反向解析变量名） ---
    % 为了确保核心逻辑清晰，我们从变量名中提取坐标
    fprintf('未找到元数据，正在通过变量名反向解析坐标...\n');
    num_pts = length(data_vars);
    coords = zeros(num_pts, 2);
    
    for i = 1:num_pts
        % 示例变量名: P_n120p5_60_0 -> 提取 [-120.5, 60]
        v_name = data_vars{i};
        coords(i, :) = parse_coords_from_name(v_name);
    end
    step_um = 0; % 无法从变量名获取步进，设为0
    is_averaged = NaN;
end

% 提取唯一轴
uniqueX = unique(coords(:, 1));
uniqueY = unique(coords(:, 2));

%% 3. 执行 MAP 重建
MAP_img = zeros(length(uniqueY), length(uniqueX), 'single');
fprintf('正在计算投影图 (共 %d 个点)...\n', size(coords, 1));

for i = 1:size(coords, 1)
    % 确定当前要读取的变量名
    if ~isempty(meta_idx)
        safe_key = sanitize_key_matlab(pos_list{i});
        curr_x = coords(i, 1);
        curr_y = coords(i, 2);
    else
        safe_key = data_vars{i};
        curr_x = coords(i, 1);
        curr_y = coords(i, 2);
    end
    
    % 读取并计算投影
    waveform = single(raw_obj.(safe_key));
    val = max(waveform) - min(waveform);
    
    % 填充矩阵
    row_idx = find(uniqueY == curr_y);
    col_idx = find(uniqueX == curr_x);
    if ~isempty(row_idx) && ~isempty(col_idx)
        MAP_img(row_idx, col_idx) = val;
    end
    
    if mod(i, 500) == 0, fprintf('进度: %.1f%%\n', (i/size(coords, 1))*100); end
end

%% 4. 绘图与保存 (SSH 预览优化)
fig = figure('Visible', 'off', 'Color', 'w');
imagesc(uniqueX, uniqueY, MAP_img);
colormap('hot'); colorbar; axis image;
xlabel('X (\mum)'); ylabel('Y (\mum)');
title(['MAP Recon: ', strrep(data_vars{1}, '_', ' ')], 'Interpreter', 'none');

saveas(fig, save_name); 

fprintf('✅ 重建成功！预览图已存至: %s\n', save_name);

%% =========================================================================
% 辅助函数
% =========================================================================

% 将变量名 P_n120p5_60_0 转换回数字坐标
function c = parse_coords_from_name(v_name)
    % 去掉开头的 P_
    str = v_name(3:end);
    % 将 p 换回 . , 将 n 换回 -
    str = strrep(str, 'p', '.');
    str = strrep(str, 'n', '-');
    % 按底杠分割
    parts = split(str, '_');
    % 转换前两个部分为 X 和 Y
    c = [str2double(parts{1}), str2double(parts{2})];
end

function safe_key = sanitize_key_matlab(pos_str)
    clean = strrep(pos_str, ' ', '');
    clean = strrep(clean, '.', 'p');
    clean = strrep(clean, '-', 'n');
    clean = strrep(clean, ',', '_');
    safe_key = ['P_', clean];
end
