"""Fixed boot-install tests, using synthetic images and fake USB only."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_boot_usb as installer
import fastboot_usb as fb
from test_fastboot_usb import Device, image


def replies(*tail, product=b"MAGC6735_65U_1_M0", unlocked=b"yes", secure=b"no",
            partition=b"0x1000000", maximum=b"0x2000000"):
    return [b"OKAY" + value for value in (product, unlocked, secure, partition, maximum)] + list(tail)


class InstallTests(unittest.TestCase):
    def test_untrusted_local_usb_library_is_rejected_before_load(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libusb-fixture"
            library.write_bytes(b"not the pinned build")
            with self.assertRaisesRegex(fb.FastbootError, "SHA-256 mismatch"):
                fb.verified_local_libusb(library)

    def test_local_usb_library_symlink_resolves_only_to_pinned_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_bytes(b"fixture")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(fb.FastbootError, "SHA-256 mismatch"):
                fb.verified_local_libusb(link)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            with patch.object(fb, "LOCAL_LIBUSB_SHA256", digest):
                self.assertEqual(fb.verified_local_libusb(link), str(target.resolve()))
            with self.assertRaisesRegex(fb.FastbootError, "must be a regular file"):
                fb.verified_local_libusb(root)
            target.unlink()
            with self.assertRaisesRegex(fb.FastbootError, "cannot read local libusb"):
                fb.verified_local_libusb(link, allow_missing=True)
            self.assertIsNone(fb.verified_local_libusb(target, allow_missing=True))

    def test_local_usb_library_fifo_is_rejected_without_waiting(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "libusb-fifo"
            os.mkfifo(fifo)
            code = ("from pathlib import Path; import sys; import fastboot_usb; "
                    "fastboot_usb.verified_local_libusb(Path(sys.argv[1]))")
            result = subprocess.run([sys.executable, "-B", "-c", code, str(fifo)],
                                    cwd=Path(fb.__file__).parent, capture_output=True, text=True, timeout=2)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be a regular file", result.stderr)

    def test_ram_only_cmdline_is_refused_before_connect_or_device_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, stock, attempt = (root / name for name in ("image", "stock", "receipt"))
            stock.write_bytes(b"fixture")
            for offset in (64, 608):
                for word in (b"androidboot.mode=charger", b"i14.autoboot=test", b"i14.test=1"):
                    with self.subTest(offset=offset, word=word):
                        attempt = root / f"receipt-{offset}-{word.split(b'=')[0].decode()}"
                        data = bytearray(image())
                        data[offset:offset + len(word)] = word
                        candidate.write_bytes(data)
                        with patch.object(installer, "validate_stock"), patch.object(installer, "connect") as connector:
                            error = io.StringIO()
                            with patch("sys.stderr", error):
                                status = installer.main(["--image", str(candidate), "--sha256",
                                    hashlib.sha256(data).hexdigest(), "--stock-backup", str(stock),
                                    "--attempt", str(attempt)])
                            self.assertEqual(status, 1)
                            self.assertIn("RAM-boot-only", error.getvalue())
                            connector.assert_not_called()
                            self.assertFalse(attempt.exists())
                        device = Device([])
                        device.port_numbers = (1, 1)
                        with self.assertRaisesRegex(fb.FastbootError, "RAM-boot-only"):
                            installer.install_boot(fb.Fastboot(device), data, lambda *_a, **_kw: None)
                        self.assertEqual(device.writes, [])

    def test_stock_backup_must_match_pinned_full_partition(self):
        for stock in (b"", b"x" * 16384, b"\0" * 0x1000000):
            with self.subTest(size=len(stock)), self.assertRaises(fb.FastbootError):
                installer.validate_stock(stock)

    def test_invalid_local_inputs_never_connect_or_create_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.img"
            stock = root / "stock.img"
            attempt = root / "attempt.jsonl"
            candidate.write_bytes(image())
            stock.write_bytes(b"bad")
            for digest in ("0" * 64, hashlib.sha256(image()).hexdigest()):
                with self.subTest(digest=digest), patch.object(installer, "connect") as connector, \
                     patch("sys.stderr", io.StringIO()):
                    self.assertEqual(installer.main([
                        "--image", str(candidate), "--sha256", digest,
                        "--stock-backup", str(stock), "--attempt", str(attempt)]), 1)
                    connector.assert_not_called()
                    self.assertFalse(attempt.exists())

    def test_receipt_is_exclusive_and_binds_exact_complete_bytes(self):
        data = image()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt.jsonl"
            with installer.attempt_receipt(path, "candidate.img", data) as record:
                record("stopped", nv_write="UNKNOWN")
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(entries[0]["partition"], "boot")
            self.assertEqual(entries[0]["candidate_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(entries[0]["candidate_bytes"], 6144)
            self.assertEqual(entries[0]["stock_sha256"],
                             "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492")
            self.assertEqual(entries[0]["padding"], "none; exact candidate bytes")
            self.assertEqual(entries[0]["readback"], "UNKNOWN")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                with installer.attempt_receipt(path, "candidate.img", data):
                    self.fail("existing receipt was reused")
            self.assertEqual(path.read_bytes(), original)

    def test_guard_failures_never_download_or_flash(self):
        cases = [dict(product=b"other"), dict(unlocked=b"no"), dict(secure=b"yes"),
                 dict(secure=b""), dict(partition=b"0x800000"),
                 dict(partition=b"unknown"), dict(maximum=b"0x1000")]
        with tempfile.TemporaryDirectory() as directory:
            for number, overrides in enumerate(cases):
                device = Device(replies(**overrides))
                device.port_numbers = (1, 1)
                path = Path(directory) / f"attempt-{number}.jsonl"
                with self.subTest(overrides=overrides), \
                     installer.attempt_receipt(path, "candidate.img", image()) as record:
                    with self.assertRaises(fb.FastbootError):
                        installer.install_boot(fb.Fastboot(device), image(), record)
                self.assertFalse(any(value.startswith((b"download:", b"flash:"))
                                     for value in device.writes))

    def test_wrong_or_unknown_physical_port_stops_before_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            for number, ports in enumerate(((1, 2), (), None)):
                device = Device([])
                device.port_numbers = ports
                with installer.attempt_receipt(Path(directory) / f"{number}.jsonl", "x", image()) as record:
                    with self.assertRaises(fb.FastbootError):
                        installer.install_boot(fb.Fastboot(device), image(), record)
                self.assertEqual(device.writes, [])

    def test_exact_download_then_durable_intent_then_only_flash_boot(self):
        data = image(kernel=20000, length=24576)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attempt.jsonl"

            class CheckingDevice(Device):
                def write(self, endpoint, payload, timeout):
                    if payload == b"flash:boot":
                        entries = [json.loads(line) for line in path.read_text().splitlines()]
                        assert entries[-1]["event"] == "flash_intent"
                        assert entries[-1]["nv_write"] == "UNKNOWN"
                    return super().write(endpoint, payload, timeout)

            device = CheckingDevice(replies(b"DATA00006000", b"OKAY", b"OKAY"))
            device.port_numbers = (1, 1)
            with installer.attempt_receipt(path, "candidate.img", data) as record:
                installer.install_boot(fb.Fastboot(device), data, record)
            self.assertEqual(device.writes[:6], [b"getvar:product", b"getvar:unlocked", b"getvar:secure",
                b"getvar:partition-size:boot", b"getvar:max-download-size", b"download:00006000"])
            self.assertEqual(b"".join(device.writes[6:-1]), data)
            self.assertEqual(device.writes[-1], b"flash:boot")
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(entries[-1]["event"], "protocol_acknowledged")
            self.assertEqual(entries[-1]["readback"], "UNKNOWN")
            self.assertNotIn(b"reboot", device.writes)

    def test_uncertain_flash_never_retries_and_receipt_blocks_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            for number, final in enumerate((TimeoutError(), b"FAILdenied", b"OK")):
                path = Path(directory) / f"attempt-{number}.jsonl"
                device = Device(replies(b"DATA00001800", b"OKAY", final))
                device.port_numbers = (1, 1)
                with installer.attempt_receipt(path, "candidate.img", image()) as record:
                    with self.assertRaises(fb.FastbootError):
                        installer.install_boot(fb.Fastboot(device), image(), record)
                    before_retry = list(device.writes)
                    with self.assertRaises(fb.FastbootError):
                        installer.install_boot(fb.Fastboot(device), image(), record)
                    self.assertEqual(device.writes, before_retry)
                self.assertEqual(device.writes.count(b"flash:boot"), 1)
                self.assertNotIn(b"reboot", device.writes)
                entries = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(entries[-1]["event"], "stopped")
                self.assertEqual(entries[-1]["nv_write"], "UNKNOWN")
                with self.assertRaises(FileExistsError):
                    with installer.attempt_receipt(path, "candidate.img", image()):
                        self.fail("uncertain attempt was reused")

    def test_download_failure_never_flashes(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Device(replies(b"DATA00000001"))
            device.port_numbers = (1, 1)
            path = Path(directory) / "attempt.jsonl"
            with installer.attempt_receipt(path, "candidate.img", image()) as record:
                with self.assertRaises(fb.FastbootError):
                    installer.install_boot(fb.Fastboot(device), image(), record)
            self.assertNotIn(b"flash:boot", device.writes)
            entries = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(entries[-1]["nv_write"], "NOT_STARTED")

    def test_other_partition_cli_option_is_rejected_before_connect(self):
        with patch.object(installer, "connect") as connector, patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                installer.main(["--image", "x", "--sha256", "0" * 64,
                                "--stock-backup", "x", "--attempt", "x",
                                "--partition", "recovery"])
            self.assertEqual(result.exception.code, 2)
            connector.assert_not_called()


if __name__ == "__main__":
    unittest.main()
