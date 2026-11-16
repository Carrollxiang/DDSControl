# -*- coding: utf-8 -*-
"""
旧 ini 配置 -> 新 JSON 配置 的一次性导入工具。

ini 格式（历史遗留，见 config/*.ini）：
    <时间戳>

    AD9910_0443:        780_Pushout
    Active Profile:     1
    Enable Profile ASF: True
    Enable Sine Out:    False
    Profile 0:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
    ...
    Profile 7:            ...

转换约定（与统一参数模型对齐）：
- device_id 使用小写 hex（0443）
- freq 保持 MHz
- phase 保持 pi 单位（ini 中即为 pi）
- amp：ini 存的是功率 mW（= 幅度分数^2），导入时取 sqrt 转回满量程分数
- name 取自设备块标题行

用法：
    from .import_ini import import_ini_file, parse_ini_text
    config = import_ini_file("config/2024-04-19-193447_AD9910_Monitor_10Gs.ini")
    store.save(config)  # 或先改名再保存
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List

from .commands import CommandError
from .config_store import FORMAT_VERSION

_DEV_RE = re.compile(r"^AD9910_([0-9A-Fa-f]{4}):\s*(.*)$")
_PROF_RE = re.compile(
    r"^Profile\s+(\d):\s*([0-9.]+)\s*MHz,\s*([0-9.]+)\s*π,\s*([0-9.]+)\s*mW")
_ACTIVE_RE = re.compile(r"^Active Profile:\s*(\d+)$")
_ASF_RE = re.compile(r"^Enable Profile ASF:\s*(True|False)$")
_SINE_RE = re.compile(r"^Enable Sine Out:\s*(True|False)$")


def _parse_bool(s: str) -> bool:
    return s.strip().lower() == "true"


def parse_ini_text(text: str, name: str = "imported") -> Dict[str, Any]:
    """解析 ini 文本为 JSON 配置 dict。"""
    blocks: List[List[str]] = [[]]
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip():
            if blocks[-1]:
                blocks.append([])
            continue
        blocks[-1].append(line)
    blocks = [b for b in blocks if b]

    device_info: Dict[str, Any] = {}
    for block in blocks:
        if not block:
            continue
        m = _DEV_RE.match(block[0])
        if not m:
            continue  # 时间戳行等
        sid = m.group(1).lower()
        dev: Dict[str, Any] = {"name": m.group(2).strip()}
        profiles: List[Any] = [None] * 8
        for line in block[1:]:
            am = _ACTIVE_RE.match(line)
            if am:
                dev["active_profile"] = int(am.group(1))
                continue
            fm = _ASF_RE.match(line)
            if fm:
                dev["enable_profile_asf"] = _parse_bool(fm.group(1))
                continue
            sm = _SINE_RE.match(line)
            if sm:
                dev["enable_sine_out"] = _parse_bool(sm.group(1))
                continue
            pm = _PROF_RE.match(line)
            if pm:
                idx = int(pm.group(1))
                try:
                    freq = float(pm.group(2))
                    phase = float(pm.group(3))
                    power_mw = float(pm.group(4))
                except ValueError:
                    raise CommandError(
                        "ini 行数值格式非法: %r" % line.strip(), "invalid_parameter")
                profiles[idx] = {
                    "freq": freq,
                    "phase": phase,
                    "amp": math.sqrt(power_mw),
                }
        dev["profiles"] = profiles
        device_info[sid] = dev

    return {
        "format_version": FORMAT_VERSION,
        "name": name,
        "device_info": device_info,
    }


def import_ini_file(path: str, name: str = "") -> Dict[str, Any]:
    """从 ini 文件导入；name 缺省时用文件主名（不含时间戳前缀）。

    编码：优先 UTF-8，失败回退 GBK（历史 ini 多为中文 Windows 生成）。
    """
    text = None
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise CommandError("无法读取 ini 文件 %s: %s" % (path, e), "not_found")
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise CommandError("无法解码 ini 文件 %s" % path, "invalid_parameter")
    if not name:
        import os
        stem = os.path.splitext(os.path.basename(path))[0]
        # 去掉前缀时间戳（2024-04-19-193447_）
        m = re.match(r"^\d{4}-\d{2}-\d{2}-\d{6}_(.*)$", stem)
        name = m.group(1) if m else stem
    return parse_ini_text(text, name=name)
