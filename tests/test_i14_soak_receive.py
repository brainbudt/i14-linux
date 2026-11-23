"""Exercise the host transport boundary with a local fixture process."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/i14-soak-receive.py"
spec = importlib.util.spec_from_file_location("receiver", SCRIPT)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class ReceiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        source = self.root / "source"
        source.mkdir(mode=0o700)
        (source / "messages").write_bytes(b"fixture\n")
        (source / "messages").chmod(0o600)
        self.binding = dict(phase_id="fixture", grant_id="fixture-export",
                            boot_id="00000000-0000-4000-8000-000000000014", config_sha256="a" * 64)
        self.archive = self.root / "fixture.tar"
        with self.archive.open("xb") as output:
            receiver.export.export_tar(source, receiver.export.inventory(source), self.binding, output)
        self.destination = self.root / "received"

    def send(self, body, seconds=5):
        program = ("import os, sys, time\nfrom pathlib import Path\n"
                   "Path(sys.argv[2]).write_text(str(os.getpid()))\n" + body)
        return receiver.receive([sys.executable, "-c", program, str(self.archive), str(self.root / "pid")],
                                self.destination, self.binding, seconds)

    send_archive = "sys.stdout.buffer.write(Path(sys.argv[1]).read_bytes()); sys.stdout.buffer.flush()\n"

    def test_normal_exit_and_persisted_bytes(self):
        self.send(self.send_archive)
        self.assertEqual((self.destination / "raw/messages").read_bytes(), b"fixture\n")
        self.assertEqual(json.loads((self.destination / "verified.json").read_text())["transport_exit"], 0)

    def test_cli_passes_owner_stdin_through_foreground_transport(self):
        binding_file = self.root / "binding.json"
        binding_file.write_text(json.dumps(self.binding))
        with self.archive.open("rb") as fixture_stream:
            subprocess.run([sys.executable, "-B", str(SCRIPT), "--binding", str(binding_file),
                            "--out", str(self.destination), "--timeout", "5", "--", "/bin/cat"],
                           stdin=fixture_stream, check=True, timeout=10)
        self.assertEqual((self.destination / "raw/messages").read_bytes(), b"fixture\n")

    def test_full_valid_archive_followed_by_nonzero_exit(self):
        with self.assertRaisesRegex(ValueError, "exited 7"):
            self.send(self.send_archive + "sys.exit(7)")
        self.assertFalse((self.destination / "verified.json").exists())

    def test_missing_and_partial_output(self):
        for index, body in enumerate(["pass", "sys.stdout.buffer.write(b'partial')"]):
            self.destination = self.root / f"incomplete-{index}"
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.send(body)
            self.assertFalse((self.destination / "verified.json").exists())

    def test_timeout_before_output_and_after_complete_archive_reaps_transport(self):
        for index, prefix in enumerate(["", self.send_archive + "os.close(1)\n"]):
            self.destination = self.root / f"timeout-{index}"
            with self.subTest(prefix=prefix), self.assertRaises((ValueError, TimeoutError, subprocess.TimeoutExpired)):
                self.send(prefix + "time.sleep(30)", seconds=1)
            with self.assertRaises(ProcessLookupError):
                os.killpg(int((self.root / "pid").read_text()), 0)
            self.assertFalse((self.destination / "verified.json").exists())
