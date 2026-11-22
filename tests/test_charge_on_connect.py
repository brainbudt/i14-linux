import importlib.util
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("charge_connect", Path(__file__).resolve().parents[1] / "scripts/charge-on-connect-once.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

DESCRIPTOR = {"device_count": 1, "idVendor": 0x1d6b, "idProduct": 0x0104,
              "bus": 2, "port_numbers": [1, 1], "serial_number": "I14_NATIVE_INIT",
              "bcdUSB": 512, "configuration": {"bConfigurationValue": 1,
              "bmAttributes": 192, "bMaxPower": 250}}
HOST = json.dumps(DESCRIPTOR)
FAILED_MODULE_HASHES = (
    "b95def24322463d1227a01358cd250e87d404119ea00e8f3170a0fc2cfaee002",
    "2f6c8cbb733de576105ef7534127335fb8e07eadac4d8af1597d8db018110be2",
    "f328df9f96e183251c22bec1d0715c55d868cfffd60b13baddf5ec49dac5e42e",
)


class ChargeConnectTests(unittest.TestCase):
    def assert_blocked_before_io(self, entrypoint, charger_pin, *, stopped=False):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pack, out = root / "ready", root / "output"
            pack.mkdir()
            if stopped:
                (pack / "STOP-NATIVE-FAILED.txt").write_text("historical failure\n")
            before = sorted(root.rglob("*"))
            with ExitStack() as stack:
                stack.enter_context(patch.object(m, "ROOT", root))
                stack.enter_context(patch.object(m, "PACK", pack))
                stack.enter_context(patch.object(m, "PINS", {"i14-charger.ko": charger_pin}))
                for method in ("open", "mkdir", "read_text", "read_bytes"):
                    stack.enter_context(patch.object(Path, method,
                        side_effect=AssertionError("refused candidate reached file I/O: " + method)))
                for owner, method in ((m.native, "scan"), (m, "wait_ready"),
                                      (m.Runner, "run"), (m.subprocess, "run"),
                                      (m.socket, "socket"), (m.socket, "create_connection")):
                    stack.enter_context(patch.object(owner, method,
                        side_effect=AssertionError("refused candidate reached action: " + method)))
                with self.assertRaisesRegex(RuntimeError, "charging candidate blocked"):
                    if entrypoint == "main":
                        m.main(out, 60)
                    else:
                        m.Runner(out).execute({})
            self.assertEqual(sorted(root.rglob("*")), before)
            self.assertFalse(out.exists())

    def test_main_known_failed_hashes_refuse_before_file_or_device_io(self):
        for pin in FAILED_MODULE_HASHES:
            with self.subTest(pin=pin):
                self.assert_blocked_before_io("main", pin)

    def test_direct_execute_known_failed_hashes_refuse_before_transport_or_load(self):
        for pin in FAILED_MODULE_HASHES:
            with self.subTest(pin=pin):
                self.assert_blocked_before_io("execute", pin)

    def test_main_stop_marker_refuses_even_without_a_known_failed_hash(self):
        self.assert_blocked_before_io("main", "0" * 64, stopped=True)

    def test_direct_execute_stop_marker_refuses_even_without_a_known_failed_hash(self):
        self.assert_blocked_before_io("execute", "0" * 64, stopped=True)

    def test_unblocked_hash_still_requires_manifest_before_any_device_action(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pack, out = root / "artifacts/ready", root / "output"
            pack.mkdir(parents=True)
            with patch.object(m, "ROOT", root), patch.object(m, "PACK", pack), \
                 patch.object(m, "PINS", {"i14-charger.ko": "0" * 64}), \
                 patch.object(m.fcntl, "flock", side_effect=lambda file, op: self.addCleanup(file.close)), \
                 patch.object(m.native, "scan", side_effect=AssertionError("manifest validation skipped")), \
                 patch.object(m, "wait_ready", side_effect=AssertionError("manifest validation skipped")):
                with self.assertRaises(FileNotFoundError):
                    m.main(out, 60)
            self.assertFalse((out / "attempt.json").exists())

    def reader(self):
        path = Path(__file__).resolve().parents[1] / "scripts/read-i14-usb-descriptor.py"
        self.assertTrue(path.exists(), "read-only descriptor helper is required")
        spec = importlib.util.spec_from_file_location("usb_descriptor", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_reader_returns_raw_descriptor_and_disposes_resources(self):
        reads, disposed = [], []
        class Device:
            idVendor, idProduct, bus, port_numbers, bcdUSB, iSerialNumber = 0x1d6b, 0x0104, 2, (1, 1), 512, 3
            def get_active_configuration(self):
                reads.append("configuration")
                return SimpleNamespace(bConfigurationValue=1, bmAttributes=192, bMaxPower=250)
        device = Device()
        core = SimpleNamespace(find=lambda **kw: [device])
        util = SimpleNamespace(get_string=lambda d, index: "I14_NATIVE_INIT",
                               dispose_resources=disposed.append)
        self.assertEqual(self.reader().read_descriptor(core, util, object()), DESCRIPTOR)
        self.assertEqual(reads, ["configuration"])
        self.assertEqual(disposed, [device])

    def test_reader_ambiguity_fails_before_serial_or_configuration_reads(self):
        device = SimpleNamespace(bus=2, port_numbers=(1, 1))
        core = SimpleNamespace(find=lambda **kw: [device, device])
        util = SimpleNamespace(dispose_resources=lambda d: None)
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            self.reader().read_descriptor(core, util, object())

    def run_main(self, binds, clock):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pack, out = root / "artifacts/ready", root / "run"
            pack.mkdir(parents=True)
            source = root / "scripts/read-i14-usb-descriptor.py"
            source.parent.mkdir()
            source.write_bytes(b"# original descriptor reader\n")
            asset = pack / "i14-charger.ko"
            asset.write_bytes(b"test-only module bytes")
            manifest = {"descriptor_reader": {"local": str(source.relative_to(root)),
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()},
                        "assets": [{"local": str(asset.relative_to(root))}]}
            (pack / "manifest.json").write_text(json.dumps(manifest))
            def bind(address):
                self.assertEqual(address, ("10.77.14.1", 0))
                self.assertFalse((out / "attempt.json").exists())
                if binds.pop(0):
                    raise OSError("source address not assigned")
            def scan():
                source.write_bytes(b"# edited after arming\n")
                return [{"idVendor": 0x1d6b, "idProduct": 0x0104,
                         "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}]
            commands = []
            def execute(runner, assets):
                self.assertEqual(binds, [])
                self.assertTrue((out / "attempt.json").exists())
                commands.append("native command")
            with patch.object(m, "ROOT", root), patch.object(m, "PACK", pack), \
                 patch.object(m, "DESCRIPTOR_READER", source), \
                 patch.object(m, "PINS", {"i14-charger.ko": hashlib.sha256(asset.read_bytes()).hexdigest()}), \
                 patch.object(m.fcntl, "flock", side_effect=lambda file, op: self.addCleanup(file.close)), \
                 patch.object(m.time, "monotonic", side_effect=clock), patch.object(m.time, "sleep"), \
                 patch.object(m.native, "scan", side_effect=scan), \
                 patch.object(m.socket, "socket") as sock, patch.object(m.Runner, "execute", execute):
                sock.return_value.__enter__.return_value.bind.side_effect = bind
                result = m.main(out, 60)
            self.assertEqual((out / "read-i14-usb-descriptor.py").read_bytes(), b"# original descriptor reader\n")
            return result, commands, (out / "attempt.json").exists()

    def test_initial_wait_reserves_attempt_only_after_source_address_returns(self):
        result, commands, attempted = self.run_main([True, False], [0, 1, 2])
        self.assertEqual((result, commands, attempted), (0, ["native command"], True))

    def test_initial_wait_deadline_sends_no_native_command(self):
        result, commands, attempted = self.run_main([True], [0, 1, 61])
        self.assertEqual((result, commands, attempted), (3, [], False))

    def test_host_descriptor_is_mandatory_and_bound_to_target(self):
        self.assertTrue(m.host_500(HOST))
        wrongs = ["", "null", "[]", HOST + HOST]
        for key, value in DESCRIPTOR.items():
            wrongs.append(json.dumps({k: v for k, v in DESCRIPTOR.items() if k != key}))
            wrongs.append(json.dumps({**DESCRIPTOR, key: None}))
        for key, value in {"device_count": 2, "bus": 3, "port_numbers": [1, 2],
                           "idVendor": 1, "idProduct": 1, "serial_number": "OTHER",
                           "bcdUSB": 768}.items():
            wrongs.append(json.dumps({**DESCRIPTOR, key: value}))
        for key, value in DESCRIPTOR["configuration"].items():
            wrongs.append(json.dumps({**DESCRIPTOR, "configuration": {
                **DESCRIPTOR["configuration"], key: value + 1}}))
            wrongs.append(json.dumps({**DESCRIPTOR, "configuration": {
                k: v for k, v in DESCRIPTOR["configuration"].items() if k != key}}))
        wrongs.append(HOST.replace('"device_count": 1', '"device_count": true'))
        for wrong in wrongs:
            self.assertFalse(m.host_500(wrong), wrong)

    def execute(self, *, host=HOST, failure=None, binds=None, clock=None):
        if clock is None:
            clock = m.time.monotonic
        calls = []
        self.commands = {}
        state = "\nphase=idle reason=initialized-read-only error=0 held_ref=0 mutated=0 usb_pilot=0\n"
        responses = {
            "identity": "\nI14_IDENTITY_OK\nUid:\t0\t0\t0\t0\ncdf27fdf-5147-40a3-a9aa-096efa22f26a\nI14_CACHED_CAPTURE_DONE\n",
            "guards": "\nMODULES_EMPTY\nSTORAGE=26:0:30535680\n" + "WDT_FLG=00000000 INT_RAW=00000000 INT_FLG=00000000\n" * 3 + "1\n2\n",
            "prepare": "\nPREP_RC=0\n", "load": "\nLOAD_RC=0\n" + state,
            "helper": "\nSTARTED /run/i14-usb-power-500.123\nHELPER_RC=0\n",
            "helper-log": "\nDONE descriptor=500mA; host configuration still requires verification\n",
            "usb-guards": "\n500\nconfigured\n0\n1\n" + state,
            "pilot": "\nPILOT_RC=0\n",
            "cached-result": "\nphase=done reason=deadline error=0 held_ref=0 mutated=1\ncore_uncertain=0x0 adc_uncertain=0x0 stop_verified=1\n",
        }
        if failure:
            responses[failure[0]] = failure[1]
        self.waiting_commands = []
        def run(name, cmd):
            calls.append(name)
            self.commands[name] = cmd
            if name == "helper-log":
                self.waiting_commands.append(len(binds or []))
            return responses[name]
        with tempfile.TemporaryDirectory() as d:
            runner = m.Runner(Path(d))
            (Path(d) / "read-i14-usb-descriptor.py").write_text("# test descriptor snapshot\n")
            def bind(address):
                if binds:
                    result = binds.pop(0)
                    if result:
                        raise OSError("source address not assigned")
            # This fictional package only exercises host flow; it is no runtime qualification.
            with patch.object(m, "PACK", Path(d) / "unqualified-test-pack"), \
                 patch.object(m, "PINS", {"i14-charger.ko": "0" * 64}), \
                 patch.object(runner, "run", side_effect=run), patch.object(m.time, "sleep"), \
                 patch.object(m.time, "monotonic", side_effect=clock), \
                 patch.object(m.socket, "socket") as sock, \
                 patch.object(m.native, "scan", return_value=[{"idVendor": 0x1d6b, "idProduct": 0x0104, "USB Serial Number": "I14_NATIVE_INIT", "locationID": 0x2110000}]), \
                 patch.object(m.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=host, stderr="")):
                sock.return_value.__enter__.return_value.bind.side_effect = bind
                try:
                    runner.execute({})
                    return calls, None
                except RuntimeError as error:
                    return calls, error

    def test_reconnect_waits_for_source_address_before_native_command(self):
        calls, error = self.execute(binds=[True, True, False], clock=range(10))
        self.assertIsNone(error)
        self.assertEqual(self.waiting_commands, [0])
        self.assertEqual(calls.count("helper-log"), 1)

    def test_reconnect_deadline_without_source_address_sends_no_command(self):
        calls, error = self.execute(binds=[True], clock=[0, 1, 61])
        self.assertIsNotNone(error)
        self.assertNotIn("helper-log", calls)
        self.assertNotIn("pilot", calls)

    def test_missing_host_descriptor_never_starts_pilot(self):
        calls, error = self.execute(host="")
        self.assertIsNotNone(error)
        self.assertNotIn("pilot", calls)

    def test_unreadable_modules_is_not_treated_as_empty(self):
        self.execute(failure=("guards", ""))
        result = subprocess.run(["/bin/sh", "-c", "cat() { return 1; }; " + self.commands["guards"]],
                                capture_output=True, text=True, timeout=3)
        self.assertNotIn("MODULES_EMPTY", result.stdout)
        calls, error = self.execute(failure=("guards", result.stdout))
        self.assertIsNotNone(error)
        self.assertNotIn("prepare", calls)

    def test_every_failed_precondition_stops_before_pilot(self):
        for failure in (("identity", "\nI14_STOP_IDENTITY\n"), ("guards", "\nMODULES_NOT_EMPTY\n"),
                        ("prepare", "\nPREP_RC=1\n"), ("load", "\nLOAD_RC=1\n"),
                        ("helper", "\nHELPER_RC=1\n"), ("helper-log", "\nERROR\n"),
                        ("usb-guards", "\n500\nconfigured\n1\n1\n")):
            with self.subTest(failure=failure):
                calls, error = self.execute(failure=failure)
                self.assertIsNotNone(error)
                self.assertNotIn("pilot", calls)

    def test_mocked_unblocked_path_starts_exactly_one_pilot(self):
        calls, error = self.execute()
        self.assertIsNone(error)
        self.assertEqual(calls.count("pilot"), 1)

    def test_unverified_stop_is_failure_without_second_pilot(self):
        calls, error = self.execute(failure=("cached-result", "\nphase=latched error=-5 held_ref=1 stop_verified=0\n"))
        self.assertIsNotNone(error)
        self.assertEqual(calls.count("pilot"), 1)


if __name__ == "__main__":
    unittest.main()
