#!python3
#
# Keysight VISA COM Example in Python using "comtypes"
# *********************************************************
# This program illustrates a few commonly used programming
# features of your Keysight Infiniium Series oscilloscope.
# *********************************************************

# Import Python modules.
# --------------------------------------------------------
import string
import time
import sys
import array
from comtypes.client import GetModule, CreateObject
from comtypes.automation import VARIANT
from matplotlib import pyplot as plt

# Run GetModule once to generate comtypes.gen.VisaComLib.
if not hasattr(sys, "frozen"):
    GetModule(r"C:\Program Files (x86)\IVI Foundation\VISA\VisaCom\GlobMgr.dll")
import comtypes.gen.VisaComLib as VisaComLib


# =========================================================
# Initialize:
# =========================================================
# def initialize():
#     # Get and display the device's *IDN? string.
#     idn_string = do_query_string("*IDN?")
#     print("Identification string '%s'" % idn_string)
#
#     # Clear status and load the default setup.
#     # do_command("*CLS")
#     # do_command("*RST")


# =========================================================
# Capture:
# =========================================================
# def capture():
#     # Set probe attenuation factor.
#     do_command(":CHANnel1:PROBe 1.0")
#     qresult = do_query_string(":CHANnel1:PROBe?")
#     print("Channel 1 probe attenuation factor: %s" % qresult)
#
#     # Use auto-scale to automatically set up oscilloscope.
#     print("Autoscale.")
#     do_command(":AUToscale")
#
#     # Set trigger mode.
#     do_command(":TRIGger:MODE EDGE")
#     qresult = do_query_string(":TRIGger:MODE?")
#     print("Trigger mode: %s" % qresult)
#
#     # Set EDGE trigger parameters.
#     do_command(":TRIGger:EDGE:SOURce CHANnel1")
#     qresult = do_query_string(":TRIGger:EDGE:SOURce?")
#     print("Trigger edge source: %s" % qresult)
#
#     do_command(":TRIGger:LEVel CHANnel1,336E-3")
#     qresult = do_query_string(":TRIGger:LEVel? CHANnel1")
#     print("Trigger level, channel 1: %s" % qresult)
#
#     do_command(":TRIGger:EDGE:SLOPe POSitive")
#     qresult = do_query_string(":TRIGger:EDGE:SLOPe?")
#     print("Trigger edge slope: %s" % qresult)
#
#     # Save oscilloscope setup.
#     setup_bytes = do_query_ieee_block_UI1(":SYSTem:SETup?")
#     nLength = len(setup_bytes)
#     with open("setup.stp", "wb") as f:
#         f.write(bytearray(setup_bytes))
#     print("Setup bytes saved: %d" % nLength)
#
#     # Change oscilloscope settings with individual commands:
#     do_command(":CHANnel1:SCALe 0.1")
#     qresult = do_query_number(":CHANnel1:SCALe?")
#     print("Channel 1 vertical scale: %f" % qresult)
#
#     do_command(":CHANnel1:OFFSet 0.0")
#     qresult = do_query_number(":CHANnel1:OFFSet?")
#     print("Channel 1 offset: %f" % qresult)
#
#     do_command(":TIMebase:SCALe 200e-6")
#     qresult = do_query_string(":TIMebase:SCALe?")
#     print("Timebase scale: %s" % qresult)
#
#     do_command(":TIMebase:POSition 0.0")
#     qresult = do_query_string(":TIMebase:POSition?")
#     print("Timebase position: %s" % qresult)
#
#     do_command(":ACQuire:MODE RTIMe")
#     qresult = do_query_string(":ACQuire:MODE?")
#     print("Acquire mode: %s" % qresult)
#
#     # Restore setup
#     with open("setup.stp", "rb") as f:
#         setup_bytes = f.read()
#     do_command_ieee_block(":SYSTem:SETup", array.array('B', setup_bytes))
#     print("Setup bytes restored: %d" % len(setup_bytes))
#
#     # Acquire waveform
#     do_command(":ACQuire:POINts 32000")
#     do_command(":DIGitize")


# =========================================================
# Analyze:
# =========================================================
# def analyze():
#
#
#     # Get waveform data
#     do_command(":WAVeform:STReaming OFF")
#     # do_command(":ACQuire:POINts:ANALog 10000")
#     data_words = do_query_ieee_block_I2(":WAVeform:DATA?")
#     nLength = len(data_words)
#     print("Number of data values: %d" % nLength)
#     # Save CSV
#     time_val_all=[]
#     voltage_all = []
#     # with open("./waveform_data.csv", "a+") as f:
#     #     for i in range(0, nLength - 1):
#     #         time_val = x_origin + (i * x_increment)
#     #         voltage = (data_words[i] * y_increment) + y_origin
#     #         f.write("%E, %f\n" % (time_val, voltage))
#     #         time_val_all.append(time_val)
#     #         voltage_all.append(voltage)
#     print("Waveform format WORD data written to waveform_data.csv.")
#     print(len(time_val_all),len(voltage_all))
#     # 创建图形和坐标轴
#     fig, ax = plt.subplots(figsize=(10, 6))
#
#     # 绘制曲线
#     ax.plot(time_val_all, voltage_all, label='曲线', color='blue', linewidth=2, linestyle='-', marker='o', markersize=3)
#
#     # 添加标题和轴标签
#     ax.set_title('曲线图示例', fontsize=15)
#     ax.set_xlabel('X轴', fontsize=12)
#     ax.set_ylabel('Y轴', fontsize=12)
#
#     # 添加网格
#     ax.grid(True, linestyle='--', alpha=0.7)
#
#     # 添加图例
#     ax.legend(fontsize=12)
#
#     # 调整布局
#     plt.tight_layout()
#
#     # 显示图形
#     plt.show()

def analyze():
    # # Measurements
    # do_command(":MEASure:SOURce CHANnel1")
    # qresult = do_query_string(":MEASure:SOURce?")
    # print("Measure source: %s" % qresult)
    #
    # do_command(":MEASure:FREQuency")
    # qresult = do_query_string(":MEASure:FREQuency?")
    # print("Measured frequency on channel 1: %s" % qresult)
    #
    # do_command(":MEASure:VAMPlitude")
    # qresult = do_query_string(":MEASure:VAMPlitude?")
    # print("Measured vertical amplitude on channel 1: %s" % qresult)
    #
    # # Download screen image
    # image_bytes = do_query_ieee_block_UI1(":DISPlay:DATA? PNG")
    # with open("screen_image.png", "wb") as f:
    #     f.write(bytearray(image_bytes))
    # print("Screen image written to 'screen_image.png'.")
    #
    # # Waveform info
    # qresult = do_query_string(":WAVeform:TYPE?")
    # print("Waveform type: %s" % qresult)
    #
    # qresult = do_query_string(":WAVeform:POINts?")
    # print("Waveform points: %s" % qresult)
    #
    # do_command(":WAVeform:SOURce CHANnel1")
    # qresult = do_query_string(":WAVeform:SOURce?")
    # print("Waveform source: %s" % qresult)
    #
    # do_command(":WAVeform:FORMat WORD")
    # print("Waveform format: %s" % do_query_string(":WAVeform:FORMat?"))

    # Get numeric values
    x_increment = do_query_number(":WAVeform:XINCrement?")
    x_origin = do_query_number(":WAVeform:XORigin?")
    y_increment = do_query_number(":WAVeform:YINCrement?")
    y_origin = do_query_number(":WAVeform:YORigin?")

    # Get waveform data
    do_command(":WAVeform:STReaming OFF")
    do_command(":WAVeform:POINts 10000")
    data_words = do_query_ieee_block_I2(":WAVeform:DATA?")
    nLength = len(data_words)
    print("Number of data values: %d" % nLength)
    # Save CSV
    time_val_all=[]
    voltage_all = []
    with open("./waveform_data.csv", "a+") as f:
        for i in range(0, nLength - 1):
            time_val = x_origin + (i * x_increment)
            voltage = (data_words[i] * y_increment) + y_origin
            f.write("%E, %f\n" % (time_val, voltage))
            time_val_all.append(time_val)
            voltage_all.append(voltage)
    print("Waveform format WORD data written to waveform_data.csv.")
    print(len(time_val_all),len(voltage_all))
    # 创建图形和坐标轴
    fig, ax = plt.subplots(figsize=(10, 6))

    # 绘制曲线
    ax.plot(time_val_all, voltage_all, label='曲线', color='blue', linewidth=2, linestyle='-', marker='o', markersize=3)

    # 添加标题和轴标签
    ax.set_title('曲线图示例', fontsize=15)
    ax.set_xlabel('X轴', fontsize=12)
    ax.set_ylabel('Y轴', fontsize=12)

    # 添加网格
    ax.grid(True, linestyle='--', alpha=0.7)

    # 添加图例
    ax.legend(fontsize=12)

    # 调整布局
    plt.tight_layout()

    # 显示图形
    plt.show()

# =========================================================
# Utility functions
# =========================================================
def do_command(command):
    print("%s" % command)
    myScope.WriteString("%s" % command, True)
    check_instrument_errors(command)


def do_command_ieee_block(command, data):
    myScope.WriteIEEEBlock(command, VARIANT(array.array('B', data)), True)
    check_instrument_errors(command)


def do_query_string(query):
    myScope.WriteString("%s" % query, True)
    result = myScope.ReadString()
    check_instrument_errors(query)
    return result


def do_query_ieee_block_UI1(query):
    myScope.WriteString("%s" % query, True)
    result = myScope.ReadIEEEBlock(VisaComLib.BinaryType_UI1, False, True)
    check_instrument_errors(query)
    return result


def do_query_ieee_block_I2(query):
    myScope.WriteString("%s" % query, True)
    result = myScope.ReadIEEEBlock(VisaComLib.BinaryType_I2, False, True)
    check_instrument_errors(query)
    return result


def do_query_number(query):
    myScope.WriteString("%s" % query, True)
    result = myScope.ReadNumber(VisaComLib.ASCIIType_R8, True)
    check_instrument_errors(query)
    return result


def do_query_numbers(query):
    myScope.WriteString("%s" % query, True)
    result = myScope.ReadList(VisaComLib.ASCIIType_R8, ",;")
    check_instrument_errors(query)
    return result


def check_instrument_errors(command):
    while True:
        myScope.WriteString(":SYSTem:ERRor? STRing", True)
        error_string = myScope.ReadString()
        if error_string:  # If there is an error string value.
            if error_string.find("0,", 0, 2) == -1:  # Not "No error".
                print("ERROR: %s, command: '%s'" % (error_string, command))
                print("Exited because of error.")
                sys.exit(1)
            else:  # "No error"
                break
        else:
            print("ERROR: :SYSTem:ERRor? STRing returned nothing, command: '%s'" % command)
            print("Exited because of error.")
            sys.exit(1)


# =========================================================
# Main program:
# =========================================================
rm = CreateObject("VISA.GlobalRM", interface=VisaComLib.IResourceManager)
k = rm.open_resource('USB0::0x2A8D::0x9046::MY63410110::0::INSTR')

myScope = CreateObject("VISA.BasicFormattedIO", interface=VisaComLib.IFormattedIO488)
myScope.IO = rm.Open("USB0::0x2A8D::0x9046::MY63410110::0::INSTR")

# Clear the interface.
myScope.IO.Clear
print("Interface cleared.")

# Set the Timeout to 15 seconds.
myScope.IO.Timeout = 15000
print("Timeout set to 15000 milliseconds.")
# do_command( ":DISK:SAVE:WAVeform CHANnel1,\"FILE1\",CSV,ON")
# Run program
# initialize()
# capture()
analyze()
myScope.IO.Close()

print("End of program")
sys.exit()
