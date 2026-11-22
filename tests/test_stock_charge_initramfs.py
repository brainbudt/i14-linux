import importlib.util
import pathlib
import subprocess
import unittest
import tempfile
import gzip
import json
import hashlib
import re
import sys
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
INIT = ROOT / "initramfs/stock-charge-init"

class StockChargeInitTests(unittest.TestCase):
    def shell(self, body):
        self.assertTrue(INIT.exists(), "missing own init")
        source = INIT.read_text().split("# ENTRYPOINT")[0]
        with tempfile.TemporaryDirectory() as tmp:
            source=source.replace("/dev/null",str(pathlib.Path(tmp)/"null"))
            return subprocess.run(["/bin/sh", "-c", source + "\n" + body], env={"PATH":"/nonexistent"}, text=True, capture_output=True)

    def test_missing_blockdev_stops_before_any_boundary(self):
        r=self.shell('log() { :; }; mounts() { exit 99; }; trial')
        self.assertEqual(r.returncode, 78)

    def test_missing_mdev_stops_before_any_boundary(self):
        r=self.shell('blockdev() { :; }; log() { :; }; mounts() { exit 99; }; trial')
        self.assertEqual(r.returncode, 78)

    def test_mounts_fallback_and_failure_paths(self):
        proc='-t proc -o nosuid,nodev,noexec proc /proc'
        sysfs='-t sysfs -o nosuid,nodev,noexec sysfs /sys'
        devtmpfs='-t devtmpfs -o nosuid devtmpfs /dev'
        devtmp='-t tmpfs -o nosuid,mode=0755 tmpfs /dev'
        run='-t tmpfs -o nosuid,nodev,noexec,mode=0755 tmpfs /run'
        cases={
            'devtmpfs': (0,[proc,sysfs,devtmpfs,run]),
            'fallback': (0,[proc,sysfs,devtmpfs,devtmp,run]),
            'proc': (1,[proc]),
            'sysfs': (1,[proc,sysfs]),
            'dev': (1,[proc,sysfs,devtmpfs,devtmp]),
            'run': (1,[proc,sysfs,devtmpfs,run]),
        }
        for failed,(status,expected) in cases.items():
            with self.subTest(failed=failed):
                body="""
mount() {
    printf '%s\\n' "$*"
    case 'FAILED':"$2":"$6" in
        proc:proc:*|sysfs:sysfs:*|fallback:devtmpfs:*|dev:devtmpfs:*|dev:tmpfs:/dev|run:tmpfs:/run) return 1;;
    esac
}; mounts
""".replace('FAILED',failed)
                r=self.shell(body)
                self.assertEqual(r.returncode,status,r.stderr)
                self.assertEqual(r.stdout.splitlines(),expected)

    def test_main_reaches_guards_and_console_through_both_dev_mount_paths(self):
        variants=('devtmpfs','fallback','mounts','identity','scan1','missing','extra','writable','capacity','gadget','scan2','console')
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root=pathlib.Path(tmp)
                for name in ('dev','run','proc/sys/kernel','proc/device-tree'):
                    (root/name).mkdir(parents=True,exist_ok=True)
                (root/'proc/sys/kernel/osrelease').write_text('3.18.19\n')
                source=INIT.read_text().split('# ENTRYPOINT')[0]
                source=source.replace('/storage-readonly ||','fake_storage ||')
                source=re.sub(r'(?<![A-Za-z0-9_/])/(proc|sys|dev|run)(?=/|[ ;\n])',lambda match: str(root/match[1]),source)
                source=source.replace('[ -b ', '[ -f ').replace('[ -c ', '[ -f ')
                names=['mmcblk0','mmcblk0boot0','mmcblk0boot1']+[f'mmcblk0p{i}' for i in range(1,24)]
                if variant=='missing': names.remove('mmcblk0p23')
                if variant=='extra': names.append('mmcblk0p24')
                body="""
scan_count=0
mount() {
    printf 'MOUNT:%s:%s\\n' "$2" "${6##*/}"
    case 'VARIANT':"$2":"${6##*/}" in
        fallback:devtmpfs:dev|mounts:devtmpfs:dev|mounts:tmpfs:dev) return 1;;
    esac
}
sha256sum() {
    case "$1" in
        */model) printf 'MODEL_HASH  %s\\n' "$1";;
        */compatible) printf '33d69af0dce2161bb2211a0bb3c91313905301bae14f4d474de0204a74a4b47a  %s\\n' "$1";;
        *) return 1;;
    esac
}
dmesg() { printf 'DMESG\\n'; }
mdev() {
    [ "$#:$1" = 1:-s ] || return 98
    scan_count=$((scan_count + 1))
    printf 'SCAN:%s\\n' "$scan_count"
    case 'VARIANT':"$scan_count" in scan1:1|scan2:2) return 1;; esac
    if [ "$scan_count" = 1 ]; then
        for name in NAMES; do : >'ROOT/dev/'"$name"; done
    elif [ 'VARIANT' != console ]; then : >'ROOT/dev/ttyGS0'; fi
}
fake_storage() {
    printf 'STORAGE_HELPER\\n'
    printf 'capacity_bytes=15634268160\\n' >'ROOT/run/i14-storage-ro.ready'
}
blockdev() {
    case "$1" in
        --getro) if [ 'VARIANT' = writable ]; then printf 0; else printf 1; fi;;
        --getsize64) if [ 'VARIANT' = capacity ]; then printf 1; else printf 15634268160; fi;;
        *) return 1;;
    esac
}
write_usb() {
    printf 'USB:%s=%s\\n' "$1" "$2"
    [ 'VARIANT':"$1" != gadget:idVendor ]
}
setsid() { [ "$#:$1" = 1:sh ] || return 98; printf 'CONSOLE\\n'; }
sleep() { [ "$1" = 120 ] || return 98; printf 'TIMER:120\\n'; }
reboot() { printf 'REBOOT:%s\\n' "$*"; return 1; }
if main; then printf 'TRIAL_OK\\n'; else printf 'GUARD_FAILED\\n'; fi
wait
"""
                body=body.replace('MODEL_HASH','bad' if variant=='identity' else '77a29783599bebf2f2e339bea3f14992c9870f6f08bb211cd1a62da9ebbfe5ae')
                body=body.replace('VARIANT',variant).replace('NAMES',' '.join(names)).replace('ROOT',str(root))
                r=subprocess.run(['/bin/sh','-c',source+'\n'+body],env={'PATH':'/nonexistent'},capture_output=True,text=True)
                self.assertEqual(r.returncode,0,r.stderr)
                lines=r.stdout.splitlines()
                self.assertEqual(lines.count('TIMER:120'),1,r.stdout)
                self.assertEqual(lines.count('REBOOT:-f'),1,r.stdout)
                success=variant in ('devtmpfs','fallback')
                self.assertIn('TRIAL_OK' if success else 'GUARD_FAILED',lines,r.stdout)
                if variant in ('mounts','identity','scan1','missing','extra'):
                    self.assertNotIn('STORAGE_HELPER',lines,r.stdout)
                if variant=='mounts': self.assertIn('MOUNT:tmpfs:dev',lines,r.stdout)
                if variant=='identity': self.assertIn('MOUNT:tmpfs:run',lines,r.stdout)
                if variant=='scan1': self.assertIn('SCAN:1',lines,r.stdout)
                if variant=='gadget':
                    self.assertIn('USB:idVendor=0E8D',lines,r.stdout)
                    self.assertNotIn('USB:enable=1',lines,r.stdout)
                    self.assertNotIn('SCAN:2',lines,r.stdout)
                if variant in ('mounts','identity','scan1','missing','extra','writable','capacity'):
                    self.assertNotIn('USB:enable=0',lines,r.stdout)
                if variant not in ('mounts','identity','scan1','missing','extra'):
                    self.assertIn('SCAN:1',lines,r.stdout)
                    self.assertIn('STORAGE_HELPER',lines,r.stdout)
                    self.assertLess(lines.index('SCAN:1'),lines.index('STORAGE_HELPER'),r.stdout)
                if variant in ('devtmpfs','fallback','scan2','console'):
                    self.assertLess(lines.index('I14_STOCK_STORAGE_26_RO'),lines.index('USB:enable=0'),r.stdout)
                    self.assertLess(lines.index('USB:enable=1'),lines.index('SCAN:2'),r.stdout)
                tty=root/'dev/ttyGS0'
                self.assertEqual(tty.read_text() if tty.exists() else '', 'CONSOLE\n' if success else '')

    def test_storage_failure_never_enables_usb(self):
        r=self.shell('requirements() { :; }; storage_nodes() { :; }; log() { :; }; mounts() { :; }; identity() { :; }; storage_guard() { return 1; }; gadget() { exit 99; }; trial')
        self.assertEqual(r.returncode, 1)

    def test_trial_waits_for_late_storage_nodes_with_bounded_rescans(self):
        for variant in ('late', 'timeout', 'scan_error'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root=pathlib.Path(tmp)
                (root/'dev').mkdir(); (root/'run').mkdir()
                source=INIT.read_text().split('# ENTRYPOINT')[0]
                source=source.replace('/storage-readonly ||', 'fake_storage ||')
                source=source.replace('/dev/',str(root/'dev')+'/').replace('/run/',str(root/'run')+'/')
                source=source.replace('[ -b ', '[ -f ')
                names=['mmcblk0','mmcblk0boot0','mmcblk0boot1']+[f'mmcblk0p{i}' for i in range(1,24)]
                body='''
scans=0
mounts() { :; }; identity() { :; }
mdev() {
    scans=$((scans + 1)); printf 'SCAN:%s\\n' "$scans"
    [ 'VARIANT':"$scans" != scan_error:2 ] || return 1
    if [ 'VARIANT' = late ] && [ "$scans" = 3 ]; then
        for name in NAMES; do : >'ROOT/dev/'"$name"; done
    fi
}
sleep() { [ "$1" = 1 ] || return 98; printf 'WAIT\\n'; }
fake_storage() {
    printf 'STORAGE_HELPER\\n'
    printf 'capacity_bytes=15634268160\\n' >'ROOT/run/i14-storage-ro.ready'
}
blockdev() { case "$1" in --getro) printf 1;; --getsize64) printf 15634268160;; *) return 1;; esac; }
gadget() { printf 'GADGET\\n'; }
console() { printf 'CONSOLE\\n'; }
if trial; then printf 'TRIAL_OK\\n'; else printf 'TRIAL_FAILED\\n'; fi
'''.replace('VARIANT',variant).replace('NAMES',' '.join(names)).replace('ROOT',str(root))
                result=subprocess.run(['/bin/sh','-c',source+'\n'+body],env={'PATH':'/nonexistent'},capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                lines=result.stdout.splitlines()
                if variant=='late':
                    self.assertIn('TRIAL_OK',lines,result.stdout)
                    self.assertLess(lines.index('SCAN:3'),lines.index('STORAGE_HELPER'))
                    self.assertLess(lines.index('STORAGE_HELPER'),lines.index('GADGET'))
                    self.assertEqual(lines.count('WAIT'),2,result.stdout)
                else:
                    self.assertIn('TRIAL_FAILED',lines,result.stdout)
                    self.assertNotIn('STORAGE_HELPER',lines,result.stdout)
                    self.assertNotIn('GADGET',lines,result.stdout)
                    self.assertEqual(sum(line.startswith('SCAN:') for line in lines),10 if variant=='timeout' else 2,result.stdout)

    def test_gadget_stops_on_failed_write(self):
        r=self.shell('write_usb() { case "$1" in idVendor) return 1;; enable) [ "$2" != 1 ] || exit 99;; esac; }; gadget')
        self.assertEqual(r.returncode, 1)

    def test_gadget_exact_lifecycle(self):
        r=self.shell('write_usb() { printf "%s=%s\\n" "$1" "$2"; }; gadget')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.splitlines(), ['enable=0','idVendor=0E8D','idProduct=2007','iManufacturer=I14 offline trial','iProduct=Stock charge trial','iSerial=I14-STOCK-TRIAL-20261101','f_acm/instances=1','functions=acm','bDeviceClass=02','enable=1'])

    def test_exact_storage_nodes_capacity_and_ro(self):
        for variant in ('valid','missing','extra','extra_boot','missing_boot','writable','capacity'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                root=pathlib.Path(tmp)
                (root/'dev').mkdir();(root/'run').mkdir()
                names=['mmcblk0','mmcblk0boot0','mmcblk0boot1']+[f'mmcblk0p{i}' for i in range(1,24)]
                if variant=='missing': names.remove('mmcblk0p23')
                if variant=='extra': names.append('mmcblk0p24')
                if variant=='extra_boot': names.append('mmcblk0boot2')
                if variant=='missing_boot': names.remove('mmcblk0boot1')
                for name in names: (root/'dev'/name).touch()
                (root/'run/i14-storage-ro.ready').write_text('capacity_bytes=15634268160\n')
                source=INIT.read_text().split('# ENTRYPOINT')[0]
                source=source.replace('/storage-readonly ||','fake_storage ||').replace('/dev/',str(root/'dev')+'/').replace('/run/',str(root/'run')+'/').replace('[ -b ', '[ -f ')
                ro='0' if variant=='writable' else '1'
                capacity='1' if variant=='capacity' else '15634268160'
                body=f'\nfake_storage() {{ :; }}; log() {{ :; }}; blockdev() {{ case "$1" in --getro) printf "{ro}";; --getsize64) printf "{capacity}";; *) return 1;; esac; }}; storage_guard'
                r=subprocess.run(['/bin/sh','-c',source+body],env={'PATH':'/nonexistent'},capture_output=True,text=True)
                self.assertEqual(r.returncode,0 if variant=='valid' else 1,r.stderr)

    def test_identity_failure_prevents_storage_and_usb(self):
        r=self.shell('requirements() { :; }; mdev() { :; }; mounts() { :; }; identity() { return 1; }; storage_guard() { exit 99; }; gadget() { exit 99; }; trial')
        self.assertEqual(r.returncode,1)

    def test_return_uses_ordinary_reboot_and_surfaces_failure(self):
        r=self.shell('log() { :; }; reboot() { printf "%s" "$1"; return 1; }; return_boot')
        self.assertEqual(r.returncode,1)
        self.assertEqual(r.stdout,'-f')

    def test_main_requests_only_one_return_even_when_guard_or_reboot_fails(self):
        for failed in ('none', 'identity', 'console'):
            with self.subTest(failed=failed):
                body='''
requirements() { :; }; storage_nodes() { :; }; mdev() { :; }; mounts() { :; }; storage_guard() { :; }; gadget() { :; };
identity() { printf 'IDENTITY\\n'; return IDENTITY_RC; };
console() { printf 'CONSOLE\\n'; return CONSOLE_RC; };
sleep() { printf 'SLEEP:%s\\n' "$1"; [ "$1" = 120 ]; };
reboot() { printf 'REBOOT:%s\\n' "$1"; return 1; };
write_usb() { printf 'USB:%s=%s\\n' "$1" "$2"; };
if main; then :; else printf 'GUARD_FAILED\\n'; fi; wait
'''.replace('IDENTITY_RC', '1' if failed=='identity' else '0').replace('CONSOLE_RC', '1' if failed=='console' else '0')
                r=self.shell(body)
                self.assertEqual(r.returncode,0,r.stderr)
                self.assertEqual(r.stdout.splitlines().count('REBOOT:-f'),1,r.stdout)
                self.assertEqual(r.stdout.splitlines().count('SLEEP:120'),1,r.stdout)
                if failed=='identity': self.assertNotIn('CONSOLE',r.stdout)

    def test_package_tools_exist(self):
        self.assertTrue((ROOT/'initramfs/make-stock-charge-cpio.py').exists())
        self.assertTrue((ROOT/'initramfs/verify-stock-charge-cpio.py').exists())

class StockChargeArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def module(name):
            spec=importlib.util.spec_from_file_location(name,ROOT/'initramfs'/f'{name}.py')
            m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
        cls.make=module('make-stock-charge-cpio')
        cls.check=module('verify-stock-charge-cpio')
        cls.build=ROOT/'builds/busybox-usb-mmc-readonly'

    def package(self, directory, extra=None):
        entries=self.make.entries(self.build)
        if extra: entries.update(extra)
        data=self.make.archive(entries)
        (directory/'stock-charge.cpio.gz').write_bytes(data)
        (directory/'manifest.json').write_text(json.dumps({'hardware_go':False,'archive_sha256':hashlib.sha256(data).hexdigest()}))
        return data

    def test_deterministic_exact_allowlist(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=pathlib.Path(tmp)
            a=self.package(directory);b=self.package(directory)
            self.assertEqual(a,b)
            self.assertEqual(self.check.verify(self.build,directory),20)
            self.assertEqual(self.check.parse_newc(gzip.decompress(a))['bin/mdev'],(0o120777,b'/bin/busybox',0,0))

    def test_mount_validator_rejects_changed_order_duplicate_or_nonvirtual_mount(self):
        script=INIT.read_bytes()
        proc=b'    mount -t proc -o nosuid,nodev,noexec proc /proc || return 1\n'
        sysfs=b'    mount -t sysfs -o nosuid,nodev,noexec sysfs /sys || return 1\n'
        self.assertTrue(hasattr(self.check,'check_virtual_mounts'),'missing exact virtual mount-chain validator')
        self.check.check_virtual_mounts(script)
        variants=(
            script.replace(proc+sysfs,sysfs+proc),
            script.replace(proc,proc+proc),
            script.replace(b'nosuid,mode=0755 tmpfs /dev',b'nosuid,nodev,mode=0755 tmpfs /dev'),
            script.replace(b'devtmpfs /dev ||',b'devtmpfs /dev &&'),
            script+b'\nmount -o rw /dev/mmcblk0 /data\n',
        )
        for changed in variants:
            with self.subTest(script=changed):
                with self.assertRaises(ValueError): self.check.check_virtual_mounts(changed)

    def test_forbidden_components_and_write_mount_script_rejected(self):
        for entry in ('system/bin/healthd','recovery','adbd','lib/modules/test.ko','init'):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                directory=pathlib.Path(tmp)
                self.package(directory,{entry:(0o100755,b'mount -o rw /dev/mmcblk0 /data',0,0)})
                with self.assertRaises(ValueError): self.check.verify(self.build,directory)

    def test_compiled_applets_and_static_binary(self):
        binary,applets=self.make.inputs(self.build)
        self.assertEqual(len(applets),47)
        self.assertTrue({'blockdev','mdev','sh','reboot','setsid','sha256sum','mount','sleep'} <= set(applets))

    def test_newc_metadata_padding_and_trailer_mutations_rejected(self):
        for variant in ('inode','nlink','name_padding','trailer_mode','trailer_inode','entry_order'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as tmp:
                directory=pathlib.Path(tmp); original=self.package(directory)
                data=bytearray(gzip.decompress(original)); off=0; headers=[]
                while True:
                    fields=[int(data[off+6+i*8:off+14+i*8],16) for i in range(13)]
                    name_end=off+110+fields[11]; start=(name_end+3)&~3; end=start+fields[6]
                    name=bytes(data[off+110:name_end-1]).decode()
                    headers.append((off,fields,name_end,start,end,name))
                    if name=='TRAILER!!!': break
                    off=(end+3)&~3
                first=headers[0]; trailer=headers[-1]
                if variant=='inode': data[6:14]=b'00000099'
                elif variant=='nlink': data[38:46]=b'00000009'
                elif variant=='name_padding':
                    h=next(h for h in headers if h[2]<h[3]); data[h[2]]=1
                elif variant=='trailer_mode': data[trailer[0]+14:trailer[0]+22]=b'000081a4'
                elif variant=='entry_order':
                    chunks=[data[h[0]:(h[4]+3)&~3] for h in headers[:-1]]
                    chunks[0],chunks[1]=chunks[1],chunks[0]
                    for index,chunk in enumerate(chunks,1): chunk[6:14]=f'{index:08x}'.encode()
                    data=bytearray().join(chunks)+data[trailer[0]:]
                else: data[trailer[0]+6:trailer[0]+14]=b'00000099'
                changed=gzip.compress(data,mtime=0)
                (directory/'stock-charge.cpio.gz').write_bytes(changed)
                (directory/'manifest.json').write_text(json.dumps({'hardware_go':False,'archive_sha256':hashlib.sha256(changed).hexdigest()}))
                with self.assertRaises(ValueError): self.check.verify(self.build,directory)

    def test_newc_data_padding_is_zero_for_payload_requiring_alignment(self):
        data=bytearray(gzip.decompress(self.make.archive({'sample':(0o100644,b'x',0,0)})))
        self.assertEqual(self.check.parse_newc(data),{'sample':(0o100644,b'x',0,0)})
        payload_end=((110+len('sample')+1+3)&~3)+1
        data[payload_end]=1
        with self.assertRaises(ValueError): self.check.parse_newc(data)

    def test_builder_refuses_existing_output_without_overwriting_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory=pathlib.Path(tmp); archive=directory/'stock-charge.cpio.gz'; archive.write_bytes(b'original evidence')
            (directory/'manifest.json').write_text('original manifest')
            with patch.object(sys,'argv',['make-stock-charge-cpio.py',str(self.build),str(directory)]):
                with self.assertRaises(FileExistsError): self.make.main()
            self.assertEqual(archive.read_bytes(),b'original evidence')
            self.assertEqual((directory/'manifest.json').read_text(),'original manifest')

if __name__ == '__main__': unittest.main()
