"""Buttons, a scrolling list, a text field and the on-screen keyboard.

Touch events are ("down"|"move"|"up", x, y). Keys are (name or character, ctrl, alt), the same
form for the on-screen keyboard and a hardware keyboard.
"""
import time

BG, FG, DIM, ACCENT = (12, 14, 18), (235, 235, 235), (140, 145, 155), (90, 160, 255)
PANEL, PANEL_HI, KEY, KEY_HI, KEY_MOD = (32, 36, 44), (60, 70, 90), (52, 56, 66), (100, 110, 130), (38, 42, 50)
GREEN, RED, YELLOW = (80, 220, 100), (240, 80, 70), (240, 200, 80)
TAP_SLOP = 24  # pixels a finger may move and still tap


def inside(rect, x, y):
    rx, ry, rw, rh = rect
    return rx <= x < rx + rw and ry <= y < ry + rh


def center_text(c, rect, text, font, color, bg, scale=1):
    x, y, w, h = rect
    tw = c.text_width(text, font, scale)
    if tw > w - 8:
        n = max(1, (w - 8) // (font.w * scale))
        text, tw = text[:n], n * font.w * scale
    c.text(x + (w - tw) // 2, y + (h - font.h * scale) // 2, text, font, color, bg, scale)


class Button:
    def __init__(self, rect, label, action, color=PANEL, fg=FG, scale=1):
        self.rect, self.label, self.action = rect, label, action
        self.color, self.fg, self.scale = color, fg, scale
        self.pressed = False

    def draw(self, c, font):
        bg = PANEL_HI if self.pressed else self.color
        c.fill(*self.rect, bg)
        center_text(c, self.rect, self.label() if callable(self.label) else self.label, font, self.fg, bg,
                    self.scale)

    def touch(self, ev):
        kind, x, y = ev
        hit = inside(self.rect, x, y)
        if kind == "down":
            self.pressed = hit
            return hit
        if kind == "move":
            if self.pressed and not hit:
                self.pressed = False
            return self.pressed
        if kind == "up":
            was, self.pressed = self.pressed, False
            if was and hit:
                self.action()
                return True
        return False


class ListView:
    """Rows of (text, subtext, color) in rect; drag scrolls, a tap calls on_tap(index)."""

    def __init__(self, rect, on_tap, row_h=100):
        self.rect, self.on_tap, self.row_h = rect, on_tap, row_h
        self.items = []
        self.offset = 0
        self.start = None
        self.pressed = None

    def set_items(self, items):
        if items != self.items:  # new scan results under a finger: never tap a row that moved or went
            self.pressed = None
            if self.start is not None:
                self.start = (self.start[0], self.start[1], None)
        self.items = items
        self.offset = max(0, min(self.offset, self._max()))

    def _max(self):
        return max(0, len(self.items) * self.row_h - self.rect[3])

    def draw(self, c, font, small):
        x, y, w, h = self.rect
        c.fill(x, y, w, h, BG)
        first = self.offset // self.row_h
        for i in range(first, min(len(self.items), first + h // self.row_h + 2)):
            top = y + i * self.row_h - self.offset
            if top >= y + h:
                break
            text, sub, color = self.items[i]
            bg = PANEL_HI if i == self.pressed else PANEL
            vis_top, vis_bot = max(top, y), min(top + self.row_h - 6, y + h)
            if vis_bot <= vis_top:
                continue
            c.fill(x, vis_top, w, vis_bot - vis_top, bg)
            if top >= y and top + self.row_h <= y + h:
                c.text(x + 20, top + 14, text, font, color, bg, maxw=w - 40)
                if sub:
                    c.text(x + 20, top + 20 + font.h, sub, small, DIM, bg, maxw=w - 40)
        if not self.items:
            c.text(x + 20, y + 20, "(leeg)", small, DIM, BG)

    def touch(self, ev):
        kind, px, py = ev
        x, y, w, h = self.rect
        if kind == "down":
            if not inside(self.rect, px, py):
                self.start = None
                return False
            self.start = (py, self.offset, True)  # (y, offset, the rows are still the same)
            i = (py - y + self.offset) // self.row_h
            self.pressed = i if i < len(self.items) else None
            return True
        if self.start is None:
            return False
        y0, off0, _ = self.start
        if kind == "move":
            if abs(py - y0) > TAP_SLOP:
                self.pressed = None
                self.offset = max(0, min(self._max(), off0 - (py - y0)))
            return True
        if kind == "up":
            i, self.pressed, start, self.start = self.pressed, None, self.start, None
            if i is not None and start[2] is not None and i < len(self.items) and abs(py - y0) <= TAP_SLOP:
                self.on_tap(i)
            return True
        return False


class TextField:
    def __init__(self, rect, label, secret=False, maxlen=63):
        self.rect, self.label, self.secret, self.maxlen = rect, label, secret, maxlen
        self.value = ""
        self.show = False

    def key(self, k):
        name, ctrl, _ = k
        if name == "backspace":
            self.value = self.value[:-1]
        elif len(name) == 1 and not ctrl and name.isprintable() and len(self.value) < self.maxlen:
            self.value += name
        else:
            return False
        return True

    def draw(self, c, font, small):
        x, y, w, h = self.rect
        c.text(x, y, self.label, small, DIM, BG)
        fy = y + small.h + 8
        c.fill(x, fy, w, h - small.h - 8, PANEL)
        text = "*" * len(self.value) if self.secret and not self.show else self.value
        n = (w - 40) // font.w
        shown = text[-n:] if len(text) > n else text
        tx = c.text(x + 16, fy + (h - small.h - 8 - font.h) // 2, shown + "_", font, FG, PANEL)
        return tx


# On-screen keyboard. Key: (label, output, width in units); output is a character or a name.
LETTERS = [
    [(ch, ch, 1) for ch in "qwertyuiop"],
    [(None, None, 0.5)] + [(ch, ch, 1) for ch in "asdfghjkl"],
    [("shift", "shift", 1.5)] + [(ch, ch, 1) for ch in "zxcvbnm"] + [("<x", "backspace", 1.5)],
    [("123", "sym", 1.5), (",", ",", 1), ("", " ", 5), (".", ".", 1), ("enter", "enter", 1.5)],
]
SYMBOLS = [
    [(ch, ch, 1) for ch in "1234567890"],
    [(ch, ch, 1) for ch in "-/:;()$&@\""],
    [("#+=", "more", 1.5)] + [(ch, ch, 1) for ch in ".,?!'*+"] + [("<x", "backspace", 1.5)],
    [("abc", "abc", 1.5), ("_", "_", 1), ("", " ", 5), ("=", "=", 1), ("enter", "enter", 1.5)],
]
MORE = [
    [(ch, ch, 1) for ch in "[]{}#%^*+="],
    [(ch, ch, 1) for ch in "_\\|~<>`@$&"],
    [("123", "sym", 1.5)] + [(ch, ch, 1) for ch in ".,?!'\";"] + [("<x", "backspace", 1.5)],
    [("abc", "abc", 1.5), (":", ":", 1), ("", " ", 5), ("/", "/", 1), ("enter", "enter", 1.5)],
]
TERMINAL_ROW = [("esc", "esc", 1), ("tab", "tab", 1), ("ctrl", "ctrl", 1.2), ("←", "left", 1),
                ("↓", "down", 1), ("↑", "up", 1), ("→", "right", 1), ("|", "|", 0.9),
                ("-", "-", 0.9), ("/", "/", 1)]
ARROW_FALLBACK = {"←": "<", "↑": "^", "→": ">", "↓": "v"}
REPEAT = {"backspace", "left", "right", "up", "down"}
MODIFIERS = {"shift", "sym", "abc", "more", "ctrl"}


class Keyboard:
    ROW_H = 92

    def __init__(self, width, bottom, on_key, terminal_row=False):
        self.width, self.bottom, self.on_key = width, bottom, on_key
        self.terminal_row = terminal_row
        self.layout = "letters"
        self.shift = 0  # 0 off, 1 next letter, 2 caps lock
        self.ctrl = False
        self.active = None  # key rect + output under the finger
        self.repeat_at = None
        self.repeated = False
        self._build()

    @property
    def height(self):
        return self.ROW_H * (5 if self.terminal_row else 4)

    @property
    def top(self):
        return self.bottom - self.height

    def _build(self):
        rows = {"letters": LETTERS, "sym": SYMBOLS, "more": MORE}[self.layout]
        if self.terminal_row:
            rows = [TERMINAL_ROW] + rows
        self.keys = []
        y = self.top
        for row in rows:
            units = sum(k[2] for k in row)
            unit = self.width / units
            x = 0.0
            for label, out, wu in row:
                w = wu * unit
                if out is not None:  # drawn with a small gap; the gap still belongs to the key
                    self.keys.append(((int(x) + 3, y + 4, int(x + w) - int(x) - 6, self.ROW_H - 8), label, out,
                                      (int(x), y, int(x + w) - int(x), self.ROW_H)))
                x += w
            y += self.ROW_H

    def set_terminal_row(self, on):
        self.terminal_row = on
        self._build()

    def key_at(self, x, y):
        for k in self.keys:
            if inside(k[3], x, y):
                return k
        return None

    def draw(self, c, font):
        c.fill(0, self.top, self.width, self.height, BG)
        for rect, label, out, _ in self.keys:
            lit = (self.active is not None and self.active[2] == out and self.active[0] == rect) or \
                (out == "shift" and self.shift) or (out == "ctrl" and self.ctrl)
            bg = KEY_HI if lit else (KEY_MOD if out in MODIFIERS | {"backspace", "enter"} or
                                     (len(out) > 1 and out != " ") else KEY)
            text = label
            if text in ARROW_FALLBACK and not font.has(text):
                text = ARROW_FALLBACK[text]
            if out == "shift" and self.shift == 2:
                text = "CAPS"
            elif len(out) == 1 and out.isalpha() and self.shift:
                text = label.upper()
            c.fill(*rect, bg)
            center_text(c, rect, text, font, FG, bg)

    def touch(self, ev):
        kind, x, y = ev
        if kind == "down":
            if y < self.top:
                return False
            self.active = self.key_at(x, y)
            self.repeated = False
            self.repeat_at = time.monotonic() + 0.45 if self.active and self.active[2] in REPEAT else None
            return True
        if self.active is None and kind != "up":
            return y >= self.top
        if kind == "move":
            k = self.key_at(x, y)
            if k is not self.active:
                self.active, self.repeated = k, False
                self.repeat_at = time.monotonic() + 0.45 if k and k[2] in REPEAT else None
            return True
        if kind == "up":
            k, self.active = self.active, None
            repeated, self.repeated = self.repeated, False
            self.repeat_at = None
            if k is not None and not repeated:
                self._press(k[2])
            return k is not None or y >= self.top
        return False

    def tick(self):
        """Key repeat while backspace or an arrow is held."""
        if self.active and self.repeat_at and time.monotonic() >= self.repeat_at:
            self._press(self.active[2])
            self.repeat_at = time.monotonic() + 0.08
            self.repeated = True
            return True
        return False

    def _press(self, out):
        if out == "shift":
            self.shift = (self.shift + 1) % 3
        elif out in ("sym", "more", "abc"):
            self.layout = "letters" if out == "abc" else out
            self._build()
        elif out == "ctrl":
            self.ctrl = not self.ctrl
        else:
            ch = out
            if len(ch) == 1 and ch.isalpha() and self.shift:
                ch = ch.upper()
                if self.shift == 1:
                    self.shift = 0
            ctrl, self.ctrl = self.ctrl, False
            self.on_key((ch, ctrl, False))
