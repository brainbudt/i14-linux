"""Differential harness: run the ORIGINAL Python tools against a scripted fake.

Reads one JSON request on stdin, prints one JSON result. USB can never be
reached: every `usb*` import is blocked, `connect` is replaced by a fake, and
`Fastboot.close` is overridden (the original calls usb.util.dispose_resources).
"""
import contextlib
import io
import json
import os
import re
import struct
import sys
from types import SimpleNamespace

for name in ("usb", "usb.core", "usb.util", "usb.backend", "usb.backend.libusb1"):
    sys.modules[name] = None  # import raises ImportError

SCRIPTS = os.environ["I14_SCRIPTS"]
sys.path.insert(0, SCRIPTS)
import fastboot_usb as fb  # noqa: E402
import install_boot_usb as installer  # noqa: E402


def build_image(kernel=32, ramdisk=16, second=0, page=2048, version=0, length=6144):
    data = bytearray(length)
    struct.pack_into("<8s10I", data, 0, b"ANDROID!", kernel, 0, ramdisk, 0, second,
                     0, 0, page, version, 0)
    return bytes(data)


def make_file(spec):
    if "image" in spec:
        data = build_image(**spec["image"])
    elif "repeat" in spec:
        data = bytes([spec["repeat"][0]]) * spec["repeat"][1]
    else:
        data = bytes.fromhex(spec.get("hex", ""))
    if "patch" in spec:
        offset, hexdata = spec["patch"]
        data = data[:offset] + bytes.fromhex(hexdata) + data[offset + len(hexdata) // 2:]
    return data


class Device:
    def __init__(self, s):
        self.responses = list(s.get("responses", []))
        self.writes, self.reads = [], 0
        self.short_write = s.get("short_write")
        self.failed_write = s.get("failed_write")
        self.interrupt_write = s.get("interrupt_write")
        self.port_numbers = None if s.get("port_numbers") is None else tuple(s["port_numbers"])

    def write(self, endpoint, data, timeout):
        assert endpoint == 1 and 0 < timeout <= 5000
        self.writes.append(bytes(data))
        n = len(self.writes)
        if n == self.interrupt_write:
            raise KeyboardInterrupt
        if n == self.failed_write:
            raise TimeoutError("private diagnostic")
        if n == self.short_write:
            return len(data) - 1
        return len(data)

    def read(self, endpoint, size, timeout):
        assert endpoint == 0x81 and size == 512 and 0 < timeout <= 5000
        self.reads += 1
        value = self.responses.pop(0)
        if "timeout" in value:
            raise TimeoutError("private diagnostic")
        if "interrupt" in value:
            raise KeyboardInterrupt
        return bytes.fromhex(value["b"])


def descriptor(mutation):
    out = SimpleNamespace(bEndpointAddress=1, bmAttributes=2, wMaxPacketSize=512)
    incoming = SimpleNamespace(bEndpointAddress=0x81, bmAttributes=2, wMaxPacketSize=512)

    class Interface(list):
        pass

    interface = Interface([out, incoming])
    interface.bInterfaceNumber, interface.bAlternateSetting = 0, 0
    interface.bInterfaceClass, interface.bInterfaceSubClass, interface.bInterfaceProtocol = 255, 66, 3
    interfaces = [interface]
    device = SimpleNamespace(idVendor=0x0bb4, idProduct=0x0c01,
                             get_active_configuration=lambda: interfaces)
    devices = [device]
    if mutation == "none":
        devices = []
    elif mutation == "two":
        devices = [device, device]
    elif mutation == "vid":
        device.idVendor = 0x18d1
    elif mutation == "pid":
        device.idProduct = 0x4ee0
    elif mutation == "two_interfaces":
        interfaces.append(interface)
    elif mutation == "class":
        interface.bInterfaceClass = 8
    elif mutation == "ep_address":
        out.bEndpointAddress = 2
    elif mutation == "ep_packet":
        incoming.wMaxPacketSize = 64
    elif mutation == "ep_extra":
        interface.append(SimpleNamespace(bEndpointAddress=0x82, bmAttributes=3, wMaxPacketSize=64))
    elif mutation != "ok":
        raise ValueError(mutation)
    return devices


def run_tool(s, directory):
    state = SimpleNamespace(connected=False, closed=0, device=None)

    class FakeFastboot(fb.Fastboot):
        def close(self):
            state.closed += 1
            if s.get("close_error"):
                raise RuntimeError("private diagnostic")

    def connect():
        state.connected = True
        mode = s.get("connect", "ok")
        if mode == "io":
            raise OSError(5, "private diagnostic")
        if mode == "other":
            raise RuntimeError("private diagnostic")
        if mode.startswith("select:"):
            fb.select_device(descriptor(mode.split(":", 1)[1]))
        state.device = Device(s)
        return FakeFastboot(state.device)

    module = fb if s["tool"] == "fastboot" else installer
    fb.connect = connect
    installer.connect = connect
    argv = [a.replace("{dir}", directory) for a in s["argv"]]
    out, err = io.StringIO(), io.StringIO()
    sys.argv[0] = "i14-fastboot" if s["tool"] == "fastboot" else "i14-install-boot"
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as exit_:
            code = exit_.code
        except KeyboardInterrupt:
            code = 130
            err.write("KeyboardInterrupt\n")
    receipt = None
    attempt = os.path.join(directory, "attempt.jsonl")
    if os.path.exists(attempt):
        with open(attempt, encoding="utf-8") as handle:
            receipt = handle.read().splitlines()
        receipt_mode = oct(os.stat(attempt).st_mode & 0o777)
    else:
        receipt_mode = None
    device = state.device
    return dict(exit=code, stdout=out.getvalue(), stderr=err.getvalue(),
                writes=[w.hex() for w in device.writes] if device else [],
                reads=device.reads if device else 0,
                connected=state.connected, closed=state.closed,
                receipt=receipt, receipt_mode=receipt_mode)


def main():
    request = json.load(sys.stdin)
    if request["tool"] == "int16":
        result = []
        for text in request["values"]:
            try:
                value = int(text, 16)
                result.append(str(max(min(value, 2**127 - 1), -(2**127 - 1))))
            except ValueError:
                result.append(None)
        print(json.dumps(result))
        return
    if request["tool"] == "validate":
        result = []
        for case in request["cases"]:
            data = make_file(case["file"])
            try:
                fb.validate_image(data, case["sha256"])
                result.append("ok")
            except fb.FastbootError as error:
                result.append(str(error))
        print(json.dumps(result))
        return
    directory = request["dir"]
    for name, spec in request.get("files", {}).items():
        with open(os.path.join(directory, name), "wb") as handle:
            handle.write(make_file(spec))
    for name in request.get("existing", []):
        with open(os.path.join(directory, name), "w") as handle:
            handle.write("old\n")
    print(json.dumps(run_tool(request, directory)))


main()
