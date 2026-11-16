# -*- coding: utf-8 -*-
"""
设备适配层：统一硬件访问接口。

DeviceManager 是 ControlHub 唯一允许访问的设备入口（在 worker 线程内调用）。
这一层封装 ad9910_wrapper 的编码/下发逻辑，不暴露寄存器、USB packet 等细节。

约定：
- device_id 为小写 hex 字符串（如 '0443'），接口同时接受 int（如 0x443）。
- 参数以语义单位给出：freq MHz、phase pi、amp 满量程分数（0~1）。
- 每个设备的状态结构：
    {
      "name": str,
      "active_profile": int(0-7),
      "enable_profile_asf": bool,
      "enable_sine_out": bool,
      "profiles": [{"freq": float, "phase": float, "amp": float}, ... 共8项]
    }
- clear_outputs 将所有 profile 的 amp 置 0（保留 freq/phase，便于恢复）。
- close() 不复位硬件（服务退出保持当前输出）。
"""

from __future__ import annotations

import copy
import logging
import math
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .commands import CommandError

logger = logging.getLogger(__name__)

PROFILE_COUNT = 8

HEX_CHARS = set("0123456789abcdef")


def normalize_device_id(device_id: Any) -> str:
    """把 int / hex 字符串归一化为小写 hex（如 '0443'）。"""
    if isinstance(device_id, int):
        return "%04x" % device_id
    s = str(device_id).strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    if not s or not all(c in HEX_CHARS for c in s):
        raise CommandError("非法设备id: %r" % (device_id,), "invalid_parameter")
    return s


def _is_number(x: Any) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def validate_updates(updates: Dict[str, Any]) -> None:
    """校验参数更新；失败抛 CommandError('invalid_parameter')。"""
    if not isinstance(updates, dict):
        raise CommandError("updates 必须是对象", "invalid_parameter")
    allowed = {"name", "active_profile", "enable_profile_asf",
               "enable_sine_out", "profiles"}
    unknown = set(updates) - allowed
    if unknown:
        raise CommandError("未知参数: %s" % sorted(unknown), "invalid_parameter")

    if "name" in updates:
        if not isinstance(updates["name"], str):
            raise CommandError("name 必须是字符串", "invalid_parameter")
    if "active_profile" in updates:
        p = updates["active_profile"]
        if isinstance(p, bool) or not isinstance(p, int) or not 0 <= p < PROFILE_COUNT:
            raise CommandError("active_profile 必须是 0~7 的整数", "invalid_parameter")
    for k in ("enable_profile_asf", "enable_sine_out"):
        if k in updates and not isinstance(updates[k], bool):
            raise CommandError("%s 必须是布尔值" % k, "invalid_parameter")

    if "profiles" in updates:
        profiles = updates["profiles"]
        if not isinstance(profiles, list) or len(profiles) != PROFILE_COUNT:
            raise CommandError(
                "profiles 必须是长度为 %d 的数组" % PROFILE_COUNT, "invalid_parameter")
        for i, item in enumerate(profiles):
            if item is None:
                continue
            if not isinstance(item, dict):
                raise CommandError("profiles[%d] 必须是对象或 null" % i, "invalid_parameter")
            bad = set(item) - {"freq", "phase", "amp"}
            if bad:
                raise CommandError(
                    "profiles[%d] 未知字段: %s" % (i, sorted(bad)), "invalid_parameter")
            if "freq" in item:
                if not _is_number(item["freq"]) or item["freq"] < 0:
                    raise CommandError("profiles[%d].freq 必须是非负数值(MHz)" % i,
                                       "invalid_parameter")
            if "amp" in item:
                if not _is_number(item["amp"]) or not 0 <= item["amp"] <= 1:
                    raise CommandError("profiles[%d].amp 必须是 0~1 的数值" % i,
                                       "invalid_parameter")
            if "phase" in item and not _is_number(item["phase"]):
                raise CommandError("profiles[%d].phase 必须是数值(pi)" % i,
                                   "invalid_parameter")


def _merge_profiles(current: List[Dict[str, float]],
                    updates: List[Optional[Dict[str, float]]]) -> List[Dict[str, float]]:
    """把部分 profile 更新合并进当前 profiles，返回新列表。"""
    result = []
    for i, upd in enumerate(updates):
        base = dict(current[i])
        if upd is not None:
            base.update(upd)
        result.append(base)
    return result


class DeviceManager(ABC):
    """设备适配器统一接口。"""

    @abstractmethod
    def start(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """打开并初始化全部设备，应用启动配置，返回设备状态字典。"""

    @abstractmethod
    def apply_parameters(self, device_id: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
        """应用参数更新，返回该设备更新后的完整参数。"""

    @abstractmethod
    def clear_outputs(self, device_id: Any) -> None:
        """清除/关闭输出（所有 profile amp 置 0）。"""

    @abstractmethod
    def load_config(self, config: Dict[str, Any]) -> None:
        """向硬件加载配置（重新下发全部参数）。"""

    @abstractmethod
    def get_applied_state(self) -> Dict[str, Any]:
        """返回全部设备的当前状态字典。"""

    @abstractmethod
    def close(self) -> None:
        """关闭设备连接（不复位输出）。"""


class AD9910DeviceManager(DeviceManager):
    """真实硬件实现：复用 ad9910_wrapper / ad9910 的编码与下发逻辑。

    wrapper / ad9910_cls 支持依赖注入，便于测试。
    refclk_mhz / div2：外部参考时钟与 REFCLK ÷2 分频器状态，与 ADI 官方评估
    软件的 External Clock / Divider Disable 一一对应；SYSCLK 由二者推导
    （SYSCLK = REF_CLK / (2 if div2 else 1)）。None 表示沿用底层驱动默认值。
    """

    def __init__(self, wrapper: Any = None, ad9910_cls: Any = None,
                 refclk_mhz: Optional[float] = None,
                 div2: Optional[bool] = None) -> None:
        if wrapper is None:
            from devices.generators.adiclockevals import ad9910_wrapper as wrapper
        if ad9910_cls is None:
            from devices.generators.adiclockevals.ad9910 import AD9910 as ad9910_cls
        self._wrapper = wrapper
        self._ad9910 = ad9910_cls
        self._refclk_mhz = refclk_mhz
        self._div2 = div2
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._closed = False

    # ---- 内部 ----

    def _apply_clock(self) -> None:
        """把外部参考时钟 / ÷2 分频配置下发给底层驱动（枚举前后均可）。"""
        if self._refclk_mhz is None and self._div2 is None:
            return
        setter = getattr(self._wrapper, "configure_clock", None)
        if setter is None:
            setter = getattr(self._ad9910, "configure_clock", None)
        if setter is None:
            return  # 替身对象没有该接口：保持其默认值
        setter(refclk_mhz=self._refclk_mhz, div2=self._div2)

    @property
    def clock(self) -> Dict[str, Any]:
        """当前时钟配置：{'refclk_mhz', 'div2', 'sysclk_mhz'}（未配置的项为 None）。"""
        refclk = self._refclk_mhz
        div2 = self._div2
        sysclk = None
        if refclk is not None and div2 is not None:
            sysclk = refclk / (2 if div2 else 1)
        return {"refclk_mhz": refclk, "div2": div2, "sysclk_mhz": sysclk}

    def _apply_device_clock(self, sn_int: int, dev: Any) -> None:
        """逐板重写 CFR3 并锁存，保证芯片时钟与软件假设一致。

        寄存器镜像(.npy)已存在时 AD9910.__init__ 会跳过 reset，若不在这里显式
        重写，芯片会保留上一次（甚至官方软件）写入的时钟配置——表现为某块板
        无输出（÷2 被旁路）或频率翻倍/减半。
        """
        fn = getattr(self._wrapper, "apply_clock", None)
        if fn is not None:
            value = fn(sn_int)
        else:
            fn = getattr(dev, "apply_clock", None)
            if fn is None:
                return  # 替身对象没有该接口
            value = fn()
        logger.info("设备 %04x 时钟寄存器已重写: CFR3=%s (div2=%s, SYSCLK=%s MHz)",
                    sn_int, value if value is not None else "n/a",
                    self._div2, self.clock["sysclk_mhz"])

    def _ensure_device(self, sid: str) -> None:
        if self._closed:
            raise CommandError("设备已关闭", "unavailable")
        if sid not in self._devices:
            raise CommandError("设备不存在: %s" % sid, "not_found")

    def _read_device_state(self, dev: Any) -> Dict[str, Any]:
        """从设备（npy 寄存器镜像）读取当前状态，不依赖硬件回读。"""
        f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = dev.status
        active_profile = 7 & dev.get_port_value(4)
        profiles = []
        for pf in dev:
            freq, ph, amp = pf.decode_single_tone(pf.value, f_sysclk)
            profiles.append({"freq": float(freq), "phase": float(ph), "amp": float(amp)})
        return {
            "name": dev.name,
            "active_profile": int(active_profile),
            "enable_profile_asf": bool(enable_profile_asf),
            "enable_sine_out": bool(enable_sine_out),
            "profiles": profiles,
        }

    def _write_profile(self, sn_int: int, idx: int,
                       freq: float, ph: float, amp: float) -> None:
        self._wrapper.load_single_tone_profile(sn_int, idx, (freq, ph, amp))

    # ---- DeviceManager 接口 ----

    def start(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock:
            self._closed = False
            self._apply_clock()    # 外部参考时钟 / ÷2 分频必须先于枚举（reset）
            ids = self._wrapper.inits()  # [int sn]，内部会 reset 并重建注册表
            self._devices = {}
            for sn_int in ids:
                sid = "%04x" % sn_int
                dev = self._ad9910.get("AD9910", sn_int)
                # 逐板重写 CFR3：npy 已存在时 __init__ 不 reset，芯片可能残留旧配置
                self._apply_device_clock(sn_int, dev)
                self._devices[sid] = self._read_device_state(dev)
            if config:
                self.load_config(config)
            return self.get_applied_state()

    def apply_parameters(self, device_id: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            sid = normalize_device_id(device_id)
            self._ensure_device(sid)
            validate_updates(updates)
            sn_int = int(sid, 16)
            dev = self._ad9910.get("AD9910", sn_int)
            state = self._devices[sid]

            if "name" in updates:
                dev.name = updates["name"]
                state["name"] = updates["name"]
            if "active_profile" in updates:
                self._wrapper.set_active_profile(sn_int, updates["active_profile"])
                state["active_profile"] = updates["active_profile"]
            if "enable_profile_asf" in updates:
                self._wrapper.set_profile_asf(sn_int, updates["enable_profile_asf"])
                state["enable_profile_asf"] = bool(updates["enable_profile_asf"])
            if "enable_sine_out" in updates:
                self._wrapper.set_sine_out(sn_int, updates["enable_sine_out"])
                state["enable_sine_out"] = bool(updates["enable_sine_out"])
            if "profiles" in updates:
                merged = _merge_profiles(state["profiles"], updates["profiles"])
                for i, p in enumerate(merged):
                    if updates["profiles"][i] is not None:
                        self._write_profile(sn_int, i, p["freq"], p["phase"], p["amp"])
                state["profiles"] = merged
            return copy.deepcopy(state)

    def clear_outputs(self, device_id: Any) -> None:
        with self._lock:
            sid = normalize_device_id(device_id)
            self._ensure_device(sid)
            sn_int = int(sid, 16)
            state = self._devices[sid]
            merged = _merge_profiles(
                state["profiles"],
                [{"amp": 0.0}] * PROFILE_COUNT)
            for i, p in enumerate(merged):
                self._write_profile(sn_int, i, p["freq"], p["phase"], p["amp"])
            state["profiles"] = merged

    def load_config(self, config: Dict[str, Any]) -> None:
        with self._lock:
            if not isinstance(config, dict):
                raise CommandError("config 必须是对象", "invalid_parameter")
            info = config.get("device_info", {})
            if not isinstance(info, dict):
                raise CommandError("config.device_info 必须是对象", "invalid_parameter")
            for sid, dev_conf in info.items():
                sid = normalize_device_id(sid)
                if sid not in self._devices:
                    continue  # 配置了但未枚举到的设备跳过
                if not isinstance(dev_conf, dict):
                    raise CommandError("设备配置必须是对象: %s" % sid, "invalid_parameter")
                updates: Dict[str, Any] = {"profiles": [None] * PROFILE_COUNT}
                for k in ("name", "active_profile", "enable_profile_asf", "enable_sine_out"):
                    if k in dev_conf:
                        updates[k] = dev_conf[k]
                profs = dev_conf.get("profiles")
                if profs is not None:
                    if not isinstance(profs, list) or len(profs) != PROFILE_COUNT:
                        raise CommandError("设备 %s 的 profiles 长度必须为 %d"
                                           % (sid, PROFILE_COUNT), "invalid_parameter")
                    updates["profiles"] = [
                        dict(p) if isinstance(p, dict) else None for p in profs]
                self.apply_parameters(sid, updates)

    def get_applied_state(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._devices)

    def close(self) -> None:
        """关闭设备连接；按约定不复位硬件，保持当前输出。"""
        with self._lock:
            self._closed = True


class FakeDeviceManager(DeviceManager):
    """内存模拟的设备适配器（测试替身），行为与真实实现一致。"""

    DEFAULT_PROFILE = {"freq": 0.0, "phase": 0.0, "amp": 0.0}

    def __init__(self, device_ids: List[str] = ("0443", "0422")) -> None:
        self._device_ids = list(device_ids)
        self._devices: Dict[str, Dict[str, Any]] = {}
        self.op_log: List[tuple] = []  # (op, device_id, *args)，便于测试断言

    def _ensure_device(self, sid: str) -> None:
        if sid not in self._devices:
            raise CommandError("设备不存在: %s" % sid, "not_found")

    def _init_device(self, sid: str, dev_conf: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        d = dev_conf or {}
        prof_updates = [dict(p) if isinstance(p, dict) else None
                        for p in (d.get("profiles") or [])]
        if len(prof_updates) > PROFILE_COUNT:
            raise CommandError("设备 %s 的 profiles 长度不能超过 %d"
                               % (sid, PROFILE_COUNT), "invalid_parameter")
        prof_updates += [None] * (PROFILE_COUNT - len(prof_updates))
        return {
            "name": d.get("name", ""),
            "active_profile": d.get("active_profile", 0),
            "enable_profile_asf": d.get("enable_profile_asf", False),
            "enable_sine_out": d.get("enable_sine_out", False),
            "profiles": _merge_profiles(
                [dict(self.DEFAULT_PROFILE)] * PROFILE_COUNT, prof_updates),
        }

    def start(self, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        cfg = (config or {}).get("device_info", {})
        self._devices = {
            sid: self._init_device(sid, cfg.get(sid))
            for sid in self._device_ids
        }
        self.op_log.append(("start", list(self._device_ids)))
        return self.get_applied_state()

    def apply_parameters(self, device_id: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
        sid = normalize_device_id(device_id)
        self._ensure_device(sid)
        validate_updates(updates)
        state = self._devices[sid]
        for k in ("name", "active_profile", "enable_profile_asf", "enable_sine_out"):
            if k in updates:
                state[k] = updates[k]
        if "profiles" in updates:
            state["profiles"] = _merge_profiles(state["profiles"], updates["profiles"])
        self.op_log.append(("apply", sid, copy.deepcopy(updates)))
        return self.get_applied_state()[sid]

    def clear_outputs(self, device_id: Any) -> None:
        sid = normalize_device_id(device_id)
        self._ensure_device(sid)
        state = self._devices[sid]
        state["profiles"] = _merge_profiles(
            state["profiles"], [{"amp": 0.0}] * PROFILE_COUNT)
        self.op_log.append(("clear", sid))

    def load_config(self, config: Dict[str, Any]) -> None:
        for sid, dev_conf in (config or {}).get("device_info", {}).items():
            sid = normalize_device_id(sid)
            if sid not in self._devices:
                continue
            d = dev_conf or {}
            state = self._devices[sid]
            for k in ("name", "active_profile", "enable_profile_asf", "enable_sine_out"):
                if k in d:
                    state[k] = d[k]
            if isinstance(d.get("profiles"), list):
                profs = d["profiles"]
                if len(profs) != PROFILE_COUNT:
                    raise CommandError("设备 %s 的 profiles 长度必须为 %d"
                                       % (sid, PROFILE_COUNT), "invalid_parameter")
                state["profiles"] = _merge_profiles(
                    state["profiles"],
                    [dict(p) if isinstance(p, dict) else None for p in profs])
        self.op_log.append(("load_config", copy.deepcopy(config or {})))

    def get_applied_state(self) -> Dict[str, Any]:
        return copy.deepcopy(self._devices)

    def close(self) -> None:
        self.op_log.append(("close",))
