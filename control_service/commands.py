# -*- coding: utf-8 -*-
"""
命令模型：进入 ControlHub FIFO 队列的写命令。

命令由 Web / RPyC 构造，经队列由唯一 worker 执行；
worker 完成后通过 Future 回传结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
from concurrent.futures import Future

from .state import AppliedState


class CommandError(Exception):
    """命令执行失败（参数校验失败、硬件失败等）。"""

    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code  # invalid_parameter / hardware_fault / not_found / ...


@dataclass
class Command:
    """一条硬件写命令。"""

    op: str  # apply_parameters / clear_outputs / load_config / set_name
    device_id: str  # 小写 hex 字符串
    payload: Dict[str, Any] = field(default_factory=dict)
    future: Optional[Future] = None  # worker 完成时 set_result / set_exception
    expected_revision: Optional[int] = None
    # 可选：若指定，worker 执行前对比全局 revision，
    # 不一致抛 CommandError('conflict')（用于 Web 编辑冲突检测的原子化）。
    # 可选回调：命令成功后由 ControlHub 提交状态前调用（如更新 name）
    on_success: Optional[Callable[[AppliedState], None]] = None
