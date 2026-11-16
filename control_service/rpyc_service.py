# -*- coding: utf-8 -*-
"""
RPyC 服务。

兼容旧接口（保留方法名与返回结构，供既有客户端使用）：
    conn.root.get_ad9910_service() -> AD9910RPyCService
      .set_frequency(device_id, profile, freq)   -> True/False
      .set_amplitude(device_id, profile, amp)    -> True/False
      .adjust_amplitude(device_id, profile, delta) -> True/False
      .set_phase(device_id, profile, phase)      -> True/False
      .close()                                   -> True

新增接口（同一服务对象）：
    .get_state_snapshot()                      -> 状态快照 dict
    .get_device_info(device_id)                -> 设备参数 dict 或 None
    .update_parameters(device_id, updates)     -> 提交并等待（抛 CommandError）
    .update_parameters_batch(updates_by_device) -> {device_id: result}
    .load_config(config)                       -> 提交并等待
    .clear_outputs(device_id)                  -> 提交并等待

所有写方法最终都通过 ControlHub 命令队列，不直接操作硬件。
连接/断开打印带时区时间戳与客户端地址。
"""

from __future__ import annotations

import logging
import math
import threading
from datetime import datetime
from typing import Any, Dict, Optional

import rpyc
from rpyc.utils.server import ThreadedServer

from .commands import Command, CommandError
from .device_manager import normalize_device_id
from .hub import ControlHub

logger = logging.getLogger(__name__)

# 与旧 server 一致的 protocol_config，保证既有客户端兼容
PROTOCOL_CONFIG = {
    "allow_public_attrs": True,
    "allow_pickle": True,
    "allow_all_attrs": True,
}


def _profile_updates(profile: int, fields: Dict[str, Any]) -> Optional[list]:
    """构造长度为 8 的 profiles 部分更新（仅改指定 profile）；越界返回 None。"""
    if isinstance(profile, bool) or not isinstance(profile, int) or not 0 <= profile < 8:
        return None
    updates = [None] * 8
    updates[profile] = fields
    return updates


def _checked_profile(profile: int) -> bool:
    return (not isinstance(profile, bool) and isinstance(profile, int)
            and 0 <= profile < 8)


def _get_peer(conn) -> str:
    try:
        sock = conn._channel.stream.sock
        return "%s:%d" % (sock.getpeername()[0], sock.getpeername()[1])
    except Exception:
        return "unknown"


def _log_peer(action: str, peer: str) -> None:
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    logger.info("[%s] RPyC %s peer=%s", ts, action, peer)


class AD9910RPyCService:
    """暴露给 rpyc 客户端的服务对象（兼容旧方法 + 新方法）。"""

    # 兼容旧 adjust_amplitude 的幅度上限 sqrt(0.2)
    _ADJUST_AMP_MAX = math.sqrt(0.2)

    def __init__(self, hub: ControlHub) -> None:
        self._hub = hub

    # ---------------- 兼容旧接口（返回 True/False） ----------------

    def set_frequency(self, device_id: int, profile: int, freq: float) -> bool:
        if not _checked_profile(profile):
            return False
        return self._try_apply(device_id, {
            "profiles": _profile_updates(profile, {"freq": float(freq)})})

    def set_amplitude(self, device_id: int, profile: int, amp: float) -> bool:
        if not _checked_profile(profile):
            return False
        return self._try_apply(device_id, {
            "profiles": _profile_updates(profile, {"amp": float(amp)})})

    def set_phase(self, device_id: int, profile: int, phase: float) -> bool:
        if not _checked_profile(profile):
            return False
        return self._try_apply(device_id, {
            "profiles": _profile_updates(profile, {"phase": float(phase)})})

    def adjust_amplitude(self, device_id: int, profile: int, delta: float) -> bool:
        """在当前幅度基础上调整，clamp 到 [0, sqrt(0.2)]（兼容旧行为）。"""
        if not _checked_profile(profile):
            return False
        try:
            sid = normalize_device_id(device_id)
            snap = self._hub.get_state_snapshot()
            cur = snap["devices"][sid]["profiles"][profile]["amp"]
        except Exception:
            return False
        new_amp = max(0.0, min(cur + delta, self._ADJUST_AMP_MAX))
        return self._try_apply(device_id, {
            # float(): 功率反馈程序常传 numpy 标量，直接传入会在
            # validate_updates 的 isinstance(x, (int, float)) 校验失败，
            # 导致静默返回 False、Web 端不更新。
            "profiles": _profile_updates(profile, {"amp": float(new_amp)})})

    def close(self) -> bool:
        return True

    # ---------------- 新增接口 ----------------

    def get_state_snapshot(self) -> Dict[str, Any]:
        return self._hub.get_state_snapshot()

    def get_device_info(self, device_id: Any) -> Optional[Dict[str, Any]]:
        try:
            sid = normalize_device_id(device_id)
        except CommandError:
            return None
        return self._hub.get_state_snapshot()["devices"].get(sid)

    def update_parameters(self, device_id: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
        """提交参数更新并同步等待结果（异常直接向客户端传播）。"""
        return self._hub.submit(Command(
            op="apply_parameters", device_id=device_id, payload=updates))

    def update_parameters_batch(self, updates_by_device: Dict[str, Any]) -> Dict[str, Any]:
        """批量更新：{device_id: updates}，逐设备提交。"""
        results = {}
        for device_id, updates in (updates_by_device or {}).items():
            try:
                sid = normalize_device_id(device_id)  # 提前归一化，避免 except 分支再抛
            except CommandError as e:
                results[str(device_id)] = {"ok": False, "error": e.message,
                                           "code": e.code}
                continue
            try:
                r = self.update_parameters(sid, updates)
                results[sid] = r
            except CommandError as e:
                results[sid] = {"ok": False, "error": e.message, "code": e.code}
        return results

    def load_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return self._hub.submit(Command(op="load_config", device_id="", payload=config))

    def clear_outputs(self, device_id: Any) -> Dict[str, Any]:
        return self._hub.submit(Command(op="clear_outputs", device_id=device_id))

    # ---------------- 内部 ----------------

    def _try_apply(self, device_id: Any, updates: Dict[str, Any]) -> bool:
        """旧接口的容错包装：失败返回 False 而非抛异常。"""
        try:
            self._hub.submit(Command(
                op="apply_parameters", device_id=device_id, payload=updates))
            return True
        except Exception as e:
            logger.error("RPyC apply 失败 device=%r updates=%r: %s",
                         device_id, updates, e)
            return False


class AD9910RPyCServer(rpyc.Service):
    """RPyC 服务入口：暴露 get_ad9910_service()，记录连接日志。"""

    def __init__(self, hub: ControlHub) -> None:
        self._hub = hub

    def on_connect(self, conn) -> None:
        peer = _get_peer(conn)
        try:
            conn._rpyc_peer = peer  # 断开时 socket 已关闭，需缓存
        except Exception:
            pass
        _log_peer("connected", peer)

    def on_disconnect(self, conn) -> None:
        peer = getattr(conn, "_rpyc_peer", None) or _get_peer(conn)
        _log_peer("disconnected", peer)

    def exposed_get_ad9910_service(self) -> AD9910RPyCService:
        return AD9910RPyCService(self._hub)


def start_rpyc_server(hub: ControlHub, host: str = "0.0.0.0",
                      port: int = 3251) -> ThreadedServer:
    """在独立线程启动 ThreadedServer，返回 server 对象（server.close() 可停止）。"""
    server = ThreadedServer(
        AD9910RPyCServer(hub),
        hostname=host,
        port=port,
        protocol_config=dict(PROTOCOL_CONFIG),
    )
    thread = threading.Thread(target=server.start,
                              name="rpyc-thread", daemon=True)
    thread.start()
    logger.info("RPyC server 启动于 %s:%d", host, port)
    return server
