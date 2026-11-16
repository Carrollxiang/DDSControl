# -*- coding: utf-8 -*-
"""
ControlHub 单元测试：唯一 worker、Future 同步等待、状态提交、
revision、状态机、busy、fault 行为。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_hub.py -v
"""

import threading
import time

import pytest

from devices.generators.adiclockevals.control_service.commands import Command, CommandError
from devices.generators.adiclockevals.control_service.device_manager import FakeDeviceManager
from devices.generators.adiclockevals.control_service.hub import ControlHub


class BlockingDM(FakeDeviceManager):
    """apply_parameters 阻塞直至 release，用于制造队列积压。"""

    def __init__(self, device_ids=("0443",)):
        super().__init__(device_ids=device_ids)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.executed = []  # 按执行顺序记录 name

    def apply_parameters(self, device_id, updates):
        self.entered.set()
        self.release.wait(5)
        r = super().apply_parameters(device_id, updates)
        self.executed.append(updates.get("name"))
        return r


class ThreadLoggingDM(FakeDeviceManager):
    """记录设备调用发生的线程 id。"""

    def __init__(self, device_ids=("0443", "0422")):
        super().__init__(device_ids=device_ids)
        self.call_threads = []
        self._guard = threading.Lock()

    def _log(self):
        with self._guard:
            self.call_threads.append(threading.get_ident())

    def apply_parameters(self, device_id, updates):
        self._log()
        return super().apply_parameters(device_id, updates)

    def clear_outputs(self, device_id):
        self._log()
        return super().clear_outputs(device_id)


class FailingDM(FakeDeviceManager):
    """apply_parameters 抛硬件异常，用于触发 fault。"""

    def apply_parameters(self, device_id, updates):
        raise RuntimeError("USB pipe broken")


@pytest.fixture
def hub():
    h = ControlHub(FakeDeviceManager(device_ids=("0443",)))
    h.start()
    yield h
    h.stop()


# ---------------- 唯一 worker 线程 + Future ----------------

def test_all_device_calls_happen_in_worker_thread():
    dm = ThreadLoggingDM()
    h = ControlHub(dm)
    h.start()
    worker_tid = h._worker.ident
    assert worker_tid is not None

    errors = []

    def submitter(n):
        try:
            for i in range(5):
                h.submit(Command(
                    op="apply_parameters",
                    device_id="0443" if n % 2 else "0422",
                    payload={"name": "t%d-%d" % (n, i)}))
        except CommandError as e:
            errors.append(e.code)

    threads = [threading.Thread(target=submitter, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors
    assert len(dm.call_threads) == 20
    assert all(tid == worker_tid for tid in dm.call_threads)
    assert h.get_state_snapshot()["revision"] == 20
    h.stop()


def test_future_synchronously_waits_and_returns_result(hub):
    r = hub.submit(Command(op="apply_parameters", device_id="0443",
                           payload={"name": "sync"}))
    assert r["ok"] is True
    assert r["parameters"]["name"] == "sync"


def test_exception_propagates_to_submitter(hub):
    with pytest.raises(CommandError) as ei:
        hub.submit(Command(op="apply_parameters", device_id="0443",
                           payload={"active_profile": 99}))
    assert ei.value.code == "invalid_parameter"


# ---------------- 有界 FIFO 与 busy ----------------

def test_fifo_order_strict():
    bdm = BlockingDM()
    h = ControlHub(bdm, queue_maxsize=2)
    h.start()
    results = []

    def run(op):
        try:
            r = h.submit(Command(op="apply_parameters", device_id="0443",
                                 payload={"name": op}))
            results.append(("ok", r["parameters"]["name"]))
        except CommandError as e:
            results.append((e.code, None))

    t1 = threading.Thread(target=run, args=("first",))
    t1.start()
    assert bdm.entered.wait(2), "worker 未进入设备调用"
    t2 = threading.Thread(target=run, args=("second",))
    t3 = threading.Thread(target=run, args=("third",))
    t2.start()
    t3.start()
    time.sleep(0.2)

    with pytest.raises(CommandError) as ei:
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "overflow"}))
    assert ei.value.code == "busy"

    bdm.release.set()
    for t in (t1, t2, t3):
        t.join(5)
    # worker 严格 FIFO：执行顺序 = 提交顺序
    assert bdm.executed == ["first", "second", "third"]
    assert sorted(results) == [("ok", "first"), ("ok", "second"),
                               ("ok", "third")]
    assert h.get_state_snapshot()["revision"] == 3
    h.stop()


# ---------------- 状态提交与 revision ----------------

def test_success_commits_state_once_and_bumps_revision(hub):
    assert hub.get_state_snapshot()["revision"] == 0
    hub.submit(Command(op="apply_parameters", device_id="0443",
                       payload={"name": "A", "active_profile": 2}))
    snap = hub.get_state_snapshot()
    assert snap["revision"] == 1
    assert snap["devices"]["0443"]["name"] == "A"
    assert snap["devices"]["0443"]["active_profile"] == 2
    assert snap["status"] == "ready"


def test_failed_validation_does_not_commit(hub):
    with pytest.raises(CommandError):
        hub.submit(Command(op="apply_parameters", device_id="0443",
                           payload={"active_profile": 9}))
    snap = hub.get_state_snapshot()
    assert snap["revision"] == 0
    assert snap["last_error"] is None
    assert snap["state_valid"] is True


def test_unknown_device_does_not_commit(hub):
    with pytest.raises(CommandError) as ei:
        hub.submit(Command(op="apply_parameters", device_id="9999",
                           payload={"name": "x"}))
    assert ei.value.code == "not_found"
    assert hub.get_state_snapshot()["revision"] == 0


def test_clear_outputs_commits(hub):
    hub.submit(Command(op="apply_parameters", device_id="0443",
                       payload={"profiles": [{"freq": 218.0, "amp": 0.3}] + [None] * 7}))
    hub.submit(Command(op="clear_outputs", device_id="0443"))
    snap = hub.get_state_snapshot()
    assert snap["revision"] == 2
    assert all(p["amp"] == 0.0 for p in snap["devices"]["0443"]["profiles"])
    assert snap["devices"]["0443"]["profiles"][0]["freq"] == 218.0


def test_load_config_commits_and_sets_active_config(hub):
    cfg = {"name": "mycfg", "device_info": {"0443": {"name": "C"}}}
    r = hub.submit(Command(op="load_config", device_id="", payload=cfg))
    assert r["active_config"] == "mycfg"
    snap = hub.get_state_snapshot()
    assert snap["active_config"] == "mycfg"
    assert snap["devices"]["0443"]["name"] == "C"


# ---------------- 状态机 / fault ----------------

def test_hardware_failure_enters_fault_and_rejects_writes():
    h = ControlHub(FailingDM(device_ids=("0443",)))
    h.start()
    with pytest.raises(CommandError) as ei:
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "boom"}))
    assert ei.value.code == "hardware_fault"
    snap = h.get_state_snapshot()
    assert snap["status"] == "fault"
    assert snap["state_valid"] is False
    assert snap["last_error"] is not None
    # 故障状态下写命令被拒绝
    with pytest.raises(CommandError) as ei2:
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "again"}))
    assert ei2.value.code == "fault"
    # 故障不产生虚假 applied state：revision 保持 0
    assert h.get_state_snapshot()["revision"] == 0
    h.stop()


def test_query_allowed_in_fault():
    h = ControlHub(FailingDM(device_ids=("0443",)))
    h.start()
    with pytest.raises(CommandError):
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "boom"}))
    # 查询不受影响
    assert h.get_state_snapshot()["status"] == "fault"
    h.stop()


def test_submit_rejected_before_start():
    h = ControlHub(FakeDeviceManager(device_ids=("0443",)))
    with pytest.raises(CommandError) as ei:
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "x"}))
    assert ei.value.code == "unavailable"


def test_stop_rejects_new_commands(hub):
    hub.stop()
    with pytest.raises(CommandError) as ei:
        hub.submit(Command(op="apply_parameters", device_id="0443",
                           payload={"name": "late"}))
    assert ei.value.code == "unavailable"


def test_applying_status_visible_while_command_runs():
    bdm = BlockingDM()
    h = ControlHub(bdm)
    h.start()

    def run():
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "slow"}))

    t = threading.Thread(target=run)
    t.start()
    assert bdm.entered.wait(2)
    # 命令执行中状态为 applying
    assert h.get_state_snapshot()["status"] == "applying"
    bdm.release.set()
    t.join(5)
    # 完成后回到 ready
    assert h.get_state_snapshot()["status"] == "ready"
    h.stop()


def test_stop_waits_for_inflight_command():
    bdm = BlockingDM()
    h = ControlHub(bdm)
    h.start()
    done = threading.Event()

    def run():
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "slow"}))
        done.set()

    t = threading.Thread(target=run)
    t.start()
    assert bdm.entered.wait(2)

    stop_finished = threading.Event()

    def do_stop():
        h.stop()
        stop_finished.set()

    st = threading.Thread(target=do_stop)
    st.start()
    time.sleep(0.3)
    # stop 必须等待当前硬件命令结束，不能立即返回
    assert not stop_finished.is_set()
    bdm.release.set()
    t.join(5)
    st.join(5)
    assert stop_finished.is_set()
    assert done.is_set()
    # 设备关闭（Fake 记录 close 操作），状态进入 stopping
    assert ("close",) in bdm.op_log
    assert h.get_state_snapshot()["status"] == "stopping"


def test_stop_rejects_new_commands_while_stopping():
    bdm = BlockingDM()
    h = ControlHub(bdm)
    h.start()
    t = threading.Thread(target=lambda: h.submit(
        Command(op="apply_parameters", device_id="0443",
                payload={"name": "slow"})))
    t.start()
    assert bdm.entered.wait(2)
    st = threading.Thread(target=h.stop)
    st.start()
    time.sleep(0.2)
    # 停止过程中新命令被拒绝
    with pytest.raises(CommandError) as ei:
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "late"}))
    assert ei.value.code == "unavailable"
    bdm.release.set()
    t.join(5)
    st.join(5)


def test_stop_resolves_pending_futures():
    bdm = BlockingDM()
    h = ControlHub(bdm, queue_maxsize=8)
    h.start()
    t = threading.Thread(target=lambda: h.submit(
        Command(op="apply_parameters", device_id="0443", payload={"name": "slow"})))
    t.start()
    assert bdm.entered.wait(2)
    # 排队一条（worker 被占用）
    pending = {}

    def submit_pending():
        try:
            h.submit(Command(op="apply_parameters", device_id="0443",
                             payload={"name": "queued"}))
        except CommandError as e:
            pending["code"] = e.code

    pt = threading.Thread(target=submit_pending)
    pt.start()
    time.sleep(0.2)
    # 停止：排队命令的 future 应被置为 unavailable，不悬挂
    h.stop()
    pt.join(5)
    assert pending.get("code") == "unavailable", pending
    bdm.release.set()
    t.join(5)


def test_expected_revision_conflict_detected_at_worker():
    bdm = BlockingDM()
    h = ControlHub(bdm, queue_maxsize=8)
    h.start()
    # 两条并发命令都基于 revision=0 提交
    results = {}

    def run(tag, expected):
        try:
            h.submit(Command(op="apply_parameters", device_id="0443",
                             payload={"name": tag}, expected_revision=expected))
            results[tag] = "ok"
        except CommandError as e:
            results[tag] = e.code

    t1 = threading.Thread(target=run, args=("first", 0))
    t1.start()
    assert bdm.entered.wait(2)
    t2 = threading.Thread(target=run, args=("second", 0))
    t2.start()
    time.sleep(0.2)
    bdm.release.set()
    t1.join(5)
    t2.join(5)
    # first 先执行成功（revision 0→1）；second 执行时 revision=1 != 0 -> conflict
    assert results == {"first": "ok", "second": "conflict"}, results
    assert h.get_state_snapshot()["devices"]["0443"]["name"] == "first"
    assert h.get_state_snapshot()["revision"] == 1
    h.stop()


def test_busy_does_not_enter_fault():
    bdm = BlockingDM()
    h = ControlHub(bdm, queue_maxsize=2)
    h.start()
    t = threading.Thread(target=lambda: h.submit(
        Command(op="apply_parameters", device_id="0443", payload={"name": "slow"})))
    t.start()
    assert bdm.entered.wait(2)
    # 队列容量 2：cmd1 执行中不占队列，再入队 2 条占满
    q2 = threading.Thread(target=lambda: h.submit(
        Command(op="apply_parameters", device_id="0443", payload={"name": "q2"})))
    q3 = threading.Thread(target=lambda: h.submit(
        Command(op="apply_parameters", device_id="0443", payload={"name": "q3"})))
    q2.start()
    q3.start()
    time.sleep(0.2)
    with pytest.raises(CommandError) as ei:
        h.submit(Command(op="apply_parameters", device_id="0443",
                         payload={"name": "overflow"}))
    assert ei.value.code == "busy"
    assert h.get_state_snapshot()["status"] == "applying"  # 非 fault
    bdm.release.set()
    for x in (t, q2, q3):
        x.join(5)
    assert h.get_state_snapshot()["status"] == "ready"
    h.stop()
