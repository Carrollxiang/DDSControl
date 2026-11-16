# -*- coding: utf-8 -*-
"""
AD9910 统一控制服务包。

架构（对应 需求说明.md）：

    Browser --HTTP/WS--> Tornado Web  ----> ControlHub ----> FIFO 队列
    RPyC client -------> RPyC 服务线程 ---> ControlHub ----> FIFO 队列
                                                          |
                                                          v
                                                 唯一硬件 worker 线程
                                                          |
                                                          v
                                              DeviceManager -> AD9910 硬件

核心约定：
- 设备 id 使用小写 hex 字符串（与 USB 序列号一致，如 '0443'）。
- 幅度统一使用满量程分数（0~1）；频率单位 MHz；相位单位 pi。
- applied state 表示服务确认写入成功的状态（host_applied），不是硬件回读。
- 底层设备协议（adiclockeval.py / ad9910.py）一律不修改。
"""

__version__ = "0.1.0"
