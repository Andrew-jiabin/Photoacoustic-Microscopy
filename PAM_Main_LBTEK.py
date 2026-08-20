# -*- coding: utf-8 -*-
"""
PAM_Main_LBTEK.py
=================
一维位移台（麓邦 LBTEK EM-LSS65-13C1）专用 PAM 扫描成像主程序。

本程序【仅】用于一维单线扫描成像，不包含任何 Prior 位移台代码：
- 控制链路：麓邦 LBMover（moverLibrary.dll）-> EM-LSS65-13C1 一维台
- 采集链路：Alazar ATS9373（外部激光触发，NPT 模式逐点采集）
- 扫描方式：以当前位置为中点 M，先向左移动 d 到左端点 L=M-d，
  再从左向右扫描总宽 2d（L->M->R，默认 d=0.5mm / 10µm 步长 / 101 点），
  逐点移动 -> 到位校验 -> 采集，扫完回到中点。
- 安全设计：速度/加速度默认低值（实测标定 acc=0.5 mm/s2）；
  MAX_MOVE_MM 单步安全护栏；异常/中断自动急停并回起点。

实测依据见仓库根目录 `LBTEK_1D_STAGE_NOTES.md`（2026-08-20 实机验证）。

运行环境：远端 LMX 主机 PAM conda 环境
    C:/Users/20211/.conda/envs/PAM/python.exe PAM_Main_LBTEK.py
"""

import gc
import os
import sys
import time
import datetime

import atsapi as ats

from Alazar_imaging.LBMover import LBMover
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem
from Alazar_imaging.AsyncProgress import progress_manager
from Alazar_imaging.Alazar_imaging_tools import lbtek_wait_settled
from Tool_code.position_trans import sanitize_pos_to_key

import numpy as np
import scipy.io as sio  # 用于保存 mat 文件

# 远端 Windows 终端默认 GBK，防止 emoji/特殊字符导致 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


# ============================== 1. 参数设置 =================================
LB_DLL_PATH = r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\LBTEK_SDK\x64\moverLibrary.dll"
MODEL_NAME = b"EM-LSS65-13C1"

# 麓邦控制器串口（CH340）。留空则自动遍历 listPorts 找能识别的设备。
COM_PORT = ""            # 例: "11" 代表 COM11。留空 = 自动识别
AXIS_ID = 1              # 官方 SDK 示例：轴 ID 从 1 开始（用 0 会读到无效位置/无法运动）

# --- 运动参数（单位 mm） ---
# 中点对称扫描：运行前请把探针手动移到成像轨迹的中点 M。
# 程序先向左移动 SCAN_HALF_RANGE_MM 到左端点，再向右扫描总宽度 2*SCAN_HALF_RANGE_MM（覆盖 M±d）。
SCAN_HALF_RANGE_MM = 0.5   # 给定距离 d：从中点向左移动 d 到左端；扫描总宽 = 2d
STEP_UM = 10               # 步长 [µm]；10µm = 0.01mm
SCAN_N_POINTS = int(2 * SCAN_HALF_RANGE_MM * 1000 / STEP_UM) + 1   # 101 点（含两端）
MAX_MOVE_MM = 1.5          # 安全护栏：任意单次移动距离上限 [mm]（>= 第一跳 d + 余量）
MAX_POS_ABS_MM = 14.0      # 行程软检查：|端点坐标| 超过此值拒绝（行程 <30mm 留余量）

# 交互开关：SSH/自动化运行时可跳过 input 等待（--auto-start 或环境变量 PAM_AUTO_START=1）
AUTO_START = False

# --- 速度 / 加速度（麓邦单位：mm/s 与 mm/s^2） ---
# 2026-08-20 实机标定：acc=0.5 mm/s2 是能完成 0.5mm 小行程的最小加速度
# （0.1 时加速距离 5mm >> 行程，无法到位）；±0.5mm 内一次到位，±1mm 需重试。
SPEED_MM_S = 2.0         # 扫描速度：实测到位精度 0.0000mm
ACCEL_MM_S2 = 0.5        # 尽可能低的可用加速度
FIRST_JUMP_SPEED_MM_S = 0.5  # 第一跳（中点->左端）低速，避免长距离快速移动产生震动

SETTLE_TIME_S = 2.0      # 兜底超时：自适应稳定检测最大等待（10um 步进实测 ~0.35s 到位）
SETTLE_STABLE_S = 0.15   # 稳定判据：位置连续 ~0.15s 无变化即视为静止


def wait_settled_adaptive(stage, target_mm, timeout_s=None, pos_tol=0.01):
    """自适应到位等待：轮询位置直到 |pos-target|<=tol 且连续无变化。
    10um 步进实测 ~0.35s 到位；大步长自动等待更久；超时返回 False（由调用方重试）。
    相比固定 sleep 2s：101 点扫描从 ~4min 提速到 ~2min。"""
    if timeout_s is None:
        timeout_s = SETTLE_TIME_S
    t0 = time.time()
    last_pos = None
    stable_s = 0.0
    while time.time() - t0 < timeout_s:
        pos, _ = stage.get_pos(AXIS_ID)
        if pos is not None:
            if abs(pos - target_mm) <= pos_tol:
                if last_pos is not None and abs(pos - last_pos) <= 0.001:
                    stable_s += 0.05
                    if stable_s >= SETTLE_STABLE_S:
                        return True
                else:
                    stable_s = 0.0
            else:
                stable_s = 0.0
            last_pos = pos
        time.sleep(0.05)
    return False

# --- DAQ 参数 (Alazar ATS9373) ---
DELAY = 1600             # 丢弃的采样点数
SAMPLES_REC = 4096
SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
AVERAGE_ENABLE = True
RECORDS_PER_POINT = 256  # 每个点采集 record 数（平均模式）
Buffer_Count = 4
CHANNEL_A_RANGE = ats.INPUT_RANGE_PM_200_MV

DATA_DIR = "./data"      # 数据保存目录（相对运行目录）


def resolve_com_port(mover):
    """自动识别麓邦控制器串口：遍历 listPorts，返回第一个 getDeviceCode>0 的端口。
    注意：listPorts 会列出所有串口（含 ELMO/Prior 等），必须按设备码过滤。
    返回格式统一为 "COMx"（openEmcvx 必须带 COM 前缀）。"""
    if COM_PORT:
        p = COM_PORT.strip().upper()
        return p if p.startswith("COM") else f"COM{p}"
    try:
        import ctypes
        buf = ctypes.create_string_buffer(1024)
        n = mover.listPorts(buf, 1024)
        ports = buf.value.decode("utf-8", errors="ignore").strip()
        print(f"[枚举] listPorts 返回 {n} 个设备: {ports!r}")
        for cand in ports.split(","):
            cand = cand.strip()
            if not cand:
                continue
            port = cand.upper() if cand.upper().startswith("COM") else f"COM{cand}"
            h = mover.openEmcvx(port.encode("utf-8"))
            if h >= 0:
                dc = mover.getDeviceCode(h)
                mover.closeEmcvx(h)
                time.sleep(0.5)   # CH340 释放串口需要时间，避免立即重开失败
                if dc > 0:
                    print(f"[枚举] 找到麓邦控制器: {port} (deviceCode={dc})")
                    return port    # 返回完整 "COMx" 格式（openEmcvx 必须带 COM 前缀）
        print("⚠️  未找到麓邦控制器，请手动设置 COM_PORT。")
    except Exception as e:
        print(f"[枚举] 失败: {e}")
    return None


def main():
    # === 2. 初始化硬件 ===
    stage = LBMover(LB_DLL_PATH)
    port = resolve_com_port(stage)
    if port is None:
        raise RuntimeError("无法确定麓邦位移台串口")

    handle = -1
    for attempt in range(3):   # 重试 3 次，规避串口释放竞态
        handle = stage.openEmcvx(port.encode("utf-8"))
        if handle >= 0:
            break
        print(f"[连接] {port} 打开失败(attempt={attempt+1}), 1s 后重试...")
        time.sleep(1.0)
    if handle < 0:
        raise RuntimeError(f"无法打开麓邦位移台串口 {port} (handle={handle})")
    stage.handle = handle
    print(f"✅ 麓邦位移台已连接: {port}, handle={handle}")

    # 初始化轴（官方示例：ID 从 1 开始，axisCount 用 getDeviceCode 返回值）
    axis_count = stage.getDeviceCode(handle)
    res = stage.initAxis(handle, AXIS_ID, MODEL_NAME, axis_count)
    if res == 0:
        print(f"✅ 型号 {MODEL_NAME.decode()} 初始化成功 (axisCount={axis_count})")
    else:
        print(f"❌ 初始化失败，错误码: {res}")
        stage.closeEmcvx(handle)
        raise RuntimeError(f"initAxis 失败: {res}")

    # --- 3. 配置速度 / 加速度（读回确认） ---
    old_speed = stage.getSpeed(handle, AXIS_ID)
    old_acc = stage.getAcceleration(handle, AXIS_ID)
    print(f"[参数] 原速度={old_speed} 原加速度={old_acc}")

    r_speed = stage.setSpeed(handle, AXIS_ID, SPEED_MM_S)
    r_acc = stage.setAcceleration(handle, AXIS_ID, ACCEL_MM_S2)
    new_speed = stage.getSpeed(handle, AXIS_ID)
    new_acc = stage.getAcceleration(handle, AXIS_ID)
    print(f"[参数] setSpeed 返回={r_speed} -> 读回 {new_speed}")
    print(f"[参数] setAcceleration 返回={r_acc} -> 读回 {new_acc}")

    # 当前位置（真实读数，ID=1）
    start_pos, ok = stage.get_pos(AXIS_ID)
    print(f"[参数] 起始位置 = {start_pos:.4f} mm (读回状态 {ok})")

    daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY,
                          channel_A_range=CHANNEL_A_RANGE)
    daq.configure_board(sample_rate=SAMPLE_RATE)
    daq.prepare_acquisition(acq_channel=ats.CHANNEL_A,
                            samples_per_record=SAMPLES_REC,
                            records_per_buffer=RECORDS_PER_POINT,
                            buffer_count=Buffer_Count,
                            records_per_point=RECORDS_PER_POINT)

    gc.disable()
    # 启动确认：--auto-start / PAM_AUTO_START=1 时跳过交互等待（SSH 自动化用）
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--auto-start", action="store_true",
                     help="跳过 Enter 等待与保存询问（自动化运行）")
    _args, _ = _ap.parse_known_args()
    auto_start = _args.auto_start or AUTO_START or \
        os.environ.get("PAM_AUTO_START", "0").strip().lower() in ("1", "true", "yes")
    if not auto_start:
        input("Press Enter to START Experiment... (确保激光器已开)")
    else:
        print("⚡ AUTO-START 模式：跳过启动确认（请确认激光器已开）")

    # === 4. 生成一维扫描轨迹（中点对称：当前位置 = 用户放置的中点 M） ===
    if SCAN_N_POINTS < 2:
        raise ValueError("SCAN_N_POINTS 必须 >= 2")
    mid_pos = start_pos                              # 中点 M = 当前位置（用户已手动放置）
    left_pos = mid_pos - SCAN_HALF_RANGE_MM          # 左端点 L = M - d
    right_pos = mid_pos + SCAN_HALF_RANGE_MM         # 右端点 R = M + d
    # 行程软检查（开环无行程读数，|端点| 超限即拒绝）
    for _p, _name in ((left_pos, "左端"), (right_pos, "右端")):
        if abs(_p) > MAX_POS_ABS_MM:
            raise RuntimeError(f"轨迹{_name} {_p:.3f} mm 超出安全行程 ±{MAX_POS_ABS_MM} mm，已拒绝启动")
    step_mm = (2 * SCAN_HALF_RANGE_MM) / (SCAN_N_POINTS - 1)
    if step_mm > MAX_MOVE_MM:
        raise RuntimeError(f"单步 {step_mm:.4f} mm 超过安全上限 {MAX_MOVE_MM} mm，已拒绝启动")
    if SCAN_HALF_RANGE_MM > MAX_MOVE_MM:
        raise RuntimeError(f"第一跳距离 {SCAN_HALF_RANGE_MM:.3f} mm 超过安全上限 {MAX_MOVE_MM} mm")

    # 先从中点向左移动 d 到左端点（低速，防震动）
    print(f"[轨迹] 中点 M={mid_pos:.4f} mm，低速 {FIRST_JUMP_SPEED_MM_S} mm/s 向左移动 "
          f"{SCAN_HALF_RANGE_MM:.3f} mm 到左端 L={left_pos:.4f}")
    stage.setSpeed(stage.handle, AXIS_ID, FIRST_JUMP_SPEED_MM_S)
    stage.setAbsoluteDisp(stage.handle, AXIS_ID, left_pos)
    stage.moveEmcvx(stage.handle, AXIS_ID, 0x06)
    if not wait_settled_adaptive(stage, left_pos):
        print("⚠️ 第一跳未稳定，重试一次...")
        stage.setAbsoluteDisp(stage.handle, AXIS_ID, left_pos)
        stage.moveEmcvx(stage.handle, AXIS_ID, 0x06)
        wait_settled_adaptive(stage, left_pos)
    # 恢复扫描速度
    stage.setSpeed(stage.handle, AXIS_ID, SPEED_MM_S)

    # 从左端点向右扫描，总宽 2d（L -> M -> R）
    trajectory = [left_pos + i * step_mm for i in range(SCAN_N_POINTS)]
    print(f"[轨迹] 单线扫描: {SCAN_N_POINTS} 点, 步长 {step_mm:.4f} mm, "
          f"范围 [{trajectory[0]:.4f}, {trajectory[-1]:.4f}] mm (总宽 {2*SCAN_HALF_RANGE_MM:.3f} mm)")

    # === 5. 开始实验 ===
    all_data = []
    try:
        progress_manager.start(total=len(trajectory), desc="🚀 1D LBTEK PAM Scanning")
        for i, tx in enumerate(trajectory):
            # A. 指令位移台移动（绝对位置，带到位校验+重试）
            stage.setAbsoluteDisp(stage.handle, AXIS_ID, tx)
            stage.moveEmcvx(stage.handle, AXIS_ID, 0x06)  # MOVE_CODE_MOVE
            # B. 自适应到位等待（到位且稳定即继续，10um 步进约 0.5s）
            ok_settle = wait_settled_adaptive(stage, tx)

            # C. 到位校验（未到位/未稳定则重试一次）
            if not ok_settle:
                stage.setAbsoluteDisp(stage.handle, AXIS_ID, tx)
                stage.moveEmcvx(stage.handle, AXIS_ID, 0x06)
                ok_settle = wait_settled_adaptive(stage, tx)

            current_pos_str = f"{tx:.4f},0,0"       # 格式化坐标字符串用于保存
            # D. 采集数据
            daq.get_one_acquisition(all_data=all_data, curr_pos_str=current_pos_str,
                                    timeout_ms=2500, Average_Enable=AVERAGE_ENABLE)
            progress_manager.update(1)

        # 回到中点（自适应到位）
        stage.setAbsoluteDisp(stage.handle, AXIS_ID, start_pos)
        stage.moveEmcvx(stage.handle, AXIS_ID, 0x06)
        wait_settled_adaptive(stage, start_pos)
        actual, _ = stage.get_pos(AXIS_ID)
        print(f"✅ 已回到中点 {start_pos:.4f} mm (实际 {actual if actual is not None else 'N/A'})")

    except KeyboardInterrupt:
        print("\n🛑 用户终止")
        stage.moveEmcvx(stage.handle, AXIS_ID, 0x01)  # STOP
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        stage.moveEmcvx(stage.handle, AXIS_ID, 0x01)  # STOP
        try:
            stage.setAbsoluteDisp(stage.handle, AXIS_ID, start_pos)
            stage.moveEmcvx(stage.handle, AXIS_ID, 0x06)
            lbtek_wait_settled(stage, stage.handle, AXIS_ID)
        except Exception:
            pass
    finally:
        time.sleep(0.5)
        # === 6. 清理与保存 ===
        try:
            gc.enable()
            daq.stop_capture()
            progress_manager.set_colour("green")
            progress_manager.stop()
        except Exception as e:
            print(f"\n❌ 清理异常: {e}")

        if len(all_data) > 0:
            if auto_start:
                save_confirm = "y"   # 自动化：直接保存，不询问
            else:
                save_confirm = input(f"\n实验完成，共采集 {len(all_data)} 个点。是否保存数据? (y/n): ").strip().lower()
                while save_confirm not in ("y", "n"):
                    save_confirm = input("请回答 y 或 n: ").strip().lower()
            if save_confirm == "y":
                print("💾 正在处理并保存数据...")
                mat_dict = {}
                index_to_pos = []
                try:
                    for item in all_data:
                        raw_data_content = item[0]
                        original_pos_str = item[1]

                        # 保护：采集失败的点（空 buffer 列表）跳过，不破坏整体保存
                        if isinstance(raw_data_content, list) and len(raw_data_content) == 0:
                            print(f"⚠️ 跳过无效数据点 {original_pos_str}（该点采集失败）")
                            continue

                        safe_key = sanitize_pos_to_key(original_pos_str)

                        if AVERAGE_ENABLE:
                            processed_data = (raw_data_content / RECORDS_PER_POINT).astype(np.uint16)
                        else:
                            if isinstance(raw_data_content, list):
                                processed_data = np.concatenate(raw_data_content).astype(np.uint16)
                            else:
                                processed_data = raw_data_content.astype(np.uint16)

                        mat_dict[safe_key] = processed_data
                        index_to_pos.append(original_pos_str)

                    mat_dict["metadata"] = {
                        "scan_points": SCAN_N_POINTS,
                        "half_range_mm": SCAN_HALF_RANGE_MM,
                        "total_range_mm": 2 * SCAN_HALF_RANGE_MM,
                        "mid_pos_mm": mid_pos,
                        "step_mm": step_mm,
                        "pos_list": index_to_pos,
                        "is_averaged": int(AVERAGE_ENABLE),
                        "speed_mm_s": new_speed,
                        "accel_mm_s2": new_acc,
                    }
                    af_fix = ""
                    if auto_start:
                        save_confirm = "n"   # 自动化：不询问后缀，直接保存默认文件名
                    else:
                        save_confirm = input("是否需要添加后缀? (y/n): ").strip().lower()
                        while save_confirm not in ("y", "n"):
                            save_confirm = input("请回答 y 或 n: ").strip().lower()
                        if save_confirm == "y":
                            af_fix = input("请输入英文后缀: ").strip().lower()

                    os.makedirs(DATA_DIR, exist_ok=True)
                    save_path = (f"{DATA_DIR}/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
                                 f"-D-{DELAY}-AVER-{RECORDS_PER_POINT}-{af_fix}.mat")
                    sio.savemat(save_path, mat_dict)
                    print(f"\n✅ 成功保存！共计 {len(mat_dict) - 1} 个坐标位点数据。")
                    print(f"   起始位置 {start_pos:.4f} mm，已归位。")
                    print(f"   数据保存至 {save_path}")
                except Exception as e:
                    print(f"❌ 数据封装失败: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print("⚠️ 用户选择不保存数据，数据已丢弃。")
        else:
            print("⚠️ 未采集到任何有效数据，跳过保存。")

        # 关闭串口
        try:
            stage.closeEmcvx(stage.handle)
        except Exception:
            pass


if __name__ == "__main__":
    main()
