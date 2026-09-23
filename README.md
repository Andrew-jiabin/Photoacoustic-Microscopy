# Photoacoustic Microscopy Control

Windows-based control and acquisition software for a laboratory photoacoustic
microscopy (PAM) system. The project combines AlazarTech ATS9373 digitizer
acquisition, Thorlabs NanoMax positioning, laser control, live terminal
operation, MATLAB reconstruction, and small-scan diagnostics.

> This is an experiment-specific hardware repository, not a plug-and-play
> package. The default serial numbers, DLL paths, LAN address, travel guards,
> and scan limits describe one validated laboratory setup and must be checked
> before use on another computer.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Deployment Status

The repository runs on two workstations. Both are HP Pro Tower ZHAN 99 G9 with
no discrete GPU. The **target** machine is the current validated deployment.

| | Source (original) | Target (current) |
| --- | --- | --- |
| Hostname | `LMX` | `DESKTOP-T8UBAOM` |
| Repository | `D:\LJB\alazar_DAQ\Photoacoustic-Microscopy` | same path |
| Python env `PAM` | `C:\Users\20211\.conda\envs\PAM\python.exe` | `C:\Users\HP\miniconda3\envs\PAM\python.exe` |
| ATS-SDK | 7.7.0 | **26.2.0** |
| ATSApi / kernel driver | 7.13.12 | **8.0.1 / 8.0.1.0** |
| Git | 2.52.0 (`D:\Program Files\Git`) | 2.55.0 (`C:\Program Files\Git`) |

The acquisition path was re-verified on the target **after** the ATSApi upgrade
and produced identical results to the source machine, so the version difference
is not a compatibility risk for PAM. ATS-GPU was deliberately not installed:
neither machine has an NVIDIA card.

## Highlights

- Closed-loop 2D sample scanning with `MAX311D + BPC303` in micrometres.
- Open-loop probe scanning with `MAX312D + MDT693B` in controller volts.
- A shared Windows terminal panel for pre-alignment, acquisition status,
  laser state, pause/resume, and live progress.
- Alazar ATS9373 NPT acquisition with per-point averaging and persistent run
  logs.
- Optional blocked-beam 532 nm standard-noise acquisition and subtraction.
- Safe two-stage `.mat` saving, position metadata, live preview snapshots, and
  HTML result generation through the data-processing skill.
- Separate NanoMax-only motion debugging that does not initialize the DAQ or
  either laser.
- A standalone one-dimensional LBTEK PAM path and legacy Prior/NI-DAQ examples.

## Recommended Entry Points

| Entry point | Purpose | Status |
| --- | --- | --- |
| `PAM_Main_Nanomax.py` | Main integrated NanoMax PAM program; sample or probe target | Recommended |
| `Tool_code/nanomax_motion_debug_panel.py` | Motion-only panel for MAX311D/MAX312D; no DAQ or laser | Recommended for motion checks |
| `PAM_Main_LBTEK.py` | One-dimensional EM-LSS65-13C1 LBTEK scan | Separate validated path |
| `PAM_Main_SDK.py` | Older Prior/LBTEK-compatible PAM prototype | Legacy |
| `PAM_Main.py` | Older Prior stage + ATS9373 loop | Legacy |
| `PAM_Main_software.py` | Older GUI acquisition application | Legacy |
| `NI_DAQ_based/` | Nanoscan/NI-DAQ experiments | Experimental/legacy |
| `Alazar_imaging/Samples_Python/` | Vendor ATS9373 Python examples and wrapper source | Reference |

Only `PAM_Main_Nanomax.py` uses the current integrated pre-alignment and laser
panel. The older entry points have different hardware assumptions and should
not be mixed with the NanoMax workflow.

### Verification entry points

Run these first on any machine you did not build yourself. Neither one starts a
scan or changes a laser state.

| Entry point | Purpose | Hardware touched |
| --- | --- | --- |
| `Tool_code/acceptance_alazar_ats9373.py` | ATSApi + DAQ wrapper acceptance | Digitizer only |
| `Tool_code/acceptance_alazar_ats9373.py --capture` | One real laser-free acquisition | Digitizer only |
| `Tool_code/acceptance_stages_nanomax_prior.py` | Small guarded travel on both stages | **Moves stages** |
| `Tool_code/nanomax_motion_debug_panel.py` | Interactive motion-only panel | **Moves stages** |
| `Tool_code/validate_laser_panel_no_hardware.py` | Import check for the laser panel | None |

## Hardware Topology

The current PAM setup is wired as follows. The serial numbers are included to
make the deployment reproducible; replace them for a different installation.

| Function | Stage/controller | Software interface | Coordinate convention |
| --- | --- | --- | --- |
| Sample | Thorlabs `MAX311D` on `BPC303` (`71241834`) | Native BPC303 DLL through `ctypes` | Closed-loop `X/Y/Z` in `um` |
| Probe | Thorlabs `MAX312D` on `MDT693B` (`2201287140-09`) | MDT serial or command DLL | Open-loop `X/Y/Z` in `V` |
| Digitizer | Alazar `ATS9373` | `atsapi` / NPT | External-trigger acquisition |
| Pulsed laser | BrightSolutions `CBOX-Micro`, 532 nm | FTDI D2XX, `9600 8N1` | Emission and trigger readback/control |
| CW laser | TOPTICA DLC pro | TCP command port `192.168.1.11:1998` | Status and dependency-safe controls |
| Legacy linear stage | LBTEK `EM-LSS65-13C1` + EM-CVx | `moverLibrary.dll` | One-dimensional position in `mm` |

### NanoMax limits and axis mapping

- `MAX311D` sample axis mapping is `BPC303 CH1 = X`, `CH2 = Y`, `CH3 = Z`.
- The MAX300 manual gives a `20 um` piezo travel and a maximum piezo input of
  `75 V`. The software rejects scan requests outside the built-in `20 um`
  imaging window and applies a `75 V` output guard.
- The main sample trajectory is position-based. It is built from the final
  position selected in the pre-alignment panel, not from a stale hard-coded
  origin.
- `MAX312D` probe control is open-loop. The program uses `75 V / 20 um` as the
  default user calibration (`20/75 um/V`), but this is a conversion estimate,
  not position feedback. The actual safe range is still validated in volts.
- The MDT serial port is discovered from the controller serial number when
  possible. Do not assume a historical COM number remains unchanged.

### Laser safety boundary

- The CBOX front-panel `Laser OFF`/standby latch remains a physical control.
  Software `emission off` is not a replacement for putting the box in a safe
  physical state before testing.
- CBOX shutdown is considered successful only after the controller flags are
  read back with the emission bit cleared. A write return value alone is not
  sufficient.
- TOPTICA controls have dependencies. Automatic shutdown uses the reverse
  order `scan -> pc_external -> pc -> cc`.
- Any command that changes laser state or stage voltage is a hardware action;
  inspect the command and the beam path before executing it.

## Project Layout

```text
.
├── PAM_Main_Nanomax.py              # current integrated PAM entry point
├── PAM_Main_LBTEK.py                # validated 1D LBTEK PAM path
├── PAM_Main.py                      # legacy Prior acquisition loop
├── PAM_Main_SDK.py                  # legacy SDK-based prototype
├── PAM_Main_software.py             # legacy GUI application
├── Alazar_imaging/                  # hardware wrappers and ATS examples
│   ├── AlazarNPTSystem.py           # ATS9373 NPT acquisition wrapper
│   ├── BPC303NativeController.py   # closed-loop BPC303 controller
│   ├── MDT693BController.py         # open-loop MDT controller
│   ├── cbox_d2xx_controller.py      # CBOX FTDI protocol wrapper
│   ├── toptica_dlc_controller.py    # TOPTICA TCP command client
│   └── Samples_Python/              # Alazar vendor examples and atsapi source
├── Nanomax/                         # panels, trajectories, runtime, I/O
│   ├── prealign_panel.py            # closed-loop sample panel
│   ├── open_loop_panel.py           # open-loop probe panel
│   ├── acquisition_panel.py         # fixed acquisition dashboard
│   ├── scan_utils.py                # range, step, pattern, and travel checks
│   ├── data_io.py                   # MAT packaging and safe save/rename
│   ├── result_preview.py            # data-processing bridge
│   └── run_log.py                   # persistent startup/cleanup state log
├── Tool_code/                       # standalone diagnostics and tests
│   ├── acceptance_alazar_ats9373.py    # digitizer acceptance check
│   ├── acceptance_nanomax_prior.py     # guarded stage acceptance check
│   ├── nanomax_motion_debug_panel.py   # motion-only interactive panel
│   ├── validate_laser_panel_no_hardware.py
│   └── pam_scan_processing_src/     # vendored processing fallback source
├── MATLAB/                          # reconstruction and analysis scripts
├── reference/                       # integration decisions and lab notes
├── PDF/                             # ATS documentation retained for reference
├── code_test/                       # older interface experiments
├── NI_DAQ_based/                    # older NI/nanoscan experiments
├── LBTEK_1D_STAGE_NOTES.md          # LBTEK hardware and field experience
├── setup.py                          # minimal package scaffold
├── LICENSE                           # MIT license
└── .gitignore                        # data, logs, backups, and vendor binaries
```

## Installation and Prerequisites

The tested deployment is Windows with the laboratory `PAM` conda environment.
The repository does not install vendor hardware drivers automatically.

### Python packages

The current integrated path requires the equivalents of:

```powershell
python -m pip install numpy scipy pyserial tqdm
```

Additional paths have optional dependencies:

- `pythonnet` and Thorlabs Kinesis for the older managed BPC wrapper in
  `Alazar_imaging/BPC303Controller.py`.
- `nidaqmx`, `pyvisa`, `comtypes`, and `matplotlib` for selected legacy files
  under `NI_DAQ_based/` and `code_test/`.
- The HTML preview bridge uses the external `PAM_PROCESSING_SKILL_PATH` when it
  exists and otherwise falls back to the vendored
  `Tool_code/pam_scan_processing_src`. **The built-in default
  (`D:\Phd_training\skills\data-processing-skill`) exists on neither
  workstation** — it is a stale default, not a missing migration step. Leave it
  unset to use the vendored fallback, or point it at a real skill directory.
- `atsapi` is the Alazar Python wrapper. The project keeps wrapper source and
  examples under `Alazar_imaging/Samples_Python/Library`; the Alazar SDK and
  device driver must still be installed and visible to Windows.

### Verified environment

The validated `PAM` conda environment is **Python 3.10.18** with 45 installed
packages. A dependency scan of the whole repository (141 `.py` files) settled
three questions that come up on every new machine:

| Question | Answer |
| --- | --- |
| Do we need NI-VISA / NI-DAQmx? | **No.** `NI_DAQ_based/` has zero importers and none of the four main entry points reference `nidaqmx`, `pyvisa`, `visa`, or `comtypes`. |
| Do we need the LBTEK CH341SER driver? | **Not unless the LBTEK 1D stage is connected.** `CH341` device count is 0; the DLL is already in place. |
| Do we need MATLAB for acquisition or control? | **No.** The 13 `.m` files only do final image reconstruction; the vendored `pam_scan_processing` package (22 modules) covers the analysis path. |

A healthy environment imports all of these: `numpy`, `scipy`, `pyserial`,
`tqdm`, `matplotlib`, `PyQt5`, `pythonnet`, `plotly`, `nidaqmx`, `pyvisa`,
`atsapi`. Only `paramiko` (used by one laser test script) and `comtypes` (used
only inside the dead `NI_DAQ_based/` tree) are absent, and neither is on the
main path.

### Vendor/runtime requirements

Install and verify the following outside Git as appropriate for the machine:

1. AlazarTech ATS9373 driver/SDK and the `atsapi` Python module.
2. Thorlabs Kinesis native libraries for BPC303 discovery and control.
3. The MDT693B command DLL or a working MDT serial backend.
4. FTDI D2XX runtime for CBOX-Micro.
5. LBTEK SDK files for `PAM_Main_LBTEK.py` if that path is used.
6. A reachable TOPTICA DLC pro command port when TOPTICA control is enabled.

The root `setup.py` is only a minimal package scaffold. It is not a complete
hardware-driver installer and does not replace the vendor installation steps.

## Deployment on a New Workstation

Use this checklist when standing the repository up on a fresh Windows machine.
It is the condensed result of the `LMX -> DESKTOP-T8UBAOM` migration.

### 1. Copy the tree, not a clone

`data/`, every `*.dll` / `*.exe`, and the vendor SDK folders are excluded by
`.gitignore`, so **`git clone` produces an incomplete installation**. Copy the
directory verbatim:

```text
D:\LJB\alazar_DAQ\Photoacoustic-Microscopy     <- the repository
D:\LJB\PAM\PriorSDK 2.0.0                      <- Prior runtime (not tracked)
```

Keep the drive letter and the paths identical to the source machine. Several
scripts and both acceptance tools assume this layout.

### 2. Install the out-of-band software

Nothing below comes from Git:

| Component | Where it ends up | Notes |
| --- | --- | --- |
| Alazar ATS-SDK + ATSApi + ATS9373 driver | `C:\AlazarTech\ATS-SDK\<version>\` | ATSApi and the kernel driver are a **matched pair** — upgrade both or neither. |
| Thorlabs Kinesis | `C:\Program Files\Thorlabs\Kinesis` | Needed for BPC303 discovery. |
| FTDI D2XX runtime | system | For the CBOX-Micro serial link. |
| Prior SDK | `D:\LJB\PAM\PriorSDK 2.0.0\` | Copied as files; no installer required. |
| Git | `C:\Program Files\Git\` | `winget install --id Git.Git -e` |
| conda env `PAM` | `%USERPROFILE%\miniconda3\envs\PAM` | Python 3.10.18, see above. |

`ATS-GPU` is **not** installed and should not be: neither workstation has an
NVIDIA GPU, and the package cannot run without one.

### 3. Re-establish the Git identity

A copied `.git` directory works, but the machine has no credentials. On the
target, copy the source machine's GitHub key to a **separate filename** so the
machine's own key (used for SSH into other hosts) keeps working:

```text
~/.ssh/id_ed25519_github          <- copied from the source machine
~/.ssh/config                     <- route ONLY github.com through it
    Host github.com
        HostName github.com
        User git
        IdentityFile ~/.ssh/id_ed25519_github
        IdentitiesOnly yes

git config --global user.name  "J.B-Lin"
git config --global user.email "122217901+Andrew-jiabin@users.noreply.github.com"
git config --global http.sslBackend openssl
```

Do **not** copy `core.editor` blindly — on the source it points at a VS Code
path that may not exist on the target.

Verify both directions, because the `~/.ssh/config` entry can break a
pre-existing passwordless login:

```powershell
ssh -T git@github.com                          # expect: Hi <user>! You've successfully authenticated
git ls-remote --heads origin                   # expect the current main commit
ssh <source-user>@<source-host> hostname       # the pre-existing path still works
```

### 4. Prove the machine before trusting it

```powershell
# Digitizer only - safe, does not arm anything
python Tool_code\acceptance_alazar_ats9373.py

# Digitizer, one real acquisition, still no laser needed
python Tool_code\acceptance_alazar_ats9373.py --capture

# Moves stages. Power them on first and read the safety note in the file.
python Tool_code\acceptance_stages_nanomax_prior.py
```

Expected results on a healthy machine, for reference:

| Check | Expected |
| --- | --- |
| `getBoardKind()` | `29` (ATS9373) |
| `getChannelInfo()` | 2 channels, 12 bits per sample |
| `configure_board(4000MSPS)` | completes in well under a second |
| `--capture` | 2 buffers, 20 records, 81,920 bytes, non-zero noise floor |
| Prior `+10 um` | settles within ~0.2 s, error `0.000` |
| NanoMax `+5 um` | settles within ~0.5 s, error `< 0.05 um` |

`GET_SERIAL_NUMBER` raises `ApiInvalidData` on these boards. That is a
firmware-level behaviour, not a fault introduced by a driver upgrade.

### 5. What is deliberately NOT in Git

`.gitignore` excludes `data/`, `results/cache/`, `run_logs/`, `*.mat`, `*.png`,
`*.txt`, `*.log`, `__pycache__/`, `*.bak*`, and all vendor binaries. Of ~1,400
files on disk only ~150 are tracked. **The experiment data in `data/` is not
backed up anywhere by this repository** — it exists only on the machine that
produced it. Plan a separate backup for it.

## Static Verification

Run these checks before a hardware session. They do not start the acquisition
program, open a controller, move a stage, or change a laser state:

```powershell
python -m py_compile `
  PAM_Main_Nanomax.py `
  Alazar_imaging\*.py `
  Nanomax\*.py `
  Tool_code\*.py

python Tool_code\validate_laser_panel_no_hardware.py
python Tool_code\acceptance_alazar_ats9373.py
```

`validate_laser_panel_no_hardware.py` validates that the laser panel can be
imported without opening CBOX or TOPTICA hardware.
`acceptance_alazar_ats9373.py` is the digitizer acceptance check described
above.

## Running the Main PAM Workflow

Run from the repository root so that relative output paths are predictable:

```powershell
python PAM_Main_Nanomax.py
```

This is a **console** program: it waits for
`input("Press Enter to START Experiment...")`, so it needs an interactive
terminal. It also refuses to start if another instance already holds the
controller. `PAM_Main_software.py` is the older PyQt5 GUI if you need a window.

### Running without a 532 nm laser

Laser handling at startup is **read-only and fault-tolerant**. The program calls
`refresh_status()` and wraps both CBOX and TOPTICA in `try/except`, so an
unreachable laser is recorded as `cbox_connection: ERROR` and does not stop
startup. To bypass laser handling completely:

```powershell
$env:PAM_532_ENABLE = "0"
$env:PAM_TOPTICA_ENABLE = "0"
$env:PAM_532_NOISE_PROMPT_ENABLE = "0"
python PAM_Main_Nanomax.py
```

This is the supported way to exercise the whole "stage motion -> acquisition ->
`.mat` save" chain with no laser present. The stages must still be powered on.

The startup sequence is:

1. Inspect `run_logs/PAM_Main_Nanomax_run.log` and decide whether the previous
   sample X/Y datum is trusted.
2. Connect the selected NanoMax controller(s) and initialize the DAQ in the
   background where possible.
3. Open the pre-alignment panel. Read laser states are shown; the program does
   not change laser states merely because it starts.
4. Select the scan range, pixel step, pattern, and initial position. The panel
   blocks non-positive or sub-resolution steps and out-of-range trajectories.
5. Type `:start`/`:run`/`:pam`/`:image`/`:scan` to begin acquisition.
6. If enabled, complete the blocked-beam 532 nm noise prompt before the formal
   scan begins.
7. Acquire, save, optionally preview, return the stages, and perform requested
   laser shutdown during final cleanup.

### Pre-alignment panel

Closed-loop sample panel hotkeys:

| Key | Action |
| --- | --- |
| Up/Down | Move sample X by `xstep` in `um` |
| Left/Right | Move sample Y by `ystep` in `um` |
| `+`/`-` | Move closed-loop sample Z by `zstep` in `um` |
| `0`/`r` | Move sample X/Y to `0 um`; this is not a BPC zero-datum rebuild |
| `s` | Refresh status |
| `h`/`?` | Redraw help |
| `:` | Enter command mode |
| `q` | Leave before acquisition; no scan is started |

When both controllers are available, `:probe`/`:open` switches to the open-loop
probe panel, and `:sample`/`:closed` switches back. The trajectory is generated
after the final panel returns, so the selected starting position is used.

Useful pre-alignment commands include:

```text
set SCAN_RANGE_X_UM 1
set SCAN_RANGE_Y_UM 1
set STEP_UM 0.1
set SCAN_PATTERN s
set xstep 0.1
set ystep 0.1
set zstep 0.1
set xyz 0 0 5
laser refresh
532 trigger external
532 emission off
532 close-at-end on
toptica close-at-end on
start
```

Enter command mode with `:` first, then type the command text at `cmd>` and
press Enter. Do not type another colon in the command text.

Open-loop probe panel movement is voltage-based:

- Up/Down changes probe Z voltage.
- Left/Right changes probe Y voltage.
- `0`/`r` sets probe Y/Z to `0 V`.
- `set y <V>`, `set z <V>`, and `set yz <Y> <Z>` set voltages.
- `max <V>` changes the program guard and MDT axis maximum when supported.
- `sample`/`closed` switches to the sample panel.

## Scan Semantics

### Sample closed-loop mode

Set `PAM_SCAN_TARGET=sample_closed_loop` (the default). The main defaults are:

| Parameter | Default | Meaning |
| --- | ---: | --- |
| `PAM_SCAN_RANGE_X_UM` | `18.75` | Requested X travel |
| `PAM_SCAN_RANGE_Y_UM` | `18.75` | Requested Y travel |
| `PAM_STEP_UM` | `0.75` | Pixel spacing; both endpoints are included |
| `SCAN_PATTERN` | `serpentine` | S-shaped scan; odd Y rows reverse X |
| `SETTLE_MS` | `120` | Extra settle time after position tolerance is met |
| `PAM_SAMPLE_POSITION_TOLERANCE_UM` | `0.02` | Closed-loop readback tolerance |
| `PAM_SAMPLE_POSITION_TIMEOUT_S` | `300` | Per-point timeout |
| `PAM_SAMPLE_POSITION_REISSUE_INTERVAL_S` | `1` | Re-send target while outside tolerance |

The scan shape is `range / step + 1`, so a `0.3 um` range with `0.1 um`
step produces four points on that axis. The range must be non-negative, an
integer multiple of the step, and no larger than the `20 um` NanoMax imaging
limit. If a closed-loop point does not settle before the timeout, the program
records the actual readback, acquires the current signal, marks the point as a
timeout in metadata, and continues instead of discarding the whole run.

### Probe open-loop mode

Set `PAM_SCAN_TARGET=probe_open_loop`. The sample is held fixed and the probe is
scanned through MDT voltages. The default fast/slow axes are `Y/Z`; the probe
has no closed-loop position feedback. The program validates every target
against the effective safe voltage before acquisition.

### Return and zero-datum policy

- `PAM_SAMPLE_START_ZERO_POLICY=auto` rebuilds the sample X/Y zero datum only
  when the previous run log does not contain a trusted cleanup/return marker.
  Valid values are `auto`, `always`, and `never`.
- The current script default is `PAM_SAMPLE_RETURN_XY_TO_ZERO_AT_END=1`, so a
  completed sample run returns X/Y to the low-end `(0, 0)`. Set it to `0` to
  return to the pre-alignment-selected start instead.
- Return motion is segmented along a straight line using
  `PAM_SAMPLE_RETURN_STEP_UM` (default `0.1 um`) or
  `PAM_PROBE_RETURN_STEP_UM`, rather than issuing one large jump.
- `SAMPLE_ZERO_XY_AT_END` is a separate BPC zero-datum rebuild switch and is
  `False` by default. Rebuilding the datum is slow and changes the controller
  reference; do not confuse it with moving to coordinate `(0, 0)`.

## Acquisition Dashboard

The fixed terminal dashboard keeps the progress line, frozen acquisition
parameters, laser readback, and command area on screen together.

During acquisition:

- `q` pauses at a point boundary. It does not immediately abort an in-progress
  DAQ transfer.
- In the paused state, `y` confirms a clean stop and `Esc` resumes acquisition.
- `+`/`-` jog closed-loop sample Z while paused; `[`/`]` halve/double the Z
  jog step when the closed-loop sample controller is available.
- `p` generates all previews, `a` generates Axis-time, `3` generates 3D, and
  `i` generates the result index.
- `:532 close-at-end on/off`, `:toptica close-at-end on/off`, and
  `:laser refresh` remain available. Other motion, DAQ, and live laser-state
  changes are frozen.

The dashboard rate is smoothed for display. After a complete successful run,
the program records scan speed by target and step in
`run_logs/pam_scan_speed_history.json`; the pre-alignment panel uses the recent
median for a time estimate. Runs with timeouts or incomplete acquisition are
not used to update the speed history.

## Lasers

Laser state is read at startup and displayed; the main program does not
automatically turn lasers on or change the trigger mode. In pre-alignment, the
following explicit commands are available:

```text
laser refresh
532 emission on/off
532 trigger ext/int
532 close-at-end on/off
toptica cc on/off
toptica pc on/off
toptica external on/off
toptica scan on/off
toptica close-at-end on/off
```

At acquisition cleanup, only the requested close-at-end options are applied:

- `532_CLOSE_AT_END=on` sends CBOX emission OFF and verifies the flags readback.
- `TOPTICA_CLOSE_AT_END=on` executes the dependency-safe LIFO shutdown and
  verifies `emission`, `cc`, `pc`, `pc_external`, and `scan` are OFF.

Script-level defaults are in `PAM_Main_Nanomax.py`; environment variables
override them without editing the source:

```powershell
$env:PAM_532_CLOSE_AT_END = "1"
$env:PAM_TOPTICA_CLOSE_AT_END = "1"
python PAM_Main_Nanomax.py
```

For a read-only TOPTICA check:

```powershell
python Tool_code\laser_control_test.py toptica --map
python Tool_code\laser_control_test.py toptica --host 192.168.1.11 --status
```

Writes require both `--write` and `--confirm-write LASER_RISK_ACCEPTED`.
`Tool_code/cbox_micro_test.py` documents the manual-backed CBOX button
sequences; the transport write path must only be used after the physical laser
state and beam path are confirmed safe.

## Blocked-Beam 532 nm Noise Reference

The main program prompts before formal acquisition when
`PAM_532_NOISE_PROMPT_ENABLE=1` (default):

1. Physically block the 532 nm output and answer `y`.
2. The program acquires one reference point using the same
   `RECORDS_PER_POINT`, `SAMPLES_REC`, `AVERAGE_ENABLE`, and timeout settings
   as normal points.
3. Remove the physical block and answer `y` again.
4. Formal acquisition starts. Each saved waveform is
   `signal_average - noise_average`.

Both signal and reference go through the same sum-then-average packaging path.
The noise reference is not added to `pos_list` or the point count. When saved,
it is placed under the existing metadata struct:

```text
metadata.noise_532.average_waveform
```

The saved waveform and reference use signed `int32` when subtraction is active,
so negative residuals are preserved. To reconstruct the averaged raw signal:

```text
raw_average_signal = saved_position_waveform
                     + metadata.noise_532.average_waveform
```

Metadata also records whether subtraction was applied and which points were
skipped due to shape mismatch. Set `PAM_532_NOISE_PROMPT_ENABLE=0` to disable
the prompt and subtraction for a run.

## MAT Data Contract

The default save directory is `./data`. The first save always uses a unique
default filename. Only after that save succeeds does the program ask whether a
suffix should be added; rename never overwrites an existing file. If the suffix
step fails, the original default file is retained.

Top-level scan variables are generated from position keys for compatibility
with existing downstream scripts. The `metadata` struct contains the stable
scan contract, including:

- `scan_shape`, `step_um`, `records_per_point`, `samples_per_record`,
  `is_averaged`;
- `pos_list` in actual acquisition order, not coordinate-sorted order;
- `actual_pos_list`, settle flags, timeout flags, and per-axis position errors;
- `scan_target`, `coordinate_unit`, `start_xyz`, and probe conversion fields;
- `noise_532` and short noise compatibility fields when a reference was used;
- `skipped_pos_list` and `position_timeout_count`.

For sample scans, the outer trajectory loop is Y/row and the inner fast loop
is X/column. Serpentine mode reverses X on odd rows. The first point is the
selected start corner, normally `(x_min, y_min)` after a low-end zero rebuild.
Consumers that render a heatmap must use `metadata.pos_list` or explicitly
reshape according to `metadata.scan_shape`; do not assume that a sorted list
matches the acquisition order of an S-shaped scan.

## Live Preview and Processing

Preview artifacts are deliberately kept outside raw `./data`:

- Live snapshots: `./results/cache/pam_live_snapshots`
- HTML previews: `./results/cache/pam_preview`

`Nanomax/result_preview.py` is the small bridge used by the main program. It
passes the current `.mat` path to the processing package instead of embedding
data-processing code in the acquisition loop. The default processing settings
are `display_window=0:4000`, `baseline=0:100`, `time_step=1`, `mode=xy`, and
Hilbert enabled for Axis-time output.

Supported preview modes are `all`, `axis`, `axis-time`, `time`, `3d`,
`interactive`, and `index`. The processing source is vendored under
`Tool_code/pam_scan_processing_src/` as a fallback; configure
`PAM_PROCESSING_SKILL_PATH` if the external data-processing skill is installed
elsewhere.

## LBTEK 1D Path

`PAM_Main_LBTEK.py` is intentionally separate from the NanoMax path. It uses
an EM-LSS65-13C1 one-dimensional stage, Alazar ATS9373 NPT acquisition, and
`.mat` output. The field-validated defaults are approximately:

- speed `2.0 mm/s`;
- acceleration `0.5 mm/s^2`;
- settle time `2 s`;
- automatic port discovery by device code;
- axis ID `1`;
- absolute single-line scan with stop/retry/return handling.

The practical LBTEK lessons and unsafe command history are recorded in
`LBTEK_1D_STAGE_NOTES.md`. Do not send undocumented configuration commands to
the controller; a previous bad command caused error `9024` and required a
power cycle to recover.

## Useful Environment Variables

The source file remains the authoritative list and default values. The most
useful runtime overrides are:

| Variable | Effect |
| --- | --- |
| `PAM_532_ENABLE` | Set to `0` to skip CBOX laser handling entirely (read-only, fault-tolerant by default) |
| `PAM_TOPTICA_ENABLE` | Set to `0` to skip TOPTICA handling entirely |
| `PAM_SCAN_TARGET` | `sample_closed_loop` or `probe_open_loop` |
| `PAM_SCAN_RANGE_X_UM`, `PAM_SCAN_RANGE_Y_UM` | Scan travel |
| `PAM_STEP_UM` | Pixel step |
| `PAM_SAMPLE_START_ZERO_POLICY` | `auto`, `always`, or `never` |
| `PAM_SAMPLE_RETURN_XY_TO_ZERO_AT_END` | Return sample X/Y to low-end zero when true |
| `PAM_SAMPLE_POSITION_TOLERANCE_UM` | Closed-loop settle tolerance |
| `PAM_SAMPLE_POSITION_TIMEOUT_S` | Per-point settle timeout |
| `PAM_SAMPLE_POSITION_REISSUE_INTERVAL_S` | Target resend interval |
| `PAM_SAMPLE_RETURN_STEP_UM`, `PAM_PROBE_RETURN_STEP_UM` | Segmented return step |
| `PAM_ACQ_TIMEOUT_MS` | DAQ point acquisition timeout |
| `PAM_DATA_SAVE_AUTO_TIMEOUT_S` | Save prompt timeout; default 60 s |
| `PAM_532_NOISE_PROMPT_ENABLE` | Enable/disable blocked-beam prompt |
| `PAM_532_CLOSE_AT_END`, `PAM_TOPTICA_CLOSE_AT_END` | Final laser shutdown choices |
| `PAM_RESULT_PREVIEW_ENABLE` | Enable live preview bridge |
| `PAM_RESULT_PREVIEW_OUTPUT_DIR` | Preview HTML directory |
| `PAM_RESULT_PREVIEW_SNAPSHOT_DIR` | Live snapshot directory |
| `PAM_PROCESSING_SKILL_PATH` | External processing-skill root |
| `PAM_PANEL_AUTO_REFRESH_S` | Pre-alignment panel refresh interval |

For hardware identity overrides used by the motion-only debug panel, see its
`--help` output and the `PAM_BPC303_*`, `PAM_MDT693B_*`, and
`PAM_NANOMAX_DEBUG_*` variables in `Nanomax/motion_debug.py`.

## Repository Hygiene

The `.gitignore` intentionally excludes:

- raw `data/`, preview cache, run logs, `.mat`, `.png`, and temporary logs;
- Python caches and local backups such as `*.bak`;
- vendor SDK directories and binary drivers (`*.dll`, `*.exe`, `*.lib`, etc.);
- temporary diagnostic scripts that are not delivery tools.

This keeps Git history focused on reproducible source, manuals, reference notes,
and validated scripts. A fresh clone therefore needs the vendor drivers and
local SDK files installed separately before hardware control can work.

The practical consequence, measured on the target machine: **~150 tracked files
versus ~1,400 on disk.** Roughly 2.4 GB of `.mat` experiment data under `data/`
plus ~63 MB of SDK binaries are not covered by Git. The SDK binaries are
re-installable; the data is not. Back `data/` up separately.

## Version Control and Backup

`origin` is `git@github.com:Andrew-jiabin/Photoacoustic-Microscopy.git` and the
working branch is `main`.

### One-click backup

`D:\LJB\_tools\git_backup.bat` (reachable from a desktop shortcut) drives the
routine:

```text
[1]  Backup and push    - stage all, commit with a timestamp, push origin/main
[2]  Pull from GitHub   - fast-forward only, never creates a merge
[3]  Show status        - working tree plus the last three commits
[4]  Exit
```

It also accepts an action as an argument, which is what makes it usable from a
terminal or a script:

```bat
D:\LJB\_tools\git_backup.bat status
D:\LJB\_tools\git_backup.bat backup
D:\LJB\_tools\git_backup.bat pull
```

Implementation notes, all of which are load-bearing:

- `.bat` files must use **CRLF** line endings, or `cmd` eats leading characters.
- The script body is **pure ASCII** and sets `chcp 65001`, so a GBK console
  cannot corrupt it.
- The repository is located via `%~dp0..\alazar_DAQ\...`, so the script contains
  **no literal drive letter**. A literal `X:\` inside a batch file is rewritten
  by some toolchains to `X:/`, which `cmd` then treats as a switch.
- It repairs `PATH` if `git` is missing, because an installer updates the system
  `PATH` but a long-running Explorer keeps passing its stale environment to
  anything launched from the desktop.
- The commit timestamp comes from PowerShell `Get-Date -Format`, not `%DATE%`,
  which on a Chinese Windows is `2026/09/23 周三` and parses unpredictably.

### Manual equivalent

```powershell
git status
git add -A
git commit -m "backup: <description>"
git push origin main
```

### Verifying write access

`git push --dry-run origin main` prints `Everything up-to-date` and never
exercises the permission check when there is nothing to send. To genuinely test
write access without changing anything:

```powershell
git push --dry-run origin HEAD:refs/heads/tmp-write-probe
# * [new branch]  HEAD -> tmp-write-probe      <- write access confirmed
git ls-remote --heads origin                  # confirm the probe was not created
```

## Handover Notes for a New Maintainer

### Machine identification

The target workstation and an unrelated machine **share the hostname
`DESKTOP-T8UBAOM` and the user `HP`**. `hostname` and `whoami` cannot tell them
apart. Identify machines by IP address or SSH alias, or probe for something only
the real one has:

```bat
if exist D:\LJB (echo TARGET) else (echo WRONG MACHINE)
```

Connecting to the wrong one makes a completed migration look lost, because that
machine has no `D:\LJB` at all.

### Things that will waste your afternoon

- **Remote shell is `cmd.exe`, and console output may be GBK.** Pipe through
  `iconv -f GBK -t UTF-8`, or filter with `grep -a`; without `-a`, a manifest
  containing CJK paths is treated as binary and `grep` prints a single
  `Binary file ... matches` line instead of the diff.
- **Never build a remote path from a shell loop variable.** In
  `for f in a b; do ssh host "del \"D:\x\$f\""; done` the variable is consumed
  before it reaches the remote shell, so the command runs against `D:\x\` — and
  because the following `if exist` is mangled the same way, the script reports
  success while the file is still there. Use literal paths and verify with a
  separate listing.
- **`__pycache__/*.pyc` never matches between machines.** Bytecode embeds the
  source's absolute path. Exclude `__pycache__`, `*.egg-info`, and `.git` when
  comparing a copied tree, or you will chase dozens of phantom differences.
- **A non-zero `setTriggerTimeOut()` makes the digitizer trigger on its own.**
  This is the only reason a full DMA acquisition can be tested with no laser and
  no external trigger — see `acceptance_alazar_ats9373.py --capture`.
- **`BPC303NativeController()` is not a read-only constructor.** It
  auto-connects, enables the piezo channels, and forces closed-loop mode. The
  channels become energised.
- **`PriorUnifiedStage.get_SDK_version()` returns an rc code**, not a version
  string. `0` means success.
- **Do not send undocumented commands to the LBTEK controller.** A previous bad
  command produced error `9024` and needed a power cycle. See
  `LBTEK_1D_STAGE_NOTES.md`.

### Where the supporting documents live

| Document | Contents |
| --- | --- |
| `README.md` (this file) | Entry points, contracts, deployment, backup |
| `LBTEK_1D_STAGE_NOTES.md` | LBTEK hardware behaviour and field experience |
| `reference/` | Integration decisions and lab notes |
| `run_logs/PAM_Main_Nanomax_run.log` | Previous run state; drives the startup zero-datum decision |
| `run_logs/pam_scan_speed_history.json` | Median scan speed per target/step, used for time estimates |

### Recommended first session on an unfamiliar machine

1. `python Tool_code\validate_laser_panel_no_hardware.py`
2. `python Tool_code\acceptance_alazar_ats9373.py`
3. Power the stages, then `python Tool_code\acceptance_stages_nanomax_prior.py`
4. `python Tool_code\acceptance_alazar_ats9373.py --capture`
5. Only then start `PAM_Main_Nanomax.py` with a small range
   (`0.3 um` / step `0.1 um` / 16 points) and the laser variables disabled.

## Safety Checklist

Before a real run:

1. Confirm the correct stage is mounted and the sample/probe wiring matches
   the axis mapping above.
2. Start with a small range and a step at or above the `0.01 um` software
   resolution guard; validate the expected point count on the panel.
3. Confirm the CBOX is physically in the required safe state before any
   emission command, and make the beam path safe before enabling emission.
4. Verify TOPTICA `scan` and external-control states are intentional.
5. Check that no other PAM process, vendor GUI, or diagnostic script owns the
   stage, DAQ, serial port, FTDI device, or laser TCP connection.
6. Use the motion-only panel for stage checks before starting a DAQ scan.
7. Keep the generated `.mat` file and run log after every experiment; the log
   is part of the startup zero-datum decision on the next run.

Never use an undocumented controller command as a discovery method while a
stage is connected to a sample. Prefer read-only checks, explicit write guards,
small motion, and a verified return path.

## License

Released under the [MIT License](LICENSE). Hardware drivers, vendor SDKs,
manuals, and external processing skills remain subject to their own licenses.
