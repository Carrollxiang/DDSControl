# -*- coding: utf-8 -*-
"""
Created on 2022-10-08

@author: Tesheng
"""

import os, sys
import re
import socket
import select
import numpy as np
import time
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "../../..")))
from devices.generators.adiclockevals.ad9910 import AD9910, Signal
from devices.generators.adiclockevals.ad9910_wrapper import inits
sys.path.pop()


def str_config_AD9910(config_str):
    """
    利用接受到的字符串进行AD9910的配置修改

    Parameters
    ----------
    config_str: str
        包含配置信息的字符串

    Returns
    -------

    Examples
    -------
    config_str = r"Change=0777, Prof=1, Freq=110.0, Amp=0.1, delta_Pwr=0.1, END"

    """
    print(config_str)
    check_completeness = re.findall("^Change(.*)END$", config_str)
    if check_completeness:
        id = re.findall("Change=([a-zA-Z\d]*),", config_str)
        # print(AD9910.gets())
        if id[0] not in [i.serial_number for i in AD9910.gets()]:
            print("No such AD9910")
            return -1

        ad9910 = AD9910.get("AD9910", int('0x' + id[0], 16))
        # print("AD9910 Connected")
        profile = re.findall("Prof=([\d]*),", config_str)
        if not profile:
            # print("No Profile Info")
            return 0
        else:
            print("Profile {}".format(profile[0]))
            profile = int(profile[0])
            if (profile < 0) or (profile > 7):
                # print("No such profile")
                return -1
            else:
                freq = re.findall("Freq=([\d\.]*),", config_str)
                amp = re.findall("[^_]Amp=([\d\.]*),", config_str)
                # print(amp)
                delta_amp = re.findall("delta_Amp=([-\d\.]*),", config_str)
                phase = re.findall("Phase=([\d\.]*),", config_str)
                if freq:
                    ad9910[profile].frequency = float(freq[0])
                    print("AD9910 {} Profile {} Change Freq {}".format(id[0], str(profile), float(freq[0])))
                    time.sleep(0.05)
                if amp:
                    # if amp[0] < 0:
                    #     ad9910[profile].amplitude = 0
                    # elif amp[0] > 0.13:
                    #     ad9910[profile].amplitude = 0.13
                    # else:
                    ad9910[profile].amplitude = float(amp[0])
                    print("AD9910 {} Profile {} Change Amp {}".format(id[0], str(profile), float(amp[0])))
                    time.sleep(0.05)
                if phase:
                    ad9910[profile].phase = float(phase[0])
                    print("AD9910 {} Profile {} Change Phase {}".format(id[0], str(profile), float(phase[0])))
                    time.sleep(0.05)
                if delta_amp:
                    if ad9910[profile].amplitude + float(delta_amp[0]) < 0:
                        ad9910[profile].amplitude = 0
                    elif ad9910[profile].amplitude + float(delta_amp[0]) > np.sqrt(0.05):
                        ad9910[profile].amplitude = np.sqrt(0.02)
                    else:
                        ad9910[profile].amplitude += float(delta_amp[0])
                    print("AD9910 {} Profile {} Change Awp {}".format(id[0], str(profile), float(ad9910[profile].amplitude)))
                print(
                    "AD9910 {} Profile {} Active Profile {}".format(id[0], str(profile), float(ad9910.get_port_value(4))))
            del ad9910

    else:
        print("Uncompleted Info!")
        return -1


def ad9910_server_TCP():
    inits()
    
    hostname = socket.gethostname()
    host_ip = socket.gethostbyname(hostname)

    server_port = 50007
    print(f"Running Server on {host_ip} : {server_port}")
    af, socktype, proto, canonname, sa = (
        socket.getaddrinfo(host_ip, server_port, socket.AF_UNSPEC, socket.SOCK_STREAM, 0,
                           socket.AI_PASSIVE))[0]
    server_socket = socket.socket(af, socktype, proto)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 设置端口可复用
    server_socket.bind(sa)  # 绑定端口
    server_socket.listen(1)  # 限制连接数

    while True:
        conn, addr = server_socket.accept()
        with conn:
            print('Connected by', addr)
            while True:
                try:
                    # 设置连接阻塞
                    conn.setblocking(0)
                    # 建立连接0.1 s之后，若未收到信息，则连接超时，自动断开连接。
                    ready = select.select([conn], [], [], 0.1)
                    if ready[0]:
                        data = conn.recv(4096)
                        data_str = data.decode()
                        if data_str != '':
                            info = str_config_AD9910(data.decode())
                            # print("Mesg Received")
                        else:
                            # 接收到空字符串，则关闭连接
                            conn.close()
                            # print("Receive Nothing, Close Socket Connection")
                            break
                    else:
                        conn.close()
                        # print("TimeOut, Close Socket Connection")
                        break
                except EOFError:
                    # print("EOFError, Close Socket Connection")
                    break



def ad9910_server_UDP():
    """
    UDP协议的AD9910服务器
    Returns
    -------

    """
    hostname = socket.gethostname()
    host_ip = socket.gethostbyname(hostname)

    server_port = 50007
    print(f"Running Server on {host_ip} : {server_port}")

    # 创建UDP socket
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server_socket.bind((host_ip, server_port))

    while True:
        # 接收UDP数据包
        data, addr = server_socket.recvfrom(4096)
        data_str = data.decode()
        if data_str != '':
            info = str_config_AD9910(data_str)
            print("Mesg Received from", addr)



ad9910_server_TCP()
    