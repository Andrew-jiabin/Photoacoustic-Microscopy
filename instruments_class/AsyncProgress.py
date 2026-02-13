import queue
import threading
from tqdm import tqdm
import time

class AsyncProgress:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        # 确保全局只有一个进度条管理实例
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(AsyncProgress, cls).__new__(cls)
                cls._instance._init_manager()
        return cls._instance

    def _init_manager(self):
        self.update_queue = queue.Queue()
        self.pbar = None
        self.stop_event = threading.Event()
        self.worker_thread = None

    def _worker(self):
        """后台线程：负责真正更新 tqdm 界面"""
        while not self.stop_event.is_set() or not self.update_queue.empty():
            try:
                # 阻塞式等待任务，超时时间设短一点以便响应停止信号
                n = self.update_queue.get(timeout=0.1)
                if self.pbar:
                    self.pbar.update(n)
                self.update_queue.task_done()
            except queue.Empty:
                continue

    def start(self, total, desc="Scanning"):
        """初始化并启动后台进度条"""
        self.stop_event.clear()
        self.pbar = tqdm(total=total, desc=desc, unit="pt", leave=True)
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()

    def update(self, n=1):
        """主程序调用的接口：只负责塞任务进队列，瞬间完成"""
        self.update_queue.put(n)

    def stop(self):
        """优雅关闭"""
        self.stop_event.set()
        if self.worker_thread:
            self.worker_thread.join()
        if self.pbar:
            self.pbar.close()

# 暴露给外部的单例对象
progress_manager = AsyncProgress()