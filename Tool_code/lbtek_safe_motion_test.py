# -*- coding: utf-8 -*-
"""
lbtek_safe_motion_test.py  v2（修复版）
====================================
麓邦一维位移台（EM-LSS65-13C1, COM11）安全实机测试。
修复要点（来自实机调试）：
  1. 轴 ID 必须为 1（官方示例 currentIndex+1）。
  2. 不使用 setInputEnable/setOutputEnable/setRelativePosEnable（曾导致控制器 9024 锁定）。
  3. 严格照官方序列：open -> getDeviceCode -> initAxis -> setAbsoluteDisp -> moveEmcvx。
  4. 加速度配置为尽可能低（0.1 起尝试，读回生效值）。
安全设计：绝对位移 + 到位校验 + 超差即停；测试范围 [base-1, base+1] mm。

运行（远端 PAM 环境）：
  C:/Users/20211/.conda/envs/PAM/python.exe Tool_code/lbtek_safe_motion_test.py
"""

import ctypes
import os
import sys
import time

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
PORT = "COM11"          # 麓邦控制器串口（CH340）
AXIS_ID = 1             # 官方示例: 轴 ID 从 1 开始

# 到位判据
POS_TOLERANCE_MM = 0.01
SETTLE_TIME_S = 2.0     # 实测：move 后需 ~2s 台子完全稳定（0.3s 读数不准）
MOVE_TIMEOUT_S = 10.0

# 安全上限（硬编码，不允许参数覆盖）
HARD_MAX_OFFSET_MM = 1.0
MAX_OFFSET_MM = 1.0     # 用户要求的 1mm 范围

# 运动参数（实测标定 2026-08-20）：
#   acc=0.5 mm/s2 是能完成小行程(0.5mm)的最小加速度；0.1 时加速距离>行程无法到位。
#   速度 2.0 mm/s 与 acc 0.5 组合实测到位误差 0.0000mm。
ACC_MM_S2 = 0.5
SPEED_MM_S = 2.0


def read_pos(mv, handle, axis=AXIS_ID):
    pos, ok = mv.get_pos(axis)
    return pos, ok


def move_abs_and_verify(mv, handle, target, label, retries=1):
    """绝对移动到 target，等待到位并验证误差。超差自动重试。返回 (实际位置, 误差mm)。"""
    for attempt in range(retries + 1):
        mv.setAbsoluteDisp(handle, AXIS_ID, target)
        mv.moveEmcvx(handle, AXIS_ID, 0x06)  # MOVE_CODE_MOVE
        start_t = time.time()
        while mv.getDoingState(handle, AXIS_ID) == 1:
            time.sleep(0.01)
            if time.time() - start_t > MOVE_TIMEOUT_S:
                print(f"  [{label}] 移动超时! 目标={target:.4f}")
                return None, None
        time.sleep(SETTLE_TIME_S)
        actual, _ = read_pos(mv, handle)
        if actual is None:
            print(f"  [{label}] 读位置失败")
            return None, None
        err = abs(actual - target)
        if err <= POS_TOLERANCE_MM or attempt == retries:
            status = "OK" if err <= POS_TOLERANCE_MM else "WARN"
            print(f"  [{status}] [{label}] 目标={target:+.4f} 实际={actual:+.4f} "
                  f"误差={err:.4f} mm 耗时={time.time()-start_t:.2f}s"
                  f"{' (重试)' if attempt else ''}")
            return actual, err
        print(f"  [RETRY] [{label}] 误差 {err:.4f} > {POS_TOLERANCE_MM}，重试...")
        time.sleep(1.0)
    return None, None


def main():
    print("=== 麓邦一维台安全实机测试 v2 (1mm 范围) ===")
    mv = LBMover(DLL_PATH)

    h = mv.openEmcvx(PORT.encode("utf-8"))
    print(f"openEmcvx({PORT}) -> {h}")
    if h < 0:
        print("打开失败：请确认串口空闲（官方软件已关闭）")
        sys.exit(1)
    mv.handle = h
    try:
        axis_count = mv.getDeviceCode(h)
        rc = mv.initAxis(h, AXIS_ID, MODEL_NAME, axis_count)
        print(f"getDeviceCode={axis_count} initAxis(ID={AXIS_ID}) -> {rc}")
        if rc != 0:
            sys.exit(1)
        time.sleep(0.3)

        pos0, ok0 = read_pos(mv, h)
        print(f"当前位置 = {pos0} mm (ok={ok0}) err={mv.getErrorCode(h, AXIS_ID)}")
        if pos0 is None:
            sys.exit(1)
        base = pos0
        print(f"基准 base={base:.4f} mm，测试范围 [{base-MAX_OFFSET_MM:.4f}, {base+MAX_OFFSET_MM:.4f}] mm")

        # 配置速度/加速度（实测标定值）
        old_speed = mv.getSpeed(h, AXIS_ID)
        old_acc = mv.getAcceleration(h, AXIS_ID)
        print(f"原参数: 速度={old_speed} 加速度={old_acc}")
        rc_s = mv.setSpeed(h, AXIS_ID, SPEED_MM_S)
        new_speed = mv.getSpeed(h, AXIS_ID)
        print(f"设置速度 {SPEED_MM_S} (rc={rc_s}) -> 读回 {new_speed}")
        rc_a = mv.setAcceleration(h, AXIS_ID, ACC_MM_S2)
        acc_eff = mv.getAcceleration(h, AXIS_ID)
        print(f"设置加速度 {ACC_MM_S2} (rc={rc_a}) -> 读回 {acc_eff}")
        print(f"最终生效: 速度={new_speed} mm/s, 加速度={acc_eff} mm/s2")

        # 测试序列：从最小起步，逐步扩大到 ±1mm
        offsets = sorted(set([0.1, -0.1, 0.5, -0.5, MAX_OFFSET_MM, -MAX_OFFSET_MM]),
                         key=abs)
        print(f"\n测试序列偏移(mm): {offsets}")
        results = []
        for k, off in enumerate(offsets):
            if abs(off) > HARD_MAX_OFFSET_MM + 1e-9:
                continue
            label = f"{k+1}/{len(offsets)} {off:+.1f}mm"
            target = base + off
            actual, err_ = move_abs_and_verify(mv, h, target, label)
            if actual is None:
                mv.moveEmcvx(h, AXIS_ID, 0x01)  # STOP
                print("该步失败，急停。")
                break
            results.append((off, actual, err_))
            # 回到基准
            _, e0 = move_abs_and_verify(mv, h, base, f"{label} 回基准")
            if e0 is not None and e0 > POS_TOLERANCE_MM:
                print("回基准超差，终止。")
                break

        # 汇总
        print("\n=== 测试汇总 ===")
        worst = max((r[2] for r in results), default=0.0)
        for off, actual, err_ in results:
            mark = "OK " if err_ <= POS_TOLERANCE_MM else "FAIL"
            print(f"  [{mark}] 偏移{off:+.2f}mm -> 实际{actual:+.4f} 误差{err_:.4f}mm")
        print(f"最大误差: {worst:.4f} mm (容忍 {POS_TOLERANCE_MM} mm)")
        print(f"总体判定: {'PASS - 1mm 范围扫描可用' if worst <= POS_TOLERANCE_MM else 'FAIL - 需排查'}")
        print(f"生效参数: 速度={new_speed} mm/s, 加速度={acc_eff} mm/s2")

        # 收尾回基准
        _, _ = move_abs_and_verify(mv, h, base, "收尾回基准")
    finally:
        mv.closeEmcvx(h)
        print("串口已关闭。")


if __name__ == "__main__":
    main()
