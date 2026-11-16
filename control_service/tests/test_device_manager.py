# -*- coding: utf-8 -*-
"""
设备适配层单元测试：normalize_device_id、validate_updates、
FakeDeviceManager 全流程、AD9910DeviceManager 的 wrapper 调用序列。

运行：
    python -m pytest devices/generators/adiclockevals/control_service/tests/test_device_manager.py -v
"""

import pytest

from devices.generators.adiclockevals.control_service.commands import CommandError
from devices.generators.adiclockevals.control_service.device_manager import (
    PROFILE_COUNT,
    AD9910DeviceManager,
    FakeDeviceManager,
    normalize_device_id,
    validate_updates,
)


# ---------------------------------------------------------------- normalize

class TestNormalizeDeviceId:
    @pytest.mark.parametrize("given,expect", [
        (0x0443, "0443"),
        (2593, "0a21"),
        ("0443", "0443"),
        ("0A21", "0a21"),
        ("0x0443", "0443"),
        ("0X0A21", "0a21"),
    ])
    def test_ok(self, given, expect):
        assert normalize_device_id(given) == expect

    @pytest.mark.parametrize("bad", ["", "zz", "04 43", None, 3.14, ["0443"]])
    def test_invalid(self, bad):
        with pytest.raises(CommandError) as ei:
            normalize_device_id(bad)
        assert ei.value.code == "invalid_parameter"


# ---------------------------------------------------------------- validate_updates

class TestValidateUpdates:
    def test_empty_updates_ok(self):
        validate_updates({})

    def test_unknown_key_rejected(self):
        with pytest.raises(CommandError) as ei:
            validate_updates({"bogus": 1})
        assert ei.value.code == "invalid_parameter"

    @pytest.mark.parametrize("bad", [-1, 8, True, "1", 1.5])
    def test_active_profile_bad(self, bad):
        with pytest.raises(CommandError):
            validate_updates({"active_profile": bad})

    @pytest.mark.parametrize("good", [0, 7])
    def test_active_profile_ok(self, good):
        validate_updates({"active_profile": good})

    def test_enable_flags_must_be_bool(self):
        with pytest.raises(CommandError):
            validate_updates({"enable_profile_asf": 1})
        with pytest.raises(CommandError):
            validate_updates({"enable_sine_out": "yes"})

    def test_name_must_be_str(self):
        with pytest.raises(CommandError):
            validate_updates({"name": 123})
        validate_updates({"name": "780_Pushout"})

    def test_profiles_wrong_length(self):
        with pytest.raises(CommandError):
            validate_updates({"profiles": [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [None] * 9})

    def test_profile_field_ranges(self):
        ok = {"profiles": [None] * 8}
        ok["profiles"][0] = {"freq": 0.0, "amp": 0.0, "phase": -100.0}
        validate_updates(ok)  # freq=0 / amp=0 / phase 任意实数均可

        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"freq": -0.1}] + [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"amp": 1.01}] + [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"amp": -0.1}] + [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"freq": "200"}] + [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"phase": None}] + [None] * 7})

    def test_profile_unknown_field(self):
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"magic": 1}] + [None] * 7})

    def test_amp_upper_bound_ok(self):
        validate_updates({"profiles": [{"amp": 1.0}] + [None] * 7})

    @pytest.mark.parametrize("nanish", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_rejected(self, nanish):
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"freq": nanish}] + [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"amp": nanish}] + [None] * 7})
        with pytest.raises(CommandError):
            validate_updates({"profiles": [{"phase": nanish}] + [None] * 7})


# ---------------------------------------------------------------- FakeDeviceManager

class TestFakeDeviceManager:
    def make(self, ids=("0443", "0422")):
        return FakeDeviceManager(device_ids=ids)

    def test_start_populates_devices(self):
        m = self.make()
        state = m.start()
        assert set(state) == {"0443", "0422"}
        assert m.op_log[0][0] == "start"

    def test_start_with_config(self):
        cfg = {"device_info": {"0443": {"name": "780_Pushout",
                                        "profiles": [{"freq": 218.0}] + [None] * 7}}}
        state = FakeDeviceManager(device_ids=("0443",)).start(cfg)
        assert state["0443"]["name"] == "780_Pushout"
        assert state["0443"]["profiles"][0]["freq"] == 218.0
        assert len(state["0443"]["profiles"]) == 8

    def test_apply_partial_profile_keeps_others(self):
        m = self.make(ids=("0443",))
        m.start()
        m.apply_parameters("0443", {"profiles": [{"amp": 0.5}] + [None] * 7})
        s = m.get_applied_state()["0443"]
        assert s["profiles"][0]["amp"] == 0.5
        assert s["profiles"][0]["freq"] == 0.0  # 未编辑字段保留
        assert s["profiles"][1] == {"freq": 0.0, "phase": 0.0, "amp": 0.0}

    def test_apply_full_fields(self):
        m = self.make(ids=("0443",))
        m.start()
        r = m.apply_parameters("0443", {
            "name": "A", "active_profile": 3,
            "enable_profile_asf": True, "enable_sine_out": True,
            "profiles": [{"freq": 1.0, "phase": 0.5, "amp": 0.1}] + [None] * 7})
        assert r["active_profile"] == 3 and r["name"] == "A"
        assert r["profiles"][0] == {"freq": 1.0, "phase": 0.5, "amp": 0.1}

    def test_clear_outputs_zeroes_amp_keeps_freq(self):
        m = self.make(ids=("0443",))
        m.start()
        m.apply_parameters("0443", {"profiles": [{"freq": 218.0, "amp": 0.3}] + [None] * 7})
        m.clear_outputs("0443")
        s = m.get_applied_state()["0443"]
        assert all(p["amp"] == 0.0 for p in s["profiles"])
        assert s["profiles"][0]["freq"] == 218.0

    def test_load_config_applies_all(self):
        m = self.make(ids=("0443",))
        m.start()
        cfg = {"device_info": {"0443": {
            "name": "B", "active_profile": 5,
            "profiles": [{"freq": 10.0}] + [None] * 7}}}
        m.load_config(cfg)
        s = m.get_applied_state()["0443"]
        assert s["name"] == "B" and s["active_profile"] == 5
        assert s["profiles"][0]["freq"] == 10.0

    def test_load_config_skips_missing_device(self):
        m = self.make(ids=("0443",))
        m.start()
        m.load_config({"device_info": {"9999": {"name": "ghost"}}})  # 不抛错

    def test_unknown_device_rejected(self):
        m = self.make(ids=("0443",))
        m.start()
        with pytest.raises(CommandError) as ei:
            m.apply_parameters("9999", {"active_profile": 0})
        assert ei.value.code == "not_found"

    def test_invalid_updates_not_committed(self):
        m = self.make(ids=("0443",))
        m.start()
        with pytest.raises(CommandError):
            m.apply_parameters("0443", {"active_profile": 9})
        # 校验失败不改变状态（不提交）
        assert m.get_applied_state()["0443"]["active_profile"] == 0

    def test_snapshot_is_deep_copy(self):
        m = self.make(ids=("0443",))
        m.start()
        s = m.get_applied_state()
        s["0443"]["name"] = "HACK"
        assert m.get_applied_state()["0443"]["name"] == ""

    def test_int_device_id_accepted(self):
        m = self.make(ids=("0443",))
        m.start()
        m.apply_parameters(0x0443, {"name": "via-int"})
        assert m.get_applied_state()["0443"]["name"] == "via-int"

    def test_op_log_records_operations(self):
        m = self.make(ids=("0443",))
        m.start()
        m.apply_parameters("0443", {"name": "A"})
        m.clear_outputs("0443")
        m.load_config({"device_info": {"0443": {"name": "B"}}})
        m.close()
        ops = [o[0] for o in m.op_log]
        assert ops == ["start", "apply", "clear", "load_config", "close"]
        assert m.op_log[1][1] == "0443"
        assert m.op_log[1][2] == {"name": "A"}

    def test_start_rejects_oversized_profiles(self):
        m = self.make(ids=("0443",))
        bad = {"device_info": {"0443": {"profiles": [None] * 9}}}
        with pytest.raises(CommandError) as ei:
            m.start(bad)
        assert ei.value.code == "invalid_parameter"


# ---------------------------------------------------------------- AD9910DeviceManager（注入假 wrapper）

class FakeProfile:
    """假 profile：value 为编码后的寄存器值，解码复用真实静态方法。"""

    def __init__(self, value):
        self._value = value

    @property
    def value(self):
        return self._value

    def decode_single_tone(self, val, f_sysclk):
        from devices.generators.adiclockevals.ad9910 import AD9910Profile
        return AD9910Profile.decode_single_tone(val, f_sysclk)


class FakeAD9910:
    _registry = {}

    @classmethod
    def get(cls, model, sn):
        return cls._registry[sn]

    def __init__(self, sn, name="", status=(1000.0, False, False, False),
                 port_e=0x90, profile_values=None):
        self.name = name
        self._status = status
        self._port_e = port_e
        values = profile_values or [0] * 8
        self._profiles = [FakeProfile(v) for v in values]
        FakeAD9910._registry[sn] = self

    def __iter__(self):
        return iter(self._profiles)

    @property
    def status(self):
        return self._status

    def get_port_value(self, port):
        assert port == 4
        return self._port_e


class FakeWrapper:
    """假 wrapper：记录调用，供断言调用序列。"""

    def __init__(self, sns=(0x0443, 0x0422)):
        self.sns = list(sns)
        self.calls = []
        for sn in sns:
            FakeAD9910(sn)

    def configure_clock(self, refclk_mhz=None, div2=None):
        self.calls.append(("configure_clock", refclk_mhz, div2))

    def apply_clock(self, sn):
        self.calls.append(("apply_clock", sn))
        return 0x1F3F4000

    def inits(self):
        self.calls.append("inits")
        return list(self.sns)

    def set_active_profile(self, sn, pf):
        self.calls.append(("active_profile", sn, pf))

    def set_profile_asf(self, sn, b):
        self.calls.append(("set_profile_asf", sn, b))

    def set_sine_out(self, sn, b):
        self.calls.append(("set_sine_out", sn, b))

    def load_single_tone_profile(self, sn, idx, single_tone):
        self.calls.append(("load_single_tone_profile", sn, idx, single_tone))


class TestAD9910DeviceManager:
    def make(self, sns=(0x0443, 0x0422), refclk_mhz=None, div2=None):
        wrapper = FakeWrapper(sns)
        return AD9910DeviceManager(wrapper=wrapper,
                                   ad9910_cls=FakeAD9910,
                                   refclk_mhz=refclk_mhz,
                                   div2=div2), wrapper

    def test_start_enumerates_and_reads_state(self):
        m, w = self.make()
        state = m.start()
        assert w.calls[0] == "inits"
        assert set(state) == {"0443", "0422"}
        # 默认 profile 值 0 -> freq=0, ph=0, amp=0
        assert state["0443"]["profiles"][0] == {"freq": 0.0, "phase": 0.0, "amp": 0.0}
        assert state["0443"]["active_profile"] == 0  # 0x90 & 7 = 0

    def test_clock_forwarded_before_enumeration(self):
        """时钟配置必须在枚举/复位设备之前下发，否则首帧 FTW 会按错误时钟编码。"""
        m, w = self.make(sns=(0x0443,), refclk_mhz=1000.0, div2=True)
        m.start()
        assert w.calls[0] == ("configure_clock", 1000.0, True)
        assert w.calls[1] == "inits"
        # SYSCLK 由 REF_CLK 与 ÷2 推导：1000 / 2 = 500 MHz
        assert m.clock == {"refclk_mhz": 1000.0, "div2": True,
                           "sysclk_mhz": 500.0}

    def test_clock_divider_bypass_derives_full_refclk(self):
        m, w = self.make(sns=(0x0443,), refclk_mhz=1000.0, div2=False)
        m.start()
        assert w.calls[0] == ("configure_clock", 1000.0, False)
        assert m.clock["sysclk_mhz"] == 1000.0

    def test_clock_not_sent_when_unset(self):
        """未指定时钟配置时沿用底层默认值，不应多发命令。"""
        m, w = self.make(sns=(0x0443,))
        m.start()
        assert all(not (isinstance(c, tuple) and c[0] == "configure_clock")
                   for c in w.calls)
        assert m.clock == {"refclk_mhz": None, "div2": None, "sysclk_mhz": None}

    def test_clock_registers_rewritten_for_every_board(self):
        """npy 已存在时 __init__ 会跳过 reset，启动必须逐板重写 CFR3。

        否则芯片可能残留上一次（甚至官方软件）写入的时钟配置，
        表现为某块板无输出（÷2 被旁路，1 GHz 直入停振）或频率翻倍/减半。
        """
        m, w = self.make(sns=(0x0443, 0x4008), refclk_mhz=1000.0, div2=True)
        m.start()
        applied = [c for c in w.calls if c[0] == "apply_clock"]
        assert applied == [("apply_clock", 0x0443), ("apply_clock", 0x4008)]

    def test_clock_rewritten_before_profile_writes(self):
        """时钟必须早于 profile 下发，保证首帧就在正确的 SYSCLK 下工作。"""
        m, w = self.make(sns=(0x0443,), refclk_mhz=1000.0, div2=True)
        cfg = {"device_info": {"0443": {"profiles": [{"freq": 42.0}] + [None] * 7}}}
        m.start(cfg)
        ops = [c[0] if isinstance(c, tuple) else c for c in w.calls]
        assert ops.index("apply_clock") < ops.index("load_single_tone_profile")

    def test_apply_parameters_write_sequence(self):
        m, w = self.make(sns=(0x0443,))
        m.start()
        w.calls.clear()
        m.apply_parameters("0443", {
            "active_profile": 2,
            "enable_profile_asf": True,
            "profiles": [{"freq": 200.0, "phase": 0.25, "amp": 0.5}] + [None] * 7})
        ops = [c[0] for c in w.calls]
        assert ops == ["active_profile", "set_profile_asf",
                       "load_single_tone_profile"]
        # profile 写入：合并后全量下发（freq/phase/amp）
        _, sn, idx, st = w.calls[-1]
        assert (sn, idx) == (0x0443, 0)
        freq, ph, amp = st
        assert abs(freq - 200.0) < 1e-6 and abs(ph - 0.25) < 1e-9
        assert abs(amp - 0.5) < 1e-9

    def test_partial_profile_merges_current_values(self):
        m, w = self.make(sns=(0x0443,))
        m.start()
        # 预置 profile0 = 200MHz/0.25pi/0.5amp
        m.apply_parameters("0443", {"profiles": [
            {"freq": 200.0, "phase": 0.25, "amp": 0.5}] + [None] * 7})
        w.calls.clear()
        # 只改 amp，freq/phase 应保留合并后下发
        m.apply_parameters("0443", {"profiles": [{"amp": 0.2}] + [None] * 7})
        _, _, _, st = w.calls[-1]
        assert abs(st[0] - 200.0) < 1e-6  # freq 保留
        assert abs(st[2] - 0.2) < 1e-9   # amp 更新

    def test_clear_outputs_writes_all_profiles_amp_zero(self):
        m, w = self.make(sns=(0x0443,))
        m.start()
        w.calls.clear()
        m.clear_outputs("0443")
        ops = [c for c in w.calls if c[0] == "load_single_tone_profile"]
        assert len(ops) == 8
        for _, sn, idx, st in ops:
            assert st[2] == 0.0  # amp 全 0
        assert sorted(idx for *_, idx, _ in [o[1:] for o in ops]) == list(range(8))

    def test_load_config_applies_each_device(self):
        m, w = self.make(sns=(0x0443, 0x0422))
        m.start()
        w.calls.clear()
        cfg = {"device_info": {"0443": {"name": "A",
                                        "profiles": [{"freq": 1.0}] + [None] * 7}}}
        m.load_config(cfg)
        assert ("load_single_tone_profile", 0x0443, 0, (1.0, 0.0, 0.0)) in w.calls
        # 未在配置中的设备不被写入
        assert not any(c[1] == 0x0422 for c in w.calls)

    def test_close_does_not_reset_hardware(self):
        m, w = self.make(sns=(0x0443,))
        m.start()
        w.calls.clear()
        m.close()
        assert w.calls == []  # close 不动硬件（保持输出）

    def test_load_config_unknown_device_skipped(self):
        m, w = self.make(sns=(0x0443,))
        m.start()
        m.load_config({"device_info": {"9999": {"name": "x"}}})  # 不抛错

    def test_start_with_config_applies_after_enumerate(self):
        m, w = self.make(sns=(0x0443,))
        cfg = {"device_info": {"0443": {"name": "boot",
                                        "profiles": [{"freq": 42.0}] + [None] * 7}}}
        state = m.start(cfg)
        assert state["0443"]["name"] == "boot"
        assert state["0443"]["profiles"][0]["freq"] == 42.0
