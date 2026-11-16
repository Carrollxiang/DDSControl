# -*- coding: utf-8 -*-
"""
Web 服务集成测试：HTTP API（状态/配置/参数/清输出/错误码）与 WebSocket 推送。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_web.py -v
"""

import json
import os
import tempfile
import threading

from tornado import gen
from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.websocket import websocket_connect

from devices.generators.adiclockevals.control_service.commands import Command, CommandError
from devices.generators.adiclockevals.control_service.config_store import ConfigStore
from devices.generators.adiclockevals.control_service.device_manager import FakeDeviceManager
from devices.generators.adiclockevals.control_service.hub import ControlHub
from devices.generators.adiclockevals.control_service.web import build_app

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))


class BlockingDM(FakeDeviceManager):
    def __init__(self, device_ids=("0443",)):
        super().__init__(device_ids=device_ids)
        self.entered = threading.Event()
        self.release = threading.Event()

    def apply_parameters(self, device_id, updates):
        self.entered.set()
        self.release.wait(5)
        return super().apply_parameters(device_id, updates)


class FailingDM(FakeDeviceManager):
    def apply_parameters(self, device_id, updates):
        raise RuntimeError("USB pipe broken")


class WebTestBase(AsyncHTTPTestCase):
    def get_app(self):
        self._tmpdir = tempfile.mkdtemp(prefix="cfgtest_")
        self.hub = ControlHub(FakeDeviceManager(device_ids=("0443", "0422")))
        self.hub.start()
        self.store = ConfigStore(self._tmpdir)
        return build_app(self.hub, self.store, STATIC_DIR)

    def tearDown(self):
        if hasattr(self, "hub"):
            self.hub.stop()
        super().tearDown()

    def get(self, path, **kw):
        return self.fetch(path, method="GET", **kw)

    def post_json(self, path, body, **kw):
        return self.fetch(path, method="POST", body=json.dumps(body), **kw)


class TestStateAPI(WebTestBase):
    def test_get_state(self):
        r = self.get("/api/state")
        assert r.code == 200
        snap = json.loads(r.body)
        assert snap["status"] == "ready"
        assert set(snap["devices"]) == {"0443", "0422"}
        assert snap["revision"] == 0


class TestConfigsAPI(WebTestBase):
    def test_list_empty(self):
        r = self.get("/api/configs")
        assert r.code == 200
        assert json.loads(r.body) == {"configs": []}

    def test_list_after_save(self):
        r = self.post_json("/api/configs", {"name": "cfg-a"})
        assert r.code == 200, r.body
        r = self.get("/api/configs")
        assert json.loads(r.body) == {"configs": ["cfg-a"]}

    def test_select_loads_config(self):
        self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                payload={"name": "first"}))
        self.post_json("/api/configs", {"name": "cfg-a"})
        r = self.post_json("/api/config/select", {"name": "cfg-a"})
        assert r.code == 200, r.body
        assert json.loads(r.body)["active_config"] == "cfg-a"
        # 保存的是 applied state：设备参数被完整写入配置
        path = os.path.join(self.store.directory, "cfg-a.json")
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["device_info"]["0443"]["name"] == "first"
        assert cfg["format_version"] == 1

    def test_select_missing_config_400(self):
        r = self.post_json("/api/config/select", {"name": "nope"})
        assert r.code == 400

    def test_select_bad_body_400(self):
        r = self.post_json("/api/config/select", {"nope": 1})
        assert r.code == 400
        r = self.fetch("/api/config/select", method="POST", body="not json")
        assert r.code == 400

    def test_save_exists_409_then_overwrite(self):
        assert self.post_json("/api/configs", {"name": "cfg-a"}).code == 200
        r = self.post_json("/api/configs", {"name": "cfg-a"})
        assert r.code == 409
        body = json.loads(r.body)
        assert body["code"] == "exists"
        # 二次确认覆盖
        r = self.post_json("/api/configs", {"name": "cfg-a", "overwrite": True})
        assert r.code == 200

    def test_save_invalid_name_400(self):
        for bad in ["../x", "a/b", "", ".hidden"]:
            r = self.post_json("/api/configs", {"name": bad})
            assert r.code == 400, bad

    def test_save_does_not_change_state_or_revision(self):
        before = self.hub.get_state_snapshot()
        r = self.post_json("/api/configs", {"name": "cfg-x"})
        assert r.code == 200
        after = self.hub.get_state_snapshot()
        # 保存配置不改变设备状态与 revision
        assert after["revision"] == before["revision"] == 0
        assert after["devices"] == before["devices"]
        assert after["status"] == "ready"


class TestSaveDuringApplying(WebTestBase):
    def get_app(self):
        self._tmpdir = tempfile.mkdtemp(prefix="cfgtest_")
        self.hub = ControlHub(BlockingDM(device_ids=("0443",)), queue_maxsize=4)
        self.hub.start()
        self.store = ConfigStore(self._tmpdir)
        return build_app(self.hub, self.store, STATIC_DIR)

    def test_save_rejected_while_applying(self):
        import time
        t = threading.Thread(target=lambda: self.hub.submit(
            Command(op="apply_parameters", device_id="0443",
                    payload={"name": "slow"})))
        t.start()
        assert self.hub._dm.entered.wait(2)
        r = self.post_json("/api/configs", {"name": "during-applying"})
        assert r.code == 409, r.body
        body = json.loads(r.body)
        assert body["code"] == "fault"
        self.hub._dm.release.set()
        t.join(5)


class TestParametersAPI(WebTestBase):
    def test_batch_apply_success(self):
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0,
            "parameters": {"name": "web", "active_profile": 1},
        })
        assert r.code == 200, r.body
        body = json.loads(r.body)
        assert body["ok"] is True
        snap = self.hub.get_state_snapshot()
        assert snap["devices"]["0443"]["name"] == "web"
        assert snap["revision"] == 1

    def test_batch_conflict_409(self):
        # 先产生一个 revision
        self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                payload={"name": "a"}))
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0,  # 过期基准
            "parameters": {"name": "b"},
        })
        assert r.code == 409
        body = json.loads(r.body)
        assert body["code"] == "conflict"
        # 未强制时不应提交
        assert self.hub.get_state_snapshot()["devices"]["0443"]["name"] == "a"

    def test_batch_force_overrides_conflict(self):
        self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                payload={"name": "a"}))
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0,
            "force": True,
            "parameters": {"name": "forced"},
        })
        assert r.code == 200
        assert self.hub.get_state_snapshot()["devices"]["0443"]["name"] == "forced"

    def test_batch_missing_base_revision_400(self):
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "parameters": {"name": "x"},
        })
        assert r.code == 400

    def test_batch_invalid_parameters_400(self):
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0,
            "parameters": {"active_profile": 99},
        })
        assert r.code == 400
        body = json.loads(r.body)
        assert body["code"] == "invalid_parameter"

    def test_batch_unknown_device_400(self):
        r = self.post_json("/api/devices/9999/parameters:batch", {
            "baseRevision": 0,
            "parameters": {"name": "x"},
        })
        assert r.code == 400


class TestOutputsClearAPI(WebTestBase):
    def test_clear_outputs(self):
        self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                payload={"profiles": [{"freq": 1.0, "amp": 0.5}] + [None] * 7}))
        r = self.post_json("/api/devices/0443/outputs:clear", {})
        assert r.code == 200, r.body
        snap = self.hub.get_state_snapshot()
        assert all(p["amp"] == 0.0 for p in snap["devices"]["0443"]["profiles"])
        assert snap["devices"]["0443"]["profiles"][0]["freq"] == 1.0


class TestWebSocket(WebTestBase):
    def ws_url(self, path="/ws"):
        return self.get_url(path).replace("http", "ws", 1)

    @gen_test
    def test_ws_initial_snapshot_and_updates(self):
        ws = yield websocket_connect(self.ws_url())
        raw1 = yield ws.read_message()
        msg1 = json.loads(raw1)
        assert msg1["revision"] == 0
        # 提交一条命令，应收到推送
        self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                payload={"name": "ws-push"}))
        raw2 = yield ws.read_message()
        msg2 = json.loads(raw2)
        assert msg2["revision"] == 1
        assert msg2["devices"]["0443"]["name"] == "ws-push"
        ws.close()


class TestStaticFiles(WebTestBase):
    def test_index_served(self):
        r = self.fetch("/")
        assert r.code == 200
        assert b"AD9910" in r.body

    def test_static_assets_served(self):
        r = self.fetch("/static/app.js")
        assert r.code == 200
        assert b"connectWS" in r.body
        r = self.fetch("/static/style.css")
        assert r.code == 200
        assert len(r.body) > 100

    def test_index_references_assets(self):
        r = self.fetch("/")
        assert b"/static/app.js" in r.body
        assert b"/static/style.css" in r.body
        
    def test_active_profile_options_present(self):
        r = self.fetch("/")
        assert r.code == 200
        for profile in range(8):
            option = ('<option value="%d">%d</option>' %
                      (profile, profile)).encode("ascii")
            assert option in r.body


class TestBusy503(WebTestBase):
    def get_app(self):
        self._tmpdir = tempfile.mkdtemp(prefix="cfgtest_")
        self.hub = ControlHub(BlockingDM(), queue_maxsize=2)
        self.hub.start()
        self.store = ConfigStore(self._tmpdir)
        return build_app(self.hub, self.store, STATIC_DIR)

    def test_queue_full_returns_503(self):
        # 慢命令占住 worker
        t1 = threading.Thread(target=lambda: self.hub.submit(
            Command(op="apply_parameters", device_id="0443",
                    payload={"name": "slow"})))
        t1.start()
        assert self.hub._dm.entered.wait(2)
        # 两条排满队列（容量 2）
        t2 = threading.Thread(target=lambda: self.hub.submit(
            Command(op="apply_parameters", device_id="0443",
                    payload={"name": "q2"})))
        t3 = threading.Thread(target=lambda: self.hub.submit(
            Command(op="apply_parameters", device_id="0443",
                    payload={"name": "q3"})))
        t2.start()
        t3.start()
        import time
        time.sleep(0.2)
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0, "parameters": {"name": "overflow"}})
        assert r.code == 503, r.body
        body = json.loads(r.body)
        assert body["code"] == "busy"
        self.hub._dm.release.set()
        for t in (t1, t2, t3):
            t.join(5)


class TestFault409(WebTestBase):
    def get_app(self):
        self._tmpdir = tempfile.mkdtemp(prefix="cfgtest_")
        self.hub = ControlHub(FailingDM(device_ids=("0443",)))
        self.hub.start()
        self.store = ConfigStore(self._tmpdir)
        return build_app(self.hub, self.store, STATIC_DIR)

    def _trigger_fault(self):
        try:
            self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                    payload={"name": "boom"}))
        except CommandError as e:
            assert e.code == "hardware_fault"
        else:
            raise AssertionError("应进入 fault")

    def test_write_rejected_409_when_fault(self):
        self._trigger_fault()
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0, "parameters": {"name": "x"}})
        assert r.code == 409, r.body
        body = json.loads(r.body)
        assert body["code"] == "fault"

    def test_save_config_rejected_409_when_fault(self):
        self._trigger_fault()
        r = self.post_json("/api/configs", {"name": "x"})
        assert r.code == 409, r.body
        assert json.loads(r.body)["code"] == "fault"

    def test_state_still_readable_in_fault(self):
        self._trigger_fault()
        r = self.get("/api/state")
        assert r.code == 200
        snap = json.loads(r.body)
        assert snap["status"] == "fault"
        assert snap["state_valid"] is False
