import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('observer_export',
    Path(__file__).resolve().parents[1]/'scripts/i14-soak-observer-export.py')
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


class ObserverExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.root = self.base/'raw'
        self.root.mkdir(mode=0o700)
        (self.root/'logs').mkdir(mode=0o700)
        self.body = b'raw\x00bytes\xff\n'*1000
        (self.root/'logs'/'messages').write_bytes(self.body)
        (self.root/'logs'/'messages').chmod(0o600)
        self.binding = {'phase_id':'D-example', 'grant_id':'export-example',
                        'boot_id':'00000000-0000-4000-8000-000000000014',
                        'config_sha256':'a'*64}

    def tearDown(self):
        self.temp.cleanup()

    def archive(self):
        output = io.BytesIO()
        export.export_tar(self.root, export.inventory(self.root), self.binding, output)
        return output.getvalue()

    def test_roundtrip_preserves_exact_bytes_and_private_files(self):
        raw = self.archive()
        self.assertGreater(len(raw), len(self.body))
        target = self.base/'received'
        result = export.unpack_tar(io.BytesIO(raw), target, self.binding)
        self.assertEqual((target/'logs/messages').read_bytes(), self.body)
        self.assertEqual((target/'logs/messages').stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        self.assertEqual(result['files']['logs/messages']['sha256'], hashlib.sha256(self.body).hexdigest())

    def test_changed_source_is_refused_without_complete_receipt(self):
        manifest = export.inventory(self.root)
        (self.root/'logs/messages').write_bytes(b'changed')
        with self.assertRaises(ValueError):
            export.export_tar(self.root, manifest, self.binding, io.BytesIO())

    def test_symlink_root_or_ancestor_is_refused(self):
        (self.base/'link').symlink_to(self.root, target_is_directory=True)
        for path in [self.base/'link', self.base/'link/logs']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                export.inventory(path)

    def test_symlink_inside_tree_is_refused(self):
        (self.root/'logs/linked').symlink_to(self.root/'logs/messages')
        with self.assertRaises(ValueError):
            export.inventory(self.root)

    def test_oversized_file_is_refused_before_read(self):
        with (self.root/'large').open('wb') as f:
            f.truncate(16*1024*1024+1)
        with self.assertRaises(ValueError):
            export.inventory(self.root)

    def test_foreign_binding_is_refused_before_destination_creation(self):
        raw = self.archive()
        target = self.base/'received'
        with self.assertRaises(ValueError):
            export.unpack_tar(io.BytesIO(raw), target, {**self.binding,'phase_id':'other'})
        self.assertFalse(target.exists())

    def test_partial_archive_is_not_accepted(self):
        raw = self.archive()
        with self.assertRaises(ValueError):
            export.unpack_tar(io.BytesIO(raw[:len(self.body)]), self.base/'received', self.binding)

    def test_unsafe_tar_name_is_refused(self):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w') as archive:
            item = tarfile.TarInfo('../escape'); item.size=3
            archive.addfile(item,io.BytesIO(b'bad'))
        with self.assertRaises(ValueError):
            export.unpack_tar(io.BytesIO(raw.getvalue()),self.base/'received',self.binding)
        self.assertFalse((self.base/'escape').exists())

    def crafted(self, members):
        manifest = export.inventory(self.root)
        first = json.dumps({'schema':1,'binding':self.binding,'manifest':manifest}).encode()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw,mode='w') as archive:
            item=tarfile.TarInfo('EXPORT-MANIFEST.json');item.size=len(first)
            archive.addfile(item,io.BytesIO(first))
            for name, data, kind in members:
                item=tarfile.TarInfo(name)
                if kind == 'link':
                    item.type=tarfile.SYMTYPE; item.linkname='/etc/passwd'
                    archive.addfile(item)
                else:
                    item.size=len(data);archive.addfile(item,io.BytesIO(data))
        return raw.getvalue()

    def test_duplicate_or_symlink_member_is_refused(self):
        for members in [[('logs/messages',self.body,'file')]*2,
                        [('logs/messages',b'','link')]]:
            with self.subTest(members=len(members)), tempfile.TemporaryDirectory(dir=self.base) as tmp:
                with self.assertRaises(ValueError):
                    export.unpack_tar(io.BytesIO(self.crafted(members)),Path(tmp)/'received',self.binding)

    def test_corrupted_member_is_refused(self):
        corrupted = b'x' + self.body[1:]
        with self.assertRaisesRegex(ValueError,'hash'):
            export.unpack_tar(io.BytesIO(self.crafted([('logs/messages',corrupted,'file')])),
                              self.base/'received',self.binding)

    def test_existing_destination_is_never_overwritten(self):
        target=self.base/'received';target.mkdir(mode=0o700)
        (target/'keep').write_bytes(b'original')
        with self.assertRaises(ValueError):
            export.unpack_tar(io.BytesIO(self.archive()),target,self.binding)
        self.assertEqual((target/'keep').read_bytes(),b'original')

    def test_change_during_stream_is_refused(self):
        test=self
        class ChangingSink(io.BytesIO):
            def write(self,data):
                (test.root/'logs/messages').write_bytes(b'x'*len(test.body))
                return super().write(data)
        with self.assertRaises(ValueError):
            export.export_tar(self.root,export.inventory(self.root),self.binding,ChangingSink())

    def test_total_quota_is_checked_before_any_file_read(self):
        value={'files':{str(i):{'bytes':16<<20,'sha256':'a'*64} for i in range(81)},
               'total_bytes':81*(16<<20)}
        with self.assertRaisesRegex(ValueError,'quota'):
            export.validate_manifest(value)

    def test_extended_header_is_refused_before_its_body(self):
        item=tarfile.TarInfo('extended')
        item.type=tarfile.XHDTYPE;item.size=2<<20
        raw=item.tobuf(format=tarfile.USTAR_FORMAT)
        with self.assertRaisesRegex(ValueError,'regular'):
            export.unpack_tar(io.BytesIO(raw),self.base/'received',self.binding)

    def test_persisted_output_is_rehashed_before_acceptance(self):
        raw=self.archive()
        def corrupt_after_flush(fd):
            os.pwrite(fd,b'X',0)
        with mock.patch.object(export.os,'fsync',side_effect=corrupt_after_flush):
            with self.assertRaisesRegex(ValueError,'persisted'):
                export.unpack_tar(io.BytesIO(raw),self.base/'received',self.binding)

    def test_nested_capture_directories_are_private(self):
        nested=self.root/'logs/20261115T193308Z';nested.mkdir(mode=0o700)
        (self.root/'logs/messages').rename(nested/'messages')
        target=self.base/'received'
        try:
            export.unpack_tar(io.BytesIO(self.archive()),target,self.binding)
        except ValueError as error:
            self.fail('nested roundtrip must stay private and succeed: '+str(error))
        self.assertEqual((target/'logs/20261115T193308Z/messages').read_bytes(),self.body)
        for path in [target/'logs',target/'logs/20261115T193308Z']:
            self.assertEqual(path.stat().st_mode & 0o777,0o700)

    def test_truncated_damaged_or_trailing_tar_end_is_refused(self):
        raw=self.archive()
        with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
            last=archive.getmember('EXPORT-COMPLETE.json')
            end=last.offset_data+((last.size+511)//512)*512
        cases=[raw[:end],raw[:end]+b'X'*512+raw[end+512:],raw+b'trailing']
        for i,data in enumerate(cases):
            with self.subTest(case=i),self.assertRaises(ValueError):
                export.unpack_tar(io.BytesIO(data),self.base/('received'+str(i)),self.binding)

    def test_parent_replacement_cannot_create_in_foreign_tree(self):
        parent=self.base/'parent';parent.mkdir(mode=0o700)
        foreign=self.base/'foreign';foreign.mkdir(mode=0o700)
        target=parent/'received';original=os.mkdir;changed=False
        def substitute(path,mode=0o777,*,dir_fd=None):
            nonlocal changed
            if not changed and (str(path)==str(target) or str(path)=='received'):
                changed=True
                parent.rename(self.base/'parked-parent')
                parent.symlink_to(foreign,target_is_directory=True)
            return original(path,mode,dir_fd=dir_fd)
        raw=self.archive()
        with mock.patch.object(export.os,'mkdir',side_effect=substitute):
            with self.assertRaises(ValueError):
                export.unpack_tar(io.BytesIO(raw),target,self.binding)
        self.assertTrue(changed)
        self.assertFalse((foreign/'received').exists())

    def test_nested_replacement_cannot_create_in_foreign_tree(self):
        target=self.base/'received'
        foreign=self.base/'foreign';foreign.mkdir(mode=0o700)
        original=os.mkdir;changed=False
        def substitute(path,mode=0o777,*,dir_fd=None):
            nonlocal changed
            if not changed and (str(path)==str(target/'logs') or str(path)=='logs'):
                changed=True
                target.rename(self.base/'parked-received')
                target.symlink_to(foreign,target_is_directory=True)
            return original(path,mode,dir_fd=dir_fd)
        raw=self.archive()
        with mock.patch.object(export.os,'mkdir',side_effect=substitute):
            with self.assertRaises(ValueError):
                export.unpack_tar(io.BytesIO(raw),target,self.binding)
        self.assertTrue(changed)
        self.assertFalse((foreign/'logs').exists())


if __name__ == '__main__':
    unittest.main()
