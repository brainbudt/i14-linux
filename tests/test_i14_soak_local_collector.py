"""Local fixtures only; no phone, real cron, network or live logs."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import os
import stat
import json
import hashlib
import time
import signal
from datetime import timedelta
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts/i14-soak-local-collector.py"
collector = None
if SOURCE.is_file():
    spec = importlib.util.spec_from_file_location("local_collector", SOURCE)
    collector = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(collector)

BOOT = "00000000-0000-4000-8000-000000000014"


class LocalCollector(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(collector, "local collector not implemented")

    def test_copy_preserves_required_bytes_and_optional_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            target = root / "capture"
            source.mkdir()
            target.mkdir()
            (source / "messages").write_bytes(b"Nov 15 18:00:00 i14 raw\n")
            (source / "i14-battery.log").write_bytes(b"2026-11-15 18:00:00 raw\n")
            result = collector.copy_logs(source, target, 4096)
            self.assertFalse(result["failed"])
            self.assertEqual((target / "messages").read_bytes(), (source / "messages").read_bytes())
            self.assertIn("messages.4", result["absent"])
            self.assertFalse((target / "messages.4").exists())

    def test_missing_required_log_is_failure_not_empty_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            target = root / "capture"
            source.mkdir()
            target.mkdir()
            result = collector.copy_logs(source, target, 4096)
            self.assertIn("messages(absent)", result["failed"])
            self.assertFalse((target / "messages").exists())

    def test_unexpected_symlink_and_budget_are_refused_without_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            source = root / "source"
            target = root / "capture"
            source.mkdir()
            target.mkdir()
            private = root / "private"
            private.write_bytes(b"must not copy\n")
            (source / "messages").symlink_to(private)
            (source / "i14-battery.log").write_bytes(b"x" * 100)
            result = collector.copy_logs(source, target, 20)
            self.assertIn("messages(read)", result["failed"])
            self.assertIn("i14-battery.log(budget)", result["failed"])
            self.assertFalse((target / "messages").exists())
            self.assertFalse((target / "i14-battery.log").exists())

    def test_manual_flag_cannot_prove_root_cron(self):
        rows = [{"pid": 2, "uid": 0, "comm": "sshd"}]
        self.assertFalse(collector.cron_origin(rows))
        self.assertFalse(collector.cron_origin([{"pid": 2, "uid": 1000, "comm": "crond"}]))
        self.assertTrue(collector.cron_origin([{"pid": 2, "uid": 0, "comm": "crond"}]))

    def test_expiry_count_and_storage_limits_are_terminal(self):
        config = {"expires_epoch": 200, "max_captures": 2, "max_bytes": 1000,
                  "reserve_bytes": 100, "capture_bytes": 200}
        collector.check_limits(config, 100, 0, 0, 1000)
        for now, count, used, free in [(200, 0, 0, 1000), (100, 2, 0, 1000),
                                       (100, 0, 900, 1000), (100, 0, 0, 299)]:
            with self.assertRaises(ValueError):
                collector.check_limits(config, now, count, used, free)

    def test_process_capture_requires_expected_boot_and_unique_rows(self):
        raw = ("2026-11-15T18:00:00Z\n" + BOOT + "\n" +
               "".join(f"{pid} 100 /service\n" for pid in range(10, 20))).encode()
        collector.validate_procs(raw, BOOT)
        with self.assertRaises(ValueError):
            collector.validate_procs(raw.replace(BOOT.encode(), b"other"), BOOT)
        with self.assertRaises(ValueError):
            collector.validate_procs(raw + b"10 100 /service\n", BOOT)

    def test_index_preserves_old_bytes_and_explicit_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "index.txt"
            path.write_bytes(b"prior evidence\n")
            collector.append_index(path, "20261115T180000Z", ["procs"], ["messages.4"], ["messages(read)"])
            self.assertTrue(path.read_bytes().startswith(b"prior evidence\n"))
            self.assertIn(b"failed: messages(read)", path.read_bytes())

    def test_index_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            evidence = root / 'evidence'
            evidence.write_bytes(b'unchanged')
            index = root / 'index.txt'
            index.symlink_to(evidence)
            with self.assertRaises(OSError):
                collector.append_index(index, '20261115T180000Z', [], [], [])
            self.assertEqual(evidence.read_bytes(), b'unchanged')

    def test_process_capture_has_output_limit_and_reaps_own_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = root / 'procs.sh'
            script.write_text('yes x\n')
            result = collector.capture_procs(script, root, timeout=1)
            self.assertNotEqual(result['rc'], 0)
            self.assertTrue(result['group_closed'])
            for name in ['procs.txt', 'procs.err']:
                self.assertLessEqual((root/name).stat().st_size, 32768)

    def test_json_receipt_is_private_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve()/'receipt.json'
            collector.write_json(path, {'result': 'PASS'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(ValueError):
                collector.write_json(path.with_name('oversized.json'), {'raw': 'x'*32768})

    def test_interruption_propagates_without_reading_subsequent_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            calls=[]
            def cancelled(path):
                calls.append(path.name)
                raise collector.CaptureInterrupted('real capture cancellation')
            with patch.object(collector._inventory,'read_snapshot',side_effect=cancelled):
                with self.assertRaises(collector.CaptureInterrupted):
                    collector.copy_logs(root,root,4096)
            self.assertEqual(calls,['messages'])

    def test_source_location_must_match_executing_modules(self):
        collector.assert_source_location(SOURCE.parent)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            with self.assertRaises(ValueError):
                collector.assert_source_location(root)
            pins={}
            for name in ['i14-soak-local-collector.py','i14-soak-observer-inventory.py','procs.sh']:
                (root/name).write_bytes(b'not executing code')
                (root/name).chmod(0o700)
                pins[name]=hashlib.sha256(b'not executing code').hexdigest()
            config={'schema':1,'grant_id':'fixture','expires_epoch':200,'max_captures':1,
                    'max_bytes':1<<20,'reserve_bytes':1<<20,'capture_bytes':1<<19,'source_sha256':pins}
            (root/'observer.json').write_text(json.dumps(config));(root/'observer.json').chmod(0o600)
            with self.assertRaises(ValueError):
                collector.load_config(root)

    def test_main_fixture_captures_raw_and_real_ancestry_flag_and_closes_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            root = base/'phase-20261115T180000Z'
            root.mkdir(mode=0o700)
            for name in ['logs', 'receipts']:
                (root/name).mkdir(mode=0o700)
            source = base/'varlog'
            source.mkdir()
            for name in ['messages', 'i14-battery.log']:
                (source/name).write_bytes(b'raw original\n')
            procs = 'printf "2026-11-15T18:00:00Z\\n' + BOOT + '\\n"\n'
            procs += ''.join(f'echo "{pid} 100 /service"\n' for pid in range(10, 20))
            pins = {}
            for name, body in [('i14-soak-local-collector.py', SOURCE.read_bytes()),
                               ('i14-soak-observer-inventory.py', SOURCE.with_name('i14-soak-observer-inventory.py').read_bytes()),
                               ('procs.sh', procs.encode())]:
                (root/name).write_bytes(body)
                (root/name).chmod(0o700)
                pins[name] = hashlib.sha256(body).hexdigest()
            config = {'schema':2, 'boot_id':BOOT, 'grant_id':'fixture-only', 'expires_epoch':int(time.time())+120,
                      'max_captures':3, 'max_bytes':50<<20, 'reserve_bytes':512<<20,
                      'capture_bytes':16<<20, 'source_sha256':pins}
            (root/'observer.json').write_text(json.dumps(config))
            (root/'observer.json').chmod(0o600)
            # Load the exact pinned source from its real fixture destination.
            global collector
            previous_collector = collector
            spec=importlib.util.spec_from_file_location('installed_fixture_collector',root/'i14-soak-local-collector.py')
            collector=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(collector)
            self.addCleanup(lambda:globals().__setitem__('collector',previous_collector))
            ancestry = [{'pid':2,'ppid':1,'uid':0,'comm':'crond','start_ticks':10}]
            original_datetime=collector.datetime
            class AdvancingDateTime(original_datetime):
                counter=0
                @classmethod
                def now(cls,tz):
                    cls.counter+=1
                    return original_datetime.now(tz)+timedelta(seconds=cls.counter)
            # All identity/cron facts are local fixtures, never device acceptance.
            with patch.object(collector, 'BASE', base), patch.object(collector, 'LOG_ROOT', source), \
                 patch.object(collector, 'runtime_identity', return_value=BOOT), \
                 patch.object(collector, 'ancestry', return_value=ancestry), \
                 patch.object(collector, 'datetime', AdvancingDateTime), \
                 patch.object(collector.os, 'geteuid', return_value=0), \
                 patch.object(collector.time, 'sleep'):
                # macOS fixtures are owned by the current uid; emulate target uid0 metadata gates.
                original_stat = Path.stat
                original_lstat = Path.lstat
                def root_stat(path, *args, **kwargs):
                    value = original_stat(path, *args, **kwargs)
                    if path in [root, root/'logs', root/'receipts']:
                        fields = list(value); fields[4] = 0
                        return os.stat_result(fields)
                    return value
                def root_lstat(path):
                    return root_stat(path, follow_symlinks=False)
                with patch.object(Path, 'stat', root_stat), patch.object(Path, 'lstat', root_lstat), \
                     patch.object(collector, 'owned_file', side_effect=lambda p, m:p.read_bytes()):
                    self.addCleanup(os.umask, os.umask(0o077))
                    (root/'observer.json').write_text(json.dumps(dict(config, max_captures=81)))
                    with self.assertRaisesRegex(ValueError, 'exporter'):
                        collector.main(['--run-dir',str(root),'--origin','cron'])
                    (root/'observer.json').write_text(json.dumps(config))
                    with patch.object(collector, 'runtime_identity', return_value='different'), \
                            self.assertRaisesRegex(ValueError, 'boot differs'):
                        collector.main(['--run-dir',str(root),'--origin','cron'])
                    self.assertEqual(collector.main(['--run-dir',str(root),'--origin','cron']),0)
                    # Real local signals during the first read must skip all later reads/helper.
                    for number in [signal.SIGALRM,signal.SIGTERM]:
                        calls=[]
                        def cancel_at_first_read(source,target,budget):
                            original_read=collector._inventory.read_snapshot
                            def raise_signal(path):
                                calls.append(path.name)
                                signal.raise_signal(number)
                                return original_read(path)
                            with patch.object(collector._inventory,'read_snapshot',side_effect=raise_signal):
                                return collector.copy_logs_original(source,target,budget)
                        collector.copy_logs_original=collector.copy_logs
                        with patch.object(collector,'copy_logs',side_effect=cancel_at_first_read), \
                             patch.object(collector,'capture_procs',return_value={'rc':0,'group_closed':True}) as procs:
                            self.assertEqual(collector.main(['--run-dir',str(root),'--origin','cron']),1)
                            procs.assert_not_called()
                        self.assertEqual(calls,['messages'])
                    self.assertEqual(collector.main(['--run-dir',str(root),'--origin','cron']),3)
                    with patch.object(collector, 'usage', side_effect=AssertionError('resumed stopped phase')):
                        self.assertEqual(collector.main(['--run-dir',str(root),'--origin','cron']),3)
            receipts=[json.loads(p.read_text()) for p in (root/'receipts').iterdir()]
            receipt=next(p for p in receipts if p['result']=='PASS')
            self.assertEqual(sorted(p['interruption_signal'] for p in receipts if 'interruption_signal' in p),
                             sorted([signal.SIGALRM,signal.SIGTERM]))
            self.assertEqual(receipt['result'], 'PASS')
            self.assertTrue(receipt['cron_origin_proven'])
            self.assertTrue(receipt['process_actor']['group_closed'])
            self.assertFalse((root/'.capture.lock').exists())
            capture = next(p for p in (root/'logs').iterdir() if p.is_dir() and (p/'messages').exists())
            self.assertEqual((capture/'messages').read_bytes(), b'raw original\n')


if __name__ == "__main__":
    unittest.main()
