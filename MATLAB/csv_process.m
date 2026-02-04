clear all;
close all;
% test5 40 20 250
Scan_step_micron=[250 250];    % 扫描距离  
scan_horizontal_pixel=40;  
scan_vertical_pixel=20;
average_times_code=1;
path="B:\Phd_training\experiment\PAM_2025_8_19\test5 40 20 250";
element_num=(scan_horizontal_pixel+1)*(scan_vertical_pixel+1);
% data_table=readtable(strcat("0_1",'.csv'));
data_table=readtable("B:\Phd_training\experiment\PAM_2025_8_19\test5 40 20 250\1_1.csv");
data=table2array(data_table(:,2));
range=[53416 56962];
data=data(range(1):range(2));
data_length=length(data);

data_length=1;
data_element=zeros(element_num,data_length);
image=zeros(data_length,(scan_horizontal_pixel+1),(scan_vertical_pixel+1));
for i = 1:element_num
    tmep_flag=1;
    for j = 1:average_times_code
        M = readtable(strcat(path,"\",num2str(i-1),"_",num2str(j),'.csv'));  % 自动跳过表头，得到2×3矩阵
        M = table2array(M(:,2));
        if tmep_flag==1
            data_temp_for_avearage=M(range(1):range(2));

            tmep_flag=0;
        else
            data_temp_for_avearage=data_temp_for_avearage+M(range(1):range(2));
        end
      
    end
    % data_temp_for_avearage=normalize(Transform_me(abs(data_temp_for_avearage)), 'range');
    % data_element(i,:)=data_temp_for_avearage;
    data_element(i,:)=max(abs(data_temp_for_avearage)); 

    % data_element(i,:)=data_temp_for_avearage;    
    
end

for i = 1:data_length
    image(i,:,:) = back_to_image(data_element(:,i),scan_vertical_pixel,scan_horizontal_pixel);
end
