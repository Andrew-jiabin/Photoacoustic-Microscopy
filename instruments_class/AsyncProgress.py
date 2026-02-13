import queue
import threading
from tqdm import tqdm
import time

class AsyncProgress:
    COLORS = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "magenta": "\033[95m",
        "cyan": "\033[96m",
        "white": "\033[97m",
        "reset": "\033[0m" # 必须加 reset，否则后面所有文字都会变色
    }
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
                task = self.update_queue.get(timeout=0.1)
                
                if isinstance(task, int):
                    if self.pbar: self.pbar.update(task)
                elif isinstance(task, str):
                    # 如果字符串以 '#' 开头或属于特定颜色名，则改变颜色
                    if task in ['red', 'green', 'blue', 'cyan', 'yellow', 'magenta']:
                        if self.pbar: self.pbar.colour = task
                    else:
                        if self.pbar: self.pbar.set_description(task)
                
                self.update_queue.task_done()
            except queue.Empty:
                continue

    def set_colour(self, color_name):
        """主程序调用的变色接口"""
        self.update_queue.put(str(color_name))

    def update(self, n=1):
        """更新进度数值"""
        self.update_queue.put(n)

    def set_description(self, text, color=None):
        """
        [增强版] 支持带颜色的描述文字
        :param text: 描述内容
        :param color: 颜色名称 (如 "yellow", "green")
        """
        if color in self.COLORS:
            # 格式：[颜色代码][文字][重置代码]
            formatted_desc = f"{self.COLORS[color]}{text}{self.COLORS['reset']}"
        else:
            formatted_desc = text
        self.update_queue.put(str(formatted_desc))

    def start(self, total, desc="PAM Scan"):
        self.stop_event.clear()
        # 整合你看到的彩色和自动对齐参数
        self.pbar = tqdm(
            total=total, 
            desc=desc, 
            unit="pixel",           # [新] 显示为 pixel/s
            leave=True, 
            dynamic_ncols=True,     # [新] 自动适应终端宽度
            colour="cyan",          # [新] 你可以根据喜好改成 blue, green 等
            ascii=False,             # 确保使用平滑的方块而不是 # 号
            smoothing=0.3
        )
        self.worker_thread = threading.Thread(target=self._worker, daemon=True)
        self.worker_thread.start()


    def stop(self):
        """优雅关闭"""
        self.stop_event.set()
        if self.worker_thread:
            self.worker_thread.join()
        if self.pbar:
            self.pbar.refresh()
            self.pbar.close()

# 暴露给外部的单例对象
progress_manager = AsyncProgress()