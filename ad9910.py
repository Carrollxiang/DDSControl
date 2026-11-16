# -*- coding: utf-8 -*-
"""
Created on 2022-10-13

@author: 张宽
"""

import os, math, numpy, re
import time

from devices.generators.generator import GeneratorChannel
from devices.generators.adiclockevals.adiclockeval import ADIClockEval
from experiment.signal import Signal, LoopedSignal

# CFR3（寄存器 0x02）的两种常用组合：REFCLK PLL 关闭（VCO SEL=111、PLL enable=0、N=0）、
# ICP=111、分频器 ResetB 正常（bit14=1）。两者只差 bit15（REFCLK ÷2 输入分频器旁路位）。
CFR3_DIV2_ENABLED = 0x1F3F4000   # bit15=0：÷2 使能 -> SYSCLK = REF_CLK / 2（本机 500 MHz）
CFR3_DIV2_BYPASS = 0x1F3FC000    # bit15=1：旁路 ÷2 -> SYSCLK = REF_CLK（1 GHz 直入，本批板停振）

class AD9910Profile(GeneratorChannel):
    """AD9910的profile寄存器"""
    
    @property
    def signal(self):
        status = self.device.status
        f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = status
        val = self.value
        if enable_ram:
            raise ValueError('AD9910的RAM模式尚未开发')
        else:
            sig = self.decode_single_tone_signal(val, f_sysclk,
                                                 enable_profile_asf,
                                                 enable_sine_out)
        return sig
    
    @signal.setter
    def signal(self, sig):
        status = self.device.status
        f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = status
        if enable_ram:
            raise ValueError('AD9910的RAM模式尚未开发')
        else:
            val = self.encode_single_tone_signal(sig, f_sysclk,
                                                 enable_profile_asf,
                                                 enable_sine_out)
        self.value = val
    
    @property
    def value(self):
        ad9910 = self.device
        pf = self.channel
        port_e = ad9910.get_port_value(4)
        value = 0xF8 & port_e
        value |= pf
        ad9910.set_port_value(4, value)
        reg = pf + 0x0E
        val = ad9910.spi_read(reg)
        ad9910.set_port_value(4, port_e)
        return val
    
    @value.setter
    def value(self, val):
        ad9910 = self.device
        pf = self.channel
        port_e = ad9910.get_port_value(4)
        value = 0xF8 & port_e
        value |= pf
        ad9910.set_port_value(4, value)
        # print('%x' % value)
        reg = pf + 0x0E
        ad9910.spi_write(reg, int(val))
        ad9910.set_port_value(4, port_e)
        ad9910.set_port_value(4, 0x10 ^ port_e)
        ad9910.set_port_value(4, port_e)

    @property
    def frequency(self):
        val = self.value
        f_sysclk = self.device.sysclk_frequency
        ftw = 0xFFFFFFFF & val
        return self.decode_frequency(ftw, f_sysclk)
    
    @frequency.setter
    def frequency(self, freq):
        val = 0x3FFFFFFF00000000 & self.value
        f_sysclk = self.device.sysclk_frequency
        ftw = self.encode_frequency(freq, f_sysclk)
        self.value = val | ftw
    
    @property
    def phase(self):
        val = self.value
        pow = 0xFFFF & val >> 0x20
        return self.decode_phase(pow)
    
    @phase.setter
    def phase(self, ph):
        val = 0x3FFF0000FFFFFFFF & self.value
        pow = self.encode_phase(ph)
        self.value = val | pow << 0x20
    
    @property
    def amplitude(self):
        val = self.value
        asf = 0x3FFF & val >> 0x30
        return self.decode_amplitude(asf)
    
    @amplitude.setter
    def amplitude(self, amp):
        val = 0xFFFFFFFFFFFF & self.value
        asf = self.encode_amplitude(amp)
        self.value = val | asf << 0x30
    
    @staticmethod
    def encode_frequency(freq, f_sysclk):
        """
        加密频率
            freq - 频率以MHz为单位 (float类)
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
        返回
            ftw - 频率调谐字 (int类)
        """
        ftw = round(freq / f_sysclk * 0x100000000)
        return 0xFFFFFFFF & ftw
    
    @staticmethod
    def encode_phase(ph):
        """
        加密相位
            ph - 相位以pi为单位 (float类)
        返回
            pow - 相位偏移字 (int类)
        """
        pow = round(ph * 0x8000)
        return 0xFFFF & pow
    
    @staticmethod
    def encode_amplitude(amp):
        """
        加密振幅
            amp - 振幅以满量程分数表示 (float类)
        返回
            asf - 振幅比例因子 (int类)
        """
        asf = round(amp * 0x4000)
        return 0x3FFF & asf
    
    @staticmethod
    def encode_single_tone(freq, ph, amp, f_sysclk):
        """
        加密single tone寄存器
            freq - 频率以MHz为单位 (float类)
            ph - 相位以pi为单位 (float类)
            amp - 振幅以满量程分数表示 (float类)
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
        返回
            val - 寄存器值 (int类)
        """
        ftw = AD9910Profile.encode_frequency(freq, f_sysclk)
        pow = AD9910Profile.encode_phase(ph)
        asf = AD9910Profile.encode_amplitude(amp)
        val = ftw | pow << 0x20 | asf << 0x30
        return val
    
    @staticmethod
    def encode_single_tone_signal(sig, f_sysclk,
                                  enable_profile_asf=False,
                                  enable_sine_out=False):
        """
        加密信号
            sig - 信号 (Signal类)
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
            enable_profile_asf - 是否使能单音profiles调制幅度 (bool类)
            enable_sine_out - 选择DDS正弦波输出位 (bool类)
        返回
            val - 寄存器值 (int类)
        """
        if isinstance(sig, Signal):
            while isinstance(sig, LoopedSignal):
                sig = sig.signal[-1][0]
        else:
            sig = Signal(sig)
        expr = sig.signal.expression
        try:
            if enable_profile_asf and expr[0] == 'multiply':
                amp, expr = expr[1:]
            else:
                amp = 0x3FFF / 0x4000
            func, expr = expr
            if func == 'cos':
                func = False
            elif func == 'sin':
                func = True
            else:
                raise ValueError
            op, var, expr = expr
            if op != 'multiply' or var != 'pi':
                raise ValueError
            op, expr, ph = expr
            if op != 'add':
                raise ValueError
            op, expr, var = expr
            if op != 'multiply':
                raise ValueError
            if not isinstance(var, str):
                raise TypeError
            op, var = var.split('_')
            if op != 't' or not var.isdigit():
                raise ValueError
            op, var, freq = expr
            if op != 'multiply' or var != 2:
                raise ValueError
        except Exception:
            raise ValueError('载入信号未识别')
        ph += 0.5 * (enable_sine_out - func)
        val = AD9910Profile.encode_single_tone(freq, ph, amp, f_sysclk)
        return val
    
    @staticmethod
    def decode_frequency(ftw, f_sysclk):
        """
        解密频率
            ftw - 频率调谐字 (int类)
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
        返回
            freq - 频率以MHz为单位 (float类)
        """
        freq = ftw / 0x100000000
        if freq > 0.5:
            freq = 1 - freq
        freq *= f_sysclk
        return float(freq)
    
    @staticmethod
    def decode_amplitude(asf):
        """
        解密振幅
            asf - 振幅比例因子 (int类)
        返回
            amp - 振幅以满量程分数表示 (float类)
        """
        amp = asf / 0x4000
        return float(amp)
    
    @staticmethod
    def decode_phase(pow):
        """
        解密相位
            pow - 相位偏移字 (int类)
        返回
            ph - 相位以pi为单位 (float类)
        """
        ph = pow / 0x8000
        return float(ph)
    
    @staticmethod
    def decode_single_tone(val, f_sysclk):
        """
        解密single tone寄存器
            val - 寄存器值 (int类)
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
        返回
            freq - 频率以MHz为单位 (float类)
            ph - 相位以pi为单位 (float类)
            amp - 振幅以满量程分数表示 (float类)
        """
        ftw = 0xFFFFFFFF & val
        freq = AD9910Profile.decode_frequency(ftw, f_sysclk)
        val >>= 0x20
        pow = 0xFFFF & val
        ph = AD9910Profile.decode_phase(pow)
        val >>= 0x10
        asf = 0x3FFF & val
        amp = AD9910Profile.decode_amplitude(asf)
        return freq, ph, amp
    
    @staticmethod
    def decode_single_tone_signal(val, f_sysclk,
                                  enable_profile_asf=False,
                                  enable_sine_out=False):
        """
        解密信号
            val - 寄存器值 (int类)
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
            enable_profile_asf - 是否使能单音profiles调制幅度 (bool类)
            enable_sine_out - 是否选择AD9910正弦波输出 (bool类)
        返回
            sig - 信号 (Signal类)
        """
        freq, ph, amp = numpy.array(AD9910Profile.decode_single_tone(val, f_sysclk))
        expr = 'multiply', 'pi', ('add', ('multiply', ('multiply', numpy.int_(2), freq), 't_0'), ph)
        if enable_sine_out:
            func = 'sin'
        else:
            func = 'cos'
        expr = func, expr
        if enable_profile_asf:
            expr = 'multiply', amp, expr
        sig = Signal(expr)
        return sig

class AD9910(ADIClockEval):
    """
    AD9910
    cls.__reg_size[reg] - 寄存器reg的字节数 (int类)
    cls.config_dir - 配置文件路径 (str类)
    self._refclk_mhz - 外部参考时钟 REF_CLK (MHz)
    self._refclk_div2 - REFCLK ÷2 输入分频器是否使能 (bool类)
    """
    _chs = {
        ch: AD9910Profile
        for ch in range(8)} # 重载Device类属性
    config_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'config'))
    __reg_size = [4] * 5 + [0, 0, 4, 2, 4, 4, 8, 8, 4] + [8] * 8 + [4]

    # ---- 时钟配置 ----
    # 所有 FTW 都按内部时钟 (SYSCLK) 计算：
    #     ftw = round(freq / SYSCLK * 2**32)
    # SYSCLK 并不是一个独立参数，而是由"外部参考时钟 + 芯片内分频"决定：
    #     SYSCLK = REF_CLK / (2 if REFCLK ÷2 使能 else 1)   (REFCLK PLL 关闭时)
    # 对应 ADI 官方评估软件的三项设置：
    #     External Clock   -> _refclk_mhz（外部参考时钟，J1 SMA 输入）
    #     Divider Disable  -> 勾选时旁路 ÷2（_refclk_div2 = False）
    #     System Clock     -> 推导出的 SYSCLK
    #
    # 本实验室实测（与官方软件一致）：
    #     External Clock = 1000 MHz，未勾选 Divider Disable（÷2 使能）
    #     -> System Clock = SYSCLK = 500 MHz
    # 若勾选 Divider Disable（CFR3[15] = 1，1 GHz 直入），本机 DDS 会完全
    # 停止输出，因此默认保持 ÷2 使能。
    _refclk_mhz = 1000.0    # 外部参考时钟 (MHz)，官方软件 External Clock
    _refclk_div2 = True     # ÷2 输入分频器使能（官方软件未勾 Divider Disable）

    @classmethod
    def get_clock(cls):
        """
        返回当前时钟配置。
        返回
            clock - {'refclk_mhz': float, 'div2': bool, 'sysclk_mhz': float}
        """
        return {
            'refclk_mhz': cls._refclk_mhz,
            'div2': cls._refclk_div2,
            'sysclk_mhz': cls._refclk_mhz / (2 if cls._refclk_div2 else 1),
        }

    @classmethod
    def configure_clock(cls, refclk_mhz=None, div2=None):
        """
        配置外部参考时钟与 ÷2 分频器（SYSCLK 由二者推导），并同步已创建设备。
            refclk_mhz - 外部参考时钟 (float类，MHz，必须为正数)
            div2 - True 使能 ÷2 分频器 (SYSCLK = REF_CLK/2)；
                   False 旁路 ÷2 (SYSCLK = REF_CLK)
        返回
            clock - 与 get_clock() 相同
        """
        if refclk_mhz is not None:
            refclk_mhz = float(refclk_mhz)
            if not math.isfinite(refclk_mhz) or refclk_mhz <= 0:
                raise ValueError('REF_CLK必须是正数(MHz): %r' % (refclk_mhz,))
            cls._refclk_mhz = refclk_mhz
        if div2 is not None:
            cls._refclk_div2 = bool(div2)
        # 已创建的设备（含其它入口创建的）同步到新值
        for dev in cls.gets():
            if isinstance(dev, AD9910):
                dev._refclk_mhz = cls._refclk_mhz
                dev._refclk_div2 = cls._refclk_div2
        return cls.get_clock()

    def __init__(self):
        clock = AD9910.get_clock()
        self._refclk_mhz = clock['refclk_mhz']
        self._refclk_div2 = clock['div2']
        path = os.path.join(
            self.config_dir,
            '%s_%s.npy' % (self.model, self.serial_number))
        # 如果寄存器文件已经存在，说明AD9910已在工作，不进行重置
        if not os.path.isfile(path):
            self.download_firmware(
                '\\'.join(('C:', 'Program Files (x86)',
                           'Analog Devices', 'AD9910 Evaluation Software',
                           'AD9910FWNR.hex')))
            self.reset()
    
    @property
    def signals(self):
        status = self.status
        f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = status
        port_e = self.get_port_value(4)
        value = 0xF8 & port_e
        reg = 0x0E
        sigs = {}
        if enable_ram:
            raise ValueError('AD9910的RAM模式尚未开发')
        else:
            for pf in self:
                self.set_port_value(4, value)
                val = self.spi_read(reg)
                sig = pf.decode_single_tone_signal(val, f_sysclk,
                                                   enable_profile_asf,
                                                   enable_sine_out)
                sigs[pf] = sig
                value += 1
                reg += 1
        self.set_port_value(4, port_e)
        return sigs
    
    @signals.setter
    def signals(self, sigs):
        status = self.status
        f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = status
        port_e = self.get_port_value(4)
        value = 0xF8 & port_e
        reg = 0x0E
        if enable_ram:
            raise ValueError('AD9910的RAM模式尚未开发')
        else:
            # self.set_auto_csb(0)
            for pf in self:
                self.set_port_value(4, value)
                try:
                    sig = sigs[pf]
                except KeyError:
                    val = 0
                else:
                    val = pf.encode_single_tone_signal(sig, f_sysclk,
                                                       enable_profile_asf,
                                                       enable_sine_out)
                self.spi_write(reg, val)
                self.set_port_value(4, 0x10 ^ value)
                value += 1
                reg += 1
            # self.set_auto_csb(1)
        self.set_port_value(4, port_e)
    
    @property
    def cfr1(self):
        return self.spi_read(0)
    
    @cfr1.setter
    def cfr1(self, val):
        value = self.get_port_value(4)
        self.spi_write(0, val)
        self.set_port_value(4, 0x10 ^ value)
        self.set_port_value(4, value)
    
    @property
    def cfr2(self):
        return self.spi_read(1)
    
    @cfr2.setter
    def cfr2(self, val):
        value = self.get_port_value(4)
        self.spi_write(1, val)
        self.set_port_value(4, 0x10 ^ value)
        self.set_port_value(4, value)
    
    @property
    def cfr3(self):
        return self.spi_read(2)
    
    @cfr3.setter
    def cfr3(self, val):
        value = self.get_port_value(4)
        self.spi_write(2, val)
        self.set_port_value(4, 0x10 ^ value)
        self.set_port_value(4, value)
    
    @property
    def refclk_frequency(self):
        """外部参考时钟 REF_CLK 频率，MHz。"""
        return self._refclk_mhz

    @property
    def sysclk_frequency(self):
        """内部时钟 (SYSCLK) 频率，MHz；由 REF_CLK 与 ÷2 分频器推导。"""
        return self._refclk_mhz / (2 if self._refclk_div2 else 1)
    
    @property
    def status(self):
        """
        返回
            f_sysclk - 内部时钟频率以MHz为单位 (float类)
            enable_profile_asf - 使能单音profiles调制幅度位 (bool类)
            enable_sine_out - 选择DDS正弦波输出位 (bool类)
            enable_ram - RAM使能位 (bool类)
        """
        cfr1 = self.cfr1
        cfr1 >>= 0x10
        enable_sine_out = bool(1 & cfr1)
        cfr1 >>= 0xF
        enable_ram = bool(1 & cfr1)
        cfr2 = self.cfr2
        enable_profile_asf = bool(1 & cfr2 >> 0x18)
        f_sysclk = self.sysclk_frequency
        return f_sysclk, enable_profile_asf, enable_sine_out, enable_ram
    
    @staticmethod
    def __encode_spi_data(reg, val=-1):
        """
        加密spi数据
            reg - 寄存器编号 (int类)
            val - 寄存器值 (int类)
        返回
            data - 数据 (bytes类)
        """
        if val < 0:
            val = reg | 0x80
            h_len = 2
        else:
            h_len = AD9910.__reg_size[reg] << 1
            b_len = h_len << 2
            val &= (1 << b_len) - 1
            val |= reg << b_len
            h_len += 2
        data = []
        for i in range(h_len):
            h = 0xF & val
            datum = 0
            for i in range(1, 8, 2):
                b = 1 & h
                datum |= b << i
                h >>= 1
            data.append(datum)
            val >>= 4
        return bytes(data)
    
    @staticmethod
    def __decode_spi_data(data):
        """
        解密spi数据
            data - 数据 (bytes类)
        返回
            val - 寄存器值 (int类)
        """
        data = list(data)
        val = 0
        while len(data) > 0:
            datum = data.pop()
            datum >>= 1
            h = 0
            for i in range(4):
                b = 1 & datum
                h |= b << i
                datum >>= 2
            val <<= 4
            val |= h
        return val
    
    @staticmethod
    def __encode_parallel_data(reg, val=-1):
        """
        加密parallel数据
            reg - 寄存器编号 (int类)
            val - 寄存器值 (int类)
        返回
            data - 数据 (bytes类)
        """
        if val < 0:
            val = reg | 0x80
            b_len = 8
        else:
            b_len = AD9910.__reg_size[reg] << 3
            val &= (1 << b_len) - 1
            val |= reg << b_len
            b_len += 8
        data = []
        for i in range(b_len):
            b = 1 & val
            data = [b, 0] + data
            val >>= 1
        return bytes(data)
    
    @classmethod
    def get_config(cls):
        """重载Device类中的方法"""
        config = ''
        for self in cls.gets():
            pf = 7 & self.get_port_value(4)
            f_sysclk, enable_profile_asf, enable_sine_out, enable_ram = self.status
            config += 'AD9910_%4s:        %s\n' % (self.serial_number, self.name)
            config += 'Active Profile:     %1d\n' % pf
            config += 'Enable Profile ASF: %s\n' % enable_profile_asf
            config += 'Enable Sine Out:    %s\n' % enable_sine_out
            for pf in self:
                val = pf.value
                freq, ph, amp = pf.decode_single_tone(val, f_sysclk)
                ph /= numpy.pi
                amp *= amp
                config += 'Profile %1d:          %11.7f MHz, %7.5f π, %11.9f mW\n' % (pf._ch, freq, ph, amp)
            config += '\n'
        return config[: -1]
    
    @classmethod
    def set_config(cls, config):
        """重载Device类中的方法"""
        for config in config[: -1].split('\n\n'):
            name, pf, enable_profile_asf, enable_sine_out, *config = config.split('\n')
            sn, name = re.findall('AD9910_([^\n]{4}):        ([^\n]*)', name)[0]
            try:
                self = AD9910.get('AD9910', int(sn, 16))
            # 若该AD9910失联，则不加载相应配置
            except KeyError:
                continue
            pf = int(re.findall('Active Profile:     (\d)', pf)[0])
            enable_profile_asf = bool(re.findall('Enable Profile ASF: ([^\n]*)', enable_profile_asf)[0])
            enable_sine_out = bool(re.findall('Enable Sine Out:    ([^\n]*)', enable_sine_out)[0])
            # 设置名称
            self.name = name
            # 设置Active Profile
            value = 0xF8 & self.get_port_value(4)
            value |= 7 & pf
            self.set_port_value(4, value)
            # 设置Enable Profile ASF
            self.set_register_bit(1, 0x18, enable_profile_asf)
            # 设置Enable Sine Out
            self.set_register_bit(1, 0x10, enable_sine_out)
            # 设置各profile
            f_sysclk = self.sysclk_frequency
            for pf in self:
                freq, ph, amp = \
                re.findall('Profile \d:          ([ 0-9.]*) MHz, ([ 0-9.]*) π, ([ 0-9.]*) mW', config[pf._ch])[0]
                freq = float(freq)
                ph = numpy.pi * float(ph)
                amp = numpy.sqrt(float(amp))
                pf.value = AD9910Profile.encode_single_tone(freq, ph, amp, f_sysclk)
    
    def delete(self):
        super().delete()
        # AD9910关闭时，删除寄存器文件
        path = os.path.join(
            self.config_dir,
            '%s_%s.npy' % (self.model, self.serial_number))
        os.remove(path)
    
    def reset(self):
        """Resets the firmware and hardware."""
        self.set_port_value(0, 0x0A)
        self.set_port_value(1, 0x00)
        self.set_port_value(2, 0x03)
        self.set_port_value(3, 0x00)
        self.set_port_config(0, 0xFF)
        self.set_port_config(1, 0x00)
        self.set_port_config(2, 0xFF)
        self.set_port_config(3, 0x00)
        self.set_port_config(4, 0xFF)
        self.set_port_value(0, 0x02)
        # AD9910重置时，重置寄存器文件
        path = os.path.join(
            self.config_dir,
            '%s_%s.npy' % (self.model, self.serial_number))
        regs = numpy.array([
            0x00000000,
            0x00400820,
            0x1F3F4000,
            0x0000007F,
            0xFFFFFFFF,
            0, 0,
            0x00000000,
            0x0000,
            0x00000000,
            0x00000000,
            0x0000000000000000,
            0x0000000000000000,
            0x00000000,
            0x08B5000000000000,
            0x0000000000000000,
            0x0000000000000000,
            0x0000000000000000,
            0x0000000000000000,
            0x0000000000000000,
            0x0000000000000000,
            0x0000000000000000,
            0x00000000])
        try:
            numpy.save(path, regs)
        except FileNotFoundError:  # 没有目标目录则先创建目录
            os.makedirs(os.path.dirname(path))
            numpy.save(path, regs)
        self.spi_write(0, 0x00000000)
        self.spi_write(1, 0x01400820)
        # CFR3 与 IO_UPDATE 交给 apply_clock()，其中脉冲会一并锁存 CFR1/CFR2
        self.apply_clock()

    def apply_clock(self):
        """
        按当前时钟配置重写 CFR3，并触发 IO_UPDATE 把 CFR1/CFR2/CFR3 锁存进芯片。
        返回
            cfr3 - 实际写入的 CFR3 值 (int类)
        说明
            CFR3 同时决定 REFCLK ÷2 输入分频器的旁路位（bit15），
            - ÷2 使能：SYSCLK = REF_CLK/2（本机 1000 MHz / 2 = 500 MHz）
            - ÷2 旁路：SYSCLK = REF_CLK（1 GHz 直入；本批评估板会停振）
            本方法**不依赖寄存器镜像文件是否存在**（__init__ 在 .npy 已存在时会跳过
            reset），所以服务每次启动都必须调用，否则芯片可能保留上一次
            （甚至官方软件）写入的时钟配置，表现为该板无输出或频率翻倍/减半。
        """
        cfr3 = CFR3_DIV2_ENABLED if self._refclk_div2 else CFR3_DIV2_BYPASS
        self.spi_write(2, cfr3)
        self.update()
        return cfr3

    def update(self):
        """给 IO_UPDATE 一个 0 -> 1 -> 0 的完整脉冲（上升沿锁存 I/O buffer）。"""
        self.set_port_value(4, 0x80)   # IO_UPDATE = 0
        self.set_port_value(4, 0x90)   # 0 -> 1：上升沿锁存
        self.set_port_value(4, 0x80)
    
    def spi_read(self, reg):
        """
        读寄存器值
            reg - 寄存器编号 (int类)
        返回
            val - 寄存器值 (int类)
        """
        '''
        write_data = self.__encode_spi_data(reg)
        read_length = AD9910.__reg_size[reg] << 1
        read_data = super().spi_read(write_data, read_length, 0)
        return self.__decode_spi_data(read_data)
        '''
        # AD9910自带的寄存器读取功能存在问题，因此引入寄存器文件与寄存器状态同步，用于寄存器读写
        path = os.path.join(
            self.config_dir,
            '%s_%s.npy' % (self.model, self.serial_number))
        while True:
            try:
                regs = numpy.load(path)
            except ValueError:
                continue
            else:
                break
        return int(regs[reg])
    
    def spi_write(self, reg, val):
        """
        写寄存器
            reg - 寄存器编号 (int类)
            val - 寄存器值 (int类)
        """
        write_data = self.__encode_spi_data(reg, val)
        self.set_ctl_value(2)
        super().spi_write(write_data)
        # AD9910自带的寄存器读取功能存在问题，因此引入寄存器文件与寄存器状态同步，用于寄存器读写
        path = os.path.join(
            self.config_dir,
            '%s_%s.npy' % (self.model, self.serial_number))
        regs = numpy.load(path)
        regs[reg] = val
        numpy.save(path, regs)
    
    def set_register_bit(self, reg, i, b):
        """
        设置寄存器的某一位
            reg - 寄存器编号 (int类)
            i - 位索引 (int类)
            b - 位值 (bool类)
        """
        value = self.get_port_value(4)
        val = self.spi_read(reg)
        i = 1 << i
        val |= i
        if not b:
            val ^= i
        self.spi_write(reg, val)
        self.set_port_value(4, 0x10 ^ value)
        self.set_port_value(4, value)


# 允许用环境变量声明时钟配置，方便不经 control_service 的其它入口
# （ad9910_server.py / send_config.py 等）也使用与硬件一致的时钟。
# 例：set AD9910_REFCLK=1000
#     set AD9910_DIVIDER_DISABLE=1   # 勾选 Divider Disable（旁路 ÷2）
_env_refclk = os.environ.get('AD9910_REFCLK')
_env_div_disable = os.environ.get('AD9910_DIVIDER_DISABLE')
try:
    AD9910.configure_clock(
        refclk_mhz=float(_env_refclk.strip()) if _env_refclk else None,
        div2=(False if (_env_div_disable or '').strip().lower() in ('1', 'true', 'yes')
              else (True if _env_div_disable else None)))
except (TypeError, ValueError):
    pass  # 非法值忽略，沿用默认（REF_CLK 1000 MHz、÷2 使能 -> SYSCLK 500 MHz）
