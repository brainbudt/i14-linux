//! Stock MT6735: idle at 299 MHz with display off; release the cap on demand.
//!
//! The vendor driver overrides the governor twice during early suspend. Use its
//! existing max-frequency control; never program PLLs, voltages or CPU hotplug.
//!
//! Port of rootfs/overlay/usr/sbin/i14-cpufreq (Python). File reads follow
//! Python's `Path.read_text()` + `int()`/`str.split()` rules so garbage in sysfs
//! gives the same decisions and the same error class.

use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

/// The Python exception classes the policy can raise.
#[derive(Debug)]
pub enum Error {
    /// OSError (missing file, permission, ...).
    Os(io::Error),
    /// ValueError (bad counters/state, int() parse, UTF-8 decode).
    Value(String),
    /// RuntimeError (unexpected controls, external change, readback mismatch).
    Runtime(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        match self {
            Error::Os(e) => write!(f, "OSError: {e}"),
            Error::Value(m) => write!(f, "ValueError: {m}"),
            Error::Runtime(m) => write!(f, "RuntimeError: {m}"),
        }
    }
}

impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self {
        Error::Os(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;
type Ticks = [i128; 8];

/// Python's str.isspace() set (Rust's lacks U+001C..U+001F).
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

fn py_split(s: &str) -> impl Iterator<Item = &str> {
    s.split(py_space).filter(|f| !f.is_empty())
}

/// str.splitlines() (the text has no '\r' left after read_text()).
fn py_splitlines(s: &str) -> Vec<&str> {
    let eol = |c| {
        matches!(
            c,
            '\n' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}'
        )
    };
    if s.is_empty() {
        return Vec::new();
    }
    s.strip_suffix(eol).unwrap_or(s).split(eol).collect()
}

/// Path.read_text(): strict UTF-8, universal newlines.
fn read_text(path: &Path) -> Result<String> {
    let text = String::from_utf8(fs::read(path)?)
        .map_err(|_| Error::Value("'utf-8' codec can't decode".into()))?;
    Ok(text.replace("\r\n", "\n").replace('\r', "\n"))
}

/// int(str) for base 10: surrounding whitespace, a sign, digits with single
/// underscores between them. Values beyond i128 saturate (kernel counters are u64).
fn py_int(text: &str) -> Result<i128> {
    let bad = || Error::Value(format!("invalid literal for int() with base 10: {text:?}"));
    // int() strips only ASCII " \t\n\v\f\r" plus non-ASCII spaces, not U+001C..U+001F.
    let s = text.trim_matches(char::is_whitespace);
    let (neg, digits) = match s.as_bytes().first() {
        Some(b'-') => (true, &s[1..]),
        Some(b'+') => (false, &s[1..]),
        _ => (false, s),
    };
    if digits.is_empty()
        || digits.starts_with('_')
        || digits.ends_with('_')
        || digits.contains("__")
    {
        return Err(bad());
    }
    let mut value: i128 = 0;
    let mut count = 0;
    for c in digits.chars().filter(|&c| c != '_') {
        let d = c.to_digit(10).filter(|_| c.is_ascii()).ok_or_else(bad)? as i128;
        value = value.saturating_mul(10).saturating_add(d);
        count += 1;
    }
    if count > 4300 {
        return Err(Error::Value(
            "exceeds the limit (4300 digits) for integer string conversion".into(),
        ));
    }
    Ok(if neg { -value } else { value })
}

fn read_int(path: &Path) -> Result<i128> {
    py_int(&read_text(path)?)
}

pub struct Policy {
    pub root: PathBuf,
    pub proc: PathBuf,
    pub cpu: PathBuf,
    pub cap: PathBuf,
    pub fix: PathBuf,
    pub started: bool,
    pub applied: i128,
    pub quiet: u32,
    pub original_fix: Option<i128>,
    pub previous: BTreeMap<String, Ticks>,
    /// syslog lines not yet sent; the service drains it after every call.
    pub log: Vec<String>,
}

impl Policy {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        let proc = root.join("proc/cpufreq");
        let cpu = root.join("sys/devices/system/cpu/cpu0/cpufreq");
        Policy {
            cap: proc.join("cpufreq_limited_max_freq_by_user"),
            fix: proc.join("cpufreq_fix_freq_in_es"),
            root,
            proc,
            cpu,
            started: false,
            applied: 0,
            quiet: 0,
            original_fix: None,
            previous: BTreeMap::new(),
            log: Vec::new(),
        }
    }

    pub fn counters(&self) -> Result<BTreeMap<String, Ticks>> {
        let mut values = BTreeMap::new();
        for line in py_splitlines(&read_text(&self.root.join("proc/stat"))?) {
            let fields: Vec<&str> = py_split(line).collect();
            let Some(name) = fields.first() else { continue };
            let n = name.strip_prefix("cpu").unwrap_or("");
            if !name.starts_with("cpu") || n.is_empty() || !n.bytes().all(|b| b.is_ascii_digit()) {
                continue;
            }
            // exclude duplicated guest time
            let ticks = fields[1..fields.len().min(9)]
                .iter()
                .map(|v| py_int(v))
                .collect::<Result<Vec<_>>>()?;
            if ticks.len() != 8 || ticks.iter().any(|&t| t < 0) {
                return Err(Error::Value("invalid per-CPU counters".into()));
            }
            values.insert(name.to_string(), ticks.try_into().unwrap());
        }
        if values.is_empty() {
            return Err(Error::Value("no per-CPU counters".into()));
        }
        Ok(values)
    }

    pub fn screen_off(&self) -> Result<bool> {
        let text = read_text(&self.proc.join("cpufreq_state"))?;
        match text.split('\n').find_map(|l| {
            l.strip_prefix("dvfs_disable_by_early_suspend = ")
                .filter(|v| *v == "0" || *v == "1")
        }) {
            Some(v) => Ok(v == "1"),
            None => Err(Error::Value("missing driver screen state".into())),
        }
    }

    pub fn write(path: &Path, value: i128) -> Result<()> {
        fs::write(path, format!("{value}\n"))?;
        if read_int(path)? != value {
            return Err(Error::Runtime(format!(
                "CPU control readback mismatch: {}",
                path.display()
            )));
        }
        Ok(())
    }

    pub fn start(&mut self) -> Result<()> {
        if read_text(&self.cpu.join("scaling_driver"))?.trim_matches(py_space) != "mt-cpufreq" {
            return Err(Error::Runtime(
                "requires the stock MT6735 CPUfreq driver".into(),
            ));
        }
        if !matches!(
            read_text(&self.cpu.join("scaling_governor"))?.trim_matches(py_space),
            "interactive" | "ondemand"
        ) {
            return Err(Error::Runtime(
                "requires an automatic CPUfreq governor".into(),
            ));
        }
        let available = read_text(&self.cpu.join("scaling_available_frequencies"))?;
        let original_fix = read_int(&self.fix)?;
        self.original_fix = Some(original_fix);
        if !py_split(&available).any(|f| f == "299000")
            || !(0..=1).contains(&original_fix)
            || read_int(&self.cap)? != 0
        {
            return Err(Error::Runtime(
                "unexpected CPU controls or an existing frequency cap".into(),
            ));
        }
        self.previous = self.counters()?;
        self.screen_off()?;
        self.started = true;
        Self::write(&self.fix, 0)
    }

    pub fn limit(&mut self, value: i128) -> Result<()> {
        if read_int(&self.cap)? != self.applied {
            return Err(Error::Runtime(
                "CPU limit changed externally; leaving that value alone".into(),
            ));
        }
        if value != self.applied {
            // Unlike the Python, `applied` follows the write itself (set after it
            // succeeds, before the readback): a failed 299000 -> 0 write leaves the cap
            // ours, and a written value with a failed readback is ours too, so close()
            // resets the cap in both cases.
            fs::write(&self.cap, format!("{value}\n"))?;
            self.applied = value;
            if read_int(&self.cap)? != value || tests::readback_fails() {
                return Err(Error::Runtime(format!(
                    "CPU control readback mismatch: {}",
                    self.cap.display()
                )));
            }
            self.log.push(if value != 0 {
                format!("CPU max: {value} kHz")
            } else {
                "CPU max: governor control".into()
            });
        }
        Ok(())
    }

    pub fn tick(&mut self) -> Result<()> {
        if read_int(&self.fix)? != 0 {
            return Err(Error::Runtime(
                "screen-off CPU fix changed externally".into(),
            ));
        }
        let current = self.counters()?;
        let mut load = Vec::new();
        if current.keys().eq(self.previous.keys()) {
            for (cpu, ticks) in &current {
                let delta: Vec<i128> = ticks
                    .iter()
                    .zip(&self.previous[cpu])
                    .map(|(a, b)| a - b)
                    .collect();
                let total = delta.iter().fold(0i128, |s, &d| s.saturating_add(d));
                if delta.iter().any(|&d| d < 0) || total <= 0 {
                    load.clear();
                    break;
                }
                load.push((total - delta[3] - delta[4]) as f64 / total as f64);
            }
        }
        self.previous = current;
        // Use the busiest core: an aggregate would hide a single busy thread.
        let busy = load.into_iter().reduce(f64::max).unwrap_or(1.0);
        if !self.screen_off()? || busy >= 0.30 {
            self.quiet = 0;
            self.limit(0)
        } else if busy < 0.05 {
            self.quiet = (self.quiet + 1).min(5);
            self.limit(if self.quiet >= 5 {
                299000
            } else {
                self.applied
            })
        } else {
            self.quiet = 0;
            self.limit(self.applied)
        }
    }

    pub fn close(&mut self) -> Result<()> {
        if self.started {
            let first = (|| -> Result<()> {
                if read_int(&self.cap)? == self.applied {
                    Self::write(&self.cap, 0)?;
                }
                Ok(())
            })();
            // finally: runs even if the cap step failed; its own error wins.
            if read_int(&self.fix)? == 0 {
                Self::write(&self.fix, self.original_fix.unwrap_or(0))?;
            }
            first?;
            self.started = false;
        }
        Ok(())
    }
}

/// Test seam: a readback that fails after a successful write (not reachable with plain files).
#[cfg(not(test))]
mod tests {
    pub fn readback_fails() -> bool {
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    thread_local!(static FAIL_READBACK: Cell<bool> = const { Cell::new(false) });

    pub fn readback_fails() -> bool {
        FAIL_READBACK.with(Cell::get)
    }

    #[test]
    fn written_cap_with_failed_readback_is_still_reset_on_stop() {
        // Regression: the cap write succeeds but its readback fails; the cap is then
        // 299000 and must still count as ours, so close() resets it.
        let root = std::env::temp_dir().join(format!("i14-cpufreq-rb-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let (proc_dir, cpu) = (
            root.join("proc/cpufreq"),
            root.join("sys/devices/system/cpu/cpu0/cpufreq"),
        );
        fs::create_dir_all(&proc_dir).unwrap();
        fs::create_dir_all(&cpu).unwrap();
        fs::write(cpu.join("scaling_driver"), "mt-cpufreq").unwrap();
        fs::write(cpu.join("scaling_governor"), "interactive").unwrap();
        fs::write(cpu.join("scaling_available_frequencies"), "1300000 299000").unwrap();
        fs::write(proc_dir.join("cpufreq_fix_freq_in_es"), "1").unwrap();
        fs::write(proc_dir.join("cpufreq_limited_max_freq_by_user"), "0").unwrap();
        fs::write(
            proc_dir.join("cpufreq_state"),
            "dvfs_disable_by_early_suspend = 1\n",
        )
        .unwrap();
        fs::write(
            root.join("proc/stat"),
            "cpu 1 0 0 1\ncpu0 1 0 0 1 0 0 0 0\n",
        )
        .unwrap();
        let mut p = Policy::new(&root);
        p.start().unwrap();
        FAIL_READBACK.with(|f| f.set(true));
        let r = p.limit(299000);
        FAIL_READBACK.with(|f| f.set(false));
        assert!(matches!(r, Err(Error::Runtime(_))));
        p.close().unwrap();
        let cap = fs::read_to_string(proc_dir.join("cpufreq_limited_max_freq_by_user")).unwrap();
        let _ = fs::remove_dir_all(&root);
        assert_eq!(cap.trim(), "0");
    }

    #[test]
    fn int_follows_python() {
        for (s, v) in [
            ("0", 0),
            (" 1\n", 1),
            ("+5", 5),
            ("-3", -3),
            ("1_000", 1000),
            ("007", 7),
            ("\u{a0}2\u{85}", 2),
        ] {
            assert_eq!(py_int(s).unwrap(), v, "{s:?}");
        }
        for s in [
            "",
            " ",
            "_1",
            "1_",
            "1__0",
            "0x10",
            "1.0",
            "- 1",
            "１",
            "\x1c0\x1f",
        ] {
            assert!(matches!(py_int(s), Err(Error::Value(_))), "{s:?}");
        }
    }

    #[test]
    fn splitlines_follows_python() {
        let v = py_splitlines("a\n\nb\x0bc\n");
        assert_eq!(v, ["a", "", "b", "c"]);
        assert!(py_splitlines("").is_empty());
        assert_eq!(py_splitlines("\n"), [""]);
    }
}
