"""Read only the exact I14's active USB configuration; never claim/reset/configure."""
import json
from pathlib import Path
import sys

from fastboot_usb import verified_local_libusb


def read_descriptor(core, util, backend):
    devices = [d for d in core.find(find_all=True, idVendor=0x1d6b,
                                  idProduct=0x0104, backend=backend)
               if d.bus == 2 and tuple(d.port_numbers or ()) == (1, 1)]
    try:
        if len(devices) != 1:
            raise RuntimeError("expected exactly one I14 at bus 2 ports 1,1")
        device = devices[0]
        serial = util.get_string(device, device.iSerialNumber)
        if serial != "I14_NATIVE_INIT":
            raise RuntimeError("wrong I14 serial")
        config = device.get_active_configuration()
        return {"device_count": len(devices), "idVendor": device.idVendor,
                "idProduct": device.idProduct, "bus": device.bus,
                "port_numbers": list(device.port_numbers), "serial_number": serial,
                "bcdUSB": device.bcdUSB,
                "configuration": {"bConfigurationValue": config.bConfigurationValue,
                                  "bmAttributes": config.bmAttributes,
                                  "bMaxPower": config.bMaxPower}}
    finally:
        for device in devices:
            util.dispose_resources(device)


def main():
    import usb.core
    import usb.util
    import usb.backend.libusb1

    pinned = verified_local_libusb(Path(sys.argv[1]))
    backend = usb.backend.libusb1.get_backend(find_library=lambda _: pinned)
    if backend is None:
        raise RuntimeError("pinned libusb backend unavailable")
    print(json.dumps(read_descriptor(usb.core, usb.util, backend), sort_keys=True))


if __name__ == "__main__":
    main()
