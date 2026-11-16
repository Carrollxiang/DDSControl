# -*- coding: utf-8 -*-
"""
AD9910 统一控制服务主入口。

用法：
    python -m devices.generators.adiclockevals.control_service.app \
        --config-dir devices/generators/adiclockevals/config \
        [--web-host 127.0.0.1] [--web-port 8080] \
        [--rpyc-host 0.0.0.0] [--rpyc-port 3251] \
        [--config <启动加载的配置名>] \
        [--refclk 1000] [--divider-disable] \
        [--exit-behavior keep|clear] [--fault-action keep|clear] \
        [--device-manager real|fake] [--fake-devices 0443,0422]

退出行为默认 keep（服务退出保持当前输出，不复位硬件）；
故障行为默认 keep（只标记 fault，不自动清输出）。

时钟参数与 ADI 官方评估软件的三项设置一一对应：
    External Clock   --refclk 1000          （外部参考时钟 REF_CLK，J1 SMA）
    Divider Disable  --divider-disable      （勾选则旁路 REFCLK ÷2 分频器）
    System Clock     由上面两项推导：SYSCLK = REF_CLK / (2 if ÷2 使能 else 1)
本实验室：--refclk 1000、不勾 Divider Disable -> SYSCLK = 500 MHz。
所有 FTW 都按 SYSCLK 计算，填错会让输出频率成比例偏离（2 倍则输出减半）。
"""

from __future__ import annotations

import argparse
import locale
import logging
import math
import os
import signal
import socket
import subprocess
from typing import Any, Dict, Optional, Tuple

from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop

from .commands import Command, CommandError
from .config_store import ConfigStore
from .device_manager import AD9910DeviceManager, FakeDeviceManager
from .hub import ControlHub
from .rpyc_service import start_rpyc_server
from .web import build_app

logger = logging.getLogger("ad9910.control_service")

STATIC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))


def resolve_bind_host(host: str) -> str:
    """把 0.0.0.0 / :: 解析为局域网 IP，便于网页展示可达地址。

    监听仍使用传入的 host；此函数仅用于状态展示。
    """
    if host not in ("0.0.0.0", "::"):
        return host
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))  # UDP connect 不实际发包，仅用于选路
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and ip != "0.0.0.0":
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """探测 TCP 端口是否已被占用（bind 测试，不设置 SO_REUSEADDR）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def find_port_owner(port: int) -> Optional[str]:
    """查找占用指定端口的进程，返回 '进程名 (PID x)'；找不到返回 None。

    Windows 使用 netstat -ano + tasklist；其他平台尝试 lsof/ss。
    """
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        enc = locale.getpreferredencoding(False) or "gbk"
        try:
            proc = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                encoding=enc, errors="replace", creationflags=flags)
            out = proc.stdout or ""
        except OSError:
            return None
        pid = None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(":%d" % port) \
                    and parts[-2] == "LISTENING":
                pid = parts[-1]
                break
        if not pid:
            return None
        try:
            proc2 = subprocess.run(
                ["tasklist", "/FI", "PID eq %s" % pid, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding=enc,
                errors="replace", creationflags=flags)
            out2 = proc2.stdout or ""
            name = out2.split('","')[0].strip('"') if out2.strip() else ""
        except OSError:
            name = ""
        return "%s (PID %s)" % (name or "未知进程", pid)
    # 非 Windows：优先 lsof，其次 ss
    for argv in (["lsof", "-i", "TCP:%d" % port],
                 ["ss", "-ltnp", "sport = :%d" % port]):
        try:
            out = subprocess.run(argv, capture_output=True, text=True).stdout
        except OSError:
            continue
        if out.strip():
            lines = [l for l in out.splitlines() if "LISTEN" in l]
            if lines:
                return lines[0].strip()
    return None


def check_ports(ports: Dict[str, Tuple[str, int]]) -> None:
    """启动前检查端口占用；被占用则报错并退出（含占用进程信息）。"""
    for name, (host, port) in ports.items():
        if port_in_use(port, host):
            owner = find_port_owner(port)
            extra = "（占用进程：%s）" % owner if owner else ""
            logger.error("端口 %d（%s）已被占用%s", port, name, extra)
            logger.error("请先停止占用进程，或改用 --%s-port 指定其他端口。",
                         name.lower())
            raise SystemExit(1)


def _positive_float(text: str) -> float:
    """argparse 类型：正有限浮点数（用于 SYSCLK 频率）。"""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("必须是数字: %r" % (text,))
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("必须是正数: %r" % (text,))
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ad9910-control-service",
        description="AD9910 统一控制服务（Web + RPyC + ControlHub）")
    p.add_argument("--config-dir", required=True,
                   help="配置目录（只允许读取其直属 .json）")
    p.add_argument("--web-host", default="0.0.0.0",
                   help="Web 监听地址（默认 0.0.0.0 对局域网开放）")
    p.add_argument("--web-port", type=int, default=8080)
    p.add_argument("--rpyc-host", default="0.0.0.0")
    p.add_argument("--rpyc-port", type=int, default=3251)
    p.add_argument("--config", default=None,
                   help="启动时自动加载的配置名（须存在于 --config-dir）")
    p.add_argument("--refclk", type=_positive_float, default=1000.0, metavar="MHz",
                   help="外部参考时钟 REF_CLK（MHz，默认 1000），对应官方评估软件的 "
                        "External Clock（J1 SMA 输入）")
    p.add_argument("--divider-disable", action="store_true",
                   help="旁路 REFCLK ÷2 输入分频器（对应官方软件勾选 Divider Disable）。"
                        "默认不旁路：SYSCLK = REF_CLK / 2 = 500 MHz。"
                        "本机实测旁路后 1 GHz 直入会让 DDS 停止输出，一般不要加")
    p.add_argument("--exit-behavior", choices=("keep", "clear"), default="keep",
                   help="退出时是否清除输出（默认 keep：保持当前输出）")
    p.add_argument("--fault-action", choices=("keep", "clear"), default="keep",
                   help="故障时是否自动清除输出（默认 keep：只标记 fault）")
    p.add_argument("--device-manager", choices=("real", "fake"), default="real",
                   help="real=真实硬件（默认）；fake=内存模拟（演示/测试）")
    p.add_argument("--fake-devices", default="0443,0422",
                   help="fake 模式下模拟的设备 id 列表（逗号分隔）")
    return p


def build_components(args: argparse.Namespace) -> Tuple[ControlHub, ConfigStore]:
    """组装 ConfigStore 与 ControlHub（含启动配置加载），供 main 与测试复用。"""
    store = ConfigStore(args.config_dir)
    refclk = getattr(args, "refclk", None)
    div2 = None if refclk is None else (not getattr(args, "divider_disable", False))
    if args.device_manager == "fake":
        dm: Any = FakeDeviceManager(
            device_ids=[s.strip() for s in args.fake_devices.split(",") if s.strip()])
    else:
        dm = AD9910DeviceManager(refclk_mhz=refclk, div2=div2)
    services = {
        # 展示用可达地址：0.0.0.0 解析为局域网 IP（监听仍用原始 host）
        "web": {"host": resolve_bind_host(args.web_host), "port": args.web_port},
        "rpyc": {"host": resolve_bind_host(args.rpyc_host), "port": args.rpyc_port},
        # 时钟：外部参考 + ÷2 分频器 -> 推导出的 SYSCLK（页面展示，便于发现减半问题）
        "refclk_mhz": refclk,
        "div2": div2,
        "sysclk_mhz": (refclk / (2 if div2 else 1)) if refclk is not None and div2 is not None else None,
    }
    hub = ControlHub(dm, services=services, fault_action=args.fault_action)
    boot_config = store.load(args.config) if args.config else None
    hub.start(boot_config)
    return hub, store


def _clear_all_outputs(hub: ControlHub) -> None:
    snap = hub.get_state_snapshot()
    for sid in snap["devices"]:
        try:
            hub.submit(Command(op="clear_outputs", device_id=sid))
        except CommandError as e:
            logger.warning("退出清除输出失败 device=%s: %s", sid, e.message)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 启动前检查端口占用，被占用则给出占用进程并退出
    check_ports({
        "Web": (args.web_host, args.web_port),
        "RPyC": (args.rpyc_host, args.rpyc_port),
    })

    hub, store = build_components(args)
    clock = hub.get_state_snapshot()["services"]
    logger.info("DDS 时钟: REF_CLK=%s MHz, divider=%s -> SYSCLK=%s MHz",
                clock.get("refclk_mhz"),
                "bypassed" if clock.get("div2") is False else "÷2 enabled",
                clock.get("sysclk_mhz"))
    logger.info("设备初始化完成，状态: %s",
                hub.get_state_snapshot()["status"])

    rpyc_server = start_rpyc_server(hub, host=args.rpyc_host, port=args.rpyc_port)
    web_app = build_app(hub, store, STATIC_DIR)
    http_server = HTTPServer(web_app)
    http_server.listen(args.web_port, address=args.web_host)
    logger.info("Web 服务启动于 http://%s:%d/", args.web_host, args.web_port)

    loop = IOLoop.current()

    def shutdown() -> None:
        loop.add_callback(loop.stop)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: shutdown())
        except (ValueError, OSError):
            pass

    try:
        loop.start()
    finally:
        logger.info("正在停止服务…")
        http_server.stop()
        try:
            rpyc_server.close()
        except Exception as e:
            logger.warning("RPyC server 关闭异常: %s", e)
        if args.exit_behavior == "clear":
            _clear_all_outputs(hub)
        hub.stop()
        loop.close()
        logger.info("服务已停止（输出保持当前状态）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
