# -*- coding: utf-8 -*-
"""
ConfigStore 单元测试：路径安全（穿越/绝对路径/符号链接）、schema 校验、
原子写与覆盖、列表。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_config_store.py -v
"""

import json
import os

import pytest

from devices.generators.adiclockevals.control_service.commands import CommandError
from devices.generators.adiclockevals.control_service.config_store import (
    FORMAT_VERSION,
    ConfigStore,
    validate_config,
)


@pytest.fixture
def store(tmp_path):
    return ConfigStore(str(tmp_path))


def make_config(name="cfg-a", device=None):
    return {
        "format_version": FORMAT_VERSION,
        "name": name,
        "device_info": {"0443": device or {"name": "780_Pushout"}},
    }


# ---------------- 路径安全 ----------------

class TestResolveSecurity:
    @pytest.mark.parametrize("bad", [
        "../x", "a/b", "a\\b", "", " ", ".", "..", ".hidden", "x/y/z",
        os.path.abspath("outside"),
    ])
    def test_rejects_unsafe_names(self, store, bad):
        with pytest.raises(CommandError) as ei:
            store.resolve(bad)
        assert ei.value.code == "invalid_parameter"

    def test_resolve_ok(self, store):
        p = store.resolve("cfg-a")
        assert os.path.dirname(p) == store.directory
        assert p.endswith("cfg-a.json")

    def test_rejects_symlinked_config_file(self, store, tmp_path):
        target = tmp_path / "real.json"
        target.write_text("{}")
        link = tmp_path / "link.json"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境无法创建符号链接")
        with pytest.raises(CommandError) as ei:
            store.resolve("link")
        assert ei.value.code == "invalid_parameter"

    def test_rejects_symlinked_config_dir(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "linkdir"
        try:
            os.symlink(real_dir, link_dir)
        except (OSError, NotImplementedError):
            pytest.skip("当前环境无法创建符号链接")
        with pytest.raises(CommandError):
            ConfigStore(str(link_dir)).resolve("a")


# ---------------- schema 校验 ----------------

class TestValidateConfig:
    def test_valid_config(self):
        validate_config(make_config())

    def test_requires_name(self):
        with pytest.raises(CommandError):
            validate_config({"device_info": {}})

    def test_bad_format_version(self):
        with pytest.raises(CommandError):
            validate_config({**make_config(), "format_version": 2})

    def test_device_info_must_be_object(self):
        with pytest.raises(CommandError):
            validate_config({**make_config(), "device_info": []})

    def test_device_config_must_be_object(self):
        with pytest.raises(CommandError):
            validate_config({"name": "x", "device_info": {"0443": "not-obj"}})

    def test_profiles_length_must_be_8(self):
        with pytest.raises(CommandError):
            validate_config({"name": "x",
                             "device_info": {"0443": {"profiles": [None] * 3}}})

    def test_missing_format_version_ok(self):
        validate_config({"name": "x", "device_info": {}})


# ---------------- 保存 / 加载 / 列表 ----------------

class TestSaveLoad:
    def test_save_then_load_roundtrip(self, store):
        cfg = make_config()
        path = store.save(cfg)
        assert os.path.exists(path)
        loaded = store.load("cfg-a")
        assert loaded == cfg
        # 无临时文件残留
        leftovers = [f for f in os.listdir(store.directory) if f.endswith(".tmp")]
        assert leftovers == []

    def test_save_exists_rejected_then_overwrite(self, store):
        store.save(make_config())
        with pytest.raises(CommandError) as ei:
            store.save(make_config())
        assert ei.value.code == "exists"
        # 覆盖后内容更新
        store.save(make_config(name="cfg-a", device={"name": "NEW"}), overwrite=True)
        assert store.load("cfg-a")["device_info"]["0443"]["name"] == "NEW"

    def test_load_missing(self, store):
        with pytest.raises(CommandError) as ei:
            store.load("nope")
        assert ei.value.code == "not_found"

    def test_load_invalid_json(self, store, tmp_path):
        (tmp_path / "bad.json").write_text("{not json")
        with pytest.raises(CommandError) as ei:
            store.load("bad")
        assert ei.value.code == "invalid_parameter"

    def test_list_only_direct_json(self, store, tmp_path):
        store.save(make_config("a"))
        store.save(make_config("b"))
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.json").write_text("{}")
        (tmp_path / "d.txt").write_text("x")
        assert store.list() == ["a", "b"]

    def test_make_from_state(self, store):
        devices = {"0443": {"name": "X",
                            "profiles": [{"freq": 0.0, "phase": 0.0, "amp": 0.0}] * 8}}
        cfg = store.make_from_state("saved", devices)
        assert cfg["name"] == "saved"
        assert cfg["format_version"] == FORMAT_VERSION
        assert cfg["device_info"] == devices
        validate_config(cfg)

    def test_save_utf8_content(self, store):
        cfg = make_config(device={"name": "780_推走"})
        store.save(cfg)
        with open(os.path.join(store.directory, "cfg-a.json"),
                  encoding="utf-8") as f:
            raw = json.load(f)
        assert raw["device_info"]["0443"]["name"] == "780_推走"
