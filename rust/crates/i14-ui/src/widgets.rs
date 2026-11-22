//! Buttons, a scrolling list, a text field and the on-screen keyboard, as
//! `i14ui/widgets.py`. Instead of calling callbacks, widgets return what
//! happened (clicked, row tapped, key pressed) and the page acts on it.

use std::time::{Duration, Instant};

use crate::canvas::{Canvas, Rgb};
use crate::font::Font;
use crate::input::{Gesture, Key, KeyName};

pub const BG: Rgb = [12, 14, 18];
pub const FG: Rgb = [235, 235, 235];
pub const DIM: Rgb = [140, 145, 155];
pub const ACCENT: Rgb = [90, 160, 255];
pub const PANEL: Rgb = [32, 36, 44];
pub const PANEL_HI: Rgb = [60, 70, 90];
pub const KEY: Rgb = [52, 56, 66];
pub const KEY_HI: Rgb = [100, 110, 130];
pub const KEY_MOD: Rgb = [38, 42, 50];
pub const GREEN: Rgb = [80, 220, 100];
pub const RED: Rgb = [240, 80, 70];
pub const YELLOW: Rgb = [240, 200, 80];
/// Pixels a finger may move and still tap.
pub const TAP_SLOP: i32 = 24;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

pub const fn rect(x: i32, y: i32, w: i32, h: i32) -> Rect {
    Rect { x, y, w, h }
}

impl Rect {
    pub fn contains(&self, x: i32, y: i32) -> bool {
        self.x <= x && x < self.x + self.w && self.y <= y && y < self.y + self.h
    }
}

/// Python's floor division (`//`) for possibly negative numerators.
pub fn floor_div(a: i32, b: i32) -> i32 {
    a.div_euclid(b)
}

pub fn center_text(
    c: &mut Canvas,
    r: Rect,
    text: &str,
    font: &Font,
    color: Rgb,
    bg: Rgb,
    scale: usize,
) {
    let mut text = text;
    let mut tw = c.text_width(text, font, scale);
    if tw > r.w - 8 {
        let n = floor_div(r.w - 8, (font.w * scale) as i32).max(1) as usize;
        text = text.char_indices().nth(n).map_or(text, |(i, _)| &text[..i]);
        tw = (n * font.w * scale) as i32;
    }
    let x = r.x + floor_div(r.w - tw, 2);
    let y = r.y + floor_div(r.h - (font.h * scale) as i32, 2);
    c.text(x, y, text, font, color, bg, scale, None);
}

pub struct Button {
    pub rect: Rect,
    pub label: String,
    pub color: Rgb,
    pub fg: Rgb,
    pub scale: usize,
    pub pressed: bool,
}

impl Button {
    pub fn new(rect: Rect, label: impl Into<String>) -> Self {
        Self {
            rect,
            label: label.into(),
            color: PANEL,
            fg: FG,
            scale: 1,
            pressed: false,
        }
    }

    pub fn color(mut self, c: Rgb) -> Self {
        self.color = c;
        self
    }

    pub fn fg(mut self, c: Rgb) -> Self {
        self.fg = c;
        self
    }

    pub fn scale(mut self, s: usize) -> Self {
        self.scale = s;
        self
    }

    pub fn draw(&self, c: &mut Canvas, font: &Font) {
        let bg = if self.pressed { PANEL_HI } else { self.color };
        let r = self.rect;
        c.fill(r.x, r.y, r.w, r.h, bg);
        center_text(c, r, &self.label, font, self.fg, bg, self.scale);
    }

    /// (handled, clicked): handled is Python's return value; clicked means the action runs.
    pub fn touch(&mut self, ev: Gesture) -> (bool, bool) {
        match ev {
            Gesture::Down(x, y) => {
                self.pressed = self.rect.contains(x, y);
                (self.pressed, false)
            }
            Gesture::Move(x, y) => {
                if self.pressed && !self.rect.contains(x, y) {
                    self.pressed = false;
                }
                (self.pressed, false)
            }
            Gesture::Up(x, y) => {
                let was = std::mem::replace(&mut self.pressed, false);
                if was && self.rect.contains(x, y) {
                    (true, true)
                } else {
                    (false, false)
                }
            }
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Item {
    pub text: String,
    pub sub: String,
    pub color: Rgb,
}

/// Rows of (text, subtext, color); drag scrolls, a tap reports the row index.
pub struct ListView {
    pub rect: Rect,
    pub row_h: i32,
    pub items: Vec<Item>,
    pub offset: i32,
    /// (y, offset, rows unchanged since the finger went down).
    pub start: Option<(i32, i32, bool)>,
    pub pressed: Option<usize>,
}

impl ListView {
    pub fn new(rect: Rect) -> Self {
        Self {
            rect,
            row_h: 100,
            items: Vec::new(),
            offset: 0,
            start: None,
            pressed: None,
        }
    }

    pub fn set_items(&mut self, items: Vec<Item>) {
        if items != self.items {
            // New scan results under a finger: never tap a row that moved or went.
            self.pressed = None;
            if let Some(s) = &mut self.start {
                s.2 = false;
            }
        }
        self.items = items;
        self.offset = self.offset.min(self.max()).max(0);
    }

    fn max(&self) -> i32 {
        (self.items.len() as i32 * self.row_h - self.rect.h).max(0)
    }

    pub fn draw(&self, c: &mut Canvas, font: &Font, small: &Font) {
        let Rect { x, y, w, h } = self.rect;
        c.fill(x, y, w, h, BG);
        let first = floor_div(self.offset, self.row_h).max(0) as usize;
        let end = self
            .items
            .len()
            .min(first + (floor_div(h, self.row_h) + 2).max(0) as usize);
        for i in first..end {
            let top = y + i as i32 * self.row_h - self.offset;
            if top >= y + h {
                break;
            }
            let it = &self.items[i];
            let bg = if self.pressed == Some(i) {
                PANEL_HI
            } else {
                PANEL
            };
            let (vis_top, vis_bot) = (top.max(y), (top + self.row_h - 6).min(y + h));
            if vis_bot <= vis_top {
                continue;
            }
            c.fill(x, vis_top, w, vis_bot - vis_top, bg);
            if top >= y && top + self.row_h <= y + h {
                c.text(
                    x + 20,
                    top + 14,
                    &it.text,
                    font,
                    it.color,
                    bg,
                    1,
                    Some(w - 40),
                );
                if !it.sub.is_empty() {
                    c.text(
                        x + 20,
                        top + 20 + font.h as i32,
                        &it.sub,
                        small,
                        DIM,
                        bg,
                        1,
                        Some(w - 40),
                    );
                }
            }
        }
        if self.items.is_empty() {
            c.text(x + 20, y + 20, "(leeg)", small, DIM, BG, 1, None);
        }
    }

    /// (handled, tapped row).
    pub fn touch(&mut self, ev: Gesture) -> (bool, Option<usize>) {
        let Rect { y, .. } = self.rect;
        match ev {
            Gesture::Down(px, py) => {
                if !self.rect.contains(px, py) {
                    self.start = None;
                    return (false, None);
                }
                self.start = Some((py, self.offset, true));
                let i = floor_div(py - y + self.offset, self.row_h);
                self.pressed = (i >= 0 && (i as usize) < self.items.len()).then_some(i as usize);
                (true, None)
            }
            Gesture::Move(_, py) => {
                let Some((y0, off0, _)) = self.start else {
                    return (false, None);
                };
                if (py - y0).abs() > TAP_SLOP {
                    self.pressed = None;
                    self.offset = (off0 - (py - y0)).min(self.max()).max(0);
                }
                (true, None)
            }
            Gesture::Up(_, py) => {
                let Some((y0, _, same)) = self.start.take() else {
                    return (false, None);
                };
                let i = self.pressed.take();
                let tapped =
                    i.filter(|&i| same && i < self.items.len() && (py - y0).abs() <= TAP_SLOP);
                (true, tapped)
            }
        }
    }
}

pub struct TextField {
    pub rect: Rect,
    pub label: String,
    pub secret: bool,
    pub maxlen: usize,
    pub value: String,
    pub show: bool,
}

/// Python's `str.isprintable()` for one character, close enough for keyboard input.
fn printable(ch: char) -> bool {
    ch == ' ' || !(ch.is_control() || ch.is_whitespace() || ('\u{e000}'..='\u{f8ff}').contains(&ch))
}

impl TextField {
    pub fn new(rect: Rect, label: impl Into<String>, secret: bool, maxlen: usize) -> Self {
        Self {
            rect,
            label: label.into(),
            secret,
            maxlen,
            value: String::new(),
            show: false,
        }
    }

    pub fn key(&mut self, k: Key) -> bool {
        match k.name {
            KeyName::Named("backspace") => {
                self.value.pop();
                true
            }
            KeyName::Char(ch)
                if !k.ctrl && printable(ch) && self.value.chars().count() < self.maxlen =>
            {
                self.value.push(ch);
                true
            }
            _ => false,
        }
    }

    pub fn draw(&self, c: &mut Canvas, font: &Font, small: &Font) -> i32 {
        let Rect { x, y, w, h } = self.rect;
        c.text(x, y, &self.label, small, DIM, BG, 1, None);
        let fy = y + small.h as i32 + 8;
        let fh = h - small.h as i32 - 8;
        c.fill(x, fy, w, fh, PANEL);
        let len = self.value.chars().count();
        let text = if self.secret && !self.show {
            "*".repeat(len)
        } else {
            self.value.clone()
        };
        let n = floor_div(w - 40, font.w as i32).max(0) as usize;
        // Python's text[-0:] is the whole text.
        let shown: String = if len > n && n > 0 {
            text.chars().skip(len - n).collect()
        } else {
            text
        };
        c.text(
            x + 16,
            fy + floor_div(fh - font.h as i32, 2),
            &(shown + "_"),
            font,
            FG,
            PANEL,
            1,
            None,
        )
    }
}

/// On-screen keyboard key: label, output (a character or a name), width in units.
type KeyDef = (Option<&'static str>, Option<&'static str>, f64);

fn chars(s: &'static str) -> Vec<KeyDef> {
    // Each ASCII character of s is a key of width 1 whose label and output are that character.
    (0..s.len())
        .map(|i| (Some(&s[i..i + 1]), Some(&s[i..i + 1]), 1.0))
        .collect()
}

fn layout(name: Layout) -> Vec<Vec<KeyDef>> {
    let k = |l: &'static str, o: &'static str, w: f64| (Some(l), Some(o), w);
    let cat = |a: Vec<KeyDef>, b: Vec<KeyDef>| a.into_iter().chain(b).collect::<Vec<_>>();
    match name {
        Layout::Letters => vec![
            chars("qwertyuiop"),
            cat(vec![(None, None, 0.5)], chars("asdfghjkl")),
            cat(
                cat(vec![k("shift", "shift", 1.5)], chars("zxcvbnm")),
                vec![k("<x", "backspace", 1.5)],
            ),
            vec![
                k("123", "sym", 1.5),
                k(",", ",", 1.0),
                k("", " ", 5.0),
                k(".", ".", 1.0),
                k("enter", "enter", 1.5),
            ],
        ],
        Layout::Sym => vec![
            chars("1234567890"),
            chars("-/:;()$&@\""),
            cat(
                cat(vec![k("#+=", "more", 1.5)], chars(".,?!'*+")),
                vec![k("<x", "backspace", 1.5)],
            ),
            vec![
                k("abc", "abc", 1.5),
                k("_", "_", 1.0),
                k("", " ", 5.0),
                k("=", "=", 1.0),
                k("enter", "enter", 1.5),
            ],
        ],
        Layout::More => vec![
            chars("[]{}#%^*+="),
            chars("_\\|~<>`@$&"),
            cat(
                cat(vec![k("123", "sym", 1.5)], chars(".,?!'\";")),
                vec![k("<x", "backspace", 1.5)],
            ),
            vec![
                k("abc", "abc", 1.5),
                k(":", ":", 1.0),
                k("", " ", 5.0),
                k("/", "/", 1.0),
                k("enter", "enter", 1.5),
            ],
        ],
    }
}

fn terminal_row() -> Vec<KeyDef> {
    let k = |l: &'static str, o: &'static str, w: f64| (Some(l), Some(o), w);
    vec![
        k("esc", "esc", 1.0),
        k("tab", "tab", 1.0),
        k("ctrl", "ctrl", 1.2),
        k("←", "left", 1.0),
        k("↓", "down", 1.0),
        k("↑", "up", 1.0),
        k("→", "right", 1.0),
        k("|", "|", 0.9),
        k("-", "-", 0.9),
        k("/", "/", 1.0),
    ]
}

fn arrow_fallback(label: &str) -> Option<&'static str> {
    Some(match label {
        "←" => "<",
        "↑" => "^",
        "→" => ">",
        "↓" => "v",
        _ => return None,
    })
}

const REPEAT: [&str; 5] = ["backspace", "left", "right", "up", "down"];
const MODIFIERS: [&str; 5] = ["shift", "sym", "abc", "more", "ctrl"];
const REPEAT_DELAY: Duration = Duration::from_millis(450);
const REPEAT_EVERY: Duration = Duration::from_millis(80);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Layout {
    Letters,
    Sym,
    More,
}

/// A laid-out key: drawn rect (with a gap), label, output, touch rect (gap included).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct KeyRect {
    pub rect: Rect,
    pub label: &'static str,
    pub out: &'static str,
    pub hit: Rect,
}

fn single_char(s: &str) -> Option<char> {
    let mut it = s.chars();
    it.next().filter(|_| it.next().is_none())
}

pub struct Keyboard {
    pub width: i32,
    pub bottom: i32,
    pub terminal_row: bool,
    pub layout: Layout,
    /// 0 off, 1 next letter, 2 caps lock.
    pub shift: u8,
    pub ctrl: bool,
    pub active: Option<KeyRect>,
    pub repeat_at: Option<Instant>,
    pub repeated: bool,
    pub keys: Vec<KeyRect>,
}

impl Keyboard {
    pub const ROW_H: i32 = 92;

    pub fn new(width: i32, bottom: i32, terminal_row: bool) -> Self {
        let mut k = Self {
            width,
            bottom,
            terminal_row,
            layout: Layout::Letters,
            shift: 0,
            ctrl: false,
            active: None,
            repeat_at: None,
            repeated: false,
            keys: Vec::new(),
        };
        k.build();
        k
    }

    pub fn height(&self) -> i32 {
        Self::ROW_H * if self.terminal_row { 5 } else { 4 }
    }

    pub fn top(&self) -> i32 {
        self.bottom - self.height()
    }

    fn build(&mut self) {
        let mut rows = layout(self.layout);
        if self.terminal_row {
            rows.insert(0, terminal_row());
        }
        self.keys.clear();
        let mut y = self.top();
        for row in rows {
            let units: f64 = row.iter().fold(0.0, |s, k| s + k.2);
            let unit = self.width as f64 / units;
            let mut x = 0.0f64;
            for (label, out, wu) in row {
                let w = wu * unit;
                if let (Some(label), Some(out)) = (label, out) {
                    // Drawn with a small gap; the gap still belongs to the key.
                    let (x0, x1) = (x as i32, (x + w) as i32);
                    self.keys.push(KeyRect {
                        rect: rect(x0 + 3, y + 4, x1 - x0 - 6, Self::ROW_H - 8),
                        label,
                        out,
                        hit: rect(x0, y, x1 - x0, Self::ROW_H),
                    });
                }
                x += w;
            }
            y += Self::ROW_H;
        }
    }

    pub fn set_terminal_row(&mut self, on: bool) {
        self.terminal_row = on;
        self.build();
    }

    pub fn key_at(&self, x: i32, y: i32) -> Option<KeyRect> {
        self.keys.iter().find(|k| k.hit.contains(x, y)).copied()
    }

    pub fn draw(&self, c: &mut Canvas, font: &Font) {
        c.fill(0, self.top(), self.width, self.height(), BG);
        for k in &self.keys {
            let lit = self
                .active
                .is_some_and(|a| a.out == k.out && a.rect == k.rect)
                || (k.out == "shift" && self.shift != 0)
                || (k.out == "ctrl" && self.ctrl);
            let one = single_char(k.out);
            let modifier = MODIFIERS.contains(&k.out) || k.out == "backspace" || k.out == "enter";
            let bg = if lit {
                KEY_HI
            } else if modifier || (one.is_none() && k.out != " ") {
                KEY_MOD
            } else {
                KEY
            };
            let mut text = k.label.to_string();
            if let Some(fb) = arrow_fallback(k.label) {
                if !font.has(single_char(k.label).unwrap()) {
                    text = fb.to_string();
                }
            }
            if k.out == "shift" && self.shift == 2 {
                text = "CAPS".into();
            } else if one.is_some_and(char::is_alphabetic) && self.shift != 0 {
                text = k.label.to_uppercase();
            }
            c.fill(k.rect.x, k.rect.y, k.rect.w, k.rect.h, bg);
            center_text(c, k.rect, &text, font, FG, bg, 1);
        }
    }

    fn arm(&mut self, k: Option<KeyRect>, now: Instant) {
        self.active = k;
        self.repeated = false;
        self.repeat_at = k
            .filter(|k| REPEAT.contains(&k.out))
            .map(|_| now + REPEAT_DELAY);
    }

    /// (handled, key typed).
    pub fn touch(&mut self, ev: Gesture, now: Instant) -> (bool, Option<Key>) {
        match ev {
            Gesture::Down(x, y) => {
                if y < self.top() {
                    return (false, None);
                }
                let k = self.key_at(x, y);
                self.arm(k, now);
                (true, None)
            }
            Gesture::Move(_, y) if self.active.is_none() => (y >= self.top(), None),
            Gesture::Move(x, y) => {
                let k = self.key_at(x, y);
                if k != self.active {
                    self.arm(k, now);
                }
                (true, None)
            }
            Gesture::Up(_, y) => {
                let k = self.active.take();
                let repeated = std::mem::replace(&mut self.repeated, false);
                self.repeat_at = None;
                let typed = match k {
                    Some(k) if !repeated => self.press(k.out),
                    _ => None,
                };
                (k.is_some() || y >= self.top(), typed)
            }
        }
    }

    /// Key repeat while backspace or an arrow is held.
    pub fn tick(&mut self, now: Instant) -> Option<Key> {
        let k = self.active?;
        if self.repeat_at.is_some_and(|t| now >= t) {
            self.repeat_at = Some(now + REPEAT_EVERY);
            self.repeated = true;
            return self.press(k.out);
        }
        None
    }

    fn press(&mut self, out: &'static str) -> Option<Key> {
        match out {
            "shift" => self.shift = (self.shift + 1) % 3,
            "sym" | "more" | "abc" => {
                self.layout = match out {
                    "sym" => Layout::Sym,
                    "more" => Layout::More,
                    _ => Layout::Letters,
                };
                self.build();
            }
            "ctrl" => self.ctrl = !self.ctrl,
            _ => {
                let name = match single_char(out) {
                    Some(ch) if ch.is_alphabetic() && self.shift != 0 => {
                        if self.shift == 1 {
                            self.shift = 0;
                        }
                        KeyName::Char(ch.to_ascii_uppercase())
                    }
                    Some(ch) => KeyName::Char(ch),
                    None => KeyName::Named(out),
                };
                let ctrl = std::mem::replace(&mut self.ctrl, false);
                return Some(Key {
                    name,
                    ctrl,
                    alt: false,
                });
            }
        }
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn button_clicks_only_when_released_inside() {
        let mut b = Button::new(rect(10, 10, 50, 50), "x");
        assert_eq!(b.touch(Gesture::Down(20, 20)), (true, false));
        assert_eq!(b.touch(Gesture::Up(21, 21)), (true, true));
        b.touch(Gesture::Down(20, 20));
        assert_eq!(b.touch(Gesture::Move(100, 100)), (false, false));
        assert_eq!(b.touch(Gesture::Up(20, 20)), (false, false));
    }

    #[test]
    fn list_tap_and_drag() {
        let mut l = ListView::new(rect(0, 100, 300, 250));
        l.set_items(
            (0..5)
                .map(|i| Item {
                    text: format!("r{i}"),
                    sub: String::new(),
                    color: FG,
                })
                .collect(),
        );
        l.touch(Gesture::Down(50, 150));
        assert_eq!(l.touch(Gesture::Up(50, 150)), (true, Some(0)));
        l.touch(Gesture::Down(50, 300));
        l.touch(Gesture::Move(50, 200));
        assert_eq!(l.offset, 100);
        assert_eq!(l.touch(Gesture::Up(50, 200)), (true, None));
        // rows change under the finger: no tap
        l.touch(Gesture::Down(50, 150));
        l.set_items(vec![]);
        assert_eq!(l.touch(Gesture::Up(50, 150)), (true, None));
    }

    #[test]
    fn keyboard_shift_and_ctrl() {
        let now = Instant::now();
        let mut k = Keyboard::new(720, 1600, true);
        let at = |k: &Keyboard, out: &str| k.keys.iter().find(|r| r.out == out).unwrap().hit;
        let tap = |k: &mut Keyboard, out: &str| {
            let r = at(k, out);
            k.touch(Gesture::Down(r.x + 5, r.y + 5), now);
            k.touch(Gesture::Up(r.x + 5, r.y + 5), now).1
        };
        assert_eq!(tap(&mut k, "shift"), None);
        assert_eq!(tap(&mut k, "a"), Some(Key::plain(KeyName::Char('A'))));
        assert_eq!(tap(&mut k, "a"), Some(Key::plain(KeyName::Char('a'))));
        tap(&mut k, "ctrl");
        assert_eq!(
            tap(&mut k, "c"),
            Some(Key {
                name: KeyName::Char('c'),
                ctrl: true,
                alt: false
            })
        );
        assert_eq!(tap(&mut k, "up"), Some(Key::plain(KeyName::Named("up"))));
        tap(&mut k, "sym");
        assert_eq!(k.layout, Layout::Sym);
    }

    #[test]
    fn keyboard_repeats_held_backspace() {
        let t0 = Instant::now();
        let mut k = Keyboard::new(720, 1600, false);
        let r = k.keys.iter().find(|r| r.out == "backspace").unwrap().hit;
        k.touch(Gesture::Down(r.x + 5, r.y + 5), t0);
        assert_eq!(k.tick(t0 + Duration::from_millis(100)), None);
        assert!(k.tick(t0 + Duration::from_millis(500)).is_some());
        assert_eq!(
            k.touch(Gesture::Up(r.x + 5, r.y + 5), t0).1,
            None,
            "no extra press after repeat"
        );
    }
}
