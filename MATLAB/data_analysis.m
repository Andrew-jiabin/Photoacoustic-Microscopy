function data_analysis_2D_v2()
    % 封装为 function 以实现嵌套函数间的变量共享
    close all; clc;

    %% 1. 数据加载与 3D 立方体重建
    mat_path = "./data/2026-05-01_23-37-52-D-1600-AVER-256-f-image-3.mat";
    fprintf('正在加载数据...\n');
    S = load(mat_path); 

    meta = S.metadata;
    scan_w = double(meta.scan_shape(1)); 
    scan_h = double(meta.scan_shape(2)); 
    pos_list = cellstr(meta.pos_list); 
    dx = meta.step_size; dy = meta.step_size;

    % 自动获取波形长度并预分配
    first_key = sanitize_key_matlab(pos_list{1});
    waveform_length = length(S.(first_key));
    Data_Cube = zeros(scan_h, scan_w, waveform_length, 'single');
    MAP_img = zeros(scan_h, scan_w, 'single'); 

    % 坐标映射重建
    all_coords = zeros(length(pos_list), 2);
    for i = 1:length(pos_list)
        all_coords(i, :) = sscanf(pos_list{i}, '%f, %f')';
    end
    min_x = min(all_coords(:, 1)); min_y = min(all_coords(:, 2));

    fprintf('正在重建 3D 矩阵并识别位点...\n');
    for i = 1:length(pos_list)
        raw_pos_str = pos_list{i};
        coords = all_coords(i, :);
        col = round((coords(1) - min_x) / dx) + 1;
        row = round((coords(2) - min_y) / dy) + 1;
        
        if row >= 1 && col >= 1 && row <= scan_h && col <= scan_w
            safe_key = sanitize_key_matlab(raw_pos_str);
            if isfield(S, safe_key)
                wf = single(S.(safe_key));
                Data_Cube(row, col, :) = wf;
                MAP_img(row, col) = max(wf) - min(wf); 
            end
        end
    end
    fprintf('重建完成。导航：[↑][↓] 控制行(Y)，[←][→] 控制列(X)。\n');

    %% 2. 交互式界面初始化
    Fs = 2e9; T = 1/Fs;
    curRow = 1; curCol = 1;
    CLIPPING_THRESHOLD = 2; % 设定：连续 2 个点达到最大值即视为量程溢出隐患

    fig = figure('Name', 'PAM 二维导航扫描器 - 失真监测版', 'NumberTitle', 'off', ...
                 'KeyPressFcn', @keyPressCallback, 'Color', 'w', 'Position', [100, 100, 1300, 850]);

    % 左上：时域信号
    ax_time = subplot(2, 2, 1); h_time = plot(NaN, NaN, 'k'); grid on; 
    xlabel('时间 (μs)'); ylabel('幅值'); title('时域信号 (Time Domain)');

    % 左下：dB 频谱
    ax_db = subplot(2, 2, 3); h_db = plot(NaN, NaN, 'b'); grid on; 
    xlabel('频率 (MHz)'); ylabel('幅度 (dB)'); title('频谱 (dB Spectrum)');

    % 右侧：二维掩码图
    ax_mask = subplot(2, 2, [2 4]); 
    mask_base = MAP_img; mask_base(mask_base > 0) = 0.3; 
    imagesc(mask_base); colormap(ax_mask, 'gray'); hold on;
    h_marker = plot(NaN, NaN, 'rs', 'MarkerSize', 12, 'LineWidth', 2, 'MarkerFaceColor', 'r'); 
    axis image; title('扫描位点导航图');
    xlabel('X (pixels)'); ylabel('Y (pixels)');

    % 底部信息栏 (支持多行显示)
    infoText = uicontrol('Style', 'text', 'Position', [30, 10, 600, 45], ...
                         'FontSize', 10, 'FontWeight', 'bold', 'HorizontalAlignment', 'left', ...
                         'BackgroundColor', 'w');

    updatePlot();

    %% 3. 嵌套导航与失真检测逻辑
    function keyPressCallback(~, event)
        switch event.Key
            case 'leftarrow',  if curCol > 1, curCol = curCol - 1; end
            case 'rightarrow', if curCol < scan_w, curCol = curCol + 1; end
            case 'uparrow',    if curRow > 1, curRow = curRow - 1; end
            case 'downarrow',  if curRow < scan_h, curRow = curRow + 1; end
        end
        updatePlot();
    end

    function updatePlot()
        % 1. 数据提取
        x_raw = squeeze(Data_Cube(curRow, curCol, :));
        x_data = double(x_raw);
        
        % 2. 失真检测 (Clipping Detection)
        % 检查时序信号最大值的连续性
        [peak_val, ~] = max(abs(x_data));
        is_at_peak = (abs(x_data) == peak_val);
        
        % 计算最长连续峰值长度
        if peak_val > 0
            diff_peak = diff([0; is_at_peak; 0]);
            starts = find(diff_peak == 1);
            ends = find(diff_peak == -1);
            max_consecutive = max(ends - starts);
        else
            max_consecutive = 0;
        end

        % 3. 频域计算 (dB)
        N = length(x_data);
        t = (0:N-1)' * T;
        X = fft(x_data);
        n_pos = 1:floor(N/2)+1;
        freq_pos = (0:floor(N/2)) * Fs / N;
        amp = abs(X(n_pos)) / N;
        db_amp = 20 * log10(amp + 1e-15);

        % 4. 更新 UI 元素
        set(h_time, 'XData', t*1e6, 'YData', x_data);
        set(h_db, 'XData', freq_pos*1e-6, 'YData', db_amp);
        set(h_marker, 'XData', curCol, 'YData', curRow);
        
        xlim(ax_db, [0 500]); 
        axis(ax_time, 'tight');

        % 5. 状态信息更新与失真警告
        statusColor = 'k';
        clippingMsg = '';
        
        if max_consecutive >= CLIPPING_THRESHOLD
            statusColor = 'r';
            clippingMsg = sprintf(' | ⚠️ 警告：检测到信号平顶 (连续 %d 点)，可能已超出量程！', max_consecutive);
        end
        
        if all(x_data == 0)
            statusMsg = ' [空数据点]'; statusColor = [0.5 0.5 0.5];
        else
            statusMsg = '';
        end
        
        set(infoText, 'String', sprintf('位置: Row(Y)=%d, Col(X)=%d%s\n最大连续峰值点数: %d%s', ...
            curRow, curCol, statusMsg, max_consecutive, clippingMsg), 'ForegroundColor', statusColor);
        
        drawnow;
    end

    function safe_key = sanitize_key_matlab(pos_str)
        clean = strrep(pos_str, ' ', '');
        clean = strrep(clean, '.', 'p');
        clean = strrep(clean, '-', 'n');
        clean = strrep(clean, ',', '_');
        safe_key = ['P_', clean];
    end
end