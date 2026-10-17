# -*- coding: utf-8 -*-
"""Verify that an AFM-mode scan file satisfies the existing .mat contract.

The point of this test is not the AFM engine -- that is covered by
``test_afm_offline.py``.  It is the hand-off to MATLAB: a file written by
``PAM_Main_Nanomax_AFM.py`` must be readable by
``MATLAB/PAM_Reconstruction_4.m`` with no changes, which means

* one top-level variable per pixel, named ``P_<x>_<y>_<z>`` with '.' -> 'p'
  and '-' -> 'n';
* every variable ``(1, 4096)`` uint16;
* a ``metadata`` struct carrying ``scan_shape``, ``step_size``, ``pos_list``,
  ``records_per_point``, ``is_averaged`` and the AFM extras.

Run:  python Tool_code/test_afm_mat_contract.py [--out PATH]
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import scipy.io as sio

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from Nanomax.data_io import build_scan_mat_dict  # noqa: E402


SAMPLES = 4096
RECORDS = 256
SCAN_W, SCAN_H = 11, 11
STEP_UM = 1.0
START_X, START_Y, START_Z = -5000.0, 250.0, 0.0


def synthetic_payload(row, col):
    """A (1, 4096) summed-record array whose amplitude encodes the pixel."""
    n = np.arange(SAMPLES, dtype=np.float64)
    # 10 MHz sampled at 4 GS/s -> 400 samples per period.
    carrier = np.sin(2 * np.pi * n / 400.0 + 0.1 * row)
    envelope = np.exp(-((n - 1200.0) ** 2) / (2 * 300.0 ** 2))
    peak_codes = 800.0 + 40.0 * row + 30.0 * col
    signal = peak_codes * envelope * carrier
    summed = np.clip(signal + 2048.0, 0, 4095) * RECORDS
    return summed.astype(np.uint32).reshape(1, SAMPLES)


def build_dataset():
    all_data = []
    afm_z = []
    for row in range(SCAN_H):
        for col in range(SCAN_W):
            x = START_X + col * STEP_UM
            y = START_Y + row * STEP_UM
            # A tilted surface: Z rises 20 nm per row.
            z = START_Z + 0.02 * row
            pos_str = "%.2f,%.2f,%.2f" % (x, y, z)
            meta = {
                "actual_pos_str": pos_str,
                "position_settle_ok": True,
                "position_timeout": False,
                "position_error_x_um": 0.0,
                "position_error_y_um": 0.0,
                "position_error_z_um": 0.0,
            }
            all_data.append([synthetic_payload(row, col), pos_str, meta])
            afm_z.append(round(z, 4))

    stage_metadata = {
        "probe_stage": "none",
        "probe_controller": "none",
        "sample_unit_um_per_unit": 0.01,
        "sample_min_step_um": 0.01,
        "afm_mode": 1,
        "afm_feedback": "demod",
        "afm_setpoint": 0.5,
        "afm_setpoint_unit": "V",
        "afm_f0_hz": 10e6,
        "afm_band_hz": 100e3,
        "afm_approach_sign": 1.0,
        "afm_coarse_step_um": 0.1,
        "afm_fine_step_um": 0.02,
        "afm_retract_um": 0.5,
        "afm_max_approach_um": 4.0,
        "afm_feedback_records": 8,
        "afm_failed_points": 0,
        "afm_z_um": afm_z,
    }

    mat_dict, index_to_pos, skipped, timeouts = build_scan_mat_dict(
        all_data, SCAN_W, SCAN_H, STEP_UM, RECORDS, SAMPLES, True,
        "sample_closed_loop_afm", "um", None, None,
        START_X, START_Y, START_Z, stage_metadata=stage_metadata,
    )
    return mat_dict, index_to_pos, skipped, timeouts


_RESULTS = []


def check(name, condition, detail=""):
    _RESULTS.append((name, bool(condition)))
    print("  %-48s %s %s" % (name, "ok" if condition else "FAIL", detail))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "afm_contract_test.mat"))
    args = parser.parse_args(argv)

    print("=" * 70)
    print("AFM .mat contract test")
    print("=" * 70)

    mat_dict, index_to_pos, skipped, timeouts = build_dataset()
    check("every point packaged", len(index_to_pos) == SCAN_W * SCAN_H,
          "%d of %d" % (len(index_to_pos), SCAN_W * SCAN_H))
    check("no skipped points", not skipped and timeouts == 0)

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sio.savemat(out, mat_dict, do_compression=False)
    print("  wrote %s (%.1f MB)" % (out, os.path.getsize(out) / 1e6))

    # Re-read the way MATLAB will.
    loaded = sio.loadmat(out)
    points = [k for k in loaded if k.startswith("P_")]
    check("one variable per pixel", len(points) == SCAN_W * SCAN_H, "%d" % len(points))

    shapes = {loaded[k].shape for k in points}
    dtypes = {str(loaded[k].dtype) for k in points}
    check("all payloads (1, 4096)", shapes == {(1, SAMPLES)}, str(shapes))
    check("all payloads uint16", dtypes == {"uint16"}, str(dtypes))

    meta = loaded["metadata"][0, 0]
    fields = meta.dtype.names
    for required in ("scan_shape", "step_size", "pos_list", "records_per_point",
                     "is_averaged", "samples_per_record", "scan_target"):
        check("metadata.%s present" % required, required in fields)
    for extra in ("afm_mode", "afm_setpoint", "afm_f0_hz", "afm_z_um", "afm_retract_um"):
        check("metadata.%s present" % extra, extra in fields)

    check("scan_shape matches", list(np.ravel(meta["scan_shape"])) == [SCAN_W, SCAN_H],
          str(list(np.ravel(meta["scan_shape"]))))
    check("step_size is 1.0 um", abs(float(np.ravel(meta["step_size"])[0]) - STEP_UM) < 1e-9)
    check("records_per_point", int(np.ravel(meta["records_per_point"])[0]) == RECORDS)
    check("is_averaged", int(np.ravel(meta["is_averaged"])[0]) == 1)

    pos_list = [str(np.ravel(meta["pos_list"])[i]) for i in range(SCAN_W * SCAN_H)]
    check("pos_list length", len(pos_list) == SCAN_W * SCAN_H)

    # The MATLAB grid code parses "x, y, z" and uses the first two numbers.
    coords = np.array([[float(v) for v in p.split(",")[:2]] for p in pos_list])
    unique_x = len(np.unique(coords[:, 0]))
    unique_y = len(np.unique(coords[:, 1]))
    check("grid recovers 11 x 11", unique_x == SCAN_W and unique_y == SCAN_H,
          "%d x %d" % (unique_x, unique_y))
    step_x = np.diff(np.unique(coords[:, 0])).mean()
    check("X spacing is step_size", abs(step_x - STEP_UM) < 1e-6, "%.6f" % step_x)

    z_values = np.ravel(meta["afm_z_um"])
    check("afm_z_um length", z_values.size == SCAN_W * SCAN_H, "%d" % z_values.size)
    check("afm_z_um tracks the tilt", abs(z_values.max() - z_values.min() - 0.20) < 1e-6,
          "span %.4f um" % (z_values.max() - z_values.min()))

    failed = [n for n, ok in _RESULTS if not ok]
    print("\n" + "=" * 70)
    print("%d checks, %d failed" % (len(_RESULTS), len(failed)))
    if failed:
        for name in failed:
            print("  FAILED: %s" % name)
        return 1
    print("ALL PASSED")
    print("mat file: %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
