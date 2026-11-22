//! The i14-ui app: pages and the page stack, as `i14ui/app.py`.
//!
//! Power button: short press = screen on/off; hold 3 s = power off. Touch and
//! keyboards only act while the screen is on (and unlocked, with a PIN).
//! Pages do not call the app; they queue `Action`s that the app carries out
//! once the page's method returns.

use std::io;

use i14_config::{Config, CHARGE_LIMITS, TIMEOUTS};
use i14_sys::LocalTime;

use crate::bt::state::{ACCEPT_RECT, REJECT_RECT};
use crate::bt::Bluetooth;
use crate::canvas::{Canvas, Rgb};
use crate::font::Font;
use crate::input::{Gesture, Key, KeyName};
use crate::status::{address_of, palette, Battery};
use crate::term::Session;
use crate::vt::{Cell, Color, BOLD, REVERSE};
use crate::widgets::{self as w, floor_div, rect, Button, Item, Keyboard, ListView, TextField};
use crate::wifi::{bars, show_ssid};

pub const DATA: &str = "/mnt/data/media/0/i14-linux";
pub const HOLD_SECONDS: f64 = 3.0;
pub const BAR_H: i32 = 80;
pub const TERMINAL_MIN_TIMEOUT: u32 = 300;
const DAYS: [&str; 7] = [
    "maandag",
    "dinsdag",
    "woensdag",
    "donderdag",
    "vrijdag",
    "zaterdag",
    "zondag",
];
const TERM_FG: Rgb = [220, 220, 220];
const BLACK: Rgb = [0, 0, 0];

/// What the pages need from Wi-Fi (implemented by `wifi::Wifi`, faked in tests).
pub trait WifiApi {
    fn enabled(&self) -> bool;
    fn scanning(&self) -> bool;
    fn attempt_active(&self) -> bool;
    fn message(&self) -> String;
    fn set_message(&mut self, m: String);
    fn changed(&self) -> bool;
    fn set_changed(&mut self, c: bool);
    fn scan_results(&self) -> Vec<ScanEntry>;
    fn status(&mut self) -> WifiStatus;
    fn saved(&mut self) -> Vec<SavedNet>;
    fn network_id(&mut self, ssid: &[u8]) -> Option<i64>;
    fn available(&mut self) -> bool;
    fn scan(&mut self) -> io::Result<()>;
    fn set_enabled(&mut self, on: bool) -> io::Result<bool>;
    fn connect(
        &mut self,
        ssid: &[u8],
        sec: &str,
        passphrase: Option<&str>,
        hidden: bool,
    ) -> io::Result<bool>;
    fn prefer(&mut self, ssid: &[u8]) -> io::Result<bool>;
    fn forget(&mut self, ssid: &[u8]) -> io::Result<bool>;
    fn monitor_fd(&mut self) -> Option<i32>;
    fn events(&mut self);
    fn tick(&mut self) -> io::Result<()>;
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ScanEntry {
    pub ssid: Vec<u8>,
    pub signal: i64,
    /// "open", "psk", "sae", "wep" or "eap".
    pub security: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SavedNet {
    pub id: i64,
    pub ssid: Vec<u8>,
    pub priority: i64,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct WifiStatus {
    /// "GEEN_WPA" when wpa_supplicant cannot be reached.
    pub wpa_state: String,
    pub ssid: Option<Vec<u8>>,
    pub ip_address: Option<String>,
}

impl WifiApi for crate::wifi::Wifi {
    fn enabled(&self) -> bool {
        crate::wifi::Wifi::enabled(self)
    }
    fn scanning(&self) -> bool {
        self.scanning
    }
    fn attempt_active(&self) -> bool {
        self.attempt.is_some()
    }
    fn message(&self) -> String {
        self.message.clone()
    }
    fn set_message(&mut self, m: String) {
        self.message = m;
    }
    fn changed(&self) -> bool {
        self.changed
    }
    fn set_changed(&mut self, c: bool) {
        self.changed = c;
    }
    fn scan_results(&self) -> Vec<ScanEntry> {
        self.scan_results
            .iter()
            .map(|r| ScanEntry {
                ssid: r.ssid.clone(),
                signal: r.signal,
                security: r.security.to_string(),
            })
            .collect()
    }
    fn status(&mut self) -> WifiStatus {
        let st = crate::wifi::Wifi::status(self);
        WifiStatus {
            wpa_state: st.get("wpa_state").unwrap_or("").to_string(),
            ip_address: st.get("ip_address").map(str::to_string),
            ssid: st.ssid_bytes,
        }
    }
    fn saved(&mut self) -> Vec<SavedNet> {
        let nets = crate::wifi::Wifi::saved(self).unwrap_or_default();
        nets.into_iter()
            .map(|n| SavedNet {
                id: n.id,
                ssid: n.ssid,
                priority: n.priority,
            })
            .collect()
    }
    fn network_id(&mut self, ssid: &[u8]) -> Option<i64> {
        crate::wifi::Wifi::network_id(self, ssid).ok().flatten()
    }
    fn available(&mut self) -> bool {
        crate::wifi::Wifi::available(self)
    }
    fn scan(&mut self) -> io::Result<()> {
        crate::wifi::Wifi::scan(self)
    }
    fn set_enabled(&mut self, on: bool) -> io::Result<bool> {
        Ok(crate::wifi::Wifi::set_enabled(self, on))
    }
    fn connect(
        &mut self,
        ssid: &[u8],
        sec: &str,
        passphrase: Option<&str>,
        hidden: bool,
    ) -> io::Result<bool> {
        crate::wifi::Wifi::connect(self, ssid, sec, passphrase, hidden)
    }
    fn prefer(&mut self, ssid: &[u8]) -> io::Result<bool> {
        crate::wifi::Wifi::prefer(self, ssid)
    }
    fn forget(&mut self, ssid: &[u8]) -> io::Result<bool> {
        crate::wifi::Wifi::forget(self, ssid)
    }
    fn monitor_fd(&mut self) -> Option<i32> {
        crate::wifi::Wifi::monitor_fd(self)
    }
    fn events(&mut self) {
        crate::wifi::Wifi::events(self)
    }
    fn tick(&mut self) -> io::Result<()> {
        crate::wifi::Wifi::tick(self);
        Ok(())
    }
}

/// The display (the framebuffer `Screen`, faked in tests).
pub trait Display {
    fn show(&mut self, canvas: &Canvas);
    fn power(&mut self, on: bool);
    fn is_on(&self) -> bool;
    fn set_brightness(&mut self, value: i64);
}

/// The world outside the app: clocks, battery, addresses, disk space, commands.
pub trait Env {
    /// CLOCK_MONOTONIC seconds.
    fn monotonic(&self) -> f64;
    fn local_time(&self) -> LocalTime;
    fn battery(&self) -> Battery;
    fn battery_level(&self) -> String;
    fn addresses(&self) -> Vec<(String, String)>;
    fn free(&self, path: &str) -> String;
    fn uptime(&self) -> i64;
    /// Runs `reboot` or `poweroff`.
    fn run(&self, program: &str);
    fn data_dir(&self) -> String;
}

pub struct Fonts {
    pub big: Font,
    pub mid: Font,
    pub small: Font,
    pub term: Font,
}

/// Everything a page may use; the page stack itself lives in `App`.
pub struct Ctx {
    pub width: i32,
    pub height: i32,
    pub fonts: Fonts,
    pub cfg: Config,
    pub wifi: Box<dyn WifiApi>,
    pub bt: Bluetooth,
    pub screen: Box<dyn Display>,
    pub env: Box<dyn Env>,
    pub session: Option<Session>,
    pub dirty: bool,
    pub last_activity: f64,
    actions: Vec<Action>,
}

impl Ctx {
    fn push(&mut self, spec: Spec) {
        self.actions.push(Action::Push(spec));
    }
    fn pop(&mut self) {
        self.actions.push(Action::Pop);
    }
}

/// What a passed PIN check does before the lock page closes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Then {
    /// Settings: remove the PIN.
    RemovePin,
    /// PinSet: the current PIN was given.
    Verified,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Spec {
    Home,
    Terminal,
    Wifi,
    Network {
        ssid: Vec<u8>,
        sec: Option<String>,
    },
    Hidden,
    Bluetooth,
    Settings,
    PinSet,
    Lock {
        then: Option<Then>,
        reason: &'static str,
    },
    Power,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Action {
    Push(Spec),
    Pop,
    /// The PIN was right: run `Then` on the page below, then close the lock page.
    Unlocked(Option<Then>),
    Power(&'static str),
}

fn hhmm(t: &LocalTime) -> String {
    format!("{:02}:{:02}", t.hour, t.minute)
}

/// Python's `str(OSError)`: "[Errno N] text" for an OS error, else the message.
fn py_err(e: &io::Error) -> String {
    let s = e.to_string();
    match (e.raw_os_error(), s.rfind(" (os error ")) {
        (Some(n), Some(i)) => format!("[Errno {n}] {}", &s[..i]),
        _ => s,
    }
}

/// The page base's touch: the keyboard first, then every button; an "up" that a
/// button took ends it. Returns the clicked button and a typed key, if any.
fn base_touch(
    buttons: &mut [Button],
    kb: Option<&mut Keyboard>,
    ev: Gesture,
    now: f64,
) -> (Option<usize>, Option<Key>) {
    if let Some(kb) = kb {
        let (handled, key) = kb.touch(ev, instant(now));
        if handled {
            return (None, key);
        }
    }
    for (i, b) in buttons.iter_mut().enumerate() {
        let (handled, clicked) = b.touch(ev);
        if clicked {
            return (Some(i), None);
        }
        if handled && matches!(ev, Gesture::Up(..)) {
            return (None, None);
        }
    }
    (None, None)
}

/// Buttons first (any that takes the event ends it), as the list pages do.
fn buttons_first(buttons: &mut [Button], ev: Gesture) -> (bool, Option<usize>) {
    for (i, b) in buttons.iter_mut().enumerate() {
        let (handled, clicked) = b.touch(ev);
        if handled {
            return (true, clicked.then_some(i));
        }
    }
    (false, None)
}

fn draw_buttons(buttons: &[Button], c: &mut Canvas, font: &Font) {
    for b in buttons {
        b.draw(c, font);
    }
}

/// Keyboard key-repeat timing runs on `Instant`; this maps the app's monotonic
/// seconds onto one fixed origin so fake clocks in tests work too.
fn instant(secs: f64) -> std::time::Instant {
    use std::sync::OnceLock;
    static ORIGIN: OnceLock<std::time::Instant> = OnceLock::new();
    let origin = *ORIGIN.get_or_init(std::time::Instant::now);
    origin + std::time::Duration::from_secs_f64(secs.max(0.0) + 1.0)
}

// --- pages ---------------------------------------------------------------------

/// Battery, addresses and SSID, refreshed at most every 5 s.
type HomeFacts = (Battery, Vec<(String, String)>, String);

pub struct Home {
    pub buttons: Vec<Button>,
    cache: Option<HomeFacts>,
    cache_at: f64,
}

const HOME_GRID: [(&str, Spec); 5] = [
    ("Terminal", Spec::Terminal),
    ("Wi-Fi", Spec::Wifi),
    ("Bluetooth", Spec::Bluetooth),
    ("Instellingen", Spec::Settings),
    ("Aan/uit", Spec::Power),
];

impl Home {
    fn new() -> Self {
        let buttons = HOME_GRID
            .iter()
            .enumerate()
            .map(|(i, (label, _))| {
                let (col, row) = ((i % 2) as i32, (i / 2) as i32);
                Button::new(rect(30 + col * 340, 900 + row * 200, 320, 170), *label)
            })
            .collect();
        Self {
            buttons,
            cache: None,
            cache_at: 0.0,
        }
    }

    fn click(&mut self, i: usize, cx: &mut Ctx) {
        cx.push(HOME_GRID[i].1.clone());
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let now = cx.env.monotonic();
        if self.cache.is_none() || now - self.cache_at > 5.0 {
            let ssid = wifi_ssid(cx);
            self.cache = Some((cx.env.battery(), cx.env.addresses(), ssid));
            self.cache_at = now;
        }
        let (bat, ip, ssid) = self.cache.as_ref().unwrap();
        let f = &cx.fonts;
        let t = cx.env.local_time();
        c.text(40, BAR_H + 30, &hhmm(&t), &f.big, w::FG, w::BG, 4, None);
        let day = DAYS[t.weekday as usize % 7];
        c.text(
            40,
            BAR_H + 170,
            &format!("{day} {:02}-{:02}-{:04}", t.day, t.month, t.year),
            &f.mid,
            w::DIM,
            w::BG,
            1,
            None,
        );
        let level = bat.cap.trim_start_matches('~');
        let color = if bat.state == "Charging" || bat.state == "Full" {
            w::GREEN
        } else if !level.is_empty()
            && level.bytes().all(|b| b.is_ascii_digit())
            && level.parse::<u64>().is_ok_and(|l| l <= 15)
        {
            w::RED
        } else {
            w::FG
        };
        c.text(
            40,
            BAR_H + 250,
            &format!("Accu {}%", bat.cap),
            &f.big,
            color,
            w::BG,
            2,
            None,
        );
        c.text(
            40,
            BAR_H + 330,
            &format!("{}, {}, {}", bat.word, bat.current, bat.volt),
            &f.mid,
            color,
            w::BG,
            1,
            None,
        );
        c.text(40, BAR_H + 420, "Wi-Fi", &f.mid, w::ACCENT, w::BG, 1, None);
        let wifi_line = if !ssid.is_empty() {
            ssid.as_str()
        } else if !cx.wifi.enabled() {
            "uit"
        } else {
            "niet verbonden"
        };
        c.text(40, BAR_H + 455, wifi_line, &f.mid, w::FG, w::BG, 1, None);
        c.text(
            40,
            BAR_H + 490,
            address_of(ip, "wlan0").unwrap_or(""),
            &f.mid,
            w::FG,
            w::BG,
            1,
            None,
        );
        c.text(40, BAR_H + 560, "USB", &f.mid, w::ACCENT, w::BG, 1, None);
        c.text(
            40,
            BAR_H + 595,
            address_of(ip, "rndis0").unwrap_or("uit"),
            &f.mid,
            w::FG,
            w::BG,
            1,
            None,
        );
        let up = cx.env.uptime();
        let since = format!(
            "aan sinds {} u {:02} m",
            up.div_euclid(3600),
            up.rem_euclid(3600) / 60
        );
        c.text(40, BAR_H + 660, &since, &f.small, w::DIM, w::BG, 1, None);
        c.text(
            40,
            BAR_H + 690,
            "knop kort: scherm uit, 3 s: uitzetten",
            &f.small,
            w::DIM,
            w::BG,
            1,
            None,
        );
        draw_buttons(&self.buttons, c, &f.mid);
    }
}

pub struct TerminalPage {
    pub keyboard: Keyboard,
    pub kb_on: bool,
    pub back: usize,
    pub drag: Option<(i32, usize, bool)>,
    pub toggle: Button,
    /// Python's TerminalPage has an empty `buttons` list; the toggle is separate.
    no_buttons: Vec<Button>,
}

impl TerminalPage {
    fn new(cx: &Ctx) -> Self {
        Self {
            keyboard: Keyboard::new(cx.width, cx.height, true),
            kb_on: true,
            back: 0,
            drag: None,
            toggle: Button::new(rect(cx.width - 250, 8, 150, BAR_H - 16), "verberg"),
            no_buttons: Vec::new(),
        }
    }

    fn area(&self, cx: &Ctx) -> (i32, i32) {
        (
            BAR_H,
            if self.kb_on {
                self.keyboard.top()
            } else {
                cx.height
            },
        )
    }

    pub fn size(&self, cx: &Ctx) -> (usize, usize) {
        let (top, bottom) = self.area(cx);
        let f = &cx.fonts.term;
        (
            floor_div(cx.width - 8, f.w as i32).max(1) as usize,
            floor_div(bottom - top - 4, f.h as i32).max(1) as usize,
        )
    }

    fn show(&mut self, cx: &mut Ctx) {
        let (cols, rows) = self.size(cx);
        match &mut cx.session {
            Some(s) if s.alive() => s.set_size(cols, rows),
            _ => cx.session = Some(Session::new(cols, rows, None, None)),
        }
    }

    pub fn toggle_keyboard(&mut self, cx: &mut Ctx) {
        self.kb_on = !self.kb_on;
        let (cols, rows) = self.size(cx);
        if let Some(s) = &mut cx.session {
            s.set_size(cols, rows);
        }
        cx.dirty = true;
    }

    fn key(&mut self, k: Key, cx: &mut Ctx) {
        let alive = cx.session.as_ref().is_some_and(Session::alive);
        if !alive {
            self.show(cx);
            return;
        }
        self.back = 0;
        let s = cx.session.as_mut().unwrap();
        let name = key_name(&k);
        let bytes = s.vt.key_bytes(&name, k.ctrl, k.alt);
        s.write(&bytes);
        cx.dirty = true;
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) {
        self.toggle.label = if self.kb_on { "verberg" } else { "toetsen" }.into();
        let (handled, clicked) = self.toggle.touch(ev);
        if clicked {
            self.toggle_keyboard(cx);
        }
        if handled {
            return;
        }
        let (top, bottom) = self.area(cx);
        if self.kb_on {
            let (handled, key) = self.keyboard.touch(ev, instant(cx.env.monotonic()));
            if let Some(k) = key {
                self.key(k, cx);
            }
            if handled {
                return;
            }
        }
        match ev {
            Gesture::Down(_, y) if top <= y && y < bottom => {
                self.drag = Some((y, self.back, false))
            }
            Gesture::Move(_, y) if self.drag.is_some() => {
                let (y0, b0, _) = self.drag.unwrap();
                let lines = floor_div(y - y0, cx.fonts.term.h as i32) as i64;
                if (y - y0).abs() > w::TAP_SLOP {
                    let limit = cx.session.as_ref().map_or(0, |s| s.vt.scrollback.len()) as i64;
                    self.back = (b0 as i64 + lines).min(limit).max(0) as usize;
                    self.drag = Some((y0, b0, true));
                    cx.dirty = true;
                }
            }
            Gesture::Up(..) if self.drag.is_some() => {
                let moved = self.drag.take().unwrap().2;
                if !moved {
                    let alive = cx.session.as_ref().is_some_and(Session::alive);
                    if !alive {
                        self.show(cx);
                    } else if !self.kb_on {
                        self.toggle_keyboard(cx);
                    }
                }
            }
            _ => {}
        }
    }

    fn tick(&mut self, cx: &mut Ctx) -> bool {
        if !self.kb_on {
            return false;
        }
        match self.keyboard.tick(instant(cx.env.monotonic())) {
            Some(k) => {
                self.key(k, cx);
                true
            }
            None => false,
        }
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let (top, bottom) = self.area(cx);
        let f = &cx.fonts.term;
        c.fill(0, top, cx.width, bottom - top, BLACK);
        if let Some(s) = &cx.session {
            let vt = &s.vt;
            let lines = vt.view(self.back);
            for (r, line) in lines.iter().enumerate() {
                draw_term_line(c, f, 4, top + 2 + r as i32 * f.h as i32, line);
            }
            if self.back == 0 && vt.cursor_visible && s.alive() && vt.y < lines.len() {
                let (cx_, cy) = (4 + (vt.x * f.w) as i32, top + 2 + (vt.y * f.h) as i32);
                let ch = lines[vt.y][vt.x].ch.to_string();
                c.text(cx_, cy, &ch, f, BLACK, TERM_FG, 1, None);
            }
            if self.back != 0 {
                c.text(
                    cx.width - 200,
                    top + 4,
                    &format!("-{} regels", self.back),
                    &cx.fonts.small,
                    w::YELLOW,
                    BLACK,
                    1,
                    None,
                );
            }
            if !s.alive() {
                let msg = "shell gestopt - tik om opnieuw te starten";
                c.text(
                    10,
                    bottom - 40,
                    msg,
                    &cx.fonts.small,
                    w::YELLOW,
                    BLACK,
                    1,
                    None,
                );
            }
        }
        if !cx.cfg.has_pin() {
            c.text(
                130,
                BAR_H - 24,
                "root, geen pincode",
                &cx.fonts.small,
                w::YELLOW,
                w::PANEL,
                1,
                None,
            );
        }
        self.toggle.label = if self.kb_on { "verberg" } else { "toetsen" }.into();
        self.toggle.draw(c, &cx.fonts.small);
        if self.kb_on {
            self.keyboard.draw(c, &cx.fonts.mid);
        }
    }
}

/// A key's name as the terminal and pages use it: the character or the name.
fn key_name(k: &Key) -> String {
    match k.name {
        KeyName::Char(ch) => ch.to_string(),
        KeyName::Named(n) => n.to_string(),
    }
}

pub fn draw_term_line(c: &mut Canvas, f: &Font, x: i32, y: i32, line: &[Cell]) {
    let color = |col: Option<Color>, default: Rgb| match col {
        None => default,
        Some(Color::Index(i)) => palette(i),
        Some(Color::Rgb(r, g, b)) => [r, g, b],
    };
    for (i, cell) in line.iter().enumerate() {
        let a = cell.attr;
        let mut fg = color(a.fg, TERM_FG);
        let mut bg = color(a.bg, BLACK);
        if a.flags & BOLD != 0 {
            if let Some(Color::Index(n)) = a.fg {
                if n < 8 {
                    fg = palette(n + 8); // bold: the bright colour, before reverse
                }
            }
        }
        if a.flags & REVERSE != 0 {
            std::mem::swap(&mut fg, &mut bg);
        }
        draw_glyph_clipped(c, f, x + (i * f.w) as i32, y, cell.ch, fg, bg);
    }
}

/// One terminal cell; unlike Canvas::text, a glyph partly past the right edge is cut, not dropped.
fn draw_glyph_clipped(c: &mut Canvas, f: &Font, x: i32, y: i32, ch: char, fg: Rgb, bg: Rgb) {
    let g = f.glyph(ch);
    let (on, off) = ([fg[0], fg[1], fg[2], 0xff], [bg[0], bg[1], bg[2], 0xff]);
    for r in 0..f.h as i32 {
        let yy = y + r;
        if yy < 0 || yy >= c.h {
            continue;
        }
        for px in 0..f.w as i32 {
            let xx = x + px;
            if xx < 0 || xx >= c.w {
                continue;
            }
            let s = yy as usize * c.stride + xx as usize * 4;
            c.buf[s..s + 4].copy_from_slice(if g.pixel(px as usize, r as usize) {
                &on
            } else {
                &off
            });
        }
    }
}

fn wifi_ssid(cx: &mut Ctx) -> String {
    let st = cx.wifi.status();
    match (st.wpa_state.as_str(), st.ssid) {
        ("COMPLETED", Some(s)) => show_ssid(&s),
        _ => String::new(),
    }
}

/// (ssid, security, shown name, subtext).
type WifiRow = (Vec<u8>, Option<String>, String, String);

pub struct WifiPage {
    pub buttons: Vec<Button>,
    pub list: ListView,
    pub rows: Vec<WifiRow>,
}

impl WifiPage {
    fn new(cx: &Ctx) -> Self {
        let b = |x: i32, label: &str| Button::new(rect(x, BAR_H + 110, 220, 90), label);
        Self {
            buttons: vec![b(20, "Wi-Fi: aan"), b(250, "Zoeken"), b(480, "Verborgen")],
            list: ListView::new(rect(0, BAR_H + 220, cx.width, cx.height - BAR_H - 220)),
            rows: Vec::new(),
        }
    }

    fn labels(&mut self, cx: &Ctx) {
        self.buttons[0].label = if cx.wifi.enabled() {
            "Wi-Fi: aan"
        } else {
            "Wi-Fi: uit"
        }
        .into();
        self.buttons[1].label = if cx.wifi.scanning() {
            "zoeken..."
        } else {
            "Zoeken"
        }
        .into();
    }

    fn show(&mut self, cx: &mut Ctx) {
        self.refresh(cx);
        if cx.wifi.enabled() && cx.wifi.available() {
            let r = cx.wifi.scan();
            self.safe(r, cx);
        }
    }

    fn safe<T>(&mut self, r: io::Result<T>, cx: &mut Ctx) {
        if let Err(e) = r {
            cx.wifi
                .set_message(format!("wpa_supplicant: {}", py_err(&e)));
        }
        self.refresh(cx);
    }

    fn click(&mut self, i: usize, cx: &mut Ctx) {
        match i {
            0 => {
                let on = !cx.wifi.enabled();
                let r = cx.wifi.set_enabled(on);
                self.safe(r, cx);
            }
            1 => {
                let r = cx.wifi.scan();
                self.safe(r, cx);
            }
            _ => cx.push(Spec::Hidden),
        }
    }

    fn refresh(&mut self, cx: &mut Ctx) {
        let st = cx.wifi.status();
        let current = if st.wpa_state == "COMPLETED" {
            st.ssid
        } else {
            None
        };
        let mut saved: Vec<SavedNet> = Vec::new();
        for n in cx.wifi.saved() {
            // a dict keyed by SSID: first position, last value
            match saved.iter_mut().find(|s| s.ssid == n.ssid) {
                Some(s) => *s = n,
                None => saved.push(n),
            }
        }
        let mut rows: Vec<WifiRow> = Vec::new();
        let mut seen: Vec<Vec<u8>> = Vec::new();
        for r in cx.wifi.scan_results() {
            seen.push(r.ssid.clone());
            let mut tags: Vec<String> = Vec::new();
            if current.as_ref() == Some(&r.ssid) {
                tags.push("verbonden".into());
            } else if saved.iter().any(|s| s.ssid == r.ssid) {
                tags.push("opgeslagen".into());
            }
            let sec = match r.security.as_str() {
                "open" => "open",
                "psk" => "WPA2",
                "sae" => "WPA3",
                "wep" => "WEP",
                _ => "802.1X",
            };
            tags.push(sec.into());
            let n = bars(r.signal);
            tags.push(format!("signaal {}{}", "|".repeat(n), ".".repeat(4 - n)));
            rows.push((
                r.ssid.clone(),
                Some(r.security.clone()),
                show_ssid(&r.ssid),
                tags.join(", "),
            ));
        }
        for n in &saved {
            if !seen.contains(&n.ssid) {
                let tag = if current.as_ref() == Some(&n.ssid) {
                    "verbonden"
                } else {
                    "opgeslagen, niet in de buurt"
                };
                rows.push((n.ssid.clone(), None, show_ssid(&n.ssid), tag.into()));
            }
        }
        let items = rows
            .iter()
            .map(|(_, _, name, sub)| Item {
                text: name.clone(),
                sub: sub.clone(),
                color: if sub.split(',').next().unwrap_or("").contains("verbonden") {
                    w::GREEN
                } else {
                    w::FG
                },
            })
            .collect();
        self.rows = rows;
        self.list.set_items(items);
        cx.wifi.set_changed(false);
        cx.dirty = true;
    }

    fn tick(&mut self, cx: &mut Ctx) -> bool {
        if cx.wifi.changed() {
            self.refresh(cx);
            return true;
        }
        false
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let st = cx.wifi.status();
        let line = match st.wpa_state.as_str() {
            "GEEN_WPA" => "wpa_supplicant draait niet".to_string(),
            "COMPLETED" => format!(
                "verbonden: {}  {}",
                show_ssid(st.ssid.as_deref().unwrap_or(b"")),
                st.ip_address.as_deref().unwrap_or("")
            ),
            _ => if cx.wifi.enabled() {
                "niet verbonden"
            } else {
                "Wi-Fi staat uit"
            }
            .to_string(),
        };
        let f = &cx.fonts;
        c.text(
            20,
            BAR_H + 15,
            &line,
            &f.mid,
            w::FG,
            w::BG,
            1,
            Some(cx.width - 40),
        );
        c.text(
            20,
            BAR_H + 60,
            &cx.wifi.message(),
            &f.small,
            w::YELLOW,
            w::BG,
            1,
            Some(cx.width - 40),
        );
        self.labels(cx);
        draw_buttons(&self.buttons, c, &f.mid);
        self.list.draw(c, &f.mid, &f.small);
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) {
        let (handled, clicked) = buttons_first(&mut self.buttons, ev);
        if let Some(i) = clicked {
            self.click(i, cx);
        }
        if handled {
            return;
        }
        if let (_, Some(i)) = self.list.touch(ev) {
            let (ssid, sec, _, _) = self.rows[i].clone();
            cx.push(Spec::Network { ssid, sec });
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum NetButton {
    ConnectSaved,
    Prefer,
    AskPassword,
    Forget,
    ConnectOpen,
    ToggleShow,
    ConnectPassword,
}

/// One network: connect, type a password, forget, make preferred.
pub struct NetworkPage {
    pub title: String,
    pub ssid: Vec<u8>,
    pub sec: Option<String>,
    pub saved: bool,
    pub field: Option<TextField>,
    pub mode: &'static str,
    pub buttons: Vec<Button>,
    actions: Vec<NetButton>,
    pub keyboard: Option<Keyboard>,
}

impl NetworkPage {
    fn new(ssid: Vec<u8>, sec: Option<String>, cx: &mut Ctx) -> Self {
        let saved = cx.wifi.network_id(&ssid).is_some();
        let mut p = Self {
            title: show_ssid(&ssid),
            ssid,
            sec,
            saved,
            field: None,
            mode: "menu",
            buttons: Vec::new(),
            actions: Vec::new(),
            keyboard: None,
        };
        p.menu(cx);
        p
    }

    fn menu(&mut self, cx: &mut Ctx) {
        let y = BAR_H + 160;
        self.buttons.clear();
        self.actions.clear();
        self.keyboard = None;
        self.mode = "menu";
        let sec_owned = self.sec.clone();
        let sec = sec_owned.as_deref();
        if self.saved {
            self.add(
                Button::new(rect(40, y, 640, 110), "Verbinden"),
                NetButton::ConnectSaved,
            );
            self.add(
                Button::new(rect(40, y + 130, 640, 110), "Voorrang geven"),
                NetButton::Prefer,
            );
            if matches!(sec, None | Some("psk")) {
                self.add(
                    Button::new(rect(40, y + 260, 640, 110), "Ander wachtwoord"),
                    NetButton::AskPassword,
                );
            }
            self.add(
                Button::new(rect(40, y + 390, 640, 110), "Vergeten").fg(w::RED),
                NetButton::Forget,
            );
        } else if sec == Some("open") {
            self.add(
                Button::new(rect(40, y, 640, 110), "Verbinden (open netwerk)"),
                NetButton::ConnectOpen,
            );
        } else if sec == Some("psk") {
            self.ask_password(cx);
        }
        cx.dirty = true;
    }

    fn add(&mut self, b: Button, a: NetButton) {
        self.buttons.push(b);
        self.actions.push(a);
    }

    fn ask_password(&mut self, cx: &mut Ctx) {
        self.mode = "password";
        self.field = Some(TextField::new(
            rect(40, BAR_H + 150, 640, 120),
            "Wachtwoord",
            true,
            63,
        ));
        let kb = Keyboard::new(cx.width, cx.height, false);
        let top = kb.top();
        self.keyboard = Some(kb);
        self.buttons.clear();
        self.actions.clear();
        self.add(
            Button::new(rect(40, top - 130, 300, 100), "toon"),
            NetButton::ToggleShow,
        );
        self.add(
            Button::new(rect(380, top - 130, 300, 100), "Verbinden").color(w::PANEL_HI),
            NetButton::ConnectPassword,
        );
        cx.dirty = true;
    }

    fn labels(&mut self) {
        if let Some(i) = self
            .actions
            .iter()
            .position(|a| *a == NetButton::ToggleShow)
        {
            let show = self.field.as_ref().is_some_and(|f| f.show);
            self.buttons[i].label = if show { "verberg" } else { "toon" }.into();
        }
    }

    fn key(&mut self, k: Key, cx: &mut Ctx) {
        let Some(field) = &mut self.field else { return };
        if k.name == KeyName::Named("enter") {
            self.connect_password(cx);
        } else {
            field.key(k);
        }
        cx.dirty = true;
    }

    fn done(&mut self, r: io::Result<bool>, cx: &mut Ctx) -> bool {
        match r {
            Ok(ok) => ok,
            Err(e) => {
                cx.wifi
                    .set_message(format!("wpa_supplicant: {}", py_err(&e)));
                false
            }
        }
    }

    fn connect_password(&mut self, cx: &mut Ctx) {
        let pass = self
            .field
            .as_ref()
            .map(|f| f.value.clone())
            .unwrap_or_default();
        let r = cx.wifi.connect(&self.ssid, "psk", Some(&pass), false);
        if self.done(r, cx) {
            cx.pop();
        }
    }

    fn click(&mut self, i: usize, cx: &mut Ctx) {
        match self.actions[i] {
            NetButton::ConnectSaved => {
                let sec = self.sec.clone().unwrap_or_else(|| "psk".into());
                let r = cx.wifi.connect(&self.ssid, &sec, None, false);
                if self.done(r, cx) {
                    cx.pop();
                }
            }
            NetButton::ConnectOpen => {
                let r = cx.wifi.connect(&self.ssid, "open", None, false);
                if self.done(r, cx) {
                    cx.pop();
                }
            }
            NetButton::ConnectPassword => self.connect_password(cx),
            NetButton::Prefer => {
                let r = cx.wifi.prefer(&self.ssid);
                self.done(r, cx);
            }
            NetButton::Forget => {
                let r = cx.wifi.forget(&self.ssid);
                if self.done(r, cx) {
                    cx.pop();
                }
            }
            NetButton::AskPassword => self.ask_password(cx),
            NetButton::ToggleShow => {
                if let Some(f) = &mut self.field {
                    f.show = !f.show;
                }
            }
        }
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let f = &cx.fonts;
        let sec = match self.sec.as_deref() {
            Some("open") => "open netwerk",
            Some("psk") => "WPA2 (wachtwoord)",
            Some("sae") => "WPA3: niet ondersteund",
            Some("wep") => "WEP: niet ondersteund",
            Some("eap") => "802.1X: niet ondersteund",
            None => "niet in de buurt",
            Some(_) => "",
        };
        c.text(
            40,
            BAR_H + 20,
            &self.title,
            &f.mid,
            w::FG,
            w::BG,
            1,
            Some(cx.width - 80),
        );
        let line = format!("{sec}{}", if self.saved { ", opgeslagen" } else { "" });
        c.text(40, BAR_H + 60, &line, &f.small, w::DIM, w::BG, 1, None);
        c.text(
            40,
            BAR_H + 95,
            &cx.wifi.message(),
            &f.small,
            w::YELLOW,
            w::BG,
            1,
            Some(cx.width - 80),
        );
        if !self.saved && self.mode == "menu" {
            c.text(
                40,
                BAR_H + 700,
                "Let op: op elk netwerk zijn SSH (alleen met sleutel)",
                &f.small,
                w::DIM,
                w::BG,
                1,
                None,
            );
            c.text(
                40,
                BAR_H + 730,
                "en de DNS-server bereikbaar voor andere apparaten.",
                &f.small,
                w::DIM,
                w::BG,
                1,
                None,
            );
        }
        if self.mode == "password" {
            let note = "Let op: SSH (sleutel) en DNS zijn bereikbaar op dit netwerk.";
            c.text(
                40,
                BAR_H + 290,
                note,
                &f.small,
                w::DIM,
                w::BG,
                1,
                Some(cx.width - 80),
            );
            if let Some(field) = &self.field {
                field.draw(c, &f.mid, &f.small);
            }
            if let Some(kb) = &self.keyboard {
                kb.draw(c, &f.mid);
            }
        }
        self.labels();
        draw_buttons(&self.buttons, c, &f.mid);
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) {
        let (clicked, key) = base_touch(
            &mut self.buttons,
            self.keyboard.as_mut(),
            ev,
            cx.env.monotonic(),
        );
        if let Some(k) = key {
            self.key(k, cx);
        }
        if let Some(i) = clicked {
            self.click(i, cx);
        }
    }

    fn tick(&mut self, cx: &mut Ctx) -> bool {
        let now = instant(cx.env.monotonic());
        match self.keyboard.as_mut().and_then(|k| k.tick(now)) {
            Some(k) => {
                self.key(k, cx);
                true
            }
            None => false,
        }
    }
}

pub struct HiddenPage {
    pub name: TextField,
    pub pw: TextField,
    /// false: the name field has the focus, true: the password field.
    pub focus_pw: bool,
    pub keyboard: Keyboard,
    pub buttons: Vec<Button>,
}

impl HiddenPage {
    fn new(cx: &Ctx) -> Self {
        let keyboard = Keyboard::new(cx.width, cx.height, false);
        let top = keyboard.top();
        Self {
            name: TextField::new(
                rect(40, BAR_H + 40, 640, 110),
                "Netwerknaam (SSID)",
                false,
                32,
            ),
            pw: TextField::new(
                rect(40, BAR_H + 180, 640, 110),
                "Wachtwoord (leeg = open netwerk)",
                true,
                63,
            ),
            focus_pw: false,
            keyboard,
            buttons: vec![
                Button::new(rect(40, top - 120, 640, 100), "Verbinden").color(w::PANEL_HI)
            ],
        }
    }

    fn key(&mut self, k: Key, cx: &mut Ctx) {
        if k.name == KeyName::Named("enter") {
            if !self.focus_pw {
                self.focus_pw = true;
            } else {
                self.connect(cx);
            }
        } else if self.focus_pw {
            self.pw.key(k);
        } else {
            self.name.key(k);
        }
        cx.dirty = true;
    }

    fn connect(&mut self, cx: &mut Ctx) {
        let ssid = self.name.value.as_bytes().to_vec();
        if ssid.is_empty() {
            cx.wifi.set_message("vul een netwerknaam in".into());
            return;
        }
        let sec = if self.pw.value.is_empty() {
            "open"
        } else {
            "psk"
        };
        let pass = (!self.pw.value.is_empty()).then_some(self.pw.value.as_str());
        let ok = match cx.wifi.connect(&ssid, sec, pass, true) {
            Ok(ok) => ok,
            Err(e) => {
                cx.wifi
                    .set_message(format!("wpa_supplicant: {}", py_err(&e)));
                false
            }
        };
        if ok {
            cx.pop();
        }
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) {
        if let Gesture::Down(x, y) = ev {
            for (i, f) in [&self.name, &self.pw].into_iter().enumerate() {
                let r = f.rect;
                if r.contains(x, y) {
                    self.focus_pw = i == 1;
                    cx.dirty = true;
                    return;
                }
            }
        }
        let (clicked, key) = base_touch(
            &mut self.buttons,
            Some(&mut self.keyboard),
            ev,
            cx.env.monotonic(),
        );
        if let Some(k) = key {
            self.key(k, cx);
        }
        if clicked.is_some() {
            self.connect(cx);
        }
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let f = &cx.fonts;
        for (i, field) in [&self.name, &self.pw].into_iter().enumerate() {
            field.draw(c, &f.mid, &f.small);
            if (i == 1) == self.focus_pw {
                let r = field.rect;
                c.fill(r.x, r.y + r.h, r.w, 4, w::ACCENT);
            }
        }
        c.text(
            40,
            BAR_H + 320,
            &cx.wifi.message(),
            &f.small,
            w::YELLOW,
            w::BG,
            1,
            Some(cx.width - 80),
        );
        self.keyboard.draw(c, &f.mid);
        draw_buttons(&self.buttons, c, &f.mid);
    }

    fn tick(&mut self, cx: &mut Ctx) -> bool {
        match self.keyboard.tick(instant(cx.env.monotonic())) {
            Some(k) => {
                self.key(k, cx);
                true
            }
            None => false,
        }
    }
}

/// A row of the Bluetooth list.
#[derive(Clone, Debug, PartialEq, Eq)]
enum BtRow {
    Header,
    Paired(String),
    Found(String, &'static str),
}

/// A question on the Bluetooth page (the pairing code itself is the app-wide dialog).
#[derive(Clone, Debug, PartialEq, Eq)]
enum BtAsk {
    /// a paired device was tapped: connect, forget or cancel
    Paired(String),
    Forget(String),
    Pair(String),
}

const BT_LIST_Y: i32 = BAR_H + 360;

pub struct BluetoothPage {
    pub buttons: Vec<Button>,
    pub list: ListView,
    rows: Vec<BtRow>,
    ask: Option<BtAsk>,
    /// seconds of visibility shown, to redraw the countdown
    shown_left: Option<i64>,
}

impl BluetoothPage {
    fn new(cx: &Ctx) -> Self {
        Self {
            buttons: Self::main_buttons(),
            list: ListView::new(rect(0, BT_LIST_Y, cx.width, cx.height - BT_LIST_Y)),
            rows: Vec::new(),
            ask: None,
            shown_left: None,
        }
    }

    fn main_buttons() -> Vec<Button> {
        vec![
            Button::new(rect(20, BAR_H + 150, 330, 90), "Bluetooth: uit"),
            Button::new(rect(370, BAR_H + 150, 330, 90), "Zoeken"),
            Button::new(rect(20, BAR_H + 250, 680, 90), "Zichtbaar (2 min)"),
        ]
    }

    fn ask_buttons(labels: &[&str]) -> Vec<Button> {
        let n = labels.len() as i32;
        let w = (640 - (n - 1) * 20) / n;
        labels
            .iter()
            .enumerate()
            .map(|(i, l)| Button::new(rect(40 + i as i32 * (w + 20), BT_LIST_Y + 200, w, 120), *l))
            .collect()
    }

    fn open(&mut self, ask: Option<BtAsk>) {
        self.buttons = match &ask {
            Some(BtAsk::Paired(_)) => Self::ask_buttons(&["Verbinden", "Vergeten", "Annuleren"]),
            Some(BtAsk::Forget(_)) | Some(BtAsk::Pair(_)) => Self::ask_buttons(&["Ja", "Nee"]),
            None => Self::main_buttons(),
        };
        self.ask = ask;
        self.list.pressed = None;
        self.list.start = None;
    }

    fn visible_left(cx: &Ctx) -> Option<i64> {
        let now = (cx.bt.clock)();
        cx.bt
            .visible_until
            .map(|u| (u - now).max(0.0).ceil() as i64)
    }

    fn labels(&mut self, cx: &Ctx) {
        if self.ask.is_some() {
            return;
        }
        self.buttons[0].label = if cx.bt.on() {
            "Bluetooth: aan"
        } else {
            "Bluetooth: uit"
        }
        .into();
        self.buttons[1].label = if cx.bt.phase.is_some() {
            "zoeken..."
        } else {
            "Zoeken"
        }
        .into();
        self.buttons[2].label = match Self::visible_left(cx) {
            Some(s) => format!("Zichtbaar: nog {s} s (stoppen)"),
            None => "Zichtbaar (2 min)".into(),
        };
    }

    fn click(&mut self, i: usize, cx: &mut Ctx) {
        if i == 0 {
            let on = !cx.bt.on();
            cx.bt.power(on);
        } else if i == 1 {
            if cx.bt.phase.is_none() {
                cx.bt.search();
            }
        } else {
            let v = !cx.bt.visible();
            cx.bt.set_visible(v);
        }
    }

    fn answer(&mut self, i: usize, cx: &mut Ctx) {
        let next = match (self.ask.clone(), i) {
            (Some(BtAsk::Paired(a)), 0) | (Some(BtAsk::Pair(a)), 0) => {
                cx.bt.pair(&a);
                None
            }
            (Some(BtAsk::Paired(a)), 1) => Some(BtAsk::Forget(a)),
            (Some(BtAsk::Forget(a)), 0) => {
                cx.bt.forget(&a);
                None
            }
            _ => None,
        };
        self.open(next);
        cx.bt.changed = true;
    }

    fn row(&mut self, r: usize, cx: &mut Ctx) {
        match self.rows.get(r).cloned() {
            Some(BtRow::Paired(a)) => self.open(Some(BtAsk::Paired(a))),
            Some(BtRow::Found(a, "klassiek")) => self.open(Some(BtAsk::Pair(a))),
            Some(BtRow::Found(..)) => {
                cx.bt.message = "BLE-koppelen wordt niet ondersteund".into();
                cx.bt.changed = true;
            }
            _ => {}
        }
    }

    fn tick(&mut self, cx: &mut Ctx) -> bool {
        let left = Self::visible_left(cx);
        let countdown = left != self.shown_left;
        self.shown_left = left;
        if !cx.bt.changed {
            return countdown;
        }
        cx.bt.changed = false;
        let mut items = Vec::new();
        let mut rows = Vec::new();
        let paired = cx.bt.paired();
        let found: Vec<_> = cx
            .bt
            .listing()
            .into_iter()
            .filter(|(a, ..)| !paired.iter().any(|(p, ..)| p == a))
            .collect();
        if !paired.is_empty() {
            items.push(Item {
                text: "Gekoppeld".into(),
                sub: String::new(),
                color: w::DIM,
            });
            rows.push(BtRow::Header);
            for (addr, name, connected) in paired {
                items.push(Item {
                    text: name,
                    sub: if connected {
                        format!("{addr}  verbonden")
                    } else {
                        addr.clone()
                    },
                    color: w::FG,
                });
                rows.push(BtRow::Paired(addr));
            }
            if !found.is_empty() {
                items.push(Item {
                    text: "Gevonden".into(),
                    sub: String::new(),
                    color: w::DIM,
                });
                rows.push(BtRow::Header);
            }
        }
        for (addr, name, rssi, kind) in found {
            items.push(Item {
                text: if name.is_empty() { addr.clone() } else { name },
                sub: format!("{addr}  {kind}")
                    + &rssi.map_or(String::new(), |r| format!("  {r} dBm")),
                color: w::FG,
            });
            rows.push(BtRow::Found(addr, kind));
        }
        self.list.set_items(items);
        self.rows = rows;
        // a device that went away (forgotten elsewhere) closes its question
        if let Some(BtAsk::Paired(a) | BtAsk::Forget(a)) = &self.ask {
            if cx.bt.keys.get(a).is_none() {
                self.open(None);
            }
        }
        true
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let f = &cx.fonts;
        let b = &cx.bt;
        let line = if b.on() && !b.address.is_empty() {
            format!("aan, adres {}", b.address)
        } else if b.on() {
            "aan".into()
        } else {
            "uit".into()
        };
        c.text(20, BAR_H + 15, &line, &f.mid, w::FG, w::BG, 1, None);
        c.text(
            20,
            BAR_H + 55,
            &b.message,
            &f.small,
            w::YELLOW,
            w::BG,
            1,
            Some(cx.width - 40),
        );
        let info = if b.visible() {
            "zichtbaar als I14: andere apparaten kunnen nu koppelen".to_string()
        } else if !b.conns.is_empty() {
            let names: Vec<String> = b.conns.iter().map(|c| b.name_of(&c.addr)).collect();
            format!("verbonden: {}", names.join(", "))
        } else {
            String::new()
        };
        c.text(
            20,
            BAR_H + 90,
            &info,
            &f.small,
            w::DIM,
            w::BG,
            1,
            Some(cx.width - 40),
        );
        self.labels(cx);
        let f = &cx.fonts;
        self.list.draw(c, &f.mid, &f.small);
        if let Some(ask) = &self.ask {
            let (title, sub) = match ask {
                BtAsk::Paired(a) => (cx.bt.name_of(a), a.clone()),
                BtAsk::Forget(a) => (format!("{} vergeten?", cx.bt.name_of(a)), a.clone()),
                BtAsk::Pair(a) => (format!("Koppelen met {}?", cx.bt.name_of(a)), a.clone()),
            };
            c.fill(20, BT_LIST_Y, cx.width - 40, 360, w::PANEL);
            c.text(
                40,
                BT_LIST_Y + 40,
                &title,
                &f.mid,
                w::FG,
                w::PANEL,
                1,
                Some(cx.width - 80),
            );
            c.text(
                40,
                BT_LIST_Y + 100,
                &sub,
                &f.small,
                w::DIM,
                w::PANEL,
                1,
                Some(cx.width - 80),
            );
        }
        draw_buttons(&self.buttons, c, &f.mid);
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) {
        let (handled, clicked) = buttons_first(&mut self.buttons, ev);
        if let Some(i) = clicked {
            if self.ask.is_some() {
                self.answer(i, cx);
            } else {
                self.click(i, cx);
            }
        }
        if !handled && self.ask.is_none() {
            if let (_, Some(r)) = self.list.touch(ev) {
                self.row(r, cx);
            }
        }
    }
}

pub struct SettingsPage {
    pub buttons: Vec<Button>,
    pub message: String,
}

impl SettingsPage {
    fn new() -> Self {
        let b = |y: i32, label: &str| Button::new(rect(40, BAR_H + y, 640, 100), label);
        Self {
            buttons: vec![
                Button::new(rect(40, BAR_H + 90, 150, 100), "-").scale(2),
                Button::new(rect(530, BAR_H + 90, 150, 100), "+").scale(2),
                b(300, ""),
                b(470, ""),
                b(590, "Pincode verwijderen"),
                b(1040, ""),
                b(1160, ""),
            ],
            message: String::new(),
        }
    }

    pub fn labels(&mut self, cx: &Ctx) {
        let t = cx.cfg.data.timeout;
        let text = if t == 0 {
            "nooit".to_string()
        } else if t < 60 {
            format!("{t} s")
        } else {
            format!("{} min", t / 60)
        };
        self.buttons[2].label = format!("Scherm uit na: {text}");
        self.buttons[3].label = if cx.cfg.has_pin() {
            "Pincode wijzigen"
        } else {
            "Pincode instellen"
        }
        .into();
        self.buttons[5].label = format!(
            "Laadgrens: {}",
            if cx.cfg.data.charge_limit_enabled {
                "aan"
            } else {
                "uit"
            }
        );
        self.buttons[6].label = format!("Stoppen bij: ~{}%", cx.cfg.data.charge_limit);
    }

    fn save_charge(&mut self, cx: &mut Ctx, change: impl FnOnce(&mut i14_config::Settings)) {
        self.message = if cx.cfg.update(change) {
            String::new()
        } else {
            "Opslaan mislukt; instelling niet gewijzigd.".into()
        };
    }

    fn brightness(&mut self, delta: i64, cx: &mut Ctx) {
        let value = (cx.cfg.data.brightness as i64 + delta).clamp(5, 255);
        cx.cfg.update(|s| s.brightness = value as u8);
        cx.screen.set_brightness(value);
    }

    fn click(&mut self, i: usize, cx: &mut Ctx) {
        match i {
            0 => self.brightness(-25, cx),
            1 => self.brightness(25, cx),
            2 => {
                let next = next_in(&TIMEOUTS, cx.cfg.data.timeout);
                cx.cfg.update(|s| s.timeout = next);
            }
            3 => cx.push(Spec::PinSet),
            4 => {
                if cx.cfg.has_pin() && !cx.cfg.damaged {
                    cx.push(Spec::Lock {
                        then: Some(Then::RemovePin),
                        reason: "huidige pincode",
                    });
                }
            }
            5 => {
                let on = !cx.cfg.data.charge_limit_enabled;
                self.save_charge(cx, |s| s.charge_limit_enabled = on);
            }
            _ => {
                let next = next_in(&CHARGE_LIMITS, cx.cfg.data.charge_limit);
                self.save_charge(cx, |s| s.charge_limit = next);
            }
        }
    }

    fn remove_pin(&mut self, cx: &mut Ctx) {
        if !cx.cfg.set_pin(None) {
            self.message = "Opslaan mislukt; pincode blijft.".into();
        }
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let f = &cx.fonts;
        c.text(
            40,
            BAR_H + 30,
            "Helderheid",
            &f.small,
            w::DIM,
            w::BG,
            1,
            None,
        );
        let value = cx.cfg.data.brightness as i32;
        c.fill(210, BAR_H + 125, 300, 30, w::PANEL);
        c.fill(210, BAR_H + 125, 300 * value / 255, 30, w::ACCENT);
        c.text(40, BAR_H + 250, "Scherm", &f.small, w::DIM, w::BG, 1, None);
        let lock = format!(
            "Vergrendeling: {}",
            if cx.cfg.has_pin() {
                "pincode aan"
            } else {
                "geen pincode"
            }
        );
        c.text(40, BAR_H + 430, &lock, &f.small, w::DIM, w::BG, 1, None);
        c.text(40, BAR_H + 740, "Opslag", &f.small, w::DIM, w::BG, 1, None);
        c.text(
            40,
            BAR_H + 780,
            &format!("Linux: {}", cx.env.free("/")),
            &f.mid,
            w::FG,
            w::BG,
            1,
            None,
        );
        c.text(
            40,
            BAR_H + 820,
            &format!("userdata: {}", cx.env.free("/mnt/data")),
            &f.mid,
            w::FG,
            w::BG,
            1,
            None,
        );
        c.text(
            40,
            BAR_H + 900,
            "Zonder pincode kan iedereen met de telefoon",
            &f.small,
            w::DIM,
            w::BG,
            1,
            None,
        );
        c.text(
            40,
            BAR_H + 930,
            "de terminal (root) gebruiken.",
            &f.small,
            w::DIM,
            w::BG,
            1,
            None,
        );
        c.text(
            40,
            BAR_H + 1000,
            "Accu (regeling iedere minuut)",
            &f.small,
            w::DIM,
            w::BG,
            1,
            None,
        );
        let limit = cx.cfg.data.charge_limit as i32;
        let band = format!(
            "Schatting op rustspanning; band ~{}-{}%.",
            limit - 10,
            limit
        );
        c.text(40, BAR_H + 1290, &band, &f.small, w::DIM, w::BG, 1, None);
        c.text(
            40,
            BAR_H + 1330,
            &self.message,
            &f.small,
            w::YELLOW,
            w::BG,
            1,
            Some(cx.width - 80),
        );
        self.labels(cx);
        draw_buttons(&self.buttons, c, &f.mid);
    }
}

fn next_in<T: PartialEq + Copy>(list: &[T], current: T) -> T {
    let i = list.iter().position(|&v| v == current).unwrap_or(0);
    list[(i + 1) % list.len()]
}

/// Digits 0-9, delete and OK.
pub struct PinPad {
    pub digits: String,
    pub buttons: Vec<Button>,
}

const PIN_LABELS: [&str; 12] = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "<x", "0", "OK"];

impl PinPad {
    fn new() -> Self {
        let buttons = PIN_LABELS
            .iter()
            .enumerate()
            .map(|(i, l)| {
                let (col, row) = ((i % 3) as i32, (i / 3) as i32);
                Button::new(rect(60 + col * 210, 640 + row * 200, 180, 170), *l).scale(2)
            })
            .collect();
        Self {
            digits: String::new(),
            buttons,
        }
    }

    /// A press; returns the digits when OK was pressed.
    fn press(&mut self, label: &str, cx: &mut Ctx) -> Option<String> {
        cx.dirty = true;
        match label {
            "<x" => {
                self.digits.pop();
            }
            "OK" => return Some(self.digits.clone()),
            _ if self.digits.chars().count() < 8 => self.digits.push_str(label),
            _ => {}
        }
        None
    }

    fn key(&mut self, k: Key, cx: &mut Ctx) -> Option<String> {
        match k.name {
            KeyName::Char(c) if c.is_ascii_digit() => self.press(&c.to_string(), cx),
            KeyName::Named("backspace") => self.press("<x", cx),
            KeyName::Named("enter") => self.press("OK", cx),
            _ => None,
        }
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) -> Option<String> {
        match base_touch(&mut self.buttons, None, ev, 0.0) {
            (Some(i), _) => self.press(PIN_LABELS[i], cx),
            _ => None,
        }
    }

    fn draw(&self, c: &mut Canvas, cx: &Ctx, prompt: &str, note: &str) {
        let f = &cx.fonts;
        c.text(60, BAR_H + 120, prompt, &f.mid, w::FG, w::BG, 1, None);
        c.text(
            60,
            BAR_H + 220,
            &"* ".repeat(self.digits.chars().count()),
            &f.big,
            w::FG,
            w::BG,
            2,
            None,
        );
        c.text(
            60,
            BAR_H + 360,
            note,
            &f.small,
            w::YELLOW,
            w::BG,
            1,
            Some(cx.width - 120),
        );
        draw_buttons(&self.buttons, c, &f.mid);
    }
}

pub struct LockPage {
    pub pad: PinPad,
    pub then: Option<Then>,
    pub reason: &'static str,
    pub fails: u32,
    pub wait_until: f64,
    pub note: String,
}

impl LockPage {
    fn new(then: Option<Then>, reason: &'static str) -> Self {
        Self {
            pad: PinPad::new(),
            then,
            reason,
            fails: 0,
            wait_until: 0.0,
            note: String::new(),
        }
    }

    fn done(&mut self, pin: String, cx: &mut Ctx) {
        let now = cx.env.monotonic();
        if now < self.wait_until {
            self.note = format!("wacht nog {} s", (self.wait_until - now) as i64 + 1);
            return;
        }
        if cx.cfg.check_pin(&pin) {
            cx.actions.push(Action::Unlocked(self.then));
            return;
        }
        self.pad.digits.clear();
        self.fails += 1;
        if self.fails >= 5 {
            self.wait_until = now + 30.0 * 2f64.powi((self.fails - 5).min(5) as i32);
            self.note = format!(
                "{} keer fout: wacht {} s",
                self.fails,
                (self.wait_until - now) as i64
            );
        } else {
            self.note = "fout".into();
        }
    }

    fn draw(&self, c: &mut Canvas, cx: &Ctx) {
        let note = if cx.cfg.damaged {
            "ui.json beschadigd: herstel via SSH/USB"
        } else {
            &self.note
        };
        self.pad
            .draw(c, cx, &format!("Voer de {} in", self.reason), note);
    }
}

pub struct PinSetPage {
    pub pad: PinPad,
    pub first: Option<String>,
    pub note: String,
    pub verified: bool,
    pub asked: bool,
}

impl PinSetPage {
    fn new(cx: &Ctx) -> Self {
        // changing it: the current PIN first
        Self {
            pad: PinPad::new(),
            first: None,
            note: String::new(),
            verified: !cx.cfg.has_pin(),
            asked: false,
        }
    }

    fn show(&mut self, cx: &mut Ctx) {
        if self.verified {
            return;
        }
        if self.asked {
            cx.pop(); // came back from the PIN check without passing it
            return;
        }
        self.asked = true;
        cx.push(Spec::Lock {
            then: Some(Then::Verified),
            reason: "huidige pincode",
        });
    }

    fn done(&mut self, pin: String, cx: &mut Ctx) {
        let n = pin.chars().count();
        if !(4..=8).contains(&n) {
            self.note = "4 tot 8 cijfers".into();
            self.pad.digits.clear();
        } else if self.first.is_none() {
            self.first = Some(pin);
            self.pad.digits.clear();
            self.note.clear();
        } else if self.first.as_deref() != Some(pin.as_str()) {
            self.first = None;
            self.pad.digits.clear();
            self.note = "niet gelijk, opnieuw".into();
        } else if cx.cfg.set_pin(Some(&pin)) {
            cx.pop();
        } else {
            self.first = None;
            self.pad.digits.clear();
            self.note = "opslaan mislukt; pincode niet gewijzigd".into();
        }
    }

    fn draw(&self, c: &mut Canvas, cx: &Ctx) {
        let prompt = if self.first.is_none() {
            "Nieuwe pincode"
        } else {
            "Nogmaals ter controle"
        };
        self.pad.draw(c, cx, prompt, &self.note);
    }
}

pub struct PowerPage {
    pub title: String,
    pub confirm: Option<&'static str>,
    pub buttons: Vec<Button>,
}

const POWER_ACTIONS: [(&str, &str); 3] = [
    ("reboot", "Herstarten"),
    ("poweroff", "Uitschakelen"),
    ("rescue", "Noodmodus (USB-shell)"),
];

impl PowerPage {
    fn new() -> Self {
        let mut buttons: Vec<Button> = (0..3)
            .map(|i| Button::new(rect(40, BAR_H + 120 + i * 170, 640, 140), ""))
            .collect();
        buttons.push(Button::new(
            rect(40, BAR_H + 120 + 3 * 170, 640, 140),
            "Annuleren",
        ));
        let mut p = Self {
            title: "Aan/uit".into(),
            confirm: None,
            buttons,
        };
        p.labels();
        p
    }

    pub fn labels(&mut self) {
        for (i, (key, label)) in POWER_ACTIONS.iter().enumerate() {
            let sure = if self.confirm == Some(*key) {
                "Zeker? Tik nogmaals: "
            } else {
                ""
            };
            self.buttons[i].label = format!("{sure}{label}");
        }
    }

    fn click(&mut self, i: usize, cx: &mut Ctx) {
        if i == 3 {
            cx.pop();
            return;
        }
        let key = POWER_ACTIONS[i].0;
        if self.confirm != Some(key) {
            self.confirm = Some(key);
            return;
        }
        cx.actions.push(Action::Power(key));
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        let text = "Noodmodus: shell via de USB-kabel; aan/uit-knop start Linux weer.";
        c.text(
            40,
            BAR_H + 40,
            text,
            &cx.fonts.small,
            w::DIM,
            w::BG,
            1,
            Some(cx.width - 80),
        );
        self.labels();
        draw_buttons(&self.buttons, c, &cx.fonts.mid);
    }
}

pub enum Page {
    Home(Home),
    Terminal(TerminalPage),
    Wifi(WifiPage),
    Network(NetworkPage),
    Hidden(HiddenPage),
    Bluetooth(BluetoothPage),
    Settings(SettingsPage),
    PinSet(PinSetPage),
    Lock(LockPage),
    Power(PowerPage),
}

impl Page {
    fn new(spec: Spec, cx: &mut Ctx) -> Self {
        match spec {
            Spec::Home => Page::Home(Home::new()),
            Spec::Terminal => Page::Terminal(TerminalPage::new(cx)),
            Spec::Wifi => Page::Wifi(WifiPage::new(cx)),
            Spec::Network { ssid, sec } => Page::Network(NetworkPage::new(ssid, sec, cx)),
            Spec::Hidden => Page::Hidden(HiddenPage::new(cx)),
            Spec::Bluetooth => Page::Bluetooth(BluetoothPage::new(cx)),
            Spec::Settings => Page::Settings(SettingsPage::new()),
            Spec::PinSet => Page::PinSet(PinSetPage::new(cx)),
            Spec::Lock { then, reason } => Page::Lock(LockPage::new(then, reason)),
            Spec::Power => Page::Power(PowerPage::new()),
        }
    }

    pub fn title(&self) -> &str {
        match self {
            Page::Home(_) => "I14",
            Page::Terminal(_) => "Terminal",
            Page::Wifi(_) => "Wi-Fi",
            Page::Network(p) => &p.title,
            Page::Hidden(_) => "Verborgen netwerk",
            Page::Bluetooth(_) => "Bluetooth",
            Page::Settings(_) => "Instellingen",
            Page::PinSet(_) => "Pincode",
            Page::Lock(_) => "Vergrendeld",
            Page::Power(p) => &p.title,
        }
    }

    fn show(&mut self, cx: &mut Ctx) {
        match self {
            Page::Terminal(p) => p.show(cx),
            Page::Wifi(p) => p.show(cx),
            Page::PinSet(p) => p.show(cx),
            Page::Power(p) => p.confirm = None, // a lock or another page in between: ask again
            _ => {}
        }
    }

    pub fn keyboard(&mut self) -> Option<&mut Keyboard> {
        match self {
            Page::Terminal(p) => Some(&mut p.keyboard),
            Page::Network(p) => p.keyboard.as_mut(),
            Page::Hidden(p) => Some(&mut p.keyboard),
            _ => None,
        }
    }

    pub fn buttons(&mut self) -> &mut Vec<Button> {
        match self {
            Page::Home(p) => &mut p.buttons,
            Page::Terminal(p) => &mut p.no_buttons,
            Page::Wifi(p) => &mut p.buttons,
            Page::Network(p) => &mut p.buttons,
            Page::Hidden(p) => &mut p.buttons,
            Page::Bluetooth(p) => &mut p.buttons,
            Page::Settings(p) => &mut p.buttons,
            Page::PinSet(p) => &mut p.pad.buttons,
            Page::Lock(p) => &mut p.pad.buttons,
            Page::Power(p) => &mut p.buttons,
        }
    }

    /// Sets labels that depend on state (Python computes them when drawn or tapped).
    pub fn sync_labels(&mut self, cx: &Ctx) {
        match self {
            Page::Wifi(p) => p.labels(cx),
            Page::Network(p) => p.labels(),
            Page::Bluetooth(p) => p.labels(cx),
            Page::Settings(p) => p.labels(cx),
            Page::Power(p) => p.labels(),
            Page::Terminal(p) => {
                p.toggle.label = if p.kb_on { "verberg" } else { "toetsen" }.into()
            }
            _ => {}
        }
    }

    fn touch(&mut self, ev: Gesture, cx: &mut Ctx) {
        let now = cx.env.monotonic();
        match self {
            Page::Terminal(p) => p.touch(ev, cx),
            Page::Wifi(p) => p.touch(ev, cx),
            Page::Network(p) => p.touch(ev, cx),
            Page::Hidden(p) => p.touch(ev, cx),
            Page::Bluetooth(p) => p.touch(ev, cx),
            Page::Home(p) => {
                if let (Some(i), _) = base_touch(&mut p.buttons, None, ev, now) {
                    p.click(i, cx);
                }
            }
            Page::Settings(p) => {
                if let (Some(i), _) = base_touch(&mut p.buttons, None, ev, now) {
                    p.click(i, cx);
                }
            }
            Page::Power(p) => {
                if let (Some(i), _) = base_touch(&mut p.buttons, None, ev, now) {
                    p.click(i, cx);
                }
            }
            Page::Lock(p) => {
                if let Some(pin) = p.pad.touch(ev, cx) {
                    p.done(pin, cx);
                }
            }
            Page::PinSet(p) => {
                if let Some(pin) = p.pad.touch(ev, cx) {
                    p.done(pin, cx);
                }
            }
        }
    }

    fn key(&mut self, k: Key, cx: &mut Ctx) {
        match self {
            Page::Terminal(p) => p.key(k, cx),
            Page::Network(p) => p.key(k, cx),
            Page::Hidden(p) => p.key(k, cx),
            Page::Lock(p) => {
                if let Some(pin) = p.pad.key(k, cx) {
                    p.done(pin, cx);
                }
            }
            Page::PinSet(p) => {
                if let Some(pin) = p.pad.key(k, cx) {
                    p.done(pin, cx);
                }
            }
            _ => {}
        }
    }

    fn tick(&mut self, cx: &mut Ctx) -> bool {
        match self {
            Page::Terminal(p) => p.tick(cx),
            Page::Wifi(p) => p.tick(cx),
            Page::Network(p) => p.tick(cx),
            Page::Hidden(p) => p.tick(cx),
            Page::Bluetooth(p) => p.tick(cx),
            _ => false,
        }
    }

    fn draw(&mut self, c: &mut Canvas, cx: &mut Ctx) {
        match self {
            Page::Home(p) => p.draw(c, cx),
            Page::Terminal(p) => p.draw(c, cx),
            Page::Wifi(p) => p.draw(c, cx),
            Page::Network(p) => p.draw(c, cx),
            Page::Hidden(p) => p.draw(c, cx),
            Page::Bluetooth(p) => p.draw(c, cx),
            Page::Settings(p) => p.draw(c, cx),
            Page::PinSet(p) => p.draw(c, cx),
            Page::Lock(p) => p.draw(c, cx),
            Page::Power(p) => p.draw(c, cx),
        }
    }

    /// Screen off: drop what a held finger held (the release never comes).
    fn cancel_input(&mut self) {
        if let Some(kb) = self.keyboard() {
            kb.active = None;
            kb.repeat_at = None;
            kb.repeated = false;
        }
        for b in self.buttons().iter_mut() {
            b.pressed = false;
        }
        match self {
            Page::Wifi(p) => {
                p.list.start = None;
                p.list.pressed = None;
            }
            Page::Bluetooth(p) => {
                p.list.start = None;
                p.list.pressed = None;
            }
            Page::Terminal(p) => p.drag = None,
            _ => {}
        }
    }
}

pub struct App {
    pub stack: Vec<Page>,
    pub cx: Ctx,
    pub canvas: Canvas,
    pub back_btn: Button,
    /// "Koppelen" / "Weigeren" of the pairing code dialog, shown over any page
    pub confirm_btns: Vec<Button>,
    /// the code currently shown and whether the screen was locked (redraw when it appears,
    /// changes, goes or the lock opens)
    shown_confirm: Option<(String, String, bool)>,
}

/// The pairing code dialog: the code from the controller, accepted or refused only by the user.
/// While the screen is PIN-locked it is only a strip under the PIN pad: no buttons, the pad stays
/// usable; after unlocking the full dialog appears, else the request runs out and is refused.
fn draw_confirm(c: &mut Canvas, cx: &Ctx, buttons: &[Button], locked: bool) {
    let Some(p) = &cx.bt.pending else {
        return;
    };
    let f = &cx.fonts;
    let max = Some(cx.width - 100);
    if locked {
        c.fill(20, 1430, cx.width - 40, 160, w::PANEL);
        let what = if p.just_works {
            format!("Koppelverzoek van {}", p.name)
        } else {
            format!("Koppelverzoek van {}, code {}", p.name, p.code)
        };
        c.text(50, 1450, &what, &f.small, w::FG, w::PANEL, 1, max);
        c.text(
            50,
            1520,
            "Ontgrendel om te koppelen",
            &f.small,
            w::YELLOW,
            w::PANEL,
            1,
            max,
        );
        return;
    }
    c.fill(20, 640, cx.width - 40, 540, w::PANEL);
    c.text(
        50,
        670,
        "Bluetooth-koppelverzoek",
        &f.small,
        w::DIM,
        w::PANEL,
        1,
        max,
    );
    c.text(
        50,
        710,
        &format!("Koppelen met {}?", p.name),
        &f.mid,
        w::FG,
        w::PANEL,
        1,
        max,
    );
    c.text(50, 750, &p.addr, &f.small, w::DIM, w::PANEL, 1, max);
    if p.just_works {
        c.text(
            50,
            820,
            "Het andere apparaat toont geen code.",
            &f.small,
            w::YELLOW,
            w::PANEL,
            1,
            max,
        );
    } else {
        c.text(
            50,
            800,
            &format!("Code {}", p.code),
            &f.big,
            w::FG,
            w::PANEL,
            2,
            max,
        );
    }
    c.text(
        50,
        900,
        "Alleen koppelen als dit dezelfde code is als op het andere apparaat.",
        &f.small,
        w::DIM,
        w::PANEL,
        1,
        max,
    );
    for b in buttons {
        b.draw(c, &f.mid);
    }
}

impl App {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        canvas: Canvas,
        screen: Box<dyn Display>,
        fonts: Fonts,
        cfg: Config,
        wifi: Box<dyn WifiApi>,
        bt: Bluetooth,
        env: Box<dyn Env>,
    ) -> Self {
        let last_activity = env.monotonic();
        let mut cx = Ctx {
            width: canvas.w,
            height: canvas.h,
            fonts,
            cfg,
            wifi,
            bt,
            screen,
            env,
            session: None,
            dirty: true,
            last_activity,
            actions: Vec::new(),
        };
        let brightness = cx.cfg.data.brightness as i64;
        cx.screen.set_brightness(brightness);
        let back_btn = Button::new(rect(0, 0, 110, BAR_H), "<")
            .color(w::PANEL)
            .scale(2);
        let r = |(x, y, w, h): (i32, i32, i32, i32)| rect(x, y, w, h);
        let confirm_btns = vec![
            Button::new(r(ACCEPT_RECT), "Koppelen").color(w::ACCENT),
            Button::new(r(REJECT_RECT), "Weigeren"),
        ];
        let mut app = Self {
            stack: Vec::new(),
            cx,
            canvas,
            back_btn,
            confirm_btns,
            shown_confirm: None,
        };
        app.push(Spec::Home);
        app
    }

    pub fn page(&mut self) -> &mut Page {
        self.stack
            .last_mut()
            .expect("the home page is never popped")
    }

    pub fn push(&mut self, spec: Spec) {
        let page = Page::new(spec, &mut self.cx);
        self.stack.push(page);
        let top = self.stack.last_mut().unwrap();
        top.show(&mut self.cx);
        self.cx.dirty = true;
        self.run_actions();
    }

    pub fn pop(&mut self) {
        if self.stack.len() > 1 {
            self.stack.pop();
            let top = self.stack.last_mut().unwrap();
            top.show(&mut self.cx);
        }
        self.cx.dirty = true;
        self.run_actions();
    }

    fn run_actions(&mut self) {
        while !self.cx.actions.is_empty() {
            let action = self.cx.actions.remove(0);
            match action {
                Action::Push(spec) => self.push(spec),
                Action::Pop => self.pop(),
                Action::Unlocked(then) => {
                    let n = self.stack.len();
                    if let (Some(then), true) = (then, n >= 2) {
                        match (&mut self.stack[n - 2], then) {
                            (Page::Settings(p), Then::RemovePin) => p.remove_pin(&mut self.cx),
                            (Page::PinSet(p), Then::Verified) => p.verified = true,
                            _ => {}
                        }
                    }
                    self.pop();
                }
                Action::Power(key) => self.power_action(key),
            }
        }
    }

    /// Sets the top page's state-dependent button labels (tests tap by label).
    pub fn sync_labels(&mut self) {
        let App { stack, cx, .. } = self;
        if let Some(p) = stack.last_mut() {
            p.sync_labels(cx);
        }
    }

    pub fn locked(&self) -> bool {
        matches!(self.stack.last(), Some(Page::Lock(p)) if p.then.is_none())
    }

    pub fn lock(&mut self) {
        if self.cx.cfg.has_pin() && !self.locked() {
            self.push(Spec::Lock {
                then: None,
                reason: "pincode",
            });
        }
        self.cx.bt.ui_locked = self.locked();
    }

    pub fn render(&mut self) {
        let c = &mut self.canvas;
        let cx = &mut self.cx;
        c.clear(w::BG);
        c.fill(0, 0, cx.width, BAR_H, w::PANEL);
        let mut x = 20;
        let locked = matches!(self.stack.last(), Some(Page::Lock(p)) if p.then.is_none());
        if self.stack.len() > 1 && !locked {
            self.back_btn.draw(c, &cx.fonts.mid);
            x = 130;
        }
        let page = self.stack.last_mut().unwrap();
        let mid_h = cx.fonts.mid.h as i32;
        c.text(
            x,
            floor_div(BAR_H - mid_h, 2),
            page.title(),
            &cx.fonts.mid,
            w::FG,
            w::PANEL,
            1,
            Some(cx.width - x - 280),
        );
        let cap = cx.env.battery_level();
        let terminal = matches!(page, Page::Terminal(_));
        let (sx, text) = if terminal {
            (cx.width - 90, format!("{cap}%"))
        } else {
            (
                cx.width - 210,
                format!("{} {cap}%", hhmm(&cx.env.local_time())),
            )
        };
        let small_h = cx.fonts.small.h as i32;
        c.text(
            sx,
            floor_div(BAR_H - small_h, 2),
            &text,
            &cx.fonts.small,
            w::DIM,
            w::PANEL,
            1,
            None,
        );
        page.draw(c, cx);
        draw_confirm(c, cx, &self.confirm_btns, locked);
        cx.screen.show(c);
        cx.dirty = false;
        self.run_actions();
    }

    pub fn touch(&mut self, ev: Gesture) {
        self.cx.last_activity = self.cx.env.monotonic();
        self.cx.bt.ui_locked = self.locked();
        if self.cx.bt.pending.is_some() && !self.cx.bt.ui_locked {
            // the pairing dialog is modal: only its two buttons count (locked: the PIN pad does)
            for (i, b) in self.confirm_btns.iter_mut().enumerate() {
                let (handled, clicked) = b.touch(ev);
                if clicked {
                    self.cx.bt.confirm(i == 0);
                    break;
                }
                if handled {
                    break;
                }
            }
            self.cx.dirty = true;
            return;
        }
        let near_bar = match ev {
            Gesture::Down(_, y) => y < BAR_H,
            _ => true,
        };
        if self.stack.len() > 1 && !self.locked() && near_bar {
            let (handled, clicked) = self.back_btn.touch(ev);
            if clicked {
                self.pop();
            }
            if handled {
                self.cx.dirty = true;
                return;
            }
        }
        let page = self.stack.last_mut().unwrap();
        page.touch(ev, &mut self.cx);
        self.cx.dirty = true;
        self.run_actions();
    }

    pub fn key(&mut self, k: Key) {
        self.cx.last_activity = self.cx.env.monotonic();
        let page = self.stack.last_mut().unwrap();
        page.key(k, &mut self.cx);
        self.cx.dirty = true;
        self.run_actions();
    }

    pub fn tick(&mut self) -> bool {
        let locked = self.locked();
        self.cx.bt.ui_locked = locked;
        let shown = self
            .cx
            .bt
            .pending
            .as_ref()
            .map(|p| (p.addr.clone(), p.code.clone(), locked));
        let dialog = shown != self.shown_confirm;
        if dialog {
            if shown.is_some() {
                // a finger on the page under the dialog must not click later
                self.cancel_input();
            }
            for b in &mut self.confirm_btns {
                b.pressed = false;
            }
            self.shown_confirm = shown;
        }
        let page = self.stack.last_mut().unwrap();
        let r = page.tick(&mut self.cx);
        self.run_actions();
        r || dialog
    }

    pub fn cancel_input(&mut self) {
        for p in &mut self.stack {
            p.cancel_input();
        }
        self.back_btn.pressed = false;
    }

    pub fn timeout(&self) -> u32 {
        let t = self.cx.cfg.data.timeout;
        if t != 0 && matches!(self.stack.last(), Some(Page::Terminal(_))) {
            t.max(TERMINAL_MIN_TIMEOUT)
        } else {
            t
        }
    }

    pub fn power_action(&mut self, key: &str) {
        self.canvas.clear(w::BG);
        let msg = match key {
            "reboot" => "Herstarten...",
            "poweroff" => "Uitschakelen...",
            _ => "Naar de noodmodus...",
        };
        self.canvas
            .text(40, 400, msg, &self.cx.fonts.big, w::RED, w::BG, 1, None);
        self.cx.screen.show(&self.canvas);
        if key == "rescue" {
            let path = format!("{}/rescue-once", self.cx.env.data_dir());
            if let Err(e) = std::fs::write(&path, "i14-ui\n") {
                if let Page::Power(p) = self.page() {
                    // open() errors name the file, as Python's do
                    p.title = format!("mislukt: {}: '{path}'", py_err(&e));
                }
                return;
            }
        }
        self.cx.env.run(if key == "poweroff" {
            "poweroff"
        } else {
            "reboot"
        });
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::bt::{Device, Found};
    use std::io::Read;
    use std::os::unix::net::UnixStream;

    struct NoScreen(bool);
    impl Display for NoScreen {
        fn show(&mut self, _: &Canvas) {}
        fn power(&mut self, on: bool) {
            self.0 = on;
        }
        fn is_on(&self) -> bool {
            self.0
        }
        fn set_brightness(&mut self, _: i64) {}
    }

    struct NoWifi;
    impl WifiApi for NoWifi {
        fn enabled(&self) -> bool {
            false
        }
        fn scanning(&self) -> bool {
            false
        }
        fn attempt_active(&self) -> bool {
            false
        }
        fn message(&self) -> String {
            String::new()
        }
        fn set_message(&mut self, _: String) {}
        fn changed(&self) -> bool {
            false
        }
        fn set_changed(&mut self, _: bool) {}
        fn scan_results(&self) -> Vec<ScanEntry> {
            Vec::new()
        }
        fn status(&mut self) -> WifiStatus {
            WifiStatus::default()
        }
        fn saved(&mut self) -> Vec<SavedNet> {
            Vec::new()
        }
        fn network_id(&mut self, _: &[u8]) -> Option<i64> {
            None
        }
        fn available(&mut self) -> bool {
            false
        }
        fn scan(&mut self) -> io::Result<()> {
            Ok(())
        }
        fn set_enabled(&mut self, _: bool) -> io::Result<bool> {
            Ok(false)
        }
        fn connect(&mut self, _: &[u8], _: &str, _: Option<&str>, _: bool) -> io::Result<bool> {
            Ok(false)
        }
        fn prefer(&mut self, _: &[u8]) -> io::Result<bool> {
            Ok(false)
        }
        fn forget(&mut self, _: &[u8]) -> io::Result<bool> {
            Ok(false)
        }
        fn monitor_fd(&mut self) -> Option<i32> {
            None
        }
        fn events(&mut self) {}
        fn tick(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    struct FixedEnv;
    impl Env for FixedEnv {
        fn monotonic(&self) -> f64 {
            1000.0
        }
        fn local_time(&self) -> LocalTime {
            LocalTime {
                year: 2026,
                month: 11,
                day: 8,
                hour: 12,
                minute: 0,
                weekday: 6,
            }
        }
        fn battery(&self) -> Battery {
            Battery {
                cap: "50".into(),
                state: "Discharging",
                word: "ontladen",
                current: String::new(),
                volt: String::new(),
            }
        }
        fn battery_level(&self) -> String {
            "50".into()
        }
        fn addresses(&self) -> Vec<(String, String)> {
            Vec::new()
        }
        fn free(&self, _: &str) -> String {
            String::new()
        }
        fn uptime(&self) -> i64 {
            0
        }
        fn run(&self, _: &str) {}
        fn data_dir(&self) -> String {
            "/nonexistent-i14-data".into()
        }
    }

    const MAC: &str = "02:11:22:33:44:55";
    const MAC_RAW: [u8; 6] = [0x55, 0x44, 0x33, 0x22, 0x11, 0x02];

    pub(crate) struct H {
        pub(crate) app: App,
        pub(crate) b: UnixStream,
    }

    pub(crate) fn harness() -> H {
        let (a, b) = UnixStream::pair().unwrap();
        a.set_nonblocking(true).unwrap();
        b.set_nonblocking(true).unwrap();
        let opener: crate::bt::Opener =
            Box::new(move |_| Ok(Box::new(a.try_clone()?) as Box<dyn Device>));
        let bt = Bluetooth::with_opener("/dev/stpbt", opener);
        let font = || Font::parse(&crate::font::tests::tiny()).unwrap();
        let fonts = Fonts {
            big: font(),
            mid: font(),
            small: font(),
            term: font(),
        };
        let app = App::new(
            Canvas::new(720, 1600, None),
            Box::new(NoScreen(true)),
            fonts,
            Config::load("/nonexistent-i14-data/ui.json"),
            Box::new(NoWifi),
            bt,
            Box::new(FixedEnv),
        );
        H { app, b }
    }

    impl H {
        fn sent(&mut self) -> Vec<u8> {
            let mut out = Vec::new();
            let mut buf = [0u8; 4096];
            while let Ok(n) = self.b.read(&mut buf) {
                if n == 0 {
                    break;
                }
                out.extend_from_slice(&buf[..n]);
            }
            out
        }

        fn tap(&mut self, label: &str) {
            self.app.sync_labels();
            let r = if self.app.cx.bt.pending.is_some() && !self.app.locked() {
                self.app
                    .confirm_btns
                    .iter()
                    .find(|b| b.label == label)
                    .map(|b| b.rect)
            } else {
                let page = self.app.stack.last_mut().unwrap();
                page.buttons()
                    .iter()
                    .find(|b| b.label == label)
                    .map(|b| b.rect)
            };
            let r = r.unwrap_or_else(|| panic!("no button {label:?}"));
            self.app.touch(Gesture::Down(r.x + 5, r.y + 5));
            self.app.touch(Gesture::Up(r.x + 5, r.y + 5));
        }

        fn labels(&mut self) -> Vec<String> {
            self.app.sync_labels();
            let page = self.app.stack.last_mut().unwrap();
            page.buttons().iter().map(|b| b.label.clone()).collect()
        }

        fn tap_row(&mut self, i: i32) {
            let y = BT_LIST_Y + i * 100 + 50;
            self.app.touch(Gesture::Down(100, y));
            self.app.touch(Gesture::Up(100, y));
        }

        fn bt_page(&mut self) {
            self.tap("Bluetooth");
            assert!(matches!(self.app.stack.last(), Some(Page::Bluetooth(_))));
            self.app.tick();
            self.app.render();
        }

        fn rows(&mut self) -> Vec<String> {
            self.app.tick();
            match self.app.stack.last() {
                Some(Page::Bluetooth(p)) => p.list.items.iter().map(|i| i.text.clone()).collect(),
                _ => panic!("not on the Bluetooth page"),
            }
        }
    }

    fn user_confirm(h: &mut H, value: u32) {
        let mut p = MAC_RAW.to_vec();
        p.extend_from_slice(&value.to_le_bytes());
        h.app.cx.bt.handle(crate::bt::EV_USER_CONFIRM, &p);
    }

    fn cmd(op: u16, params: &[u8]) -> Vec<u8> {
        crate::bt::command(op, params)
    }

    #[test]
    fn bluetooth_page_offers_visibility_and_no_scan_only_line() {
        let mut h = harness();
        h.bt_page();
        assert_eq!(
            h.labels(),
            ["Bluetooth: uit", "Zoeken", "Zichtbaar (2 min)"]
        );
        h.tap("Zichtbaar (2 min)");
        assert!(h.app.cx.bt.on() && h.app.cx.bt.visible() && h.app.cx.bt.auto_on);
        assert!(
            h.labels()[2].starts_with("Zichtbaar: nog "),
            "{:?}",
            h.labels()
        );
        h.app.render();
        let l = h.labels()[2].clone();
        h.tap(&l);
        assert!(!h.app.cx.bt.visible());
    }

    #[test]
    fn the_code_dialog_appears_on_any_page_and_is_modal() {
        let mut h = harness();
        h.app.cx.bt.ready_for_tests();
        h.app.cx.bt.set_visible(true);
        h.tap("Instellingen");
        let depth = h.app.stack.len();
        h.sent();
        user_confirm(&mut h, 123456);
        assert!(h.app.cx.bt.wake);
        assert!(h.app.tick(), "the dialog needs a redraw");
        h.app.render();
        // the page under the dialog does not react, not even the back button
        h.app.touch(Gesture::Down(5, 5));
        h.app.touch(Gesture::Up(5, 5));
        assert_eq!(h.app.stack.len(), depth);
        assert_eq!(h.sent(), b"");
        h.tap("Koppelen");
        assert_eq!(h.sent(), cmd(crate::bt::USER_CONFIRM_REPLY, &MAC_RAW));
        assert!(h.app.cx.bt.pending.is_none());
        assert!(h.app.tick(), "the dialog goes away");
        h.app.render();
    }

    #[test]
    fn a_locked_phone_cannot_accept_a_pairing_code() {
        let mut h = harness();
        let dir = std::env::temp_dir().join(format!("i14-ui-bt-lock-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        h.app.cx.cfg = Config::load(dir.join("ui.json"));
        assert!(h.app.cx.cfg.set_pin(Some("1234")));
        h.app.cx.bt.ready_for_tests();
        h.app.cx.bt.set_visible(true);
        h.app.lock();
        assert!(h.app.locked());
        h.sent();
        user_confirm(&mut h, 123456);
        h.app.tick();
        h.app.render();
        // a tap where "Koppelen" would be reaches the PIN pad, not the dialog
        let (x, y, bw, bh) = crate::bt::state::ACCEPT_RECT;
        h.app.touch(Gesture::Down(x + bw / 2, y + bh / 2));
        h.app.touch(Gesture::Up(x + bw / 2, y + bh / 2));
        assert_eq!(h.sent(), b"", "accepted while locked");
        assert!(h.app.cx.bt.pending.is_some());
        assert!(h.app.locked());
        // the PIN pad still works: clear the stray digit, unlock, then the dialog accepts
        for l in ["<x", "<x", "1", "2", "3", "4", "OK"] {
            h.tap(l);
        }
        assert!(!h.app.locked());
        assert!(h.app.tick(), "the full dialog appears");
        h.app.render();
        h.tap("Koppelen");
        assert_eq!(h.sent(), cmd(crate::bt::USER_CONFIRM_REPLY, &MAC_RAW));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn the_code_dialog_can_refuse() {
        let mut h = harness();
        h.app.cx.bt.ready_for_tests();
        h.app.cx.bt.set_visible(true);
        user_confirm(&mut h, 1);
        h.sent();
        h.tap("Weigeren");
        assert_eq!(h.sent(), cmd(crate::bt::USER_CONFIRM_NEG_REPLY, &MAC_RAW));
        assert_eq!(h.app.cx.bt.message, "koppelen geweigerd");
    }

    #[test]
    fn a_found_classic_device_can_be_paired() {
        let mut h = harness();
        h.bt_page();
        h.app.cx.bt.ready_for_tests();
        let found = |name: &str, kind| Found {
            name: Some(name.into()),
            rssi: Some(-50),
            kind,
            psrm: Some(1),
        };
        h.app
            .cx
            .bt
            .devices
            .push((MAC.into(), found("MacBook", "klassiek")));
        h.app
            .cx
            .bt
            .devices
            .push(("01:02:03:04:05:06".into(), found("Tag", "BLE")));
        h.app.cx.bt.changed = true;
        assert_eq!(h.rows(), ["MacBook", "Tag"]);
        h.tap_row(1);
        assert_eq!(h.app.cx.bt.message, "BLE-koppelen wordt niet ondersteund");
        h.tap_row(0);
        assert_eq!(h.labels(), ["Ja", "Nee"]);
        h.app.render();
        h.tap("Nee");
        assert_eq!(h.labels()[1], "Zoeken");
        assert!(h.app.cx.bt.pairing.is_none());
        h.tap_row(0);
        h.sent();
        h.tap("Ja");
        assert_eq!(h.app.cx.bt.pairing.as_ref().unwrap().addr, MAC);
        assert!(h
            .sent()
            .starts_with(&cmd(crate::bt::CREATE_CONNECTION, &MAC_RAW)[..3]));
    }

    #[test]
    fn a_paired_device_can_be_connected_or_forgotten() {
        let mut h = harness();
        h.app.cx.bt.keys.put(MAC, [9; 16], 5, "MacBook").unwrap();
        h.bt_page();
        h.app.cx.bt.changed = true;
        assert_eq!(h.rows(), ["Gekoppeld", "MacBook"]);
        h.tap_row(0); // the header does nothing
        assert_eq!(h.labels()[1], "Zoeken");
        h.tap_row(1);
        assert_eq!(h.labels(), ["Verbinden", "Vergeten", "Annuleren"]);
        h.app.render();
        h.tap("Annuleren");
        h.tap_row(1);
        h.tap("Vergeten");
        assert_eq!(h.labels(), ["Ja", "Nee"]);
        h.tap("Nee");
        assert!(h.app.cx.bt.keys.get(MAC).is_some(), "Nee keeps the key");
        h.tap_row(1);
        h.tap("Vergeten");
        h.tap("Ja");
        assert!(h.app.cx.bt.keys.get(MAC).is_none());
        assert_eq!(h.app.cx.bt.message, "vergeten: MacBook");
        assert!(h.rows().is_empty());
        // Verbinden on a paired device starts a reconnection (radio switched on for it)
        h.app.cx.bt.keys.put(MAC, [9; 16], 5, "MacBook").unwrap();
        h.app.cx.bt.changed = true;
        h.rows();
        h.tap_row(1);
        h.tap("Verbinden");
        assert!(h.app.cx.bt.on() && h.app.cx.bt.pairing.is_some());
    }
}
