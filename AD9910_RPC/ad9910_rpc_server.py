# ad9910_rpc_server.py

import asyncio
import logging
import sys
import os
import socket

sys.path.append(os.path.abspath(os.path.join(os.getcwd(), "../../..")))

try:
    from devices.generators.adiclockevals.ad9910_wrapper import inits
except ImportError as e:
    print(f"Failed to import device libraries: {e}")
    sys.exit(1)

from sipyco.pc_rpc import Server
from ad9910_rpc_service import AD9910RPCService

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def start_rpc_server():
    inits()

    service = AD9910RPCService()
    server = Server({"ad9910": service}, None, False, False)

    host_ip = socket.gethostbyname(socket.gethostname())
    await server.start(host_ip, 3250)
    logger.info(f"RPC server started on {host_ip}")

    try:
        await asyncio.Event().wait()
    except KeyboardInterrupt:
        logger.info("Shutting down RPC server...")
    finally:
        await server.stop()
        service.close()

if __name__ == "__main__":
    asyncio.run(start_rpc_server())
