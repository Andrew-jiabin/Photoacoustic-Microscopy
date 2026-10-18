# -*- coding: utf-8 -*-
"""How far to lift the sample between two imaging points.

The problem
-----------
After a point is taken the sample must be backed away from the probe *before*
X/Y moves, or the probe is dragged sideways through the surface.  How far is not
a constant:

* a **cell** is a dome.  On top of it the height barely changes between
  neighbouring points, so lifting a fixed 0.5 um per point wastes travel (and
  time) on every one of hundreds of points; at the *edge* the height changes by
  most of the cell height in a single step, and a fixed 0.5 um may not clear it.
* a **resolution target** is two flat levels.  Ninety-nine points in a row need
  nothing, and the one point that steps from the low bar to the high bar needs
  the full step height plus clearance.

So the lift has to be a function of *where we are going next*, not a constant.
That is what this module computes.

The interface
-------------
A policy is an object with::

    lift_um(z_found, here_xy, next_xy, history, sign) -> float

``z_found``   the Z at which the surface was found at this point (um)
``here_xy``   ``(x, y)`` of this point (um)
``next_xy``   ``(x, y)`` of the point about to be scanned, or ``None`` on the
              last point of the scan
``history``   ``[(x, y, z_found), ...]`` of the points already done this scan
``sign``      ``approach_sign``: ``+1`` if increasing Z approaches the probe

The returned distance is always positive and is clamped by the caller against
``max_retract_um`` and the Z window, so a policy cannot drive the stage into a
limit.

Policies
--------
``FixedLift``       the old behaviour: one number, always.
``RunningMaxLift``  lift to ``margin`` above the deepest Z seen so far.  Handles
                    a two-level target after the high level has been visited
                    once, and any surface that only ever gets higher.
``SlopeLift``       extrapolate the local slope one point ahead: the lift grows
                    at a cell edge and shrinks on the dome, with no user input.
``MapLift``         a user-supplied height map ``(x, y) -> height``.  The only
                    policy that knows about a step *before* stepping onto it.
``CompositeLift``   the max of several: never less safe than any of them.

``make_policy`` builds the default, which is a composite of the fixed floor, the
running max and the local slope.  The fixed floor means the new behaviour is
never *less* than the old one.
"""

from __future__ import annotations

import math
import os
from typing import Callable, Iterable, Optional


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #

class RetractPolicy:
    """Base class: subclasses implement ``lift_um``."""

    #: Human-readable name for the run log.
    name = "policy"

    def lift_um(self, z_found, here_xy=None, next_xy=None, history=(), sign=1.0):
        raise NotImplementedError

    def observe(self, z_found, here_xy=None, ok=True):
        """Record where the probe met (or nearly met) the surface.

        Called for every approach, including failures -- a failed approach that
        ran deeper than any success is exactly what a running-max policy must
        remember.  Default: nothing, because most policies work from ``history``
        and need no state of their own.
        """

    def reset(self):
        """Forget everything measured so far (call between scans)."""

    def describe(self):
        return self.name

    # Convenience so a policy can be used as ``callable(...)``.
    def __call__(self, *args, **kwargs):
        return self.lift_um(*args, **kwargs)


class FixedLift(RetractPolicy):
    """Always lift the same distance."""

    name = "fixed"

    def __init__(self, um: float):
        self.um = float(um)
        if self.um <= 0:
            raise ValueError("FixedLift needs a positive distance")

    def lift_um(self, z_found, here_xy=None, next_xy=None, history=(), sign=1.0):
        return self.um

    def describe(self):
        return "fixed %.3f um" % self.um


class RunningMaxLift(RetractPolicy):
    """Clear the deepest Z found so far, plus a margin.

    ``depth(z) = sign * z`` is "how far towards the probe".  The lift is
    ``deepest - depth(z_found) + margin``, so after the deepest point of the
    scan every later point starts at least ``margin`` clear of it, whatever the
    local height does.

    This is what makes a stepped sample safe *after* the first contact with the
    high level -- but not on the step itself, because nothing in the history
    knows the step is coming.  Pair it with :class:`MapLift` when the step
    position is known, or rely on ``max_approach_um`` to bound the surprise.
    """

    name = "running_max"

    def __init__(self, margin_um: float = 0.05, floor_um: float = 0.0,
                 max_um: float = 2.0):
        self.margin_um = float(margin_um)
        self.floor_um = float(floor_um)
        self.max_um = float(max_um)
        if self.margin_um < 0:
            raise ValueError("margin_um must be >= 0")
        self._deepest = None

    def observe(self, z_found, here_xy=None, ok=True):
        value = float(z_found)
        if not math.isfinite(value):
            return
        if self._deepest is None or value > self._deepest:
            self._deepest = value

    def reset(self):
        self._deepest = None

    def lift_um(self, z_found, here_xy=None, next_xy=None, history=(), sign=1.0):
        sign = 1.0 if sign >= 0 else -1.0
        # Work in "depth" (sign * z) so one expression covers both directions.
        deepest = sign * float(z_found)
        if self._deepest is not None:
            deepest = max(deepest, sign * self._deepest)
        for item in history:
            try:
                deepest = max(deepest, sign * float(item[2]))
            except (TypeError, IndexError, ValueError):
                continue
        lift = deepest - sign * float(z_found) + self.margin_um
        return _clamp(lift, self.floor_um, self.max_um)

    def describe(self):
        return "running max (margin %.3f um, floor %.3f um)" % (
            self.margin_um, self.floor_um
        )


class SlopeLift(RetractPolicy):
    """Extrapolate the local height slope one point ahead.

    The slope is estimated by least squares over the last ``window`` points,
    in "depth per um of XY distance", and multiplied by the distance to the next
    point.  On a dome the slope is ~0 at the top and large at the rim, so the
    lift follows the shape without a map.  ``safety`` > 1 adds margin for the
    curvature the straight-line extrapolation cannot see.

    Uses only points *already* measured, so it lags one point behind a step
    edge.  It is a smoothing of the needed lift, not a predictor of it; combine
    with :class:`RunningMaxLift`, which never forgets.
    """

    name = "slope"

    def __init__(self, margin_um: float = 0.05, floor_um: float = 0.0,
                 window: int = 5, safety: float = 2.0, max_um: float = 2.0):
        self.margin_um = float(margin_um)
        self.floor_um = float(floor_um)
        self.window = max(2, int(window))
        self.safety = float(safety)
        self.max_um = float(max_um)

    def lift_um(self, z_found, here_xy=None, next_xy=None, history=(), sign=1.0):
        if next_xy is None or here_xy is None:
            return _clamp(self.margin_um, self.floor_um, self.max_um)
        sign = 1.0 if sign >= 0 else -1.0
        step = _distance(here_xy, next_xy)
        if not step > 0:
            return _clamp(self.margin_um, self.floor_um, self.max_um)

        points = [(float(h[0]), float(h[1]), sign * float(h[2]))
                  for h in list(history)[-self.window:] if _looks_like_point(h)]
        if len(points) < 2:
            return _clamp(self.margin_um, self.floor_um, self.max_um)

        slope = _slope_along(points + [(float(here_xy[0]), float(here_xy[1]),
                                       sign * float(z_found))])
        # A negative slope means the next point is expected lower: only the
        # margin is needed.  A positive one is a rise to clear.
        rise = max(0.0, slope) * step * self.safety
        return _clamp(rise + self.margin_um, self.floor_um, self.max_um)

    def describe(self):
        return "local slope (window %d, safety %.1f, margin %.3f um)" % (
            self.window, self.safety, self.margin_um
        )


class MapLift(RetractPolicy):
    """Use a known height map: ``height(x_um, y_um) -> protrusion_um``.

    The map says how much *closer to the probe* the surface is at that point --
    a cell top is positive, a gap between cells is zero.  It is **not** a stage
    Z: the same map then works whether ``approach_sign`` is +1 or -1, because
    the lift is a distance, and the retract direction is already known.

    The lift is::

        max(floor, h(next) - h(here) + margin)

    i.e. exactly the rise to the next point plus clearance.  This is the only
    policy that can clear a step *before* falling off it, which is what a
    resolution target needs: the operator knows the bar height, so the one point
    that climbs is pre-empted instead of discovered by crashing into it.

    ``height`` may also be a ``lookup(x, y)`` object.  Unknown points should
    return ``nan`` rather than raise; the policy then falls back to the floor.
    """

    name = "map"

    def __init__(self, height: Callable[[float, float], float],
                 margin_um: float = 0.05, floor_um: float = 0.0,
                 max_um: float = 2.0, name: str = ""):
        if not callable(height):
            raise TypeError("MapLift needs height(x_um, y_um)")
        self.height = height
        self.margin_um = float(margin_um)
        self.floor_um = float(floor_um)
        self.max_um = float(max_um)
        self._name = name or "map"

    def lift_um(self, z_found, here_xy=None, next_xy=None, history=(), sign=1.0):
        if here_xy is None or next_xy is None:
            return _clamp(self.margin_um, self.floor_um, self.max_um)
        try:
            h_here = float(self.height(*here_xy))
            h_next = float(self.height(*next_xy))
        except Exception:
            return _clamp(self.margin_um, self.floor_um, self.max_um)
        if not (math.isfinite(h_here) and math.isfinite(h_next)):
            return _clamp(self.margin_um, self.floor_um, self.max_um)
        rise = h_next - h_here
        return _clamp(max(0.0, rise) + self.margin_um, self.floor_um, self.max_um)

    def describe(self):
        return "%s (margin %.3f um, floor %.3f um)" % (
            self._name, self.margin_um, self.floor_um
        )


class CompositeLift(RetractPolicy):
    """The maximum of several policies -- never less safe than any of them."""

    name = "composite"

    def __init__(self, *policies):
        flat = []
        for item in policies:
            if isinstance(item, (list, tuple)):
                flat.extend(item)
            elif item is not None:
                flat.append(item)
        if not flat:
            raise ValueError("CompositeLift needs at least one policy")
        self.policies = flat

    def lift_um(self, z_found, here_xy=None, next_xy=None, history=(), sign=1.0):
        best = 0.0
        for policy in self.policies:
            try:
                value = float(policy.lift_um(z_found, here_xy, next_xy, history, sign))
            except Exception:
                continue
            if math.isfinite(value) and value > best:
                best = value
        return best

    def observe(self, z_found, here_xy=None, ok=True):
        for policy in self.policies:
            try:
                policy.observe(z_found, here_xy, ok)
            except Exception:
                continue

    def reset(self):
        for policy in self.policies:
            try:
                policy.reset()
            except Exception:
                continue

    def describe(self):
        return "max of [%s]" % "; ".join(p.describe() for p in self.policies)


# --------------------------------------------------------------------------- #
# Height maps
# --------------------------------------------------------------------------- #

def grid_map(xs, ys, heights, name="grid"):
    """Build a ``height(x, y)`` from a rectangular grid.

    ``heights[j][i]`` is the height at ``xs[i], ys[j]``; ``nan`` marks unknown
    cells.  Nearest-neighbour lookup, which is what a scan grid needs -- the
    caller asks about exactly the points it will visit.
    """
    xs = [float(v) for v in xs]
    ys = [float(v) for v in ys]
    if not xs or not ys:
        raise ValueError("grid_map needs at least one x and one y")
    rows = [list(row) for row in heights]
    if len(rows) != len(ys):
        raise ValueError("heights must have one row per y (%d != %d)"
                         % (len(rows), len(ys)))

    def lookup(x, y):
        i = _nearest_index(xs, float(x))
        j = _nearest_index(ys, float(y))
        return rows[j][i]

    lookup.name = name
    return lookup


def csv_map(path, name=None):
    """Load a height map from CSV: a header row of x values, then ``y,h,h,...``.

    Blank cells and ``nan`` mean unknown.  This is the format a profilometer or
    an earlier scan can be dumped into without a converter.
    """
    path = os.path.abspath(path)
    with open(path, "r", encoding="utf-8-sig") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    if len(lines) < 2:
        raise ValueError("height map %s needs a header and at least one row" % path)

    header = _split_row(lines[0])
    xs = [float(v) for v in header[1:]]
    ys, rows = [], []
    for line in lines[1:]:
        cells = _split_row(line)
        ys.append(float(cells[0]))
        rows.append([_parse_height(v) for v in cells[1:]])
    return grid_map(xs, ys, rows, name=name or os.path.basename(path))


def constant_map(height_um=0.0, name="flat"):
    """A flat map -- useful to exercise the code path without a real surface."""
    def lookup(_x, _y):
        return float(height_um)
    lookup.name = name
    return lookup


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #

POLICY_NAMES = ("fixed", "running_max", "slope", "map", "auto")


def make_policy(kind="auto", base_um=0.5, margin_um=0.05, max_um=2.0,
                height=None, window=5, safety=2.0):
    """Build a policy from a short name.

    ``auto`` (default) is ``max(fixed base_um, running_max, slope)``: the old
    constant is the floor, so nothing that used to be safe becomes unsafe, and
    the two extra terms only ever add lift where the surface demands it.
    """
    kind = str(kind or "auto").strip().lower()
    if kind not in POLICY_NAMES:
        raise ValueError("retract policy must be one of %s, got %r"
                         % (POLICY_NAMES, kind))

    if kind == "fixed":
        return FixedLift(base_um)

    parts = []
    if base_um and base_um > 0:
        parts.append(FixedLift(base_um))
    if kind in ("auto", "running_max"):
        parts.append(RunningMaxLift(margin_um=margin_um, floor_um=0.0, max_um=max_um))
    if kind in ("auto", "slope"):
        parts.append(SlopeLift(margin_um=margin_um, floor_um=0.0,
                               window=window, safety=safety, max_um=max_um))
    if kind == "map":
        if height is None:
            raise ValueError("the map policy needs a height function")
        parts.append(MapLift(height, margin_um=margin_um, floor_um=0.0,
                             max_um=max_um))
    if len(parts) == 1:
        return parts[0]
    return CompositeLift(*parts)


def make_map_policy(spec, base_um=0.5, margin_um=0.05, max_um=2.0,
                    window=5, safety=2.0):
    """``make_policy("map")`` from a file or module spec.

    ``spec`` is ``file:...csv`` / ``.py`` or ``module:name``.  A ``.py`` module
    must define ``height(x_um, y_um)`` (or ``create_height(config)``).
    """
    text = str(spec or "").strip()
    if not text:
        raise ValueError("map policy needs a height map spec")
    kind, sep, target = text.partition(":")
    if not sep:
        kind, target = ("file", text) if text.lower().endswith((".csv", ".py")) else ("module", text)

    if kind.lower() == "file":
        path = os.path.abspath(target)
        if path.lower().endswith(".csv"):
            height = csv_map(path)
        else:
            height = _height_from_module(_load_module_from_file(path), path)
    else:
        import importlib
        module = importlib.import_module(target)
        height = _height_from_module(module, target)

    policy = MapLift(height, margin_um=margin_um, floor_um=base_um, max_um=max_um)
    policy._name = "map %s" % getattr(height, "name", target)
    return policy


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

def _height_from_module(module, label):
    fn = getattr(module, "height", None)
    if callable(fn):
        return fn
    factory = getattr(module, "create_height", None)
    if callable(factory):
        built = factory()
        if callable(built):
            return built
    raise ValueError("%s must define height(x_um, y_um)" % label)


def _load_module_from_file(path):
    import importlib.util

    if not os.path.exists(path):
        raise ValueError("height module not found: %s" % path)
    spec = importlib.util.spec_from_file_location("pam_afm_height_map", path)
    if spec is None or spec.loader is None:
        raise ValueError("cannot load height module from %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _clamp(value, lo, hi):
    value = float(value)
    if not math.isfinite(value):
        return float(lo)
    if lo and value < lo:
        value = float(lo)
    if hi and value > hi:
        value = float(hi)
    return value


def _distance(a, b):
    return math.hypot(float(b[0]) - float(a[0]), float(b[1]) - float(a[1]))


def _looks_like_point(item):
    try:
        return len(item) >= 3
    except TypeError:
        return False


def _slope_along(points):
    """Least-squares slope of depth vs distance travelled, in depth/um."""
    if len(points) < 2:
        return 0.0
    # Parameterise by cumulative distance rather than by index, so a
    # serpentine path with uneven steps still gives a slope per um.
    s = [0.0]
    for i in range(1, len(points)):
        s.append(s[-1] + _distance(points[i - 1], points[i]))
    n = float(len(points))
    mean_s = sum(s) / n
    mean_h = sum(p[2] for p in points) / n
    num = sum((s[i] - mean_s) * (points[i][2] - mean_h) for i in range(len(points)))
    den = sum((v - mean_s) ** 2 for v in s)
    if den <= 0:
        return 0.0
    return num / den


def _nearest_index(values, target):
    best_i, best_d = 0, None
    for i, value in enumerate(values):
        d = abs(value - target)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return best_i


def _split_row(line):
    if "," in line:
        return [c.strip() for c in line.split(",")]
    return line.split()


def _parse_height(text):
    text = (text or "").strip()
    if not text or text.lower() in ("nan", "none", "-"):
        return float("nan")
    return float(text)
