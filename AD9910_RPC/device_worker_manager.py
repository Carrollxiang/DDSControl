# device_worker_manager.py

import queue
import threading
import logging
import time
from typing import Dict, Tuple, Optional
import os, sys
import re
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "../../..")))
from devices.generators.adiclockevals.ad9910 import AD9910

logger = logging.getLogger(__name__)

CONFIG_DELAY = 0.02  # Delay between configurations in seconds


class AD9910ConfigError(Exception):
    """Custom exception for AD9910 configuration errors"""
    pass

class DeviceWorkerManager:
    """管理每个AD9910设备的专属worker，使用结构化参数"""

    def __init__(self):
        self.devices: Dict[int, Tuple[queue.Queue, threading.Thread]] = {}
        self.lock = threading.Lock()

    def start(self):
        """初始化所有设备的worker线程"""
        with self.lock:
            for device in AD9910.gets():
                device_id = int(device.serial_number, 16)  # 直接使用整数ID
                q = queue.Queue(maxsize=50)
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(device_id, q),
                    daemon=True
                )
                t.start()
                self.devices[device_id] = (q, t)
                logger.info(f"Started worker for device {device_id}")

    def _worker_loop(self, device_id: int, q: queue.Queue):
        """设备专属worker的主循环"""
        device = AD9910.get("AD9910", device_id)
        while True:
            try:
                command, addr = q.get()
                if command is None:  # 关闭信号
                    break

                self._apply_command(device, command)

            except Exception as e:
                logger.error(f"Device {device_id} worker error: {e}")
            finally:
                q.task_done()

    def _apply_command(self, device: AD9910, command: dict):
        """应用配置命令到设备"""
        try:
            profile = command['profile']
            if not (0 <= profile <= 7):
                raise AD9910ConfigError(f"Invalid profile number: {profile}")

            if 'freq' in command:
                device[profile].frequency = command['freq']
                logger.info(f"Device {command['id']} Profile {profile} Freq set to {command['freq']}")
                time.sleep(CONFIG_DELAY)

            if 'amp' in command:
                device[profile].amplitude = command['amp']
                logger.info(f"Device {command['id']} Profile {profile} Amp set to {command['amp']}")
                time.sleep(CONFIG_DELAY)

            if 'phase' in command:
                device[profile].phase = command['phase']
                logger.info(f"Device {command['id']} Profile {profile} Phase set to {command['phase']}")
                time.sleep(CONFIG_DELAY)

            if 'delta_amp' in command:
                new_amp = device[profile].amplitude + command['delta_amp']
                device[profile].amplitude = max(0, min(new_amp, 0.4472))  # sqrt(0.2)
                logger.info(f"Device {command['id']} Profile {profile} Amp adjusted to {device[profile].amplitude}")

        except Exception as e:
            raise AD9910ConfigError(f"Failed to apply configuration: {e}")

    def put_command(self, device_id: int, command: dict, addr: tuple) -> bool:
        """将命令路由到对应设备的队列"""
        with self.lock:
            if device_id in self.devices:
                self.devices[device_id][0].put((command, addr))
                return True
            else:
                logger.warning(f"Unknown device ID: {device_id}")
                return False

    def shutdown(self):
        """安全关闭所有worker"""
        with self.lock:
            for q, t in self.devices.values():
                q.put((None, None))  # 发送终止信号
                t.join(timeout=1)
            self.devices.clear()
