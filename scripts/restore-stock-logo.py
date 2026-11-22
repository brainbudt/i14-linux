#!/usr/bin/env python3
"""Restore ONLY the original stock logo partition over BROM legacy DA (native macOS serial).

The way back for a custom boot splash (docs/research/SPLASH-2026-11-02.md section 3, step 5):
  --read-only   one session: read logo, compare with the stock sha (BROM read path proven, nothing written)
  (default)     one session: write the verified stock logo.img, then read logo back

Reuses the guarded DA write of scripts/restore-stock-boot.py (proven on hardware 2 November 2026)
with the logo offset, size and image; that script is not changed. The write hook refuses anything
except the exact stock logo image at the GPT logo offset. No erase, format or other partition write.
No automatic retry. Phone: off, only Volume up held, then USB in (BROM).
"""
import argparse
import hashlib
import importlib.util
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STOCK = ROOT / "backups/20261102-phase0-logo/logo.img"  # copy of /Volumes/I14_BACKUP/i14-backups/20261102-phase0/logo.img
STOCK_SHA256 = "919068673649da0af3650c8eb7ce8bc486fefb0fef0d883ad0b161d254bfb372"
LOGO_OFFSET = 0x3D80000  # GPT entry 9 'logo', from backups/20261030T011515Z-native-serial/primary-userarea.bin
LOGO_SIZE = 0x800000


def load_boot_restore():
    """restore-stock-boot.py as a module, retargeted to logo (module globals drive its write hook)."""
    spec = importlib.util.spec_from_file_location("i14_restore_boot", ROOT / "scripts/restore-stock-boot.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STOCK, mod.STOCK_SHA256, mod.BOOT_OFFSET, mod.BOOT_SIZE = STOCK, STOCK_SHA256, LOGO_OFFSET, LOGO_SIZE
    return mod


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serialport", required=True, help="Exact MTK BROM /dev/cu.usbmodem path")
    parser.add_argument("--outdir", required=True, type=Path, help="New directory for the readback")
    parser.add_argument("--packet", type=int, default=16384, choices=[16384, 65536, 262144, 1048576])
    parser.add_argument("--read-only", action="store_true", help="only read logo and compare with stock")
    args = parser.parse_args(argv)
    boot = load_boot_restore()
    boot.PACKET = args.packet
    stock = boot.stock_bytes()  # size and sha of the stock logo, or RestoreError
    revision = subprocess.check_output(["git", "-C", str(boot.VENDOR), "rev-parse", "HEAD"], text=True).strip()
    if revision != boot.PIN:
        raise boot.RestoreError("Vendor revision mismatch")
    subprocess.run(["git", "-C", str(boot.VENDOR), "diff", "--exit-code", "--quiet", "HEAD"], check=True)
    args.outdir.mkdir(mode=0o700)
    after = args.outdir / ("logo-readback.img" if args.read_only else "logo-after-restore.img")

    spec = importlib.util.spec_from_file_location("i14_readback_guard", ROOT / "scripts/mtk-readback.py")
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    guard.serial_port_allowed(args.serialport, [])  # path syntax check only
    sys.path.insert(0, str(boot.VENDOR))
    from mtkclient.Library.Connection.seriallib import SerialClass
    from mtkclient.Library.DA.legacy.dalegacy_lib import DALegacy
    guard.install_serial_guards(SerialClass)
    DALegacy.readflash = guard.emmc_readflash
    if args.read_only:
        DALegacy.writeflash = lambda *a, **k: (_ for _ in ()).throw(boot.RestoreError("read-only session"))
    else:
        DALegacy.writeflash = boot.make_guarded_write(stock)
    for name in ("formatflash", "sdmmc_write_image"):
        setattr(DALegacy, name, lambda *a, **k: (_ for _ in ()).throw(boot.RestoreError("forbidden")))

    commands = ([] if args.read_only else [f"w logo {STOCK}"]) + [f"r logo {after}"]
    sys.argv = [str(boot.VENDOR / "mtk.py"), "--serialport", args.serialport, "--stock", "--noreconnect",
                "multi", ";".join(c + " --stock --noreconnect" for c in commands)]
    print(f"Logo session: {commands}", flush=True)
    try:
        runpy.run_path(str(boot.VENDOR / "mtk.py"), run_name="__main__")
    except SystemExit:
        pass
    if after.exists():
        data = after.read_bytes()
        print(f"{after.name}: {len(data)} bytes sha256={boot.sha(data)}", flush=True)
        if boot.sha(data) == STOCK_SHA256:
            print("RESULT: FULL 8 MiB LOGO READBACK MATCHES STOCK", flush=True)
            return 0
    if args.read_only:
        print("RESULT: readback missing or not stock (nothing was written)", flush=True)
        return 2
    print("RESULT: readback missing or mismatch; outcome UNKNOWN. Do not reboot blindly.", flush=True)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # RestoreError of the loaded module, or a vendor/serial failure
        print(f"Logo restore stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
