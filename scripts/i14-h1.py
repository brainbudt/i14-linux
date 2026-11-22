#!/usr/bin/env python3
"""H1 measurement payload. Importing is inert; execution is Linux-phone-only.

No service selection, deployment, screen writes, secret output or recovery logic.
The execution owner owns service stop/start and restoration in the H1 runbook.
"""
import argparse
import fcntl
import hashlib
import json
import math
import mmap
import os
from pathlib import Path
import platform
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback
import zlib

DATA = Path("/mnt/data/media/0/i14-linux")
LIGHT = Path("/sys/class/leds/lcd-backlight/brightness")
PERMIT = Path("/run/i14-h1-permit.json")
TOUCH = "/run/i14-touch"
UI = {"python": "/usr/sbin/i14-ui", "rust": "/usr/libexec/i14-rs/i14-ui"}


def parse_stat(text):
    head, rest = text.rsplit(")", 1)
    fields = rest.split()  # starts at field 3; comm may contain spaces and ')'
    return dict(pid=int(head.split("(", 1)[0]), ppid=int(fields[1]),
                ticks=int(fields[11]) + int(fields[12]), start_ticks=int(fields[19]))


def uptime():
    return float(Path("/proc/uptime").read_text().split()[0])


def snapshot(pid):
    root = Path("/proc") / str(pid)
    value = parse_stat((root / "stat").read_text())
    value["uptime"] = uptime()
    rss = re.search(r"^VmRSS:\s+(\d+) kB$", (root / "status").read_text(), re.M)
    if rss is None:
        raise ValueError("RSS unavailable")
    value["rss_kib"] = int(rss[1])
    return value


def same_process(before, after):
    if (before["pid"], before["start_ticks"]) != (after["pid"], after["start_ticks"]):
        raise ValueError("process exited or PID was reused")
    if after["ticks"] < before["ticks"]:
        raise ValueError("CPU counter went backwards")


def idle_result(before, after, hz):
    same_process(before, after)
    elapsed = after["uptime"] - before["uptime"]
    if elapsed < 60 or hz <= 0:
        raise ValueError("need a complete 60 second interval and measured CLK_TCK")
    ticks = after["ticks"] - before["ticks"]
    return dict(before=before, after=after, elapsed_s=elapsed, clk_tck=hz,
                cpu_ticks=ticks, cpu_percent_one_core=ticks / hz / elapsed * 100,
                rss_after_kib=after["rss_kib"], children_included=False)


def idle_wait(read_light, clock=time.monotonic, sleep=time.sleep):
    end = clock() + 60
    while True:
        if read_light() != 0:
            raise ValueError("screen is not off during idle sample")
        left = end - clock()
        if left <= 0:
            return
        sleep(min(1, left))


def observe_change(read_crc, trigger, clock=time.monotonic, sleep=time.sleep):
    baseline = read_crc()
    start = clock()
    trigger()  # Must only write the request; never wait for its ACK here.
    previous = start
    while True:
        value = read_crc()
        observed = clock()  # actual completion of this framebuffer observation
        if observed - start > 3:
            return dict(start_s=start, end_s=observed, observed_s=None,
                        previous_s=previous, latency_ms=None, timeout=True)
        if value != baseline:
            return dict(start_s=start, end_s=observed, observed_s=observed,
                        previous_s=previous, latency_ms=(observed - start) * 1000,
                        timeout=False)
        previous = observed
        sleep(0.002)


def observe_start(launch, read_light, read_uptime=uptime,
                  clock=time.monotonic, sleep=time.sleep):
    if read_light() != 0:
        raise ValueError("startup requires backlight zero before launch")
    start_up, start = read_uptime(), clock()
    launch()
    previous_up = start_up
    while True:
        light = read_light()
        observed_up, observed = read_uptime(), clock()
        if observed - start > 10:
            return dict(start_uptime_s=start_up, on_uptime_s=None,
                        elapsed_uptime_s=None, timeout=True)
        if light > 0:
            return dict(start_uptime_s=start_up, on_uptime_s=observed_up,
                        previous_off_uptime_s=previous_up,
                        elapsed_uptime_s=observed_up - start_up, timeout=False)
        previous_up = observed_up
        sleep(0.020)


def check_permit(permit, boot, now, blockers):
    if blockers:
        raise ValueError("another experiment owns the phone")
    expiry = permit.get("expires_uptime_s")
    if (permit.get("boot_id") != boot or type(expiry) not in (int, float)
            or not math.isfinite(expiry) or not now < expiry <= now + 900):
        raise ValueError("missing, stale or wrong-boot H1 owner permit")
    for key, expected in (("e_closed", True), ("r_closed", True),
                          ("drain_active", False), ("soak_active", False)):
        if permit.get(key) is not expected:
            raise ValueError("H1 owner permit does not close all experiment gates")


def preflight(no_ui=False):
    if (platform.system(), platform.machine(), platform.release(), os.geteuid()) != (
            "Linux", "aarch64", "3.18.19", 0):
        raise ValueError("H1 execution requires root on the recorded I14 stock kernel")
    info = PERMIT.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("H1 owner permit must be a root-owned regular file, mode 0600")
    permit = json.loads(PERMIT.read_text())
    blockers = []
    if os.path.lexists("/run/i14-drain-request"):
        blockers.append("drain request")
    marker = DATA / "rootfs-trial"
    if marker.exists() or marker.is_symlink():
        raw = marker.read_bytes()
        if (not raw.startswith(b"rollback ") or
                permit.get("accepted_rollback_marker_sha256") != hashlib.sha256(raw).hexdigest()):
            blockers.append("active or unreconciled rootfs trial")
    lock = Path("/run/i14-battery-calibration.lock")
    if lock.exists():
        with lock.open("r") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                blockers.append("calibration lock held")
    guard = json.loads(Path("/run/i14-charge-status.json").read_text())
    if guard.get("drain") is not False:
        blockers.append("drain status unknown or active")
    for path in Path("/proc").iterdir():
        if path.name.isdecimal() and int(path.name) != os.getpid():
            try:
                cmd = (path / "cmdline").read_bytes()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if any(token in cmd for token in (b"i14-battery-calibrate", b"battery-calibration-code-",
                                              b"i14-drain-", b"rootfs-trial")):
                blockers.append("experiment process or calibration wrapper")
            if no_ui and any(executable.encode() in cmd.split(b"\0") for executable in UI.values()):
                blockers.append("existing UI controller")
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    check_permit(permit, boot, uptime(), blockers)
    return boot


class Framebuffer:
    def __enter__(self):
        self.fd = os.open("/dev/fb0", os.O_RDONLY | os.O_CLOEXEC)
        try:
            fixed, variable = bytearray(80), bytearray(160)
            fcntl.ioctl(self.fd, 0x4602, fixed, True)
            fcntl.ioctl(self.fd, 0x4600, variable, True)
            x, y, vx, vy, ox, oy, bpp = struct.unpack_from("=7I", variable)
            self.stride = struct.unpack_from("=I", fixed, 48)[0]
            if (x, y, ox, oy, bpp) != (720, 1600, 0, 0, 32) or self.stride != 2944:
                raise ValueError("framebuffer layout differs from the historical I14 layout")
            self.memory = mmap.mmap(self.fd, self.stride * 80, access=mmap.ACCESS_READ)
            return self
        except BaseException:
            os.close(self.fd)
            raise

    def crc(self):
        return zlib.crc32(self.memory)

    def __exit__(self, *args):
        self.memory.close()
        os.close(self.fd)


def save_result(path, value):
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def light():
    return int(LIGHT.read_text())


def taps(pid, settings, back):
    # Existing helper keeps its PIN gate, hold/settle, rate limits and cleanup.
    subprocess.run([TOUCH, "pin-gate"], check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=5)
    before = snapshot(pid)
    rows = []
    with Framebuffer() as fb:
        for index, xy in enumerate([settings, back] * 10):
            preflight()
            same_process(before, snapshot(pid))
            if light() <= 0:
                raise ValueError("tap measurement requires screen on")
            row = dict(index=index + 1, action="settings" if index % 2 == 0 else "back",
                       ack_ok=False, timeout=False, observed_s=None, latency_ms=None)
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as transport:
                    transport.settimeout(5)
                    transport.connect("/run/i14-touch.sock")
                    row.update(observe_change(fb.crc, lambda: transport.sendall(f"tap {xy[0]} {xy[1]}\n".encode())))
                    reply = b""
                    while not reply.endswith(b"\n") and len(reply) < 256:
                        chunk = transport.recv(256)
                        if not chunk:
                            break
                        reply += chunk
                    row["ack_observed_s"] = time.monotonic()
                    row["ack_ok"] = reply.strip() == b"ok"
            except OSError as error:
                row["transport_error"] = type(error).__name__
            rows.append(row)
            if not row["ack_ok"] or row["timeout"]:
                break  # Lost UI navigation state must not cause further blind taps.
            time.sleep(0.30)
    same_process(before, snapshot(pid))
    return dict(rows=rows, requested=20,
                completed=sum(r["ack_ok"] and not r["timeout"] for r in rows), poll_s=0.002,
                timeout_s=3, rows_observed=[0, 79], hold_s=0.12, settle_s=0.10,
                historical_helper_equivalence="UNPROVEN")


def startup_binding(child,implementation):
    root=Path('/proc')/str(child.pid)
    info=parse_stat((root/'stat').read_text())
    argv=[v.decode() for v in (root/'cmdline').read_bytes().split(b"\0") if v]
    exe=os.readlink(root/'exe')
    expected=UI[implementation] if implementation=='rust' else os.path.realpath('/usr/bin/python3')
    if info['pid']!=child.pid or info['ppid']!=os.getpid() or exe!=expected or UI[implementation] not in argv:
        raise ValueError('measured startup child binding differs')
    return dict(pid=child.pid,ppid=info['ppid'],start_ticks=info['start_ticks'],argv=argv,
                exe=exe,entrypoint=UI[implementation],
                entrypoint_sha256=hashlib.sha256(Path(UI[implementation]).read_bytes()).hexdigest())


def startup(implementation):
    child = None
    def launch():
        nonlocal child
        child = subprocess.Popen([UI[implementation]], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
    try:
        result = observe_start(launch, light)
        result["child_exited_early"] = child.poll() is not None
        result["child_pid"] = child.pid
        result["binding_valid"] = False
        if not result["child_exited_early"]:
            try:
                result["child_binding"] = startup_binding(child,implementation)
                result["binding_valid"] = True
            except (OSError,ValueError) as error:
                result["binding_error"] = type(error).__name__
                report_failure(error)
        return result
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


def terminal_child(pid):
    if not os.isatty(1):
        raise ValueError("terminal-child must run inside the UI terminal")
    ancestor, seen = os.getpid(), set()
    while ancestor not in (0, 1, pid) and ancestor not in seen:
        seen.add(ancestor)
        ancestor = parse_stat(Path(f"/proc/{ancestor}/stat").read_text())["ppid"]
    if ancestor != pid:
        raise ValueError("UI process must be an ancestor; an SSH terminal is not the UI workload")
    before, start = snapshot(pid), time.monotonic()
    subprocess.run(["seq", "1", "20000"], check=True, timeout=10)
    end, after = time.monotonic(), snapshot(pid)
    same_process(before, after)
    return dict(wall_s=end - start, cpu_ticks=after["ticks"] - before["ticks"],
                clk_tck=os.sysconf("SC_CLK_TCK"), before=before, after=after,
                completion="seq exited after PTY writes; UI pixel completion UNKNOWN",
                historical_comparable=False, method_amendment_required=True)


def report_failure(error, stream=sys.stderr):
    # Own source locations identify the failing guard without leaking exception data.
    frames = [dict(function=frame.f_code.co_name, line=line)
              for frame, line in traceback.walk_tb(error.__traceback__)
              if frame.f_code.co_filename == __file__]
    print(json.dumps(dict(exception=type(error).__name__, frames=frames)), file=stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("idle", "taps", "startup", "terminal-child"))
    parser.add_argument("--impl", choices=("python", "rust"), required=True)
    parser.add_argument("--service", choices=("ui", "power", "cpufreq", "netwatch"), default="ui")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--settings", type=int, nargs=2, metavar=("X", "Y"))
    parser.add_argument("--back", type=int, nargs=2, metavar=("X", "Y"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.mode != "startup" and (args.pid is None or args.pid <= 1):
        parser.error("--pid must identify the verified measured process")
    if args.mode == "taps" and (args.settings is None or args.back is None):
        parser.error("taps needs verified --settings X Y and --back X Y")
    for xy in (args.settings, args.back):
        if xy and not (0 <= xy[0] < 720 and 0 <= xy[1] < 1600):
            parser.error("coordinates outside the recorded display")
    if args.out.parent != Path("/run") or not re.fullmatch(r"h1-[a-z0-9-]+\.json", args.out.name):
        parser.error("--out must be a fresh /run/h1-<lowercase-label>.json path")
    boot = preflight(no_ui=args.mode == "startup")
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    result = dict(schema=1, mode=args.mode, implementation=args.impl, service=args.service,
                  boot_id=boot, captured_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if args.out.exists():
        raise FileExistsError("result path already exists")
    if args.mode == "idle":
        before = snapshot(args.pid)
        def checked_light():
            preflight()
            return light()
        idle_wait(checked_light)
        result["measurement"] = idle_result(before, snapshot(args.pid), os.sysconf("SC_CLK_TCK"))
    elif args.mode == "taps":
        result["measurement"] = taps(args.pid, args.settings, args.back)
    elif args.mode == "startup":
        result["measurement"] = startup(args.impl)
    else:
        result["measurement"] = terminal_child(args.pid)
    save_result(args.out, result)
    measure = result["measurement"]
    bad = measure.get("timeout", False) or measure.get("child_exited_early", False) or measure.get("binding_valid", True) is False
    bad = bad or any(r["timeout"] or not r["ack_ok"] for r in measure.get("rows", []))
    print("H1 result saved; terminal method remains proposed" if args.mode == "terminal-child" else "H1 result saved")
    return int(bad)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Never print process command lines, config values, ACK bodies or pixels.
        print(f"H1 refused or incomplete ({type(exc).__name__})", file=sys.stderr)
        report_failure(exc)
        sys.exit(2)
    except KeyboardInterrupt:
        print("H1 interrupted; owner must restore services", file=sys.stderr)
        sys.exit(130)
