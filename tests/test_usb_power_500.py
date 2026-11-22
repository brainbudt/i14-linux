"""Exercise the actual fixed helper in an isolated path-substituted configfs."""
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "tools/i14-usb-power-500"
CONTROLLER = "musb-hdrc.3.auto"


class UsbPower500Tests(unittest.TestCase):
    def fixture(self, *, uid="0", wrong_model=False, wrong_compatible=False,
                readback_failure=False, attribute="0xc0", controller=CONTROLLER,
                extra_function=False, wrong_link=False, wrong_vid=False,
                real_uid="0", slow_child=False, child_power_fault=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        gadget = root / "sys/kernel/config/usb_gadget/i14"
        for directory in ("proc/device-tree", "run", "mock", "dev",
                          "proc/self", f"sys/class/udc/{CONTROLLER}"):
            (root / directory).mkdir(parents=True)
        (root / "proc/self/status").write_text(f"Name:\tsh\nUid:\t{real_uid}\t{uid}\t0\t0\n")
        (root / "proc/mounts").write_text("tmpfs /run tmpfs rw 0 0\n")
        for directory in ("configs/c.1/strings", "strings/0x409",
                          "functions/acm.usb0", "functions/ecm.usb0"):
            (gadget / directory).mkdir(parents=True)
        for name, value in {"idVendor": "0x9999" if wrong_vid else "0x1d6b",
                            "idProduct": "0x0104", "UDC": controller,
                            "strings/0x409/serialnumber": "I14_NATIVE_INIT",
                            "configs/c.1/bmAttributes": attribute,
                            "configs/c.1/MaxPower": "2"}.items():
            (gadget / name).write_text(value + "\n")
        for function in ("acm.usb0", "ecm.usb0"):
            (gadget / f"configs/c.1/{function}").symlink_to(
                "../../functions/" + ("ecm.usb0" if wrong_link else function))
        if extra_function:
            (gadget / "functions/mass_storage.usb0").mkdir()
        (root / "proc/device-tree/model").write_bytes(
            b"OTHER\0" if wrong_model else b"I14 MT6735 initramfs bring-up\0")
        (root / "proc/device-tree/compatible").write_bytes(
            b"OTHER\0" if wrong_compatible else b"magcomm,magc6735-65u-1-m0\0mediatek,mt6735\0")
        (root / f"sys/class/udc/{CONTROLLER}/state").write_text("configured\n")
        mocks = {
            "id": f'case "$1" in -ru) echo {real_uid};; *) echo {uid};; esac\n',
            "stat": "echo tmpfs\n",
            "sha256sum": 'exec /usr/bin/shasum -a 256 "$@"\n',
            "cat": 'case "$1" in */MaxPower)\n' +
                   ('if [ "$(/bin/cat "$1")" = 500 ]; then echo 2; exit 0; fi\n' if readback_failure else '') +
                   ';; esac\nexec /bin/cat "$@"\n',
            "sleep": 'value=$(/bin/cat "$TEST_ROOT/sys/kernel/config/usb_gadget/i14/UDC")\n'
                     'printf "<%s>\\n" "$value" >>"$TEST_ROOT/sequence"\n/bin/sleep 0.02\n',
        }
        if slow_child:
            mocks["sleep"] = mocks["sleep"].replace("0.02", "0.2")
        if child_power_fault:
            mocks["sleep"] += 'echo 2 >"$TEST_ROOT/sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower"\n'
        for name, body in mocks.items():
            path = root / "mock" / name
            path.write_text("#!/bin/sh\n" + body)
            path.chmod(0o755)
        code = SOURCE.read_text()
        code = re.sub(r"/sys/|/proc/|/run/", lambda m: str(root) + m[0], code)
        script = root / "helper"
        script.write_text(code)
        self.root, self.gadget, self.script = root, gadget, script
        self.env = dict(os.environ, TEST_ROOT=str(root), PATH=str(root / "mock") + ":" + os.environ["PATH"])
        return root, gadget

    def execute(self, args=(), hup=False):
        result = subprocess.run(["/bin/sh", str(self.script), *args], env=self.env,
                                capture_output=True, text=True, timeout=3)
        if hup:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pidfile, = (self.root / "run").glob("i14-usb-power-500.*/pid")
            os.kill(int(pidfile.read_text()), signal.SIGHUP)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            logs = list((self.root / "run").glob("i14-usb-power-500.*/log"))
            if result.returncode or (logs and ("DONE" in logs[0].read_text() or "ERROR" in logs[0].read_text())):
                break
            time.sleep(0.01)
        return result

    def test_only_applets_available_in_native_busybox_are_needed(self):
        root, gadget = self.fixture()
        for unavailable in ("id", "cmp", "stat", "mktemp"):
            path = root / "mock" / unavailable
            path.write_text('#!/bin/sh\necho unavailable >>"$TEST_ROOT/unavailable"\nexit 97\n')
            path.chmod(0o755)
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((root / "unavailable").exists())
        self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "500\n")

    def test_proc_uid_and_ram_mount_guards_fail_before_mutation(self):
        for status, mounts in (("Uid:\t0\t0\t1\t0\n", "tmpfs /run tmpfs rw 0 0\n"),
                               ("Uid:\t0\t0\t0\t1\n", "tmpfs /run tmpfs rw 0 0\n"),
                               ("Name:\tsh\n", "tmpfs /run tmpfs rw 0 0\n"),
                               ("Uid:\t0\t0\t0\t0\n", "/dev/root /run ext4 rw 0 0\n"),
                               ("Uid:\t0\t0\t0\t0\n", "tmpfs /other tmpfs rw 0 0\n")):
            with self.subTest(status=status, mounts=mounts):
                root, gadget = self.fixture()
                (root / "proc/self/status").write_text(status)
                (root / "proc/mounts").write_text(mounts)
                self.assertNotEqual(self.execute().returncode, 0)
                self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "2\n")
                self.assertFalse(list((root / "run").iterdir()))

    def test_native_initramfs_rootfs_run_is_accepted_as_ram(self):
        # Native boot f43f5407 (2026-11-01): /run is a plain directory on the initramfs rootfs.
        root, gadget = self.fixture()
        (root / "proc/mounts").write_text(
            "rootfs / rootfs rw 0 0\nproc /proc proc rw,relatime 0 0\nsysfs /sys sysfs rw,relatime 0 0\n"
            "configfs /sys/kernel/config configfs rw,relatime 0 0\n")
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "500\n")

    def test_non_ram_run_or_root_is_refused_before_mutation(self):
        for mounts in ("/dev/root / ext4 rw 0 0\n",
                       "rootfs / rootfs rw 0 0\n/dev/mmcblk0p20 /run ext4 rw 0 0\n",
                       "rootfs / rootfs rw 0 0\n/dev/root / ext4 rw 0 0\n",
                       "tmpfs /run tmpfs rw 0 0\n/dev/mmcblk0p20 /run ext4 rw 0 0\n",
                       "tmpfs / rootfs rw 0 0\n",
                       "rootfs / rootfs rw 0 0\n/dev/mmcblk0p20 /run/data ext4 rw 0 0\n",
                       "tmpfs /run tmpfs rw 0 0\n/dev/mmcblk0p20 /run/data ext4 rw 0 0\n"):
            with self.subTest(mounts=mounts):
                root, gadget = self.fixture()
                (root / "proc/mounts").write_text(mounts)
                self.assertNotEqual(self.execute().returncode, 0)
                self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "2\n")
                self.assertFalse(list((root / "run").iterdir()))

    def test_symlinked_run_is_refused_before_mutation(self):
        root, gadget = self.fixture()
        (root / "run").rename(root / "persistent")
        (root / "run").symlink_to(root / "persistent", target_is_directory=True)
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "2\n")
        self.assertFalse(list((root / "persistent").iterdir()))

    def test_writes_500_then_exactly_one_same_controller_reenumeration(self):
        root, gadget = self.fixture()
        result = self.execute()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "500\n")
        self.assertEqual((gadget / "configs/c.1/bmAttributes").read_text(), "0xc0\n")
        self.assertEqual((gadget / "UDC").read_text(), CONTROLLER + "\n")
        self.assertEqual((root / "sequence").read_text().splitlines(), [f"<{CONTROLLER}>", "<>"])
        log, = (root / "run").glob("i14-usb-power-500.*/log")
        self.assertIn("DONE", log.read_text())
        self.assertTrue((log.parent / "pid").read_text().strip().isdigit())

    def test_bad_identity_root_or_gadget_never_mutates(self):
        for kwargs in ({"uid": "1000"}, {"real_uid": "1000"}, {"wrong_model": True}, {"wrong_compatible": True},
                       {"wrong_vid": True}, {"wrong_link": True}, {"extra_function": True},
                       {"attribute": "0x80"}, {"controller": "other-controller"}):
            with self.subTest(kwargs=kwargs):
                root, gadget = self.fixture(**kwargs)
                before = {p: p.read_bytes() for p in gadget.rglob("*") if p.is_file()}
                self.assertNotEqual(self.execute().returncode, 0)
                self.assertEqual(before, {p: p.read_bytes() for p in before})
                self.assertFalse(list((root / "run").iterdir()))
                self.assertFalse((root / "sequence").exists())

    def test_other_exact_metadata_and_controller_count_are_checked_before_write(self):
        for field, value in (("idProduct", "0x0001"), ("strings/0x409/serialnumber", "OTHER")):
            with self.subTest(field=field):
                root, gadget = self.fixture()
                (gadget / field).write_text(value + "\n")
                self.assertNotEqual(self.execute().returncode, 0)
                self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "2\n")
                self.assertFalse((root / "sequence").exists())
        root, gadget = self.fixture()
        (root / "sys/class/udc/other").mkdir()
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertEqual((gadget / "configs/c.1/MaxPower").read_text(), "2\n")

    def test_child_survives_hup_and_remains_one_bounded_reenumeration(self):
        root, gadget = self.fixture(slow_child=True)
        self.assertEqual(self.execute(hup=True).returncode, 0)
        log, = (root / "run").glob("i14-usb-power-500.*/log")
        self.assertIn("DONE", log.read_text())
        self.assertEqual((gadget / "UDC").read_text(), CONTROLLER + "\n")
        self.assertEqual(len((root / "sequence").read_text().splitlines()), 2)

    def test_child_recheck_failure_does_not_unbind(self):
        root, gadget = self.fixture(child_power_fault=True)
        self.assertEqual(self.execute().returncode, 0)  # Parent reports launch, not completion.
        log, = (root / "run").glob("i14-usb-power-500.*/log")
        self.assertIn("ERROR", log.read_text())
        self.assertNotIn("DONE", log.read_text())
        self.assertEqual((gadget / "UDC").read_text(), CONTROLLER + "\n")
        self.assertEqual((root / "sequence").read_text().splitlines(), [f"<{CONTROLLER}>"])

    def test_readback_failure_cannot_unbind(self):
        root, gadget = self.fixture(readback_failure=True)
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertEqual((gadget / "UDC").read_text(), CONTROLLER + "\n")
        self.assertFalse((root / "sequence").exists())

    def test_arguments_and_already_changed_power_are_refused(self):
        root, gadget = self.fixture()
        self.assertNotEqual(self.execute(("arbitrary",)).returncode, 0)
        (gadget / "configs/c.1/MaxPower").write_text("500\n")
        self.assertNotEqual(self.execute().returncode, 0)
        self.assertFalse((root / "sequence").exists())


if __name__ == "__main__":
    unittest.main()
