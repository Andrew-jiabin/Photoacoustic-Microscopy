# Alazar_NPT_Handler.py
import ctypes
import numpy as np
import os
import sys
import time
import traceback
# 假设 atsapi 就在 Library 路径下，或者你可以直接 pip install atsapi
# sys.path.append(os.path.join(os.path.dirname(__file__), '../..', 'Library'))
import atsapi as ats

class AlazarNPTSystem:
    def __init__(self, systemId=1, boardId=1):
        self.board = ats.Board(systemId=systemId, boardId=boardId)
        self.buffers = []
        self.buffer_list_handle = [] # 保持对Buffer对象的引用防止被GC
        self.samplesPerSec = 2000000000.0
        self.is_capturing = False
        
    def configure_board(self):
        # 时钟设置 (4GS/s)
        self.board.setCaptureClock(ats.INTERNAL_CLOCK, ats.SAMPLE_RATE_2000MSPS, ats.CLOCK_EDGE_RISING, 0)
        
        # 通道设置
        self.board.inputControlEx(ats.CHANNEL_A, ats.DC_COUPLING, ats.INPUT_RANGE_PM_400_MV, ats.IMPEDANCE_50_OHM)
        self.board.inputControlEx(ats.CHANNEL_B, ats.DC_COUPLING, ats.INPUT_RANGE_PM_400_MV, ats.IMPEDANCE_50_OHM)
        
        # 触发设置 (使用 Channel A 作为触发源? 还是外部 TTL?)
        # 你的描述是：激光使用内部频率(80K)进行发射,该80K的脉冲也引到trigger
        # 这意味着采集卡应该设置为【外部触发】(External Trigger)
        self.board.setExternalTrigger(ats.DC_COUPLING, ats.ETR_2V5)
        
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
        self.board.setTriggerDelay(1250)
        self.board.setTriggerTimeOut(0) # 无限等待触发
        
        # 如果激光器自己发光并给板卡触发，则无需此步，或设为 AUX_OUT_TRIGGER
        self.board.configureAuxIO(ats.AUX_OUT_TRIGGER, 0)
        print("✅ [DAQ] 板卡配置完成")

    def prepare_acquisition(self,num_points:int,acq_channel=ats.CHANNEL_A, samples_per_record=4096,
                             records_per_buffer=16,buffer_count=4, records_per_point=1024, preTriggerSamples=0):
        """
        分配 DMA 内存
        """
        self.samplesPerRecord = samples_per_record
        self.recordsPerBuffer = records_per_buffer
        self.bufferCount = buffer_count
        self.recordsPerPoint = records_per_point
        self.preTriggerSamples = preTriggerSamples
        self.buffersPerPoint=int(records_per_point//records_per_buffer)
        
        # 计算大小
        _, bitsPerSample = self.board.getChannelInfo()
        bytesPerSample = (bitsPerSample.value + 7) // 8
        self.bytesPerBuffer = bytesPerSample * self.samplesPerRecord * self.recordsPerBuffer
        
        # 通道掩码 (只采 A 通道示例)
        self.channels = acq_channel
        
        # 分配 Buffer
        sample_type = ctypes.c_uint8 if bytesPerSample == 1 else ctypes.c_uint16
        self.buffers = []
        for i in range(buffer_count):
            self.buffers.append(ats.DMABuffer(self.board.handle, sample_type, self.bytesPerBuffer))
            
        # 提交 Buffer 给驱动
        self.board.setRecordSize(self.preTriggerSamples, self.samplesPerRecord)
        
        # 无限采集模式设置 (recordsPerAcquisition 设置为 infinite 0x7FFFFFFF)
        # 也可以设置为足够大的数
        self.board.beforeAsyncRead(self.channels,
                                   0,
                                   self.samplesPerRecord,
                                   self.recordsPerBuffer,
                                   self.recordsPerPoint * num_points, 
                                   ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT | ats.ADMA_FIFO_ONLY_STREAMING)

        for buf in self.buffers:
            self.board.postAsyncBuffer(buf.addr, buf.size_bytes)

        self.buffer_idx = 0 # 循环索引    
        

    def start_capture(self):
        self.board.startCapture()
        self.is_capturing = True

    def get_one_acquisition(self, all_data, pos_mapping, curr_pos_str, timeout_ms, Average_Enable=False):
        pixel_data_buffers = []
        sub_timeout = int(timeout_ms / self.buffersPerPoint)
        
        for _ in range(self.buffersPerPoint):
            data = self._fetch_next_buffer(sub_timeout)
            if data is not None:
                pixel_data_buffers.append(data)
        
        if Average_Enable and len(pixel_data_buffers) > 0:
            # --- 高性能简化操作 ---，1. 直接拼接原始 Buffer (不做 reshape)
            combined_raw = np.concatenate(pixel_data_buffers)
            
            # 2. 仅进行整数求和 (dtype 使用 uint32 防止溢出)，这比 np.mean 快得多，因为不涉及浮点运算和除法
            summed_data = np.sum(combined_raw.reshape(-1, self.samplesPerRecord), 
                                 axis=0, dtype=np.uint32)
            
            # 3. 存入结果，不进行类型转换，留给最后处理
            all_data.append([summed_data])
        else:
            all_data.append(pixel_data_buffers)
            
        pos_mapping.append(curr_pos_str)

        
    
    def _fetch_next_buffer(self, timeout_ms):
        try:
            buffer = self.buffers[self.buffer_idx % self.bufferCount]
            
            # 这里的 timeout 决定了主循环的卡顿程度, 如果激光是 80kHz, 1个buffer存10个record, 理论只需 0.125ms, 所以 timeout_ms=10 足够了
            self.board.waitAsyncBufferComplete(buffer.addr, timeout_ms=timeout_ms)
            # 经验：如果出现 ApiWaitTimeout 一定要检查 Trigger 本身是不是有问题
            
            # 1. 拷贝数据 (非常重要！因为 DMA 会复写这块内存)
            # data_copy = np.array(buffer.buffer, copy=True)
            # 为了速度，可以使用 copy
            data_copy = np.copy(buffer.buffer)
            
            # 2. 重新提交 Buffer
            self.board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
            self.buffer_idx += 1
            # print(self.buffer_idx)
        except Exception as e:
            print(traceback.format_exc())

        return data_copy


    def stop_capture(self):
        # print("🛑 [DAQ] 停止采集")
        self.board.abortAsyncRead()
        self.is_capturing = False