import importlib.util
from pathlib import Path
import struct
import hashlib
import json
import tempfile
import types
from contextlib import contextmanager
from unittest.mock import patch
import unittest

spec=importlib.util.spec_from_file_location("seccfg_io",Path(__file__).resolve().parents[1]/"scripts/i14_seccfg_io.py")
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class WireDA:
    def __init__(self, replies=(b"\x5a",b"\x69"), fail_write=None):
        self.replies=iter(replies)
        self.writes=[]
        self.fail_write=fail_write
    def usbwrite(self,data):
        self.writes.append(bytes(data))
        return len(self.writes)!=self.fail_write
    def usbread(self,count): return next(self.replies)

class WireTests(unittest.TestCase):
    def test_exact_single_prefix_wire_contract(self):
        prefix=bytes(range(256))*32
        da=WireDA()
        m.write_prefix(da,prefix)
        self.assertEqual(da.writes,[b"\x62",b"\x01",b"\x08",struct.pack(">Q",0x4f80000),
                                    struct.pack(">Q",8192),struct.pack(">I",16384),b"\x5a",prefix,struct.pack(">H",sum(prefix)&65535)])
    def test_wrong_length_stops_before_first_command(self):
        da=WireDA()
        with self.assertRaises(m.SeccfgError): m.write_prefix(da,b"x"*8191)
        self.assertEqual(da.writes,[])
    def test_header_ack_and_cont_must_match_exactly(self):
        for replies in [(b"",),(b"\x69",),(b"\x5a",b""),(b"\x5a",b"\x5a")]:
            with self.assertRaises(m.SeccfgError): m.write_prefix(WireDA(replies),bytes(8192))
    def test_each_short_write_stops_without_retry(self):
        for index in range(1,10):
            da=WireDA(fail_write=index)
            with self.assertRaises(m.SeccfgError): m.write_prefix(da,bytes(8192))
            self.assertEqual(len(da.writes),index)

class DriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)/"prepared"
        self.attach_calls=0
        self.mode="ok"
        self.original=bytes(m.SIZE)
        self.da=WireDA()
        self.da.current=self.original
        self.da.read_reg32=lambda addr: 0
        self.da.write_reg32=lambda addr,val: True
        self.da.readflash=self.readflash
        original_write=self.da.usbwrite
        def write(data):
            result=original_write(data)
            if len(data)==8192: self.da.current=bytes(data)+self.original[8192:]
            return result
        self.da.usbwrite=write
        @contextmanager
        def attached_session(serialport):
            self.attach_calls+=1
            yield types.SimpleNamespace(daloader=types.SimpleNamespace(da=self.da))
        attach=types.SimpleNamespace(attached_session=attached_session)
        spec=importlib.util.spec_from_file_location("template",Path(m.__file__).parent/"seccfg-v3.py")
        template=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(template)
        self.crypto_calls=0
        def transform(data,*,encrypt,read32,write32,timeout,ready_timeout):
            self.crypto_calls+=1
            read32(0x1000a000)
            write32(0x1000a004,0)
            result=bytes(value^0xaa for value in data)
            if self.mode=="unstable" and self.crypto_calls==3: result=bytes(len(data))
            return result
        self.runtime=(attach,template,types.SimpleNamespace(transform=transform))
        patcher=patch.object(m,"source_snapshot",return_value={"frozen-source":"bound"})
        self.snapshot=patcher.start()
        self.addCleanup(patcher.stop)
    def readflash(self,**kwargs):
        self.assertEqual(kwargs,dict(addr=0x4f80000,length=0x80000,filename="",parttype="user",display=False))
        if self.mode=="stale-live": return b"x"*m.SIZE
        if self.mode=="bad-readback" and self.da.writes: return b"x"*m.SIZE
        return self.da.current
    def prepared(self):
        manifest=m.prepare(self.directory,_runtime=self.runtime)
        self.attach_calls=0
        self.crypto_calls=0
        self.da.writes.clear()
        return manifest
    def test_prepare_is_mmio_only_and_outputs_private_hash_bound_artifacts(self):
        manifest=self.prepared()
        self.assertEqual(self.da.writes,[])
        self.assertEqual(manifest["phase"],"PREPARED_NO_STORAGE_WRITES")
        self.assertEqual(set(manifest["crypto"].values()),{"PASS"})
        self.assertEqual(self.directory.stat().st_mode&0o777,0o700)
        for name,entry in manifest["artifacts"].items():
            p=self.directory/name
            self.assertEqual(p.stat().st_mode&0o777,0o600)
            self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),entry["sha256"])
        candidate=(self.directory/"candidate.bin").read_bytes()
        self.assertEqual(candidate[0x1860:],self.original[0x1860:])
        self.assertEqual((self.directory/"prefix.bin").read_bytes(),candidate[:8192])
    def test_prepare_rejects_unstable_crypto_and_failed_register_ack(self):
        self.mode="unstable"
        with self.assertRaises(m.SeccfgError): m.prepare(self.directory,_runtime=self.runtime)
        self.assertFalse((self.directory/"manifest.json").exists())
        self.assertEqual(self.da.writes,[])
        self.directory=Path(self.tmp.name)/"failed-ack"
        self.mode="ok"
        self.da.write_reg32=lambda addr,value: False
        with self.assertRaises(m.SeccfgError): m.prepare(self.directory,_runtime=self.runtime)
        self.assertFalse((self.directory/"manifest.json").exists())
    def test_commit_writes_only_prefix_and_receipt_follows_full_readback(self):
        manifest=self.prepared()
        receipt=m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertEqual(receipt["written_bytes"],8192)
        self.assertEqual(receipt["readback_size"],524288)
        self.assertFalse(receipt["reset_performed"])
        self.assertEqual((self.directory/"readback.bin").read_bytes(),(self.directory/"candidate.bin").read_bytes())
        self.assertTrue((self.directory/"receipt.json").exists())
        self.assertEqual(len(self.da.writes),9)
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertEqual(self.attach_calls,1)
    def test_wrong_explicit_hash_sources_crypto_or_artifacts_prevent_connection(self):
        manifest=self.prepared()
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,"0"*64,_runtime=self.runtime)
        self.assertEqual(self.attach_calls,0)
        self.snapshot.return_value={"frozen-source":"changed"}
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.snapshot.return_value={"frozen-source":"bound"}
        original_manifest=(self.directory/"manifest.json").read_bytes()
        manifest["crypto"]["body_roundtrip"]="FAIL"
        (self.directory/"manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        (self.directory/"manifest.json").write_bytes(original_manifest)
        (self.directory/"prefix.bin").write_bytes(bytes(8192))
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertEqual(self.attach_calls,0)
        self.assertEqual(self.da.writes,[])
    def test_rehashed_wrong_header_or_preserved_tail_is_still_rejected_before_connection(self):
        for offset in [0,0x1900,9000]:
            self.directory=Path(self.tmp.name)/("tamper-"+str(offset))
            self.da.current=self.original
            manifest=self.prepared()
            candidate=bytearray((self.directory/"candidate.bin").read_bytes())
            candidate[offset]^=1
            (self.directory/"candidate.bin").write_bytes(candidate)
            (self.directory/"prefix.bin").write_bytes(candidate[:8192])
            for name in ["candidate.bin","prefix.bin"]:
                data=(self.directory/name).read_bytes()
                manifest["artifacts"][name]={"size":len(data),"sha256":m.sha(data)}
            manifest["candidate_sha256"]=m.sha(candidate)
            (self.directory/"manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(m.SeccfgError): m.commit(self.directory,m.sha(candidate),_runtime=self.runtime)
            self.assertEqual(self.attach_calls,0)
            self.assertEqual(self.da.writes,[])
    def test_stale_live_original_prevents_first_persistent_command(self):
        manifest=self.prepared()
        self.mode="stale-live"
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertEqual(self.da.writes,[])
        self.assertFalse((self.directory/"commit-attempt.json").exists())
    def test_write_failure_or_readback_mismatch_has_unknown_marker_and_no_receipt(self):
        manifest=self.prepared()
        self.da.fail_write=8
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertTrue((self.directory/"commit-attempt.json").exists())
        self.assertEqual(json.loads((self.directory/"commit-failure.json").read_text())["outcome"],"UNKNOWN")
        self.assertFalse((self.directory/"receipt.json").exists())
        count=len(self.da.writes)
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertEqual(len(self.da.writes),count)
        self.directory=Path(self.tmp.name)/"new-prepared"
        self.da.current=self.original
        self.da.fail_write=None
        self.da.replies=iter((b"\x5a",b"\x69"))
        manifest=self.prepared()
        self.mode="bad-readback"
        with self.assertRaises(m.SeccfgError): m.commit(self.directory,manifest["candidate_sha256"],_runtime=self.runtime)
        self.assertFalse((self.directory/"receipt.json").exists())
        self.assertTrue((self.directory/"commit-failure.json").exists())

class ImportTests(unittest.TestCase):
    def test_shared_module_import_is_hardware_free(self):
        import subprocess,sys
        code="import importlib.util,sys;s=importlib.util.spec_from_file_location('io',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);assert 'serial' not in sys.modules;assert 'usb' not in sys.modules;assert 'mtkclient' not in sys.modules"
        subprocess.run([sys.executable,"-c",code,str(Path(m.__file__).resolve())],check=True)

if __name__=="__main__": unittest.main()
