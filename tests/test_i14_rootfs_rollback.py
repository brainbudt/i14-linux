"""The rootfs trial in /i14init (v10c) and the Linux-side watcher rootfs/overlay/usr/sbin/i14-rootfs-trial.

/i14init's normal-start tail (from the rescue-once check up to the USB gadget) runs here with its
real functions (check, init_ok, root_ok, trial_on_boot, rollback) and the real state_write from
initramfs/i14charge. Fakes: e2fsck and mount look at the image file's text (BADFS: check fails,
NOMOUNT: mount fails, NOINIT: no /sbin/init), rescue records its reason and stops. Images are small
text files: OLD (the running image before the swap) and NEW. The marker is written as the swap
writes it: one line padded to TRIAL_SIZE bytes. Design: docs/ROOTFS-SWAP-ROLLBACK.md.
"""
import hashlib
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT = (ROOT / "initramfs/i14init").read_text()
LIB = ROOT / "initramfs/i14charge"
WATCHER = ROOT / "rootfs/overlay/usr/sbin/i14-rootfs-trial"


def piece(start, end):
    return INIT[INIT.index(start):INIT.index(end)]


def header():
    names = ("TRIAL=", "TRIAL_SIZE=", "PREV=", "NEWIMG=", "TRIAL_MAX=", "trial=")
    return "".join(l + "\n" for l in INIT.splitlines() if l.startswith(names))


FAKES = r'''
log() { echo "$*" >>"$D/log"; }
rescue() { echo "rescue $1" >>"$D/did"; exit 0; }
sync() { :; }
e2fsck() { if grep -q BADFS "$2"; then echo "$2: bad"; return 4; fi; echo "$2: clean"; return 0; }
mount() {
    img=; for x; do img=$tgt; tgt=$x; done
    grep -q NOMOUNT "$img" && return 1
    mkdir -p "$D/root/sbin"
    grep -q NOINIT "$img" || { : >"$D/root/sbin/init"; chmod 755 "$D/root/sbin/init"; }
    grep -q NOUMOUNT "$img" && touch "$D/refuse-unmount"
    echo "mounted $(head -n 1 "$img" | cut -d' ' -f1)" >>"$D/did"
}
umount() { [ ! -e "$D/refuse-unmount" ] || return 1; rm -rf "$D/root/sbin"; echo umount >>"$D/did"; }
'''


# Full userdata (ENOSPC), modelled on ext4: nothing new can be allocated (the directory takes no
# new name: mode 0555), while what needs no allocation still works: overwriting an existing file in
# place, a rename onto an existing name, and unlink.
FULL = r'''
mv() {
    t=; for x; do t=$x; done
    [ -e "$t" ] || { echo "mv: No space left on device" >&2; return 1; }
    chmod u+w "$DIR"; command mv "$@"; rc=$?; chmod a-w "$DIR"; return $rc
}
rm() { chmod u+w "$DIR"; command rm "$@"; rc=$?; chmod a-w "$DIR"; return $rc; }
'''
TRIAL_SIZE = 512


def pad(line):
    return line[:TRIAL_SIZE - 1].ljust(TRIAL_SIZE - 1) + "\n"


def boot(d, full=False):
    """One normal start of /i14init's tail on the userdata directory d/data (full: a full
    userdata, see FULL). Returns did, log."""
    d = pathlib.Path(d)
    (d / "did").write_text("")
    (d / "log").write_text("")
    funcs = piece("check() {", "\nmount_data() {") + piece("init_ok() {", "\n. /i14/charge")
    tail = piece("\nif [ -e $DIR/rescue-once ]; then", '\ngadget || rescue "usb gadget setup failed"')
    fakes = FAKES + (FULL if full else "")
    code = (f'D={d}\nDIR=$D/data\n{header()}{fakes}. "{LIB}"\n{funcs}\n{tail}\necho booted >>"$D/did"\n'
            ).replace("/i14/root", "$D/root")
    env = dict(os.environ, PATH="/usr/bin:/bin")
    r = subprocess.run(["sh", "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    did = [l for l in (d / "did").read_text().splitlines() if l]
    return did, (d / "log").read_text()


class Disk:
    """Userdata directory with the images and marker files."""

    def __init__(self, test, **files):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.d = pathlib.Path(self.tmp.name)
        self.data = self.d / "data"
        self.data.mkdir()
        for name, text in files.items():
            (self.data / name.replace("_", "-").replace("-img", ".img")).write_text(text + "\n")

    def boot(self, full=False):
        return boot(self.d, full)

    def marker(self, line):
        (self.data / "rootfs-trial").write_text(pad(line))

    def link_trial_image(self):
        os.link(self.data / "rootfs.img", self.data / "rootfs-trial.img")

    def files(self):
        return {p.name: p.read_text().splitlines()[0].rstrip() if p.read_text() else ""
                for p in sorted(self.data.iterdir()) if p.is_file() and p.name not in ("i14init.log",)}

    def same(self, a, b):
        return (self.data / a).stat().st_ino == (self.data / b).stat().st_ino


SWAPPED = dict(rootfs_img="NEW image", rootfs_prev_img="OLD image")


def swapped(test, marker, **files):
    """The state `i14-rootfs-swap.py start` leaves: new rootfs.img (also rootfs-trial.img),
    rootfs-prev.img, the padded marker."""
    disk = Disk(test, **dict(SWAPPED, **files))
    disk.link_trial_image()
    disk.marker(marker)
    return disk


ROLLED_BACK = {"rootfs.img": "OLD image", "rootfs-trial.img": "NEW image"}


class DecisionTable(unittest.TestCase):
    def test_fresh_no_marker_boots_rootfs_img_and_changes_nothing(self):
        disk = Disk(self, rootfs_img="OLD image")
        self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"])
        self.assertEqual(disk.files(), {"rootfs.img": "OLD image"})

    def test_fresh_with_a_left_over_prev_image_still_boots_rootfs_img(self):
        disk = Disk(self, **SWAPPED)
        self.assertEqual(disk.boot()[0], ["mounted NEW", "booted"])
        self.assertEqual(set(disk.files()), {"rootfs.img", "rootfs-prev.img"})

    def test_unconfirmed_trial_gets_two_starts_then_rolls_back(self):
        disk = swapped(self, "trial 0 src=abc at=T ack=H")
        did, log = disk.boot()
        self.assertEqual(did, ["mounted NEW", "booted"])
        self.assertEqual(disk.files()["rootfs-trial"], "trial 1 src=abc at=T ack=H")
        self.assertEqual((disk.data / "rootfs-trial").stat().st_size, TRIAL_SIZE)  # rewritten in place
        self.assertIn("rootfs trial: start 1 of 2", log)
        self.assertEqual(disk.boot()[0], ["mounted NEW", "booted"])
        self.assertEqual(disk.files()["rootfs-trial"], "trial 2 src=abc at=T ack=H")
        did, log = disk.boot()  # never confirmed: the third start goes back
        self.assertEqual(did, ["mounted OLD", "booted"])
        f = disk.files()
        self.assertRegex(f.pop("rootfs-trial"),
                         r"^rollback not confirmed after 2 starts \(.+\); was: trial 2 src=abc at=T ack=H$")
        self.assertEqual(f, ROLLED_BACK)
        self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"])  # and it stays there
        self.assertEqual({k: v for k, v in disk.files().items() if k != "rootfs-trial"}, ROLLED_BACK)

    def test_trial_counts_without_the_comment(self):
        disk = swapped(self, "trial 1")
        disk.boot()
        self.assertEqual(disk.files()["rootfs-trial"], "trial 2")

    def test_confirmed_trial_boots_the_new_image(self):
        disk = Disk(self, rootfs_trial_confirmed="confirmed ...", **SWAPPED)
        self.assertEqual(disk.boot()[0], ["mounted NEW", "booted"])
        self.assertEqual(disk.files()["rootfs-prev.img"], "OLD image")

    def test_missing_prev_image_goes_to_rescue_and_keeps_the_marker(self):
        for marker in ("trial 0 x", "trial 2", "garbage"):
            for link in (False, True):  # no trial image, or rootfs.img is the trial image
                disk = Disk(self, rootfs_img="NEW image")
                disk.marker(marker)
                if link:
                    disk.link_trial_image()
                self.assertEqual(disk.boot()[0], ["rescue rootfs trial without rootfs-prev.img: no way back"], marker)
                self.assertEqual(disk.files()["rootfs-trial"], marker)

    def test_corrupt_marker_rolls_back(self):
        for marker in ("", "garbage", "trial", "trial x", "trial 10", "trial -1", "Trial 0", "trial 0\nmore",
                       "trial 0 x\nmore"):
            disk = swapped(self, marker)
            did, _ = disk.boot()
            self.assertEqual(did, ["mounted OLD", "booted"], repr(marker))
            f = disk.files()
            self.assertTrue(f.pop("rootfs-trial").startswith("rollback unreadable rootfs-trial"), f)
            self.assertEqual(f, ROLLED_BACK)

    def test_marker_of_the_wrong_size_rolls_back_without_writing_it(self):
        disk = swapped(self, "x")
        (disk.data / "rootfs-trial").write_text("trial 0 src=abc\n")  # not the swap's 512 bytes
        self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"])
        self.assertEqual((disk.data / "rootfs-trial").read_text(), "trial 0 src=abc\n")  # never grown
        self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"])  # next start: rolled back earlier

    def test_marker_that_is_not_a_file_goes_to_rescue(self):
        disk = Disk(self, **SWAPPED)
        (disk.data / "rootfs-trial").mkdir()
        self.assertEqual(disk.boot()[0], ["rescue rootfs-trial is not a file"])

    def test_e2fsck_failure_of_the_new_image_rolls_back_in_the_same_start(self):
        disk = swapped(self, "trial 0 s", rootfs_img="NEW BADFS")
        did, log = disk.boot()
        self.assertEqual(did, ["mounted OLD", "booted"])
        self.assertIn("e2fsck rootfs.img: rc=4", log)
        f = disk.files()
        self.assertTrue(f["rootfs-trial"].startswith("rollback trial start 1: rootfs.img check failed ("), f)
        self.assertTrue(f["rootfs-trial"].endswith("; was: trial 1 s"), f)

    def test_mount_or_init_failure_of_the_new_image_rolls_back(self):
        for new, why, did in (("NEW NOMOUNT", "rootfs mount failed", ["mounted OLD", "booted"]),
                              ("NEW NOINIT", "rootfs has no usable /sbin/init",
                               ["mounted NEW", "umount", "mounted OLD", "booted"])):
            disk = swapped(self, "trial 1", rootfs_img=new)
            self.assertEqual(disk.boot()[0], did, new)
            self.assertIn(f"rollback trial start 2: {why}", disk.files()["rootfs-trial"])

    def test_refuses_to_rename_images_if_failed_trial_cannot_unmount(self):
        disk = swapped(self, "trial 0", rootfs_img="NEW NOINIT NOUMOUNT")
        did, _ = disk.boot()
        self.assertEqual(did, ["mounted NEW", "rescue rootfs has no usable /sbin/init and cannot unmount"])
        self.assertEqual(disk.files()["rootfs.img"], "NEW NOINIT NOUMOUNT")
        self.assertEqual(disk.files()["rootfs-prev.img"], "OLD image")

    def test_missing_new_image_during_a_trial_rolls_back(self):
        disk = Disk(self, rootfs_prev_img="OLD image")
        disk.marker("trial 0")
        self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"])
        self.assertEqual(disk.files()["rootfs.img"], "OLD image")

    def test_start_that_never_swapped_goes_back_to_the_same_image(self):
        # start cut off after the marker, before the rename: rootfs.img is still rootfs-prev.img
        disk = Disk(self, rootfs_img="OLD image")
        (disk.data / "rootfs.img.new").write_text("NEW image\n")
        os.link(disk.data / "rootfs.img", disk.data / "rootfs-prev.img")
        os.link(disk.data / "rootfs.img.new", disk.data / "rootfs-trial.img")
        disk.marker("trial 0 s")
        self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"])
        f = disk.files()
        self.assertTrue(f.pop("rootfs-trial").startswith("rollback rootfs.img is still rootfs-prev.img"))
        self.assertEqual(f, {"rootfs.img": "OLD image", "rootfs.img.new": "NEW image", "rootfs-trial.img": "NEW image"})

    def test_without_a_trial_failures_still_go_to_rescue_unchanged(self):
        for img, why in (("OLD BADFS", "rootfs.img check failed"), ("OLD NOMOUNT", "rootfs mount failed"),
                         ("OLD NOINIT", "rootfs has no usable /sbin/init")):
            disk = Disk(self, rootfs_img=img, rootfs_prev_img="PREV image")
            self.assertEqual(disk.boot()[0][-1], f"rescue {why}", img)
            self.assertEqual(disk.files()["rootfs-prev.img"], "PREV image")  # never touched without a trial
        self.assertEqual(Disk(self).boot()[0], ["rescue no rootfs.img"])

    def test_broken_old_image_after_a_rollback_goes_to_rescue(self):
        disk = swapped(self, "trial 2", rootfs_prev_img="OLD BADFS")
        self.assertEqual(disk.boot()[0], ["rescue rootfs.img check failed"])
        self.assertEqual(disk.files()["rootfs.img"], "OLD BADFS")
        disk = swapped(self, "trial 0", rootfs_img="NEW BADFS", rootfs_prev_img="OLD BADFS")
        self.assertEqual(disk.boot()[0], ["rescue rootfs.img check failed"])

    def test_an_interrupted_rollback_finishes(self):
        marker = "rollback not confirmed after 2 starts (2026-11-10 15:00:00); was: trial 2 src=abc"
        for renamed in (False, True):  # cut off right after the record, or after the rename
            disk = swapped(self, marker)
            if renamed:
                os.replace(disk.data / "rootfs-prev.img", disk.data / "rootfs.img")
            self.assertEqual(disk.boot()[0], ["mounted OLD", "booted"], renamed)
            self.assertEqual(disk.files(), dict(ROLLED_BACK, **{"rootfs-trial": marker}))

    def test_rollback_with_nothing_left_goes_to_rescue(self):
        disk = Disk(self, rootfs_trial_img="NEW image")
        disk.marker("rollback x")
        self.assertEqual(disk.boot()[0], ["rescue rollback: no rootfs.img"])

    def test_counter_that_cannot_be_written_goes_back_instead_of_rescue(self):
        disk = swapped(self, "trial 0 s")
        (disk.data / "rootfs-trial").chmod(0o444)
        did, log = disk.boot()
        self.assertEqual(did, ["mounted OLD", "booted"])
        self.assertIn("cannot write rootfs-trial; going back anyway", log)
        self.assertEqual(disk.files(), dict(ROLLED_BACK, **{"rootfs-trial": "trial 0 s"}))
        did, log = disk.boot()  # still "trial 0", but prev is gone and the trial image is not running
        self.assertEqual(did, ["mounted OLD", "booted"])
        self.assertIn("rolled back earlier", log)

    def test_full_userdata_still_counts_and_rolls_back(self):
        """Review S2: counter and rollback must not need a new file or block on a full userdata."""
        def run():
            out = []
            for marker in ("trial 0 s", "trial 2 s"):
                disk = swapped(self, marker)
                disk.data.chmod(0o555)
                try:
                    out.append((disk.boot(full=True)[0], disk.files()))
                finally:
                    disk.data.chmod(0o755)
            return out
        (did0, f0), (did2, f2) = run()
        self.assertEqual(did0, ["mounted NEW", "booted"])
        self.assertEqual(f0["rootfs-trial"], "trial 1 s")
        self.assertEqual(did2, ["mounted OLD", "booted"])
        self.assertTrue(f2.pop("rootfs-trial").startswith("rollback not confirmed after 2 starts"))
        self.assertEqual(f2, ROLLED_BACK)

    def test_rescue_once_comes_first_and_does_not_count_a_start(self):
        disk = swapped(self, "trial 1", rescue_once="")
        self.assertEqual(disk.boot()[0], ["rescue rescue-once flag"])
        self.assertEqual(disk.files()["rootfs-trial"], "trial 1")


class Structure(unittest.TestCase):
    def test_counter_is_written_before_any_check_or_mount_of_the_new_image(self):
        tail = INIT[INIT.index("\ntrial_on_boot\n"):]
        self.assertLess(tail.index("trial_on_boot"), tail.index("root_ok"))
        f = piece("trial_on_boot() {", "\nrollback() {")
        self.assertLess(f.index('record "trial $((n + 1))'), f.index('trial=$((n + 1))'))

    def test_trial_code_allocates_nothing_on_userdata(self):
        """Review S2: no new file (no state_write, no > redirection), no rename to a new name."""
        f = piece("size_of() {", "\n. /i14/charge")
        self.assertNotIn("state_write", f)
        self.assertNotRegex(f.replace("1<>$TRIAL", "").replace(">/dev/null", "").replace("2>/dev/null", ""), r">")
        self.assertEqual(f.count("mv "), 1)
        self.assertIn("mv -f $PREV $DIR/rootfs.img", f)  # onto the existing name
        self.assertIn('[ "$(size_of $TRIAL)" = $TRIAL_SIZE ] || return 1', f)  # never grows the record
        self.assertIn(f"TRIAL_SIZE={TRIAL_SIZE} ", INIT)

    def test_charging_path_is_untouched_by_the_trial(self):
        charge = piece("charge() {", "\ninit_ok() {")
        for word in ("TRIAL", "rollback", "PREV"):
            self.assertNotIn(word, charge)

    def test_trial_max_is_two(self):
        self.assertIn("TRIAL_MAX=2 ", INIT)

    def test_inittab_starts_the_watcher_before_openrc(self):
        tab = (ROOT / "rootfs/overlay/etc/inittab").read_text().splitlines()
        lines = [l for l in tab if l and not l.startswith("#")]
        self.assertEqual(lines[0], "::sysinit:/usr/sbin/i14-rootfs-trial start")
        self.assertEqual(lines[1], "::sysinit:/sbin/openrc sysinit")
        self.assertEqual(WATCHER.stat().st_mode & 0o777, 0o755)


NONCE = "f00d" * 16
ACK_HASH = hashlib.sha256(NONCE.encode()).hexdigest()
MARKER = f"trial 1 src=abc at=T ack={ACK_HASH}"


class Watcher(unittest.TestCase):
    """i14-rootfs-trial watch with a fake clock (sleep moves /proc/uptime), rc-service, /proc/net and
    boot id. ack: the content of rootfs-trial-ack (the Mac's acknowledgement), None for none."""
    LISTEN = "  0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1\n"
    LOOPBACK = "  0: 0100007F:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 1 1\n"
    VALID_ACK = f"ack {NONCE} boot-7\n"

    def watch(self, marker=MARKER, down="", listen=True, hook="", arg="watch", ack=VALID_ACK, net=None,
              capture=True, corrupt=False, rolled_back=False):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "bin").mkdir()
            (d / "net").mkdir()
            (d / "data").mkdir()
            if rolled_back:
                (d / "data/rootfs.img").write_text("old image\n")
                (d / "data/rootfs-trial.img").write_text("failed trial\n")
            if marker is not None:
                (d / "data/rootfs-trial").write_text(pad(marker))
            if ack is not None:
                (d / "data/rootfs-trial-ack").write_text(ack)
            (d / "boot_id").write_text("boot-7\n")
            (d / "uptime").write_text("20.00 1.00\n")
            (d / "down").write_text(down)
            (d / "hook").write_text(hook + "\n")
            (d / "net/tcp").write_text("  sl  local_address rem_address   st\n" +
                                       (net if net is not None else self.LISTEN if listen else ""))
            stubs = {
                "rc-service": 'grep -qw "$1" "$D/down" && exit 3; exit 0',
                "sleep": 'read -r u _ <"$D/uptime"; t=$((${u%.*} + $1)); echo "$t.00 1.00" >"$D/uptime"; . "$D/hook"',
                "reboot": 'read -r u _ <"$D/uptime"; echo "reboot $* t=${u%.*}" >>"$D/did"',
                "logger": ":", "sync": ":", "sha256sum": 'shasum -a 256 "$@"',
            }
            for name, body in stubs.items():
                (d / "bin" / name).write_text("#!/bin/sh\n" + body + "\n")
                (d / "bin" / name).chmod(0o755)
            conf = d / "conf"
            conf.write_text('i14_trial_services="sshd i14-power i14-wifi"\n')
            env = dict(os.environ, PATH=f"{d}/bin:/usr/bin:/bin", D=str(d), I14_TRIAL_CONF=str(conf),
                       I14_TRIAL_DIR=str(d / "data"), I14_UPTIME=str(d / "uptime"), I14_PROC_NET=str(d / "net"),
                       I14_BOOT_ID=str(d / "boot_id"))
            (d / "root/var/lib/unbound").mkdir(parents=True)
            (d / "root/etc").mkdir()
            key = b";;last_queried: 1\n. IN DNSKEY 257 3 8 PUBLIC\n"
            (d / "root/var/lib/unbound/root.key").write_bytes(key)
            (d / "root/etc/i14-rootfs-root-key.sha256").write_text(hashlib.sha256(key).hexdigest() + "\n")
            (d / "root/etc/i14-rootfs-id").write_text("src=abc at=T\n")
            env["I14_TRIAL_ROOT"] = str(d / "root")
            # Exercise the synchronous pre-OpenRC function, then let a managed writer change the live file.
            if capture and marker is not None and marker.startswith("trial "):
                source = WATCHER.read_text().split('\ncase "$1" in\nstart)')[0]
                c = subprocess.run(["sh", "-c", source + "\nroot_key_capture\n"], env=env,
                                   capture_output=True, text=True)
                self.assertEqual(c.returncode, 0, c.stderr)
                (d / "root/var/lib/unbound/root.key").write_bytes(key.replace(b"queried: 1", b"queried: 2"))
                if corrupt:
                    (d / "data/rootfs-trial-root-key.boot-7").write_text("bad\n")
            r = subprocess.run(["sh", str(WATCHER), arg], env=env, capture_output=True, text=True, timeout=60)
            files = {p.name: p.read_text() for p in (d / "data").iterdir()}
            did = (d / "did").read_text() if (d / "did").exists() else ""
            return r, files, did

    def test_healthy_and_acknowledged_trial_is_confirmed_after_ten_minutes(self):
        r, files, did = self.watch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("rootfs-trial", files)
        self.assertRegex(files["rootfs-trial-confirmed"],
                         rf"^confirmed .+ uptime=6[0-2]\ds ack-boot=boot-7 after: {MARKER}\n$")
        self.assertEqual(did, "")
        self.assertIn("healthy and acknowledged by the Mac: trial confirmed", files["rootfs-trial.log"])

    def test_already_rolled_back_image_never_reboots_for_a_stale_trial_marker(self):
        result, files, did = self.watch(rolled_back=True, ack=None, capture=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(did, "")
        self.assertEqual(files["rootfs-trial"], pad(MARKER))
        self.assertNotIn("rootfs-trial-confirmed", files)

    def test_missing_or_corrupt_early_root_key_prevents_confirmation(self):
        for kw in (dict(capture=False), dict(corrupt=True)):
            r, files, did = self.watch(**kw)
            self.assertNotIn("rootfs-trial-confirmed", files)
            self.assertIn("root-key-carry", files["rootfs-trial.log"])
            self.assertIn("reboot -f", did)

    def test_early_root_key_bytes_survive_a_managed_live_update(self):
        r, files, did = self.watch()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(";;last_queried: 1", files["rootfs-trial-root-key.boot-7"])
        self.assertIn("root_key_boot=boot-7", files["rootfs-trial-root-key-receipt"])
        self.assertIn("rootfs-trial-confirmed", files)

    def test_local_health_alone_never_confirms(self):
        """Review S1: sshd on 127.0.0.1:22 and every service up, but no authenticated SSH ack."""
        cases = (
            (dict(ack=None, net=self.LOOPBACK), "no ack from the Mac"),
            (dict(ack=f"ack {NONCE} boot-6\n"), "ack is for another boot"),     # left from start 1
            (dict(ack=f"ack {'0' * 64} boot-7\n"), "ack is for another trial"),  # another trial's nonce
            (dict(marker="trial 1 src=abc at=T"), "no ack= hash in the marker"),
        )
        for kw, why in cases:
            with self.subTest(why):
                r, files, did = self.watch(**kw)
                self.assertNotIn("rootfs-trial-confirmed", files)
                self.assertEqual(files["rootfs-trial"].rstrip(), kw.get("marker", MARKER))
                self.assertRegex(did, r"^reboot -f t=18[0-2]\d\n$")  # the deadline still holds
                self.assertIn(f"mac-ack({why})", files["rootfs-trial.log"])

    def test_late_ack_or_service_is_waited_for(self):
        for kw, at in ((dict(down="i14-wifi", hook='[ $t -ge 900 ] && : >"$D/down"'), "9"),
                       (dict(ack=None, hook=f'[ $t -ge 1200 ] && echo "ack {NONCE} boot-7" >"$D/data/rootfs-trial-ack"'),
                        "12")):
            r, files, did = self.watch(**kw)
            self.assertRegex(files["rootfs-trial-confirmed"], rf"uptime={at}[0-2]\ds")
            self.assertIn("not healthy yet, missing: ", files["rootfs-trial.log"])
            self.assertEqual(did, "")

    def test_unhealthy_trial_reboots_at_the_deadline_and_keeps_the_marker(self):
        for kw, missing in ((dict(down="i14-power"), "i14-power"), (dict(listen=False), "sshd-port-22")):
            r, files, did = self.watch(**kw)
            self.assertEqual(files["rootfs-trial"].rstrip(), MARKER)
            self.assertNotIn("rootfs-trial-confirmed", files)
            self.assertRegex(did, r"^reboot -f t=18[0-2]\d\n$")
            self.assertIn(f"missing: {missing}", files["rootfs-trial.log"])

    def test_no_marker_or_not_a_trial_does_nothing(self):
        for marker in (None, "rollback x"):
            r, files, did = self.watch(marker=marker)
            self.assertEqual(r.returncode, 0)
            self.assertEqual(did, "")
            self.assertNotIn("rootfs-trial-confirmed", files)

    def test_start_without_a_marker_returns_at_once(self):
        r, files, did = self.watch(marker=None, ack=None, arg="start")
        self.assertEqual((r.returncode, files, did), (0, {}, ""))

    def test_check_reports_what_is_missing(self):
        r, _, _ = self.watch(down="i14-wifi", arg="check")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "missing: i14-wifi"))
        r, _, _ = self.watch(arg="check")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "healthy"))

    def test_ack_reports_the_acknowledgement(self):
        r, _, _ = self.watch(arg="ack")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "acknowledged"))
        r, _, _ = self.watch(arg="ack", ack=None)
        self.assertEqual((r.returncode, r.stdout.strip()), (1, "not acknowledged: no ack from the Mac"))

    def test_ipv6_listener_counts(self):
        with tempfile.TemporaryDirectory() as d:
            net = pathlib.Path(d)
            (net / "tcp6").write_text("  0: 00000000000000000000000000000000:0016 "
                                      "00000000000000000000000000000000:0000 0A 0\n")
            text = WATCHER.read_text()
            func = text[text.index("ssh_listening() {"):text.index("\nhealth() {")]
            r = subprocess.run(["sh", "-c", f'NET={net}\n{func}\nssh_listening && echo yes'],
                               capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "yes")
            (net / "tcp6").write_text("  0: 0000:0016 0000:0000 01 0\n")  # established, not listening
            r = subprocess.run(["sh", "-c", f'NET={net}\n{func}\nssh_listening && echo yes'],
                               capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "")

    def test_conf_lists_every_default_service(self):
        text = (ROOT / "rootfs/overlay/etc/conf.d/i14-rootfs-trial").read_text()
        build = (ROOT / "rootfs/build-alpine-rootfs.sh").read_text()
        line = next(l for l in build.splitlines() if l.startswith("for s in networking sshd"))
        for s in line.split(" in ", 1)[1].split(";")[0].split():
            if s != "local":
                self.assertIn(f" {s}", " " + text.split('"')[1], s)


if __name__ == "__main__":
    unittest.main()
