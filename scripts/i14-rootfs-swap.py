#!/usr/bin/env python3
"""Stage and inspect an offline I14 trial on a quiescent local userdata filesystem.

Supply the prepared rootfs.img.new, its independently verified SHA-256, and the
corresponding candidate root directory (including the carried key and stamp).
rootfs.img must still be B5, with a previously prepared hard link at rootfs-prev.img.
The previous image and carried key each need a separate trusted SHA-256.

Keep --state on the host, outside userdata and the candidate root. It contains
the private ACK nonce. All verification commands are read-only. Use the same
filesystem for inode checks and an independently observed --boot-id for boot
checks. verify-rollback checks the B5 object identified before the trial.
Add --check-restored-sha256 for byte equality with a pristine offline B5
image. A writable boot can legitimately change its raw filesystem bytes.
"""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile


ACTIVE = "rootfs.img"
PREV = "rootfs-prev.img"
NEW = "rootfs.img.new"
TRIAL = "rootfs-trial.img"
MARKER = "rootfs-trial"
ACK = "rootfs-trial-ack"
CONFIRMED = "rootfs-trial-confirmed"
RECEIPT = "rootfs-trial-root-key-receipt"
SIZE = 512
FREE_FLOOR = 2 << 30
SHA256 = r"[0-9a-f]{64}"
STAMP = r"src=[a-zA-Z0-9_.-]+ at=[a-zA-Z0-9_.:-]+"
BOOT_ID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}"


class Refused(ValueError):
    pass


def check(condition, reason):
    if not condition:
        raise Refused(reason)


def identity(info):
    return (info.st_dev, info.st_ino)


@contextmanager
def regular(path):
    info = path.lstat()
    check(stat.S_ISREG(info.st_mode), f"{path}: expected a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        check(os.path.samestat(os.fstat(fd), info), f"{path}: changed while opening")
        yield stream


def read(path, limit=4096):
    with regular(path) as stream:
        data = stream.read(limit + 1)
    check(len(data) <= limit, f"{path}: oversized file")
    return data


def digest(path):
    with regular(path) as stream:
        check(os.fstat(stream.fileno()).st_size > 0, f"{path}: empty file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image_id(path):
    info = path.lstat()
    check(stat.S_ISREG(info.st_mode) and info.st_size > 0, f"{path}: expected a nonempty regular image")
    return identity(info)


def directory(path):
    check(stat.S_ISDIR(path.lstat().st_mode), f"{path}: expected a directory")


def root_path(root, name):
    path = root
    for part in Path(name).parts[:-1]:
        directory(path)
        path = path / part
    directory(path)
    return path / Path(name).name


def absent(path):
    check(not os.path.lexists(path), f"{path}: unexpected existing entry")


def check_space(data, floor):
    usage = os.statvfs(data)
    check(usage.f_bavail * usage.f_frsize >= floor, f"userdata has less than {floor} bytes available")


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish(path, contents):
    """Publish complete, fsynced bytes without truncating or replacing an existing name."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".") as tmp:
        tmp.write(contents)
        tmp.flush()
        os.fsync(tmp.fileno())
        os.link(tmp.name, path)
    sync_dir(path.parent)


@dataclass(frozen=True)
class Evidence:
    previous: tuple
    candidate: tuple
    previous_sha256: str
    candidate_sha256: str
    root_key_sha256: str
    stamp: str
    nonce: str
    min_free_bytes: int

    def validate(self):
        for pin in (self.previous_sha256, self.candidate_sha256, self.root_key_sha256, self.nonce):
            check(isinstance(pin, str) and re.fullmatch(SHA256, pin), "invalid SHA-256 or nonce")
        for obj in (self.previous, self.candidate):
            check(isinstance(obj, (tuple, list)) and len(obj) == 2 and
                  all(type(n) is int and n >= 0 for n in obj), "invalid image identity")
        check(self.previous[0] == self.candidate[0] and self.previous != self.candidate,
              "images must be distinct inodes on the same filesystem")
        check(type(self.min_free_bytes) is int and self.min_free_bytes >= FREE_FLOOR, "invalid userdata floor")
        check(isinstance(self.stamp, str) and re.fullmatch(STAMP, self.stamp) and len(self.stamp) <= 220,
              "expected a bounded 'src=SOURCE at=TIME' stamp")

    def line(self, count):
        ack = hashlib.sha256(self.nonce.encode("ascii")).hexdigest()
        return f"trial {count} {self.stamp} ack={ack}"

    @classmethod
    def load(cls, path):
        check(not path.lstat().st_mode & 0o077, "host nonce file must be private")
        raw = json.loads(read(path))
        try:
            result = cls(**raw)
        except TypeError as exc:
            raise Refused("invalid host state fields") from exc
        result.validate()
        return result


def root_metadata(root, evidence):
    for name, value in (("etc/i14-rootfs-id", evidence.stamp),
                        ("etc/i14-rootfs-root-key.sha256", evidence.root_key_sha256)):
        check(read(root_path(root, name)) == (value + "\n").encode(), f"{name}: host evidence mismatch")


def verify_images(data, evidence, rollback=False):
    active = image_id(data / ACTIVE)
    check(image_id(data / TRIAL) == tuple(evidence.candidate), "lost the recorded candidate inode")
    if rollback:
        absent(data / PREV)
        check(active == tuple(evidence.previous), "B5 inode was not restored")
    else:
        check(image_id(data / PREV) == tuple(evidence.previous), "previous B5 inode changed")
        check(active == tuple(evidence.candidate), "candidate inode is not active")


def marker(data):
    value = read(data / MARKER, SIZE)
    check(len(value) == SIZE and value.endswith(b"\n") and b"\n" not in value[:-1],
          "expected one 512-byte trial record")
    return value[:-1].rstrip(b" ").decode("ascii")


def check_stale_names(data, allowed=()):
    for path in data.iterdir():
        if path.name in allowed:
            continue
        if path.name == MARKER + ".log":
            info = path.lstat()
            check(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, f"{path}: unsafe watcher log")
        elif path.name == MARKER or path.name.startswith((MARKER + "-", MARKER + ".")):
            raise Refused(f"{path}: reconcile existing trial evidence first")


def stage(args):
    data = args.data
    floor = max(args.min_free_bytes, FREE_FLOOR)
    check_space(data, floor)
    check_stale_names(data)
    absent(args.state)
    old = image_id(data / PREV)
    check(image_id(data / ACTIVE) == old, "active image is not the prepared B5 hard link")
    new = image_id(data / NEW)
    evidence = Evidence(old, new, args.previous_sha256, args.candidate_sha256, args.root_key_sha256,
                        args.stamp, secrets.token_hex(32), floor)
    evidence.validate()
    check(digest(data / PREV) == evidence.previous_sha256, "B5 SHA-256 mismatch")
    check(digest(data / NEW) == evidence.candidate_sha256, "prepared candidate SHA-256 mismatch")
    root_metadata(args.root, evidence)
    check(digest(root_path(args.root, "var/lib/unbound/root.key")) == evidence.root_key_sha256,
          "prepared root.key SHA-256 mismatch")

    # Flush the prepared image data before committing any names that depend on it.
    for image in (PREV, NEW):
        with regular(data / image) as stream:
            os.fsync(stream.fileno())
    publish(args.state, (json.dumps(asdict(evidence), indent=2) + "\n").encode())
    os.link(data / NEW, data / TRIAL, follow_symlinks=False)
    sync_dir(data)
    check(image_id(data / TRIAL) == new and image_id(data / ACTIVE) == old, "links changed before swap")
    publish(data / MARKER, evidence.line(0).encode().ljust(SIZE - 1) + b"\n")
    check(marker(data) == evidence.line(0), "published trial record mismatch")
    check_space(data, floor)
    # Up to this rename B5 is still active. From here the record and both recovery
    # names are durable. Keep all evidence after an error or an interrupted stage.
    os.replace(data / NEW, data / ACTIVE)
    sync_dir(data)
    verify_images(data, evidence)


def key_receipt(args, evidence):
    boot = args.boot_id
    check(re.fullmatch(BOOT_ID, boot), "expected an independently observed Linux boot ID")
    root_metadata(args.root, evidence)
    expected = (f"root_key_boot={boot}\nroot_key_stamp={evidence.stamp}\n"
                f"root_key_sha256={evidence.root_key_sha256}\n")
    check(read(args.data / RECEIPT) == expected.encode(), "pre-Unbound receipt mismatch")
    check(digest(args.data / f"rootfs-trial-root-key.{boot}") == evidence.root_key_sha256,
          "pre-Unbound capture SHA-256 mismatch")


def verify(args):
    data = args.data
    evidence = Evidence.load(args.state)
    rollback = args.command == "verify-rollback"
    verify_images(data, evidence, rollback)
    current = "|".join(re.escape(evidence.line(n)) for n in (1, 2))
    if rollback:
        absent(data / CONFIRMED)
        prior = f"{current}|{re.escape(evidence.line(0))}"
        reason = r"[^\r\n]+"
        if args.rehearsal:
            absent(data / ACK)
            prior = re.escape(evidence.line(2))
            reason = "not confirmed after 2 starts"
        check(re.fullmatch(rf"rollback {reason} \({DATE}\); was: (?:{prior})", marker(data)),
              "missing rollback record for this trial")
        if args.check_restored_sha256:
            check(digest(data / ACTIVE) == evidence.previous_sha256, "restored B5 SHA-256 mismatch")
        return
    if args.command == "verify-staged":
        check_space(data, evidence.min_free_bytes)
        check_stale_names(data, allowed=(TRIAL, MARKER))
        check(marker(data) == evidence.line(0), "not a fresh staged trial")
        for path, pin in ((PREV, evidence.previous_sha256), (ACTIVE, evidence.candidate_sha256)):
            check(digest(data / path) == pin, f"{path}: staged SHA-256 changed")
        return

    key_receipt(args, evidence)
    if args.command == "verify-confirmed":
        absent(data / MARKER)
        pattern = rf"confirmed {DATE} uptime=[0-9]+s ack-boot={args.boot_id} after: (?:{current})\n"
        check(re.fullmatch(pattern, read(data / CONFIRMED).decode("ascii")), "invalid confirmation record")
    else:
        absent(data / CONFIRMED)
        check(re.fullmatch(current, marker(data)), "not a counted start of this trial")
    if args.command in ("verify-ack", "verify-confirmed"):
        expected = f"ack {evidence.nonce} {args.boot_id}\n"
        check(read(data / ACK) == expected.encode(), "ACK is not bound to this nonce and boot")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("stage", "arm an offline candidate and save private host evidence"),
        ("verify-staged", "check both pinned images, their names and the fresh record"),
        ("verify-trial", "check a counted boot and its pre-Unbound receipt before ACK"),
        ("verify-ack", "check a counted boot, its key receipt and the existing ACK"),
        ("verify-confirmed", "check the watcher's completed confirmation and ACK"),
        ("verify-rollback", "check the restored B5 inode, retained candidate and rollback record"),
    ):
        sub = commands.add_parser(name, aliases=["start"] if name == "stage" else [], help=description)
        sub.add_argument("--data", type=Path, required=True, help="offline userdata image directory")
        sub.add_argument("--state", type=Path, required=True, help="private host evidence file")
        if name not in ("verify-staged", "verify-rollback"):
            sub.add_argument("--root", type=Path, required=True, help="corresponding candidate root directory")
        if name == "stage":
            for pin in ("previous", "candidate", "root-key"):
                sub.add_argument(f"--{pin}-sha256", required=True, help=f"independent {pin} SHA-256")
            sub.add_argument("--stamp", required=True, help="independent candidate stamp, 'src=... at=...'")
            sub.add_argument("--min-free-bytes", type=int, default=FREE_FLOOR, help="raise the 2 GiB userdata floor")
        elif name not in ("verify-staged", "verify-rollback"):
            sub.add_argument("--boot-id", required=True, help="independently observed current boot ID")
        elif name == "verify-rollback":
            sub.add_argument("--rehearsal", action="store_true", help="require two unconfirmed starts and no ACK")
            sub.add_argument("--check-restored-sha256", action="store_true", help="also require original B5 bytes")

    args = parser.parse_args(argv)
    staging = args.command in ("stage", "start")
    try:
        for root in (args.data, getattr(args, "root", None)):
            if root is not None:
                check(not args.state.resolve().is_relative_to(root.resolve()), "--state must be private host evidence")
        fd = os.open(args.data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if staging else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            if staging:
                stage(args)
            else:
                verify(args)
        finally:
            os.close(fd)
    except (OSError, ValueError) as exc:
        print(f"{args.command}: {exc}", file=sys.stderr)
        return 1
    print(f"{args.command}: verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
