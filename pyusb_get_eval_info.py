import usb.core
import usb.util
from ctypes import c_ubyte, c_int, byref
import time


class EvbdInfoGetter:
    def __init__(self, vendor_id, product_id):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.RETRY_COUNT = 3
        self.INFO_LENGTH = 0x16  # 22 bytes
        self.TIMEOUT = 1000  # 1 second timeout

    def get_device_by_handle(self, handle):
        """Convert handle to USB device object"""
        try:
            devices = list(usb.core.find(
                find_all=True,
                idVendor=self.vendor_id,
                idProduct=self.product_id
            ))
            print("devices", devices)
            return devices[handle] if 0 <= handle < len(devices) else None
        except:
            return None

    def get_evbd_info(self, handle, board_info, length):
        """
        Replacement for GetEvbdInfo function

        Args:
            handle: Device handle (integer)
            board_info: Buffer for board info (ctypes array of c_ubyte)
            length: Buffer length (ctypes c_int)

        Returns:
            0 on success, negative values on error:
            -200: Invalid handle
            -1: Buffer too small
            -150: Communication error
        """
        # Convert handle to device object
        device = self.get_device_by_handle(handle)
        if not device:
            return -200

        # Check buffer length
        if length.value < self.INFO_LENGTH:
            return -1

        try:
            # Configure device
            if device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
            device.set_configuration()

            # Get endpoint addresses
            cfg = device.get_active_configuration()
            intf = cfg[(0, 0)]
            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
            )
            ep_in = usb.util.find_descriptor(
                intf,
                custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
            )

            # Send initial command (0x18)
            init_cmd = bytes([0x18])
            ep_out.write(init_cmd, timeout=self.TIMEOUT)

            # Try to get valid response (up to 3 attempts)
            for _ in range(self.RETRY_COUNT):
                # Read response
                response = ep_in.read(0x17, timeout=self.TIMEOUT)

                # Check if response is valid (starts with 0x01 and second byte is 'P' or 'Q')
                if (response[0] == 0x01 and
                        chr(response[1]) in ['P', 'Q']):
                    # Copy data to board_info buffer
                    for i in range(self.INFO_LENGTH):
                        board_info[i] = response[i + 1]
                    length.value = self.INFO_LENGTH
                    return 0

                time.sleep(0.1)  # Short delay between retries

            # If we get here, no valid response was received
            length.value = 0
            return -150

        except usb.core.USBError as e:
            length.value = 0
            return -150
        finally:
            # Cleanup
            try:
                device.reset()
            except:
                pass


# Usage example:
def create_evbd_info_getter():
    VID = 0x0456  # Replace with your device's VID
    PID = 0xEE25  # Replace with your device's PID
    # VID = 0x0483  # Replace with your device's VID
    # PID = 0x5740  # Replace with your device's PID
    return EvbdInfoGetter(VID, PID)


def get_board_info(handle):
    """
    Wrapper function to match original DLL usage
    """
    getter = create_evbd_info_getter()
    length = c_int(22)
    board_info = (c_ubyte * 22)()

    result = getter.get_evbd_info(
        c_int(handle).value,
        board_info,
        length
    )

    if result == 0:
        # Convert board info to readable format
        info = bytes(board_info[:length.value])
        return result, info
    else:
        return result, None


# Example usage:
if __name__ == "__main__":
    # Test with a device handle
    result, info = get_board_info(0)
    if result == 0:
        print(f"Success! Board Info: {info.hex()}")
    else:
        print(f"Error: {result}")