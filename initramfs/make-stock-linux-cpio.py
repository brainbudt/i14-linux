#!/usr/bin/env python3
"""Deterministic phase-1 initramfs for the stock 3.18 kernel (console + battery log)."""
import argparse
import hashlib
import importlib.util
import json
import pathlib
import stat

SOURCE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("stock_charge_cpio", SOURCE / "make-stock-charge-cpio.py")
base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(base)


def entries(build):
    binary, applets = base.inputs(build)  # pinned static BusyBox, 47 applets
    result = {n: (stat.S_IFDIR | 0o755, b"", 0, 0) for n in ("bin", "dev", "proc", "sys", "run")}
    result["bin/busybox"] = (stat.S_IFREG | 0o755, binary, 0, 0)
    result["init"] = (stat.S_IFREG | 0o755, (SOURCE / "stock-linux-init").read_bytes(), 0, 0)
    result["storage-readonly"] = (stat.S_IFREG | 0o755, (SOURCE / "storage-readonly").read_bytes(), 0, 0)
    result["bin/i14-pmic-dump"] = (stat.S_IFREG | 0o755, (SOURCE / "i14-pmic-dump").read_bytes(), 0, 0)
    for name in applets:
        if name != "busybox":
            result["bin/" + name] = (stat.S_IFLNK | 0o777, b"/bin/busybox", 0, 0)
    for name, major, minor in (("console", 5, 1), ("null", 1, 3), ("kmsg", 1, 11)):
        result["dev/" + name] = (stat.S_IFCHR | 0o600, b"", major, minor)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("build", type=pathlib.Path, help="BusyBox build dir (busybox, busybox.config, busybox.links)")
    p.add_argument("output", type=pathlib.Path)
    a = p.parse_args()
    items = entries(a.build)
    data = base.archive(items)
    a.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    (a.output / "stock-linux.cpio.gz").write_bytes(data)
    manifest = {
        "archive_sha256": hashlib.sha256(data).hexdigest(),
        "entries": {n: {"mode": oct(v[0]), "sha256": hashlib.sha256(v[1]).hexdigest()} for n, v in sorted(items.items())},
    }
    (a.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"archive_sha256": manifest["archive_sha256"], "entries": len(items), "bytes": len(data)}))


if __name__ == "__main__":
    main()
