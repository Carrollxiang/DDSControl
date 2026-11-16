import re
import socket
from time import sleep
import numpy as np


def new_send_config(config_str, target_id: str, target_port: int):
    """

    Parameters
    ----------
    config_str
    id
    port

    Returns
    -------

    """
    config_str_list = config_str.split(",")
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.settimeout(2)
        client_socket.connect((target_id, target_port))
    except socket.timeout:
        print("链接超时")
    print("{} connected.".format(config_str_list[0]))
    
    client_socket.send(config_str.encode())
    client_socket.shutdown(socket.SHUT_RDWR)
    client_socket.close()


# config_str = r"Change=0A21, Prof=1, Freq=110.0, Amp=0.001, END"
# new_send_config(config_str, target_id=r"192.168.1.106", target_port=50007)
