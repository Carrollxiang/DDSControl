# -*- coding: utf-8 -*-
"""
ini → JSON 导入工具测试：解析、mW→分数转换、π 相位、设备块边界。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_import_ini.py -v
"""

import math
import os

from devices.generators.adiclockevals.control_service.config_store import (
    ConfigStore,
    validate_config,
)
from devices.generators.adiclockevals.control_service.import_ini import (
    import_ini_file,
    parse_ini_text,
)

SAMPLE = """2024-04-19 19:34:47

AD9910_0443:        780_Pushout
Active Profile:     1
Enable Profile ASF: True
Enable Sine Out:    False
Profile 0:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 1:          218.0000001 MHz, 0.00000 π, 0.040004883 mW
Profile 2:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 3:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 4:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 5:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 6:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 7:            0.0000000 MHz, 0.00000 π, 0.000000000 mW

AD9910_0422:        780_2DMOT_Cooling
Active Profile:     1
Enable Profile ASF: True
Enable Sine Out:    False
Profile 0:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 1:          214.6999999 MHz, 0.00000 π, 0.055966437 mW
Profile 2:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 3:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 4:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 5:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 6:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
Profile 7:            0.0000000 MHz, 0.00000 π, 0.000000000 mW
"""


class TestParseIniText:
    def test_basic_structure(self):
        cfg = parse_ini_text(SAMPLE, name="test")
        assert cfg["name"] == "test"
        assert cfg["format_version"] == 1
        assert set(cfg["device_info"]) == {"0443", "0422"}

    def test_device_fields(self):
        dev = parse_ini_text(SAMPLE)["device_info"]["0443"]
        assert dev["name"] == "780_Pushout"
        assert dev["active_profile"] == 1
        assert dev["enable_profile_asf"] is True
        assert dev["enable_sine_out"] is False
        assert len(dev["profiles"]) == 8

    def test_freq_and_phase_kept(self):
        dev = parse_ini_text(SAMPLE)["device_info"]["0443"]
        assert abs(dev["profiles"][1]["freq"] - 218.0000001) < 1e-9
        assert dev["profiles"][1]["phase"] == 0.0  # pi 单位保持

    def test_power_mw_to_fraction_sqrt(self):
        dev = parse_ini_text(SAMPLE)["device_info"]["0443"]
        # 0.040004883 mW -> sqrt = 0.2000122...
        assert abs(dev["profiles"][1]["amp"] - math.sqrt(0.040004883)) < 1e-9
        # 0 mW -> 0
        assert dev["profiles"][0]["amp"] == 0.0

    def test_empty_and_timestamp_lines_ignored(self):
        cfg = parse_ini_text("\n2024-01-01 00:00:00\n\n\n" + SAMPLE)
        assert set(cfg["device_info"]) == {"0443", "0422"}

    def test_exportable_roundtrip(self):
        # 导入结果必须能通过 schema 校验并存回 ConfigStore
        cfg = parse_ini_text(SAMPLE, name="roundtrip")
        validate_config(cfg)
        import tempfile
        store = ConfigStore(tempfile.mkdtemp())
        store.save(cfg)
        loaded = store.load("roundtrip")
        assert loaded["device_info"]["0422"]["profiles"][1]["freq"] == 214.6999999


class TestImportIniFile:
    def test_imports_real_file_with_default_name(self):
        path = os.path.join(os.path.dirname(__file__), "..", "..",
                            "config", "2024-04-19-193447_AD9910_Monitor_10Gs.ini")
        cfg = import_ini_file(path)
        assert cfg["name"] == "AD9910_Monitor_10Gs"  # 去掉时间戳前缀
        assert "0443" in cfg["device_info"]
        validate_config(cfg)

    def test_missing_file(self):
        try:
            import_ini_file("no_such_file.ini")
        except Exception as e:
            assert getattr(e, "code", None) == "not_found"
        else:
            raise AssertionError("应报 not_found")
