# -*- coding: utf-8 -*-
"""TTL level on the ATS9373 AUX connector, for an external signal relay.

Why this exists
---------------
The board has no analog auxiliary input.  AUX I/O is a 3.3 V TTL digital port
(5 V compliant): as an input it only offers Trigger Enable and a
software-readable bit (``GET_AUX_INPUT_LEVEL`` returns 0 or 1), and TRIG IN
"cannot be digitized" (ATS9373 User Manual V1-5 p.45).  So two analog signals
cannot both reach the digitizer at 4 GS/s.

What AUX *can* do is drive the coil of a small RF relay.  With a two-way relay
on the input of CH A, one cable (the 10 MHz monitor) is selected during the
approach and the other (the PA transducer) during imaging.  CH A then stays at
4 GS/s for the whole scan: no clock change, no channel change, no abort/re-arm.
Measured alternative, for comparison: switching B(100 MS/s) -> A(4 GS/s) costs
231 ms, and switching back costs 217 ms.

SDK call
--------
``AlazarConfigureAuxIO(handle, AUX_OUT_SERIAL_DATA, level)`` with level 0 or 1.
Despite the name, this mode is documented as "a general purpose digital output"
(ATS-SDK Guide 25.1, enum ALAZAR_AUX_IO_MODES).  Verified on the board,
including toggling the pin while a stream was running: the acquisition is not
disturbed.

Wiring
------
AUX centre pin -> 1 kOhm -> base/gate of a small NPN or logic-level MOSFET,
relay coil between +5 V and the collector/drain, flyback diode across the coil.
Do **not** drive the coil from the pin directly: it is a logic output, not a
driver.  Use a signal relay with gold contacts (a reed relay is fine at 10 MHz);
a power relay will inject noise into a microvolt-level PA signal.

Prefer a **DPDT** relay over an SPDT one, still driven by the single AUX line:
one pole switches the signal into CH A, the other parks the *unselected*
cable on a 50 Ohm dummy load.  That keeps the source terminated (no open-circuit
pickup) and leaves CH A connected at every instant, including the
break-before-make gap.  With an SPDT, wire the transducer to the normally-closed
contact so a power loss parks CH A on the PA signal instead of leaving it open.

The second pole must be **crossed** against the first::

    pole 1 common -> CH A        pole 2 common -> 50 Ohm to ground
    pole 1 N.O.   -> monitor     pole 2 N.O.   -> transducer
    pole 1 N.C.   -> transducer  pole 2 N.C.   -> monitor

Wired the same way round, the dummy load would land on the *selected* cable and
halve the source impedance -- a 6 dB drop that reads like a sensitivity change.

Is the switching itself hard on CH A?
-------------------------------------
No.  Signal and reed relays are break-before-make, so the two cables are never
connected to each other -- there is no moment when one source drives the other.
The input has a 50 Ohm termination and ESD diodes; during the few hundred
microseconds of the transfer the input simply has no DC path and the reading is
invalid, which is not a stress on the part.  At millivolt levels there is no
arcing across the contacts (that needs tens of volts).  What would damage the
input is overvoltage, not switching, so keep both cables inside the selected
input range.

Two things that do matter, and neither is destructive:

* bond the shields of both cables to the same ground at the digitizer end, or
  the two sources will drive a ground loop through the contacts and the reading
  will jump;
* keep the coil wires away from the signal wires -- the flyback transient is
  coupled capacitively, not through the contacts.
"""

from __future__ import annotations

import argparse
import sys
import time

import atsapi as ats


#: ``AUX_OUT_SERIAL_DATA`` -- the SDK's general purpose digital output mode.
#: The Python wrapper does not export it, so it is named here.
AUX_OUT_SOFTWARE = 14

LEVEL_LOW = 0
LEVEL_HIGH = 1


class AuxRelay(object):
    """One TTL line that selects between the approach and imaging inputs.

    ``select("approach")`` drives the pin to ``active_level``; ``select("imaging")``
    drives it to the opposite level.  Which physical position that is depends on
    how the relay is wired, so ``active_level`` is a constructor argument.
    """

    def __init__(self, board=None, system_id=1, board_id=1, active_level=LEVEL_HIGH,
                 verbose=True):
        self._shared_board = board
        self.system_id = system_id
        self.board_id = board_id
        self.active_level = LEVEL_HIGH if active_level else LEVEL_LOW
        self.verbose = verbose
        self.board = None
        self.level = None

    def _log(self, message):
        if self.verbose:
            print("[AUX] " + str(message))

    def open(self):
        if self.board is not None:
            return self
        self.board = self._shared_board or ats.Board(
            systemId=self.system_id, boardId=self.board_id
        )
        return self

    def set(self, level):
        """Drive the pin low (0) or high (1)."""
        if self.board is None:
            raise RuntimeError("open() first")
        level = LEVEL_HIGH if level else LEVEL_LOW
        self.board.configureAuxIO(AUX_OUT_SOFTWARE, level)
        self.level = level
        self._log("level %d" % level)
        return self

    def select(self, which):
        """``"approach"`` -> active level, ``"imaging"`` -> the other one."""
        key = str(which).strip().lower()
        if key in ("approach", "a", "sense", "feedback"):
            return self.set(self.active_level)
        if key in ("imaging", "image", "payload", "i"):
            return self.set(LEVEL_LOW if self.active_level else LEVEL_HIGH)
        raise ValueError("select() expects 'approach' or 'imaging', got %r" % (which,))

    def close(self):
        if self.board is not None and self._shared_board is None:
            self.board = None
        return self


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Drive the ATS9373 AUX pin as a TTL output (relay test)"
    )
    parser.add_argument("--level", type=int, choices=(0, 1),
                        help="set the pin and exit")
    parser.add_argument("--toggle", type=float, default=0.0,
                        help="toggle the pin every N seconds until Ctrl-C")
    parser.add_argument("--active-level", type=int, default=1, choices=(0, 1),
                        help="level that selects the approach input")
    args = parser.parse_args(argv)

    relay = AuxRelay(active_level=args.active_level)
    relay.open()
    try:
        if args.level is not None:
            relay.set(args.level)
            print("AUX level %d" % args.level)
            return 0
        if args.toggle > 0:
            level = LEVEL_LOW
            while True:
                relay.set(level)
                print("AUX level %d" % level)
                sys.stdout.flush()
                level = LEVEL_HIGH if level == LEVEL_LOW else LEVEL_LOW
                time.sleep(args.toggle)
        parser.print_help()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        relay.close()


if __name__ == "__main__":
    raise SystemExit(main())
