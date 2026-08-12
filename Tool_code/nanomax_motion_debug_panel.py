"""NanoMax-only motion debug entry point.

This intentionally does not import or initialize DAQ or laser modules.
It opens the same terminal motion panels used by PAM_Main_Nanomax.py and
connects only the BPC303 closed-loop sample NanoMax and/or MDT693B open-loop
probe NanoMax.
"""

import os
import sys


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from Nanomax.motion_debug import main


if __name__ == "__main__":
    main()
