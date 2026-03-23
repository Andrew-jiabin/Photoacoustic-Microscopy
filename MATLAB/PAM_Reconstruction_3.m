%% 1. 全量加载数据（内存换速度）
mat_path = "./data/2026-03-21_17-24-30.mat";
fprintf('正在将数据全量加载至内存...\n');
tic;
S = load(mat_path); % 一次性读入所有变量到结构体 S 中
load_time = toc;
fprintf('加载完成，耗时: %.2f 秒\n', load_time);

save_name = "D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\result_reconstruction_fast.png";

%% 2. 提取元数据与参数
if ~isfield(S, 'metadata')
    error('未找到元数据，请确保 Python 端已正确保存 metadata 字段。');
end

meta = S.metadata;
scan_w = double(meta.scan_shape(1));
scan_h = double(meta.scan_shape(2));
pos_list = cellstr(meta.pos_list); % 这里的顺序与采集顺序完全一致

%% 3. 高速重建 (Vectorized Logic)
% 预分配矩阵
MAP_img_flat = zeros(1, length(pos_list), 'single');

fprintf('正在进行并行化特征提取...\n');
tic;
% 遍历 pos_list，按采集顺序提取特征
% 注意：结构体字段访问 S.(key) 在内存中非常快
for i = 1:length(pos_list)
    safe_key = sanitize_key_matlab(pos_list{i});
    
    if isfield(S, safe_key)
        waveform = single(S.(safe_key));
        % 核心计算：极差投影
        MAP_img_flat(i) = max(waveform) - min(waveform);
    end
end

% 4. 将一维序列还原为二维图像 (处理蛇形扫描逻辑)
% 由于 trajectory 在 Python 里是按行生成的，我们直接 reshape
MAP_img = reshape(MAP_img_flat, [scan_w, scan_h])';

% 如果 Python 端是蛇形扫描（偶数行翻转），这里需要镜像翻转回来
for h = 2:2:scan_h
    MAP_img(h, :) = fliplr(MAP_img(h, :));
end
recon_time = toc;
fprintf('重建完成，计算耗时: %.2f 秒\n', recon_time);

%% 5. 绘图与保存
fig = figure('Visible', 'off', 'Color', 'w');
% 根据实际坐标轴显示（这里假设从 0,0 开始，步长由元数据提供）
imagesc(MAP_img); 
colormap('hot'); colorbar; axis image;
title('Fast MAP Reconstruction');
saveas(fig, save_name);
fprintf('✅ 成功！总处理时间: %.2f 秒\n', load_time + recon_time);

%% 辅助函数 (保持不变)
function safe_key = sanitize_key_matlab(pos_str)
    clean = strrep(pos_str, ' ', '');
    clean = strrep(clean, '.', 'p');
    clean = strrep(clean, '-', 'n');
    clean = strrep(clean, ',', '_');
    safe_key = ['P_', clean];
end
