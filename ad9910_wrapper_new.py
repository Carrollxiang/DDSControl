# -*- coding: utf-8 -*-
"""
Created on 2020-05-13

@author: 张宽
"""
from threading import Lock
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "../../..")))
from devices.generators.adiclockevals.ad9910 import AD9910
sys.path.pop()

_device_locks = {}  # {serial_number: Lock}

def get_path():
    return AD9910.get_path(AD9910.config_dir, 'ini', '%Y-%m-%d-%H%M%S', '%s')

def load_config(path):
    """
    从字符串加载设备的配置。
        path - 配置文件的绝对路径 (str类)
    """
    AD9910.load_config(path)

def save_config(path):
    """
    保存设备的当前配置到配置文件。
        path - 配置文件的绝对路径 (str类)
    """
    return AD9910.save_config(path)


def inits():
    """
    系统位04: 780nm激光系统
        子系统位    1: 780移频光路     2: 2DMOT光路       3: 3DMOT光路       4: 780探测光路
        编号位      1: Repumper        2: Cooling
    系统位07: 红阱系统
        子系统位    2: 808阵列光路
        编号位      0: 微系综          1: SLM             2: 寻址
    系统位0A: 474nm激光系统
        子系统位    2: 474X向全局光路      4: 474寻址光路
        编号位      0: 共振          1: 激发
    """
    AD9910.inits()

    return [int(ad9910.serial_number, 16) for ad9910 in AD9910.gets()]

def deletes():
    AD9910.deletes()

def get_name(sn):
    """
    获取AD9910的名称
        sn - 设备id (int类)
    返回
        name - AD9910的名称
    """
    ad9910 = AD9910.get('AD9910', sn)
    return ad9910.name

def read_status(sn):
    """
    读取AD9910信息
        sn - 设备id (int类)
    返回
        status = enable_profile_asf | enable_sine_out << 1 | enable_ram << 2
            enable_profile_asf - 使能单音profiles调制幅度位 (bool类)
            enable_sine_out - 选择DDS正弦波输出位 (bool类)
            enable_ram - RAM使能位 (bool类)
    """
    ad9910 = AD9910.get('AD9910', sn)
    status = ad9910.status
    f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = status
    status = enable_profile_asf | enable_sine_out << 1 | enable_ram << 2
    return status

def read_single_tone_profiles(sn):
    """
    读取所有profile
        sn - 设备id (int类)
    返回
        activate - 当前激活的profile寄存器编号
        pfs = [(freq, ph, amp), ...]
            freq - 频率以MHz为单位 (float类)
            ph - 相位以pi为单位 (float类)
            amp - 振幅以满量程分数表示 (float类)
    """
    if sn not in _device_locks:
        _device_locks[sn] = Lock()

    with _device_locks[sn]:  # 确保同一设备操作串行化
        ad9910 = AD9910.get('AD9910', sn)
        f_sysclk = ad9910.sysclk_frequency
        port_e = ad9910.get_port_value(4)
        # value = 0xF8 & port_e
        reg = 0x0E
        pfs = []
        for pf in ad9910:
            # ad9910.set_port_value(4, value)
            val = ad9910.spi_read(reg)
            pfs.append(pf.decode_single_tone(val, f_sysclk))
            # value += 1
            reg += 1
        # ad9910.set_port_value(4, port_e)
        activate = 7 & port_e
        return activate, pfs

def set_name(sn, name):
    """
    设置AD9910的名称
        sn - 设备id (int类)
        name - AD9910的名称
    """
    ad9910 = AD9910.get('AD9910', sn)
    ad9910.name = name

def set_active_profile(sn, pf):
    """
    设置激活的profile
        sn - 设备id (int类)
        pf - profile编号 (int类)
    """
    ad9910 = AD9910.get('AD9910', sn)
    value = 0xF8 & ad9910.get_port_value(4)
    value |= 7 & pf
    ad9910.set_port_value(4, value)

def set_profile_asf(sn, b):
    """
    设置使能单音profiles调制幅度位
        sn - 设备id (int类)
        b - 位值 (bool类)
    """
    ad9910 = AD9910.get('AD9910', sn)
    ad9910.set_register_bit(1, 0x18, b)

def set_sine_out(sn, b):
    """
    设置选择DDS正弦波输出位
        sn - 设备id (int类)
        b - 位值 (bool类)
    """
    ad9910 = AD9910.get('AD9910', sn)
    ad9910.set_register_bit(0, 0x10, b)

def set_ram(sn, b):
    """
    设置RAM使能位
        sn - 设备id (int类)
        b - 位值 (bool类)
    """
    ad9910 = AD9910.get('AD9910', sn)
    ad9910.set_register_bit(0, 0x1F, b)



def load_single_tone_profile(sn, pf, single_tone):
    """
    加载profile
        sn - 设备id (int类)
        pf - profile编号 (int类)
        single_tone = (freq, ph, amp)
            freq - 频率以MHz为单位 (float类)
            ph - 相位以pi为单位 (float类)
            amp - 振幅以满量程分数表示 (float类)
    """
    if sn not in _device_locks:
        _device_locks[sn] = Lock()

    with _device_locks[sn]:  # 确保同一设备操作串行化
        ad9910 = AD9910.get('AD9910', sn)
        f_sysclk = ad9910.sysclk_frequency
        port_e = ad9910.get_port_value(4)
        pf &= 7
        value = 0xF8 & port_e
        value |= pf
        ad9910.set_port_value(4, value)
        reg = pf + 0x0E
        val = ad9910[pf].encode_single_tone(*single_tone, f_sysclk)
        ad9910.spi_write(reg, val)
        ad9910.set_port_value(4, port_e)
        ad9910.set_port_value(4, 0x10 ^ port_e)
        ad9910.set_port_value(4, port_e)
