"""Bounded USB wait, then ONE read-only cached baseline; never charge or retry."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT / "evidence/2026-11-01/r3-run-181615/tn.py"
TRANSPORT_SHA = "5e1aa9e36d72eadb0aeaca606867d3bfda8fbcc54e34c7eebbd1e142c5813f90"
COMMAND = r'''if [ "$(uname -r)" = 7.0.10-i14-bringup ] && [ "$(uname -m)" = aarch64 ] && [ "$(sha256sum /proc/device-tree/model)" = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a  /proc/device-tree/model" ] && [ "$(sha256sum /proc/device-tree/compatible)" = "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a  /proc/device-tree/compatible" ]; then echo I14_IDENTITY_OK; cat /proc/self/status; cat /proc/sys/kernel/random/boot_id; cat /proc/uptime; cat /proc/modules; cat /sys/devices/i14-charger/state; cat /sys/devices/i14-reboot-target/battery_sample; cat /sys/class/block/mmcblk0/size; for p in /sys/class/block/mmcblk*; do echo "$p"; cat "$p/ro"; done; cat /sys/class/block/mmcblk0/stat; cat /proc/mounts; cat /sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower; cat /sys/class/udc/musb-hdrc.3.auto/state; cat /sys/class/net/usb0/carrier; echo I14_CACHED_CAPTURE_DONE; else echo I14_STOP_IDENTITY; fi'''


def records(tree):
    if isinstance(tree, list):
        for child in tree:
            yield from records(child)
    elif isinstance(tree, dict):
        if "idVendor" in tree:
            yield {k: tree.get(k) for k in ("idVendor", "idProduct", "USB Serial Number", "locationID")}
        yield from records(tree.get("IORegistryEntryChildren", []))


def target_present(devices):
    return any(d == {"idVendor": 0x1d6b, "idProduct": 0x0104,
                     "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}
               for d in devices)


def scan():
    result = subprocess.run(["ioreg", "-p", "IOUSB", "-a", "-l"],
                            capture_output=True, check=True, timeout=5)
    return list(records(plistlib.loads(result.stdout)))


def log(out, message):
    line = datetime.datetime.now(datetime.timezone.utc).isoformat() + " " + message
    with (out / "watch.log").open("a") as file:
        file.write(line + "\n")
    print(line, flush=True)


def capture(out):
    data = TRANSPORT.read_bytes()
    if hashlib.sha256(data).hexdigest() != TRANSPORT_SHA:
        raise RuntimeError("transport hash mismatch; no command sent")
    # Restrict the existing transport to the Mac's dedicated phone address.
    old = b"socket.create_connection(('10.77.14.2',23),timeout=10)"
    if data.count(old) != 1:
        raise RuntimeError("unexpected transport; no command sent")
    data = data.replace(old, b"socket.create_connection(('10.77.14.2',23),timeout=10,source_address=('10.77.14.1',0))")
    (out / "transport.py").write_bytes(data)
    command = out / "cached-baseline.command"
    command.write_text(COMMAND + "\n")
    result = subprocess.run([sys.executable, "-I", str(out / "transport.py"),
                             str(command), str(out / "cached-baseline.txt"), "10"],
                            capture_output=True, timeout=18)
    (out / "transport-output.txt").write_bytes(result.stdout + result.stderr)
    raw = (out / "cached-baseline.txt").read_bytes() if (out / "cached-baseline.txt").exists() else b""
    ok = result.returncode == 0 and b"\nI14_CACHED_CAPTURE_DONE\r\n" in raw and b"\nI14_CHECK_END\r\n" in raw
    return 0 if ok else 2


def watch(out, seconds):
    if (out / "attempt.json").exists():
        return 2
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if target_present(scan()):
            # Reserve before connecting: transport failure is terminal, never retried.
            with (out / "attempt.json").open("x") as file:
                json.dump({"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "mode": "cached-read-only"}, file)
            log(out, "FOUND exact target; capturing once")
            result = capture(out)
            log(out, "COMPLETE" if result == 0 else "STOP capture incomplete; no retry")
            return result
        time.sleep(2)
    log(out, "TIMEOUT; no phone command sent")
    return 3


if __name__ == "__main__":
    out = Path(sys.argv[1]).resolve()
    seconds = int(sys.argv[2])
    if not 1 <= seconds <= 3600:
        raise SystemExit("duration must be 1..3600 seconds")
    lock = (ROOT / "artifacts/i14-native-watch.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    out.mkdir(parents=True, exist_ok=False)
    (out / "pid").write_text(str(os.getpid()) + "\n")
    log(out, "START waiting up to %d seconds; cached-read-only, one attempt" % seconds)
    try:
        raise SystemExit(watch(out, seconds))
    except Exception as error:
        log(out, "STOP " + repr(error))
        raise SystemExit(2)
