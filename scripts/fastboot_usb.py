#!/usr/bin/env python3
"""Bounded Fastboot fallback for this I14; no flash, erase or unlock commands.

Run with the project's PyUSB environment (imports never open USB):
  .venv/bin/python scripts/fastboot_usb.py --query product
  .venv/bin/python scripts/fastboot_usb.py --reboot
  .venv/bin/python scripts/fastboot_usb.py --boot IMAGE --sha256 HEX

Boot uploads only to RAM and requires an unlocked bootloader. A successful
Fastboot acknowledgement does not prove Linux booted. Ctrl-C stops the host;
after a transfer error, inspect device state before starting another action.
The measured product and USB descriptors are guards, not a unique serial.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import sys
import time

PRODUCT = "MAGC6735_65U_1_M0"
MAX_IMAGE = 0x1000000
LOCAL_LIBUSB_SHA256 = "dff5fd707882ce5ed3e9e0055fee727466b0530dccc25cd31f98c8bd22034d32"
QUERIES = ("product", "unlocked", "secure", "max-download-size",
           "partition-size:boot", "partition-size:recovery")


class FastbootError(Exception):
    pass


def validate_image(data, sha256):
    """Validate the complete bytes before connecting or writing any command."""
    if not re.fullmatch(r"[0-9a-fA-F]{64}", sha256 or ""):
        raise FastbootError("expected a 64-digit SHA-256")
    if not 1632 <= len(data) <= MAX_IMAGE:
        raise FastbootError("image must fit the 16 MiB boot partition")
    if hashlib.sha256(data).hexdigest() != sha256.lower():
        raise FastbootError("image SHA-256 mismatch")
    if data[:8] != b"ANDROID!":
        raise FastbootError("missing legacy Android boot magic")
    kernel, _, ramdisk, _, second, _, _, page, version, _ = struct.unpack_from("<10I", data, 8)
    if version != 0 or page not in (2048, 4096, 8192, 16384, 32768, 65536):
        raise FastbootError("expected Android header v0 with a valid page size")
    if not kernel:
        raise FastbootError("empty kernel")
    required = page + sum(((size + page - 1) // page) * page
                          for size in (kernel, ramdisk, second))
    if required > len(data):
        raise FastbootError("boot segments or their page padding exceed the image")
    return data


def select_device(devices):
    """Require one candidate and the measured active Fastboot interface."""
    devices = list(devices)
    if len(devices) != 1:
        raise FastbootError("expected exactly one matching USB device")
    device = devices[0]
    if (device.idVendor, device.idProduct) != (0x0bb4, 0x0c01):
        raise FastbootError("USB VID/PID mismatch")
    interfaces = list(device.get_active_configuration())
    if len(interfaces) != 1:
        raise FastbootError("unexpected active USB interface layout")
    interface = interfaces[0]
    signature = (interface.bInterfaceNumber, interface.bAlternateSetting,
                 interface.bInterfaceClass, interface.bInterfaceSubClass,
                 interface.bInterfaceProtocol)
    endpoints = sorted((ep.bEndpointAddress, ep.bmAttributes, ep.wMaxPacketSize)
                       for ep in interface)
    if signature != (0, 0, 255, 66, 3) or endpoints != [(1, 2, 512), (0x81, 2, 512)]:
        raise FastbootError("Fastboot interface or endpoint mismatch")
    return device


def verified_local_libusb(library, *, allow_missing=False):
    try:
        library.lstat()
    except FileNotFoundError as exc:
        if allow_missing:
            return None
        raise FastbootError("cannot read local libusb") from exc
    except OSError as exc:
        raise FastbootError("cannot read local libusb") from exc
    try:
        # The Mac tooling path is a symlink to the installed libusb package.
        target = library.resolve(strict=True)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (OSError, RuntimeError) as exc:
        raise FastbootError("cannot read local libusb") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FastbootError("local libusb must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as source:
            actual = hashlib.file_digest(source, "sha256").hexdigest()
    except OSError as exc:
        raise FastbootError("cannot read local libusb") from exc
    finally:
        os.close(fd)
    if actual != LOCAL_LIBUSB_SHA256:
        raise FastbootError("local libusb SHA-256 mismatch")
    return str(target)


def connect():
    # Lazy imports keep validation and tests independent of USB discovery.
    import usb.core
    import usb.util
    import usb.backend.libusb1

    library = Path(__file__).resolve().parents[1] / "artifacts/tooling/lib/libusb-1.0.so"
    pinned = verified_local_libusb(library, allow_missing=True) if sys.platform == "darwin" else None
    if pinned is not None:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _: pinned)
    else:
        backend = usb.backend.libusb1.get_backend()
    if backend is None:
        raise FastbootError("libusb backend unavailable")
    candidates = usb.core.find(find_all=True, idVendor=0x0bb4, idProduct=0x0c01,
                               backend=backend)
    device = select_device(candidates)
    usb.util.claim_interface(device, 0)
    return Fastboot(device)


class Fastboot:
    def __init__(self, device):
        self.device = device

    def close(self):
        import usb.util
        usb.util.dispose_resources(self.device)

    def _write(self, data):
        try:
            written = self.device.write(1, data, timeout=3000)
        except Exception as error:
            raise FastbootError("USB write failed; stopped without retry") from error
        if written != len(data):
            raise FastbootError("short USB write; stopped without retry")

    def _response(self, expected=b"OKAY"):
        deadline = time.monotonic() + 10
        for _ in range(256):
            remaining = int((deadline - time.monotonic()) * 1000)
            if remaining <= 0:
                raise FastbootError("Fastboot response deadline exceeded")
            try:
                packet = bytes(self.device.read(0x81, 512, timeout=min(3000, remaining)))
            except Exception as error:
                raise FastbootError("USB read failed; stopped without retry") from error
            if not 4 <= len(packet) <= 64:
                raise FastbootError("short or oversized Fastboot response")
            status, payload = packet[:4], packet[4:]
            if status == b"INFO":
                continue
            if status == b"FAIL":
                raise FastbootError("device rejected command")
            if status != expected:
                raise FastbootError("unexpected Fastboot response status")
            return payload
        raise FastbootError("too many Fastboot INFO responses")

    def _command(self, command, expected=b"OKAY"):
        self._write(command.encode("ascii"))
        return self._response(expected)

    def _getvar(self, name):
        try:
            return self._command("getvar:" + name).decode("ascii")
        except UnicodeError as error:
            raise FastbootError("non-ASCII Fastboot variable") from error

    def _guard(self):
        if self._getvar("product") != PRODUCT:
            raise FastbootError("product mismatch; action stopped")

    def query(self, name):
        if name not in QUERIES:
            raise FastbootError("query is not allowed")
        self._guard()
        return PRODUCT if name == "product" else self._getvar(name)

    def reboot(self):
        self._guard()
        self._command("reboot")

    def boot(self, data, sha256):
        validate_image(data, sha256)
        self._guard()
        if self._getvar("unlocked") != "yes":
            raise FastbootError("temporary boot requires unlocked=yes")
        try:
            partition = int(self._getvar("partition-size:boot"), 16)
            maximum = int(self._getvar("max-download-size"), 16)
        except ValueError as error:
            raise FastbootError("invalid boot partition or download size") from error
        if partition != MAX_IMAGE or maximum < len(data):
            raise FastbootError("boot partition or maximum download size mismatch")
        self._download(data)
        self._command("boot")

    def _download(self, data):
        accepted = self._command(f"download:{len(data):08x}", expected=b"DATA")
        if not re.fullmatch(b"[0-9a-fA-F]{8}", accepted) or int(accepted, 16) != len(data):
            raise FastbootError("device accepted an incorrect download length")
        for offset in range(0, len(data), 16384):
            self._write(data[offset:offset + 16384])
        self._response()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--query", choices=QUERIES)
    actions.add_argument("--reboot", action="store_true")
    actions.add_argument("--boot", metavar="IMAGE", type=Path)
    parser.add_argument("--sha256", metavar="HEX")
    args = parser.parse_args(argv)
    if bool(args.boot) != bool(args.sha256):
        parser.error("--boot and --sha256 must be used together")
    client = None
    result = 0
    try:
        if args.boot:
            with args.boot.open("rb") as source:
                data = source.read(MAX_IMAGE + 1)
            validate_image(data, args.sha256)
        client = connect()
        if args.query:
            print(f"{args.query}={client.query(args.query)}")
        elif args.reboot:
            client.reboot()
            print(f"{PRODUCT}: reboot acknowledged")
        else:
            client.boot(data, args.sha256)
            print(f"{PRODUCT}: RAM boot command acknowledged; verify actual boot separately")
    except FastbootError as error:
        print(f"stopped: {error}", file=sys.stderr)
        result = 1
    except OSError:
        print("stopped: I/O failed; no retry", file=sys.stderr)
        result = 1
    except Exception:
        print("stopped: USB setup failed; no retry", file=sys.stderr)
        result = 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                print("stopped: USB cleanup failed", file=sys.stderr)
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
