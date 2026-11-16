# ad9910_rpc_service.py
import numpy as np
import os, sys
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "../../..")))
from devices.generators.adiclockevals.ad9910 import AD9910
import logging

logger = logging.getLogger(__name__)


class AD9910RPCService:
    def __init__(self):
        # 初始化时不需要启动worker
        pass

    def set_frequency(self, device_id: int, profile: int, freq: float):
        try:
            device = AD9910.get("AD9910", device_id)
            device[profile].frequency = freq
            logger.info(f"Device {device_id} Profile {profile} Freq set to {freq} MHz")
            return True
        except Exception as e:
            logger.error(f"Failed to set frequency for device {device_id}: {e}")
            return False

    def set_power(self, device_id: int, profile: int, amp: float):
        try:
            device = AD9910.get("AD9910", device_id)
            device[profile].amplitude = np.sqrt(amp)
            logger.info(f"Device {device_id} Profile {profile} Power set to {amp} mW")
            return True
        except Exception as e:
            logger.error(f"Failed to set amplitude for device {device_id}: {e}")
            return False

    def adjust_amplitude(self, device_id: int, profile: int, delta: float):
        try:
            device = AD9910.get("AD9910", device_id)
            new_amp = device[profile].amplitude + delta
            device[profile].amplitude = max(0, min(new_amp, np.sqrt(0.5)))  # sqrt(0.2)
            logger.info(f"Device {device_id} Profile {profile} Amp adjusted to {device[profile].amplitude}")
            return True
        except Exception as e:
            logger.error(f"Failed to adjust amplitude for device {device_id}: {e}")
            return False

    def set_phase(self, device_id: int, profile: int, phase: float):
        try:
            device = AD9910.get("AD9910", device_id)
            device[profile].phase = phase
            logger.info(f"Device {device_id} Profile {profile} Phase set to {phase} Pi")
            return True
        except Exception as e:
            logger.error(f"Failed to set phase for device {device_id}: {e}")
            return False

    def close(self):
        # 不需要关闭worker
        pass