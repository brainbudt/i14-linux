"""Bounded I14 SECCFG preparation and fixed-prefix commit; import does no I/O."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
OFFSET = 0x4f80000
SIZE = 0x80000
PREFIX_SIZE = 8192
RECORD_SIZE = 0x1860
ORIGINAL_SHA = "07854d2fef297a06ba81685e660c332de36d5d18d546927d30daad6d7fda1541"
FIXED = {
    "backups/20261030T011515Z-native-serial/lk.img": "2c5e18cccf0a174552e2c081552c82b9fec6c444bb56d2758cc7c0d6b4d7c4b4",
    "artifacts/unlock-analysis-20261030/own-preloader.bin": "4f791ddbbeaa2343f1aec84603e5e77cfe75dd94b29b8ee1598413fc77b69c30",
    "scripts/mtk-session.py": "bfcc66d5b3965eb57dbb6c5973c9b212692be9f4df5e2995a5c644252fe2664d",
    "scripts/mtk-readback.py": "924ed34c11fe862cb50946809f78dbb3567d73836bb31d710eac2297746a8fca",
    "scripts/seccfg-v3.py": "0adc6fdf31d0add20e8f1ebeb9b69de5524d10d822c14dc52a858af59afb9e0a",
}
VARIABLE = ["scripts/i14_hacc.py", "scripts/i14_seccfg_io.py", "scripts/prepare-seccfg.py", "scripts/commit-seccfg.py",
            "backups/20261030T011515Z-native-serial/.state", "backups/20261030T011515Z-native-serial/primary-header.bin"]
ARTIFACTS = {"original.bin": SIZE, "plaintext.bin": 0x1830, "ciphertext.bin": 0x1830,
             "decrypted.bin": 0x1830, "fresh-ciphertext.bin": 0x1830,
             "public-input.bin": 32, "public-cipher.bin": 32, "public-decrypted.bin": 32,
             "public-fresh-cipher.bin": 32, "candidate.bin": SIZE, "prefix.bin": PREFIX_SIZE}
CRYPTO_KEYS = {"public_roundtrip", "public_fresh_encrypt", "body_roundtrip", "body_fresh_encrypt"}

class SeccfgError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def save_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        if stream.write(data) != len(data):
            raise SeccfgError("Short local artifact write")
        stream.flush()
        os.fsync(stream.fileno())


def save_json(path, data):
    save_new(path, (json.dumps(data, sort_keys=True, indent=2)+"\n").encode())


def source_snapshot():
    result = {}
    for name in list(FIXED) + VARIABLE:
        digest = sha((ROOT/name).read_bytes())
        if name in FIXED and digest != FIXED[name]:
            raise SeccfgError("Pinned source or own input changed")
        result[name] = digest
    return result


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT/"scripts"/filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def runtime():
    return (module("i14_attach", "mtk-session.py"), module("i14_template", "seccfg-v3.py"),
            module("i14_hacc_bound", "i14_hacc.py"))


def live_original(da):
    data = bytes(da.readflash(addr=OFFSET, length=SIZE, filename="", parttype="user", display=False))
    if len(data) != SIZE or sha(data) != ORIGINAL_SHA:
        raise SeccfgError("Live SECCFG is not the frozen original; no storage write allowed")
    return data


def callbacks(da):
    def read32(addr):
        result = da.read_reg32(addr)
        if type(result) is not int or not 0 <= result <= 0xffffffff:
            raise SeccfgError("DA register read did not return an acknowledged uint32")
        return result
    def write32(addr, value):
        if da.write_reg32(addr, value) is not True:
            raise SeccfgError("DA register write was not acknowledged")
        return True
    return read32, write32


def prepare(directory, serialport="/dev/cu.usbmodem21101", *, _runtime=None):
    sources = source_snapshot()
    attach, template, hacc = runtime() if _runtime is None else _runtime
    directory = Path(directory)
    directory.mkdir(mode=0o700)
    artifacts = {}
    with attach.attached_session(serialport) as ctx:
        da = ctx.daloader.da
        original = live_original(da)
        read32, write32 = callbacks(da)
        def transform(data, encrypt):
            value = bytes(hacc.transform(data, encrypt=encrypt, read32=read32, write32=write32,
                                         timeout=120.0, ready_timeout=0.5))
            if len(value) != len(data):
                raise SeccfgError("HACC transform length mismatch")
            return value
        public = bytes(range(32))
        public_cipher = transform(public, True)
        public_decrypted = transform(public_cipher, False)
        public_fresh = transform(public, True)
        if public_decrypted != public or public_fresh != public_cipher:
            raise SeccfgError("Public HACC roundtrip or fresh encryption differs")
        body = template.plaintext_body()
        if len(body) != 0x1830:
            raise SeccfgError("Unexpected V3 body length")
        cipher = transform(body, True)
        decrypted = transform(cipher, False)
        fresh = transform(body, True)
        if decrypted != body or fresh != cipher:
            raise SeccfgError("Full-body HACC roundtrip or fresh encryption differs")
        candidate = template.assemble_partition(original, cipher)
        if len(candidate) != SIZE or candidate[RECORD_SIZE:] != original[RECORD_SIZE:]:
            raise SeccfgError("Candidate changed outside the V3 record")
        artifacts = {"original.bin": original, "plaintext.bin": body, "ciphertext.bin": cipher,
                     "decrypted.bin": decrypted, "fresh-ciphertext.bin": fresh,
                     "public-input.bin": public, "public-cipher.bin": public_cipher,
                     "public-decrypted.bin": public_decrypted, "public-fresh-cipher.bin": public_fresh,
                     "candidate.bin": candidate, "prefix.bin": candidate[:PREFIX_SIZE]}
    if source_snapshot() != sources:
        raise SeccfgError("Sources changed during RAM/MMIO preparation")
    for name, data in artifacts.items():
        if len(data) != ARTIFACTS[name]:
            raise SeccfgError("Artifact length mismatch")
        save_new(directory/name, data)
    manifest = {"version": 1, "phase": "PREPARED_NO_STORAGE_WRITES", "utc": utc(), "sources": sources,
                "target": {"offset": OFFSET, "partition_size": SIZE, "write_size": PREFIX_SIZE, "parttype": "user"},
                "original_sha256": ORIGINAL_SHA, "candidate_sha256": sha(candidate),
                "crypto": dict.fromkeys(sorted(CRYPTO_KEYS), "PASS"),
                "artifacts": {name: {"size": len(data), "sha256": sha(data)} for name, data in artifacts.items()}}
    save_json(directory/"manifest.json", manifest)
    return manifest


def validate_prepared(directory, expected_sha, template):
    directory = Path(directory)
    manifest = json.loads((directory/"manifest.json").read_text())
    if manifest.get("version") != 1 or manifest.get("phase") != "PREPARED_NO_STORAGE_WRITES":
        raise SeccfgError("Unsupported preparation manifest")
    if manifest.get("sources") != source_snapshot():
        raise SeccfgError("Prepared sources are no longer frozen")
    if manifest.get("crypto") != dict.fromkeys(sorted(CRYPTO_KEYS), "PASS"):
        raise SeccfgError("Preparation crypto checks are not all PASS")
    if manifest.get("target") != {"offset": OFFSET, "partition_size": SIZE, "write_size": PREFIX_SIZE, "parttype": "user"}:
        raise SeccfgError("Prepared target differs from the fixed SECCFG prefix")
    if manifest.get("original_sha256") != ORIGINAL_SHA or set(manifest.get("artifacts", {})) != set(ARTIFACTS):
        raise SeccfgError("Preparation coverage mismatch")
    artifacts = {}
    for name, size in ARTIFACTS.items():
        path = directory/name
        if path.is_symlink():
            raise SeccfgError("Prepared artifacts must not be symlinks")
        data = path.read_bytes()
        if len(data) != size or manifest["artifacts"][name] != {"size": size, "sha256": sha(data)}:
            raise SeccfgError("Prepared artifact hash or length mismatch")
        artifacts[name] = data
    original, candidate = artifacts["original.bin"], artifacts["candidate.bin"]
    if sha(original) != ORIGINAL_SHA or sha(candidate) != expected_sha or manifest.get("candidate_sha256") != expected_sha:
        raise SeccfgError("Explicit candidate or original hash mismatch")
    if artifacts["plaintext.bin"] != template.plaintext_body() or artifacts["decrypted.bin"] != artifacts["plaintext.bin"]:
        raise SeccfgError("Body crypto evidence mismatch")
    if artifacts["fresh-ciphertext.bin"] != artifacts["ciphertext.bin"]:
        raise SeccfgError("Fresh body encryption evidence mismatch")
    if artifacts["public-input.bin"] != bytes(range(32)) or artifacts["public-decrypted.bin"] != artifacts["public-input.bin"]:
        raise SeccfgError("Public roundtrip evidence mismatch")
    if artifacts["public-fresh-cipher.bin"] != artifacts["public-cipher.bin"]:
        raise SeccfgError("Public fresh encryption evidence mismatch")
    if template.assemble_partition(original, artifacts["ciphertext.bin"]) != candidate:
        raise SeccfgError("Candidate V3 structure or unchanged tail mismatch")
    if candidate[RECORD_SIZE:] != original[RECORD_SIZE:] or artifacts["prefix.bin"] != candidate[:PREFIX_SIZE]:
        raise SeccfgError("Prefix padding or unchanged partition tail mismatch")
    return manifest, artifacts


def write_prefix(da, prefix):
    if len(prefix) != PREFIX_SIZE:
        raise SeccfgError("Only the exact 8192-byte SECCFG prefix may be written")
    def send(data):
        if da.usbwrite(data) is not True:
            raise SeccfgError("DA write transport failed; outcome UNKNOWN, no retry")
    for data in (b"\x62", b"\x01", b"\x08", struct.pack(">Q", OFFSET),
                 struct.pack(">Q", PREFIX_SIZE), struct.pack(">I", 16384)):
        send(data)
    if bytes(da.usbread(1)) != b"\x5a":
        raise SeccfgError("DA header ACK mismatch; outcome UNKNOWN, no retry")
    send(b"\x5a")
    send(prefix)
    send(struct.pack(">H", sum(prefix) & 65535))
    if bytes(da.usbread(1)) != b"\x69":
        raise SeccfgError("DA CONT_CHAR mismatch; outcome UNKNOWN, no retry")


def commit(directory, expected_sha, serialport="/dev/cu.usbmodem21101", *, _runtime=None):
    directory = Path(directory)
    # Validate local custody before importing vendor code or connecting.
    source_snapshot()
    attach, template, _ = runtime() if _runtime is None else _runtime
    manifest, artifacts = validate_prepared(directory, expected_sha, template)
    if (directory/"commit-attempt.json").exists() or (directory/"receipt.json").exists():
        raise SeccfgError("A commit attempt already exists; reconcile its outcome, do not retry")
    attempted = False
    try:
        with attach.attached_session(serialport) as ctx:
            da = ctx.daloader.da
            live_original(da)
            if source_snapshot() != manifest["sources"]:
                raise SeccfgError("Sources changed before the first persistent command")
            save_json(directory/"commit-attempt.json", {"utc": utc(), "candidate_sha256": expected_sha,
                       "outcome": "UNKNOWN_UNTIL_FULL_READBACK", "offset": OFFSET, "length": PREFIX_SIZE})
            attempted = True
            write_prefix(da, artifacts["prefix.bin"])
            readback = bytes(da.readflash(addr=OFFSET, length=SIZE, filename="", parttype="user", display=False))
            if len(readback) != SIZE or sha(readback) != expected_sha:
                raise SeccfgError("Full SECCFG readback mismatch; outcome UNKNOWN, no retry")
            save_new(directory/"readback.bin", readback)
        # A failed close also prevents an optimistic completion receipt.
        receipt = {"utc": utc(), "outcome": "FULL_READBACK_MATCH", "candidate_sha256": expected_sha,
                   "readback_sha256": sha(readback), "readback_size": len(readback), "offset": OFFSET,
                   "written_bytes": PREFIX_SIZE, "reset_performed": False}
        save_json(directory/"receipt.json", receipt)
        return receipt
    except BaseException as exc:
        if attempted:
            try:
                save_json(directory/"commit-failure.json", {"utc": utc(), "outcome": "UNKNOWN", "error_type": type(exc).__name__,
                           "candidate_sha256": expected_sha, "retry_allowed": False})
            except OSError:
                pass
        raise
