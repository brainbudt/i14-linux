//! Whole pages against `i14ui/app.py`: one scenario of taps, keys, back, lock
//! and PIN entry runs in Python and in Rust with the same fakes (screen, Wi-Fi,
//! a missing Bluetooth device, fixed time and battery), and every rendered frame
//! must be identical. Needs `python3` and I14_FONTS (the Terminus fonts).
//! The terminal page is left out: on macOS the Rust side has no pty.
//!
//! Deliberate difference (docs/RUST-MIGRATION.md): the Bluetooth page. The Rust interface pairs
//! (visibility button, paired list, code dialog); the Python rollback stays scan-only. Frames
//! drawn while the Bluetooth page is open are therefore not compared byte for byte; instead
//! `bluetooth_page_is_the_only_deliberate_difference` checks that they do differ, that Python is
//! still scan-only, and that every other frame still matches.

use std::cell::RefCell;
use std::io;
use std::path::PathBuf;
use std::process::Command;
use std::rc::Rc;

use i14_config::Config;
use i14_sys::LocalTime;
use i14_ui::app::*;
use i14_ui::bt::Bluetooth;
use i14_ui::canvas::Canvas;
use i14_ui::font::Font;
use i14_ui::input::{Gesture, Key, KeyName};
use i14_ui::status::Battery;
use serde_json::{json, Value};

struct FakeDisplay {
    frames: Rc<RefCell<Vec<Vec<u8>>>>,
    on: bool,
}

impl Display for FakeDisplay {
    fn show(&mut self, c: &Canvas) {
        self.frames.borrow_mut().push(c.buf.clone());
    }
    fn power(&mut self, on: bool) {
        self.on = on;
    }
    fn is_on(&self) -> bool {
        self.on
    }
    fn set_brightness(&mut self, _: i64) {}
}

struct FakeWifi {
    enabled: bool,
    scanning: bool,
    message: String,
    changed: bool,
    results: Vec<ScanEntry>,
}

impl WifiApi for FakeWifi {
    fn enabled(&self) -> bool {
        self.enabled
    }
    fn scanning(&self) -> bool {
        self.scanning
    }
    fn attempt_active(&self) -> bool {
        false
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
        self.results.clone()
    }
    fn status(&mut self) -> WifiStatus {
        WifiStatus {
            wpa_state: "COMPLETED".into(),
            ssid: Some(b"ExampleNet".to_vec()),
            ip_address: Some("192.0.2.16".into()),
        }
    }
    fn saved(&mut self) -> Vec<SavedNet> {
        vec![
            SavedNet {
                id: 0,
                ssid: b"ExampleNet".to_vec(),
                priority: 0,
            },
            SavedNet {
                id: 1,
                ssid: b"OldNet".to_vec(),
                priority: 2,
            },
        ]
    }
    fn network_id(&mut self, ssid: &[u8]) -> Option<i64> {
        match ssid {
            b"ExampleNet" => Some(0),
            b"OldNet" => Some(1),
            _ => None,
        }
    }
    fn available(&mut self) -> bool {
        true
    }
    fn scan(&mut self) -> io::Result<()> {
        self.scanning = true;
        Ok(())
    }
    fn set_enabled(&mut self, on: bool) -> io::Result<bool> {
        self.enabled = on;
        self.message = format!("Wi-Fi {}", if on { "aan" } else { "uit" });
        Ok(true)
    }
    fn connect(
        &mut self,
        ssid: &[u8],
        sec: &str,
        passphrase: Option<&str>,
        hidden: bool,
    ) -> io::Result<bool> {
        self.message = format!(
            "connect {} {sec} {:?} {hidden}",
            String::from_utf8_lossy(ssid),
            passphrase.map(str::len)
        );
        Ok(false)
    }
    fn prefer(&mut self, _: &[u8]) -> io::Result<bool> {
        // as the real Wifi reports a refused socket: Python's str(OSError)
        Err(io::Error::other("[Errno 111] Connection refused"))
    }
    fn forget(&mut self, _: &[u8]) -> io::Result<bool> {
        self.message = "vergeten".into();
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

struct FakeEnv;

impl Env for FakeEnv {
    fn monotonic(&self) -> f64 {
        1000.0
    }
    fn local_time(&self) -> LocalTime {
        LocalTime {
            year: 2026,
            month: 11,
            day: 7,
            hour: 9,
            minute: 5,
            weekday: 5,
        }
    }
    fn battery(&self) -> Battery {
        Battery {
            cap: "~12".into(),
            state: "Discharging",
            word: "ontladen",
            current: "-150 mA".into(),
            volt: "3.71 V".into(),
            eta: None,
        }
    }
    fn battery_level(&self) -> String {
        "~12".into()
    }
    fn addresses(&self) -> Vec<(String, String)> {
        vec![("wlan0".into(), "192.0.2.16".into())]
    }
    fn free(&self, path: &str) -> String {
        format!("1.5 van 2.9 GB vrij {path}")
    }
    fn uptime(&self) -> i64 {
        3725
    }
    fn run(&self, _: &str) {}
    fn data_dir(&self) -> String {
        "/nonexistent-i14-data".into()
    }
}

const PY: &str = r#"
import json, os, sys, types
from i14ui import app as appmod, bt, config, fb, widgets
from i14ui.font import Font
fonts_dir, cfg_path, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
ops = json.loads(sys.stdin.read())

class FakeTime:
    @staticmethod
    def monotonic(): return 1000.0
    @staticmethod
    def strftime(fmt): return {"%H:%M": "09:05", "%d-%m-%Y": "07-11-2026"}[fmt]
    @staticmethod
    def localtime(): return types.SimpleNamespace(tm_wday=5)
appmod.time = FakeTime
widgets.time = FakeTime
appmod.battery = lambda: ("~12", "Discharging", "ontladen", "-150 mA", "3.71 V")
appmod.battery_level = lambda: "~12"
appmod.addresses = lambda: {"wlan0": "192.0.2.16"}
appmod.free = lambda p: f"1.5 van 2.9 GB vrij {p}"
real_read = appmod.read
appmod.read = lambda path, default="?": "3725.5 100.0" if path == "/proc/uptime" else real_read(path, default)
appmod.DATA = "/nonexistent-i14-data"
appmod.subprocess = types.SimpleNamespace(run=lambda *a, **k: None)

frames = []
class FakeScreen:
    def __init__(self):
        self.canvas = fb.Canvas(720, 1600)
        self.on = True
    def show(self):
        with open(os.path.join(out_dir, f"{len(frames)}.raw"), "wb") as f:
            f.write(self.canvas.buf)
        frames.append(1)
    def power(self, on): self.on = on
    def set_brightness(self, v): pass

class FakeWifi:
    enabled, scanning, attempt, message, changed = True, False, None, "", False
    scan_results = [
        {"ssid": b"ExampleNet", "signal": -50, "security": "psk"},
        {"ssid": b"NeighborNet\xff", "signal": -70, "security": "psk"},
        {"ssid": b"Cafe", "signal": -80, "security": "open"},
        {"ssid": b"OfficeNet", "signal": -90, "security": "eap"},
        {"ssid": b"NewNet6", "signal": -66, "security": "sae"},
    ]
    def monitor_fd(self): return None
    def status(self): return {"wpa_state": "COMPLETED", "ssid_bytes": b"ExampleNet", "ip_address": "192.0.2.16"}
    def saved(self): return [{"id": 0, "ssid": b"ExampleNet", "priority": 0}, {"id": 1, "ssid": b"OldNet", "priority": 2}]
    def network_id(self, ssid): return {b"ExampleNet": 0, b"OldNet": 1}.get(ssid)
    def available(self): return True
    def scan(self): self.scanning = True
    def set_enabled(self, on):
        self.enabled = on
        self.message = "Wi-Fi " + ("aan" if on else "uit")
        return True
    def connect(self, ssid, sec="psk", passphrase=None, hidden=False):
        self.message = f"connect {ssid.decode('utf-8', 'replace')} {sec} {'None' if passphrase is None else f'Some({len(passphrase)})'} {str(hidden).lower()}"
        return False
    def prefer(self, ssid): raise ConnectionRefusedError(111, "Connection refused")
    def forget(self, ssid):
        self.message = "vergeten"
        return False
    def tick(self): pass

def no_bt(path): raise FileNotFoundError(2, "No such file or directory")
f = lambda n: Font(os.path.join(fonts_dir, n))
fonts = (f("ter-132b.psf.gz"), f("ter-124b.psf.gz"), f("ter-120n.psf.gz"), f("ter-120n.psf.gz"))
a = appmod.App(FakeScreen(), fonts, config.Config(cfg_path), FakeWifi(), bt.Bluetooth(opener=no_bt))
for op in ops:
    kind = op[0]
    if kind == "render":
        a.render()
    elif kind == "tap":
        for b in a.page.buttons:
            text = b.label() if callable(b.label) else b.label
            if text == op[1]:
                x, y = b.rect[0] + 5, b.rect[1] + 5
                a.touch(("down", x, y)); a.touch(("up", x, y))
                break
        else:
            raise SystemExit(f"no button {op[1]!r} on {type(a.page).__name__}")
    elif kind == "touch":
        a.touch((op[1], op[2], op[3]))
    elif kind == "key":
        a.key((op[1], op[2], False))
    elif kind == "tick":
        if a.page.tick(): a.dirty = True
    elif kind == "lock":
        a.lock()
    elif kind == "pop":
        a.pop()
    elif kind == "page":
        print(type(a.page).__name__)
print(len(frames))
"#;

fn scenario() -> Vec<Value> {
    let mut ops = vec![json!(["render"]), json!(["page"])];
    let tap = |l: &str| json!(["tap", l]);
    let r = || json!(["render"]);
    ops.extend([tap("Wi-Fi"), r(), json!(["page"])]);
    // list rows: 0 ExampleNet (connected), 1 NeighborNet, 2 Cafe, 3 OfficeNet, 4 NewNet6, then OldNet (saved, not near)
    let row = |i: i32| {
        [
            json!(["touch", "down", 50, 300 + 20 + i * 100]),
            json!(["touch", "up", 50, 300 + 20 + i * 100]),
        ]
    };
    ops.extend(row(0));
    ops.extend([
        r(),
        json!(["page"]),
        tap("Voorrang geven"),
        r(),
        tap("Vergeten"),
        r(),
        json!(["touch", "down", 5, 5]),
        json!(["touch", "up", 5, 5]),
        r(),
    ]);
    ops.extend(row(1));
    ops.extend([
        r(),
        json!(["key", "h", false]),
        json!(["key", "!", false]),
        json!(["key", "backspace", false]),
        json!(["key", "x", true]),
    ]);
    ops.extend([
        r(),
        tap("toon"),
        r(),
        json!(["key", "enter", false]),
        r(),
        json!(["pop"]),
    ]);
    ops.extend(row(2));
    ops.extend([r(), tap("Verbinden (open netwerk)"), r(), json!(["pop"])]);
    ops.extend(row(3));
    ops.extend([r(), json!(["pop"])]);
    // drag the list, then a row tap after scrolling: OldNet
    ops.extend([
        json!(["touch", "down", 50, 900]),
        json!(["touch", "move", 50, 850]),
        json!(["touch", "up", 50, 850]),
        r(),
    ]);
    ops.extend([
        tap("Wi-Fi: aan"),
        r(),
        tap("zoeken..."),
        r(),
        tap("Verborgen"),
        r(),
    ]);
    ops.extend([
        json!(["key", "enter", false]),
        r(),
        json!(["touch", "down", 100, 150]),
        json!(["key", "A", false]),
        json!(["key", "enter", false]),
        json!(["key", "s", false]),
        r(),
    ]);
    ops.extend([
        json!(["touch", "down", 100, 330]),
        json!(["key", "q", false]),
        json!(["key", "enter", false]),
        r(),
        json!(["pop"]),
        json!(["pop"]),
        r(),
    ]);
    ops.extend([
        tap("Bluetooth"),
        r(),
        tap("Bluetooth: uit"),
        r(),
        tap("Zoeken"),
        json!(["tick"]),
        r(),
        json!(["pop"]),
    ]);
    ops.extend([
        tap("Instellingen"),
        r(),
        tap("+"),
        tap("+"),
        tap("-"),
        tap("Scherm uit na: 1 min"),
        tap("Laadgrens: uit"),
        tap("Stoppen bij: ~80%"),
        r(),
    ]);
    ops.extend([
        tap("Pincode instellen"),
        r(),
        tap("1"),
        tap("2"),
        tap("OK"),
        r(),
    ]);
    for d in ["1", "2", "3", "4", "OK", "1", "2", "3", "5", "OK"] {
        ops.push(tap(d));
    }
    ops.push(r());
    for d in ["1", "2", "3", "4", "OK", "1", "2", "3", "4", "OK"] {
        ops.push(tap(d));
    }
    ops.extend([r(), json!(["page"]), json!(["lock"]), r()]);
    for _ in 0..5 {
        ops.extend(["9", "9", "9", "9", "OK"].map(tap));
    }
    ops.extend([
        r(),
        json!(["touch", "down", 5, 5]),
        json!(["touch", "up", 5, 5]),
        r(),
    ]);
    ops.extend([
        json!(["key", "1", false]),
        json!(["key", "2", false]),
        json!(["key", "backspace", false]),
        r(),
        json!(["page"]),
    ]);
    ops.extend([
        json!(["pop"]),
        r(),
        tap("Pincode wijzigen"),
        r(),
        json!(["page"]),
        json!(["pop"]),
        r(),
        json!(["page"]),
    ]);
    ops.extend([
        tap("Pincode verwijderen"),
        r(),
        json!(["pop"]),
        json!(["pop"]),
        r(),
    ]);
    ops.extend([
        tap("Aan/uit"),
        r(),
        tap("Herstarten"),
        r(),
        tap("Noodmodus (USB-shell)"),
        tap("Zeker? Tik nogmaals: Noodmodus (USB-shell)"),
        r(),
        json!(["page"]),
    ]);
    ops
}

fn page_name(p: &Page) -> &'static str {
    match p {
        Page::Home(_) => "Home",
        Page::Terminal(_) => "TerminalPage",
        Page::Wifi(_) => "WifiPage",
        Page::Network(_) => "NetworkPage",
        Page::Hidden(_) => "HiddenPage",
        Page::Bluetooth(_) => "BluetoothPage",
        Page::Settings(_) => "SettingsPage",
        Page::PinSet(_) => "PinSetPage",
        Page::Lock(_) => "LockPage",
        Page::Power(_) => "PowerPage",
    }
}

/// Frames, page names (for "page" ops) and the indices of frames drawn on the Bluetooth page.
fn run_rust(ops: &[Value], fonts: &str, cfg: &str) -> (Vec<Vec<u8>>, Vec<String>, Vec<usize>) {
    let frames = Rc::new(RefCell::new(Vec::new()));
    let f = |n: &str| Font::open(format!("{fonts}/{n}")).unwrap();
    let fonts = Fonts {
        big: f("ter-132b.psf.gz"),
        mid: f("ter-124b.psf.gz"),
        small: f("ter-120n.psf.gz"),
        term: f("ter-120n.psf.gz"),
    };
    let wifi = FakeWifi {
        enabled: true,
        scanning: false,
        message: String::new(),
        changed: false,
        results: [
            (&b"ExampleNet"[..], -50, "psk"),
            (b"NeighborNet\xff", -70, "psk"),
            (b"Cafe", -80, "open"),
            (b"OfficeNet", -90, "eap"),
            (b"NewNet6", -66, "sae"),
        ]
        .iter()
        .map(|(s, sig, sec)| ScanEntry {
            ssid: s.to_vec(),
            signal: *sig,
            security: sec.to_string(),
        })
        .collect(),
    };
    let bt = Bluetooth::with_opener(
        "/dev/stpbt",
        Box::new(|_| Err(io::Error::from_raw_os_error(2))),
    );
    let display = FakeDisplay {
        frames: frames.clone(),
        on: true,
    };
    let mut app = App::new(
        Canvas::new(720, 1600, None),
        Box::new(display),
        fonts,
        Config::load(cfg),
        Box::new(wifi),
        bt,
        Box::new(FakeEnv),
    );
    let mut pages = Vec::new();
    let mut bt_frames = Vec::new();
    for op in ops {
        match op[0].as_str().unwrap() {
            "render" => {
                if matches!(app.stack.last(), Some(Page::Bluetooth(_))) {
                    bt_frames.push(frames.borrow().len());
                }
                app.render()
            }
            "tap" => {
                let label = op[1].as_str().unwrap();
                app.sync_labels();
                let page = app.stack.last_mut().unwrap();
                let r = page
                    .buttons()
                    .iter()
                    .find(|b| b.label == label)
                    .map(|b| b.rect);
                let Some(r) = r else {
                    panic!("no button {label:?} on {}", page_name(page))
                };
                app.touch(Gesture::Down(r.x + 5, r.y + 5));
                app.touch(Gesture::Up(r.x + 5, r.y + 5));
            }
            "touch" => {
                let (x, y) = (
                    op[2].as_i64().unwrap() as i32,
                    op[3].as_i64().unwrap() as i32,
                );
                app.touch(match op[1].as_str().unwrap() {
                    "down" => Gesture::Down(x, y),
                    "move" => Gesture::Move(x, y),
                    _ => Gesture::Up(x, y),
                });
            }
            "key" => {
                let name = op[1].as_str().unwrap();
                let mut it = name.chars();
                let k = match (it.next(), it.next()) {
                    (Some(c), None) => KeyName::Char(c),
                    _ => KeyName::Named(Box::leak(name.to_string().into_boxed_str())),
                };
                app.key(Key {
                    name: k,
                    ctrl: op[2].as_bool().unwrap(),
                    alt: false,
                });
            }
            "tick" => {
                if app.tick() {
                    app.cx.dirty = true;
                }
            }
            "lock" => app.lock(),
            "pop" => app.pop(),
            "page" => pages.push(page_name(app.stack.last().unwrap()).to_string()),
            other => panic!("{other}"),
        }
    }
    let out = frames.borrow().clone();
    (out, pages, bt_frames)
}

#[test]
fn pages_render_like_python() {
    let Some(fonts) = std::env::var("I14_FONTS").ok() else {
        eprintln!("skipped: set I14_FONTS to the Terminus font directory");
        return;
    };
    let tmp = std::env::temp_dir().join(format!("i14-app-parity-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&tmp);
    let out = tmp.join("frames");
    std::fs::create_dir_all(&out).unwrap();
    let ops = scenario();
    let lib = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../rootfs/overlay/usr/lib");
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(PY)
        .arg(&fonts)
        .arg(tmp.join("py-ui.json"))
        .arg(&out)
        .env("PYTHONPATH", lib)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    child
        .stdin
        .take()
        .unwrap()
        .write_all(serde_json::to_string(&ops).unwrap().as_bytes())
        .unwrap();
    let res = child.wait_with_output().unwrap();
    assert!(
        res.status.success(),
        "python: {}",
        String::from_utf8_lossy(&res.stderr)
    );
    let stdout = String::from_utf8(res.stdout).unwrap();
    let mut lines: Vec<String> = stdout.lines().map(str::to_string).collect();
    let n: usize = lines.pop().unwrap().parse().unwrap();

    let (frames, pages, bt_frames) =
        run_rust(&ops, &fonts, tmp.join("rs-ui.json").to_str().unwrap());
    eprintln!(
        "{} frames, pages {:?}, Bluetooth frames {:?}",
        n, lines, bt_frames
    );
    assert_eq!(pages, lines, "page sequence");
    assert_eq!(frames.len(), n, "frame count");
    // the deliberate difference: Bluetooth-page frames must differ (pairing UI vs scan-only)
    assert_eq!(
        bt_frames.len(),
        3,
        "the scenario draws the Bluetooth page three times"
    );
    for &i in &bt_frames {
        let want = std::fs::read(out.join(format!("{i}.raw"))).unwrap();
        assert_ne!(
            want, frames[i],
            "Bluetooth frame {i} is identical: is the difference gone?"
        );
    }
    for (i, got) in frames.iter().enumerate() {
        if bt_frames.contains(&i) {
            continue;
        }
        let want = std::fs::read(out.join(format!("{i}.raw"))).unwrap();
        if &want != got {
            let first = want.iter().zip(got).position(|(a, b)| a != b).unwrap_or(0);
            std::fs::write(tmp.join(format!("rust-{i}.raw")), got).unwrap();
            panic!(
                "frame {i} differs from byte {first} (row {}); frames in {}",
                first / (720 * 4),
                tmp.display()
            );
        }
    }
    let _ = std::fs::remove_dir_all(&tmp);
}

/// The Python rollback stays scan-only; the Rust page offers pairing. Runs without fonts.
#[test]
fn bluetooth_page_is_the_only_deliberate_difference() {
    let py = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../../rootfs/overlay/usr/lib/i14ui/app.py");
    let py = std::fs::read_to_string(py).unwrap();
    assert!(py.contains("Alleen zoeken; koppelen wordt nog niet ondersteund."));
    let rs = include_str!("../src/app.rs");
    assert!(
        !rs.contains("Alleen zoeken; koppelen"),
        "the Rust page pairs"
    );
    assert!(rs.contains("\"Zichtbaar (2 min)\""));
}
