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
        while not self.stop_event.is_set() or not self.update_queue.empty():
            try:
                # 接收任务包
                task = self.update_queue.get(timeout=0.1)
                
                if isinstance(task, int):
                    # 如果是数字，更新进度步数
                    if self.pbar: self.pbar.update(task)
                elif isinstance(task, str):
                    # 如果是字符串，更新左侧描述文字
                    if self.pbar: self.pbar.set_description(task)
                
                self.update_queue.task_done()
            except queue.Empty:
                continue

    def update(self, n=1):
        """更新进度数值"""
        self.update_queue.put(n)

    def set_description(self, desc):
        """[新增] 动态更新左侧描述文字"""
        self.update_queue.put(str(desc))

    def start(self, total, desc="Scanning"):
        """初始化并启动后台进度条"""
        self.stop_event.clear()
        self.pbar = tqdm(total=total, desc=desc, unit="pt", leave=True)
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()


    def stop(self):
        """优雅关闭"""
        self.stop_event.set()
        if self.worker_thread:
            self.worker_thread.join()
        if self.pbar:
            self.pbar.close()
            self.pbar.refresh()
            self.pbar.close()
            print("")

# 暴露给外部的单例对象
progress_manager = AsyncProgress()