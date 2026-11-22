//! The shared I14 settings file (`ui.json`): brightness, screen timeout, PIN and
//! charge limit. Same file, rules and PIN hash as `i14ui/config.py`, so the
//! Python and Rust programs can read each other's settings.
//!
//! A file that exists but cannot be read or is damaged fails closed: `damaged`
//! is set, the screen stays locked (no PIN opens it) and nothing is written
//! until the file is repaired or removed over SSH or the USB console. No file at
//! all means first use: no PIN.

use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};
use sha2::Sha256;

pub const PATH: &str = "/mnt/data/media/0/i14-linux/ui.json";
/// Screen timeouts in seconds; 0 = never.
pub const TIMEOUTS: [u32; 6] = [30, 60, 120, 300, 600, 0];
pub const CHARGE_LIMITS: [u8; 5] = [60, 70, 80, 90, 100];
pub const PIN_ROUNDS: u32 = 100_000;

#[derive(Clone, Debug, PartialEq)]
pub struct Settings {
    pub brightness: u8,
    pub timeout: u32,
    /// The stored PIN entry ({"salt", "hash", "rounds"}, extra keys kept), if any.
    pub pin: Option<Map<String, Value>>,
    pub charge_limit_enabled: bool,
    pub charge_limit: u8,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            brightness: 120,
            timeout: 60,
            pin: None,
            charge_limit_enabled: false,
            charge_limit: 80,
        }
    }
}

pub struct Config {
    pub path: PathBuf,
    pub data: Settings,
    pub damaged: bool,
}

/// Python's `int(x)` for the JSON values it accepts.
fn py_int(v: &Value) -> Option<i64> {
    match v {
        Value::Bool(b) => Some(*b as i64),
        Value::Number(n) => n.as_i64().or_else(|| {
            n.as_f64()
                .filter(|f| f.is_finite())
                .map(|f| f.trunc() as i64)
        }),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

/// Python's `x in [ints]` (so 60.0 matches 60, but true/"60" do not match).
fn py_in(v: &Value, allowed: &[u32]) -> Option<u32> {
    let n = v.as_f64().filter(|_| v.is_number())?;
    allowed.iter().copied().find(|&a| a as f64 == n)
}

fn parse(text: &str) -> Result<Settings, String> {
    let loaded: Value = serde_json::from_str(text).map_err(|e| e.to_string())?;
    let obj = loaded.as_object().ok_or("not an object")?;
    let mut s = Settings::default();
    match obj.get("pin") {
        None | Some(Value::Null) => {}
        Some(Value::Object(p))
            if ["salt", "hash", "rounds"]
                .iter()
                .all(|k| p.contains_key(*k)) =>
        {
            s.pin = Some(p.clone())
        }
        Some(_) => return Err("bad pin entry".into()),
    }
    if let Some(t) = obj.get("timeout").and_then(|v| py_in(v, &TIMEOUTS)) {
        s.timeout = t;
    }
    if let Some(b) = obj.get("brightness") {
        // A value int() rejects falls back to the default; others are clamped.
        s.brightness = py_int(b).map_or(120, |b| b.clamp(5, 255) as u8);
    }
    let enabled = obj
        .get("charge_limit_enabled")
        .map_or(Some(false), Value::as_bool);
    let limit = match obj.get("charge_limit") {
        None => Some(80),
        Some(v) => v
            .as_u64()
            .and_then(|n| CHARGE_LIMITS.iter().copied().find(|&l| l as u64 == n)),
    };
    if let (Some(e), Some(l)) = (enabled, limit) {
        s.charge_limit_enabled = e;
        s.charge_limit = l;
    }
    Ok(s)
}

fn pbkdf2(pin: &str, salt: &[u8], rounds: u32) -> [u8; 32] {
    let mut out = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<Sha256>(pin.as_bytes(), salt, rounds, &mut out);
    out
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(s: &str) -> Option<Vec<u8>> {
    // bytes.fromhex accepts whitespace between bytes; the files we write never have any.
    let s: String = s.split_whitespace().collect();
    if !s.len().is_multiple_of(2) || !s.is_ascii() {
        return None;
    }
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).ok())
        .collect()
}

/// Constant-time comparison of two strings of the same length.
fn same(a: &[u8], b: &[u8]) -> bool {
    a.len() == b.len() && a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

impl Config {
    pub fn load(path: impl AsRef<Path>) -> Self {
        let path = path.as_ref().to_path_buf();
        let (data, damaged) = match fs::read(&path) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => (Settings::default(), false),
            Err(_) => (Settings::default(), true),
            Ok(bytes) => match std::str::from_utf8(&bytes)
                .map_err(|e| e.to_string())
                .and_then(parse)
            {
                Ok(s) => (s, false),
                Err(_) => (Settings::default(), true),
            },
        };
        Self {
            path,
            data,
            damaged,
        }
    }

    pub fn has_pin(&self) -> bool {
        self.damaged || self.data.pin.is_some()
    }

    /// Changes settings and saves them; on failure the old settings stay in
    /// effect. Never writes over a damaged file (it may hold the PIN).
    pub fn update(&mut self, change: impl FnOnce(&mut Settings)) -> bool {
        if self.damaged {
            return false;
        }
        let old = self.data.clone();
        change(&mut self.data);
        if self.save().is_ok() {
            return true;
        }
        self.data = old;
        false
    }

    pub fn set_pin(&mut self, pin: Option<&str>) -> bool {
        let entry = match pin {
            None => None,
            Some(pin) => {
                let mut salt = [0u8; 16];
                if File::open("/dev/urandom")
                    .and_then(|mut f| f.read_exact(&mut salt))
                    .is_err()
                {
                    return false;
                }
                let mut m = Map::new();
                m.insert("salt".into(), hex(&salt).into());
                m.insert("hash".into(), hex(&pbkdf2(pin, &salt, PIN_ROUNDS)).into());
                m.insert("rounds".into(), PIN_ROUNDS.into());
                Some(m)
            }
        };
        self.update(|s| s.pin = entry)
    }

    pub fn check_pin(&self, pin: &str) -> bool {
        if self.damaged {
            return false;
        }
        let Some(p) = &self.data.pin else { return true };
        // A damaged entry never unlocks; remove it over SSH/USB.
        let salt = p.get("salt").and_then(Value::as_str).and_then(unhex);
        let rounds = p
            .get("rounds")
            .and_then(py_int)
            .filter(|&r| r >= 1 && r <= u32::MAX as i64);
        let stored = p.get("hash").and_then(Value::as_str);
        match (salt, rounds, stored) {
            (Some(salt), Some(rounds), Some(stored)) => same(
                hex(&pbkdf2(pin, &salt, rounds as u32)).as_bytes(),
                stored.as_bytes(),
            ),
            _ => false,
        }
    }

    fn to_json(&self) -> String {
        let d = &self.data;
        let pin = d
            .pin
            .as_ref()
            .map_or("null".to_string(), |p| Value::Object(p.clone()).to_string());
        format!(
            "{{\"brightness\": {}, \"timeout\": {}, \"pin\": {}, \"charge_limit_enabled\": {}, \"charge_limit\": {}}}",
            d.brightness, d.timeout, pin, d.charge_limit_enabled, d.charge_limit
        )
    }

    /// Atomic replace: temp file (0600) in the same directory, fsync, rename.
    fn save(&self) -> io::Result<()> {
        let dir = self
            .path
            .parent()
            .filter(|d| !d.as_os_str().is_empty())
            .unwrap_or(Path::new("."));
        fs::create_dir_all(dir)?;
        let tmp = dir.join(format!(".ui-{}-{}", std::process::id(), unique()));
        let result = (|| {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&tmp)?;
            f.set_permissions(fs::Permissions::from_mode(0o600))?;
            f.write_all(self.to_json().as_bytes())?;
            f.sync_all()?;
            fs::rename(&tmp, &self.path)
        })();
        if result.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        result
    }
}

fn unique() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static N: AtomicU64 = AtomicU64::new(0);
    N.fetch_add(1, Ordering::Relaxed)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmpdir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("i14-config-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn missing_file_is_first_use() {
        let c = Config::load(tmpdir("missing").join("ui.json"));
        assert!(!c.damaged && !c.has_pin() && c.check_pin("anything"));
        assert_eq!(c.data, Settings::default());
    }

    #[test]
    fn damaged_file_fails_closed_and_is_never_overwritten() {
        let p = tmpdir("damaged").join("ui.json");
        for bad in [
            "{",
            "[]",
            "{\"pin\": 5}",
            "{\"pin\": {\"salt\": \"00\"}}",
            "\u{ff}",
        ] {
            fs::write(&p, bad).unwrap();
            let mut c = Config::load(&p);
            assert!(c.damaged && c.has_pin() && !c.check_pin(""), "{bad}");
            assert!(!c.update(|s| s.brightness = 9));
            assert_eq!(fs::read_to_string(&p).unwrap(), bad);
        }
    }

    #[test]
    fn normalizes_values_like_python() {
        let p = tmpdir("norm").join("ui.json");
        fs::write(&p, r#"{"brightness": "300", "timeout": 61, "charge_limit_enabled": true, "charge_limit": 75, "x": 1}"#)
            .unwrap();
        let c = Config::load(&p);
        assert!(!c.damaged);
        assert_eq!((c.data.brightness, c.data.timeout), (255, 60));
        assert_eq!(
            (c.data.charge_limit_enabled, c.data.charge_limit),
            (false, 80),
            "both reset together"
        );
        fs::write(&p, r#"{"brightness": 2.9, "timeout": 300.0, "charge_limit_enabled": true, "charge_limit": 90}"#)
            .unwrap();
        let c = Config::load(&p);
        assert_eq!(
            (
                c.data.brightness,
                c.data.timeout,
                c.data.charge_limit_enabled,
                c.data.charge_limit
            ),
            (5, 300, true, 90)
        );
        fs::write(&p, r#"{"brightness": [1], "charge_limit": true}"#).unwrap();
        let c = Config::load(&p);
        assert_eq!((c.data.brightness, c.data.charge_limit), (120, 80));
    }

    #[test]
    fn pin_round_trip_and_save() {
        let p = tmpdir("pin").join("sub").join("ui.json");
        let mut c = Config::load(&p);
        assert!(c.update(|s| s.brightness = 77));
        assert!(c.set_pin(Some("1234")));
        let c = Config::load(&p);
        assert!(!c.damaged && c.has_pin());
        assert!(c.check_pin("1234") && !c.check_pin("1235"));
        assert_eq!(c.data.brightness, 77);
        assert_eq!(
            fs::metadata(&p).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let leftovers = fs::read_dir(p.parent().unwrap()).unwrap().count();
        assert_eq!(leftovers, 1, "no temp files left");
    }

    #[test]
    fn known_pbkdf2_vector() {
        // RFC 7914 section 11: PBKDF2-HMAC-SHA256("passwd", "salt", 1, 64) starts 55ac046e.
        assert!(hex(&pbkdf2("passwd", b"salt", 1)).starts_with("55ac046e56e3089f"));
    }
}
pub mod charge_status;
