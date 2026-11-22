"""Record inputs and output for an unpackaged I14 rootfs build."""

import argparse
import hashlib
import json
from pathlib import Path
import re


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def manifest(work, packages, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("source commit must be a full Git SHA")
    inputs = ["minirootfs.tar.gz", "system.img", "build-alpine-rootfs.sh", "configure-dns.py",
              "make-android-props.py", "write-build-manifest.py", "android-wifi.sha256", "authorized_keys"]
    if (work / "dns-clients.conf").exists():
        inputs.append("dns-clients.conf")
    package_list = sorted(packages.read_text().splitlines())
    if not package_list:
        raise ValueError("installed package inventory is empty")
    overlay = {}
    for path in sorted((work / "overlay").rglob("*")):
        mode = path.lstat().st_mode
        if path.is_symlink():
            entry = {"link": path.readlink().as_posix()}
        elif path.is_file():
            entry = {"sha256": digest(path)}
        elif path.is_dir():
            continue
        else:
            raise ValueError("unsupported file type in rootfs overlay")
        overlay[path.relative_to(work).as_posix()] = dict(entry, mode=f"{mode & 0o7777:04o}")
    return {
        "source_commit": commit,
        "inputs_sha256": {name: digest(work / name) for name in inputs},
        "overlay": overlay,
        "packages": package_list,
        "rootfs_sha256": digest(work / "out/rootfs.img"),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("packages", type=Path)
    parser.add_argument("commit")
    args = parser.parse_args()
    try:
        data = manifest(args.work, args.packages, args.commit)
        with (args.work / "out/build-manifest.json").open("x") as output:
            json.dump(data, output, indent=2, sort_keys=True)
            output.write("\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Build manifest failed: {exc}\n")
