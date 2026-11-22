"""Linux input devices: finding them, the multitouch protocol B (fts_ts) and key codes to keys.

Events are struct input_event on arm64: two 64-bit time fields, u16 type, u16 code, s32 value.
"""
import fcntl
import os
import struct
import time

EVENT = struct.Struct("qqHHi")
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT, SYN_DROPPED = 0, 3
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
BTN_TOUCH = 330
KEY_POWER, KEY_VOLUMEDOWN, KEY_VOLUMEUP = 116, 114, 115
EVIOCGKEY = 0x80604518      # _IOR('E', 0x18, 96): keys that are down now
EVIOCSCLOCKID = 0x400445A0  # _IOW('E', 0xa0, int): timestamps from CLOCK_MONOTONIC
SYSFS = "/sys/class/input"


def _bits(text):
    """Capability bitmap from sysfs ("40 0 1000"): words, most significant first, 64 bits each on arm64."""
    value = 0
    for word in text.split():
        value = value << 64 | int(word, 16)
    return value


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def classify(sysfs=SYSFS):
    """{"touch": [paths], "power": [paths], "keyboard": [paths]} from the sysfs capability bitmaps."""
    found = {"touch": [], "power": [], "keyboard": []}
    try:
        names = sorted(n for n in os.listdir(sysfs) if n.startswith("event"))
    except OSError:
        return found
    for n in names:
        dev = f"{sysfs}/{n}/device"
        name = _read(f"{dev}/name")
        absbits = _bits(_read(f"{dev}/capabilities/abs") or "0")
        keybits = _bits(_read(f"{dev}/capabilities/key") or "0")
        path = f"/dev/input/{n}"
        if absbits >> ABS_MT_POSITION_X & 1 and absbits >> ABS_MT_POSITION_Y & 1:
            found["touch"].append(path)
        elif name == "mtk-kpd" or (keybits >> KEY_POWER & 1 and not keybits >> 30 & 1):
            found["power"].append(path)
        elif keybits >> 30 & 1 and keybits >> 44 & 1:  # KEY_A and KEY_Z: a real keyboard
            found["keyboard"].append(path)
    return found


def open_device(path):
    if not os.path.exists(path):  # /dev is static (no devtmpfs): make the node from sysfs
        try:
            major, minor = _read(f"{SYSFS}/{os.path.basename(path)}/dev").split(":")
            os.mknod(path, 0o600 | 0o020000, os.makedev(int(major), int(minor)))
        except (OSError, ValueError):
            pass
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, EVIOCSCLOCKID, struct.pack("i", time.CLOCK_MONOTONIC))
    except OSError:
        pass
    return fd


def key_down(fd, code):
    bits = bytearray(96)
    try:
        fcntl.ioctl(fd, EVIOCGKEY, bits)
    except OSError:
        return False
    return bool(bits[code // 8] >> (code % 8) & 1)


def events(data):
    """(time, type, code, value) for each whole event in data."""
    for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
        sec, usec, etype, code, value = EVENT.unpack_from(data, i)
        yield sec + usec / 1e6, etype, code, value


class Touch:
    """Protocol B (slots) to single-finger gestures: ("down"|"move"|"up", x, y), one per SYN_REPORT.

    Follows the first finger that touches; other fingers are ignored until all have lifted.
    Also handles single-touch devices (ABS_X/ABS_Y + BTN_TOUCH), e.g. a uinput test device.
    """

    def __init__(self):
        self.slot = 0
        self.slots = {}       # slot -> [tracking id, x, y] for fingers that touch now
        self.last = {}        # slot -> [x, y]: the kernel only sends values that changed, even
                              # across a lift, so a slot's position outlives its contact
        self.active = None    # followed slot
        self.down = False
        self.pos = (0, 0)
        self.dropped = False
        self.wait = False     # the followed finger lifted while others still touch
        self.single = {"x": 0, "y": 0, "touch": None}

    def cancel(self):
        """Forget any contact (screen off: the chip sleeps and may never report a lift). Keeps the slot
        and the last positions: the kernel still only sends values that change."""
        self.slots.clear()  # `dropped` stays: the rest of a damaged frame must still be skipped
        self.active, self.down, self.wait = None, False, False

    def feed(self, etype, code, value):
        """One raw event; returns a gesture tuple after a SYN_REPORT, else None."""
        if etype == EV_SYN and code == SYN_DROPPED:
            self.dropped = True
            return None
        if self.dropped:
            if etype == EV_SYN and code == SYN_REPORT:
                self.dropped = False  # state is unknown: end any gesture
                self.slots.clear()  # `last` stays: maybe stale after the loss, but never the y=0 back button
                self.active = None
                if self.down:
                    self.down = False
                    return ("up",) + self.pos
            return None
        if etype == EV_ABS:
            if code == ABS_MT_SLOT:
                self.slot = value
            elif code == ABS_MT_TRACKING_ID:
                if value < 0:
                    self.slots.pop(self.slot, None)
                else:
                    self.slots[self.slot] = [value, *self.last.get(self.slot, [0, 0])]
            elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y):
                i = 0 if code == ABS_MT_POSITION_X else 1
                self.last.setdefault(self.slot, [0, 0])[i] = value
                if self.slot in self.slots:
                    self.slots[self.slot][1 + i] = value
            elif code == ABS_X:
                self.single["x"] = value
            elif code == ABS_Y:
                self.single["y"] = value
        elif etype == EV_KEY and code == BTN_TOUCH:
            self.single["touch"] = bool(value)
        elif etype == EV_SYN and code == SYN_REPORT:
            return self._report()
        return None

    def _report(self):
        if not self.slots:
            self.wait = False
        if self.slots or self.active is not None:
            if self.active not in self.slots:
                # a new gesture only starts once every finger has lifted
                self.active = min(self.slots) if self.slots and not self.down and not self.wait else None
                if self.active is None and self.down:
                    self.wait = bool(self.slots)
            if self.active is not None:
                _, x, y = self.slots[self.active]
                was, self.down, self.pos = self.down, True, (x, y)
                return ("move" if was else "down", x, y)
        elif self.single["touch"] is not None:  # single-touch device
            if self.single["touch"]:
                was, self.down, self.pos = self.down, True, (self.single["x"], self.single["y"])
                return ("move" if was else "down",) + self.pos
        if self.down:
            self.down = False
            return ("up",) + self.pos
        return None


# Key codes (linux/input-event-codes.h) to characters, US layout: (plain, shifted).
_ROWS = {
    2: "1!", 3: "2@", 4: "3#", 5: "4$", 6: "5%", 7: "6^", 8: "7&", 9: "8*", 10: "9(", 11: "0)",
    12: "-_", 13: "=+", 16: "qQ", 17: "wW", 18: "eE", 19: "rR", 20: "tT", 21: "yY", 22: "uU",
    23: "iI", 24: "oO", 25: "pP", 26: "[{", 27: "]}", 30: "aA", 31: "sS", 32: "dD", 33: "fF",
    34: "gG", 35: "hH", 36: "jJ", 37: "kK", 38: "lL", 39: ";:", 40: "'\"", 41: "`~", 43: "\\|",
    44: "zZ", 45: "xX", 46: "cC", 47: "vV", 48: "bB", 49: "nN", 50: "mM", 51: ",<", 52: ".>",
    53: "/?", 57: "  ",
}
NAMED = {1: "esc", 14: "backspace", 15: "tab", 28: "enter", 96: "enter", 103: "up", 108: "down",
         105: "left", 106: "right", 102: "home", 107: "end", 104: "pgup", 109: "pgdn", 111: "delete"}
SHIFT, CTRL, ALT = {42, 54}, {29, 97}, {56, 100}
CAPS = 58


class Keyboard:
    """A hardware keyboard's events to Key tuples: (name or character, ctrl, alt)."""

    def __init__(self):
        self.held = set()
        self.caps = False

    def feed(self, etype, code, value):
        if etype != EV_KEY:
            return None
        if code in SHIFT | CTRL | ALT:
            (self.held.add if value else self.held.discard)(code)
            return None
        if value == 0:
            return None
        if code == CAPS and value == 1:
            self.caps = not self.caps
            return None
        ctrl, alt = bool(self.held & CTRL), bool(self.held & ALT)
        if code in NAMED:
            return (NAMED[code], ctrl, alt)
        if code in _ROWS:
            plain, shifted = _ROWS[code]
            shift = bool(self.held & SHIFT)
            if plain.isalpha():
                shift ^= self.caps
            return (shifted if shift else plain, ctrl, alt)
        return None
