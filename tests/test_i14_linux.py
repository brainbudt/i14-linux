import gzip
import importlib.util
import pathlib
import struct
import subprocess
import sys
import unittest
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESCUE_HASH_FIXTURE = b"$6$i14-test$" + b"x" * 85 + b"."


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cpio_names(raw):
    pos, out = 0, {}
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


def bionic_find(blob, key):
    """Property lookup the way bionic (Android 6) walks the trie."""
    off = 0
    for part in key.encode().split(b"."):
        cur = struct.unpack_from("<I", blob, 128 + off + 16)[0]
        while cur:
            name = blob[128 + cur + 20:128 + cur + 20 + blob[128 + cur]]
            cmp = (len(part) > len(name)) - (len(part) < len(name)) or (part > name) - (part < name)
            if cmp == 0:
                break
            cur = struct.unpack_from("<I", blob, 128 + cur + (8 if cmp < 0 else 12))[0]
        if not cur:
            return None
        off = cur
    prop = struct.unpack_from("<I", blob, 128 + off + 4)[0]
    return blob[128 + prop + 4:128 + prop + 96].split(b"\0")[0].decode() if prop else None


class BootImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.b = load("i14boot", "scripts/build-i14-boot.py")
        if not all(p.is_file() for p in (cls.b.bootimg.STOCK, cls.b.BUSYBOX, cls.b.E2FSCK)):
            raise unittest.SkipTest("requires local stock boot backup, BusyBox and e2fsck")
        cls.stock = cls.b.bootimg.STOCK.read_bytes()
        cls.img = cls.b.build(cls.stock, RESCUE_HASH_FIXTURE)

    def test_kernel_and_cmdline(self):
        page, ks = 2048, struct.unpack_from("<I", self.stock, 8)[0]
        self.assertEqual(self.img[page:page + ks], self.stock[page:page + ks])
        self.assertEqual(self.img[64:576].split(b"\0")[0], b"bootopt=64S3,32N2,64N2 rdinit=/i14init")
        test = self.b.build(self.stock, RESCUE_HASH_FIXTURE, self.b.CMDLINE_CHARGE_TEST)
        self.assertEqual(test[64:576].split(b"\0")[0],
                         b"bootopt=64S3,32N2,64N2 rdinit=/i14init androidboot.mode=charger i14.autoboot=test")

    def test_ramdisk_is_stock_then_ours(self):
        ks, rs = struct.unpack_from("<I", self.img, 8)[0], struct.unpack_from("<I", self.img, 16)[0]
        start = 2048 + ks + (-ks % 2048)
        ramdisk = self.img[start:start + rs]
        android = self.b.stock_ramdisk(self.stock)
        self.assertTrue(ramdisk.startswith(android))
        rest = ramdisk[len(android):].lstrip(b"\0")
        self.assertLess(len(ramdisk) - len(android) - len(rest), 4)
        stock_files = cpio_names(zlib.decompressobj(31).decompress(android))
        self.assertIn("init", stock_files)  # Android's init stays available
        ours = cpio_names(gzip.decompress(rest))
        self.assertEqual(ours["i14init"][1], (ROOT / "initramfs/i14init").read_bytes())
        mode, shadow = ours["etc/shadow"]
        self.assertEqual(mode & 0o777, 0o600)
        self.assertEqual(shadow.split(b":")[1], RESCUE_HASH_FIXTURE)
        for applet in self.b.APPLETS:
            self.assertEqual(ours["i14/bin/" + applet][1], b"busybox")
        self.assertFalse(set(ours) & set(stock_files) - {"i14"})


class Properties(unittest.TestCase):
    def test_minimal_area_is_found_by_bionic_lookup(self):
        p = load("props", "rootfs/make-android-props.py")
        blob = p.build()
        self.assertEqual(len(blob), 128 * 1024)
        for key, value in p.PROPS.items():
            self.assertEqual(bionic_find(blob, key), value)
        self.assertEqual(p.read(blob), p.PROPS)

    def test_writer_matches_stock_ordering(self):
        stock = ROOT / "artifacts/wifi-20261102/android-properties.bin"
        if not stock.exists():
            self.skipTest("no stock property dump")
        p = load("props2", "rootfs/make-android-props.py")
        props = p.read(stock.read_bytes())
        rebuilt = p.build(props)
        for key, value in props.items():
            self.assertEqual(bionic_find(stock.read_bytes(), key), value)
            self.assertEqual(bionic_find(rebuilt, key), value)


class Scripts(unittest.TestCase):
    def test_shell_syntax(self):
        scripts = [ROOT / "initramfs/i14init", ROOT / "initramfs/i14charge", ROOT / "rootfs/build-alpine-rootfs.sh",
                   ROOT / "scripts/ram-boot.sh", ROOT / "scripts/build-e2fsck.sh"]
        scripts += sorted((ROOT / "rootfs/overlay/usr/sbin").iterdir())
        scripts += sorted((ROOT / "rootfs/overlay/etc/init.d").iterdir())
        scripts += sorted((ROOT / "rootfs/overlay/etc/local.d").iterdir())
        for script in scripts:
            if script.is_dir():  # __pycache__ left by the StatusScreen tests
                continue
            if b"python" in script.read_bytes().split(b"\n", 1)[0]:
                compile(script.read_text(), str(script), "exec")  # syntax only, no .pyc
                continue
            r = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")

    def test_i14init_uses_only_shipped_applets(self):
        b = load("i14boot2", "scripts/build-i14-boot.py")
        text = (ROOT / "initramfs/i14init").read_text() + (ROOT / "initramfs/i14charge").read_text()
        for tool in ("mount", "umount", "mdev", "grep", "cat", "mkdir", "rm", "readlink", "switch_root", "date",
                     "sync", "mv", "reboot", "poweroff", "dd", "od", "tr", "tail", "sleep"):
            self.assertIn(tool, b.APPLETS)
        if not all(p.is_file() for p in (b.BUSYBOX, b.E2FSCK)):
            self.skipTest("requires local BusyBox and e2fsck build inputs")
        ours = b.entries(RESCUE_HASH_FIXTURE)
        self.assertEqual(ours["i14/charge"][1], (ROOT / "initramfs/i14charge").read_bytes())
        self.assertIn(b"broken_system_clock = 1", ours["i14/e2fsck.conf"][1])
        self.assertEqual(ours["i14/bin/e2fsck"][0] & 0o777, 0o755)


class BatteryGuard(unittest.TestCase):
    def run_guard(self, capacity, status, charger_type):
        import os, tempfile
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "bat").mkdir()
            for name, value in (("capacity", capacity), ("status", status), ("batt_vol", "3500")):
                (d / "bat" / name).write_text(value + "\n")
            (d / "type").write_text(charger_type + "\n")
            (d / "cmd").write_text("")
            (d / "bin").mkdir()
            for stub, body in (("poweroff", "echo off > %s/poweroff" % d), ("logger", ":"),
                               ("sleep", "exit 99")):  # sleep ends the loop after one round
                (d / "bin" / stub).write_text("#!/bin/sh\n" + body + "\n")
                (d / "bin" / stub).chmod(0o755)
            env = dict(os.environ, PATH=f"{d}/bin:/usr/bin:/bin", I14_BATTERY=str(d / "bat"),
                       I14_CHARGER_TYPE=str(d / "type"), I14_CURRENT_CMD=str(d / "cmd"), I14_LOG=str(d / "log"))
            # Run one real iteration; fake sysfs and poweroff, never a host shutdown.
            env["PYTHONPATH"] = str(ROOT / "rootfs/overlay/usr/lib")
            code = ("import os, subprocess; from i14ui.charging import Guard; "
                    "g=Guard(battery=os.environ['I14_BATTERY'], charger_type=os.environ['I14_CHARGER_TYPE'], "
                    "command=os.environ['I14_CURRENT_CMD'], log=os.environ['I14_LOG']); "
                    "off=g.tick(); subprocess.run(['poweroff']) if off else None")
            r = subprocess.run([sys.executable, "-B", "-c", code], env=env,
                               capture_output=True, text=True, timeout=20)
            return (d / "poweroff").exists(), (d / "cmd").read_text().strip(), r

    def test_powers_off_when_empty_and_not_charging(self):
        off, cmd, _ = self.run_guard("5", "Discharging", "0")
        self.assertTrue(off)
        self.assertEqual(cmd, "")  # no periodic legacy command writes

    def test_keeps_running_while_charging(self):
        off, cmd, _ = self.run_guard("4", "Charging", "2")
        self.assertFalse(off)
        self.assertEqual(cmd, "")  # BCCT policy keeps stock USB limits

    def test_powers_off_when_charging_does_not_keep_up(self):
        off, _, _ = self.run_guard("3", "Charging", "2")
        self.assertTrue(off)

    def test_keeps_running_above_threshold(self):
        off, _, _ = self.run_guard("6", "Discharging", "1")
        self.assertFalse(off)


class ChargeHold(unittest.TestCase):
    """i14-charge-hold against fake sysfs files, with a 0.1 s step."""

    def setUp(self):
        import os, tempfile
        self.tmp = tempfile.TemporaryDirectory()
        d = self.d = pathlib.Path(self.tmp.name)
        (d / "bat").mkdir()
        self.set(4300, 100)
        (d / "type").write_text("2\n")
        (d / "cmd").write_text("")
        self.env = dict(os.environ, I14_BATTERY=str(d / "bat"), I14_CHARGER_TYPE=str(d / "type"),
                        I14_CURRENT_CMD=str(d / "cmd"), I14_HOLD_PID=str(d / "pid"),
                        I14_HOLD_LOG=str(d / "log"), I14_HOLD_STEP="0.1")

    def tearDown(self):
        import os, time
        pid = int((self.d / "pid").read_text()) if (self.d / "pid").exists() else None
        self.hold("stop")
        for _ in range(50):  # the TERM trap still writes the log after kill returns
            try:
                os.kill(pid, 0) if pid else None
            except ProcessLookupError:
                break
            if not pid:
                break
            time.sleep(0.05)
        self.tmp.cleanup()

    def set(self, mv, cap):
        (self.d / "bat" / "batt_vol").write_text(f"{mv}\n")
        (self.d / "bat" / "capacity").write_text(f"{cap}\n")
        (self.d / "bat" / "status").write_text("Full\n")

    def hold(self, *args):
        return subprocess.run(["sh", str(ROOT / "rootfs/overlay/usr/sbin/i14-charge-hold"), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def cmd_becomes(self, want):
        import time
        for _ in range(50):
            if (self.d / "cmd").read_text().strip() == want:
                return True
            time.sleep(0.05)
        return False

    def test_discharges_above_band_and_charges_at_the_bottom(self):
        self.assertEqual(self.hold("start", "3950", "4050").returncode, 0)
        self.assertTrue(self.cmd_becomes("1 1"))
        self.set(4000, 80)  # inside the band: keeps discharging
        import time
        time.sleep(0.3)
        self.assertEqual((self.d / "cmd").read_text().strip(), "1 1")
        self.set(3950, 70)
        self.assertTrue(self.cmd_becomes("1 0"))

    def test_reasserts_discharge_after_the_guard_reset_it(self):
        self.hold("start")
        self.assertTrue(self.cmd_becomes("1 1"))
        self.set(4000, 80)  # in the band, still discharging
        (self.d / "cmd").write_text("1 0\n")  # i14-battery-guard after a charger-type change
        self.assertTrue(self.cmd_becomes("1 1"))

    def test_low_capacity_always_charges(self):
        self.set(4100, 15)
        self.hold("start")
        self.assertTrue(self.cmd_becomes("1 0"))

    def test_stop_turns_charging_on(self):
        self.hold("start")
        self.assertTrue(self.cmd_becomes("1 1"))
        r = self.hold("stop")
        self.assertEqual(r.returncode, 0)
        self.assertEqual((self.d / "cmd").read_text().strip(), "1 0")
        self.assertFalse((self.d / "pid").exists())
        self.assertIn("not running", self.hold("status").stdout)

    def test_refuses_a_bad_band(self):
        self.assertEqual(self.hold("start", "3600", "4000").returncode, 2)
        self.assertEqual(self.hold("start", "4000", "4300").returncode, 2)
        self.assertFalse((self.d / "pid").exists())


def load_script(name, path):
    import importlib.machinery
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class StatusScreen(unittest.TestCase):
    def font(self, tmp):
        # 2 glyphs, 8x2 px: glyph 0 = "?", glyph 1 = "A" and "\u00c4" (unicode table)
        glyphs = bytes([0x00, 0x00, 0x81, 0xFF])
        table = b"?\xff" + "A\u00c4".encode() + b"\xff"
        header = b"\x72\xb5\x4a\x86" + struct.pack("<7I", 0, 32, 1, 2, 2, 2, 8)
        path = pathlib.Path(tmp) / "f.psf.gz"
        path.write_bytes(gzip.compress(header + glyphs + table))
        return path

    def test_font_unicode_table_and_rows(self):
        import tempfile
        s = load_script("i14status", "rootfs/overlay/usr/sbin/i14-status")
        with tempfile.TemporaryDirectory() as d:
            f = s.Font(self.font(d))
        self.assertEqual(f.map["A"], 1)
        self.assertEqual(f.map["\u00c4"], 1)
        on, off = bytes((1, 2, 3)) + b"\xff", b"\0\0\0\xff"
        rows = f.rows("A", 1, (1, 2, 3))
        self.assertEqual(rows[0], on + off * 6 + on)  # 0x81
        self.assertEqual(rows[1], on * 8)             # 0xFF
        self.assertEqual(len(f.rows("A", 2, (1, 2, 3))), 4)  # scale doubles rows
        self.assertEqual(f.rows("Z", 1, (1, 2, 3))[1], off * 8)  # unknown -> "?"

    def test_status_text_from_battery_files(self):
        import tempfile
        s = load_script("i14status2", "rootfs/overlay/usr/sbin/i14-status")
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for name, value in (("capacity", "71"), ("status", "Charging"), ("batt_vol", "4012")):
                (d / name).write_text(value + "\n")
            (d / "fg").write_text("-6112\n")
            s.BAT, s.FG_CURRENT = str(d), str(d / "fg")
            s.addresses = lambda: {"wlan0": "192.0.2.5", "rndis0": "172.16.42.1"}
            s.ssid = lambda: "ExampleNet"
            texts = [line[0] for line in s.status_lines()]
        for expected in ("Accu 71%", "laden, +611 mA", "4.01 V", "ExampleNet", "192.0.2.5", "172.16.42.1"):
            self.assertIn(expected, texts)


class StatusKeys(unittest.TestCase):
    """Power-key handling of i14-status with fake input, screen and clock."""

    def run_keys(self, events, key_is_down):
        import types
        s = load_script("i14status_keys", "rootfs/overlay/usr/sbin/i14-status")
        calls = {"shutdown": 0, "select": 0}

        class Stop(Exception):
            pass

        def fake_select(r, w, x, timeout=None):
            calls["select"] += 1
            if calls["select"] > 3:
                raise Stop
            return (r, [], []) if calls["select"] == 1 else ([], [], [])

        data = b"".join(struct.pack("qqHHi", int(t), int(round(t % 1 * 1e6)), *ev)
                        for t, ev in ((t, (1, 116, v)) if isinstance(v, int) else (t, v) for t, v in events))
        s.select = types.SimpleNamespace(select=fake_select)
        s.os = types.SimpleNamespace(read=lambda fd, n: data)
        s.key_down = lambda fd: key_is_down
        s.status_lines = lambda: []
        s.shutdown = lambda screen: calls.__setitem__("shutdown", calls["shutdown"] + 1)
        screen = types.SimpleNamespace(on=False, draw=lambda lines: None,
                                       power=lambda on: setattr(screen, "on", on))
        try:
            s.run(screen, 0)
        except Stop:
            pass
        return calls["shutdown"]

    def test_queued_long_press_powers_off(self):
        self.assertEqual(self.run_keys([(100.0, 1), (103.5, 0)], key_is_down=False), 1)

    def test_queued_short_press_does_not_power_off(self):
        self.assertEqual(self.run_keys([(100.0, 1), (100.4, 0)], key_is_down=False), 0)

    def test_lost_release_does_not_power_off(self):
        import time as _t
        old = _t.monotonic() - 10  # pressed long ago, release never arrived, key is up now
        self.assertEqual(self.run_keys([(old, 1)], key_is_down=False), 0)

    def test_old_press_survives_no_drop(self):
        # lost release, then SYN_DROPPED, a fresh press and SYN_REPORT: the 10 s old press must not count
        import time as _t
        old, now = _t.monotonic() - 10, _t.monotonic()
        events = [(old, 1), (now, (0, 3, 0)), (now, (0, 0, 0))]  # the new press itself was lost too
        self.assertEqual(self.run_keys(events, key_is_down=True), 0)

    def test_held_key_powers_off(self):
        import time as _t
        self.assertEqual(self.run_keys([(_t.monotonic() - 10, 1)], key_is_down=True), 1)


if __name__ == "__main__":
    unittest.main()
