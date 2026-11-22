#!/usr/bin/env python3
"""One bounded H1 tap/terminal block; owner supplies the reviewed same-boot freeze.

No selector/settings changes. The selected UI is restarted after helper creation.
A fresh helper per block is a method amendment, with a real quiet minute before
any input; it never increases the unchanged 30/minute budget. No automatic retry.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

KEYS = [(683,1186),(252,1278),(468,1278),(504,1462),(683,1186),(454,1370),(666,1554)]
HELPER_SHA = "f56fbba4457116657f6d15adb857c795741dba58b987a99ddeb66e3369cd69e9"


def await_backlight_on(read_light, clock=time.monotonic, sleep=time.sleep):
    """A newly spawned UI's initial zero is not its later screen-off idle state."""
    deadline = clock() + 8
    while True:
        value = read_light()
        if clock() >= deadline:
            raise ValueError("fresh UI backlight did not become ready within 8s")
        if value > 0:
            return
        sleep(.02)


def quiet_restart(status, restart, clock=time.monotonic, sleep=time.sleep):
    first = status()
    if first["taps"] != 0 or first["age"] >= 20:
        raise ValueError("input block requires a newly started helper")
    begin = clock()
    sleep(60)
    quiet = clock() - begin
    if quiet < 60:
        raise ValueError("quiet minute incomplete")
    def checked():
        current = status()
        if (current["pid"] != first["pid"] or current["taps"] != 0
                or current["age"] >= 100):
            raise ValueError("helper changed, received input or has insufficient idle budget")
    checked()
    ui = restart()
    checked()
    return dict(quiet_s=quiet, ui=ui)


def await_shell(read_children, ui_pid, ui_start, shell_exe,
                clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + 3
    while True:
        children = read_children()
        if clock() >= deadline:
            raise ValueError("UI shell deadline expired; no keyboard input")
        if len(children) > 1:
            raise ValueError("ambiguous UI children; no keyboard input")
        if children:
            child = children[0]
            if (child["ppid"] != ui_pid or child["start_ticks"] < ui_start
                    or child["argv"] != ["/bin/sh", "-l"] or child["exe"] != shell_exe
                    or child["session"] != child["pid"] or child["tty_nr"] == 0):
                raise ValueError("UI child is not the expected controlling-PTY login shell")
            return child
        sleep(.05)


def terminal_entry(tap, read_children, ui_pid, ui_start, shell_exe, trace,
                   keys=None, clock=time.monotonic, sleep=time.sleep):
    trace["terminal_shell_proven"] = False
    tap(190, 985)
    def read():
        trace["terminal_children"] = read_children()
        return trace["terminal_children"]
    shell = await_shell(read, ui_pid, ui_start, shell_exe, clock, sleep)
    trace["terminal_shell"] = shell
    trace["terminal_shell_proven"] = True
    if keys is None:
        keys = lambda: [tap(x,y) for x,y in KEYS]
    keys()


def proc(pid):
    root = Path("/proc") / str(pid)
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, ppid=int(fields[1]), session=int(fields[3]), tty_nr=int(fields[4]),
                start_ticks=int(fields[19]), exe=os.readlink(root / "exe"),
                argv=[v.decode() for v in (root / "cmdline").read_bytes().split(b"\0") if v])


def processes():
    rows = []
    for p in Path("/proc").glob("[0-9]*/stat"):
        try:
            rows.append(proc(int(p.parent.name)))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return rows


def identify_ui(h1, impl):
    rows = processes()
    children = [p for p in rows if p["argv"] and Path(p["argv"][0]).name != "supervise-daemon"
                and any(v in p["argv"] for v in h1.UI.values())] if rows else []
    # Kernel threads have empty argv.
    if len(children) != 1:
        raise ValueError("need exactly one UI child")
    child = children[0]
    expected = h1.UI[impl] if impl == "rust" else os.path.realpath("/usr/bin/python3")
    parents = [p for p in rows if p["pid"] == child["ppid"]]
    if (h1.UI[impl] not in child["argv"] or child["exe"] != expected or len(parents) != 1
            or parents[0]["ppid"] != 1 or parents[0]["exe"] != "/sbin/supervise-daemon"
            or parents[0]["argv"][:2] != ["supervise-daemon", "i14-ui"]
            or h1.UI[impl] not in parents[0]["argv"]):
        raise ValueError("UI child/supervisor/implementation binding differs")
    return child


def terminal_children(ui_pid, trace):
    rows = [p for p in processes() if p["ppid"] == ui_pid]
    trace["terminal_children"] = rows
    for row in rows:
        links = [os.readlink(f'/proc/{row["pid"]}/fd/{n}') for n in (0,1,2)]
        row["stdio"] = links
        if len(set(links)) != 1 or not re.fullmatch(r"/dev/pts/[0-9]+", links[0]):
            raise ValueError("UI child stdio differs from a shared PTY slave")
    return rows


def run(args, h1, trace):
    freeze = json.loads(args.freeze.read_text())
    info = args.freeze.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("freeze must be root0600 regular file")
    if freeze["boot_id"] != h1.preflight():
        raise ValueError("wrong frozen boot")
    required = {"/run/i14-h1.py", "/run/i14-h1-input.py", h1.TOUCH,
                *h1.UI.values(), "/etc/conf.d/i14-ui"}
    required.update("/usr/lib/i14ui/" + name + ".py" for name in (
        "__init__", "app", "bcct", "bt", "charging", "config", "fb", "font",
        "inputdev", "term", "vt", "widgets", "wifi"))
    pins = freeze["sha256"]
    if not required <= pins.keys() or pins[h1.TOUCH] != HELPER_SHA:
        raise ValueError("incomplete source freeze")
    for path, digest in pins.items():
        if not (path in required or re.fullmatch(r"/usr/lib/i14ui/[a-z0-9_]+\.py", path)):
            raise ValueError("unexpected frozen path")
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError("frozen source mismatch")
    selector = Path("/etc/conf.d/i14-ui").read_bytes()
    if re.findall(rb'^i14_impl="(rust|python)"$', selector, re.M) != [args.impl.encode()]:
        raise ValueError("selected UI differs")
    trace.update(boot_id=freeze["boot_id"], implementation=args.impl, mode=args.mode, source_sha256=pins)
    initial = identify_ui(h1, args.impl)
    trace["initial_ui"] = initial
    owned_helper = False
    wrapper = None
    def command(argv, timeout=15, check=True):
        result = subprocess.run(argv, capture_output=True, timeout=timeout)
        # Fixed commands only: never terminal streams, settings or PIN values.
        trace.setdefault("commands", []).append(dict(argv=argv, rc=result.returncode,
                                                      stdout=result.stdout.decode(errors="replace"),
                                                      stderr=result.stderr.decode(errors="replace")))
        if check and result.returncode:
            raise ValueError("block command failed")
        return result
    def status():
        result = command([h1.TOUCH, "status"], timeout=5)
        match = re.fullmatch(rb"pong pid=(\d+) age=(\d+)s taps=(\d+)\n", result.stdout)
        if not match:
            raise ValueError("helper status malformed")
        return dict(zip(("pid", "age", "taps"), map(int, match.groups())))
    def restart():
        h1.preflight()
        command(["rc-service", "i14-ui", "stop"])
        if any(any(v in p["argv"] for v in h1.UI.values()) for p in processes()):
            raise ValueError("UI or supervisor survived stop")
        command(["rc-service", "i14-ui", "start"])
        deadline = time.monotonic() + 8
        while True:
            try:
                current = identify_ui(h1, args.impl)
                if current["start_ticks"] <= initial["start_ticks"] or h1.light() <= 0:
                    raise ValueError("fresh UI not ready")
                return current
            except ValueError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
    try:
        command([h1.TOUCH, "pin-gate"], timeout=5)
        old = command([h1.TOUCH, "status"], check=False, timeout=5)
        if old.returncode != 1 or old.stdout != b"not running\n":
            raise ValueError("another helper may own input")
        command([h1.TOUCH, "start", "--lifetime", "900", "--idle", "120"])
        owned_helper = True
        trace.update(quiet_restart(status, restart))
        ui = trace["ui"]
        def current():
            now = identify_ui(h1, args.impl)
            if now != ui:
                raise ValueError("UI identity changed")
            h1.preflight()
        devices = [p for p in Path("/sys/class/input").glob("event*")
                   if (p / "device/name").read_text().strip() == "I14 test tap injector"]
        if len(devices) != 1:
            raise ValueError("helper input device absent or ambiguous")
        node = Path("/dev/input") / devices[0].name
        rdev = node.stat().st_rdev
        fds = [str(p) for p in Path(f'/proc/{ui["pid"]}/fd').iterdir()
               if p.stat().st_rdev == rdev and stat.S_ISCHR(p.stat().st_mode)]
        trace["helper_input"] = dict(node=str(node), rdev=rdev, UI_fds=fds)
        if len(fds) != 1:
            raise ValueError("selected UI does not own helper input")
        current()
        if args.mode == "taps":
            trace["measurement"] = h1.taps(ui["pid"], [530,1185], [55,40])
            if trace["measurement"]["completed"] != 20:
                raise ValueError("tap block incomplete; no retry")
        else:
            with h1.Framebuffer():  # Same 720x1600/stride/offset gate as the tap method.
                pass
            target = Path("/run/h")
            if os.path.lexists(target) or os.path.lexists(args.terminal_out):
                raise ValueError("wrapper/result target occupied")
            if any(p["ppid"] == ui["pid"] for p in processes()):
                raise ValueError("fresh Home already has child processes")
            body = ("#!/bin/sh\nexec python3 -B /run/i14-h1.py terminal-child --impl " + args.impl
                    + " --pid " + str(ui["pid"]) + " --out " + str(args.terminal_out) + "\n").encode()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(fd, "wb") as stream:
                wrapper = os.fstat(stream.fileno())
                stream.write(body)
            def tap(x,y):
                current()
                command([h1.TOUCH, "tap", str(x), str(y)], timeout=5)
            def children():
                current()
                return terminal_children(ui["pid"], trace)
            terminal_entry(tap, children, ui["pid"], ui["start_ticks"], os.path.realpath("/bin/sh"), trace)
            deadline = time.monotonic() + 20
            while not args.terminal_out.exists():
                current()
                if time.monotonic() >= deadline:
                    raise ValueError("terminal result missing; no retry")
                time.sleep(.05)
            # The writer may still be serializing its exclusive result file.
            while any("/run/i14-h1.py" in p["argv"] for p in processes()):
                if time.monotonic() >= deadline:
                    raise ValueError("terminal writer did not finish")
                time.sleep(.05)
            trace["terminal_result"] = json.loads(args.terminal_out.read_text())
            result = trace["terminal_result"]
            if (result["mode"] != "terminal-child" or result["implementation"] != args.impl
                    or result["boot_id"] != freeze["boot_id"]
                    or any(result["measurement"][k]["pid"] != ui["pid"]
                           or result["measurement"][k]["start_ticks"] != ui["start_ticks"]
                           for k in ("before", "after"))):
                raise ValueError("terminal result identity differs")
            current()
    finally:
        if wrapper is not None:
            info = Path("/run/h").lstat()
            if (info.st_dev, info.st_ino) != (wrapper.st_dev, wrapper.st_ino) or Path("/run/h").read_bytes() != body:
                raise ValueError("owned wrapper changed; cleanup HOLD")
            Path("/run/h").unlink()
            trace["wrapper_removed"] = True
        if owned_helper:
            command([h1.TOUCH, "stop"])
            trace["helper_stopped"] = True
            if os.path.lexists("/run/i14-touch.sock") or any(
                    (p / "device/name").read_text().strip() == "I14 test tap injector"
                    for p in Path("/sys/class/input").glob("event*")):
                raise ValueError("helper cleanup incomplete; restoration HOLD")
            trace["helper_socket_and_device_absent"] = True
        # Service/selector restoration and full K belong to the execution owner.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("taps", "terminal"))
    parser.add_argument("--impl", choices=("rust", "python"), required=True)
    parser.add_argument("--freeze", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--terminal-out", type=Path)
    args = parser.parse_args()
    paths = [args.out] + ([args.terminal_out] if args.mode == "terminal" else [])
    if any(p is None or p.parent != Path("/run") or not re.fullmatch(r"h1-[a-z0-9-]+\.json", p.name)
           or os.path.lexists(p) for p in paths) or len(set(paths)) != len(paths):
        parser.error("fresh distinct /run/h1-<label>.json paths required")
    source = Path(__file__).with_name("i14-h1.py")
    spec = importlib.util.spec_from_file_location("h1", source)
    h1 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h1)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    trace = dict(schema=1, ok=False, historical_comparable=False,
                 completion="tap first top-bar CRC change / terminal seq exit after PTY writes; final pixels UNKNOWN")
    try:
        run(args, h1, trace)
        trace["ok"] = True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        trace["error"] = dict(type=type(error).__name__, detail=str(error))
    finally:
        h1.save_result(args.out, trace)
    return int(not trace["ok"])

if __name__ == "__main__":
    sys.exit(main())
