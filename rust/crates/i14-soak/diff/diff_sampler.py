#!/usr/bin/env python3
"""Differential test: scripts/i14-soak-sample (sh and dash) versus the Rust sampler.

Each case builds one fake device tree + stubs + pre-state, copies it into one
sandbox per implementation, runs, and compares log/log.1/crontab bytes, exit code
and stderr. Normalized: epoch/iso (checked separately), the order of the
"services"/"dns" objects (completion order in the shell), sandbox paths, and the
install line's "/bin/sh " prefix (the Rust line runs the binary directly).
"""
import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts/i14-soak-sample"
CRATE = Path(__file__).resolve().parents[1]
RUST = CRATE.parents[1] / "target/debug/i14-soak-sample"  # workspace target dir
SERVICES = ("i14-power", "i14-cpufreq", "i14-ui", "unbound", "i14-wifi", "i14-netwatch", "sshd")
PY = shutil.which("python3")
SHELLS = {"sh": ["/bin/sh"], "dash": ["/bin/dash"]}

FILES = {
    "boot": "proc/sys/kernel/random/boot_id", "uptime": "proc/uptime", "loadavg": "proc/loadavg",
    "meminfo": "proc/meminfo", "readback": "proc/mtk_battery_cmd/battery_cmd",
    "capacity": "sys/class/power_supply/battery/capacity", "status": "sys/class/power_supply/battery/status",
    "batt_vol": "sys/class/power_supply/battery/batt_vol", "batt_temp": "sys/class/power_supply/battery/batt_temp",
    "fg": "sys/devices/platform/battery_meter/FG_Current",
    "fgp": "sys/devices/platform/battery_meter/FG_g_fg_dbg_percentage_fg",
    "zcv": "sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_zcv",
    "cstatus": "run/i14-charge-status.json", "bcct": "proc/driver/thermal/clbcct",
    "type": "sys/devices/platform/battery/Charger_Type", "usb": "sys/class/power_supply/usb/online",
    "ac": "sys/class/power_supply/ac/online", "light": "sys/class/leds/lcd-backlight/brightness",
    "freq": "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
    "cap": "proc/cpufreq/cpufreq_limited_max_freq_by_user", "fix": "proc/cpufreq/cpufreq_fix_freq_in_es",
    "blog": "var/log/i14-battery.log",
}
NUMERIC = ["capacity", "batt_vol", "batt_temp", "fg", "fgp", "zcv", "type", "usb", "ac", "light", "freq", "cap", "fix"]
STRINGS = ["boot", "status", "cstatus", "bcct", "loadavg"]

NUMBERS = [b"79\n", b"0\n", b"-1234\n", b"+5\n", b"-0\n", b"007\n", b"1.5\n", b"1234.56\n", b"1e5\n",
           b"12345678901234\n", b"99999999999999999999999\n", b"1" + b"0" * 400 + b"\n", b".5\n", b"5.\n",
           b"-\n", b"0x10\n", b" 79\n", b"79 \n", b"79\r\n", b"79\nsecond\n", b"\n79\n", b"", b"\n\n",
           b"1.23456789012\n", b"0.00001\n", b"-0.0\n", b"+-5\n", b"4294967296\n", b"9999999999.5\n",
           b"\xff\xfe\n", "٣\n".encode(), b"79\x00\n", b"3.14159265358979\n", b"000000.000100\n"]
TEXTS = [b"boot-a\n", b"", b"\n", b'Charging "ok"\\test\nsecond\tline\x01\n', "ünïcødé ✓ 😀\n".encode(),
         b"\xff\xfe\xc3(\n", b"a\x00b\n", b"tab\there\r\n", b"\n\nleading blank\n", b"trailing\n\n\n",
         b"del\x7fchar\n", bytes(range(1, 32)) + b"\n", b"x" * 100000 + b"\n", b"no newline at end",
         b'{"schema":1,"backend":"bcct","paused":true}\n', b"klog 0\ncurr_limit 1\nmtk-cl-bcct00 1 mA, state 1\n",
         b"back\\slash \\u0041 \\n\n", b"%s %d %%\n", b"-n\n", b"-e \\t\n"]
UPTIMES = [b"1234.56 890.00\n", b"", b"\n", b"abc def\n", b"1.0 2\n3.0 4\n", b"  12.5   7\n", b"12\x00.5 x\n",
           b"\t99.9\n", b"1e3 1\n", b"-5 1\n", b"12.5", b"9999999999999.25 1\n", b"12,5 1\n"]
MEMINFOS = [b"MemTotal: 1000000 kB\nMemAvailable: 654321 kB\n", b"", b"MemAvailable:\n",
            b"MemAvailable:  12 kB\nMemAvailable: 13 kB\n", b"  MemAvailable:\t77 kB\n", b"memavailable: 5\n",
            b"MemAvailable: 1.5e3 kB", b"MemAvailable: abc\n", b"X\x00\nMemAvailable: 88\n", b"\n\nMemAvailable: 9 kB\n"]
READBACKS = [b"=> get_usb_current_unlimited=1,\ncmd_discharging = -1\n", b"", b"get_usb_current_unlimited=0 cmd_discharging=1\n",
             b"get_usb_current_unlimited=+-5\ncmd_discharging\t=\t+3\n", b"get_usb_current_unlimited=1 get_usb_current_unlimited=2\n",
             b"cmd_discharging=x\ncmd_discharging = 7\n", b"cmd_discharging= -1 cmd_discharging =2\n",
             b"get_usb_current_unlimited=\ncmd_discharging=\n", b"xget_usb_current_unlimited=12345678901\n",
             b"cmd_discharging = 0\x00 1\n", b"get_usb_current_unlimited=007 cmd_discharging=-0\n"]
IP_OUT = ['printf "3: wlan0 inet 192.0.2.14/24 scope global wlan0\\n"', 'exit 1', 'printf ""',
          'printf "inet\\n"', 'printf "a inet\\nb inet 10.0.0.1/8\\n"', 'printf "x  inet\\t1.2.3.4/32 inet 5.6.7.8\\n"; exit 3',
          'printf "inet 9.9.9.9"', 'printf "\\n\\ninet q\\"uote\\\\\\n"', 'printf "inet a\\001b\\n"']
RC = ['[ "$1" != i14-ui ]', 'exit 127', 'case $1 in sshd) exit 3;; unbound) exit 1;; esac',
      'exit 0', 'kill -9 $$', 'echo noise; echo err >&2; [ "$1" = sshd ]']
BB = ['[ "$1" = nslookup ] && [ "$3" = 127.0.0.1 ]', 'exit 1', 'echo out; exit 0']
BLOGS = [b"old\nlast \"quoted\" \\ line\t\x02\n", b"", b"one line no newline", b"a\nb\n\n", b"x\n" * 50000 + b"tail\n",
         "gauge=80 ünï\n".encode(), b"\x00\n", b"a\r\nb\r\n"]
LOGS = [None, b"", b'{"partial":', b'{"a":1}\n', b"partial\x00", b"x" * 300 + b"\n", b"\n"]
MAXES = [None, "1", "100000", "0", "-1", "abc", "", " 5", "5 ", "+5", "99999999999999999999", "200"]
CRONS = [None, b"", b"# keep this\n5 * * * * echo other\n", b"no trailing newline",
         b"a\n* * * * * /bin/sh '/old/path' # i14-soak-sample\nb\n", b"x # i14-soak-sample \nfoo# i14-soak-sample\n",
         b"crlf # i14-soak-sample\r\nkeep\r\n", b"\n\n", b"# i14-soak-sample\n# i14-soak-sample\n"]

FAST_TIMEOUT = '#!/bin/sh\nshift 3\nexec "$@"\n'
SLOW_TIMEOUT = (f"#!{PY}\nimport subprocess, sys\ntry:\n    sys.exit(subprocess.run(sys.argv[4:], "
                "timeout=float(sys.argv[3])).returncode)\nexcept subprocess.TimeoutExpired:\n    sys.exit(137)\n")
FLOCK = (f"#!{PY}\nimport fcntl, sys\ntry:\n    fcntl.flock(int(sys.argv[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
         "except BlockingIOError:\n    sys.exit(1)\n")


def gen_case(rng, n):
    c = {"files": {}, "args": [], "env": {}, "ip": IP_OUT[0], "rc": RC[0], "bb": BB[0], "slow": False,
         "log": None, "cron": None, "lock": None, "flock_stub": True, "dirs": [], "name": f"case{n}"}
    mode = rng.random()
    for k in NUMERIC:
        c["files"][k] = rng.choice(NUMBERS) if rng.random() < 0.5 else b"%d\n" % rng.randint(-5000, 5000)
    for k in STRINGS:
        c["files"][k] = rng.choice(TEXTS)
    c["files"]["uptime"] = rng.choice(UPTIMES)
    c["files"]["meminfo"] = rng.choice(MEMINFOS)
    c["files"]["readback"] = rng.choice(READBACKS)
    c["files"]["blog"] = rng.choice(BLOGS)
    for k in list(c["files"]):
        r = rng.random()
        if r < 0.08:
            c["files"][k] = None  # missing
        elif r < 0.10:
            c["dirs"].append(k)  # a directory in place of the file
    c["ip"], c["rc"], c["bb"] = rng.choice(IP_OUT), rng.choice(RC), rng.choice(BB)
    c["log"] = rng.choice(LOGS)
    m = rng.choice(MAXES)
    if m is not None:
        c["env"]["I14_SOAK_MAX_BYTES"] = m
    if mode < 0.15:
        c["args"] = [rng.choice(["install", "uninstall"])]
        c["cron"] = rng.choice(CRONS)
    elif mode < 0.18:
        c["args"] = [rng.choice(["bogus", "--help", "install extra"])]
        if c["args"][0] == "install extra":
            c["args"] = ["install", "extra"]
            c["cron"] = rng.choice(CRONS)
    elif mode < 0.21:
        c["lock"] = "held"
    elif mode < 0.23:
        c["env"]["I14_SOAK_LOG"] = "@sandbox/soak/logdir"
        c["dirs_extra"] = ["soak/logdir/x"]
    if rng.random() < 0.05:
        c["slow"] = True
        c["rc"] = f"exec {PY} -c 'import time; time.sleep(3)'"
    return c


def fixed_cases():
    base = gen_case(random.Random(0), 0)
    base.update(args=[], cron=None, lock=None, env={}, slow=False, rc=RC[0], ip=IP_OUT[0], bb=BB[0], log=None,
                dirs=[], flock_stub=True)
    base.pop("dirs_extra", None)
    base["files"] = {k: b"7\n" for k in NUMERIC} | {"boot": b"boot-a\n", "status": b"Full\n", "cstatus": TEXTS[14],
                     "bcct": TEXTS[15], "loadavg": b"0.1 0.2 0.3 1/2 3\n", "uptime": UPTIMES[0], "meminfo": MEMINFOS[0],
                     "readback": READBACKS[0], "blog": BLOGS[0]}
    cases = []

    def v(name, **kw):
        c = {**base, "files": dict(base["files"]), "env": dict(base["env"]), "name": name, **kw}
        cases.append(c)
        return c
    v("test-defaults", files={k: None for k in FILES} | {
        "boot": b"boot-a\n", "uptime": b"1234.56 890.00\n", "loadavg": b"0.01 0.02 0.03 1/42 123\n",
        "meminfo": MEMINFOS[0], "readback": READBACKS[0], "capacity": b"79\n", "status": TEXTS[3],
        "batt_vol": b"4100\n", "batt_temp": b"250\n", "fg": b"-1234\n", "zcv": b"4109\n", "cstatus": TEXTS[14],
        "bcct": TEXTS[15], "type": b"2\n", "usb": b"1\n", "ac": b"0\n", "light": b"0\n", "freq": b"299000\n",
        "cap": b"299000\n", "fix": b"0\n", "blog": BLOGS[0]}, env={})
    v("all-missing", files={k: None for k in FILES}, ip="exit 1", rc="exit 127", bb="exit 1")
    v("rotate-1", log=b'{"a":1}\n', env={"I14_SOAK_MAX_BYTES": "1"})
    v("rotate-equal", log=b"x" * 199 + b"\n", env={"I14_SOAK_MAX_BYTES": "200"})
    v("rotate-below", log=b"x" * 198 + b"\n", env={"I14_SOAK_MAX_BYTES": "200"})
    v("max0-nolog", log=None, env={"I14_SOAK_MAX_BYTES": "0"})
    v("lock-held-flock", lock="held")
    v("lock-held-mkdir", lock="mkdir", flock_stub=False)
    v("lock-dir-missing", env={"I14_SOAK_LOCK": "@sandbox/nodir/lock"})
    v("log-is-dir", env={"I14_SOAK_LOG": "@sandbox/soak/logdir"}, dirs_extra=["soak/logdir/x"])
    v("slow-probes", slow=True, rc=f"exec {PY} -c 'import time; time.sleep(3)'",
      bb=f"exec {PY} -c 'import time; time.sleep(3)'")
    v("bad-path-install", args=["install"], cron=CRONS[2], prog_dir="has space")
    v("percent-path-install", args=["install"], cron=CRONS[2], prog_dir="pct%dir")
    v("cron-dir-missing", args=["install"], env={"I14_SOAK_CRONTAB": "@sandbox/new/dir/root"})
    for i, cr in enumerate(CRONS):
        v(f"install-{i}", args=["install"], cron=cr)
        v(f"uninstall-{i}", args=["uninstall"], cron=cr)
    v("usage", args=["bogus"])
    v("empty-arg", args=[""])
    v("battery-override", env={"I14_BATTERY": "@sandbox/device/alt"}, extra_files={"alt/capacity": b"55\n"})
    v("readback-override", env={"I14_CURRENT_READBACK": "@sandbox/device/rb"}, extra_files={"rb": b"cmd_discharging=1\n"})
    v("empty-env-is-default", env={"I14_SOAK_UPTIME": "", "I14_FG_CURRENT": ""})
    return cases


def build(c, root, impl):
    tree, bindir = root / "device", root / "bin"
    bindir.mkdir(parents=True)
    for k, rel in FILES.items():
        val = c["files"].get(k)
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if k in c.get("dirs", []):
            p.mkdir()
            (p / "inner").write_bytes(b"1\n")
        elif val is not None:
            p.write_bytes(val if isinstance(val, bytes) else val.encode("latin-1"))
    for rel, val in c.get("extra_files", {}).items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(val)
    for rel in c.get("dirs_extra", []):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (tree / "proc/mtk_battery_cmd").mkdir(parents=True, exist_ok=True)
    os.mkfifo(tree / "proc/mtk_battery_cmd/current_cmd")

    def cmd(name, body):
        p = bindir / name
        p.write_text(body)
        p.chmod(0o755)
    cmd("ip", "#!/bin/sh\n" + c["ip"] + "\n")
    cmd("rc-service", "#!/bin/sh\n" + c["rc"] + "\n")
    cmd("busybox", "#!/bin/sh\n" + c["bb"] + "\n")
    cmd("timeout", SLOW_TIMEOUT if c["slow"] else FAST_TIMEOUT)
    if c.get("flock_stub", True) and impl != "rust":
        cmd("flock", FLOCK)
    log = root / "soak/samples.jsonl"
    if c["log"] is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_bytes(c["log"] if isinstance(c["log"], bytes) else c["log"].encode("latin-1"))
    cron = root / "root.crontab"
    if c["cron"] is not None:
        cron.write_bytes(c["cron"] if isinstance(c["cron"], bytes) else c["cron"].encode("latin-1"))
    prog = root / c.get("prog_dir", "prog") / "i14-soak-sample"
    prog.parent.mkdir(parents=True)
    shutil.copy2(RUST if impl == "rust" else SCRIPT, prog)
    env = {k: v for k, v in os.environ.items() if not k.startswith("I14_")}
    env.update(LC_ALL="C", PATH=f"{bindir}:/usr/bin:/bin:/usr/sbin:/sbin", I14_SOAK_ROOT=str(tree),
               I14_SOAK_LOG=str(log), I14_SOAK_LOCK=str(root / "lock"), I14_SOAK_CRONTAB=str(cron))
    for k, v in c["env"].items():
        env[k] = v.replace("@sandbox", str(root))
    return env, prog, log, cron


def run(c, impl):
    root = Path(tempfile.mkdtemp(prefix=f"soakdiff-{impl}-", dir=WORK))
    env, prog, log, cron = build(c, root, impl)
    holder = None
    if c["lock"] == "held":
        import fcntl
        holder = open(root / "lock", "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif c["lock"] == "mkdir":
        (root / "lock.d").mkdir()
    argv = ([str(prog)] if impl == "rust" else SHELLS[impl] + [str(prog)]) + c["args"]
    t0 = time.time()
    p = subprocess.run(argv, env=env, capture_output=True, timeout=20)
    elapsed = time.time() - t0
    if holder:
        holder.close()
    logp = Path(env["I14_SOAK_LOG"])
    crp = Path(env["I14_SOAK_CRONTAB"])
    read = lambda q: q.read_bytes() if q.is_file() else None
    out = {"rc": p.returncode, "stderr": p.stderr.replace(str(root).encode(), b"<ROOT>"),
           "log": read(logp), "log1": read(Path(str(logp) + ".1")), "cron": read(crp), "elapsed": elapsed,
           "t0": t0, "cron_mode": (crp.stat().st_mode & 0o777) if crp.is_file() else None,
           "log_mode": (logp.stat().st_mode & 0o777) if logp.is_file() else None}
    if out["cron"] is not None:
        out["cron"] = out["cron"].replace(str(root).encode(), b"<ROOT>").replace(b"* * * * * /bin/sh '", b"* * * * * '")
    leftovers = sorted(x.name for x in crp.parent.glob(crp.name + ".i14-soak.*")) if crp.parent.exists() else []
    out["leftovers"] = leftovers
    shutil.rmtree(root, ignore_errors=True)
    return out


PROBES = re.compile(rb'(?<!\\)"services":\{([^{}]*)\},|(?<!\\)"dns":\{("ok":(?:true|false),"ms":[^{}]*)\},')
STAMP = re.compile(rb'\{"epoch":(\d+),"iso":"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)",')


def norm_log(data, t0, check):
    if data is None:
        return None
    lines = data.split(b"\n")
    out = []
    for line in lines:
        m = STAMP.search(line)
        if m:
            epoch = int(m[1])
            if check:
                assert abs(epoch - t0) < 30, (epoch, t0)
                assert time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)).encode() in (m[2],) or abs(
                    time.mktime(time.strptime(m[2].decode(), "%Y-%m-%dT%H:%M:%SZ")) - time.mktime(time.gmtime(epoch))) <= 1
            line = line[:m.start()] + b'{"epoch":E,"iso":"I",' + line[m.end():]
            segs = list(PROBES.finditer(line))
            if len(segs) == 2:
                services = dns = None
                for s in segs:
                    if s[1] is not None:
                        services = s[1]
                    else:
                        dns = s[2]
                items = dict(x.split(b":", 1) for x in services.split(b","))
                canon = b'"services":{' + b",".join(b'"%s":%s' % (k.encode(), items[b'"%s"' % k.encode()])
                                                    for k in SERVICES) + b'},"dns":{' + dns + b"},"
                line = line[:segs[0].start()] + canon + line[segs[1].end():]
        out.append(line)
    return b"\n".join(out)


KEYS = re.compile(rb'(?<![\\\\])"([a-z0-9_]+)":')


def fields_of(line):
    marks = [(m.start(), m[1]) for m in KEYS.finditer(line)]
    out = {}
    for i, (pos, key) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(line)
        out.setdefault(key, line[pos:end])
    return out


def field_diff(a, b):
    if a is None or b is None or a.count(b"\n") != b.count(b"\n"):
        return (a if a is None else a[-400:], b if b is None else b[-400:])
    da, db = [], []
    for la, lb in zip(a.split(b"\n"), b.split(b"\n")):
        if la == lb:
            continue
        fa, fb = fields_of(la), fields_of(lb)
        for k in dict.fromkeys(list(fa) + list(fb)):
            if fa.get(k) != fb.get(k):
                da.append((fa.get(k) or b"")[:200]); db.append((fb.get(k) or b"")[:200])
        if not da:
            da.append(la[:300]); db.append(lb[:300])
    return (da, db)


def compare(c):
    rust = run(c, "rust")
    res = {"name": c["name"], "diffs": [], "rust_log": rust["log"]}
    for impl in SHELLS:
        sh = run(c, impl)
        for key in ("rc", "log1", "cron", "cron_mode", "log_mode", "leftovers"):
            if sh[key] != rust[key]:
                res["diffs"].append((impl, key, sh[key], rust[key]))
        a, b = norm_log(sh["log"], sh["t0"], False), norm_log(rust["log"], rust["t0"], True)
        if a != b:
            res["diffs"].append((impl, "log", *field_diff(a, b)))
        if norm_log(sh["log1"], 0, False) != norm_log(rust["log1"], 0, False):
            res["diffs"].append((impl, "log1", sh["log1"], rust["log1"]))
        script_msgs = (b"usage: i14-soak-sample", b"use a script path")
        if any(m in sh["stderr"] for m in script_msgs) or any(m in rust["stderr"] for m in script_msgs):
            if sh["stderr"] != rust["stderr"]:
                res["diffs"].append((impl, "stderr", sh["stderr"], rust["stderr"]))
        elif bool(sh["stderr"]) != bool(rust["stderr"]):
            res["diffs"].append((impl, "stderr-presence", sh["stderr"][:300], rust["stderr"][:300]))
        res.setdefault("shell_logs", []).append(sh["log"])
    if c["slow"]:
        res["slow_elapsed"] = rust["elapsed"]
    return res


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    WORK = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp())
    WORK.mkdir(parents=True, exist_ok=True)
    rng = random.Random(1107)
    cases = fixed_cases() + [gen_case(rng, i) for i in range(n)]
    results = []
    with cf.ThreadPoolExecutor(8) as pool:
        for r in pool.map(compare, cases):
            results.append(r)
    def classify(r, d):
        impl, key, a, b = d
        if r["name"] == "lock-held-mkdir":
            return "A mkdir-fallback lock (no flock CLI): shell skips, Rust (flock) samples"
        if key == "rc" and impl == "sh" and (a, b) == (1, 2):
            return "B redirection failure: bash exits 1, dash and Rust exit 2"
        if key == "log" and all(x == b'"ms":},' for x in a) and all(y == b'"ms":0},' for y in b):
            return "C multi-line uptime: macOS awk rejects -v newline -> shell writes invalid \"ms\":}"
        if key == "stderr-presence" and (b"Killed: 9" in a or b"newline in string" in a) and not b:
            return "D shell-only stderr noise (bash job report / macOS awk error)"
        return None
    cats, bad = {}, []
    for r in results:
        unexplained = [d for d in r["diffs"] if classify(r, d) is None]
        for d in r["diffs"]:
            c = classify(r, d)
            if c:
                cats.setdefault(c, set()).add(r["name"])
        if unexplained:
            bad.append({**r, "diffs": unexplained})
    corpus = WORK / "sampler-logs"
    corpus.mkdir(exist_ok=True)
    for r in results:
        for i, data in enumerate([r["rust_log"]] + r.get("shell_logs", [])):
            if data:
                (corpus / f"{r['name']}-{i}.jsonl").write_bytes(data)
    print(f"cases: {len(results)} (fixed {len(fixed_cases())}, generated {n}) x shells {list(SHELLS)}; "
          f"unexplained differences: {len(bad)}")
    for c, names in sorted(cats.items()):
        print(f"  expected [{len(names)} cases] {c}")
    for r in bad:
        print("==", r["name"])
        for impl, key, a, b in r["diffs"]:
            print(f"   [{impl}] {key}:\n      shell={a!r:.600}\n      rust ={b!r:.600}")
    slow = [r["slow_elapsed"] for r in results if "slow_elapsed" in r]
    if slow:
        print(f"slow-probe cases: {len(slow)}, max rust elapsed {max(slow):.2f}s")
