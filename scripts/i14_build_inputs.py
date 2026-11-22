"""Verify pinned archives before handing their contents to a build."""

import argparse
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tarfile


@contextmanager
def verified_archive(path, expected):
    with path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected:
            raise SystemExit(f"SHA-256 mismatch for {path.name}")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:*") as archive:
            yield archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("sha256")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    with verified_archive(args.archive, args.sha256) as archive:
        archive.extractall(args.directory, filter="data")
