//! Wi-Fi settings through wpa_supplicant's control socket (/run/wpa_supplicant/wlan0).
//!
//! The only Wi-Fi path of the UI: it never touches the driver or wmt_loader (a second wmt_loader
//! crashes the kernel). Networks the user adds are saved to the userdata file that /etc/init.d/i14-wifi
//! merges at start (update_config=0 stays); passwords are stored as the derived 256-bit PSK, not as text.
//! Every change is written to userdata first and then to wpa_supplicant's start file (i14-wifi-conf), so a
//! respawned wpa_supplicant starts with the same networks. Wi-Fi "off" = a flag file on userdata: the start
//! file then marks every network disabled, and i14-netwatch leaves Wi-Fi alone.
//!
//! Port of `i14ui/wifi.py`. Python's `OSError` is `io::Error` here; its Display follows Python's `str(e)`.
use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::os::unix::io::{AsRawFd, RawFd};
use std::os::unix::net::UnixDatagram;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub const CTRL: &str = "/run/wpa_supplicant/wlan0";
pub const IFACE: &str = "wlan0";
pub const DATA: &str = "/mnt/data/media/0/i14-linux";
pub const NETWORKS: &str = "/mnt/data/media/0/i14-linux/wpa_supplicant.conf";
pub const ROOTFS_NETWORKS: &str = "/etc/wpa_supplicant/wpa_supplicant.conf";
pub const OFF_FLAG: &str = "/mnt/data/media/0/i14-linux/wifi-off";
pub const UDHCPC_PID: &str = "/run/i14-udhcpc-wlan0.pid";
pub const CONF_TOOL: &str = "/usr/sbin/i14-wifi-conf";
pub const ASSOC_TIMEOUT: f64 = 30.0;
pub const DHCP_TIMEOUT: f64 = 25.0;
const REQUEST_TIMEOUT: f64 = 1.0; // short: the screen waits for it

/// `time.monotonic()`: seconds since the first call in this process.
pub fn monotonic() -> f64 {
    static T0: OnceLock<Instant> = OnceLock::new();
    T0.get_or_init(Instant::now).elapsed().as_secs_f64()
}

// --- Python compatibility helpers ---------------------------------------------------------------
/// Python's `str(OSError)`: "[Errno N] text" for OS errors, the message otherwise.
fn py_str(e: &io::Error) -> String {
    match e.raw_os_error() {
        Some(code) => format!("[Errno {code}] {}", strerror(e)),
        None => e.to_string(),
    }
}

/// Python's `e.strerror or e`.
fn strerror(e: &io::Error) -> String {
    let s = e.to_string();
    match e.raw_os_error() {
        Some(code) => s
            .strip_suffix(&format!(" (os error {code})"))
            .unwrap_or(&s)
            .to_string(),
        None => s,
    }
}

fn ctrl_error(kind: io::ErrorKind, msg: String) -> io::Error {
    io::Error::new(kind, msg)
}

fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// Python's `str.splitlines()`.
fn splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let (mut start, mut it) = (0, s.char_indices().peekable());
    while let Some((i, c)) = it.next() {
        if matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            out.push(&s[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && it.peek().map(|&(_, n)| n) == Some('\n') {
                it.next();
                start += 1;
            }
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// Python's `int(s)` for base 10 (ASCII digits only).
fn py_int(s: &str) -> Option<i64> {
    let t = py_strip(s);
    let (neg, d) = match t.as_bytes().first() {
        Some(b'-') => (true, &t[1..]),
        Some(b'+') => (false, &t[1..]),
        _ => (false, t),
    };
    if d.is_empty()
        || d.starts_with('_')
        || d.ends_with('_')
        || d.contains("__")
        || !d.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return None;
    }
    let v: i64 = d.replace('_', "").parse().ok()?;
    Some(if neg { -v } else { v })
}

/// Python's `str.isdigit()` (ASCII digits only).
fn is_digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn py_byte_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | b'\x0b' | b'\x0c')
}

/// Python's `bytes.fromhex(s)`.
fn fromhex(s: &str) -> Option<Vec<u8>> {
    let (b, mut i, mut out) = (s.as_bytes(), 0, Vec::new());
    loop {
        while i < b.len() && py_byte_space(b[i]) {
            i += 1;
        }
        if i >= b.len() {
            return Some(out);
        }
        let hi = (b[i] as char).to_digit(16)?;
        let lo = (*b.get(i + 1)? as char).to_digit(16)?;
        out.push((hi * 16 + lo) as u8);
        i += 2;
    }
}

/// Python's `int(two_bytes, 16)` appended to a bytearray (must give 0..=255).
fn py_hex2(b: &[u8]) -> Option<u8> {
    let (mut s, mut e) = (0, b.len());
    while s < e && py_byte_space(b[s]) {
        s += 1;
    }
    while e > s && py_byte_space(b[e - 1]) {
        e -= 1;
    }
    let (neg, d) = match b[s..e].first() {
        Some(b'-') => (true, &b[s + 1..e]),
        Some(b'+') => (false, &b[s + 1..e]),
        _ => (false, &b[s..e]),
    };
    if d.is_empty() {
        return None;
    }
    let mut v = 0u32;
    for &c in d {
        v = v * 16 + (c as char).to_digit(16)?;
    }
    if neg && v != 0 {
        return None; // bytearray.append(-n): ValueError
    }
    Some(v as u8)
}

/// Python's `str.isprintable()` for one character: not Cc, Cf, Co, Zs (except space), Zl, Zp.
/// Unassigned code points (Cn) are not detected (that needs the full Unicode table).
fn py_printable(c: char) -> bool {
    const CF: &[(u32, u32)] = &[
        (0xAD, 0xAD),
        (0x600, 0x605),
        (0x61C, 0x61C),
        (0x6DD, 0x6DD),
        (0x70F, 0x70F),
        (0x890, 0x891),
        (0x8E2, 0x8E2),
        (0x180E, 0x180E),
        (0x200B, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x2064),
        (0x2066, 0x206F),
        (0xFEFF, 0xFEFF),
        (0xFFF9, 0xFFFB),
        (0x110BD, 0x110BD),
        (0x110CD, 0x110CD),
        (0x13430, 0x1343F),
        (0x1BCA0, 0x1BCA3),
        (0x1D173, 0x1D17A),
        (0xE0001, 0xE0001),
        (0xE0020, 0xE007F),
    ];
    const CO: &[(u32, u32)] = &[(0xE000, 0xF8FF), (0xF0000, 0xFFFFD), (0x100000, 0x10FFFD)];
    let u = c as u32;
    let in_table = |t: &[(u32, u32)]| t.iter().any(|&(a, b)| (a..=b).contains(&u));
    c == ' ' || !(c.is_control() || c.is_whitespace() || in_table(CF) || in_table(CO))
}

// --- control socket -------------------------------------------------------------------------------
/// One client socket to wpa_supplicant (requests, or events after attach()).
pub struct Ctrl {
    pub path: PathBuf,
    pub local: PathBuf,
    pub sock: Option<UnixDatagram>,
}

impl Ctrl {
    pub fn new(path: impl Into<PathBuf>, local_dir: impl AsRef<Path>) -> Ctrl {
        static N: AtomicU32 = AtomicU32::new(0);
        let n = N.fetch_add(1, Ordering::Relaxed) + 1;
        let local = local_dir
            .as_ref()
            .join(format!("i14-ui-wpa-{}-{n}", std::process::id()));
        Ctrl {
            path: path.into(),
            local,
            sock: None,
        }
    }

    pub fn open(&mut self) -> io::Result<&UnixDatagram> {
        if self.sock.is_none() {
            let _ = fs::remove_file(&self.local);
            let s = UnixDatagram::bind(&self.local).and_then(|s| s.connect(&self.path).map(|_| s));
            match s {
                Ok(s) => self.sock = Some(s),
                Err(e) => {
                    let _ = fs::remove_file(&self.local);
                    return Err(ctrl_error(e.kind(), py_str(&e)));
                }
            }
        }
        Ok(self.sock.as_ref().expect("opened"))
    }

    pub fn close(&mut self) {
        self.sock = None;
        let _ = fs::remove_file(&self.local);
    }

    pub fn fileno(&self) -> Option<RawFd> {
        self.sock.as_ref().map(|s| s.as_raw_fd())
    }

    pub fn request(&mut self, cmd: &str, timeout: f64) -> io::Result<String> {
        let s = self.open()?;
        let r = (|| {
            s.set_read_timeout(Some(Duration::from_secs_f64(timeout)))?;
            s.set_write_timeout(Some(Duration::from_secs_f64(timeout)))?;
            s.send(cmd.as_bytes())?;
            let end = monotonic() + timeout;
            let mut buf = vec![0u8; 65536];
            loop {
                s.set_read_timeout(Some(Duration::from_secs_f64((end - monotonic()).max(0.05))))?;
                let n = s.recv(&mut buf)?;
                let reply = String::from_utf8_lossy(&buf[..n]).into_owned();
                if !reply.starts_with('<') {
                    return Ok(reply); // skip unsolicited events on this socket
                }
            }
        })();
        r.map_err(|e: io::Error| {
            self.close();
            let text = match e.kind() {
                io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut => "timed out".to_string(),
                _ => py_str(&e),
            };
            let word = cmd.split(py_isspace).find(|w| !w.is_empty()).unwrap_or("");
            ctrl_error(e.kind(), format!("{word}: {text}"))
        })
    }

    pub fn attach(&mut self) -> io::Result<()> {
        if py_strip(&self.request("ATTACH", REQUEST_TIMEOUT)?) != "OK" {
            return Err(ctrl_error(io::ErrorKind::Other, "ATTACH refused".into()));
        }
        self.sock.as_ref().expect("attached").set_nonblocking(true)
    }

    pub fn events(&mut self) -> Vec<String> {
        let mut out = Vec::new();
        let mut buf = vec![0u8; 65536];
        loop {
            let Some(s) = self.sock.as_ref() else {
                return out;
            };
            match s.recv(&mut buf) {
                Ok(n) => out.push(String::from_utf8_lossy(&buf[..n]).into_owned()),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => return out,
                Err(_) => {
                    self.close();
                    return out;
                }
            }
        }
    }
}

impl Drop for Ctrl {
    fn drop(&mut self) {
        self.close();
    }
}

// --- parsing -------------------------------------------------------------------------------------
/// wpa_supplicant's printf_encode()d SSID back to bytes.
pub fn decode_ssid(raw: &[u8]) -> Vec<u8> {
    let (mut out, mut i) = (Vec::new(), 0);
    while i < raw.len() {
        let c = raw[i];
        if c == b'\\' && i + 1 < raw.len() {
            let n = raw[i + 1];
            if n == b'x' && i + 3 < raw.len() {
                if let Some(v) = py_hex2(&raw[i + 2..i + 4]) {
                    out.push(v);
                    i += 4;
                    continue;
                }
            }
            let simple = match n {
                b'\\' => Some(0x5C),
                b'"' => Some(0x22),
                b'n' => Some(0x0A),
                b'r' => Some(0x0D),
                b't' => Some(0x09),
                b'e' => Some(0x1B),
                _ => None,
            };
            if let Some(v) = simple {
                out.push(v);
                i += 2;
                continue;
            }
        }
        out.push(c);
        i += 1;
    }
    out
}

pub fn show_ssid(ssid: &[u8]) -> String {
    let text: String = String::from_utf8_lossy(ssid)
        .chars()
        .map(|ch| if py_printable(ch) { ch } else { '?' })
        .collect();
    if text.is_empty() {
        "(verborgen)".into()
    } else {
        text
    }
}

pub fn parse_kv(text: &str) -> HashMap<String, String> {
    splitlines(text)
        .into_iter()
        .filter_map(|l| l.split_once('='))
        .map(|(k, v)| (k.into(), v.into()))
        .collect()
}

pub fn security(flags: &str) -> &'static str {
    if flags.contains("EAP") {
        "eap"
    } else if flags.contains("PSK") {
        "psk"
    } else if flags.contains("SAE") {
        "sae"
    } else if flags.contains("WEP") {
        "wep"
    } else {
        "open"
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct ScanResult {
    pub ssid: Vec<u8>,
    pub signal: i64,
    pub security: &'static str,
    pub flags: String,
    pub freq: String,
}

/// Strongest entry per SSID, strongest first.
pub fn parse_scan(text: &str) -> Vec<ScanResult> {
    let mut best: Vec<ScanResult> = Vec::new(); // insertion-ordered, like the Python dict
    for line in splitlines(text).into_iter().skip(1) {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() < 5 || parts[4].is_empty() {
            continue;
        }
        let Some(signal) = py_int(parts[2]) else {
            continue;
        };
        let ssid = decode_ssid(parts[4].as_bytes());
        if ssid.iter().all(|&b| b == 0) {
            continue;
        }
        let entry = ScanResult {
            ssid,
            signal,
            security: security(parts[3]),
            flags: parts[3].into(),
            freq: parts[1].into(),
        };
        match best.iter_mut().find(|e| e.ssid == entry.ssid) {
            Some(e) if signal > e.signal => *e = entry,
            Some(_) => {}
            None => best.push(entry),
        }
    }
    best.sort_by_key(|e| std::cmp::Reverse(e.signal)); // stable, like sorted()
    best
}

#[derive(Clone, Debug, PartialEq)]
pub struct Network {
    pub id: i64,
    pub ssid: Vec<u8>,
    pub flags: String,
    /// Filled in by `Wifi::saved()` (0 from `parse_networks`).
    pub priority: i64,
}

/// LIST_NETWORKS.
pub fn parse_networks(text: &str) -> Vec<Network> {
    let mut out = Vec::new();
    for line in splitlines(text).into_iter().skip(1) {
        let parts: Vec<&str> = line.split('\t').collect();
        if parts.len() >= 2 && is_digits(parts[0]) {
            let Ok(id) = parts[0].parse() else { continue };
            out.push(Network {
                id,
                ssid: decode_ssid(parts[1].as_bytes()),
                flags: parts.get(3).unwrap_or(&"").to_string(),
                priority: 0,
            });
        }
    }
    out
}

pub fn bars(dbm: i64) -> usize {
    if dbm >= -55 {
        4
    } else if dbm >= -67 {
        3
    } else if dbm >= -75 {
        2
    } else if dbm >= -85 {
        1
    } else {
        0
    }
}

/// The WPA2 PSK (what wpa_passphrase computes): PBKDF2-HMAC-SHA1, 4096 rounds, 32 bytes.
pub fn psk_hex(passphrase: &str, ssid: &[u8]) -> String {
    let mut out = [0u8; 32];
    pbkdf2::pbkdf2_hmac::<sha1::Sha1>(passphrase.as_bytes(), ssid, 4096, &mut out);
    hex(&out)
}

// --- the saved networks file ---------------------------------------------------------------------
fn value_ssid(value: &str) -> Vec<u8> {
    let value = py_strip(value);
    if value.starts_with('"') && value.ends_with('"') && value.len() >= 2 {
        return value.as_bytes()[1..value.len() - 1].to_vec();
    }
    if value.starts_with("P\"") {
        let last = value.char_indices().last().map_or(0, |(i, _)| i); // value[2:-1]
        return decode_ssid(value.get(2..last).unwrap_or("").as_bytes());
    }
    fromhex(value).unwrap_or_else(|| value.as_bytes().to_vec())
}

#[derive(Clone, Debug, PartialEq)]
pub struct Saved {
    pub ssid: Option<Vec<u8>>,
    pub priority: i64,
}

struct Part {
    net: bool,
    lines: Vec<String>,
}

fn block_ssid(block: &[String]) -> Option<Vec<u8>> {
    block
        .iter()
        .map(|l| py_strip(l))
        .find_map(|s| s.strip_prefix("ssid=").map(value_ssid))
}

fn is_net(p: &Part, ssid: &[u8]) -> bool {
    p.net && block_ssid(&p.lines).as_deref() == Some(ssid)
}

/// network={...} blocks of a wpa_supplicant config file; other lines are kept as they are.
#[derive(Clone, Debug)]
pub struct Store {
    pub path: PathBuf,
}

impl Store {
    pub fn new(path: impl Into<PathBuf>) -> Store {
        Store { path: path.into() }
    }

    fn parts(&self) -> io::Result<Vec<Part>> {
        let text = match fs::read_to_string(&self.path) {
            Ok(t) => t,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(e),
        };
        let (mut parts, mut block): (Vec<Part>, Option<Vec<String>>) = (Vec::new(), None);
        for line in splitlines(&text) {
            let s = py_strip(line);
            if let Some(b) = block.as_mut() {
                b.push(line.into());
                if s == "}" {
                    parts.push(Part {
                        net: true,
                        lines: block.take().expect("block"),
                    });
                }
            } else if s.starts_with("network={") {
                block = Some(vec![line.into()]);
            } else {
                parts.push(Part {
                    net: false,
                    lines: vec![line.into()],
                });
            }
        }
        if let Some(b) = block {
            parts.push(Part {
                net: false,
                lines: b,
            }); // unterminated: keep as it is
        }
        Ok(parts)
    }

    pub fn networks(&self) -> io::Result<Vec<Saved>> {
        let mut out = Vec::new();
        for p in self.parts()?.iter().filter(|p| p.net) {
            let mut prio = 0;
            for line in &p.lines {
                if let Some(v) = py_strip(line).strip_prefix("priority=") {
                    if let Some(n) = py_int(v) {
                        prio = n;
                    }
                }
            }
            out.push(Saved {
                ssid: block_ssid(&p.lines),
                priority: prio,
            });
        }
        Ok(out)
    }

    fn write(&self, parts: &[Part]) -> io::Result<()> {
        let text: String = parts
            .iter()
            .flat_map(|p| &p.lines)
            .map(|l| format!("{l}\n"))
            .collect();
        let d = match self.path.parent() {
            Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
            _ => PathBuf::from("."),
        };
        fs::create_dir_all(&d)?;
        let (mut f, tmp) = mkstemp(&d, ".wpa-")?;
        let r = (|| {
            f.set_permissions(fs::Permissions::from_mode(0o600))?;
            f.write_all(text.as_bytes())?;
            f.flush()?;
            fsync(&f)?;
            fs::rename(&tmp, &self.path)?;
            fsync(&File::open(&d)?)
        })();
        if r.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        r
    }

    /// Add or replace the network for ssid; returns the block lines.
    pub fn put(
        &self,
        ssid: &[u8],
        key_mgmt: &str,
        psk: Option<&str>,
        priority: Option<i64>,
        hidden: bool,
    ) -> io::Result<Vec<String>> {
        let mut block = vec![
            "network={".to_string(),
            format!("\t# {}", show_ssid(ssid)),
            format!("\tssid={}", hex(ssid)),
            format!("\tkey_mgmt={key_mgmt}"),
        ];
        if let Some(psk) = psk.filter(|p| !p.is_empty()) {
            block.push(format!("\tpsk={psk}"));
        }
        if hidden {
            block.push("\tscan_ssid=1".into());
        }
        let mut parts = self.parts()?;
        let mut priority = priority;
        if priority.is_none() {
            if let Some(old) = parts.iter().find(|p| is_net(p, ssid)) {
                for line in &old.lines {
                    if let Some(v) = py_strip(line).strip_prefix("priority=") {
                        priority = py_int(v);
                    }
                }
            }
        }
        if let Some(p) = priority.filter(|&p| p != 0) {
            block.push(format!("\tpriority={p}"));
        }
        block.push("}".into());
        parts.retain(|p| !is_net(p, ssid));
        parts.push(Part {
            net: true,
            lines: block.clone(),
        });
        self.write(&parts)?;
        Ok(block)
    }

    pub fn set_priority(&self, ssid: &[u8], priority: i64) -> io::Result<bool> {
        let mut parts = self.parts()?;
        let mut found = false;
        for p in parts.iter_mut().filter(|p| is_net(p, ssid)) {
            p.lines.retain(|l| !py_strip(l).starts_with("priority="));
            let at = p.lines.len() - 1;
            p.lines.insert(at, format!("\tpriority={priority}"));
            found = true;
        }
        if found {
            self.write(&parts)?;
        }
        Ok(found)
    }

    pub fn remove(&self, ssid: &[u8]) -> io::Result<bool> {
        let mut parts = self.parts()?;
        let n = parts.len();
        parts.retain(|p| !is_net(p, ssid));
        if parts.len() != n {
            self.write(&parts)?;
            return Ok(true);
        }
        Ok(false)
    }
}

/// `tempfile.mkstemp(prefix=..., dir=d)`: a new 0600 file with a unique name.
fn mkstemp(d: &Path, prefix: &str) -> io::Result<(File, PathBuf)> {
    static N: AtomicU32 = AtomicU32::new(0);
    let mut last = io::Error::from(io::ErrorKind::AlreadyExists);
    for _ in 0..100 {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |t| t.as_nanos() as u64);
        let mut x = nanos
            ^ (u64::from(std::process::id()) << 32)
            ^ u64::from(N.fetch_add(1, Ordering::Relaxed));
        let name: String = (0..8)
            .map(|_| {
                let c = b"abcdefghijklmnopqrstuvwxyz0123456789_"[(x % 37) as usize];
                x /= 37;
                c as char
            })
            .collect();
        let path = d.join(format!("{prefix}{name}"));
        match OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)
        {
            Ok(f) => return Ok((f, path)),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => last = e,
            Err(e) => return Err(e),
        }
    }
    Err(last)
}

/// `os.fsync(fd)` (fsync on Linux).
fn fsync(f: &File) -> io::Result<()> {
    f.sync_all()
}

// --- the manager the screens use -----------------------------------------------------------------
/// `subprocess.run(argv, timeout=...)`: None when it cannot start or runs too long (then killed).
fn run(argv: &[&str], timeout: f64, capture: bool) -> Option<(ExitStatus, Vec<u8>)> {
    let mut cmd = Command::new(argv[0]);
    cmd.args(&argv[1..]);
    if capture {
        cmd.stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
    }
    let mut child = cmd.spawn().ok()?;
    let reader = child.stdout.take().map(|mut out| {
        std::thread::spawn(move || {
            let mut buf = Vec::new();
            let _ = out.read_to_end(&mut buf);
            buf
        })
    });
    let end = Instant::now() + Duration::from_secs_f64(timeout);
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if Instant::now() < end => std::thread::sleep(Duration::from_millis(10)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let out = reader.and_then(|r| r.join().ok()).unwrap_or_default();
    Some((status, out))
}

pub fn ipv4(iface: &str) -> String {
    let Some((_, out)) = run(&["ip", "-4", "-o", "addr", "show", "dev", iface], 2.0, true) else {
        return String::new();
    };
    let out = String::from_utf8_lossy(&out);
    for line in splitlines(&out) {
        let parts: Vec<&str> = line.split(py_isspace).filter(|w| !w.is_empty()).collect();
        if let Some(i) = parts.iter().position(|&w| w == "inet") {
            if let Some(addr) = parts.get(i + 1) {
                return addr.split('/').next().unwrap_or("").to_string();
            }
        }
    }
    String::new()
}

/// Regenerate /run/i14-wpa.conf from the saved networks and the off flag.
pub fn write_start_file() -> bool {
    run(&[CONF_TOOL], 10.0, false).is_some_and(|(st, _)| st.code() == Some(0))
}

/// Make the one DHCP client (i14-wifi) drop its lease and ask anew: release, then renew.
pub fn udhcpc_new_lease(pid_file: &Path) -> bool {
    let Ok(text) = fs::read_to_string(pid_file) else {
        return false;
    };
    // Only a real process: 0 or a negative pid would signal a whole process group, or everything.
    let Some(pid) = py_int(&text)
        .and_then(|p| u32::try_from(p).ok())
        .filter(|&p| p > 0 && p <= i32::MAX as u32)
    else {
        return false;
    };
    if i14_sys::process::kill(pid, libc::SIGUSR2).is_err() {
        return false; // release (address and route removed)
    }
    i14_sys::process::kill(pid, libc::SIGUSR1).is_ok() // renew from scratch
}

/// STATUS as key=value pairs, plus the decoded SSID.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Status {
    pub fields: HashMap<String, String>,
    pub ssid_bytes: Option<Vec<u8>>,
}

impl Status {
    pub fn get(&self, key: &str) -> Option<&str> {
        self.fields.get(key).map(String::as_str)
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Assoc,
    Dhcp,
}

/// A network typed in this attempt, saved once it works.
#[derive(Clone, Debug, PartialEq)]
pub struct NewNet {
    pub key_mgmt: &'static str,
    pub psk: Option<String>,
    pub hidden: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Attempt {
    pub ssid: Vec<u8>,
    pub id: i64,
    pub new: Option<NewNet>,
    pub phase: Phase,
    pub since: f64,
    pub before: Option<Vec<u8>>,
    pub not_found: u32,
    pub replaces: Option<i64>,
    /// None until a new network was (or failed to be) saved; Python's `a.get("saved", True)`.
    pub saved: Option<bool>,
    pub stale: bool,
}

/// State for the Wi-Fi screens. Call events() when the event socket is readable and tick() often.
pub struct Wifi {
    /// Regenerates the start file; true on success (default `write_start_file`).
    pub start_file: Box<dyn FnMut() -> bool>,
    /// The current IPv4 address or "" (default `ipv4(IFACE)`).
    pub ip: Box<dyn FnMut() -> String>,
    /// Asks the DHCP client for a fresh lease (default `udhcpc_new_lease(UDHCPC_PID)`).
    pub new_lease: Box<dyn FnMut() -> bool>,
    pub req: Ctrl,
    pub mon: Ctrl,
    pub store: Store,
    pub rootfs_store: Store,
    pub off_flag: PathBuf,
    pub scan_results: Vec<ScanResult>,
    pub scanning: bool,
    pub scan_started: f64,
    pub attempt: Option<Attempt>,
    pub message: String,
    pub attached: bool,
    pub next_attach: f64,
    pub next_ping: f64,
    pub changed: bool,
    pub lease_on_connect: bool,
    pub last_ssid: Option<Vec<u8>>,
}

pub const STALE: &str = "; startbestand niet bijgewerkt (geldt pas na herstart van wifi)";

impl Wifi {
    /// `Wifi()` with the system paths.
    pub fn system() -> Wifi {
        Wifi::new(
            CTRL,
            Store::new(NETWORKS),
            Store::new(ROOTFS_NETWORKS),
            OFF_FLAG,
            "/run",
        )
    }

    pub fn new(
        ctrl_path: impl AsRef<Path>,
        store: Store,
        rootfs_store: Store,
        off_flag: impl Into<PathBuf>,
        local_dir: impl AsRef<Path>,
    ) -> Wifi {
        let ctrl_path = ctrl_path.as_ref();
        let mut w = Wifi {
            start_file: Box::new(write_start_file),
            ip: Box::new(|| ipv4(IFACE)),
            new_lease: Box::new(|| udhcpc_new_lease(Path::new(UDHCPC_PID))),
            req: Ctrl::new(ctrl_path, local_dir.as_ref()),
            mon: Ctrl::new(ctrl_path, local_dir.as_ref()),
            store,
            rootfs_store,
            off_flag: off_flag.into(),
            scan_results: Vec::new(),
            scanning: false,
            scan_started: f64::NEG_INFINITY,
            attempt: None,
            message: String::new(),
            attached: false,
            next_attach: 0.0,
            next_ping: f64::NEG_INFINITY,
            changed: true,
            lease_on_connect: false,
            last_ssid: None,
        };
        let st = w.status(); // the network now: a later switch to another one needs a new lease
        w.last_ssid = if st.get("wpa_state") == Some("COMPLETED") {
            st.ssid_bytes
        } else {
            None
        };
        w
    }

    // control socket
    pub fn cmd(&mut self, text: &str) -> io::Result<String> {
        Ok(py_strip(&self.request(text)?).to_string())
    }

    /// One request. A failure means wpa_supplicant is gone or restarted: on Linux the event socket
    /// then stays connected to the dead one and gets nothing, so it is dropped and attaches again.
    pub fn request(&mut self, text: &str) -> io::Result<String> {
        self.req
            .request(text, REQUEST_TIMEOUT)
            .inspect_err(|_| self.drop_monitor())
    }

    pub fn drop_monitor(&mut self) {
        if self.attached {
            self.mon.close();
            self.attached = false;
            self.next_attach = 0.0;
        }
    }

    pub fn monitor_fd(&mut self) -> Option<RawFd> {
        if !self.attached && monotonic() >= self.next_attach {
            match self.mon.attach() {
                Ok(()) => {
                    self.attached = true;
                    self.lease_check(); // the network now; one that changed while nobody listened gets a new lease
                }
                Err(_) => {
                    self.mon.close();
                    self.next_attach = monotonic() + 5.0;
                }
            }
        }
        if self.attached {
            self.mon.fileno()
        } else {
            None
        }
    }

    pub fn available(&mut self) -> bool {
        self.cmd("PING").is_ok_and(|r| r == "PONG")
    }

    pub fn enabled(&self) -> bool {
        !self.off_flag.exists()
    }

    pub fn status(&mut self) -> Status {
        let Ok(reply) = self.cmd("STATUS") else {
            return Status {
                fields: HashMap::from([("wpa_state".into(), "GEEN_WPA".into())]),
                ssid_bytes: None,
            };
        };
        let fields = parse_kv(&reply);
        let ssid_bytes = fields.get("ssid").map(|s| decode_ssid(s.as_bytes()));
        Status { fields, ssid_bytes }
    }

    // actions
    fn set_flag(&self, off: bool) -> io::Result<()> {
        if off {
            let mut f = File::create(&self.off_flag)?;
            f.write_all(b"off\n")?;
            f.flush()?;
            fsync(&f)
        } else {
            match fs::remove_file(&self.off_flag) {
                Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
                r => r,
            }
        }
    }

    /// Switch Wi-Fi on or off for good: flag file, start file, then wpa_supplicant reloads it.
    /// If the start file or the reload fails, the old state is put back and nothing claims success.
    pub fn set_enabled(&mut self, on: bool) -> bool {
        if self.attempt.is_some() {
            // its network must go and the others must be enabled again, either way
            self.end_attempt("vorige poging afgebroken".into(), false);
        }
        let was_on = self.enabled();
        if let Err(e) = self.set_flag(!on) {
            self.message = format!("opslaan mislukt: {}", strerror(&e));
            self.changed = true;
            return false;
        }
        let ok = (self.start_file)() && self.cmd("RECONFIGURE").is_ok_and(|r| r == "OK");
        if !ok {
            let _ = self.set_flag(!was_on);
            (self.start_file)();
            self.message = format!(
                "Wi-Fi {} mislukt; niets veranderd",
                if on { "aanzetten" } else { "uitzetten" }
            );
            self.changed = true;
            return false;
        }
        self.message = if on { "Wi-Fi aan" } else { "Wi-Fi uit" }.into();
        self.lease_on_connect = on; // the lease of before "off" may have lost its route: a fresh one
        self.changed = true;
        true
    }

    pub fn scan(&mut self) -> io::Result<()> {
        if !self.enabled() {
            self.message = "Wi-Fi staat uit".into();
            return Ok(());
        }
        let reply = self.cmd("SCAN")?;
        if reply == "OK" || reply == "FAIL-BUSY" {
            self.scanning = true;
            self.scan_started = monotonic();
            self.message = "zoeken...".into();
        } else {
            self.message = format!("zoeken mislukt ({reply})");
        }
        self.changed = true;
        Ok(())
    }

    pub fn refresh_results(&mut self) {
        self.scan_results = self
            .request("SCAN_RESULTS")
            .map(|r| parse_scan(&r))
            .unwrap_or_default();
        self.changed = true;
    }

    /// Saved networks: the running ones (with id) plus what the files hold.
    pub fn saved(&mut self) -> io::Result<Vec<Network>> {
        let mut running = self
            .request("LIST_NETWORKS")
            .map(|r| parse_networks(&r))
            .unwrap_or_default();
        let mut prio = HashMap::new();
        for n in self
            .rootfs_store
            .networks()?
            .into_iter()
            .chain(self.store.networks()?)
        {
            prio.insert(n.ssid, n.priority);
        }
        for n in &mut running {
            n.priority = prio.get(&Some(n.ssid.clone())).copied().unwrap_or(0);
        }
        Ok(running)
    }

    pub fn network_id(&mut self, ssid: &[u8]) -> io::Result<Option<i64>> {
        Ok(self
            .saved()?
            .into_iter()
            .find(|n| n.ssid == ssid)
            .map(|n| n.id))
    }

    fn fail(&mut self, message: &str) -> io::Result<bool> {
        self.message = message.into();
        self.changed = true;
        Ok(false)
    }

    /// Connect to a saved network (no passphrase) or a new one. Saved on success only.
    pub fn connect(
        &mut self,
        ssid: &[u8],
        sec: &str,
        passphrase: Option<&str>,
        hidden: bool,
    ) -> io::Result<bool> {
        if self.attempt.is_some() {
            // a second choice ends the first: its unconfirmed network must not stay
            self.end_attempt("vorige poging afgebroken".into(), false);
        }
        match sec {
            // refused before anything changes (Wi-Fi stays off if it was)
            "sae" => return self.fail("WPA3-only netwerken worden niet ondersteund"),
            "wep" => return self.fail("WEP wordt niet ondersteund (onveilig)"),
            "eap" => return self.fail("Bedrijfsnetwerken (802.1X) worden niet ondersteund"),
            _ => {}
        }
        let good = |p: &str| p.is_ascii() && (8..=63).contains(&p.len());
        if sec == "psk" && passphrase.is_some_and(|p| !good(p)) {
            return self.fail("Wachtwoord moet 8 tot 63 tekens zijn"); // checked before anything changes
        }
        if !self.enabled() && !self.set_enabled(true) {
            return Ok(false); // its message says why
        }
        let mut nid = self.network_id(ssid)?;
        let (mut new_block, mut replaces) = (None, None);
        if passphrase.is_some() || nid.is_none() {
            let (psk, key_mgmt) = if sec == "psk" {
                match passphrase.filter(|p| good(p)) {
                    Some(p) => (Some(psk_hex(p, ssid)), "WPA-PSK"),
                    None => return self.fail("Wachtwoord moet 8 tot 63 tekens zijn"),
                }
            } else {
                (None, "NONE")
            };
            replaces = nid; // a saved network with a new password: the old entry goes once this one works
            let reply = self.cmd("ADD_NETWORK")?;
            let Some(id) = Some(&reply)
                .filter(|r| is_digits(r))
                .and_then(|r| r.parse().ok())
            else {
                return self.fail(&format!("Instellen mislukt ({reply})"));
            };
            nid = Some(id);
            let mut settings = vec![
                format!("ssid {}", hex(ssid)),
                format!("key_mgmt {key_mgmt}"),
            ];
            if let Some(psk) = &psk {
                settings.push(format!("psk {psk}"));
            }
            if hidden {
                settings.push("scan_ssid 1".into());
            }
            for s in settings {
                if self.cmd(&format!("SET_NETWORK {id} {s}"))? != "OK" {
                    self.cmd(&format!("REMOVE_NETWORK {id}"))?;
                    return self.fail("Instellen mislukt");
                }
            }
            new_block = Some(NewNet {
                key_mgmt,
                psk,
                hidden,
            });
        }
        let id = nid.expect("a network id");
        let before = self.status().ssid_bytes;
        self.cmd(&format!("SELECT_NETWORK {id}"))?; // disables the others until this attempt ends
        self.attempt = Some(Attempt {
            ssid: ssid.to_vec(),
            id,
            new: new_block,
            phase: Phase::Assoc,
            since: monotonic(),
            before,
            not_found: 0,
            replaces,
            saved: None,
            stale: false,
        });
        self.message = format!("verbinden met {}...", show_ssid(ssid));
        self.changed = true;
        Ok(true)
    }

    fn end_attempt(&mut self, message: String, keep: bool) {
        let a = self.attempt.take();
        if let Some(a) = a.filter(|a| a.new.is_some() && !keep) {
            let _ = self.cmd(&format!("REMOVE_NETWORK {}", a.id));
        }
        let _ = self.cmd("ENABLE_NETWORK all");
        self.message = message;
        self.changed = true;
    }

    /// Remove the network from the files first, then from the running wpa_supplicant.
    pub fn forget(&mut self, ssid: &[u8]) -> io::Result<bool> {
        let gone = match self.store.remove(ssid) {
            Ok(g) => self.rootfs_store.remove(ssid).map_or(g, |r| r || g),
            Err(e) => return self.fail(&format!("vergeten mislukt: {}", strerror(&e))),
        };
        let fresh = (self.start_file)();
        let nid = self.network_id(ssid)?;
        if let Some(n) = nid {
            self.cmd(&format!("REMOVE_NETWORK {n}"))?;
        }
        self.message = format!(
            "{} vergeten{}",
            show_ssid(ssid),
            if fresh { "" } else { STALE }
        );
        self.changed = true;
        Ok(gone || nid.is_some())
    }

    /// Make ssid the preferred saved network (highest priority).
    pub fn prefer(&mut self, ssid: &[u8]) -> io::Result<bool> {
        let top = self
            .saved()?
            .iter()
            .map(|n| n.priority)
            .max()
            .unwrap_or(0)
            .max(0)
            + 1;
        if !self.store.set_priority(ssid, top)? {
            // the file first: only networks added here
            return self.fail("alleen netwerken die hier zijn toegevoegd");
        }
        if let Some(n) = self.network_id(ssid)? {
            self.cmd(&format!("SET_NETWORK {n} priority {top}"))?;
        }
        let fresh = (self.start_file)();
        self.message = format!(
            "{} heeft nu voorrang{}",
            show_ssid(ssid),
            if fresh { "" } else { STALE }
        );
        self.changed = true;
        Ok(true)
    }

    // events and time
    pub fn events(&mut self) {
        let evs = if self.attached {
            self.mon.events()
        } else {
            Vec::new()
        };
        for ev in evs {
            self.handle(&ev);
        }
        if self.attached && self.mon.sock.is_none() {
            self.attached = false;
        }
    }

    pub fn handle(&mut self, ev: &str) {
        let text = ev.split_once('>').map_or(ev, |(_, t)| t);
        self.changed = true;
        if text.starts_with("CTRL-EVENT-SCAN-RESULTS") {
            self.scanning = false;
            if self.message == "zoeken..." {
                self.message.clear();
            }
            self.refresh_results();
            return;
        }
        if text.starts_with("CTRL-EVENT-SCAN-FAILED") {
            self.scanning = false;
            self.message = "zoeken mislukt".into();
            return;
        }
        if text.starts_with("CTRL-EVENT-CONNECTED") {
            self.lease_check();
        }
        let Some(a) = &self.attempt else { return };
        let (id, phase, ssid) = (a.id, a.phase, a.ssid.clone());
        if text.starts_with("CTRL-EVENT-SSID-TEMP-DISABLED")
            && format!("{text} ").contains(&format!("id={id} "))
        {
            if text.contains("reason=WRONG_KEY") {
                self.end_attempt("Verkeerd wachtwoord".into(), false);
            } else if text.contains("reason=CONN_FAILED") || text.contains("reason=AUTH_FAILED") {
                self.end_attempt("Verbinding geweigerd door het netwerk".into(), false);
            }
        } else if text.starts_with("CTRL-EVENT-NETWORK-NOT-FOUND") && phase == Phase::Assoc {
            let a = self.attempt.as_mut().expect("attempt");
            a.not_found += 1;
            if a.not_found >= 2 {
                self.end_attempt(format!("{} niet gevonden", show_ssid(&ssid)), false);
            }
        } else if text.starts_with("CTRL-EVENT-CONNECTED")
            && phase == Phase::Assoc
            && self.status().ssid_bytes.as_deref() == Some(&ssid[..])
        {
            self.connected();
        }
    }

    fn connected(&mut self) {
        let Some(a) = self.attempt.as_ref() else {
            return;
        };
        let (ssid, new, replaces) = (a.ssid.clone(), a.new.clone(), a.replaces);
        let (mut saved, mut stale) = (a.saved, a.stale);
        if let Some(n) = new {
            match self
                .store
                .put(&ssid, n.key_mgmt, n.psk.as_deref(), None, n.hidden)
            {
                Ok(_) => {
                    saved = Some(true);
                    stale = !(self.start_file)(); // saved, but a wpa_supplicant respawn would not know it
                }
                Err(e) => {
                    saved = Some(false);
                    self.message = format!("verbonden, maar opslaan mislukt: {}", strerror(&e));
                }
            }
            if let Some(a) = self.attempt.as_mut() {
                a.new = None; // keep it running whatever happens next
            }
            if let Some(r) = replaces {
                let _ = self.cmd(&format!("REMOVE_NETWORK {r}"));
            }
        }
        if let Some(a) = self.attempt.as_mut() {
            (a.saved, a.stale, a.phase, a.since) = (saved, stale, Phase::Dhcp, monotonic());
        }
        if saved.unwrap_or(true) {
            self.message = format!("verbonden met {}, adres ophalen...", show_ssid(&ssid));
        }
        let _ = self.cmd("ENABLE_NETWORK all");
    }

    /// On every association: a fresh DHCP lease when the network changed (any reason: a connect,
    /// a forgotten network, wpa_supplicant's own choice) or Wi-Fi was switched back on.
    fn lease_check(&mut self) {
        let ssid = self.status().ssid_bytes;
        let changed = self.last_ssid.is_some() && ssid.is_some() && ssid != self.last_ssid;
        if changed || self.lease_on_connect {
            (self.new_lease)();
        }
        self.lease_on_connect = false;
        if ssid.is_some() {
            self.last_ssid = ssid;
        }
    }

    pub fn tick(&mut self) {
        if monotonic() >= self.next_ping {
            // notices a restarted wpa_supplicant
            self.next_ping = monotonic() + 30.0;
            let _ = self.request("PING");
        }
        if self.scanning && monotonic() - self.scan_started > 15.0 {
            self.scanning = false;
            self.refresh_results();
        }
        let Some(a) = &self.attempt else { return };
        let (ssid, phase, before, saved, stale) =
            (a.ssid.clone(), a.phase, a.before.clone(), a.saved, a.stale);
        let age = monotonic() - a.since;
        match phase {
            Phase::Assoc => {
                if self.status().get("wpa_state") == Some("COMPLETED")
                    && self.status().ssid_bytes.as_deref() == Some(&ssid[..])
                {
                    self.connected();
                } else if age > ASSOC_TIMEOUT {
                    self.end_attempt(
                        format!("Geen verbinding met {} (time-out)", show_ssid(&ssid)),
                        false,
                    );
                }
            }
            Phase::Dhcp => {
                let ip = (self.ip)();
                let wait = if before.as_deref() == Some(&ssid[..]) {
                    1.0
                } else {
                    4.0
                };
                if !ip.is_empty() && age > wait {
                    // after a release the old address goes
                    let mut note = if saved.unwrap_or(true) {
                        ""
                    } else {
                        " (niet opgeslagen)"
                    }
                    .to_string();
                    if stale {
                        note += STALE;
                    }
                    self.end_attempt(
                        format!("Verbonden met {}, adres {ip}{note}", show_ssid(&ssid)),
                        true,
                    );
                } else if age > DHCP_TIMEOUT {
                    self.end_attempt("Verbonden, maar geen IP-adres gekregen (DHCP)".into(), true);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;
    use std::sync::{Arc, Mutex};
    use std::thread::JoinHandle;

    type Replies = Arc<Mutex<Box<dyn FnMut(&str) -> String + Send>>>;

    /// A short private directory (macOS limits unix socket paths to ~104 bytes).
    fn tmpdir() -> PathBuf {
        static N: AtomicU32 = AtomicU32::new(0);
        let d = PathBuf::from(format!(
            "/tmp/i14w-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir(&d).unwrap();
        fs::set_permissions(&d, fs::Permissions::from_mode(0o700)).unwrap();
        d
    }

    /// A unix datagram server answering like wpa_supplicant; records commands, can send events.
    struct FakeSupplicant {
        path: PathBuf,
        sock: Option<Arc<UnixDatagram>>,
        replies: Replies,
        commands: Arc<Mutex<Vec<String>>>,
        attached: Arc<Mutex<Vec<PathBuf>>>,
        stop: Arc<AtomicBool>,
        thread: Option<JoinHandle<()>>,
    }

    impl FakeSupplicant {
        fn new(d: &Path, replies: Replies) -> FakeSupplicant {
            let path = d.join("wlan0");
            let sock = Arc::new(UnixDatagram::bind(&path).unwrap());
            sock.set_read_timeout(Some(Duration::from_millis(100)))
                .unwrap();
            let commands: Arc<Mutex<Vec<String>>> = Arc::default();
            let attached: Arc<Mutex<Vec<PathBuf>>> = Arc::default();
            let stop = Arc::new(AtomicBool::new(false));
            let thread = {
                let (sock, replies, commands, attached, stop) = (
                    sock.clone(),
                    replies.clone(),
                    commands.clone(),
                    attached.clone(),
                    stop.clone(),
                );
                std::thread::spawn(move || {
                    let mut buf = [0u8; 4096];
                    while !stop.load(Ordering::SeqCst) {
                        let Ok((n, addr)) = sock.recv_from(&mut buf) else {
                            continue;
                        };
                        let cmd = String::from_utf8_lossy(&buf[..n]).into_owned();
                        commands.lock().unwrap().push(cmd.clone());
                        let Some(addr) = addr.as_pathname().map(Path::to_path_buf) else {
                            continue;
                        };
                        let reply = if cmd == "ATTACH" {
                            attached.lock().unwrap().push(addr.clone());
                            "OK".to_string()
                        } else {
                            (replies.lock().unwrap())(&cmd)
                        };
                        let _ = sock.send_to(format!("{reply}\n").as_bytes(), &addr);
                    }
                })
            };
            FakeSupplicant {
                path,
                sock: Some(sock),
                replies,
                commands,
                attached,
                stop,
                thread: Some(thread),
            }
        }

        fn event(&self, text: &str) {
            let sock = self.sock.as_ref().unwrap();
            for addr in self.attached.lock().unwrap().iter() {
                let _ = sock.send_to(format!("<3>{text}").as_bytes(), addr);
            }
        }

        fn commands(&self) -> Vec<String> {
            self.commands.lock().unwrap().clone()
        }

        fn close(&mut self) {
            self.stop.store(true, Ordering::SeqCst);
            if let Some(t) = self.thread.take() {
                t.join().unwrap();
            }
            self.sock = None;
        }
    }

    impl Drop for FakeSupplicant {
        fn drop(&mut self) {
            self.close();
        }
    }

    // --- WifiParsing --------------------------------------------------------------------------------
    #[test]
    fn psk_matches_the_ieee_test_vector() {
        assert_eq!(
            psk_hex("password", b"IEEE"),
            "f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e"
        );
    }

    #[test]
    fn ssid_escapes() {
        assert_eq!(
            decode_ssid(br#"caf\xc3\xa9 \"x\" \\"#),
            "café \"x\" \\".as_bytes()
        );
    }

    #[test]
    fn scan_results() {
        let text = "bssid / frequency / signal level / flags / ssid\n\
                    aa:bb:cc:dd:ee:01\t2412\t-70\t[WPA2-PSK-CCMP][ESS]\tExampleNet\n\
                    aa:bb:cc:dd:ee:02\t5180\t-50\t[WPA2-PSK-CCMP][ESS]\tExampleNet\n\
                    aa:bb:cc:dd:ee:03\t2437\t-60\t[ESS]\tGuestNet\n\
                    aa:bb:cc:dd:ee:04\t2437\t-40\t[WPA2-EAP-CCMP][ESS]\tWorkNet\n\
                    aa:bb:cc:dd:ee:05\t2437\t-30\t[ESS]\t\\x00\\x00\n";
        let res: Vec<_> = parse_scan(text)
            .into_iter()
            .map(|r| (r.ssid, r.signal, r.security))
            .collect();
        assert_eq!(
            res,
            vec![
                (b"WorkNet".to_vec(), -40, "eap"),
                (b"ExampleNet".to_vec(), -50, "psk"),
                (b"GuestNet".to_vec(), -60, "open")
            ]
        );
    }

    fn ssids(n: &[Saved]) -> Vec<Vec<u8>> {
        n.iter().map(|n| n.ssid.clone().unwrap()).collect()
    }

    #[test]
    fn store_put_replace_priority_remove() {
        let d = tmpdir();
        let path = d.join("w.conf");
        fs::write(
            &path,
            "# kept\nnetwork={\n\tssid=\"OldNet\"\n\tpsk=\"geheim123\"\n}\n",
        )
        .unwrap();
        let s = Store::new(&path);
        s.put(b"NewNet", "WPA-PSK", Some(&"ab".repeat(32)), None, false)
            .unwrap();
        s.put(b"NewNet", "WPA-PSK", Some(&"cd".repeat(32)), None, false)
            .unwrap();
        assert_eq!(
            ssids(&s.networks().unwrap()),
            vec![b"OldNet".to_vec(), b"NewNet".to_vec()]
        );
        assert!(s.set_priority(b"NewNet", 5).unwrap());
        s.put(b"NewNet", "WPA-PSK", Some(&"ef".repeat(32)), None, false)
            .unwrap(); // a new password keeps the priority
        let text = fs::read_to_string(&path).unwrap();
        assert!(text.contains("# kept"));
        assert!(text.contains(&format!("ssid={}", hex(b"NewNet"))));
        assert!(text.contains(&format!("psk={}", "ef".repeat(32))));
        assert!(!text.contains(&"cd".repeat(32)));
        assert!(text.contains("priority=5"));
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert!(s.remove(b"OldNet").unwrap());
        assert!(!s.remove(b"OldNet").unwrap());
        assert_eq!(ssids(&s.networks().unwrap()), vec![b"NewNet".to_vec()]);
        let names: Vec<_> = fs::read_dir(&d)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        assert_eq!(names, vec![std::ffi::OsString::from("w.conf")]); // no temporary files left
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn escape_and_value_edge_cases_match_python() {
        let cases: [(&[u8], &[u8]); 7] = [
            (br"\x-0a", b"\x00a"),
            (br"\x 3a", b"\x03a"),
            (br"\x+fa", b"\x0fa"),
            (br"\x-1a", br"\x-1a"),
            (br"\xg0", br"\xg0"),
            (br"\x0", br"\x0"),
            (br"\x_1z", br"\x_1z"),
        ];
        for (input, want) in cases {
            assert_eq!(decode_ssid(input), want);
        }
        assert_eq!(value_ssid("ab cd"), b"\xab\xcd");
        assert_eq!(value_ssid("P\"a\\x41\""), b"aA");
        assert_eq!(value_ssid("\""), b"\"");
    }

    #[test]
    fn show_ssid_and_errors_read_like_python() {
        assert_eq!(show_ssid(b""), "(verborgen)");
        assert_eq!(show_ssid(b"a\x01b\xffc\xe2\x80\xaed"), "a?b\u{fffd}c?d");
        let d = tmpdir();
        let mut c = Ctrl::new(d.join("nope"), &d);
        assert_eq!(
            c.request("SCAN", 1.0).unwrap_err().to_string(),
            "[Errno 2] No such file or directory"
        );
        fs::remove_dir_all(&d).unwrap();
    }

    // --- WifiManager --------------------------------------------------------------------------------
    #[derive(Default)]
    struct St {
        wpa_state: String,
        ssid: Option<String>,
        ip: String,
        next_id: u32,
        add_fails: bool,
    }

    struct T {
        d: PathBuf,
        state: Arc<Mutex<St>>,
        sup: FakeSupplicant,
        store: Store,
        start_files: Arc<Mutex<Vec<Vec<Saved>>>>,
        w: Wifi,
    }

    fn setup() -> T {
        let d = tmpdir();
        let state = Arc::new(Mutex::new(St {
            wpa_state: "SCANNING".into(),
            next_id: 3,
            ..St::default()
        }));
        let st = state.clone();
        let replies: Replies = Arc::new(Mutex::new(Box::new(move |cmd: &str| {
            let mut st = st.lock().unwrap();
            match cmd {
                "PING" => "PONG".into(),
                "STATUS" => {
                    let out = format!("wpa_state={}", st.wpa_state);
                    match &st.ssid {
                        Some(s) => format!("{out}\nssid={s}"),
                        None => out,
                    }
                }
                "LIST_NETWORKS" => {
                    "network id / ssid / bssid / flags\n0\tExampleNet\tany\t[CURRENT]".into()
                }
                "ADD_NETWORK" if st.add_fails => "FAIL".into(),
                "ADD_NETWORK" => {
                    st.next_id += 1;
                    st.next_id.to_string()
                }
                _ => "OK".into(),
            }
        })));
        let sup = FakeSupplicant::new(&d, replies);
        let store = Store::new(d.join("saved.conf"));
        let start_files: Arc<Mutex<Vec<Vec<Saved>>>> = Arc::default();
        let mut w = Wifi::new(
            &sup.path,
            store.clone(),
            Store::new(d.join("r.conf")),
            d.join("off"),
            &d,
        );
        let ip_state = state.clone();
        w.ip = Box::new(move || ip_state.lock().unwrap().ip.clone());
        let (files, s) = (start_files.clone(), store.clone());
        w.start_file = Box::new(move || {
            files.lock().unwrap().push(s.networks().unwrap());
            true
        });
        let mut t = T {
            d,
            state,
            sup,
            store,
            start_files,
            w,
        };
        assert!(t.w.monitor_fd().is_some());
        t
    }

    impl Drop for T {
        fn drop(&mut self) {
            self.w.req.close();
            self.w.mon.close();
            self.sup.close();
            let _ = fs::remove_dir_all(&self.d);
        }
    }

    impl T {
        fn pump(&mut self, mut until: impl FnMut(&Wifi) -> bool, timeout: f64) -> bool {
            let end = monotonic() + timeout;
            while monotonic() < end {
                self.w.events();
                if until(&self.w) {
                    return true;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            false
        }

        fn cmds(&self) -> Vec<String> {
            self.sup.commands()
        }

        fn has(&self, cmd: &str) -> bool {
            self.cmds().iter().any(|c| c == cmd)
        }

        fn set(&self, wpa_state: Option<&str>, ssid: Option<&str>, ip: Option<&str>) {
            let mut st = self.state.lock().unwrap();
            if let Some(v) = wpa_state {
                st.wpa_state = v.into();
            }
            if let Some(v) = ssid {
                st.ssid = Some(v.into());
            }
            if let Some(v) = ip {
                st.ip = v.into();
            }
        }

        fn leases(&mut self) -> Arc<Mutex<Vec<i32>>> {
            let leases: Arc<Mutex<Vec<i32>>> = Arc::default();
            let l = leases.clone();
            self.w.new_lease = Box::new(move || {
                l.lock().unwrap().push(1);
                true
            });
            leases
        }

        fn off(&self) -> PathBuf {
            self.d.join("off")
        }

        fn saved_ssids(&self) -> Vec<Vec<u8>> {
            ssids(&self.store.networks().unwrap())
        }
    }

    fn in_dhcp(w: &Wifi) -> bool {
        w.attempt.as_ref().is_some_and(|a| a.phase == Phase::Dhcp)
    }

    #[test]
    fn wrong_password_is_reported_and_not_saved() {
        let mut t = setup();
        assert!(t
            .w
            .connect(b"NeighborNet", "psk", Some("verkeerd1"), false)
            .unwrap());
        let sets: Vec<_> = t
            .cmds()
            .into_iter()
            .filter(|c| c.starts_with("SET_NETWORK 4"))
            .collect();
        assert!(sets.contains(&format!("SET_NETWORK 4 ssid {}", hex(b"NeighborNet"))));
        assert!(sets.contains(&format!(
            "SET_NETWORK 4 psk {}",
            psk_hex("verkeerd1", b"NeighborNet")
        )));
        assert!(t.has("SELECT_NETWORK 4"));
        t.sup.event("CTRL-EVENT-SSID-TEMP-DISABLED id=4 ssid=\"NeighborNet\" auth_failures=1 duration=10 reason=WRONG_KEY");
        assert!(t.pump(|w| w.attempt.is_none(), 3.0));
        assert_eq!(t.w.message, "Verkeerd wachtwoord");
        assert!(t.has("REMOVE_NETWORK 4"));
        assert!(t.has("ENABLE_NETWORK all"));
        assert_eq!(t.store.networks().unwrap(), vec![]);
        assert!(t.start_files.lock().unwrap().is_empty());
    }

    #[test]
    fn success_saves_the_network_and_waits_for_an_address() {
        let mut t = setup();
        t.w.connect(b"NeighborNet", "psk", Some("goedgoed"), false)
            .unwrap();
        t.set(Some("COMPLETED"), Some("NeighborNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:bb completed [id=4]");
        assert!(t.pump(in_dhcp, 3.0));
        assert_eq!(t.saved_ssids(), vec![b"NeighborNet".to_vec()]);
        assert_eq!(
            ssids(t.start_files.lock().unwrap().last().unwrap()),
            vec![b"NeighborNet".to_vec()]
        ); // a respawn starts with it
        t.w.attempt.as_mut().unwrap().since -= 30.0;
        t.w.tick();
        assert_eq!(t.w.message, "Verbonden, maar geen IP-adres gekregen (DHCP)");
        assert_eq!(t.saved_ssids(), vec![b"NeighborNet".to_vec()]); // still saved
    }

    #[test]
    fn address_ends_the_attempt() {
        let mut t = setup();
        t.w.connect(b"GuestNet", "open", None, false).unwrap();
        assert!(t.has("SET_NETWORK 4 key_mgmt NONE"));
        t.set(Some("COMPLETED"), Some("GuestNet"), Some("10.0.0.7"));
        t.w.tick();
        t.w.attempt.as_mut().unwrap().since -= 10.0;
        t.w.tick();
        assert!(t.w.attempt.is_none());
        assert_eq!(t.w.message, "Verbonden met GuestNet, adres 10.0.0.7");
    }

    #[test]
    fn network_not_found() {
        let mut t = setup();
        t.w.connect(b"ExampleNet", "psk", None, false).unwrap(); // saved: no password needed
        assert!(!t.has("ADD_NETWORK"));
        assert!(t.has("SELECT_NETWORK 0"));
        t.sup.event("CTRL-EVENT-NETWORK-NOT-FOUND");
        t.sup.event("CTRL-EVENT-NETWORK-NOT-FOUND");
        assert!(t.pump(|w| w.attempt.is_none(), 3.0));
        assert_eq!(t.w.message, "ExampleNet niet gevonden");
        assert!(!t.has("REMOVE_NETWORK 0")); // a saved network stays
    }

    #[test]
    fn unsupported_and_short_passwords() {
        let mut t = setup();
        assert!(!t.w.connect(b"X", "sae", Some("12345678"), false).unwrap());
        assert!(t.w.message.contains("WPA3"));
        assert!(!t.w.connect(b"X", "psk", Some("kort"), false).unwrap());
        assert!(!t.has("ADD_NETWORK"));
    }

    #[test]
    fn off_and_on() {
        // the flag first, then the start file, then wpa_supplicant reloads it (also what a respawn reads)
        let mut t = setup();
        t.w.set_enabled(false);
        assert!(t.off().exists());
        assert_eq!(
            (
                t.start_files.lock().unwrap().len(),
                t.cmds().last().unwrap().as_str()
            ),
            (1, "RECONFIGURE")
        );
        t.w.set_enabled(true);
        assert!(!t.off().exists());
        assert_eq!(
            (
                t.start_files.lock().unwrap().len(),
                t.cmds().last().unwrap().as_str()
            ),
            (2, "RECONFIGURE")
        );
        let leases = t.leases();
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:bb completed [id=0]");
        assert!(t.pump(|_| !leases.lock().unwrap().is_empty(), 3.0)); // back on: a fresh lease
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:bb completed [id=0]");
        t.pump(|_| false, 0.3);
        assert_eq!(*leases.lock().unwrap(), vec![1]); // only once
    }

    #[test]
    fn switching_off_is_undone_when_it_cannot_take_effect() {
        let mut t = setup();
        t.w.start_file = Box::new(|| false);
        assert!(!t.w.set_enabled(false));
        assert!(!t.off().exists()); // the switch still says on
        assert!(!t.has("RECONFIGURE"));
        assert!(t.w.message.contains("mislukt"));
        t.w.start_file = Box::new(|| true);
        {
            let mut g = t.sup.replies.lock().unwrap();
            let mut orig = std::mem::replace(&mut *g, Box::new(|_: &str| String::new()));
            *g = Box::new(move |cmd: &str| {
                if cmd == "RECONFIGURE" {
                    "FAIL".into()
                } else {
                    orig(cmd)
                }
            });
        }
        assert!(!t.w.set_enabled(false));
        assert!(!t.off().exists());
        assert!(t.w.enabled());
    }

    #[test]
    fn unsupported_network_while_off_leaves_wifi_off() {
        let mut t = setup();
        fs::write(t.off(), "off\n").unwrap();
        assert!(!t.w.connect(b"X", "eap", None, false).unwrap());
        assert!(!t.w.connect(b"X", "psk", Some("kort"), true).unwrap());
        assert_eq!(t.w.message, "Wachtwoord moet 8 tot 63 tekens zijn");
        assert!(!t.w.enabled());
        assert!(!t.has("RECONFIGURE"));
    }

    #[test]
    fn connect_while_off_stops_when_switching_on_fails() {
        let mut t = setup();
        fs::write(t.off(), "off\n").unwrap();
        t.w.start_file = Box::new(|| false);
        assert!(!t
            .w
            .connect(b"NeighborNet", "psk", Some("goedgoed"), false)
            .unwrap());
        assert!(!t.has("ADD_NETWORK"));
        assert!(t.w.message.contains("mislukt"));
        assert!(!t.w.enabled());
    }

    #[test]
    fn switching_off_ends_a_running_attempt_even_when_it_fails() {
        let mut t = setup();
        t.w.connect(b"NeighborNet", "psk", Some("goedgoed"), false)
            .unwrap();
        t.w.start_file = Box::new(|| false);
        assert!(!t.w.set_enabled(false));
        assert!(t.has("REMOVE_NETWORK 4"));
        assert_eq!(
            t.cmds()
                .iter()
                .filter(|c| *c == "ENABLE_NETWORK all")
                .count(),
            1
        );
        assert!(t.w.attempt.is_none());
    }

    #[test]
    fn prefer_leaves_networks_from_the_rootfs_alone() {
        let mut t = setup();
        assert!(!t.w.prefer(b"ExampleNet").unwrap()); // running, but not in the userdata file
        assert!(!t
            .cmds()
            .iter()
            .any(|c| c.starts_with("SET_NETWORK 0 priority")));
    }

    #[test]
    fn forget_removes_running_and_saved() {
        let mut t = setup();
        t.store
            .put(
                b"ExampleNet",
                "WPA-PSK",
                Some(&"ab".repeat(32)),
                None,
                false,
            )
            .unwrap();
        assert!(t.w.forget(b"ExampleNet").unwrap());
        assert!(t.has("REMOVE_NETWORK 0"));
        assert_eq!(t.store.networks().unwrap(), vec![]);
        assert_eq!(*t.start_files.lock().unwrap(), vec![Vec::<Saved>::new()]);
    }

    #[test]
    fn forget_keeps_the_running_network_when_the_file_cannot_be_written() {
        let mut t = setup();
        t.store
            .put(
                b"ExampleNet",
                "WPA-PSK",
                Some(&"ab".repeat(32)),
                None,
                false,
            )
            .unwrap();
        fs::set_permissions(&t.d, fs::Permissions::from_mode(0o500)).unwrap(); // no new file next to it
        if fs::write(t.d.join("probe"), "").is_ok() {
            // root ignores the directory mode (tests run as root on the phone): nothing to test here
            let _ = fs::remove_file(t.d.join("probe"));
            fs::set_permissions(&t.d, fs::Permissions::from_mode(0o700)).unwrap();
            eprintln!("skipped: directory permissions do not apply to this user");
            return;
        }
        let r = t.w.forget(b"ExampleNet");
        fs::set_permissions(&t.d, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(!r.unwrap());
        assert!(!t.has("REMOVE_NETWORK 0"));
        assert!(t.w.message.contains("vergeten mislukt"));
    }

    #[test]
    fn any_switch_to_another_network_gets_a_new_lease() {
        let mut t = setup();
        let leases = t.leases();
        t.set(Some("COMPLETED"), Some("ExampleNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:01 completed [id=0]");
        assert!(t.pump(|w| w.last_ssid.as_deref() == Some(b"ExampleNet"), 3.0));
        assert!(leases.lock().unwrap().is_empty()); // the same network again: keep the lease
        t.w.forget(b"ExampleNet").unwrap(); // wpa_supplicant then picks another saved network by itself
        t.set(None, Some("WorkNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:02 completed [id=1]");
        assert!(t.pump(|_| !leases.lock().unwrap().is_empty(), 3.0));
        assert_eq!(t.w.last_ssid.as_deref(), Some(&b"WorkNet"[..]));
    }

    #[test]
    fn a_network_joined_before_attach_is_the_baseline() {
        let mut t = setup();
        t.w.mon.close(); // start over: STATUS said SCANNING, then ExampleNet connected before ATTACH
        (t.w.attached, t.w.last_ssid, t.w.next_attach) = (false, None, 0.0);
        let leases = t.leases();
        t.set(Some("COMPLETED"), Some("ExampleNet"), None);
        assert!(t.w.monitor_fd().is_some());
        assert_eq!(
            (t.w.last_ssid.clone(), leases.lock().unwrap().clone()),
            (Some(b"ExampleNet".to_vec()), vec![])
        );
        t.set(None, Some("WorkNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:02 completed [id=1]");
        assert!(t.pump(|_| !leases.lock().unwrap().is_empty(), 3.0));
    }

    #[test]
    fn a_new_network_from_the_ui_gets_one_new_lease() {
        let mut t = setup();
        let leases = t.leases();
        t.set(Some("COMPLETED"), Some("ExampleNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:01 completed [id=0]");
        assert!(t.pump(|w| w.last_ssid.as_deref() == Some(b"ExampleNet"), 3.0));
        t.w.connect(b"NeighborNet", "psk", Some("goedgoed"), false)
            .unwrap();
        t.set(None, Some("NeighborNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:03 completed [id=4]");
        assert!(t.pump(in_dhcp, 3.0));
        assert_eq!(*leases.lock().unwrap(), vec![1]);
    }

    #[test]
    fn a_restarted_wpa_supplicant_is_attached_again() {
        let mut t = setup();
        let leases = t.leases();
        t.set(Some("COMPLETED"), Some("ExampleNet"), None);
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:01 completed [id=0]");
        assert!(t.pump(|w| w.last_ssid.as_deref() == Some(b"ExampleNet"), 3.0));
        t.sup.close(); // wpa_supplicant dies; on Linux the event socket would just go quiet
        fs::remove_file(&t.sup.path).unwrap();
        t.w.next_ping = 0.0;
        t.w.tick(); // the periodic PING notices it, also with the screen off
        assert!(!t.w.attached);
        t.set(None, Some("WorkNet"), None); // it came back on another network while nobody listened
        t.sup = FakeSupplicant::new(&t.d, t.sup.replies.clone());
        assert!(t.w.monitor_fd().is_some());
        assert!(t.w.attached);
        assert_eq!(*leases.lock().unwrap(), vec![1]);
        assert_eq!(t.w.last_ssid.as_deref(), Some(&b"WorkNet"[..]));
        t.sup.event("CTRL-EVENT-SCAN-RESULTS");
        t.w.scanning = true;
        assert!(t.pump(|w| !w.scanning, 3.0)); // events arrive again
    }

    #[test]
    fn a_second_choice_removes_the_first_unconfirmed_network() {
        let mut t = setup();
        t.w.connect(b"A-net", "psk", Some("wachtwoordA"), false)
            .unwrap();
        t.w.connect(b"B-net", "psk", Some("wachtwoordB"), false)
            .unwrap();
        let c = t.cmds();
        let pos = |s: &str| c.iter().position(|x| x == s).unwrap();
        assert!(pos("REMOVE_NETWORK 4") < pos("SELECT_NETWORK 5"));
        assert_eq!(t.w.attempt.as_ref().unwrap().id, 5);
    }

    #[test]
    fn add_network_failure_is_a_message_not_a_crash() {
        let mut t = setup();
        t.state.lock().unwrap().add_fails = true;
        assert!(!t
            .w
            .connect(b"NeighborNet", "psk", Some("goedgoed"), false)
            .unwrap());
        assert_eq!(t.w.message, "Instellen mislukt (FAIL)");
        assert!(t.w.attempt.is_none());
    }

    #[test]
    fn a_stale_start_file_is_reported() {
        let mut t = setup();
        t.store
            .put(
                b"ExampleNet",
                "WPA-PSK",
                Some(&"ab".repeat(32)),
                None,
                false,
            )
            .unwrap();
        t.w.start_file = Box::new(|| false);
        assert!(t.w.forget(b"ExampleNet").unwrap());
        assert!(t.w.message.contains("startbestand niet bijgewerkt"));
        t.w.connect(b"NeighborNet", "psk", Some("goedgoed"), false)
            .unwrap();
        t.set(Some("COMPLETED"), Some("NeighborNet"), Some("10.0.0.9"));
        t.sup
            .event("CTRL-EVENT-CONNECTED - Connection to aa:03 completed [id=4]");
        assert!(t.pump(in_dhcp, 3.0));
        t.w.attempt.as_mut().unwrap().since -= 10.0;
        t.w.tick();
        assert!(!t.w.message.contains("(niet opgeslagen)")); // it is on userdata: back after a reboot
        assert!(t.w.message.contains("startbestand niet bijgewerkt"));
        assert_eq!(t.saved_ssids(), vec![b"NeighborNet".to_vec()]);
    }

    #[test]
    fn scan_results_event() {
        let mut t = setup();
        t.w.scan().unwrap();
        assert!(t.w.scanning);
        t.sup.event("CTRL-EVENT-SCAN-RESULTS");
        assert!(t.pump(|w| !w.scanning, 3.0));
        assert!(t.has("SCAN_RESULTS"));
    }
}
