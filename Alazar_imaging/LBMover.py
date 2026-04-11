import ctypes
import os

class LBMover:
    def __init__(self, dll_path=r"D:\LJB\alazar_DAQ\Photoacoustic-Microscopy\LBTEK_SDK\x64\moverLibrary.dll"):
        """
        初始化类，负责加载 DLL 动态库并配置环境。
        :param dll_path: DLL 文件的绝对路径
        实验室目前唯一一台是: EM-LSS65-13C1 (1维位移台)
        """
        # 1. 路径处理
        self.dll_abs_path = os.path.abspath(dll_path) # 将相对路径转为绝对路径
        dll_dir = os.path.dirname(self.dll_abs_path)  # 获取 DLL 所在的文件夹路径
        
        # 2. 环境切换（核心步骤）
        # 麓邦的 DLL 启动时会读取同目录下的 'deviceModelsParam' 配置文件。
        # 如果不切换工作目录，DLL 会因为找不到配置文件而导致 open 失败。
        self.orig_dir = os.getcwd() # 保存 Python 脚本当前的运行目录
        os.chdir(dll_dir)           # 临时切换到 DLL 所在目录
        
        try:
            # 加载 DLL 库。CDLL 用于加载遵循 cdecl 调用约定的 C 库
            self.lib = ctypes.CDLL(self.dll_abs_path)
        except Exception as e:
            os.chdir(self.orig_dir) # 如果加载失败，务必切换回原目录
            raise RuntimeError(f"无法加载库文件，请检查路径或 Python 位数: {e}")
            
        self.handle = -1            # 初始化句柄为 -1（代表未连接状态）
        self._setup_all_functions() # 执行函数原型配置
        os.chdir(self.orig_dir)     # 配置完成后，切回原工作目录

    def _get_f(self, name):
        """
        内部辅助函数：自动对齐说明书名与 DLL 真实导出名。
        例如：说明书叫 'openEmcvx'，如果 DLL 里实际叫 'open'，它会自动匹配。
        """
        if hasattr(self.lib, name):
            return getattr(self.lib, name) # 找到原名直接返回
        
        short_name = name.replace("Emcvx", "") # 尝试去掉后缀
        if hasattr(self.lib, short_name):
            return getattr(self.lib, short_name)
        return None

    def _setup_all_functions(self):
        """
        配置所有 C 函数的输入参数类型 (argtypes) 和返回类型 (restype)。
        这是防止 Python 崩溃（Segment Fault）的最重要步骤。
        """
        
        # --- A. 字符串处理类函数 ---
        # listPorts(char*, int) 和 getAllModels(char*, int)
        for f_name in ['listPorts', 'getAllModels']:
            f = self._get_f(f_name)
            if f: 
                # 参数 1: 字符串缓冲区, 参数 2: 长度
                f.argtypes = [ctypes.c_char_p, ctypes.c_uint]
                f.restype = ctypes.c_int # 返回搜到的数量
                setattr(self, f_name, f) # 将函数绑定到当前实例

        # --- B. 基础连接函数 ---
        # openEmcvx(char*) -> 返回 int 句柄
        f = self._get_f('openEmcvx')
        if f: f.argtypes, f.restype = [ctypes.c_char_p], ctypes.c_int; setattr(self, 'openEmcvx', f)
        
        # isOpen(char*) -> 返回 0/1
        f = self._get_f('isOpenEmcvx')
        if f: f.argtypes, f.restype = [ctypes.c_char_p], ctypes.c_int; setattr(self, 'isOpenEmcvx', f)
        
        # closeEmcvx(int)
        f = self._get_f('closeEmcvx')
        if f: f.argtypes, f.restype = [ctypes.c_int], ctypes.c_int; setattr(self, 'closeEmcvx', f)

        # --- C. 运动控制与整数设置 (参数多为 int, int) ---
        # 大部分函数返回 0 代表成功，负数代表失败
        int_funcs = [
            'setJogTime', 'setJogDelay', 'setInputEnable', 'setOutputEnable',
            'setAxisEnable', 'setRelativePosEnable', 'getDoingState', 'getPositiveLimitEnable',
            'getNegativeLimitEnable', 'getOriginEable', 'getJogTime', 'getJogDelay',
            'getAxisType', 'getInputEnable', 'getOutputEnable', 'getAxisEnable',
            'getRelativePosEnable', 'getErrorCode'
        ]
        for name in int_funcs:
            f = self._get_f(name)
            if f: f.argtypes, f.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int; setattr(self, name, f)
        
        # 特殊处理：moveEmcvx 需要 3 个参数: handle, ID, func_code
        f = self._get_f('moveEmcvx')
        if f: 
            f.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
            f.restype = ctypes.c_int
            setattr(self, 'moveEmcvx', f)

        # --- D. 浮点数设置类 (handle, ID, float_val) ---
        float_set_funcs = ['setSpeed', 'setAcceleration', 'setAbsoluteDisp', 'setRelativeDisp', 'setJogStep']
        for name in float_set_funcs:
            f = self._get_f(name)
            if f: f.argtypes, f.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_float], ctypes.c_int; setattr(self, name, f)

        # --- E. 浮点数获取类 (handle, ID) -> 返回 float ---
        float_get_funcs = ['getSpeed', 'getAcceleration', 'getAbsoluteDisp', 'getRelativeDisp', 'getJogStep']
        for name in float_get_funcs:
            f = self._get_f(name)
            if f: f.argtypes, f.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_float; setattr(self, name, f)

        # --- F. 其他特殊函数 ---
        # 获取控制器轴数
        f = self._get_f('getDeviceCode')
        if f: f.argtypes, f.restype = [ctypes.c_int], ctypes.c_int; setattr(self, 'getDeviceCode', f)

        # 获取当前位置：GetCurrentPos(handle, ID, int* ok) -> 返回 float
        f = self._get_f('GetCurrentPos')
        if f: 
            f.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
            f.restype = ctypes.c_float
            setattr(self, 'GetCurrentPos', f)

        # 初始化轴：根据说明书 5.2，参数为 (handle, ID, model_str, AxisCount)
        f = self._get_f('initAxis')
        if f: 
            f.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            f.restype = ctypes.c_int
            setattr(self, 'initAxis', f)

    # --- 方便直接调用的 Python 封装 ---
    def get_pos(self, axis_id=0):
        """获取指定轴的当前位置"""
        ok_flag = ctypes.c_int(0) # 创建一个 C 语言整数用于接收状态
        pos = self.GetCurrentPos(self.handle, axis_id, ctypes.byref(ok_flag))
        return pos, ok_flag.value # 返回 (位置浮点数, 状态码)
