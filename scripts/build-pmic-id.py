#!/usr/bin/env python3
"""Local reproducible static ARM64 build; never executes the native PMIC helper."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from i14_build_inputs import verified_archive

ROOT = Path(__file__).resolve().parents[1]
ZIG_SHA = "3cc2bab367e185cdfb27501c4b30b1b0653c28d9f73df8dc91488e66ece5fa6b"
out = Path(sys.argv[1]) if len(sys.argv) == 2 else ROOT / "builds" / ("pmic-id-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
if len(sys.argv) > 2 or out.exists() or out.is_symlink():
    raise SystemExit("one new output directory only; existing output refused")
out = out.resolve()
archive = ROOT / "artifacts/build-tools/zig-aarch64-macos-0.15.2.tar.xz"
with verified_archive(archive, ZIG_SHA) as source:
    out.mkdir(parents=True, mode=0o700)
    source.extractall(out, filter="data")
env = dict(os.environ, SOURCE_DATE_EPOCH="1725494400", TZ="UTC", LC_ALL="C",
           ZIG_GLOBAL_CACHE_DIR=str(ROOT / "artifacts/build-tools/zig-cache"))


def run(args):
    return subprocess.run(["rtk", "proxy", *map(str, args)], check=True, capture_output=True, env=env).stdout


for name in ("i14-pmic-id.c",):
    (out / name).write_bytes((ROOT / "tools" / name).read_bytes())
(out / "test_pmic_id.c").write_bytes((ROOT / "tests/test_pmic_id.c").read_bytes())
zig = out / "zig-aarch64-macos-0.15.2/zig"
flags = ["-target", "aarch64-linux.3.18.19-musl", "-mcpu=cortex_a53", "-std=c11",
         "-static", "-Oz", "-g", "-Wall", "-Wextra", "-Werror", f"-ffile-prefix-map={out}=BUILD"]
run([zig, "cc", *flags, out / "i14-pmic-id.c", "-o", out / "i14-pmic-id.full"])
llvm = Path(os.environ["LLVM_BIN"]) if "LLVM_BIN" in os.environ else Path(shutil.which("llvm-strip") or "llvm-strip").parent
run([llvm / "llvm-strip", "-s", out / "i14-pmic-id.full", "-o", out / "i14-pmic-id"])
run(["clang", "-std=c11", "-Wall", "-Wextra", "-Werror", ROOT / "tests/test_pmic_id.c", "-o", out / "host-tests"])
(out / "host-tests.txt").write_bytes(run([out / "host-tests"]))
(out / "disassembly.txt").write_bytes(run([llvm / "llvm-objdump", "-d", out / "i14-pmic-id.full"]))
data = (out / "i14-pmic-id").read_bytes()
assert data[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<HH", data, 16) == (2, 183)
table = struct.unpack_from("<Q", data, 32)[0]
size, count = struct.unpack_from("<HH", data, 54)
assert not {2, 3}.intersection(struct.unpack_from("<I", data, table+i*size)[0] for i in range(count))
manifest = {"status": "unexecuted native helper; source review required before manual use",
            "source_sha256": hashlib.sha256((out / "i14-pmic-id.c").read_bytes()).hexdigest(),
            "test_source_sha256": hashlib.sha256((out / "test_pmic_id.c").read_bytes()).hexdigest(),
            "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "zig_archive_sha256": ZIG_SHA, "zig_binary_sha256": hashlib.sha256(zig.read_bytes()).hexdigest(),
            "compiler_version": run([zig, "cc", *flags[:4], "--version"]).decode(), "flags": flags,
            "binary_bytes": len(data), "binary_sha256": hashlib.sha256(data).hexdigest(),
            "mapping": {"physical_base": "0x10001000", "bytes": 4096},
            "queries": ["PMIC READ0200", "PMIC READ0202", "PMIC READ0200"],
            "limits": {"total_ns": 1000000000, "poll_phase_ns": 200000000, "maximum_polls": 4000},
            "native_executed": False, "host_mock_passed": True}
(out / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
files = ("i14-pmic-id", "i14-pmic-id.full", "i14-pmic-id.c", "test_pmic_id.c", "host-tests.txt", "disassembly.txt", "manifest.json")
(out / "SHA256SUMS").write_text("".join(f"{hashlib.sha256((out/name).read_bytes()).hexdigest()}  {name}\n" for name in files))
print(json.dumps({"output": str(out), "bytes": len(data), "sha256": manifest["binary_sha256"], "native_executed": False}, indent=2))
