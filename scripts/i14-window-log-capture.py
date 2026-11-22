"""OFFLINE PREP: independent one-shot, bounded, read-only kernel-log capture.

Not in the active supervisor allowlist. Explicit --capture-once is required;
the fixed output directory is itself a permanent one-shot reservation. Never
remove/reuse it to recover an uncertain session. No reconnect or device stop
command is sent. Closing the socket is only a host stop, not a device receipt.
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
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "artifacts/i14-native-watch.lock"
OUT = ROOT / "artifacts/i14-window-log-capture-once"
TARGET = {"idVendor": 0x1d6b, "idProduct": 0x0104, "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}
MODEL = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a"
COMPAT = "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a"
KERNEL = "7.0.10-i14-bringup"
BYTE_CAP, SECONDS = 8388608, 245
STOP_REQUESTED = False
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"

# BusyBox has no dmesg-follow or timeout applet. 240 tail snapshots are
# bounded by count and read buffer; the host independently limits time/bytes.
COMMAND = (
    'if [ "$(uname -r)" = ' + KERNEL + ' ] && [ "$(uname -m)" = aarch64 ] '
    "&& grep -qE '^Uid:[[:space:]]+0[[:space:]]+0[[:space:]]+0[[:space:]]+0$' /proc/self/status "
    '&& [ "$(sha256sum /proc/device-tree/model)" = "' + MODEL + '  /proc/device-tree/model" ] '
    '&& [ "$(sha256sum /proc/device-tree/compatible)" = "' + COMPAT + '  /proc/device-tree/compatible" ]; '
    'then b=$(cat /proc/sys/kernel/random/boot_id); '
    'if printf "%s\\n" "$b" | grep -qE "^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$"; then '
    'printf "\\n@@I14_LOG_BEGIN ' + KERNEL + ' aarch64 ' + MODEL + ' ' + COMPAT + ' %s\\n" "$b"; '
    'i=0; while [ "$i" -lt 240 ]; do '
    'if [ "$(cat /proc/sys/kernel/random/boot_id)" != "$b" ]; then echo @@I14_LOG_STOP_BOOT; break; fi; '
    'printf "@@I14_LOG_SNAPSHOT %s\\n" "$i"; dmesg -r -s 16384 2>&1; r=$?; '
    'e=$(cat /proc/sys/kernel/random/boot_id); printf "\\n@@I14_LOG_STATUS %s %s %s\\n" "$i" "$r" "$e"; '
    '[ "$r" = 0 ] && [ "$e" = "$b" ] || break; i=$((i+1)); '
    '[ "$i" -ge 240 ] || sleep 1; done; '
    '[ "$i" != 240 ] || printf "@@I14_LOG_DONE %s\\n" "$b"; '
    'else echo @@I14_LOG_STOP_BOOT; fi; else echo @@I14_LOG_STOP_IDENTITY; fi\n'
).encode("ascii")


class ProtocolError(ValueError):
    pass


class Stopped(Exception):
    pass


def check_stop():
    if STOP_REQUESTED:
        raise Stopped("host stop")


def write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write made no progress")
        view = view[written:]
    os.fsync(fd)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        write_all(fd, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())
    finally:
        os.close(fd)
    sync_dir(path.parent)


def records(tree):
    if isinstance(tree, list):
        for item in tree:
            yield from records(item)
    elif isinstance(tree, dict):
        if "idVendor" in tree:
            yield {key: tree.get(key) for key in TARGET}
        yield from records(tree.get("IORegistryEntryChildren", []))


def scan():
    result = subprocess.run(["ioreg", "-p", "IOUSB", "-a", "-l"], capture_output=True, check=True, timeout=5)
    return list(records(plistlib.loads(result.stdout)))


def source_ready():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("10.77.14.1", 0))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class Telnet:
    """Decode controls across recv boundaries; retain exact wire bytes separately."""
    def __init__(self):
        self.state, self.option = "data", None

    def feed(self, data):
        clean, replies = bytearray(), []
        for byte in data:
            if self.state == "data":
                if byte == 255:
                    self.state = "iac"
                else:
                    clean.append(byte)
            elif self.state == "iac":
                if byte == 255:
                    clean.append(byte)
                    self.state = "data"
                elif byte in (251, 252, 253, 254):
                    self.option, self.state = byte, "option"
                else:
                    self.state = "sub" if byte == 250 else "data"
            elif self.state == "option":
                if self.option in (251, 253):
                    replies.append(bytes((255, 254 if self.option == 251 else 252, byte)))
                self.state = "data"
            elif self.state == "sub":
                if byte == 255:
                    self.state = "sub_iac"
            elif self.state == "sub_iac":
                self.state = "data" if byte == 240 else "sub"
        return bytes(clean), replies


class Parser:
    def __init__(self):
        self.pending, self.boot, self.current = b"", None, None
        self.completed, self.done, self.read_failed = 0, False, False

    def feed(self, data):
        self.pending += data
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            if line.startswith(b"@@I14_LOG_BEGIN "):
                values = line.decode("ascii", errors="replace").split()
                if self.boot is not None or len(values) != 6 or values[1:5] != [KERNEL, "aarch64", MODEL, COMPAT] \
                        or not re.fullmatch(UUID, values[5]):
                    raise ProtocolError("identity or begin boot invalid")
                self.boot = values[5]
            elif line.startswith(b"@@I14_LOG_STOP"):
                raise ProtocolError("device identity/boot guard refused")
            elif line.startswith(b"@@I14_LOG_SNAPSHOT "):
                if self.boot is None or self.current is not None or self.done \
                        or line != ("@@I14_LOG_SNAPSHOT %d" % self.completed).encode() or self.completed >= 240:
                    raise ProtocolError("snapshot framing/order invalid")
                self.current = self.completed
            elif line.startswith(b"@@I14_LOG_STATUS "):
                values = line.decode("ascii", errors="replace").split()
                if self.current is None or len(values) != 4 or values[1] != str(self.current) \
                        or not re.fullmatch(r"[0-9]{1,3}", values[2]) or values[3] != self.boot:
                    raise ProtocolError("snapshot status or end boot invalid")
                self.current = None
                if values[2] != "0":
                    self.read_failed = True
                    return
                self.completed += 1
            elif line.startswith(b"@@I14_LOG_DONE "):
                if self.current is not None or self.completed != 240 or self.done \
                        or line != ("@@I14_LOG_DONE " + str(self.boot)).encode():
                    raise ProtocolError("premature/invalid completion")
                self.done = True
            elif line.startswith(b"@@I14_LOG_"):
                raise ProtocolError("unknown framing")


def capture(out, sock, deadline):
    parser, telnet, count = Parser(), Telnet(), 0
    fds = []
    try:
        for name in ("wire.raw", "stream.raw"):
            fds.append(os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
        sync_dir(out)
        check_stop()
        sock.sendall(COMMAND)
        while True:
            check_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                verdict = "PARTIAL_TIME_BOUND"
                break
            if count >= BYTE_CAP:
                verdict = "PARTIAL_BYTE_BOUND"
                break
            sock.settimeout(min(1, remaining))
            try:
                data = sock.recv(min(16384, BYTE_CAP - count))
            except socket.timeout:
                continue
            except OSError:
                verdict = "PARTIAL_TRANSPORT_LOSS"
                break
            if not data:
                verdict = "PARTIAL_EOF"
                break
            count += len(data)
            write_all(fds[0], data)
            clean, replies = telnet.feed(data)
            write_all(fds[1], clean)
            try:
                parser.feed(clean)
            except ProtocolError:
                verdict = "STOP_PROTOCOL"
                break
            # Finish persisting and parsing the received chunk before a host stop.
            check_stop()
            if parser.read_failed or parser.done:
                verdict = "STOP_DMESG" if parser.read_failed else "COMPLETE_BOUNDED_CAPTURE"
                break
            try:
                for reply in replies:
                    sock.sendall(reply)
            except OSError:
                verdict = "PARTIAL_TRANSPORT_LOSS"
                break
    except (Stopped, KeyboardInterrupt):
        verdict = "STOP_SIGNAL"
    finally:
        for fd in fds:
            os.close(fd)
    return {"verdict": verdict, "capture_complete": parser.done and verdict == "COMPLETE_BOUNDED_CAPTURE", "boot_id": parser.boot,
            "snapshots_complete": parser.completed, "incomplete_snapshot": parser.current,
            "wire_bytes": count, "telnet_state_at_end": telnet.state}


def run():
    try:
        lock = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return 5
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            return 5
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 5
        try:
            OUT.mkdir(mode=0o700)  # no parents, no reuse, including after a crash
        except FileExistsError:
            return 5
        sync_dir(OUT.parent)
        result = {"verdict": "STOP_ERROR", "capture_complete": False, "attempted": False,
                  "charging_blocked": True, "ready_for_charging": False, "boot_id": None}
        sock = None
        try:
            check_stop()
            devices = scan()
            related = [d for d in devices if d.get("USB Serial Number") == TARGET["USB Serial Number"]
                       or (d.get("idVendor"), d.get("idProduct")) == (TARGET["idVendor"], TARGET["idProduct"])]
            if related != [TARGET]:
                result["verdict"] = "NO_EXACT_USB"
            elif not source_ready():
                result["verdict"] = "NO_SOURCE"
            else:
                check_stop()
                write_json(OUT / "attempt.json", {"utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                           "pid": os.getpid(), "target": TARGET, "source_address": "10.77.14.1",
                           "command_sha256": hashlib.sha256(COMMAND).hexdigest(),
                           "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                           "wire_byte_cap": BYTE_CAP, "host_seconds": SECONDS,
                           "terminal": "never reconnect; never reuse this directory"})
                result["attempted"] = True
                check_stop()
                deadline = time.monotonic() + SECONDS
                sock = socket.create_connection(("10.77.14.2", 23), timeout=5,
                                                source_address=("10.77.14.1", 0))
                result.update(capture(OUT, sock, deadline))
        except BaseException as error:
            result.update(verdict="STOP_SIGNAL" if isinstance(error, (Stopped, KeyboardInterrupt)) else "STOP_ERROR",
                          capture_complete=False, error=type(error).__name__)
        finally:
            if sock is not None:
                sock.close()
        for name in ("stream.raw", "wire.raw"):
            path = OUT / name
            if path.exists():
                data = path.read_bytes()
                result[name.removesuffix(".raw") + "_sha256"] = hashlib.sha256(data).hexdigest()
                result[name.removesuffix(".raw") + "_bytes"] = len(data)
        write_json(OUT / "result.json", result)
        return 0 if result["capture_complete"] else 4 if not result["attempted"] and result["verdict"] != "STOP_ERROR" else 2
    finally:
        os.close(lock)


def on_term(signum, frame):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def main(argv):
    if argv != ["--capture-once"]:
        print("OFFLINE PREP; activation requires --capture-once and a separately reviewed epoch", file=sys.stderr)
        return 5
    return run()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    sys.exit(main(sys.argv[1:]))
