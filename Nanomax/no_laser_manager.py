"""Null laser manager for the laser-free PAM programs.

``PAM_Main_Nanomax_ClosedLoop.py`` and ``PAM_Main_Prior.py`` run without any laser
control: no 532 nm CBOX and no TOPTICA CW laser. The shared terminal panels,
however, were built around a laser-manager object:

* ``Nanomax/prealign_panel.py`` calls ``refresh_status``, ``panel_items(acquisition=False)``
  and ``execute_prealign_command``;
* ``Nanomax/acquisition_panel.py`` calls ``panel_items(acquisition=True)`` and
  ``execute_acquisition_command``.

This module provides that object as an explicit, clearly labelled stand-in, so the
panels can be reused unchanged while no laser code is reachable from a laser-free run.

``panel_items(acquisition=False)`` returns an empty list on purpose: the prealignment
panel omits its whole Lasers section when the item list is empty, which is the
honest rendering for a program that has no laser control at all. (``acquisition=True``
keeps three explicit ``NOT_USED`` rows, so the acquisition dashboard still shows the
section the operator expects from the NanoMax program, with the removal stated in
plain text rather than left to inference.)
"""


class NoLaserManager:
    """Stand-in laser manager that reports the removal instead of pretending."""

    label = "Laser control removed"

    def __init__(self, log_callback=None, reason="laser control is not part of this program"):
        self.log_callback = log_callback
        self.reason = str(reason)
        self.refresh_count = 0

    def _log(self, event, **fields):
        if self.log_callback is not None:
            try:
                self.log_callback(event, **fields)
            except Exception:
                pass

    def refresh_status(self):
        """No hardware to poll; kept so the panels can call it unconditionally."""
        self.refresh_count += 1
        return {"status": "removed", "reason": self.reason}

    def panel_items(self, acquisition=False):
        if not acquisition:
            # Empty list => the prealignment panel prints no Lasers section at all.
            return []
        return [
            ("LASER_CONTROL", "REMOVED", self.reason),
            ("532_CBOX", "NOT_USED", "no 532 nm laser path in this program"),
            ("TOPTICA_CW", "NOT_USED", "no CW laser path in this program"),
        ]

    def execute_prealign_command(self, tokens):
        """Return None so the prealignment panel keeps ownership of the command.

        The panel treats a non-None return as "the laser manager handled this".
        Returning None for everything means every laser-ish command falls through to
        the panel's normal handling, where it surfaces as an unknown command instead
        of silently doing nothing.
        """
        return None

    def execute_acquisition_command(self, tokens):
        text = " ".join(str(token) for token in tokens)
        message = (
            f"Laser commands are unavailable in this program ({self.reason}); "
            f"ignored: {text!r}. Use the stop key to end the scan."
        )
        self._log("NO_LASER_COMMAND_IGNORED", command=text)
        return message
