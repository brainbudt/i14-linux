#!/usr/bin/env python3
"""Phase 4 E4a/E4b in one command (PHASE4-E3E4-RESULT section 7.2, 7.5, 7.6), over the USB
serial console because the Mac's ECM link stays down under 7.0.

  e4-run.py check                      host checks only (hashes, files); touches nothing
  e4-run.py e4a --go                   the dead-man test (cs=15)
  e4-run.py e4b --go                   60 s charging (cs=14, td0_ms from the saved E4a PASS)
  e4-run.py e5 --go [--run-s 1800]     E5: run_s of charging (300..1800), same module and guards, prep 3600
  e4-run.py judge e4a|e4b|e5 STATE LOG judge saved output again

Only with the user present and their explicit go for this one test. Start in v7 Linux with
the cable in the Mac and no charge current for at least 60 s (i14-charge-hold, or unplugged
then plugged back in just before the start). Every stop after the insmod ends in `reboot -f`
back to v7; nothing is flashed. Output: artifacts/phase4-e3e4-20261102/<mode>/<UTC time>/."""
import argparse
import glob
import hashlib
import importlib.util
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
ART = ROOT / "artifacts/phase4-e3e4-20261102"
BUILD = ART / "build-r5"  # det baseline, AUXADC miss per pass, mode e5 (2026-11-03)
E34 = ART / "phone/e34.sh"
BOOT = ROOT / "builds/i14-wdtfix-trial/boot.img"
# The trial busybox drops insmod options (no CONFIG_FEATURE_CMDLINE_MODULE_OPTIONS):
# tools/i14-insmod.c, built by scripts/build-insmod.sh, replaces it for e34.sh ins.
INSMOD = ART / "insmod/insmod"
# PATH cannot override it (FEATURE_SH_STANDALONE runs applets first); an ash function can.
# e34.sh is sourced unchanged in a subshell, so its prep and /proc/modules gates still run.
INS = ("chmod 700 /run/e34/bin/insmod && ( set -- ins /run/e34/i14-e4-charge.ko {params}; "
       'insmod() {{ /run/e34/bin/insmod "$@"; }}; . /run/e34/e34.sh )')
SHA = {
    BOOT: "cc73e461dae3e6d5b3316a85246034d94ec86574661106d288bdcc40d05c160c",
    BUILD / "i14-e4-charge.ko": "be466c6c40d11d27d367df78a8b7c8987d19249f8dbf200082de6df448b0fae5",
    E34: "c04e05bea4dbd810b8767ba9c6a008c418f959a53afbfaa6d1075aca6157dacc",
    INSMOD: "2aafdec05ed4a7a1576656d49e34ed2beb46c0f6373dec329d8cff65cf516d11",
}
SSH = shlex.split(os.environ.get("I14_SSH", "ssh root@172.16.42.1"))
V7_HOST = "172.16.42.1"
VBAT_MIN, VBAT_MAX = 3800, 4100      # rest VBAT window for E4 (section 7.1 point 2)
TEMP_MIN, TEMP_MAX = 100, 400        # batt_temp in 0.1 degC (section 7.7)
REST_SECONDS = 60
PARAMS = {"e4a": "mode=e4a cs=15", "e4b": "mode=e4b cs=14 td0_ms={td0}",
          "e5": "mode=e5 cs=14 td0_ms={td0} run_s={run}"}
DONE_WITHIN = {"e4a": 15, "e4b": 70}  # section 7.7: phase must reach done in time; e5: run_s + 70
POLL = {"e4a": 1, "e4b": 5, "e5": 30}
CAP = {"e4a": 600, "e4b": 600, "e5": 3600}  # e34.sh prep session cap (reboot -f)
LOG_EVERY = 120  # e5: the module keeps 256 passes (1 s each); fetch the log well within that
PWRAP_CLEAN = "WDT_FLG=00000000 INT_RAW=00000000 INT_FLG=00000000"
F48_GATES, F7A_MODE, CC_DET, CS_DET = 0x18, 0x44, 0x80, 0x20  # CHR_EN|CSDAC_EN, MODE|HWCV, 0xf4c b7, b5


# the identity predicates of scripts/wait-native-once.py COMMAND (section 7.2 step 4)
IDENTITY = ('[ "$(uname -r)" = 7.0.10-i14-bringup ] && [ "$(uname -m)" = aarch64 ] && '
            '[ "$(sha256sum /proc/device-tree/model)" = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a  '
            '/proc/device-tree/model" ] && [ "$(sha256sum /proc/device-tree/compatible)" = '
            '"867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a  /proc/device-tree/compatible" ] '
            '&& echo I14_IDENTITY_OK')


class Stop(Exception):
    """Stop the run; the phone gets `reboot -f` if 7.0 was reached."""


# ---------- judging (pure, host-tested) ----------

def prep_ok(rc, text, cap=600):
    """e34.sh prep runs i14-pwrap-status 3 times (3 samples each): every sample must be clean."""
    samples = [l for l in text.splitlines() if l.startswith("I14_PWRAP_STATUS")]
    return (rc == 0 and f"PREP_OK cap={cap} " in text and "kexec_crash_loaded=0" in text
            and len(samples) >= 3 and all(PWRAP_CLEAN in l for l in samples))


def parse_state(text):
    state = {}
    for line in text.splitlines():
        key, _, value = line.strip().partition(" ")
        if key:
            state[key] = value.strip()
    return state


def parse_log(text):
    """Rows of the i14-e4/log table as dicts; register columns as ints (named like '0f48')."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].startswith("# "):
        return []
    names = lines[0][2:].split()
    rows = []
    for line in lines[1:]:
        if line.startswith("#"):
            continue
        cells = line.split()
        if len(cells) != len(names):
            continue
        row = dict(zip(names, cells))
        for name in names:
            if len(name) == 4 and name.startswith("0f"):
                row[name] = int(row[name], 16)
        rows.append(row)
    return rows


def judge(mode, state_text, log_text):
    """(passed, reasons). Spec section 6.3 / 6.4 plus the stop criteria of RESULT 7.7.
    E4b's 'VBAT/FG clearly better than E3' stays a human call; the numbers are reported."""
    s, rows, why = parse_state(state_text), parse_log(log_text), []
    if s.get("mode") != mode:
        why.append(f"mode {s.get('mode')!r}, expected {mode}")
    if s.get("phase") != "done":
        why.append(f"phase {s.get('phase')!r}, not done")
    want = "deadman_pass" if mode == "e4a" else "complete"
    if s.get("result") != want:
        why.append(f"result {s.get('result')!r}, expected {want}")
    for key, value in (("stop_verified", "1"), ("may_be_on", "0"), ("det_stuck", "0")):
        if s.get(key) != value:
            why.append(f"{key} {s.get(key)!r}, expected {value}")
    if not rows:
        why.append("log has no rows")
    elif [int(r["seq"]) for r in rows] != list(range(len(rows))):  # the ring keeps 256: e5 merges fetches
        why.append("log incomplete: seq not contiguous from 0")
    elif rows[-1].get("phase") != "done":
        why.append("log incomplete: does not end in a done pass")
    if mode == "e4a":
        td0 = s.get("td0_out_ms", "unknown")
        if not td0.isdigit() or int(td0) > 6000:
            why.append(f"td0_out_ms {td0!r}, expected <= 6000")
        if not any(r["0f48"] & F48_GATES == F48_GATES and r["0f7a"] & F7A_MODE == F7A_MODE for r in rows):
            why.append("log never shows 0xf48&0x18=0x18 with 0xf7a&0x44=0x44")
    else:
        charging = [r for r in rows if r.get("phase") == "charging"]
        if not charging:
            why.append("log has no charging passes")
        for r in charging:
            if r["0f48"] & F48_GATES != F48_GATES or r["0f7a"] & F7A_MODE != F7A_MODE:
                why.append(f"pass {r['seq']}: gates or MODE/HWCV not 1 (0f48={r['0f48']:04x} 0f7a={r['0f7a']:04x})")
                break
            if not r["0f4c"] & CC_DET:
                why.append(f"pass {r['seq']}: 0xf4c CC_DET not 1 (0f4c={r['0f4c']:04x})")
                break
        # [HW, E4b 2026-11-03] CS_DET toggles pass by pass while the current regulates at CS_VTH
        # (16 of 27 passes): it must show up, not hold in every 1 Hz sample.
        if charging and not any(r["0f4c"] & CS_DET for r in charging):
            why.append("0xf4c CS_DET never 1 while charging")
    return not why, why


def charge_summary(rows):
    """Numbers for the human VBAT check (e4b/e5): rest before, under charge, after the stop."""
    def full(phase):
        return [r for r in rows if r.get("phase") == phase and r.get("full") == "1"]
    pre, chg, done = full("preflight"), full("charging"), full("done")
    if not chg:
        return "charge summary: no full charging passes"
    d = [int(r["isense_mv"]) - int(r["vbat_mv"]) for r in chg]
    v = [int(r["vbat_mv"]) for r in chg]
    return (f"charge summary: preflight VBAT {pre[0]['vbat_mv'] if pre else '?'} mV; charging {len(chg)} passes, "
            f"VBAT {v[0]} -> {v[-1]} mV, ISENSE-VBAT mean {sum(d) / len(d):.1f} mV; "
            f"after the stop VBAT {done[-1]['vbat_mv'] if done else '?'} mV")


def td0_for_e4b(verdict_path):
    """td0_ms from a saved E4a PASS; refuses anything else."""
    v = json.loads(pathlib.Path(verdict_path).read_text())
    if v.get("mode") != "e4a" or not v.get("passed"):
        raise Stop(f"{verdict_path}: not an E4a PASS")
    td0 = int(v["state"]["td0_out_ms"])
    if not 2000 <= td0 <= 6000:
        raise Stop(f"td0_out_ms {td0} outside 2000..6000")
    return td0


def rest_ok(samples):
    """samples: (t, status, fg_current_0_1mA, vbat_mv, temp_0_1C). Rest = no charge current
    (FG > 0 is out of the battery) over at least REST_SECONDS. Returns (ok, reason, vbat)."""
    if not samples or samples[-1][0] - samples[0][0] < REST_SECONDS:
        return False, "fewer than 60 s of samples", None
    for t, status, fg, mv, temp in samples:
        if status not in ("Discharging", "Cmd discharging", "Not charging") or fg <= 0:
            return False, f"charge current at +{t - samples[0][0]:.0f} s (status {status}, FG {fg})", None
        if not TEMP_MIN <= temp <= TEMP_MAX:
            return False, f"battery temperature {temp / 10} C outside 10..40", None
    mv = samples[-1][3]
    if not VBAT_MIN <= mv <= VBAT_MAX:
        return False, f"rest VBAT {mv} mV outside {VBAT_MIN}..{VBAT_MAX}", mv
    return True, "ok", mv


# ---------- host and phone steps ----------

def sha256(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def host_checks():
    for path, want in SHA.items():
        got = sha256(path)
        if got != want:
            raise Stop(f"{path}: sha256 {got}, expected {want}")
    sums = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=BUILD, capture_output=True, text=True)
    if sums.returncode:
        raise Stop("build/SHA256SUMS: " + sums.stdout + sums.stderr)
    for tool in (ROOT / "scripts/ram-boot.sh", ROOT / ".venv/bin/python", pathlib.Path(SSH[0])):
        if not tool.exists():
            raise Stop(f"missing {tool}")
    print("host checks OK: boot.img, i14-e4-charge.ko, e34.sh, insmod, SHA256SUMS")


def v7(cmd, timeout=30):
    r = subprocess.run(SSH + [cmd], env=dict(os.environ, I14_HOST=V7_HOST),
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout


def v7_rest_vbat(log):
    """Sample v7 for REST_SECONDS + a bit; refuse unless at rest, in the window, 10..40 C."""
    cmd = ("cd /sys/class/power_supply/battery && echo \"$(cat status)|"
           "$(cat /sys/devices/platform/battery_meter/FG_Current)|$(cat batt_vol)|$(cat batt_temp)\"")
    samples, t0 = [], time.time()
    while time.time() - t0 < REST_SECONDS + 15:
        rc, out = v7(cmd)
        if rc:
            raise Stop("v7 not reachable over USB (172.16.42.1); cable in the Mac?")
        status, fg, mv, temp = out.strip().split("|")
        samples.append((time.time(), status, int(fg), int(mv), int(temp)))
        log(f"v7 rest sample: {status} FG={int(fg) / 10:+.1f} mA VBAT={mv} mV T={int(temp) / 10} C")
        time.sleep(10)
    ok, reason, mv = rest_ok(samples)
    if not ok:
        raise Stop("rest check: " + reason)
    return mv


def load_console():
    spec = importlib.util.spec_from_file_location("i14push", ROOT / "scripts/i14-serial-push.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def wait_port(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        ports = sorted(glob.glob("/dev/cu.usbmodem*"))
        if ports:
            return ports[0]
        time.sleep(1)
    raise Stop("no serial port from 7.0 (it reboots to v7 by itself after 120 s)")


def fastboot_present():  # the same test as scripts/ram-boot.sh
    return subprocess.run(["bash", "-c", "ioreg -p IOUSB -l -w0 | grep -A30 '\"idVendor\" = 2996' "
                           "| grep -q '\"idProduct\" = 3073'"]).returncode == 0


def wait_fastboot(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        if fastboot_present():
            return True
        time.sleep(1)
    return False


def wait_v7(seconds, log):
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            if v7("uname -r", 10)[1].strip().startswith("3.18"):
                log(f"back in v7 after {time.time() - t0:.0f} s")
                return True
        except subprocess.TimeoutExpired:
            pass
        time.sleep(5)
    log("v7 did not come back over USB in time: see section 7.8")
    return False


def merge_logs(texts):
    """The 256-pass ring fetched several times, as one table ordered by seq (later fetch wins)."""
    header, rows = None, {}
    for text in texts:
        for line in text.splitlines():
            if line.startswith("# seq"):
                header = header or line
            elif line.strip() and not line.startswith("#") and line.split()[0].isdigit():
                rows[int(line.split()[0])] = line
    return "\n".join([header or ""] + [rows[k] for k in sorted(rows)]) + "\n" if header else ""


def run(mode, out, run_s=None):
    out.mkdir(parents=True, exist_ok=True)
    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(out / "run.log", "a") as trace:
            trace.write(line + "\n")

    host_checks()
    td0 = td0_for_e4b(ART / "e4a/verdict.json") if mode != "e4a" else None
    params = PARAMS[mode].format(td0=td0, run=run_s)
    done_within = run_s + 70 if mode == "e5" else DONE_WITHIN[mode]
    rest_mv = v7_rest_vbat(log)
    log(f"rest VBAT {rest_mv} mV: in the window; rebooting to the bootloader")
    result = {"mode": mode, "params": params, "rest_vbat_mv": rest_mv}
    con = None
    try:  # from the reboot request on, every way out ends in reboot -f (if a console exists) and v7 recovery
        v7("(sleep 1; i14-reboot-bootloader) >/dev/null 2>&1 &")
        if not wait_fastboot(60):
            raise Stop("no fastboot device within 60 s")
        rb = subprocess.run([str(ROOT / "scripts/ram-boot.sh"), str(BOOT)], capture_output=True, text=True)
        log("ram-boot: " + (rb.stdout + rb.stderr).strip().replace("\n", " | "))
        if rb.returncode:
            raise Stop("ram-boot failed (section 7.8)")
        result["port"] = port = wait_port(90)
        con = load_console().Console(port)

        def sh(cmd, timeout=30, name=None):
            rc, text = con.run(cmd, timeout)
            if name:
                (out / name).write_text(text + "\n")
            log(f"$ {cmd[:90]} -> rc={rc}")
            return rc, text

        rc, text = sh(IDENTITY, name="identity.txt")
        if "I14_IDENTITY_OK" not in text:
            raise Stop("identity check failed")
        rc, text = sh('m=$(cat /proc/modules) && [ -z "$m" ] && echo MODULES_EMPTY; n=0; rw=0; '
                      'for p in /sys/class/block/mmcblk*; do n=$((n+1)); [ "$(cat "$p/ro")" = 1 ] || rw=$((rw+1)); done; '
                      'echo STORAGE=$n:$rw', name="guards.txt")
        if "MODULES_EMPTY" not in text or "STORAGE=26:0" not in text:
            raise Stop("guard check failed (modules or eMMC read-only)")
        rc, text = sh('kill "$(cat /run/i14-trial-reboot.pid)"; sleep 1; dmesg | grep I14_TRIAL_REBOOT_CANCELLED')
        if "I14_TRIAL_REBOOT_CANCELLED" not in text:
            raise Stop("could not cancel the 120 s reboot timer")
        push = load_console().push
        push(con, str(E34), "/run/e34/e34.sh")
        push(con, str(BUILD / "i14-e4-charge.ko"), "/run/e34/i14-e4-charge.ko")
        push(con, str(INSMOD), "/run/e34/bin/insmod")
        rc, text = sh(f"chmod 700 /run/e34/e34.sh; /run/e34/e34.sh prep {CAP[mode]}", 60, "prep.txt")
        if not prep_ok(rc, text, CAP[mode]):
            raise Stop("prep did not give PREP_OK with clean pwrap samples")
        log(f"insmod {params}")
        rc, text = sh(INS.format(params=params), 60, "ins.txt")  # rc = insmod rc
        result["ins_rc"] = rc
        if rc:
            sh("dmesg | tail -n 60", 30, "ins-dmesg.txt")
            raise Stop(f"insmod rc={rc}; see ins.txt and ins-dmesg.txt (section 7.5 table)")
        t0, polls, logs, t_log = time.time(), [], [], time.time()
        while True:
            if mode == "e5" and time.time() - t_log >= LOG_EVERY:
                rc, text = sh("cat /sys/kernel/debug/i14-e4/log", 120)
                if rc == 0:  # a failed fetch adds nothing; a gap it leaves fails the judge
                    logs.append(text)
                t_log = time.time()
            rc, text = sh("grep -E '^(phase|result|stop_reason|may_be_on|det_stuck|kicks|passes) ' "
                          "/sys/kernel/debug/i14-e4/state")
            polls.append(text)
            s = parse_state(text)
            if s.get("det_stuck") == "1" or s.get("result", "").startswith(("aborted", "deadman_fail")):
                sh("echo 1 >/sys/kernel/debug/i14-e4/stop")
                log("abort criterion: " + text.replace("\n", "; "))
                time.sleep(3)
                break
            if s.get("phase") == "done":
                break
            if time.time() - t0 > done_within:
                sh("echo 1 >/sys/kernel/debug/i14-e4/stop")
                log(f"phase not done within {done_within} s: stop requested")
                time.sleep(3)
                break
            time.sleep(POLL[mode])
        (out / "polls.txt").write_text("\n----\n".join(polls) + "\n")
        _, state = sh("cat /sys/kernel/debug/i14-e4/state", 30, "state.txt")
        rc, elog = sh("cat /sys/kernel/debug/i14-e4/log", 120, "log.txt")
        if rc:
            elog = ""  # no final log: the judge fails ("log has no rows")
        if logs and elog:  # e5: the whole session, not only the last 256 passes
            elog = merge_logs(logs + [elog])
            (out / "log-all.txt").write_text(elog)
        sh("dmesg", 120, "dmesg.txt")
        passed, why = judge(mode, state, elog)
        result.update(passed=passed, reasons=why, state=parse_state(state))
        if mode != "e4a":
            result["charge_summary"] = charge_summary(parse_log(elog))
            log(result["charge_summary"])
        if mode != "e4a" and passed:  # spec 6.4 also needs VBAT/FG clearly better than E3: a human call
            result.update(passed=False, auto_passed=True, pending="VBAT/FG versus the E3 baseline")
            log("AUTO CHECKS PASS; pending: VBAT/FG versus the E3 baseline (decide together)")
        else:
            log(("PASS" if passed else "FAIL") + (": " + "; ".join(why) if why else ""))
    except Stop as e:
        result.update(passed=False, reasons=[str(e)])
        log(f"STOP: {e}")
    except (Exception, KeyboardInterrupt, SystemExit) as e:
        result.update(passed=False, reasons=[f"{type(e).__name__}: {e}"])
        log(f"ERROR: {type(e).__name__}: {e}")
        try:  # lock-free stop first, if the module is there
            con.s.write(b"echo 1 >/sys/kernel/debug/i14-e4/stop\n")
            time.sleep(3)
        except Exception:
            pass
    finally:
        if con is None:
            log("no 7.0 console: nothing loaded; the 120 s timer returns 7.0 to v7 by itself")
        else:
            log("reboot -f back to v7")
            try:
                con.s.write(b"reboot -f\n")
            except Exception as e:
                log(f"could not send reboot -f ({e}); mtk-wdt or the session cap resets within 600 s")
        (out / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")
    if wait_v7(240, log):
        _, kmsg = v7("cat /proc/last_kmsg", 60)
        (out / "last_kmsg.txt").write_text(kmsg)
        _, bat = v7("cat /sys/class/power_supply/battery/batt_vol /sys/class/power_supply/battery/status")
        log("v7 battery after: " + bat.strip().replace("\n", " "))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["check", "e4a", "e4b", "e5", "judge"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--run-s", type=int, default=1800, help="e5 only: charging time, 300..1800 s")
    ap.add_argument("--go", action="store_true", help="the user is present and gave a go for this test")
    a = ap.parse_args()
    try:
        if a.action == "check":
            host_checks()
            return 0
        if a.action == "judge":
            mode, state, elog = a.files
            passed, why = judge(mode, pathlib.Path(state).read_text(), pathlib.Path(elog).read_text())
            print(charge_summary(parse_log(pathlib.Path(elog).read_text())))
            if passed and mode != "e4a":
                print("AUTO CHECKS PASS; pending: VBAT/FG versus the E3 baseline")
                return 3
            print("PASS" if passed else "FAIL: " + "; ".join(why))
            return 0 if passed else 1
        if not a.go:
            raise Stop("needs --go: only with the user present and their go for this test")
        if a.action == "e5" and not 300 <= a.run_s <= 1800:
            raise Stop("--run-s must be 300..1800")
        out = ART / a.action / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        if a.action == "e4a":  # only the latest E4a counts for E4b
            (ART / "e4a/verdict.json").unlink(missing_ok=True)
        result = run(a.action, out, a.run_s if a.action == "e5" else None)
        if result.get("passed") and a.action == "e4a":
            (ART / "e4a").mkdir(exist_ok=True)
            (ART / "e4a/verdict.json").write_text((out / "verdict.json").read_text())
        print(f"output: {out}")
        return 0 if result.get("passed") else 3 if result.get("auto_passed") else 1
    except Stop as e:
        print(f"STOP: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
