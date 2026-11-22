import importlib.util
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("readback", Path(__file__).resolve().parents[1] / "scripts/mtk-readback.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class Endpoint:
    wMaxPacketSize = 512
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.requests = []
    def read(self, length, timeout):
        self.requests.append((length, timeout))
        value = next(self.chunks)
        if isinstance(value, Exception):
            raise value
        return value

class FakeDA:
    def __init__(self, blocks):
        self.blocks = iter(blocks)
        self.writes = []
        self.config = types.SimpleNamespace(iot=False, hwcode=0x321)
        storage = types.SimpleNamespace(flashtype="emmc", flashsize=100000000)
        legacy = types.SimpleNamespace(partitiontype_and_size=lambda parttype, length: (length, 8))
        self.daconfig = types.SimpleNamespace(storage=storage, legacy_storage=legacy)
        self.Cmd = types.SimpleNamespace(READ_CMD=b"\xd6")
        self.Rsp = types.SimpleNamespace(ACK=b"\x5a")
    def check_usb_cmd(self): return b"\x00"
    def sdmmc_switch_part(self, part): return True
    def usbread(self, length, **kw):
        return next(self.blocks)
    def usbwrite(self, data):
        self.writes.append(data)
        return True

class ReadbackTests(unittest.TestCase):
    def usb(self, chunks):
        return types.SimpleNamespace(EP_IN=Endpoint(chunks), rxleft=bytearray(), timeout=1000, maxsize=4096)
    def test_fragments_are_exact_and_host_requests_capped(self):
        obj = self.usb([b"x" * 8192] * 4)
        self.assertEqual(m.usbread(obj, 32768, w_max_packet_size=1048576), b"x" * 32768)
        self.assertTrue(all(n <= 16384 and timeout > 0 for n, timeout in obj.EP_IN.requests))
    def test_surplus_preserved_for_next_protocol_read(self):
        obj = self.usb([b"abc"])
        self.assertEqual(m.usbread(obj, 1), b"a")
        self.assertEqual(m.usbread(obj, 2), b"bc")
        self.assertEqual(len(obj.EP_IN.requests), 1)
    def test_tail_request_is_packet_aligned_to_preserve_checksum_surplus(self):
        obj = self.usb([b"x" * 600 + b"cs"])
        self.assertEqual(m.usbread(obj, 600, w_max_packet_size=16384), b"x" * 600)
        self.assertEqual(obj.EP_IN.requests[0][0], 1024)
        self.assertEqual(m.usbread(obj, 2), b"cs")
    def test_empty_and_endpoint_error_abort_without_retry(self):
        for value in (b"", OSError("transport")):
            obj = self.usb([value])
            with self.assertRaises(m.ReadbackError): m.usbread(obj, 4)
            self.assertEqual(len(obj.EP_IN.requests), 1)
    def test_valid_checksum_and_exact_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "boot.img"
            da = FakeDA([b"\x5a", b"abcd", struct.pack(">H", sum(b"abcd"))])
            self.assertTrue(m.emmc_readflash(da, 0, 4, str(dest)))
            self.assertEqual(dest.read_bytes(), b"abcd")
            self.assertFalse(Path(str(dest)+".partial").exists())
            self.assertEqual(da.writes[-1], b"\x5a")
    def test_corrupt_empty_and_short_data_never_ack_or_publish(self):
        for payload, checksum in [(b"abcd", b"\x00\x00"), (b"", b"\x00\x00"), (b"a", b"\x00\x61"), (b"abcd", b"")]:
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "boot.img"
                da = FakeDA([b"\x5a", payload, checksum])
                with self.assertRaises(m.ReadbackError): m.emmc_readflash(da, 0, 4, str(dest))
                self.assertFalse(dest.exists())
                self.assertNotEqual(da.writes[-1], b"\x5a")
    def test_existing_final_or_partial_not_overwritten(self):
        for suffix in ("", ".partial"):
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "boot.img"
                occupied = Path(str(dest)+suffix)
                occupied.write_bytes(b"evidence")
                da = FakeDA([])
                with self.assertRaises((m.ReadbackError, FileExistsError)): m.emmc_readflash(da, 0, 4, str(dest))
                self.assertEqual(occupied.read_bytes(), b"evidence")
                self.assertEqual(da.writes, [])
    def test_script_rejects_writes_options_and_empty_commands(self):
        for text in ["flash boot image", "rf out --generatekeys", "--parttype=user", "printgpt --parttype=boot1", "ro -1 4 out", "printgpt; reboot", ""]:
            with self.assertRaises(m.ReadbackError): m.checked_commands(text)
        self.assertEqual(m.checked_commands("printgpt\nro 0 0x1000 out --parttype=boot1")[0], "printgpt --stock --noreconnect")
    def test_failed_second_block_retains_only_checked_prefix(self):
        data = b"x" * 16384
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "boot.img"
            da = FakeDA([b"\x5a", data, struct.pack(">H", sum(data)&65535), b"yz", b"\x00\x00"])
            with self.assertRaises(m.ReadbackError): m.emmc_readflash(da, 0, len(data)+2, str(dest))
            self.assertFalse(dest.exists())
            self.assertEqual(Path(str(dest)+".partial").read_bytes(), data)
    def test_unsupported_hardware_aborts_before_usb_or_output(self):
        da = FakeDA([])
        da.config.hwcode = 0x335
        with self.assertRaises(m.ReadbackError): m.emmc_readflash(da, 0, 4, "")
        self.assertEqual(da.writes, [])
    def test_negotiated_packet_size_and_each_block_checksum(self):
        data = b"x" * 16384
        tail = b"yz"
        da = FakeDA([b"\x5a", data, struct.pack(">H", sum(data)&65535), tail, struct.pack(">H", sum(tail))])
        self.assertEqual(m.emmc_readflash(da, 0, len(data)+2, ""), data+tail)
        self.assertEqual(da.writes[5], struct.pack(">I", 16384))
        self.assertEqual(da.writes[6:], [b"\x5a", b"\x5a"])
    def test_serial_requires_exact_path_and_mediatek_vid_pid(self):
        port = types.SimpleNamespace(device="/dev/cu.usbmodem21101", vid=0x0e8d, pid=0x0003)
        self.assertTrue(m.serial_port_allowed(port.device, [port]))
        self.assertFalse(m.serial_port_allowed(port.device, []))
        for bad in [types.SimpleNamespace(device=port.device, vid=0x1234, pid=3), types.SimpleNamespace(device=port.device, vid=0x0e8d, pid=0x9999)]:
            with self.assertRaises(m.ReadbackError): m.serial_port_allowed(port.device, [bad])
        with self.assertRaises(m.ReadbackError): m.serial_port_allowed("DETECT", [port])
    def test_serial_reads_are_exact_finite_and_do_not_flush(self):
        class Device:
            timeout = 500
            def read(self, length):
                self.request = length
                return b"x" * length
            def flush(self): raise AssertionError("must not flush")
        obj = types.SimpleNamespace(device=Device())
        self.assertEqual(m.serial_usbread(obj, 4096), b"x" * 4096)
        self.assertEqual(obj.device.timeout, 1)
        self.assertEqual(obj.device.request, 4096)
        for payload in (b"", b"x"):
            obj.device.read = lambda length: payload
            with self.assertRaises(m.ReadbackError): m.serial_usbread(obj, 4)
    def test_serial_short_write_aborts_without_retry_or_flush(self):
        class Device:
            writes = 0
            def write(self, data):
                self.writes += 1
                return len(data)-1
            def flush(self): raise AssertionError("must not flush")
        obj = types.SimpleNamespace(device=Device())
        with self.assertRaises(m.ReadbackError): m.serial_write(obj, b"abcd")
        self.assertEqual(obj.device.writes, 1)
        self.assertEqual(obj.device.write_timeout, 1)
    def test_installed_serial_aliases_never_reach_upstream_flush(self):
        class Serial:
            def usbwrite(self, data, pktsize=None): raise AssertionError("upstream flush")
            def usbreadwrite(self, data, resplen): raise AssertionError("upstream flush")
        class Device:
            def write(self, data): return len(data)
            def read(self, length): return b"x" * length
            def flush(self): raise AssertionError("must not flush")
        m.install_serial_guards(Serial)
        obj = Serial()
        obj.device = Device()
        self.assertTrue(obj.usbwrite(b"test"))
        self.assertEqual(obj.usbreadwrite(b"test", 1), b"x")
        obj.flush()
    def test_serial_open_is_bounded_and_preserves_input(self):
        class Device:
            is_open = False
            def __init__(self, **options): self.options = options
            def _reset_input_buffer(self): raise AssertionError("input discarded")
            def open(self):
                self._reset_input_buffer()
                self.is_open = True
            def close(self): self.is_open = False
        obj = types.SimpleNamespace(connected=False, portname="/dev/cu.usbmodem21101")
        with patch.dict("sys.modules", {"serial": types.SimpleNamespace(Serial=Device)}):
            with patch.object(m, "serial_detectdevices", return_value=[obj.portname]):
                self.assertTrue(m.serial_connect(obj))
        self.assertEqual(obj.device.options["timeout"], 1)
        self.assertEqual(obj.device.options["write_timeout"], 1)
        self.assertEqual(obj.device.port, obj.portname)
    def test_memory_read_handles_more_than_one_da_packet(self):
        data = b"x" * 16384
        tail = b"yz"
        da = FakeDA([b"\x5a", data, struct.pack(">H", sum(data)&65535), tail, struct.pack(">H", sum(tail))])
        self.assertEqual(m.emmc_readflash(da, 0, len(data)+2, ""), data+tail)

if __name__ == "__main__": unittest.main()
