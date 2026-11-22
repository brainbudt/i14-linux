"""Host tests for scripts/e1-gate-test.py against a simulated MT6328 + stock charger policy."""
import importlib.util
import json
import os
import pathlib
import tempfile
import time
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load(tmp):
    os.environ["E1_LOG"] = str(pathlib.Path(tmp) / "log")
    os.environ["E1_RESULT"] = str(pathlib.Path(tmp) / "result.json")
    spec = importlib.util.spec_from_file_location("e1", ROOT / "scripts/e1-gate-test.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # a private time namespace: patching mod.time.sleep would replace time.sleep for every later test
    mod.time = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith("_")})
    mod.time.sleep = lambda s: None
    return mod


_SIGNALS = {}


def setUpModule():
    import signal
    _SIGNALS.update({s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)})


def tearDownModule():
    # e1.main() leaves these ignored on purpose (the real script exits right after); restore them so
    # later tests and their child processes (which inherit SIG_IGN) are not affected
    import signal
    for s, handler in _SIGNALS.items():
        signal.signal(s, handler)


class FakeIO:
    """pmic_access semantics of pmic.c plus a stock policy that re-enables charging
    every `period` node operations after it was switched off."""

    RO_CHR0 = 0x62  # CHR_LDO_DET, CHRDET, LV_DET as read while charging (0x7b & ~0x19)

    def __init__(self, gate=True, period=60, strup_fault_on=None, status="Charging"):
        self.regs = {0x0000: 0x40, 0x0F48: 0x7B, 0x0F7A: 0xD4}
        self.gate, self.period, self.ops, self.last = gate, period, 0, 0
        self.strup_fault_on, self.st, self.sent = strup_fault_on, status, []

    def _policy(self):
        self.ops += 1
        if self.ops % self.period == 0:
            self.regs[0x0F48], self.regs[0x0F7A] = 0x7B, 0xD4

    def _apply(self, reg, val):
        if reg == 0x0F48:
            v = val & 0x19
            if self.gate and not v & 0x10:
                v &= ~0x08  # CSDAC_EN does not stick with CHR_EN=0
            self.regs[reg] = v | self.RO_CHR0
            if self.strup_fault_on == val:
                self.regs[0x0000] = 0x41
        elif reg == 0x0F7A:
            v = val & 0xD7
            if self.gate and not self.regs[0x0F48] & 0x10:
                v &= ~0x04
            self.regs[reg] = v
        else:
            self.regs[reg] = val

    def send(self, data):
        self.sent.append(data)
        self._policy()
        text = data.decode()
        if len(data) > 5:
            a, v = (int(x, 16) for x in text.split(" "))
            self._apply(a, v)
            return [f"[store_pmic_access] write PMU reg 0x{a:x} with value 0x{v:x} !"]
        a = int(text, 16)
        self.last = self.regs.get(a, 0)
        return [f"[store_pmic_access] read PMU reg 0x{a:x} with value 0x{self.last:x} !"]

    def cat(self):
        return str(self.last)

    def status(self):
        return self.st


class E1Gate(unittest.TestCase):
    def run_e1(self, io):
        with tempfile.TemporaryDirectory() as d:
            e1 = load(d)
            code = e1.main(io)
            result = json.loads((pathlib.Path(d) / "result.json").read_text()) if code != 1 else None
        return code, result

    def test_gate_model_is_detected(self):
        code, r = self.run_e1(FakeIO(gate=True))
        self.assertEqual(code, 0)
        self.assertEqual(len(r["rounds"]), 3)
        for rnd in r["rounds"]:
            self.assertTrue(rnd["verdict"].startswith("gate:"), rnd)

    def test_no_gate_is_detected(self):
        code, r = self.run_e1(FakeIO(gate=False))
        self.assertEqual(code, 0)
        for rnd in r["rounds"]:
            self.assertTrue(rnd["verdict"].startswith("no gate"), rnd)

    def policy_after(self, trigger):
        """Fake where the stock policy re-enables charging right after a given write."""
        io = FakeIO(gate=True, period=10**9)
        send = io.send
        seen = []

        def hook(data):
            out = send(data)
            seen.append(data)
            # the wait loop (two 0xf48 reads in a row) and the trigger write both get a policy round
            if data == trigger or seen[-2:] == [b"0f48", b"0f48"]:
                io.regs[0x0F48], io.regs[0x0F7A] = 0x7B, 0xD4
                seen.clear()
            return out
        io.send = hook
        return io

    def test_policy_after_csdac_write_is_inconclusive(self):
        code, r = self.run_e1(self.policy_after(b"0f48 0009"))
        self.assertTrue(r["rounds"] and all(x["verdict"].startswith("inconclusive") for x in r["rounds"]), r)

    def test_policy_after_mode_write_is_inconclusive(self):
        code, r = self.run_e1(self.policy_after(b"0f7a 00d4"))
        self.assertTrue(r["rounds"] and all(x["mode_verdict"].startswith("inconclusive") for x in r["rounds"]), r)

    def disable_after_csdac(self, every):
        """No gate in the chip, but the stock disable path (CSDAC_EN=0, then CHR_EN=0) runs
        right after the CSDAC write and logs like the real driver."""
        io = FakeIO(gate=False, period=10**9)
        send = io.send
        seen, state = [], {"n": 0}

        def hook(data):
            out = send(data)
            seen.append(data)
            if seen[-2:] == [b"0f48", b"0f48"]:  # wait loop: policy turns charging on
                io.regs[0x0F48], io.regs[0x0F7A] = 0x7B, 0xD4
                seen.clear()
            if data == b"0f48 0009" and (every or state["n"] == 0):
                state["n"] += 1
                io.regs[0x0F48] = 0x63
                out = out + ["[BATTERY] pchr_turn_on_charging(), enable =0 "]
            return out
        io.send = hook
        return io

    def test_logged_disable_between_write_and_read_is_inconclusive(self):
        code, r = self.run_e1(self.disable_after_csdac(every=True))
        verdicts = [x["verdict"] for x in r["rounds"]]
        self.assertTrue(verdicts and all(v.startswith("inconclusive") for v in verdicts), verdicts)
        self.assertTrue(r["rounds"][0]["policy_records_csdac_window"])

    def test_one_off_disable_cannot_fake_a_gate(self):
        code, r = self.run_e1(self.disable_after_csdac(every=False))
        self.assertTrue(r["rounds"][0]["verdict"].startswith("no gate"), r["rounds"][0])

    def test_only_allowed_writes_are_sent(self):
        io = FakeIO(gate=True)
        self.run_e1(io)
        writes = {d for d in io.sent if len(d) > 5}
        self.assertTrue(writes <= {b"0f48 0001", b"0f48 0009", b"0f48 0019", b"0f7a 00d0", b"0f7a 00d4"}, writes)

    def test_strup_fault_repairs_before_restoring_charging(self):
        io = FakeIO(gate=True, strup_fault_on=0x0009)
        code, r = self.run_e1(io)
        self.assertEqual(code, 2)
        self.assertIn("STRUP_CON0", r["error"])
        repair = io.sent.index(b"0000 0040")
        self.assertLess(repair, len(io.sent) - 1 - io.sent[::-1].index(b"0f48 0019"))
        self.assertEqual(io.regs[0x0000], 0x40)

    def test_unverified_restore_fails_loudly(self):
        code, r = self.run_e1(FakeIO(gate=True, status="Charging"))
        self.assertEqual(code, 0)
        io = FakeIO(gate=True)
        io.status = lambda: "Charging" if io.ops < 5 else "Not charging"
        code, r = self.run_e1(io)
        self.assertEqual(code, 3)
        self.assertFalse(r["restored"])

    def test_mode_gate_needs_verified_baseline(self):
        code, r = self.run_e1(FakeIO(gate=True))
        self.assertTrue(all(rnd["mode_verdict"].startswith("gate: MODE") for rnd in r["rounds"]), r["rounds"])
        io = FakeIO(gate=True)
        io.ignore_f7a = True  # PMIC ignores MODE writes: MODE stays 1 -> baseline not verified
        apply = io._apply
        io._apply = lambda reg, val: None if reg == 0x0F7A and val == 0xD0 else apply(reg, val)
        code, r = self.run_e1(io)
        self.assertTrue(all(rnd["mode_verdict"].startswith("inconclusive") for rnd in r["rounds"]), r["rounds"])

    def test_lost_write_ack_still_checks_strup_before_restore(self):
        io = FakeIO(gate=True)
        send = io.send
        state = {"n": 0}

        def flaky(data):
            out = send(data)
            if data == b"0f48 0009" and state["n"] == 0:
                state["n"] = 1
                io.regs[0x0000] = 0x41  # corrupted and the write record went missing
                return []
            return out
        io.send = flaky
        code, r = self.run_e1(io)
        self.assertEqual(code, 2)
        first_restore = len(io.sent) - 1 - io.sent[::-1].index(b"0f48 0019")
        self.assertIn(b"0000 0040", io.sent[:first_restore])
        self.assertEqual(io.regs[0x0000], 0x40)

    def test_oserror_during_round_restores(self):
        io = FakeIO(gate=True)
        send = io.send
        state = {"n": 0}

        def boom(data):
            state["n"] += 1
            if state["n"] == 30:
                raise OSError(5, "I/O error")
            return send(data)
        io.send = boom
        code, r = self.run_e1(io)
        self.assertEqual(code, 2)
        self.assertTrue(r["restored"])
        self.assertIn("OSError", r["error"])

    def test_oserror_during_cleanup_exits_3(self):
        io = FakeIO(gate=True)
        send = io.send

        def dead_after_round(data):
            if data == b"0f48 0019" and io.cleanup:
                raise OSError(5, "I/O error")
            return send(data)
        io.cleanup = False
        io.send = dead_after_round
        with tempfile.TemporaryDirectory() as d:
            e1 = load(d)
            real = e1.measure_round

            def one_round_then_fail(p):
                io.cleanup = True
                raise e1.Abort("stop")
            e1.measure_round = one_round_then_fail
            self.assertEqual(e1.main(io), 3)

    def test_failed_strup_repair_switches_charging_off(self):
        io = FakeIO(gate=True, strup_fault_on=0x0009)
        send = io.send

        def no_repair(data):
            if data == b"0000 0040":
                io.sent.append(data)
                return []  # unacknowledged and ineffective
            return send(data)
        io.send = no_repair
        code, r = self.run_e1(io)
        self.assertEqual(code, 3)
        self.assertEqual([d for d in io.sent if len(d) > 5][-1], b"0f48 0001")  # last write: charging off
        self.assertEqual(io.regs[0x0F48] & 0x10, 0)
        self.assertNotIn(b"0f48 0019", io.sent[io.sent.index(b"0000 0040"):])

    def test_wrong_start_state_writes_nothing(self):
        io = FakeIO(gate=True)
        io.regs[0x0F48] = 0x63
        code, _ = self.run_e1(io)
        self.assertEqual(code, 1)
        self.assertFalse([d for d in io.sent if len(d) > 5])


if __name__ == "__main__":
    unittest.main()
