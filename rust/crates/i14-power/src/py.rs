//! The few Python behaviours the guard's outputs depend on: `str(OSError)`,
//! `str.strip`/`splitlines`, `repr(float)` and `json.dump` (ensure_ascii,
//! allow_nan=False). Error texts reach the status JSON and the UI.

use std::fmt;
use std::io;
use std::path::Path;

/// A caught Python OSError or ValueError, as `str(exc)`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error(pub String);

impl Error {
    pub fn value(text: &str) -> Self {
        Error(text.to_string())
    }

    /// `str(OSError)`: "[Errno N] strerror" plus ": 'filename'" when there is one.
    pub fn os(e: &io::Error, file: Option<&Path>) -> Self {
        Error::os2(e, file, None)
    }

    /// As `os`, with os.replace's "'src' -> 'dst'".
    pub fn os2(e: &io::Error, file: Option<&Path>, file2: Option<&Path>) -> Self {
        let head = match e.raw_os_error() {
            Some(n) => {
                let text = io::Error::from_raw_os_error(n).to_string();
                let suffix = format!(" (os error {n})");
                format!(
                    "[Errno {n}] {}",
                    text.strip_suffix(&suffix).unwrap_or(&text)
                )
            }
            None => e.to_string(),
        };
        Error(match (file, file2) {
            (Some(a), Some(b)) => format!("{head}: {} -> {}", repr_path(a), repr_path(b)),
            (Some(a), None) => format!("{head}: {}", repr_path(a)),
            _ => head,
        })
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// Python's `str.isspace()` for one character.
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

/// Python's `str.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// Python's `str.splitlines()` (ASCII text, as bcct.py reads it).
pub fn splitlines(s: &str) -> Vec<&str> {
    let b = s.as_bytes();
    let (mut out, mut start, mut i) = (Vec::new(), 0, 0);
    while i < b.len() {
        let sep = match b[i] {
            b'\r' if b.get(i + 1) == Some(&b'\n') => 2,
            b'\n' | b'\r' | 0x0b | 0x0c | 0x1c | 0x1d | 0x1e => 1,
            _ => 0,
        };
        if sep > 0 {
            out.push(&s[start..i]);
            i += sep;
            start = i;
        } else {
            i += s[i..].chars().next().map_or(1, char::len_utf8);
        }
    }
    if start < b.len() {
        out.push(&s[start..]);
    }
    out
}

/// Python's `repr(str)`.
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || (0x7f..0xa0).contains(&(c as u32)) => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

pub fn repr_path(p: &Path) -> String {
    repr_str(&p.to_string_lossy())
}

/// Python's `repr(float)` (shortest round-trip digits, exponent outside 1e-4..1e16).
pub fn float_repr(x: f64) -> String {
    if !x.is_finite() {
        // json.dump(allow_nan=False) would raise; the guard never produces these.
        return if x.is_nan() {
            "NaN".into()
        } else if x > 0.0 {
            "Infinity".into()
        } else {
            "-Infinity".into()
        };
    }
    let sign = if x.is_sign_negative() { "-" } else { "" };
    let e_form = format!("{:e}", x.abs()); // e.g. "1.2345e3", "1e-5"
    let (mant, exp) = e_form.split_once('e').expect("LowerExp has an exponent");
    let exp: i32 = exp.parse().expect("LowerExp exponent");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let n = digits.len() as i32;
    let decpt = exp + 1;
    let body = if !(-4..16).contains(&exp) {
        let rest = &digits[1..];
        let m = if rest.is_empty() {
            digits[..1].to_string()
        } else {
            format!("{}.{rest}", &digits[..1])
        };
        format!("{m}e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs())
    } else if decpt <= 0 {
        format!("0.{}{digits}", "0".repeat((-decpt) as usize))
    } else if decpt >= n {
        format!("{digits}{}.0", "0".repeat((decpt - n) as usize))
    } else {
        format!(
            "{}.{}",
            &digits[..decpt as usize],
            &digits[decpt as usize..]
        )
    };
    format!("{sign}{body}")
}

/// One value of the flat status dict.
#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    /// A number read back from a status file: stays int or float as it was.
    Num(serde_json::Number),
    Str(String),
}

impl From<bool> for Json {
    fn from(v: bool) -> Self {
        Json::Bool(v)
    }
}
impl From<i64> for Json {
    fn from(v: i64) -> Self {
        Json::Int(v)
    }
}
impl From<f64> for Json {
    fn from(v: f64) -> Self {
        Json::Float(v)
    }
}
impl<T: Into<Json>> From<Option<T>> for Json {
    fn from(v: Option<T>) -> Self {
        v.map_or(Json::Null, Into::into)
    }
}
impl From<String> for Json {
    fn from(v: String) -> Self {
        Json::Str(v)
    }
}
impl From<&str> for Json {
    fn from(v: &str) -> Self {
        Json::Str(v.to_string())
    }
}
impl From<serde_json::Number> for Json {
    fn from(v: serde_json::Number) -> Self {
        Json::Num(v)
    }
}

fn json_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            c => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
}

/// `json.dump(dict, f, allow_nan=False)` for a flat dict, in insertion order.
pub fn dump(pairs: &[(&str, Json)]) -> String {
    let mut out = String::from("{");
    for (i, (key, value)) in pairs.iter().enumerate() {
        if i > 0 {
            out.push_str(", ");
        }
        json_str(key, &mut out);
        out.push_str(": ");
        match value {
            Json::Null => out.push_str("null"),
            Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
            Json::Int(n) => out.push_str(&n.to_string()),
            Json::Float(f) => out.push_str(&float_repr(*f)),
            Json::Num(n) if n.is_f64() => out.push_str(&float_repr(n.as_f64().unwrap_or(f64::NAN))),
            Json::Num(n) => out.push_str(&n.to_string()),
            Json::Str(s) => json_str(s, &mut out),
        }
    }
    out.push('}');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python() {
        for (x, s) in [
            (1000.0, "1000.0"),
            (0.1 + 0.2, "0.30000000000000004"),
            (1e16, "1e+16"),
            (1.5e-5, "1.5e-05"),
            (0.0001, "0.0001"),
            (123456.789, "123456.789"),
            (9999999999999998.0, "9999999999999998.0"),
            (-0.0, "-0.0"),
            (0.0, "0.0"),
            (-2.5, "-2.5"),
            (1.2345e100, "1.2345e+100"),
        ] {
            assert_eq!(float_repr(x), s);
        }
    }

    #[test]
    fn strings_match_python() {
        assert_eq!(splitlines("a\r\nb\n\nc\x0bd\n"), ["a", "b", "", "c", "d"]);
        assert_eq!(splitlines(""), Vec::<&str>::new());
        assert_eq!(strip("\x1c 80\n"), "80");
        assert_eq!(repr_str("it's"), "\"it's\"");
        assert_eq!(repr_str("/a/b"), "'/a/b'");
        let mut s = String::new();
        json_str("é\u{1f600}\x7f\"", &mut s);
        assert_eq!(s, "\"\\u00e9\\ud83d\\ude00\\u007f\\\"\"");
    }
}
