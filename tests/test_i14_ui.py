"""i14-ui (rootfs/overlay/usr/lib/i14ui): touch, keyboard, terminal emulator, Wi-Fi, Bluetooth, settings, pages."""
import gzip
import json
import os
import pathlib
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rootfs/overlay/usr/lib"))

from i14ui import app as appmod  # noqa: E402
from i14ui import bt, config, fb, inputdev, term, vt, widgets, wifi  # noqa: E402
from i14ui.font import Font  # noqa: E402

EV_ABS, EV_KEY, EV_SYN = 3, 1, 0


class PrivateSettings(unittest.TestCase):
    def test_existing_user_settings_migrate_without_losing_values(self):
        with tempfile.TemporaryDirectory() as d:
            folder = pathlib.Path(d) / "i14-linux"
            folder.mkdir()
            folder.chmod(0o755)
            settings = folder / "ui.json"
            settings.write_text('{"brightness": 160}')
            settings.chmod(0o644)
            self.assertEqual(config.Config(settings)["brightness"], 160)
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
            networks = folder / "wpa_supplicant.conf"
            wifi.Store(networks).put(b"fixture", "NONE")
            self.assertEqual(wifi.Store(networks).networks()[0]["ssid"], b"fixture")
            self.assertEqual(networks.stat().st_mode & 0o777, 0o600)

    def test_settings_directory_may_not_be_a_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d)
            target = base / "elsewhere"
            target.mkdir()
            folder = base / "i14-linux"
            folder.symlink_to(target, target_is_directory=True)
            self.assertTrue(config.Config(folder / "ui.json").damaged)
            with self.assertRaises(OSError):
                wifi.Store(folder / "wpa_supplicant.conf").put(b"fixture", "NONE")
            self.assertEqual(list(target.iterdir()), [])


def make_font(d, w=8, h=16):
    """A PSF2 font without unicode table: glyph i = chr(i), each glyph a filled first row."""
    count, size = 256, h * ((w + 7) // 8)
    header = b"\x72\xb5\x4a\x86" + struct.pack("<7I", 0, 32, 0, count, size, h, w)
    glyph = b"\xff" + b"\0" * (size - 1)
    path = pathlib.Path(d) / f"f{w}x{h}.psf.gz"
    path.write_bytes(gzip.compress(header + glyph * count))
    return Font(path)


def mt(slot=None, tid=None, x=None, y=None):
    ev = []
    if slot is not None:
        ev.append((EV_ABS, 0x2F, slot))
    if tid is not None:
        ev.append((EV_ABS, 0x39, tid))
    if x is not None:
        ev.append((EV_ABS, 0x35, x))
    if y is not None:
        ev.append((EV_ABS, 0x36, y))
    return ev + [(EV_SYN, 0, 0)]


def gestures(touch, *frames):
    out = []
    for frame in frames:
        for ev in frame:
            g = touch.feed(*ev)
            if g:
                out.append(g)
    return out


class TouchProtocolB(unittest.TestCase):
    def test_tap_and_drag(self):
        t = inputdev.Touch()
        got = gestures(t, mt(0, 5, 100, 200), mt(x=110), mt(y=260), mt(tid=-1))
        self.assertEqual(got, [("down", 100, 200), ("move", 110, 200), ("move", 110, 260), ("up", 110, 260)])

    def test_second_finger_is_ignored_until_all_lift(self):
        t = inputdev.Touch()
        got = gestures(t, mt(0, 1, 10, 10), mt(1, 2, 500, 500), mt(0, -1), mt(1, None, 510, 510), mt(1, -1),
                       mt(0, 3, 50, 60), mt(0, -1))
        self.assertEqual(got, [("down", 10, 10), ("move", 10, 10), ("up", 10, 10), ("down", 50, 60), ("up", 50, 60)])

    def test_unchanged_coordinates_are_not_resent(self):
        # the kernel drops ABS values equal to the slot's last one, also after a lift: a second tap
        # on the same row brings only X (seen on the phone: the tap landed on y=0, the back button)
        t = inputdev.Touch()
        got = gestures(t, mt(0, 1, 151, 1370), mt(0, -1), mt(0, 2, 54), mt(0, -1), mt(0, 3), mt(0, -1))
        self.assertEqual(got, [("down", 151, 1370), ("up", 151, 1370), ("down", 54, 1370), ("up", 54, 1370),
                               ("down", 54, 1370), ("up", 54, 1370)])

    def test_dropped_events_end_the_gesture(self):
        t = inputdev.Touch()
        got = gestures(t, mt(0, 1, 10, 10), [(EV_SYN, 3, 0), (EV_ABS, 0x35, 99)], [(EV_SYN, 0, 0)])
        self.assertEqual(got, [("down", 10, 10), ("up", 10, 10)])
        self.assertEqual(gestures(t, mt(0, 4, 20)), [("down", 20, 10)])  # Y kept, not 0 (the back button)

    def test_cancel_keeps_skipping_a_damaged_frame(self):
        t = inputdev.Touch()
        t.feed(EV_SYN, 3, 0)  # SYN_DROPPED, then the screen goes off before the closing SYN_REPORT
        t.cancel()
        self.assertEqual(gestures(t, [(EV_ABS, 0x39, 5), (EV_ABS, 0x35, 7), (EV_SYN, 0, 0)]), [])

    def test_single_touch_device(self):
        t = inputdev.Touch()
        got = gestures(t, [(EV_ABS, 0, 5), (EV_ABS, 1, 6), (EV_KEY, 330, 1), (EV_SYN, 0, 0)],
                       [(EV_KEY, 330, 0), (EV_SYN, 0, 0)])
        self.assertEqual(got, [("down", 5, 6), ("up", 5, 6)])

    def test_classify_from_sysfs(self):
        with tempfile.TemporaryDirectory() as d:
            def dev(n, name, abs_="0", key="0"):
                p = pathlib.Path(d) / n / "device" / "capabilities"
                p.mkdir(parents=True)
                (p.parent / "name").write_text(name + "\n")
                (p / "abs").write_text(abs_ + "\n")
                (p / "key").write_text(key + "\n")
            dev("event1", "mtk-kpd", key="1c000000000000 0")
            dev("event5", "fts_ts", abs_="661800000000000", key="400 0 0 0 10168000000000 4d04081460000")
            dev("event7", "BT keyboard", key=f"{(1 << 30) | (1 << 44):x}")
            dev("event3", "m_alsps_input", abs_="101")
            found = inputdev.classify(d)
        self.assertEqual(found, {"touch": ["/dev/input/event5"], "power": ["/dev/input/event1"],
                                 "keyboard": ["/dev/input/event7"]})

    def test_hardware_keyboard(self):
        k = inputdev.Keyboard()
        feed = lambda code, v=1: k.feed(EV_KEY, code, v)  # noqa: E731
        self.assertEqual(feed(30), ("a", False, False))
        feed(42)
        self.assertEqual(feed(30), ("A", False, False))
        self.assertEqual(feed(2), ("!", False, False))
        feed(42, 0)
        feed(29)
        self.assertEqual(feed(46), ("c", True, False))
        feed(29, 0)
        self.assertEqual(feed(103), ("up", False, False))
        self.assertIsNone(feed(30, 0))  # release
        feed(58)
        self.assertEqual(feed(30), ("A", False, False))  # caps lock


class Terminal(unittest.TestCase):
    def vt(self, cols=20, rows=5):
        self.replies = []
        return vt.VT(cols, rows, reply=self.replies.append)

    def test_print_wrap_and_scrollback(self):
        t = self.vt(10, 3)
        t.feed(b"0123456789abc\r\nline2\r\nline3\r\nline4")
        self.assertEqual(t.text().splitlines(), ["line2", "line3", "line4"])
        self.assertEqual("".join(c[0] for c in t.scrollback[0]), "0123456789")
        self.assertEqual("".join(c[0] for c in t.view(2)[0]).rstrip(), "0123456789")

    def test_deferred_wrap_at_the_last_column(self):
        t = self.vt(5, 2)
        t.feed("abcde\r\n")
        self.assertEqual((t.x, t.y), (0, 1))
        self.assertEqual(t.text().splitlines()[0], "abcde")

    def test_cursor_and_erase(self):
        t = self.vt(10, 3)
        t.feed("aaaaaaaaaa\r\nbbbbbbbbbb\r\ncccccccccc\x1b[2;4H\x1b[K\x1b[1;3H\x1b[1K")
        self.assertEqual(t.text().splitlines(), ["   aaaaaaa", "bbb", "cccccccccc"])
        t.feed("\x1b[2J")
        self.assertEqual(t.text().strip(), "")

    def test_scroll_region_insert_delete_lines(self):
        t = self.vt(5, 5)
        t.feed("1\r\n2\r\n3\r\n4\r\n5\x1b[2;4r\x1b[4;1H\n")
        self.assertEqual(t.text().splitlines(), ["1", "3", "4", "", "5"])
        self.assertEqual(t.scrollback, [])  # a region scroll never feeds the scrollback
        t.feed("\x1b[2;1H\x1b[L")
        self.assertEqual(t.text().splitlines(), ["1", "", "3", "4", "5"])
        t.feed("\x1b[M")
        self.assertEqual(t.text().splitlines(), ["1", "3", "4", "", "5"])

    def test_insert_and_delete_characters(self):
        t = self.vt(8, 1)
        t.feed("abcdef\x1b[1;2H\x1b[2@XY\x1b[1;1H\x1b[P")
        self.assertEqual(t.text(), "XYbcdef")

    def test_alternate_screen_restores_the_shell(self):
        t = self.vt(10, 3)
        t.feed("prompt$ ")
        t.feed("\x1b[?1049h\x1b[2J\x1b[Hnano")
        self.assertEqual(t.text().strip(), "nano")
        t.feed("\x1b[?1049l")
        self.assertEqual(t.text().strip(), "prompt$")
        self.assertEqual((t.x, t.y), (8, 0))

    def test_colours_and_reverse(self):
        t = self.vt()
        t.feed("\x1b[1;31;44mR\x1b[0m\x1b[38;5;200mP\x1b[38;2;1;2;3mT\x1b[7mV\x1b[m")
        line = t.screen[0]
        self.assertEqual(line[0][1], (1, 4, vt.BOLD))
        self.assertEqual(line[1][1], (200, None, 0))
        self.assertEqual(line[2][1], ((1, 2, 3), None, 0))
        self.assertEqual(line[3][1], ((1, 2, 3), None, vt.REVERSE))
        self.assertEqual(t.attr, vt.DEFAULT)

    def test_odd_sequences_never_crash(self):
        t = self.vt()
        t.feed("\x1b[²m\x1b[1;³H\x1b[?¹h".encode())  # superscript digits: isdigit() but not int()
        t.feed("\x1b[31m\x1b[38;5;300mX\x1b[48;5;999mY\x1b[38;5mZ")
        self.assertEqual(t.screen[0][t.x - 3][1], (1, None, 0))  # an impossible colour keeps red
        self.assertTrue(t.text().strip().endswith("XYZ"))
        t.feed("\x1b[38;2;300;0;0mW")  # an impossible RGB value keeps the colour too
        self.assertEqual(t.screen[0][t.x - 1][1], (1, None, 0))

    def test_replies_for_status_requests(self):
        t = self.vt()
        t.feed("ab\x1b[6n\x1b[c")
        self.assertEqual(self.replies, [b"\x1b[1;3R", b"\x1b[?6c"])

    def test_utf8_split_over_reads_and_line_drawing(self):
        t = self.vt()
        data = "é€".encode()
        t.feed(data[:1])
        t.feed(data[1:])
        t.feed("\x1b(0qx\x1b(B")
        self.assertEqual(t.text().strip(), "é€─│")

    def test_osc_title_is_skipped(self):
        t = self.vt()
        t.feed("\x1b]0;title\x07ok\x1b]2;x\x1b\\!")
        self.assertEqual(t.text().strip(), "ok!")

    def test_key_bytes(self):
        t = self.vt()
        self.assertEqual(t.key_bytes("up"), b"\x1b[A")
        t.feed("\x1b[?1h")
        self.assertEqual(t.key_bytes("up"), b"\x1bOA")
        self.assertEqual(t.key_bytes("c", ctrl=True), b"\x03")
        self.assertEqual(t.key_bytes("enter"), b"\r")
        self.assertEqual(t.key_bytes("backspace"), b"\x7f")
        self.assertEqual(t.key_bytes("x", alt=True), b"\x1bx")

    def test_saved_cursor_survives_a_smaller_screen(self):
        t = self.vt(71, 75)
        t.feed("\x1b[75;60H\x1b7\x1b[?1049h")  # cursor saved on line 75, then nano-like alt screen
        t.resize(71, 52)  # the keyboard appears
        t.feed("\x1b8x\x1b[?1049l\x1b[uy")
        self.assertEqual((t.y, t.rows), (51, 52))

    def test_resize_keeps_the_cursor_line(self):
        t = self.vt(10, 6)
        t.feed("a\r\nb\r\nc\r\nd\r\ne\r\nf")
        t.resize(8, 3)
        self.assertEqual(t.text().splitlines(), ["d", "e", "f"])
        self.assertEqual(t.y, 2)
        self.assertEqual(["".join(c[0] for c in ln).strip() for ln in t.scrollback], ["a", "b", "c"])
        t.resize(8, 5)
        self.assertEqual(len(t.screen), 5)
        self.assertEqual(t.y, 2)

    def test_the_shell_gets_hangup_back_even_though_the_app_ignores_it(self):
        old = signal.signal(signal.SIGHUP, signal.SIG_IGN)  # as i14-ui's main() does
        try:
            s = term.Session(80, 24, argv=[sys.executable, "-c",
                                           "import signal; print(int(signal.getsignal(signal.SIGHUP)))"])
        finally:
            signal.signal(signal.SIGHUP, old)
        try:
            end = time.monotonic() + 10
            while s.alive and time.monotonic() < end:
                s.read()
                time.sleep(0.02)
            self.assertEqual(s.vt.text().strip(), "0")  # SIG_DFL: a closing terminal ends its programs
        finally:
            s.close()
            term.reap()

    def test_real_shell_in_a_pty(self):
        s = term.Session(40, 10, argv=["/bin/sh"], env={"HOME": "/tmp", "PATH": "/usr/bin:/bin", "PS1": "$ "})
        try:
            s.write(b"echo hel''lo; exit\r")
            end = time.monotonic() + 10
            while s.alive and time.monotonic() < end:
                s.read()
                time.sleep(0.02)
            self.assertIn("hello", s.vt.text())
            self.assertFalse(s.alive)
        finally:
            s.close()
            term.reap()


class FakeSupplicant:
    """A unix datagram server answering like wpa_supplicant; records commands, can send events."""

    def __init__(self, d, replies):
        self.path = os.path.join(d, "wlan0")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(0.1)
        self.replies = replies
        self.commands = []
        self.attached = []
        self.stop = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                continue
            cmd = data.decode()
            self.commands.append(cmd)
            if cmd == "ATTACH":
                self.attached.append(addr)
                reply = "OK"
            else:
                reply = self.replies(cmd)
            self.sock.sendto(reply.encode() + b"\n", addr)

    def event(self, text):
        for addr in self.attached:
            self.sock.sendto(f"<3>{text}".encode(), addr)

    def close(self):
        self.stop = True
        self.thread.join()
        self.sock.close()


class WifiParsing(unittest.TestCase):
    def test_psk_matches_the_ieee_test_vector(self):
        self.assertEqual(wifi.psk_hex("password", b"IEEE"),
                         "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e")

    def test_ssid_escapes(self):
        self.assertEqual(wifi.decode_ssid(r"caf\xc3\xa9 \"x\" \\"), 'café "x" \\'.encode())

    def test_scan_results(self):
        text = ("bssid / frequency / signal level / flags / ssid\n"
                "aa:bb:cc:dd:ee:01\t2412\t-70\t[WPA2-PSK-CCMP][ESS]\tExampleNet\n"
                "aa:bb:cc:dd:ee:02\t5180\t-50\t[WPA2-PSK-CCMP][ESS]\tExampleNet\n"
                "aa:bb:cc:dd:ee:03\t2437\t-60\t[ESS]\tGuestNet\n"
                "aa:bb:cc:dd:ee:04\t2437\t-40\t[WPA2-EAP-CCMP][ESS]\tWorkNet\n"
                "aa:bb:cc:dd:ee:05\t2437\t-30\t[ESS]\t\\x00\\x00\n")
        res = wifi.parse_scan(text)
        self.assertEqual([(r["ssid"], r["signal"], r["security"]) for r in res],
                         [(b"WorkNet", -40, "eap"), (b"ExampleNet", -50, "psk"), (b"GuestNet", -60, "open")])

    def test_store_put_replace_priority_remove(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "w.conf")
            with open(path, "w") as f:
                f.write('# kept\nnetwork={\n\tssid="OldNet"\n\tpsk="geheim123"\n}\n')
            s = wifi.Store(path)
            s.put(b"NewNet", "WPA-PSK", "ab" * 32)
            s.put(b"NewNet", "WPA-PSK", "cd" * 32)
            self.assertEqual([n["ssid"] for n in s.networks()], [b"OldNet", b"NewNet"])
            self.assertTrue(s.set_priority(b"NewNet", 5))
            s.put(b"NewNet", "WPA-PSK", "ef" * 32)  # a new password keeps the priority
            text = pathlib.Path(path).read_text()
            self.assertIn("# kept", text)
            self.assertIn("ssid=" + b"NewNet".hex(), text)
            self.assertIn("psk=" + "ef" * 32, text)
            self.assertNotIn("cd" * 32, text)
            self.assertIn("priority=5", text)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertTrue(s.remove(b"OldNet"))
            self.assertFalse(s.remove(b"OldNet"))
            self.assertEqual([n["ssid"] for n in s.networks()], [b"NewNet"])
            self.assertEqual(os.listdir(d), ["w.conf"])  # no temporary files left


class WifiManager(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(dir="/tmp", prefix="w")
        self.state = {"wpa_state": "SCANNING", "ssid": None, "ip": "", "next_id": 3}

        def replies(cmd):
            st = self.state
            if cmd == "PING":
                return "PONG"
            if cmd == "STATUS":
                out = f"wpa_state={st['wpa_state']}"
                return out + (f"\nssid={st['ssid']}" if st["ssid"] else "")
            if cmd == "LIST_NETWORKS":
                return "network id / ssid / bssid / flags\n0\tExampleNet\tany\t[CURRENT]"
            if cmd == "ADD_NETWORK":
                if st.get("add_fails"):
                    return "FAIL"
                st["next_id"] += 1
                return str(st["next_id"])
            return "OK"
        self.sup = FakeSupplicant(self.d, replies)
        self.store = wifi.Store(os.path.join(self.d, "saved.conf"))
        self.start_files = []
        self.w = wifi.Wifi(self.sup.path, store=self.store, rootfs_store=wifi.Store(os.path.join(self.d, "r.conf")),
                           off_flag=os.path.join(self.d, "off"), ip=lambda: self.state["ip"], local_dir=self.d,
                           start_file=lambda: self.start_files.append(self.store.networks()) or True)
        self.assertIsNotNone(self.w.monitor_fd())

    def tearDown(self):
        self.w.req.close()
        self.w.mon.close()
        self.sup.close()
        import shutil
        shutil.rmtree(self.d)

    def pump(self, until, timeout=3):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.w.events()
            if until():
                return True
            time.sleep(0.02)
        return False

    def test_wrong_password_is_reported_and_not_saved(self):
        self.assertTrue(self.w.connect(b"NeighborNet", "psk", passphrase="verkeerd1"))
        sets = [c for c in self.sup.commands if c.startswith("SET_NETWORK 4")]
        self.assertIn("SET_NETWORK 4 ssid " + b"NeighborNet".hex(), sets)
        self.assertIn("SET_NETWORK 4 psk " + wifi.psk_hex("verkeerd1", b"NeighborNet"), sets)
        self.assertIn("SELECT_NETWORK 4", self.sup.commands)
        self.sup.event('CTRL-EVENT-SSID-TEMP-DISABLED id=4 ssid="NeighborNet" auth_failures=1 duration=10 '
                       'reason=WRONG_KEY')
        self.assertTrue(self.pump(lambda: self.w.attempt is None))
        self.assertEqual(self.w.message, "Verkeerd wachtwoord")
        self.assertIn("REMOVE_NETWORK 4", self.sup.commands)
        self.assertIn("ENABLE_NETWORK all", self.sup.commands)
        self.assertEqual(self.store.networks(), [])
        self.assertEqual(self.start_files, [])

    def test_success_saves_the_network_and_waits_for_an_address(self):
        self.w.connect(b"NeighborNet", "psk", passphrase="goedgoed")
        self.state.update(wpa_state="COMPLETED", ssid="NeighborNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:bb completed [id=4]")
        self.assertTrue(self.pump(lambda: self.w.attempt and self.w.attempt["phase"] == "dhcp"))
        self.assertEqual([n["ssid"] for n in self.store.networks()], [b"NeighborNet"])
        self.assertEqual([n["ssid"] for n in self.start_files[-1]], [b"NeighborNet"])  # a respawn starts with it
        self.w.attempt["since"] -= 30
        self.w.tick()
        self.assertEqual(self.w.message, "Verbonden, maar geen IP-adres gekregen (DHCP)")
        self.assertEqual([n["ssid"] for n in self.store.networks()], [b"NeighborNet"])  # still saved

    def test_address_ends_the_attempt(self):
        self.w.connect(b"GuestNet", "open")
        self.assertIn("SET_NETWORK 4 key_mgmt NONE", self.sup.commands)
        self.state.update(wpa_state="COMPLETED", ssid="GuestNet", ip="10.0.0.7")
        self.w.tick()
        self.w.attempt["since"] -= 10
        self.w.tick()
        self.assertIsNone(self.w.attempt)
        self.assertEqual(self.w.message, "Verbonden met GuestNet, adres 10.0.0.7")

    def test_network_not_found(self):
        self.w.connect(b"ExampleNet", "psk")  # saved: no password needed
        self.assertNotIn("ADD_NETWORK", self.sup.commands)
        self.assertIn("SELECT_NETWORK 0", self.sup.commands)
        self.sup.event("CTRL-EVENT-NETWORK-NOT-FOUND")
        self.sup.event("CTRL-EVENT-NETWORK-NOT-FOUND")
        self.assertTrue(self.pump(lambda: self.w.attempt is None))
        self.assertEqual(self.w.message, "ExampleNet niet gevonden")
        self.assertNotIn("REMOVE_NETWORK 0", self.sup.commands)  # a saved network stays

    def test_unsupported_and_short_passwords(self):
        self.assertFalse(self.w.connect(b"X", "sae", passphrase="12345678"))
        self.assertIn("WPA3", self.w.message)
        self.assertFalse(self.w.connect(b"X", "psk", passphrase="kort"))
        self.assertNotIn("ADD_NETWORK", self.sup.commands)

    def test_off_and_on(self):
        # the flag first, then the start file, then wpa_supplicant reloads it (also what a respawn reads)
        self.w.set_enabled(False)
        self.assertTrue(os.path.exists(os.path.join(self.d, "off")))
        self.assertEqual((len(self.start_files), self.sup.commands[-1]), (1, "RECONFIGURE"))
        self.w.set_enabled(True)
        self.assertFalse(os.path.exists(os.path.join(self.d, "off")))
        self.assertEqual((len(self.start_files), self.sup.commands[-1]), (2, "RECONFIGURE"))
        leases = []
        self.w.new_lease = lambda: leases.append(1)
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:bb completed [id=0]")
        self.assertTrue(self.pump(lambda: leases))  # back on: a fresh lease (the old one may have no route)
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:bb completed [id=0]")
        self.pump(lambda: False, timeout=0.3)
        self.assertEqual(leases, [1])  # only once

    def test_switching_off_is_undone_when_it_cannot_take_effect(self):
        self.w.start_file = lambda: False
        self.assertFalse(self.w.set_enabled(False))
        self.assertFalse(os.path.exists(os.path.join(self.d, "off")))  # the switch still says on
        self.assertNotIn("RECONFIGURE", self.sup.commands)
        self.assertIn("mislukt", self.w.message)
        self.w.start_file = lambda: True
        orig = self.sup.replies
        self.sup.replies = lambda cmd: "FAIL" if cmd == "RECONFIGURE" else orig(cmd)
        self.assertFalse(self.w.set_enabled(False))
        self.assertFalse(os.path.exists(os.path.join(self.d, "off")))
        self.assertTrue(self.w.enabled)

    def test_unsupported_network_while_off_leaves_wifi_off(self):
        with open(os.path.join(self.d, "off"), "w") as f:
            f.write("off\n")
        self.assertFalse(self.w.connect(b"X", "eap"))
        self.assertFalse(self.w.connect(b"X", "psk", passphrase="kort", hidden=True))
        self.assertEqual(self.w.message, "Wachtwoord moet 8 tot 63 tekens zijn")
        self.assertFalse(self.w.enabled)
        self.assertNotIn("RECONFIGURE", self.sup.commands)

    def test_connect_while_off_stops_when_switching_on_fails(self):
        with open(os.path.join(self.d, "off"), "w") as f:
            f.write("off\n")
        self.w.start_file = lambda: False
        self.assertFalse(self.w.connect(b"NeighborNet", "psk", passphrase="goedgoed"))
        self.assertNotIn("ADD_NETWORK", self.sup.commands)
        self.assertIn("mislukt", self.w.message)
        self.assertFalse(self.w.enabled)

    def test_switching_off_ends_a_running_attempt_even_when_it_fails(self):
        self.w.connect(b"NeighborNet", "psk", passphrase="goedgoed")
        self.w.start_file = lambda: False
        self.assertFalse(self.w.set_enabled(False))
        self.assertIn("REMOVE_NETWORK 4", self.sup.commands)
        self.assertEqual(self.sup.commands.count("ENABLE_NETWORK all"), 1)
        self.assertIsNone(self.w.attempt)

    def test_prefer_leaves_networks_from_the_rootfs_alone(self):
        self.assertFalse(self.w.prefer(b"ExampleNet"))  # running, but not in the userdata file
        self.assertFalse(any(c.startswith("SET_NETWORK 0 priority") for c in self.sup.commands))

    def test_forget_removes_running_and_saved(self):
        self.store.put(b"ExampleNet", "WPA-PSK", "ab" * 32)
        self.assertTrue(self.w.forget(b"ExampleNet"))
        self.assertIn("REMOVE_NETWORK 0", self.sup.commands)
        self.assertEqual(self.store.networks(), [])
        self.assertEqual(self.start_files, [[]])

    def test_forget_keeps_the_running_network_when_the_file_cannot_be_written(self):
        self.store.put(b"ExampleNet", "WPA-PSK", "ab" * 32)
        with patch.object(wifi.os, "replace", side_effect=PermissionError("read-only fixture")):
            self.assertFalse(self.w.forget(b"ExampleNet"))
        self.assertNotIn("REMOVE_NETWORK 0", self.sup.commands)
        self.assertIn("vergeten mislukt", self.w.message)

    def test_any_switch_to_another_network_gets_a_new_lease(self):
        leases = []
        self.w.new_lease = lambda: leases.append(1)
        self.state.update(wpa_state="COMPLETED", ssid="ExampleNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:01 completed [id=0]")
        self.assertTrue(self.pump(lambda: self.w.last_ssid == b"ExampleNet"))
        self.assertEqual(leases, [])  # the same network again: keep the lease
        self.w.forget(b"ExampleNet")  # wpa_supplicant then picks another saved network by itself
        self.state.update(ssid="WorkNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:02 completed [id=1]")
        self.assertTrue(self.pump(lambda: leases))
        self.assertEqual(self.w.last_ssid, b"WorkNet")

    def test_a_network_joined_before_attach_is_the_baseline(self):
        self.w.mon.close()  # start over: STATUS said SCANNING, then ExampleNet connected before ATTACH
        self.w.attached, self.w.last_ssid, self.w.next_attach = False, None, 0.0
        leases = []
        self.w.new_lease = lambda: leases.append(1)
        self.state.update(wpa_state="COMPLETED", ssid="ExampleNet")
        self.assertIsNotNone(self.w.monitor_fd())
        self.assertEqual((self.w.last_ssid, leases), (b"ExampleNet", []))
        self.state.update(ssid="WorkNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:02 completed [id=1]")
        self.assertTrue(self.pump(lambda: leases))

    def test_a_new_network_from_the_ui_gets_one_new_lease(self):
        leases = []
        self.w.new_lease = lambda: leases.append(1)
        self.state.update(wpa_state="COMPLETED", ssid="ExampleNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:01 completed [id=0]")
        self.assertTrue(self.pump(lambda: self.w.last_ssid == b"ExampleNet"))
        self.w.connect(b"NeighborNet", "psk", passphrase="goedgoed")
        self.state.update(ssid="NeighborNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:03 completed [id=4]")
        self.assertTrue(self.pump(lambda: self.w.attempt and self.w.attempt["phase"] == "dhcp"))
        self.assertEqual(leases, [1])

    def test_a_restarted_wpa_supplicant_is_attached_again(self):
        leases = []
        self.w.new_lease = lambda: leases.append(1)
        self.state.update(wpa_state="COMPLETED", ssid="ExampleNet")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:01 completed [id=0]")
        self.assertTrue(self.pump(lambda: self.w.last_ssid == b"ExampleNet"))
        self.sup.close()  # wpa_supplicant dies; on Linux the event socket would just go quiet
        os.unlink(self.sup.path)
        self.w.next_ping = 0
        self.w.tick()  # the periodic PING notices it, also with the screen off
        self.assertFalse(self.w.attached)
        self.state.update(ssid="WorkNet")  # it came back on another network while nobody listened
        self.sup = FakeSupplicant(self.d, self.sup.replies)
        self.assertIsNotNone(self.w.monitor_fd())
        self.assertTrue(self.w.attached)
        self.assertEqual(leases, [1])
        self.assertEqual(self.w.last_ssid, b"WorkNet")
        self.sup.event("CTRL-EVENT-SCAN-RESULTS")
        self.w.scanning = True
        self.assertTrue(self.pump(lambda: not self.w.scanning))  # events arrive again

    def test_a_second_choice_removes_the_first_unconfirmed_network(self):
        self.w.connect(b"A-net", "psk", passphrase="wachtwoordA")
        self.w.connect(b"B-net", "psk", passphrase="wachtwoordB")
        self.assertLess(self.sup.commands.index("REMOVE_NETWORK 4"), self.sup.commands.index("SELECT_NETWORK 5"))
        self.assertEqual(self.w.attempt["id"], 5)

    def test_add_network_failure_is_a_message_not_a_crash(self):
        self.state["add_fails"] = True
        self.assertFalse(self.w.connect(b"NeighborNet", "psk", passphrase="goedgoed"))
        self.assertEqual(self.w.message, "Instellen mislukt (FAIL)")
        self.assertIsNone(self.w.attempt)

    def test_a_stale_start_file_is_reported(self):
        self.store.put(b"ExampleNet", "WPA-PSK", "ab" * 32)
        self.w.start_file = lambda: False
        self.assertTrue(self.w.forget(b"ExampleNet"))
        self.assertIn("startbestand niet bijgewerkt", self.w.message)
        self.w.connect(b"NeighborNet", "psk", passphrase="goedgoed")
        self.state.update(wpa_state="COMPLETED", ssid="NeighborNet", ip="10.0.0.9")
        self.sup.event("CTRL-EVENT-CONNECTED - Connection to aa:03 completed [id=4]")
        self.assertTrue(self.pump(lambda: self.w.attempt and self.w.attempt["phase"] == "dhcp"))
        self.w.attempt["since"] -= 10
        self.w.tick()
        self.assertNotIn("(niet opgeslagen)", self.w.message)  # it is on userdata: back after a reboot
        self.assertIn("startbestand niet bijgewerkt", self.w.message)
        self.assertEqual([n["ssid"] for n in self.store.networks()], [b"NeighborNet"])

    def test_scan_results_event(self):
        self.w.scan()
        self.assertTrue(self.w.scanning)
        self.sup.event("CTRL-EVENT-SCAN-RESULTS")
        self.assertTrue(self.pump(lambda: not self.w.scanning))
        self.assertIn("SCAN_RESULTS", self.sup.commands)


class Bluetooth(unittest.TestCase):
    def setUp(self):
        self.a, self.b = socket.socketpair()
        self.a.setblocking(False)
        self.bt = bt.Bluetooth(opener=lambda p: os.dup(self.a.fileno()))

    def tearDown(self):
        self.bt.power(False)
        self.a.close()
        self.b.close()

    def sent(self):
        self.b.setblocking(False)
        data = b""
        try:
            while True:
                data += self.b.recv(4096)
        except BlockingIOError:
            return data

    def reply(self, data):
        self.b.send(data)
        self.bt.read()

    def complete(self, opcode, params=b""):
        p = bytes([1]) + struct.pack("<H", opcode) + b"\x00" + params
        self.reply(bytes([4, 0x0E, len(p)]) + p)

    def test_power_on_resets_and_reads_the_address(self):
        self.assertTrue(self.bt.power(True))
        self.assertEqual(self.sent(), bt.command(bt.RESET))
        self.complete(bt.RESET)
        self.assertTrue(self.sent().startswith(bt.command(bt.READ_BD_ADDR)))
        self.complete(bt.READ_BD_ADDR, bytes([6, 5, 4, 3, 2, 1]))
        self.assertEqual(self.bt.address, "01:02:03:04:05:06")

    def ready(self):
        self.bt.power(True)
        self.complete(bt.RESET)
        self.complete(bt.WRITE_INQUIRY_MODE)
        self.sent()

    def test_search_waits_for_the_reset_when_it_switches_bluetooth_on(self):
        self.bt.search()
        self.assertEqual(self.sent(), bt.command(bt.RESET))  # no inquiry before the reset has finished
        self.assertEqual(self.bt.phase, "start")
        self.complete(bt.RESET)
        self.assertNotIn(bt.command(bt.INQUIRY, b"\x33\x8b\x9e\x08\x00"), self.sent())
        self.complete(bt.WRITE_INQUIRY_MODE)
        self.assertEqual(self.sent(), bt.command(bt.INQUIRY, b"\x33\x8b\x9e\x08\x00"))
        self.assertEqual(self.bt.phase, "inquiry")

    def test_a_silent_controller_ends_the_search(self):
        self.bt.search()
        self.bt.phase_end = 0
        self.bt.tick()
        self.assertEqual((self.bt.phase, self.bt.on, self.bt.message), (None, False, "Bluetooth reageert niet"))

    def test_search_classic_names_then_le(self):
        self.ready()
        self.bt.search()
        self.assertTrue(self.sent().startswith(bt.command(bt.INQUIRY, b"\x33\x8b\x9e\x08\x00")))
        # extended inquiry result with a name, an RSSI result without one
        eir = bytes([7, 0x09]) + b"Toets1" + b"\0" * 232
        rec = bytes([1, 1, 1, 1, 1, 1]) + bytes([1, 0, 0x40, 0x05, 0x25, 0, 0]) + struct.pack("b", -60)
        self.reply(bytes([4, 0x2F, 255]) + bytes([1]) + rec + eir)
        rec2 = bytes([2, 2, 2, 2, 2, 2]) + bytes([1, 0, 0, 0, 0, 0, 0]) + struct.pack("b", -80)
        self.reply(bytes([4, 0x22, 15, 1]) + rec2)
        self.reply(bytes([4, 0x01, 1, 0]))  # inquiry complete: ask the second device's name
        self.assertTrue(self.sent().startswith(bt.command(bt.REMOTE_NAME, bytes([2] * 6) + b"\x01\x00\x00\x00")))
        name = b"Speaker".ljust(248, b"\0")
        self.reply(bytes([4, 0x07, 255, 0]) + bytes([2] * 6) + name)
        self.assertEqual(self.bt.phase, "le")
        adv = bytes([0x02, 1, 0, 0]) + bytes([3] * 6) + bytes([5, 4, 0x09]) + b"Tag" + struct.pack("b", -50)
        self.reply(bytes([4, 0x3E, len(adv)]) + adv)
        self.bt.phase_end = 0
        self.bt.tick()
        self.assertIsNone(self.bt.phase)
        self.bt.devices["04:04:04:04:04:04"] = {"name": None, "rssi": -20, "kind": "BLE"}
        self.assertEqual(self.bt.listing(), [("03:03:03:03:03:03", "Tag", -50, "BLE"),
                                             ("01:01:01:01:01:01", "Toets1", -60, "klassiek"),
                                             ("02:02:02:02:02:02", "Speaker", -80, "klassiek"),
                                             ("04:04:04:04:04:04", "", -20, "BLE")])  # unnamed last
        del self.bt.devices["04:04:04:04:04:04"]
        self.assertEqual(self.bt.message, "3 apparaten gevonden")

    def test_write_error_switches_bluetooth_off_instead_of_raising(self):
        self.ready()
        self.b.close()  # the other end is gone: writes fail
        self.bt.search()
        self.assertFalse(self.bt.on)
        self.assertIn("Bluetooth-fout", self.bt.message)
        self.bt.tick()

    def test_a_search_that_switched_bluetooth_on_switches_it_off_again(self):
        self.bt.search()
        self.assertTrue(self.bt.on)
        self.complete(bt.RESET)
        self.complete(bt.WRITE_INQUIRY_MODE)
        self.bt.phase, self.bt.phase_end = "le", 0
        self.bt.tick()
        self.assertFalse(self.bt.on)
        self.bt.power(True)  # switched on by the user: stays on after a search
        self.bt.search()
        self.bt.phase, self.bt.phase_end = "le", 0
        self.bt.tick()
        self.assertTrue(self.bt.on)

    def test_a_failed_reset_write_is_reported_not_hidden(self):
        self.b.close()
        self.assertFalse(self.bt.power(True))
        self.bt.search()
        self.assertFalse(self.bt.on)
        self.assertIn("Bluetooth-fout", self.bt.message)
        self.assertIsNone(self.bt.phase)

    def test_a_refused_reset_switches_a_user_enabled_radio_off_too(self):
        self.bt.power(True)
        self.bt.handle(0x0E, bytes([1]) + struct.pack("<H", bt.RESET) + b"\x03")
        self.assertFalse(self.bt.on)
        self.assertEqual(self.bt.message, "Bluetooth-reset mislukt (status 3)")

    def test_a_refused_reset_ends_the_waiting_search(self):
        self.bt.search()
        self.bt.handle(0x0E, bytes([1]) + struct.pack("<H", bt.RESET) + b"\x03")
        self.assertEqual((self.bt.phase, self.bt.on), (None, False))
        self.assertEqual(self.bt.message, "Bluetooth-reset mislukt (status 3)")

    def test_packets_split_and_junk(self):
        p = bt.Packets()
        self.assertEqual(p.feed(b"\x00\x04\x0e"), [])
        self.assertEqual(p.feed(b"\x01\x05\x04\x0e\x00"), [(0x0E, b"\x05"), (0x0E, b"")])


class Settings(unittest.TestCase):
    def test_charge_limit_is_validated_and_preserves_pin(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ui.json")
            c = config.Config(path)
            c.set_pin("4821")
            c.set("charge_limit", 70)
            c.set("charge_limit_enabled", True)
            saved = config.Config(path)
            self.assertTrue(saved["charge_limit_enabled"])
            self.assertEqual(saved["charge_limit"], 70)
            self.assertTrue(saved.check_pin("4821"))
            for bad in (True, 80.0, 85, None, "80"):
                data = dict(saved.data, charge_limit=bad)
                pathlib.Path(path).write_text(__import__('json').dumps(data))
                self.assertFalse(config.Config(path)["charge_limit_enabled"])

    def test_failed_save_keeps_previous_setting(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            c = config.Config(os.path.join(d, "ui.json"))
            with mock.patch.object(c, "save", return_value=False):
                self.assertFalse(c.set("charge_limit_enabled", True))
            self.assertFalse(c["charge_limit_enabled"])

    def test_defaults_bad_values_and_pin(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ui.json")
            with open(path, "w") as f:
                f.write('{"timeout": 7, "brightness": 999, "other": 1}')
            c = config.Config(path)
            self.assertEqual((c["timeout"], c["brightness"]), (60, 255))
            self.assertFalse(c.has_pin)
            salt = bytes.fromhex("48210000000000000000000000000000")
            with patch.object(config.os, "urandom", return_value=salt):
                self.assertTrue(c.set_pin("4821"))
            c2 = config.Config(path)
            self.assertTrue(c2.has_pin)
            self.assertTrue(c2.check_pin("4821"))
            self.assertFalse(c2.check_pin("4822"))
            self.assertEqual(json.loads(pathlib.Path(path).read_text()), dict(
                config.DEFAULTS, timeout=60, brightness=255,
                pin={"salt": salt.hex(),
                     "hash": "734adf0a8363f86f5f7fa0e1806855abb78ab1ff0a87d9a4f368db76770ca5ab",
                     "rounds": 100_000},
            ))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            c2.set_pin(None)
            self.assertFalse(config.Config(path).has_pin)

    def test_damaged_file_fails_closed(self):
        for text in ("{not json", "[]", '{"pin": "1234"}', '{"pin": {"salt": "00"}}'):
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "ui.json")
                with open(path, "w") as f:
                    f.write(text)
                c = config.Config(path)
                self.assertTrue(c.damaged and c.has_pin, text)
                self.assertFalse(c.check_pin("1234") or c.check_pin(""), text)
                self.assertFalse(c.set("timeout", 30))
                self.assertEqual(pathlib.Path(path).read_text(), text)  # never overwritten

    def test_missing_file_is_first_use(self):
        with tempfile.TemporaryDirectory() as d:
            c = config.Config(os.path.join(d, "ui.json"))
            self.assertFalse(c.damaged or c.has_pin)
            self.assertEqual(c.data, config.DEFAULTS)


class OnScreenKeyboard(unittest.TestCase):
    def setUp(self):
        self.keys = []
        self.kb = widgets.Keyboard(720, 1600, self.keys.append, terminal_row=True)

    def tap(self, out):
        rect = next(k[0] for k in self.kb.keys if k[2] == out)
        x, y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        self.kb.touch(("down", x, y))
        self.kb.touch(("up", x, y))

    def test_letters_shift_caps_and_symbols(self):
        self.tap("h")
        self.tap("shift")
        self.tap("i")
        self.tap("i")
        self.tap("shift")
        self.tap("shift")  # caps lock
        self.tap("o")
        self.tap("o")
        self.tap("sym")
        self.tap("1")
        self.tap("more")
        self.tap("\\")
        self.tap("abc")
        self.tap(" ")
        self.tap("enter")
        self.assertEqual([k[0] for k in self.keys], ["h", "I", "i", "O", "O", "1", "\\", " ", "enter"])

    def test_the_gap_between_keys_belongs_to_a_key(self):
        c = next(k for k in self.kb.keys if k[2] == "c")
        self.kb.touch(("down", c[0][0] + c[0][2] + 1, c[0][1] + 5))  # in the drawn gap right of "c"
        self.kb.touch(("up", c[0][0] + c[0][2] + 1, c[0][1] + 5))
        self.assertEqual(len(self.keys), 1)

    def test_ctrl_applies_to_the_next_key(self):
        self.tap("ctrl")
        self.tap("c")
        self.tap("c")
        self.assertEqual(self.keys, [("c", True, False), ("c", False, False)])

    def test_slide_to_another_key_and_release_outside(self):
        a = next(k[0] for k in self.kb.keys if k[2] == "a")
        s = next(k[0] for k in self.kb.keys if k[2] == "s")
        self.kb.touch(("down", a[0] + 5, a[1] + 5))
        self.kb.touch(("move", s[0] + 5, s[1] + 5))
        self.kb.touch(("up", s[0] + 5, s[1] + 5))
        self.kb.touch(("down", a[0] + 5, a[1] + 5))
        self.kb.touch(("move", 5, 100))  # off the keyboard
        self.kb.touch(("up", 5, 100))
        self.assertEqual([k[0] for k in self.keys], ["s"])

    def test_backspace_repeats_while_held(self):
        r = next(k[0] for k in self.kb.keys if k[2] == "backspace")
        self.kb.touch(("down", r[0] + 5, r[1] + 5))
        self.kb.repeat_at = time.monotonic() - 1
        self.assertTrue(self.kb.tick())
        self.kb.repeat_at = time.monotonic() - 1
        self.kb.tick()
        self.kb.touch(("up", r[0] + 5, r[1] + 5))
        self.assertEqual([k[0] for k in self.keys], ["backspace", "backspace"])  # no extra one on release


class FakeScreen:
    def __init__(self):
        self.canvas = fb.Canvas(720, 1600)
        self.on = True
        self.shown = 0
        self.brightness = None

    def show(self):
        self.shown += 1

    def power(self, on):
        self.on = on

    def set_brightness(self, v):
        self.brightness = v


class FakeWifi:
    enabled, scanning, attempt, message, changed, scan_results = True, False, None, "", False, []

    def monitor_fd(self):
        return None

    def status(self):
        return {"wpa_state": "COMPLETED", "ssid_bytes": b"ExampleNet", "ip_address": "192.0.2.16"}

    def saved(self):
        return [{"id": 0, "ssid": b"ExampleNet", "flags": "[CURRENT]", "priority": 0}]

    def network_id(self, ssid):
        return 0 if ssid == b"ExampleNet" else None

    def available(self):
        return True

    def scan(self):
        self.scanning = True

    def tick(self):
        pass


class Pages(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        f = make_font(self.d.name)
        self.cfg = config.Config(os.path.join(self.d.name, "ui.json"))
        self.screen = FakeScreen()
        self.app = appmod.App(self.screen, (f, f, f, f), self.cfg, FakeWifi(), bt.Bluetooth(opener=self.no_bt),
                             terminal_enabled=True)

    def tearDown(self):
        if self.app.session:
            self.app.session.close()
        term.reap()
        self.d.cleanup()

    @staticmethod
    def no_bt(path):
        raise FileNotFoundError(2, "No such file or directory")

    def tap_button(self, label):
        for b in self.app.page.buttons:
            text = b.label() if callable(b.label) else b.label
            if text == label:
                x, y = b.rect[0] + 5, b.rect[1] + 5
                self.app.touch(("down", x, y))
                self.app.touch(("up", x, y))
                return
        self.fail(f"no button {label!r}")

    def test_every_page_renders(self):
        self.app.render()
        for label, cls in (("Wi-Fi", appmod.WifiPage), ("Bluetooth", appmod.BluetoothPage),
                           ("Instellingen", appmod.SettingsPage), ("Aan/uit", appmod.PowerPage)):
            self.tap_button(label)
            self.assertIsInstance(self.app.page, cls)
            self.app.render()
            self.app.pop()
        self.assertIsInstance(self.app.page, appmod.Home)

    def test_dns_profile_has_no_terminal_access(self):
        f = self.app.big
        app = appmod.App(self.screen, (f, f, f, f), self.cfg, FakeWifi(), bt.Bluetooth(opener=self.no_bt),
                         terminal_enabled=False)
        self.assertNotIn("Terminal", [b.label for b in app.page.buttons])
        app.push(appmod.TerminalPage)
        self.assertIsInstance(app.page, appmod.Home)
        self.assertIsNone(app.session)

    def test_bluetooth_failure_is_shown_not_raised(self):
        self.tap_button("Bluetooth")
        self.tap_button("Bluetooth: uit")
        self.assertIn("Bluetooth starten mislukt", self.app.bt.message)
        self.app.render()

    def test_terminal_page_draws_odd_output(self):
        self.tap_button("Terminal")
        vt_ = self.app.session.vt
        vt_.feed("\x1b[²m\x1b[38;5;300mX\x1b[48;5;999mY\x1b[38;5;200;48;5;255mZ".encode())
        vt_.main[0][0] = ("Q", (300, 999, 0))  # even a cell that got an impossible colour some other way
        self.app.render()
        self.assertEqual(appmod.palette(300), appmod.palette(255))

    def test_bold_reverse_colour_stays_readable(self):
        vt_ = vt.VT(4, 1)
        vt_.feed("\x1b[1;7;31mA\x1b[0;7;31mB")
        c = fb.Canvas(40, 16)
        self.app.line_cache.clear()
        self.app.draw_term_line(c, 0, 0, vt_.screen[0])
        # the test font: first glyph row lit (foreground), the rest background
        self.assertEqual((c.pixel(0, 0), c.pixel(0, 5)), ((0, 0, 0), appmod.palette(9)))  # black on bright red
        self.assertEqual((c.pixel(8, 0), c.pixel(8, 5)), ((0, 0, 0), appmod.palette(1)))  # black on red

    def test_terminal_typing_runs_a_command(self):
        self.tap_button("Terminal")
        page = self.app.page
        self.assertIsInstance(page, appmod.TerminalPage)
        for ch in "echo zo''ef":
            page.key((ch, False, False))
        page.key(("enter", False, False))
        s = self.app.session
        end = time.monotonic() + 10
        while "zoef" not in s.vt.text() and time.monotonic() < end:
            s.read()
            time.sleep(0.02)
        self.assertIn("zoef", s.vt.text())
        self.app.render()
        cols, rows = page.size()
        self.assertEqual((s.vt.cols, s.vt.rows), (cols, rows))
        page.toggle_keyboard()  # more lines without the keyboard
        self.assertGreater(s.vt.rows, rows)

    def test_brightness_and_timeout_are_saved(self):
        self.tap_button("Instellingen")
        self.tap_button("+")
        self.assertEqual(self.screen.brightness, 145)
        self.tap_button("Scherm uit na: 1 min")
        self.assertEqual(config.Config(self.cfg.path)["timeout"], 120)

    def test_charge_limit_buttons_save_and_cycle(self):
        self.tap_button("Instellingen")
        self.tap_button("Laadgrens: uit")
        self.assertTrue(config.Config(self.cfg.path)["charge_limit_enabled"])
        for value, following in ((80, 90), (90, 100), (100, 60), (60, 70), (70, 80)):
            self.tap_button(f"Stoppen bij: ~{value}%")
            self.assertEqual(config.Config(self.cfg.path)["charge_limit"], following)
        self.tap_button("Laadgrens: aan")
        self.assertFalse(config.Config(self.cfg.path)["charge_limit_enabled"])
        self.app.render()

    def test_pin_set_lock_and_change(self):
        self.tap_button("Instellingen")
        self.tap_button("Pincode instellen")
        for digits in ("1234", "1234"):
            for d in digits:
                self.tap_button(d)
            self.tap_button("OK")
        self.assertIsInstance(self.app.page, appmod.SettingsPage)
        self.assertTrue(self.cfg.check_pin("1234") and self.cfg.has_pin)
        self.app.lock()
        self.assertTrue(self.app.locked())
        for d in "9999":
            self.tap_button(d)
        self.tap_button("OK")
        self.assertTrue(self.app.locked())
        self.app.touch(("down", 5, 5))  # no back button while locked
        self.app.touch(("up", 5, 5))
        self.assertTrue(self.app.locked())
        for d in "1234":
            self.tap_button(d)
        self.tap_button("OK")
        self.assertIsInstance(self.app.page, appmod.SettingsPage)
        # changing asks for the current PIN; backing out of that leaves settings untouched
        self.tap_button("Pincode wijzigen")
        self.assertIsInstance(self.app.page, appmod.LockPage)
        self.app.pop()
        self.assertIsInstance(self.app.page, appmod.SettingsPage)

    def test_main_loop_survives_the_shell_ending(self):
        self.tap_button("Terminal")
        runner = appmod.Runner(self.app, [], [], [])
        s = self.app.session
        s.write(b"exit\r")
        end = time.monotonic() + 10
        while s.alive and time.monotonic() < end:
            self.app.dirty = True
            runner.last_render = 0.0
            runner.loop_once()
        self.assertFalse(s.alive)
        for _ in range(3):  # renders and polls after the pty is closed
            self.app.dirty = True
            runner.loop_once()

    def test_screen_off_stops_a_held_backspace(self):
        self.tap_button("Terminal")
        kb = self.app.page.keyboard
        r = next(k[0] for k in kb.keys if k[2] == "backspace")
        self.app.touch(("down", r[0] + 5, r[1] + 5))
        runner = appmod.Runner(self.app, [], [], [])
        runner.screen_on(False)  # the release happens while the screen is off and is never delivered
        kb.repeat_at = time.monotonic() - 1
        self.assertFalse(kb.tick())
        self.assertIsNone(kb.active)

    def test_power_menu_confirmation_does_not_survive_a_lock(self):
        done = []
        self.app.power_action = done.append
        self.tap_button("Aan/uit")
        self.tap_button("Herstarten")
        self.cfg.set_pin("1234")
        self.app.lock()
        for d in "1234":
            self.tap_button(d)
        self.tap_button("OK")
        self.tap_button("Herstarten")
        self.assertEqual(done, [])

    def test_power_menu_needs_a_second_tap(self):
        done = []
        self.app.power_action = done.append
        self.tap_button("Aan/uit")
        self.tap_button("Herstarten")
        self.assertEqual(done, [])
        self.tap_button("Zeker? Tik nogmaals: Herstarten")
        self.assertEqual(done, ["reboot"])

    def test_rescue_writes_the_flag_i14init_reads_then_reboots(self):
        ran = []
        orig_data, orig_run = appmod.DATA, appmod.subprocess.run
        appmod.DATA = self.d.name
        appmod.subprocess.run = lambda argv, **kw: ran.append(argv)
        try:
            self.app.power_action("rescue")
        finally:
            appmod.DATA, appmod.subprocess.run = orig_data, orig_run
        self.assertTrue(os.path.exists(os.path.join(self.d.name, "rescue-once")))
        self.assertEqual(ran, [["reboot"]])
        init = (ROOT / "initramfs/i14init").read_text()
        self.assertIn("DIR=/i14/data/media/0/i14-linux", init)  # the same directory as /mnt/data/media/0/...
        self.assertIn("if [ -e $DIR/rescue-once ]; then", init)

    def test_new_scan_results_under_a_finger_cancel_the_tap(self):
        opened = []
        lst = widgets.ListView((0, 0, 720, 400), opened.append)
        lst.set_items([("A", "", (1, 1, 1)), ("B", "", (1, 1, 1))])
        lst.touch(("down", 50, 150))  # on "B"
        lst.set_items([("C", "", (1, 1, 1))])  # the list shrank meanwhile
        lst.touch(("up", 50, 150))
        lst.set_items([("A", "", (1, 1, 1)), ("B", "", (1, 1, 1))])
        lst.touch(("down", 50, 150))
        lst.set_items([("B", "", (1, 1, 1)), ("A", "", (1, 1, 1))])  # reordered
        lst.touch(("up", 50, 150))
        self.assertEqual(opened, [])
        lst.touch(("down", 50, 150))
        lst.touch(("up", 50, 150))
        self.assertEqual(opened, [1])

    def test_screen_off_resets_the_touch_parser(self):
        runner = appmod.Runner(self.app, [], [7], [])
        t = runner.touch[7]
        gestures(t, mt(0, 1, 100, 100))  # a finger down, then the chip sleeps without a lift
        runner.screen_on(False)
        self.assertEqual(gestures(runner.touch[7], mt(0, 2, 300, 300), mt(0, -1)), [("down", 300, 300),
                                                                                  ("up", 300, 300)])
        runner.screen_on(True)
        runner.screen_on(False)
        # the positions survive: a tap on the same row brings only X (the kernel drops the unchanged Y)
        self.assertEqual(gestures(runner.touch[7], mt(0, 3, 54)), [("down", 54, 300)])

    def test_back_button_and_lists(self):
        self.tap_button("Wi-Fi")
        page = self.app.page
        self.assertEqual(page.rows[0][2], "ExampleNet")
        page.list.touch(("down", 50, page.list.rect[1] + 20))
        page.list.touch(("up", 50, page.list.rect[1] + 20))
        self.assertIsInstance(self.app.page, appmod.NetworkPage)
        self.app.render()
        self.app.touch(("down", 20, 20))
        self.app.touch(("up", 20, 20))
        self.assertIsInstance(self.app.page, appmod.WifiPage)


class BatteryStatus(unittest.TestCase):
    def setUp(self):
        import json
        from unittest import mock
        from i14ui import charging
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.d=pathlib.Path(self.tmp.name)
        for key,value in {"capacity":"100","status":"Full","batt_vol":"4050","current":"-6000","boot_id":"boot-a"}.items():
            (self.d/key).write_text(value)
        self.status=dict(schema=1,backend="bcct",boot_id="boot-a",updated_monotonic=1000.0,
                         level=80,zcv_mv=4109,paused=True,valid=True,enabled=True,limit=80,error=None,online=True)
        self.file=self.d/"charge.json"
        patches=[mock.patch.object(appmod,"BAT",str(self.d)),mock.patch.object(appmod,"FG_CURRENT",str(self.d/"current")),
                 mock.patch.object(appmod,"CHARGE_STATUS",str(self.file),create=True),
                 mock.patch.object(charging,"BOOT_ID",str(self.d/"boot_id")),
                 mock.patch.object(charging.time,"monotonic",return_value=1000.0)]
        for patch in patches:patch.start();self.addCleanup(patch.stop)
        self.write()

    def write(self):
        import json
        self.file.write_text(json.dumps(self.status))

    def test_fresh_guard_estimate_and_pause_override_driver100_full(self):
        cap,st,word,current,volt=appmod.battery()
        self.assertEqual((cap,st,word),("~80","Cmd discharging","laden gepauzeerd"))
        self.assertEqual(appmod.battery_level(),"~80")
        self.assertEqual((current,volt),("+600 mA","4.05 V"))

    def test_release_means_allowed_without_claiming_full(self):
        self.status.update(paused=False,level=72,zcv_mv=4030)
        self.write()
        self.assertEqual(appmod.battery()[:3],("~72","Charging","laden toegestaan"))

    def test_unplugged_and_disabled_limit_keep_estimate(self):
        self.status.update(paused=False,enabled=False,valid=False,online=False,level=65,zcv_mv=3962)
        self.write()
        self.assertEqual(appmod.battery()[:3],("~65","Discharging","ontladen"))

    def test_missing_stale_bad_and_wrong_boot_status_never_display100(self):
        self.file.unlink()
        self.assertEqual(appmod.battery()[0],"?")
        for change in ({"updated_monotonic":849.0},{"updated_monotonic":1001.0},{"updated_monotonic":float("nan")},
                       {"boot_id":"other-boot"},{"level":True},{"level":101},{"paused":"yes"},{"backend":"legacy"}):
            original=dict(self.status);self.status.update(change);self.write()
            self.assertEqual(appmod.battery()[0],"?",change)
            self.assertEqual(appmod.battery_level(),"?",change)
            self.status=original
        self.status.pop("error")
        self.write()
        self.assertEqual(appmod.battery()[0],"?")
        self.file.write_text("{broken")
        self.assertEqual(appmod.battery()[0],"?")

    def test_backend_error_is_visible(self):
        self.status.update(error="BCCT readback failed",valid=False,paused=False)
        self.write()
        self.assertEqual(appmod.battery()[1:3],("Not charging","regelingfout"))


if __name__ == "__main__":
    unittest.main()
