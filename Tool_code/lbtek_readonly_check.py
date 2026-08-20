# -*- coding: utf-8 -*-
"""
lbtek_readonly_check.py
========================
麓邦一维位移台【只读】检查脚本：枚举串口 -> 连接 -> 初始化 -> 读参数。
不执行任何移动指令，可安全随时运行。
"""

import ctypes
import os
import sys

# 远端 Windows 终端默认 GBK，防止 emoji/特殊字符导致 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "Alazar_imaging")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from LBMover import LBMover  # noqa: E402

DLL_PATH = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\LBTEK_SDK\x64\moverLibrary.dll"
MODEL_NAME = b"EM-LSS65-13C1"


def main():
    mv = LBMover(DLL_PATH)

    # 1. 枚举串口
    buf = ctypes.create_string_buffer(1024)
    n = mv.listPorts(buf, 1024)
    ports = buf.value.decode("utf-8", errors="ignore").strip()
    print(f"[枚举] listPorts 返回 {n} 个设备: {ports!r}")

    if not ports:
        print("❌ 未找到麓邦控制器串口。请确认设备供电与 USB 连接。")
        sys.exit(1)

    # 2. 逐个尝试连接（只读，不移动）
    for cand in ports.split(","):
        cand = cand.strip()
        if not cand:
            continue
        port = cand.upper() if cand.upper().startswith("COM") else f"COM{cand}"
        handle = mv.openEmcvx(port.encode("utf-8"))
        if handle < 0:
            print(f"  - {port}: 打开失败 handle={handle}")
            continue
        print(f"  - {port}: ✅ 打开成功 handle={handle}")
        mv.handle = handle
        try:
            axis_count = mv.getDeviceCode(handle)
            print(f"    getDeviceCode -> 轴数={axis_count}")
            res = mv.initAxis(handle, 0, MODEL_NAME, axis_count)
            print(f"    initAxis({MODEL_NAME.decode()}, axis_count={axis_count}): "
                  f"{'✅' if res == 0 else f'❌ {res}'}")
            if res != 0:
                mv.closeEmcvx(handle)
                continue

            mv.setAxisEnable(handle, 0, 1)
            p_lim = mv.getPositiveLimitEnable(handle, 0)
            n_lim = mv.getNegativeLimitEnable(handle, 0)
            origin = mv.getOriginEable(handle, 0)
            err = mv.getErrorCode(handle, 0)
            pos, ok = mv.get_pos(0)
            speed = mv.getSpeed(handle, 0)
            acc = mv.getAcceleration(handle, 0)
            disp = mv.getAbsoluteDisp(handle, 0)
            print(f"    轴数={axis_count} 正限位={p_lim} 负限位={n_lim} 原点={origin} 错误码={err}")
            print(f"    位置={pos if pos is not None else 'N/A'} mm (ok={ok})")
            print(f"    速度={speed} mm/s 加速度={acc} 当前绝对目标={disp}")
            print("✅ 只读检查完成，未执行任何移动。")
        finally:
            mv.closeEmcvx(handle)
        break


if __name__ == "__main__":
    main()
