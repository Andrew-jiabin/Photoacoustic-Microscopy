function [Scan_step_micron,scan_horizontal_pixel,scan_vertical_pixel,average_times_code,path,range,sum_or_max,example_used] = get_information()
Scan_step_micron=[250 250];    % 扫描距离  
path="G:\PhD_training\experiment\PAM_2025_8_20\test6 70 50 125";
scan_horizontal_pixel=70;  
scan_vertical_pixel=50;
average_times_code=1;
range=[63000 65000];
sum_or_max=1;
example_used="\380_1.csv";
end

% test4 40 20 250 是有问题的
% 