from LBMover import LBMover
import ctypes
import time

def run_comprehensive_test(port_name="COM6"):
    # 实例化类
    mv = LBMover()
    
    print(f"--- 启动硬件自检: {port_name} ---")
    
    # 1. 连接测试
    handle = mv.openEmcvx(port_name.encode('utf-8'))
    if handle < 0:
        print("错误：无法打开串口")
        return
    mv.handle = handle # 存入实例供后续使用

    try:
        # 2. 控制器信息测试
        axis_count = mv.getDeviceCode(handle)
        print(f"控制器支持轴数: {axis_count}")

        # 3. 获取型号列表
        buf = ctypes.create_string_buffer(1024)
        num_models = mv.getAllModels(buf, 1024)
        models = buf.value.decode('utf-8')
        print(f"支持的型号数量: {num_models}, 型号名: {models}")

        # 4. 初始化轴 (假设我们要操作轴 0，使用第一个型号名)
        if num_models > 0:
            first_model = models.split(',')[0]
            res = mv.initAxis(handle, 0, first_model.encode('utf-8'), axis_count)
            print(f"初始化轴 0 ({first_model}): {'成功' if res==0 else '失败'}")

        # 5. 读取/设置速度测试
        old_speed = mv.getSpeed(handle, 0)
        mv.setSpeed(handle, 0, 5.0)
        new_speed = mv.getSpeed(handle, 0)
        print(f"速度测试: 原速 {old_speed} -> 现速 {new_speed}")

        # 6. 获取限位状态
        p_limit = mv.getPositiveLimitEnable(handle, 0)
        n_limit = mv.getNegativeLimitEnable(handle, 0)
        print(f"限位检查: 正向={p_limit}, 负向={n_limit}")

        # 7. 位置读取测试
        pos, ok = mv.get_pos(0)
        print(f"当前位置: {pos}, 读取状态: {ok}")

        # 8. 错误码检查
        err = mv.getErrorCode(handle, 0)
        print(f"当前轴错误码 (0为正常): {err}")

    except Exception as e:
        print(f"测试过程中发生意外: {e}")
    finally:
        # 9. 关闭
        mv.closeEmcvx(handle)
        print("--- 自检结束，句柄已释放 ---")

if __name__ == "__main__":
    # 你可以在这里输入你确定的 COM 口
    run_comprehensive_test("COM6")