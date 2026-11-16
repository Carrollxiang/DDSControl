# control_service 文档

本目录记录控制服务的问题复盘、设计决策与运维约定。代码与注释仍是第一手依据，
文档用于解释「为什么这样写」，避免同一坑重复踩。

| 文档 | 内容 |
|---|---|
| [ad9910-clock-refclk-sysclk.md](ad9910-clock-refclk-sysclk.md) | AD9910 输出频率减半 / 停振 / 单板无输出复盘：REF_CLK 与 SYSCLK 分离、CFR3 ÷2 分频器、IO_UPDATE 上升沿、逐板重写时钟、修改方案与自检清单 |

## 结论速查

- **时钟只声明两件事**：`--refclk`（外部参考，官方软件 External Clock）与
  `--divider-disable`（是否勾选 Divider Disable）。`SYSCLK` 由公式推导，禁止手工填写。
- 本实验室：`REF_CLK = 1000 MHz` + ÷2 使能 → `SYSCLK = 500 MHz`；
  `FTW = round(freq / SYSCLK × 2^32)`。
- `CFR3 = 0x1F3F4000`（÷2 使能）是正确值；`0x1F3FC000`（旁路 ÷2、1 GHz 直入）
  在本批评估板上会**停止输出**。
- `reset()` 必须给 IO_UPDATE 完整的 `0→1→0` 脉冲，否则 CFR1/2/3 是否生效取决于
  端口上电电平，时钟配置会不可复现。
- **`.npy` 已存在时 `AD9910.__init__` 会跳过 `reset()`**，所以启动时由
  `AD9910DeviceManager.start()` 逐板调用 `apply_clock()` 重写 CFR3 + 锁存。
  单板无输出（如 0x4008）就是该板芯片残留"旁路 ÷2"导致，重启服务即自动纠正。
