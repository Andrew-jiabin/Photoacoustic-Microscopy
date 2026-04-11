#ifndef MOVERLIBRARY_H
#define MOVERLIBRARY_H

#ifdef __cplusplus
#ifdef MOVER_LIBRARY
#define DLL_EXPORT extern "C" __declspec( dllexport )
#else
#define DLL_EXPORT extern "C" __declspec( dllimport )
#endif
#else
#define DLL_EXPORT
#endif

#define MOVE_CODE_STOP      0x01    // 停止
#define MOVE_CODE_RESTORE   0x02    // 回原点
#define MOVE_CODE_DRIVE_R   0x04    // 向右相对运动
#define MOVE_CODE_DRIVE_L   0x05    // 向左相对运动
#define MOVE_CODE_MOVE      0x06    // 运动到指定位置
#define MOVE_CODE_JOG_R     0x07    // 向右步进
#define MOVE_CODE_JOG_L     0x08    // 向左步进

DLL_EXPORT int listPorts(char *serialNo, unsigned int len); // 列举串口设备
DLL_EXPORT int openEmcvx(char *serialNo);                   // 打开串口设备
DLL_EXPORT int isOpen(char *serialNo);                      // 检查串口设备是否已打开
DLL_EXPORT int closeEmcvx(int handle);                      // 关闭串口设备
DLL_EXPORT int readEmcvx(int handle, char *data, int len);  // 读取数据
DLL_EXPORT int writeEmcvx(int handle, char *data, int len); // 写入数据

// 设置D寄存器
DLL_EXPORT int moveEmcvx(int handle, int ID, int func);              // 运行
DLL_EXPORT int setSpeed(int handle, int ID, float speed);       // 设置速度
DLL_EXPORT int setAcceleration(int handle, int ID, float Acc);  // 设置加速度
DLL_EXPORT int setAbsoluteDisp(int handle, int ID, float disp); // 设置绝对位移量
DLL_EXPORT int setRelativeDisp(int handle, int ID, float disp); // 设置相对位移量
DLL_EXPORT int setJogTime(int handle, int ID, int time);        // 设置步进时间
DLL_EXPORT int setJogStep(int handle, int ID, float step);      // 设置步进步长
DLL_EXPORT int setJogDelay(int handle, int ID, int delay);      // 设置步进延迟

DLL_EXPORT int setInputEnable(int handle, int ID, int enable);              // 输入有效
DLL_EXPORT int setOutputEnable(int handle, int ID, int enable);             // 输出有效
DLL_EXPORT int setAxisEnable(int handle, int ID, int enable);               // 轴使能
DLL_EXPORT int setRelativePosEnable(int handle, int ID, int enable);        // 相对位置值使能

// 只读
DLL_EXPORT int getDoingState(int handle, int ID);           // 运动状态
DLL_EXPORT int getPositiveLimitEnable(int handle, int ID);  // 限位+
DLL_EXPORT int getNegativeLimitEnable(int handle, int ID);  // 限位-
DLL_EXPORT int getOriginEable(int handle, int ID);          // 原点
DLL_EXPORT int getDeviceCode(int handle);                   // 控制器设备型号（支持的轴数量）

// 获取寄存器值
DLL_EXPORT float getSpeed(int handle, int ID);          // 速度
DLL_EXPORT float getAcceleration(int handle, int ID);   // 加速度
DLL_EXPORT float getAbsoluteDisp(int handle, int ID);   // 绝对位移量
DLL_EXPORT float getRelativeDisp(int handle, int ID);   // 相对位移量
DLL_EXPORT int getJogTime(int handle, int ID);          // 步进时间
DLL_EXPORT float getJogStep(int handle, int ID);        // 步进步长
DLL_EXPORT int getJogDelay(int handle, int ID);         // 步进延迟

DLL_EXPORT int getAxisType(int handle, int ID);             // 轴类型
DLL_EXPORT float GetCurrentPos(int handle, int ID, int *ok);// 当前位置
DLL_EXPORT int getInputEnable(int handle, int ID);          // 输入有效
DLL_EXPORT int getOutputEnable(int handle, int ID);         // 输出有效
DLL_EXPORT int getAxisEnable(int handle, int ID);           // 轴使能
DLL_EXPORT int getRelativePosEnable(int handle, int ID);    // 相对位置值使能,未使能时，drive不需要设置位移量

DLL_EXPORT int getAllModels(char* modelName, int len);      // 获取所有位移台型号名称用","隔开
DLL_EXPORT int initAxis(int handle, int ID, char* model, int AxisCount);   // 初始化轴
DLL_EXPORT int getErrorCode(int handle, int ID);            // 获取错误代码 ,9025轴参数错误

#endif // MOVERLIBRARY_H
