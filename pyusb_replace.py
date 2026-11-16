# import usb.core
# import usb.util
# 
# 
# def find_device(vid=0x0456, pid=0xEE25):
#     """
#     使用 pyusb 查找指定 VID/PID 的设备
#     """
#     # 查找设备
#     device = usb.core.find(idVendor=vid, idProduct=pid)
# 
#     if device is None:
#         raise ValueError('Device not found')
# 
#     # 获取设备详细信息
#     try:
#         # 设置设备配置
#         device.set_configuration()
# 
#         # 获取设备信息
#         manufacturer = usb.util.get_string(device, device.iManufacturer)
#         product = usb.util.get_string(device, device.iProduct)
#         serial_number = usb.util.get_string(device, device.iSerialNumber)
# 
#         return {
#             'device': device,
#             'manufacturer': manufacturer,
#             'product': product,
#             'serial_number': serial_number,
#             'bus': device.bus,
#             'address': device.address
#         }
#     except Exception as e:
#         print(f"Error getting device details: {e}")
#         return {'device': device}
# 
# 
# def list_all_devices():
#     """
#     列出系统中所有的 USB 设备
#     """
#     devices = usb.core.find(find_all=True)
# 
#     for device in devices:
#         try:
#             print(f"VID: 0x{device.idVendor:04x}, "
#                   f"PID: 0x{device.idProduct:04x}, "
#                   f"Bus: {device.bus}, "
#                   f"Address: {device.address}")
#         except Exception as e:
#             print(f"Error reading device: {e}")
# 
# 
# # 使用示例
# if __name__ == "__main__":
#     # 列出所有设备
#     print("All USB devices:")
#     list_all_devices()
# 
#     # 查找特定设备
#     try:
#         dev_info = find_device(0x0456, 0xEE25)
#         print("\nFound target device:")
#         for key, value in dev_info.items():
#             print(f"{key}: {value}")
#     except ValueError as e:
#         print(f"\nError: {e}")


"""
DLL Handle Analysis:
FUN_10002d50 does the following:
1. Iterates through device array at this+8
2. For each valid device (non-zero entry):
   - Stores its index as a handle in param_1 array
   - Handles are sequential indices of found devices

PyUSB Implementation to match DLL behavior:
"""
import usb.core
import usb.util


class UsbDeviceManager:
    def __init__(self, vendor_id, product_id):
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.devices = []
        self._handle_map = {}  # Maps DLL-style handles to PyUSB devices

    def find_all_devices(self):
        """Find all matching devices and create DLL-compatible handles"""
        # Find all devices matching VID/PID
        found_devices = list(usb.core.find(
            find_all=True,
            idVendor=self.vendor_id,
            idProduct=self.product_id
        ))

        # Reset handle mappings
        self.devices = []
        self._handle_map = {}

        # Create sequential handles (0-based index) like the DLL
        for idx, device in enumerate(found_devices):
            handle = idx  # DLL uses array index as handle
            self._handle_map[handle] = device
            self.devices.append(device)

        return list(self._handle_map.keys())  # Return handles in DLL format

    def get_device_by_handle(self, handle):
        """Get PyUSB device object from DLL-style handle"""
        return self._handle_map.get(handle)

    def communicate_with_device(self, handle, data):
        """Example communication using PyUSB device"""
        device = self.get_device_by_handle(handle)
        if not device:
            raise ValueError(f"Invalid handle: {handle}")

        # Configure device if needed
        if device.is_kernel_driver_active(0):
            device.detach_kernel_driver(0)
        device.set_configuration()

        # Perform communication (example)
        # You'll need to adjust endpoint numbers and transfer types based on your device
        endpoint_out = device[0][(0, 0)][0]
        endpoint_in = device[0][(0, 0)][1]

        # Write data
        endpoint_out.write(data)

        # Read response
        response = endpoint_in.read(64)  # Adjust buffer size as needed
        return response


# Usage example:
if __name__ == "__main__":
    VID = 0x0456  # Replace with your device's VID
    PID = 0xEE25  # Replace with your device's PID

    manager = UsbDeviceManager(VID, PID)
    handles = manager.find_all_devices()
    print(f"Found {len(handles)} devices with handles: {handles}")

    # Communicate with each device
    for handle in handles:
        try:
            device = manager.get_device_by_handle(handle)
            print(f"Device {handle}: {device}")
            # Add your communication code here
        except Exception as e:
            print(f"Error with device {handle}: {e}")