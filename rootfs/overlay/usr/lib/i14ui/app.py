"""The i14-ui app: pages, the main loop, the power button.

Power button: short press = screen on/off; hold 3 s = power off (as i14-status did).
Touch and keyboards only act while the screen is on (and unlocked, with a PIN).
"""
import os
import select
import signal
import subprocess
import sys
import time

from . import bt as btmod
from . import config as cfgmod
from . import fb
from . import inputdev
from . import term
from . import widgets as w
from . import wifi as wifimod
from .font import Font
from .charging import read_status, STATUS as CHARGE_STATUS

FONTS = "/usr/share/consolefonts/"
BAT = "/sys/class/power_supply/battery"
FG_CURRENT = "/sys/devices/platform/battery_meter/FG_Current"  # 0.1 mA, negative = into the battery
DATA = "/mnt/data/media/0/i14-linux"
HOLD_SECONDS = 3.0
BAR_H = 80
TERMINAL_MIN_TIMEOUT = 300
DAYS = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def read(path, default="?"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def addresses():
    out = {}
    try:
        text = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[1] != "lo":
            out[parts[1]] = parts[3].split("/")[0]
    return out


def level_text(snapshot):
    return f"~{snapshot['level']}" if snapshot and snapshot["level"] is not None else "?"


def battery_level():
    return level_text(read_status(CHARGE_STATUS))


def battery():
    snapshot = read_status(CHARGE_STATUS)
    cap = level_text(snapshot)
    if not snapshot or snapshot["level"] is None:
        st, word = "Not charging", "accumeting onbekend"
    elif snapshot["error"]:
        st, word = "Not charging", "regelingfout"
    elif snapshot["paused"]:
        st, word = "Cmd discharging", "laden gepauzeerd"
    elif snapshot.get("online") is True:
        st, word = "Charging", "laden toegestaan"
    elif snapshot.get("online") is False:
        st, word = "Discharging", "ontladen"
    else:
        st, word = "Not charging", "laadstatus onbekend"
    try:
        current = f"{-int(read(FG_CURRENT, '0')) // 10:+d} mA"
    except ValueError:
        current = "? mA"
    try:
        volt = f"{int(read(BAT + '/batt_vol', '0')) / 1000:.2f} V"
    except ValueError:
        volt = "? V"
    return cap, st, word, current, volt

def free(path):
    try:
        s = os.statvfs(path)
    except OSError:
        return "?"
    return f"{s.f_bavail * s.f_frsize / 2**30:.1f} van {s.f_blocks * s.f_frsize / 2**30:.1f} GB vrij"


# --- pages ---------------------------------------------------------------------------------------
class Page:
    title = ""
    keyboard = None  # a widgets.Keyboard when the page types

    def __init__(self, app):
        self.app = app
        self.buttons = []

    def show(self):
        pass

    def hide(self):
        pass

    def draw(self, c):
        for b in self.buttons:
            b.draw(c, self.app.mid)

    def touch(self, ev):
        if self.keyboard and self.keyboard.touch(ev):
            return
        for b in self.buttons:
            if b.touch(ev) and ev[0] == "up":
                return

    def key(self, k):
        pass

    def tick(self):
        return False


class Home(Page):
    title = "I14"

    def __init__(self, app):
        super().__init__(app)
        a = app
        grid = [("Wi-Fi", lambda: a.push(WifiPage)),
                ("Bluetooth", lambda: a.push(BluetoothPage)), ("Instellingen", lambda: a.push(SettingsPage)),
                ("Aan/uit", lambda: a.push(PowerPage))]
        if a.terminal_enabled:
            grid.insert(0, ("Terminal", lambda: a.push(TerminalPage)))
        for i, (label, action) in enumerate(grid):
            col, row = i % 2, i // 2
            self.buttons.append(w.Button((30 + col * 340, 900 + row * 200, 320, 170), label, action))
        self.cache = None
        self.cache_at = 0

    def draw(self, c):
        now = time.monotonic()
        if self.cache is None or now - self.cache_at > 5:
            self.cache = (battery(), addresses(), self.app.wifi_ssid())
            self.cache_at = now
        (cap, st, word, current, volt), ip, ssid = self.cache
        big, mid, small = self.app.big, self.app.mid, self.app.small
        c.text(40, BAR_H + 30, time.strftime("%H:%M"), big, w.FG, w.BG, scale=4)
        day = DAYS[time.localtime().tm_wday]
        c.text(40, BAR_H + 170, f"{day} {time.strftime('%d-%m-%Y')}", mid, w.DIM, w.BG)
        color = w.GREEN if st in ("Charging", "Full") else (w.RED if cap.lstrip("~").isdigit() and int(cap.lstrip("~")) <= 15 else w.FG)
        c.text(40, BAR_H + 250, f"Accu {cap}%", big, color, w.BG, scale=2)
        c.text(40, BAR_H + 330, f"{word}, {current}, {volt}", mid, color, w.BG)
        c.text(40, BAR_H + 420, "Wi-Fi", mid, w.ACCENT, w.BG)
        c.text(40, BAR_H + 455, ssid or ("uit" if not self.app.wifi.enabled else "niet verbonden"), mid, w.FG, w.BG)
        c.text(40, BAR_H + 490, ip.get("wlan0", ""), mid, w.FG, w.BG)
        c.text(40, BAR_H + 560, "USB", mid, w.ACCENT, w.BG)
        c.text(40, BAR_H + 595, ip.get("rndis0", "uit"), mid, w.FG, w.BG)
        up = int(float(read("/proc/uptime", "0").split()[0]))
        c.text(40, BAR_H + 660, f"aan sinds {up // 3600} u {up % 3600 // 60:02d} m", small, w.DIM, w.BG)
        c.text(40, BAR_H + 690, "knop kort: scherm uit, 3 s: uitzetten", small, w.DIM, w.BG)
        super().draw(c)


class TerminalPage(Page):
    title = "Terminal"

    def __init__(self, app):
        super().__init__(app)
        self.keyboard = w.Keyboard(app.width, app.height, self.key, terminal_row=True)
        self.kb_on = True
        self.back = 0
        self.drag = None
        self.toggle = w.Button((app.width - 250, 8, 150, BAR_H - 16), lambda: "verberg" if self.kb_on else "toetsen",
                               self.toggle_keyboard)

    def area(self):
        bottom = self.keyboard.top if self.kb_on else self.app.height
        return BAR_H, bottom

    def size(self):
        top, bottom = self.area()
        f = self.app.term_font
        return (self.app.width - 8) // f.w, (bottom - top - 4) // f.h

    def show(self):
        cols, rows = self.size()
        if self.app.session is None or not self.app.session.alive:
            self.app.session = term.Session(cols, rows)
        else:
            self.app.session.set_size(cols, rows)

    def toggle_keyboard(self):
        self.kb_on = not self.kb_on
        if self.app.session:
            self.app.session.set_size(*self.size())
        self.app.dirty = True

    def key(self, k):
        s = self.app.session
        if s is None or not s.alive:
            self.show()
            return
        name, ctrl, alt = k
        self.back = 0
        s.write(s.vt.key_bytes(name, ctrl, alt))
        self.app.dirty = True

    def touch(self, ev):
        if self.toggle.touch(ev):
            return
        top, bottom = self.area()
        kind, x, y = ev
        if self.kb_on and self.keyboard.touch(ev):
            return
        if kind == "down" and top <= y < bottom:
            self.drag = (y, self.back, False)
        elif kind == "move" and self.drag:
            y0, b0, _ = self.drag
            lines = (y - y0) // self.app.term_font.h
            if abs(y - y0) > w.TAP_SLOP:
                s = self.app.session
                limit = len(s.vt.scrollback) if s else 0
                self.back = max(0, min(limit, b0 + lines))
                self.drag = (y0, b0, True)
                self.app.dirty = True
        elif kind == "up" and self.drag:
            moved = self.drag[2]
            self.drag = None
            s = self.app.session
            if not moved:
                if s is None or not s.alive:
                    self.show()
                elif not self.kb_on:
                    self.toggle_keyboard()

    def tick(self):
        return self.keyboard.tick() if self.kb_on else False

    def draw(self, c):
        top, bottom = self.area()
        f = self.app.term_font
        c.fill(0, top, self.app.width, bottom - top, (0, 0, 0))
        s = self.app.session
        if s is not None:
            vt = s.vt
            lines = vt.view(self.back)
            for r, line in enumerate(lines):
                self.app.draw_term_line(c, 4, top + 2 + r * f.h, line)
            if self.back == 0 and vt.cursor_visible and s.alive and vt.y < len(lines):
                cx, cy = 4 + vt.x * f.w, top + 2 + vt.y * f.h
                ch, attr = lines[vt.y][vt.x]
                c.text(cx, cy, ch, f, (0, 0, 0), (220, 220, 220))
            if self.back:
                c.text(self.app.width - 200, top + 4, f"-{self.back} regels", self.app.small, w.YELLOW, (0, 0, 0))
            if not s.alive:
                c.text(10, bottom - 40, "shell gestopt - tik om opnieuw te starten", self.app.small, w.YELLOW,
                       (0, 0, 0))
        if not self.app.cfg.has_pin:
            c.text(130, BAR_H - 24, "root, geen pincode", self.app.small, w.YELLOW, w.PANEL)
        self.toggle.draw(c, self.app.small)
        if self.kb_on:
            self.keyboard.draw(c, self.app.mid)


class WifiPage(Page):
    title = "Wi-Fi"

    def __init__(self, app):
        super().__init__(app)
        wf = app.wifi
        self.buttons = [
            w.Button((20, BAR_H + 110, 220, 90), lambda: "Wi-Fi: aan" if wf.enabled else "Wi-Fi: uit",
                     self.toggle),
            w.Button((250, BAR_H + 110, 220, 90), lambda: "zoeken..." if wf.scanning else "Zoeken", self.scan),
            w.Button((480, BAR_H + 110, 220, 90), "Verborgen", lambda: app.push(HiddenPage)),
        ]
        self.list = w.ListView((0, BAR_H + 220, app.width, app.height - BAR_H - 220), self.tap)
        self.rows = []

    def show(self):
        self.refresh()
        if self.app.wifi.enabled and self.app.wifi.available():
            self.safe(self.app.wifi.scan)

    def safe(self, fn, *args):
        try:
            fn(*args)
        except OSError as e:
            self.app.wifi.message = f"wpa_supplicant: {e}"
        self.refresh()

    def toggle(self):
        self.safe(self.app.wifi.set_enabled, not self.app.wifi.enabled)

    def scan(self):
        self.safe(self.app.wifi.scan)

    def refresh(self):
        wf = self.app.wifi
        st = wf.status()
        current = st.get("ssid_bytes") if st.get("wpa_state") == "COMPLETED" else None
        saved = {n["ssid"]: n for n in wf.saved()}
        rows, seen = [], set()
        for r in wf.scan_results:
            seen.add(r["ssid"])
            tags = []
            if r["ssid"] == current:
                tags.append("verbonden")
            elif r["ssid"] in saved:
                tags.append("opgeslagen")
            tags.append({"open": "open", "psk": "WPA2", "sae": "WPA3", "wep": "WEP", "eap": "802.1X"}[r["security"]])
            tags.append("signaal " + "|" * wifimod.bars(r["signal"]) + "." * (4 - wifimod.bars(r["signal"])))
            rows.append((r["ssid"], r["security"], wifimod.show_ssid(r["ssid"]), ", ".join(tags)))
        for ssid, n in saved.items():
            if ssid not in seen:
                tag = "verbonden" if ssid == current else "opgeslagen, niet in de buurt"
                rows.append((ssid, None, wifimod.show_ssid(ssid), tag))
        self.rows = rows
        self.list.set_items([(name, sub, w.GREEN if "verbonden" in sub.split(",")[0] else w.FG)
                             for _, _, name, sub in rows])
        wf.changed = False
        self.app.dirty = True

    def tap(self, i):
        ssid, sec, _, _ = self.rows[i]
        self.app.push(NetworkPage, ssid=ssid, sec=sec)

    def tick(self):
        if self.app.wifi.changed:
            self.refresh()
            return True
        return False

    def draw(self, c):
        wf = self.app.wifi
        st = wf.status()
        if st.get("wpa_state") == "GEEN_WPA":
            line = "wpa_supplicant draait niet"
        elif st.get("wpa_state") == "COMPLETED":
            line = f"verbonden: {wifimod.show_ssid(st.get('ssid_bytes', b''))}  {st.get('ip_address', '')}"
        else:
            line = "niet verbonden" if wf.enabled else "Wi-Fi staat uit"
        c.text(20, BAR_H + 15, line, self.app.mid, w.FG, w.BG, maxw=self.app.width - 40)
        c.text(20, BAR_H + 60, wf.message, self.app.small, w.YELLOW, w.BG, maxw=self.app.width - 40)
        super().draw(c)
        self.list.draw(c, self.app.mid, self.app.small)

    def touch(self, ev):
        for b in self.buttons:
            if b.touch(ev):
                return
        self.list.touch(ev)


class NetworkPage(Page):
    """One network: connect, type a password, forget, make preferred."""

    def __init__(self, app, ssid, sec):
        super().__init__(app)
        self.ssid, self.sec = ssid, sec
        self.title = wifimod.show_ssid(ssid)
        self.saved = app.wifi.network_id(ssid) is not None
        self.field = None
        self.mode = "menu"
        self._menu()

    def _menu(self):
        a, y = self.app, BAR_H + 160
        self.buttons, self.keyboard, self.mode = [], None, "menu"
        if self.saved:
            self.buttons.append(w.Button((40, y, 640, 110), "Verbinden", self.connect_saved))
            self.buttons.append(w.Button((40, y + 130, 640, 110), "Voorrang geven", self.prefer))
            if self.sec in (None, "psk"):
                self.buttons.append(w.Button((40, y + 260, 640, 110), "Ander wachtwoord", self.ask_password))
            self.buttons.append(w.Button((40, y + 390, 640, 110), "Vergeten", self.forget, fg=w.RED))
        elif self.sec == "open":
            self.buttons.append(w.Button((40, y, 640, 110), "Verbinden (open netwerk)", self.connect_open))
        elif self.sec == "psk":
            self.ask_password()
        a.dirty = True

    def ask_password(self):
        a = self.app
        self.mode = "password"
        self.field = w.TextField((40, BAR_H + 150, 640, 120), "Wachtwoord", secret=True)
        self.keyboard = w.Keyboard(a.width, a.height, self.key)
        top = self.keyboard.top
        self.buttons = [w.Button((40, top - 130, 300, 100), lambda: "verberg" if self.field.show else "toon",
                                 self.toggle_show),
                        w.Button((380, top - 130, 300, 100), "Verbinden", self.connect_password, color=w.PANEL_HI)]
        a.dirty = True

    def toggle_show(self):
        self.field.show = not self.field.show

    def key(self, k):
        if self.field is None:
            return
        if k[0] == "enter":
            self.connect_password()
        else:
            self.field.key(k)
        self.app.dirty = True

    def _do(self, fn, *args, **kw):
        try:
            ok = fn(*args, **kw)
        except OSError as e:
            self.app.wifi.message = f"wpa_supplicant: {e}"
            ok = False
        return ok

    def connect_saved(self):
        if self._do(self.app.wifi.connect, self.ssid, self.sec or "psk"):
            self.app.pop()

    def connect_open(self):
        if self._do(self.app.wifi.connect, self.ssid, "open"):
            self.app.pop()

    def connect_password(self):
        if self._do(self.app.wifi.connect, self.ssid, "psk", passphrase=self.field.value):
            self.app.pop()

    def prefer(self):
        self._do(self.app.wifi.prefer, self.ssid)

    def forget(self):
        if self._do(self.app.wifi.forget, self.ssid):
            self.app.pop()

    def draw(self, c):
        a = self.app
        sec = {"open": "open netwerk", "psk": "WPA2 (wachtwoord)", "sae": "WPA3: niet ondersteund",
               "wep": "WEP: niet ondersteund", "eap": "802.1X: niet ondersteund", None: "niet in de buurt"}
        c.text(40, BAR_H + 20, self.title, a.mid, w.FG, w.BG, maxw=a.width - 80)
        c.text(40, BAR_H + 60, sec.get(self.sec, "") + (", opgeslagen" if self.saved else ""), a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 95, a.wifi.message, a.small, w.YELLOW, w.BG, maxw=a.width - 80)
        if not self.saved and self.mode == "menu":
            c.text(40, BAR_H + 700, "Let op: op elk netwerk zijn SSH (alleen met sleutel)", a.small, w.DIM, w.BG)
            c.text(40, BAR_H + 730, "en de DNS-server bereikbaar voor andere apparaten.", a.small, w.DIM, w.BG)
        if self.mode == "password":
            c.text(40, BAR_H + 290, "Let op: SSH (sleutel) en DNS zijn bereikbaar op dit netwerk.", a.small, w.DIM,
                   w.BG, maxw=a.width - 80)
            self.field.draw(c, a.mid, a.small)
            self.keyboard.draw(c, a.mid)
        super().draw(c)

    def tick(self):
        return self.keyboard.tick() if self.keyboard else False


class HiddenPage(Page):
    title = "Verborgen netwerk"

    def __init__(self, app):
        super().__init__(app)
        self.name = w.TextField((40, BAR_H + 40, 640, 110), "Netwerknaam (SSID)", maxlen=32)
        self.pw = w.TextField((40, BAR_H + 180, 640, 110), "Wachtwoord (leeg = open netwerk)", secret=True)
        self.focus = self.name
        self.keyboard = w.Keyboard(app.width, app.height, self.key)
        self.buttons = [w.Button((40, self.keyboard.top - 120, 640, 100), "Verbinden", self.connect,
                                 color=w.PANEL_HI)]

    def key(self, k):
        if k[0] == "enter":
            if self.focus is self.name:
                self.focus = self.pw
            else:
                self.connect()
        else:
            self.focus.key(k)
        self.app.dirty = True

    def connect(self):
        ssid = self.name.value.encode()
        if not ssid:
            self.app.wifi.message = "vul een netwerknaam in"
            return
        sec = "psk" if self.pw.value else "open"
        try:
            ok = self.app.wifi.connect(ssid, sec, passphrase=self.pw.value or None, hidden=True)
        except OSError as e:
            self.app.wifi.message, ok = f"wpa_supplicant: {e}", False
        if ok:
            self.app.pop()

    def touch(self, ev):
        if ev[0] == "down":
            for f in (self.name, self.pw):
                if w.inside(f.rect, ev[1], ev[2]):
                    self.focus = f
                    self.app.dirty = True
                    return
        super().touch(ev)

    def draw(self, c):
        a = self.app
        for f in (self.name, self.pw):
            f.draw(c, a.mid, a.small)
            if f is self.focus:
                x, y, fw, fh = f.rect
                c.fill(x, y + fh, fw, 4, w.ACCENT)
        c.text(40, BAR_H + 320, a.wifi.message, a.small, w.YELLOW, w.BG, maxw=a.width - 80)
        self.keyboard.draw(c, a.mid)
        super().draw(c)

    def tick(self):
        return self.keyboard.tick()


class BluetoothPage(Page):
    title = "Bluetooth"

    def __init__(self, app):
        super().__init__(app)
        b = app.bt
        self.buttons = [
            w.Button((20, BAR_H + 150, 330, 90), lambda: "Bluetooth: aan" if b.on else "Bluetooth: uit", self.toggle),
            w.Button((370, BAR_H + 150, 330, 90), lambda: "zoeken..." if b.phase else "Zoeken", self.search),
        ]
        self.list = w.ListView((0, BAR_H + 260, app.width, app.height - BAR_H - 260), lambda i: None)

    def toggle(self):
        self.app.bt.power(not self.app.bt.on)

    def search(self):
        if not self.app.bt.phase:
            self.app.bt.search()

    def tick(self):
        b = self.app.bt
        if b.changed:
            b.changed = False
            self.list.set_items([(name or addr, f"{addr}  {kind}" + (f"  {rssi} dBm" if rssi is not None else ""),
                                  w.FG) for addr, name, rssi, kind in b.listing()])
            return True
        return False

    def draw(self, c):
        a, b = self.app, self.app.bt
        c.text(20, BAR_H + 15, ("aan, adres " + b.address) if b.on and b.address else ("aan" if b.on else "uit"),
               a.mid, w.FG, w.BG)
        c.text(20, BAR_H + 55, b.message, a.small, w.YELLOW, w.BG, maxw=a.width - 40)
        c.text(20, BAR_H + 90, "Alleen zoeken; koppelen wordt nog niet ondersteund.", a.small, w.DIM, w.BG)
        super().draw(c)
        self.list.draw(c, a.mid, a.small)

    def touch(self, ev):
        for btn in self.buttons:
            if btn.touch(ev):
                return
        self.list.touch(ev)


class SettingsPage(Page):
    title = "Instellingen"

    def __init__(self, app):
        super().__init__(app)
        a = app
        self.buttons = [
            w.Button((40, BAR_H + 90, 150, 100), "-", lambda: self.brightness(-25), scale=2),
            w.Button((530, BAR_H + 90, 150, 100), "+", lambda: self.brightness(25), scale=2),
            w.Button((40, BAR_H + 300, 640, 100), self.timeout_label, self.next_timeout),
            w.Button((40, BAR_H + 470, 640, 100), lambda: "Pincode wijzigen" if a.cfg.has_pin else "Pincode instellen",
                     lambda: a.push(PinSetPage)),
            w.Button((40, BAR_H + 590, 640, 100), "Pincode verwijderen", self.remove_pin),
            w.Button((40, BAR_H + 1040, 640, 100),
                     lambda: "Laadgrens: " + ("aan" if a.cfg["charge_limit_enabled"] else "uit"), self.toggle_charge),
            w.Button((40, BAR_H + 1160, 640, 100),
                     lambda: f"Stoppen bij: ~{a.cfg['charge_limit']}%", self.next_charge_limit),
        ]
        self.message = ""

    def save_charge(self, key, value):
        self.message = "" if self.app.cfg.set(key, value) else "Opslaan mislukt; instelling niet gewijzigd."

    def toggle_charge(self):
        self.save_charge("charge_limit_enabled", not self.app.cfg["charge_limit_enabled"])

    def next_charge_limit(self):
        limits = cfgmod.CHARGE_LIMITS
        self.save_charge("charge_limit", limits[(limits.index(self.app.cfg["charge_limit"]) + 1) % len(limits)])

    def brightness(self, delta):
        value = max(5, min(255, self.app.cfg["brightness"] + delta))
        self.app.cfg.set("brightness", value)
        self.app.screen.set_brightness(value)

    def timeout_label(self):
        t = self.app.cfg["timeout"]
        text = "nooit" if t == 0 else (f"{t} s" if t < 60 else f"{t // 60} min")
        return f"Scherm uit na: {text}"

    def next_timeout(self):
        ts = cfgmod.TIMEOUTS
        self.app.cfg.set("timeout", ts[(ts.index(self.app.cfg["timeout"]) + 1) % len(ts)])

    def remove_pin(self):
        if self.app.cfg.has_pin and not self.app.cfg.damaged:
            self.app.push(LockPage, then=self._remove_pin, reason="huidige pincode")

    def _remove_pin(self):
        if not self.app.cfg.set_pin(None):
            self.message = "Opslaan mislukt; pincode blijft."

    def draw(self, c):
        a = self.app
        c.text(40, BAR_H + 30, "Helderheid", a.small, w.DIM, w.BG)
        value = a.cfg["brightness"]
        c.fill(210, BAR_H + 125, 300, 30, w.PANEL)
        c.fill(210, BAR_H + 125, 300 * value // 255, 30, w.ACCENT)
        c.text(40, BAR_H + 250, "Scherm", a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 430, "Vergrendeling: " + ("pincode aan" if a.cfg.has_pin else "geen pincode"),
               a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 740, "Opslag", a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 780, "Linux: " + free("/"), a.mid, w.FG, w.BG)
        c.text(40, BAR_H + 820, "userdata: " + free("/mnt/data"), a.mid, w.FG, w.BG)
        c.text(40, BAR_H + 900, "Zonder pincode kan iedereen met de telefoon", a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 930, "de terminal (root) gebruiken.", a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 1000, "Accu (regeling iedere minuut)", a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 1290, f"Schatting op rustspanning; band ~{a.cfg['charge_limit'] - 10}-{a.cfg['charge_limit']}%.", a.small, w.DIM, w.BG)
        c.text(40, BAR_H + 1330, self.message, a.small, w.YELLOW, w.BG, maxw=a.width - 80)
        super().draw(c)


class PinPad(Page):
    """Digits 0-9, delete and OK."""

    def __init__(self, app):
        super().__init__(app)
        self.digits = ""
        labels = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "<x", "0", "OK"]
        for i, label in enumerate(labels):
            col, row = i % 3, i // 3
            self.buttons.append(w.Button((60 + col * 210, 640 + row * 200, 180, 170), label,
                                         lambda l=label: self.press(l), scale=2))

    def press(self, label):
        if label == "<x":
            self.digits = self.digits[:-1]
        elif label == "OK":
            self.done(self.digits)
        elif len(self.digits) < 8:
            self.digits += label
        self.app.dirty = True

    def key(self, k):
        if k[0].isdigit():
            self.press(k[0])
        elif k[0] == "backspace":
            self.press("<x")
        elif k[0] == "enter":
            self.press("OK")

    def draw(self, c, prompt="", note=""):
        a = self.app
        c.text(60, BAR_H + 120, prompt, a.mid, w.FG, w.BG)
        c.text(60, BAR_H + 220, "* " * len(self.digits), a.big, w.FG, w.BG, scale=2)
        c.text(60, BAR_H + 360, note, a.small, w.YELLOW, w.BG, maxw=a.width - 120)
        super().draw(c)


class LockPage(PinPad):
    title = "Vergrendeld"
    lock = True

    def __init__(self, app, then=None, reason="pincode"):
        super().__init__(app)
        self.then, self.reason = then, reason
        self.fails, self.wait_until, self.note = 0, 0.0, ""

    def done(self, pin):
        now = time.monotonic()
        if now < self.wait_until:
            self.note = f"wacht nog {int(self.wait_until - now) + 1} s"
            return
        if self.app.cfg.check_pin(pin):
            if self.then:
                self.then()
            self.app.pop()
            return
        self.digits = ""
        self.fails += 1
        if self.fails >= 5:
            self.wait_until = now + 30 * 2 ** min(self.fails - 5, 5)
            self.note = f"{self.fails} keer fout: wacht {int(self.wait_until - now)} s"
        else:
            self.note = "fout"

    def draw(self, c):
        note = self.note
        if self.app.cfg.damaged:
            note = "ui.json beschadigd: herstel via SSH/USB"
        super().draw(c, f"Voer de {self.reason} in", note)


class PinSetPage(PinPad):
    title = "Pincode"

    def __init__(self, app):
        super().__init__(app)
        self.first, self.note = None, ""
        self.verified = not app.cfg.has_pin  # changing it: the current PIN first
        self.asked = False

    def show(self):
        if self.verified:
            return
        if self.asked:  # came back from the PIN check without passing it
            self.app.pop()
            return
        self.asked = True
        self.app.push(LockPage, then=lambda: setattr(self, "verified", True), reason="huidige pincode")

    def done(self, pin):
        if not 4 <= len(pin) <= 8:
            self.note, self.digits = "4 tot 8 cijfers", ""
        elif self.first is None:
            self.first, self.digits, self.note = pin, "", ""
        elif pin != self.first:
            self.first, self.digits, self.note = None, "", "niet gelijk, opnieuw"
        elif self.app.cfg.set_pin(pin):
            self.app.pop()
        else:
            self.first, self.digits, self.note = None, "", "opslaan mislukt; pincode niet gewijzigd"

    def draw(self, c):
        super().draw(c, "Nieuwe pincode" if self.first is None else "Nogmaals ter controle", self.note)


class PowerPage(Page):
    title = "Aan/uit"
    ACTIONS = {"reboot": "Herstarten", "poweroff": "Uitschakelen", "rescue": "Noodmodus (USB-shell)"}

    def __init__(self, app):
        super().__init__(app)
        self.confirm = None
        for i, key in enumerate(self.ACTIONS):
            self.buttons.append(w.Button((40, BAR_H + 120 + i * 170, 640, 140), lambda k=key: self.label(k),
                                         lambda k=key: self.press(k)))
        self.buttons.append(w.Button((40, BAR_H + 120 + 3 * 170, 640, 140), "Annuleren", app.pop))

    def show(self):
        self.confirm = None  # a lock or another page in between: ask again

    def label(self, key):
        return ("Zeker? Tik nogmaals: " if self.confirm == key else "") + self.ACTIONS[key]

    def press(self, key):
        if self.confirm != key:
            self.confirm = key
            return
        self.app.power_action(key)

    def draw(self, c):
        c.text(40, BAR_H + 40, "Noodmodus: shell via de USB-kabel; aan/uit-knop start Linux weer.", self.app.small,
               w.DIM, w.BG, maxw=self.app.width - 80)
        super().draw(c)


# --- the app -------------------------------------------------------------------------------------
TERM_COLORS = [(0, 0, 0), (205, 49, 49), (13, 188, 121), (229, 229, 16), (36, 114, 200), (188, 63, 188),
               (17, 168, 205), (229, 229, 229), (102, 102, 102), (241, 76, 76), (35, 209, 139), (245, 245, 67),
               (59, 142, 234), (214, 112, 214), (41, 184, 219), (255, 255, 255)]


def palette(index):
    if isinstance(index, tuple):
        return index
    index = max(0, min(255, index))  # a backstop: a colour byte above 255 would crash the drawing
    if index < 16:
        return TERM_COLORS[index]
    if index < 232:
        i = index - 16
        return tuple((0, 95, 135, 175, 215, 255)[v] for v in (i // 36, i // 6 % 6, i % 6))
    g = 8 + (index - 232) * 10
    return (g, g, g)


class App:
    def __init__(self, screen, fonts, cfg, wifi, bt, *, terminal_enabled=False):
        self.screen = screen
        self.canvas = screen.canvas
        self.width, self.height = self.canvas.w, self.canvas.h
        self.big, self.mid, self.small, self.term_font = fonts
        self.cfg, self.wifi, self.bt = cfg, wifi, bt
        self.terminal_enabled = terminal_enabled
        self.session = None
        self.stack = []
        self.dirty = True
        self.last_activity = time.monotonic()
        self.line_cache = {}
        self.back_btn = w.Button((0, 0, 110, BAR_H), "<", self.pop, color=w.PANEL, scale=2)
        screen.set_brightness(cfg["brightness"])
        self.push(Home)

    # pages
    @property
    def page(self):
        return self.stack[-1]

    def push(self, cls, **kw):
        if cls is TerminalPage and not self.terminal_enabled:
            return self.page
        if self.stack:
            self.stack[-1].hide()
        page = cls(self, **kw)
        self.stack.append(page)
        page.show()
        self.dirty = True
        return page

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop().hide()
            self.page.show()
        self.dirty = True

    def locked(self):
        return isinstance(self.page, LockPage) and self.page.then is None

    def lock(self):
        if self.cfg.has_pin and not self.locked():
            self.push(LockPage)

    # status
    def wifi_ssid(self):
        st = self.wifi.status()
        return wifimod.show_ssid(st["ssid_bytes"]) if st.get("wpa_state") == "COMPLETED" and "ssid_bytes" in st else ""

    # drawing
    def draw_term_line(self, c, x, y, line):
        key = (tuple(line), x)
        rows = self.line_cache.get(key)
        f = self.term_font
        if rows is None:
            glyphs = []
            for ch, (fg, bg, flags) in line:
                fgc = palette(fg) if fg is not None else (220, 220, 220)
                bgc = palette(bg) if bg is not None else (0, 0, 0)
                if flags & 1 and isinstance(fg, int) and fg < 8:  # bold: the bright colour, before reverse
                    fgc = palette(fg + 8)
                if flags & 4:
                    fgc, bgc = bgc, fgc
                glyphs.append(f.rows(ch, 1, fgc, bgc))
            rows = [b"".join(g[r] for g in glyphs) for r in range(f.h)]
            if len(self.line_cache) > 400:
                self.line_cache.clear()
            self.line_cache[key] = rows
        stride = c.stride
        for r, row in enumerate(rows):
            if 0 <= y + r < c.h:
                s = (y + r) * stride + x * 4
                n = min(len(row), max(0, (c.w - x) * 4))  # never resize the buffer (its size is the mmap's)
                c.buf[s:s + n] = row[:n]

    def render(self):
        c = self.canvas
        c.clear(w.BG)
        page = self.page
        c.fill(0, 0, self.width, BAR_H, w.PANEL)
        x = 20
        if len(self.stack) > 1 and not self.locked():
            self.back_btn.draw(c, self.mid)
            x = 130
        c.text(x, (BAR_H - self.mid.h) // 2, page.title, self.mid, w.FG, w.PANEL, maxw=self.width - x - 280)
        cap = battery_level()
        c.text(self.width - 210 if not isinstance(page, TerminalPage) else self.width - 90,
               (BAR_H - self.small.h) // 2, f"{time.strftime('%H:%M')} {cap}%"
               if not isinstance(page, TerminalPage) else f"{cap}%", self.small, w.DIM, w.PANEL)
        page.draw(c)
        self.screen.show()
        self.dirty = False

    # input
    def touch(self, ev):
        self.last_activity = time.monotonic()
        if len(self.stack) > 1 and not self.locked() and (ev[0] != "down" or ev[2] < BAR_H) and \
                self.back_btn.touch(ev):
            self.dirty = True
            return
        self.page.touch(ev)
        self.dirty = True

    def key(self, k):
        self.last_activity = time.monotonic()
        self.page.key(k)
        self.dirty = True

    def cancel_input(self):
        """Screen off: the release of a held finger never reaches the page, so drop what it held."""
        for page in self.stack:
            kb = page.keyboard
            if kb:
                kb.active, kb.repeat_at, kb.repeated = None, None, False
            for b in page.buttons:
                b.pressed = False
            lst = getattr(page, "list", None)
            if lst:
                lst.start = lst.pressed = None
            if hasattr(page, "drag"):
                page.drag = None
        self.back_btn.pressed = False

    def timeout(self):
        t = self.cfg["timeout"]
        if t and isinstance(self.page, TerminalPage):
            t = max(t, TERMINAL_MIN_TIMEOUT)
        return t

    def power_action(self, key):
        self.screen.canvas.clear(w.BG)
        self.canvas.text(40, 400, {"reboot": "Herstarten...", "poweroff": "Uitschakelen...",
                                   "rescue": "Naar de noodmodus..."}[key], self.big, w.RED, w.BG)
        self.screen.show()
        if key == "rescue":
            try:
                with open(os.path.join(DATA, "rescue-once"), "w") as f:
                    f.write("i14-ui\n")
            except OSError as e:
                self.page.title = f"mislukt: {e}"
                return
        subprocess.run(["poweroff" if key == "poweroff" else "reboot"], check=False)


class Runner:
    """The main loop around App: devices, select, screen on/off and the power button."""

    def __init__(self, app, power_fds, touch_fds, kbd_fds):
        self.app = app
        self.power_fds, self.touch_fds, self.kbd_fds = power_fds, touch_fds, kbd_fds
        self.touch = {fd: inputdev.Touch() for fd in touch_fds}
        self.kbds = {fd: inputdev.Keyboard() for fd in kbd_fds}
        self.pressed = None
        self.dropped = {fd: False for fd in power_fds}
        self.next_scan = 0.0
        self.paths = {}
        self.last_render = 0.0
        self.minute = None

    def screen_on(self, on):
        a = self.app
        if on and not a.screen.on:
            a.lock()
            a.last_activity = time.monotonic()
            a.dirty = True
            a.render()
            a.screen.power(True)
        elif not on and a.screen.on:
            a.screen.power(False)
            a.cancel_input()
            for t in self.touch.values():  # the touch chip sleeps now: a held finger may never lift
                t.cancel()

    def shutdown(self):
        self.screen_on(True)
        self.app.power_action("poweroff")

    def power_events(self, fd, data):
        for stamp, etype, code, value in inputdev.events(data):
            if etype == inputdev.EV_SYN and code == inputdev.SYN_DROPPED:
                self.dropped[fd], self.pressed = True, None
                continue
            if self.dropped[fd]:
                if etype == inputdev.EV_SYN and code == inputdev.SYN_REPORT:
                    self.dropped[fd] = False
                    self.pressed = time.monotonic() if inputdev.key_down(fd, inputdev.KEY_POWER) else None
                continue
            if etype != inputdev.EV_KEY or code != inputdev.KEY_POWER:
                continue
            if value == 1:
                self.pressed = stamp
            elif value == 0 and self.pressed is not None:
                held, self.pressed = stamp - self.pressed, None
                if held >= HOLD_SECONDS:
                    self.shutdown()
                    return
                self.screen_on(not self.app.screen.on)

    def rescan_keyboards(self):
        found = set(inputdev.classify()["keyboard"])
        have = {self.paths.get(fd) for fd in self.kbd_fds}
        for path in found - have:
            try:
                fd = inputdev.open_device(path)
            except OSError:
                continue
            self.kbd_fds.append(fd)
            self.kbds[fd] = inputdev.Keyboard()
            self.paths[fd] = path

    def loop_once(self):
        a = self.app
        now = time.monotonic()
        if self.pressed is not None and now - self.pressed >= HOLD_SECONDS:
            if any(inputdev.key_down(fd, inputdev.KEY_POWER) for fd in self.power_fds):
                self.shutdown()
                return
            self.pressed = None
        if now >= self.next_scan:
            self.rescan_keyboards()
            self.next_scan = now + 5
        term.reap()
        fds, mon = self.fds()
        timeout = 30.0
        if a.screen.on:
            t = a.timeout()
            timeout = 0.5 if not t else max(0.05, min(0.5, a.last_activity + t - now))
            if a.page.keyboard and a.page.keyboard.active:
                timeout = 0.05
        if self.pressed is not None or a.wifi.attempt or a.wifi.scanning or a.bt.phase:
            timeout = min(timeout, 0.2 if self.pressed is not None else 1.0)
        try:
            ready, _, _ = select.select(fds, [], [], timeout)
        except InterruptedError:
            ready = []
        for fd in ready:
            if a.session and fd == a.session.fd:
                if not a.session.read():
                    a.dirty = True
                elif isinstance(a.page, TerminalPage):
                    a.dirty = True
            elif fd is mon:
                a.wifi.events()
            elif a.bt.on and fd == a.bt.fd:
                a.bt.read()
            else:
                try:
                    data = os.read(fd, inputdev.EVENT.size * 64)
                except BlockingIOError:
                    continue
                except OSError:
                    self.drop(fd)
                    continue
                if fd in self.power_fds:
                    self.power_events(fd, data)
                elif fd in self.touch:  # always parsed; acts only while the screen is on
                    for _, et, code, value in inputdev.events(data):
                        g = self.touch[fd].feed(et, code, value)
                        if g and a.screen.on:
                            a.touch(g)
                elif fd in self.kbds:
                    for _, et, code, value in inputdev.events(data):
                        k = self.kbds[fd].feed(et, code, value)
                        if k and a.screen.on:
                            a.key(k)
        try:
            a.wifi.tick()
        except OSError:
            pass
        a.bt.tick()
        if a.screen.on:
            if a.page.tick():
                a.dirty = True
            minute = time.strftime("%H:%M")
            if minute != self.minute:
                self.minute, a.dirty = minute, True
            t = a.timeout()
            if t and time.monotonic() - a.last_activity > t:
                self.screen_on(False)
            elif a.dirty and time.monotonic() - self.last_render > 0.03:
                # more input waiting (fast typing, a burst of output): draw once it is read, or every 0.2 s
                # the list again: reading may have closed an fd (the shell ended, Bluetooth failed)
                busy = select.select(self.fds()[0], [], [], 0)[0] if time.monotonic() - self.last_render < 0.2 \
                    else []
                if not busy:
                    a.render()
                    self.last_render = time.monotonic()

    def fds(self):
        """Everything to wait on now, and the Wi-Fi event socket (or None)."""
        a = self.app
        fds = list(self.power_fds) + list(self.touch_fds) + list(self.kbd_fds)
        if a.session and a.session.alive:
            fds.append(a.session.fd)
        mon = a.wifi.monitor_fd()
        if mon is not None:
            fds.append(mon)
        if a.bt.on:
            fds.append(a.bt.fd)
        return fds, mon

    def drop(self, fd):
        for group in (self.power_fds, self.touch_fds, self.kbd_fds):
            if fd in group:
                group.remove(fd)
        self.kbds.pop(fd, None)
        self.touch.pop(fd, None)
        self.paths.pop(fd, None)
        try:
            os.close(fd)
        except OSError:
            pass


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    screen = fb.Screen()
    fonts = tuple(Font(FONTS + name) for name in
                  ("ter-132b.psf.gz", "ter-124b.psf.gz", "ter-120n.psf.gz", "ter-120n.psf.gz"))
    devices = inputdev.classify()
    if not devices["power"]:
        raise SystemExit("power key input (mtk-kpd) not found")
    power = [inputdev.open_device(p) for p in devices["power"]]
    touch, kbd = [], []
    paths = {}
    for p in devices["touch"]:
        fd = inputdev.open_device(p)
        touch.append(fd)
        paths[fd] = p
    for p in devices["keyboard"]:
        fd = inputdev.open_device(p)
        kbd.append(fd)
        paths[fd] = p
    app = App(screen, fonts, cfgmod.Config(), wifimod.Wifi(), btmod.Bluetooth(),
              terminal_enabled=os.environ.get("I14_ENABLE_TERMINAL") == "1")
    runner = Runner(app, power, touch, kbd)
    runner.paths = paths
    try:
        runner.screen_on(True)  # show once at start (locked when a PIN is set)
        while True:
            runner.loop_once()
    finally:  # never leave the display on (stop, error, failed poweroff)
        if app.session:
            app.session.close()
        app.bt.power(False)
        screen.off_for_good()
