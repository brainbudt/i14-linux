//! A small VT102/xterm-subset terminal emulator (TERM=vt102), as `i14ui/vt.py`.
//!
//! Enough for a shell, less, top/htop and nano: cursor movement, erase, scroll
//! region, insert/delete of lines and characters, SGR colours, the alternate
//! screen, DEC line drawing, deferred wrap and scrollback. Replies (cursor
//! position, device attributes) collect in `replies` for the caller to write
//! back into the pty.

use std::collections::BTreeSet;

pub const BOLD: u8 = 1;
pub const UNDERLINE: u8 = 2;
pub const REVERSE: u8 = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Color {
    /// Palette index: 0-15 from SGR 30-37/90-97 etc., up to 255 from `38;5;n`.
    Index(u8),
    Rgb(u8, u8, u8),
}

/// Foreground, background (None = default) and BOLD/UNDERLINE/REVERSE flags.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub struct Attr {
    pub fg: Option<Color>,
    pub bg: Option<Color>,
    pub flags: u8,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Cell {
    pub ch: char,
    pub attr: Attr,
}

pub const BLANK: Cell = Cell {
    ch: ' ',
    attr: Attr {
        fg: None,
        bg: None,
        flags: 0,
    },
};

type Line = Vec<Cell>;

fn dec_graphics(ch: char) -> char {
    const FROM: &str = "`afgjklmnopqrstuvwxyz{|}~";
    const TO: &str = "◆▒°±┘┐┌└┼⎺⎻─⎼⎽├┤┴┬│≤≥π≠£·";
    FROM.chars()
        .position(|c| c == ch)
        .and_then(|i| TO.chars().nth(i))
        .unwrap_or(ch)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum State {
    Ground,
    Esc,
    Csi,
    String,
    StringEsc,
    Charset0,
    Charset1,
    Hash,
}

/// Saved cursor: x, y, attribute, G0 line drawing.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Saved {
    pub x: usize,
    pub y: usize,
    pub attr: Attr,
    pub g0_graphics: bool,
}

const SAVED0: Saved = Saved {
    x: 0,
    y: 0,
    attr: BLANK.attr,
    g0_graphics: false,
};

/// Numeric CSI parameters are capped here; every use compares them with screen
/// sizes or small constants, so the cap never changes a result.
const ARG_CAP: i64 = 1 << 40;

pub struct Vt {
    pub cols: usize,
    pub rows: usize,
    pub max_scrollback: usize,
    pub scrollback: Vec<Line>,
    pub main: Vec<Line>,
    pub alt: Option<Vec<Line>>,
    pub x: usize,
    pub y: usize,
    pub attr: Attr,
    pub wrap_pending: bool,
    pub top: usize,
    pub bottom: usize,
    pub autowrap: bool,
    pub origin: bool,
    pub insert: bool,
    pub app_cursor: bool,
    pub cursor_visible: bool,
    pub saved: Saved,
    pub g0_graphics: bool,
    pub tabs: BTreeSet<usize>,
    pub state: State,
    pub params: String,
    pub dirty: bool,
    /// Bytes to send back to the program (device status and attribute reports).
    pub replies: Vec<u8>,
    /// An incomplete UTF-8 sequence at the end of the last `feed`.
    pending: Vec<u8>,
}

impl Vt {
    pub fn new(cols: usize, rows: usize, scrollback: usize) -> Self {
        let mut vt = Vt {
            cols,
            rows,
            max_scrollback: scrollback,
            scrollback: Vec::new(),
            main: Vec::new(),
            alt: None,
            x: 0,
            y: 0,
            attr: Attr::default(),
            wrap_pending: false,
            top: 0,
            bottom: 0,
            autowrap: true,
            origin: false,
            insert: false,
            app_cursor: false,
            cursor_visible: true,
            saved: SAVED0,
            g0_graphics: false,
            tabs: BTreeSet::new(),
            state: State::Ground,
            params: String::new(),
            dirty: true,
            replies: Vec::new(),
            pending: Vec::new(),
        };
        vt.reset();
        vt
    }

    // --- state ---------------------------------------------------------------

    pub fn reset(&mut self) {
        self.main = self.blank_screen();
        self.alt = None;
        self.x = 0;
        self.y = 0;
        self.attr = Attr::default();
        self.wrap_pending = false;
        self.top = 0;
        self.bottom = self.rows - 1;
        self.autowrap = true;
        self.origin = false;
        self.insert = false;
        self.app_cursor = false;
        self.cursor_visible = true;
        self.saved = SAVED0;
        self.g0_graphics = false;
        self.tabs = (8..1000).step_by(8).collect();
        self.state = State::Ground;
        self.params.clear();
        self.dirty = true;
    }

    /// The screen being drawn on: the alternate one while it is active.
    pub fn screen(&self) -> &Vec<Line> {
        self.alt.as_ref().unwrap_or(&self.main)
    }

    fn screen_mut(&mut self) -> &mut Vec<Line> {
        self.alt.as_mut().unwrap_or(&mut self.main)
    }

    fn save_cursor(&mut self) {
        self.saved = Saved {
            x: self.x,
            y: self.y,
            attr: self.attr,
            g0_graphics: self.g0_graphics,
        };
    }

    /// Back to the saved cursor; clamped, as the screen may have shrunk since.
    fn restore(&mut self) {
        let s = self.saved;
        self.attr = s.attr;
        self.g0_graphics = s.g0_graphics;
        self.x = s.x.min(self.cols - 1);
        self.y = s.y.min(self.rows - 1);
        self.wrap_pending = false;
    }

    fn blank_screen(&self) -> Vec<Line> {
        vec![vec![BLANK; self.cols]; self.rows]
    }

    pub fn resize(&mut self, cols: usize, rows: usize) {
        if (cols, rows) == (self.cols, self.rows) {
            return;
        }
        let alt_active = self.alt.is_some();
        let fit = |line: &mut Line| line.resize(cols, BLANK);
        for is_main in [true, false] {
            let active = is_main != alt_active;
            let mut scr = if is_main {
                std::mem::take(&mut self.main)
            } else {
                match self.alt.take() {
                    Some(a) => a,
                    None => continue,
                }
            };
            scr.iter_mut().for_each(fit);
            if rows < scr.len() {
                let cut = scr.len() - rows;
                let top_cut = if active { cut.min(self.y) } else { cut }; // keep the cursor line
                let removed: Vec<Line> = scr.drain(..top_cut).collect();
                if is_main {
                    self.scrollback.extend(removed);
                }
                scr.truncate(rows);
                if active {
                    self.y -= top_cut;
                }
            }
            scr.resize(rows, vec![BLANK; cols]);
            if is_main {
                self.main = scr;
            } else {
                self.alt = Some(scr);
            }
        }
        // Python's scrollback[-0:] keeps everything; match it.
        if self.max_scrollback > 0 && self.scrollback.len() > self.max_scrollback {
            let extra = self.scrollback.len() - self.max_scrollback;
            self.scrollback.drain(..extra);
        }
        self.scrollback.iter_mut().for_each(fit);
        self.cols = cols;
        self.rows = rows;
        self.x = self.x.min(cols - 1);
        self.y = self.y.min(rows - 1);
        self.top = 0;
        self.bottom = rows - 1;
        self.wrap_pending = false;
        self.dirty = true;
    }

    // --- input ---------------------------------------------------------------

    /// Output from the program, as bytes (decoded as UTF-8 across calls; invalid
    /// sequences become U+FFFD).
    pub fn feed(&mut self, data: &[u8]) {
        let mut buf = std::mem::take(&mut self.pending);
        buf.extend_from_slice(data);
        let mut rest = &buf[..];
        loop {
            match std::str::from_utf8(rest) {
                Ok(s) => {
                    s.chars().for_each(|c| self.char(c));
                    break;
                }
                Err(e) => {
                    let (good, bad) = rest.split_at(e.valid_up_to());
                    // SAFETY-free: from_utf8 vouched for `good`.
                    std::str::from_utf8(good)
                        .unwrap()
                        .chars()
                        .for_each(|c| self.char(c));
                    match e.error_len() {
                        Some(n) => {
                            self.char('\u{fffd}');
                            rest = &bad[n..];
                        }
                        None => {
                            self.pending = bad.to_vec();
                            break;
                        }
                    }
                }
            }
        }
        self.dirty = true;
    }

    /// Already-decoded text.
    pub fn feed_str(&mut self, text: &str) {
        text.chars().for_each(|c| self.char(c));
        self.dirty = true;
    }

    fn char(&mut self, ch: char) {
        let o = ch as u32;
        match self.state {
            State::Ground => {
                if o >= 0x20 && o != 0x7f {
                    self.print(ch)
                } else {
                    self.control(ch)
                }
            }
            State::Esc => self.esc(ch),
            State::Csi => {
                if o < 0x20 {
                    self.control(ch); // C0 controls inside a sequence take effect
                } else if (0x40..=0x7e).contains(&o) {
                    self.state = State::Ground;
                    self.csi(ch);
                } else {
                    self.params.push(ch);
                    if self.params.chars().count() > 64 {
                        self.state = State::Ground;
                    }
                }
            }
            State::String => {
                // OSC, DCS, ...: skip until BEL or ESC \
                if ch == '\x07' {
                    self.state = State::Ground;
                } else if ch == '\x1b' {
                    self.state = State::StringEsc;
                }
            }
            State::StringEsc => {
                self.state = if ch == '\\' {
                    State::Ground
                } else {
                    State::String
                }
            }
            State::Charset0 => {
                self.g0_graphics = ch == '0';
                self.state = State::Ground;
            }
            State::Charset1 | State::Hash => {
                if self.state == State::Hash && ch == '8' {
                    // DECALN: fill with E
                    let cols = self.cols;
                    for line in self.screen_mut() {
                        *line = vec![
                            Cell {
                                ch: 'E',
                                attr: Attr::default()
                            };
                            cols
                        ];
                    }
                }
                self.state = State::Ground;
            }
        }
    }

    fn control(&mut self, ch: char) {
        match ch {
            '\x1b' => self.state = State::Esc,
            '\r' => {
                self.x = 0;
                self.wrap_pending = false;
            }
            '\n' | '\x0b' | '\x0c' => self.index(),
            '\x08' => {
                self.x = self.x.saturating_sub(1);
                self.wrap_pending = false;
            }
            '\t' => {
                let next = self
                    .tabs
                    .range(self.x + 1..)
                    .next()
                    .copied()
                    .unwrap_or(usize::MAX);
                self.x = next.min(self.cols - 1);
                self.wrap_pending = false;
            }
            _ => {} // BEL, SO, SI, NUL and the rest: ignored
        }
    }

    fn esc(&mut self, ch: char) {
        self.state = State::Ground;
        match ch {
            '[' => {
                self.state = State::Csi;
                self.params.clear();
            }
            ']' | 'P' | 'X' | '^' | '_' => self.state = State::String,
            '(' => self.state = State::Charset0,
            ')' | '*' | '+' => self.state = State::Charset1,
            '#' => self.state = State::Hash,
            '7' => self.save_cursor(),
            '8' => self.restore(),
            'D' => self.index(),
            'E' => {
                self.x = 0;
                self.index();
            }
            'M' => self.reverse_index(),
            'H' => {
                self.tabs.insert(self.x);
            }
            'c' => self.reset(),
            _ => {}
        }
    }

    // --- printing and scrolling ----------------------------------------------

    fn print(&mut self, ch: char) {
        let ch = if self.g0_graphics {
            dec_graphics(ch)
        } else {
            ch
        };
        if self.wrap_pending {
            if self.autowrap {
                self.x = 0;
                self.index();
            }
            self.wrap_pending = false;
        }
        let (x, y, cols, attr, insert) = (self.x, self.y, self.cols, self.attr, self.insert);
        let line = &mut self.screen_mut()[y];
        if insert {
            line.insert(x, BLANK);
            line.truncate(cols);
        }
        line[x] = Cell { ch, attr };
        if x == cols - 1 {
            self.wrap_pending = true;
        } else {
            self.x += 1;
        }
    }

    /// An erased cell: keeps the current background colour.
    fn blank(&self) -> Cell {
        match self.attr.bg {
            Some(bg) => Cell {
                ch: ' ',
                attr: Attr {
                    fg: None,
                    bg: Some(bg),
                    flags: 0,
                },
            },
            None => BLANK,
        }
    }

    fn blank_line(&self) -> Line {
        vec![self.blank(); self.cols]
    }

    fn scroll_up(&mut self, n: i64, top: usize, bottom: usize) {
        let n = n.min((bottom - top + 1) as i64).max(1);
        let to_scrollback = self.alt.is_none() && top == 0;
        for _ in 0..n {
            let blank = self.blank_line();
            let scr = self.screen_mut();
            let line = scr.remove(top);
            scr.insert(bottom, blank);
            if to_scrollback {
                self.scrollback.push(line);
            }
        }
        if self.scrollback.len() > self.max_scrollback {
            let extra = self.scrollback.len() - self.max_scrollback;
            self.scrollback.drain(..extra);
        }
    }

    fn scroll_down(&mut self, n: i64, top: usize, bottom: usize) {
        let n = n.min((bottom - top + 1) as i64).max(1);
        for _ in 0..n {
            let blank = self.blank_line();
            let scr = self.screen_mut();
            scr.remove(bottom);
            scr.insert(top, blank);
        }
    }

    fn index(&mut self) {
        if self.y == self.bottom {
            self.scroll_up(1, self.top, self.bottom);
        } else if self.y < self.rows - 1 {
            self.y += 1;
        }
    }

    fn reverse_index(&mut self) {
        if self.y == self.top {
            self.scroll_down(1, self.top, self.bottom);
        } else if self.y > 0 {
            self.y -= 1;
        }
    }

    // --- CSI -----------------------------------------------------------------

    fn args(&self) -> Vec<Option<i64>> {
        let raw = self.params.trim_start_matches(['?', '>', '=', '!']);
        raw.split(';')
            .map(|part| {
                let part = part.split(':').next().unwrap_or("");
                if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) {
                    // Saturating parse: see ARG_CAP.
                    Some(
                        part.bytes()
                            .fold(0i64, |acc, b| (acc * 10 + (b - b'0') as i64).min(ARG_CAP)),
                    )
                } else {
                    None
                }
            })
            .collect()
    }

    fn csi(&mut self, fin: char) {
        let private = self.params.chars().next().filter(|c| "?>=!".contains(*c));
        let args = self.args();
        let a = |i: usize, d: i64| -> i64 {
            match args.get(i).copied().flatten() {
                None => d,
                Some(0) if d == 1 => d,
                Some(v) => v,
            }
        };
        let (cols, rows) = (self.cols as i64, self.rows as i64);
        let (x, y, top, bottom) = (
            self.x as i64,
            self.y as i64,
            self.top as i64,
            self.bottom as i64,
        );
        let clamp = |v: i64, lo: i64, hi: i64| v.min(hi).max(lo) as usize;
        if fin != 'm' && !"hlnrcst".contains(fin) {
            self.wrap_pending = false;
        }
        match fin {
            'A' => self.y = (y - a(0, 1)).max(if y >= top { top } else { 0 }) as usize,
            'B' | 'e' => {
                self.y = (y + a(0, 1)).min(if y <= bottom { bottom } else { rows - 1 }) as usize
            }
            'C' | 'a' => self.x = (x + a(0, 1)).min(cols - 1) as usize,
            'D' => self.x = (x - a(0, 1)).max(0) as usize,
            'E' => {
                self.x = 0;
                self.y = (y + a(0, 1)).min(bottom) as usize;
            }
            'F' => {
                self.x = 0;
                self.y = (y - a(0, 1)).max(top) as usize;
            }
            'G' | '`' => self.x = (a(0, 1) - 1).min(cols - 1) as usize,
            'H' | 'f' => {
                let mut row = a(0, 1) - 1;
                if self.origin {
                    row += top;
                }
                self.y = clamp(row, 0, rows - 1);
                self.x = clamp(a(1, 1) - 1, 0, cols - 1);
            }
            'd' => self.y = clamp(a(0, 1) - 1 + if self.origin { top } else { 0 }, 0, rows - 1),
            'J' => {
                let b = self.blank();
                let (xu, yu) = (self.x, self.y);
                let mode = a(0, 0);
                let scr = self.screen_mut();
                match mode {
                    0 => {
                        scr[yu][xu..].fill(b);
                        scr[yu + 1..].iter_mut().for_each(|l| l.fill(b));
                    }
                    1 => {
                        scr[yu][..=xu].fill(b);
                        scr[..yu].iter_mut().for_each(|l| l.fill(b));
                    }
                    2 | 3 => scr.iter_mut().for_each(|l| l.fill(b)),
                    _ => {}
                }
                if mode == 3 {
                    self.scrollback.clear();
                }
            }
            'K' => {
                let b = self.blank();
                let (xu, yu) = (self.x, self.y);
                let mode = a(0, 0);
                let line = &mut self.screen_mut()[yu];
                match mode {
                    0 => line[xu..].fill(b),
                    1 => line[..=xu].fill(b),
                    2 => line.fill(b),
                    _ => {}
                }
            }
            'X' => {
                let b = self.blank();
                let n = a(0, 1).min(cols - x) as usize;
                let (xu, yu) = (self.x, self.y);
                self.screen_mut()[yu][xu..xu + n].fill(b);
            }
            '@' => {
                let b = self.blank();
                let n = a(0, 1).min(cols - x) as usize;
                let (xu, yu, cu) = (self.x, self.y, self.cols);
                let line = &mut self.screen_mut()[yu];
                line.splice(xu..xu, std::iter::repeat_n(b, n));
                line.truncate(cu);
            }
            'P' => {
                let b = self.blank();
                let n = a(0, 1).min(cols - x) as usize;
                let (xu, yu) = (self.x, self.y);
                let line = &mut self.screen_mut()[yu];
                line.drain(xu..xu + n);
                line.extend(std::iter::repeat_n(b, n));
            }
            'L' => {
                if top <= y && y <= bottom {
                    self.scroll_down(a(0, 1), self.y, self.bottom);
                    self.x = 0;
                }
            }
            'M' => {
                if top <= y && y <= bottom {
                    let n = a(0, 1).min(bottom - y + 1).max(1);
                    let (yu, bu) = (self.y, self.bottom);
                    for _ in 0..n {
                        // deleted lines never go to the scrollback
                        let blank = self.blank_line();
                        let scr = self.screen_mut();
                        scr.remove(yu);
                        scr.insert(bu, blank);
                    }
                    self.x = 0;
                }
            }
            'S' => self.scroll_up(a(0, 1), self.top, self.bottom),
            'T' if private.is_none() => self.scroll_down(a(0, 1), self.top, self.bottom),
            'r' if private.is_none() => {
                let new_top = a(0, 1) - 1;
                let new_bottom = match args.get(1).copied().flatten() {
                    Some(v) if v != 0 => v,
                    _ => rows,
                } - 1;
                if 0 <= new_top && new_top < new_bottom && new_bottom < rows {
                    self.top = new_top as usize;
                    self.bottom = new_bottom as usize;
                    self.x = 0;
                    self.y = if self.origin { self.top } else { 0 };
                }
            }
            'm' => self.sgr(&args),
            'h' | 'l' => self.mode(private, &args, fin == 'h'),
            'n' => match a(0, 0) {
                6 => {
                    let row = y - if self.origin { top } else { 0 };
                    self.replies
                        .extend_from_slice(format!("\x1b[{};{}R", row + 1, x + 1).as_bytes());
                }
                5 => self.replies.extend_from_slice(b"\x1b[0n"),
                _ => {}
            },
            'c' => match private {
                Some('>') => self.replies.extend_from_slice(b"\x1b[>0;0;0c"),
                None => self.replies.extend_from_slice(b"\x1b[?6c"), // VT102
                _ => {}
            },
            's' if private.is_none() => self.save_cursor(),
            'u' if private.is_none() => self.restore(),
            'g' => match a(0, 0) {
                0 => {
                    self.tabs.remove(&self.x);
                }
                3 => self.tabs.clear(),
                _ => {}
            },
            'b' if self.x > 0 => {
                // REP: repeat the previous character
                let prev = self.screen()[self.y][self.x - 1].ch;
                for _ in 0..a(0, 1).min(cols * rows) {
                    self.print(prev);
                }
            }
            _ => {}
        }
    }

    fn mode(&mut self, private: Option<char>, args: &[Option<i64>], on: bool) {
        for &m in args {
            if private == Some('?') {
                match m {
                    Some(1) => self.app_cursor = on,
                    Some(6) => {
                        self.origin = on;
                        self.x = 0;
                        self.y = if on { self.top } else { 0 };
                    }
                    Some(7) => self.autowrap = on,
                    Some(25) => self.cursor_visible = on,
                    Some(47 | 1047 | 1049) => self.alt_screen(on, m == Some(1049)),
                    _ => {}
                }
            } else if m == Some(4) {
                self.insert = on;
            }
        }
    }

    fn alt_screen(&mut self, on: bool, save: bool) {
        if on && self.alt.is_none() {
            if save {
                self.save_cursor();
            }
            self.alt = Some(self.blank_screen());
        } else if !on && self.alt.is_some() {
            self.alt = None;
            if save {
                self.restore();
            }
        }
        self.wrap_pending = false;
    }

    fn sgr(&mut self, args: &[Option<i64>]) {
        let Attr {
            mut fg,
            mut bg,
            mut flags,
        } = self.attr;
        let mut i = 0;
        while i < args.len() {
            let p = args[i].unwrap_or(0);
            match p {
                0 => (fg, bg, flags) = (None, None, 0),
                1 => flags |= BOLD,
                4 => flags |= UNDERLINE,
                7 => flags |= REVERSE,
                22 => flags &= !BOLD,
                24 => flags &= !UNDERLINE,
                27 => flags &= !REVERSE,
                30..=37 => fg = Some(Color::Index((p - 30) as u8)),
                39 => fg = None,
                40..=47 => bg = Some(Color::Index((p - 40) as u8)),
                49 => bg = None,
                90..=97 => fg = Some(Color::Index((p - 90 + 8) as u8)),
                100..=107 => bg = Some(Color::Index((p - 100 + 8) as u8)),
                38 | 48 if i + 1 < args.len() => {
                    let mut color = None;
                    if args[i + 1] == Some(5) && i + 2 < args.len() {
                        let n = args[i + 2];
                        i += 2;
                        // 256 palette entries only
                        color = n.filter(|&n| n <= 255).map(|n| Color::Index(n as u8));
                    } else if args[i + 1] == Some(2) && i + 4 < args.len() {
                        let rgb = &args[i + 2..i + 5];
                        i += 4;
                        if rgb.iter().all(|v| v.is_none_or(|v| v <= 255)) {
                            let c = |v: Option<i64>| v.unwrap_or(0) as u8;
                            color = Some(Color::Rgb(c(rgb[0]), c(rgb[1]), c(rgb[2])));
                        }
                    }
                    // an impossible colour leaves the current one as it is
                    if let Some(c) = color {
                        if p == 38 {
                            fg = Some(c)
                        } else {
                            bg = Some(c)
                        }
                    }
                }
                _ => {}
            }
            i += 1;
        }
        self.attr = Attr { fg, bg, flags };
    }

    // --- keys ----------------------------------------------------------------

    /// Bytes the terminal sends for a key: a single character or a name like "up", "enter".
    pub fn key_bytes(&self, name: &str, ctrl: bool, alt: bool) -> Vec<u8> {
        let arrow = match name {
            "up" => Some(b'A'),
            "down" => Some(b'B'),
            "right" => Some(b'C'),
            "left" => Some(b'D'),
            "home" => Some(b'H'),
            "end" => Some(b'F'),
            _ => None,
        };
        if let Some(c) = arrow {
            let mut out = if self.app_cursor {
                b"\x1bO".to_vec()
            } else {
                b"\x1b[".to_vec()
            };
            out.push(c);
            return out;
        }
        let special: Option<&[u8]> = match name {
            "enter" => Some(b"\r"),
            "backspace" => Some(b"\x7f"),
            "tab" => Some(b"\t"),
            "esc" => Some(b"\x1b"),
            "pgup" => Some(b"\x1b[5~"),
            "pgdn" => Some(b"\x1b[6~"),
            "delete" => Some(b"\x1b[3~"),
            "insert" => Some(b"\x1b[2~"),
            _ => None,
        };
        let mut out = match special {
            Some(s) => s.to_vec(),
            None => {
                let mut out = name.as_bytes().to_vec();
                let mut chars = name.chars();
                if let (true, Some(ch), None) = (ctrl, chars.next(), chars.next()) {
                    let lower: Vec<char> = ch.to_lowercase().collect();
                    match lower.as_slice() {
                        [c @ 'a'..='z'] => out = vec![*c as u8 - 96],
                        [c @ ('@' | '[' | '\\' | ']' | '^' | '_')] => out = vec![*c as u8 & 0x1f],
                        [' '] => out = vec![0],
                        ['?'] => out = vec![0x7f],
                        _ => {}
                    }
                }
                out
            }
        };
        if alt {
            out.insert(0, 0x1b);
        }
        out
    }

    // --- view ----------------------------------------------------------------

    /// The rows to show, scrolled back by `back` lines (0 = live screen; only
    /// the main screen scrolls).
    pub fn view(&self, back: usize) -> Vec<&Line> {
        if back == 0 || self.alt.is_some() {
            return self.screen().iter().collect();
        }
        let back = back.min(self.scrollback.len());
        self.scrollback[self.scrollback.len() - back..]
            .iter()
            .chain(&self.main)
            .take(self.rows)
            .collect()
    }

    /// Plain text of the live screen (tests and debugging).
    pub fn text(&self) -> String {
        self.screen()
            .iter()
            .map(|l| {
                l.iter()
                    .map(|c| c.ch)
                    .collect::<String>()
                    .trim_end()
                    .to_string()
            })
            .collect::<Vec<_>>()
            .join("\n")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prints_wraps_and_scrolls_into_scrollback() {
        let mut v = Vt::new(4, 2, 10);
        v.feed(b"abcdefghij");
        assert_eq!(v.text(), "efgh\nij");
        assert_eq!(v.scrollback.len(), 1);
        assert_eq!(
            v.view(1)[0].iter().map(|c| c.ch).collect::<String>(),
            "abcd"
        );
    }

    #[test]
    fn utf8_split_across_feeds_and_invalid_bytes() {
        let mut v = Vt::new(10, 1, 0);
        v.feed(b"\xc3");
        v.feed(b"\xa9\xff!");
        assert_eq!(v.text(), "é\u{fffd}!");
    }

    #[test]
    fn cursor_report_and_attributes() {
        let mut v = Vt::new(20, 5, 0);
        v.feed(b"\x1b[3;4H\x1b[6n\x1b[c\x1b[1;38;5;200;48;2;1;2;3mX");
        assert_eq!(v.replies, b"\x1b[3;4R\x1b[?6c");
        let c = v.screen()[2][3];
        assert_eq!(c.ch, 'X');
        assert_eq!(
            c.attr,
            Attr {
                fg: Some(Color::Index(200)),
                bg: Some(Color::Rgb(1, 2, 3)),
                flags: BOLD
            }
        );
    }

    #[test]
    fn key_bytes_like_python() {
        let mut v = Vt::new(10, 2, 0);
        assert_eq!(v.key_bytes("up", false, false), b"\x1b[A");
        v.feed(b"\x1b[?1h");
        assert_eq!(v.key_bytes("up", false, false), b"\x1bOA");
        assert_eq!(v.key_bytes("c", true, false), [3]);
        assert_eq!(v.key_bytes("[", true, true), [0x1b, 0x1b]);
        assert_eq!(v.key_bytes("?", true, false), [0x7f]);
        assert_eq!(v.key_bytes("é", false, false), "é".as_bytes());
    }
}
