# -*- coding: utf-8 -*-
"""
端到端集成测试：Web HTTP + WebSocket + RPyC 三个入口共用同一 ControlHub，
验证跨接口状态一致性与完整业务流程（保存配置→修改→加载）。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_e2e.py -v
"""

import json
import os
import tempfile
import time

import rpyc
from tornado.testing import AsyncHTTPTestCase

from devices.generators.adiclockevals.control_service.commands import Command
from devices.generators.adiclockevals.control_service.config_store import ConfigStore
from devices.generators.adiclockevals.control_service.device_manager import FakeDeviceManager
from devices.generators.adiclockevals.control_service.hub import ControlHub
from devices.generators.adiclockevals.control_service.rpyc_service import start_rpyc_server
from devices.generators.adiclockevals.control_service.web import build_app

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
RPYC_PORT = 33551

CLIENT_CONFIG = {
    "allow_public_attrs": True,
    "allow_pickle": True,
    "allow_all_attrs": True,
}


class EndToEndTest(AsyncHTTPTestCase):
    def get_app(self):
        self._tmpdir = tempfile.mkdtemp(prefix="e2e_")
        self.hub = ControlHub(FakeDeviceManager(device_ids=("0443", "0422")))
        self.hub.start()
        self.store = ConfigStore(self._tmpdir)
        self._rpyc = start_rpyc_server(self.hub, host="127.0.0.1", port=RPYC_PORT)
        time.sleep(0.3)
        return build_app(self.hub, self.store, STATIC_DIR)

    def tearDown(self):
        if hasattr(self, "_rpyc"):
            self._rpyc.close()
        if hasattr(self, "hub"):
            self.hub.stop()
        super().tearDown()

    def rpyc(self):
        conn = rpyc.connect("127.0.0.1", RPYC_PORT, config=CLIENT_CONFIG)
        return conn, conn.root.get_ad9910_service()

    def post_json(self, path, body):
        return self.fetch(path, method="POST", body=json.dumps(body))

    def get_json(self, path):
        r = self.fetch(path, method="GET")
        assert r.code == 200, r.body
        return json.loads(r.body)

    # ---------------- 业务流程 ----------------

    def test_rpyc_change_visible_via_http(self):
        conn, svc = self.rpyc()
        assert svc.set_frequency(0x0443, 2, 150.0) is True
        conn.close()
        snap = self.get_json("/api/state")
        assert snap["devices"]["0443"]["profiles"][2]["freq"] == 150.0
        assert snap["revision"] == 1

    def test_save_config_then_rpyc_load(self):
        # 先通过 RPyC 修改
        conn, svc = self.rpyc()
        svc.update_parameters("0443", {"name": "RPC_SET"})
        conn.close()
        # 网页保存 applied state 为配置（保存不改变 active_config 与状态）
        r = self.post_json("/api/configs", {"name": "e2e-cfg"})
        assert r.code == 200
        conn, svc = self.rpyc()
        snap = svc.get_state_snapshot()
        assert snap["active_config"] is None
        assert snap["devices"]["0443"]["name"] == "RPC_SET"
        # 配置文件内容正确
        cfg = self.store.load("e2e-cfg")
        assert cfg["device_info"]["0443"]["name"] == "RPC_SET"
        # RPyC 加载配置后才生效
        assert svc.load_config(cfg)["active_config"] == "e2e-cfg"
        assert svc.get_state_snapshot()["active_config"] == "e2e-cfg"
        conn.close()

    def test_rpyc_load_config_updates_http_state(self):
        # 准备配置
        self.hub.submit(Command(op="apply_parameters", device_id="0443",
                                payload={"name": "A"}))
        self.post_json("/api/configs", {"name": "cfg2"})
        # 修改后通过 RPyC 重新加载配置
        conn, svc = self.rpyc()
        svc.update_parameters("0443", {"name": "MUTATED"})
        conn.close()
        conn, svc = self.rpyc()
        svc.load_config(self.store.load("cfg2"))
        conn.close()
        # HTTP 状态反映配置加载结果
        snap = self.get_json("/api/state")
        assert snap["devices"]["0443"]["name"] == "A"  # 配置中的值
        assert snap["active_config"] == "cfg2"

    def test_cross_interface_revision_conflict(self):
        # RPyC 先修改（revision=1）
        conn, svc = self.rpyc()
        svc.update_parameters("0443", {"name": "rpyc"})
        conn.close()
        # 页面用旧 baseRevision 提交 → 409
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0, "parameters": {"name": "page"}})
        assert r.code == 409
        body = json.loads(r.body)
        assert body["code"] == "conflict"
        # 强制覆盖成功
        r = self.post_json("/api/devices/0443/parameters:batch", {
            "baseRevision": 0, "force": True, "parameters": {"name": "page"}})
        assert r.code == 200
        snap = self.get_json("/api/state")
        assert snap["devices"]["0443"]["name"] == "page"

    def test_config_list_via_http_after_save(self):
        self.post_json("/api/configs", {"name": "c1"})
        self.post_json("/api/configs", {"name": "c2"})
        assert self.get_json("/api/configs")["configs"] == ["c1", "c2"]

    def test_full_workflow(self):
        # 1. 保存初始状态为配置
        assert self.post_json("/api/configs", {"name": "golden"}).code == 200
        # 2. RPyC 修改参数
        conn, svc = self.rpyc()
        assert svc.set_amplitude(0x0443, 0, 0.5) is True
        conn.close()
        # 3. 网页看到新状态
        snap = self.get_json("/api/state")
        assert snap["devices"]["0443"]["profiles"][0]["amp"] == 0.5
        # 4. 重新加载 golden 配置，恢复
        assert self.post_json("/api/config/select", {"name": "golden"}).code == 200
        snap = self.get_json("/api/state")
        assert snap["devices"]["0443"]["profiles"][0]["amp"] == 0.0
        assert snap["active_config"] == "golden"
        # 5. 清除输出
        assert self.post_json("/api/devices/0443/outputs:clear", {}).code == 200
        snap = self.get_json("/api/state")
        assert all(p["amp"] == 0.0 for p in snap["devices"]["0443"]["profiles"])
