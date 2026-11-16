import numpy as np

import time

import socket


def send_config(config_str, id:str, port:int):
    """

    Parameters
    ----------
    config_str
    id
    port

    Returns
    -------

    """
    socket.setdefaulttimeout(5)
    config_str_list = config_str.split(",")

    s1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = s1.connect_ex((id, port))
    if result == 0:
        print("{} connected.".format(config_str_list[0]))
        s1.send(config_str.encode())
        data = s1.recv(1024)
        # print(data.decode())
        s1.send("quit".encode())
        s1.shutdown(socket.SHUT_RDWR)
    else:
        print("{} not connected.".format(config_str_list[0]))
    s1.close()