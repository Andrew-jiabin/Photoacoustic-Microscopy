
from ctypes import WinDLL, create_string_buffer
import os
import serial
import time

class Prior_xy_stage:
    def __init__(self, path, COM_port):
        if os.path.exists(path):
            self.SDKPrior = WinDLL(path)
        else:
            raise RuntimeError("DLL could not be loaded.")
        self.SDKPrior.PriorScientificSDK_Initialise()
        self.sessionID = self.SDKPrior.PriorScientificSDK_OpenNewSession()
        if self.sessionID < 0:
            print(f"Error getting sessionID")          
        self.rx = create_string_buffer(1000)
        self.cmd_simple("controller.connect "+COM_port)
        
    def cmd(self,msg):
        ret = self.SDKPrior.PriorScientificSDK_cmd(
            self.sessionID, create_string_buffer(msg.encode()), self.rx
        )
        return ret, self.rx.value.decode()
    
    def cmd_simple(self,msg):
        self.SDKPrior.PriorScientificSDK_cmd(
        self.sessionID, create_string_buffer(msg.encode()), self.rx
    )
        
    def get_ID(self):
        return self.sessionID
    
    def get_SDK_vision(self):
        return self.SDKPrior.PriorScientificSDK_Version(self.rx)
    
    def get_position(self): 
        self.SDKPrior.PriorScientificSDK_cmd(
        self.sessionID, create_string_buffer("controller.stage.position.get".encode()), self.rx
        )
        return self.rx.value.decode()
    
    def set_position(self,position:list): 
        self.SDKPrior.PriorScientificSDK_cmd(
        self.sessionID, create_string_buffer(f"controller.stage.goto-position {position[0]} {position[1]}".encode()), self.rx
        )
    
    def stage_deinitial(self):
        return self.cmd("controller.disconnect")
    # def fuction_test(self):
        


 
if __name__ == "__main__":
    stage=Prior_xy_stage(r"D:\LJB\PAM\PriorSDK 2.0.0\x64\PriorScientificSDK.dll","4")
    print(stage.get_position())



