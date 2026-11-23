#!/usr/bin/env python3
"""Fixed boot-only install; see docs/BOOT-INSTALL-2026-10-30.md before use.

No automatic reboot, restore, erase, retry or other partition writes. Run only
after the exact candidate's native RAM boot has been independently verified.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

from fastboot_usb import FastbootError, MAX_IMAGE, PRODUCT, connect, validate_image

STOCK_SHA256 = "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492"
TEST_CMDLINE_PREFIXES = (b"androidboot.mode=", b"i14.autoboot=", b"i14.test=")


def validate_install_cmdline(data):
    """Check both NUL-terminated command-line fields of the legacy v0 header."""
    for start, end in ((64, 576), (608, 1632)):
        for word in data[start:end].split(b"\0", 1)[0].split():
            for prefix in TEST_CMDLINE_PREFIXES:
                if word.startswith(prefix):
                    shown = word if len(word) <= 64 and all(33 <= c <= 126 for c in word) else prefix
                    raise FastbootError(f"RAM-boot-only test image ({shown.decode('ascii')}); never install it")


def validate_stock(data):
    if len(data) != MAX_IMAGE or hashlib.sha256(data).hexdigest() != STOCK_SHA256:
        raise FastbootError("stock backup must match the pinned complete 16 MiB boot partition")


@contextmanager
def attempt_receipt(path, source, data):
    """Exclusive append-only evidence; persist intent and its directory entry."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        started = False

        def record(event, **fields):
            nonlocal started
            if event == "protocol_started":
                if started:
                    raise FastbootError("attempt already consumed; reuse prohibited")
                started = True
            entry = dict(event=event, time_utc=datetime.now(timezone.utc).isoformat(),
                         readback="UNKNOWN", **fields)
            output.write(json.dumps(entry, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())

        record("attempt_created", partition="boot", product=PRODUCT,
               expected_port=[1, 1], candidate_source=str(Path(source).resolve()),
               candidate_bytes=len(data), candidate_sha256=hashlib.sha256(data).hexdigest(),
               stock_bytes=MAX_IMAGE, stock_sha256=STOCK_SHA256,
               padding="none; exact candidate bytes", nv_write="NOT_STARTED")
        directory = os.open(Path(path).resolve().parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        yield record


def install_boot(client, data, record):
    """Only called after local validation and exclusive receipt creation."""
    validate_install_cmdline(data)
    record("protocol_started", nv_write="NOT_STARTED")
    nv_write = "NOT_STARTED"
    try:
        if tuple(client.device.port_numbers or ()) != (1, 1):
            raise FastbootError("physical USB port mismatch")
        client._guard()
        if client._getvar("unlocked") != "yes":
            raise FastbootError("boot install requires unlocked=yes")
        if client._getvar("secure") != "no":
            raise FastbootError("boot install requires secure=no")
        try:
            partition = int(client._getvar("partition-size:boot"), 16)
            maximum = int(client._getvar("max-download-size"), 16)
        except ValueError as error:
            raise FastbootError("invalid boot partition or maximum download size") from error
        if partition != MAX_IMAGE or not 0 < len(data) <= min(partition, maximum):
            raise FastbootError("boot partition or maximum download size mismatch")
        client._download(data)
        # This durable record must precede the first possible nonvolatile write.
        record("flash_intent", command="flash:boot", nv_write="UNKNOWN")
        nv_write = "UNKNOWN"
        client._command("flash:boot")
        record("protocol_acknowledged", nv_write="ACKNOWLEDGED_UNVERIFIED")
    except BaseException:
        record("stopped", nv_write=nv_write)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--stock-backup", required=True, type=Path)
    parser.add_argument("--attempt", required=True, type=Path,
                        help="new exclusive JSONL receipt; existing paths are never reused")
    args = parser.parse_args(argv)
    client = None
    result = 0
    try:
        with args.image.open("rb") as source:
            data = source.read(MAX_IMAGE + 1)
        validate_image(data, args.sha256)
        validate_install_cmdline(data)
        with args.stock_backup.open("rb") as stock:
            validate_stock(stock.read(MAX_IMAGE + 1))
        with attempt_receipt(args.attempt, args.image, data) as record:
            try:
                client = connect()
                install_boot(client, data, record)
            except BaseException:
                if client is None:
                    record("stopped", nv_write="NOT_STARTED")
                raise
        print(f"{PRODUCT}: flash:boot acknowledged; readback=UNKNOWN; no reboot sent")
    except FastbootError as error:
        print(f"stopped: {error}; inspect receipt before any further attempt", file=sys.stderr)
        result = 1
    except FileExistsError:
        print("stopped: attempt receipt already exists; reuse prohibited", file=sys.stderr)
        result = 1
    except (Exception, KeyboardInterrupt):
        print("stopped: I/O or interruption; inspect receipt; no retry", file=sys.stderr)
        result = 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                print("stopped: USB cleanup failed; inspect receipt", file=sys.stderr)
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
