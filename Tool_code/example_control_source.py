# -*- coding: utf-8 -*-
"""Template for a user-supplied Z criterion.

Point the scan at this file and it will regulate whatever ``value()`` returns
instead of the 10 MHz amplitude::

    set PAM_AFM_CONTROL_SOURCE=file:D:\\LJB\\alazar_DAQ\\Photoacoustic-Microscopy\\Tool_code\\example_control_source.py
    set PAM_AFM_SETPOINT=90          # in this source's unit (deg)

The contract (see ``Nanomax/afm_control.py``) is deliberately small:

    unit        str                       logging / metadata only
    value()     number | (number, snr) | None
    prepare()   optional   open or arm the instrument
    finish()    optional   close it
    describe()  optional   one line for the run log

``None`` (or a NaN) means "no valid reading right now"; the scan engine then
fails that point with ``no_reading`` instead of acting on garbage.

The setpoint is compared to ``value()`` verbatim, so the unit of the setpoint is
the unit of the source.  If you need a different scale, wrap the source in
``afm_control.ScaledSource(base, gain=..., offset=...)`` rather than rescaling
here -- then the number in ``PAM_AFM_SETPOINT`` still means what it says.

``approach_sign`` still decides which way Z must move for a positive error, so a
signal that *falls* as the probe approaches needs no change here: set
``PAM_AFM_APPROACH_SIGN=-1``.

Nothing in this file is specific to a lock-in; it is a phase reading because
that is the most likely next thing after the amplitude.
"""

import math
import os


class DemoSource(object):
    """A phase reading that drifts, to prove the swap works end to end."""

    unit = "deg"

    def __init__(self, config):
        self.config = config
        self.prepared = False
        # Anything you need from the environment, read it here under your own
        # prefix -- do not reuse PAM_AFM_* names, the scan already reads those.
        self.offset = float(os.environ.get("DEMO_SOURCE_OFFSET", "90.0"))

    def prepare(self):
        self.prepared = True

    def value(self):
        # A bare number is allowed; (number, snr) is allowed too.
        return self.offset + 10.0 * math.sin(0.1), 20.0

    def finish(self):
        self.prepared = False

    def describe(self):
        return "demo phase source (deg), offset %.1f" % self.offset


def create_source(config):
    """Called once, before the first point.  Return the source.

    A zero-argument signature also works, and returning a bare callable is
    accepted: ``return lambda: my_lockin.read()``.
    """
    return DemoSource(config)
