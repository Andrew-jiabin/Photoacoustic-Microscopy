# YOKOGAWA DLM3034 实时波形读取技术报告

> 编制日期：2026-10-18
> 设备：`YOKOGAWA,DLM3034,90Y500919,F2.00`（350 MHz / 4 通道，选件 `125MPOINTS,LOGIC`）
> 网络：示波器 `192.168.1.100`，与 PAM-NEW（tailnet `100.79.154.4`）同接一个路由器
> 代码：`Nanomax/dlm_scope.py`、`Tool_code/dlm_scope_cli.py`、`Tool_code/dlm_scope_web.py`、`Tool_code/test_dlm_scope_offline.py`
> 依赖：**零第三方依赖**（纯 Python 标准库）。不需要 pyvisa / NI-VISA / 任何驱动。

---

## 0. 结论速查（下次直接看这一段）

| 问题 | 结论 |
|---|---|
| 走什么协议 | **VXI-11（ONC RPC，TCP 111）**。示波器不开裸 socket（5025/10001/23/80 全拒），只开 111 和 21 |
| 波形用什么命令 | 只有一条：`:WAVEFORM:SEND?`，回 IEEE 488.2 定长块 `#9<9位数字><payload>` |
| 单通道一帧要多久 | **~450–480 ms** → **~2.1–2.2 Hz**（这是新记录的天花板） |
| **四通道一帧要多久** | **~590 ms → ~1.7 Hz**，即**四通道只比单通道贵 ~110 ms** |
| 多通道能不能时间对齐 | **能，但有前提**：`:START` 后必须让记录跑够 **~0.15 s** 再 `:STOP`（`MULTI_SETTLE_S`）。实测 8/8 对齐；settle 只有 0.02 s 时对齐率掉到 1/4 且反而更慢（831 ms） |
| 速度瓶颈在哪 | **在示波器本身，不在路由器**。局域网搬 250 kB 只要 3–11 ms（≈20–76 MB/s），而等新记录固定 ~320 ms，且对窗口长度、记录长度、时基、holdoff、触发模式**全部不敏感** |
| 怎么给实时控制用 | 用 `Streamer` 后台线程解耦：抓取按示波器节奏走，控制循环调 `snapshot()` **微秒级返回**并附带 `age_s` |
| 能不能在示波器运行时读 | **不能**。运行中 `:WAVEFORM:SEND?` 返回 `#9000000000`（0 字节合法块），必须 `:STOP` 之后读 |
| `:STOP` 之后记录会冻结吗 | **不会**。示波器继续以 ~2 Hz 采，`SEND?` 只交出最新那条。`latest()` 不保证等于上一次 `read()` |
| **窗口切小能更实时吗** | **不能**。窗口 9999 点 → 99 点（数据减 100 倍），换记录速率仍是 **2.21–2.22 Hz**。0.45 s 是"产出新记录"的节拍，不是搬字节的节拍（见 §15.1） |
| **那怎么才算实时** | **别搬波形，读标量**：`:MEASure:MODE ON` + `:MEASure:CHANnel<n>:<item>:VALue?`，0.41 ms/次、**不用 STOP**、**2–9.4 Hz**（记录越短越快，地板 ~102 ms）（见 §15.3） |
| 标量通道最容易踩什么 | `:MEASure:MODE` 出厂 **OFF**，不开就全是 `NAN`；且必须先设时基再开测量，否则又回 `NAN` |

**一句话选型建议**：要"实时"就别搬波形——让示波器把波形算成一个数（`MeasureStreamer`，2–9.4 Hz），需要波形本身时才走 `Streamer`（~2.2 Hz）。若控制回路快于 0.1 s，示波器仍**不能**当主传感器——应该用 ATS9373 采集卡的片上 FFT；示波器适合当**慢速参考/第二意见/标定用**。

---

## 1. 设备身份与网络拓扑

### 1.1 两台同名 PC（重要，别连错）

实验室有两台**同名同型号**的主机，hostname 都是 `DESKTOP-T8UBAOM`，型号都是 HP Pro Tower ZHAN 99 G9。**只能按 tailnet IP 认身份**：

| tailnet IP | ssh 别名 | `D:\LJB`（仓库 + 采集卡板子） | `D:\Phd_training`（说明书等资料） |
|---|---|---|---|
| **100.79.154.4** | **`PAM-NEW`** | ✅ | ❌ |
| **100.118.93.35** | `Work` | ❌ | ✅ |

开发在 **PAM-NEW** 上做（采集卡在那台）；说明书在 **Work** 上。所有跨机操作前先确认 IP。

### 1.2 仪器自报身份

```
*IDN?  ->  YOKOGAWA,DLM3034,90Y500919,F2.00
:OPT?  ->  125MPOINTS,LOGIC
```

注意：**实物是 DLM3034，不是说明书封面的 DLM3054**。同属 DLM3000 系列，用户手册通用，命令集一致。

### 1.3 端口探测结果（PAM-NEW 上 `Test-NetConnection`）

| 端口 | 服务 | 结果 |
|---|---|---|
| 111 | ONC RPC portmapper（VXI-11） | **开放** |
| 21 | FTP | **开放** |
| 23 | Telnet | 拒绝 |
| 80 | HTTP | 拒绝 |
| 5025 | SCPI raw socket（Keysight 惯例） | 拒绝 |
| 10001 | NI VISA socket | 拒绝 |
| 4880 / 5000 | — | 拒绝 |

→ **VXI-11 是唯一可用的命令通道**。

---

## 2. 说明书能给的信息（及其边界）

本地说明书：`D:\Phd_training\exp_materials\小示波器\DLM3000用户手册_第1版_IMDLM3054-02CN_19.05.17.pdf`（280 页，13.0 MB）。

第 18 章「以太网通信」关于远程读取的**全部原文**只有一句：

```
传输方式    以太网(1000BASE-T / 100BASE-TX / 10BASE-T)
通信协议    TCP/IP
支持服务    服务器: FTP、VXI-11 和 Socket
```

**关键边界**：

- 用户手册**不含 SCPI 命令表**。命令表在单独的《通信接口手册》`IM DLM3054-17EN`，本地没有。
- 横河官方 CDN（`cdn.tmi.yokogawa.com/IM*.pdf`）对该文件全部返回 **403 AccessDenied**，无法下载。
- 说明书里写的 "Socket" 服务在**这台机器上是关的**（5025/10001 全拒）。

→ **本报告中每一条 SCPI 命令、每一个时序数字，都是实测得到的**，不是从手册抄的。这也是为什么报告里保留了大量踩坑记录——下次换机器/换固件，这些是最值钱的部分。

---

## 3. 为什么自己写 VXI-11 客户端（而不用 pyvisa）

### 3.1 pyvisa-py 能通，但太慢

pyvisa-py 可以正常打开 `TCPIP::192.168.1.100::INSTR`，但读一帧 250 kB 记录要 **320–800 ms**。原因：它每次 `device_read` 都是一次独立 RPC 往返，而每次往返要空耗 ~28 ms；并且它按较小的 `requestSize` 分块，块数一多就线性叠加。

### 3.2 自研客户端

`Nanomax/dlm_scope.py` 里的 `Vxi11Transport` 是一个零依赖的 ONC RPC / VXI-11 客户端：

- 一次 RPC 拿全整块（`requestSize` 预置 600000）
- `TCP_NODELAY` 打开
- 实测 **250,012 B 只要 3–11 ms**（≈ 20–76 MB/s）

| 方案 | 250 kB 记录耗时 | 依赖 |
|---|---|---|
| pyvisa-py | 320–800 ms | pyvisa + pyvisa-py |
| **自研 VXI-11** | **3–11 ms** | 无（标准库） |

---

## 4. VXI-11 协议要点（复用的核心，含踩坑）

### 4.1 常量

```
portmapper : prog=100000  vers=2  proc=3 (GETPORT)  proto=6 (TCP)
core chan  : prog=0x0607AF vers=1
abort chan : prog=0x0607B0 vers=1
device_    : create_link=10  write=11  read=12  clear=15  destroy_link=23
device name: "inst0"
```

### 4.2 报文成帧

每条 RPC 前有一个 **4 字节 record mark**：`0x80000000 | length`，最后一帧置高位。大回复会分成多帧，**必须按 record mark 重组**，不能靠 `recv()` 一次拿全。`Vxi11Transport` 里对应 `_recv_exact(n, sock)` / `_recv_record(sock)`。

### 4.3 XDR 编码

- `pack_string` = 4 字节长度 + 数据 + 补零到 4 字节对齐
- `pack_opaque` 同上

### 4.4 ⚠️ 踩坑一：`device_write` 必须带 END 标志

VXI-11 spec table 5 的 `Device_Flags`，`OP_FLAG_END = 0x08`。

**不带 END 时的症状**：示波器收下字节但**不执行命令**；随后 `device_read` 挂满整个 io timeout，报 **VXI-11 error 15（io timeout）**。这个症状极容易误判成"读超时/网络问题"。

```python
def write(self, data, io_timeout=5000, flags=OP_FLAG_END):
    if isinstance(data, str):
        data = data.encode("ascii")
    args = struct.pack(">iIII", self.lid, io_timeout, 0, flags) + _xopaque(data)
    ...
```

### 4.5 ⚠️ 踩坑二：`device_write` 参数个数

参数是 `(link, io_timeout, lock_timeout, flags, data)` = **4 个前置 int + 一个 opaque**。

写成 5 个 int（`>iIIIi`）时，`device_write` **返回 size = 0**（示波器不接受）。这个也是靠 monkey-patch `socket.send/sendall`、把 pyvisa-py 和自研客户端的**线上字节逐位对比**才定位到的——当时 `device_read` 的字节两边完全相同，差异只在 write。

### 4.6 ⚠️ 踩坑三：`requestSize` 必须 ≥ 整个块

`requestSize = 65536` 时，DLM 会**把数据截断到 65536 B 并且照样置 END 位**（谎报传输完成），之后再 read 就报 error 15。

客户端固定 `expect = 2 * 300000`（≥ 整块），**不要只靠 END 位循环读**。

### 4.7 格式与字节序

`:WAVEFORM:FORMat WORD` + `:WAVEFORM:BYTeorder LSBFirst` → 小端 int16，2 B/point。
`:WAVEFORM:FORMat BYTE` → 1 B/point，**偏移二进制**（以 128 为零点）。

---

## 5. 波形 SCPI 命令（实测清单）

| 命令 | 作用 | 备注 |
|---|---|---|
| `*IDN?` | 身份 | 返回 DLM3034 |
| `:WAVEFORM:SEND?` | **唯一的波形传输命令** | 回 `#9<n><payload>` |
| `:WAVEFORM:TRACE <n>` | 选择源（`1`..`4` / `MATH1`） | **是选择器，不是数据查询**；`:WAVEFORM:TRACE?` 会报 `141 Invalid character data` |
| `:WAVEFORM:START <i>` / `:WAVEFORM:END <i>` | 选记录窗口 | `END 499999999` = 「到记录末尾」哨兵 |
| `:WAVEFORM:FORMat WORD\|BYTE` | 采样位宽 | WORD = 2 B/point |
| `:WAVEFORM:BYTeorder LSBFirst` | 字节序 | |
| `:WAVEFORM:SRATe?` | 采样率 | 有效（6.25e6） |
| `:CHANnel<n>:VDIV?` / `:OFFSet?` | 通道刻度/偏移 | 有效 |
| `:ACQuire:RLENgth?` / `:MODE?` | 记录长度 / 采集模式 | 有效（125000 / NORM） |
| `:TIMebase:TDIV?` | 时基 | 有效 |
| `:TRIGger:MODE?` | 触发模式 | 有效（AUTO / NORM） |
| `:START` / `:STOP` | 启动 / 停止采集 | **必须先 STOP 才读得到数据** |
| `:STATus:ERRor?` | 读错误队列 | ⚠️ **不是** `:SYSTem:ERRor?` |

**实测不存在的头部**（发下去会往错误队列推 `113,"Undefined header;..."`）：
`:SINGle`、`:SYSTem:ERRor?`、`:WAVEFORM:ASC?`、`:WAVEFORM:DATA?`、`:WAVEFORM:WORD?`、`:WAVEFORM:MODE?`、`:WAVEFORM:RLENGTH?`、`:WAVEFORM:STATUS?`、`:ACQUIRE:STATUS?`、`:ACQUIRE:SRATE?`、`:STARt?`、`:STOP?`、`:RUN?`、`:ACQuire:STATe?`、`:TIMebase:MODE?`、`:HISTory:MODE?`。

**实测存在、但不在波形路径上的头部**（§15.3 才挖出来的，别被 `410` 骗了）：

| 命令 | 说明 |
|---|---|
| `:MEASure:MODE ON\|OFF` | 自动测量引擎总开关，**出厂 OFF** |
| `:MEASure:CHANnel<n>:<item>:STATe 0\|1` | 逐项开关（item ∈ `RMS MAX MIN AMPL SDEV FREQ PER`） |
| `:MEASure:CHANnel<n>:<item>:VALue?` | 读标量，**0.41 ms/次，不需要 STOP** |
| `:MEASure?` | dump 整棵树（19 745 字符，21 trace × 36 item） |
| `:TIMebase:SRATe?` | 采样率（6.25e6） |
| `:ACQuire:COUNT?` | 返回 `INF`（连续采集），不是计数器 |
| `:COMMunicate?` / `:COMMunicate:HEADer?` | 通讯设置，`HEAD 1` 表示响应带头部 |
| `:MATH1:...` | 运算/FFT 配置（`HCUT/LCUT` 已见 10 MHz） |
| `:HISTory:...` | 历史记录（`REC 0`），未深挖 |

> ⚠️ **`410,"Query INTERRUPTED"` 的含义**：不是"命令不存在"，而是"这个头**存在**，但你发了查询却没把响应读走"。只有 `113,"Undefined header"` 才是真的不存在。我一开始就是先 `write` 再读错误队列，于是把 6 个存在的头误判成不存在。

### 5.1 块格式

```
#9 000250000 <payload 250000 bytes>
^^ ^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^
|  9位十进制长度
"#" + 一位长度位数(9)
```

`block_payload(data)` 负责剥头。空块（`#9000000000`）返回 `b""`；长度不足抛 `ScopeError`。

### 5.2 ⚠️ 采集中读不到数据

**采集运行时** `:WAVEFORM:SEND?` 返回 `#9000000000`——一个**格式完全合法的 0 字节块**。这非常容易被误判成"通道没配好"。实际含义是：运行中不给数据，必须 `:STOP`。

### 5.3 ⚠️ `:SINGle` 在这台固件上不存在

`:SINGle` 被发下去后，状态队列回报 `113,"Undefined header;:SINGLE"`，命令什么也不做。
→ **只有 `:START` → `:STOP` 才会产生新记录。**

### 5.4 ⚠️ 错误队列命令名

`:SYSTem:ERRor?` 在这台固件上是 undefined header，发了之后**查询无应答**，表现为读超时（不是报错）。正确的是 `:STATus:ERRor?`。
注意错误队列是**持久的**，会一直累积到被读走；调试时定期 `scope.errors()` 清空，否则会看到几百条历史残留。

---

## 6. 性能实测

### 6.1 ~320 ms 是固件常数

`:STOP` 本身只用 **1.6–7 ms**，但**紧接着的第一次 `:WAVEFORM:SEND?` 会阻塞 ~320 ms**。这是示波器在准备记录。下面每一项都单独改过，耗时**几乎不动**：

| 变量 | 取值 | 首次 SEND? 耗时 |
|---|---|---|
| 窗口 END | 999 / 9999 / 49999 / 124999 | 321 / 321 / 323 / 329 ms |
| `:ACQuire:RLENgth` | 125000 / 12500 / 1250 | 329 / 321 / 320 ms |
| `:TIMebase:TDIV` | 2 ms / 20 ms / 200 µs | 329 / 319 / 329 ms |
| `:TRIGger:HOLDoff` | MINimum / 0（都是 20 ns） | 321 / 321 ms |
| 触发模式 | AUTO / NORM（有效触发电平） | 321 / 319 ms |

→ **单次新记录端到端 ~0.45 s，~2.2 Hz 是天花板**。这不是本模块的 bug，也没找到任何能绕过的 SCPI 开关。

### 6.2 ⭐ `:STOP` 不冻结记录

一次 `:STOP` 之后连续 `SEND?`，payload **一直在变**：示波器继续以 ~2 Hz 采，`SEND?` 只交出最新那条。典型节奏：

```
#   t       耗时      字节     说明
0   0.00    322 ms    20000   等新记录
1   0.32      4 ms    20000   缓冲区里的下一条
2   0.33      3 ms    20000
3   0.34      4 ms    20000
4   0.47    129 ms        0   0 字节合法块（缓冲区空的）
5   0.78    320 ms    20000   又等了一条
...
```

**推论**：
- 一次 STOP 之后有一段**便宜的突发窗口**（约 5 次读 / 约 150 ms），每次只要 2–4 ms；
- 突发窗口用完后，下一条命令就要付 ~130 ms + ~320 ms；
- **这个代价落在当时正在飞的那条命令上**，不一定是 `SEND?`——实测一次夹在中间 `:WAVEFORM:VDIV?` 查询吃了 129.9 ms，一次 `refresh_state()` 吃了 451.8 ms。
- 所以 `latest()` **不保证**等于上一次 `read()`。要对齐就必须自己比对 payload。

### 6.3 ⭐ settle 时长（`:START` 后到 `:STOP` 的等待）

这是唯一一个能显著改变多通道结果的旋钮：

| settle | 单通道 | 四通道 | 四通道同源率 |
|---|---|---|---|
| 0.02 s | **478 ms** | 831 ms | **1 / 4** |
| 0.05 s | 479 ms | 832 ms | 1 / 4 |
| 0.10 s | 477 ms | 607 ms | 3 / 4 |
| **0.15 s** | 553 ms | **593 ms** | **4 / 4** |
| 0.30 s | 553 ms | 624 ms | 4 / 4 |
| 0.50 s | 923 ms | 960 ms | 2 / 4 |

→ 单通道用短 settle（0.02 s）最快；**多通道必须 settle ≥ 0.15 s**，否则不但慢 240 ms，而且四个通道根本不是同一次采集。代码里 `MULTI_SETTLE_S = 0.15`，`read_multi()` 会自动把 settle 抬到这个值（`read()` 不动）。

---

## 7. 速度瓶颈归属：示波器 vs 路由器？（明确回答）

**结论：瓶颈 100% 在示波器固件，路由器/网卡/网络完全不是限制。**

证据链：

1. **网络侧实测吞吐**：一次 RPC 取 250,012 B 只要 **3–11 ms** → 约 **20–76 MB/s**。千兆局域网的零头都不到。
2. **等记录是常数**：那 ~320 ms 对**窗口长度、记录长度、时基、holdoff、触发模式全部不敏感**（见 §6.1）。如果是网络瓶颈，数据量减 100 倍（125000 → 1250 点）耗时应该掉下去；实测 329 ms → 320 ms。
3. **通道数翻 4 倍，时间几乎不变**：单通道 478 ms → 四通道 593 ms，多搬 3 倍数据只多花 **115 ms**。数据搬运几乎不要钱，等记录才要钱。
4. **换实现只省掉"网络那部分"**：pyvisa-py 320–800 ms → 自研 3–11 ms，省的全是**协议往返开销**，那 ~320 ms 一分不少地留在那儿。

**如果以后想再确认一次**，最快的两条判据：
- 把记录长度调到最小（1250 点），看单次耗时是否下降 → 实测不降，说明不是传输量的问题；
- `--bench` 看 `rearm_s`（arm+stop）与 `transfer_s`（SEND?）的拆分。

**要更快怎么办**：示波器这条路已经到头了。真需要 < 30 ms 的回路，走 ATS9373 采集卡的片上 FFT（仓库里已有的那条路）。示波器定位为慢速参考。

---

## 8. 多通道采集（明确回答）

**可以同时采集多个通道，并且在正确配置下四个通道来自同一次采集、时间对齐。**

### 8.1 机制

`:WAVEFORM:TRACE` 只是**选择**哪条 trace 交给 `:WAVEFORM:SEND?`。一次 `:STOP` 之后，传输缓冲区里放着这次采集的各条 trace，所以切换 TRACE 再 `SEND?` 只要 **2–4 ms**。

### 8.2 ⚠️ 两个前提（缺一不可）

1. **settle 要够**：`:START` 后至少等 **0.15 s** 再 `:STOP`（§6.3）。settle 0.02 s 时四通道来自 4 次不同采集。
2. **扫描必须是一串紧凑的突发**：中间**不要插入任何别的 SCPI 命令**。缓冲区便宜窗口只有约 5 次读 / 约 150 ms；插进去的那条命令随时可能撞上"换页"，付掉 130 ms + 320 ms。
   → 代码里 `:CHANnel<n>:VDIV?` 的查询全部**提到扫描之前**做完（`read_multi()` 已如此实现）。

### 8.3 实测（修好之后，8 次连续）

```
read_multi(channels=(1,2,3,4))   x8
  588.5 / 589.4 / 589.6 / 590.2 / 590.5 / 591.4 / 595.6 ... ms
  min 588.5   median 590.0   max 595.6 ms   -> 1.69 Hz
  misaligned 0 / 8      empty blocks 0
```

对比单通道 478 ms：**四通道只贵 ~110 ms**，而且 8/8 对齐。

### 8.4 对齐性验证

`read_multi(..., verify=True)` 会在扫描结束后立刻重读第一条 trace，字节不一致就 `stats["misaligned"] += 1`。

⚠️ **verify 要花 ~330 ms**（第 5 次读落到便宜窗口之外），所以默认 `verify=False`。只在需要确认对齐的标定场合打开。

### 8.5 API

```python
with DlmScope("192.168.1.100") as scope:
    waves = scope.read_multi(channels=(1, 2, 3, 4), window=(0, 9999))
    # -> {1: Waveform, 2: Waveform, 3: Waveform, 4: Waveform}
    print(waves[1].summary()["pkpk_v"])

    waves = scope.read_multi(channels=(1, 2), verify=True)   # 顺带确认同源
    print(scope.stats["misaligned"])
```

`Streamer(channels=(1,2,3,4))` 同样走 `read_multi`，后台流式多通道（自动带上 0.15 s settle）。

---

## 9. 实时流式传输架构（给实时控制用）

### 9.1 问题

示波器只给 ~2 Hz 的新记录（四通道 ~1.7 Hz），但控制循环不能每次等半秒。

### 9.2 解法：`Streamer` 后台解耦

- 后台 daemon 线程按示波器自己的节奏跑 `START → STOP → SEND?`，拿到就发布；
- 控制循环调 `snapshot()`，返回 `(data, age_s)`，**实测 median 6 µs / max 165 µs**，永不阻塞；
- `age_s` 让调用方自己决定这份数据算不算过期（而不是被库替你决定）。

```python
from Nanomax.dlm_scope import DlmScope, Streamer

with DlmScope("192.168.1.100") as scope:
    with Streamer(scope, channels=(1, 2), window=(0, 9999)) as s:
        while running:
            data, age = s.snapshot()          # Waveform 或 {ch: Waveform}
            if age > 1.0:                     # 数据太老，这一拍别用
                continue
            amp = data[1].summary()["pkpk_v"]  # 或换成你要的任何判据
            z_step = pi.update(target - amp)
            stage.move(z_step)
```

`Streamer.stats()` 返回 `{"records", "age_s", "points", "errors", "rate_hz"}`，方便在 UI 上显示健康度。

### 9.3 实测（PAM-NEW 真机）

```
python -m Tool_code.dlm_scope_cli --follow 6
  -> records   12 in 6.0 s -> 2.00 Hz
     snapshot  median 6 us  max 165 us (the loop never waits on the scope)
     age       last sample 0.276 s old
```

---

## 10. 接入 AFM Z 控制（`ScopeSource`）

`ScopeSource` 把示波器包装成 `Nanomax.afm_control` 要求的 `value()` 接口：

```python
from Nanomax.dlm_scope import DlmScope, ScopeSource
src = ScopeSource(DlmScope("192.168.1.100"), stat="pkpk", unit="V")
```

- `stat`：`pkpk`（默认）/ `rms` / `mean` / `min` / `max`
- `unit`：`V`（经 `CODES_PER_DIV` 换算）/ `code`（原始码）
- 同时实现 `describe()` / `ready()` / `prepare()` / `finish()`

⚠️ **每次 `value()` 都是一次新记录 = ~0.45 s**，所以它是**慢速参考**，不是 30 ms 回路的传感器。用于标定跑、或作为采集卡之外的第二意见。

---

## 11. 网页示波器

`Tool_code/dlm_scope_web.py`：Python 标准库 `http.server.ThreadingHTTPServer`，自包含单页 HTML + Canvas，**零第三方依赖**。

启动（PAM-NEW 上）：

```bat
C:\Users\HP\miniconda3\envs\PAM\python.exe -m Tool_code.dlm_scope_web ^
    --host 192.168.1.100 --channels 1,2,3,4 --port 8791
```

浏览器打开 `http://100.79.154.4:8791/`（实验室任一机器都行，同一 tailnet）。

参数：`--host` / `--channels`（逗号分隔）/ `--window START END` / `--points`（默认 1500，等间隔抽样）/ `--port`（默认 8765）/ `--bind`（默认 0.0.0.0）。

页面行为：每通道一条 lane，10×6 格 graticule，通道配色 CH1 黄 / CH2 青 / CH3 品红 / CH4 绿；300 ms 轮询 `/data`（JSON）；有暂停按钮；`age > 1.5 s` 时显示「数据过期」。

已验证：页面 HTTP 200 / 4485 B；`/data` 返回 4 通道 JSON（约 30 kB，各 1500 pts，rate 1.98–2.12 Hz）。

> ⚠️ **一台示波器只跑一个客户端**。两个进程同时 START/STOP 会互相打断：实测并发时单次读取从 345 ms 涨到 772 ms，且 `latest()` 拿不到同一条记录。
> 端口注意：PAM-NEW 上 **8765 已被 `pythonw.exe` 占用**，默认改用 **8791**。
> 进程注意：远程用 `start /b` 起的进程会随 ssh 会话结束被杀，必须用常驻方式启动。

---

## 12. 文件清单与用法

| 文件 | 作用 |
|---|---|
| `Nanomax/dlm_scope.py` | 核心库：VXI-11 传输、`DlmScope`、`Waveform`、`Streamer`、`ScopeSource`、**`MeasureStreamer` / `MeasureSource`（标量通道）** |
| `Tool_code/dlm_scope_cli.py` | 命令行前端（**纯 ASCII 输出，GBK 控制台安全**） |
| `Tool_code/dlm_scope_web.py` | 网页示波器 |
| `Tool_code/test_dlm_scope_offline.py` | 离线自检（**84 项全过**），不需要连示波器 |

CLI 用法（在仓库根目录）：

```bat
python -m Tool_code.dlm_scope_cli --check                  :: 身份 + 状态 + 一次读取 + latest + 错误队列
python -m Tool_code.dlm_scope_cli --bench 10               :: 测 10 条新记录，报 median / distinct / rate
python -m Tool_code.dlm_scope_cli --stream 5               :: 打印 5 条新记录
python -m Tool_code.dlm_scope_cli --follow 8               :: 后台流式 8 秒，报 records / 延迟 / age
python -m Tool_code.dlm_scope_cli --save rec.csv --window 0 9999
python -m Tool_code.dlm_scope_cli --source pkpk            :: 以 AFM 源的方式取标量
:: 标量通道（快 1.1-4.2 倍，不用 STOP）：
python -m Tool_code.dlm_scope_cli --measure RMS
python -m Tool_code.dlm_scope_cli --measure RMS,MAX --channels 1,2 --tdiv 20E-6 --rlen 1250 --follow 10
```

`--host` 默认 `192.168.1.100`，`--channel` 默认 1。

---

## 13. 真机验收记录（PAM-NEW，2026-10-18）

| 项目 | 结果 |
|---|---|
| 离线自检 | **84 / 84 passed**（2026-10-18 追加标量通道 18 项后） |
| `--check` | 10000 pts / 345.6 ms（独占时）；`latest()` 3–4 ms |
| `--bench 6` | median **454.2 ms**，distinct **6/6**（每条都是新记录），rate **2.20 Hz** |
| `--follow 6` | 12 records / 6.0 s = **2.00 Hz**；snapshot median 6 µs / max 165 µs |
| `read_multi(1,2,3,4)` ×8 | median **590 ms** → **1.69 Hz**，**misaligned 0/8**，empty 0 |
| `read_multi(..., verify=True)` ×8 | median 919 ms（verify 多花 ~330 ms），misaligned 0/8 |
| 多通道同源（手写突发，10 次） | **10 / 10 字节一致** |
| 网页 | `http://100.79.154.4:8791/` → 200 / 4485 B；`/data` 4 通道约 30 kB，rate 1.98–2.12 Hz |
| **`--measure RMS --follow 6`**（TDIV 2 ms / 125 kpts） | **5.72 Hz**，median gap 206.6 ms，941 次轮询/s，age median 1 ms |
| **`--measure RMS --follow 6`**（TDIV 20 µs / 1 250 pts） | **9.34 Hz**，median gap 102.5 ms，1276 次轮询/s |

---

## 14. ⚠️ 未标定项与下一步

### 14.1 码 → 伏换算系数未标定

payload 是**未加 CH offset / VDIV 的原始 ADC 码**。验证：改 `:CHANnel1:OFFSet`（0.05 → 0.07 → 0.25 V）和 `:CHANnel1:VDIV`（0.025 / 0.05 / 0.1）时，min/max/span **完全不变**（span 恒 32767）。

- `:WAVEFORM:VDIV?` 在这台固件上**不是合法查询**；
- SCPI 命令手册（IM DLM3054-17EN）本地没有且官方 CDN 403；
- 所以系数**读不出来**，只能现场标定。

代码里 `CODES_PER_DIV = 8192.0`（Yokogawa 16-bit 惯例：8 格跨满量程）。**需要在台面上核对一次**：给一个已知幅度的信号，比对屏幕读数和 `Waveform.summary()["pkpk_v"]`。

> 注意：`Waveform.codes` **永远是精确的原始码**；伏特数只是一个线性缩放。控制回路只需要单调的代理量，直接用 codes 即可，不必等标定。

### 14.2 建议的下一步

1. **标定 `CODES_PER_DIV`**（一次即可，写回 `dlm_scope.py` 常量）。
2. **把示波器接进 AFM 回路当慢速参考**：`Streamer` + `ScopeSource`，与采集卡片上 FFT 并行。只要标量的话改用 `MeasureStreamer` / `MeasureSource`（**2–9.4 Hz**，见 §15.3）。
3. **有信号时重测触发路径**：接上 10 MHz 超声后，把 `:TRIGger:MODE` 切 NORM 对比 `--measure` 更新率——理论上限会从"100 ms 超时"变成"触发率"（§15.2，目前无信号无法验证）。
4. 如果要更快：只能换数据源（采集卡），示波器波形通道已到 ~2.2 Hz / 四通道 ~1.7 Hz 的天花板。
5. 可以尝试用 FTP（端口 21 也开）批量导出记录文件——适合离线存盘，不适合实时。
6. 可考虑用 `:MATH1`（FFT，`HCUT/LCUT` 已见 10 MHz 字样）让示波器直接做频谱，再读其测量值——对"10 MHz 分量幅值"比时域 RMS 更贴题，尚未验证。

---

## 15. 追问：「能不能更小一段、更实时？」（2026-10-18 追加）

> 提问原话：*"感觉数据是一大段一大段传输过来的，可是我需要的是实时拿到当前的波形呀，能不能更小一段传输……你再看看说明书"*
>
> **结论先说**：把窗口切小**不能**提高实时性，因为 0.45 s 是"示波器产出一条新记录"的节拍，不是"搬字节"的节拍。但顺着这个问题在说明书和命令树里翻，找到了一条**真的更快**的路：**让示波器自己算标量**（`MEASure` 引擎），~0.4 ms 一次查询、**不需要 `:STOP`**、更新率 **2–9.4 Hz**（视记录长度），是波形通道的 **1.1–4.2 倍**，且 IO 成本降低约三个数量级。若控制回路只需要"当前幅值"这一个数，就该走这条。

### 15.1 直接回答：窗口切到 99 点，换记录速率一点没变

两个对照实验（每次 6 s，同一台示波器）：

**A) 一次 `:STOP` 后死轮询，不再重新 arm**

| 窗口点数 | 读次数 | unique payload | 单次读耗时 |
|---|---|---|---|
| 99 | 3863 | **1** | 1.3 ms |
| 999 | 3919 | **1** | 1.4 ms |
| 9999 | 2673 | **1** | 2.0 ms |
| 124999 | 460 | **1** | 9.9 ms |

6 秒里读了几千次，**交出来的始终是同一条记录**（0.17–0.20 条/s）。轮询很快，但快的是"重复要同一份东西"。

**B) 每次重新 arm（即 `read()` 的做法）**

| 窗口点数 | 读次数 | unique | 换记录速率 |
|---|---|---|---|
| 99 | 14 | 14 | **2.22 Hz** |
| 999 | 14 | 14 | **2.22 Hz** |
| 9999 | 14 | 14 | **2.21 Hz** |

窗口从 9999 点缩到 99 点（数据量减 100 倍），**换记录速率完全不变**。数据变少了，节拍没变。

**为什么？** 再做一个反证：一次 `:STOP` 之后**交替**取两个不同窗口（`0-99` 与 `1-100`），每轮只要 **1.7 ms**（599 轮/s），窗口位置换到 `50000-59999` 也一样（2.3 ms，438 轮/s）。这说明**示波器完全能廉价地切任意片段**——它慢的不是"切片"，而是**产生下一条记录**。所以：

> 0.45 s = 示波器准备一条新记录的时间。要更实时，只能减少"需要新记录的次数"，或者干脆不搬波形。

### 15.2 说明书里唯一相关的一条：AUTO 超时 = max(100 ms, 10 div)

说明书（`DLM3000用户手册` 2.1 节）原文：

- **AUTO 模式**：*"如果在超时时间内满足触发条件，仪器将在每次触发时更新显示波形。否则……自动更新显示的波形"*
- **超时时间 = max(100 ms, 时间轴 10 div 对应的时间)**
- **NORM 模式**：*"仪器只在触发条件成立时更新显示波形"*

这条规则**实测成立**（用标量通道测，见 15.3，因为它比波形通道干净得多）：

| TDIV | 10 div | 说明书预测 | 实测 |
|---|---|---|---|
| 50 ms/div | 500 ms | 2.00 Hz | **1.98 Hz（511 ms）** ✅ |
| 20 µs/div | 0.2 ms → 取 100 ms | 10 Hz | **9.36 Hz（102 ms）** ✅（100 ms 地板） |

→ 那个"~100 ms 地板"就是说明书里的 **100 ms 最小超时**，不是玄学。

**但触发这条路现在验证不了**，原因很具体：
- 触发源固定是 **CHAN4**（`:TRIGger:SOURce?` 返回四通道复合串，CHAN4 `LEV 340 mV`）；
- **`:TRIGger:SOURce` 不可远程设置**（`113,"Undefined header"`），`:TRIGger:HOLDoff` 也只能查不能设；
- **四个通道当前都没有任何真实信号**（只有本底噪声）。切 NORM + 把电平放到 CHAN4 均值后，8 次读**全部 0 点**——没有触发，NORM 就永不更新。

> ⚠️ **待现场验证**：一旦 10 MHz 超声真的接上，NORM 模式下 AUTO 超时不再生效，更新率会变成**触发率**，天花板应该明显高于 10 Hz。这一条必须在有信号时重测（`--measure ... --follow`，同时把 `:TRIGger:MODE` 切 NORM 对比）。

另外 TDIV 500 ms/div 时实测反而 **5.14 Hz**（波形通道降到 1.87 Hz），与"10 div = 5 s → 0.2 Hz"不符——这个时基应已进入**滚动模式**（说明书：滚动模式下不显示 MATH 波形、不能用历史功能），更新行为另算。慢时基不在实时控制范围内，未深究。

### 15.3 ⭐ 真正更快的路：让示波器自己算，只读一个数

示波器内置**自动测量引擎**，本来就每帧在算 RMS / MAX / MIN / AMPL / FREQ。之前一直读不到，是因为踩了两个坑：

1. **`:MEASure:MODE` 出厂是 `OFF`**。此时所有 `:MEASure:...:VALue?` 都回 **`NAN`**，看着像命令不存在。
2. **`:MEASure:CHANnel<n>:<item>:STATe 1` 必须在时基设置之后**。先开测量再改 `TDIV` / `RLENgth`，值会全部退回 `NAN`（实测）。

打开之后：

```
:MEASure:MODE ON
:MEASure:CHANnel1:RMS:STATe 1
:MEASure:CHANnel1:RMS:VALue?        ->  :MEAS:CHAN1:RMS:VAL 803.261E-06
```

**成本**：一次查询 **0.41 ms**（实测 2415 次/s），而且**示波器在运行（RUN）状态下就能读，不需要 `:STOP`**。对比波形通道一次 ~450 ms、还必须先停。

**更新率（决定性对照表，同一次实验、同一台机）**：

| TDIV | RLEN | SRATE | 标量更新率 | 中位间隔 | 同设置下波形速率 |
|---|---|---|---|---|---|
| 2 ms | 125 000 | 6.25 MS/s | 5.92 Hz | 196 ms | 2.24 Hz |
| 200 µs | 12 500 | 6.25 MS/s | 7.31 Hz | 116 ms | 2.26 Hz |
| **20 µs** | **1 250** | 6.25 MS/s | **9.36 Hz** | **102 ms** | 2.26 Hz |
| 2 µs | 125 | 62.5 MS/s | 8.82 Hz | 104 ms | 2.25 Hz |
| 50 ms | 125 000 | 250 kS/s | 1.98 Hz | 511 ms | 2.26 Hz |
| 500 ms | 125 000 | 25 kS/s | 5.14 Hz | 212 ms | 1.87 Hz |

两个要点：

- **记录越短，标量越快**（2 ms → 20 µs：5.9 → 9.4 Hz），**而波形通道一动不动（2.24 → 2.26 Hz）**。这是"标量通道"和"波形通道"最本质的区别：波形通道的 0.45 s 与记录长度无关，标量通道的节拍里**有**记录长度那一项。
- **地板是 ~102 ms**（≈说明书那个 100 ms 最小超时）。

**CLI 实测**（真机，`--measure`）：

```
$ python -m Tool_code.dlm_scope_cli --measure RMS --channels 1 --tdiv 20E-6 --rlen 1250 --follow 6
values    44 changes in 4.7 s -> 9.34 Hz (median gap 102.5 ms)
polls     7660 -> 1276 queries/s (each ~0.4 ms, none of them blocks)
age       median 0.000 s
staleness worst case = one refresh gap = 133 ms, not the poll rate
```

**可用的测量项**（`MEAS_ITEMS`）：`RMS` `MAX` `MIN` `AMPL` `SDEV` `FREQ` `PER`。
⚠️ **`PKPK` 和 `MEAN` 不存在**——这两个是最容易先猜的，实测都是 `113,"Undefined header"`；要峰峰值用 `MAX - MIN` 或 `AMPL`。

**延迟语义**：`age_s` 是"距上次成功轮询"，只能证明链路活着；**控制决策的最坏陈旧度 = 一个刷新间隔（102–208 ms）**，不是轮询周期。这一点在 `MeasureStreamer.snapshot()` 的注释里写死了。

### 15.4 选型：什么需求走哪条路

| 你需要 | 用什么 | 速率 | 代价 |
|---|---|---|---|
| **一个标量**（幅值/有效值/峰值）做控制判据 | `MeasureStreamer` / `MeasureSource` | **2–9.4 Hz**（记录越短越快） | 0.4 ms/次，不阻塞，不用 STOP |
| **整条波形**（看形状、算 FFT、存盘） | `Streamer` / `DlmScope.read()` | **~2.2 Hz**（四通道 ~1.7 Hz） | ~450 ms/帧，必须 STOP |
| 快于 0.1 s 的闭环 | **换数据源**：ATS9373 采集卡片上 FFT | 30 ms 级 | 示波器这条路已到物理天花板 |

**一句话**：要"实时"，就不要搬波形——让示波器把波形算成一个数，你只取那个数。10 MHz 超声的幅值判据正好只要一个数。

### 15.5 用法

```python
from Nanomax.dlm_scope import DlmScope, MeasureStreamer, MeasureSource

# 想更快就先缩短记录：20 us/div + 1250 点 -> ~9.4 Hz
with DlmScope("192.168.1.100") as scope:
    scope.write(":TIMebase:TDIV 20E-6")
    scope.write(":ACQuire:RLENgth 1250")
    import time; time.sleep(0.5)          # 时基先落定，再开测量

    # 后台线程按示波器节奏轮询，控制循环拿 snapshot() 只需微秒
    with MeasureStreamer(scope, channels=(1,), items=("RMS",)) as m:
        while running:
            values, age = m.snapshot()     # {1: {"RMS": 8.0e-04}}, 0.001
            amp = values.get(1, {}).get("RMS")
            ...                            # 决策
```

接 AFM Z 回路（和 `ScopeSource` 同一个契约，可直接替换）：

```python
src = MeasureSource(scope, item="RMS", channel=1)
value, weight = src.value()        # ~0.4 ms，而不是 ~450 ms
```

命令行：

```bash
python -m Tool_code.dlm_scope_cli --measure RMS                  # 读一次
python -m Tool_code.dlm_scope_cli --measure RMS,MAX --channels 1,2 \
       --tdiv 20E-6 --rlen 1250 --follow 10                      # 流 10 秒
```

---

## 16. 附录：踩坑清单（按被坑概率排序）

1. **`device_write` 不带 `OP_FLAG_END` (0x08)** → 命令不执行，下一次 read 报 error 15 挂满超时。最隐蔽。
2. **`device_write` 参数写成 5 个 int** → 返回 size 0。正确是 4 个 int + opaque（`>iIII`）。
3. **采集中读 `:WAVEFORM:SEND?`** → 返回合法的 0 字节块 `#9000000000`，容易被当成"没配好"。
4. **以为 `:STOP` 冻结了记录** → 它没冻。连续读一直在变，`latest()` ≠ 上一次 `read()`。
5. **多通道扫描 settle 太短** → 四通道来自不同次采集（0.02 s 时 1/4 对齐），而且更慢。用 ≥ 0.15 s。
6. **多通道扫描中间插了别的 SCPI 命令** → 那条命令撞上缓冲区换页，吃掉 130 ms + 320 ms。查询全部提前做。
7. **两台客户端同时连一台示波器** → 互相打断 START/STOP，单次读取从 345 ms 涨到 772 ms。
8. **`:WAVEFORM:TRACE` 当成数据查询** → 它是源选择器；`?` 会报 `141 Invalid character data`。
9. **`:SINGle` 不存在** → 报 `113,"Undefined header;:SINGLE"`。只有 `:START`→`:STOP` 才产生新记录。
10. **`:SYSTem:ERRor?` 不存在** → 正确是 `:STATus:ERRor?`；用错表现为读超时。
11. **错误队列会一直累积** → 调试时会看到几百条历史残留，定期 drain。
12. **`requestSize` 太小** → DLM 截断到 requestSize 还置 END 位（谎报），之后再 read 报 error 15。
13. **record mark 多帧** → 大回复必须按 4 字节 record mark 重组。
14. **`pack_string` 传 bytes** → 报 `'bytes' object has no attribute 'encode'`；要传 str。
15. **两台同名 PC** → 只能按 tailnet IP 认身份（见 §1.1）。
16. **临时脚本放错目录** → 脚本所在目录会进 `sys.path[0]`。若该目录下有旧版 `Nanomax/` 包，会静默导入旧代码，测出来的数字全是旧的。探针脚本请放到仓库根目录跑。
17. **远程 `start /b`** → 进程随 ssh 会话死，必须常驻启动。
18. **PAM-NEW 端口 8765 被占** → 换 8791。
19. **GBK 远程控制台** → 所有程序输出必须纯 ASCII（`dlm_scope_cli.py` 已按此约束写）。
20. **以为 `:MEASure:...:VALue?` 不存在** → 它存在，只是 `:MEASure:MODE` 出厂 `OFF`，所以回 `NAN`。先 `:MEASure:MODE ON`。
21. **先开测量、后改时基** → 值全部退回 `NAN`。顺序必须是：`TDIV` → `RLENgth` → 等一下 → `measure_on()`。
22. **查 `:MEASure?` 时先 write 再读错误队列** → 会得到 `410,"Query INTERRUPTED"`。**410 不是"不存在"**，恰恰是"这个头存在、但你没把响应读走"；只有 `113,"Undefined header"` 才是真的不存在。我一开始据此误判了 6 个存在的头。
23. **猜 `PKPK` / `MEAN`** → 这两个测量项不存在（`113`）。可用项是 `RMS MAX MIN AMPL SDEV FREQ PER`。
24. **把"轮询很快"当成"数据很新"** → 一次 STOP 后紧轮询 6 秒能读 3863 次，但只有 **1** 条 unique payload。速度不等于新鲜度，必须数 unique。
25. **改时基后没重跑自检** → 本地 `repo/` 是残缺副本（缺 `Nanomax/afm_control.py`），离线自检必须在 PAM-NEW 上跑，否则 `[8]` 段会 ImportError 而看不出原因。
