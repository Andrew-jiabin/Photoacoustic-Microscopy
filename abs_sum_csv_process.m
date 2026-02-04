clear all;
close all;
[Scan_step_micron,scan_horizontal_pixel,scan_vertical_pixel,average_times_code,path,range,sum_or_max,example_used] = get_information();
element_num=(scan_horizontal_pixel+1)*(scan_vertical_pixel+1);
data_table=readtable(strcat(path,example_used));
data=table2array(data_table(:,2));
data=data(range(1):range(2));
if sum_or_max==1
    data_length=1;   % 因为求了max
else
    data_length=length(data);
end
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
    data_element(i,:)=sum(abs(data_temp_for_avearage),1); 

    % data_element(i,:)=data_temp_for_avearage;    
    
end

for i = 1:data_length
    image(i,:,:) = back_to_image(data_element(:,i),scan_vertical_pixel,scan_horizontal_pixel);
end
