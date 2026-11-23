"""Synthetic files only. Native identity/process inputs are mocked; no transport."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
def load(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace('-', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
guard = load(ROOT/'scripts/i14-soak-phase-export.py')
export = load(ROOT/'scripts/i14-soak-observer-export.py')
inventory = load(ROOT/'scripts/i14-soak-observer-inventory.py')
real_actors = guard.actors
sha = lambda raw: hashlib.sha256(raw).hexdigest()
BOOT = '00000000-0000-4000-8000-000000000014'

class PhaseExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data = self.base/'observer'; self.data.mkdir(mode=0o700)
        self.run = self.data/'phase-20261116T020000Z'; self.run.mkdir(mode=0o700)
        self.cron = self.base/'root-cron'
        self.original = b'*/1 * * * * existing-sampler\n'
        self.write(self.cron, self.original)
        self.sources = {
            'i14-soak-local-collector.py': (ROOT/'scripts/i14-soak-local-collector.py').read_bytes(),
            'i14-soak-observer-inventory.py': (ROOT/'scripts/i14-soak-observer-inventory.py').read_bytes(),
            'procs.sh': (ROOT/'scripts/i14-soak-procs.sh').read_bytes(),
        }
        self.deps = {name: (ROOT/'scripts'/name).read_bytes() for name in
                     ['i14-soak-observer-export.py', 'i14-soak-observer-inventory.py']}
        for name, raw in self.sources.items(): self.write(self.run/name, raw, 0o700)
        config = dict(schema=2, boot_id=BOOT, grant_id='phase-one', expires_epoch=2000,
                      max_captures=80, max_bytes=1281<<20, capture_bytes=16<<20,
                      reserve_bytes=512<<20, source_sha256={n:sha(b) for n,b in self.sources.items()})
        self.config = json.dumps(config, sort_keys=True).encode()
        self.write(self.run/'observer.json', self.config)
        self.write(self.run/'root-crontab.before', self.original)
        self.close = dict(result='CRON_RESTORED_NO_ACTOR', grant_id='close-one', run_dir=str(self.run),
                          boot_id=BOOT, config_sha256=sha(self.config),
                          source_sha256=config['source_sha256'], cronline_sha256='b'*64,
                          restored_cron_sha256=sha(self.original))
        self.close_raw = json.dumps(self.close, sort_keys=True).encode()
        self.write(self.run/'close-receipt.json', self.close_raw)
        self.bundle = dict(phase_id='phase-one', grant_id='export-one', close_grant_id='close-one',
                           run_dir=str(self.run), boot_id=BOOT, config_sha256=sha(self.config),
                           source_sha256=config['source_sha256'], cronline_sha256='b'*64,
                           original_cron_sha256=sha(self.original), close_receipt_sha256=sha(self.close_raw),
                           start_epoch=1000, deadline_epoch=1800)
        # Only fixture filesystem/owner/native-proc inputs differ from production constants.
        self.addCleanup(patch.stopall)
        patch.object(guard, 'BASE', self.data, create=True).start()
        patch.object(guard, 'CRON', self.cron, create=True).start()
        patch.object(guard, 'OWNER', os.getuid(), create=True).start()
        self.native = patch.object(guard, 'native', return_value=BOOT, create=True).start()
        self.actors = patch.object(guard, 'actors', return_value=[], create=True).start()

    def write(self, path, raw, mode=0o600):
        path.write_bytes(raw); path.chmod(mode)

    def call(self, bundle=None, deps=None, clock=None, stream=None):
        return guard.stream_phase(bundle or self.bundle, deps or self.deps,
                                  stream or io.BytesIO(), clock or (lambda: 1100))

    def test_complete_bound_tar_roundtrip(self):
        target = io.BytesIO(); result = self.call(stream=target)
        self.assertEqual(result.get('result'), 'CLOSED_PHASE_STREAMED_NOT_CLEANUP_AUTHORIZED')
        manifest = export.unpack_tar(io.BytesIO(target.getvalue()), self.base/'received',
                    {k:self.bundle[k] for k in ['phase_id','grant_id','boot_id','config_sha256']})
        self.assertEqual((self.base/'received'/'observer.json').read_bytes(), self.config)
        self.assertEqual(manifest['total_bytes'], sum(p.stat().st_size for p in self.run.iterdir()))

    def test_foreign_close_provenance_is_refused(self):
        changed = {**self.close, 'grant_id':'foreign-close'}
        raw = json.dumps(changed).encode(); self.write(self.run/'close-receipt.json', raw)
        bundle = {**self.bundle, 'close_receipt_sha256':sha(raw)}
        with self.assertRaises(ValueError): self.call(bundle=bundle)

    def test_active_collector_refused_before_archive(self):
        self.actors.return_value = [88]
        target = io.BytesIO()
        with self.assertRaises(ValueError): self.call(stream=target)
        self.assertEqual(target.getvalue(), b'')

    def test_cron_modified_after_restoration_refused(self):
        self.write(self.cron, self.original+b'foreign\n')
        with self.assertRaises(ValueError): self.call()

    def test_config_bytes_refused(self):
        self.write(self.run/'observer.json', self.config+b' ')
        with self.assertRaises(ValueError): self.call()

    def test_source_bytes_refused(self):
        self.write(self.run/'procs.sh', b'changed', 0o700)
        with self.assertRaises(ValueError): self.call()

    def test_dependency_pin_refused(self):
        deps = dict(self.deps); deps['i14-soak-observer-export.py'] += b'\n'
        with self.assertRaises(ValueError): self.call(deps=deps)

    def test_future_expired_overlong_lease_refused(self):
        for times in [(1200,1800),(100,1050),(1000,1901)]:
            with self.subTest(times=times), self.assertRaises(ValueError):
                self.call(bundle={**self.bundle,'start_epoch':times[0],'deadline_epoch':times[1]})

    def test_lock_and_foreign_namespace_refused(self):
        self.write(self.run/'.capture.lock', b'')
        with self.assertRaises(ValueError): self.call()
        (self.run/'.capture.lock').unlink()
        with self.assertRaises(ValueError): self.call(bundle={**self.bundle,'run_dir':str(self.base)})

    def test_late_lease_midstream_refused(self):
        class Late(io.BytesIO):
            pass
        target=Late(); state={'now':1100}
        original=target.write
        def write(raw):
            n=original(raw); state['now']=1800; return n
        target.write=write
        with self.assertRaises(ValueError): self.call(clock=lambda:state['now'], stream=target)

    def test_after_stream_identity_change_refused(self):
        self.native.side_effect=[BOOT,ValueError('changed boot')]
        with self.assertRaises(ValueError): self.call()

    def test_actor_appearing_after_stream_refused(self):
        self.actors.side_effect = [[], [88]]
        with self.assertRaises(ValueError): self.call()

    def test_cron_change_during_stream_refused_even_with_complete_tar(self):
        class Changed(io.BytesIO):
            pass
        target = Changed(); original = target.write
        def write(raw):
            self.write(self.cron, self.original+b'foreign\n')
            return original(raw)
        target.write = write
        with self.assertRaises(ValueError): self.call(stream=target)
        # Complete tar bytes do not imply caller rc0 or permission to clean up.
        self.assertGreater(len(target.getvalue()), 0)

    def test_short_output_write_refused(self):
        class Short(io.BytesIO):
            def write(self, raw):
                return max(0, len(raw)-1)
        with self.assertRaises(ValueError): self.call(stream=Short())

    def test_inexact_schema_or_phase_config_refused(self):
        with self.assertRaises(ValueError): self.call(bundle={**self.bundle, 'extra': True})
        with self.assertRaises(ValueError): self.call(bundle={**self.bundle, 'phase_id': 'foreign'})

    def test_private_root_mode_refused(self):
        self.run.chmod(0o755)
        target = io.BytesIO()
        with self.assertRaises(ValueError): self.call(stream=target)
        self.assertEqual(target.getvalue(), b'')

    def test_symlink_phase_refused_without_touching_foreign_files(self):
        foreign=self.base/'foreign'; foreign.mkdir(); link=self.data/'phase-20261116T030000Z';link.symlink_to(foreign)
        with self.assertRaises(ValueError): self.call(bundle={**self.bundle,'run_dir':str(link)})
        self.assertEqual(list(foreign.iterdir()),[])

    def test_R1_manifest_provenance_swap_restored_after_tar_refused(self):
        original_inventory = export.inventory
        original_export = export.export_tar
        target = io.BytesIO()
        def swapped(fd):
            self.write(self.run/'observer.json', self.config+b' ')
            return original_inventory(fd)
        def restore_after_tar(*args):
            result = original_export(*args)
            self.write(self.run/'observer.json', self.config)
            return result
        with patch.object(guard, 'dependencies', return_value=(export,inventory)), \
             patch.object(export, 'inventory', side_effect=swapped), \
             patch.object(export, 'export_tar', side_effect=restore_after_tar):
            with self.assertRaises(ValueError): self.call(stream=target)
        self.assertEqual(target.getvalue(), b'')

    def test_R1_manifest_required_mode_swap_refused(self):
        original_inventory = export.inventory
        original_export = export.export_tar
        target = io.BytesIO()
        def swapped(fd):
            (self.run/'observer.json').chmod(0o700)
            return original_inventory(fd)
        def restore_after_tar(*args):
            result = original_export(*args)
            (self.run/'observer.json').chmod(0o600)
            return result
        with patch.object(guard, 'dependencies', return_value=(export,inventory)), \
             patch.object(export, 'inventory', side_effect=swapped), \
             patch.object(export, 'export_tar', side_effect=restore_after_tar):
            with self.assertRaises(ValueError): self.call(stream=target)
        self.assertEqual(target.getvalue(), b'')

    def actor_fixture(self, argv, cwd=None, error=None):
        context = MagicMock()
        context.__enter__.return_value = iter([SimpleNamespace(name='47',path='/fixture/proc/47')])
        with patch.object(guard.os, 'scandir', return_value=context), \
             patch.object(guard, 'fixed', return_value=b'\0'.join(a.encode() for a in argv)+b'\0'), \
             patch.object(guard.os, 'readlink', return_value=cwd, side_effect=error) as readlink:
            found = real_actors(self.run)
            return found, readlink.call_count

    def test_R2_relative_collector_actor_found(self):
        found, _ = self.actor_fixture(['python3','i14-soak-local-collector.py',
                       '--run-dir',str(self.run),'--origin','manual'], str(self.run))
        self.assertEqual(found, [47])

    def test_R2_normalized_absolute_collector_actor_found(self):
        found, _ = self.actor_fixture(['python3',str(self.run)+'/./i14-soak-local-collector.py',
                                      '--run-dir='+str(self.run),'--origin','manual'])
        self.assertEqual(found, [47])

    def test_R2_unresolved_relevant_cwd_refused(self):
        with self.assertRaises(ValueError):
            self.actor_fixture(['python3','i14-soak-local-collector.py','--run-dir',str(self.run)],
                               error=PermissionError('fixture cwd denied'))

    def test_R2_unknown_script_alias_for_this_phase_refused(self):
        with self.assertRaises(ValueError):
            self.actor_fixture(['python3','alias.py','--run-dir',str(self.run)],str(self.run))

    def test_R2_external_alias_relative_run_directory_refused(self):
        with self.assertRaises(ValueError):
            self.actor_fixture(['python3','/fixture/external-alias.py','--run-dir','.','--origin','manual'],
                               str(self.run))

    def test_R2_external_alias_equals_relative_run_directory_refused(self):
        with self.assertRaises(ValueError):
            self.actor_fixture(['python3','/fixture/external-alias.py','--run-dir=.','--origin','manual'],
                               str(self.run))

    def test_R2_external_alias_empty_run_directory_refused(self):
        with self.assertRaises(ValueError):
            self.actor_fixture(['python3','/fixture/external-alias.py','--run-dir','','--origin','manual'],
                               str(self.run))

    def test_external_alias_for_foreign_run_directory_is_irrelevant(self):
        found, _ = self.actor_fixture(['python3','/fixture/external-alias.py','--run-dir','other'],
                                     str(self.base/'foreign'))
        self.assertEqual(found, [])

    def test_R2_external_alias_abbreviated_run_directory_refused(self):
        for option in ['--r','--run-d','--run-di=.']:
            args=[option] if '=' in option else [option,'.']
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.actor_fixture(['python3','/fixture/external-alias.py',*args,'--origin','manual'],
                                   str(self.run))

    def test_unrelated_actor_does_not_read_cwd(self):
        found, reads = self.actor_fixture(['unrelated','--ordinary'])
        self.assertEqual((found,reads), ([],0))

    def test_R3_deadline_during_final_actor_check_refused(self):
        state = {'now':1100}
        def final_actor_check(root):
            state['now'] = 1800
            return []
        calls=[]
        def actors(root):
            calls.append(root)
            return final_actor_check(root) if len(calls)==2 else []
        self.actors.side_effect = actors
        with self.assertRaises(ValueError): self.call(clock=lambda:state['now'])

if __name__ == '__main__': unittest.main()
