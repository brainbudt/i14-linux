#!/usr/bin/env python3
"""Restore ONLY the original stock boot partition over BROM legacy DA (native macOS serial).

One session: read current boot -> write verified stock boot.img -> read back boot.
Reuses the proven guards from mtk-readback.py (16 KiB host/DA packets, checksummed reads).
The write hook refuses anything except the exact stock image at the GPT boot offset.
No erase, format, seccfg, preloader or other partition write. No automatic retry.
"""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "artifacts/tooling/mtkclient"
PIN = "118a000a83ea31c4f427fe5225abfe964546e05e"
STOCK = ROOT / "backups/20261030T011515Z-native-serial/boot.img"
STOCK_SHA256 = "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492"
BOOT_OFFSET = 0x1D80000  # GPT 'boot', verified from primary-userarea.bin
BOOT_SIZE = 0x1000000
PACKET = 16384  # overridable via --packet
RESP_TIMEOUT = 60
ACK = b"\x5a"
CONT = b"\x69"


class RestoreError(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stock_bytes():
    data = STOCK.read_bytes()
    if len(data) != BOOT_SIZE or sha(data) != STOCK_SHA256:
        raise RestoreError("Stock boot backup size/hash mismatch")
    return data


def make_guarded_write(stock):
    def writeflash(self, addr, length, filename="", offset=0, parttype=None, wdata=None, display=True):
        if parttype not in (None, "user"):
            raise RestoreError("Only the user area may be written")
        if addr != BOOT_OFFSET or length != BOOT_SIZE or offset != 0 or wdata is not None:
            raise RestoreError(f"Refused write addr={addr:#x} len={length:#x}; only boot is allowed")
        if not filename or Path(filename).resolve() != STOCK.resolve() or Path(filename).read_bytes() != stock:
            raise RestoreError("Refused write: file is not the verified stock boot image")
        dev = self.mtk.port.cdc.device

        def read1(timeout=None):
            timeout = RESP_TIMEOUT if timeout is None else timeout
            old = dev.timeout
            dev.timeout = timeout
            try:
                data = bytes(dev.read(1))
            finally:
                dev.timeout = old
            if len(data) != 1:
                raise RestoreError("DA response timeout during write; outcome UNKNOWN, no retry")
            return data

        def send(data):
            if self.usbwrite(data) is not True:
                raise RestoreError("DA write transport failed; outcome UNKNOWN, no retry")

        for part in (self.Cmd.SDMMC_WRITE_DATA_CMD, b"\x01", b"\x08", struct.pack(">Q", BOOT_OFFSET),
                     struct.pack(">Q", BOOT_SIZE), struct.pack(">I", PACKET)):
            send(part)
        if read1() != ACK:
            raise RestoreError("DA write header not acknowledged; nothing written")
        print(f"Write header ACK; writing stock boot in {PACKET} byte packets", flush=True)
        import time as _t
        t0 = _t.time()
        for pos in range(0, BOOT_SIZE, PACKET):
            chunk = stock[pos:pos + PACKET]
            send(ACK)
            send(chunk)
            send(struct.pack(">H", sum(chunk) & 0xFFFF))
            if read1() != CONT:
                raise RestoreError(f"DA packet at {pos:#x} not confirmed; outcome UNKNOWN, no retry")
            if (pos + PACKET) % (256 * 1024) == 0:
                print(f"  confirmed {(pos + PACKET) // 1024} KiB at {_t.time() - t0:.1f}s", flush=True)
        print("Stock boot write: all packets confirmed by DA", flush=True)
        return True
    return writeflash


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serialport", required=True, help="Exact MTK BROM /dev/cu.usbmodem path")
    parser.add_argument("--outdir", required=True, type=Path, help="New directory for before/after readbacks")
    parser.add_argument("--packet", type=int, default=16384, choices=[16384, 65536, 262144, 1048576])
    args = parser.parse_args(argv)
    global PACKET
    PACKET = args.packet
    stock = stock_bytes()
    revision = subprocess.check_output(["git", "-C", str(VENDOR), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN:
        raise RestoreError("Vendor revision mismatch")
    subprocess.run(["git", "-C", str(VENDOR), "diff", "--exit-code", "--quiet", "HEAD"], check=True)
    args.outdir.mkdir(mode=0o700)
    before, after = args.outdir / "boot-before-restore.img", args.outdir / "boot-after-restore.img"

    spec = importlib.util.spec_from_file_location("i14_readback_guard", ROOT / "scripts/mtk-readback.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.serial_port_allowed(args.serialport, [])  # path syntax check only
    sys.path.insert(0, str(VENDOR))
    from mtkclient.Library.Connection.seriallib import SerialClass
    from mtkclient.Library.DA.legacy.dalegacy_lib import DALegacy
    guard.install_serial_guards(SerialClass)
    DALegacy.readflash = guard.emmc_readflash
    DALegacy.writeflash = make_guarded_write(stock)
    for name in ("formatflash", "sdmmc_write_image"):
        setattr(DALegacy, name, lambda *a, **k: (_ for _ in ()).throw(RestoreError("forbidden")))

    commands = [f"w boot {STOCK}", f"r boot {after}"]  # Linux boot already saved in run3
    sys.argv = [str(VENDOR / "mtk.py"), "--serialport", args.serialport, "--stock", "--noreconnect",
                "multi", ";".join(c + " --stock --noreconnect" for c in commands)]
    print(f"Restore session: {commands}", flush=True)
    try:
        runpy.run_path(str(VENDOR / "mtk.py"), run_name="__main__")
    except SystemExit:
        pass
    for path in (before, after):
        if path.exists():
            data = path.read_bytes()
            print(f"{path.name}: {len(data)} bytes sha256={sha(data)}", flush=True)
    if after.exists() and sha(after.read_bytes()) == STOCK_SHA256:
        print("RESULT: FULL 16 MiB READBACK MATCHES STOCK BOOT", flush=True)
        return 0
    print("RESULT: readback missing or mismatch; outcome UNKNOWN. Do not reboot blindly.", flush=True)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RestoreError as exc:
        print(f"Restore stopped: {exc}", file=sys.stderr)
        sys.exit(1)
