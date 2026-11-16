# AD9910 输出频率减半 / 停振问题：REF_CLK 与 SYSCLK 分离改造

- 状态：已修复并上机验证（输出恢复，设定频率与实测一致）
- 最后更新：2026-11-10
- 影响范围：`devices/generators/adiclockevals/ad9910.py`、`ad9910_wrapper.py`、
  `control_service/`（app / device_manager / 前端 / start_web.ps1）
- 关键词：AD9910、CFR3、REF_CLK、REFCLK ÷2 输入分频器、SYSCLK、FTW、IO_UPDATE、半频、停振

## 1. 现象

1. 用 `start_web.cmd` 启动控制服务后，网页设定 110 MHz，频谱仪实测只有 **55 MHz**
   （正好一半）；同一块板用 ADI 官方评估软件输出正常。
2. 修复过程中一度出现**完全无输出**。
3. 最终修复后输出正常，设定值与实测值一致。

官方软件的正常工作设置为（用户确认，是本问题的判定基准）：

| 官方软件项 | 设置 |
|---|---|
| External Clock | **1000 MHz** |
| System Clock | **500 MHz**（软件自动算出并显示） |
| Divider Disable | **不勾选** |

## 2. 根因

### 2.1 AD9910 的时钟链

```
REF_CLK(J1 SMA) ──> REFCLK ÷2 输入分频器 ──> [PLL 或 旁路] ──> SYSCLK ──> DDS 内核
```

- `DDS 输出频率 = FTW × SYSCLK / 2^32`，所以 SYSCLK 是频率编码的唯一基准。
- REFCLK ÷2 分频器**默认使能**（`CFR3[15]=0`），`CFR3[15]=1` 表示旁路。
- 本机：REF_CLK 1000 MHz、÷2 使能、PLL 关闭 → **SYSCLK = 500 MHz**。

相关寄存器位（AD9910 datasheet Table 20，CFR3 @ 0x02）：

| 位 | 含义 | 本机取值 |
|---|---|---|
| `[26:24]` VCO SEL | `110`/`111` = PLL 旁路 | `111` |
| `[21:19]` ICP | 电荷泵电流 | `111` |
| `[15]` | REFCLK ÷2 输入分频器**旁路**位（0=分频使能，1=旁路） | **`0`（使能）** |
| `[14]` | 分频器 ResetB（1=正常工作） | `1` |
| `[8]` | PLL 使能（0=旁路） | `0` |
| `[7:1]` | PLL 反馈分频 N（12~127，PLL 关闭时无效） | `0000000` |

由上面组合出的两个常量：

| CFR3 | 含义 | 推导出的 SYSCLK |
|---|---|---|
| `0x1F3F4000` | PLL 关闭 + **÷2 使能** | REF_CLK / 2 = **500 MHz** ✅ 本机正确值 |
| `0x1F3FC000` | PLL 关闭 + **÷2 旁路**（1 GHz 直入） | REF_CLK = 1000 MHz ❌ 本机实测**停振** |

两者只差 `0x8000`（bit15）。

### 2.2 缺陷一：软件把 SYSCLK 当成独立常量（→ 半频）

旧代码在 `AD9910.__init__` 里硬编码 `self.__f_sysclk = 1000.`，与「外部参考时钟 1000 MHz」
混为一谈。芯片实际 SYSCLK 是 500 MHz，于是：

```
FTW = round(110 / 1000 × 2^32) = 0x1C28F5C3
实际输出 = FTW × 500 MHz / 2^32 = 55 MHz        ← 正好一半
正确编码 = round(110 / 500 × 2^32) = 0x3851EB85
```

页面回读也用同一个错误基准解码，所以**页面上永远「自洽」**，看不出异常。

### 2.3 缺陷二：CFR3 写入是否生效取决于端口电平时序（→ 配置不可复现）

`reset()` 原本这样结尾：

```python
self.spi_write(0, 0x00000000)   # CFR1
self.spi_write(1, 0x01400820)   # CFR2
self.spi_write(2, 0x1F3FC000)   # CFR3（旁路 ÷2）
self.set_port_value(4, 0x90)    # IO_UPDATE: ?
self.set_port_value(4, 0x80)    # 只有下降沿
```

AD9910 的 **IO_UPDATE 是上升沿触发**。这段代码能否把 CFR1/2/3 真正锁存，取决于进入时
port E 的 bit4 电平：若上电/EEPROM 默认已是高电平，则 `0x90 → 0x80` 只有下降沿，
三条 CFR 都不会生效，芯片停留在复位默认值（÷2 使能 → 500 MHz）。

这正好解释了历史上的观测组合：

- CFR2 的 profile-ASF 位没出问题：配置加载会走 `set_profile_asf()`，那条路径自带
  `0x10 ^ value → value` 的 toggle，**有**上升沿。
- CFR3 只在 `reset()` 里写一次（项目内 `cfr1/cfr2/cfr3` 的 setter 都没有被调用），
  **没有**其它路径 → 一直没生效 → 芯片停在 ÷2（500 MHz）
  → 软件却按 1000 MHz 编码 → **输出半频**。
- 官方软件自己做完整的 IO_UPDATE，所以它一直是准的。

### 2.4 修复过程中的插曲：为什么「一度完全没输出」

第一版修复把 IO_UPDATE 改成强制 `0 → 1 → 0` 脉冲，同时保留 `CFR3 = 0x1F3FC000`
（旁路 ÷2）。于是 CFR3 第一次真正落到芯片：1 GHz 直接进 DDS 内核，**本机时钟链路
不支持**，芯片失去 SYSCLK → 输出彻底消失。

现场留下的证据（`devices/generators/adiclockevals/config/AD9910_*.npy` 是所有板的寄存器
镜像，`spi_write` 会同步记录）：

```
AD9910_0003.npy CFR3=0x1f3fc000
AD9910_0443.npy CFR3=0x1f3fc000
AD9910_0666.npy CFR3=0x1f3fc000
AD9910_0723.npy CFR3=0x1f3fc000
AD9910_0A21.npy CFR3=0x1f3fc000
AD9910_4008.npy CFR3=0x1f3fc000
```

> 结论：`0x1F3FC000`（旁路 ÷2）在这批评估板上不可用，必须保持 ÷2 使能。

### 2.5 三个阶段对照

| 阶段 | 软件使用的 SYSCLK | 芯片实际 CFR3 | 芯片 SYSCLK | 观测 |
|---|---|---|---|---|
| 改动前 | 1000 MHz（硬编码） | 期望 `0x1F3FC000`，实际未锁存 → 默认（÷2 使能） | 500 MHz | **半频** |
| 中间版 | 1000 MHz | `0x1F3FC000` 真正锁存 | 1 GHz 直入失败 | **无输出** |
| 当前 | 500 MHz（由 REF_CLK/÷2 推导） | `0x1F3F4000` 真正锁存 | 500 MHz | **正常** |

> 「改动前未锁存」是由现象 + 代码路径推断（CFR2 经单项设置生效、CFR3 仅在 reset 写）
> 得出，与 `npy` 镜像和官方软件行为一致。

## 3. 修改方案

核心原则：**外部参考时钟与内部时钟分开** —— 只声明「REF_CLK 频率 + ÷2 分频器状态」，
SYSCLK 一律由二者推导，禁止手工填写 SYSCLK。

```
SYSCLK = REF_CLK / (2 if REFCLK ÷2 使能 else 1)     # REFCLK PLL 关闭
FTW    = round(freq / SYSCLK × 2^32)
```

与官方软件一一对应：

| 官方评估软件 | 本服务 | 本实验室取值 |
|---|---|---|
| External Clock | `--refclk 1000` | 1000 MHz |
| Divider Disable（勾选=旁路） | `--divider-disable` | 不勾选（÷2 使能） |
| System Clock | 自动推导，只读展示 | 500 MHz |

### 3.1 `ad9910.py`

```python
class AD9910(ADIClockEval):
    _refclk_mhz = 1000.0    # 官方软件 External Clock
    _refclk_div2 = True     # 官方软件未勾 Divider Disable（÷2 使能）

    @classmethod
    def get_clock(cls):
        return {
            'refclk_mhz': cls._refclk_mhz,
            'div2': cls._refclk_div2,
            'sysclk_mhz': cls._refclk_mhz / (2 if cls._refclk_div2 else 1),
        }

    @classmethod
    def configure_clock(cls, refclk_mhz=None, div2=None):
        """设置 REF_CLK / ÷2 并同步已创建实例；返回 get_clock()。>0 校验。"""

    @property
    def refclk_frequency(self):   # 外部参考时钟
    @property
    def sysclk_frequency(self):   # 派生：FTW 编码用
```

`status` / `set_config` / 所有 FTW 编码统一使用 `self.sysclk_frequency`（不再有独立
`_sysclk_mhz`）。环境变量入口：`AD9910_REFCLK`、`AD9910_DIVIDER_DISABLE=1`。

模块级常量（CFR3 只差 bit15）：

```python
CFR3_DIV2_ENABLED = 0x1F3F4000   # bit15=0：÷2 使能 -> SYSCLK = REF_CLK / 2
CFR3_DIV2_BYPASS  = 0x1F3FC000   # bit15=1：旁路 ÷2 -> SYSCLK = REF_CLK（本批板停振）
```

`apply_clock()`：按当前分频器状态重写 CFR3 + 触发 IO_UPDATE，返回写入值。
`update()`：只做 `0→1→0` 的 IO_UPDATE 脉冲。`reset()` 复用 `apply_clock()`：

```python
self.spi_write(0, 0x00000000)                # CFR1
self.spi_write(1, 0x01400820)                # CFR2（profile ASF 使能）
self.apply_clock()                           # CFR3 + IO_UPDATE（脉冲一并锁存 CFR1/CFR2）

def apply_clock(self):
    cfr3 = CFR3_DIV2_ENABLED if self._refclk_div2 else CFR3_DIV2_BYPASS
    self.spi_write(2, cfr3)
    self.update()                             # 0x80 -> 0x90 -> 0x80
    return cfr3
```

要点：

1. CFR3 由 `div2` 推导，不再写死一个"碰运气"的值；
2. IO_UPDATE 用完整 `0→1→0` 脉冲，**不依赖端口上电电平**，保证每次启动配置一致；
3. `apply_clock()` **不依赖 `.npy` 是否存在**，因此可以在每次服务启动时逐板调用
   （原因见第 8 章：`__init__` 在镜像已存在时会跳过 `reset`）。

### 3.2 `ad9910_wrapper.py`

新增 `configure_clock(refclk_mhz=None, div2=None)` / `get_clock()` / `apply_clock(sn)`，
转发到 `AD9910`，供 `control_service` 与其它入口使用（替代原先的 `set_sysclk`）。

### 3.3 `control_service/device_manager.py`

```python
AD9910DeviceManager(wrapper=None, ad9910_cls=None,
                    refclk_mhz=None, div2=None)
```

- `start()` 在**枚举/复位设备之前**调用 `_apply_clock()`，保证首个 FTW 就按正确时钟编码；
- 枚举后、读状态与加载配置之前，对**每块板**调用 `_apply_device_clock(sn, dev)`
  （`wrapper.apply_clock(sn)`），逐板重写 CFR3 并锁存，避免某板残留旧时钟配置；
- `clock` 属性返回 `{refclk_mhz, div2, sysclk_mhz}`（未配置项为 `None`）；
- 替身对象没有相应接口时静默跳过，便于测试。

### 3.4 `control_service/app.py`

```
--refclk MHz          外部参考时钟（默认 1000），= 官方 External Clock
--divider-disable     旁路 REFCLK ÷2（= 勾选官方 Divider Disable）；默认不加
```

- 由两者推导 `sysclk_mhz`，写入状态快照的 `services`：
  `{"refclk_mhz": 1000.0, "div2": true, "sysclk_mhz": 500.0}`；
- 启动日志打印：`DDS 时钟: REF_CLK=1000.0 MHz, divider=÷2 enabled -> SYSCLK=500.0 MHz`；
- 移除了旧的 `--sysclk`（避免"两个真相"）。

### 3.5 前端

页面右上角显示 `REF_CLK 1000 MHz → SYSCLK 500 MHz`，悬停可看 ÷2 状态
（`÷2 分频器使能/旁路`）。显示 `—` 表示后端未上报（旧进程）或浏览器缓存旧脚本，
提示重启 + `Ctrl+F5`。

### 3.6 `start_web.ps1`

```powershell
$refclk = if ($env:AD9910_REFCLK) { $env:AD9910_REFCLK } else { '1000' }
$clockArgs = @('--refclk', $refclk)
if ($env:AD9910_DIVIDER_DISABLE -eq '1') { $clockArgs += '--divider-disable' }
...
& $py -m ...app ... --config initial @clockArgs
```

注释为纯 ASCII（PowerShell 5.1 读无 BOM 的 UTF-8 会把中文显示成乱码）。

### 3.7 文档与测试

- 本文件 + `README.md`（参数表、`## 时钟：REF_CLK 与 SYSCLK` 章节）；
- `tests/test_app.py`：`--refclk` 默认/取值/非法值、`--divider-disable` 开关；
- `tests/test_device_manager.py`：时钟在 `inits` 之前下发、÷2 旁路时 SYSCLK = REF_CLK、
  未配置时不发命令。

## 4. 验证

### 4.1 自动化

```bash
python -m pytest devices/generators/adiclockevals/control_service/tests/ -q
# 173 passed, 2 skipped
```

定向校验：

```python
from devices.generators.adiclockevals.ad9910 import AD9910, AD9910Profile
AD9910.get_clock()      # {'refclk_mhz': 1000.0, 'div2': True, 'sysclk_mhz': 500.0}
AD9910Profile.encode_frequency(110.0, 500.0)   # 0x3851EB85（正确）
AD9910Profile.encode_frequency(110.0, 1000.0)  # 0x1C28F5C3（旧值，输出会减半）
```

### 4.2 上机自检清单（重启服务后）

1. 控制台：`DDS clock: REF_CLK 1000 MHz, divider enabled (/2) -> SYSCLK 500 MHz`
2. 控制台：**每块板**一行
   `设备 4008 时钟寄存器已重写: CFR3=0x1f3f4000 (div2=True, SYSCLK=500.0 MHz)`
3. 网页右上角：`REF_CLK 1000 MHz → SYSCLK 500 MHz`
4. 寄存器镜像里 CFR3 应为 ÷2 使能：

   ```bash
   python -c "import numpy;print(hex(int(numpy.load('devices/generators/adiclockevals/config/AD9910_0443.npy')[2])))"
   # 期望 0x1f3f4000（0x1f3fc000 表示仍在旁路 ← 会停振）
   ```

5. 频谱仪判定表（网页设 100 MHz）：

   | 实测 | 含义 | 处理 |
   |---|---|---|
   | **100 MHz** | 正确 | 完成 |
   | 50 MHz | 软件 SYSCLK 是实际的 2 倍 | 检查 `--refclk` / 是否误加 `--divider-disable` |
   | 200 MHz | 软件 SYSCLK 是实际的一半 | 确认 ÷2 是否被硬件旁路 |
   | 无输出 | 该板芯片无 SYSCLK | 看该板的 `CFR3` 日志/镜像；必要时用官方软件恢复 |

## 5. 运维规则（避免复发）

1. **不要手填 SYSCLK**：只声明 `--refclk` 与 `--divider-disable`，SYSCLK 由公式推导。
2. **不要为了「1 GHz 直入」把 CFR3[15] 置 1**：本批评估板实测会停振，默认保持 ÷2 使能。
3. **改动 `reset()` 的寄存器时序必须上机验证**：IO_UPDATE 必须是显式上升沿，
   任何"依赖端口默认电平"的写法都会让配置不可复现。
4. 换板卡或换参考时钟时，按顺序确认：官方软件 External Clock / Divider Disable /
   System Clock 三项 → 改 `--refclk`（必要时加 `--divider-disable`）→ 设 100 MHz 实测。
5. 页面回读不是硬件回读，**永远用频谱仪/官方软件交叉验证一次**。

## 6. 排查命令速查

```bash
# 当前生效的时钟假设（服务状态）
curl http://127.0.0.1:18867/api/state        # services.refclk_mhz / div2 / sysclk_mhz

# 所有板的 CFR1/CFR2/CFR3 镜像
python -c "import numpy,glob;[print(p,hex(int(numpy.load(p)[0])),hex(int(numpy.load(p)[1])),hex(int(numpy.load(p)[2]))) for p in sorted(glob.glob('devices/generators/adiclockevals/config/AD9910_*.npy'))]"

# 编码核对
python -c "from devices.generators.adiclockevals.ad9910 import AD9910Profile as P;print(hex(P.encode_frequency(110.0,500.0)))"
```

## 7. 涉及文件

| 文件 | 改动 |
|---|---|
| `ad9910.py` | REF_CLK/÷2 模型、`configure_clock`/`get_clock`、派生 `sysclk_frequency`、CFR3 常量与 `apply_clock()`/`update()`、环境变量 |
| `ad9910_wrapper.py` | `configure_clock` / `get_clock` / `apply_clock(sn)` 转发 |
| `control_service/app.py` | `--refclk` / `--divider-disable`、services 上报三项、启动日志（移除 `--sysclk`） |
| `control_service/device_manager.py` | `refclk_mhz`/`div2` 注入、`_apply_clock()`、逐板 `_apply_device_clock()` + 日志、`clock` 属性 |
| `control_service/static/index.html`、`app.js` | 显示 `REF_CLK → SYSCLK`，悬停提示 ÷2 状态 |
| `control_service/start_web.ps1` | 传 `--refclk`（默认 1000）、可选 `--divider-disable` |
| `control_service/README.md` | 参数表与「时钟：REF_CLK 与 SYSCLK」章节 |
| `control_service/tests/test_app.py`、`test_device_manager.py` | 时钟参数、逐板重写下发与顺序用例 |

## 8. 后续事件：单板（0x4008）无输出

### 8.1 现象

时钟改造上线后大多数板正常，但 **0x4008（1013 AOM）无输出**，同一块板用官方软件正常。

### 8.2 现场证据

`config/AD9910_*.npy` 是逐次 `spi_write` 同步写入的寄存器镜像（每次服务启动都会刷新，
可用文件 mtime 判断是哪一次运行写的）：

| 文件 | CFR3 | profile0 的 FTW | 解读 |
|---|---|---|---|
| `AD9910_4008.npy` | `0x1f3fc000`（÷2 **旁路**） | `0x3851eb86`（110 MHz @ **500 MHz**） | profile 是新代码按 500 MHz 编码的，CFR3 却是旧值 |
| 其它板 | `0x1f3fc000` | 各自正常 | 芯片侧已经各自回到 ÷2 |

关键矛盾：**profile 用了 500 MHz 编码（新代码），CFR3 却还是 `0x1F3FC000`（旧代码）**
→ 说明新代码从来没有写过 CFR3。

### 8.3 根因：`.npy` 存在时 `__init__` 跳过 reset

```python
# ad9910.py::AD9910.__init__
if not os.path.isfile(path):       # 寄存器镜像已存在 -> 不下载固件、不 reset
    self.download_firmware(...)
    self.reset()
```

`ADIClockEval.inits()` 开头会 `cls.deletes()`，但**新进程里设备注册表为空**，
`deletes()` 什么也没做；随后实例化时 `.npy` 已存在 → 跳过 `reset()`。

结果：**服务启动只写 profile（profile 每次加载配置都写），从不配置 CFR3/IO_UPDATE。**
芯片保留上一次写入的时钟配置——可能是上一版强制 latch 的"旁路 ÷2"，也可能是
官方软件留下的配置。

- 其它板恢复正常的偶然原因：期间掉过电或被官方软件操作过，芯片回到复位默认
  （÷2 使能）→ 500 MHz → 正常。
- 0x4008 一直保持"旁路 ÷2"（1 GHz 直入）→ 芯片没有 SYSCLK → **无输出**。

### 8.4 修复

- `AD9910.apply_clock()`：按当前 `_refclk_div2` 写 CFR3 并做 `0→1→0` 的 IO_UPDATE，
  返回写入值；**不依赖 `.npy` 是否存在**。`reset()` 复用它。
- `ad9910_wrapper.apply_clock(sn)` 转发给单板。
- `AD9910DeviceManager.start()`：枚举完成后、读取状态与 `load_config()` 之前，
  对**每块板**调用 `_apply_device_clock()`，并打印
  `设备 XXXX 时钟寄存器已重写: CFR3=... (div2=..., SYSCLK=... MHz)`。
- 效果：「启动必然重新配置时钟」不再依赖镜像文件是否存在、也不依赖设备是否掉电；
  某块板被官方软件或旧版本配错时钟后，重启服务即可自动纠正。

### 8.5 教训

1. 判断"配置是否真的下发"不能只看 `.npy`（它只反映"曾经写过什么"），
   要同时看**代码路径是否被执行**（`__init__` 的跳过条件是隐蔽的）。
2. 任何"每次启动都该生效"的硬件配置，必须显式、逐板、幂等地重写一次，
   不能把责任交给"上次运行的残留状态"。
3. 多板系统里，单板异常优先怀疑**该板芯片侧状态**（不是软件分支），
   因为软件对每块板走的是同一段代码。
