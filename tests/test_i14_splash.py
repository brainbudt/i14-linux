"""rootfs/overlay/usr/sbin/i14-splash with a fake framebuffer and the committed assets."""
import importlib.machinery
import importlib.util
import os
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSETS = ROOT / "rootfs/overlay/usr/share/i14-splash"
loader = importlib.machinery.SourceFileLoader("i14splash", str(ROOT / "rootfs/overlay/usr/sbin/i14-splash"))
spec = importlib.util.spec_from_loader("i14splash", loader)
splash = importlib.util.module_from_spec(spec)
loader.exec_module(splash)


class FakeFb:
    """Keeps a 720-wide RGBA canvas; records blits."""
    def __init__(self, w=720, h=1600):
        self.xres, self.yres, self.canvas, self.blits, self.shown = w, h, bytearray(w * h * 4), [], 0

    def blit(self, x, y, w, h, rgba):
        self.blits.append((x, y, w, h))
        for r in range(h):
            s = ((y + r) * self.xres + x) * 4
            self.canvas[s:s + w * 4] = rgba[r * w * 4:(r + 1) * w * 4]

    def show(self):
        self.shown += 1


class Splash(unittest.TestCase):
    def setUp(self):
        self.anim, self.base = splash.load(str(ASSETS))

    def test_assets_fit_the_panel(self):
        self.assertEqual((self.anim["w"], self.anim["h"], len(self.base)), (720, 1600, 720 * 1600 * 4))
        self.assertEqual(len(self.anim["cursor"]["states"]), 2)
        self.assertEqual(len(self.anim["dots"]["states"]), 4)

    def test_first_state_is_the_lk_image(self):
        fb = FakeFb()
        with tempfile.TemporaryDirectory() as d:
            stop = os.path.join(d, "stop")
            ticks = iter(range(1000))
            splash.run(fb, self.anim, self.base, stop=stop, clock=lambda: next(ticks) * 0.0,
                       sleep=lambda s: open(stop, "w").close())
        self.assertEqual(bytes(fb.canvas), self.base)  # tick 0 is state (cursor on, 3 dots)
        self.assertEqual(fb.blits[0], (0, 0, 720, 1600))
        self.assertEqual(fb.shown, 1)

    def test_animation_cycles_and_stops(self):
        seen = [splash.state(self.anim, t) for t in range(16)]
        self.assertEqual(seen[:4], [(0, 3), (0, 3), (1, 0), (1, 0)])
        self.assertEqual({c for c, _ in seen}, {0, 1})
        self.assertEqual({d for _, d in seen}, {0, 1, 2, 3})
        fb, now = FakeFb(), [0.0]
        with tempfile.TemporaryDirectory() as d:
            ticks = splash.run(fb, self.anim, self.base, stop=os.path.join(d, "never"), clock=lambda: now[0],
                               sleep=lambda s: now.__setitem__(0, now[0] + s), limit=5)
        self.assertEqual(ticks, 20)  # 5 s at 250 ms
        boxes = {tuple(self.anim["cursor"]["box"]), tuple(self.anim["dots"]["box"])}
        self.assertTrue(all(b in boxes for b in fb.blits[1:]))  # after the base: patches only

    def test_refuses_another_panel(self):
        with self.assertRaises(ValueError):
            splash.run(FakeFb(1080, 2340), self.anim, self.base, stop="/nonexistent", limit=0)

    def test_swizzle(self):
        px = bytes([1, 2, 3, 4])
        self.assertEqual(splash.swizzle(px, (0, 8, 16, 24)), px)
        self.assertEqual(splash.swizzle(px, (16, 8, 0, 24)), bytes([3, 2, 1, 4]))  # BGRA


def psf1(path, unicode=True):
    """Tiny PSF1 font: 256 glyphs 8x4; glyph 65 ('A') has the left pixel set in row 0."""
    import gzip, struct
    glyphs = bytearray(256 * 4)
    glyphs[65 * 4] = 0x80
    data = b"\x36\x04" + bytes([2 if unicode else 0, 4]) + bytes(glyphs)
    if unicode:
        for i in range(256):
            data += struct.pack("<HH", i if i != 65 else 0x41, 0xFFFF)
    with gzip.open(path, "wb") as f:
        f.write(data)


def psf2(path):
    import gzip, struct
    glyphs = bytearray(128 * 4)
    glyphs[66 * 4] = 0x40  # 'B': second pixel of row 0
    with gzip.open(path, "wb") as f:
        f.write(b"\x72\xb5\x4a\x86" + struct.pack("<7I", 0, 32, 0, 128, 4, 4, 8) + bytes(glyphs))


class KernelLog(unittest.TestCase):
    def test_format_record_strips_mediatek_prefix_and_escapes(self):
        rec = b"6,123,157517826,-;(0)[93:bat_thread][BAT_thread]Cable in, CHR_Type_num=2\\x0d\n SUBSYSTEM=x\n"
        self.assertEqual(splash.format_record(rec), "[  157.517826] [BAT_thread]Cable in, CHR_Type_num=2")
        self.assertEqual(splash.format_record(b"6,5,1234567,-; -(1)[0:swapper/1]hello\n"), "[    1.234567] hello")
        self.assertEqual(splash.format_record(b"no header"), "")

    def test_fonts_psf1_and_psf2(self):
        with tempfile.TemporaryDirectory() as d:
            for name, make, ch, x in (("a.psf.gz", psf1, "A", 0), ("b.psf.gz", psf2, "B", 1)):
                p = os.path.join(d, name)
                make(p)
                f = splash.Font(p)
                self.assertEqual((f.w, f.h), (8, 4))
                self.assertEqual(f.bits(ch)[0], [x])
            p = os.path.join(d, "bad.psf.gz")
            import gzip
            with gzip.open(p, "wb") as fh:
                fh.write(b"nope")
            with self.assertRaises(ValueError):
                splash.Font(p)

    def test_render_draws_on_the_base_below_booting(self):
        anim, base = splash.load(str(ASSETS))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.psf.gz")
            psf1(p)
            font = splash.Font(p)
        n = splash.log_lines(font, 1600)
        self.assertEqual(n, (1600 - splash.LOG_MARGIN - splash.LOG_TOP) // 4)
        self.assertGreater(splash.LOG_TOP, anim["dots"]["box"][1] + anim["dots"]["box"][3])  # under "booting..."
        y0 = splash.LOG_TOP
        out = splash.render_log(base, 720, y0, ["A"], font, n)
        self.assertEqual(len(out), 720 * 4 * n * 4)
        i = splash.LOG_MARGIN * 4
        self.assertEqual(out[i:i + 4], bytes(splash.LOG_COLOR) + b"\xff")  # the one set pixel
        changed = [k for k in range(0, len(out), 4) if out[k:k + 4] != base[y0 * 2880 + k:y0 * 2880 + k + 4]]
        self.assertEqual(changed, [i])  # everything else is the gradient

    def test_kmsg_reads_records_and_keeps_the_last(self):
        r, w = os.pipe()
        for k in range(5):
            os.write(w, f"6,{k},{k * 1000000},-;line {k}\n".encode())
        log = splash.Kmsg.__new__(splash.Kmsg)
        import collections, fcntl
        fcntl.fcntl(r, fcntl.F_SETFL, os.O_NONBLOCK)
        log.fd, log.lines = r, collections.deque(maxlen=3)
        self.assertTrue(log.poll())  # a pipe returns the 5 records in one read: only the first line is used
        self.assertEqual(len(log.lines), 1)
        self.assertFalse(log.poll())
        os.close(w)
        os.close(r)

    def test_poll_is_bounded_under_a_log_flood(self):
        log = splash.Kmsg.__new__(splash.Kmsg)
        import collections
        log.fd, log.lines = 7, collections.deque(maxlen=3)
        calls = [0]
        real = splash.os.read
        def flood(fd, n):
            calls[0] += 1
            return f"6,{calls[0]},1000000,-;spam {calls[0]}\n".encode()
        splash.os.read = flood
        try:
            self.assertTrue(log.poll(budget_s=10, max_records=100))
            self.assertEqual(calls[0], 100)
            calls[0] = 0
            self.assertFalse(log.poll(budget_s=-1, max_records=100))  # time budget already spent
            self.assertEqual(calls[0], 0)
        finally:
            splash.os.read = real
        self.assertEqual(list(log.lines)[-1], "[    1.000000] spam 100")
        import errno as E
        calls[0] = 0
        def epipe(fd, n):
            calls[0] += 1
            raise OSError(E.EPIPE, "overwritten")
        splash.os.read = epipe
        try:
            self.assertFalse(log.poll(budget_s=10, max_records=50))  # EPIPE retries count against the bound
            self.assertEqual(calls[0], 50)
            self.assertEqual(log.fd, 7)  # EPIPE does not switch the log off
        finally:
            splash.os.read = real

    def test_other_read_errors_stop_only_the_log(self):
        log = splash.Kmsg.__new__(splash.Kmsg)
        import collections, errno as E
        log.fd, log.lines = 7, collections.deque(maxlen=3)
        real = splash.os.read
        splash.os.read = lambda fd, n: (_ for _ in ()).throw(OSError(E.EINVAL, "too big"))
        try:
            self.assertFalse(log.poll())
            self.assertIsNone(log.fd)
            self.assertFalse(log.poll())
        finally:
            splash.os.read = real

    def test_run_redraws_the_log_box_when_lines_arrive(self):
        anim, base = splash.load(str(ASSETS))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.psf.gz")
            psf1(p)
            font = splash.Font(p)
            log = type("L", (), {"lines": ["A"], "poll": lambda self: True})()
            fb, now = FakeFb(), [0.0]
            splash.run(fb, anim, base, stop=os.path.join(d, "never"), clock=lambda: now[0],
                       sleep=lambda s: now.__setitem__(0, now[0] + s), limit=1, log=log, font=font)
        n = splash.log_lines(font, 1600)
        self.assertIn((0, splash.LOG_TOP, 720, font.h * n), fb.blits)


if __name__ == "__main__":
    unittest.main()
