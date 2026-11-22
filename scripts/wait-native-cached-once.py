"""Bounded wait, then at most ONE cached read-only check of the native I14.

Usage: wait-native-cached-once.py NEW_OUTPUT_DIR SECONDS [--power-evidence]

Never loads modules, starts helpers, writes sysfs/configfs, takes a fresh
battery sample or reads charger_state (those do PMIC I/O).  A transport
failure is terminal: there is no retry.  Charging stays BLOCKED/UNKNOWN.
"""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "artifacts/i14-native-watch.lock"
TRANSPORT = ROOT / "evidence/2026-11-01/r3-run-181615/tn.py"
TRANSPORT_SHA = "5e1aa9e36d72eadb0aeaca606867d3bfda8fbcc54e34c7eebbd1e142c5813f90"
BOUND_TRANSPORT_SHA = "d7d496ffbc80f0588f8d8fab73e9663f1eb8bf4c47058d3944f0d29d192484e8"
CONNECT_OLD = b"socket.create_connection(('10.77.14.2',23),timeout=10)"
CONNECT_NEW = b"socket.create_connection(('10.77.14.2',23),timeout=10,source_address=('10.77.14.1',0))"
SOURCE_IP = "10.77.14.1"
TARGET = {"idVendor": 0x1d6b, "idProduct": 0x0104, "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}
POLL_SECONDS = 2
RAW_NAME = "cached-check.txt"
COMMAND_NAME = "cached-check.command"
KERNEL_LOG_NAME = "kernel-ringbuffer.raw"
KERNEL_LOG_CAP = 262144

KERNEL = "7.0.10-i14-bringup"
MODEL = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a"
COMPAT = "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a"
CAPACITY_SECTORS = 30535680
RO_NAMES = {"mmcblk0", "mmcblk0boot0", "mmcblk0boot1"} | {"mmcblk0p%d" % i for i in range(1, 24)}
PAYLOAD_SECTIONS = ["boot_id", "uptime", "modules", "mmcblk0_size", "mmcblk_ro", "mmcblk0_stat", "mounts",
                    "cached_i14_charger", "cached_battery_sample", "maxpower", "udc_state", "usb0_carrier"]

# One line.  `r` reads an optional node or reports ABSENT/UNREADABLE; nothing is written.
COMMAND = (
    'r(){ if [ -e "$1" ]; then cat "$1" 2>&1 || echo I14_UNREADABLE; else echo I14_ABSENT; fi; }; '
    's(){ echo "@@I14_BEGIN $1"; }; e(){ echo "@@I14_END $1"; }; '
    "s identity; uname -r; uname -m; grep '^Uid:' /proc/self/status; "
    "sha256sum /proc/device-tree/model; sha256sum /proc/device-tree/compatible; e identity; "
    'if [ "$(uname -r)" = ' + KERNEL + ' ] && [ "$(uname -m)" = aarch64 ] '
    "&& grep -qE '^Uid:[[:space:]]+0[[:space:]]+0[[:space:]]+0[[:space:]]+0$' /proc/self/status "
    '&& [ "$(sha256sum /proc/device-tree/model)" = "' + MODEL + '  /proc/device-tree/model" ] '
    '&& [ "$(sha256sum /proc/device-tree/compatible)" = "' + COMPAT + '  /proc/device-tree/compatible" ]; '
    "then echo I14_IDENTITY_OK; "
    "s boot_id; cat /proc/sys/kernel/random/boot_id; e boot_id; "
    "s uptime; cat /proc/uptime; e uptime; "
    "s modules; cat /proc/modules; e modules; "
    "s mmcblk0_size; cat /sys/class/block/mmcblk0/size; e mmcblk0_size; "
    's mmcblk_ro; for p in /sys/class/block/mmcblk*; do echo "${p##*/} $(cat "$p/ro")"; done; e mmcblk_ro; '
    "s mmcblk0_stat; cat /sys/class/block/mmcblk0/stat; e mmcblk0_stat; "
    "s mounts; cat /proc/mounts; e mounts; "
    "s cached_i14_charger; r /sys/devices/i14-charger/state; e cached_i14_charger; "
    "s cached_battery_sample; r /sys/devices/i14-reboot-target/battery_sample; e cached_battery_sample; "
    "s maxpower; r /sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower; e maxpower; "
    "s udc_state; r /sys/class/udc/musb-hdrc.3.auto/state; e udc_state; "
    "s usb0_carrier; r /sys/class/net/usb0/carrier; e usb0_carrier; "
    "echo I14_CACHED_CAPTURE_DONE; else echo I14_STOP_IDENTITY; fi"
)

POWER_PAYLOAD_SECTIONS = ["boot_id", "uptime", "kernel_log", "cmdline", "mounts", "chosen_bootargs",
                          "chosen_bootargs_override", "usb_startup_log", "storage_startup_log", "usb_snapshot_log"] \
    + [name for name in PAYLOAD_SECTIONS[2:] if name != "mounts"] + ["boot_id_end", "uptime_end"]

# This BusyBox has no head -c, dd or base64. dmesg's own read buffer bounds the
# kernel stream; its explicit status must survive even when the read fails.
POWER_COMMAND = COMMAND[:COMMAND.index("s boot_id;")] + (
    "s boot_id; cat /proc/sys/kernel/random/boot_id; e boot_id; "
    "s uptime; cat /proc/uptime; e uptime; "
    "s kernel_log; if [ -x /bin/dmesg ]; then dmesg -r -s 262145 2>&1; "
    "printf '\\n@@I14_DMESG_RC %s\\n' \"$?\"; else echo I14_ABSENT; fi; e kernel_log; "
    "s cmdline; cat /proc/cmdline; e cmdline; "
    "s mounts; cat /proc/mounts; e mounts; "
    "s chosen_bootargs; r /proc/device-tree/chosen/bootargs; echo; e chosen_bootargs; "
    "s chosen_bootargs_override; r /proc/device-tree/chosen/linux,cmdline-override; echo; e chosen_bootargs_override; "
    'lr(){ if [ -e "$1" ]; then head -n 257 "$1" 2>&1 || echo I14_UNREADABLE; else echo I14_ABSENT; fi; }; '
    "s usb_startup_log; lr /run/usb-network.log; e usb_startup_log; "
    "s storage_startup_log; lr /run/storage-readonly.log; e storage_startup_log; "
    "s usb_snapshot_log; lr /run/usb-snapshot.log; e usb_snapshot_log; "
) + COMMAND[COMMAND.index("s modules;"):COMMAND.index("s mounts;")] + (
    COMMAND[COMMAND.index("s cached_i14_charger;"):COMMAND.index("echo I14_CACHED_CAPTURE_DONE;")]
) + (
    "s boot_id_end; cat /proc/sys/kernel/random/boot_id; e boot_id_end; "
    "s uptime_end; cat /proc/uptime; e uptime_end; "
    "echo I14_CACHED_CAPTURE_DONE; else echo I14_STOP_IDENTITY; fi"
)

HISTORICAL_NOTE = ("Cached kernel values from earlier operations in this boot, if any. They are historical: "
                   "no current voltage, SOC, charging state or physical cutoff may be derived from them.")


class Stopped(Exception):
    """Raised by the SIGTERM handler so receipts are closed before exit."""


class IntegrityError(RuntimeError):
    """Pinned source mismatch; this is never a fallback opportunity."""


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# ---------- hardware / network boundaries (mocked in tests) ----------

def records(tree):
    if isinstance(tree, list):
        for child in tree:
            yield from records(child)
    elif isinstance(tree, dict):
        if "idVendor" in tree:
            yield {k: tree.get(k) for k in TARGET}
        yield from records(tree.get("IORegistryEntryChildren", []))


def scan():
    result = subprocess.run(["ioreg", "-p", "IOUSB", "-a", "-l"], capture_output=True, check=True, timeout=5)
    return list(records(plistlib.loads(result.stdout)))


def source_ready():
    """Local bind only; no packet is sent and no TCP probe is made."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((SOURCE_IP, 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_transport(out):
    result = subprocess.run([sys.executable, "-I", str(out / "transport.py"), str(out / COMMAND_NAME),
                             str(out / RAW_NAME), "10"], capture_output=True, timeout=20)
    (out / "transport-output.txt").write_bytes(result.stdout + result.stderr)
    return result.returncode


# ---------- decisions ----------

def classify(devices):
    related = [d for d in devices if d.get("USB Serial Number") == TARGET["USB Serial Number"]
               or (d.get("idVendor"), d.get("idProduct")) == (TARGET["idVendor"], TARGET["idProduct"])]
    if not related:
        return "absent"
    return "exact" if related == [TARGET] else "mismatch"


def sections_of(lines):
    """Return {name: [lines]} or None when framing is broken/duplicated."""
    found, current, body = {}, None, []
    for line in lines:
        if line.startswith("@@I14_BEGIN "):
            if current is not None:
                return None
            current, body = line[len("@@I14_BEGIN "):], []
        elif line.startswith("@@I14_END "):
            if current is None or line[len("@@I14_END "):] != current or current in found:
                return None
            found[current], current = body, None
        elif current is not None:
            body.append(line)
    return None if current is not None else found


def optional(body):
    if body == ["I14_ABSENT"]:
        return {"status": "ABSENT", "raw": body}
    if not body or body[-1] == "I14_UNREADABLE":
        return {"status": "UNKNOWN", "raw": body}
    return {"status": "PRESENT", "raw": body}


def usb_value(body):
    item = optional(body)
    if item["status"] == "PRESENT" and len(body) == 1:
        return body[0]
    return item["status"]


def blocked(result):
    result.update({"charging_blocked": True, "charging_status": "UNKNOWN", "ready_for_charging": False})
    return result


def kernel_log_of(raw):
    """Keep received log bytes opaque; framing/status never depend on decoding."""
    frames = re.findall(rb"(?:^|\n)@@I14_BEGIN kernel_log\r?\n(.*?)@@I14_END kernel_log\r?(?:\n|$)", raw, re.S)
    if len(frames) != 1:
        return None, b""
    body = frames[0]
    if body in (b"I14_ABSENT\n", b"I14_ABSENT\r\n"):
        status, rc, data = "ABSENT", None, b""
    else:
        match = re.fullmatch(rb"(.*?)\r?\n@@I14_DMESG_RC ([0-9]+)\r?\n", body, re.S)
        if not match:
            return None, b""
        data, rc = match[1], int(match[2])
        status = "PRESENT" if rc == 0 else "UNREADABLE"
    kept = data[:KERNEL_LOG_CAP]
    return {"status": status, "read_rc": rc, "byte_cap": KERNEL_LOG_CAP,
            "captured_bytes": len(kept), "received_bytes": len(data), "truncated": len(data) > KERNEL_LOG_CAP,
            "raw_file": KERNEL_LOG_NAME, "sha256": sha256(kept),
            "byte_scope": "received transport bytes; CRLF and dmesg's possible final newline are retained"}, kept


def evaluate(raw, transport_rc, power_evidence=False):
    lines = [l.rstrip("\r") for l in raw.decode(errors="replace").split("\n")]
    count = lines.count
    result = blocked({"transport_rc": transport_rc, "capture_complete": False, "identity": None, "boot": None,
                      "storage": None, "usb": None, "cached_diagnostics": None, "stop_reasons": [],
                      "framing": {k: count(k) for k in ("I14_IDENTITY_OK", "I14_STOP_IDENTITY",
                                                        "I14_CACHED_CAPTURE_DONE", "I14_CHECK_END")}})

    def stop(verdict, code, reason):
        result["stop_reasons"].append(reason)
        result.update({"verdict": verdict, "exit_code": code})
        return result

    found = sections_of(lines)
    frame = result["framing"]
    if frame["I14_IDENTITY_OK"] and frame["I14_STOP_IDENTITY"]:
        return stop("STOP_CONTRADICTION", 6, "device printed both identity OK and STOP")
    if frame["I14_STOP_IDENTITY"] == 1 and frame["I14_CHECK_END"] == 1 and found is not None \
            and list(found) == ["identity"]:
        result["identity"] = {"ok": False, "device_lines": found["identity"]}
        return stop("STOP_IDENTITY", 6, "device identity gate refused; no payload")
    payload_sections = POWER_PAYLOAD_SECTIONS if power_evidence else PAYLOAD_SECTIONS
    order = ["identity"] + payload_sections
    complete = (transport_rc == 0 and found is not None and list(found) == order
                and all(frame[k] == 1 for k in ("I14_IDENTITY_OK", "I14_CACHED_CAPTURE_DONE", "I14_CHECK_END"))
                and lines.index("@@I14_END identity") < lines.index("I14_IDENTITY_OK")
                < lines.index("@@I14_BEGIN boot_id")
                and lines.index("@@I14_END " + payload_sections[-1]) < lines.index("I14_CACHED_CAPTURE_DONE")
                < lines.index("I14_CHECK_END"))
    if not complete:
        return stop("STOP_INCOMPLETE", 2, "missing/duplicate framing, sections or transport rc != 0")
    result["capture_complete"] = True

    ident = found["identity"]
    expected = [KERNEL, "aarch64", None, MODEL + "  /proc/device-tree/model", COMPAT + "  /proc/device-tree/compatible"]
    ok = (len(ident) == 5 and all(e is None or a == e for a, e in zip(ident, expected))
          and re.fullmatch(r"Uid:\s+0\s+0\s+0\s+0", ident[2]) is not None)
    result["identity"] = {"ok": ok, "kernel": ident[0] if ident else None, "device_lines": ident}
    if not ok:
        return stop("STOP_CONTRADICTION", 6, "device said identity OK but host check of identity lines failed")

    boot_id = found["boot_id"][0] if len(found["boot_id"]) == 1 else None
    uptime = found["uptime"][0].split() if len(found["uptime"]) == 1 else []
    result["boot"] = {"boot_id": boot_id, "uptime": found["uptime"], "modules": found["modules"]}
    size = found["mmcblk0_size"]
    capacity = int(size[0]) if len(size) == 1 and size[0].isdigit() else None
    ro = {}
    for line in found["mmcblk_ro"]:
        name, _, value = line.partition(" ")
        ro[name] = value if name not in ro else None
    stat = found["mmcblk0_stat"][0].split() if len(found["mmcblk0_stat"]) == 1 else []
    result["storage"] = {
        "capacity_sectors": capacity, "capacity_ok": capacity == CAPACITY_SECTORS,
        "ro_node_count": len(found["mmcblk_ro"]), "ro_nodes": ro,
        "names_ok": set(ro) == RO_NAMES and len(found["mmcblk_ro"]) == len(RO_NAMES),
        "all_ro": bool(ro) and all(v == "1" for v in ro.values()),
        "stat": found["mmcblk0_stat"],
        "sectors_written": int(stat[6]) if len(stat) >= 11 and stat[6].isdigit() else None,
        "mounts": found["mounts"],
    }
    result["usb"] = {k: usb_value(found[k]) for k in ("maxpower", "udc_state", "usb0_carrier")}
    state = optional(found["cached_i14_charger"])
    if state["status"] == "PRESENT":
        phase = re.search(r"(?:^| )phase=(\S+)", state["raw"][0])
        latched_ns = re.search(r"(?:^| )latched_ns=(\d+)", " ".join(state["raw"]))
        state["phase"] = phase.group(1) if phase else "UNKNOWN"
        state["latched"] = state["phase"] == "latched-no-more-io" or bool(latched_ns and int(latched_ns.group(1)))
    result["cached_diagnostics"] = {"historical_only": True, "note": HISTORICAL_NOTE,
                                    "charger_state": state, "battery_sample": optional(found["cached_battery_sample"])}

    storage = result["storage"]
    problems = [why for bad, why in (
        (not (boot_id and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot_id)), "boot_id"),
        (len(uptime) != 2 or not all(re.fullmatch(r"\d+\.\d+", u) for u in uptime), "uptime"),
        (not storage["capacity_ok"], "mmcblk0 capacity"),
        (not storage["names_ok"], "block node set"),
        (not storage["all_ro"], "block node read-only"),
        (storage["sectors_written"] is None, "block stat"),
        (not found["mounts"], "mounts"),
    ) if bad]
    if problems:
        return stop("STOP_BASE_DATA", 6, "mandatory base data failed: " + ", ".join(problems))
    if power_evidence:
        kernel_log, _ = kernel_log_of(raw)
        if kernel_log is None:
            result["capture_complete"] = False
            return stop("STOP_INCOMPLETE", 2, "kernel log framing or explicit read status missing")
        end_id = found["boot_id_end"][0] if len(found["boot_id_end"]) == 1 else None
        end_uptime = found["uptime_end"][0].split() if len(found["uptime_end"]) == 1 else []
        result["boot"].update({"end_boot_id": end_id, "end_uptime": found["uptime_end"], "same_boot": end_id == boot_id})
        pstore_mounts = [line for line in found["mounts"] if len(line.split()) >= 3 and line.split()[2] == "pstore"]
        result["power_evidence"] = {
            "kernel_log": kernel_log,
            "cmdline": optional(found["cmdline"]),
            "chosen": {name: optional(found[name][:-1] if found[name] and found[name][-1] == "" else found[name])
                       for name in ("chosen_bootargs", "chosen_bootargs_override")},
            "startup_logs": {name: {**optional(found[name]), "line_cap": 256, "truncated": len(found[name]) > 256,
                                    "raw": found[name][:256]}
                             for name in ("usb_startup_log", "storage_startup_log", "usb_snapshot_log")},
            "pstore": {"status": "UNKNOWN" if pstore_mounts else "ABSENT", "mounts": pstore_mounts,
                       "records_read": False, "reason": "backend binding not qualified; dmesg-only fallback"},
        }
        if end_id != boot_id:
            result["capture_complete"] = False
            return stop("STOP_BOOT_CHANGED", 6, "begin/end boot IDs differ or end boot ID missing")
        if len(end_uptime) != 2 or not all(re.fullmatch(r"\d+\.\d+", u) for u in end_uptime) \
                or float(end_uptime[0]) < float(uptime[0]):
            result["capture_complete"] = False
            return stop("STOP_BASE_DATA", 6, "end uptime missing, invalid or regressed")
        result.update({"verdict": "COMPLETE_POWER_EVIDENCE", "exit_code": 0})
        return result
    result.update({"verdict": "COMPLETE_CACHED_BASELINE", "exit_code": 0})
    return result


# ---------- receipts ----------

def write_durable(path, data, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_json(path, obj, exclusive=False):
    write_durable(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode(), exclusive)


def log(out, milestone, **fields):
    line = " ".join([utc(), "I14WATCH", milestone] + ["%s=%s" % kv for kv in fields.items()])
    with (out / "watch.log").open("a") as file:
        file.write(line + "\n")
        file.flush()
        os.fsync(file.fileno())
    print(line, flush=True)


def finish(out, result):
    result.setdefault("attempted", (out / "attempt.json").exists())
    write_json(out / "result.json", blocked(result))
    log(out, "RESULT", verdict=result["verdict"])
    return result["exit_code"]


def preflight(out, power_evidence=False):
    data = TRANSPORT.read_bytes()
    if sha256(data) != TRANSPORT_SHA or data.count(CONNECT_OLD) != 1:
        raise IntegrityError("transport pin mismatch; no command sent")
    data = data.replace(CONNECT_OLD, CONNECT_NEW)
    if sha256(data) != BOUND_TRANSPORT_SHA:
        raise IntegrityError("bound transport pin mismatch; no command sent")
    write_durable(out / "transport.py", data)
    command = POWER_COMMAND if power_evidence else COMMAND
    write_durable(out / COMMAND_NAME, (command + "\n").encode())


def attempt_once(out, devices, power_evidence=False):
    command = POWER_COMMAND if power_evidence else COMMAND
    mode = "power-evidence-read-only-once" if power_evidence else "cached-read-only-once"
    write_json(out / "attempt.json", {"utc": utc(), "pid": os.getpid(), "mode": mode,
                                      "usb": [dict(TARGET)], "source_address": SOURCE_IP,
                                      "command_sha256": sha256((command + "\n").encode()),
                                      "bound_transport_sha256": BOUND_TRANSPORT_SHA,
                                      "terminal": "this attempt is never retried"}, exclusive=True)
    log(out, "ATTEMPT_RESERVED")
    transport_error = None
    try:
        rc = run_transport(out)
    except BaseException as error:  # Stopped, timeout, OSError: terminal, never retried
        log(out, "TRANSPORT_RETURNED", rc="none", error=type(error).__name__)
        rc, transport_error = None, "transport raised %r" % (error,)
    else:
        log(out, "TRANSPORT_RETURNED", rc=rc)
    raw = (out / RAW_NAME).read_bytes() if (out / RAW_NAME).exists() else b""
    if power_evidence:
        kernel_log, log_bytes = kernel_log_of(raw)
        if kernel_log is not None:
            write_durable(out / KERNEL_LOG_NAME, log_bytes)
    result = evaluate(raw, rc, power_evidence)
    if transport_error:
        result["stop_reasons"].append(transport_error)
    return finish(out, result)


def no_command(out, verdict, code, reason, failure_kind=None):
    result = {"verdict": verdict, "exit_code": code, "attempted": False,
              "capture_complete": False, "stop_reasons": [reason]}
    if failure_kind:
        result["failure_kind"] = failure_kind
    return finish(out, result)


def watch(out, seconds, power_evidence=False):
    deadline = time.monotonic() + seconds
    seen = waited = False
    while True:
        devices = scan()
        state = classify(devices)
        if state == "mismatch":
            log(out, "STOP_MISMATCH")
            return no_command(out, "STOP_USB_MISMATCH", 4, "related USB device(s) not exactly the one target")
        if state == "exact":
            if not seen:
                log(out, "TARGET_EXACT")
                seen = True
            if source_ready():
                return attempt_once(out, devices, power_evidence)
            if not waited:
                log(out, "WAIT_SOURCE", address=SOURCE_IP)
                waited = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log(out, "TIMEOUT")
            return no_command(out, "TIMEOUT_NO_COMMAND", 3, "deadline reached; no phone command sent")
        time.sleep(min(POLL_SECONDS, remaining))


def on_term(signum, frame):
    raise Stopped("signal %d" % signum)


def main(argv):
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--power-evidence") \
            or not re.fullmatch(r"\d{1,4}", argv[1]) or not 1 <= int(argv[1]) <= 3600:
        print("usage: wait-native-cached-once.py NEW_OUTPUT_DIR SECONDS(1..3600) [--power-evidence]", file=sys.stderr)
        return 5
    out, seconds = Path(argv[0]).resolve(), int(argv[1])
    power_evidence = len(argv) == 3
    command = POWER_COMMAND if power_evidence else COMMAND
    mode = "power-evidence-read-only-once" if power_evidence else "cached-read-only-once"
    try:
        lock_fd = os.open(LOCK, os.O_RDWR)  # existing shared lock only; never created here
    except FileNotFoundError:
        print("REFUSED: lock file %s missing" % LOCK, file=sys.stderr)
        return 5
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("REFUSED: %s held by another process" % LOCK, file=sys.stderr)
            return 5
        if os.path.lexists(out):
            print("REFUSED: output %s already exists" % out, file=sys.stderr)
            return 5
        out.mkdir(parents=True)
        write_durable(out / "pid", ("%d\n" % os.getpid()).encode())
        log(out, "START", pid=os.getpid(), seconds=seconds)
        try:
            preflight(out, power_evidence)
            write_json(out / "intent.json", {"utc": utc(), "pid": os.getpid(), "seconds": seconds,
                                             "mode": mode, "target": TARGET,
                                             "script_sha256": sha256(Path(__file__).read_bytes()),
                                             "transport_sha256": TRANSPORT_SHA,
                                             "bound_transport_sha256": BOUND_TRANSPORT_SHA,
                                             "command_sha256": sha256((command + "\n").encode()),
                                             "charging_blocked": True}, exclusive=True)
            log(out, "PREFLIGHT_OK")
            code = watch(out, seconds, power_evidence)
        except BaseException as error:
            log(out, "STOP_SIGNAL" if isinstance(error, Stopped) else "STOP_ERROR", error=type(error).__name__)
            code = 2
            if not (out / "result.json").exists():
                code = no_command(out, "STOP_ERROR", 2, repr(error),
                                  "integrity" if isinstance(error, IntegrityError) else None) if not (out / "attempt.json").exists() \
                    else finish(out, evaluate(b"", None, power_evidence))
        verdict = json.loads((out / "result.json").read_text()).get("verdict")
        log(out, "END", exit=code, verdict=verdict)
        return code
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_term)
    sys.exit(main(sys.argv[1:]))
