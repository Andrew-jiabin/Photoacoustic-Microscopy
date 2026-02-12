from __future__ import division
import ctypes
import numpy as np
import os
import signal
import sys
import time

sys.path.append(os.path.join(os.path.dirname(__file__), '../..', 'Library'))
import atsapi as ats

samplesPerSec = 4000000000.0
triggerDelay_sec = 0
class Board_DAQ():
    def ConfigureBoard(self, board):
    # Configures a board for acquisition
    def ConfigureBoard(self, board):
        # TODO: Select clock parameters as required to generate this
        # sample rate
        #
        # For example: if samplesPerSec is 100e6 (100 MS/s), then you can
        # either:
        #  - select clock source INTERNAL_CLOCK and sample rate
        #    SAMPLE_RATE_100MSPS
        #  - or select clock source FAST_EXTERNAL_CLOCK, sample rate
        #    SAMPLE_RATE_USER_DEF, and connect a 100MHz signal to the
        #    EXT CLK BNC connector
        global samplesPerSec, triggerDelay_sec

        # 所以 ECLK 属于系统设定的一部分
        board.setCaptureClock(ats.INTERNAL_CLOCK,
                            ats.SAMPLE_RATE_4000MSPS,
                            ats.CLOCK_EDGE_RISING,
                            0)
        
        # TODO: Select channel A input parameters as required.
        board.inputControlEx(ats.CHANNEL_A,
                            ats.DC_COUPLING,
                            ats.INPUT_RANGE_PM_400_MV,
                            ats.IMPEDANCE_50_OHM)
        
        
        # TODO: Select channel B input parameters as required.
        board.inputControlEx(ats.CHANNEL_B,
                            ats.DC_COUPLING,
                            ats.INPUT_RANGE_PM_400_MV,
                            ats.IMPEDANCE_50_OHM)
        
        # TODO: Select trigger inputs and levels as required.
        board.setTriggerOperation(ats.TRIG_ENGINE_OP_J,
                                ats.TRIG_ENGINE_J,
                                ats.CHANNEL_A,    # 可以选择从哪里触发 atsappi.py ：line400
                                ats.TRIGGER_SLOPE_POSITIVE,
                                150,
                                ats.TRIG_ENGINE_K,
                                ats.TRIG_DISABLE,
                                ats.TRIGGER_SLOPE_POSITIVE,
                                128)

        # TODO: Select external trigger parameters as required.
        board.setExternalTrigger(ats.DC_COUPLING,
                                ats.ETR_TTL)

        # TODO: Set trigger delay as required.
        
        triggerDelay_samples = int(triggerDelay_sec * samplesPerSec + 0.5)


        # 让采集卡延迟 triggerDelay_samples 个采样点采样
        board.setTriggerDelay(triggerDelay_samples)

        # TODO: Set trigger timeout as required.
        #
        # NOTE: The board will wait for a for this amount of time for a
        # trigger event.  If a trigger event does not arrive, then the
        # board will automatically trigger. Set the trigger timeout value
        # to 0 to force the board to wait forever for a trigger event.
        #
        # IMPORTANT: The trigger timeout value should be set to zero after
        # appropriate trigger parameters have been determined, otherwise
        # the board may trigger if the timeout interval expires before a
        # hardware trigger event arrives.
        # Gemini 说单位是 us
        board.setTriggerTimeOut(0)

        # Configure AUX I/O connector as required
        board.configureAuxIO(ats.AUX_OUT_TRIGGER,
                            0)
        
    def AcquireData(self, board):
        # No pre-trigger samples in NPT mode
        preTriggerSamples = 0

        # TODO: Select the number of samples per record.
        postTriggerSamples = 4096

        # TODO: Select the number of records per DMA buffer.
        recordsPerBuffer = 10

        # TODO: Select the number of buffers per acquisition.
        buffersPerAcquisition = 10
        
        # TODO: Select the active channels.
        channels = ats.CHANNEL_A
        channelCount = 0

        # 通过位与方式来判断当前有几个通道
        for c in ats.channels:
            channelCount += (c & channels == c)
        # print("channelCount is :",channelCount)

        # TODO: Should data be saved to file?
        saveData = False
        dataFile = None
        if saveData:
            dataFile = open(os.path.join(os.path.dirname(__file__),
                                        "data.bin"), 'wb')

        # Compute the number of bytes per record and per buffer
        memorySize_samples, bitsPerSample = board.getChannelInfo()
        bytesPerSample = (bitsPerSample.value + 7) // 8
        samplesPerRecord = preTriggerSamples + postTriggerSamples
        bytesPerRecord = bytesPerSample * samplesPerRecord
        bytesPerBuffer = bytesPerRecord * recordsPerBuffer * channelCount

        # TODO: Select number of DMA buffers to allocate
        bufferCount = 4

        # Allocate DMA buffers

        sample_type = ctypes.c_uint8
        if bytesPerSample > 1:
            sample_type = ctypes.c_uint16

        buffers = []
        for i in range(bufferCount):
            buffers.append(ats.DMABuffer(board.handle, sample_type, bytesPerBuffer))
        
        # Set the record size
        board.setRecordSize(preTriggerSamples, postTriggerSamples)

        recordsPerAcquisition = recordsPerBuffer * buffersPerAcquisition

        # Configure the board to make an NPT AutoDMA acquisition
        board.beforeAsyncRead(channels,
                            -preTriggerSamples,
                            samplesPerRecord,
                            recordsPerBuffer,
                            recordsPerAcquisition,
                            ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT | ats.ADMA_FIFO_ONLY_STREAMING)
        


        # Post DMA buffers to board
        for buffer in buffers:
            board.postAsyncBuffer(buffer.addr, buffer.size_bytes)

        start = time.time() # Keep track of when acquisition started
        try:
            board.startCapture() # Start the acquisition
            print("Capturing %d buffers. Press <enter> to abort" %
                buffersPerAcquisition)
            buffersCompleted = 0
            bytesTransferred = 0
            while (buffersCompleted < buffersPerAcquisition and not
                ats.enter_pressed()):
                # Wait for the buffer at the head of the list of available
                # buffers to be filled by the board.
                buffer = buffers[buffersCompleted % len(buffers)]
                board.waitAsyncBufferComplete(buffer.addr, timeout_ms=5000)
                buffersCompleted += 1
                bytesTransferred += buffer.size_bytes
                # --- 新增绘图部分 ---
                import matplotlib.pyplot as plt
                
                # 将 DMA 缓冲区数据 reshape 为 (记录数, 样本数)
                # buffer.buffer 是一个一维 NumPy 数组
                reshaped_data = buffer.buffer.reshape(recordsPerBuffer, -1)
                
                # 提取当前 Buffer 中的第一条记录 (第一条 A-line) 进行观察
                # 注意：ATS9373 数据通常在高 12 位，这里直接画出原始编码值
                sample_record = reshaped_data[0] 
                
                plt.clf() # 清除上一帧图像
                plt.title(f"Live A-line (Buffer {buffersCompleted})")
                plt.plot(sample_record)
                plt.xlabel("Samples")
                plt.ylabel("ADC Code")
                plt.ylim(0, 65535) # 16-bit 数据的显示范围
                plt.pause(0.01)    # 必须加 pause，否则窗口会卡死
                # -------------------
                # TODO: Process sample data in this buffer. Data is available
                # as a NumPy array at buffer.buffer

                # NOTE:
                #
                # While you are processing this buffer, the board is already
                # filling the next available buffer(s).
                #
                # You MUST finish processing this buffer and post it back to the
                # board before the board fills all of its available DMA buffers
                # and on-board memory.
                #
                # Samples are arranged in the buffer as follows:
                # S0A, S0B, ..., S1A, S1B, ...
                # with SXY the sample number X of channel Y.
                #
                # A 12-bit sample code is stored in the most significant bits of
                # each 16-bit sample value.
                #
                # Sample codes are unsigned by default. As a result:
                # - 0x0000 represents a negative full scale input signal.
                # - 0x8000 represents a ~0V signal.
                # - 0xFFFF represents a positive full scale input signal.
                # Optionaly save data to file
                if dataFile:
                    buffer.buffer.tofile(dataFile)

                # Add the buffer to the end of the list of available buffers.
                board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
        finally:
            board.abortAsyncRead()
        # Compute the total transfer time, and display performance information.
        transferTime_sec = time.time() - start
        print("Capture completed in %f sec" % transferTime_sec)
        buffersPerSec = 0
        bytesPerSec = 0
        recordsPerSec = 0
        if transferTime_sec > 0:
            buffersPerSec = buffersCompleted / transferTime_sec
            bytesPerSec = bytesTransferred / transferTime_sec
            recordsPerSec = recordsPerBuffer * buffersCompleted / transferTime_sec
        print("Captured %d buffers (%f buffers per sec)" %
            (buffersCompleted, buffersPerSec))
        print("Captured %d records (%f records per sec)" %
            (recordsPerBuffer * buffersCompleted, recordsPerSec))
        print("Transferred %d bytes (%f bytes per sec)" %
            (bytesTransferred, bytesPerSec))
    

if __name__ == "__main__":
    import matplotlib.pyplot as plt
    plt.ion() # 开启交互模式，允许在循环中不断更新画布
    
    board = ats.Board(systemId = 1, boardId = 1)
    # 这里记得调用类方法时，如果你已经把函数封装进 Board_DAQ 类，
    # 需要实例化类或者直接调用（当前代码中 ConfigureBoard 带有 self 参数，请注意调用方式）
    Board_DAQ.ConfigureBoard(board) 
    Board_DAQ.AcquireData(board)