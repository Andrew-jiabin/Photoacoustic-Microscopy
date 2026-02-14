def get_expected_trajectory(SCAN_W, SCAN_H, STEP_UM, START_X, START_Y):
    
    expected_trajectory_str = []
    for j in range(SCAN_H):
        curr_y = START_Y + j * STEP_UM
        
        # 偶数行 (0, 2, 4...) X 增加；奇数行 (1, 3, 5...) X 减小
        if j % 2 == 0:
            x_range = range(START_X, START_X + SCAN_W)
        else:
            x_range = range(START_X + SCAN_W - 1, START_X - 1, -1)
        
        for curr_x in x_range:
            # 严格匹配位移台返回格式: "X,Y,0"
            # 注意：这里要确保负号和空格与硬件返回完全一致
            target_s = f"{curr_x},{curr_y},0"
            expected_trajectory_str.append(target_s)
    
    return expected_trajectory_str