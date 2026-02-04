function image = back_to_image(Data_1D,scan_vertical_pixel,scan_horizontal_pixel)
%UNTITLED2 此处显示有关此函数的摘要
%   此处显示详细说明
image=zeros((scan_horizontal_pixel+1),(scan_vertical_pixel+1));
Back_flag=0;
temp_Index=1;
for i=1:1:(scan_vertical_pixel+1)
    if Back_flag==0
        for j=1:1:(scan_horizontal_pixel+1)
            image(j,i)=Data_1D(temp_Index);
            temp_Index=temp_Index+1;
        end
        Back_flag=1;
        
        continue 
    end
    if Back_flag==1
        for j=(scan_horizontal_pixel+1):-1:1
            image(j,i)=Data_1D(temp_Index);
            temp_Index=temp_Index+1;
        end
        Back_flag=0;
        
        continue
    end
    
end