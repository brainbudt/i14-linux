#!/usr/bin/env python3
"""Inert eMMC guard fixtures: regular temp files stand in for block nodes."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class StorageReadOnlyTests(unittest.TestCase):
    def run_guard(self, capacity="15634268160", failed="", present=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name in ("dev", "run", "mock", "state"):
            (root / name).mkdir()
        (root / "dev/kmsg").touch()
        if present:
            for name in ("mmcblk0", "mmcblk0p1", "mmcblk0boot0", "mmcblk0boot1", "mmcblk0rpmb"):
                (root / "dev" / name).touch()
        mock = root / "mock/blockdev"
        mock.write_text('''#!/bin/sh
name=${2##*/}
echo "$1 $name" >>"$TEST_ROOT/calls"
case "$1" in
 --setro) [ "$name" != "$TEST_FAILED" ] || exit 1; echo 1 >"$TEST_ROOT/state/$name" ;;
 --getro) if [ -f "$TEST_ROOT/state/$name" ]; then cat "$TEST_ROOT/state/$name"; else echo 0; fi ;;
 --getsize64) echo "$TEST_CAPACITY" ;;
 *) exit 99 ;;
esac
''')
        mock.chmod(0o755)
        sleep = root / "mock/sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        code = (HERE / "storage-readonly").read_text().replace('[ -b "', '[ -f "')
        code = code.replace(">/dev/kmsg", ">>/dev/kmsg")
        code = re.sub(r"/dev/|/run/", lambda match: str(root) + match[0], code)
        script = root / "guard"
        script.write_text(code)
        env = dict(os.environ, TEST_ROOT=str(root), TEST_CAPACITY=capacity,
                   TEST_FAILED=failed, PATH=str(root / "mock") + ":" + os.environ["PATH"])
        result = subprocess.run(["/bin/sh", str(script)], env=env, capture_output=True, text=True, timeout=3)
        return root, result

    def test_ready_requires_exact_capacity_and_every_device_ro_without_rpmb(self):
        root, result = self.run_guard()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ready = (root / "run/i14-storage-ro.ready").read_text()
        self.assertIn("capacity_bytes=15634268160", ready)
        for name in ("mmcblk0", "mmcblk0p1", "mmcblk0boot0", "mmcblk0boot1"):
            self.assertIn(name + " ro=1", ready)
        calls = (root / "calls").read_text()
        self.assertNotIn("rpmb", calls)
        self.assertTrue(all(line.split()[0] in ("--setro", "--getro", "--getsize64") for line in calls.splitlines()))

    def test_capacity_mismatch_keeps_all_seen_devices_ro_and_no_ready_marker(self):
        root, result = self.run_guard(capacity="15634268161")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "run/i14-storage-ro.ready").exists())
        self.assertIn("I14_STORAGE_STOPPED", (root / "dev/kmsg").read_text())
        self.assertEqual(len(list((root / "state").iterdir())), 4)

    def test_ro_failure_still_protects_other_devices_without_retry_or_ready(self):
        root, result = self.run_guard(failed="mmcblk0p1")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "run/i14-storage-ro.ready").exists())
        calls = (root / "calls").read_text()
        self.assertEqual(calls.count("--setro mmcblk0p1\n"), 1)
        self.assertEqual(len(list((root / "state").iterdir())), 3)

    def test_absent_hardware_times_out_without_blockdev_calls(self):
        root, result = self.run_guard(present=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((root / "calls").exists())
        self.assertIn("I14_STORAGE_TIMEOUT", (root / "dev/kmsg").read_text())


if __name__ == "__main__":
    unittest.main()
