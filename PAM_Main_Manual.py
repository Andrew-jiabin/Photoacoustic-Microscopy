# -*- coding: utf-8 -*-
"""
PAM_Main_Manual.py
==================
PAM 手动点位成像程序（无位移台版）。

适用情形：探针位置完全由人手动物理调整，程序不驱动任何电动位移台。
流程：
  1. 启动后询问激光是否已打开（y/n）。
  2. 对每个点（默认 10 个点）：
       - 每间隔 1s 采集一次时域信号，共采 10 次；
       - 采完后询问"是否已手动调整到下一个点"，按 Enter 继续，输入 q 结束。
  3. 全部点采完（默认 10 点 x 10 次 = 100 个时域信号）后保存 .mat。

由 PAM_Main_LBTEK.py 改造而来：移除麓邦位移台控制，仅保留 Alazar 采集与保存。

运行环境：远端 LMX 主机 PAM conda 环境
    C:/Users/20211/.conda/envs/PAM/python.exe PAM_Main_Manual.py
"""

import gc
import os
import sys
import time
import datetime

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy")
import atsapi as ats
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem

# ============================== 1. 参数设置 =================================
MANUAL_POINTS = 21      # 手动采集点数
SHOTS_PER_POINT = 10     # 每点采集次数
ACQ_INTERVAL_S = 1.0     # 两次采集间隔 [s]

# 交互开关：--auto-start 时跳过所有 input（无人值守自动采 10 点）
AUTO_START = False

# --- DAQ 参数 (Alazar ATS9373) ---
DELAY = 1400             # 丢弃的采样点数
SAMPLES_REC = 4096
SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
AVERAGE_ENABLE = True
RECORDS_PER_POINT = 1024  # 每次采集平均的 record 数
Buffer_Count = 4
CHANNEL_A_RANGE = ats.INPUT_RANGE_PM_200_MV

DATA_DIR = "./data"      # 数据保存目录（相对运行目录）


def flush_stdin():
    """清除 stdin 残留按键缓冲（防止误按多次 Enter 被下一次 input() 直接消费）。"""
    try:
        import msvcrt
        while msvcrt.kbhit():
            msvcrt.getch()
    except Exception:
        pass


def ask_input(prompt):
    """input() 封装：先清空残留缓冲再询问，确保每次询问都当场等待用户输入。"""
    flush_stdin()
    return input(prompt)


def main():
    print("=== PAM 手动点位成像（无位移台）===")
    print(f"计划：{MANUAL_POINTS} 点 x 每点 {SHOTS_PER_POINT} 次 "
          f"(间隔 {ACQ_INTERVAL_S}s) = {MANUAL_POINTS*SHOTS_PER_POINT} 个时域信号")

    # --auto-start / PAM_AUTO_START=1 跳过交互
    import argparse
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--auto-start", action="store_true")
    _args, _ = _ap.parse_known_args()
    auto_start = _args.auto_start or AUTO_START or \
        os.environ.get("PAM_AUTO_START", "0").strip().lower() in ("1", "true", "yes")

    # 第一次询问激光是否打开：按 Enter（任意输入）即视为已打开
    if not auto_start:
        ask_input("确认激光已打开后按 Enter 开始采集...")
    else:
        print("⚡ AUTO-START：跳过激光确认（请确认激光已开）")

    # --- 2. 初始化采集卡 ---
    daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY,
                          channel_A_range=CHANNEL_A_RANGE)
    daq.configure_board(sample_rate=SAMPLE_RATE)
    daq.prepare_acquisition(acq_channel=ats.CHANNEL_A,
                            samples_per_record=SAMPLES_REC,
                            records_per_buffer=RECORDS_PER_POINT,
                            buffer_count=Buffer_Count,
                            records_per_point=RECORDS_PER_POINT)
    print("✅ [DAQ] 板卡配置完成")
    gc.disable()

    # --- 3. 逐点采集 ---
    all_data = []   # (point_idx, shot_idx, data)
    try:
        for p in range(1, MANUAL_POINTS + 1):
            print(f"\n===== 第 {p}/{MANUAL_POINTS} 点：开始采集 {SHOTS_PER_POINT} 次 =====")
            for s in range(1, SHOTS_PER_POINT + 1):
                t0 = time.time()
                daq.get_one_acquisition(all_data=all_data,
                                        curr_pos_str=f"p{p}_s{s},0,0",
                                        timeout_ms=2500,
                                        Average_Enable=AVERAGE_ENABLE)
                dt = time.time() - t0
                # 检查数据有效性
                item = all_data[-1]
                ok_flag = not (isinstance(item[0], list) and len(item[0]) == 0)
                print(f"  [第{p}点/第{s}次] 耗时{dt:.2f}s {'OK' if ok_flag else '无信号'}")
                if s < SHOTS_PER_POINT:
                    time.sleep(max(0.0, ACQ_INTERVAL_S - dt))

            # 采完一点，询问是否手动调整到下一点
            if p < MANUAL_POINTS and not auto_start:
                ans = ask_input(f"✅ 第 {p} 点完成（{SHOTS_PER_POINT} 次）。"
                            f"是否已手动调整到第 {p+1} 点？按 Enter 继续，输入 q 结束: ").strip().lower()
                if ans == "q":
                    print("用户选择提前结束。")
                    break
            else:
                print(f"✅ 第 {p} 点完成（{SHOTS_PER_POINT} 次）。")

        print(f"\n采集结束：共 {len(all_data)} 个时域信号。")
    except KeyboardInterrupt:
        print("\n🛑 用户中断采集。")
    except Exception as e:
        print(f"\n❌ 采集错误: {e}")
    finally:
        time.sleep(0.5)
        try:
            gc.enable()
            daq.stop_capture()
        except Exception as e:
            print(f"清理异常: {e}")

        # --- 4. 保存 ---
        if len(all_data) > 0:
            if auto_start:
                save_confirm = "y"
            else:
                save_confirm = ask_input(f"共采集 {len(all_data)} 个时域信号。是否保存数据? (y/n): ").strip().lower()
                while save_confirm not in ("y", "n"):
                    save_confirm = ask_input("请回答 y 或 n: ").strip().lower()
            if save_confirm == "y":
                import scipy.io as sio
                import numpy as np
                mat_dict = {}
                shot_map = {}
                for item in all_data:
                    raw = item[0]
                    key = item[1].split(",")[0]   # "p1_s1"
                    if isinstance(raw, list) and len(raw) == 0:
                        print(f"⚠️ 跳过无效数据 {key}（采集失败）")
                        continue
                    if AVERAGE_ENABLE:
                        mat_dict[key] = (raw / RECORDS_PER_POINT).astype(np.uint16)
                    else:
                        # 非平均模式：raw 可能是多个 buffer 的列表，需拼接
                        if isinstance(raw, list):
                            mat_dict[key] = np.concatenate(raw).astype(np.uint16)
                        else:
                            mat_dict[key] = raw.astype(np.uint16)
                    shot_map[key] = item[1]
                mat_dict["metadata"] = {
                    "mode": "manual_points",
                    "manual_points": MANUAL_POINTS,
                    "shots_per_point": SHOTS_PER_POINT,
                    "acq_interval_s": ACQ_INTERVAL_S,
                    "actual_signals": len(mat_dict) - 1,
                    "shot_map": shot_map,
                    "is_averaged": int(AVERAGE_ENABLE),
                }
                af_fix = ""
                if not auto_start:
                    ans = ask_input("是否需要添加后缀? (y/n): ").strip().lower()
                    if ans == "y":
                        af_fix = ask_input("请输入英文后缀: ").strip().lower()
                os.makedirs(DATA_DIR, exist_ok=True)
                save_path = (f"{DATA_DIR}/{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
                             f"-MANUAL-{SHOTS_PER_POINT}x{RECORDS_PER_POINT}-{af_fix}.mat")
                sio.savemat(save_path, mat_dict)
                print(f"✅ 成功保存 {len(mat_dict)-1} 个时域信号 -> {save_path}")
            else:
                print("⚠️ 用户选择不保存。")
        else:
            print("⚠️ 未采集到数据，跳过保存。")


if __name__ == "__main__":
    main()
