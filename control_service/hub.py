# -*- coding: utf-8 -*-
"""
ControlHub：Web / RPyC 与硬件之间的唯一入口。

职责：
- 持有容量固定的 FIFO 命令队列（queue.Queue(maxsize=256)）
- 运行唯一硬件 worker 线程（唯一接触设备的线程）
- 同步等待命令执行结果（concurrent.futures.Future）
- 成功后一次性提交 applied state 并递增全局 revision
- 管理 starting/ready/applying/fault/stopping 状态
- 队列满返回 busy；fault 状态下限制写命令

线程模型：
- worker 线程是唯一调用 DeviceManager 的线程
- 查询（get_state_snapshot）直接读线程安全快照，不经过队列
"""

from __future__ import annotations

import logging
import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional

from .commands import Command, CommandError
from .device_manager import DeviceManager, normalize_device_id
from .state import AppliedState, StateStore

logger = logging.getLogger(__name__)

WRITE_OPS = ("apply_parameters", "clear_outputs", "load_config")

HUB_TIMEOUT = 30.0  # 单条命令同步等待超时（秒）


class ControlHub:
    QUEUE_MAXSIZE = 256

    def __init__(self, device_manager: DeviceManager,
                 queue_maxsize: int = QUEUE_MAXSIZE,
                 services: Optional[Dict[str, Any]] = None,
                 fault_action: str = "keep") -> None:
        if fault_action not in ("keep", "clear"):
            raise ValueError("fault_action 必须是 keep 或 clear")
        self._dm = device_manager
        self._fault_action = fault_action
        self._queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._store = StateStore()
        self._store.replace(AppliedState(status="starting"))
        self._services = dict(services or {})
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._listeners: List[Callable[[Dict[str, Any]], None]] = []

    # ---------------- 生命周期 ----------------

    def start(self, config: Optional[Dict[str, Any]] = None) -> None:
        """枚举硬件、应用启动配置并启动 worker 线程。"""
        devices = self._dm.start(config)
        self._store.replace(AppliedState(
            status="ready",
            revision=0,
            state_valid=True,
            state_source="host_applied",
            services=self._services,
            devices=devices))
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="ad9910-worker", daemon=True)
        self._worker.start()
        self._broadcast()

    def stop(self) -> None:
        """停收新命令 -> 等待当前命令结束 -> worker 退出 -> 关闭设备（不复位）。"""
        self._set_status("stopping")
        self._stop_event.set()
        self._queue.put(None)  # 唤醒可能阻塞的 worker
        # 终止排队中尚未执行的命令（防止 future 永不 resolve）
        while True:
            try:
                cmd = self._queue.get_nowait()
            except queue.Empty:
                break
            if cmd is None:
                continue
            if cmd.future is not None and not cmd.future.done():
                cmd.future.set_exception(
                    CommandError("服务正在停止", "unavailable"))
            self._queue.task_done()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=10.0)
        self._dm.close()
        # worker 最后一条命令可能已把状态改回 ready，此处强制置为最终状态
        self._set_status("stopping")
        self._broadcast()

    def set_services(self, services: Dict[str, Any]) -> None:
        """注入 web/rpyc 地址端口信息（app 启动时调用）。"""
        self._services = dict(services)
        self._store.mutate(lambda s: setattr(s, "services", dict(services)))

    # ---------------- 查询 ----------------

    def get_state_snapshot(self) -> Dict[str, Any]:
        """读取线程安全状态快照（不经过命令队列）。"""
        return self._store.snapshot()

    # ---------------- 命令提交 ----------------

    def submit(self, cmd: Command) -> Dict[str, Any]:
        """提交命令并同步等待执行结果。

        返回执行结果 dict；队列满抛 CommandError('busy')；
        参数错误抛 CommandError('invalid_parameter'/'not_found')；
        硬件故障抛 CommandError('hardware_fault') 并进入 fault。
        """
        status = self.get_state_snapshot()["status"]
        if status in ("starting", "stopping"):
            raise CommandError("服务未就绪或正在停止", "unavailable")
        if status == "fault" and cmd.op in WRITE_OPS:
            raise CommandError("设备处于故障状态，拒绝写命令", "fault")
        cmd.future = Future()
        try:
            self._queue.put_nowait(cmd)
        except queue.Full:
            raise CommandError("硬件命令队列已满", "busy")
        return cmd.future.result(timeout=HUB_TIMEOUT)

    # ---------------- worker ----------------

    def _worker_loop(self) -> None:
        while True:
            if self._stop_event.is_set() and self._queue.empty():
                break
            try:
                cmd = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if cmd is None:  # 停止信号
                break
            try:
                self._execute(cmd)
            finally:
                self._queue.task_done()

    def _execute(self, cmd: Command) -> None:
        self._set_status("applying")
        try:
            result = self._run(cmd)
        except CommandError as e:
            # 参数/设备类错误：不进入 fault，状态不提交
            cmd.future.set_exception(e)
            self._set_status("ready")
        except Exception as e:
            logger.exception("硬件命令执行异常 op=%s device=%s", cmd.op, cmd.device_id)
            self._enter_fault("%s: %s" % (type(e).__name__, e))
            cmd.future.set_exception(CommandError(str(e), "hardware_fault"))
        else:
            cmd.future.set_result(result)
            self._set_status("ready")

    def _run(self, cmd: Command) -> Dict[str, Any]:
        """在 worker 线程内执行命令并提交状态（唯一接触硬件的路径）。"""
        if cmd.op in ("apply_parameters", "clear_outputs"):
            cmd.device_id = normalize_device_id(cmd.device_id)
        if cmd.op == "apply_parameters":
            # 编辑冲突检测原子化：以 worker 执行时的全局 revision 为准
            if cmd.expected_revision is not None:
                cur = self.get_state_snapshot()["revision"]
                if cur != cmd.expected_revision:
                    raise CommandError(
                        "版本冲突：当前 revision=%d，编辑基准=%d"
                        % (cur, cmd.expected_revision), "conflict")
            params = self._dm.apply_parameters(cmd.device_id, cmd.payload)
            self._commit({cmd.device_id: params})
            return {"ok": True, "device_id": cmd.device_id, "parameters": params}
        if cmd.op == "clear_outputs":
            self._dm.clear_outputs(cmd.device_id)
            devices = self._dm.get_applied_state()
            self._commit(devices)
            return {"ok": True, "device_id": cmd.device_id}
        if cmd.op == "load_config":
            self._dm.load_config(cmd.payload)
            devices = self._dm.get_applied_state()
            self._commit(devices, active_config=cmd.payload.get("name"))
            return {"ok": True, "active_config": cmd.payload.get("name")}
        raise CommandError("未知命令: %s" % cmd.op, "invalid_parameter")

    # ---------------- 状态提交与广播 ----------------

    def _commit(self, devices: Dict[str, Any],
                active_config: Optional[str] = None) -> None:
        """命令成功：一次性提交设备状态并递增全局 revision。"""
        def mutate(state: AppliedState) -> None:
            state.devices.update(devices)
            if active_config is not None:
                state.active_config = active_config
            state.revision += 1
            state.status = "ready"
            state.state_valid = True
            state.last_error = None
        self._store.mutate(mutate)
        self._broadcast()

    def _enter_fault(self, message: str) -> None:
        """严重硬件故障：标记 fault，不生成虚假 applied state。

        fault_action='clear' 时，尝试在 worker 线程内清输出（可失败，不影响状态）。
        """
        if self._fault_action == "clear":
            try:
                for sid in self.get_state_snapshot()["devices"]:
                    self._dm.clear_outputs(sid)
            except Exception:
                logger.exception("fault 后清除输出失败")
        def mutate(state: AppliedState) -> None:
            state.status = "fault"
            state.state_valid = False
            state.last_error = message
        self._store.mutate(mutate)
        self._broadcast()

    def _set_status(self, status: str) -> None:
        self._store.mutate(lambda s: setattr(s, "status", status))

    # ---------------- 订阅 ----------------

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    def _broadcast(self) -> None:
        snap = self._store.snapshot()
        for cb in list(self._listeners):
            try:
                cb(snap)
            except Exception:
                logger.exception("状态广播回调异常")
