# -*- coding: utf-8 -*-
"""
状态模型：线程安全 applied-state 快照。

applied state 表示本服务最后一次确认写入成功的状态（host_applied），
不是硬件回读。由 ControlHub 持有，RLock 保护，供 Web/RPyC 查询。
"""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# 设备状态（一个 AD9910）
# profiles: list[dict] 长度为 8，每项 {freq: float(MHz), phase: float(pi), amp: float(0~1)}
# 其余字段：name, active_profile, enable_profile_asf, enable_sine_out


@dataclass
class AppliedState:
    """整个服务的状态快照（可序列化为 JSON）。"""

    status: str = "starting"  # starting/ready/applying/fault/stopping
    revision: int = 0
    state_valid: bool = True
    state_source: str = "host_applied"
    active_config: Optional[str] = None
    services: Dict[str, Any] = field(default_factory=dict)
    devices: Dict[str, Any] = field(default_factory=dict)  # {hex_id: device_dict}
    last_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """返回可 JSON 序列化的字典副本（深拷贝语义由调用方保证）。"""
        return {
            "status": self.status,
            "revision": self.revision,
            "state_valid": self.state_valid,
            "state_source": self.state_source,
            "active_config": self.active_config,
            "services": dict(self.services),
            "devices": dict(self.devices),
            "last_error": self.last_error,
        }


class StateStore:
    """线程安全的状态存储：ControlHub 唯一写者，查询方直接读。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = AppliedState()

    def snapshot(self) -> Dict[str, Any]:
        """返回当前快照的深拷贝（外部修改不影响内部状态）。"""
        with self._lock:
            return copy.deepcopy(self._state.to_dict())

    def replace(self, state: AppliedState) -> None:
        """整体替换状态（由 ControlHub worker 提交成功后调用）。"""
        with self._lock:
            self._state = state

    def mutate(self, fn) -> None:
        """在锁内对 AppliedState 做原地修改。"""
        with self._lock:
            fn(self._state)
