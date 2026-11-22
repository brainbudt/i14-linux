import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("session", Path(__file__).resolve().parents[1]/"scripts/mtk-session.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = {"hwcode": 0x321, "flashmode": "LEGACY", "flashtype": "emmc", "flashsize": m.FLASH_SIZE,
                      "m_emmc_ua_size": m.FLASH_SIZE, "m_emmc_boot1_size": 0x400000,
                      "m_emmc_boot2_size": 0x400000, "target_config": dict.fromkeys(m.SECURITY_KEYS, False), "patched": True}
        self.state_path = self.root/".state"
        self.state_path.write_text(json.dumps(self.state))
        self.preloader = self.root/"own-preloader.bin"
        self.preloader.write_bytes(b"own source")
        self.reference = self.root/"primary-header.bin"
        self.reference.write_bytes(b"g"*4096)
        for name, value in [("PRELOADER_SHA256", hashlib.sha256(self.preloader.read_bytes()).hexdigest()),
                            ("REFERENCE_SHA256", hashlib.sha256(self.reference.read_bytes()).hexdigest())]:
            patcher = patch.object(m, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.events = []
        self.mode = "ok"
        outer = self
        class Config:
            def init_hwcode(self, hwcode): self.hwcode = hwcode
        class FakeMtk:
            def __init__(self, config, serialportname):
                self.config = config
                outer.instance = self
                self.preloader = types.SimpleNamespace(init=lambda: outer.fail("BROM handshake invoked"))
                self.port = types.SimpleNamespace(cdc=types.SimpleNamespace(connect=self.connect), close=self.close)
                self.daloader = types.SimpleNamespace(reinit=self.reinit, upload_da=lambda: outer.fail("upload invoked"))
            def connect(self):
                outer.events.append("connect")
                return outer.mode != "connect-fail"
            def reinit(self):
                outer.events.append("reinit")
                self.daloader.da = types.SimpleNamespace(check_usb_cmd=self.status, readflash=self.readflash,
                                                         writeflash=lambda: outer.fail("write invoked"), shutdown=lambda: outer.fail("reset invoked"))
                self.daloader.lft = types.SimpleNamespace(patched_read=False, da2address=None, readmem=lambda a,n=1: 7,
                                                          writeregister=lambda a, words: True, seccfg=lambda: outer.fail("unlock invoked"))
                if outer.mode == "state-race": outer.state_path.write_text("changed")
                return outer.mode != "reinit-fail"
            def status(self):
                outer.events.append("status")
                return b"" if outer.mode == "bad-status" else b"\x00"
            def readflash(self, **kwargs):
                outer.events.append(("read", kwargs))
                return b"bad" if outer.mode == "bad-gpt" else outer.reference.read_bytes()
            def close(self): outer.events.append("close")
        self.factories = (Config, FakeMtk)
    def attach(self):
        return m.attached_session(state_path=self.state_path, preloader_path=self.preloader,
                                  reference_path=self.reference, _factories=self.factories)
    def test_attach_only_reinitializes_and_probes_before_yield(self):
        with self.attach() as ctx:
            self.assertEqual(self.events[:3], ["connect", "reinit", "status"])
            self.assertEqual(self.events[3][1], dict(addr=0, length=4096, filename="", parttype="user", display=False))
            self.assertEqual(ctx.config.preloader, b"own source")
            self.assertFalse(ctx.config.reconnect)
            self.assertEqual(ctx.daloader.lft.readmem(0), 7)
        self.assertEqual(self.events[-1], "close")
    def test_flash_unlock_and_reset_blocked_but_register_api_available(self):
        with self.attach() as ctx:
            for call in (ctx.daloader.upload_da, ctx.daloader.da.writeflash, ctx.daloader.da.shutdown,
                         ctx.preloader.init, ctx.daloader.lft.seccfg):
                with self.assertRaises(m.SessionError): call()
            self.assertTrue(ctx.daloader.lft.writeregister(0, [1]))
            self.assertFalse(ctx.daloader.lft.patched_read)
            self.assertIsNone(ctx.daloader.lft.da2address)
    def test_protocol_failures_close_once_and_never_yield(self):
        for mode in ["connect-fail", "reinit-fail", "bad-status", "bad-gpt", "state-race"]:
            self.mode = mode
            self.events.clear()
            self.state_path.write_text(json.dumps(self.state))
            with self.assertRaises(m.SessionError):
                with self.attach(): self.fail("invalid context yielded")
            self.assertEqual(self.events.count("connect"), 1)
            self.assertEqual(self.events.count("close"), 1)
    def test_callback_error_still_closes_without_reset(self):
        with self.assertRaises(ValueError):
            with self.attach(): raise ValueError("caller failure")
        self.assertEqual(self.events.count("close"), 1)
    def test_bad_state_preloader_or_reference_stops_before_connect(self):
        for key,value in [("hwcode",0x335),("flashmode","XFLASH"),("flashsize",1),("m_emmc_boot1_size",1),("patched",False)]:
            candidate = dict(self.state)
            candidate[key] = value
            with self.assertRaises(m.SessionError): m.validate_state(candidate)
        self.state["target_config"]["sbc"] = True
        with self.assertRaises(m.SessionError): m.validate_state(self.state)
        self.state["target_config"]["sbc"] = False
        self.state_path.write_text(json.dumps(self.state))
        self.preloader.write_bytes(b"other image")
        with self.assertRaises(m.SessionError):
            with self.attach(): self.fail("bad preloader yielded")
        self.assertEqual(self.events, [])
        self.preloader.write_bytes(b"own source")
        self.reference.write_bytes(b"other reference")
        with self.assertRaises(m.SessionError):
            with self.attach(): self.fail("bad reference yielded")
        self.assertEqual(self.events, [])
    def test_import_does_not_load_vendor_usb_or_serial(self):
        import subprocess,sys
        code = "import importlib.util,sys; s=importlib.util.spec_from_file_location('session',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert 'serial' not in sys.modules; assert 'usb' not in sys.modules; assert 'mtkclient' not in sys.modules"
        subprocess.run([sys.executable,"-c",code,str(Path(m.__file__).resolve())],check=True)

if __name__ == "__main__": unittest.main()
