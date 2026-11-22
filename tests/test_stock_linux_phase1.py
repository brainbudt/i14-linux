import gzip
import hashlib
import importlib.util
import pathlib
import struct
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def newc(data):
    raw, pos, out = gzip.decompress(data), 0, {}
    while True:
        f = [int(raw[pos + 6 + 8 * i:pos + 14 + 8 * i], 16) for i in range(13)]
        mode, size, namesize = f[1], f[6], f[11]
        name = raw[pos + 110:pos + 110 + namesize - 1].decode()
        pos = (pos + 110 + namesize + 3) & ~3
        body = raw[pos:pos + size]
        pos = (pos + size + 3) & ~3
        if name == "TRAILER!!!":
            return out
        out[name] = (mode, body)


class BootImage(unittest.TestCase):
    def test_repack_keeps_stock_header_and_kernel(self):
        b = load("bootimg", "scripts/build-stock-linux-bootimg.py")
        stock = b.STOCK.read_bytes()
        ramdisk = gzip.compress(b"x" * 5000, mtime=0)
        out = b.repack(stock, ramdisk)
        ks, page = struct.unpack_from("<I", stock, 8)[0], 2048
        self.assertEqual(out[:16], stock[:16])
        self.assertEqual(struct.unpack_from("<I", out, 16)[0], len(ramdisk))
        self.assertEqual(out[20:576], stock[20:576])  # addrs, page, name, cmdline
        self.assertEqual(out[608:page], stock[608:page])
        self.assertEqual(out[page:page + ks], stock[page:page + ks])
        ro = page + ks + (-ks % page)
        self.assertEqual(out[ro:ro + len(ramdisk)], ramdisk)
        self.assertEqual(len(out) % page, 0)

    def test_id_formula_reproduces_stock(self):
        b = load("bootimg2", "scripts/build-stock-linux-bootimg.py")
        stock = b.STOCK.read_bytes()
        ks, rs = struct.unpack_from("<I", stock, 8)[0], struct.unpack_from("<I", stock, 16)[0]
        ro = 2048 + ks + (-ks % 2048)
        self.assertEqual(b.repack(stock, stock[ro:ro + rs])[576:608], stock[576:608])


class Initramfs(unittest.TestCase):
    def test_entries(self):
        m = load("cpio", "initramfs/make-stock-linux-cpio.py")
        build = ROOT / "builds/busybox-usb-pmic-identification"
        files = newc(m.base.archive(m.entries(build)))
        self.assertEqual(files["init"][1], (ROOT / "initramfs/stock-linux-init").read_bytes())
        self.assertEqual(files["bin/i14-pmic-dump"][1], (ROOT / "initramfs/i14-pmic-dump").read_bytes())
        self.assertIn("storage-readonly", files)
        for applet in ("sh", "cat", "mdev", "blockdev", "reboot", "setsid", "sha256sum"):
            self.assertEqual(files["bin/" + applet][1], b"/bin/busybox")

    def test_init_avoids_disabled_command_builtin(self):
        # Phase-1 attempt 1 died at "requirements": this BusyBox has no `command`.
        config = (ROOT / "builds/busybox-usb-pmic-identification/busybox.config").read_text()
        self.assertIn("# CONFIG_ASH_CMDCMD is not set", config)
        for name in ("initramfs/stock-linux-init", "initramfs/i14-pmic-dump", "initramfs/storage-readonly"):
            for line in (ROOT / name).read_text().splitlines():
                self.assertNotRegex(line.split("#")[0], r"(^|[;&|]\s*)\s*command\s", name)


class PmicDump(unittest.TestCase):
    def run_dump(self, *args):
        with tempfile.TemporaryDirectory() as d:
            node = pathlib.Path(d) / "pmic_access"
            node.write_text("")
            return subprocess.run(["sh", str(ROOT / "initramfs/i14-pmic-dump"), *args],
                                  env={"I14_PMIC_ACCESS": str(node), "PATH": "/usr/bin:/bin"},
                                  capture_output=True, text=True)

    def test_only_short_read_requests(self):
        r = self.run_dump("0xffe0", "0xfffe")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [l.split() for l in r.stdout.splitlines() if not l.startswith("#")]
        self.assertEqual(len(rows), 16)
        for addr, echoed in rows:  # regular file echoes back exactly what was written
            self.assertLessEqual(len(echoed), 5)
            self.assertEqual(int(echoed, 16), int(addr, 16))

    def test_rejects_out_of_range(self):
        self.assertNotEqual(self.run_dump("0", "0x10000").returncode, 0)
        self.assertNotEqual(self.run_dump("0x20", "0x10").returncode, 0)


if __name__ == "__main__":
    unittest.main()
