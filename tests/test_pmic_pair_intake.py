"""Offline intake regressions; fixtures are local, synthetic evidence only."""
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).absolute().parents[1]
SCRIPT = ROOT / "scripts/i14-pmic-pair-intake.py"
ART = ROOT / "artifacts/pmic-pair-intake-prep-20261102"
NOW = datetime.datetime(2026, 11, 2, 10, tzinfo=datetime.timezone.utc)
BOOT = "4a3dc960-87e8-4829-987e-717b0bb0d717"
OTHER_BOOT = "11111111-2222-3333-4444-555555555555"
MODEL = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a"
COMPAT = "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a"
WORKER = "8103f7ffea390e6e8ca60cabafad712722ab893ba02c39333ba003807b60a802"
RO = {n: "1" for n in ["mmcblk0", "mmcblk0boot0", "mmcblk0boot1"]
      + ["mmcblk0p%d" % n for n in range(1, 24)]}


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def fixture():
    """Literal complete worker-profile copy; no worker is imported."""
    ident = ["7.0.10-i14-bringup", "aarch64", "Uid: 0 0 0 0",
             MODEL + "  /proc/device-tree/model", COMPAT + "  /proc/device-tree/compatible"]
    mounts = ["rootfs / rootfs rw 0 0", "proc /proc proc rw 0 0"]
    stat = ["3 0 24 250 0 0 0 0 0 244 250"]
    sections = {
        "identity": ident, "boot_id": [BOOT], "uptime": ["113.12 110.90"],
        "kernel_log": ["<6>Linux boot", "@@I14_DMESG_RC 0"], "cmdline": ["console=ttyS0"],
        "mounts": mounts, "chosen_bootargs": ["console=ttyS0"],
        "chosen_bootargs_override": ["I14_ABSENT"], "usb_startup_log": ["I14_ABSENT"],
        "storage_startup_log": ["I14_ABSENT"], "usb_snapshot_log": ["I14_ABSENT"],
        "modules": [], "mmcblk0_size": ["30535680"],
        "mmcblk_ro": [n + " " + v for n, v in RO.items()], "mmcblk0_stat": stat,
        "cached_i14_charger": ["I14_ABSENT"], "cached_battery_sample": ["I14_ABSENT"],
        "maxpower": ["2"], "udc_state": ["configured"], "usb0_carrier": ["1"],
        "boot_id_end": [BOOT], "uptime_end": ["113.22 111.00"],
    }
    result = {"verdict": "COMPLETE_POWER_EVIDENCE", "transport_rc": 0, "exit_code": 0,
              "attempted": True, "charging_blocked": True, "charging_status": "UNKNOWN",
              "ready_for_charging": False, "stop_reasons": [],
              "framing": {"I14_IDENTITY_OK": 1, "I14_STOP_IDENTITY": 0,
                          "I14_CACHED_CAPTURE_DONE": 1, "I14_CHECK_END": 1},
              "capture_complete": True, "identity": {"ok": True, "kernel": ident[0], "device_lines": ident},
              "boot": {"boot_id": BOOT, "end_boot_id": BOOT, "same_boot": True,
                       "uptime": sections["uptime"], "end_uptime": sections["uptime_end"], "modules": []},
              "storage": {"capacity_sectors": 30535680, "capacity_ok": True, "ro_node_count": 26,
                          "ro_nodes": dict(RO), "names_ok": True, "all_ro": True, "stat": stat,
                          "sectors_written": 0, "mounts": mounts}}
    envelope = {"schema": 1, "captured_at": "2026-11-02T09:59:30Z", "worker_sha256": WORKER,
                "raw_sha256": "", "result_sha256": "", "previous_boot_id": OTHER_BOOT,
                "once_status": "CONSUMED", "historical_pmic_outcome": "UNKNOWN",
                "global_block": "INTEGRITY_BLOCK"}
    return envelope, result, sections


def wire(sections):
    lines = ["# command echo is inert evidence"]
    for name, body in sections.items():
        lines.extend(["@@I14_BEGIN " + name] + body + ["@@I14_END " + name])
        if name == "identity":
            lines.append("I14_IDENTITY_OK")
    return ("\r\n".join(lines + ["I14_CACHED_CAPTURE_DONE", "I14_CHECK_END", ""]).encode())


def copy_bytes(envelope, result, sections):
    raw, result_bytes = wire(sections), encoded(result)
    envelope = dict(envelope, raw_sha256=hashlib.sha256(raw).hexdigest(),
                    result_sha256=hashlib.sha256(result_bytes).hexdigest())
    return encoded(envelope), result_bytes, raw


AUDIT_ACTIVE = False


def audit(event, args):
    if not AUDIT_ACTIVE:
        return
    if event.startswith(("socket.", "subprocess.", "ctypes.", "os.exec", "os.spawn")) \
            or event in {"os.system", "os.kill", "fcntl.flock", "os.mkdir", "os.remove", "os.rename"}:
        raise AssertionError("forbidden capability: " + event)
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        raise AssertionError("intake tried to write a file")


sys.addaudithook(audit)


class IntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SCRIPT.exists():
            spec = importlib.util.spec_from_file_location("pair_intake", SCRIPT)
            cls.m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.m)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fixture-", dir=ART)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def validate(self, change=None):
        envelope, result, sections = fixture()
        if change:
            change(envelope, result, sections)
        return self.m.validate_copy(*copy_bytes(envelope, result, sections), now=NOW)

    def assert_closed(self, result):
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIs(result["dispatch_allowed"], False)
        self.assertIs(result["first_read_permission"], False)
        self.assertEqual(result["custody"], "NOT_ACQUIRED")
        self.assertEqual(result["charging"], "UNKNOWN")

    def test_current_package_refuses_before_any_action(self):
        self.assertTrue(SCRIPT.is_file(), "offline intake checker is missing")
        spec = importlib.util.spec_from_file_location("pair_intake", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = module.main([])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["dispatch_allowed"])
        self.assertFalse(result["first_read_permission"])
        self.assertEqual(result["custody"], "NOT_ACQUIRED")
        self.assertEqual(result["charging"], "UNKNOWN")
        self.assertEqual(result["package"]["native_module"], "UNBUILT")
        self.assertEqual(result["package"]["read_qualified"], 0)

    def test_complete_copy_is_local_evidence_and_new_ap_boot_is_not_pmic_reset(self):
        baseline = self.validate()
        self.assertEqual(baseline["status"], "VALID_LOCAL_COPY")
        self.assertIs(baseline["new_ap_boot"], True)
        self.assertEqual(baseline["pmic_reset"], "UNKNOWN")
        self.assertEqual(baseline["once_status"], "CONSUMED")
        self.assert_closed(self.m.decide({"integrity": "MATCH", "native_module": "BUILT",
                                          "read_qualified": 1}, baseline))

    def test_identity_boot_storage_and_receipt_contradictions_are_unknown(self):
        changes = {
            "kernel": lambda e, r, s: s["identity"].__setitem__(0, "other-kernel"),
            "model": lambda e, r, s: s["identity"].__setitem__(3, "0" * 64 + "  /proc/device-tree/model"),
            "compatible": lambda e, r, s: s["identity"].__setitem__(4, "0" * 64 + "  /proc/device-tree/compatible"),
            "architecture": lambda e, r, s: s["identity"].__setitem__(1, "armv7l"),
            "root": lambda e, r, s: s["identity"].__setitem__(2, "Uid: 0 1000 0 0"),
            "changed boot": lambda e, r, s: s.__setitem__("boot_id_end", [OTHER_BOOT]),
            "missing end": lambda e, r, s: s.pop("boot_id_end"),
            "regressed uptime": lambda e, r, s: s.__setitem__("uptime_end", ["1.00 0.00"]),
            "capacity": lambda e, r, s: s.__setitem__("mmcblk0_size", ["1"]),
            "25 RO nodes": lambda e, r, s: s["mmcblk_ro"].pop(),
            "writable node": lambda e, r, s: s["mmcblk_ro"].__setitem__(0, "mmcblk0 0"),
            "duplicate RO node": lambda e, r, s: s["mmcblk_ro"].append("mmcblk0 1"),
            "writes": lambda e, r, s: s.__setitem__("mmcblk0_stat", ["3 0 24 250 1 0 8 0 0 244 250"]),
            "block mount": lambda e, r, s: s["mounts"].append("/dev/mmcblk0p1 /data ext4 rw 0 0"),
            "claimed complete": lambda e, r, s: r.__setitem__("capture_complete", False),
            "bool is not rc": lambda e, r, s: r.__setitem__("transport_rc", False),
            "bool is not capacity": lambda e, r, s: r["storage"].__setitem__("capacity_sectors", True),
            "source binding": lambda e, r, s: e.__setitem__("worker_sha256", "0" * 64),
            "stale": lambda e, r, s: e.__setitem__("captured_at", "2026-11-02T09:54:59Z"),
            "future": lambda e, r, s: e.__setitem__("captured_at", "2026-11-02T10:00:01Z"),
            "cleared once": lambda e, r, s: e.__setitem__("once_status", "UNUSED"),
            "cleared history": lambda e, r, s: e.__setitem__("historical_pmic_outcome", "RESET"),
            "cleared block": lambda e, r, s: e.__setitem__("global_block", "CLEAR"),
            "endpoint injection": lambda e, r, s: e.__setitem__("endpoint", "$(touch bad)"),
            "partial result": lambda e, r, s: r.pop("boot"),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                result = self.validate(change)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertTrue(result["reasons"])

    def test_unknown_once_is_retained_even_in_complete_copy(self):
        result = self.validate(lambda e, r, s: e.__setitem__("once_status", "UNKNOWN"))
        self.assertEqual(result["once_status"], "UNKNOWN")
        self.assert_closed(self.m.decide(self.m.inspect_package(ROOT), result))

    def test_contradictory_worker_completion_or_charging_claims_are_unknown(self):
        changes = {
            "missing framing": lambda e, r, s: r.pop("framing"),
            "wrong frame count": lambda e, r, s: r["framing"].__setitem__("I14_CHECK_END", 0),
            "bool frame count": lambda e, r, s: r["framing"].__setitem__("I14_CHECK_END", True),
            "contradictory stop": lambda e, r, s: r["framing"].__setitem__("I14_STOP_IDENTITY", 1),
            "extra frame claim": lambda e, r, s: r["framing"].__setitem__("UNEXPECTED", 1),
            "no attempt": lambda e, r, s: r.__setitem__("attempted", False),
            "claimed charging": lambda e, r, s: r.__setitem__("charging_status", "CHARGING"),
            "claimed ready": lambda e, r, s: r.__setitem__("ready_for_charging", True),
            "claimed unblocked": lambda e, r, s: r.__setitem__("charging_blocked", False),
            "missing charging limit": lambda e, r, s: r.pop("charging_status"),
            "contradictory reason": lambda e, r, s: r.__setitem__("stop_reasons", ["incomplete"]),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.assertEqual(self.validate(change)["status"], "UNKNOWN")

    def test_freshness_boundary_and_previous_boot_are_evidence_only(self):
        result = self.validate(lambda e, r, s: e.update(captured_at="2026-11-02T09:55:00Z",
                                                       previous_boot_id=BOOT))
        self.assertEqual(result["status"], "VALID_LOCAL_COPY")
        self.assertEqual(result["age_seconds"], 300)
        self.assertIs(result["new_ap_boot"], False)
        self.assertEqual(result["pmic_reset"], "UNKNOWN")

    def test_json_float_overflow_is_unknown_even_with_rebound_result_hash(self):
        envelope, result, sections = fixture()
        e, r, raw = copy_bytes(envelope, result, sections)
        for number in (b'1e999', b'-1e999', b'1e309'):
            with self.subTest(number=number):
                changed = r[:-1] + b', "unused_diagnostic": ' + number + b'}'
                env = dict(json.loads(e), result_sha256=hashlib.sha256(changed).hexdigest())
                actual = self.m.validate_copy(encoded(env), changed, raw, now=NOW)
                self.assertEqual(actual["status"], "UNKNOWN")
                self.assertTrue(any("nonfinite" in reason for reason in actual["reasons"]))

    def test_exact_uptime_comparison_rejects_overflow_and_rounding_regressions(self):
        cases = [("9" * 400 + ".00", "8" * 400 + ".00"),
                 ("1000000000000000000000000000000.02", "1000000000000000000000000000000.01")]
        for begin, end in cases:
            with self.subTest(begin=begin):
                envelope, result, sections = fixture()
                sections["uptime"] = result["boot"]["uptime"] = [begin + " 0.00"]
                sections["uptime_end"] = result["boot"]["end_uptime"] = [end + " 0.00"]
                actual = self.m.validate_copy(*copy_bytes(envelope, result, sections), now=NOW)
                self.assertEqual(actual["status"], "UNKNOWN")
                self.assertTrue(any("regressed uptime" in reason for reason in actual["reasons"]))

    def test_echo_only_and_partial_markers_stay_unknown_with_correct_hashes(self):
        envelope, result, sections = fixture()
        e, r, raw = copy_bytes(envelope, result, sections)
        cases = [b"# echo I14_IDENTITY_OK I14_CACHED_CAPTURE_DONE I14_CHECK_END\n",
                 raw.replace(b"I14_CHECK_END\r\n", b""),
                 raw.replace(b"I14_IDENTITY_OK\r\n", b"I14_IDENTITY_OK\r\nI14_STOP_IDENTITY\r\n")]
        for body in cases:
            env = json.loads(e)
            env["raw_sha256"] = hashlib.sha256(body).hexdigest()
            self.assertEqual(self.m.validate_copy(encoded(env), r, body, now=NOW)["status"], "UNKNOWN")

    def test_raw_hash_result_hash_echo_and_duplicate_json_cannot_qualify(self):
        envelope, result, sections = fixture()
        e, r, raw = copy_bytes(envelope, result, sections)
        cases = [(e, r, raw + b"tamper"), (e, r + b" ", raw),
                 (e, r, b"# echo I14_IDENTITY_OK I14_CACHED_CAPTURE_DONE I14_CHECK_END"),
                 (e.replace(b'"schema": 1', b'"schema": 1, "schema": 1'), r, raw),
                 (e.replace(b'"schema": 1', b'"schema": true'), r, raw),
                 (e, b'{"a":1,"a":2}', raw), (e, b'{"n":NaN}', raw)]
        for args in cases:
            self.assertEqual(self.m.validate_copy(*args, now=NOW)["status"], "UNKNOWN")

    def test_duplicated_actual_frame_is_unknown_even_with_rebound_hash(self):
        envelope, result, sections = fixture()
        e, r, raw = copy_bytes(envelope, result, sections)
        raw += b"@@I14_BEGIN boot_id\n" + BOOT.encode() + b"\n@@I14_END boot_id\n"
        env = json.loads(e)
        env["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        self.assertEqual(self.m.validate_copy(encoded(env), r, raw, now=NOW)["status"], "UNKNOWN")

    def test_bounded_physical_reader_rejects_link_directory_fifo_and_oversize(self):
        data = self.path / "data"
        data.write_bytes(b"abcd")
        self.assertEqual(self.m.read_regular(data, cap=4), b"abcd")
        with self.assertRaises(ValueError):
            self.m.read_regular(data, cap=3)
        (self.path / "link").symlink_to(data)
        (self.path / "linked-dir").symlink_to(self.path, target_is_directory=True)
        os.mkfifo(self.path / "fifo")
        for path in [self.path / "link", self.path / "linked-dir/data", self.path, self.path / "fifo"]:
            with self.subTest(path=str(path)), self.assertRaises((OSError, ValueError)):
                self.m.read_regular(path)

    def test_receipt_directory_is_fixed_local_data_only(self):
        envelope, result, sections = fixture()
        for name, data in zip(["intake-receipt.json", "result.json", "cached-check.txt"],
                              copy_bytes(envelope, result, sections)):
            (self.path / name).write_bytes(data)
        self.assertEqual(self.m.inspect_copy(self.path, now=NOW)["status"], "VALID_LOCAL_COPY")
        (self.path / "result.json").unlink()
        self.assertEqual(self.m.inspect_copy(self.path, now=NOW)["status"], "UNKNOWN")

    def test_repackaged_source_or_manifest_claims_fail_integrity(self):
        for relative in self.m.FIXED_PINS:
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        packet = "artifacts/pmic-pair-build-prep-20261102"
        for line in (ROOT / packet / "SHA256SUMS").read_text().splitlines():
            _, name = line.split("  ", 1)
            target = self.path / packet / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / packet / name, target)
        self.assertEqual(self.m.inspect_package(self.path)["integrity"], "MATCH")
        source = self.path / "kernel/i14-pmic-observer.c"
        original = source.read_bytes()
        changed = original.replace(b"I14_OBSERVER_READ_QUALIFIED = 0", b"I14_OBSERVER_READ_QUALIFIED = 1")
        self.assertNotEqual(original, changed)
        source.write_bytes(changed)
        self.assertEqual(self.m.inspect_package(self.path)["integrity"], "MISMATCH")
        source.write_bytes((ROOT / "kernel/i14-pmic-observer.c").read_bytes())
        manifest = self.path / packet / "input-manifest.json"
        data = json.loads(manifest.read_text())
        data["read_qualified"] = 1
        data["native_module"] = "BUILT"
        manifest.write_bytes(encoded(data))
        self.assertEqual(self.m.inspect_package(self.path)["integrity"], "MISMATCH")

    def test_cli_has_no_mutation_or_capability_calls(self):
        global AUDIT_ACTIVE
        envelope, result, sections = fixture()
        for name, data in zip(["intake-receipt.json", "result.json", "cached-check.txt"],
                              copy_bytes(envelope, result, sections)):
            (self.path / name).write_bytes(data)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in ART.rglob("*") if p.is_file()}
        output = io.StringIO()
        AUDIT_ACTIVE = True
        try:
            spec = importlib.util.spec_from_file_location("intake_audited", SCRIPT)
            audited = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(audited)
            with contextlib.redirect_stdout(output):
                self.assertEqual(audited.main([]), 1)
            copied_output = io.StringIO()
            with contextlib.redirect_stdout(copied_output):
                self.assertEqual(audited.main(["--receipt-dir", str(self.path)]), 1)
        finally:
            AUDIT_ACTIVE = False
        self.assert_closed(json.loads(output.getvalue()))
        self.assert_closed(json.loads(copied_output.getvalue()))
        after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in ART.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
