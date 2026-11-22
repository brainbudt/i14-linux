#!/usr/bin/env python3
"""Differential test: the shell functions of initramfs/i14charge against i14-charge-wait.

charge_wait: side A sources initramfs/i14charge in sh with the sleep stub of
tests/test_i14_autoboot.py (moves a fake /proc/uptime, runs a hook), the real log() of
/i14init with its three files redirected into the test directory and a fixed date.
Side B runs the installed wrapper from initramfs/i14charge with the
binary's test clock replaying the same timeline. Compared: rc, $why, $cap, $vol, the
final uptime, the three log files, every file under ps/ and the flag file.

Intended difference: AB_MAX_S (initramfs/i14charge, default 28800 s = 8 h) ends charging
mode in rescue mode (rc 1, why "charge time limit") in i14-charge-wait only; the shell
charge_wait has no overall bound and is left as it was. Every compared scenario ends (no
charger) long before the limit, which charge_diff asserts, so the comparison is unaffected;
limit_check runs the Rust side alone through charge_wait_rs with a short AB_MAX_S.
Second intended difference, so that bound (and BADREAD_S) cannot fail open: where the shell
counts an unreadable or non-numeric /proc/uptime as 0, i14-charge-wait ends in rescue mode
(rc 1, why "uptime unreadable (...)"), as it does when the uptime goes backwards (why
"uptime went backwards (OLD -> NEW)"). Compared scenarios never touch the uptime file;
limit_check covers this Rust side alone too.

pwrkey_watch: the shell function (paths moved into the test directory, -c -> -p so FIFOs
stand in for evdev nodes) against `i14-charge-wait --pwrkey-watch` with I14_TEST_FIFO:
the chosen device, the log line, and which written events create $PWRKEY.

usage: differential.py BIN [--shell sh] [--n N] [--seed S]
"""
import argparse
import os
import pathlib
import random
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[4]
LIB = REPO / "initramfs/i14charge"
INIT = (REPO / "initramfs/i14init").read_text()
DATE = "2026-11-07 12:00:00"

# log() exactly as /i14init has it, with /dev/kmsg and /i14/i14init.log moved into $D.
_LOG = INIT[INIT.index("log() {"):INIT.index("\n}\n", INIT.index("log() {")) + 3]
LOG_FUNC = _LOG.replace("/dev/kmsg", '"$D/kmsg"').replace("/i14/i14init.log", '"$D/ramlog"')
assert LOG_FUNC != _LOG and "$DIR/i14init.log" in LOG_FUNC

PROPOSED = "charge_wait_rs"
AB_MAX_S = int(LIB.read_text().split("\nAB_MAX_S=", 1)[1].split()[0])  # the default of i14charge

FIELDS = {"capacity": "ps/battery/capacity", "batt_vol": "ps/battery/batt_vol",
          "batt_temp": "ps/battery/batt_temp", "status": "ps/battery/status",
          "usb": "ps/usb/online", "ac": "ps/ac/online"}
PRES = ["", "AB_PCT=101; ", "AB_MIN_S=30 AB_HOLD_S=20; ", "AB_PCT=101; AB_MIN_S=30 AB_HOLD_S=20; "]
CAPS = ["0", "3", "14", "15", "16", "20", "50", "99", "100", "007"]
VOLS = ["3400", "3500", "3599", "3600", "3650", "3799", "3800", "3900", "4350"]
TEMPS = ["-50", "-1", "0", "1", "250", "449", "450", "451", "600", "-0"]
STATUSES = ["Charging", "Charging", "Full", "Discharging", "Not charging", "Unknown"]
BAD = ["", "3.9V", "-", "abc", "--5", "+5", "1e3", None]  # None: file removed
BAD_REALCAT = BAD + [" 20", "20 ", "1 2", "\t5"]


def num(v):
    d = v[1:] if v.startswith("-") else v
    return d != "" and all(c in "0123456789" for c in d)


def seen(v, realcat):
    """What $(cat F) gives for a file written as V + newline (None: missing)."""
    if v is None:
        return ""
    return v if realcat else v.strip(" \t")


class Scenario:
    def __init__(self, name, pre="", init=None, events=(), realcat=False, logdir=True):
        self.name, self.pre, self.realcat, self.logdir = name, pre, realcat, logdir
        self.init = dict(capacity="20", batt_vol="3900", batt_temp="250", status="Charging", usb="1", ac="0")
        self.init.update(init or {})
        self.events = sorted(events, key=lambda e: e[0])  # (t, field|'pwrkey', value|None)
        end = max([e[0] for e in self.events] + [10]) + 50
        self.events.append((end, "usb", "0"))  # every scenario ends: no charger at the end
        self.events.append((end, "ac", "0"))

    def readable_at(self, t):
        state = dict(self.init)
        for at, f, v in self.events:
            if at <= t and f != "pwrkey":
                state[f] = v
        vals = {k: seen(v, self.realcat) for k, v in state.items()}
        return num(vals["capacity"]) and num(vals["batt_vol"]) and num(vals["batt_temp"]) and \
            num(vals["usb"]) and num(vals["ac"]) and vals["status"] != ""

    def safe(self):
        """The shell spins without sleeping (fake time stands still) on a key press while
        values are unreadable; such scenarios are left to the Rust tests."""
        return all(self.readable_at(t) for t, f, _ in self.events if f == "pwrkey")

    def hook(self):
        lines = []
        for t, f, v in self.events:
            if f == "pwrkey":
                lines.append(f'[ $t = {t} ] && : >"$D/pwrkey"')
            elif v is None:
                lines.append(f'[ $t = {t} ] && rm -f "$D/{FIELDS[f]}"')
            else:
                assert "'" not in v and "\n" not in v and "\\" not in v
                lines.append(f"[ $t = {t} ] && printf '%s\\n' '{v}' >\"$D/{FIELDS[f]}\"")
        return "\n".join(lines) + "\n"

    def timeline(self):
        out = []
        for t, f, v in self.events:
            if f == "pwrkey":
                out.append(f"{t} touch pwrkey")
            elif v is None:
                out.append(f"{t} rm {FIELDS[f]}")
            else:
                out.append(f"{t} set {FIELDS[f]} {v}")
        return "\n".join(out) + "\n"


def setup(d, sc, bin_path):
    for sub in ("ps/battery", "ps/usb", "ps/ac", "bin"):
        (d / sub).mkdir(parents=True)
    if sc.logdir:
        (d / "dir").mkdir()
    for k, v in sc.init.items():
        if v is not None:
            (d / FIELDS[k]).write_text(v + "\n")
    (d / "uptime").write_text("10.00 5.00\n")
    (d / "hook").write_text(sc.hook())
    (d / "timeline").write_text(sc.timeline())
    os.symlink(bin_path, d / "bin/i14-charge-wait")


def snapshot(d, stdout, rc):
    files = {}
    for rel in ["uptime", "kmsg", "ramlog", "dir/i14init.log", "pwrkey"] + list(FIELDS.values()):
        p = d / rel
        files[rel] = p.read_bytes() if p.exists() else None
    return dict(rc=rc, stdout=stdout, files=files)


def run_side(sc, side, shell, bin_path):
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        setup(d, sc, bin_path)
        env = dict(os.environ, PATH=f"{d}/bin:/usr/bin:/bin", D=str(d), FAKE_DATE=DATE,
                   I14_PS=str(d / "ps"), I14_UPTIME=str(d / "uptime"), I14_PWRKEY=str(d / "pwrkey"))
        report = "printf 'rc=%s\\nwhy=%s\\ncap=%s\\nvol=%s\\n' \"$rc\" \"$why\" \"$cap\" \"$vol\"\n"
        if side == "shell":
            fakes = ('DIR=$D/dir\n' + LOG_FUNC +
                     'date() { echo "$FAKE_DATE"; }\n'
                     'sleep() { read -r u _ <"$D/uptime"; t=$((${u%.*} + $1)); echo "$t.00 1.00" >"$D/uptime"; '
                     '. "$D/hook"; }\n')
            if not sc.realcat:
                fakes += 'cat() { v=; read -r v <"$1" || [ -n "$v" ]; echo "$v"; }\n'
            script = f'{fakes}. "{LIB}"\n{sc.pre}\ncharge_wait; rc=$?\n{report}'
        else:
            env.update(I14_KMSG=str(d / "kmsg"), I14_RAMLOG=str(d / "ramlog"), I14_DIR=str(d / "dir"),
                       I14_TEST_ROOT=str(d), I14_TEST_TIMELINE=str(d / "timeline"), I14_TEST_DATE=DATE)
            script = f'. "{LIB}"\n{sc.pre}\n{PROPOSED}\nrc=$?\n{report}'
        try:
            r = subprocess.run([shell, "-c", script], env=env, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return dict(rc="timeout")
        return snapshot(d, r.stdout, r.returncode)


def scenarios(n, seed):
    rnd = random.Random(seed)
    out = []
    # Named families from the brief.
    for pre in PRES:
        ramp = [(10 + 30 * i, "capacity", str(5 + i)) for i in range(1, 40)]
        ramp += [(10 + 45 * i, "batt_vol", str(3600 + 20 * i)) for i in range(1, 20)]
        out.append(Scenario(f"ramp {pre}", pre, dict(capacity="5", batt_vol="3600"), ramp))
        out.append(Scenario(f"vol never 3800 {pre}", pre, dict(batt_vol="3799"), [(1200, "pwrkey", None)]))
        for k in (1, 3, 5, 7, 20, 60):
            flap = [(10 + k * i, "capacity", "14" if i % 2 else "15") for i in range(1, 900 // k)]
            out.append(Scenario(f"cap flap {k}s {pre}", pre, {}, flap))
    for n_s in list(range(1, 75, 4)) + [55, 59, 60, 61, 64, 65]:
        for field, bad in (("capacity", ""), ("status", ""), ("batt_vol", "3.9V"), ("usb", None)):
            start = rnd.randrange(11, 400)
            out.append(Scenario(f"unreadable {field}={bad!r} {n_s}s", "", {},
                                [(start, field, bad), (start + n_s, field, Scenario("x").init[field])]))
    for n_s in range(1, 42, 2):
        for which in ("usb", "both"):
            start = rnd.randrange(11, 500)
            ev = [(start, "usb", "0"), (start + n_s, "usb", "1")]
            init = {"ac": "1"} if which == "usb" else {}
            out.append(Scenario(f"unplug {which} {n_s}s", rnd.choice(PRES), init, ev))
    for t in list(range(11, 70, 3)) + [100, 299, 300, 305, 421, 430, 900]:
        for vol in ("3599", "3600", "3650", "3900"):
            out.append(Scenario(f"key at {t} vol={vol}", rnd.choice(PRES), dict(batt_vol=vol), [(t, "pwrkey", None)]))
    # Random scenarios.
    while len(out) < n:
        realcat = rnd.random() < 0.2
        init = dict(capacity=rnd.choice(CAPS), batt_vol=rnd.choice(VOLS), batt_temp=rnd.choice(TEMPS),
                    status=rnd.choice(STATUSES), usb=rnd.choice("1110"), ac=rnd.choice("0001"))
        bad_pool = BAD_REALCAT if realcat else BAD
        if rnd.random() < 0.1:
            init[rnd.choice(list(FIELDS))] = rnd.choice(bad_pool)
        events = []
        for _ in range(rnd.randrange(0, 14)):
            t = rnd.randrange(11, 1300)
            kind = rnd.random()
            if kind < 0.15:
                events.append((t, "pwrkey", None))
            elif kind < 0.35:
                f = rnd.choice(list(FIELDS))
                events.append((t, f, rnd.choice(bad_pool)))
                events.append((t + rnd.randrange(1, 90), f, Scenario("x").init[f]))
            elif kind < 0.5:
                events.append((t, rnd.choice(["usb", "ac"]), rnd.choice("01")))
            elif kind < 0.6:
                events.append((t, "status", rnd.choice(STATUSES)))
            elif kind < 0.75:
                events.append((t, "capacity", rnd.choice(CAPS)))
            elif kind < 0.9:
                events.append((t, "batt_vol", rnd.choice(VOLS)))
            else:
                events.append((t, "batt_temp", rnd.choice(TEMPS)))
        sc = Scenario(f"random {len(out)}", rnd.choice(PRES), init, events, realcat, rnd.random() < 0.85)
        out.append(sc)
    return out


def charge_diff(bin_path, shell, n, seed):
    total = skipped = failed = 0
    rcs = {}
    for sc in scenarios(n, seed):
        # the shell has no overall bound: compare only what ends before the Rust one
        assert sc.events[-1][0] - 10 < AB_MAX_S and "AB_MAX_S" not in sc.pre, sc.name
        if not sc.safe():
            skipped += 1
            continue
        total += 1
        a = run_side(sc, "shell", shell, bin_path)
        b = run_side(sc, "rust", shell, bin_path)
        key = (a.get("stdout") or b"").split(b"\n")[0].decode() + (" " + (a.get("stdout") or b"").split(b"\n")[1].decode().split(" ")[0] if a.get("stdout") else "")
        rcs[key] = rcs.get(key, 0) + 1
        if a != b:
            failed += 1
            if failed <= 5:
                print(f"MISMATCH {sc.name} pre={sc.pre!r} init={sc.init} events={sc.events}")
                for k in ("rc", "stdout"):
                    if a.get(k) != b.get(k):
                        print(f"  {k}: shell={a.get(k)!r}\n  {' ' * len(k)}  rust ={b.get(k)!r}")
                for k in (a.get("files") or {}):
                    if a["files"][k] != (b.get("files") or {}).get(k):
                        print(f"  {k}: shell={a['files'][k]!r}\n  {' ' * len(k)}  rust ={b['files'].get(k)!r}")
    print(f"charge_wait [{shell}]: {total} scenarios, {failed} mismatches, {skipped} skipped (key while "
          f"unreadable), shell rc counts {dict(sorted(rcs.items(), key=str))}")
    return failed == 0


def limit_check(bin_path, shell):
    """Rust only (the intended difference): start gates never met -> rescue at AB_MAX_S, through
    charge_wait_rs; the same values with the default limit, cut off by unplugging, power off."""
    ok = True
    never = dict(capacity="14")
    cases = [
        (Scenario("limit 600", "AB_MAX_S=600; ", never, [(2000, "pwrkey", None)]),
         b"rc=1\nwhy=charge time limit\ncap=14\nvol=3900\n", b"610.00 1.00\n"),
        (Scenario("limit 600, no autoboot", "AB_PCT=101; AB_MAX_S=600; ", dict(capacity="100", status="Full"),
                  [(2000, "pwrkey", None)]), b"rc=1\nwhy=charge time limit\ncap=100\nvol=3900\n", b"610.00 1.00\n"),
        (Scenario("default limit", "", never, [(1000, "batt_temp", "251")]),
         b"rc=2\nwhy=no charger for 30s\ncap=14\nvol=3900\n", b"1075.00 1.00\n"),
        (Scenario("uptime unreadable", "", never, [(2000, "pwrkey", None)]),
         b"rc=1\nwhy=uptime unreadable (not a number)\ncap=14\nvol=3900\n", b"x\n"),
    ]
    # the clock breaks at the sample of 100 s (the timeline line is Rust-only, not a FIELD)
    clock_sc = cases[-1][0]
    clock_sc.timeline = lambda base=clock_sc.timeline: base() + "100 set uptime x\n"
    for sc, want, uptime in cases:
        b = run_side(sc, "rust", shell, bin_path)
        got = (b.get("stdout"), (b.get("files") or {}).get("uptime"))
        if got != (want, uptime):
            ok = False
            print(f"LIMIT MISMATCH {sc.name}: got {got!r}, want {(want, uptime)!r}")
    print(f"charge time limit [{shell}]: {len(cases)} Rust-only checks, {'ok' if ok else 'FAILED'}")
    return ok


# ---------------------------------------------------------------- pwrkey_watch

_PK = LIB.read_text()
PK_FUNC = _PK[_PK.index("pwrkey_watch() {"):_PK.index("\n}\n", _PK.index("pwrkey_watch() {")) + 3]
PK_FUNC_T = (PK_FUNC.replace("/sys/class/input/event*", '"$INPUT"/event*')
             .replace("/dev/input/${e##*/} /dev/${e##*/}", '"$DEVD"/input/${e##*/} "$DEVD"/${e##*/}')
             .replace('[ -c "$n" ]', '[ -p "$n" ]'))
assert PK_FUNC_T.count("$DEVD") == 2 and "-p" in PK_FUNC_T and '"$INPUT"' in PK_FUNC_T


def event(etype, code, value):
    return struct.pack("<qqHHi", 1762516800, 123456, etype, code, value)  # arm64/host 64-bit layout


def start_watch(d, side, shell, bin_path):
    env = dict(os.environ, PATH="/usr/bin:/bin", D=str(d), INPUT=str(d / "input"), DEVD=str(d / "dev"),
               I14_PWRKEY=str(d / "pwrkey"), I14_INPUT=str(d / "input"), I14_DEV=str(d / "dev"),
               I14_TEST_FIFO="1", I14_RAMLOG=str(d / "log"), I14_KMSG=str(d / "kmsg"), I14_DIR=str(d / "nodir"),
               PWRKEY=str(d / "pwrkey"))
    if side == "shell":
        script = 'log() { echo "$*" >>"$D/log"; }\nPWRKEY=$I14_PWRKEY\n' + PK_FUNC_T + "pwrkey_watch\n"
        argv = [shell, "-c", script]
    else:
        argv = [bin_path, "--pwrkey-watch"]
    return subprocess.Popen(argv, env=env, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def stop(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.wait()


def make_layout(d, layout):
    """layout: [(N, name|None, nodes)] with nodes a subset of {'input', 'dev', 'reg'}."""
    (d / "input").mkdir()
    (d / "dev/input").mkdir(parents=True)
    fifos = []
    for n, name, nodes in layout:
        e = d / "input" / f"event{n}" / "device"
        e.mkdir(parents=True)
        if name is not None:
            (e / "name").write_text(name)
        for where in nodes:
            p = d / "dev" / ("input/" if where == "input" else "") / f"event{n}"
            if where == "reg":
                p = d / "dev" / f"event{n}"
                if not p.exists():
                    p.write_text("not a device")
                continue
            if not p.exists():
                os.mkfifo(p)
                fifos.append(p)
    return fifos


def chosen(p, fifos, timeout=4.0):
    """Which FIFO the watcher opened (only a FIFO with a reader opens for writing without blocking),
    or 'exit rc=N' when it gave up."""
    end = time.time() + timeout
    while time.time() < end:
        for f in fifos:
            try:
                fd = os.open(f, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                continue
            os.close(fd)
            return str(f).rsplit("/dev/", 1)[1]
        if p.poll() is not None:
            return f"exit rc={p.returncode}"
        time.sleep(0.02)
    return "none"


def selection_case(layout, side, shell, bin_path):
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        fifos = make_layout(d, layout)
        p = start_watch(d, side, shell, bin_path)
        try:
            got = chosen(p, fifos)
        finally:
            stop(p)
        log = (d / "log").read_text() if (d / "log").exists() else ""
        return got, log


EVENTS = [("press", event(1, 116, 1), True), ("release", event(1, 116, 0), False),
          ("repeat", event(1, 116, 2), False), ("other key", event(1, 114, 1), False),
          ("syn", event(0, 0, 0), False), ("abs", event(3, 116, 1), False),
          ("press again", event(1, 116, 1), True), ("8-byte tail", event(1, 116, 1)[16:], True),
          ("16-byte head", event(1, 116, 1)[:16], False), ("msc then key", event(4, 4, 116) + event(1, 116, 1), None)]


def events_case(side, shell, bin_path):
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (fifo,) = make_layout(d, [(0, "mtk-kpd\n", ["input"])])
        p = start_watch(d, side, shell, bin_path)
        flag = d / "pwrkey"
        seen = []
        try:
            w = os.open(fifo, os.O_WRONLY)  # blocks until the watcher opened it
            for name, data, _ in EVENTS:
                flag.unlink(missing_ok=True)
                os.write(w, data)
                end = time.time() + 1.0
                while time.time() < end and not flag.exists():
                    time.sleep(0.01)
                seen.append((name, flag.exists()))
                time.sleep(0.2)  # let it settle (shell: dd/od/tr for the next read)
            os.close(w)  # EOF: both wait a second and open again
            flag.unlink(missing_ok=True)
            w = os.open(fifo, os.O_WRONLY)
            os.write(w, event(1, 116, 1))
            end = time.time() + 3.0
            while time.time() < end and not flag.exists():
                time.sleep(0.01)
            seen.append(("press after reopen", flag.exists()))
            os.close(w)
        finally:
            stop(p)
        return seen


def pwrkey_diff(bin_path, shell, n, seed):
    rnd = random.Random(seed)
    names = ["mtk-kpd\n", "mtk-kpd", "mtk-kpd\n\n", "mtk-kpd ", "ACCDET\n", "mtk-tpd\n", None]
    layouts = [[], [(0, "ACCDET\n", ["input"])], [(0, "mtk-kpd\n", [])], [(0, "mtk-kpd\n", ["reg"])],
               [(0, "mtk-kpd\n", ["input"]), (1, "ACCDET\n", ["dev"]), (10, "mtk-kpd\n", ["input", "dev"]),
                (2, "mtk-kpd\n", ["input"])]]
    while len(layouts) < n:
        k = rnd.randrange(1, 6)
        nums = rnd.sample(range(0, 13), k)
        layouts.append([(i, rnd.choice(names), rnd.sample(["input", "dev", "reg"], rnd.randrange(0, 3)))
                        for i in nums])
    fails = 0
    for lay in layouts:
        a = selection_case(lay, "shell", shell, bin_path)
        b = selection_case(lay, "rust", shell, bin_path)
        if a != b:
            fails += 1
            print(f"MISMATCH pwrkey layout {lay}: shell={a} rust={b}")
    print(f"pwrkey_watch device selection [{shell}]: {len(layouts)} layouts, {fails} mismatches")
    a = events_case("shell", shell, bin_path)
    b = events_case("rust", shell, bin_path)
    ev_fails = 0
    for (name, x), (_, y), (_, _, want) in zip(a, b, EVENTS + [("", b"", True)]):
        status = "ok" if x == y and (want is None or x == want) else "MISMATCH"
        if status != "ok":
            ev_fails += 1
        print(f"  event {name:18} shell={x!s:5} rust={y!s:5} {status}")
    print(f"pwrkey_watch events [{shell}]: {len(a)} checks, {ev_fails} mismatches")
    return fails == 0 and ev_fails == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bin")
    ap.add_argument("--shell", default="/bin/dash")
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--seed", type=int, default=20261107)
    ap.add_argument("--pk-layouts", type=int, default=40)
    ap.add_argument("--only", choices=["charge", "pwrkey"])
    a = ap.parse_args()
    ok = True
    if a.only in (None, "charge"):
        ok &= charge_diff(os.path.abspath(a.bin), a.shell, a.n, a.seed)
        ok &= limit_check(os.path.abspath(a.bin), a.shell)
    if a.only in (None, "pwrkey"):
        ok &= pwrkey_diff(os.path.abspath(a.bin), a.shell, a.pk_layouts, a.seed)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
