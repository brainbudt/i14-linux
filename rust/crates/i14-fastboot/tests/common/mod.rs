#![allow(dead_code)]
//! Helpers shared by the ported Python tests (fake USB only).

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

use i14_fastboot::fake::Reply;

/// `image(**fields)` from tests/test_fastboot_usb.py.
#[derive(Clone, Copy)]
pub struct Img {
    pub kernel: u32,
    pub ramdisk: u32,
    pub second: u32,
    pub page: u32,
    pub version: u32,
    pub length: usize,
}

impl Default for Img {
    fn default() -> Self {
        Img {
            kernel: 32,
            ramdisk: 16,
            second: 0,
            page: 2048,
            version: 0,
            length: 6144,
        }
    }
}

impl Img {
    pub fn build(self) -> Vec<u8> {
        let mut data = vec![0u8; self.length];
        let mut header = b"ANDROID!".to_vec();
        for value in [
            self.kernel,
            0,
            self.ramdisk,
            0,
            self.second,
            0,
            0,
            self.page,
            self.version,
            0,
        ] {
            header.extend_from_slice(&value.to_le_bytes());
        }
        data[..header.len()].copy_from_slice(&header);
        data
    }
}

pub fn image() -> Vec<u8> {
    Img::default().build()
}

pub fn sha(data: &[u8]) -> String {
    i14_fastboot::image::sha256_hex(data)
}

pub fn r(bytes: &[u8]) -> Reply {
    Reply::Bytes(bytes.to_vec())
}

pub fn okay(value: &[u8]) -> Reply {
    let mut packet = b"OKAY".to_vec();
    packet.extend_from_slice(value);
    Reply::Bytes(packet)
}

/// A fresh empty directory (tempfile.TemporaryDirectory analogue).
pub fn tempdir(tag: &str) -> PathBuf {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    let n = COUNTER.fetch_add(1, Ordering::SeqCst);
    let dir = PathBuf::from(env!("CARGO_TARGET_TMPDIR"))
        .join(format!("{tag}-{}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

pub fn args(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

pub fn lines(path: &std::path::Path) -> Vec<String> {
    std::fs::read_to_string(path)
        .unwrap()
        .lines()
        .map(str::to_owned)
        .collect()
}

/// Minimal field lookup in one receipt line produced by our own encoder.
pub fn field(line: &str, key: &str) -> String {
    let needle = format!("\"{key}\": ");
    let start = line
        .find(&needle)
        .unwrap_or_else(|| panic!("{key} missing in {line}"))
        + needle.len();
    let rest = &line[start..];
    if let Some(stripped) = rest.strip_prefix('"') {
        stripped[..stripped.find('"').unwrap()].to_owned()
    } else {
        rest[..rest.find([',', '}']).unwrap()].to_owned()
    }
}
