//! Own stock BCCT slot 00, reserved for i14-power; other thermal slots stay intact.
//!
//! The caller must keep USB unlimited disabled: that stock policy bypasses BCCT.
//! No access to current_cmd belongs here (even reading it disables charging).

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use crate::py::{self, Error};

pub const CONFIG: &str = "/proc/driver/thermal/clbcct";
pub const THERMAL_ROOT: &str = "/sys/class/thermal";

/// Writes one kernel command; replaceable so tests can stand in for the kernel.
pub type Writer = Box<dyn FnMut(&Path, &str) -> Result<(), Error>>;

/// The charger backend the guard drives (BCCT on the phone, fakes in tests).
pub trait Control {
    fn paused(&mut self) -> Result<bool, Error>;
    fn set_paused(&mut self, paused: bool) -> Result<(), Error>;
    fn release(&mut self) -> Result<(), Error>;
}

/// One write(2) of `data`; a short write is an error and is not retried.
pub fn write_once(f: &mut impl Write, data: &[u8], path: &Path) -> Result<(), Error> {
    let n = loop {
        match f.write(data) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue, // os.write retries EINTR (PEP 475)
            r => break r.map_err(|e| Error::os(&e, None))?,
        }
    };
    if n != data.len() {
        return Err(Error(format!(
            "[Errno {}] short kernel command write: {}",
            libc::EIO,
            py::repr_path(path)
        )));
    }
    Ok(())
}

/// The vendor proc callbacks require a complete command in one write.
pub fn write_command(path: &Path, text: &str) -> Result<(), Error> {
    if text.contains('\n') || text.contains('\r') {
        return Err(Error::value("kernel command must be one line"));
    }
    let data = format!("{text}\n");
    let mut f = OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_CLOEXEC)
        .open(path)
        .map_err(|e| Error::os(&e, Some(path)))?;
    write_once(&mut f, data.as_bytes(), path)
    // close(2) errors are not seen here (std drops the descriptor); Python's os.close would raise them.
}

/// `Path.read_text(encoding="ascii")`.
fn read_ascii(path: &Path) -> Result<String, Error> {
    let bytes = fs::read(path).map_err(|e| Error::os(&e, Some(path)))?;
    if let Some(i) = bytes.iter().position(|b| !b.is_ascii()) {
        return Err(Error(format!(
            "'ascii' codec can't decode byte 0x{:02x} in position {i}: ordinal not in range(128)",
            bytes[i]
        )));
    }
    Ok(String::from_utf8(bytes).expect("ASCII is UTF-8"))
}

/// `Path.glob` for one level: directory order, unreadable directories yield nothing.
fn glob(dir: &Path, matches: impl Fn(&[u8]) -> bool) -> Vec<PathBuf> {
    let Ok(entries) = fs::read_dir(dir) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter(|e| matches(e.file_name().as_bytes()))
        .map(|e| dir.join(e.file_name()))
        .collect()
}

/// `Path.resolve(strict=True)`. The error names `path`; Python names the first missing component.
fn resolve(path: &Path) -> Result<PathBuf, Error> {
    fs::canonicalize(path).map_err(|e| Error::os(&e, Some(path)))
}

/// Python's int() of an ASCII digit string within `lo..=hi`; huge values are out of range.
fn ranged(digits: &str, negative: bool, lo: i64, hi: i64) -> bool {
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return false;
    }
    match digits.parse::<i64>() {
        Ok(n) => (lo..=hi).contains(&if negative { -n } else { n }),
        Err(_) => false,
    }
}

#[derive(Clone, Debug, PartialEq)]
struct Snapshot {
    device: PathBuf,
    klog: i64,
    limits: Vec<i64>,
    states: Vec<i64>,
}

pub struct Bcct {
    pub config: PathBuf,
    pub thermal_root: PathBuf,
    pub write: Writer,
}

impl Default for Bcct {
    fn default() -> Self {
        Bcct::new(CONFIG, THERMAL_ROOT)
    }
}

impl Bcct {
    pub fn new(config: impl Into<PathBuf>, thermal_root: impl Into<PathBuf>) -> Self {
        Bcct {
            config: config.into(),
            thermal_root: thermal_root.into(),
            write: Box::new(write_command),
        }
    }

    fn snapshot(&self, require_effective: bool) -> Result<Snapshot, Error> {
        let mut devices = Vec::new();
        for device in glob(&self.thermal_root, |n| n.starts_with(b"cooling_device")) {
            if py::strip(&read_ascii(&device.join("type"))?) == "mtk-cl-bcct00" {
                devices.push(device);
            }
        }
        if devices.len() != 1 {
            return Err(Error::value("BCCT slot 00 missing or ambiguous"));
        }
        let device = devices.remove(0);
        if py::strip(&read_ascii(&device.join("max_state"))?) != "1" {
            return Err(Error::value("unexpected BCCT max_state"));
        }
        for zone in glob(&self.thermal_root, |n| n.starts_with(b"thermal_zone")) {
            for binding in glob(&zone, |n| {
                n.len() > 4 && n.starts_with(b"cdev") && n[4].is_ascii_digit()
            }) {
                let link = fs::symlink_metadata(&binding).is_ok_and(|m| m.file_type().is_symlink());
                if link && resolve(&binding)? == resolve(&device)? {
                    return Err(Error::value("BCCT slot 00 belongs to a thermal zone"));
                }
            }
        }
        let text = read_ascii(&self.config)?;
        let lines = py::splitlines(&text);
        if lines.len() != 5 || !(lines[0] == "klog 0" || lines[0] == "klog 1") {
            return Err(Error::value("bad BCCT configuration"));
        }
        let effective = lines[1]
            .strip_prefix("curr_limit ")
            .filter(|d| ranged(d, false, 1, 65535));
        let Some(effective) = effective else {
            return Err(Error::value("bad BCCT effective limit"));
        };
        let effective: i64 = effective.parse().expect("checked digits");
        let (mut limits, mut states) = (Vec::new(), Vec::new());
        for (index, line) in lines[2..].iter().enumerate() {
            // mtk-cl-bcctNN (-?[0-9]+) mA, state ([01])
            let slot = line
                .strip_prefix(&format!("mtk-cl-bcct{index:02} "))
                .and_then(|rest| rest.split_once(" mA, state "))
                .filter(|(limit, state)| {
                    let (neg, digits) = limit
                        .strip_prefix('-')
                        .map_or((false, *limit), |d| (true, d));
                    ranged(digits, neg, -1, 32767) && (*state == "0" || *state == "1")
                });
            let Some((limit, state)) = slot else {
                return Err(Error::value("bad BCCT slot"));
            };
            let (neg, digits) = limit
                .strip_prefix('-')
                .map_or((false, limit), |d| (true, d));
            let n: i64 = digits.parse().expect("checked digits");
            limits.push(if neg { -n } else { n });
            states.push(if state == "1" { 1 } else { 0 });
        }
        if ![-1, 0, 1].contains(&limits[0]) || (states[0] != 0 && limits[0] != 1) {
            return Err(Error::value("BCCT slot 00 is occupied"));
        }
        if py::strip(&read_ascii(&device.join("cur_state"))?) != states[0].to_string() {
            return Err(Error::value("BCCT state readback disagrees"));
        }
        if require_effective && states[0] != 0 && effective != 1 {
            return Err(Error::value("BCCT pause is not effective"));
        }
        let klog = if lines[0].ends_with('1') { 1 } else { 0 };
        Ok(Snapshot {
            device,
            klog,
            limits,
            states,
        })
    }
}

impl Control for Bcct {
    fn paused(&mut self) -> Result<bool, Error> {
        Ok(self.snapshot(true)?.states[0] != 0)
    }

    fn set_paused(&mut self, paused: bool) -> Result<(), Error> {
        let mut want = self.snapshot(paused)?;
        if (want.states[0] != 0) == paused {
            return Ok(());
        }
        if paused && want.limits[0] != 1 {
            want.limits[0] = 1;
            let words: Vec<String> = std::iter::once(want.klog)
                .chain(want.limits.iter().copied())
                .map(|n| n.to_string())
                .collect();
            (self.write)(&self.config, &words.join(" "))?;
            if self.snapshot(true)? != want {
                return Err(Error::value("BCCT configuration write did not read back"));
            }
        }
        (self.write)(
            &want.device.join("cur_state"),
            if paused { "1" } else { "0" },
        )?;
        let actual = self.snapshot(true)?;
        if actual.device != want.device
            || actual.klog != want.klog
            || actual.limits != want.limits
            || actual.states[0] != paused as i64
            || actual.states[1..] != want.states[1..]
        {
            return Err(Error::value("BCCT state write did not read back"));
        }
        Ok(())
    }

    fn release(&mut self) -> Result<(), Error> {
        self.set_paused(false)
    }
}
