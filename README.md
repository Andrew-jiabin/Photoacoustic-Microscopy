# Photoacoustic-Microscopy
## By Jiabin Lin

# NanoMax 压电平台配置记录

更新时间：2026-07-01

## 实物型号与用途

| 位置 | NanoMax 型号 | 控制器 | 控制方式 | 程序用途 |
| :--- | :--- | :--- | :--- | :--- |
| 样品台 | MAX311D | BPC303, serial `71241834` | 闭环位置控制 | `SCAN_TARGET = "sample_closed_loop"` 时移动样品 |
| 探针台 | MAX312D | MDT693B, serial `2201287140-09` | 开环电压控制 | `SCAN_TARGET = "probe_open_loop"` 时保持样品不动，只移动探针 |

闭环样品台的 BPC303 通道已经按程序约定连接：

| 轴 | BPC303 通道 |
| :--- | :--- |
| X | CH1 |
| Y | CH2 |
| Z | CH3 |

## 电压安全限制

Thorlabs `MAX300 Series NanoMax 3-Axis Flexure Stage User Guide` (`10997-D02`) 对 NanoMax 压电执行器给出的关键限制：

- 压电执行器最大输入电压：`75 V`
- 压电行程：`20 um`
- 典型压电分辨率：`5 nm`
- 手册明确提示 SMC 接口处可能存在最高 `75 V` 的高压，需要保持电缆连接且注意安全。

因此，MAX311D 和 MAX312D 都按 `75 V` 作为软件安全上限处理。不要因为控制器本身可能支持更高输出范围，就把 NanoMax 压电平台的限制提高到 75 V 以上。

## 2026-07-01 只读枚举结果

本次只运行了只读/连接类函数，没有运行任何 `SetPosition`、`SetOutputVoltage`、`SetXAxisVoltage`、`SetYAxisVoltage`、`SetZAxisVoltage` 或 `SetXYZAxisVoltage`。

Kinesis/BPC 只读枚举：

- `DeviceManagerCLI.GetDeviceList()` 返回：`71241834`
- `DeviceFactory.GetDeviceInfo("71241834")` 返回：
- `GetDescription() = "APT Piezo Controller"`
- `GetTypeID() = 71`
- `GetPID() = 64240`
- `GetSerialNo() = 71241834`

MDT 开环控制器只读枚举：

- `MDT_COMMAND_LIB_x64.dll List` 连续枚举到：`2201287140-09, ... MDT693B, Thorlabs Inc.`
- 枚举过程中 COM 口曾显示为 `COM4` 和 `COM5`，因此程序应优先使用 serial number 自动打开，而不是长期硬编码 COM 口。
- `Open(serial, 115200, 3)` 返回 handle `0`，`IsOpen` 返回 `1`，`Close` 返回 `0`。
- `GetLimitVoltage` 和 `GetXAxis/YAxis/ZAxis Min/MaxVoltage` 均返回 `-1`，所以当前 DLL 不能可靠读出 MDT693B 内部电压限制；实际安全限制以 NanoMax 手册的 `75 V` 为准。

## 程序使用注意

- 闭环样品扫描使用 `BPC303NativeController` 和 `SCAN_TARGET = "sample_closed_loop"`。
- 开环探针扫描使用 `MDT693BController` 和 `SCAN_TARGET = "probe_open_loop"`。
- 开环探针没有位置反馈，保存坐标单位是 `V`，不是 `um`。
- 启用探针扫描时必须显式设置 `PROBE_STEP_V`，或先完成标定后设置 `PROBE_UM_PER_V`。
- 未经现场确认，不要运行任何会改变压电输入电压或位置的测试脚本。
- 激光与 NanoMax 主程序集成细节见 `reference/PAM_Nanomax_laser_integration_notes.md`。532 nm CBOX 关闭以 D2XX `9600 8N1 no-flow` 通信和 `flags` 读回为准；软件只能关闭 emission，不能替代前面板物理 Laser OFF/Stand By 操作。
- 采集结束数据先保存默认 `.mat`，再按需安全 rename 后缀；采集中按 `q` 暂停后可用 `p/a/3/i` 在 `results/cache` 生成当前数据预览。
- Maintenance note: `results/cache/` is preview-only and ignored by git; return-to-start uses `PAM_SAMPLE_RETURN_POSITION_TIMEOUT_S` separately from acquisition settle timeout.

# 位移台性能测试
### 经验
    1. 当SS设置为1的时候，基本上都无法准确到达下一位置，但是SS为2的时候，全都变得能准确到达  
    2. 


### 测试样例
| Step | Start_Pos | Target_Pos | Actual_Pos | Time (ms) | Result |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | [-802868,158379] | [-802867,158380] | [-802867,158380] | 114.56 | ✅ SUCCESS |
| 10 | [-802868,158379] | [-802858,158389] | [-802858,158389] | 121.09 | ✅ SUCCESS |
| 100 | [-802868,158379] | [-802768,158479] | [-802768,158479] | 117.52 | ✅ SUCCESS |
| 1000 | [-802868,158379] | [-801868,159379] | [-801868,159379] | 129.31 | ✅ SUCCESS |
| 5000 | [-802868,158379] | [-797868,163379] | [-797868,163379] | 189.65 | ✅ SUCCESS |

### 位移台稳定性统计测试 (SS=2, Repeats=100)
| Step | Mean Time (ms) | Variance | Success Rate | Final Status |
| :--- | :--- | :--- | :--- | :--- |
| 1 | 115.24 | 12.37 | 100% | ✅ STABLE |
| 10 | 115.08 | 10.68 | 100% | ✅ STABLE |
| 100 | 114.89 | 14.23 | 100% | ✅ STABLE |
| 1000 | 131.68 | 11.72 | 100% | ✅ STABLE |
| 5000 | 192.81 | 15.95 | 100% | ✅ STABLE |
