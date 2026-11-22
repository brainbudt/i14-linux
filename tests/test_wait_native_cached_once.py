"""Offline tests for scripts/wait-native-cached-once.py.

Only the hardware/network boundaries are replaced: the IORegistry scan, the
source-address bind check, the transport subprocess and sleeping.  Parsing,
waiting, locking, receipts and result decisions run for real.  The transport
CLI is never imported or executed here.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wait_native_cached", ROOT / "scripts/wait-native-cached-once.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

BASELINE = ROOT / "evidence/2026-11-01/returned-cached-203805Z/cached-baseline.txt"
MODEL = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a"
COMPAT = "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a"
RO_NAMES = ["mmcblk0", "mmcblk0boot0", "mmcblk0boot1"] + ["mmcblk0p%d" % i for i in range(1, 24)]
STAT = "       3        0       24      250        0        0        0        0        0      244      250        0        0        0        0        0        0"
MOUNTS = [
    "rootfs / rootfs rw 0 0",
    "proc /proc proc rw,relatime 0 0",
    "sysfs /sys sysfs rw,relatime 0 0",
    "devtmpfs /dev devtmpfs rw,relatime,size=418808k,nr_inodes=104702,mode=755 0 0",
    "devpts /dev/pts devpts rw,relatime,mode=600,ptmxmode=000 0 0",
    "configfs /sys/kernel/config configfs rw,relatime 0 0",
]
LATCHED_STATE = [
    "phase=latched-no-more-io reason=mode-readback error=-117 held_ref=1 mutated=1 usb0_carrier=1 usb_pilot=1 usb_state=7 usb_budget_ma=500 usb_suspended=0 usb_config_ma=500",
    "arm_ns=1 arm_deadline_ns=2 start_ns=3 active_deadline_ns=4 end_ns=0 latched_ns=99",
    "samples=unknown",
]


def payload(**changes):
    """Device output per section, values copied from the 20:38 baseline."""
    sections = {
        "identity": ["7.0.10-i14-bringup", "aarch64", "Uid:    0       0       0       0",
                     MODEL + "  /proc/device-tree/model", COMPAT + "  /proc/device-tree/compatible"],
        "boot_id": ["4a3dc960-87e8-4829-987e-717b0bb0d717"],
        "uptime": ["113.12 110.90"],
        "modules": [],
        "mmcblk0_size": ["30535680"],
        "mmcblk_ro": ["%s 1" % n for n in sorted(RO_NAMES)],
        "mmcblk0_stat": [STAT],
        "mounts": list(MOUNTS),
        "cached_i14_charger": ["I14_ABSENT"],
        "cached_battery_sample": ["I14_ABSENT"],
        "maxpower": ["2"],
        "udc_state": ["configured"],
        "usb0_carrier": ["1"],
    }
    sections.update(changes)
    return sections


def transcript(sections=None, identity_ok=True, done=True, end=True, drop=()):
    sections = payload() if sections is None else sections
    lines = ["", "BusyBox v1.37.0 (2024-09-05 00:00:00 UTC) built-in shell (ash)", "",
             "# " + m.COMMAND + "; printf '\\nI14_CHECK_END\\n'"]

    def section(name):
        if name not in drop:
            lines.extend(["@@I14_BEGIN " + name] + sections[name] + ["@@I14_END " + name])

    section("identity")
    if identity_ok:
        lines.append("I14_IDENTITY_OK")
        for name in m.PAYLOAD_SECTIONS:
            section(name)
        if done:
            lines.append("I14_CACHED_CAPTURE_DONE")
    else:
        lines.append("I14_STOP_IDENTITY")
    if end:
        lines.extend(["", "I14_CHECK_END"])
    return ("\r\n".join(lines) + ("\r\n# " if end else "")).encode()


def device(**changes):
    d = {"idVendor": 0x1d6b, "idProduct": 0x0104, "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}
    d.update(changes)
    return d


POWER_ORDER = ["identity", "boot_id", "uptime", "kernel_log", "cmdline", "mounts",
               "chosen_bootargs", "chosen_bootargs_override", "usb_startup_log", "storage_startup_log",
               "usb_snapshot_log", "modules", "mmcblk0_size", "mmcblk_ro", "mmcblk0_stat",
               "cached_i14_charger", "cached_battery_sample", "maxpower", "udc_state", "usb0_carrier",
               "boot_id_end", "uptime_end"]


def power_transcript(log=b"<6>Linux boot\n", log_rc=0, log_absent=False, drop=(), **changes):
    """Literal power-profile wire fixture; never executes the emitted command."""
    p = payload()
    p.update(cmdline=["console=ttyS0 panic=10 i14.trial_reboot=off"], chosen_bootargs=["console=ttyS0\x00"],
             chosen_bootargs_override=["I14_ABSENT"], usb_startup_log=["I14_USB_ECM_READY"],
             storage_startup_log=["I14_STORAGE_READONLY_DONE"], usb_snapshot_log=["I14_ABSENT"],
             boot_id_end=["4a3dc960-87e8-4829-987e-717b0bb0d717"], uptime_end=["113.22 111.00"])
    p.update(changes)
    raw = b""
    for name in POWER_ORDER:
        if name in drop:
            continue
        raw += ("@@I14_BEGIN " + name + "\r\n").encode()
        if name == "kernel_log":
            raw += b"I14_ABSENT\r\n" if log_absent else log + ("\r\n@@I14_DMESG_RC %d\r\n" % log_rc).encode()
        else:
            raw += ("\r\n".join(p[name]) + ("\r\n" if p[name] else "")).encode()
        raw += ("@@I14_END " + name + "\r\n").encode()
        if name == "identity":
            raw += b"I14_IDENTITY_OK\r\n"
    return raw + b"I14_CACHED_CAPTURE_DONE\r\nI14_CHECK_END\r\n"


class FixtureAnchorTests(unittest.TestCase):
    def test_fixture_payload_lines_occur_in_stored_baseline(self):
        baseline = {l.rstrip("\r") for l in BASELINE.read_bytes().decode().split("\n")}
        p = payload()
        for name in ("boot_id", "uptime", "mmcblk0_size", "mmcblk0_stat", "mounts"):
            for line in p[name]:
                self.assertIn(line, baseline, name)
        self.assertIn("Uid:    0       0       0       0", baseline)
        self.assertEqual(sum(1 for l in baseline if l.startswith("/sys/class/block/mmcblk")), 26)


class EvaluateTests(unittest.TestCase):
    def test_complete_capture_reports_baseline_but_charging_stays_blocked(self):
        r = m.evaluate(transcript(), 0)
        self.assertEqual(r["verdict"], "COMPLETE_CACHED_BASELINE")
        self.assertEqual(r["exit_code"], 0)
        self.assertTrue(r["capture_complete"])
        self.assertTrue(r["identity"]["ok"])
        self.assertEqual(r["boot"]["boot_id"], "4a3dc960-87e8-4829-987e-717b0bb0d717")
        self.assertEqual(r["boot"]["modules"], [])
        self.assertEqual(r["storage"]["capacity_sectors"], 30535680)
        self.assertEqual(r["storage"]["ro_node_count"], 26)
        self.assertTrue(r["storage"]["all_ro"])
        self.assertEqual(r["storage"]["sectors_written"], 0)
        self.assertEqual(r["usb"], {"maxpower": "2", "udc_state": "configured", "usb0_carrier": "1"})
        self.assertIs(r["charging_blocked"], True)
        self.assertEqual(r["charging_status"], "UNKNOWN")
        self.assertIs(r["ready_for_charging"], False)

    def test_echo_only_transcript_is_not_a_receipt(self):
        echo = ("# " + m.COMMAND + "; printf '\\nI14_CHECK_END\\n'\r\n").encode()
        r = m.evaluate(echo, 0)
        self.assertFalse(r["capture_complete"])
        self.assertEqual(r["verdict"], "STOP_INCOMPLETE")
        self.assertNotEqual(r["exit_code"], 0)

    def test_truncated_before_end_marker_is_incomplete(self):
        r = m.evaluate(transcript(end=False), 0)
        self.assertFalse(r["capture_complete"])
        self.assertEqual(r["verdict"], "STOP_INCOMPLETE")

    def test_truncated_before_done_marker_is_incomplete(self):
        r = m.evaluate(transcript(done=False), 0)
        self.assertFalse(r["capture_complete"])

    def test_transport_rc0_with_missing_section_is_not_health_proof(self):
        r = m.evaluate(transcript(drop=("mmcblk_ro",)), 0)
        self.assertFalse(r["capture_complete"])
        self.assertNotEqual(r["exit_code"], 0)
        self.assertIs(r["charging_blocked"], True)

    def test_nonzero_transport_rc_is_incomplete_even_with_full_text(self):
        r = m.evaluate(transcript(), 1)
        self.assertFalse(r["capture_complete"])
        self.assertNotEqual(r["exit_code"], 0)

    def test_legacy_baseline_without_sections_is_not_accepted(self):
        r = m.evaluate(BASELINE.read_bytes(), 0)
        self.assertFalse(r["capture_complete"])

    def test_device_identity_stop_has_no_payload_and_stops(self):
        r = m.evaluate(transcript(identity_ok=False), 0)
        self.assertEqual(r["verdict"], "STOP_IDENTITY")
        self.assertNotEqual(r["exit_code"], 0)
        self.assertIsNone(r["boot"])

    def test_host_identity_mismatch_contradicts_device_ok(self):
        bad = payload(identity=["7.0.10-i14-bringup", "aarch64", "Uid:    0       0       0       0",
                                "0" * 64 + "  /proc/device-tree/model", COMPAT + "  /proc/device-tree/compatible"])
        r = m.evaluate(transcript(bad), 0)
        self.assertEqual(r["verdict"], "STOP_CONTRADICTION")
        self.assertNotEqual(r["exit_code"], 0)

    def test_non_root_identity_contradicts_device_ok(self):
        bad = payload(identity=["7.0.10-i14-bringup", "aarch64", "Uid:    1000    1000    1000    1000",
                                MODEL + "  /proc/device-tree/model", COMPAT + "  /proc/device-tree/compatible"])
        self.assertEqual(m.evaluate(transcript(bad), 0)["verdict"], "STOP_CONTRADICTION")

    def test_wrong_capacity_stops(self):
        r = m.evaluate(transcript(payload(mmcblk0_size=["30535679"])), 0)
        self.assertEqual(r["verdict"], "STOP_BASE_DATA")
        self.assertNotEqual(r["exit_code"], 0)

    def test_one_writable_node_stops(self):
        ro = ["%s 1" % n for n in sorted(RO_NAMES)]
        ro[5] = ro[5][:-1] + "0"
        r = m.evaluate(transcript(payload(mmcblk_ro=ro)), 0)
        self.assertEqual(r["verdict"], "STOP_BASE_DATA")
        self.assertFalse(r["storage"]["all_ro"])

    def test_missing_or_extra_block_node_stops(self):
        ro = ["%s 1" % n for n in sorted(RO_NAMES)]
        self.assertEqual(m.evaluate(transcript(payload(mmcblk_ro=ro[:-1])), 0)["verdict"], "STOP_BASE_DATA")
        self.assertEqual(m.evaluate(transcript(payload(mmcblk_ro=ro + ["mmcblk1 1"])), 0)["verdict"], "STOP_BASE_DATA")

    def test_bad_boot_id_stops(self):
        self.assertEqual(m.evaluate(transcript(payload(boot_id=["I14_ABSENT"])), 0)["verdict"], "STOP_BASE_DATA")

    def test_duplicate_section_is_contradiction(self):
        raw = transcript().replace(b"@@I14_BEGIN mounts", b"@@I14_BEGIN uptime\r\n1.0 1.0\r\n@@I14_END uptime\r\n@@I14_BEGIN mounts")
        self.assertFalse(m.evaluate(raw, 0)["capture_complete"])

    def test_missing_cached_nodes_are_absent_or_unknown_not_failure(self):
        p = payload(cached_battery_sample=["cat: read error: No data available", "I14_UNREADABLE"],
                    maxpower=["I14_ABSENT"], usb0_carrier=["I14_ABSENT"])
        r = m.evaluate(transcript(p), 0)
        self.assertEqual(r["verdict"], "COMPLETE_CACHED_BASELINE")
        cached = r["cached_diagnostics"]
        self.assertEqual(cached["charger_state"]["status"], "ABSENT")
        self.assertEqual(cached["battery_sample"]["status"], "UNKNOWN")
        self.assertIs(cached["historical_only"], True)
        self.assertEqual(r["usb"]["maxpower"], "ABSENT")
        self.assertEqual(r["usb"]["usb0_carrier"], "ABSENT")

    def test_latched_cached_state_is_preserved_and_never_ready(self):
        r = m.evaluate(transcript(payload(cached_i14_charger=LATCHED_STATE)), 0)
        state = r["cached_diagnostics"]["charger_state"]
        self.assertEqual(state["status"], "PRESENT")
        self.assertIs(state["latched"], True)
        self.assertEqual(state["phase"], "latched-no-more-io")
        self.assertEqual(state["raw"], LATCHED_STATE)
        self.assertIs(r["ready_for_charging"], False)
        self.assertIs(r["charging_blocked"], True)
        self.assertEqual(r["charging_status"], "UNKNOWN")


class CommandSafetyTests(unittest.TestCase):
    def test_command_reads_only_allowlisted_paths_and_never_writes(self):
        import re
        paths = set(re.findall(r"/(?:sys|proc)[^\s;\"')]*", m.COMMAND))
        allowed = {
            "/proc/device-tree/model", "/proc/device-tree/compatible", "/proc/self/status",
            "/proc/sys/kernel/random/boot_id", "/proc/uptime", "/proc/modules", "/proc/mounts",
            "/sys/class/block/mmcblk0/size", "/sys/class/block/mmcblk*", "/sys/class/block/mmcblk0/stat",
            "/sys/devices/i14-charger/state", "/sys/devices/i14-reboot-target/battery_sample",
            "/sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower",
            "/sys/class/udc/musb-hdrc.3.auto/state", "/sys/class/net/usb0/carrier",
        }
        self.assertEqual(paths, allowed)
        self.assertNotIn(">", m.COMMAND.replace("2>&1", ""))
        for text in ("charger_state", "echo sample", "/control", "/battery "):
            self.assertNotIn(text, m.COMMAND)
        tokens = set(re.split(r"[\s;|&()]+", m.COMMAND))
        for cmd in ("insmod", "modprobe", "rmmod", "reboot", "poweroff", "tee", "dd", "mount", "umount", "kill"):
            self.assertNotIn(cmd, tokens)


class UsbTests(unittest.TestCase):
    def test_exact_single_target(self):
        self.assertEqual(m.classify([device()]), "exact")
        self.assertEqual(m.classify([device(idVendor=0x0e8d, idProduct=0x2000, **{"USB Serial Number": "X"})]), "absent")

    def test_partial_or_ambiguous_match_is_mismatch(self):
        for change in ({"idVendor": 0xe8d}, {"idProduct": 1}, {"USB Serial Number": "OTHER"},
                       {"locationID": 0x2120000}, {"USB Serial Number": None}):
            self.assertEqual(m.classify([device(**change)]), "mismatch", change)
        self.assertEqual(m.classify([device(), device()]), "mismatch")
        self.assertEqual(m.classify([device(), device(locationID=0x2120000)]), "mismatch")

    def test_nested_registry(self):
        self.assertEqual(m.classify(list(m.records([{"IORegistryEntryChildren": [device()]}]))), "exact")


class WatchHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.lock = self.base / "i14-native-watch.lock"
        self.lock.write_bytes(b"")
        self.out = self.base / "run"
        self.clock = [0.0]
        self.calls = []
        patches = [
            patch.object(m.time, "monotonic", lambda: self.clock[0]),
            patch.object(m.time, "sleep", self.sleep),
            patch.object(m, "LOCK", self.lock),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def sleep(self, seconds):
        self.clock[0] += seconds

    def fake_transport(self, raw, rc=0, mode="cached-read-only-once"):
        def run(out):
            self.calls.append(out)
            # Receipts must be durable before the single connect.
            attempt = json.loads((out / "attempt.json").read_text())
            self.assertEqual(attempt["mode"], mode)
            self.assertTrue((out / "intent.json").exists())
            (out / m.RAW_NAME).write_bytes(raw)
            return rc
        return run

    def main(self, seconds="30", scan=None, source=None, transport=None, extra_args=()):
        scan = scan or (lambda: [device()])
        source = source or (lambda: True)
        transport = transport or self.fake_transport(transcript())
        with patch.object(m, "scan", scan), patch.object(m, "source_ready", source), \
                patch.object(m, "run_transport", transport):
            return m.main([str(self.out), seconds, *extra_args])

    def result(self):
        return json.loads((self.out / "result.json").read_text())

    def milestones(self):
        return [l.split()[2] for l in (self.out / "watch.log").read_text().splitlines()]


class WatchTests(WatchHarness):
    def test_complete_run_captures_once_and_writes_handoff_files(self):
        self.assertEqual(self.main(), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.result()["verdict"], "COMPLETE_CACHED_BASELINE")
        self.assertIs(self.result()["charging_blocked"], True)
        self.assertEqual(self.milestones(), ["START", "PREFLIGHT_OK", "TARGET_EXACT", "ATTEMPT_RESERVED",
                                             "TRANSPORT_RETURNED", "RESULT", "END"])
        self.assertIn("exit=0", (self.out / "watch.log").read_text().splitlines()[-1])

    def test_source_not_ready_means_no_attempt_yet(self):
        polls = []

        def source():
            polls.append(1)
            self.assertFalse((self.out / "attempt.json").exists())
            return len(polls) >= 3
        self.assertEqual(self.main(source=source), 0)
        self.assertEqual(len(polls), 3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.milestones().count("WAIT_SOURCE"), 1)

    def test_source_never_ready_times_out_without_command(self):
        self.assertEqual(self.main(seconds="10", source=lambda: False), 3)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.out / "attempt.json").exists())
        self.assertEqual(self.result()["verdict"], "TIMEOUT_NO_COMMAND")
        self.assertIs(self.result()["attempted"], False)

    def test_absent_target_times_out_within_limit(self):
        self.assertEqual(self.main(seconds="7", scan=lambda: []), 3)
        self.assertEqual(self.calls, [])
        self.assertLessEqual(self.clock[0], 7 + m.POLL_SECONDS)

    def test_mismatch_stops_without_connecting(self):
        self.assertEqual(self.main(scan=lambda: [device(locationID=0x2120000)]), 4)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.out / "attempt.json").exists())
        self.assertEqual(self.result()["verdict"], "STOP_USB_MISMATCH")

    def test_failed_transport_is_terminal_and_not_retried(self):
        self.assertEqual(self.main(transport=self.fake_transport(b"", rc=1)), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.out / "attempt.json").exists())
        self.assertEqual(self.result()["verdict"], "STOP_INCOMPLETE")

    def test_transport_exception_is_terminal_and_not_retried(self):
        def boom(out):
            self.calls.append(out)
            raise m.Stopped("SIGTERM")
        self.assertEqual(self.main(transport=boom), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.result()["attempted"], True)
        self.assertFalse(self.result()["capture_complete"])
        self.assertIs(self.result()["charging_blocked"], True)

    def test_identity_stop_on_device_is_nonzero(self):
        self.assertEqual(self.main(transport=self.fake_transport(transcript(identity_ok=False))), 6)
        self.assertEqual(len(self.calls), 1)

    def test_lock_contention_refuses_before_creating_output(self):
        holder = open(self.lock, "a")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.main(), 5)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.calls, [])

    def test_lock_is_held_during_capture(self):
        def run(out):
            self.calls.append(out)
            probe = open(self.lock, "a")
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                probe.close()
            (out / m.RAW_NAME).write_bytes(transcript())
            return 0
        self.assertEqual(self.main(transport=run), 0)

    def test_missing_lock_file_refuses(self):
        self.lock.unlink()
        self.assertEqual(self.main(), 5)
        self.assertFalse(self.out.exists())

    def test_output_reuse_refused_and_untouched(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("x")
        self.assertEqual(self.main(), 5)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["keep.txt"])
        self.assertEqual(self.calls, [])

    def test_duration_bounds_and_format(self):
        for bad in ("0", "3601", "abc", "-5"):
            self.assertEqual(self.main(seconds=bad), 5, bad)
            self.assertFalse(self.out.exists())
        self.assertEqual(self.calls, [])

    def test_transport_pin_mismatch_stops_before_waiting(self):
        fake = self.base / "tn.py"
        fake.write_bytes(b"print(1)\n")
        scans = []
        with patch.object(m, "TRANSPORT", fake):
            self.assertEqual(self.main(scan=lambda: scans.append(1) or [device()]), 2)
        self.assertEqual(scans, [])
        self.assertEqual(self.calls, [])
        self.assertFalse((self.out / "attempt.json").exists())
        self.assertEqual(self.result().get("failure_kind"), "integrity")


class PowerWatchTests(WatchHarness):
    def test_power_cli_captures_raw_log_and_baseline_in_one_attempt(self):
        log = b"<6>boot\xff\x00\n<4>probe warning\n"
        transport = self.fake_transport(power_transcript(log=log), mode="power-evidence-read-only-once")
        self.assertEqual(self.main(transport=transport, extra_args=("--power-evidence",)), 0)
        result = self.result()
        self.assertEqual(result["verdict"], "COMPLETE_POWER_EVIDENCE")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.out / "kernel-ringbuffer.raw").read_bytes(), log)
        self.assertTrue((self.out / "cached-check.txt").exists())
        self.assertEqual(result["power_evidence"]["kernel_log"]["status"], "PRESENT")
        self.assertTrue(result["charging_blocked"])
        self.assertFalse(result["ready_for_charging"])

    def test_power_raw_file_is_byte_capped_without_modifying_full_transport_capture(self):
        log = b"x" * 262144 + b"\xff"
        raw = power_transcript(log=log)
        transport = self.fake_transport(raw, mode="power-evidence-read-only-once")
        self.assertEqual(self.main(transport=transport, extra_args=("--power-evidence",)), 0)
        self.assertEqual((self.out / "kernel-ringbuffer.raw").read_bytes(), b"x" * 262144)
        self.assertEqual((self.out / "cached-check.txt").read_bytes(), raw)
        self.assertTrue(self.result()["power_evidence"]["kernel_log"]["truncated"])

    def test_power_profile_waits_for_source_without_reserving_attempt(self):
        self.assertEqual(self.main(seconds="5", source=lambda: False, extra_args=("--power-evidence",)), 3)
        self.assertFalse((self.out / "attempt.json").exists())
        self.assertEqual(self.calls, [])

    def test_unknown_or_repeated_flags_refuse_before_output_or_scan(self):
        for flags in (("--unknown",), ("--power-evidence", "--power-evidence")):
            self.assertEqual(self.main(extra_args=flags), 5)
            self.assertFalse(self.out.exists())
            self.assertEqual(self.calls, [])

    def test_attempt_usb_receipt_keeps_only_selected_target_metadata(self):
        unrelated = {"idVendor": 0x1234, "idProduct": 9, "USB Serial Number": "private-other-device",
                     "locationID": 0x2120000, "private": "do not retain"}
        selected = device()
        self.assertEqual(self.main(scan=lambda: [unrelated, selected]), 0)
        attempt = json.loads((self.out / "attempt.json").read_text())
        self.assertEqual(attempt["usb"], [{"idVendor": 0x1d6b, "idProduct": 0x0104,
                                         "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}])
        self.assertNotIn("private", (self.out / "attempt.json").read_text())

    def test_power_transport_failure_preserves_partial_bytes_and_never_retries(self):
        raw = power_transcript(log=b"partial kernel output\xff")
        raw = raw[:raw.index(b"\r\n@@I14_DMESG_RC")]
        transport = self.fake_transport(raw, rc=1, mode="power-evidence-read-only-once")
        self.assertEqual(self.main(transport=transport, extra_args=("--power-evidence",)), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.out / "cached-check.txt").read_bytes(), raw)
        self.assertFalse(self.result()["capture_complete"])
        self.assertTrue(self.result()["charging_blocked"])

    def test_power_transport_exception_keeps_any_already_written_kernel_frame(self):
        raw = power_transcript(log=b"last saved log\xff\n")
        raw = raw[:raw.index(b"@@I14_BEGIN cmdline")]

        def interrupted(out):
            self.calls.append(out)
            (out / m.RAW_NAME).write_bytes(raw)
            raise m.Stopped("SIGTERM")

        self.assertEqual(self.main(transport=interrupted, extra_args=("--power-evidence",)), 2)
        self.assertTrue((self.out / "kernel-ringbuffer.raw").exists())
        self.assertEqual((self.out / "kernel-ringbuffer.raw").read_bytes(), b"last saved log\xff\n")
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.result()["capture_complete"])


class PowerEvaluateTests(unittest.TestCase):
    def evaluate(self, raw):
        return m.evaluate(raw, 0, power_evidence=True)

    def test_unavailable_kernel_log_keeps_complete_baseline_and_names_absence(self):
        for raw, expected in ((power_transcript(log_absent=True), "ABSENT"),
                              (power_transcript(log=b"dmesg: klogctl: Operation not permitted\n", log_rc=1), "UNREADABLE")):
            with self.subTest(expected=expected):
                result = self.evaluate(raw)
                self.assertEqual(result["exit_code"], 0)
                self.assertTrue(result["capture_complete"])
                self.assertEqual(result["power_evidence"]["kernel_log"]["status"], expected)
                self.assertTrue(result["charging_blocked"])
                self.assertFalse(result["ready_for_charging"])

    def test_chosen_optional_status_ignores_the_framing_newline_after_raw_dt_bytes(self):
        result = self.evaluate(power_transcript(chosen_bootargs=["I14_ABSENT", ""],
                                              chosen_bootargs_override=["cat: read error", "I14_UNREADABLE", ""]))
        chosen = result["power_evidence"]["chosen"]
        self.assertEqual(chosen["chosen_bootargs"]["status"], "ABSENT")
        self.assertEqual(chosen["chosen_bootargs_override"]["status"], "UNKNOWN")

    def test_log_retention_has_exact_byte_boundary_and_one_byte_truncation_detection(self):
        for length, truncated in ((262143, False), (262144, False), (262145, True)):
            with self.subTest(length=length):
                result = self.evaluate(power_transcript(log=b"x" * length))
                log = result["power_evidence"]["kernel_log"]
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(log["captured_bytes"], min(length, 262144))
                self.assertEqual(log["byte_cap"], 262144)
                self.assertIs(log["truncated"], truncated)
                self.assertEqual(log["sha256"], hashlib.sha256(b"x" * min(length, 262144)).hexdigest())

    def test_dmesg_without_explicit_read_status_stops_instead_of_empty_success(self):
        raw = power_transcript().replace(b"\r\n@@I14_DMESG_RC 0", b"")
        result = self.evaluate(raw)
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse(result["capture_complete"])

    def test_missing_or_unfinished_power_section_stops(self):
        for raw in (power_transcript(drop=("cmdline",)), power_transcript()[:-15],
                    power_transcript().replace(b"@@I14_END kernel_log", b"@@I14_END wrong")):
            with self.subTest(raw=raw[-50:]):
                result = self.evaluate(raw)
                self.assertNotEqual(result["exit_code"], 0)
                self.assertFalse(result["capture_complete"])

    def test_different_end_boot_id_is_terminal_even_with_complete_payload(self):
        result = self.evaluate(power_transcript(boot_id_end=["21bcaa17-c098-4b96-9bd4-706826e8af57"]))
        self.assertEqual(result["verdict"], "STOP_BOOT_CHANGED")
        self.assertNotEqual(result["exit_code"], 0)
        self.assertFalse(result["capture_complete"])
        self.assertFalse(result["boot"]["same_boot"])

    def test_invalid_or_regressing_end_uptime_stops(self):
        for value in ([], ["error"], ["112.00 111.00"]):
            with self.subTest(value=value):
                self.assertNotEqual(self.evaluate(power_transcript(uptime_end=value))["exit_code"], 0)

    def test_no_pstore_mount_is_reported_and_existing_mount_does_not_qualify_records(self):
        for mounts, status in ((MOUNTS, "ABSENT"), (MOUNTS + ["pstore /sys/fs/pstore pstore rw 0 0"], "UNKNOWN")):
            with self.subTest(status=status):
                result = self.evaluate(power_transcript(mounts=mounts))
                pstore = result["power_evidence"]["pstore"]
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(pstore["status"], status)
                self.assertFalse(pstore["records_read"])

    def test_power_profile_preserves_cmdline_startup_logs_and_cached_latch_without_unblocking(self):
        result = self.evaluate(power_transcript(cached_i14_charger=LATCHED_STATE))
        power = result["power_evidence"]
        self.assertEqual(power["cmdline"]["raw"], ["console=ttyS0 panic=10 i14.trial_reboot=off"])
        self.assertEqual(power["startup_logs"]["usb_startup_log"]["raw"], ["I14_USB_ECM_READY"])
        self.assertTrue(result["cached_diagnostics"]["charger_state"]["latched"])
        self.assertEqual(result["cached_diagnostics"]["battery_sample"]["status"], "ABSENT")
        self.assertTrue(result["boot"]["same_boot"])
        self.assertTrue(result["charging_blocked"])
        self.assertFalse(result["ready_for_charging"])


class PreflightTests(unittest.TestCase):
    def test_bound_transport_copy_matches_pinned_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            m.preflight(Path(d))
            data = (Path(d) / "transport.py").read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(),
                             "d7d496ffbc80f0588f8d8fab73e9663f1eb8bf4c47058d3944f0d29d192484e8")
            self.assertEqual(data.count(b"source_address=('10.77.14.1',0)"), 1)
            self.assertEqual(data.count(b"create_connection("), 1)
            self.assertEqual((Path(d) / "cached-check.command").read_text(), m.COMMAND + "\n")

    def test_power_preflight_stages_early_bounded_log_and_no_active_pstore_or_pmic_command(self):
        with tempfile.TemporaryDirectory() as d:
            m.preflight(Path(d), power_evidence=True)
            command = (Path(d) / "cached-check.command").read_text()
        self.assertLess(command.index("I14_IDENTITY_OK"), command.index("s boot_id;"))
        self.assertLess(command.index("s uptime;"), command.index("s kernel_log;"))
        self.assertLess(command.index("s kernel_log;"), command.index("s cmdline;"))
        self.assertLess(command.index("s cmdline;"), command.index("s modules;"))
        self.assertIn("dmesg -r -s 262145", command)
        self.assertIn("@@I14_DMESG_RC", command)
        self.assertIn("/proc/device-tree/chosen/bootargs", command)
        self.assertIn("/run/usb-network.log", command)
        self.assertIn("/run/storage-readonly.log", command)
        tokens = set(__import__("re").split(r"[\s;|&()]+", command))
        self.assertTrue(tokens.isdisjoint({"mount", "umount", "insmod", "modprobe", "rmmod", "rm", "reboot", "poweroff", "tee", "dd"}))
        for forbidden in ("/sys/fs/pstore", "/dev/watchdog", "/battery ", "charger_state", "echo sample", "dmesg -c"):
            self.assertNotIn(forbidden, command)


if __name__ == "__main__":
    unittest.main()
