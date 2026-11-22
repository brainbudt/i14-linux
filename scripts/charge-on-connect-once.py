"""Historical charger runner; failed candidates are blocked before arming or I/O."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "artifacts/charging-mode-init-20261101/ready"
spec = importlib.util.spec_from_file_location("native_wait", ROOT / "scripts/wait-native-once.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)
PINS = {"i14-charger.ko": "f328df9f96e183251c22bec1d0715c55d868cfffd60b13baddf5ec49dac5e42e",
        "i14-usb-power-500": "5bd4d6f9469b4f42cb8a0b87d5eed90428eb72e25d87162c8ea95366a97d8003"}
FAILED_CHARGER_HASHES = frozenset({
    "b95def24322463d1227a01358cd250e87d404119ea00e8f3170a0fc2cfaee002",  # R3
    "2f6c8cbb733de576105ef7534127335fb8e07eadac4d8af1597d8db018110be2",  # R5
    "f328df9f96e183251c22bec1d0715c55d868cfffd60b13baddf5ec49dac5e42e",  # MODE
})
REMOTE = "/run/i14-charge-mode-20261101"
DESCRIPTOR_READER = ROOT / "scripts/read-i14-usb-descriptor.py"


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def require_candidate_not_failed():
    # Rejecting historical failures does not qualify any other candidate for runtime.
    require(not (PACK / "STOP-NATIVE-FAILED.txt").exists(),
            "charging candidate blocked: STOP-NATIVE-FAILED.txt; no retry")
    require(PINS.get("i14-charger.ko") not in FAILED_CHARGER_HASHES,
            "charging candidate blocked: known failed R3/R5/MODE module; no retry")


def host_500(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    expected = {"device_count": 1, "idVendor": 0x1d6b, "idProduct": 0x0104,
                "bus": 2, "port_numbers": [1, 1], "serial_number": "I14_NATIVE_INIT",
                "bcdUSB": 512, "configuration": {"bConfigurationValue": 1,
                "bmAttributes": 192, "bMaxPower": 250}}
    return (data == expected and
            all(type(data[k]) is int for k in ("device_count", "idVendor", "idProduct", "bus", "bcdUSB")) and
            all(type(v) is int for v in data["port_numbers"]) and
            all(type(v) is int for v in data["configuration"].values()))


def wait_ready(deadline):
    # Local metadata and bind checks only; never probe a native TCP port.
    while time.monotonic() < deadline:
        if native.target_present(native.scan()):
            try:
                with socket.socket() as s:
                    s.bind(("10.77.14.1", 0))
                return True
            except OSError:
                pass
        time.sleep(1)
    return False


class Runner:
    def __init__(self, out):
        self.out = out
        self.boot = None
        self.step = 0

    def log(self, text):
        native.log(self.out, text)

    def run(self, name, command):
        self.step += 1
        if self.boot:
            command = 'if [ "$(cat /proc/sys/kernel/random/boot_id)" = "' + self.boot + '" ]; then ' + command + '; else echo I14_STOP_BOOT_CHANGED; fi'
        p = self.out / ("%02d-%s.command" % (self.step, name))
        p.write_text(command + "\n")
        dest = p.with_suffix(".txt")
        r = subprocess.run([sys.executable, "-I", str(self.out / "transport.py"), str(p), str(dest), "12"],
                           capture_output=True, timeout=20)
        raw = dest.read_bytes().decode(errors="replace").replace("\r", "") if dest.exists() else ""
        (p.with_suffix(".stderr")).write_bytes(r.stderr)
        require(r.returncode == 0 and "\nI14_CHECK_END\n" in raw, name + ": transport incomplete; no retry")
        # Echoed shell source is one line; markers below must be actual output lines.
        body = raw.split("\n", 1)[-1]
        require("\nI14_STOP_BOOT_CHANGED\n" not in raw, "boot changed")
        return body

    def execute(self, assets):
        require_candidate_not_failed()
        # Copy the already tested, source-bound transport; do not import its CLI.
        data = native.TRANSPORT.read_bytes()
        require(hashlib.sha256(data).hexdigest() == native.TRANSPORT_SHA, "transport pin")
        old = b"socket.create_connection(('10.77.14.2',23),timeout=10)"
        require(data.count(old) == 1, "transport layout")
        data = data.replace(old, b"socket.create_connection(('10.77.14.2',23),timeout=10,source_address=('10.77.14.1',0))")
        (self.out / "transport.py").write_bytes(data)
        b = self.run("identity", native.COMMAND)
        require("\nI14_IDENTITY_OK\n" in b and "\nI14_CACHED_CAPTURE_DONE\n" in b, "identity rejected")
        require(re.search(r"\nUid:\s+0\s+0\s+0\s+0\s*\n", b), "root required")
        boot = re.findall(r"(?m)^([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$", b)
        require(len(boot) == 1, "boot receipt")
        self.boot = boot[0]
        # Only a fresh module-free boot is eligible; never unload/reset an old latch.
        b = self.run("guards", 'm=$(cat /proc/modules) && [ -z "$m" ] && echo MODULES_EMPTY; n=0; rw=0; for p in /sys/class/block/mmcblk*; do n=$((n+1)); [ "$(cat "$p/ro")" = 1 ] || rw=$((rw+1)); done; echo STORAGE=$n:$rw:$(cat /sys/class/block/mmcblk0/size); /usr/bin/i14-pwrap-status; cat /sys/class/net/usb0/carrier; cat /sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower')
        require("\nMODULES_EMPTY\n" in b and "\nSTORAGE=26:0:30535680\n" in b, "module/storage guards")
        require(b.count("WDT_FLG=00000000 INT_RAW=00000000 INT_FLG=00000000") == 3, "PWRAP guards")
        require("\n1\n2\n" in b, "carrier/initial descriptor")
        b = self.run("prepare", "umask 077; mkdir " + REMOTE + "; echo PREP_RC=$?")
        require("\nPREP_RC=0\n" in b, "RAM directory exists/failed")
        for name, data in assets.items():
            remote = REMOTE + "/" + name
            b = self.run("listen-" + name, "nc -l -p 18081 -w 20 >" + remote + " 2>" + REMOTE + "/receive.log & echo LISTEN_PID=$!")
            require(re.search(r"\nLISTEN_PID=\d+\n", b), "listener launch")
            with socket.create_connection(("10.77.14.2", 18081), timeout=5, source_address=("10.77.14.1", 0)) as s:
                s.sendall(data)
                s.shutdown(socket.SHUT_WR)
                s.settimeout(5)
                while s.recv(4096):
                    pass
            b = self.run("hash-" + name, "sha256sum " + remote)
            require("\n" + PINS[name] + "  " + remote + "\n" in b, "remote asset hash")
        b = self.run("load", "insmod " + REMOTE + "/i14-charger.ko; echo LOAD_RC=$?; cat /sys/devices/i14-charger/state")
        require("\nLOAD_RC=0\n" in b and re.search(r"\nphase=idle reason=initialized-read-only error=0 held_ref=0 mutated=0 ", b), "module load/idle")
        # R3 performs its own fresh CH0/2/3 measurements; no extra ADC requests.
        b = self.run("helper", "chmod 700 " + REMOTE + "/i14-usb-power-500; " + REMOTE + "/i14-usb-power-500; echo HELPER_RC=$?")
        match = re.search(r"\nSTARTED (/run/i14-usb-power-500\.\d+)\n", b)
        require(match and "\nHELPER_RC=0\n" in b, "helper did not launch")
        self.log("USB helper launched; checking reconnect and host descriptor")
        time.sleep(4)
        # Reenumeration can precede the source IP; failed native I/O stays terminal.
        require(wait_ready(time.monotonic() + 60), "USB/source address did not return; no command sent")
        b = self.run("helper-log", "cat " + match.group(1) + "/log")
        require("\nDONE descriptor=500mA;" in b and "\nERROR" not in b, "helper not DONE")
        sp = subprocess.run([str(ROOT / ".venv/bin/python"), "-I",
                             str(self.out / "read-i14-usb-descriptor.py"),
                             str(ROOT / "artifacts/tooling/lib/libusb-1.0.so")],
                            capture_output=True, text=True, timeout=25)
        (self.out / "host-usb.json").write_text(sp.stdout)
        (self.out / "host-usb.stderr").write_text(sp.stderr)
        require(sp.returncode == 0 and host_500(sp.stdout), "missing/wrong host 500mA descriptor; no pilot")
        b = self.run("usb-guards", "cat /sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower; cat /sys/class/udc/musb-hdrc.3.auto/state; cat /sys/class/udc/musb-hdrc.3.auto/gadget/suspended; cat /sys/class/net/usb0/carrier; cat /sys/devices/i14-charger/state")
        require("\n500\nconfigured\n0\n1\n" in b, "USB guards")
        require(re.search(r"\nphase=idle reason=initialized-read-only error=0 held_ref=0 mutated=0 ", b), "charger no longer clean")
        self.log("Issuing ONE source-reviewed MODE-init hypothesis 70mA/max5s pilot; native guards enforce ADC/WDT/budget")
        b = self.run("pilot", "printf 'usb-pilot-500\\n' >/sys/devices/i14-charger/control; echo PILOT_RC=$?; cat /sys/devices/i14-charger/state")
        require("\nPILOT_RC=0\n" in b, "pilot command refused")
        time.sleep(18)
        b = self.run("cached-result", "cat /sys/devices/i14-charger/state; cat /proc/uptime")
        require(re.search(r"\nphase=done .*error=0 held_ref=0 ", b) and re.search(r"\bstop_verified=1(?:\s|$)", b) and re.search(r"\bcore_uncertain=0x0(?:\s|$)", b) and re.search(r"\badc_uncertain=0x0(?:\s|$)", b), "pilot not verified done; preserve state, no retry")
        self.log("PILOT_DONE stop verified; physical/net charging remains unproven")


def main(out, seconds):
    require_candidate_not_failed()
    require(1 <= seconds <= 3600, "wait duration must be 1..3600")
    lock = (ROOT / "artifacts/i14-native-watch.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    out.mkdir(exist_ok=False, parents=True)
    (out / "pid").write_text(str(os.getpid()) + "\n")
    runner = Runner(out)
    manifest = json.loads((PACK / "manifest.json").read_text())
    reader = manifest["descriptor_reader"]
    require(reader["local"] == str(DESCRIPTOR_READER.relative_to(ROOT)), "descriptor reader path")
    reader_data = DESCRIPTOR_READER.read_bytes()
    require(hashlib.sha256(reader_data).hexdigest() == reader["sha256"], "descriptor reader pin")
    with (out / "read-i14-usb-descriptor.py").open("xb") as file:
        file.write(reader_data)
    (out / "descriptor-reader.json").write_text(json.dumps(reader) + "\n")
    assets = {}
    for a in manifest["assets"]:
        name = Path(a["local"]).name
        if name in PINS:
            data = (ROOT / a["local"]).read_bytes()
            require(hashlib.sha256(data).hexdigest() == PINS[name], "local pin " + name)
            assets[name] = data
    require(set(assets) == set(PINS), "missing asset")
    runner.log("WAIT max%d seconds; one guarded attempt; no flash/reboot" % seconds)
    deadline = time.monotonic() + seconds
    if wait_ready(deadline):
        (out / "attempt.json").write_text(json.dumps({"mode": "source-reviewed-mode-init-hypothesis-pilot-once", "unix_time": time.time()}))
        try:
            runner.execute(assets)
            return 0
        except Exception as error:
            runner.log("STOP " + repr(error) + "; no automatic retry")
            return 2
    runner.log("TIMEOUT; no attempt")
    return 3


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve(), int(sys.argv[2])))
