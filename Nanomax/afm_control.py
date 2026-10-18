# -*- coding: utf-8 -*-
"""The Z criterion as a pluggable interface.

Why this exists
---------------
The scan engine must not know *what* it is regulating.  It only needs a number
to compare against ``setpoint``, in whatever unit the caller declared.  Today
that number is the 10 MHz amplitude from the digitizer; tomorrow it may be a
demodulated phase, a lock-in output, a cantilever deflection, a photon count, or
something the user computes from several of those.  All of them are the same
thing to the controller: a scalar that responds to Z.

The contract
------------
A control source is any object with::

    unit : str                       # for logging and metadata only
    value() -> float | (float, float) | None

``value()`` returns the current reading, optionally with an SNR.  Returning
``None`` (or a non-finite number) means "no valid reading right now"; the scan
engine then skips the step instead of acting on garbage -- useful when an
external instrument is still settling.

Optionally::

    prepare()            # open / arm the instrument
    finish()             # close it
    describe() -> str    # one line for the run log
    ready() -> bool      # False = skip the step, same as value() is None

Nothing is enforced by inheritance.  ``adopt()`` normalises a plain callable, a
``measure()``-style object or a full source into this shape, so a user module
can be three lines long::

    def create_source(config):
        return lambda: read_my_lockin()

Adapters
--------
``CallableSource``   wrap ``fn()``.
``ConstantSource``   a fixed number (bench tests, "always converged").
``ScaledSource``     ``gain * base + offset``: change the unit the setpoint is
                     expressed in without touching the controller.
``SmoothSource``     exponential or median smoothing: lets a tighter deadband be
                     used without the loop chasing noise.
``load_source``      build any of the above from a string spec.

The unit question
-----------------
The setpoint is compared to ``value()`` verbatim, so the *unit of the setpoint
is the unit of the source*.  ``ScaledSource`` is the honest way to change it:
do not rescale inside the source and then forget which number the setpoint was
in.
"""

from __future__ import annotations

import importlib
import inspect
import math
import os
from typing import Callable, Optional


#: What a source must provide.  Documentation, not an ABC -- duck typing is the
#: point, and an ABC would only add a base class for user modules to import.
REQUIRED = ("value",)
OPTIONAL = ("prepare", "finish", "describe", "ready", "unit")


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #

class CallableSource:
    """Turn ``fn() -> number`` into a control source.

    ``fn`` may return a bare number, ``(value, snr)``, or ``None`` for "no
    reading".  This is what ``create_source()`` in a user module usually
    returns, and it is also what the digitizer wrappers are.
    """

    def __init__(self, fn: Callable[[], object], unit: str = "au", name: str = "callable",
                 prepare: Optional[Callable[[], None]] = None,
                 finish: Optional[Callable[[], None]] = None):
        if not callable(fn):
            raise TypeError("CallableSource needs a callable")
        self._fn = fn
        self.unit = str(unit)
        raw_name = str(name or "")
        # A lambda's __name__ is "<lambda>", which tells nobody anything.
        self._name = "callable" if raw_name in ("", "<lambda>") else raw_name
        self._prepare = prepare
        self._finish = finish

    def value(self):
        return self._fn()

    def prepare(self):
        if self._prepare is not None:
            self._prepare()

    def finish(self):
        if self._finish is not None:
            self._finish()

    def describe(self):
        return "%s [%s]" % (self._name, self.unit)


class ConstantSource:
    """A fixed reading.  For bench tests and for "the surface is not moving"."""

    def __init__(self, value: float, unit: str = "au"):
        self._value = float(value)
        self.unit = str(unit)

    def value(self):
        return self._value

    def prepare(self):
        return None

    def finish(self):
        return None

    def describe(self):
        return "constant %.6g %s" % (self._value, self.unit)


class ScaledSource:
    """``gain * base + offset``.

    The reason this exists: the setpoint is in the source's unit.  If a new
    instrument reports volts where the operator thinks in microns of gap, wrap
    it here and keep the controller, the setpoint and the log consistent.
    Negative gain is legitimate -- it flips the sense of the signal, which is
    the same thing ``approach_sign = -1`` does, only expressed on the input side
    instead of the output side.
    """

    def __init__(self, base, gain: float = 1.0, offset: float = 0.0, unit: str = ""):
        self.base = adopt(base)
        self.gain = float(gain)
        self.offset = float(offset)
        self.unit = unit or getattr(self.base, "unit", "au")

    def value(self):
        raw = self.base.value()
        if raw is None:
            return None
        value, snr = _split(raw)
        if not math.isfinite(value):
            return None
        return self.gain * value + self.offset, snr

    def prepare(self):
        _call_if_present(self.base, "prepare")

    def finish(self):
        _call_if_present(self.base, "finish")

    def describe(self):
        return "%s -> %.4g*x %+.4g %s" % (
            _describe(self.base), self.gain, self.offset, self.unit
        )


class SmoothSource:
    """Noise rejection in front of the controller.

    ``mode="ema"``: ``y += alpha * (x - y)``.  ``alpha=1`` passes the signal
    through unchanged; smaller is smoother and laggier.
    ``mode="median"``: median of the last ``window`` readings.  Better than an
    EMA against isolated spikes (a dropped digitizer buffer, a cosmic ray),
    worse against continuous noise.

    Smoothing is the honest alternative to widening the deadband: the deadband
    hides noise by refusing to act, which biases the landing point; smoothing
    removes the noise and keeps the loop acting on the true value.
    """

    def __init__(self, base, alpha: float = 0.25, window: int = 0, mode: str = "ema"):
        self.base = adopt(base)
        self.alpha = float(alpha)
        self.window = int(window)
        self.mode = str(mode).lower()
        if self.mode not in ("ema", "median"):
            raise ValueError("mode must be 'ema' or 'median'")
        if self.mode == "ema" and not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        if self.mode == "median" and self.window < 3:
            raise ValueError("median mode needs window >= 3")
        self.unit = getattr(self.base, "unit", "au")
        self._y = None
        self._history = []

    def value(self):
        raw = self.base.value()
        if raw is None:
            return None
        value, snr = _split(raw)
        if not math.isfinite(value):
            return None
        if self.mode == "ema":
            self._y = value if self._y is None else self._y + self.alpha * (value - self._y)
            return self._y, snr
        self._history.append(value)
        if len(self._history) > self.window:
            del self._history[0]
        ordered = sorted(self._history)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid], snr
        return 0.5 * (ordered[mid - 1] + ordered[mid]), snr

    def reset(self):
        self._y = None
        self._history = []

    def prepare(self):
        self.reset()
        _call_if_present(self.base, "prepare")

    def finish(self):
        _call_if_present(self.base, "finish")

    def describe(self):
        if self.mode == "ema":
            detail = "ema alpha %.2f" % self.alpha
        else:
            detail = "median of %d" % self.window
        return "%s -> %s" % (_describe(self.base), detail)


class _MeasureSource:
    """Adapts an object that only has ``measure()`` (the digitizer wrappers)."""

    def __init__(self, base):
        self.base = base
        self.unit = getattr(base, "unit", "au")

    def value(self):
        return self.base.measure()

    def prepare(self):
        _call_if_present(self.base, "prepare")

    def finish(self):
        _call_if_present(self.base, "finish")

    def describe(self):
        return _describe(self.base)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _split(raw):
    """``raw`` -> ``(value, snr)``, accepting a bare number or a tuple."""
    if isinstance(raw, tuple):
        value = float(raw[0])
        snr = float(raw[1]) if len(raw) > 1 and raw[1] is not None else 0.0
        return value, snr
    return float(raw), 0.0


def _call_if_present(obj, name):
    fn = getattr(obj, name, None)
    if callable(fn):
        fn()


def _describe(obj):
    fn = getattr(obj, "describe", None)
    if callable(fn):
        try:
            return str(fn())
        except Exception:
            pass
    return obj.__class__.__name__


def adopt(obj):
    """Normalise anything usable into an object with ``value()``.

    Accepts a control source, an object with ``measure()`` (the digitizer
    wrappers), or a plain callable.  Idempotent: adopting a source returns it
    unchanged, so this is safe to call at every layer.
    """
    if hasattr(obj, "value") and callable(getattr(obj, "value")):
        return obj
    if hasattr(obj, "measure") and callable(getattr(obj, "measure")):
        return _MeasureSource(obj)
    if callable(obj):
        return CallableSource(obj, unit=getattr(obj, "unit", "au"),
                              name=getattr(obj, "__name__", "callable"))
    raise TypeError(
        "a control source needs value(), or measure(), or be callable; got %r"
        % (type(obj).__name__,)
    )


def check_source(obj):
    """Return a list of problems with ``obj`` as a control source (empty = fine).

    Kept separate from :func:`adopt` so a user module can be validated and the
    *reason* reported, instead of a bare TypeError three points into a scan.
    """
    problems = []
    if not hasattr(obj, "value") or not callable(getattr(obj, "value")):
        problems.append("no value() method")
    unit = getattr(obj, "unit", None)
    if unit is not None and not isinstance(unit, str):
        problems.append("unit must be a string, got %r" % (unit,))
    return problems


def read_value(source):
    """One reading, normalised to ``(value, snr)``; ``(nan, 0)`` if invalid.

    Every caller should go through this rather than calling ``value()``
    directly, so "no reading" is handled in exactly one place.
    """
    raw = source.value()
    if raw is None:
        return float("nan"), 0.0
    value, snr = _split(raw)
    if not math.isfinite(value):
        return float("nan"), 0.0
    return value, snr


# --------------------------------------------------------------------------- #
# Loading a source from a string spec
# --------------------------------------------------------------------------- #

def load_source(spec, config=None):
    """Build a control source from ``spec``.

    ``spec`` forms::

        feedback                the caller's default (see below)
        const:1.25              a constant, in the setpoint's unit
        file:D:\\path\\src.py    load that file and call create_source(config)
        my_package.my_source    import that module and call create_source(config)

    A user module must define ``create_source(config)`` returning an object with
    ``value()``; a bare callable is also accepted and wrapped.  Optional
    ``prepare()`` / ``finish()`` / ``describe()`` / ``unit`` are honoured.

    ``config`` is passed through untouched, so a source can read its own
    settings from the same environment (use a distinct prefix -- do not reuse
    ``PAM_AFM_*`` names that the scan already reads, or the two will fight).
    """
    if spec is None:
        raise ValueError("control source spec is required")

    text = str(spec).strip()
    if not text or text.lower() in ("feedback", "auto", "default"):
        raise ValueError(
            "load_source() needs an explicit spec; 'feedback' means 'use the "
            "caller's default' and must be resolved before this call"
        )

    kind, sep, target = text.partition(":")
    if sep and kind.lower() == "const":
        return ConstantSource(float(target))

    if sep and kind.lower() == "file":
        import importlib.util

        path = os.path.abspath(target)
        if not os.path.exists(path):
            raise ValueError("control source file not found: %s" % path)
        module_spec = importlib.util.spec_from_file_location("pam_afm_control_source", path)
        if module_spec is None or module_spec.loader is None:
            raise ValueError("cannot load control source from %s" % path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        return _from_module(module, path, config)

    if sep and kind.lower() in ("module", "py"):
        text = target

    return _from_module(importlib.import_module(text), text, config)


def _call_factory(factory, config):
    """Call ``create_source(config)``, tolerating a zero-argument factory.

    A user module may legitimately write either signature; guessing wrong once
    would otherwise be a ``TypeError`` several layers away from the cause.
    """
    try:
        sig = inspect.signature(factory)
        takes_arg = any(
            p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
            for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        takes_arg = True
    return factory(config) if takes_arg else factory()


def _from_module(module, label, config):
    factory = getattr(module, "create_source", None)
    if factory is None:
        # A module may also expose SOURCE (an instance) instead of a factory.
        instance = getattr(module, "SOURCE", None)
        if instance is None:
            raise ValueError("%s must define create_source(config) or SOURCE" % label)
        return adopt(instance)
    built = _call_factory(factory, config)
    if built is None:
        raise ValueError("create_source() in %s returned None" % label)
    source = adopt(built)
    problems = check_source(source)
    if problems:
        raise ValueError("create_source() in %s returned an unusable source: %s"
                         % (label, "; ".join(problems)))
    return source


def describe_source(source):
    """One line for the run log, never raising."""
    try:
        return _describe(adopt(source))
    except Exception:
        return source.__class__.__name__
