# AD9910 统一控制服务

用 Python 完全替代 LabVIEW（`AD9910.vi`）的 AD9910 控制服务：同一台电脑上的多块
AD9910 评估板，通过**网页**与 **RPyC** 两个入口统一控制。

```
Browser --HTTP/WS--> Tornado Web ----> ControlHub ----> FIFO 队列
RPyC client -------> RPyC 服务线程 ---> ControlHub ----> FIFO 队列
                                                      |
                                                      v
                                             唯一硬件 worker 线程
                                                      |
                                                      v
                                          DeviceManager -> AD9910 硬件
```

核心原则：

- 浏览器和 RPyC **不直接操作硬件**；所有写操作进入同一个有界 FIFO（容量 256）。
- 只有一个 worker 线程持有并访问设备连接。
- 查询直接读取线程安全的状态快照（`applied state`，标注 `host_applied`，
  不是硬件回读）。
- 每个成功命令只提交一次状态并递增全局 `revision`；参数校验失败、硬件失败
  不产生虚假状态。
- 页面编辑使用基准 `baseRevision` 检测冲突，可确认后强制覆盖。

## 快速启动

```bash
# 真实硬件
python -m devices.generators.adiclockevals.control_service.app \
    --config-dir devices/generators/adiclockevals/config

# 无硬件演示（内存模拟设备 0443/0422）
python -m devices.generators.adiclockevals.control_service.app \
    --config-dir temp/cfg --device-manager fake
```

打开 <http://127.0.0.1:8080/> 即可使用网页控制台。

### 命令行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--config-dir` | （必填） | 配置目录，只允许读取其直属 `.json` |
| `--web-host` / `--web-port` | `127.0.0.1` / `8080` | Web 服务监听 |
| `--rpyc-host` / `--rpyc-port` | `0.0.0.0` / `3251` | RPyC 服务监听（沿用旧端口） |
| `--config` | 无 | 启动时自动加载的配置名 |
| `--refclk` | `1000` | 外部参考时钟 REF_CLK（MHz），对应官方软件 External Clock |
| `--divider-disable` | 不启用 | 旁路 REFCLK ÷2 分频器（对应官方软件勾选 Divider Disable）；不勾时 SYSCLK = REF_CLK/2，见下文 |
| `--exit-behavior` | `keep` | `keep` 退出保持输出；`clear` 退出前清输出 |
| `--fault-action` | `keep` | `keep` 故障只标记 fault；`clear` 故障后尝试清输出 |
| `--device-manager` | `real` | `real` 真实硬件；`fake` 内存模拟（演示/测试） |
| `--fake-devices` | `0443,0422` | fake 模式下模拟的设备 id |

## HTTP API

| 接口 | 说明 |
|---|---|
| `GET /api/state` | 状态快照（status/revision/services/devices/last_error） |
| `GET /api/configs` | 现有配置名列表 |
| `POST /api/config/select` | `{"name": "..."}` 加载配置 |
| `POST /api/configs` | `{"name": "..."}` 保存**当前 applied state** 为配置；`?overwrite=true` 或 body `overwrite:true` 二次确认覆盖 |
| `POST /api/devices/{id}/parameters:batch` | `{"baseRevision": n, "parameters": {...}}` 批量应用参数；`force:true` 忽略冲突 |
| `POST /api/devices/{id}/outputs:clear` | 清除输出（所有 profile 幅度置 0，频率/相位保留） |
| `WS /ws` | 状态变更实时推送 |

错误码：`400` 参数/格式错误；`409` 版本冲突、故障状态、文件已存在；
`503` 命令队列满、服务未就绪；`500` 内部错误。

### 参数结构（`parameters`）

```json
{
  "name": "780_Pushout",
  "active_profile": 1,
  "enable_profile_asf": true,
  "enable_sine_out": false,
  "profiles": [
    {"freq": 218.0, "phase": 0.0, "amp": 0.2},
    null, null, null, null, null, null, null
  ]
}
```

- `freq`：MHz；`phase`：π 单位；`amp`：满量程分数（0~1）。
- `profiles` 恒为长度 8 的数组，`null` 表示该 profile 不修改；
  提交只发送真正编辑的字段（服务端与编辑过的 profile 合并）。

## RPyC 接口（:3251）

兼容既有客户端（方法名与返回结构不变）：

```python
import rpyc
conn = rpyc.connect("192.168.1.20", 3251, config={
    "allow_public_attrs": True, "allow_pickle": True, "allow_all_attrs": True})
svc = conn.root.get_ad9910_service()
svc.set_frequency(0x0443, 1, 218.0)    # -> True/False
svc.set_amplitude(0x0443, 1, 0.2)      # -> True/False
svc.adjust_amplitude(0x0443, 1, 0.05)  # -> True/False（clamp 到 [0, sqrt(0.2)]）
svc.set_phase(0x0443, 2, 0.25)         # -> True/False
svc.close()                            # -> True
```

新增接口（同一服务对象）：

```python
svc.get_state_snapshot()                       # 状态快照 dict
svc.get_device_info("0443")                   # 设备参数或 None
svc.update_parameters("0443", {...})          # 提交并同步等待（异常向客户端传播）
svc.update_parameters_batch({"0443": {...}})  # {device_id: result}
svc.load_config({...})                        # 加载配置（payload 为配置 JSON）
svc.clear_outputs("0443")                     # 清除输出
```

连接/断开打印带时区日志：

```
[2026-08-05T10:20:31+08:00] RPyC connected peer=192.168.1.20:53124
[2026-08-05T10:25:17+08:00] RPyC disconnected peer=192.168.1.20:53124
```

> 旧 TCP 字符串协议（:50007/:50107）与 sipyco（:3250）已停用，请迁移到本服务。

> **安全说明**：为兼容旧客户端，RPyC 沿用了 `allow_pickle`/`allow_all_attrs`
> 配置，且默认监听 `0.0.0.0:3251`。同一局域网内的任意客户端可发起 RPC（含
> pickle 反序列化）。请仅在可信网络使用，或通过防火墙限制 3251 端口访问；
> 如需仅本机使用，可加 `--rpyc-host 127.0.0.1`。

## 配置格式（JSON，`--config-dir` 直属）

```json
{
  "format_version": 1,
  "name": "my-config",
  "device_info": {
    "0443": {
      "name": "780_Pushout",
      "active_profile": 1,
      "enable_profile_asf": true,
      "enable_sine_out": false,
      "profiles": [{"freq": 0.0, "phase": 0.0, "amp": 0.0}, null, null, null, null, null, null, null]
    }
  }
}
```

- `device_id` 为小写 hex（USB 序列号）。
- 只允许访问配置目录直属 `.json`；禁止绝对路径、`../`、符号链接。
- 保存采用临时文件 + 原子替换；已存在默认 `409`，页面二次确认后覆盖。
- 保存的是 **applied state**（不含页面暂存值），且不改变设备状态与 `revision`。
- `fault` / `applying` / 状态不可信时拒绝保存。

### 旧 ini 导入

```python
from devices.generators.adiclockevals.control_service.import_ini import import_ini_file
from devices.generators.adiclockevals.control_service.config_store import ConfigStore

store = ConfigStore("config")
cfg = import_ini_file("config/2024-04-19-193447_AD9910_Monitor_10Gs.ini")
store.save(cfg, overwrite=False)
```

旧 ini 中幅度为功率 mW（= 幅度分数²），导入时自动 `sqrt` 转回分数；频率（MHz）
与相位（π）原样保留。

## 前端

原生 HTML/CSS/JavaScript，无构建工具。功能：

- 服务状态 / revision / Web/RPyC 地址 / 当前配置
- 设备（板卡）切换、参数表格（8 profile × 频率/相位/幅度）、搜索过滤
- 本地暂存编辑：数值显示最多 **6 位有效数字**，内部与提交保留完整浮点精度，
  只提交用户真正编辑的字段
- 基准 revision 冲突检测：编辑期间状态变更 → 标记冲突 → 确认后强制覆盖
- 批量应用 / 放弃暂存 / 清除输出
- 配置加载、保存（二次确认覆盖）

## 时钟：REF_CLK 与 SYSCLK（重要）

> 完整复盘与排查过程见 [docs/ad9910-clock-refclk-sysclk.md](docs/ad9910-clock-refclk-sysclk.md)。

外部参考时钟和芯片内部时钟是**两个不同的量**，软件不再用单一 "SYSCLK" 参数，
而是把两者分开配置，SYSCLK 由它们推导——与 ADI 官方评估软件的三项设置一一对应：

| 官方评估软件 | 本服务参数 | 本实验室设置 |
|---|---|---|
| **External Clock** | `--refclk 1000` | 1000 MHz（J1 SMA 外部参考） |
| **Divider Disable**（勾选 = 旁路 ÷2） | `--divider-disable` | **不勾选**（÷2 使能） |
| **System Clock** | 自动推导 | **500 MHz** |

```
SYSCLK = REF_CLK / (2 if REFCLK ÷2 使能 else 1)   # REFCLK PLL 关闭时
ftw    = round(freq / SYSCLK * 2**32)            # 所有频率都按 SYSCLK 编码
```

`reset()` 按该配置写 CFR3 并给出 IO_UPDATE 上升沿确保真正锁存：

- ÷2 使能（默认）→ `CFR3 = 0x1F3F4000`，SYSCLK = REF_CLK / 2 = 500 MHz
- `--divider-disable` → `CFR3 = 0x1F3FC000`，SYSCLK = REF_CLK = 1000 MHz

> **服务启动时会逐板重写 CFR3。** 寄存器镜像 `.npy` 已存在时 `AD9910.__init__`
> 会跳过 `reset()`（否则每次启动都要重下载固件），因此
> `AD9910DeviceManager.start()` 在枚举后对每块板调用 `apply_clock()`，控制台打印
> `设备 4008 时钟寄存器已重写: CFR3=0x1f3f4000 ...`。这样即使某块板的芯片被官方
> 软件或旧版本配成"旁路 ÷2"（会停振、无输出），重启服务即自动纠正。

> 本机实测：勾选 Divider Disable（1 GHz 直入）会让 DDS **完全停止输出**，所以
> 默认不旁路 ÷2。

页面右上角显示 `REF_CLK 1000 MHz → SYSCLK 500 MHz`（悬停可看 ÷2 状态）。若显示
`—`，说明是旧进程或浏览器缓存了旧脚本：重启服务并 `Ctrl+F5` 强刷。

不经本服务的其它入口可用环境变量：`AD9910_REFCLK=1000`、
`AD9910_DIVIDER_DISABLE=1`（勾选 Divider Disable 时才用）。

验证方法：网页设 100 MHz，频谱仪应读到 100 MHz。若读到 50 MHz，说明软件使用的
SYSCLK 是实际的 2 倍；若读到 200 MHz 则相反——前者把 `--refclk` 减半或勾
`--divider-disable`，后者改回默认。

## 生命周期与安全行为

- 服务退出（Ctrl+C / SIGTERM）：停收新命令 → 等待当前命令结束 → 关闭设备；
  **默认不复位硬件、保持当前输出**（`--exit-behavior clear` 可改为先清输出）。
- 严重硬件故障：进入 `fault`，标记 `state_valid=false` 并记录 `last_error`；
  **默认不动输出**，故障期间拒绝写命令、允许查询（`--fault-action clear` 可改为
  故障后尝试清输出）。
- 关闭浏览器不会关闭设备输出。

## 测试

```bash
python -m pytest devices/generators/adiclockevals/control_service/tests/ -q
node --check devices/generators/adiclockevals/control_service/static/app.js
```

测试全部使用 `FakeDeviceManager`，无需真实硬件与 ADI DLL。
覆盖：适配层/参数校验、ControlHub（FIFO、唯一 worker、revision、状态机、busy/fault）、
RPyC（旧接口兼容/新接口/日志）、Web（HTTP/WS/错误码/静态资源）、
配置管理（路径安全/原子写/覆盖/ini 导入）、端到端跨接口流程。

## 目录结构

```
control_service/
  app.py            # 主入口（命令行参数、双服务启动、优雅退出）
  hub.py            # ControlHub：FIFO + 唯一 worker + 状态快照 + revision + 状态机
  device_manager.py # DeviceManager（真实 AD9910 / Fake）与参数校验
  state.py          # applied state 快照（线程安全深拷贝）
  commands.py       # 命令模型
  config_store.py   # JSON 配置管理（schema/路径安全/原子写）
  import_ini.py     # 旧 ini -> JSON 一次性导入
  rpyc_service.py   # RPyC 服务（兼容旧方法 + 新方法 + 连接日志）
  web.py            # Tornado HTTP API + WebSocket + 静态文件
  static/           # 前端（index.html / style.css / app.js）
  tests/            # pytest 测试套件
  docs/             # 问题复盘与设计决策（见 docs/README.md）
```
