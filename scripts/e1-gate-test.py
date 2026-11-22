#!/usr/bin/env python3
"""E1 (docs/research/PHASE4-STATE-2026-11-02.md): on the STOCK 3.18 kernel, does MT6328
CSDAC_EN (0xf48 b3) / CSDAC_MODE (0xf7a b2) keep its value while CHR_EN (0xf48 b4) = 0?

Runs as root on the phone (Alpine on the stock kernel) while it charges. The stock charger
policy keeps running (BAT_thread every ~10 s rewrites 0xf48/0xf7a and re-enables charging).

Write interface: /sys/devices/platform/mt-pmic/pmic_access (pmic.c store_pmic_access).
More than 5 bytes = "ADDR VALUE" (hex, one space), parse errors are ignored (a malformed
write would hit 0x0000 or write 0), <= 5 bytes = read, `cat` prints the last read (%u).
Safety:
- only (register, value) pairs from ALLOWED, formatted as exactly b"%04x %04x", one write();
- every access must be confirmed by exactly one NEW kernel log record (/dev/kmsg, opened at
  its end), and a read must also match the value `cat` returns;
- STRUP_CON0 (0x0000, b0 = THR_DET_DIS) must read 64 after every write;
- measurements are bracketed by CHR_EN checks; stock-policy interference => "inconclusive";
- the exit path restores and VERIFIES the stock charging state; exit code 0 only then.
Exit codes: 0 ok, 1 aborted before any write, 2 aborted but restored, 3 restore NOT verified.
"""
import json
import os
import re
import signal
import sys
import time

NODE = "/sys/devices/platform/mt-pmic/pmic_access"
STATUS = "/sys/class/power_supply/battery/status"
STRUP, CHR0, CHR25 = 0x0000, 0x0F48, 0x0F7A
ALLOWED = {(CHR0, 0x0001), (CHR0, 0x0009), (CHR0, 0x0019), (CHR25, 0x00D0), (CHR25, 0x00D4), (STRUP, 0x0040)}
STOCK_CHR0, STOCK_CHR25, STRUP_OK = 0x7B, 0xD4, 64
CSDAC_REPEATS = 5
LOG = os.environ.get("E1_LOG", "/root/e1-gate-test.log")
RESULT = os.environ.get("E1_RESULT", "/root/e1-result.json")
WRITE_RE = re.compile(r"\[store_pmic_access\] write PMU reg 0x([0-9a-f]+) with value 0x([0-9a-f]+) !")
READ_RE = re.compile(r"\[store_pmic_access\] read PMU reg 0x([0-9a-f]+) with value 0x([0-9a-f]+) !")


class Abort(Exception):
    pass


def say(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:  # logging must never stop the restore path
        pass


class Kmsg:
    """New kernel log records only (opened at the end of the ring)."""

    def __init__(self):
        self.fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
        os.lseek(self.fd, 0, os.SEEK_END)

    def new(self):
        out = []
        while True:
            try:
                rec = os.read(self.fd, 8192)
            except BlockingIOError:
                return out
            except OSError as e:  # EPIPE: records were overwritten while we were not reading
                raise Abort(f"kernel log overrun ({e}); evidence incomplete")
            out.append(rec.decode("utf-8", "replace").split(";", 1)[-1].split("\n", 1)[0])


class NodeIO:
    """The real pmic_access node plus fresh kernel log records."""

    def __init__(self):
        self.kmsg = Kmsg()

    def send(self, data):
        fd = os.open(NODE, os.O_WRONLY)
        try:
            n = os.write(fd, data)
        finally:
            os.close(fd)
        if n != len(data):
            raise Abort(f"short write {n}/{len(data)} for {data!r}")
        time.sleep(0.02)  # let the printk records land
        return self.kmsg.new()

    def cat(self):
        with open(NODE) as f:
            return f.read().strip()

    def status(self):
        with open(STATUS) as f:
            return f.read().strip()


# Stock charger driver activity (it logs every turn-on/off and its register setters).
POLICY_RE = re.compile(r"\[BATTERY\]|\[charging_|pchr_|charging_hw|bat_thread")


class Pmic:
    def __init__(self, io=None):
        self.io = io or NodeIO()
        self.writes = 0
        self.journal = []  # every new kernel log record seen, in order

    def _send(self, data):
        lines = self.io.send(data)
        self.journal.extend(lines)
        return lines

    def policy_since(self, mark):
        """Stock-driver log records seen since journal index `mark`."""
        return [l for l in self.journal[mark:] if POLICY_RE.search(l) and "store_pmic_access" not in l]

    def read(self, reg):
        lines = self._send(b"%04x" % reg)
        hits = [m for m in (READ_RE.search(l) for l in lines) if m]
        if any(WRITE_RE.search(l) for l in lines) or len(hits) != 1 or int(hits[0].group(1), 16) != reg:
            raise Abort(f"read 0x{reg:04x} not confirmed by exactly one fresh log record: {lines}")
        value = int(hits[0].group(2), 16)
        text = self.io.cat()
        if not text.isdigit() or int(text) != value:
            raise Abort(f"read 0x{reg:04x}: log says 0x{value:x}, node says {text!r}")
        return value

    def write(self, reg, value):
        if (reg, value) not in ALLOWED:
            raise Abort(f"write 0x{reg:04x}=0x{value:04x} not allowed")
        data = b"%04x %04x" % (reg, value)
        assert len(data) == 9 and data.count(b" ") == 1
        lines = self._send(data)
        hits = [m for m in (WRITE_RE.search(l) for l in lines) if m]
        if len(hits) != 1 or (int(hits[0].group(1), 16), int(hits[0].group(2), 16)) != (reg, value):
            raise Abort(f"write {data!r} not confirmed by exactly one fresh log record: {lines}")
        self.writes += 1
        strup = self.read(STRUP)
        if strup != STRUP_OK:
            raise StrupFault(strup)


class StrupFault(Abort):
    def __init__(self, value):
        super().__init__(f"STRUP_CON0 is {value}, expected {STRUP_OK}")
        self.value = value


def plausible_chr0(v):
    # Charger connected: CHRDET (b5) = 1 and VCDT_HV_EN (b0) = 1 in every state we create.
    # A stale value from an earlier read of another register (e.g. 0x40) fails this.
    return v & 0x21 == 0x21


def bits(v):
    return {"chr_en": v >> 4 & 1, "csdac_en": v >> 3 & 1}


def wait_for_policy_round(p, timeout=25):
    """Switch charging off and wait until the stock policy switches it back on: that marks
    the start of a ~10 s window without policy writes."""
    p.write(CHR0, 0x0001)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = p.read(CHR0)
        if not plausible_chr0(v):
            raise Abort(f"implausible 0xf48 read 0x{v:x}")
        if v >> 4 & 1:
            return time.monotonic()
        time.sleep(0.1)
    raise Abort("stock policy did not re-enable charging within 25 s")


def measure_round(p):
    t0 = wait_for_policy_round(p)
    r = {"t_after_policy_s": None}
    # The stock policy re-enables charging within ~0.4 s (seen on hardware), so the MODE
    # check comes first and every observation is bracketed by its own CHR_EN read.
    p.write(CHR0, 0x0001)
    a = p.read(CHR0)
    m0 = p.read(CHR25)
    mode_mark = len(p.journal)
    p.write(CHR25, 0x00D0)
    mz = p.read(CHR25)  # MODE=0 baseline must read back before MODE=1 means anything
    p.write(CHR25, 0x00D4)
    m1 = p.read(CHR25)
    c = p.read(CHR0)  # bracket: still CHR0 after the MODE writes?
    mode_policy = p.policy_since(mode_mark)
    # CSDAC: repeat write+readback; any stock-driver record in the window spoils the round,
    # because its disable path (CSDAC_EN=0, then CHR_EN=0) could fake "does not stick".
    csdac_mark = len(p.journal)
    reads = []
    for _ in range(CSDAC_REPEATS):
        p.write(CHR0, 0x0009)
        reads.append(p.read(CHR0))
    b = reads[-1]
    csdac_policy = p.policy_since(csdac_mark)
    p.write(CHR0, 0x0019)
    d = p.read(CHR0)
    p.write(CHR25, 0x00D4)
    m2 = p.read(CHR25)
    r["t_after_policy_s"] = round(time.monotonic() - t0, 2)
    for name, v in [("a", a), ("c", c), ("d", d)] + [(f"b{i}", x) for i, x in enumerate(reads)]:
        if not plausible_chr0(v):
            raise Abort(f"implausible 0xf48 read {name}=0x{v:x}")
    r.update({"chr0_after_0001": hex(a), "chr25_with_chr0": hex(m0), "chr0_after_0009": [hex(x) for x in reads],
              "policy_records_csdac_window": csdac_policy, "policy_records_mode_window": mode_policy,
              "chr25_after_mode0_chr0": hex(mz),
              "chr25_after_mode1_chr0": hex(m1), "chr0_bracket": hex(c), "chr0_after_0019": hex(d),
              "chr25_after_mode1_chr1": hex(m2)})
    # The CSDAC observation is valid when its own readback b still shows CHR_EN=0 (a policy
    # write would have set CHR_EN=1 in the same register); MODE needs a and c both CHR_EN=0.
    clean = bits(a)["chr_en"] == 0 and all(bits(x)["chr_en"] == 0 for x in reads) and not csdac_policy
    mode_clean = bits(a)["chr_en"] == 0 and bits(c)["chr_en"] == 0 and not mode_policy
    if any(bits(x)["chr_en"] == 0 and bits(x)["csdac_en"] == 1 for x in reads):
        r["verdict"] = "no gate: CSDAC_EN stuck with CHR_EN=0 at least once"
    elif not clean:
        r["verdict"] = "inconclusive (stock driver active or CHR_EN changed during the CSDAC window)"
    elif bits(d)["csdac_en"] == 1:
        r["verdict"] = (f"gate: CSDAC_EN did not stick with CHR_EN=0 in {len(reads)}/{len(reads)} tries "
                        "without logged stock-driver activity; sticks with CHR_EN=1")
    else:
        r["verdict"] = "unexpected: CSDAC_EN not set even with CHR_EN=1"
    if not mode_clean or mz >> 2 & 1:
        r["mode_verdict"] = "inconclusive (MODE=0 baseline not read back, or policy interference)"
    elif m1 >> 2 & 1:
        r["mode_verdict"] = "MODE sticks with CHR_EN=0"
    elif m2 >> 2 & 1:
        r["mode_verdict"] = "gate: MODE does not stick with CHR_EN=0, sticks with CHR_EN=1"
    else:
        r["mode_verdict"] = "unexpected: MODE not set even with CHR_EN=1"
    return r


def repair_strup(p):
    say("STRUP_CON0 is not 64: writing 0x0040 before charging may be switched on")
    try:
        p.write(STRUP, 0x0040)
    except StrupFault:
        pass
    return p.read(STRUP) == STRUP_OK


def restore(p):
    """Bring back and verify the stock charging state. Returns True only if verified.
    STRUP_CON0 is checked (and repaired) before every attempt to switch charging on."""
    for _ in range(2):
        try:
            strup_ok = p.read(STRUP) == STRUP_OK or repair_strup(p)
        except Exception as e:  # unconfirmed read/repair: treat STRUP as broken
            say(f"STRUP_CON0 check/repair failed: {type(e).__name__}: {e}")
            strup_ok = False
        if not strup_ok:
            say("STRUP_CON0 NOT repaired: switching charging off (the stock policy may switch it on again)")
            try:
                p.write(CHR0, 0x0001)
            except Exception:
                pass
            return False
        try:
            p.write(CHR0, 0x0019)
            p.write(CHR25, 0x00D4)
            break
        except StrupFault:
            continue
    else:
        return False
    v = m = st = None
    end = time.monotonic() + 30
    while time.monotonic() < end:
        v, m = p.read(CHR0), p.read(CHR25)
        st = p.io.status()
        if v == STOCK_CHR0 and m == STOCK_CHR25 and st == "Charging" and p.read(STRUP) == STRUP_OK:
            say(f"restored: 0xf48=0x{v:x} 0xf7a=0x{m:x} status={st} STRUP={STRUP_OK}")
            return True
        time.sleep(0.5)
    say(f"restore NOT verified: 0xf48={v} 0xf7a={m} status={st}")
    return False


def main(io=None):
    p = Pmic(io)
    say("E1 gate test start")
    st = p.io.status()
    pre = {"strup": p.read(STRUP), "chr0": p.read(CHR0), "chr25": p.read(CHR25), "status": st}
    say(f"preflight {pre}")
    if pre != {"strup": STRUP_OK, "chr0": STOCK_CHR0, "chr25": STOCK_CHR25, "status": "Charging"}:
        say("not in the expected stock charging state; no writes done")
        return 1

    # From here on every exit restores. Signals end the test once; cleanup is not re-entered.
    def stop(signum, _frame):
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        raise Abort(f"signal {signum}")

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, stop)
    rounds, error, ok = [], None, False
    try:
        for i in range(3):
            r = measure_round(p)
            say(f"round {i + 1}: {r}")
            rounds.append(r)
    except Exception as e:  # Abort, StrupFault, OSError, ...
        error = f"{type(e).__name__}: {e}"
    finally:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(s, signal.SIG_IGN)
        try:
            ok = restore(p)
        except Exception as e:
            say(f"restore failed: {type(e).__name__}: {e}")
            ok = False
    if error:
        say(f"ABORTED: {error}")
    evidence = [l for l in p.journal if "store_pmic_access] write" in l or POLICY_RE.search(l)]
    result = {"preflight": pre, "rounds": rounds, "error": error, "restored": ok, "writes": p.writes,
              "evidence_log": evidence}
    try:
        with open(RESULT, "w") as f:
            json.dump(result, f, indent=1)
        say(f"result written to {RESULT}")
    except Exception as e:
        say(f"could not write {RESULT}: {e}")
    if not ok:
        return 3
    return 2 if error else 0


if __name__ == "__main__":
    sys.exit(main())
