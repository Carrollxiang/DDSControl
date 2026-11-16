# -*- coding: utf-8 -*-
"""
app 主入口辅助函数测试：resolve_bind_host、port_in_use、find_port_owner、check_ports。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_app.py -v
"""

import socket
import threading

import pytest

from devices.generators.adiclockevals.control_service.app import (
    build_parser,
    check_ports,
    find_port_owner,
    port_in_use,
    resolve_bind_host,
)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestResolveBindHost:
    def test_explicit_host_kept(self):
        assert resolve_bind_host("192.168.1.20") == "192.168.1.20"
        assert resolve_bind_host("127.0.0.1") == "127.0.0.1"

    def test_wildcard_resolved_to_lan_ip(self):
        ip = resolve_bind_host("0.0.0.0")
        assert ip != "0.0.0.0"
        parts = ip.split(".")
        assert len(parts) == 4 and all(p.isdigit() for p in parts)


class TestPortInUse:
    def test_free_port_reported_free(self):
        port = free_port()
        assert port_in_use(port) is False

    def test_bound_port_reported_in_use(self):
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            # 服务以 0.0.0.0 监听，检测同样用 0.0.0.0
            assert port_in_use(port) is True
        finally:
            s.close()
        # 关闭后释放
        assert port_in_use(port) is False


class TestFindPortOwner:
    def test_returns_process_name_and_pid(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            owner = find_port_owner(port)
            assert owner is not None, "应能找到占用进程"
            assert "PID" in owner
            # 占用进程就是当前 python 测试进程
            assert str(__import__("os").getpid()) in owner
        finally:
            s.close()

    def test_free_port_returns_none(self):
        port = free_port()
        assert find_port_owner(port) is None


class TestCheckPorts:
    def test_free_ports_pass(self):
        # 不抛异常
        check_ports({"Web": ("0.0.0.0", free_port()),
                     "RPyC": ("0.0.0.0", free_port())})

    def test_occupied_port_raises_system_exit(self):
        s = socket.socket()
        s.bind(("0.0.0.0", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            with pytest.raises(SystemExit) as ei:
                check_ports({"Web": ("0.0.0.0", port)})
            assert ei.value.code == 1
        finally:
            s.close()


class TestClockArguments:
    """--refclk / --divider-disable：对应官方软件 External Clock / Divider Disable，
    SYSCLK 由二者推导（默认 1000 MHz + ÷2 -> 500 MHz）。"""

    def test_defaults(self):
        args = build_parser().parse_args(["--config-dir", "."])
        assert args.refclk == 1000.0
        assert args.divider_disable is False

    def test_accepts_positive_refclk(self):
        args = build_parser().parse_args(["--config-dir", ".", "--refclk", "500"])
        assert args.refclk == 500.0

    def test_divider_disable_flag(self):
        args = build_parser().parse_args(
            ["--config-dir", ".", "--refclk", "1000", "--divider-disable"])
        assert args.divider_disable is True

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", "nan", "inf"])
    def test_rejects_invalid_refclk(self, bad):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--config-dir", ".", "--refclk", bad])
