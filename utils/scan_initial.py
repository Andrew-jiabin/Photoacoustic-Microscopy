
from ctypes import WinDLL, create_string_buffer
import os
def get_scan_initial(path):
    if os.path.exists(path):
        SDKPrior = WinDLL(path)
    else:
        raise RuntimeError("DLL could not be loaded.")
    rx = create_string_buffer(1000)
    def cmd(msg):
        ret = SDKPrior.PriorScientificSDK_cmd(
            sessionID, create_string_buffer(msg.encode()), rx
        )
        return ret, rx.value.decode()

    SDKPrior.PriorScientificSDK_Initialise()
    ret = SDKPrior.PriorScientificSDK_Version(rx)
    sessionID = SDKPrior.PriorScientificSDK_OpenNewSession()
    if sessionID < 0:
        print(f"Error getting sessionID {ret}")
    else:
        print(f"SessionID = {sessionID}")

    cmd("controller.connect 4")
    print(cmd("controller.stage.position.get"))
    # disconnect cleanly from controller
    cmd("controller.disconnect")

 
if __name__ == "__main__":
    get_scan_initial(r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll")