# -*- coding: utf-8 -*-
"""
Created on 2020-04-27

@author: 张宽
"""

from ctypes import *
from devices.generators.generator import Generator

class ADIClockEvalError(Exception):
    """
    ADI的DDS产生的错误
    cls.__errs = {err: error, ...} - 错误枚举
        err - 错误码 (int类)
        error - 错误 (str类)
    """
    __errs = {
        0: 'ERROR_STD_SUCCESS',
        -1: 'ERROR_STD_INVALID_ARG',
        -101: 'ERROR_HW_NO_DEVICES',
        -102: 'ERROR_HW_DEVICE_IN_USE',
        -103: 'ERROR_HW_DEVICE',
        -104: 'ERROR_HW_ID',
        -105: 'ERROR_HW_PIPE',
        -106: 'ERROR_HW_DESCRIPTOR',
        -107: 'ERROR_HW_DRIVER_BUSY',
        -150: 'ERROR_CMD_FAILED',
        -51: 'ERROR_I2C_WRITE',
        -52: 'ERROR_I2C_WRITE_BUFFSIZE',
        -53: 'ERROR_I2C_READ',
        -54: 'ERROR_I2C_READ_BUFFSIZE',
        -55: 'ERROR_I2C_SPEED_CHANGE',
        -56: 'ERROR_I2C_SPEED_READ',
        -57: 'ERROR_I2C_PING',
        -200: 'ERROR_MUTEX_WAIT_FAILED'}
    
    def __init__(self, *args):
        try:
            err, = args
            err = self.__errs[err]
        except (ValueError, KeyError):
            super().__init__(*args)
        else:
            super().__init__(err)

class ADIClockEval(Generator):
    """
    ADI的DDS
    cls._devs = {(model, sn): adi, ...} - ADIClockEval注册表
        model - 型号 (str类)
        sn - 设备id (int类)
        adi - 设备 (ADIClockEval类)
    cls.__dll
    """
    _devs = {}
    WinDLL(
        '\\'.join(('C:', 'Program Files (x86)',
                   'Analog Devices', 'USB Eval Board Programmer',
                   'ADI_CYUSB_USB4.dll')))
    __dll = WinDLL(
        '\\'.join(('C:', 'Program Files (x86)',
                   'Analog Devices', 'USB Eval Board Programmer',
                   'adiclockeval.dll')))
    
    def __new__(cls):
        raise ADIClockEvalError('应使用inits方法创建ADI的DDS')
    
    def __init__(self):
        self.reset()
    
    def __str__(self):
        if self.name == '':
            return '%s_%s' % (self.model, self.serial_number)
        return self.name
    
    @classmethod
    def __find_hardware(cls):
        """Finds the connected hardware."""
        vids = (c_int * 1)(0x0456)
        pids = (c_int * 1)(0xEE25)
        # length = c_int(1)
        length = c_long(1)
        err = cls.__dll.FindHardware(byref(vids), byref(pids), length)
        print('__find_hardware running')
        print(err)
        if err < 0:
            raise ADIClockEvalError(err)
        return err
    
    @classmethod
    def __get_hardware_handles(cls):
        """Gets handles to hardware instances."""
        handles = (c_int * cls.__find_hardware())()
        print("__get_hardware_handles running")
        print("hardware_handles", handles)
        err = cls.__dll.GetHardwareHandles(handles)
        if err < 0:
            raise ADIClockEvalError(err)
        return handles[:]
    
    @classmethod
    def __get_evbd_info(cls, handle):
        """
        Retrieves information about the hardware.
            handle - Handle to a hardware instance (int类)
        """
        handle = c_int(handle)
        length = 22
        board_info = (c_ubyte * length)()
        length = c_int(length)
        err = cls.__dll.GetEvbdInfo(handle, byref(board_info), byref(length))
        if err < 0:
            raise ADIClockEvalError(err)
        eeprom_i2c_address = board_info[0]
        device_id = board_info[1] | board_info[2] << 8
        product_id = ''.join(['%02X' % c for c in board_info[10:2:-1]])
        product_id = product_id.lstrip('0')
        ports_default_values = board_info[11:16]
        ports_default_configurations = board_info[16:21]
        ctl5_0_values = board_info[21]
        return (
            eeprom_i2c_address,
            device_id,
            product_id,
            ports_default_values,
            ports_default_configurations,
            ctl5_0_values)
    
    @classmethod
    def inits(cls):
        """寻找并初始化所有ADIClockEval"""
        print('inits running')
        cls.deletes()
        devs = cls._devs
        handles = cls.__get_hardware_handles()
        print(handles)
        brand = 'Analog Device'
        models = {}
        for cls in ADIClockEval.__subclasses__():
            model = cls.__name__
            models[model] = cls
        for handle in handles:
            sn, model = cls.__get_evbd_info(handle)[1:3]
            cls = models[model]
            self = super().__new__(
                cls, brand, model,
                '%04X' % sn,
                handle=handle)
            self.__init__()
            devs[model, sn] = self
    
    @classmethod
    def deletes(cls):
        """删除所有ADIClockEval"""
        for adi in cls.gets():
            adi.delete()
    
    def delete(self):
        """删除设备"""
        self.reset()
        model = self.model
        sn = self.serial_number
        del self._devs[model, int(sn, 16)]
        brand = 'Analog Device'
        del super()._devs[brand, model, sn]
    
    def is_connected(self):
        """Gets whether the hardware is connected."""
        handle = c_int(self._handle)
        return bool(self.__dll.IsConnected(handle))
    
    def reset(self):
        """Resets the firmware and hardware."""
        handle = c_int(self._handle)
        err = self.__dll.Reset(handle)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def spi_read(self, write_data, read_length, four_wire):
        """
        Performs a SPI read on the hardware, after writing any data given.
            write_data - Array of data to write before reading (bytes类)
            read_length - Number of bytes to read (int类)
            four_wire - 1 if using four-wire interface, 0 otherwise (int类)
        """
        handle = c_int(self._handle)
        write_length = len(write_data)
        write_data = (c_ubyte * write_length).from_buffer_copy(write_data)
        write_length = c_int(write_length)
        read_data = (c_ubyte * read_length)()
        read_length = c_int(read_length)
        four_wire = c_int(four_wire)
        err = self.__dll.SpiRead(
            handle,
            byref(write_data), write_length,
            byref(read_data), read_length,
            four_wire)
        if err < 0:
            raise ADIClockEvalError(err)
        return bytes(read_data)
    
    def spi_write(self, write_data):
        """
        Performs a SPI write on the hardware.
            write_data - Array of data to write (bytes类)
        """
        handle = c_int(self._handle)
        write_length = len(write_data)
        write_data = (c_ubyte * write_length).from_buffer_copy(write_data)
        write_length = c_int(write_length)
        err = self.__dll.SpiWrite(handle, byref(write_data), write_length)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def get_port_config(self, port):
        """
        Gets the configuration of the given port on the hardware.
            port - A FX2IOPort enumeration element (int类)
                PortA = 0
                PortB = 1
                PortC = 2
                PortD = 3
                PortE = 4
        """
        handle = c_int(self._handle)
        port = c_int(port)
        config = c_ubyte()
        err = self.__dll.GetPortConfig(handle, port, byref(config))
        if err < 0:
            raise ADIClockEvalError(err)
        return config.value
    
    def set_port_config(self, port, config):
        """
        Sets the given port configuration on the hardware.
            port - A FX2IOPort enumeration element (int类)
                PortA = 0
                PortB = 1
                PortC = 2
                PortD = 3
                PortE = 4
            config - Port configuration (int类)
        """
        handle = c_int(self._handle)
        port = c_int(port)
        config = c_ubyte(config)
        err = self.__dll.SetPortConfig(handle, port, config)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def get_port_value(self, port):
        """
        Gets the value of the given port on the hardware.
            port - A FX2IOPort enumeration element (int类)
                PortA = 0
                PortB = 1
                PortC = 2
                PortD = 3
                PortE = 4
        """
        handle = c_int(self._handle)
        port = c_int(port)
        value = c_ubyte()
        err = self.__dll.GetPortValue(handle, port, byref(value))
        if err < 0:
            raise ADIClockEvalError(err)
        return value.value
    
    def set_port_value(self, port, value):
        """
        Gets the value of the given port on the hardware.
            port - A FX2IOPort enumeration element (int类)
                PortA = 0
                PortB = 1
                PortC = 2
                PortD = 3
                PortE = 4
            value - Port value (int类)
        """
        handle = c_int(self._handle)
        port = c_int(port)
        value = c_ubyte(value)
        err = self.__dll.SetPortValue(handle, port, value)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def get_ctl_value(self):
        """Gets the CTL value on the hardware."""
        handle = c_int(self._handle)
        value = c_ubyte()
        err = self.__dll.GetCtlValue(handle, byref(value))
        if err < 0:
            raise ADIClockEvalError(err)
        return value.value
    
    def set_ctl_value(self, value):
        """
        Sets the CTL value on the hardware.
            value - CTL value (int类)
        """
        handle = c_int(self._handle)
        value = c_ubyte(value)
        err = self.__dll.SetCtlValue(handle, value)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def get_evbd_info(self):
        """Retrieves information about the hardware."""
        return self.__get_evbd_info(self._handle)
    
    def download_firmware(self, file_path):
        """
        Downloads a firmware file ( *.hex) to the hardware.
            file_path - Path to the firmware file (str类)
        """
        handle = c_int(self._handle)
        file_path = str(file_path).encode()
        file_path = (c_char * len(file_path)).from_buffer_copy(file_path)
        err = self.__dll.DownloadFirmware(handle, file_path)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def set_auto_csb(self, value):
        """
        Sets the AutoCSB flag on the hardware.
            value -  AutoCSB flag value
                1 = (Default) CSB will be controlled by the firmware
                    during SPI communications
                0 = CSB must be controlled using SetPortValue()
        """
        handle = c_int(self._handle)
        value = c_ubyte(value)
        err = self.__dll.SetAutoCSB(handle, value)
        if err < 0:
            raise ADIClockEvalError(err)
    
    def write_parallel_data(self, write_data):
        """
        Writes parallel data to the hardware.
            write_data - Array of data to write (bytes类)
        """
        handle = c_int(self._handle)
        write_length = len(write_data)
        write_data = (c_ubyte * write_length).from_buffer_copy(write_data)
        write_length = c_int(write_length)
        err = self.__dll.WriteParallelData(
            handle, byref(write_data), write_length)
        if err < 0:
            raise ADIClockEvalError(err)
