//! Linux input events to gestures and keys, as `i14ui/inputdev.py`: finding
//! devices from sysfs, multitouch protocol B (fts_ts) and keyboard key codes.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::Path;

pub const EV_SYN: u16 = 0;
pub const EV_KEY: u16 = 1;
pub const EV_ABS: u16 = 3;
pub const SYN_REPORT: u16 = 0;
pub const SYN_DROPPED: u16 = 3;
pub const ABS_X: u16 = 0x00;
pub const ABS_Y: u16 = 0x01;
pub const ABS_MT_SLOT: u16 = 0x2f;
pub const ABS_MT_POSITION_X: u16 = 0x35;
pub const ABS_MT_POSITION_Y: u16 = 0x36;
pub const ABS_MT_TRACKING_ID: u16 = 0x39;
pub const BTN_TOUCH: u16 = 330;
pub const KEY_POWER: u16 = 116;
pub const KEY_VOLUMEDOWN: u16 = 114;
pub const KEY_VOLUMEUP: u16 = 115;
const KEY_A: u32 = 30;
const KEY_Z: u32 = 44;

/// Size of `struct input_event` on arm64: two i64 time fields, u16 type, u16 code, i32 value.
pub const EVENT_SIZE: usize = 24;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Event {
    pub time: f64,
    pub kind: u16,
    pub code: u16,
    pub value: i32,
}

/// Each whole event in `data` (a trailing partial event is ignored).
pub fn events(data: &[u8]) -> impl Iterator<Item = Event> + '_ {
    data.as_chunks::<EVENT_SIZE>().0.iter().map(|e| {
        let i64_at = |o: usize| i64::from_ne_bytes(e[o..o + 8].try_into().unwrap());
        Event {
            time: i64_at(0) as f64 + i64_at(8) as f64 / 1e6,
            kind: u16::from_ne_bytes([e[16], e[17]]),
            code: u16::from_ne_bytes([e[18], e[19]]),
            value: i32::from_ne_bytes(e[20..24].try_into().unwrap()),
        }
    })
}

/// Bit `n` of a sysfs capability bitmap ("40 0 1000"): 64-bit words, most significant first.
fn bit(text: &str, n: u32) -> bool {
    let words: Vec<&str> = text.split_whitespace().collect();
    let i = (n / 64) as usize;
    if i >= words.len() {
        return false;
    }
    let word = u64::from_str_radix(words[words.len() - 1 - i], 16).unwrap_or(0);
    word >> (n % 64) & 1 == 1
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct Devices {
    pub touch: Vec<String>,
    pub power: Vec<String>,
    pub keyboard: Vec<String>,
}

/// Sorts `/sys/class/input/event*` into touch screens, power keys and keyboards.
pub fn classify(sysfs: impl AsRef<Path>) -> Devices {
    let sysfs = sysfs.as_ref();
    let mut found = Devices::default();
    let Ok(dir) = fs::read_dir(sysfs) else {
        return found;
    };
    let mut names: Vec<String> = dir
        .filter_map(|d| d.ok()?.file_name().into_string().ok())
        .filter(|n| n.starts_with("event"))
        .collect();
    names.sort();
    let read = |p: &Path| {
        fs::read_to_string(p)
            .map(|s| s.trim().to_string())
            .unwrap_or_default()
    };
    for n in names {
        let dev = sysfs.join(&n).join("device");
        let name = read(&dev.join("name"));
        let abs = read(&dev.join("capabilities/abs"));
        let key = read(&dev.join("capabilities/key"));
        let path = format!("/dev/input/{n}");
        if bit(&abs, ABS_MT_POSITION_X as u32) && bit(&abs, ABS_MT_POSITION_Y as u32) {
            found.touch.push(path);
        } else if name == "mtk-kpd" || (bit(&key, KEY_POWER as u32) && !bit(&key, KEY_A)) {
            found.power.push(path);
        } else if bit(&key, KEY_A) && bit(&key, KEY_Z) {
            found.keyboard.push(path); // KEY_A and KEY_Z: a real keyboard
        }
    }
    found
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Gesture {
    Down(i32, i32),
    Move(i32, i32),
    Up(i32, i32),
}

/// Protocol B (slots) to single-finger gestures, one per SYN_REPORT.
///
/// Follows the first finger that touches; other fingers are ignored until all
/// have lifted. Also handles single-touch devices (ABS_X/ABS_Y + BTN_TOUCH).
#[derive(Default)]
pub struct Touch {
    slot: i32,
    /// slot -> (tracking id, x, y) for fingers touching now.
    slots: BTreeMap<i32, (i32, i32, i32)>,
    /// slot -> last (x, y): the kernel only sends values that changed, even
    /// across a lift, so a slot's position outlives its contact.
    last: HashMap<i32, [i32; 2]>,
    active: Option<i32>,
    down: bool,
    pos: (i32, i32),
    dropped: bool,
    /// The followed finger lifted while others still touch.
    wait: bool,
    single_x: i32,
    single_y: i32,
    single_touch: Option<bool>,
}

impl Touch {
    pub fn new() -> Self {
        Self::default()
    }

    /// Forgets any contact (screen off: the chip sleeps and may never report a
    /// lift). Keeps the slot, the last positions and `dropped`.
    pub fn cancel(&mut self) {
        self.slots.clear();
        self.active = None;
        self.down = false;
        self.wait = false;
    }

    /// One raw event; returns a gesture after a SYN_REPORT.
    pub fn feed(&mut self, kind: u16, code: u16, value: i32) -> Option<Gesture> {
        if kind == EV_SYN && code == SYN_DROPPED {
            self.dropped = true;
            return None;
        }
        if self.dropped {
            if kind == EV_SYN && code == SYN_REPORT {
                // State is unknown: end any gesture. `last` stays (maybe stale,
                // but never the y=0 back button).
                self.dropped = false;
                self.slots.clear();
                self.active = None;
                if self.down {
                    self.down = false;
                    return Some(Gesture::Up(self.pos.0, self.pos.1));
                }
            }
            return None;
        }
        match (kind, code) {
            (EV_ABS, ABS_MT_SLOT) => self.slot = value,
            (EV_ABS, ABS_MT_TRACKING_ID) => {
                if value < 0 {
                    self.slots.remove(&self.slot);
                } else {
                    let [x, y] = self.last.get(&self.slot).copied().unwrap_or([0, 0]);
                    self.slots.insert(self.slot, (value, x, y));
                }
            }
            (EV_ABS, ABS_MT_POSITION_X | ABS_MT_POSITION_Y) => {
                let i = usize::from(code == ABS_MT_POSITION_Y);
                self.last.entry(self.slot).or_insert([0, 0])[i] = value;
                if let Some(s) = self.slots.get_mut(&self.slot) {
                    if i == 0 {
                        s.1 = value
                    } else {
                        s.2 = value
                    }
                }
            }
            (EV_ABS, ABS_X) => self.single_x = value,
            (EV_ABS, ABS_Y) => self.single_y = value,
            (EV_KEY, BTN_TOUCH) => self.single_touch = Some(value != 0),
            (EV_SYN, SYN_REPORT) => return self.report(),
            _ => {}
        }
        None
    }

    fn report(&mut self) -> Option<Gesture> {
        if self.slots.is_empty() {
            self.wait = false;
        }
        if !self.slots.is_empty() || self.active.is_some() {
            if !self.active.is_some_and(|a| self.slots.contains_key(&a)) {
                // A new gesture only starts once every finger has lifted.
                self.active = match self.slots.keys().next() {
                    Some(&first) if !self.down && !self.wait => Some(first),
                    _ => None,
                };
                if self.active.is_none() && self.down {
                    self.wait = !self.slots.is_empty();
                }
            }
            if let Some(a) = self.active {
                let (_, x, y) = self.slots[&a];
                let was = self.down;
                self.down = true;
                self.pos = (x, y);
                return Some(if was {
                    Gesture::Move(x, y)
                } else {
                    Gesture::Down(x, y)
                });
            }
        } else if let Some(touching) = self.single_touch {
            if touching {
                let was = self.down;
                self.down = true;
                self.pos = (self.single_x, self.single_y);
                let (x, y) = self.pos;
                return Some(if was {
                    Gesture::Move(x, y)
                } else {
                    Gesture::Down(x, y)
                });
            }
        }
        if self.down {
            self.down = false;
            return Some(Gesture::Up(self.pos.0, self.pos.1));
        }
        None
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KeyName {
    Char(char),
    Named(&'static str),
}

/// A key press: name or character, ctrl held, alt held.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Key {
    pub name: KeyName,
    pub ctrl: bool,
    pub alt: bool,
}

impl Key {
    pub fn plain(name: KeyName) -> Self {
        Self {
            name,
            ctrl: false,
            alt: false,
        }
    }
}

/// Key codes (linux/input-event-codes.h) to characters, US layout: (plain, shifted).
fn printable(code: u16) -> Option<(char, char)> {
    const ROWS: &[(u16, &str)] = &[
        (2, "1!"),
        (3, "2@"),
        (4, "3#"),
        (5, "4$"),
        (6, "5%"),
        (7, "6^"),
        (8, "7&"),
        (9, "8*"),
        (10, "9("),
        (11, "0)"),
        (12, "-_"),
        (13, "=+"),
        (16, "qQ"),
        (17, "wW"),
        (18, "eE"),
        (19, "rR"),
        (20, "tT"),
        (21, "yY"),
        (22, "uU"),
        (23, "iI"),
        (24, "oO"),
        (25, "pP"),
        (26, "[{"),
        (27, "]}"),
        (30, "aA"),
        (31, "sS"),
        (32, "dD"),
        (33, "fF"),
        (34, "gG"),
        (35, "hH"),
        (36, "jJ"),
        (37, "kK"),
        (38, "lL"),
        (39, ";:"),
        (40, "'\""),
        (41, "`~"),
        (43, "\\|"),
        (44, "zZ"),
        (45, "xX"),
        (46, "cC"),
        (47, "vV"),
        (48, "bB"),
        (49, "nN"),
        (50, "mM"),
        (51, ",<"),
        (52, ".>"),
        (53, "/?"),
        (57, "  "),
    ];
    ROWS.iter().find(|(c, _)| *c == code).map(|(_, s)| {
        let mut it = s.chars();
        (it.next().unwrap(), it.next().unwrap())
    })
}

fn named(code: u16) -> Option<&'static str> {
    Some(match code {
        1 => "esc",
        14 => "backspace",
        15 => "tab",
        28 | 96 => "enter",
        103 => "up",
        108 => "down",
        105 => "left",
        106 => "right",
        102 => "home",
        107 => "end",
        104 => "pgup",
        109 => "pgdn",
        111 => "delete",
        _ => return None,
    })
}

const SHIFT: [u16; 2] = [42, 54];
const CTRL: [u16; 2] = [29, 97];
const ALT: [u16; 2] = [56, 100];
const CAPS: u16 = 58;

/// A hardware keyboard's events to keys.
#[derive(Default)]
pub struct Keyboard {
    held: BTreeSet<u16>,
    caps: bool,
}

impl Keyboard {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn feed(&mut self, kind: u16, code: u16, value: i32) -> Option<Key> {
        if kind != EV_KEY {
            return None;
        }
        if SHIFT.contains(&code) || CTRL.contains(&code) || ALT.contains(&code) {
            if value != 0 {
                self.held.insert(code);
            } else {
                self.held.remove(&code);
            }
            return None;
        }
        if value == 0 {
            return None;
        }
        if code == CAPS {
            if value == 1 {
                self.caps = !self.caps;
            }
            return None;
        }
        let any = |set: &[u16]| set.iter().any(|c| self.held.contains(c));
        let (ctrl, alt) = (any(&CTRL), any(&ALT));
        if let Some(n) = named(code) {
            return Some(Key {
                name: KeyName::Named(n),
                ctrl,
                alt,
            });
        }
        let (plain, shifted) = printable(code)?;
        let mut shift = any(&SHIFT);
        if plain.is_alphabetic() {
            shift ^= self.caps;
        }
        Some(Key {
            name: KeyName::Char(if shift { shifted } else { plain }),
            ctrl,
            alt,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::Gesture::{Down, Move, Up};
    use super::*;

    type Raw = (u16, u16, i32);

    fn mt(slot: Option<i32>, tid: Option<i32>, x: Option<i32>, y: Option<i32>) -> Vec<Raw> {
        let mut ev = Vec::new();
        if let Some(s) = slot {
            ev.push((EV_ABS, 0x2f, s));
        }
        if let Some(t) = tid {
            ev.push((EV_ABS, 0x39, t));
        }
        if let Some(x) = x {
            ev.push((EV_ABS, 0x35, x));
        }
        if let Some(y) = y {
            ev.push((EV_ABS, 0x36, y));
        }
        ev.push((EV_SYN, 0, 0));
        ev
    }

    fn gestures(t: &mut Touch, frames: &[Vec<Raw>]) -> Vec<Gesture> {
        frames
            .iter()
            .flatten()
            .filter_map(|&(k, c, v)| t.feed(k, c, v))
            .collect()
    }

    const N: Option<i32> = None;

    #[test]
    fn tap_and_drag() {
        let mut t = Touch::new();
        let got = gestures(
            &mut t,
            &[
                mt(Some(0), Some(5), Some(100), Some(200)),
                mt(N, N, Some(110), N),
                mt(N, N, N, Some(260)),
                mt(N, Some(-1), N, N),
            ],
        );
        assert_eq!(
            got,
            [Down(100, 200), Move(110, 200), Move(110, 260), Up(110, 260)]
        );
    }

    #[test]
    fn second_finger_is_ignored_until_all_lift() {
        let mut t = Touch::new();
        let got = gestures(
            &mut t,
            &[
                mt(Some(0), Some(1), Some(10), Some(10)),
                mt(Some(1), Some(2), Some(500), Some(500)),
                mt(Some(0), Some(-1), N, N),
                mt(Some(1), N, Some(510), Some(510)),
                mt(Some(1), Some(-1), N, N),
                mt(Some(0), Some(3), Some(50), Some(60)),
                mt(Some(0), Some(-1), N, N),
            ],
        );
        assert_eq!(
            got,
            [
                Down(10, 10),
                Move(10, 10),
                Up(10, 10),
                Down(50, 60),
                Up(50, 60)
            ]
        );
    }

    #[test]
    fn unchanged_coordinates_are_not_resent() {
        let mut t = Touch::new();
        let got = gestures(
            &mut t,
            &[
                mt(Some(0), Some(1), Some(151), Some(1370)),
                mt(Some(0), Some(-1), N, N),
                mt(Some(0), Some(2), Some(54), N),
                mt(Some(0), Some(-1), N, N),
                mt(Some(0), Some(3), N, N),
                mt(Some(0), Some(-1), N, N),
            ],
        );
        assert_eq!(
            got,
            [
                Down(151, 1370),
                Up(151, 1370),
                Down(54, 1370),
                Up(54, 1370),
                Down(54, 1370),
                Up(54, 1370)
            ]
        );
    }

    #[test]
    fn dropped_events_end_the_gesture() {
        let mut t = Touch::new();
        let got = gestures(
            &mut t,
            &[
                mt(Some(0), Some(1), Some(10), Some(10)),
                vec![(EV_SYN, 3, 0), (EV_ABS, 0x35, 99)],
                vec![(EV_SYN, 0, 0)],
            ],
        );
        assert_eq!(got, [Down(10, 10), Up(10, 10)]);
        assert_eq!(
            gestures(&mut t, &[mt(Some(0), Some(4), Some(20), N)]),
            [Down(20, 10)]
        );
    }

    #[test]
    fn cancel_keeps_skipping_a_damaged_frame() {
        let mut t = Touch::new();
        t.feed(EV_SYN, 3, 0);
        t.cancel();
        assert_eq!(
            gestures(
                &mut t,
                &[vec![(EV_ABS, 0x39, 5), (EV_ABS, 0x35, 7), (EV_SYN, 0, 0)]]
            ),
            []
        );
    }

    #[test]
    fn screen_off_cancel_keeps_positions() {
        let mut t = Touch::new();
        gestures(&mut t, &[mt(Some(0), Some(1), Some(100), Some(100))]);
        t.cancel();
        let got = gestures(
            &mut t,
            &[
                mt(Some(0), Some(2), Some(300), Some(300)),
                mt(Some(0), Some(-1), N, N),
            ],
        );
        assert_eq!(got, [Down(300, 300), Up(300, 300)]);
        t.cancel();
        assert_eq!(
            gestures(&mut t, &[mt(Some(0), Some(3), Some(54), N)]),
            [Down(54, 300)]
        );
    }

    #[test]
    fn single_touch_device() {
        let mut t = Touch::new();
        let got = gestures(
            &mut t,
            &[
                vec![
                    (EV_ABS, 0, 5),
                    (EV_ABS, 1, 6),
                    (EV_KEY, 330, 1),
                    (EV_SYN, 0, 0),
                ],
                vec![(EV_KEY, 330, 0), (EV_SYN, 0, 0)],
            ],
        );
        assert_eq!(got, [Down(5, 6), Up(5, 6)]);
    }

    #[test]
    fn classify_from_sysfs() {
        let d = std::env::temp_dir().join(format!("i14-classify-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        let dev = |n: &str, name: &str, abs: &str, key: &str| {
            let p = d.join(n).join("device").join("capabilities");
            fs::create_dir_all(&p).unwrap();
            fs::write(p.parent().unwrap().join("name"), format!("{name}\n")).unwrap();
            fs::write(p.join("abs"), format!("{abs}\n")).unwrap();
            fs::write(p.join("key"), format!("{key}\n")).unwrap();
        };
        dev("event1", "mtk-kpd", "0", "1c000000000000 0");
        dev(
            "event5",
            "fts_ts",
            "661800000000000",
            "400 0 0 0 10168000000000 4d04081460000",
        );
        dev(
            "event7",
            "BT keyboard",
            "0",
            &format!("{:x}", (1u64 << 30) | (1u64 << 44)),
        );
        dev("event3", "m_alsps_input", "101", "0");
        let found = classify(&d);
        let _ = fs::remove_dir_all(&d);
        assert_eq!(
            found,
            Devices {
                touch: vec!["/dev/input/event5".into()],
                power: vec!["/dev/input/event1".into()],
                keyboard: vec!["/dev/input/event7".into()],
            }
        );
    }

    #[test]
    fn hardware_keyboard() {
        let mut k = Keyboard::new();
        let mut feed = |code: u16, v: i32| k.feed(EV_KEY, code, v);
        let ch = |c: char, ctrl: bool| {
            Some(Key {
                name: KeyName::Char(c),
                ctrl,
                alt: false,
            })
        };
        assert_eq!(feed(30, 1), ch('a', false));
        feed(42, 1);
        assert_eq!(feed(30, 1), ch('A', false));
        assert_eq!(feed(2, 1), ch('!', false));
        feed(42, 0);
        feed(29, 1);
        assert_eq!(feed(46, 1), ch('c', true));
        feed(29, 0);
        assert_eq!(feed(103, 1), Some(Key::plain(KeyName::Named("up"))));
        assert_eq!(feed(30, 0), None);
        feed(58, 1);
        assert_eq!(feed(30, 1), ch('A', false));
    }

    #[test]
    fn decodes_raw_events() {
        let mut raw = Vec::new();
        raw.extend_from_slice(&3i64.to_ne_bytes());
        raw.extend_from_slice(&500_000i64.to_ne_bytes());
        raw.extend_from_slice(&3u16.to_ne_bytes());
        raw.extend_from_slice(&0x35u16.to_ne_bytes());
        raw.extend_from_slice(&(-7i32).to_ne_bytes());
        raw.extend_from_slice(&[0; 5]); // partial trailing event
        let got: Vec<_> = events(&raw).collect();
        assert_eq!(
            got,
            [Event {
                time: 3.5,
                kind: 3,
                code: 0x35,
                value: -7
            }]
        );
    }
}
