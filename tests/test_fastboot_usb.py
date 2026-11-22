"""Offline protocol tests: never connect to USB."""
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fastboot_usb.py"
spec = importlib.util.spec_from_file_location("fastboot_usb", SCRIPT)
fb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fb)


def image(**fields):
    values = dict(kernel=32, ramdisk=16, second=0, page=2048, version=0, length=6144)
    values.update(fields)
    data = bytearray(values["length"])
    struct.pack_into("<8s10I", data, 0, b"ANDROID!", values["kernel"], 0,
                     values["ramdisk"], 0, values["second"], 0, 0,
                     values["page"], values["version"], 0)
    return bytes(data)


class Device:
    def __init__(self, responses, short_write=None, failed_write=None):
        self.responses = list(responses)
        self.writes = []
        self.short_write = short_write
        self.failed_write = failed_write
        self.reads = 0

    def write(self, endpoint, data, timeout):
        assert endpoint == 1 and 0 < timeout <= 5000
        self.writes.append(bytes(data))
        if len(self.writes) == self.failed_write:
            raise TimeoutError("private diagnostic")
        if len(self.writes) == self.short_write:
            return len(data) - 1
        return len(data)

    def read(self, endpoint, size, timeout):
        assert endpoint == 0x81 and size == 512 and 0 < timeout <= 5000
        self.reads += 1
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def responses(*tail, unlocked=b"yes", size=b"0x1000000", maximum=b"0x2000000"):
    return [b"OKAYMAGC6735_65U_1_M0", b"OKAY" + unlocked,
            b"OKAY" + size, b"OKAY" + maximum, *tail]


def descriptor():
    out = SimpleNamespace(bEndpointAddress=1, bmAttributes=2, wMaxPacketSize=512)
    incoming = SimpleNamespace(bEndpointAddress=0x81, bmAttributes=2, wMaxPacketSize=512)

    class Interface(list):
        bInterfaceNumber = 0
        bAlternateSetting = 0
        bInterfaceClass = 255
        bInterfaceSubClass = 66
        bInterfaceProtocol = 3

    interface = Interface([out, incoming])
    device = SimpleNamespace(idVendor=0x0bb4, idProduct=0x0c01,
                             get_active_configuration=lambda: [interface])
    return device, interface


class ImageTests(unittest.TestCase):
    def test_legacy_v0_image_and_hash_are_required(self):
        valid = image()
        self.assertEqual(fb.validate_image(valid, hashlib.sha256(valid).hexdigest()), valid)
        invalid = [b"", b"x" * 6144, image(page=0), image(page=1024),
                   image(version=1), image(kernel=0), image(kernel=100000),
                   image(ramdisk=100000), image(second=100000), valid[:-1],
                   b"x" * (0x1000000 + 1)]
        for data in invalid:
            with self.subTest(length=len(data), header=data[:48]):
                with self.assertRaises(fb.FastbootError):
                    fb.validate_image(data, hashlib.sha256(data).hexdigest())
        for digest in ["0" * 64, "0" * 63, "z" * 64]:
            with self.subTest(digest=digest), self.assertRaises(fb.FastbootError):
                fb.validate_image(valid, digest)

    def test_bad_image_never_connects_to_usb(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.img"
            path.write_bytes(b"bad")
            with patch.object(fb, "connect") as connector:
                self.assertEqual(fb.main(["--boot", str(path), "--sha256", "0" * 64]), 1)
                connector.assert_not_called()

    def test_import_does_not_load_pyusb(self):
        code = "import runpy,sys; runpy.run_path(sys.argv[1],run_name='test'); assert 'usb.core' not in sys.modules"
        import subprocess
        result = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cleanup_error_is_concise_and_returns_failure(self):
        device = Device([b"OKAYMAGC6735_65U_1_M0"])
        client = fb.Fastboot(device)
        stderr = io.StringIO()
        with patch.object(fb, "connect", return_value=client), \
             patch.object(client, "close", side_effect=RuntimeError("private diagnostic")), \
             patch("sys.stderr", stderr), patch("sys.stdout", io.StringIO()):
            self.assertEqual(fb.main(["--query", "product"]), 1)
        self.assertIn("cleanup", stderr.getvalue())
        self.assertNotIn("private diagnostic", stderr.getvalue())


class IdentityTests(unittest.TestCase):
    def test_unique_vid_pid_and_exact_descriptor_required(self):
        device, interface = descriptor()
        self.assertIs(fb.select_device([device]), device)
        for devices in [[], [device, device]]:
            with self.subTest(count=len(devices)), self.assertRaises(fb.FastbootError):
                fb.select_device(devices)
        for target, attribute, value in [
            (device, "idVendor", 1), (device, "idProduct", 1),
            (interface, "bInterfaceNumber", 1), (interface, "bAlternateSetting", 1),
            (interface, "bInterfaceClass", 1), (interface, "bInterfaceSubClass", 1),
            (interface, "bInterfaceProtocol", 1),
            (interface[0], "bEndpointAddress", 2),
            (interface[1], "bmAttributes", 3), (interface[0], "wMaxPacketSize", 64),
        ]:
            original = getattr(target, attribute)
            setattr(target, attribute, value)
            with self.subTest(attribute=attribute), self.assertRaises(fb.FastbootError):
                fb.select_device([device])
            setattr(target, attribute, original)

    def test_wrong_product_stops_before_action(self):
        device = Device([b"OKAYother"])
        with self.assertRaises(fb.FastbootError):
            fb.Fastboot(device).reboot()
        self.assertEqual(device.writes, [b"getvar:product"])


class ProtocolTests(unittest.TestCase):
    def test_queries_and_reboot_check_product_each_time(self):
        device = Device([b"INFOhello", b"OKAYMAGC6735_65U_1_M0",
                         b"OKAYno", b"OKAYMAGC6735_65U_1_M0", b"OKAY"])
        client = fb.Fastboot(device)
        self.assertEqual(client.query("unlocked"), "no")
        client.reboot()
        self.assertEqual(device.writes, [b"getvar:product", b"getvar:unlocked",
                                         b"getvar:product", b"reboot"])

    def test_unapproved_query_never_writes(self):
        device = Device([])
        with self.assertRaises(fb.FastbootError):
            fb.Fastboot(device).query("serialno")
        self.assertEqual(device.writes, [])

    def test_failure_malformed_reply_timeout_and_short_write_stop(self):
        for reply in [b"FAILnot allowed", b"OK", b"", b"WHATunknown", b"DATA00000000",
                      b"OKAY" + b"x" * 61, TimeoutError("private diagnostic")]:
            device = Device([reply])
            with self.subTest(reply=reply), self.assertRaises(fb.FastbootError):
                fb.Fastboot(device).reboot()
            self.assertEqual(device.writes, [b"getvar:product"])
        device = Device([], short_write=1)
        with self.assertRaises(fb.FastbootError):
            fb.Fastboot(device).reboot()
        self.assertEqual(device.reads, 0)

    def test_info_flood_is_bounded(self):
        device = Device([b"INFOx"] * 257)
        with self.assertRaises(fb.FastbootError):
            fb.Fastboot(device).reboot()
        self.assertEqual(device.reads, 256)
        self.assertEqual(device.writes, [b"getvar:product"])

    def test_boot_downloads_exact_bytes_then_boots_after_final_okay(self):
        data = image()
        device = Device(responses(b"DATA00001800", b"INFOdownloaded", b"OKAY", b"OKAY"))
        fb.Fastboot(device).boot(data, hashlib.sha256(data).hexdigest())
        self.assertEqual(device.writes[:5], [b"getvar:product", b"getvar:unlocked",
            b"getvar:partition-size:boot", b"getvar:max-download-size", b"download:00001800"])
        self.assertEqual(b"".join(device.writes[5:-1]), data)
        self.assertEqual(device.writes[-1], b"boot")

    def test_large_image_uses_complete_chunks_and_stops_at_second_chunk_error(self):
        data = image(kernel=20000, length=24576)
        digest = hashlib.sha256(data).hexdigest()
        device = Device(responses(b"DATA00006000", b"OKAY", b"OKAY"))
        fb.Fastboot(device).boot(data, digest)
        self.assertEqual([len(chunk) for chunk in device.writes[5:-1]], [16384, 8192])
        self.assertEqual(b"".join(device.writes[5:-1]), data)
        for option in (dict(short_write=7), dict(failed_write=7)):
            device = Device(responses(b"DATA00006000", b"OKAY", b"OKAY"), **option)
            with self.subTest(option=option), self.assertRaises(fb.FastbootError):
                fb.Fastboot(device).boot(data, digest)
            self.assertEqual(len(device.writes), 7)
            self.assertEqual(device.reads, 5)
            self.assertNotIn(b"boot", device.writes)

    def test_boot_guards_stop_before_download(self):
        data = image()
        for options in [dict(unlocked=b"no"), dict(size=b"0x800000"),
                        dict(size=b"unknown"), dict(maximum=b"0x1000"),
                        dict(maximum=b"unknown")]:
            device = Device(responses(**options))
            with self.subTest(options=options), self.assertRaises(fb.FastbootError):
                fb.Fastboot(device).boot(data, hashlib.sha256(data).hexdigest())
            self.assertFalse(any(value.startswith(b"download:") for value in device.writes))
        device = Device([])
        with self.assertRaises(fb.FastbootError):
            fb.Fastboot(device).boot(data, "0" * 64)
        self.assertEqual(device.writes, [])

    def test_boot_never_continues_after_download_error(self):
        data = image()
        for tail, short in [([b"FAILdenied"], None), ([b"DATA00000001"], None),
                            ([b"DATAgarbage!"], None), ([b"OKAY"], None),
                            ([b"DATA00001800", b"FAILinvalid"], None),
                            ([b"DATA00001800", TimeoutError()], None),
                            ([b"DATA00001800"], 6)]:
            device = Device(responses(*tail), short_write=short)
            with self.subTest(tail=tail, short=short), self.assertRaises(fb.FastbootError):
                fb.Fastboot(device).boot(data, hashlib.sha256(data).hexdigest())
            self.assertNotIn(b"boot", device.writes)
            if tail[0] != b"DATA00001800":
                self.assertEqual(len(device.writes), 5)


if __name__ == "__main__":
    unittest.main()
