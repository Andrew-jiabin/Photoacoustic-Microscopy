# Alazar_NPT_Handler.py
import ctypes
import numpy as np
import os
import sys
import time

# 假设 atsapi 就在 Library 路径下，或者你可以直接 pip install atsapi
# sys.path.append(os.path.join(os.path.dirname(__file__), '../..', 'Library'))
import atsapi as ats

class AlazarNPTSystem:
    def __init__(self, systemId=1, boardId=1):
        self.board = ats.Board(systemId=systemId, boardId=boardId)
        self.buffers = []
        self.buffer_list_handle = [] # 保持对Buffer对象的引用防止被GC
        self.samplesPerSec = 4000000000.0
        self.is_capturing = False
        
    def configure_board(self, internal_freq=80000):
        """
        配置板卡。
        :param internal_freq: 内部触发频率 (Hz), 默认 80kHz
        """
        # 时钟设置 (4GS/s)
        self.board.setCaptureClock(ats.INTERNAL_CLOCK, ats.SAMPLE_RATE_4000MSPS, ats.CLOCK_EDGE_RISING, 0)
        
        # 通道设置
        self.board.inputControlEx(ats.CHANNEL_A, ats.DC_COUPLING, ats.INPUT_RANGE_PM_400_MV, ats.IMPEDANCE_50_OHM)
        self.board.inputControlEx(ats.CHANNEL_B, ats.DC_COUPLING, ats.INPUT_RANGE_PM_400_MV, ats.IMPEDANCE_50_OHM)
        
        # 触发设置 (使用 Channel A 作为触发源? 还是外部 TTL?)
        # 你的描述是：激光使用内部频率(80K)进行发射,该80K的脉冲也引到trigger
        # 这意味着采集卡应该设置为【外部触发】(External Trigger)
        self.board.setExternalTrigger(ats.DC_COUPLING, ats.ETR_TTL)
        
        self.board.setTriggerOperation(ats.TRIG_ENGINE_OP_J,
                                       ats.TRIG_ENGINE_J,
                                       ats.TRIG_EXTERNAL, # 外部触发
                                       ats.TRIGGER_SLOPE_POSITIVE,
                                       150,
                                       ats.TRIG_ENGINE_K,
                                       ats.TRIG_DISABLE,
                                       ats.TRIGGER_SLOPE_POSITIVE,
                                       128)
        
        # 设置触发延迟和超时
        self.board.setTriggerDelay(0)
        self.board.setTriggerTimeOut(0) # 无限等待触发
        
        # 配置 AUX I/O 输出 Pacer 信号 (如果需要板卡产生80k给激光器，需要用 AUX_OUT_PACER)
        # 如果激光器自己发光并给板卡触发，则无需此步，或设为 AUX_OUT_TRIGGER
        self.board.configureAuxIO(ats.AUX_OUT_TRIGGER, 0)
        print("✅ [DAQ] 板卡配置完成")

    def prepare_acquisition(self, samples_per_record=4096, records_per_buffer=10, buffer_count=8):
        """
        分配 DMA 内存
        """
        self.samplesPerRecord = samples_per_record
        self.recordsPerBuffer = records_per_buffer
        self.bufferCount = buffer_count
        
        # 计算大小
        _, bitsPerSample = self.board.getChannelInfo()
        bytesPerSample = (bitsPerSample.value + 7) // 8
        self.bytesPerBuffer = bytesPerSample * samples_per_record * records_per_buffer
        
        # 通道掩码 (只采 A 通道示例)
        self.channels = ats.CHANNEL_A 
        
        # 分配 Buffer
        sample_type = ctypes.c_uint8 if bytesPerSample == 1 else ctypes.c_uint16
        self.buffers = []
        for i in range(buffer_count):
            self.buffers.append(ats.DMABuffer(self.board.handle, sample_type, self.bytesPerBuffer))
            
        # 提交 Buffer 给驱动
        self.board.setRecordSize(0, samples_per_record)
        
        # 无限采集模式设置 (recordsPerAcquisition 设置为 infinite 0x7FFFFFFF)
        # 也可以设置为足够大的数
        self.board.beforeAsyncRead(self.channels,
                                   0,
                                   samples_per_record,
                                   records_per_buffer,
                                   0x7FFFFFFF, 
                                   ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT | ats.ADMA_FIFO_ONLY_STREAMING)

        for buf in self.buffers:
            self.board.postAsyncBuffer(buf.addr, buf.size_bytes)
            
        self.buffer_idx = 0 # 循环索引

    def start_capture(self):
        self.board.startCapture()
        self.is_capturing = True
        print("🚀 [DAQ] 开始采集 (等待触发)...")

    def fetch_next_buffer(self, timeout_ms=10):
        """
        尝试获取下一个 Buffer 数据 (非阻塞/短超时)
        :return: (numpy_array, bool_success)
        """
        if not self.is_capturing: return None, False
        
        buffer = self.buffers[self.buffer_idx % self.bufferCount]
        
        try:
            # 这里的 timeout 决定了主循环的卡顿程度
            # 如果激光是 80kHz, 1个buffer存10个record，理论只需 0.125ms
            # 所以 timeout_ms=10 足够了
            self.board.waitAsyncBufferComplete(buffer.addr, timeout_ms=timeout_ms)
            
            # 1. 拷贝数据 (非常重要！因为 DMA 会复写这块内存)
            # data_copy = np.array(buffer.buffer, copy=True)
            # 为了速度，可以使用 copy
            data_copy = np.copy(buffer.buffer)
            
            # 2. 重新提交 Buffer
            self.board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
            self.buffer_idx += 1
            
            return data_copy, True
            
        except ats.AlazarException as e:
            # 超时是正常的，意味着还没有攒够数据
            if "ApiWaitTimeout" in str(e): 
                return None, False
            else:
                raise e

    def stop_capture(self):
        print("🛑 [DAQ] 停止采集")
        self.board.abortAsyncRead()
        self.is_capturing = False