# Test control source used to prove the pluggable path works.
# Run with:  set PAM_AFM_CONTROL_SOURCE=file:D:\LJB\_stage\stage\_demo_source.py
import math


class DemoSource(object):
    """Pretends to be a lock-in reading a phase instead of an amplitude."""

    unit = "deg"

    def __init__(self, config):
        self.config = config
        self.prepared = False

    def prepare(self):
        self.prepared = True

    def value(self):
        # A bare number is allowed; (number, snr) is allowed too.
        return 90.0 + 10.0 * math.sin(0.1)

    def finish(self):
        self.prepared = False

    def describe(self):
        return "demo phase source (deg)"


def create_source(config):
    return DemoSource(config)
