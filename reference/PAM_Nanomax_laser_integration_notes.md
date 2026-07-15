# PAM NanoMax And Laser Integration Notes

This note records the operational decisions that are easy to lose when editing
`PAM_Main_Nanomax.py`.

## Main Entry Point

- Keep `PAM_Main_Nanomax.py` as the operator-facing entry point.
- Put reusable hardware code under `Alazar_imaging/`, not inside the main script.
- Keep prealignment and acquisition dashboards under `Nanomax/`.
- Use `Tool_code/` for diagnostics and standalone tests only.

## Closed-Loop Sample NanoMax

- The sample stage is MAX311D on BPC303 serial `71241834`.
- BPC303 channel mapping is user-wired and confirmed as `1/2/3 = X/Y/Z`.
- Closed-loop sample motion is position-based in microns.
- The program builds sample trajectories from the prealignment-selected start
  position, not from a hardcoded origin.
- Default end behavior now returns sample X/Y to the prealignment-selected scan
  start. Set `DEFAULT_SAMPLE_RETURN_XY_TO_ZERO_AT_END = True` in
  `PAM_Main_Nanomax.py`, or set env var `PAM_SAMPLE_RETURN_XY_TO_ZERO_AT_END=1`,
  only when the run should return to low-end `X/Y = 0,0`.
- `SAMPLE_ZERO_XY_AT_END` remains separate. Keep it `False` unless the operator
  explicitly wants the slow BPC303 zero-datum rebuild.

## Open-Loop Probe NanoMax

- The probe stage is MAX312D on MDT693B serial `2201287140-09`.
- Historical working serial port was `COM7`, but do not hardcode it as a
  guarantee. Enumerate current ports and degrade gracefully if the probe
  controller is absent.
- Open-loop probe motion is voltage-based; the main sample Z control must stay
  closed-loop position-based.

## Laser Runtime

- CBOX-Micro 532 nm control is through FTDI D2XX, not ordinary COM/VCP serial.
- CBOX software can control emission on/off after the physical Laser OFF/Stand
  By state is prepared. It should not be treated as a full physical power switch.
- Toptica control is TCP on `192.168.1.11:1998`.
- Toptica close-at-end must follow dependency/LIFO order:
  `scan -> pc_external -> pc -> cc`.
- Script-level defaults for final laser shutdown are in `PAM_Main_Nanomax.py`:
  `DEFAULT_532_CLOSE_AT_END` and `DEFAULT_TOPTICA_CLOSE_AT_END`.
- Runtime env overrides still work:
  `PAM_532_CLOSE_AT_END=1` and `PAM_TOPTICA_CLOSE_AT_END=1`.
- During acquisition, only close-at-end toggles and laser status refresh should
  remain mutable; motion, scan, DAQ, and live laser state controls are frozen.

## Verified Small-Scan Test

On 2026-07-15, a real remote hardware test used:

- `PAM_SCAN_RANGE_X_UM=0.3`
- `PAM_SCAN_RANGE_Y_UM=0.3`
- `PAM_STEP_UM=0.1`
- shape `4 x 4`, total `16` points
- `PAM_SAMPLE_START_ZERO_POLICY=never`

Panel commands were sent in prealignment:

```text
532 close-at-end on
toptica close-at-end on
start
```

Observed cleanup messages:

```text
532 emission OFF sent; ok=True.
TOPTICA safe_off_lifo done: pc_external->False, pc->False, cc->False.
```

Read-only status after cleanup showed CBOX emission off and Toptica
`cc/pc/pc_external/scan` all off.

## Validation Checklist

Before committing control changes, run:

```powershell
C:\Users\20211\.conda\envs\PAM\python.exe -m py_compile `
  PAM_Main_Nanomax.py Nanomax\runtime.py Nanomax\run_log.py

C:\Users\20211\.conda\envs\PAM\python.exe Tool_code\validate_laser_panel_no_hardware.py
```

Use a real hardware run only when the operator confirms the stage and lasers are
safe to access.
