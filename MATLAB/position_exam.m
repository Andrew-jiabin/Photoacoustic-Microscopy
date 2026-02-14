%% === 1. 参数设置与数据加载 ===
clear; clc;
filepath = 'data.mat';
data = load(filepath);
point_check = 100;
% --- 你可以手动填，也可以让它自动读 ---
MANUAL_STEP = 1; % <--- 在这里填你的 STEP_UM
auto_step = double(data.scan_params.step); 

% 优先使用手动设置的值
step_to_use = MANUAL_STEP; 

pos_map = data.pos_map;
X = pos_map(:, 1);
Y = pos_map(:, 2);

fprintf('📏 使用步长: %d um (数据记录步长: %d um)\n', step_to_use, auto_step);

%% === 2. 原始坐标逐条打印 (揪出罪魁祸首) ===
fprintf('\n📋 原始采集坐标明细 (前 20 点):\n');
fprintf('------------------------------------\n');
fprintf(' 序号  |    X    |    Y    | 差值(dX)\n');
fprintf('------------------------------------\n');

for i = 1:min(point_check, size(pos_map, 1))
    if i > 1
        dx = X(i) - X(i-1);
    else
        dx = 0;
    end
    fprintf(' [%3d] | %7d | %7d | (%d) \n', i, X(i), Y(i), dx);
end

%% === 3. 网格索引计算 (针对 1um 优化) ===
% 如果坐标是整数且 Step=1，公式简化为：Idx = Pos - min(Pos) + 1
X_idx = (X - min(X)) / step_to_use + 1;
Y_idx = (Y - min(Y)) / step_to_use + 1;

% 检查是否有非整数索引 (如果 Step=1，这里理论上全为整数)
if any(mod(X_idx, 1) ~= 0)
    fprintf('⚠️ 警告：发现非整数索引！说明物理坐标间距不是 %d 的倍数。\n', step_to_use);
end

% 强制转为整数类型以便后续建图
X_idx = round(X_idx);
Y_idx = round(Y_idx);

%% === 4. 统计分析 ===
expected_total = data.scan_params.width * data.scan_params.height;
actual_total = length(X);

% 查找重复
[~, unique_idx] = unique([X_idx, Y_idx], 'rows');
duplicates = actual_total - length(unique_idx);

fprintf('\n📊 诊断报告:\n');
fprintf(' - 总点数: %d / 预期: %d\n', actual_total, expected_total);
fprintf(' - 重复点: %d (坐标完全一样的点)\n', duplicates);