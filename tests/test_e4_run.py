"""Host tests for scripts/e4-run.py: judging, rest check, and a simulated run over a fake console."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("e4run", ROOT / "scripts/e4-run.py")
e4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e4)

REGS = [f"{0x0f48 + 2 * i:04x}" for i in range(45)]
HEADER = ("# seq t_ns phase reason kicked full err " + " ".join(REGS) +
          " fg_0cb2 fg_0ccc raw0 raw1 raw2 raw3 vbat_mv isense_mv vbus_mv ntc_mv temp_ok temp_mc adc_err")


def row(seq, phase, f48=0x63, f7a=0xd0, f4c=0x00):
    regs = {"0f48": f48, "0f7a": f7a, "0f4c": f4c}
    cells = [str(seq), str(seq * 10**8), phase, "none", "0", "0", "0"]
    cells += [f"{regs.get(r, 0):04x}" for r in REGS]
    cells += ["0"] * 6 + ["4000", "4010", "5200", "600", "1", "25000", "0"]
    return " ".join(cells)


def state(mode="e4a", phase="done", result="deadman_pass", td0="4010", stop_verified=1, may_be_on=0, det_stuck=0):
    return (f"mode {mode}\ncs 15\ntd0_param_ms 0\nphase {phase}\nresult {result}\nstop_reason deadman\n"
            f"may_be_on {may_be_on}\nstop_verified {stop_verified}\ndet_stuck {det_stuck}\nwdt_off 0\n"
            f"td0_out_ms {td0}\ntd0_out_lo_ms 3990\ntd0_clear_ms 4100\npasses 9\nkicks 1\nio_errors 0\n")


E4A_LOG = "\n".join([HEADER, row(0, "preflight"), row(1, "enable", 0x7b, 0xd4), row(2, "deadman", 0x7b, 0xd4),
                     row(3, "done", 0x63, 0xd0)])
E4B_LOG = "\n".join([HEADER, row(0, "preflight")] + [row(i, "charging", 0x7b, 0xd4, 0xa0) for i in range(1, 5)]
                    + [row(5, "done")])


class Judge(unittest.TestCase):
    def test_e4a_pass(self):
        self.assertEqual(e4.judge("e4a", state(), E4A_LOG), (True, []))

    def test_e4a_fails(self):
        cases = [
            (state(result="deadman_fail window"), "result"),
            (state(td0="6001"), "td0_out_ms"),
            (state(td0="unknown"), "td0_out_ms"),
            (state(stop_verified=0), "stop_verified"),
            (state(may_be_on=1), "may_be_on"),
            (state(det_stuck=1), "det_stuck"),
            (state(phase="deadman"), "phase"),
            (state(mode="e4b"), "mode"),
        ]
        for text, word in cases:
            ok, why = e4.judge("e4a", text, E4A_LOG)
            self.assertFalse(ok, text)
            self.assertTrue(any(word in w for w in why), (word, why))

    def test_e4a_needs_gates_and_mode_seen_together(self):
        only_gates = "\n".join([HEADER, row(0, "enable", 0x7b, 0xd0), row(1, "deadman", 0x63, 0xd4), row(2, "done")])
        ok, why = e4.judge("e4a", state(), only_gates)
        self.assertFalse(ok)
        self.assertIn("never shows", why[0])
        self.assertFalse(e4.judge("e4a", state(), "")[0])

    def test_e4b(self):
        ok, why = e4.judge("e4b", state("e4b", result="complete"), E4B_LOG)
        self.assertTrue(ok, why)
        bad_det = E4B_LOG.replace(row(3, "charging", 0x7b, 0xd4, 0xa0), row(3, "charging", 0x7b, 0xd4, 0x20))
        ok, why = e4.judge("e4b", state("e4b", result="complete"), bad_det)
        self.assertFalse(ok)
        self.assertIn("CC_DET", why[0])
        # CS_DET toggles while regulating [HW E4b]: once is enough, never is a FAIL
        toggling = E4B_LOG.replace(row(3, "charging", 0x7b, 0xd4, 0xa0), row(3, "charging", 0x7b, 0xd4, 0x8e))
        self.assertTrue(e4.judge("e4b", state("e4b", result="complete"), toggling)[0])
        no_cs = "\n".join([HEADER, row(0, "preflight")] + [row(i, "charging", 0x7b, 0xd4, 0x8e) for i in range(1, 5)])
        ok, why = e4.judge("e4b", state("e4b", result="complete"), no_cs)
        self.assertFalse(ok)
        self.assertIn("CS_DET never", why[-1])
        bad_mode = E4B_LOG.replace(row(2, "charging", 0x7b, 0xd4, 0xa0), row(2, "charging", 0x7b, 0x90, 0xa0))
        self.assertFalse(e4.judge("e4b", state("e4b", result="complete"), bad_mode)[0])
        self.assertFalse(e4.judge("e4b", state("e4b", result="aborted time"), E4B_LOG)[0])


class Rest(unittest.TestCase):
    def samples(self, status="Cmd discharging", fg=1170, mv=4000, temp=250, n=8):
        return [(i * 10.0, status, fg, mv, temp) for i in range(n)]

    def test_ok(self):
        self.assertEqual(e4.rest_ok(self.samples()), (True, "ok", 4000))
        self.assertTrue(e4.rest_ok(self.samples(status="Discharging"))[0])

    def test_refusals(self):
        self.assertFalse(e4.rest_ok(self.samples(n=6))[0])          # 50 s only
        self.assertFalse(e4.rest_ok(self.samples(status="Charging"))[0])
        self.assertFalse(e4.rest_ok(self.samples(fg=-300))[0])      # current into the battery
        self.assertFalse(e4.rest_ok(self.samples(mv=4101))[0])
        self.assertFalse(e4.rest_ok(self.samples(mv=3799))[0])
        self.assertFalse(e4.rest_ok(self.samples(temp=401))[0])
        s = self.samples()
        s[3] = (30.0, "Full", -50, 4000, 250)                        # one charging sample
        self.assertFalse(e4.rest_ok(s)[0])

    def test_td0_from_e4a(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "v.json"
            p.write_text(json.dumps({"mode": "e4a", "passed": True, "state": {"td0_out_ms": "4010"}}))
            self.assertEqual(e4.td0_for_e4b(p), 4010)
            p.write_text(json.dumps({"mode": "e4a", "passed": False, "state": {"td0_out_ms": "4010"}}))
            with self.assertRaises(e4.Stop):
                e4.td0_for_e4b(p)
            p.write_text(json.dumps({"mode": "e4a", "passed": True, "state": {"td0_out_ms": "1500"}}))
            with self.assertRaises(e4.Stop):
                e4.td0_for_e4b(p)


class FakeSerial:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)


class FakePhone:
    """Answers the runner's console commands like the RAM-booted 7.0 would."""

    def __init__(self, ins_rc=0, final_state=None, polls_until_done=2, fail_on=None):
        self.s = FakeSerial()
        self.cmds = []
        self.ins_rc, self.polls_left = ins_rc, polls_until_done
        self.final_state = final_state or state()
        self.fail_on = fail_on

    def run(self, cmd, timeout=30):
        self.cmds.append(cmd)
        if self.fail_on and self.fail_on in cmd:
            raise SystemExit(f"no end marker for: {cmd[:80]}")
        if "uname -r" in cmd:
            return 0, "I14_IDENTITY_OK"
        if "MODULES_EMPTY" in cmd:
            return 0, "MODULES_EMPTY\nSTORAGE=26:0"
        if "i14-trial-reboot.pid" in cmd:
            return 0, "[  30.1] I14_TRIAL_REBOOT_CANCELLED"
        if "e34.sh prep " in cmd:
            cap = cmd.split("prep ")[1].split()[0]
            return 0, "\n".join([f"I14_PWRAP_STATUS sample={i} {e4.PWRAP_CLEAN} WDT_SRC=x" for i in [0, 1, 2] * 3]
                                + [f"I14_E34 PREP_OK cap={cap} keeper=1 cap_pid=2 panic_on_oops=1 panic=5 kexec_crash_loaded=0"])
        if " ins " in cmd:
            return self.ins_rc, f"I14_E34 INS rc={self.ins_rc}"
        if cmd.startswith("grep -E"):
            self.polls_left -= 1
            return 0, self.final_state if self.polls_left <= 0 else state(phase="deadman", result="running")
        if "i14-e4/state" in cmd:
            return 0, self.final_state if self.polls_left <= 0 else state(phase="deadman", result="running")
        if "i14-e4/log" in cmd:
            return 0, E4A_LOG
        return 0, ""


class SimulatedRun(unittest.TestCase):
    fastboot = True

    def run_with(self, phone):
        return self.run_with_mode(phone, "e4a")

    def run_with_mode(self, phone, mode, run_s=None):
        pushed = []
        fake_mod = type("M", (), {"Console": lambda port: phone,
                                  "push": staticmethod(lambda con, local, remote: pushed.append(remote))})
        saved = {k: getattr(e4, k) for k in ("host_checks", "v7_rest_vbat", "v7", "wait_port", "load_console",
                                             "wait_v7", "wait_fastboot", "subprocess")}
        real_sleep = e4.time.sleep
        rb = type("R", (), {"returncode": 0, "stdout": "OKAY", "stderr": ""})
        e4.host_checks = lambda: None
        e4.v7_rest_vbat = lambda log: 4000
        e4.v7 = lambda cmd, timeout=30: (0, "")
        e4.wait_port = lambda s: "/dev/cu.fake"
        e4.load_console = lambda: fake_mod
        e4.wait_v7 = lambda s, log: False
        e4.wait_fastboot = lambda s: self.fastboot
        e4.subprocess = type("S", (), {"run": staticmethod(lambda *a, **k: rb), "TimeoutExpired": Exception})
        e4.time.sleep = lambda s: None
        try:
            with tempfile.TemporaryDirectory() as d:
                result = e4.run(mode, pathlib.Path(d) / "out", run_s)
                files = {p.name for p in (pathlib.Path(d) / "out").iterdir()}
        finally:
            for k, v in saved.items():
                setattr(e4, k, v)
            e4.time.sleep = real_sleep
        return result, pushed, files

    def test_pass_path(self):
        phone = FakePhone()
        result, pushed, files = self.run_with(phone)
        self.assertTrue(result["passed"], result)
        self.assertEqual(pushed, ["/run/e34/e34.sh", "/run/e34/i14-e4-charge.ko", "/run/e34/bin/insmod"])
        ins = [c for c in phone.cmds if " ins " in c]
        # our insmod as an ash function: the trial busybox insmod drops module options (E4a attempt 2)
        self.assertEqual(ins, ["chmod 700 /run/e34/bin/insmod && ( set -- ins /run/e34/i14-e4-charge.ko "
                               'mode=e4a cs=15; insmod() { /run/e34/bin/insmod "$@"; }; . /run/e34/e34.sh )'])
        self.assertEqual(phone.s.written[-1], b"reboot -f\n")
        self.assertTrue({"state.txt", "log.txt", "dmesg.txt", "verdict.json", "prep.txt"} <= files, files)

    def test_insmod_error_stops_and_reboots(self):
        phone = FakePhone(ins_rc=1)
        result, _, files = self.run_with(phone)
        self.assertFalse(result["passed"])
        self.assertIn("insmod rc=1", result["reasons"][0])
        self.assertFalse(any("i14-e4/state" in c for c in phone.cmds))
        self.assertEqual(phone.s.written[-1], b"reboot -f\n")

    def test_lost_console_after_insmod_stops_then_reboots(self):
        phone = FakePhone(fail_on="grep -E")
        result, _, _ = self.run_with(phone)
        self.assertFalse(result["passed"])
        self.assertEqual(phone.s.written, [b"echo 1 >/sys/kernel/debug/i14-e4/stop\n", b"reboot -f\n"])

    def test_bad_prep_never_loads(self):
        phone = FakePhone()
        real = phone.run
        phone.run = lambda cmd, timeout=30: (0, "I14_E34 PREP_KEXEC_CRASH_NOT_0") if "prep 600" in cmd else real(cmd, timeout)
        result, _, _ = self.run_with(phone)
        self.assertFalse(result["passed"])
        self.assertFalse(any(" ins " in c for c in phone.cmds))

    def test_never_done_requests_stop(self):
        phone = FakePhone(polls_until_done=10**6)
        clock = iter(range(0, 10**6, 2))
        saved = e4.time.time
        e4.time.time = lambda: next(clock)
        try:
            result, _, _ = self.run_with(phone)
        finally:
            e4.time.time = saved
        self.assertIn("echo 1 >/sys/kernel/debug/i14-e4/stop", phone.cmds)
        self.assertFalse(result["passed"])


class Fixes(SimulatedRun):
    def test_no_fastboot_means_no_ram_boot_and_no_console(self):
        self.fastboot = False
        phone = FakePhone()
        result, _, _ = self.run_with(phone)
        self.assertFalse(result["passed"])
        self.assertIn("no fastboot", result["reasons"][0])
        self.assertEqual(phone.cmds, [])
        self.assertEqual(phone.s.written, [])

    def test_identity_uses_the_full_predicate(self):
        phone = FakePhone()
        self.run_with(phone)
        self.assertEqual(phone.cmds[0], e4.IDENTITY)
        for part in ("uname -m", "/proc/device-tree/model", "/proc/device-tree/compatible"):
            self.assertIn(part, e4.IDENTITY)

    def test_e4b_auto_pass_is_pending(self):
        phone = FakePhone(final_state=state("e4b", result="complete"))
        real = phone.run
        phone.run = lambda cmd, timeout=30: (0, E4B_LOG) if "i14-e4/log" in cmd else real(cmd, timeout)
        saved = e4.td0_for_e4b
        e4.td0_for_e4b = lambda p: 4010
        try:
            result, _, _ = self.run_with_mode(phone, "e4b")
        finally:
            e4.td0_for_e4b = saved
        self.assertFalse(result["passed"])
        self.assertTrue(result["auto_passed"])
        self.assertIn(" ins /run/e34/i14-e4-charge.ko mode=e4b cs=14 td0_ms=4010", [c for c in phone.cmds if " ins " in c][0])


    def test_e5_uses_cap_3600_run_s_and_merges_the_log(self):
        phone = FakePhone(final_state=state("e5", result="complete"))
        phone.polls_left = 3
        real, fetch = phone.run, iter([E4B_LOG.split("\n")[0] + "\n" + "\n".join(E4B_LOG.split("\n")[1:3]), E4B_LOG])
        phone.run = lambda cmd, timeout=30: (0, next(fetch, E4B_LOG)) if "i14-e4/log" in cmd else real(cmd, timeout)
        saved = (e4.td0_for_e4b, e4.LOG_EVERY)
        e4.td0_for_e4b, e4.LOG_EVERY = (lambda p: 4010), 0
        try:
            result, _, files = self.run_with_mode(phone, "e5", 1800)
        finally:
            e4.td0_for_e4b, e4.LOG_EVERY = saved
        self.assertTrue(result["auto_passed"], result)
        self.assertIn("e34.sh prep 3600", " ".join(phone.cmds))
        self.assertIn(" ins /run/e34/i14-e4-charge.ko mode=e5 cs=14 td0_ms=4010 run_s=1800",
                      [c for c in phone.cmds if " ins " in c][0])
        self.assertIn("log-all.txt", files)
        self.assertIn("charge summary", result["charge_summary"])

    def test_judge_rejects_a_log_with_a_gap(self):
        gap = E4B_LOG.replace(row(2, "charging", 0x7b, 0xd4, 0xa0) + "\n", "")
        ok, why = e4.judge("e4b", state("e4b", result="complete"), gap)
        self.assertFalse(ok)
        self.assertIn("log incomplete", " ".join(why))

    def test_judge_rejects_a_log_without_its_end(self):
        head = E4B_LOG.split("\n")
        ok, why = e4.judge("e4b", state("e4b", result="complete"), "\n".join(head[:4]))
        self.assertFalse(ok)
        self.assertIn("does not end in a done pass", " ".join(why))

    def test_merge_logs_orders_by_seq_and_drops_duplicates(self):
        head = "# seq t_ns phase"
        merged = e4.merge_logs([f"{head}\n1 10 a\n2 20 b\n", f"{head}\n2 20 b\n3 30 c\n"])
        self.assertEqual(merged, f"{head}\n1 10 a\n2 20 b\n3 30 c\n")
        self.assertEqual(e4.merge_logs([""]), "")

    def test_prep_cap(self):
        text = "\n".join([f"I14_PWRAP_STATUS sample=0 {e4.PWRAP_CLEAN}"] * 3 + ["I14_E34 PREP_OK cap=3600 kexec_crash_loaded=0"])
        self.assertTrue(e4.prep_ok(0, text, 3600))
        self.assertFalse(e4.prep_ok(0, text, 600))

    def test_new_e4a_attempt_drops_an_old_pass(self):
        import sys
        saved = (e4.ART, e4.run, sys.argv)
        with tempfile.TemporaryDirectory() as d:
            e4.ART = pathlib.Path(d)
            (e4.ART / "e4a").mkdir()
            (e4.ART / "e4a/verdict.json").write_text('{"mode": "e4a", "passed": true}')
            e4.run = lambda mode, out, run_s=None: {"passed": False}
            sys.argv = ["e4-run.py", "e4a", "--go"]
            try:
                self.assertEqual(e4.main(), 1)
                self.assertFalse((e4.ART / "e4a/verdict.json").exists())
            finally:
                e4.ART, e4.run, sys.argv = saved


    def test_cli_judge_e4b_is_pending(self):
        import subprocess, sys
        with tempfile.TemporaryDirectory() as d:
            st, lg = pathlib.Path(d) / "state", pathlib.Path(d) / "log"
            st.write_text(state("e4b", result="complete"))
            lg.write_text(E4B_LOG)
            r = subprocess.run([sys.executable, str(ROOT / "scripts/e4-run.py"), "judge", "e4b", str(st), str(lg)],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("pending", r.stdout)


if __name__ == "__main__":
    unittest.main()


class Prep(unittest.TestCase):
    REAL = pathlib.Path(__file__).resolve().parents[1] / "artifacts/phase4-e3e4-20261102/e4a/20261103T161047Z/prep.txt"

    def test_nine_clean_samples_pass(self):  # 3 calls x 3 samples, as on hardware 2026-11-03
        text = "\n".join([f"I14_PWRAP_STATUS sample={i} {e4.PWRAP_CLEAN} WDT_SRC=x" for i in [0, 1, 2] * 3]
                         + ["I14_E34 PREP_OK cap=600 keeper=1 cap_pid=2 panic_on_oops=1 panic=5 kexec_crash_loaded=0"])
        self.assertTrue(e4.prep_ok(0, text))

    def test_one_dirty_sample_fails(self):
        text = "\n".join([f"I14_PWRAP_STATUS sample=0 {e4.PWRAP_CLEAN}"] * 8
                         + ["I14_PWRAP_STATUS sample=2 WDT_FLG=00000001 INT_RAW=00000000 INT_FLG=00000000",
                            "I14_E34 PREP_OK cap=600 kexec_crash_loaded=0"])
        self.assertFalse(e4.prep_ok(0, text))

    def test_no_samples_or_rc_fails(self):
        self.assertFalse(e4.prep_ok(0, "I14_E34 PREP_OK cap=600 kexec_crash_loaded=0"))
        self.assertFalse(e4.prep_ok(1, "I14_E34 PREP_OK cap=600 kexec_crash_loaded=0"))

    @unittest.skipUnless(REAL.exists(), "hardware artifact not present")
    def test_real_hardware_prep_passes(self):
        self.assertTrue(e4.prep_ok(0, self.REAL.read_text()))

