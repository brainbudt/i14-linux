"""Synthetic input checks for builders, with no phone or private build inputs."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ToolArchive(unittest.TestCase):
    def test_optimization_cannot_skip_compiler_archive_hash(self):
        for script in ("build-pmic-id.py", "build-pwrap-status.py"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                scripts = work / "scripts"
                scripts.mkdir()
                for name in (script, "i14_build_inputs.py"):
                    source = ROOT / "scripts" / name
                    if source.exists():
                        shutil.copyfile(source, scripts / name)
                archive = work / "artifacts/build-tools/zig-aarch64-macos-0.15.2.tar.xz"
                archive.parent.mkdir(parents=True)
                archive.write_bytes(b"wrong compiler fixture")
                out = work / "out"
                result = subprocess.run([sys.executable, "-O", scripts / script, out],
                                        capture_output=True, text=True, timeout=8)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SHA-256 mismatch", result.stderr)
                self.assertFalse(out.exists())


class RescueInputs(unittest.TestCase):
    def test_rescue_password_input_requires_private_regular_file_and_crypt_hash(self):
        internal = ROOT / "scripts/build-i14-boot.py"
        spec = importlib.util.spec_from_file_location("i14_boot_builder", internal)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rescue-password.hash"
            fixture = b"$6$fixture$" + b"x" * 86
            path.write_bytes(fixture + b"\n")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "private"):
                builder.read_rescue_hash(path)
            path.chmod(0o600)
            self.assertEqual(builder.read_rescue_hash(path), fixture)
            link = path.with_name("link.hash")
            link.symlink_to(path)
            with self.assertRaises(OSError):
                builder.read_rescue_hash(link)
            path.write_bytes(b"not-a-crypt-hash\n")
            with self.assertRaisesRegex(ValueError, "SHA-512"):
                builder.read_rescue_hash(path)


class Manifest(unittest.TestCase):
    def test_image_and_input_digests_are_labeled_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            output = work / "out"
            output.mkdir()
            (output / "rootfs.img").write_bytes(b"fixture")
            for name in ("minirootfs.tar.gz", "system.img", "build-alpine-rootfs.sh", "configure-dns.py",
                         "make-android-props.py", "write-build-manifest.py", "android-wifi.sha256",
                         "authorized_keys"):
                (work / name).write_bytes(name.encode())
            (work / "overlay").mkdir()
            (work / "overlay/config").write_text("fixture")
            packages = work / "packages"
            packages.write_text("unbound-1.25.2-r2\nopenssh-10.3_p1-r2\n")
            script = ROOT / "rootfs/write-build-manifest.py"
            result = subprocess.run([sys.executable, script, work, packages, "a" * 40],
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((output / "build-manifest.json").read_text())
            self.assertEqual(report["source_commit"], "a" * 40)
            self.assertEqual(report["rootfs_sha256"], hashlib.sha256(b"fixture").hexdigest())
            self.assertEqual(report["inputs_sha256"]["system.img"], hashlib.sha256(b"system.img").hexdigest())
            self.assertEqual(report["packages"], ["openssh-10.3_p1-r2", "unbound-1.25.2-r2"])
