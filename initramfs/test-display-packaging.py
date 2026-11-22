#!/usr/bin/env python3
"""Offline archive and inert init checks; never load host modules or open sockets."""
import gzip
import os
from pathlib import Path
import re
import runpy
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest

HERE = Path(__file__).resolve().parent
MODULES = HERE.parent / "artifacts/display-touch-20261030T082847Z/modules"
CONTROL_MODULES = HERE.parent / "artifacts/display-touch-20261030T082847Z/control-modules"
LINKS = ("bin/sh bin/mount bin/dmesg sbin/ip sbin/ifconfig sbin/route sbin/udhcpc "
         "sbin/mdev usr/bin/nc usr/sbin/httpd usr/sbin/telnetd usr/bin/tcpsvd "
         "bin/ln usr/sbin/udhcpd sbin/blockdev bin/kill sbin/insmod").split()


def elf(kind=2, machine=183, dynamic=False):
    data = bytearray(120)
    data[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", data, 16, kind, machine)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<I", data, 64, 3 if dynamic else 1)
    return data


def unpack(data):
    raw = gzip.decompress(data)
    result = {}
    offset = 0
    while True:
        fields = [int(raw[offset+6+i*8:offset+14+i*8], 16) for i in range(13)]
        name = raw[offset+110:offset+109+fields[11]].decode()
        start = (offset+110+fields[11]+3)&~3
        if name == "TRAILER!!!":
            return result
        result[name] = fields, raw[start:start+fields[6]], offset
        offset = (start+fields[6]+3)&~3


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.build = self.root / "build"
        self.build.mkdir()
        (self.build / "busybox").write_bytes(elf())
        (self.build / "busybox.config").write_bytes((HERE / "busybox.config").read_bytes())
        (self.build / "busybox.links").write_text("\n".join(LINKS))
        self.status = self.root / "status-binary"
        self.status.write_bytes(elf())

    def run_tool(self, name, args=(), *, optimized=False):
        base = [sys.executable, *(["-O"] if optimized else []), str(HERE / name), str(self.build)]
        if name == "make-cpio.py":
            base.append(str(HERE / "init"))
        return subprocess.run(base + list(map(str, args)), capture_output=True, text=True)

    def test_existing_cli_and_optional_status_are_deterministic(self):
        for args in ((), ("--status", self.status)):
            with self.subTest(args=args):
                result = self.run_tool("make-cpio.py", args)
                self.assertEqual(result.returncode, 0, result.stderr)
                first = (self.build / "initramfs.cpio.gz").read_bytes()
                self.assertEqual(self.run_tool("make-cpio.py", args).returncode, 0)
                self.assertEqual(first, (self.build / "initramfs.cpio.gz").read_bytes())
                result = self.run_tool("verify-cpio.py", args)
                self.assertEqual(result.returncode, 0, result.stderr)
                entries = unpack(first)
                self.assertEqual("usr/bin/i14-status" in entries, bool(args))
                if args:
                    fields, data, _ = entries["usr/bin/i14-status"]
                    self.assertEqual(data, self.status.read_bytes())
                    self.assertEqual(fields[1], stat.S_IFREG | 0o755)
                    self.assertEqual(fields[2:4], [0, 0])
                    self.assertNotEqual(self.run_tool("verify-cpio.py").returncode, 0)

    def test_status_rejects_nonstatic_wrong_arch_and_nonexecutables(self):
        for data in (elf(dynamic=True), elf(machine=62), elf(kind=1), b"not-elf"):
            self.status.write_bytes(data)
            self.assertNotEqual(self.run_tool("make-cpio.py", ("--status", self.status)).returncode, 0)

    def test_module_hash_is_mandatory_under_optimization(self):
        module = self.root / "evdev.ko"
        data = (elf(kind=1) + b"\0vermagic=7.0.10-i14-bringup SMP preempt mod_unload aarch64\0depends=\0")
        module.write_bytes(data.ljust(26560, b"\0"))
        args = ("--evdev-module", module)
        with self.subTest(tool="make-cpio.py"):
            result = self.run_tool("make-cpio.py", args, optimized=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exact module hash/size mismatch", result.stderr)

        result = self.run_tool("make-cpio.py", optimized=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_tool("verify-cpio.py", optimized=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.build / "initramfs.cpio.gz"
        entries = {name: (fields[1], body, *fields[9:11])
                   for name, (fields, body, _) in unpack(archive.read_bytes()).items()}
        entries["lib/modules/i14"] = (stat.S_IFDIR | 0o755, b"", 0, 0)
        entries["lib/modules/i14/evdev.ko"] = (stat.S_IFREG | 0o644, module.read_bytes(), 0, 0)
        pack = runpy.run_path(str(HERE / "make-stock-charge-cpio.py"))["archive"]
        archive.write_bytes(pack(entries))
        with self.subTest(tool="verify-cpio.py"):
            result = self.run_tool("verify-cpio.py", args, optimized=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("exact module hash/size mismatch", result.stderr)

    def test_verifier_requires_explicit_debug_image(self):
        result = self.run_tool("make-cpio.py", ("--debug",), optimized=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_tool("verify-cpio.py", optimized=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected debug marker", result.stderr)
        result = self.run_tool("verify-cpio.py", ("--debug",), optimized=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pwm_and_backlight_require_an_explicit_pair(self):
        for option in ("--pwm-module", "--backlight-module"):
            result = self.run_tool("make-cpio.py", (option, self.status))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be supplied together", result.stderr)

    @unittest.skipUnless((CONTROL_MODULES / "pwm-mtk-disp.ko").is_file(), "exact local control artifacts unavailable")
    def test_exact_pwm_pair_bytes_modes_ownership_and_independent_guards(self):
        args = ("--pwm-module", CONTROL_MODULES / "pwm-mtk-disp.ko", "--backlight-module", CONTROL_MODULES / "pwm_bl.ko")
        result = self.run_tool("make-cpio.py", args)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_tool("verify-cpio.py", args)
        self.assertEqual(result.returncode, 0, result.stderr)
        original = (self.build / "initramfs.cpio.gz").read_bytes()
        entries = unpack(original)
        for name in ("pwm-mtk-disp.ko", "pwm_bl.ko"):
            fields, data, _ = entries["lib/modules/i14/" + name]
            self.assertEqual(fields[1], stat.S_IFREG | 0o644)
            self.assertEqual(fields[2:4], [0, 0])
            self.assertEqual(data, (CONTROL_MODULES / name).read_bytes())
        for omitted in ((), args[:2], args[2:]):
            self.assertNotEqual(self.run_tool("verify-cpio.py", omitted).returncode, 0)
        raw = bytearray(gzip.decompress(original))
        offset = entries["lib/modules/i14/pwm_bl.ko"][2]
        raw[offset+14:offset+22] = f"{stat.S_IFREG | 0o755:08x}".encode()
        (self.build / "initramfs.cpio.gz").write_bytes(gzip.compress(raw, mtime=0))
        self.assertNotEqual(self.run_tool("verify-cpio.py", args).returncode, 0)
        altered = self.root / "pwm_bl.ko"
        data = bytearray((CONTROL_MODULES / "pwm_bl.ko").read_bytes())
        data[-1] ^= 1
        altered.write_bytes(data)
        bad_args = (*args[:3], altered)
        self.assertNotEqual(self.run_tool("make-cpio.py", bad_args).returncode, 0)
        self.assertNotEqual(self.run_tool("verify-cpio.py", bad_args).returncode, 0)

    @unittest.skipUnless((MODULES / "evdev.ko").is_file(), "exact local module artifacts unavailable")
    def test_exact_modules_and_independent_verifier_mode_and_content_guards(self):
        args = ("--evdev-module", MODULES / "evdev.ko", "--edt-ft5x06-module", MODULES / "edt-ft5x06.ko")
        result = self.run_tool("make-cpio.py", args)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_tool("verify-cpio.py", args)
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = self.build / "initramfs.cpio.gz"
        original = archive.read_bytes()
        entries = unpack(original)
        for module in ("evdev.ko", "edt-ft5x06.ko"):
            fields, data, _ = entries["lib/modules/i14/" + module]
            self.assertEqual(data, (MODULES / module).read_bytes())
            self.assertEqual(fields[1], stat.S_IFREG | 0o644)
        self.assertNotEqual(self.run_tool("verify-cpio.py").returncode, 0)
        (self.build / "busybox.links").write_text("\n".join(name for name in LINKS if name != "sbin/insmod"))
        self.assertNotEqual(self.run_tool("make-cpio.py", args).returncode, 0)
        (self.build / "busybox.links").write_text("\n".join(LINKS))
        offset = entries["lib/modules/i14/evdev.ko"][2]
        raw = bytearray(gzip.decompress(original))
        raw[offset+14:offset+22] = f"{stat.S_IFREG | 0o755:08x}".encode()
        archive.write_bytes(gzip.compress(raw, mtime=0))
        self.assertNotEqual(self.run_tool("verify-cpio.py", args).returncode, 0)
        altered = self.root / "evdev.ko"
        data = bytearray((MODULES / "evdev.ko").read_bytes())
        data[-1] ^= 1
        altered.write_bytes(data)
        self.assertNotEqual(self.run_tool("make-cpio.py", ("--evdev-module", altered)).returncode, 0)
        data = bytearray((MODULES / "evdev.ko").read_bytes())
        data[18:20] = struct.pack("<H", 62)
        altered.write_bytes(data)
        self.assertNotEqual(self.run_tool("make-cpio.py", ("--evdev-module", altered)).returncode, 0)


class DisplayInitTests(unittest.TestCase):
    def run_init(self, optional=True, fail_module=False, fail_status=False, lighting=False, fail_lighting=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ("dev", "etc", "proc", "sys", "run", "usr/bin", "lib/modules/i14"):
            (root / name).mkdir(parents=True, exist_ok=True)
        (root / "dev/kmsg").touch()
        (root / "dev/console").touch()
        (root / "proc/cmdline").write_text("i14.trial_reboot=off")
        if optional:
            for module in ("evdev.ko", "edt-ft5x06.ko"):
                (root / "lib/modules/i14" / module).touch()
            if lighting:
                for module in ("pwm-mtk-disp.ko", "pwm_bl.ko"):
                    (root / "lib/modules/i14" / module).touch()
            status = root / "usr/bin/i14-status"
            status.write_text("#!/bin/sh\necho status >>\"$TEST_ROOT/calls\"\necho status-log\nexit " + ("1" if fail_status else "0") + "\n")
            status.chmod(0o755)
        code = (HERE / "init").read_text().replace(">/dev/kmsg", ">>/dev/kmsg")
        code = re.sub(r"/etc/i14-debug|/proc/|/sys/|/run/|/dev/|/lib/modules/|/usr/bin/i14-status",
                      lambda m: str(root) + m[0], code)
        prelude = ("mount(){ :; }; uname(){ echo fixture; }; ip(){ :; }; "
                   "setsid(){ exit 1; }; sleep(){ exit 0; }; "
                   "sh(){ echo \"$*\" >>\"$TEST_ROOT/calls\"; }; "
                   "insmod(){ echo \"insmod $*\" >>\"$TEST_ROOT/calls\"; "
                   + ('case "$*" in *pwm*) return 1 ;; esac; ' if fail_lighting else '')
                   + "return " + ("1" if fail_module else "0") + "; };\n")
        script = root / "init-test"
        script.write_text(prelude + code)
        with (root / "stdout").open("w") as out:
            subprocess.run(["/bin/sh", str(script)], env=dict(os.environ, TEST_ROOT=str(root)), stdout=out, stderr=out, timeout=3)
        deadline = time.monotonic() + 2
        while optional and time.monotonic() < deadline:
            if (root / "run/i14-status.pid").exists() and "status" in (root / "calls").read_text().splitlines():
                break
            time.sleep(0.01)
        return root

    def test_optional_init_sequence_preserves_usb_on_module_or_status_failure(self):
        for failure in ({}, {"fail_module": True}, {"fail_status": True}):
            with self.subTest(failure=failure):
                root = self.run_init(**failure)
                calls = (root / "calls").read_text().splitlines()
                self.assertEqual(calls[0], "/storage-readonly")
                self.assertEqual(len(calls), 4, calls)
                self.assertTrue(calls[1].endswith("/evdev.ko"), calls)
                self.assertTrue(calls[2].endswith("/edt-ft5x06.ko"), calls)
                self.assertEqual(calls[3], "status")
                self.assertTrue((root / "run/i14-status.pid").read_text().strip().isdigit())
                self.assertIn("status-log", (root / "run/i14-status.log").read_text())
                self.assertIn("I14_USB_HELPER_SKIPPED_NONDEBUG", (root / "dev/kmsg").read_text())
                if failure.get("fail_module"):
                    self.assertIn("I14_DISPLAY_MODULE_FAILED", (root / "dev/kmsg").read_text())
                if failure.get("fail_status"):
                    deadline = time.monotonic() + 1
                    while "I14_STATUS_FAILED" not in (root / "dev/kmsg").read_text() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIn("I14_STATUS_FAILED status=1", (root / "dev/kmsg").read_text())

    def test_absent_optional_files_keep_existing_init_flow(self):
        root = self.run_init(optional=False)
        self.assertEqual((root / "calls").read_text().splitlines(), ["/storage-readonly"])
        self.assertFalse((root / "run/i14-status.pid").exists())
        self.assertFalse((root / "run/i14-status.log").exists())

    def test_optional_lighting_loads_provider_first_and_failure_keeps_ui_and_usb(self):
        for fail in (False, True):
            root = self.run_init(lighting=True, fail_lighting=fail)
            calls = (root / "calls").read_text().splitlines()
            self.assertEqual(calls[0], "/storage-readonly")
            self.assertEqual(len(calls), 6, calls)
            self.assertEqual([Path(line.removeprefix("insmod ")).name for line in calls[1:5]],
                             ["evdev.ko", "edt-ft5x06.ko", "pwm-mtk-disp.ko", "pwm_bl.ko"])
            self.assertEqual(calls[-1], "status")
            log = (root / "dev/kmsg").read_text()
            self.assertIn("I14_USB_HELPER_SKIPPED_NONDEBUG", log)
            self.assertIn("I14_STATUS_STARTED", log)
            self.assertEqual(log.count("I14_DISPLAY_MODULE_FAILED"), 2 if fail else 0)


if __name__ == "__main__":
    unittest.main()
