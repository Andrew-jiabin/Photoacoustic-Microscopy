# -*- coding: utf-8 -*-
"""
daq_smoke_test.py
=================
DAQ 冒烟测试：仅初始化 Alazar 采集卡并采集 3 个点，验证激光触发链路。
不移动位移台。数据非空且有信号（max > 阈值）即视为通过。
"""
import os
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

sys.path.insert(0, r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy")
import atsapi as ats
from Alazar_imaging.AlazarNPTSystem import AlazarNPTSystem

DELAY = 1600
SAMPLES_REC = 4096
SAMPLE_RATE = ats.SAMPLE_RATE_4000MSPS
RECORDS_PER_POINT = 256
Buffer_Count = 4
CHANNEL_A_RANGE = ats.INPUT_RANGE_PM_200_MV


def main():
    print("=== DAQ 冒烟测试（3 点，不移动） ===")
    daq = AlazarNPTSystem(systemId=1, boardId=1, Delay=DELAY,
                          channel_A_range=CHANNEL_A_RANGE)
    daq.configure_board(sample_rate=SAMPLE_RATE)
    daq.prepare_acquisition(acq_channel=ats.CHANNEL_A,
                            samples_per_record=SAMPLES_REC,
                            records_per_buffer=RECORDS_PER_POINT,
                            buffer_count=Buffer_Count,
                            records_per_point=RECORDS_PER_POINT)
    print("板卡配置完成")

    all_data = []
    import numpy as np
    ok_points = 0
    for i in range(3):
        t0 = time.time()
        daq.get_one_acquisition(all_data=all_data, curr_pos_str=f"{i},0,0",
                                timeout_ms=2500, Average_Enable=True)
        dt = time.time() - t0
        item = all_data[-1]
        data = item[0]
        if isinstance(data, list) and len(data) == 0:
            print(f"  [点{i}] ❌ 无数据（超时/无触发）耗时{dt:.2f}s")
            continue
        arr = data
        print(f"  [点{i}] ✅ 数据 shape={arr.shape} min={arr.min()} max={arr.max()} "
              f"mean={arr.mean():.1f} 耗时{dt:.2f}s")
        if arr.max() > 50:   # 有信号阈值（uint16 平均后）
            ok_points += 1

    print(f"\n有信号点数: {ok_points}/3")
    if ok_points >= 1:
        print("✅ 冒烟通过：DAQ 链路正常（有激光触发信号）")
        # 数据总览
        n0 = all_data[-1][0]
        print(f"波形前 20 点: {n0[:20].tolist()}")
        return 0
    else:
        print("❌ 冒烟失败：未采到信号（检查激光是否开启/触发接线）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
