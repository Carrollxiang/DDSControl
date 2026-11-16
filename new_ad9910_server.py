# -*- coding: utf-8 -*-
"""
AD9910 Configuration Server with Concurrent Message Processing

架构说明:
+--------------------------------------------------------------------+
|                        TCP Server Thread (Main)                    |
|                                                                    |
|  1. 监听端口 (create_server_socket)                                |
|  2. 接收新连接 → 为每个连接创建线程 (handle_tcp_connection)        |
|                                                                    |
|  +-------------------+    +-----------------------------------+    |
|  | handle_tcp_connection  |                                    |    |
|  |-------------------|    | DeviceWorkerManager               |    |
|  | 接收原始消息        | →  |                                   |    |
|  | 验证格式           |    | 1. 为每个设备创建专属队列+工作线程 |    |
|  | 路由到设备队列      |    | 2. 根据设备ID分发消息 (put_message)|    |
|  +---------------- --+    | 3. 工作线程处理消息 (_worker_loop) |    |
|                           +-----------------------------------+    |
+--------------------------------------------------------------------+

函数职责说明:
1. 核心类:
   - AD9910Server: 主服务入口
     - ad9910_server_TCP_concurrent(): 启动TCP监听循环
     - handle_tcp_connection(): 处理单个TCP连接的生命周期

2. 设备管理:
   - DeviceWorkerManager: 设备线程和消息路由管理器
     - start(): 初始化所有设备工作线程
     - _worker_loop(): 设备配置执行核心 (循环处理队列消息)
     - put_message(): 消息路由到指定设备队列
     - shutdown(): 安全停止所有线程

3. 配置处理:
   - validate_config_string(): 验证消息格式有效性
   - parse_config_string(): 提取参数并移除异常字符(如[])
   - apply_configuration(): 将参数应用到物理设备

4. 网络层:
   - create_server_socket(): 创建并配置TCP监听套接字

工作流示例:
  TCP消息 → handle_tcp_connection → DeviceWorkerManager.put_message
    → 设备专属队列 → _worker_loop → apply_configuration → 硬件设备


Created on 2025-04-23
@author: Tesheng


TCP Server Thread (Main)
│
├── Accept Connections → Spawn New Thread per Connection
│   └── handle_tcp_connection: 接收消息 → worker_manager.put_message
│
└── DeviceWorkerManager
    ├── Device 1: Queue → Worker Thread 1 (处理消息并配置设备)
    ├── Device 2: Queue → Worker Thread 2
    └── ...


"""

import os, sys
import re
import socket
import select
import numpy as np
import time
import logging
import queue
import threading
from typing import Optional, Tuple, List, Dict
from concurrent.futures import ThreadPoolExecutor

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Constants
TCP_TIMEOUT = 0.1  # Socket timeout in seconds
CONFIG_DELAY = 0.02  # Delay between configurations in seconds

# Add device library path temporarily
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "../../..")))
try:
    from devices.generators.adiclockevals.ad9910 import AD9910, Signal
    from devices.generators.adiclockevals.ad9910_wrapper import inits
except ImportError as e:
    logger.error(f"Failed to import device libraries: {e}")
    sys.exit(1)
finally:
    sys.path.pop()


class AD9910ConfigError(Exception):
    """Custom exception for AD9910 configuration errors"""
    pass


class DeviceWorkerManager:
    """管理每个AD9910设备的专属worker"""

    def __init__(self):
        self.devices = {}  # {device_id: (queue, thread)}
        self.lock = threading.Lock()

    def start(self):
        """初始化时调用，确保设备ID比较方式一致"""
        with self.lock:
            for device in AD9910.gets():
                # 从设备获取的真实ID统一处理为整数
                device_id = int(device.serial_number, 16)  # 假设serial_number是类似'0x0666'的字符串
                q = queue.Queue(maxsize=50)
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(device_id, q),
                    daemon=True
                )
                t.start()
                self.devices[device_id] = (q, t)
                logger.info(f"Started worker for device {hex(device_id)}")

    def _worker_loop(self, device_id, q):
        """设备专属worker的主循环"""
        device = AD9910.get("AD9910", device_id)
        while True:
            try:
                message, addr = q.get()
                
                print(f"{device_id} get ", message)
                if message is None:  # 关闭信号
                    break

                params = parse_config_string(message)
                # 将消息中的ID转换为整数进行比较
                message_device_id = int(params['id'], 16) if params else None

                if params and message_device_id == device_id:
                    print(params)
                    apply_configuration(device, params)
                else:
                    logger.error(f"Device ID mismatch in worker {hex(device_id)}")

            except Exception as e:
                logger.error(f"Device {hex(device_id)} worker error: {e}")
            finally:
                q.task_done()

    def put_message(self, message: str, addr: tuple) -> bool:
        """路由消息到对应设备的队列，处理十六进制ID的各种输入格式"""
        params = parse_config_string(message)
        if not params:
            return False

        try:
            # 将字符串形式的十六进制数转换为整数（无论是否有0x前缀）
            device_id = int(params['id'], 16)  # 如 '666' -> 0x666 (1638)

            with self.lock:
                if device_id in self.devices:
                    self.devices[device_id][0].put((message, addr))
                    return True
                else:
                    logger.warning(f"Unknown device ID: {hex(device_id)}")
                    return False
        except ValueError as e:
            logger.error(f"Invalid device ID format: {params['id']} - {e}")
            return False

    def shutdown(self):
        """安全关闭所有worker"""
        with self.lock:
            for q, t in self.devices.values():
                q.put((None, None))  # 发送终止信号
                t.join(timeout=1)
            self.devices.clear()


def validate_config_string(config_str: str) -> bool:
    """Validate the configuration string format"""
    if not config_str.strip():
        logger.warning("Empty configuration string received")
        return False

    if not re.search(r"^Change=.*, Prof=\d+,.*END$", config_str):
        logger.warning(f"Invalid configuration string format: {config_str}")
        return False

    return True


def parse_config_string(config_str: str) -> Optional[dict]:
    """Parse the configuration string into a dictionary of parameters"""
    try:
        # Preprocess the string to remove all square brackets
        cleaned_str = re.sub(r'[\[\]]', '', config_str)

        # 提取原始ID（可能是0666或0x0666格式）
        raw_id = re.findall(r"Change=([a-zA-Z\d]*),", cleaned_str)[0]

        # 统一转换为不带0x的小写十六进制字符串
        device_id = raw_id.lower().replace('0x', '')  # 无论输入是0666还是0x0666，都变为666

        params = {
            'id': device_id,
            'profile': int(re.findall(r"Prof=([\d]*),", cleaned_str)[0]),
            'freq': float(re.findall(r"Freq=([\d\.]*),", cleaned_str)[0]) 
            if re.findall(r"Freq=([\d\.]*),", cleaned_str) else None,
            'amp': float(re.findall(r"[^_]Amp=([\d\.]*),", cleaned_str)[0]) 
            if re.findall(r"[^_]Amp=([\d\.]*),", cleaned_str) else None,
            'delta_amp': float(re.findall(r"delta_Amp=(-?[\d\.]*),", cleaned_str)[0])
            if re.findall(r"delta_Amp=(-?[\d\.]*),", cleaned_str) else None,
            'phase': float(re.findall(r"Phase=(-?[\d\.]*),", cleaned_str)[0])
            if re.findall(r"Phase=(-?[\d\.]*),", cleaned_str) else None
        }
        return params
    except (IndexError, ValueError) as e:
        logger.error(f"Failed to parse configuration string: {e}")
        return None


def apply_configuration(ad9910: AD9910, params: dict) -> None:
    """Apply the configuration parameters to the AD9910 device"""
    try:
        profile = params['profile']
        if profile < 0 or profile > 7:
            raise AD9910ConfigError(f"Invalid profile number: {profile}")

        if params['freq'] is not None:
            ad9910[profile].frequency = params['freq']
            logger.info(f"AD9910 {params['id']} Profile {profile} Frequency set to {params['freq']}")
            time.sleep(CONFIG_DELAY)

        if params['amp'] is not None:
            ad9910[profile].amplitude = params['amp']
            logger.info(f"AD9910 {params['id']} Profile {profile} Amplitude set to {params['amp']}")
            time.sleep(CONFIG_DELAY)

        if params['phase'] is not None:
            ad9910[profile].phase = params['phase']
            logger.info(f"AD9910 {params['id']} Profile {profile} Phase set to {params['phase']}")
            time.sleep(CONFIG_DELAY)

        if params['delta_amp'] is not None:
            new_amp = ad9910[profile].amplitude + params['delta_amp']
            ad9910[profile].amplitude = max(0, min(new_amp, np.sqrt(0.08)))
            print("why?")
            logger.info(f"AD9910 {params['id']} Profile {profile} Amplitude adjusted to {ad9910[profile].amplitude}")

        logger.info(f"AD9910 {params['id']} Profile {profile} Active Profile {ad9910.get_port_value(4)}")
    except Exception as e:
        raise AD9910ConfigError(f"Failed to apply configuration: {e}")


def create_server_socket(host_ip: str, port: int) -> socket.socket:
    """Create and configure a TCP server socket"""
    try:
        # Get address info for TCP socket
        addr_info = socket.getaddrinfo(
            host_ip, port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            0,
            socket.AI_PASSIVE
        )[0]
        af, socktype, proto, canonname, sa = addr_info

        # Create and configure TCP socket
        server_socket = socket.socket(af, socktype, proto)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.settimeout(1)  # Set timeout for accept operations
        server_socket.bind(sa)
        server_socket.listen(100)  # Increased backlog for multiple connections
        return server_socket

    except socket.error as e:
        logger.error(f"Failed to create TCP socket: {e}")
        raise


# 修改主服务器类
class AD9910Server:
    def __init__(self):
        self.worker_manager = DeviceWorkerManager()

    def ad9910_server_TCP_concurrent(self) -> None:
        """Main TCP server function with concurrent message processing"""
        try:
            inits()
            self.worker_manager.start()  # 启动设备worker
            host_ip = socket.gethostbyname(socket.gethostname())
            server_port = 50018

            print(f"running on {host_ip}:{server_port}")
            server_socket = create_server_socket(host_ip, server_port)

            while True:
                try:
                    # Accept new connections
                    conn, addr = server_socket.accept()
                    # Handle connection in main thread (messages go to queue)
                    threading.Thread(
                        target=self.handle_tcp_connection,
                        args=(conn, addr),
                        daemon=True
                    ).start()

                except socket.timeout:
                    # Timeout is normal for non-blocking accept
                    continue
                except KeyboardInterrupt:
                    logger.info("Server shutdown requested")
                    break
                except Exception as e:
                    logger.error(f"Server error: {e}")
                    time.sleep(0.1)  # Prevent tight loop on errors
        finally:
            self.worker_manager.shutdown()  # 清理worker

    def handle_tcp_connection(self, conn: socket.socket, addr: Tuple[str, int]) -> None:
        """Handle an incoming TCP connection and add messages to queue"""
        logger.info(f"New TCP connection from {addr}")
        try:
            while True:
                data = conn.recv(4096)
                print(data.decode())
                if not data:
                    break
                if not self.worker_manager.put_message(data.decode(), addr):
                    print(1)
                    conn.sendall(b"Invalid device or message format")
                else:
                    print(2)
                    conn.sendall(b"Message routed to device worker")
        except Exception as e:
            logger.error(f"Error handling connection from {addr}: {e}")
        finally:
            conn.close()
            logger.info(f"Closed connection with {addr}")



if __name__ == "__main__":
    server = AD9910Server()
    server.ad9910_server_TCP_concurrent()
