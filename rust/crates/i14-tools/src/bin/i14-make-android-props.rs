//! i14-make-android-props OUTPUT: write a minimal Android 6 (bionic) property area,
//! the standard-library-only Rust port of rootfs/make-android-props.py. The two CLIs
//! write byte-identical 128 KiB areas; tests/test_i14_linux.py (RustPropsWriter) checks
//! that parity by compiling and running both against temporary paths.

use std::cmp::Ordering;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::exit;

/// The four fixed properties, in insertion order (parity with PROPS in the Python CLI).
const PROPS: [(&str, &str); 4] = [
    ("service.wcn.driver.ready", "yes"),
    ("persist.mtk.wcn.combo.chipid", "0x0321"),
    ("service.wcn.coredump.mode", "0"),
    ("ro.build.type", "user"),
];
const SIZE: usize = 128 * 1024;
const HEADER: usize = 128;
const MAGIC: u32 = 0x504F_5250;
const VERSION: u32 = 0xFC6E_D0AB;
const VALUE_MAX: usize = 92;

/// The property area after the 128-byte header: a trie of prop_bt nodes plus prop_info
/// records, allocated from offset 0 and zero-padded to the full size on output.
struct Area {
    data: Vec<u8>,
    used: usize,
}

impl Area {
    fn new() -> Self {
        let mut area = Area {
            data: vec![0; SIZE - HEADER],
            used: 0,
        };
        area.node(b"").expect("root node fits"); // root
        area
    }

    fn alloc(&mut self, size: usize) -> Result<usize, String> {
        let off = self.used;
        let end = off + ((size + 3) & !3);
        if end > self.data.len() {
            return Err("property area full".to_string());
        }
        self.used = end;
        Ok(off)
    }

    fn node(&mut self, name: &[u8]) -> Result<usize, String> {
        let off = self.alloc(20 + name.len() + 1)?;
        self.data[off] = u8::try_from(name.len()).expect("name fits in u8");
        self.data[off + 20..off + 20 + name.len()].copy_from_slice(name);
        Ok(off)
    }

    /// Field 0=prop 1=left 2=right 3=children of the node at `off` (little-endian u32).
    fn field(&self, off: usize, index: usize) -> u32 {
        u32::from_le_bytes(
            self.data[off + 4 + 4 * index..off + 8 + 4 * index]
                .try_into()
                .unwrap(),
        )
    }

    fn set_field(&mut self, off: usize, index: usize, value: u32) {
        self.data[off + 4 + 4 * index..off + 8 + 4 * index].copy_from_slice(&value.to_le_bytes());
    }

    /// Find or add `name` among the children of `parent` (a binary tree: shorter names
    /// first, then byte order; smaller goes left), like bionic's find_prop_bt.
    fn child(&mut self, parent: usize, name: &[u8]) -> Result<usize, String> {
        let mut cur = self.field(parent, 3) as usize;
        if cur == 0 {
            let new = self.node(name)?;
            self.set_field(parent, 3, new as u32);
            return Ok(new);
        }
        loop {
            let cmp = {
                let other = &self.data[cur + 20..cur + 20 + self.data[cur] as usize];
                name.len().cmp(&other.len()).then_with(|| name.cmp(other))
            };
            if cmp == Ordering::Equal {
                return Ok(cur);
            }
            let side = if cmp == Ordering::Less { 1 } else { 2 };
            let next = self.field(cur, side) as usize;
            if next == 0 {
                let new = self.node(name)?;
                self.set_field(cur, side, new as u32);
                return Ok(new);
            }
            cur = next;
        }
    }

    fn add(&mut self, key: &str, value: &str) -> Result<(), String> {
        if value.len() >= VALUE_MAX {
            return Err(format!("value too long: {key}"));
        }
        let mut cur = 0;
        for part in key.as_bytes().split(|&b| b == b'.') {
            cur = self.child(cur, part)?;
        }
        let info = self.alloc(4 + VALUE_MAX + key.len() + 1)?;
        self.data[info..info + 4].copy_from_slice(&((value.len() as u32) << 24).to_le_bytes());
        self.data[info + 4..info + 4 + value.len()].copy_from_slice(value.as_bytes());
        self.data[info + 4 + VALUE_MAX..info + 4 + VALUE_MAX + key.len()]
            .copy_from_slice(key.as_bytes());
        self.set_field(cur, 0, info as u32);
        Ok(())
    }

    fn into_bytes(self) -> Vec<u8> {
        let mut out = Vec::with_capacity(SIZE);
        out.extend_from_slice(
            &u32::try_from(self.used)
                .expect("used fits in u32")
                .to_le_bytes(),
        );
        out.extend_from_slice(&0u32.to_le_bytes()); // header serial
        out.extend_from_slice(&MAGIC.to_le_bytes());
        out.extend_from_slice(&VERSION.to_le_bytes());
        out.resize(HEADER, 0);
        out.extend_from_slice(&self.data);
        out
    }
}

fn build() -> Result<Vec<u8>, String> {
    let mut area = Area::new();
    for (key, value) in PROPS {
        area.add(key, value)?;
    }
    Ok(area.into_bytes())
}

const USAGE: &str = "Usage: i14-make-android-props [--] OUTPUT";

fn help() -> ! {
    println!("{USAGE}\n\nWrite the minimal Android 6 property area used by the MediaTek Wi-Fi tools.\n\nOptions:\n  -h, --help  Print this help");
    exit(0);
}

fn usage_error(message: &str) -> ! {
    eprintln!("{USAGE}\n{message}");
    exit(2);
}

/// argparse in the reference Python 3.13 uses Unicode decimal digits in its
/// negative-number regex. Each entry is the zero in a Unicode 15.1 decimal block.
fn decimal_digit(c: char) -> bool {
    const ZEROS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60,
        0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0,
        0x1e950, 0x1fbf0,
    ];
    let code = c as u32;
    ZEROS.iter().any(|&zero| (zero..zero + 10).contains(&code))
}

fn is_negative_number(body: &[u8]) -> bool {
    let Ok(text) = std::str::from_utf8(body) else {
        return false;
    };
    // The reference regex's '$' also matches before a final newline.
    let text = text.strip_suffix('\n').unwrap_or(text);
    match text.split_once('.') {
        Some((whole, fraction)) => {
            !fraction.is_empty()
                && whole.chars().all(decimal_digit)
                && fraction.chars().all(decimal_digit)
        }
        None => !text.is_empty() && text.chars().all(decimal_digit),
    }
}

fn output_arg() -> PathBuf {
    let mut output = None;
    let mut options = true;
    let mut extra = false;
    for arg in std::env::args_os().skip(1) {
        if options {
            match arg.as_encoded_bytes() {
                b"--" => {
                    options = false;
                    continue;
                }
                [b'-', b'-', name @ ..] if b"help".starts_with(name) => help(),
                [b'-', b'-', tail @ ..]
                    if tail
                        .split(|&b| b == b'=')
                        .next()
                        .is_some_and(|prefix| b"help".starts_with(prefix)) =>
                {
                    usage_error("Help takes no argument.");
                }
                [b'-', b'=', ..] => usage_error("Ambiguous option."),
                [b'-', b'h', rest @ ..] => {
                    // argparse processes a cluster of short options before showing help.
                    match rest.iter().find(|&&b| b != b'h') {
                        Some(b'=' | b'-') => usage_error("Help takes no argument."),
                        _ => help(),
                    }
                }
                [b'-', body @ ..]
                    if !body.is_empty() && !body.contains(&b' ') && !is_negative_number(body) =>
                {
                    extra = true;
                    continue;
                }
                _ => {}
            }
        }
        if output.is_some() {
            extra = true;
        } else {
            output = Some(arg);
        }
    }
    if extra {
        usage_error("Expected one output path. Use -- before paths beginning with '-'.");
    }
    let output = output.unwrap_or_else(|| usage_error("Missing output path."));
    // pathlib drops trailing slashes and redundant "." components before writing.
    Path::new(&output).components().collect()
}

fn main() {
    let output = output_arg();
    let blob = build().unwrap_or_else(|error| {
        eprintln!("i14-make-android-props: {error}");
        exit(1);
    });
    let result = output
        .parent()
        .map_or(Ok(()), fs::create_dir_all)
        .and_then(|()| fs::write(&output, blob));
    if let Err(error) = result {
        eprintln!("i14-make-android-props: {}: {error}", output.display());
        exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn children_share_nodes_and_sort_by_length_then_bytes() {
        let mut area = Area::new();
        let root_child = area.child(0, b"bb").unwrap();
        let short = area.child(0, b"z").unwrap();
        let equal_length = area.child(0, b"cc").unwrap();
        let used = area.used;
        assert_eq!(area.child(0, b"bb").unwrap(), root_child);
        assert_eq!(area.used, used);
        assert_eq!(area.field(root_child, 1) as usize, short);
        assert_eq!(area.field(root_child, 2) as usize, equal_length);
    }

    #[test]
    fn allocations_are_aligned_and_bounded() {
        let mut area = Area::new();
        let offset = area.alloc(5).unwrap();
        assert_eq!(offset % 4, 0);
        assert_eq!(area.used, offset + 8);
        let remaining = area.data.len() - area.used;
        area.alloc(remaining).unwrap();
        assert!(area.alloc(1).is_err());
        assert_eq!(area.used, area.data.len());
    }
}
