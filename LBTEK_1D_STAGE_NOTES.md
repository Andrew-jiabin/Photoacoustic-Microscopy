# LBTEK 一维位移台（EM-LSS65-13C1）使用与调试笔记

> 更新时间：2026-08-20（实机调试完成，1mm 范围往返测试 PASS）
> 适用脚本：`PAM_Main_LBTEK.py`（一维单线 PAM 扫描成像主程序）

---

## 1. 硬件与连接

| 项 | 值 |
| :--- | :--- |
| 位移台 | 麓邦 LBTEK **EM-LSS65-13C1**（一维直线台，行程 <30mm） |
| 控制器 | 麓邦 EM-CVx（配套官方软件 `EM_CVx_V2.2.1`） |
| 通信口 | **COM11（CH340 USB 转串口）**，VID_1A86&PID_7523 |
| SDK | `LBTEK_SDK\x64\moverLibrary.dll`（+ `deviceModelsParam` 型号配置） |
| 型号参数 | 2000 脉冲/圈 = 1mm（0.5 µm/脉冲）；`unitString=mm`；`restoreSpeed=2` |
| 位置反馈 | **开环**（位置为软件计数，`GetCurrentPos` 的 ok 标志恒 0 属正常） |

**识别要点**：`listPorts` 会列出电脑上**所有**串口（实测含 ELMO GMAS、Prior 台等），
必须逐个 `openEmcvx` + `getDeviceCode`，取 `deviceCode > 0` 的端口才是麓邦控制器。
不能取 listPorts 第一个。

## 2. 正确调用序列（官方 SDK 示例 widget.cpp 为权威参考）

```python
mv = LBMover(DLL_PATH)                    # DLL 内部会 chdir 到 x64 目录读取 deviceModelsParam
h  = mv.openEmcvx(b"COM11")               # 必须 "COM11" 格式；纯数字 "11" 会打开失败
axis_count = mv.getDeviceCode(h)          # -> 1
mv.initAxis(h, AXIS_ID, b"EM-LSS65-13C1", axis_count)   # AXIS_ID = 1（重要！）
```

- **轴 ID 必须为 1**（官方示例 `comboBox->currentIndex() + 1`）。用 ID=0 会读到无效位置、
  运动命令全部无效（moveEmcvx 返回 0 但电机不动、doing 恒 0）。
- 运动命令：
  - 绝对移动：`setAbsoluteDisp(h, 1, x)` → `moveEmcvx(h, 1, 0x06)`（MOVE）
  - 相对移动：`setRelativeDisp(h, 1, d)` → `moveEmcvx(h, 1, 0x04/0x05)`（DRIVE_R/L）
  - JOG：`setJogStep/setJogTime/setJogDelay` → `moveEmcvx(h, 1, 0x07/0x08)`（JOG_R/L）
  - 回原点：`moveEmcvx(h, 1, 0x02)`（RESTORE）
  - 停止：`moveEmcvx(h, 1, 0x01)`（STOP）
- 到位检测：`getDoingState(h, 1) == 0` 表示运动结束；之后**必须再等 ~2s** 才能读到稳定位置。

## 3. 关键坑与教训（血泪经验，务必遵守）

### 3.1 严禁乱发配置命令（会导致控制器锁死）
- **绝对不要**对不知含义的寄存器发 `setInputEnable / setOutputEnable / setRelativePosEnable`。
- 实测：排查时发送这些命令后，控制器进入**错误码 9024 锁定**，所有运动被拒绝，
  **连官方软件都无法控制**，只能**断电重启**恢复。
- **遇错先断电重启，不要继续试命令**。

### 3.2 错误码 9024
- 特征：连接后立即存在（与参数设置无关）；`GetCurrentPos` 读到假大值（如 98.99mm，
  而实际行程 <30mm）；`getOriginEable=0`；所有运动命令被拒绝（doing 恒 0）。
- 恢复：断电重启控制器，错误码归 0，原点信号恢复 1，位置读数正常。

### 3.3 加速度 0.1 mm/s² 不可用
- 低加速度下加速距离 = v²/(2a)。v=1mm/s、a=0.1 时加速距离 5mm >> 0.1mm 行程，
  电机物理上无法完成小行程（实测 0.1mm 目标只走 0.0045mm）。
- **最小可用加速度 = 0.5 mm/s²**（实测 0.5mm 目标到位误差 0.0000mm）。

### 3.4 settle 时间必须 ~2s
- move 后 0.3s 读数不稳（曾误判 0.0955mm 误差）；等 2s 后读数 0.0000mm。
- 测试脚本与主程序均采用 `SETTLE_TIME_S = 2.0`。

### 3.5 ±1mm 大行程需到位重试
- acc=0.5 下 1mm 行程运动学受限，首次可能停在不正确位置（误差 ~0.17mm），
  **重新下发同一目标即可到位**（重试一次后误差 0.0000mm）。
- 主程序已内置到位校验 + 重试逻辑。

## 4. 实测参数与结果（2026-08-20）

| 参数 | 值 |
| :--- | :--- |
| 速度 | **2.0 mm/s** |
| 加速度 | **0.5 mm/s²**（尽可能低且可用） |
| settle | 2.0 s |
| 到位公差 | 0.01 mm |

1mm 范围往返测试（±0.1 / ±0.5 / ±1.0 mm，各回基准）：

| 偏移 | 误差 |
| :--- | :--- |
| ±0.1 mm | 0.0000 mm ✅ |
| ±0.5 mm | 0.0000 mm ✅ |
| ±1.0 mm | 0.0000 mm ✅（首次 0.17，重试后到位） |

**总体判定：PASS，1mm 范围扫描可用。**

## 5. 脚本清单与用法

| 脚本 | 用途 |
| :--- | :--- |
| `PAM_Main_LBTEK.py` | **一维单线 PAM 扫描成像主程序**（Alazar 采集 + .mat 保存） |
| `Tool_code/lbtek_safe_motion_test.py` | 1mm 范围往返安全测试（自带到位校验/重试/急停） |
| `Tool_code/lbtek_readonly_check.py` | 只读自检（枚举 → 连接 → 读参数，不移动） |

运行（远端 PAM conda 环境，工作目录 = 项目根目录）：

```powershell
C:\Users\20211\.conda\envs\PAM\python.exe PAM_Main_LBTEK.py
C:\Users\20211\.conda\envs\PAM\python.exe Tool_code\lbtek_safe_motion_test.py
```

`PAM_Main_LBTEK.py` 关键参数（文件头部可改）：
- `SCAN_N_POINTS` / `SCAN_RANGE_MM`：单线点数与范围（默认 11 点 / 1mm）
- `MAX_MOVE_MM = 1.0`：安全护栏，单次移动超过即拒绝启动
- `COM_PORT = ""`：留空自动识别麓邦串口；可硬编码 `"11"`

## 7. 成像逻辑审查（2026-08-20 二次审查）

主程序已确认**仅包含一维扫描逻辑，无任何 Prior 代码依赖**（文件头已改写为独立程序说明）。

| 环节 | 说明 |
| :--- | :--- |
| 初始化链 | `LBMover` → `openEmcvx(COM11)` → `getDeviceCode` → `initAxis(ID=1)` |
| DAQ 链 | `AlazarNPTSystem` 配置（ATS9373, 4GS/s, 4096 samples, 外部触发 NPT）→ `get_one_acquisition` 逐点采集，`buffersPerPoint=1`（256 records/点平均） |
| 轨迹 | 一维单线：`start_pos + i*step`，11 点 / 1mm，步长 0.1mm |
| 每点流程 | 绝对移动 → `lbtek_wait_settled` → settle 2s → **到位校验（>0.01mm 重试一次）** → 采集 |
| 异常处理 | `KeyboardInterrupt`/异常 → `STOP`(0x01) + 回起点（带校验重试） |
| 数据保存 | `.mat`，坐标经 `sanitize_pos_to_key` 作 key；metadata 记录参数；**采集失败点自动跳过不破坏整体** |

审查后修复项：
1. 文件头 docstring 清除 Prior/SDK 复制说明（程序仅一维扫描）。
2. **回起点加到位校验**（超差自动重试一次）。
3. **保存时跳过无效数据点**（采集失败的空 buffer 不再导致整个保存崩溃）。

## 6. 安全注意事项

- **台上载有精密仪器**：任何实机移动测试都从 0.05–0.1 mm 极小范围起步，逐步扩大。
- 测试前确认串口空闲（官方软件 EM_CVx 需先关闭）。
- 回原点（RESTORE）会移动台子到原点传感器，操作前确认路径无障碍。
- 官方 SDK 示例源码：`Tool_code/emcvx_example/EM_cvx_libraryExample/emcvxExample/widget.cpp`。
