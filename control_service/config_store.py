# -*- coding: utf-8 -*-
"""
配置管理：JSON 配置读写。

约束：
- 配置文件必须位于 --config-dir 直属目录下，仅 *.json
- 禁止绝对路径与 ../ 路径穿越、禁止符号链接
- 保存采用临时文件 + 原子替换；已存在默认拒绝（CommandError('exists') → 409），
  二次确认（overwrite=True）后覆盖
- 加载前做 schema 校验
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional

from .commands import CommandError

FORMAT_VERSION = 1


def validate_config(config: Dict[str, Any]) -> None:
    """配置 schema 校验；失败抛 CommandError('invalid_parameter')。"""
    if not isinstance(config, dict):
        raise CommandError("配置必须是 JSON 对象", "invalid_parameter")
    if "format_version" in config and config["format_version"] != FORMAT_VERSION:
        raise CommandError("不支持的 format_version: %r" % config["format_version"],
                           "invalid_parameter")
    name = config.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CommandError("配置缺少合法的 name", "invalid_parameter")
    info = config.get("device_info", {})
    if not isinstance(info, dict):
        raise CommandError("device_info 必须是对象", "invalid_parameter")
    for sid, dev in info.items():
        if not isinstance(dev, dict):
            raise CommandError("设备 %s 的配置必须是对象" % sid, "invalid_parameter")
        for key in ("name", "active_profile", "enable_profile_asf", "enable_sine_out"):
            if key in dev and key != "name" and not isinstance(dev[key], (bool, int)):
                raise CommandError("设备 %s 的 %s 类型错误" % (sid, key),
                                   "invalid_parameter")
        profs = dev.get("profiles")
        if profs is not None:
            if not isinstance(profs, list) or len(profs) != 8:
                raise CommandError("设备 %s 的 profiles 必须为长度 8 的数组" % sid,
                                   "invalid_parameter")
            for p in profs:
                if p is not None and not isinstance(p, dict):
                    raise CommandError("设备 %s 的 profile 必须是对象" % sid,
                                       "invalid_parameter")


class ConfigStore:
    """config-dir 直属 JSON 配置文件的读写。"""

    def __init__(self, config_dir: str) -> None:
        self._dir = os.path.abspath(config_dir)
        os.makedirs(self._dir, exist_ok=True)

    @property
    def directory(self) -> str:
        return self._dir

    def list(self) -> List[str]:
        """返回全部直属 .json 配置名（不含扩展名，按名称排序）。"""
        names = []
        try:
            entries = os.listdir(self._dir)
        except OSError:
            return names
        for entry in entries:
            if entry.endswith(".json") and os.path.isfile(
                    os.path.join(self._dir, entry)):
                names.append(entry[:-len(".json")])
        return sorted(names)

    def resolve(self, name: str) -> str:
        """校验配置名并返回安全的绝对路径；非法抛 CommandError。"""
        if not isinstance(name, str) or not name.strip():
            raise CommandError("配置名不能为空", "invalid_parameter")
        if os.path.sep in name or (os.path.altsep and os.path.altsep in name):
            raise CommandError("配置名不能包含路径分隔符", "invalid_parameter")
        if name in (".", "..") or name.startswith("."):
            raise CommandError("配置名非法", "invalid_parameter")
        path = os.path.join(self._dir, name + ".json")
        if os.path.isabs(path) and os.path.dirname(path) != self._dir:
            raise CommandError("配置路径越界", "invalid_parameter")
        if os.path.islink(path):
            raise CommandError("禁止符号链接配置", "invalid_parameter")
        # 目录本身也不允许符号链接
        if os.path.islink(self._dir):
            raise CommandError("配置目录不允许是符号链接", "invalid_parameter")
        return path

    def load(self, name: str) -> Dict[str, Any]:
        """读取并校验配置。"""
        path = self.resolve(name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except FileNotFoundError:
            raise CommandError("配置不存在: %s" % name, "not_found")
        except json.JSONDecodeError as e:
            raise CommandError("配置 JSON 解析失败: %s" % e, "invalid_parameter")
        validate_config(config)
        return config

    def save(self, config: Dict[str, Any], overwrite: bool = False) -> str:
        """保存配置（原子写）；已存在且未确认覆盖抛 CommandError('exists')。"""
        validate_config(config)
        name = config["name"]
        path = self.resolve(name)
        if os.path.exists(path) and not overwrite:
            raise CommandError("配置文件已存在: %s" % name, "exists")
        payload = json.dumps(config, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(
            dir=self._dir, prefix=".%s." % name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return path

    def make_from_state(self, name: str, devices: Dict[str, Any]) -> Dict[str, Any]:
        """用 applied state 的设备字典组装待保存配置（保存的是 applied state）。"""
        if not isinstance(name, str) or not name.strip():
            raise CommandError("配置名不能为空", "invalid_parameter")
        return {
            "format_version": FORMAT_VERSION,
            "name": name,
            "device_info": devices,
        }
