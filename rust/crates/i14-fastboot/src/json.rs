//! Byte-exact helpers for Python's `json.dumps(..., sort_keys=True)` and
//! `datetime.now(timezone.utc).isoformat()`.

use std::fmt::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

/// Python `json.dumps(str)` with the default `ensure_ascii=True`.
pub fn string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for ch in text.chars() {
        push_char(&mut out, u32::from(ch));
    }
    out.push('"');
    out
}

/// JSON string for bytes decoded the way Python decodes file system paths
/// (UTF-8 with `surrogateescape`: each undecodable byte becomes U+DC80+byte).
pub fn os_string(bytes: &[u8]) -> String {
    let mut out = String::from("\"");
    for chunk in bytes.utf8_chunks() {
        for ch in chunk.valid().chars() {
            push_char(&mut out, u32::from(ch));
        }
        for byte in chunk.invalid() {
            push_char(&mut out, 0xdc00 + u32::from(*byte));
        }
    }
    out.push('"');
    out
}

fn push_char(out: &mut String, code: u32) {
    match code {
        0x22 => out.push_str("\\\""),
        0x5c => out.push_str("\\\\"),
        0x0a => out.push_str("\\n"),
        0x0d => out.push_str("\\r"),
        0x09 => out.push_str("\\t"),
        0x08 => out.push_str("\\b"),
        0x0c => out.push_str("\\f"),
        0x20..=0x7e => out.push(char::from(code as u8)),
        0x1_0000.. => {
            let value = code - 0x1_0000;
            let _ = write!(
                out,
                "\\u{:04x}\\u{:04x}",
                0xd800 + (value >> 10),
                0xdc00 + (value & 0x3ff)
            );
        }
        _ => {
            let _ = write!(out, "\\u{code:04x}");
        }
    }
}

/// Days since 1970-01-01 to (year, month, day), proleptic Gregorian.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = yoe + era * 400 + i64::from(month <= 2);
    (year, month, day)
}

/// Python `datetime.fromtimestamp(t, timezone.utc).isoformat()`; the
/// fraction is omitted when microseconds are zero, as Python does.
pub fn isoformat_utc(time: SystemTime) -> String {
    let since = time.duration_since(UNIX_EPOCH).unwrap_or_default();
    let seconds = i64::try_from(since.as_secs()).unwrap_or(i64::MAX);
    let micros = since.subsec_micros();
    let (year, month, day) = civil_from_days(seconds.div_euclid(86_400));
    let rem = seconds.rem_euclid(86_400);
    let mut out = format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    );
    if micros != 0 {
        let _ = write!(out, ".{micros:06}");
    }
    out.push_str("+00:00");
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    #[test]
    fn isoformat_matches_python() {
        // datetime.fromtimestamp(1793828362.273581, timezone.utc).isoformat()
        let t = UNIX_EPOCH + Duration::from_micros(1_793_828_362_273_581);
        assert_eq!(isoformat_utc(t), "2026-11-04T21:39:22.273581+00:00");
        assert_eq!(isoformat_utc(UNIX_EPOCH), "1970-01-01T00:00:00+00:00");
        let leap = UNIX_EPOCH + Duration::from_secs(951_782_400); // 2000-02-29
        assert_eq!(isoformat_utc(leap), "2000-02-29T00:00:00+00:00");
    }

    #[test]
    fn strings_match_ensure_ascii() {
        assert_eq!(
            string("a\"b\\c\n\u{7f}é😀"),
            "\"a\\\"b\\\\c\\n\\u007f\\u00e9\\ud83d\\ude00\""
        );
        assert_eq!(os_string(b"a\xffb"), "\"a\\udcffb\"");
    }
}
