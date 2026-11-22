//! Fixed boot-only install (port of `install_boot_usb.py`).
//!
//! No automatic reboot, restore, erase, retry or other partition writes. There
//! is no readback: every receipt line says `"readback": "UNKNOWN"`.

use std::fs::{File, OpenOptions};
use std::io::Write as _;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};
use std::time::SystemTime;

use crate::image::{MAX_IMAGE, py_int16, sha256_hex};
use crate::protocol::{Fastboot, PRODUCT, Transport};
use crate::{Error, Result, json};

/// SHA-256 of the complete 16 MiB stock boot partition backup.
pub const STOCK_SHA256: &str = "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492";

/// Command-line words of RAM-boot-only test images (build-i14-boot.py --charge-test and
/// --charge-limit-test): flashed, they would enter the charging path on every boot.
pub const TEST_CMDLINE_PREFIXES: [&[u8]; 3] =
    [b"androidboot.mode=", b"i14.autoboot=", b"i14.test="];

/// Python `install_boot_usb.validate_install_cmdline`: refuse a RAM-boot-only test image,
/// where a word of the header cmdline or extra_cmdline (v0 offsets 64..576 and 608..1632, up
/// to the first NUL; words split as Python's `bytes.split()`) starts with a test prefix.
pub fn validate_install_cmdline(data: &[u8]) -> Result<()> {
    for (start, end) in [(64, 576), (608, 1632)] {
        let field = &data[start.min(data.len())..end.min(data.len())];
        let text = field.split(|&b| b == 0).next().unwrap_or(&[]);
        let words = text
            .split(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c))
            .filter(|w| !w.is_empty());
        for word in words {
            if let Some(prefix) = TEST_CMDLINE_PREFIXES.iter().find(|p| word.starts_with(p)) {
                let printable = word.len() <= 64 && word.iter().all(|c| (33..=126).contains(c));
                let shown = if printable { word } else { prefix };
                return Err(Error::Fastboot(format!(
                    "RAM-boot-only test image ({}); never install it",
                    String::from_utf8_lossy(shown)
                )));
            }
        }
    }
    Ok(())
}

pub fn validate_stock(data: &[u8]) -> Result<()> {
    if data.len() != MAX_IMAGE || sha256_hex(data) != STOCK_SHA256 {
        return Err(Error::fastboot(
            "stock backup must match the pinned complete 16 MiB boot partition",
        ));
    }
    Ok(())
}

/// A receipt field value (the JSON types the receipt uses).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Value {
    Str(String),
    /// A file system path, encoded like Python's `str(Path)`.
    Path(PathBuf),
    Int(i64),
    IntList(Vec<i64>),
}

impl Value {
    fn to_json(&self) -> String {
        match self {
            Value::Str(text) => json::string(text),
            Value::Path(path) => json::os_string(path.as_os_str().as_bytes()),
            Value::Int(number) => number.to_string(),
            Value::IntList(items) => {
                let inner: Vec<String> = items.iter().map(i64::to_string).collect();
                format!("[{}]", inner.join(", "))
            }
        }
    }
}

fn s(text: &str) -> Value {
    Value::Str(text.to_owned())
}

/// Python `Path(path).resolve()` (non-strict `os.path.realpath`).
pub fn py_resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .unwrap_or_else(|_| PathBuf::from("/"))
            .join(path)
    };
    let mut resolved = PathBuf::from("/");
    resolve_into(&mut resolved, &absolute, 0);
    resolved
}

fn resolve_into(resolved: &mut PathBuf, rest: &Path, depth: usize) {
    for component in rest.components() {
        match component {
            Component::RootDir => *resolved = PathBuf::from("/"),
            Component::CurDir | Component::Prefix(_) => {}
            Component::ParentDir => {
                resolved.pop();
            }
            Component::Normal(name) => {
                let candidate = resolved.join(name);
                match std::fs::read_link(&candidate) {
                    Ok(target) if depth < 40 => resolve_into(resolved, &target, depth + 1),
                    _ => *resolved = candidate,
                }
            }
        }
    }
}

/// Exclusive, append-only JSONL evidence (`attempt_receipt`).
///
/// Creation persists the `attempt_created` line and the directory entry.
/// Every line is flushed and fsynced before the call returns.
pub struct Receipt {
    output: File,
    started: bool,
}

impl Receipt {
    pub fn create(path: &Path, source: &Path, data: &[u8]) -> Result<Receipt> {
        let output = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)?;
        let mut receipt = Receipt {
            output,
            started: false,
        };
        receipt.record(
            "attempt_created",
            &[
                ("partition", s("boot")),
                ("product", s(PRODUCT)),
                ("expected_port", Value::IntList(vec![1, 1])),
                ("candidate_source", Value::Path(py_resolve(source))),
                ("candidate_bytes", Value::Int(data.len() as i64)),
                ("candidate_sha256", Value::Str(sha256_hex(data))),
                ("stock_bytes", Value::Int(MAX_IMAGE as i64)),
                ("stock_sha256", s(STOCK_SHA256)),
                ("padding", s("none; exact candidate bytes")),
                ("nv_write", s("NOT_STARTED")),
            ],
        )?;
        let parent = py_resolve(path)
            .parent()
            .map_or_else(|| PathBuf::from("/"), Path::to_path_buf);
        File::open(parent)?.sync_all()?;
        Ok(receipt)
    }

    /// Append one event line. A second `protocol_started` is refused.
    pub fn record(&mut self, event: &str, fields: &[(&str, Value)]) -> Result<()> {
        if event == "protocol_started" {
            if self.started {
                return Err(Error::fastboot(
                    "attempt already consumed; reuse prohibited",
                ));
            }
            self.started = true;
        }
        let mut entry: Vec<(&str, Value)> = vec![
            ("event", s(event)),
            (
                "time_utc",
                Value::Str(json::isoformat_utc(SystemTime::now())),
            ),
            ("readback", s("UNKNOWN")),
        ];
        for (key, value) in fields {
            if entry.iter().any(|(existing, _)| existing == key) {
                return Err(Error::Other(format!("duplicate receipt field {key}")));
            }
            entry.push((key, value.clone()));
        }
        entry.sort_by(|a, b| a.0.cmp(b.0));
        let body: Vec<String> = entry
            .iter()
            .map(|(key, value)| format!("{}: {}", json::string(key), value.to_json()))
            .collect();
        let line = format!("{{{}}}\n", body.join(", "));
        self.output.write_all(line.as_bytes())?;
        self.output.flush()?;
        self.output.sync_all()?;
        Ok(())
    }

    fn stopped(&mut self, nv_write: &str) -> Result<()> {
        self.record("stopped", &[("nv_write", s(nv_write))])
    }

    /// Record `stopped` for a failure before any client existed.
    pub fn record_stopped_not_started(&mut self) -> Result<()> {
        self.stopped("NOT_STARTED")
    }
}

/// Only called after local validation and exclusive receipt creation.
pub fn install_boot<T: Transport>(
    client: &mut Fastboot<T>,
    data: &[u8],
    receipt: &mut Receipt,
) -> Result<()> {
    validate_install_cmdline(data)?; // also here, so no caller can install a RAM-boot-only test image
    receipt.record("protocol_started", &[("nv_write", s("NOT_STARTED"))])?;
    let mut nv_write = "NOT_STARTED";
    let outcome = install_steps(client, data, receipt, &mut nv_write);
    if let Err(error) = outcome {
        // Python: `except BaseException: record("stopped", ...); raise` - a
        // failure to record replaces the original exception.
        receipt.stopped(nv_write)?;
        return Err(error);
    }
    Ok(())
}

fn install_steps<T: Transport>(
    client: &mut Fastboot<T>,
    data: &[u8],
    receipt: &mut Receipt,
    nv_write: &mut &'static str,
) -> Result<()> {
    if client.device.port_numbers().unwrap_or_default() != [1, 1] {
        return Err(Error::fastboot("physical USB port mismatch"));
    }
    client.guard()?;
    if client.getvar("unlocked")? != "yes" {
        return Err(Error::fastboot("boot install requires unlocked=yes"));
    }
    if client.getvar("secure")? != "no" {
        return Err(Error::fastboot("boot install requires secure=no"));
    }
    let invalid = || Error::fastboot("invalid boot partition or maximum download size");
    let partition = py_int16(&client.getvar("partition-size:boot")?).ok_or_else(invalid)?;
    let maximum = py_int16(&client.getvar("max-download-size")?).ok_or_else(invalid)?;
    let length = data.len() as i128;
    if partition != MAX_IMAGE as i128 || !(0 < length && length <= partition.min(maximum)) {
        return Err(Error::fastboot(
            "boot partition or maximum download size mismatch",
        ));
    }
    client.download(data)?;
    // This durable record must precede the first possible nonvolatile write.
    receipt.record(
        "flash_intent",
        &[("command", s("flash:boot")), ("nv_write", s("UNKNOWN"))],
    )?;
    *nv_write = "UNKNOWN";
    client.command("flash:boot", b"OKAY")?;
    receipt.record(
        "protocol_acknowledged",
        &[("nv_write", s("ACKNOWLEDGED_UNVERIFIED"))],
    )?;
    Ok(())
}
