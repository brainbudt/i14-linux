"""Reattach to the existing I14 legacy DA; no USB or serial action on import."""
import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "artifacts/tooling/mtkclient"
PIN = "118a000a83ea31c4f427fe5225abfe964546e05e"
GUARD_SHA256 = "924ed34c11fe862cb50946809f78dbb3567d73836bb31d710eac2297746a8fca"
STATE = ROOT / "backups/20261030T011515Z-native-serial/.state"
PRELOADER = ROOT / "artifacts/unlock-analysis-20261030/own-preloader.bin"
PRELOADER_SHA256 = "4f791ddbbeaa2343f1aec84603e5e77cfe75dd94b29b8ee1598413fc77b69c30"
REFERENCE = STATE.parent / "primary-header.bin"
REFERENCE_SHA256 = "abaeedba21a3680155f1d5d7445227e411348f483abc734646927bfe8f08f624"
FLASH_SIZE = 0x3a3e00000
SECURITY_KEYS = {"sbc", "sla", "daa", "epp", "cert", "memread", "memwrite", "cmdC8"}

class SessionError(RuntimeError):
    pass


def validate_state(state):
    expected = {"hwcode": 0x321, "flashmode": "LEGACY", "flashtype": "emmc",
                "flashsize": FLASH_SIZE, "m_emmc_ua_size": FLASH_SIZE,
                "m_emmc_boot1_size": 0x400000, "m_emmc_boot2_size": 0x400000}
    if any(state.get(key) != value for key, value in expected.items()):
        raise SessionError("Saved state does not match the measured I14 storage/chip")
    flags = state.get("target_config")
    if not isinstance(flags, dict) or set(flags) != SECURITY_KEYS or any(value is not False for value in flags.values()):
        raise SessionError("Saved security flags are not exactly the measured all-false set")
    if state.get("patched") is not True:
        raise SessionError("Saved DA patch state differs from the existing session")


def verified_bytes(path, expected, size=None):
    data = Path(path).read_bytes()
    if (size is not None and len(data) != size) or hashlib.sha256(data).hexdigest() != expected:
        raise SessionError("Local input hash or size mismatch")
    return data


def verify_vendor():
    revision = subprocess.check_output(["rtk", "proxy", "git", "-C", str(VENDOR), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN:
        raise SessionError("Vendor revision mismatch")
    subprocess.run(["rtk", "proxy", "git", "-C", str(VENDOR), "diff", "--exit-code", "--quiet", "HEAD"], check=True)


def dependencies():
    verify_vendor()
    guard_path = ROOT / "scripts/mtk-readback.py"
    verified_bytes(guard_path, GUARD_SHA256)
    spec = importlib.util.spec_from_file_location("i14_readback_guard", guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    sys.path.insert(0, str(VENDOR))
    from mtkclient.config.mtk_config import MtkConfig
    from mtkclient.Library.mtk_class import Mtk
    from mtkclient.Library.Connection.seriallib import SerialClass
    from mtkclient.Library.DA.legacy.dalegacy_lib import DALegacy
    guard.install_serial_guards(SerialClass)
    DALegacy.readflash = guard.emmc_readflash
    return MtkConfig, Mtk


def forbidden(*args, **kwargs):
    raise SessionError("Persistent flash, reset, upload or unlock action is outside this attach context")


def protect_context(mtk):
    # Keep register access available for an explicitly requested later volatile HACC step.
    for obj in (mtk.daloader, mtk.daloader.da):
        for name in ("writeflash", "formatflash", "upload_da", "shutdown", "finish", "sdmmc_write_data", "sdmmc_write_image"):
            if hasattr(obj, name):
                setattr(obj, name, forbidden)
    for name in ("crasher", "bypass_security"):
        if hasattr(mtk, name):
            setattr(mtk, name, forbidden)
    for name in ("init", "reset_to_brom", "send_da", "jump_da"):
        if hasattr(mtk.preloader, name):
            setattr(mtk.preloader, name, forbidden)
    for name in ("seccfg", "writeflash"):
        if hasattr(mtk.daloader.lft, name):
            setattr(mtk.daloader.lft, name, forbidden)


@contextmanager
def attached_session(serialport="/dev/cu.usbmodem21101", state_path=STATE,
                     preloader_path=PRELOADER, reference_path=REFERENCE, *, _factories=None):
    """Yield an identity-probed Mtk context, then close without reset; never retry."""
    state_path = Path(state_path).resolve()
    state_bytes = state_path.read_bytes()
    state = json.loads(state_bytes)
    validate_state(state)
    preloader = verified_bytes(preloader_path, PRELOADER_SHA256)
    reference = verified_bytes(reference_path, REFERENCE_SHA256, 4096)
    Config, Mtk = dependencies() if _factories is None else _factories
    cfg = Config()
    cfg.init_hwcode(0x321)
    cfg.hwparam_path = str(state_path.parent)
    cfg.preloader = preloader
    cfg.preloader_filename = str(preloader_path)
    cfg.stock = True
    cfg.reconnect = False
    cfg.generatekeys = False
    mtk = Mtk(cfg, serialportname=serialport)
    try:
        if not mtk.port.cdc.connect():
            raise SessionError("The exact native serial port is unavailable; no reconnect attempted")
        if state_path.read_bytes() != state_bytes:
            raise SessionError("State changed before reattach")
        if not mtk.daloader.reinit():
            raise SessionError("Saved DA state could not be reinitialized")
        if state_path.read_bytes() != state_bytes:
            raise SessionError("State changed during reattach")
        validate_state(state)
        if mtk.daloader.lft.patched_read is not False:
            raise SessionError("Reinitialized extension unexpectedly requires patched memory reads")
        protect_context(mtk)
        status = mtk.daloader.da.check_usb_cmd()
        if status is None or len(status) != 1:
            raise SessionError("Existing DA did not answer its status query")
        data = mtk.daloader.da.readflash(addr=0, length=4096, filename="", parttype="user", display=False)
        if bytes(data) != reference:
            raise SessionError("Live 4-KiB primary GPT bytes differ from the verified backup")
        yield mtk
    finally:
        mtk.port.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serialport", default="/dev/cu.usbmodem21101")
    args = parser.parse_args(argv)
    with attached_session(args.serialport):
        pass
    print("Existing legacy DA attached; status and exact 4-KiB GPT identity match. Closed without reset.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Attach stopped ({type(exc).__name__}); no retry/reset/upload. Preserve session state.", file=sys.stderr)
        sys.exit(1)
