"""scripts/restore-stock-logo.py: the boot restore's guarded DA write, retargeted to logo only."""
import importlib.util
import pathlib
import struct
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("restore_logo", ROOT / "scripts/restore-stock-logo.py")
logo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logo)


class FakeDev:
    def __init__(self, replies):
        self.timeout, self.replies = 1, list(replies)

    def read(self, n):
        return self.replies.pop(0) if self.replies else b""


class FakeDA:
    """Just enough of DALegacy for the write hook: Cmd, usbwrite and mtk.port.cdc.device."""
    class Cmd:
        SDMMC_WRITE_DATA_CMD = b"\xc0"

    def __init__(self, packets):
        self.sent = []
        dev = FakeDev([b"\x5a"] + [b"\x69"] * packets)
        self.mtk = type("M", (), {"port": type("P", (), {"cdc": type("C", (), {"device": dev})})})

    def usbwrite(self, data):
        self.sent.append(bytes(data))
        return True


@unittest.skipUnless(logo.STOCK.exists(), "stock logo backup not present")
class RestoreStockLogo(unittest.TestCase):
    def setUp(self):
        self.boot = logo.load_boot_restore()
        self.boot.PACKET = 1048576
        self.stock = self.boot.stock_bytes()
        self.write = self.boot.make_guarded_write(self.stock)

    def test_retargeted_to_the_gpt_logo_partition(self):
        self.assertEqual((self.boot.BOOT_OFFSET, self.boot.BOOT_SIZE), (0x3D80000, 0x800000))
        self.assertEqual(len(self.stock), 0x800000)
        self.assertEqual(self.boot.sha(self.stock), logo.STOCK_SHA256)

    def test_the_boot_script_itself_still_targets_boot(self):
        spec = importlib.util.spec_from_file_location("boot_again", ROOT / "scripts/restore-stock-boot.py")
        boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(boot)
        self.assertEqual((boot.BOOT_OFFSET, boot.BOOT_SIZE), (0x1D80000, 0x1000000))

    def test_refuses_any_other_target_or_file(self):
        da = FakeDA(8)
        for addr, length, name in ((0x1D80000, 0x800000, logo.STOCK), (0x3D80000, 0x1000000, logo.STOCK),
                                   (0x3D80000, 0x800000, ROOT / "README.md")):
            with self.assertRaises(self.boot.RestoreError):
                self.write(da, addr, length, filename=str(name))
        with self.assertRaises(self.boot.RestoreError):
            self.write(da, 0x3D80000, 0x800000, filename=str(logo.STOCK), parttype="boot1")
        self.assertEqual(da.sent, [])  # nothing reached the DA

    def test_writes_exactly_the_stock_logo_at_the_logo_offset(self):
        da = FakeDA(8)
        self.assertTrue(self.write(da, 0x3D80000, 0x800000, filename=str(logo.STOCK)))
        header = da.sent[:6]
        self.assertEqual(header[3], struct.pack(">Q", 0x3D80000))
        self.assertEqual(header[4], struct.pack(">Q", 0x800000))
        data = b"".join(da.sent[7::3])  # per packet: ACK, chunk, checksum
        self.assertEqual(data, self.stock)

    def test_unconfirmed_packet_stops(self):
        da = FakeDA(3)  # the 4th packet gets no CONT
        with self.assertRaises(self.boot.RestoreError):
            self.write(da, 0x3D80000, 0x800000, filename=str(logo.STOCK))


if __name__ == "__main__":
    unittest.main()
