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
- The D2XX handle must restore the UART settings on every open:
  `9600 baud`, `8 data bits`, `1 stop bit`, `no parity`, and `no flow control`.
  Without this, stale COM/VCP settings can let `FT_Write` report success while
  the controller returns no response.
- CBOX software can control emission on/off after the physical Laser OFF/Stand
  By state is prepared. It should not be treated as a full physical power
  switch; the physical Laser OFF latch is still a front-panel action.
- Treat the CBOX `flags` readback as the shutdown truth. For emission shutdown,
  `set_emission(False)` is not enough by itself; success means the inferred
  emission bit in `flags` is cleared. A known OFF flags example is `035F12`.
- Toptica control is TCP on `192.168.1.11:1998`.
- Toptica close-at-end must follow dependency/LIFO order:
  `scan -> pc_external -> pc -> cc`.
- Script-level defaults for final laser shutdown are in `PAM_Main_Nanomax.py`:
  `DEFAULT_532_CLOSE_AT_END` and `DEFAULT_TOPTICA_CLOSE_AT_END`.
- Runtime env overrides still work:
  `PAM_532_CLOSE_AT_END=1` and `PAM_TOPTICA_CLOSE_AT_END=1`.
- During acquisition, only close-at-end toggles and laser status refresh should
  remain mutable; motion, scan, DAQ, and live laser state controls are frozen.
- `PAM_Main_Nanomax.py` calls the same laser finalizer from error,
  keyboard-interrupt, final cleanup, and `atexit` paths. The one-shot cleanup
  marker is set only after readback confirms the requested laser shutdown, so a
  failed first attempt can still be retried by later cleanup paths.

Manual 532 emission-off diagnostic, when the operator explicitly wants only the
software emission disabled:

```powershell
C:\Users\20211\.conda\envs\PAM\python.exe Tool_code\cbox_d2xx_control.py emission_off --write --confirm-write LASER_RISK_ACCEPTED
```

Expected success wording includes:

```text
532 emission OFF verified; response_ok=True; flags=035F12
```

## Closed-Loop Position Tolerance Policy

- BPC303 position commands can stop outside tolerance after one command. The
  controller wrapper therefore reissues the target at
  `PAM_SAMPLE_POSITION_REISSUE_INTERVAL_S` while readback remains outside
  `PAM_SAMPLE_POSITION_TOLERANCE_UM`.
- If a point still fails to settle before `PAM_SAMPLE_POSITION_TIMEOUT_S`, the
  scan no longer aborts the whole dataset. It logs
  `ACQUISITION_POSITION_TIMEOUT_CONTINUE`, stores target and actual positions in
  the `.mat` metadata, acquires the current signal, and moves on.
- Return-to-start is segmented along a straight line with
  `PAM_SAMPLE_RETURN_STEP_UM` or `PAM_PROBE_RETURN_STEP_UM`, avoiding one large
  step back to the start or low-end zero.
- Save prompts default to preserving data. If the operator does not answer
  within `PAM_DATA_SAVE_AUTO_TIMEOUT_S`, data are saved automatically with no
  filename suffix.

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
532 emission OFF verified; response_ok=True; flags=035F12; attempt=1.
TOPTICA safe_off_lifo done: pc_external->False, pc->False, cc->False.
532 close-at-end verified OFF.
TOPTICA close-at-end verified OFF.
```

Read-only status after cleanup showed CBOX emission off and Toptica
`cc/pc/pc_external/scan` all off.

## Validation Checklist

Before committing control changes, run:

```powershell
C:\Users\20211\.conda\envs\PAM\python.exe -m py_compile `
  PAM_Main_Nanomax.py Alazar_imaging\*.py Nanomax\*.py Tool_code\*.py

C:\Users\20211\.conda\envs\PAM\python.exe Tool_code\validate_laser_panel_no_hardware.py
```

Use a real hardware run only when the operator confirms the stage and lasers are
safe to access.
