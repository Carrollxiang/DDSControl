# -*- coding: utf-8 -*-
"""
RPyC 服务集成测试：ThreadedServer 启动、旧接口兼容、
新接口、连接日志。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_rpyc.py -v
"""

import logging
import time

import pytest

from devices.generators.adiclockevals.control_service.commands import Command, CommandError
from devices.generators.adiclockevals.control_service.device_manager import FakeDeviceManager
from devices.generators.adiclockevals.control_service.hub import ControlHub
from devices.generators.adiclockevals.control_service.rpyc_service import (
    AD9910RPyCService,
    start_rpyc_server,
)

CLIENT_CONFIG = {
    "allow_public_attrs": True,
    "allow_pickle": True,
    "allow_all_attrs": True,
}


@pytest.fixture
def rpyc_env():
    """启动 hub + rpyc server，返回 (hub, server, connect 工厂)。"""
    hub = ControlHub(FakeDeviceManager(device_ids=("0443", "0422")))
    hub.start()
    server = start_rpyc_server(hub, host="127.0.0.1", port=33451)
    time.sleep(0.3)

    def connect():
        import rpyc
        return rpyc.connect("127.0.0.1", 33451, config=CLIENT_CONFIG)

    yield hub, server, connect

    server.close()
    hub.stop()


class TestServer:
    def test_server_starts_and_connects(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            assert isinstance(svc, AD9910RPyCService)
        finally:
            conn.close()

    def test_connection_logged(self, rpyc_env, caplog):
        hub, server, connect = rpyc_env
        with caplog.at_level(logging.INFO, logger="devices.generators.adiclockevals.control_service.rpyc_service"):
            conn = connect()
            conn.close()
            time.sleep(0.2)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "RPyC connected peer=127.0.0.1:" in text
        assert "RPyC disconnected peer=127.0.0.1:" in text


class TestLegacyCompat:
    """旧客户端方法：set_frequency/set_amplitude/adjust_amplitude/set_phase/close。"""

    def test_set_frequency(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            r = svc.set_frequency(0x0443, 1, 218.0)
            assert r is True
            snap = hub.get_state_snapshot()
            assert abs(snap["devices"]["0443"]["profiles"][1]["freq"] - 218.0) < 1e-6
            assert snap["revision"] == 1
        finally:
            conn.close()

    def test_set_amplitude_and_phase(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            assert svc.set_amplitude(0x0443, 0, 0.5) is True
            assert svc.set_phase(0x0443, 2, 0.25) is True
            snap = hub.get_state_snapshot()
            assert abs(snap["devices"]["0443"]["profiles"][0]["amp"] - 0.5) < 1e-9
            assert abs(snap["devices"]["0443"]["profiles"][2]["phase"] - 0.25) < 1e-9
        finally:
            conn.close()

    def test_adjust_amplitude_clamps_to_legacy_max(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            svc.set_amplitude(0x0443, 0, 0.4)
            assert svc.adjust_amplitude(0x0443, 0, 1.0) is True
            amp = hub.get_state_snapshot()["devices"]["0443"]["profiles"][0]["amp"]
            assert abs(amp - 0.4472) < 1e-3  # clamp 到 sqrt(0.2)
        finally:
            conn.close()

    def test_adjust_amplitude_accepts_numpy_delta(self, rpyc_env):
        """功率反馈程序常传 numpy.float64；必须提交成功并更新状态。

        回归：delta 未做 float() 转换时，new_amp 为 numpy.float64，
        会在 validate_updates 的 isinstance 校验失败，静默返回 False。
        """
        np = pytest.importorskip("numpy")
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            assert svc.set_amplitude(0x0443, 0, 0.30) is True
            assert svc.adjust_amplitude(0x0443, 0, np.float64(-0.05)) is True
            snap = hub.get_state_snapshot()
            assert abs(snap["devices"]["0443"]["profiles"][0]["amp"] - 0.25) < 1e-9
            assert snap["revision"] == 2
        finally:
            conn.close()

    def test_invalid_device_returns_false(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            assert svc.set_frequency(0x9999, 0, 10.0) is False
        finally:
            conn.close()

    def test_close_returns_true(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            assert svc.close() is True
        finally:
            conn.close()

    def test_out_of_range_profile_returns_false(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            assert svc.set_frequency(0x0443, 9, 100.0) is False
            assert svc.set_amplitude(0x0443, -1, 0.5) is False
            assert svc.set_phase(0x0443, 8, 0.1) is False
            assert svc.adjust_amplitude(0x0443, 9, 0.1) is False
            # 状态未被修改
            assert hub.get_state_snapshot()["revision"] == 0
        finally:
            conn.close()


class TestNewApi:
    def test_get_state_snapshot(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            snap = svc.get_state_snapshot()
            assert set(snap["devices"]) == {"0443", "0422"}
            assert snap["status"] == "ready"
        finally:
            conn.close()

    def test_get_device_info(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            info = svc.get_device_info("0443")
            assert info is not None and "profiles" in info
            assert svc.get_device_info("9999") is None
        finally:
            conn.close()

    def test_update_parameters_commits(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            r = svc.update_parameters("0443", {"name": "via-rpyc", "active_profile": 3})
            assert r["ok"] is True
            snap = hub.get_state_snapshot()
            assert snap["devices"]["0443"]["name"] == "via-rpyc"
            assert snap["devices"]["0443"]["active_profile"] == 3
        finally:
            conn.close()

    def test_update_parameters_propagates_error(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            with pytest.raises(Exception):  # CommandError 经 rpyc 传播
                svc.update_parameters("0443", {"active_profile": 9})
        finally:
            conn.close()

    def test_update_parameters_batch(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            results = svc.update_parameters_batch({
                "0443": {"name": "a"},
                "0422": {"name": "b"},
                "9999": {"name": "ghost"},
            })
            assert results["0443"]["ok"] is True
            assert results["0422"]["ok"] is True
            assert results["9999"]["ok"] is False
        finally:
            conn.close()

    def test_update_parameters_batch_invalid_id_does_not_crash(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            results = svc.update_parameters_batch({
                "0443": {"name": "a"},
                "not-an-id!!": {"name": "b"},
                "0422": {"name": "c"},
            })
            assert results["0443"]["ok"] is True
            assert results["0422"]["ok"] is True
            assert results["not-an-id!!"]["ok"] is False
        finally:
            conn.close()

    def test_load_config(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            cfg = {"name": "rpyc-cfg",
                   "device_info": {"0443": {"name": "from-cfg"}}}
            r = svc.load_config(cfg)
            assert r["active_config"] == "rpyc-cfg"
            snap = hub.get_state_snapshot()
            assert snap["active_config"] == "rpyc-cfg"
            assert snap["devices"]["0443"]["name"] == "from-cfg"
        finally:
            conn.close()

    def test_clear_outputs(self, rpyc_env):
        hub, server, connect = rpyc_env
        conn = connect()
        try:
            svc = conn.root.get_ad9910_service()
            svc.update_parameters("0443", {"profiles": [{"freq": 1.0, "amp": 0.3}] + [None] * 7})
            svc.clear_outputs("0443")
            snap = hub.get_state_snapshot()
            assert all(p["amp"] == 0.0 for p in snap["devices"]["0443"]["profiles"])
            assert snap["devices"]["0443"]["profiles"][0]["freq"] == 1.0
        finally:
            conn.close()
