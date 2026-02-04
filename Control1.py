import struct
from ctypes import WinDLL, create_string_buffer
import os
import re
import time as te

import sys
import pyvisa

import zlib
rm=pyvisa.ResourceManager('C:/Windows/System32/visa32.dll')
k=rm.open_resource('USB0::0x2A8D::0x9046::MY63410110::0::INSTR')
k.timeout=10000
k.write('*CLS')

preamble_string = k.query(":WAVeform:PREamble?")

# Get numeric values for later calculations.
x_increment = k.query(":WAVeform:XINCrement?")
x_origin = k.query(":WAVeform:XORigin?")
y_increment = k.query(":WAVeform:YINCrement?")
y_origin = k.query(":WAVeform:YORigin?")
# Get the waveform data.
k.write(":WAVeform:STReaming OFF")


sData = k.query(":WAVeform:DATA?")

print(sData)

#
# import struct
# from ctypes import WinDLL, create_string_buffer
# import os
# import re
# import time as te
#
# import sys
# import pyvisa
#
# import zlib
# rm=pyvisa.ResourceManager('C:/Windows/System32/visa32.dll')
# k=rm.open_resource('TCPIP0::169.254.51.214::inst0::INSTR')
# k.timeout=10000
# k.write('*CLS')
#
# #######Control Switch#######
# realhw = False
# ############################
# rx = create_string_buffer(1000)
# path = "./PriorScientificSDK.dll"
# file_path='./params.txt'
#
# if os.path.exists(path):
#     SDKPrior = WinDLL(path)
# else:
#     raise RuntimeError("DLL could not be loaded.")
#
# ret = SDKPrior.PriorScientificSDK_Initialise()
# if ret:
#     print(f"Error initialising {ret}")
#     sys.exit()
# else:
#     print(f"Ok initialising {ret}")
#
# ret = SDKPrior.PriorScientificSDK_Version(rx)
# print(f"dll version api ret={ret}, version={rx.value.decode()}")
#
# sessionID = SDKPrior.PriorScientificSDK_OpenNewSession()
# if sessionID < 0:
#     print(f"Error getting sessionID {ret}")
# else:
#     print(f"SessionID = {sessionID}")
#
# ret = SDKPrior.PriorScientificSDK_cmd(
#     sessionID, create_string_buffer(b"dll.apitest 33 goodresponse"), rx)
# print(f"api response {ret}, rx = {rx.value.decode()}")
#
# ret = SDKPrior.PriorScientificSDK_cmd(
#     sessionID, create_string_buffer(b"dll.apitest -300 stillgoodresponse"), rx)
# print(f"api response {ret}, rx = {rx.value.decode()}")
#
# k.write(":MEASure:SOURce CHANnel1")
# qresult = k.query(":MEASure:SOURce?")
# print("Measure source: %s" % qresult)
# k.write(":MEASure:FREQuency")
# qresult = k.query(":MEASure:FREQuency?")
# print("Measured frequency on channel 1: %s" % qresult)
# k.write(":MEASure:VAMPlitude")
# qresult = k.query(":MEASure:VAMPlitude?")
# print("Measured vertical amplitude on channel: %s" % qresult)
# # Get the waveform type.
# qresult = k.query(":WAVeform:TYPE?")
# print("Waveform type: %s" % qresult)
# # Get the number of waveform points.
# qresult = k.query(":WAVeform:POINts?")
# print("Waveform points: %s" % qresult)
# # Set the waveform source.
# print("Waveform source: %s" % qresult)
# # Choose the format of the data returned:
# k.write(":WAVeform:FORMat ASCii")
# print("Waveform format: %s" % k.query(":WAVeform:FORMat?"))
# k.write(":WAVeform:COUPling LFREJECT")
# # Display the waveform settings from preamble:
# wav_form_dict = {
#     0: "ASCii",
#     1: "BYTE",
#     2: "WORD",
#     3: "LONG",
#     4: "LONGLONG",
# }
# acq_type_dict = {
#     1: "RAW",
#     2: "AVERage",
#     3: "VHIStogram",
#     4: "HHIStogram",
#     6: "INTerpolate",
#     10: "PDETect",
# }
# acq_mode_dict = {
#     0: "RTIMe",
#     1: "ETIMe",
#     3: "PDETect",
# }
# coupling_dict = {
#     0: "AC",
#     1: "DC",
#     2: "DCFIFTY",
#     3: "LFREJECT",
# }
# units_dict = {
#     0: "UNKNOWN",
#     1: "VOLT",
#     2: "SECOND",
#     3: "CONSTANT",
#     4: "AMP",
#     5: "DECIBEL",
# }
# preamble_string = k.query(":WAVeform:PREamble?")
# (
#     wav_form, acq_type, wfmpts, avgcnt, x_increment, x_origin,
#     x_reference, y_increment, y_origin, y_reference, coupling,
#     x_display_range, x_display_origin, y_display_range,
#     y_display_origin, date, time, frame_model, acq_mode,
#     completion, x_units, y_units, max_bw_limit, min_bw_limit
# ) = preamble_string.split(",")
# print("Waveform format: %s" % wav_form_dict[int(wav_form)])
# print("Acquire type: %s" % acq_type_dict[int(acq_type)])
# print("Waveform points desired: %s" % wfmpts)
# print("Waveform average count: %s" % avgcnt)
# print("Waveform X increment: %s" % x_increment)
# print("Waveform X origin: %s" % x_origin)
# print("Waveform X reference: %s" % x_reference)  # Always 0.
# print("Waveform Y increment: %s" % y_increment)
# print("Waveform Y origin: %s" % y_origin)
# print("Waveform Y reference: %s" % y_reference)  # Always 0.
# print("Coupling: %s" % coupling_dict[int(coupling)])
# print("Waveform X display range: %s" % x_display_range)
# print("Waveform X display origin: %s" % x_display_origin)
# print("Waveform Y display range: %s" % y_display_range)
# print("Waveform Y display origin: %s" % y_display_origin)
# print("Date: %s" % date)
# print("Time: %s" % time)
# print("Frame model #: %s" % frame_model)
# print("Acquire mode: %s" % acq_mode_dict[int(acq_mode)])
# print("Completion pct: %s" % completion)
# print("Waveform X units: %s" % units_dict[int(x_units)])
# print("Waveform Y units: %s" % units_dict[int(y_units)])
# print("Max BW limit: %s" % max_bw_limit)
# print("Min BW limit: %s" % min_bw_limit)
# # Get numeric values for later calculations.
# x_increment = k.query(":WAVeform:XINCrement?")
# x_origin = k.query(":WAVeform:XORigin?")
# y_increment = k.query(":WAVeform:YINCrement?")
# y_origin = k.query(":WAVeform:YORigin?")
# # Get the waveform data.
# k.write(":WAVeform:STReaming OFF")
#
# def cmd(msg):
#     print(msg)
#     ret = SDKPrior.PriorScientificSDK_cmd(
#         sessionID, create_string_buffer(msg.encode()), rx
#     )
#     if ret:
#         print(f"Api error {ret}")
#     else:
#         print(f"OK {rx.value.decode()}")
#
#     return ret, rx.value.decode()
#
# def read_params(file_path):
#     params = {}
#     with open(file_path, 'r') as file:
#         for line in file:
#             key, value = line.strip().split('=')
#             if value.isdigit():  # 整数类型
#                 value = int(value)
#             elif value.replace('.', '', 1).isdigit():  # 浮点数类型
#                 value = float(value)
#             elif value.lower() == 'true':  # 布尔类型
#                 value = True
#             elif value.lower() == 'false':  # 布尔类型
#                 value = False
#             params[key] = value
#     return params
#
# save_path='./data/'
#
# def get(q):
#     sData = k.query(":WAVeform:DATA?")
#     # Unpack signed byte data.
#     pattern=re.compile(r'[-+]?\d*\.\d+E[-+]?\d+')
#     tt=pattern.findall(sData)
#     t=[float(ttt) for ttt in tt]
#     print("Number of data values: %d" % len(t))
#     # Save waveform data values to CSV file.                                                                                      '
#
#     f = open("{0}waveform_data{1}.csv".format(save_path,q), "w")
#
#     for i in range(0, len(t) - 1):
#         time_val = float(x_origin) + float(i * float(x_increment))
#         voltage = t[i]
#         f.write("%E, %f\n" % (time_val, voltage))
#     f.close()
#     print("Waveform format BYTE data written to waveform_data.csv.")
#
#
#
# if __name__ == '__main__':
#     params=read_params(file_path)
#     q = 0
#     cout=int(params['length']/params['pixel'])
#     cmd('controller.connect 4')
#     for p in range(cout-1):
#         for p in range(cout-1):
#             te.sleep(3)
#             get(q)
#             q += 1
#             cmd('controller.stage.move-relative {X} 0'.format(X=params['pixel']))
#         cmd('controller.stage.move-relative -{X} -{Y}'.format(X=(cout-1)*params['pixel'],Y=params['pixel']))