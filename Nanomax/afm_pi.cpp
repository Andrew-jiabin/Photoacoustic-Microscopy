// afm_pi.cpp -- incremental (velocity-form) digital PI controller for the
// AFM-style Z approach on the NanoMax sample stage.
//
// Why velocity form
// -----------------
// The actuator is a closed-loop piezo stage that takes an absolute Z position.
// The quantity the loop actually needs to decide is "how far do I move next",
// which is exactly what an incremental PI produces:
//
//     e[k]  = setpoint - y[k]
//     dz[k] = sign * ( Kp * (e[k] - e[k-1]) + Ki * dt * e[k] )
//     z[k+1] = z[k] + clamp(dz[k], -dz_max, +dz_max)
//
// This form has no separate integral state to wind up: the integrator *is* the
// accumulated position, and because the position is bounded by the stage
// window and the per-point travel budget, the integral is bounded with it.
// A setpoint change mid-scan is therefore bumpless -- nothing has to be
// re-initialised, and no derivative kick can occur on the first step because
// e[-1] is seeded from the first measurement.
//
// No monotonicity assumption
// --------------------------
// Nothing here assumes the measurement grows as Z advances.  ``sign`` says
// which way Z has to move for a positive error, and the loop will drive either
// direction.  The only structural assumption is that the measurement responds
// to Z at all -- if it does not, one of two progress guards fires (``diverged``
// when the error keeps growing, ``stalled`` when it stops improving) and the
// caller skips the point instead of grinding through the travel budget.
//
// Thread safety
// -------------
// One handle = one loop.  Handles are independent, but a single handle must not
// be stepped from two threads at once.  No global state is used.
//
// Build (no MSVC required, see Tool_code/build_afm_pi.py):
//     python -m ziglang c++ -target x86_64-windows-gnu -O2 -shared \
//         -o Nanomax/afm_pi.dll Nanomax/afm_pi.cpp

#include <cmath>
#include <cstring>
#include <new>

#ifdef _WIN32
#  define AFM_PI_EXPORT extern "C" __declspec(dllexport)
#else
#  define AFM_PI_EXPORT extern "C" __attribute__((visibility("default")))
#endif

namespace {

enum Status {
    ST_OK = 0,
    ST_CONVERGED = 1,
    ST_SATURATED = 2,
    ST_Z_LIMIT = 3,
    ST_EXHAUSTED = 4,
    ST_DIVERGED = 5,
    ST_STALLED = 6,
    ST_BADARG = -1
};

const double kInf = 1e308;

// How many consecutive non-improving steps count as "the loop is not in
// control".  The default is fixed rather than tuned, because it is a property
// of the plant, not a knob; but it *is* overridable through the configuration
// because the blind travel it allows is ``stall_steps * dz_limit``, and that
// has to be able to cover a retract when the reading is saturated.
const int kStallSteps = 8;
const int kGrowSteps = 8;

inline double clampd(double v, double lo, double hi) {
    if (v < lo) return lo;
    if (v > hi) return hi;
    return v;
}

// Named is_finite_value, not finite: <math.h> on mingw already declares a
// ::finite(double), and the two would collide.
inline bool is_finite_value(double v) {
    return std::isfinite(v) != 0;
}

}  // namespace

// The layout of these two structs is part of the ABI: Nanomax/afm_pi.py
// mirrors them field for field in ctypes.  Doubles first, then ints, so there
// is no padding ambiguity on either side.
struct afm_pi_config {
    double kp;             // um per measurement unit
    double ki;             // um per (measurement unit * second)
    double dt_s;           // nominal period, used when the measured dt <= 0
    double dz_limit_um;    // cap on |dz| for one step
    double deadband;       // |e| <= deadband counts as on target
    double sign;           // +1: advancing Z raises the measurement
    double kp_boost;       // extra Kp multiplier while far from target
    double boost_error;    // |e| above which the boost applies (0 = off)
    double ff_gain;        // feed-forward on setpoint change (0 = off)
    double z_min_um;       // travel window; use +-1e308 for "no limit"
    double z_max_um;
    int hold_steps;        // consecutive in-deadband steps needed to lock
    int max_steps;         // hard cap on iterations
    int stall_steps;       // consecutive non-improving steps before giving up
};

struct afm_pi_output {
    double z_um;        // position after this step
    double dz_um;       // increment applied this step (after clamping)
    double error;       // e[k]
    double p_term;      // sign * Kp * de
    double i_term;      // sign * Ki * dt * e[k]
    double dt_s;        // period actually used
    int steps;          // steps taken so far
    int converged;      // 1 once the deadband has been held
    int saturated;      // 1 if dz was clamped this step
    int limit_hit;      // 1 if the Z window stopped the move
    int exhausted;      // 1 if max_steps was reached
    int diverged;       // 1 if the error kept growing
    int stalled;        // 1 if the error stopped improving
    int status;         // one of Status
};

struct afm_pi_state {
    afm_pi_config cfg;
    double z;
    double e_prev;
    double sp_prev;
    double e_abs_start;
    double e_best;
    int steps;
    int hold;
    int growing;
    int stall;
    int have_prev;
    int done;
    afm_pi_output last;
};

AFM_PI_EXPORT int afm_pi_default_config(afm_pi_config* cfg) {
    if (cfg == nullptr) return ST_BADARG;
    std::memset(cfg, 0, sizeof(*cfg));
    cfg->kp = 0.010;
    cfg->ki = 0.200;
    cfg->dt_s = 0.010;
    cfg->dz_limit_um = 0.300;
    cfg->deadband = 0.005;
    cfg->sign = 1.0;
    cfg->kp_boost = 1.0;      // 1.0 = no boost
    cfg->boost_error = 0.0;   // 0 = boost disabled
    cfg->ff_gain = 0.0;
    cfg->z_min_um = -kInf;
    cfg->z_max_um = kInf;
    cfg->hold_steps = 2;
    cfg->max_steps = 200;
    cfg->stall_steps = kStallSteps;
    return ST_OK;
}

AFM_PI_EXPORT void* afm_pi_create(const afm_pi_config* cfg) {
    if (cfg == nullptr) return nullptr;
    afm_pi_state* st = new (std::nothrow) afm_pi_state();
    if (st == nullptr) return nullptr;
    st->cfg = *cfg;

    // Reject configurations that cannot work, rather than silently misbehaving.
    if (!is_finite_value(st->cfg.kp) || !is_finite_value(st->cfg.ki) || st->cfg.kp < 0.0 ||
        st->cfg.ki < 0.0) {
        delete st;
        return nullptr;
    }
    if (st->cfg.dz_limit_um <= 0.0 || !is_finite_value(st->cfg.dz_limit_um)) {
        delete st;
        return nullptr;
    }
    if (st->cfg.sign == 0.0 || !is_finite_value(st->cfg.sign)) {
        delete st;
        return nullptr;
    }
    if (st->cfg.dt_s <= 0.0) st->cfg.dt_s = 1e-3;
    if (st->cfg.deadband < 0.0) st->cfg.deadband = 0.0;
    if (st->cfg.hold_steps < 1) st->cfg.hold_steps = 1;
    if (st->cfg.max_steps < 1) st->cfg.max_steps = 1;
    if (st->cfg.stall_steps < 1) st->cfg.stall_steps = kStallSteps;
    if (st->cfg.kp_boost < 1.0) st->cfg.kp_boost = 1.0;
    if (st->cfg.boost_error < 0.0) st->cfg.boost_error = 0.0;
    if (st->cfg.z_min_um > st->cfg.z_max_um) {
        delete st;
        return nullptr;
    }

    st->z = 0.0;
    st->e_prev = 0.0;
    st->sp_prev = 0.0;
    st->e_abs_start = 0.0;
    st->e_best = kInf;
    st->steps = 0;
    st->hold = 0;
    st->growing = 0;
    st->stall = 0;
    st->have_prev = 0;
    st->done = 0;
    std::memset(&st->last, 0, sizeof(st->last));
    st->last.z_um = 0.0;
    return static_cast<void*>(st);
}

// Seed the loop with where the stage actually is and what it reads there.
// Seeding e_prev from the first measurement is what keeps the first step free
// of a derivative kick.
AFM_PI_EXPORT int afm_pi_reset(void* handle, double z0, double measurement0) {
    if (handle == nullptr) return ST_BADARG;
    afm_pi_state* st = static_cast<afm_pi_state*>(handle);
    if (!is_finite_value(z0) || !is_finite_value(measurement0)) return ST_BADARG;
    st->z = z0;
    st->steps = 0;
    st->hold = 0;
    st->growing = 0;
    st->stall = 0;
    st->have_prev = 0;
    st->done = 0;
    st->e_abs_start = 0.0;
    st->e_best = kInf;
    st->e_prev = 0.0;
    st->sp_prev = 0.0;
    std::memset(&st->last, 0, sizeof(st->last));
    st->last.z_um = z0;
    return ST_OK;
}

// One control step.  ``dt_s`` may be <= 0, in which case the nominal period
// from the configuration is used -- that is the fallback when the caller has
// no reliable clock, not the normal path.
AFM_PI_EXPORT int afm_pi_step(void* handle, double setpoint, double measurement,
                              double dt_s, afm_pi_output* out) {
    if (handle == nullptr || out == nullptr) return ST_BADARG;
    afm_pi_state* st = static_cast<afm_pi_state*>(handle);
    if (!is_finite_value(setpoint) || !is_finite_value(measurement)) return ST_BADARG;

    const afm_pi_config& cfg = st->cfg;

    double dt = (is_finite_value(dt_s) && dt_s > 0.0) ? dt_s : cfg.dt_s;
    // A wildly long period (the caller was blocked for a second) would put a
    // huge Ki*dt kick into one step; cap it at 10x nominal instead.
    const double dt_cap = cfg.dt_s * 10.0;
    if (dt > dt_cap) dt = dt_cap;

    const double e = setpoint - measurement;

    if (!st->have_prev) {
        st->e_prev = e;          // no derivative kick on the first step
        st->sp_prev = setpoint;
        st->e_abs_start = std::fabs(e);
        st->e_best = std::fabs(e);
        st->have_prev = 1;
    }

    // Gain scheduling: while the error is large, a bigger Kp closes the gap in
    // fewer steps.  It is a pure multiplier on the proportional path only, so
    // the integral behaviour near the setpoint is unchanged.
    double kp = cfg.kp;
    if (cfg.boost_error > 0.0 && std::fabs(e) > cfg.boost_error) {
        kp *= cfg.kp_boost;
    }

    const double de = e - st->e_prev;
    const double p_term = cfg.sign * kp * de;
    const double i_term = cfg.sign * cfg.ki * dt * e;

    // Feed-forward: when the setpoint itself jumps, move proportionally at
    // once instead of waiting for the integral to catch up.
    double ff = 0.0;
    if (cfg.ff_gain != 0.0) {
        ff = cfg.sign * cfg.ff_gain * (setpoint - st->sp_prev);
    }

    double dz = p_term + i_term + ff;
    const double dz_raw = dz;
    dz = clampd(dz, -cfg.dz_limit_um, cfg.dz_limit_um);
    const int saturated = (dz != dz_raw) ? 1 : 0;

    double z_next = st->z + dz;
    int limit_hit = 0;
    if (z_next < cfg.z_min_um) {
        z_next = cfg.z_min_um;
        limit_hit = 1;
    } else if (z_next > cfg.z_max_um) {
        z_next = cfg.z_max_um;
        limit_hit = 1;
    }

    st->z = z_next;
    st->e_prev = e;
    st->sp_prev = setpoint;
    st->steps += 1;

    // Lock when the deadband has been held for hold_steps in a row.
    if (std::fabs(e) <= cfg.deadband) {
        st->hold += 1;
    } else {
        st->hold = 0;
    }
    const int converged = (st->hold >= cfg.hold_steps) ? 1 : 0;

    // Progress guard, two failure modes.  A measurement that ignores Z either
    // lets the error grow (wrong sign, runaway) or never improves (probe far
    // away, no coupling at all).  Without both checks the loop would quietly
    // grind through the whole travel budget and report a bogus point.
    //
    // Only steps taken *towards* the surface count as evidence of a dead loop.
    // A step away from it cannot be: when the probe starts inside the sample
    // (a retract that was too small for the next point's height) the reading
    // is clipped at its maximum, so "no improvement" is exactly what a correct
    // retreat looks like.  ``sign`` is the sign of d(reading)/dZ, so the only
    // steps that are *not* retreating have ``dz * sign >= 0``; a zero-length
    // step counts too, which is what keeps a gains-off guard check honest.
    // (Note the weaker condition ``dz * sign * e > 0`` is useless here: a
    // correctly wired loop always moves in the direction that reduces |e|, so
    // it would always be true.)
    const double e_abs = std::fabs(e);
    const int toward = ((dz * cfg.sign) >= 0.0) ? 1 : 0;

    if (e_abs < st->e_best) {
        st->e_best = e_abs;      // monotone ratchet: noise cannot fake progress
        st->stall = 0;
    } else if (toward) {
        st->stall += 1;
    }

    if (e_abs > st->e_abs_start * 1.5 + cfg.deadband) {
        st->growing += 1;
    } else {
        st->growing = 0;
    }
    const int diverged = (st->growing >= kGrowSteps) ? 1 : 0;
    const int stalled = (st->stall >= cfg.stall_steps) ? 1 : 0;

    const int exhausted = (st->steps >= cfg.max_steps) ? 1 : 0;

    int status = ST_OK;
    if (converged) {
        status = ST_CONVERGED;
    } else if (diverged) {
        status = ST_DIVERGED;
    } else if (stalled) {
        status = ST_STALLED;
    } else if (exhausted) {
        status = ST_EXHAUSTED;
    } else if (limit_hit) {
        status = ST_Z_LIMIT;
    } else if (saturated) {
        status = ST_SATURATED;
    }

    out->z_um = st->z;
    out->dz_um = dz;
    out->error = e;
    out->p_term = p_term;
    out->i_term = i_term;
    out->dt_s = dt;
    out->steps = st->steps;
    out->converged = converged;
    out->saturated = saturated;
    out->limit_hit = limit_hit;
    out->exhausted = exhausted;
    out->diverged = diverged;
    out->stalled = stalled;
    out->status = status;

    st->last = *out;
    if (converged || diverged || stalled || exhausted) st->done = 1;
    return status;
}

AFM_PI_EXPORT int afm_pi_get_state(void* handle, afm_pi_output* out) {
    if (handle == nullptr || out == nullptr) return ST_BADARG;
    afm_pi_state* st = static_cast<afm_pi_state*>(handle);
    *out = st->last;
    return ST_OK;
}

// Retuning mid-scan without rebuilding the handle keeps the position and the
// previous error, so the change is bumpless.
AFM_PI_EXPORT int afm_pi_set_gains(void* handle, double kp, double ki) {
    if (handle == nullptr) return ST_BADARG;
    if (!is_finite_value(kp) || !is_finite_value(ki) || kp < 0.0 || ki < 0.0) return ST_BADARG;
    afm_pi_state* st = static_cast<afm_pi_state*>(handle);
    st->cfg.kp = kp;
    st->cfg.ki = ki;
    return ST_OK;
}

// Change the per-step travel cap without disturbing the loop state.
//
// The caller uses this to hold a *speed* rather than a step size: it passes
// ``speed_um_s * dt`` measured over the step that just finished, so the loop
// closes at the requested um/s no matter how long a step actually took (a
// settle, a slow digitizer read, an OS hiccup).  Clamping a fixed step instead
// would make the real speed drift with the loop period -- slow when the host is
// slow, fast when it is idle -- which is exactly the behaviour that breaks
// probes.
AFM_PI_EXPORT int afm_pi_set_dz_limit(void* handle, double dz_limit_um) {
    if (handle == nullptr) return ST_BADARG;
    if (!is_finite_value(dz_limit_um) || dz_limit_um <= 0.0) return ST_BADARG;
    afm_pi_state* st = static_cast<afm_pi_state*>(handle);
    st->cfg.dz_limit_um = dz_limit_um;
    return ST_OK;
}

AFM_PI_EXPORT void afm_pi_destroy(void* handle) {
    if (handle == nullptr) return;
    delete static_cast<afm_pi_state*>(handle);
}

AFM_PI_EXPORT const char* afm_pi_version(void) {
    return "afm_pi 1.4 (incremental PI, C++/C ABI)";
}

// Deterministic closed-loop check, so the Python side can prove that the DLL it
// loaded computes the same numbers as the reference implementation.
//
// Plant: y(z) = plant_gain * z, starting at z = 0 with y = 0.  Setpoint 1.0,
// 40 fixed steps of dt = 1 ms.  The gains are chosen so the loop actually
// converges (with plant_gain = 1 the error falls by ~5x per step), which makes
// the returned Z a meaningful number as well as a reproducible one.
AFM_PI_EXPORT int afm_pi_selftest(double plant_gain, double* final_z, int* steps) {
    if (final_z == nullptr || steps == nullptr) return ST_BADARG;
    if (!is_finite_value(plant_gain) || plant_gain == 0.0) return ST_BADARG;

    afm_pi_config cfg;
    afm_pi_default_config(&cfg);
    cfg.kp = 0.100;
    cfg.ki = 700.0;
    cfg.dt_s = 0.001;
    cfg.dz_limit_um = 1.0;
    cfg.deadband = 0.0;      // run the full 40 steps, no early lock
    cfg.hold_steps = 1000;   // never declare convergence in this test
    cfg.max_steps = 40;

    void* h = afm_pi_create(&cfg);
    if (h == nullptr) return ST_BADARG;

    double z = 0.0;
    afm_pi_reset(h, 0.0, 0.0);
    afm_pi_output out;
    for (int i = 0; i < 40; ++i) {
        const double y = plant_gain * z;
        afm_pi_step(h, 1.0, y, 0.001, &out);
        z = out.z_um;
    }
    *final_z = z;
    *steps = out.steps;
    afm_pi_destroy(h);
    return 0;
}
