//! Local image validation, done before any USB connection or command.

use sha2::{Digest, Sha256};

use crate::{Error, Result};

/// Size of the I14 boot partition; also the largest accepted image.
pub const MAX_IMAGE: usize = 0x100_0000;

const PAGE_SIZES: [u64; 6] = [2048, 4096, 8192, 16384, 32768, 65536];

/// Lowercase hex SHA-256 (Python `hashlib.sha256(data).hexdigest()`).
pub fn sha256_hex(data: &[u8]) -> String {
    hex(&Sha256::digest(data))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(char::from(DIGITS[usize::from(byte >> 4)]));
        out.push(char::from(DIGITS[usize::from(byte & 15)]));
    }
    out
}

/// `re.fullmatch(r"[0-9a-fA-F]{N}", s)`.
pub(crate) fn is_hex_of_len(s: &[u8], len: usize) -> bool {
    s.len() == len && s.iter().all(u8::is_ascii_hexdigit)
}

fn le_u32(data: &[u8], offset: usize) -> u64 {
    let bytes = [
        data[offset],
        data[offset + 1],
        data[offset + 2],
        data[offset + 3],
    ];
    u64::from(u32::from_le_bytes(bytes))
}

/// Validate the complete bytes before connecting or writing any command.
///
/// Same checks and order as `fastboot_usb.validate_image`: digest syntax,
/// size bounds, digest match, legacy `ANDROID!` magic, header v0 with a
/// valid page size, non-empty kernel, and segments (page padded) inside the
/// image. `sha256` of `None` behaves like Python's `sha256 or ""`.
pub fn validate_image<'a>(data: &'a [u8], sha256: Option<&str>) -> Result<&'a [u8]> {
    let sha256 = sha256.unwrap_or("");
    if !is_hex_of_len(sha256.as_bytes(), 64) {
        return Err(Error::fastboot("expected a 64-digit SHA-256"));
    }
    if !(1632..=MAX_IMAGE).contains(&data.len()) {
        return Err(Error::fastboot("image must fit the 16 MiB boot partition"));
    }
    if sha256_hex(data) != sha256.to_ascii_lowercase() {
        return Err(Error::fastboot("image SHA-256 mismatch"));
    }
    if &data[..8] != b"ANDROID!" {
        return Err(Error::fastboot("missing legacy Android boot magic"));
    }
    // "<10I" at offset 8: kernel, _, ramdisk, _, second, _, _, page, version, _
    let kernel = le_u32(data, 8);
    let ramdisk = le_u32(data, 16);
    let second = le_u32(data, 24);
    let page = le_u32(data, 36);
    let version = le_u32(data, 40);
    if version != 0 || !PAGE_SIZES.contains(&page) {
        return Err(Error::fastboot(
            "expected Android header v0 with a valid page size",
        ));
    }
    if kernel == 0 {
        return Err(Error::fastboot("empty kernel"));
    }
    let required = page
        + [kernel, ramdisk, second]
            .iter()
            .map(|size| size.div_ceil(page) * page)
            .sum::<u64>();
    if required > data.len() as u64 {
        return Err(Error::fastboot(
            "boot segments or their page padding exceed the image",
        ));
    }
    Ok(data)
}

/// Whitespace `int()` strips from ASCII input (`Py_ISSPACE`; unlike
/// `str.isspace()` this excludes the 0x1c..=0x1f separators).
fn py_isspace(byte: u8) -> bool {
    matches!(byte, b'\t' | b'\n' | 0x0b | 0x0c | b'\r' | b' ')
}

/// Python `int(text, 16)` for an ASCII string, saturating at the i128 range.
///
/// Accepts surrounding whitespace, a sign, an optional `0x`/`0X` prefix and
/// single underscores between digits (also directly after the prefix).
/// Returns `None` where Python raises `ValueError`.
pub fn py_int16(text: &str) -> Option<i128> {
    let bytes = text.as_bytes();
    let start = bytes.iter().position(|b| !py_isspace(*b))?;
    let end = bytes.iter().rposition(|b| !py_isspace(*b))? + 1;
    let mut s = &bytes[start..end];
    let negative = match s.first() {
        Some(b'-') => {
            s = &s[1..];
            true
        }
        Some(b'+') => {
            s = &s[1..];
            false
        }
        _ => false,
    };
    let mut prefixed = false;
    if s.len() >= 2 && s[0] == b'0' && (s[1] == b'x' || s[1] == b'X') {
        s = &s[2..];
        prefixed = true;
    }
    if s.is_empty() {
        return None;
    }
    let mut value: i128 = 0;
    let mut previous_underscore = false;
    for (index, byte) in s.iter().enumerate() {
        if *byte == b'_' {
            if previous_underscore || (index == 0 && !prefixed) {
                return None;
            }
            previous_underscore = true;
            continue;
        }
        let digit = char::from(*byte).to_digit(16)?;
        previous_underscore = false;
        value = value.saturating_mul(16).saturating_add(i128::from(digit));
    }
    if previous_underscore {
        return None;
    }
    Some(if negative { -value } else { value })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn int16_matches_python_rules() {
        let cases: &[(&str, Option<i128>)] = &[
            ("0x1000000", Some(0x100_0000)),
            ("1000000", Some(0x100_0000)),
            (" 0X10\n", Some(16)),
            ("+0x10", Some(16)),
            ("-0x10", Some(-16)),
            ("0x_10", Some(16)),
            ("1_0", Some(16)),
            ("_10", None),
            ("10_", None),
            ("1__0", None),
            ("0x", None),
            ("", None),
            ("unknown", None),
            ("- 1", None),
            ("\x1c10\x1f", None),
            ("\x0b10\x0c", Some(16)),
        ];
        for (text, expected) in cases {
            assert_eq!(py_int16(text), *expected, "{text:?}");
        }
    }
}
