"""A small VT102/xterm-subset terminal emulator for the on-screen terminal (TERM=vt102).

Enough for a shell, less, top/htop and nano: cursor movement, erase, scroll region, insert/delete
of lines and characters, SGR colours, the alternate screen, DEC line drawing, deferred wrap,
scrollback. Cells are (character, attribute); attribute = (fg, bg, flags) with fg/bg None
(default), a palette index or an (r, g, b) tuple. Replies (cursor position, device attributes)
go to the reply callback, i.e. back into the pty.
"""
import codecs

BOLD, UNDERLINE, REVERSE = 1, 2, 4
DEFAULT = (None, None, 0)
BLANK = (" ", DEFAULT)
DEC_GRAPHICS = dict(zip("`afgjklmnopqrstuvwxyz{|}~",
                        "◆▒°±┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·"))


class VT:
    def __init__(self, cols, rows, reply=None, scrollback=1000):
        self.cols, self.rows = cols, rows
        self.reply = reply or (lambda data: None)
        self.max_scrollback = scrollback
        self.scrollback = []
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.reset()

    # --- state -------------------------------------------------------------------------------
    def reset(self):
        self.main = self._blank_screen()
        self.alt = None
        self.screen = self.main
        self.x = self.y = 0
        self.attr = DEFAULT
        self.wrap_pending = False
        self.top, self.bottom = 0, self.rows - 1
        self.autowrap = True
        self.origin = False
        self.insert = False
        self.app_cursor = False
        self.cursor_visible = True
        self.saved = (0, 0, DEFAULT, False)
        self.g0_graphics = False
        self.tabs = set(range(8, 1000, 8))
        self.state = "ground"
        self.params = ""
        self.dirty = True

    def _restore(self):
        """Back to the saved cursor; clamped, as the screen may have shrunk since (resize)."""
        x, y, self.attr, self.g0_graphics = self.saved
        self.x, self.y = min(x, self.cols - 1), min(y, self.rows - 1)
        self.wrap_pending = False

    def _blank_line(self):
        return [BLANK] * self.cols

    def _blank_screen(self):
        return [self._blank_line() for _ in range(self.rows)]

    def resize(self, cols, rows):
        if (cols, rows) == (self.cols, self.rows):
            return
        active = "alt" if self.alt is not None else "main"
        for name in ("main", "alt"):
            scr = getattr(self, name)
            if scr is None:
                continue
            scr = [(line + [BLANK] * cols)[:cols] for line in scr]
            if rows < len(scr):
                cut = len(scr) - rows
                top_cut = min(cut, self.y) if name == active else cut  # keep the cursor line
                if name == "main":
                    self.scrollback.extend(scr[:top_cut])
                scr = scr[top_cut:top_cut + rows]
                if name == active:
                    self.y -= top_cut
            while len(scr) < rows:
                scr.append([BLANK] * cols)
            setattr(self, name, scr)
        self.screen = self.alt if self.alt is not None else self.main
        self.scrollback = [(line + [BLANK] * cols)[:cols] for line in self.scrollback[-self.max_scrollback:]]
        self.cols, self.rows = cols, rows
        self.x, self.y = min(self.x, cols - 1), max(0, min(self.y, rows - 1))
        self.top, self.bottom = 0, rows - 1
        self.wrap_pending = False
        self.dirty = True

    # --- input -------------------------------------------------------------------------------
    def feed(self, data):
        text = self.decoder.decode(data) if isinstance(data, (bytes, bytearray)) else data
        for ch in text:
            self._char(ch)
        self.dirty = True

    def _char(self, ch):
        st = self.state
        o = ord(ch)
        if st == "ground":
            if o >= 0x20 and o != 0x7F:
                self._print(ch)
            else:
                self._control(ch)
        elif st == "esc":
            self._esc(ch)
        elif st == "csi":
            if o < 0x20:
                self._control(ch)  # C0 controls inside a sequence take effect
            elif 0x40 <= o <= 0x7E:
                self.state = "ground"
                self._csi(ch)
            else:
                self.params += ch
                if len(self.params) > 64:
                    self.state = "ground"
        elif st == "string":  # OSC, DCS, ...: skip until BEL or ESC \
            if ch == "\x07":
                self.state = "ground"
            elif ch == "\x1b":
                self.state = "string_esc"
        elif st == "string_esc":
            self.state = "ground" if ch == "\\" else "string"
        elif st == "charset0":
            self.g0_graphics = ch == "0"
            self.state = "ground"
        elif st in ("charset1", "hash"):
            if st == "hash" and ch == "8":  # DECALN: fill with E
                for line in self.screen:
                    line[:] = [("E", DEFAULT)] * self.cols
            self.state = "ground"

    def _control(self, ch):
        if ch == "\x1b":
            self.state = "esc"
        elif ch == "\r":
            self.x, self.wrap_pending = 0, False
        elif ch in "\n\x0b\x0c":
            self._index()
        elif ch == "\b":
            if self.x > 0:
                self.x -= 1
            self.wrap_pending = False
        elif ch == "\t":
            self.x = min([t for t in self.tabs if t > self.x] + [self.cols - 1])
            self.x = min(self.x, self.cols - 1)
            self.wrap_pending = False
        # BEL, SO, SI, NUL and the rest: ignored

    def _esc(self, ch):
        self.state = "ground"
        if ch == "[":
            self.state, self.params = "csi", ""
        elif ch in "]PX^_":
            self.state = "string"
        elif ch == "(":
            self.state = "charset0"
        elif ch in ")*+":
            self.state = "charset1"
        elif ch == "#":
            self.state = "hash"
        elif ch == "7":
            self.saved = (self.x, self.y, self.attr, self.g0_graphics)
        elif ch == "8":
            self._restore()
            self.wrap_pending = False
        elif ch == "D":
            self._index()
        elif ch == "E":
            self.x = 0
            self._index()
        elif ch == "M":
            self._reverse_index()
        elif ch == "H":
            self.tabs.add(self.x)
        elif ch == "c":
            self.reset()

    # --- printing and scrolling -------------------------------------------------------------
    def _print(self, ch):
        if self.g0_graphics:
            ch = DEC_GRAPHICS.get(ch, ch)
        if self.wrap_pending:
            if self.autowrap:
                self.x = 0
                self._index()
            self.wrap_pending = False
        line = self.screen[self.y]
        if self.insert:
            line.insert(self.x, BLANK)
            del line[self.cols:]
        line[self.x] = (ch, self.attr)
        if self.x == self.cols - 1:
            self.wrap_pending = True
        else:
            self.x += 1

    def _blank(self):
        return (" ", (None, self.attr[1], 0)) if self.attr[1] is not None else BLANK

    def _scroll_up(self, n, top=None, bottom=None):
        top = self.top if top is None else top
        bottom = self.bottom if bottom is None else bottom
        n = max(1, min(n, bottom - top + 1))
        for _ in range(n):
            line = self.screen.pop(top)
            if self.screen is self.main and top == 0:
                self.scrollback.append(line)
            self.screen.insert(bottom, [self._blank()] * self.cols)
        if len(self.scrollback) > self.max_scrollback:
            del self.scrollback[:len(self.scrollback) - self.max_scrollback]

    def _scroll_down(self, n, top=None, bottom=None):
        top = self.top if top is None else top
        bottom = self.bottom if bottom is None else bottom
        n = max(1, min(n, bottom - top + 1))
        for _ in range(n):
            self.screen.pop(bottom)
            self.screen.insert(top, [self._blank()] * self.cols)

    def _index(self):
        if self.y == self.bottom:
            self._scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _reverse_index(self):
        if self.y == self.top:
            self._scroll_down(1)
        elif self.y > 0:
            self.y -= 1

    # --- CSI -------------------------------------------------------------------------------
    def _args(self, default=1):
        raw = self.params.lstrip("?>=!")
        out = []
        for part in raw.split(";"):
            part = part.split(":")[0]
            out.append(int(part) if part.isascii() and part.isdigit() else None)  # not "²": isdigit, no int
        return out, default

    def _csi(self, final):
        private = self.params[:1] if self.params[:1] in "?>=!" else ""
        args, _ = self._args()

        def a(i=0, d=1):
            v = args[i] if i < len(args) else None
            return d if v is None or (v == 0 and d == 1) else v

        line = self.screen[self.y]
        if final != "m":
            self.wrap_pending = False if final not in "hlnrcst" else self.wrap_pending
        if final == "A":
            self.y = max(self.top if self.y >= self.top else 0, self.y - a())
        elif final == "B" or final == "e":
            self.y = min(self.bottom if self.y <= self.bottom else self.rows - 1, self.y + a())
        elif final == "C" or final == "a":
            self.x = min(self.cols - 1, self.x + a())
        elif final == "D":
            self.x = max(0, self.x - a())
        elif final == "E":
            self.x, self.y = 0, min(self.bottom, self.y + a())
        elif final == "F":
            self.x, self.y = 0, max(self.top, self.y - a())
        elif final in "G`":
            self.x = min(self.cols - 1, a() - 1)
        elif final in "Hf":
            row, col = a(0) - 1, a(1) - 1
            if self.origin:
                row += self.top
            self.y = max(0, min(self.rows - 1, row))
            self.x = max(0, min(self.cols - 1, col))
        elif final == "d":
            self.y = max(0, min(self.rows - 1, a() - 1 + (self.top if self.origin else 0)))
        elif final == "J":
            mode = a(0, 0)
            if mode == 0:
                line[self.x:] = [self._blank()] * (self.cols - self.x)
                for y in range(self.y + 1, self.rows):
                    self.screen[y] = [self._blank()] * self.cols
            elif mode == 1:
                line[:self.x + 1] = [self._blank()] * (self.x + 1)
                for y in range(self.y):
                    self.screen[y] = [self._blank()] * self.cols
            elif mode in (2, 3):
                for y in range(self.rows):
                    self.screen[y] = [self._blank()] * self.cols
                if mode == 3:
                    self.scrollback.clear()
        elif final == "K":
            mode = a(0, 0)
            if mode == 0:
                line[self.x:] = [self._blank()] * (self.cols - self.x)
            elif mode == 1:
                line[:self.x + 1] = [self._blank()] * (self.x + 1)
            elif mode == 2:
                line[:] = [self._blank()] * self.cols
        elif final == "X":
            n = min(a(), self.cols - self.x)
            line[self.x:self.x + n] = [self._blank()] * n
        elif final == "@":
            n = min(a(), self.cols - self.x)
            line[self.x:self.x] = [self._blank()] * n
            del line[self.cols:]
        elif final == "P":
            n = min(a(), self.cols - self.x)
            del line[self.x:self.x + n]
            line.extend([self._blank()] * n)
        elif final == "L":
            if self.top <= self.y <= self.bottom:
                self._scroll_down(a(), self.y, self.bottom)
                self.x = 0
        elif final == "M":
            if self.top <= self.y <= self.bottom:
                n = max(1, min(a(), self.bottom - self.y + 1))
                for _ in range(n):  # deleted lines never go to the scrollback
                    self.screen.pop(self.y)
                    self.screen.insert(self.bottom, [self._blank()] * self.cols)
                self.x = 0
        elif final == "S":
            self._scroll_up(a())
        elif final == "T" and not private:
            self._scroll_down(a())
        elif final == "r" and not private:
            top, bottom = a(0) - 1, (args[1] if len(args) > 1 and args[1] else self.rows) - 1
            if 0 <= top < bottom <= self.rows - 1:
                self.top, self.bottom = top, bottom
                self.x, self.y = 0, (top if self.origin else 0)
        elif final == "m":
            self._sgr(args)
        elif final in "hl":
            self._mode(private, args, final == "h")
        elif final == "n":
            if a(0, 0) == 6:
                y = self.y - (self.top if self.origin else 0)
                self.reply(f"\x1b[{y + 1};{self.x + 1}R".encode())
            elif a(0, 0) == 5:
                self.reply(b"\x1b[0n")
        elif final == "c":
            if private == ">":
                self.reply(b"\x1b[>0;0;0c")
            elif not private:
                self.reply(b"\x1b[?6c")  # VT102
        elif final == "s" and not private:
            self.saved = (self.x, self.y, self.attr, self.g0_graphics)
        elif final == "u" and not private:
            self._restore()
        elif final == "g":
            if a(0, 0) == 0:
                self.tabs.discard(self.x)
            elif a(0, 0) == 3:
                self.tabs.clear()
        elif final == "b" and self.x > 0:  # REP: repeat the previous character
            prev = line[self.x - 1][0]
            for _ in range(min(a(), self.cols * self.rows)):
                self._print(prev)

    def _mode(self, private, args, on):
        for m in args:
            if private == "?":
                if m == 1:
                    self.app_cursor = on
                elif m == 6:
                    self.origin = on
                    self.x, self.y = 0, (self.top if on else 0)
                elif m == 7:
                    self.autowrap = on
                elif m == 25:
                    self.cursor_visible = on
                elif m in (47, 1047, 1049):
                    self._alt_screen(on, save=(m == 1049))
            elif m == 4:
                self.insert = on

    def _alt_screen(self, on, save):
        if on and self.alt is None:
            if save:
                self.saved = (self.x, self.y, self.attr, self.g0_graphics)
            self.alt = self._blank_screen()
            self.screen = self.alt
        elif not on and self.alt is not None:
            self.alt = None
            self.screen = self.main
            if save:
                self._restore()
        self.wrap_pending = False

    def _sgr(self, args):
        fg, bg, flags = self.attr
        i = 0
        if not args:
            args = [0]
        while i < len(args):
            p = args[i] if args[i] is not None else 0
            if p == 0:
                fg, bg, flags = None, None, 0
            elif p == 1:
                flags |= BOLD
            elif p == 4:
                flags |= UNDERLINE
            elif p == 7:
                flags |= REVERSE
            elif p == 22:
                flags &= ~BOLD
            elif p == 24:
                flags &= ~UNDERLINE
            elif p == 27:
                flags &= ~REVERSE
            elif 30 <= p <= 37:
                fg = p - 30
            elif p == 39:
                fg = None
            elif 40 <= p <= 47:
                bg = p - 40
            elif p == 49:
                bg = None
            elif 90 <= p <= 97:
                fg = p - 90 + 8
            elif 100 <= p <= 107:
                bg = p - 100 + 8
            elif p in (38, 48) and i + 1 < len(args):
                color, valid = None, False
                if args[i + 1] == 5 and i + 2 < len(args):
                    n, i = args[i + 2], i + 2
                    color, valid = n, n is not None and n <= 255  # 256 palette entries only
                elif args[i + 1] == 2 and i + 4 < len(args):
                    rgb, i = args[i + 2:i + 5], i + 4
                    color, valid = tuple(v or 0 for v in rgb), all(v is None or v <= 255 for v in rgb)
                if valid:  # an impossible colour leaves the current one as it is
                    if p == 38:
                        fg = color
                    else:
                        bg = color
            i += 1
        self.attr = (fg, bg, flags)

    # --- keys ------------------------------------------------------------------------------
    def key_bytes(self, name, ctrl=False, alt=False):
        """Bytes the terminal sends for a key: a character or a name like "up", "enter"."""
        arrows = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
        if name in arrows:
            return (b"\x1bO" if self.app_cursor else b"\x1b[") + arrows[name].encode()
        special = {"enter": b"\r", "backspace": b"\x7f", "tab": b"\t", "esc": b"\x1b",
                   "pgup": b"\x1b[5~", "pgdn": b"\x1b[6~", "delete": b"\x1b[3~", "insert": b"\x1b[2~"}
        if name in special:
            out = special[name]
        else:
            out = name.encode()
            if ctrl and len(name) == 1:
                c = name.lower()
                if "a" <= c <= "z":
                    out = bytes([ord(c) - 96])
                elif c in "@[\\]^_":
                    out = bytes([ord(c.upper()) & 0x1F])
                elif c == " ":
                    out = b"\0"
                elif c == "?":
                    out = b"\x7f"
        return (b"\x1b" + out) if alt else out

    # --- view ------------------------------------------------------------------------------
    def view(self, back=0):
        """The rows to show, scrolled back by back lines (0 = live screen; only the main screen scrolls)."""
        if back <= 0 or self.screen is not self.main:
            return self.screen
        back = min(back, len(self.scrollback))
        lines = self.scrollback[len(self.scrollback) - back:] + self.main
        return lines[:self.rows]

    def text(self):
        """Plain text of the live screen (tests and debugging)."""
        return "\n".join("".join(c[0] for c in line).rstrip() for line in self.screen)
