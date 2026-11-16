#!/usr/bin/env python3
"""
AD9910 RPyC Client Example
基于RPyC协议的AD9910设备RPC客户端示例
"""

import logging
import rpyc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    # 连接到RPyC服务器
    try:
        conn = rpyc.connect("localhost", 3251, config={
            'allow_public_attrs': True,
            'allow_pickle': True,
            'allow_all_attrs': True
        })
        logger.info("Connected to AD9910 RPyC server")
    except Exception as e:
        logger.error(f"Failed to connect to server: {e}")
        return

    try:
        # 获取AD9910服务
        ad9910_service = conn.root.get_ad9910_service()

        # 测试设置频率
        result = ad9910_service.set_frequency(0x0422, 3, 100.)  # 设备ID 2, profile 0, 100MHz
        logger.info(f"Set frequency result: {result}")

        # 测试设置幅度
        result = ad9910_service.set_amplitude(0x0422, 3, 0.2)  # 设备ID 2, profile 0, 幅度0.2
        logger.info(f"Set amplitude result: {result}")

        # # 测试调整幅度
        # result = ad9910_service.adjust_amplitude(0x0422, 3, 0.05)  # 设备ID 2, profile 0, 增加0.05
        # logger.info(f"Adjust amplitude result: {result}")

        # 测试设置相位
        result = ad9910_service.set_phase(0x0422, 3, 0.5)  # 设备ID 2, profile 0, 相位0.5
        logger.info(f"Set phase result: {result}")

    except Exception as e:
        logger.error(f"Error during operation: {e}")
    finally:
        # 关闭连接
        try:
            ad9910_service.close()
            conn.close()
        except:
            pass


if __name__ == "__main__":
    main()