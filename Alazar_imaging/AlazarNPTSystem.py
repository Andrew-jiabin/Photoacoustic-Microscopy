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

        self.is_capturing = False
        
    def configure_board(self,sample_rate):
        # 时钟设置 (4GS/s)
        self.board.setCaptureClock(ats.INTERNAL_CLOCK, sample_rate, ats.CLOCK_EDGE_RISING, 0)
        
        # 通道设置
        self.board.inputControlEx(ats.CHANNEL_A, ats.DC_COUPLING, ats.INPUT_RANGE_PM_40_MV, ats.IMPEDANCE_50_OHM)
        self.board.inputControlEx(ats.CHANNEL_B, ats.DC_COUPLING, ats.INPUT_RANGE_PM_40_MV, ats.IMPEDANCE_50_OHM)
        
        # 触发设置 (使用 Channel A 作为触发源? 还是外部 TTL?)
        # 你的描述是：激光使用内部频率(80K)进行发射,该80K的脉冲也引到trigger
        # 这意味着采集卡应该设置为【外部触发】(External Trigger)
        self.board.setExternalTrigger(ats.DC_COUPLING, ats.ETR_2V5)
        
        self.board.setTriggerOperation(ats.TRIG_ENGINE_OP_J,
                                       ats.TRIG_ENGINE_J,
                                       ats.TRIG_EXTERNAL, # 外部触发
                                       ats.TRIGGER_SLOPE_POSITIVE,
                                       160,
                                       ats.TRIG_ENGINE_K,
                                       ats.TRIG_DISABLE,
                                       ats.TRIGGER_SLOPE_POSITIVE,
                                       128)
        
        # 设置触发延迟和超时
        self.board.setTriggerDelay(1380)
        self.board.setTriggerTimeOut(0) # 无限等待触发
        
        # 如果激光器自己发光并给板卡触发，则无需此步，或设为 AUX_OUT_TRIGGER
        self.board.configureAuxIO(ats.AUX_OUT_TRIGGER, 0)
        print("✅ [DAQ] 板卡配置完成")

    def prepare_acquisition(self, acq_channel=ats.CHANNEL_A, samples_per_record=4096,
                            records_per_buffer=64, buffer_count=4, records_per_point=64, preTriggerSamples=0):
        """
        仅负责分配内存和计算大小，不再向驱动提交异步读取(beforeAsyncRead)
        """
        self.samplesPerRecord = samples_per_record
        self.recordsPerBuffer = records_per_buffer
        self.bufferCount = buffer_count
        self.recordsPerPoint = records_per_point
        self.preTriggerSamples = preTriggerSamples
        self.buffersPerPoint = int(records_per_point // records_per_buffer) # 如果都是64，这就是1
        
        _, bitsPerSample = self.board.getChannelInfo()
        bytesPerSample = (bitsPerSample.value + 7) // 8
        self.bytesPerBuffer = bytesPerSample * self.samplesPerRecord * self.recordsPerBuffer
        self.channels = acq_channel
        
        sample_type = ctypes.c_uint8 if bytesPerSample == 1 else ctypes.c_uint16
        self.buffers = []
        for i in range(buffer_count):
            self.buffers.append(ats.DMABuffer(self.board.handle, sample_type, self.bytesPerBuffer))   
        

    def start_capture(self):
        self.board.startCapture()
        self.is_capturing = True

    # 只考虑A通道的代码，升级了兼容性
    # def get_one_acquisition(self, all_data, curr_pos_str, timeout_ms, Average_Enable=False):
    # # 1. 彻底终止之前的异步任务，防止残留
    #     self.board.abortAsyncRead()
        
    #     # 2. 【关键重置】软件索引归零
    #     # 确保我们 wait 的第一个 buffer 永远是接下来 position 的第一个 buffer
    #     self.buffer_idx = 0 

    #     # 3. 重新配置本次点的采集参数
    #     self.board.beforeAsyncRead(self.channels,
    #                                 0,
    #                                 self.samplesPerRecord,
    #                                 self.recordsPerBuffer,
    #                                 self.recordsPerPoint, 
    #                                 ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT)

    #     # 4. 重新挂载 Buffers
    #     # 驱动现在知道：这 bufferCount 个缓冲区是按顺序给这 recordsPerPoint 用的
    #     for buf in self.buffers:
    #         self.board.postAsyncBuffer(buf.addr, buf.size_bytes)

    #     # 3. 正式开始接受外部触发 (此时激光打过来的脉冲才是有效信号)
    #     self.board.startCapture()
    #     self.is_capturing = True
    #     # ================================

    #     pixel_data_buffers = []
    #     sub_timeout = int(timeout_ms / self.buffersPerPoint) if self.buffersPerPoint > 0 else timeout_ms
        
    #     for _ in range(self.buffersPerPoint):
    #         data = self._fetch_next_buffer(sub_timeout)
    #         if data is not None:
    #             pixel_data_buffers.append(data)

    #     # 4. 获取完当前点的所需数据后，立即终止异步读取！丢弃后续的激光触发。
    #     self.board.abortAsyncRead()
    #     self.is_capturing = False

    #     # --- 后续的平均和保存逻辑不变 ---
    #     if Average_Enable and len(pixel_data_buffers) > 0:
    #         combined_raw = np.concatenate(pixel_data_buffers)
    #         summed_data = np.sum(combined_raw.reshape(-1, self.samplesPerRecord), 
    #                              axis=0, dtype=np.uint32)
    #         all_data.append([summed_data, curr_pos_str])
    #     else:
    #         all_data.append([pixel_data_buffers, curr_pos_str])


    def get_one_acquisition(self, all_data, curr_pos_str, timeout_ms, Average_Enable=False):
        """
        使用B通道会同时采集A通道数据, 需要进行数据buffer的抽取, 并需要降低采样率为2G/s, 并存在引入噪声采样的问题 (4G/s 可以避免这个问题) 
        """
        # 1. 彻底终止之前的异步任务，防止残留
        self.board.abortAsyncRead()
        
        # 2. 【关键重置】软件索引归零
        self.buffer_idx = 0 

        # 3. 重新配置本次点的采集参数
        self.board.beforeAsyncRead(self.channels,
                                    0,
                                    self.samplesPerRecord,
                                    self.recordsPerBuffer,
                                    self.recordsPerPoint, 
                                    ats.ADMA_EXTERNAL_STARTCAPTURE | ats.ADMA_NPT)

        # 4. 重新挂载 Buffers
        for buf in self.buffers:
            self.board.postAsyncBuffer(buf.addr, buf.size_bytes)

        # 5. 正式开始接受外部触发
        self.board.startCapture()
        self.is_capturing = True

        pixel_data_buffers = []
        sub_timeout = int(timeout_ms / self.buffersPerPoint) if self.buffersPerPoint > 0 else timeout_ms
        
        # --- 自动检测是否为 A+B 交织模式 ---
        # 如果 self.channels 同时包含 A 和 B，则判定为交织模式
        is_dual_channel = (self.channels & ats.CHANNEL_A) and (self.channels & ats.CHANNEL_B)

        for _ in range(self.buffersPerPoint):
            data = self._fetch_next_buffer(sub_timeout)
            if data is not None:
                if is_dual_channel:
                    # ========================================================
                    # 【逻辑 A：双通道提取 B】
                    # 数据格式：[A0, B0, A1, B1...] -> 提取 B：[1::2]
                    data_to_append = data[1::2]
                    pixel_data_buffers.append(data_to_append)
                else:
                    # 【逻辑 B：单通道模式】
                    # 数据本身就是纯净的信号，直接添加
                    pixel_data_buffers.append(data)
        
        # 6. 获取完当前点的所需数据后立即终止
        self.board.abortAsyncRead()
        self.is_capturing = False

        # --- 后续逻辑处理 ---
        if Average_Enable and len(pixel_data_buffers) > 0:
            # 无论上面是哪种模式，这里的 pixel_data_buffers 每个元素长度均已对齐 samplesPerRecord
            combined_raw = np.concatenate(pixel_data_buffers)
            
            # 使用 uint32 防止 64 次累加溢出
            summed_data = np.sum(combined_raw.reshape(-1, self.samplesPerRecord), 
                                axis=0, dtype=np.uint32)
            
            all_data.append([summed_data, curr_pos_str])
        else:
            if len(pixel_data_buffers) == 0:
                print("data error - ljb") # 保留你的错误标识
            all_data.append([pixel_data_buffers, curr_pos_str])

    # 注意：_fetch_next_buffer 里面可以保留 postAsyncBuffer，
    # 但由于每次每个点结束后我们 abort 了，其实对于单点仅需 1 个 Buffer 的情况，不 post 也没事。
        
   
    def _fetch_next_buffer(self, timeout_ms):
        data_copy = None
        buffer = None
        
        try:
            buffer = self.buffers[self.buffer_idx % self.bufferCount]
            
            # 等待采集完成
            self.board.waitAsyncBufferComplete(buffer.addr, timeout_ms=timeout_ms)
            
            # 拷贝数据
            data_copy = np.copy(buffer.buffer)
            
            # 【关键】只有在这里提交！提交后，buffer 就归板卡管了
            self.board.postAsyncBuffer(buffer.addr, buffer.size_bytes)
            
            self.buffer_idx += 1
            
        except Exception as e:
            print(f"\n[ERROR] 获取缓冲区失败: {e}")
            # 如果发生了超时或其他错误，buffer 可能还在板卡手里，也可能在挂起状态
            # 这里的处理逻辑取决于你是否想尝试恢复采集
            
        return data_copy


    def stop_capture(self):
        # print("🛑 [DAQ] 停止采集")
        self.board.abortAsyncRead()
        self.is_capturing = False