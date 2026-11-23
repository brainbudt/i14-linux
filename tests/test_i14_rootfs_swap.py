"""Local files only. Exercise the host CLI against the actual initramfs and watcher records."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_i14_rootfs_rollback import ROOT, WATCHER, boot

SCRIPT = ROOT / "scripts/i14-rootfs-swap.py"
BOOT = "02fc6e24-8d75-462a-8865-21f3ffeaa305"
OTHER_BOOT = "247c7dbb-7624-45f7-b7de-3842ef789d32"
STAMP = "src=abc123 at=20261123T123456Z"
KEY = b";;last_queried: 1\n. IN DNSKEY 257 3 8 PUBLIC\n"
OLD, NEW = b"OLD image\n", b"NEW image\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class TrialFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.data = self.base / "data"
        self.data.mkdir()
        self.state = self.base / "state.json"
        self.root = self.base / "candidate-root"
        (self.root / "etc").mkdir(parents=True)
        self.live_key = self.root / "var/lib/unbound/root.key"
        self.live_key.parent.mkdir(parents=True)
        self.live_key.write_bytes(KEY)
        self.stamp = self.root / "etc/i14-rootfs-id"
        self.stamp.write_text(STAMP + "\n")
        self.digest = self.root / "etc/i14-rootfs-root-key.sha256"
        self.digest.write_text(sha(KEY) + "\n")
        self.active = self.data / "rootfs.img"
        self.prev = self.data / "rootfs-prev.img"
        self.new = self.data / "rootfs.img.new"
        self.trial = self.data / "rootfs-trial.img"
        self.marker = self.data / "rootfs-trial"
        self.ack = self.data / "rootfs-trial-ack"
        self.confirmed = self.data / "rootfs-trial-confirmed"
        self.receipt = self.data / "rootfs-trial-root-key-receipt"
        self.capture = self.data / f"rootfs-trial-root-key.{BOOT}"
        self.active.write_bytes(OLD)
        os.link(self.active, self.prev)
        self.new.write_bytes(NEW)

    def cli(self, command, *extra, ok=True):
        self.assertTrue(SCRIPT.is_file(), "host swap CLI is missing")
        args = [sys.executable, "-B", str(SCRIPT), command, "--data", str(self.data),
                "--state", str(self.state)]
        if command in ("stage", "start"):
            args += ["--previous-sha256", sha(OLD), "--candidate-sha256", sha(NEW),
                     "--root-key-sha256", sha(KEY), "--stamp", STAMP, "--root", str(self.root)]
        elif command in ("verify-trial", "verify-ack", "verify-confirmed"):
            args += ["--root", str(self.root), "--boot-id", BOOT]
        r = subprocess.run([*args, *extra], capture_output=True, text=True, timeout=8)
        if ok:
            self.assertEqual(r.returncode, 0, r.stderr)
        else:
            self.assertNotEqual(r.returncode, 0, r.stdout)
            self.assertNotIn("Traceback", r.stderr)
        return r.stdout

    def started(self):
        self.cli("stage")
        self.cli("verify-staged")
        self.assertEqual(boot(self.base)[0], ["mounted NEW", "booted"])
        (self.base / "boot-id").write_text(BOOT + "\n")
        # Use the watcher's own pre-OpenRC capture, including its three-line receipt.
        self.watcher('root_key_capture')
        self.live_key.write_bytes(b"Unbound advanced its RFC5011 state\n")

    def watcher(self, action):
        source = WATCHER.read_text().split('\ncase "$1" in\nstart)')[0]
        env = dict(os.environ, I14_TRIAL_DIR=str(self.data), I14_TRIAL_ROOT=str(self.root),
                   I14_BOOT_ID=str(self.base / "boot-id"), I14_UPTIME=str(self.base / "uptime"),
                   I14_TRIAL_CONF=str(self.base / "test.conf"))
        # Portable sha256sum for macOS. No logging or global sync during a fixture.
        (self.base / "uptime").write_text("620.00 0\n")
        action = ('\nsha256sum() { shasum -a 256 "$@"; }\nsync() { :; }\n'
                  'log() { :; }\n' + action)
        r = subprocess.run(["sh", "-c", source + action], env=env, capture_output=True, text=True, timeout=8)
        self.assertEqual(r.returncode, 0, r.stderr)

    def acknowledge(self):
        nonce = json.loads(self.state.read_bytes())["nonce"]
        self.ack.write_text(f"ack {nonce} {BOOT}\n")

    def rolled_back(self):
        self.cli("stage")
        for _ in range(2):
            self.assertEqual(boot(self.base)[0], ["mounted NEW", "booted"])
        self.assertEqual(boot(self.base)[0], ["mounted OLD", "booted"])

    def refused_stage(self, *args):
        self.cli("stage", *args, ok=False)
        self.assertEqual(self.active.read_bytes(), OLD)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.state.exists())

    def test_stage_links_images_and_writes_exact_initramfs_record(self):
        previous, candidate = self.active.stat().st_ino, self.new.stat().st_ino
        self.cli("start")
        self.assertEqual(self.prev.stat().st_ino, previous)
        self.assertEqual(self.active.stat().st_ino, candidate)
        self.assertTrue(self.active.samefile(self.trial))
        self.assertFalse(self.new.exists())
        nonce = json.loads(self.state.read_bytes())["nonce"]
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)
        text = f"trial 0 {STAMP} ack={sha(nonce.encode())}".encode()
        self.assertEqual(self.marker.read_bytes(), text.ljust(511) + b"\n")
        self.cli("verify-staged")
        self.cli("stage", ok=False)
        self.cli("verify-staged")

    def test_free_floor_leaves_active_image_untouched(self):
        self.refused_stage("--min-free-bytes", str(2**63))

    def test_concurrent_stage_is_refused(self):
        fd = os.open(self.data, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.refused_stage()
        finally:
            os.close(fd)

    def test_interrupted_final_rename_keeps_recoverable_trial(self):
        swap = runpy.run_path(str(SCRIPT))
        args = ["stage", "--data", str(self.data), "--state", str(self.state), "--root", str(self.root),
                "--stamp", STAMP, "--previous-sha256", sha(OLD), "--candidate-sha256", sha(NEW),
                "--root-key-sha256", sha(KEY)]
        with patch("os.replace", side_effect=OSError("interrupted final rename")):
            with patch("sys.stderr") as stderr:
                self.assertEqual(swap["main"](args), 1)
                self.assertIn("interrupted final rename", str(stderr.write.call_args_list))
        self.assertTrue(self.active.samefile(self.prev))
        self.assertTrue(self.new.samefile(self.trial))
        self.assertEqual(self.marker.stat().st_size, 512)
        self.cli("verify-staged", ok=False)
        self.assertEqual(boot(self.base)[0], ["mounted OLD", "booted"])
        self.cli("verify-rollback")
        self.cli("verify-rollback", "--rehearsal", ok=False)

    def test_missing_previous_image(self):
        self.prev.unlink()
        self.refused_stage()

    def test_candidate_colliding_with_b5(self):
        self.new.unlink()
        os.link(self.prev, self.new)
        self.refused_stage("--candidate-sha256", sha(OLD))

    def test_previous_must_be_the_active_b5_inode(self):
        self.prev.unlink()
        self.prev.write_bytes(OLD)
        self.refused_stage()

    def test_independent_image_or_key_pin_mismatch(self):
        for arg in ("--previous-sha256", "--candidate-sha256", "--root-key-sha256"):
            with self.subTest(arg=arg):
                self.refused_stage(arg, "0" * 64)

    def test_wrong_staged_key_or_stamp(self):
        self.live_key.write_bytes(b"changed")
        self.refused_stage()
        self.live_key.write_bytes(KEY)
        self.refused_stage("--stamp", "src=other at=T")

    def test_symlink_inputs_and_intermediate_root_directories(self):
        for path in (self.active, self.new, self.prev, self.live_key, self.root / "etc"):
            with self.subTest(path=path):
                original = path.with_name(path.name + ".saved")
                path.rename(original)
                path.symlink_to(original)
                self.refused_stage()
                path.unlink()
                original.rename(path)

    def test_refuses_old_trial_names_including_dangling_links(self):
        for path in (self.trial, self.ack, self.confirmed, self.receipt, self.capture,
                     self.data / "rootfs-trial-ack.new", self.data / "rootfs-trial.new",
                     self.data / "rootfs-trial.log"):
            with self.subTest(path=path):
                path.symlink_to(self.base / "missing-target")
                self.refused_stage()
                path.unlink()

    def test_evidence_cannot_be_stored_on_userdata_or_overwritten(self):
        self.cli("stage", "--state", str(self.data / "secret"), ok=False)
        self.assertFalse((self.data / "secret").exists())
        self.state.write_bytes(b"previous trial\n")
        self.cli("stage", ok=False)
        self.assertEqual(self.state.read_bytes(), b"previous trial\n")
        self.assertEqual(self.active.read_bytes(), OLD)

    def test_staged_verification_rejects_replaced_trial_and_modified_candidate(self):
        self.cli("stage")
        self.trial.unlink()
        self.trial.write_bytes(NEW)
        self.cli("verify-staged", ok=False)
        self.trial.unlink()
        os.link(self.active, self.trial)
        self.active.write_bytes(b"wrong")
        self.cli("verify-staged", ok=False)

    def test_verify_staged_refuses_stale_boot_receipt_and_short_record(self):
        self.cli("stage")
        self.receipt.write_bytes(b"stale\n")
        self.cli("verify-staged", ok=False)
        self.receipt.unlink()
        self.marker.write_bytes(self.marker.read_bytes().rstrip() + b"\n")
        self.cli("verify-staged", ok=False)

    def test_dangling_active_record_or_symlinked_key_cannot_confirm(self):
        self.started()
        self.acknowledge()
        self.watcher('confirm "$(marker)"')
        self.marker.symlink_to(self.base / "missing-target")
        self.cli("verify-confirmed", ok=False)
        self.marker.unlink()
        saved = self.base / "saved-key"
        self.capture.rename(saved)
        self.capture.symlink_to(saved)
        self.cli("verify-confirmed", ok=False)

    def test_real_init_rollback_preserves_both_original_inodes(self):
        self.rolled_back()
        self.cli("verify-rollback", "--rehearsal", "--check-restored-sha256")
        self.active.write_bytes(b"changed after boot\n")
        self.cli("verify-rollback", "--check-restored-sha256", ok=False)
        self.acknowledge()
        self.cli("verify-rollback", "--rehearsal", ok=False)

    def test_verify_requires_independent_private_evidence(self):
        self.cli("stage")
        counterfeit = self.data / "copied-state"
        counterfeit.write_bytes(self.state.read_bytes())
        counterfeit.chmod(0o600)
        self.cli("verify-staged", "--state", str(counterfeit), ok=False)
        self.state.chmod(0o644)
        self.cli("verify-staged", ok=False)

    def test_rollback_record_with_candidate_still_active(self):
        self.rolled_back()
        self.active.unlink()
        os.link(self.trial, self.active)
        self.cli("verify-rollback", ok=False)

    def test_rollback_without_original_failed_candidate_inode(self):
        self.rolled_back()
        self.trial.rename(self.data / "kept-for-test")
        self.cli("verify-rollback", ok=False)
        self.trial.write_bytes(NEW)
        self.cli("verify-rollback", ok=False)

    def test_rollback_with_copied_previous_inode(self):
        self.rolled_back()
        self.active.rename(self.data / "kept-for-test")
        self.active.write_bytes(OLD)
        self.cli("verify-rollback", ok=False)

    def test_rollback_requires_current_trial_record_and_consumed_prev(self):
        self.rolled_back()
        os.link(self.active, self.prev)
        self.cli("verify-rollback", ok=False)
        self.prev.unlink()
        self.marker.write_bytes(self.marker.read_bytes().replace(b"ack=", b"old="))
        self.cli("verify-rollback", ok=False)

    def test_boot_receipt_and_ack_match_real_watcher(self):
        self.started()
        self.cli("verify-trial")
        self.cli("verify-staged", ok=False)
        self.cli("verify-ack", ok=False)
        self.acknowledge()
        self.cli("verify-ack")
        self.watcher('ack_ok "$(marker)" && root_key_ok && confirm "$(marker)"')
        self.cli("verify-confirmed")
        self.cli("verify-trial", ok=False)

    def test_incorrect_receipt_or_boot_metadata(self):
        self.started()
        for path in (self.capture, self.receipt, self.digest, self.stamp):
            with self.subTest(path=path):
                saved = path.read_bytes()
                path.unlink()
                self.cli("verify-trial", ok=False)
                path.write_bytes(b"tampered\n")
                self.cli("verify-trial", ok=False)
                path.write_bytes(saved)
        self.cli("verify-trial", "--boot-id", OTHER_BOOT, ok=False)
        self.cli("verify-trial", "--boot-id", "../escape", ok=False)
        for field in ("root_key_boot", "root_key_stamp", "root_key_sha256"):
            with self.subTest(field=field):
                saved = self.receipt.read_bytes()
                self.receipt.write_bytes(saved.replace((field + "=").encode(), (field + "=old").encode()))
                self.cli("verify-trial", ok=False)
                self.receipt.write_bytes(saved)

    def test_ack_must_match_independent_boot_and_nonce(self):
        self.started()
        self.acknowledge()
        saved = self.ack.read_text()
        for invalid in (saved.replace(BOOT, OTHER_BOOT), saved.replace("ack ", "ack wrong"), saved + saved):
            self.ack.write_text(invalid)
            self.cli("verify-ack", ok=False)
        self.ack.write_text(saved)
        self.marker.write_bytes(self.marker.read_bytes().replace(b"ack=", b"bad="))
        self.cli("verify-ack", ok=False)

    def test_incomplete_or_replayed_confirmation(self):
        self.started()
        self.acknowledge()
        old = self.marker.read_bytes()
        self.watcher('confirm "$(marker)"')
        self.marker.write_bytes(old)
        self.cli("verify-confirmed", ok=False)
        self.marker.unlink()
        record = self.confirmed.read_text()
        for invalid in (record.replace(BOOT, OTHER_BOOT), record.replace("trial 1", "trial 0"),
                        record.replace("ack=", "old="), record + record):
            self.confirmed.write_text(invalid)
            self.cli("verify-confirmed", ok=False)
