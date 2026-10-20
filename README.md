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

Both machines track `origin/main`. Check the deployment with `git log -1
--oneline` and `git status --short` on the target itself rather than trusting a
commit hash written down here -- this file is edited far less often than the
code.

## Machine Identity

Every note, commit message, and run-log entry in this repository identifies the
machine by its **tailnet IP**, never by hostname. Both workstations are HP Pro
Tower ZHAN 99 G9 and both are named `DESKTOP-T8UBAOM`, so the hostname cannot
tell them apart — that collision has already caused work to be looked for on the
wrong machine.

| tailnet IP | SSH alias | code + digitizer | ATS-SDK manuals |
| --- | --- | --- | --- |
| **`100.79.154.4`** | `PAM-NEW` | yes | no |
| `100.118.93.35` | `Work` | no | yes (`D:\Phd_training\exp_materials\15万采集卡\`) |

Rules that follow from the collision:

- Address a machine by IP or by the SSH alias that resolves to it. A hostname in
  a note is a bug.
- Only `100.79.154.4` has the repository *and* the ATS9373. Only
  `100.118.93.35` has the SDK manuals. A question that needs both needs two
  sessions.
- The digitizer can be opened by **one process at a time**. All digitizer
  probes are serial. Two concurrent probes leave one of them hanging for
  minutes on a two-second job.

## Build Toolchain

### Python

| interpreter | version | numpy | scipy | use |
| --- | --- | --- | --- | --- |
| `C:\Users\HP\miniconda3\envs\PAM\python.exe` | 3.10.18 | 2.2.6 | 1.15.3 | **the imaging environment** |
| `C:\Users\HP\python311\python.exe` | 3.11 | 2.4.6 | — | builds the DLL, runs `-m Nanomax.afm_pi` |

Only the `PAM` conda environment has `scipy`, so it is the only one that can
import `Nanomax.data_io` and therefore the only one that can run a scan or
`--dry-run`. `python311` is enough for compiling and for the controller
self-checks, which is why both are listed.

`atsapi` is not installed as a package; it is imported from
`Alazar_imaging\Samples_Python\Library` (verified with `where /r D:\ atsapi.py`).

### C++ — the PI controller

Neither workstation has MSVC or MinGW. `C:\Program Files (x86)\Microsoft Visual
Studio\2022\BuildTools` contains only `Common7\Licenses\MSBuild`, there is no
`VC\Tools\MSVC`, `vswhere.exe` is absent, and `zig` / `g++` / `clang++` are not
on `PATH`. Installing a toolchain on a lab machine is not acceptable, so the
controller is built with the **`ziglang` PyPI wheel**, which ships a complete
C/C++ compiler and needs no installation:

```bat
:: one-off, into a scratch directory (no admin, no PATH change)
pip install --no-deps --target D:\LJB\_stage\zigpkg ziglang-0.16.0-py3-none-win_amd64.whl

:: build
set PYTHONPATH=D:\LJB\_stage\zigpkg
C:\Users\HP\python311\python.exe -m ziglang c++ ^
    -target x86_64-windows-gnu -O2 -shared ^
    -o Nanomax\afm_pi.dll Nanomax\afm_pi.cpp
```

The result is a **363,008-byte self-contained DLL** with no CRT dependency
beyond what Windows already provides. `Tool_code\build_afm_pi.py` wraps this and
falls back to `zig` on `PATH`, `cl`, `g++`, or `clang++` if one of those ever
appears.

Two build traps worth remembering:

- mingw's `<math.h>` already declares `::finite(double)`. A helper named
  `finite` collides with it and the build dies with twelve errors, so the
  controller calls its own `is_finite_value`.
- The remote `cmd.exe` console is GBK. A build or test script that prints a
  non-ASCII character (`façade`, `µm`, `万`) raises
  `UnicodeEncodeError: 'gbk' codec can't encode character`. Script output that
  has to run over SSH must be pure ASCII.

## How the AFM Z loop works

### The loop

Z is the measurement. At every XY point the sample is driven until a feedback
signal reaches a setpoint, the A-line is taken there, and the sample backs off
before the next point. The surface is re-discovered per point, so tilt and
thermal drift are tracked instead of being a systematic error, and the Z travel
per point is bounded by `retract_um + max_approach_um` no matter how long the
scan runs.

Three things are interfaces rather than constants, because they are the three
things that change with the sample: **what** the Z decision is based on
(`PAM_AFM_CONTROL_SOURCE`), **how fast** the gap may close
(`PAM_AFM_APPROACH_SPEED_UM_S`), and **how high** to lift between points
(`PAM_AFM_RETRACT_POLICY`). Each has its own section below.

### Incremental (velocity-form) PI

The stage takes an absolute Z, but the decision the loop actually has to make is
"how far to move next", which is exactly what an incremental PI produces:

```
e[k]   = setpoint - y[k]
dz[k]  = sign * ( Kp*(e[k]-e[k-1]) + Ki*dt*e[k] )
z[k+1] = z[k] + clamp(dz[k], -dz_limit, +dz_limit)
```

- **No integral state to wind up.** The integrator *is* the accumulated
  position, and the position is already bounded by the Z window and the
  per-point travel budget, so the integral is bounded with it.
- **Bumpless.** `e[-1]` is seeded from the first measurement, so there is no
  derivative kick on step one, and a setpoint change mid-scan needs no
  re-initialisation.
- **No monotonicity assumption.** `sign` only says which way Z has to move for
  a positive error. A signal that *falls* as the probe approaches works
  identically with `sign = -1`; nothing creeps "until a threshold is crossed",
  and an overshoot is corrected downward.
- **`dt` is capped at 10× nominal**, so one long host stall cannot inject a
  huge `Ki*dt` kick.

### Two progress guards

A feedback signal that ignores Z either lets the error grow or never improves.
Without a guard the loop quietly grinds through the whole travel budget and
reports a bogus point, so both failure modes are detected:

| guard | condition | meaning |
| --- | --- | --- |
| `diverged` | `|e| > 1.5*|e_start| + deadband` for 8 steps | wrong sign, runaway |
| `stalled` | no new minimum `|e|` for `stall_steps`, counting only steps that are **not** retreating | probe too far, no coupling |

The stall test uses a **monotone ratchet** (`e_best` only ever decreases), so
noise riding on a constant reading cannot fake progress. `stall_steps` is not a
fixed constant: it is derived from `pi_stall_um / dz_limit_um`, because a
*saturated* reading is flat too, and backing out of saturation needs a blind
allowance comfortably larger than the retract distance.

Only steps that are **not retreating** count towards `stalled` (`dz * sign >= 0`,
where `sign` is the sign of `d(reading)/dZ`). A step away from the surface cannot
be evidence of a dead loop: when the retract was smaller than the next point's
height the probe *starts* inside the sample, the reading is clipped at its
maximum, and "no improvement" is exactly what a correct retreat looks like. Before
that exemption, a 5 µm cell dome scanned with the old 0.5 µm lift lost 5 of 13
points to `stalled` — not a crash, but a hole in the image. A zero-length step
still counts, which is what keeps the gains-off guard check in the selftest
honest.

Note the weaker-looking condition `dz * sign * e > 0` is useless here: a
correctly wired loop always moves in the direction that reduces `|e|`, so it
would always be true.

### Blind search

Far from the surface the reading is zero — indistinguishable from a
disconnected probe — so the PI has no information and its stall guard would
(correctly) give up after a fraction of a micrometre. While the reading is below
`search_floor_frac × setpoint`, Z is therefore advanced coarsely in the approach
direction until the signal appears, and only then does the PI take over. This is
the one place where the loop moves without feedback; it is bounded by the same
travel budget and logged as `AFM_SEARCH_BEGIN` / `AFM_SEARCH_HANDOVER`.

### Gains

`kp` is µm per unit of *change* in the reading (a damping term), `ki` is µm per
(unit · second) (the creep term). Both are in whatever unit the caller feeds to
the loop, so a setpoint in volts gives µm/V and µm/(V·s), and a setpoint in
board units gives µm/au.

Guessing them is the main source of trouble, so the recommended path is to
measure the plant once and let `suggest_gains()` derive them:

```python
kp, ki = suggest_gains(sensitivity_per_um)   # kp = 0.5/S, ki = 0.3/(S*dt)
```

`PAM_AFM_SENSITIVITY` feeds that in; the defaults (`kp=0.003`, `ki=0.05`) are
tuned for the on-FPGA FFT amplitude scale of roughly 250–320 au.

### Where the number comes from

The loop never reads the digitizer directly. It calls `value()` on a *control
source*, so the Z criterion can be the 10 MHz amplitude, a demodulated phase, a
lock-in output, or a reading from another instrument:

```bat
set PAM_AFM_CONTROL_SOURCE=file:D:\path\to\my_source.py
```

The module must define `create_source(config)` returning an object with
`value()` (see `Tool_code\example_control_source.py`). The number is compared
against `PAM_AFM_SETPOINT` in the same unit, so nothing converts anything — the
source owns its units.

The contract lives in `Nanomax/afm_control.py` and is deliberately small:

| member | required | meaning |
| --- | --- | --- |
| `value()` | yes | `number`, `(number, snr)`, or `None` |
| `unit` | no | logging and metadata only |
| `prepare()` / `finish()` | no | open / close the instrument |
| `describe()` | no | one line for the run log |

`None` or a NaN means **no valid reading**; the point then fails with
`no_reading` instead of the loop acting on garbage. `adopt()` accepts a plain
callable, an object with `measure()`, or a full source, so `create_source` can
return `lambda: read_my_lockin()`. Adapters in the same module do the usual
shaping: `ScaledSource` (gain/offset, so the setpoint keeps its meaning),
`SmoothSource` (EMA or median — the honest alternative to widening the deadband,
which hides noise by refusing to act and therefore biases the landing point).

### Speed: slow towards the probe, fast away from it

The dangerous motion is the one that closes the gap, so the closing rate is a
**rate**, in µm/s, not a step size:

```bat
set PAM_AFM_APPROACH_SPEED_UM_S=1.0      :: far from the setpoint
set PAM_AFM_FINAL_SPEED_UM_S=0.4         :: last 15% of the error
set PAM_AFM_FINAL_BAND_FRAC=0.15
set PAM_AFM_SEARCH_SPEED_UM_S=3.0        :: the blind phase, still capped
```

Each step the scanner calls `afm_pi_set_dz_limit(speed * measured_dt)`, so the
closing rate is the same whether a step took 30 ms or 300 ms. A fixed µm/step
limit is *not* a speed: at a 30 ms loop it means 1.7 µm/s and at 300 ms 0.17 µm/s,
and the probe only cares about the first number. `dz_limit_um` remains as an
absolute per-step backstop.

Moving *away* is a single stage command at full speed with no settle — it is the
one motion that cannot break anything, so there is nothing to ramp and nothing
to wait for.

"Careful" and "unusably slow" are the same code path, so the bound on slowness is
explicit rather than hoped for:

- `PAM_AFM_MAX_APPROACH_S` caps the wall-clock time per point (default 15 s).
  A step count cannot do this, because the number of steps depends on the loop
  period.
- `check_config()` compares `max_approach_um / (speed × dt)` against
  `pi_max_steps` and prints the worst-case cost before the scan starts, so a
  rate limit that would turn every point into `max_steps` is visible up front
  instead of looking like a broken probe.

### Retract height is a policy, not a number

How far to lift before the XY move depends on the sample: a **cell** is a dome
(little lift on top, a lot at the rim) and a **resolution target** is two levels
(nothing for most points, the full bar height on the one point that climbs). One
constant cannot be right for both, so `Nanomax/afm_retract.py` makes it a
function of *where we are going next*:

```bat
set PAM_AFM_RETRACT_POLICY=auto
set PAM_AFM_RETRACT_UM=0.5               :: hard floor, never less than this
set PAM_AFM_RETRACT_MARGIN_UM=0.05       :: clearance on top of the computed lift
set PAM_AFM_RETRACT_MAP=file:surface.csv :: only for policy=map
```

| policy | lift | use |
| --- | --- | --- |
| `fixed` | always `retract_um` | old behaviour, a flat sample |
| `running_max` | deepest Z seen so far + margin | anything that only ever gets higher |
| `slope` | local height slope × distance to the next point | a dome, with no map (lags one point — see below) |
| `map` | `h(next) - h(here) + margin` | a known step, cleared *before* stepping onto it |
| `auto` | max of fixed + running_max + slope | default; never less than the old constant |

`retract_um` is a floor applied by the scanner itself, so a policy can only ever
make the retract **bigger**. `max_retract_um` caps it, and the Z window clamps
the result.

The height map is **protrusion towards the probe**, not a stage Z: a cell top is
positive, a gap between cells is zero. The same map then works for either
`approach_sign`. CSV format is a header row of x values followed by
`y,h,h,...`; blank cells or `nan` mean unknown and fall back to the floor. See
`Tool_code\example_height_map.csv`.

The property that matters is not "the lift looks reasonable" but "at the moment
XY moves, Z clears the next point". `Tool_code/test_afm_offline.py` asserts
exactly that on a stepped surface: with `map` the probe clears all four moves,
with `fixed` it clears one — which is why the policy exists.

The same test then scans a **cell dome** (5 µm high, 4 µm 1/e radius, 2 µm pitch,
13 points) with every policy and counts the moves where Z was still inside the
next point when XY moved:

| policy | moves inside the sample |
| --- | --- |
| `fixed` 0.5 µm | 3 / 12 |
| `slope` | 3 / 12, and all of them on the ascent |
| `auto` | 1 / 12 |
| `map` | 0 / 12 |
| `fixed` with `retract_um = dome_rise(...)` | 0 / 12 |

That is the honest answer for a cell: a **reactive** policy cannot be right on a
convex flank, because the least-squares slope underestimates the *next* rise and
is therefore always one point late. `auto` only wins the descent, where
`running_max` still remembers the apex. So on a dome, either supply a map or
compute the floor from the geometry:

```python
from Nanomax.afm_retract import dome_rise
dome_rise(height_um=5.0, radius_um=4.0, pitch_um=2.0)   # 1.516 um
```

`dome_rise` returns `H * pitch / (r * sqrt(e))`: the worst climb between two
neighbouring points of `H exp(-x^2 / (2 r^2))`, assuming the steepest part of the
bump falls between two sample points. It is a bound, not an estimate, which is
what a safety floor should be. A two-level target needs no such helper — the
number is the bar height, and a `fixed` lift of that size clears every move.

### Trigger-free acquisition

The laser trigger is not connected, and this board does not free-run on
`TRIG_DISABLE` alone. What actually works, measured on hardware at 4×4096:

| configuration | result |
| --- | --- |
| `TRIG_DISABLE` + timeout **0** | ✗ `ApiWaitTimeout` after 2.0 s |
| `TRIG_DISABLE` + timeout **0.01 s** | ✓ 0.010 s, valid data |
| `TRIG_EXTERNAL` + 0.01 s | ✓ 0.011 s |
| `TRIG_CHAN_A` + 0.01 s | ✓ 0.009 s |
| DSP FFT, `TRIG_DISABLE` + 0 | ✗ `ApiWaitTimeout` |
| DSP FFT, `TRIG_DISABLE` + 0.01 s | ✓ 0.020 s |

`setTriggerTimeOut(0)` means **"wait forever"**, not "do not wait" — a non-zero
timeout is what makes the board self-trigger when the interval expires. The
parameter is an **integer count of 10 µs ticks**, not seconds; passing a float
raises `ctypes.ArgumentError`.

### Where an A-line comes from, and why the timeout must beat the laser

An A-line is only meaningful if it starts on a laser pulse. With no trigger
cable, a 4096-point record at 4 GS/s covers 1.024 µs out of a 952 µs laser
period — it lands at a random phase, so summing 256 of them averages the
photoacoustic signal against the empty part of the cycle and the result is
noise with a DC offset. **Free-running records are not a substitute for
triggered ones.**

`TRIG_EXTERNAL` plus a non-zero timeout gets both behaviours out of one
configuration, with no mode switch and no reconfiguration:

| state | what the board does | A-line valid? |
| --- | --- | --- |
| laser on, cable attached | waits for the sync pulse, one record per pulse | yes, phase-locked |
| laser off / cable out | timeout expires, triggers itself | no, but the approach loop still reads |

The single hard constraint: **the timeout must be longer than the laser
period.** If it is shorter, the board times out before every pulse, never locks,
and you are back to random-phase records without any error message. Measured
laser: 1050 Hz → 952 µs, so the default is 150 ticks = 1.5 ms (57 % margin,
against 5 % for the 1 ms it used to be).

Measured on the board with no trigger cable attached, 4 GS/s × 4096, using
`AlazarBurstCapture`:

| trigger | ticks | records | elapsed | per record |
| --- | --- | --- | --- | --- |
| `disable` | 1 | 256 | 49.3 ms | 0.19 ms |
| `external` | 1 | 256 | 49.0 ms | 0.19 ms |
| `external` | 100 | 256 | 174.2 ms | 0.68 ms |
| `external` | 150 | 256 | 240.0 ms | 0.94 ms |

So with no trigger the payload costs 240 ms instead of 49 ms — that is the
timeout being waited out 256 times, not extra work. Once the laser is connected
the same 256 records cost 256 × 952 µs ≈ 244 ms, i.e. the trigger costs nothing
that the laser was not already going to cost. The fixed arm/abort overhead is
~46 ms in every row, which is why a single-record capture looks the same
everywhere.

`AlazarStreamSensor` uses the same mechanism, so `stream` and `relay` mode
inherit it: approach readings when the laser is off, laser-locked records when
it is on. Set `PAM_AFM_LASER_PERIOD_US` if the repetition rate changes; the
timeout is derived from it as `ceil(1.3 × period / 10 µs)`.

### Channels and the 4 GS/s limit

4 GS/s is not a property of the board, it is a property of *one* channel: the
two 2 GS/s ADCs are interleaved in dual-edge sampling (DES) mode. Measured on
the board, quoted with the driver's own error strings:

| request | result |
| --- | --- |
| A, 4 GS/s | ok |
| B, 4 GS/s | `ApiInconsistentChannel` |
| A+B, 4 GS/s | `ApiDESOnlySupportedInSingleChannelMode` |
| A+B, 2 GS/s | ok |

So a second channel cannot record while A runs at 4 GS/s, and switching channels
per point is expensive: `setCaptureClock` ~65 ms, changing the channel mask
~55 ms, one arm/abort cycle ~46 ms -- which compounds, on the real class, into
**231 ms to leave B for A and 217 ms to come back**, i.e. ~450 ms per point.

There is no third analog input to fall back on. AUX I/O is a 3.3 V TTL digital
input -- `GET_AUX_INPUT_LEVEL` returns 0 or 1, and its only input functions are
Trigger Enable and a software-readable bit -- while TRIG IN is a comparator
whose data "cannot be digitized" (User Manual V1-5 p.45). ECLK, when fitted, is
a clock input. The board has exactly two analog channels.

So two analog signals at the same time means **A+B at 2 GS/s**; that is the only
simultaneous configuration this board has. For anything below 50 MHz it costs
nothing in bandwidth (2 GS/s is 20x Nyquist), but a 4096-point record then spans
2.048 µs instead of 1.024 µs, so the reconstruction's sample rate has to become
2e9 -- or the record has to drop to 2048 points to keep the same window. In
practice 2 GS/s was rejected for the imaging channel because of the ~500 MHz
interleaving artefact (see the next-but-one section).

**A relay on the AUX pin is the way out**: keep A at 4 GS/s and switch which
cable it sees. That is the `relay` feedback mode, documented below.

### One continuous stream instead of switching

That 46 ms is the abort/re-arm cycle, not the board. Leaving a single 4 GS/s
stream running and demodulating the newest record of each DMA buffer gives:

| trigger timeout | records/s | readings/s (8 records/buffer) |
| --- | --- | --- |
| 10 µs | 196 962 | 24 620 |
| 100 µs | 22 180 | 2 772 |
| 1 ms | 2 016 | 252 |
| 10 ms | 200 | 25 |

`AlazarStreamSensor` wraps exactly that: `TRIG_EXTERNAL` plus a timeout, one or
both channels, host-side demodulation with a precomputed phasor (~6 µs per
reading). With the laser off the board self-triggers; with the laser on it
follows the laser as long as the timeout is longer than the laser period.
Approach feedback and the imaging payload then come from the same stream, with
no reconfiguration between points.

With `--channels A+B` the board runs at 2 GS/s, both inputs are de-interleaved
from each record (`A0 B0 A1 B1 ...`), `read()` returns the amplitude of
`--signal-channel`, and `latest()` returns both channels as a dict. Measured at
2 GS/s dual: 199 626 records/s, 249.9 readings/s at a 1 ms timeout, with A and B
showing independent noise as expected.

```bat
python -m Alazar_imaging.AlazarStreamSensor --selftest
python -m Alazar_imaging.AlazarStreamSensor --probe --duration 3
python -m Alazar_imaging.AlazarStreamSensor --probe --channels A+B --signal-channel B
python -m Alazar_imaging.AlazarStreamSensor --probe --channel B --sample-rate-hz 2e9
```

### Keeping 4 GS/s: a relay on the AUX pin

The three obvious ways to get an approach signal *and* a 4 GS/s imaging input
all fail, and it is worth writing down why, because each one looks fine until
it is measured:

| approach | why it does not work |
| --- | --- |
| one cable into A, demod at 4 GS/s | the approach monitor and the transducer are two different signals on two different connectors; they cannot be summed into one input |
| approach on B at 100 MS/s, reconfig per point | works, but a B→A→B round trip measured **231 ms + 217 ms** per point, and the clock change is a PLL relock, not a memory copy |
| A+B together at 2 GS/s | the only simultaneous configuration, but at 2 GS/s the two ADCs are interleaved, and the interleaving mismatch puts a **spurious tone near 500 MHz** in the spectrum. Below 50 MHz that should not matter -- yet it was observed on this board, so 2 GS/s is not trusted for the imaging channel |

What is left is to keep **A at 4 GS/s for the entire scan** and move the
*connector*, not the clock. A two-way signal relay on the CH A input does that,
driven by the AUX I/O pin configured as `AUX_OUT_SERIAL_DATA` (the SDK's
"general purpose digital output", level 0/1). AUX cannot carry a signal, but it
can certainly carry a logic level, and toggling it mid-stream was verified not
to disturb the acquisition.

```
AUX centre pin --[1k]-- base of a small NPN / logic-level MOSFET
                          collector -- relay coil -- +5 V   (flyback diode across the coil)
                          pole 1 common -- CH A centre pin
                          pole 1 N.O.   -- approach monitor
                          pole 1 N.C.   -- PA transducer
                          pole 2 common -- 50 ohm to ground
                          pole 2 N.O.   -- PA transducer      crossed on purpose: pole 2
                          pole 2 N.C.   -- approach monitor   terminates whichever cable
                                                              CH A is not connected to
```

Pole 2 is deliberately crossed against pole 1. Energised, CH A sits on the
monitor and the transducer sees the dummy load; released, the roles swap. Wiring
pole 2 the same way round as pole 1 would put the 50 Ω load on the *selected*
cable, halving the source impedance -- which looks like a 6 dB drop and is easy
to misread as a sensitivity change.

Do not drive the coil from the pin. Use a signal relay with gold contacts or a
reed relay (closes in ~1 ms); a power relay will inject switching noise into a
microvolt-level PA signal.

Prefer a **DPDT** relay over an SPDT one -- still one AUX line. One pole
switches the signal into CH A, the other parks the unselected cable on a 50 Ω
dummy load, so the source stays terminated and CH A is connected at every
instant. With an SPDT, put the transducer on the normally-closed contact so a
power loss parks CH A on the PA signal rather than leaving it open.

**Is the switching hard on CH A? No.** Signal and reed relays are
break-before-make, so the two cables are never joined and neither source ever
drives the other. During the transfer (a few hundred µs) the input has no DC
path and the reading is invalid, but that is not a stress on the part: the input
has a 50 Ω termination and ESD diodes, and at millivolt levels there is no
arcing across the contacts (that needs tens of volts). What damages an input is
overvoltage, not switching -- keep both cables inside the selected input range.
Two things do matter, and neither is destructive: bond both cable shields to the
same ground at the digitizer end, or the two sources drive a ground loop through
the contacts; and keep the coil wires away from the signal wires, because the
flyback transient couples capacitively rather than through the contacts.

`PAM_AFM_FEEDBACK=relay` selects this path. The stream is configured once, at
the start of the scan, on A at 4 GS/s; each point flips the relay to the
approach cable, runs the PI, flips to the transducer, waits
`PAM_AFM_RELAY_SETTLE_MS` (default 2.0 ms) and sums the newest
`PAM_AFM_RECORDS_PER_POINT` records out of the *same* stream. Nothing is
reconfigured, so the ~450 ms round trip disappears.

Measured on the board with `_test_relay_fb.py` (no relay fitted, both cables
summed -- the numbers below are timing, not signal):

| step | cost |
| --- | --- |
| `prepare()` (one-time 4 GS/s configure + arm) | 206 ms |
| approach reading, laser off | 4.9 ms (203 Hz) |
| `payload(1)` incl. 2 ms relay settle | 4.9 ms |
| `payload(256)`, laser off | 160 ms |
| `payload(256)`, locked to a 1050 Hz laser | ~244 ms (256 × 952 µs) |
| payload dtype | `uint32`, shape `(1, 4096)` |

```bat
set PAM_AFM_FEEDBACK=relay
set PAM_AFM_RELAY_SETTLE_MS=2.0
python -m Alazar_imaging.AlazarAuxRelay --level 1
python -m Alazar_imaging.AlazarAuxRelay --toggle 0.5
```

The relay's `active_level` is a constructor argument, because which position is
"approach" depends on which contact the monitor is on. `AlazarAuxRelay` exposes
`--active-level {0,1}` for that. Until a relay is actually fitted, `relay` mode
still runs -- it just sums both cables, which is the right way to bench-test the
code path without touching the optics.

### On-FPGA FFT

`DSP_MODULE 0 id=0x10000 (FFT) v5.32, maxLength 4096, 12 bits`. With
`fftSetup` at 4096 points the board returns 2048 `FLOAT_AMP2` bins per record,
so the 10 MHz peak is read without a host-side FFT: **325 readings/s** versus
**20 readings/s** for the abort-per-reading time-domain path. (That 20 is the
re-arm cost, not the path's ceiling: a continuous time-domain stream does
252–24 620 readings/s, see the table above.) Both modes run at 100 MS/s, because
a 4096-sample record then spans 40.96 µs — 409.6 cycles of the carrier, against
10.24 cycles at 4 GS/s.

`--channel B` is supported. The FFT block processes one input, so B at 100 MS/s
works and B at 4 GS/s does not (see the channel table above).

Three traps, all found on hardware:

- **FFT/DSP methods live on `DspModule`, not on `Board`.** Probing
  `hasattr(ats.Board, "fftSetup")` wrongly suggests the SDK is missing six
  symbols. Use `board.dspGetModules()[0]`.
- **Never abort and restart the capture per reading.** After a
  `dspAbortCapture()` the second `beforeAsyncRead()` does not re-arm the FFT and
  the first buffer comes back **1854–1882 of 2048 bins NaN**. Start once, keep
  the stream running, and get freshness from `flush()`, which reads and discards
  exactly `buffer_count` buffers (327 µs at 8×4096 @ 100 MS/s).
- **`ats.AlazarAllocBufferU16` is not a module attribute** — it lives on the
  loaded DLL object. Allocate through `ats.DMABuffer(handle, c_uint16, bytes)`
  and reinterpret the memory as float32; it owns the matching free.

### Timing

The loop measures its own period, so the clock has to be able to resolve it.
`time.monotonic()` on Windows is backed by `GetTickCount64` and only resolves to
about 15 ms; two consecutive reads inside one control step often return the same
value, `dt` comes out as 0, and the controller silently falls back to its
nominal period. The scan engine uses `time.perf_counter()`.

## Backups

The working tree on the target must be **clean before a scan**, because a `.mat`
file is only reproducible if the code that produced it is committed. The
discipline is: verify, then `git add` the specific files, then commit with a
message that says what was measured and on which machine (by tailnet IP), then
`git push`. Uncommitted work on `100.79.154.4` is work that does not exist.

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
- Two laser-free entry points -- `PAM_Main_Prior.py` (Prior ProScan) and
  `PAM_Main_Nanomax_ClosedLoop.py` (NanoMax) -- that run the whole
  stage -> acquisition -> `.mat` chain with no laser path in the program at all.
- A two-point rectangle scan in the shared pre-alignment panel: mark two
  opposite corners with the hand pad or the terminal, and the panel builds the
  serpentine scan, snapping the span to whole steps and returning to the start
  corner in short legs.

## Recommended Entry Points

| Entry point | Purpose | Status |
| --- | --- | --- |
| `PAM_Main_Nanomax.py` | Main integrated NanoMax PAM program; sample or probe target | Recommended |
| `PAM_Main_Prior.py` | Prior ProScan PAM with no laser; same panel, dashboard and run log | Recommended for a Prior stage |
| `PAM_Main_Nanomax_ClosedLoop.py` | NanoMax closed-loop PAM with no laser | Recommended for a laser-free NanoMax |
| `PAM_Main_Nanomax_AFM.py` | AFM approach/retract scan: Z tracks a feedback setpoint, one A-line per point | Recommended for surface tracking |
| `Tool_code/nanomax_motion_debug_panel.py` | Motion-only panel for MAX311D/MAX312D; no DAQ or laser | Recommended for motion checks |
| `PAM_Main_LBTEK.py` | One-dimensional EM-LSS65-13C1 LBTEK scan | Separate validated path |
| `PAM_Main_SDK.py` | Older Prior/LBTEK-compatible PAM prototype | Legacy |
| `PAM_Main.py` | Older Prior stage + ATS9373 loop | Legacy |
| `PAM_Main_software.py` | Older GUI acquisition application | Legacy |
| `NI_DAQ_based/` | Nanoscan/NI-DAQ experiments | Experimental/legacy |
| `Alazar_imaging/Samples_Python/` | Vendor ATS9373 Python examples and wrapper source | Reference |

The first three entry points share the same pre-alignment panel, acquisition
dashboard, trajectory builder, `.mat` writer and run-log format; they differ
only in which stage they drive and whether a laser exists in the process.
`PAM_Main_Nanomax.py` is the only one with laser control -- the other two have
no laser path at all, which is what makes them safe to run with the laser
hardware powered down. The remaining entry points are older and carry different
hardware assumptions; do not mix them with the current workflow.

### Verification entry points

Run these first on any machine you did not build yourself. None of them starts a
scan or changes a laser state; the two marked **Moves stages** do move hardware.

| Entry point | Purpose | Hardware touched |
| --- | --- | --- |
| `Tool_code/acceptance_alazar_ats9373.py` | ATSApi + DAQ wrapper acceptance | Digitizer only |
| `Tool_code/acceptance_alazar_ats9373.py --capture` | One real laser-free acquisition | Digitizer only |
| `Tool_code/acceptance_trigger_frequency.py` | Measure the external trigger rate | Digitizer only |
| `Tool_code/acceptance_nanomax_prior.py` | Small guarded travel on both stages | **Moves stages** |
| `Tool_code/acceptance_prior_nanomax_nolaser.py` | Laser-free entry-point self-check, no hardware needed | None |
| `Tool_code/nanomax_motion_debug_panel.py` | Interactive motion-only panel | **Moves stages** |
| `Tool_code/validate_laser_panel_no_hardware.py` | Import check for the laser panel | None |
| `Tool_code/test_afm_offline.py` | AFM engine + PI controller + the three interfaces + both sample shapes, 142 checks | None |
| `python -m Nanomax.afm_pi --selftest` | C++/Python controller agreement | None |
| `python -m Nanomax.afm_pi --compare` | 200-step bit-exact DLL vs reference | None |
| `PAM_Main_Nanomax_AFM.py --dry-run` | Full AFM loop on a synthetic surface | None |

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
├── PAM_Main_Nanomax_ClosedLoop.py   # NanoMax closed-loop PAM, no laser
├── PAM_Main_Nanomax_AFM.py          # AFM approach/retract scan, PI Z loop
├── PAM_Main_Prior.py                # Prior ProScan PAM, no laser
├── PAM_Main_LBTEK.py                # validated 1D LBTEK PAM path
├── PAM_Main.py                      # legacy Prior acquisition loop
├── PAM_Main_SDK.py                  # legacy SDK-based prototype
├── PAM_Main_software.py             # legacy GUI application
├── Alazar_imaging/                  # hardware wrappers and ATS examples
│   ├── AlazarNPTSystem.py           # ATS9373 NPT acquisition wrapper
│   ├── AlazarFFTSpectrum.py         # on-FPGA FFT readout, trigger-free
│   ├── AlazarStreamSensor.py        # continuous stream + host demod, 250 Hz
│   ├── AlazarAuxRelay.py            # AUX pin as a TTL output, for a 2-way relay
│   ├── BPC303NativeController.py   # closed-loop BPC303 controller
│   ├── PriorStageAdapter.py         # Prior ProScan behind the BPC303 interface
│   ├── PriorUnifiedStage.py         # Prior SDK transport
│   ├── MDT693BController.py         # open-loop MDT controller
│   ├── cbox_d2xx_controller.py      # CBOX FTDI protocol wrapper
│   ├── toptica_dlc_controller.py    # TOPTICA TCP command client
│   └── Samples_Python/              # Alazar vendor examples and atsapi source
├── Nanomax/                         # panels, trajectories, runtime, I/O
│   ├── prealign_panel.py            # closed-loop sample panel
│   ├── open_loop_panel.py           # open-loop probe panel
│   ├── acquisition_panel.py         # fixed acquisition dashboard
│   ├── scan_utils.py                # range, step, pattern, and travel checks
│   ├── afm_scan.py                  # approach/retract state machine
│   ├── afm_control.py               # the Z criterion as a pluggable interface
│   ├── afm_retract.py               # how high to lift between points
│   ├── afm_pi.py                    # digital PI: ctypes binding + reference
│   ├── afm_pi.cpp                   # the compiled controller (stable C ABI)
│   ├── data_io.py                   # MAT packaging and safe save/rename
│   ├── result_preview.py            # data-processing bridge
│   ├── terminal_panel.py            # shared terminal rendering helpers
│   ├── no_laser_manager.py          # laser-free stand-in for the laser manager
│   └── run_log.py                   # persistent startup/cleanup state log
├── Tool_code/                       # standalone diagnostics and tests
│   ├── acceptance_alazar_ats9373.py    # digitizer acceptance check
│   ├── acceptance_trigger_frequency.py # external trigger rate measurement
│   ├── acceptance_nanomax_prior.py     # guarded stage acceptance check
│   ├── acceptance_prior_nanomax_nolaser.py # laser-free entry-point self-check
│   ├── nanomax_motion_debug_panel.py   # motion-only interactive panel
│   ├── test_afm_offline.py             # 142 offline checks for the AFM path
│   ├── build_afm_pi.py                 # compiler-agnostic DLL build
│   ├── example_control_source.py       # template for a custom Z criterion
│   ├── example_height_map.csv          # template for a surface height map
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

# Digitizer only - measures the external trigger rate
python Tool_code\acceptance_trigger_frequency.py

# Moves stages. Power them on first and read the safety note in the file.
python Tool_code\acceptance_nanomax_prior.py
```

Expected results on a healthy machine, for reference:

| Check | Expected |
| --- | --- |
| `getBoardKind()` | `29` (ATS9373) |
| `getChannelInfo()` | 2 channels, 12 bits per sample |
| `configure_board(4000MSPS)` | completes in well under a second |
| `--capture` | 2 buffers, 20 records, 81,920 bytes, non-zero noise floor |
| `acceptance_trigger_frequency.py` | methods A and B agree to <0.05 % |
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

## Trigger Frequency Measurement

`Tool_code/acceptance_trigger_frequency.py` answers one question with hard
numbers: **what rate is the external trigger actually running at?** Run it when
a scan is slower or faster than expected, or after changing the trigger source.
It opens the digitizer only — no stage is opened, nothing moves, and no
acquisition parameter of the imaging programs is altered.

```powershell
python Tool_code\acceptance_trigger_frequency.py
python Tool_code\acceptance_trigger_frequency.py --expect 1000
```

`--expect HZ` additionally prints the deviation from a nominal rate.

### Why the trigger rate matters

Each record needs exactly one trigger, and each scan point needs
`records_per_point` records — 256 for `PAM_Main_Prior.py`, 512 for the NanoMax
programs. The trigger rate therefore sets the scan speed directly:

| Trigger rate | Prior, s/point | NanoMax, s/point |
| --- | --- | --- |
| 1000 Hz | 0.256 | 0.512 |
| 1050 Hz | 0.244 | 0.488 |
| 50 Hz | 5.12 | 10.24 |

A source that has silently dropped from ~1 kHz to 50 Hz makes every scan twenty
times slower with no error message anywhere. This script is how you catch that.

**A slow trigger also silently loses every point.** The host waits
`PAM_ACQ_TIMEOUT_MS` (default `1000`) for one point's records. That wait has to
exceed the point duration above, or the host gives up before the trigger has
produced the records and the run ends with a normal-looking `RUN_END_NORMAL`,
`DATA_SAVE_SKIPPED reason=no_packageable_data`, and an empty `.mat`. At 50 Hz a
Prior point needs 5.12 s, so the 1 s default cannot work; at 1050 Hz it needs
0.244 s and the default is comfortable.

`PAM_ACQ_TIMEOUT_MS` is a **host-side wait, not a digitizer setting** -- raising
it changes no acquisition parameter. Measured on the target: 50 Hz with the
default 1000 ms lost all 25 points of a 4x4 um scan; the same scan at 50 Hz with
8000 ms saved 25/25 in 133.8 s; at 1050 Hz the default 1000 ms saved 25/25 in
12.25 s.

**A run that lost its points is no longer reported as a plain success.** Every
point is checked as it arrives: when the digitizer hands back no buffer the
program counts it and writes one `ACQUISITION_POINT_EMPTY` line per point
(`reason=daq_returned_no_buffer`, plus `consecutive` and the running
`daq_empty_points`). `ACQUISITION_DONE`, `RUN_END_NORMAL`, `RUN_END_ERROR`, and
`RUN_END_INTERRUPTED` all carry `daq_empty_points`, so the count reaches the run
log. When every point was empty the terminal event is still `RUN_END_NORMAL` --
the four-event terminal contract is unchanged -- but the run log reports status
`no_data` instead of `normal`, and the sample X/Y zero datum is no longer
trusted. A following run with `PAM_SAMPLE_START_ZERO_POLICY=auto` therefore
rebuilds the datum (`zero_reason=no_data_requires_rebuild`) rather than assuming
the previous run left a good one behind.

The check reuses the exact predicate the save path uses, so the run-time count
and the save-time `DATA_SAVE_SKIPPED reason=no_packageable_data` can never
disagree: `no_data` appears exactly when no point was packageable, and a partial
loss still saves the good points and reports `DATA_SAVED` with `skipped_points`.

**A run whose save failed is not a clean normal end either.** `RUN_END_NORMAL` is
written before the final save, so a run whose save reported failure used to read
as a plain success with `zero_datum_ready` True and no `.mat` on disk. The run log
now reports status `save_failed` instead (`zero_reason=save_failed_requires_rebuild`,
so `auto` rebuilds the datum), derived from the `DATA_SAVE_ONCE_RETRY_NEEDED` line
the program writes when the save returns `status: failed`. The terminal event and
the four-event contract are unchanged.

### The two methods

**Method A — digitise the trigger signal on the sample LSB.**
The ATS9373 User Manual (p.45) states that when External Trigger Input is the
trigger source, the LSB of each 12-bit sample is replaced by the live state of
the trigger signal. One long record is therefore a 1-bit sampled copy of the
trigger waveform, clocked by the board's own +/-2 ppm oscillator. Counting
rising edges in that bit and dividing by the exact record duration gives the
frequency without involving the Windows timer at all, plus the pulse width and
the jitter.

**Method B — count how many records the trigger produces.**
Arm N records with the imaging programs' exact trigger settings, time each run,
then least-squares fit `t = T0 + N / r` across several N. The fit removes the
fixed arming overhead and the residuals show whether the trigger is regular.

The two methods share no code path, so agreement between them is real evidence.
Method B is pulse-width agnostic, so it is the tie-breaker when method A sees
nothing — for example a trigger pulse far narrower than the chosen sample
period.

Method A prints every bit of the 16-bit word that moves. The **trigger bit** is
the one whose edge rate is orders of magnitude below the others; the remaining
bits are analog noise and toggle at roughly a quarter of the sample rate
(250 kHz at 1 MS/s, 25 MHz at 100 MS/s). On this board the trigger bit is
**bit 4** — bits 3..0 are always 0, because the 12-bit sample is left-justified
in the 16-bit word.

### Reference measurements

| Date | Source | Method A | Method B | Pulse width |
| --- | --- | --- | --- | --- |
| 2026-09-23 | original | — | 1000.4 Hz | — |
| 2026-10-06 | before the source swap | 50.0000 Hz | 49.9927 Hz | 5.21 us |
| 2026-10-06 | after the source swap | 1050.0000 Hz | 1049.8238 Hz | 4.07 us |

A healthy run prints, for example:

```text
  method A (sample LSB)      : 1050.0000 Hz
  method B (record counting) : 1049.8238 Hz
  agreement                  : 0.017 %
```

Note that the pulse width changed with the source (5.21 us -> 4.07 us), which
confirms a physically different source rather than a divided-down version of the
same one.

### Timeout behaviour

The board is configured with `setTriggerTimeOut(0)` — wait forever — exactly as
the imaging programs do, so a host-side wall clock (25 s for method A, 60 s for
method B) is the only thing preventing an indefinite hang when nothing is
connected. If no trigger arrives the script reports
`RESULT: FAIL (no external trigger detected)` and exits with code 3.

## Running the Main PAM Workflow

Run from the repository root so that relative output paths are predictable:

```powershell
python PAM_Main_Nanomax.py
```

This is a **console** program: it waits for
`input("Press Enter to START Experiment...")`, so it needs an interactive
terminal. It also refuses to start if another instance already holds the
controller. `PAM_Main_software.py` is the older PyQt5 GUI if you need a window.

Two laser-free variants share the same panel, dashboard and `.mat` contract:

```powershell
python PAM_Main_Prior.py                 # Prior ProScan, no laser
python PAM_Main_Nanomax_ClosedLoop.py    # NanoMax closed loop, no laser
```

Neither contains a laser path, so nothing about laser state can stop or alter
them. Both still need their stage powered on.

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

#### Two-point rectangle scan

Instead of hand-placing the scan start, mark two opposite corners of the region
you want and let the panel build the scan:

```text
p1                 mark the current X,Y as corner A
p2                 mark corner B; the rectangle is applied as soon as both exist
rect 0 0 5 5       set both corners numerically instead
rect               re-apply the rectangle from the marked corners
rect clear         drop the marked corners
set return_step 2  longest leg used to travel back to the start corner, um
```

Marking a corner re-reads the **live** stage position, so it does not matter
whether the stage was moved by the panel arrow keys, by a `set x/y` command, or
by the controller's hand pad. The panel then:

- snaps the span to a whole number of `STEP_UM` steps and reports the leftover
  as `snap_residual_x_um` / `snap_residual_y_um` in the run log;
- forces the S-shaped (serpentine) pattern, so an earlier `set SCAN_PATTERN
  raster` cannot silently undo it;
- moves the stage to the start corner itself, so the recorded start position is
  the corner the scan actually begins from;
- refuses the whole request, without moving anything, if the rectangle leaves
  the stage travel window.

If corner A is the *far* end of the trajectory, the travel back to the start
corner is split into legs of at most `RECT_RETURN_STEP_UM` (default `1 um`),
each settled before the next leg is issued. `RECT_RETURN_STEP_UM` is the panel's
own setting; it is **not** the program's `RETURN_STEP_UM`, which is the line step
used when the program returns the stage after the scan.

Every *single* pre-alignment move is capped as well. The panel refuses one
command larger than `MAX_ABS_MOVE_UM` (default `100 um`; change it with
`set max_move <um>` or `PAM_PANEL_MAX_ABS_MOVE_UM`, `0` disables the check) and
logs `PREALIGN_MOVE_REFUSED` instead of moving. The travel window alone cannot
catch this: a target computed in the wrong unit frame can sit inside the window
while still being a millimetre-scale command, which is exactly what happened on
2026-10-06. The segmented return legs are exempt, because the travel has already
been split before they are issued.

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
  A run that produced no usable DAQ data (status `no_data`) counts as
  untrusted, so `auto` rebuilds it (`zero_reason=no_data_requires_rebuild`).
  Valid values are `auto`, `always`, and `never`.
- The current script default is `PAM_SAMPLE_RETURN_XY_TO_ZERO_AT_END=1`, so a
  completed sample run returns X/Y to the low-end `(0, 0)`. Set it to `0` to
  return to the pre-alignment-selected start instead.
- Return motion is segmented along a straight line rather than issued as one
  jump: `PAM_SAMPLE_RETURN_STEP_UM` (default `0.1 um`),
  `PAM_PROBE_RETURN_STEP_UM`, and `PAM_PRIOR_RETURN_STEP_UM` (default `2 um`)
  for the Prior end-of-scan return. The pre-alignment panel's return to a
  two-point start corner uses its own `RECT_RETURN_STEP_UM` (default `1 um`).
  Every leg settles before the next is issued, so no single stage command
  exceeds the configured step.
- `SAMPLE_ZERO_XY_AT_END` is a separate BPC zero-datum rebuild switch and is
  `False` by default. Rebuilding the datum is slow and changes the controller
  reference; do not confuse it with moving to coordinate `(0, 0)`.
- **Unit-frame rule.** A position reading is only a valid move target in the unit
  frame it was read in. `ss` persists on the controller across programs, so
  `um_per_unit` must always be derived from the controller
  (`PriorStageAdapter.apply_ss_mode`) and never hardcoded, and a reading taken
  through one adapter must never be fed into another program's absolute move.
  Violating this on 2026-10-06 drove the stage 13.4 mm instead of a few microns
  and destroyed the fibre taper in the beam path; see the incident note in the
  Safety Checklist.

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
| `PAM_SAMPLE_RETURN_STEP_UM`, `PAM_PROBE_RETURN_STEP_UM` | Segmented return step, NanoMax (default 0.1 um) |
| `PAM_PRIOR_RETURN_STEP_UM` | Segmented return step, Prior end-of-scan return (default 2 um) |
| `PAM_PRIOR_POSITION_TOLERANCE_UM` | Prior per-point settle tolerance (default `PAM_STEP_UM`/5, capped at 1 um, raised to at least one SDK unit) |
| `PAM_SAMPLE_PREALIGN_ENABLE` | Enable the closed-loop pre-alignment panel |
| `PAM_PRIOR_PREALIGN_ENABLE` | Enable the pre-alignment panel in `PAM_Main_Prior.py` |
| `PAM_PANEL_MAX_ABS_MOVE_UM` | Refuse any one pre-alignment move larger than this (default 100 um; 0 disables) |
| `PAM_ACQ_TIMEOUT_MS` | Host-side wait for one point's records; must exceed the point duration at the measured trigger rate |
| `PAM_PRIOR_SS_MODE` | Prior unit frame: `high` (default, 0.01 um/unit), `micron`, `legacy`, `value`, `none` |
| `PAM_PRIOR_UM_PER_UNIT` | Set the Prior unit frame explicitly, skipping the controller read |
| `PAM_PRIOR_COM` | Prior controller COM port (default `4`) |
| `PAM_PRIOR_TRAVEL_UM`, `PAM_PRIOR_TRAVEL_MIN_UM`, `PAM_PRIOR_TRAVEL_MAX_UM` | Prior working window; the default is symmetric about the controller origin |
| `PAM_PRIOR_Z_ENABLE` | Enable Prior Z; off by default because this rig has no verified Z drive |
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
[3]  Show status        - working tree, the last three commits, and anything
                          not yet pushed to origin/main
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
- `backup` is safe to run on a hunch: when the working tree is already clean it
  prints `nothing staged - skipping commit`, skips the commit, and still pushes,
  so it never fails just because there was nothing to do.
- A failed `git commit` aborts **before** the push instead of reporting a bogus
  success. Exit codes: `0` success, `1` git or the repository was not found,
  `2` unknown action.

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
- **Compare remote file hashes with `certutil -hashfile`, not
  `type FILE | sha256sum`.** `type` rewrites LF as CRLF, so an LF file always
  hashes differently and a CRLF file always matches. The comparison then reports
  "files differ" for a correct deployment -- or "match" for a wrong one. Use
  `ssh host "certutil -hashfile D:\path\to\file.py SHA256"` and read the
  second line.
- **"Deployed" is not "synced".** Hash every file after a deploy. A working copy
  can sit one revision ahead of the target with nothing failing, and a test then
  breaks on the target for a reason that looks like a code bug.
- **`__pycache__/*.pyc` never matches between machines.** Bytecode embeds the
  source's absolute path. Exclude `__pycache__`, `*.egg-info`, and `.git` when
  comparing a copied tree, or you will chase dozens of phantom differences.
- **A non-zero `setTriggerTimeOut()` makes the digitizer trigger on its own.**
  This is the only reason a full DMA acquisition can be tested with no laser and
  no external trigger — see `acceptance_alazar_ats9373.py --capture`.
- **`atsapi.SAMPLE_RATE_*` constants are internal codes, not hertz.**
  `SAMPLE_RATE_1MSPS == 20` and `SAMPLE_RATE_4000MSPS == 128`. They are the
  correct argument for `setCaptureClock()`, but doing arithmetic on them raises
  no error and silently produces nonsense — a one-second record computed as
  "50000 seconds". Carry the real Hz value separately. There is no read-back
  either: `getParameter(CHANNEL_A, 0x10000)` returns `ApiInvalidData`.
- **The 12-bit sample is left-justified in the 16-bit word.** Bits 3..0 are
  always 0 and the 12-bit LSB sits in bit 4, so a scan that only looks at bit 0
  sees nothing. See the bit scan in `acceptance_trigger_frequency.py`.
- **Never time a trigger with "records / elapsed".** The fixed arming overhead
  dominates short runs and once produced a reading of 780 Hz for a 1 kHz source.
  Use the multi-N fit in `acceptance_trigger_frequency.py` method B.
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
| `docs/DLM3000_scope_report.md` | Yokogawa DLM3034: VXI-11 waveform readout, measured timings, the 2-9.4 Hz scalar measurement path, pitfalls |
| `reference/` | Integration decisions and lab notes |
| `run_logs/PAM_Main_Nanomax_run.log` | Previous run state; drives the startup zero-datum decision |
| `run_logs/pam_scan_speed_history.json` | Median scan speed per target/step, used for time estimates |

### Recommended first session on an unfamiliar machine

1. `python Tool_code\validate_laser_panel_no_hardware.py`
2. `python Tool_code\acceptance_alazar_ats9373.py`
3. Power the stages, then `python Tool_code\acceptance_nanomax_prior.py`
4. `python Tool_code\acceptance_alazar_ats9373.py --capture`
5. `python Tool_code\acceptance_trigger_frequency.py` — record the trigger rate,
   because it sets the scan speed
6. `python Tool_code\acceptance_prior_nanomax_nolaser.py` -- a no-hardware
   self-check of the laser-free entry points.
7. Only then start an entry point with a small range (`0.3 um` / step `0.1 um` /
   16 points) and the laser variables disabled: `PAM_Main_Nanomax.py` for the
   integrated program, or `PAM_Main_Prior.py` / `PAM_Main_Nanomax_ClosedLoop.py`
   when no laser should be in the process at all.

## Yokogawa DLM3034 Scope — slow reference over VXI-11

The lab's small scope (`YOKOGAWA,DLM3034,90Y500919,F2.00`, IP `192.168.1.100`)
can hand a waveform to the PC over the LAN. It is **read-only** here: nothing
in this repository changes an imaging parameter on it, and nothing arms
anything.

**Read `docs/DLM3000_scope_report.md` before touching this.** Every command and
every timing number in it was measured on this unit; the manual set we own has
no SCPI command table (the communication manual `IM DLM3054-17EN` is missing
and Yokogawa's CDN returns 403 for it).

### What it is good for

| | |
| --- | --- |
| One fresh record, one channel | ~450-480 ms → **~2.2 Hz** |
| One fresh record, four channels | ~590 ms → **~1.7 Hz** |
| Extra channels | cost ~110 ms total, not 4x |
| Bottleneck | the instrument, **not** the network (250 kB crosses the LAN in 3-11 ms) |
| **One scalar, via the scope's own measurement engine** | **2-9.4 Hz** (shorter record = faster), 0.4 ms per query, **no `:STOP` needed** |

A whole waveform is capped at ~2 Hz and **shrinking the transfer window does not
help** — 99 points and 9 999 points both refresh at 2.21-2.22 Hz, because the
0.45 s is the instrument producing a *new record*, not moving bytes. If a loop
only needs a number, let the scope compute it: `:MEASure:MODE ON` plus
`:MEASure:CHANnel<n>:<item>:VALue?` answers in 0.41 ms and refreshes at
**5.9 Hz** (TDIV 2 ms / 125 kpts) up to **9.4 Hz** (TDIV 20 us / 1 250 pts),
against 2.26 Hz for the waveform at the same settings. See report section 15.

The scope is a **slow reference / second opinion / calibration source**, never
the sensor of a fast loop — the ATS9373 on-FPGA FFT is the fast path.

### Files

| File | Role |
| --- | --- |
| `Nanomax/dlm_scope.py` | Zero-dependency VXI-11 client, `DlmScope`, `Waveform`, `Streamer`, `ScopeSource`, `MeasureStreamer` / `MeasureSource` |
| `Tool_code/dlm_scope_cli.py` | ASCII-only command line front end (GBK console safe) |
| `Tool_code/dlm_scope_web.py` | Browser scope view, standard library only |
| `Tool_code/test_dlm_scope_offline.py` | 84 offline checks, no instrument needed |

### Quick start

```bat
python -m Tool_code.dlm_scope_cli --check        :: identity, state, one record
python -m Tool_code.dlm_scope_cli --bench 10     :: latency, rate, distinct records
python -m Tool_code.dlm_scope_cli --follow 8     :: sustained streaming, snapshot latency
python -m Tool_code.dlm_scope_cli --measure RMS --tdiv 20E-6 --rlen 1250 --follow 10
python -m Tool_code.dlm_scope_web --channels 1,2,3,4 --port 8791
```

Then open `http://100.79.154.4:8791/`. Port 8765 is already taken on PAM-NEW by
a `pythonw.exe`.

### Three things that will bite you

1. **One client at a time.** Two processes issuing `:START`/`:STOP` interrupt
   each other: a single read went from 345 ms to 772 ms under contention, and
   `latest()` stopped returning the same record.
2. **`:STOP` does not freeze the record.** The scope keeps acquiring at ~2 Hz
   and `SEND?` just hands over the newest one, so `latest()` is not guaranteed
   to repeat `read()`.
3. **Multi-channel needs a long enough settle.** With the default 0.02 s only
   1 sweep in 4 is time-aligned (and it is *slower*); `read_multi()` raises the
   settle to `MULTI_SETTLE_S = 0.15 s` and then measures 8/8 aligned. Do not
   interleave other SCPI commands inside the sweep — the unlucky one pays
   ~130 ms + ~320 ms for the next record.
4. **`:MEASure:MODE` ships OFF.** Every `:MEASure:...:VALue?` answers `NAN`
   until it is switched on, which reads like "the command does not exist".
   Set the time base **first**, then call `measure_on()` — touching `TDIV` or
   `RLENgth` afterwards sends every value back to `NAN`. And `PKPK` / `MEAN`
   are not measurement items here; use `MAX - MIN` or `AMPL`.
5. **`410,"Query INTERRUPTED"` does not mean "unknown header".** It means the
   header exists but the response was never read. Only `113,"Undefined
   header"` means it does not exist — I mis-wrote off six real headers by
   trusting the 410.

### Codes, not volts

The payload is raw ADC codes. `CODES_PER_DIV = 8192.0` is the Yokogawa 16-bit
convention and is **not yet bench-calibrated** — see section 14 of the report.
`Waveform.codes` is exact either way, and a control loop only needs a monotone
proxy, so use the codes until someone calibrates the constant.

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

### Incident: unintended 13.4 mm move (2026-10-06)

A helper script read the stage position through a different unit frame than the
program it then drove, and passed that number back as an absolute `set xy`
target. The stage travelled 13.4 mm instead of the intended few microns and
destroyed the fibre taper. **No zero, home, or datum command was issued** -- the
log records `zero_after_return=False` and contains no `ZERO_*`/`HOME_*`/`DATUM_*`
event, so "it tried to zero the stage" was not what happened.

Rules that came out of it:

- Derive `um_per_unit` from the controller; never hardcode it, and never feed a
  reading from one unit frame into another program's absolute move.
- A launcher does not need `set xy` at all. The pre-alignment panel already takes
  the live stage position as its start, so the two-point flow needs only
  `p1`/`p2`.
- Guard absolute moves with a distance limit, and split long returns into short
  legs so a bad target can never become one long jump.
- A clamp must never turn a one-step jog into a long move. The pause-Z helper now
  refuses any clamp that would exceed the requested step instead of silently
  driving to the window edge (`ACQUISITION_PAUSE_Z_MOVE_REFUSED`,
  `reason=clamp_exceeds_step`), and it reads the stage's own lower bound when the
  stage provides one.

The stage was put back with `D:\LJB\_stage\_restore_prior_position.py`, which
derives the unit from the controller, refuses to move if the unit frame disagrees
with the recorded target, checks the target against the controller travel window,
and walks the whole return in 10 um legs.

## License

Released under the [MIT License](LICENSE). Hardware drivers, vendor SDKs,
manuals, and external processing skills remain subject to their own licenses.
