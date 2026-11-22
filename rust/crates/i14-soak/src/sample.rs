//! Port of scripts/i14-soak-sample: one JSON line per minute, from root's crontab.
//!
//! No device writes, and /proc/mtk_battery_cmd/current_cmd is never opened (reading
//! it disables charging). Every input path, the output/lock/crontab paths and the
//! probe command names are overridable with the same I14_* variables as the shell
//! script; an empty variable means "use the default", as `${VAR:-default}` does.
//! The JSON line is byte-identical to the shell's (awk/sed/printf emulated below),
//! except that "services" keeps a fixed key order and "services" precedes "dns"
//! (the shell prints both in completion order of parallel jobs).

use std::ffi::{OsStr, OsString};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};
use std::os::unix::process::ExitStatusExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

pub const SERVICES: [&str; 7] = [
    "i14-power",
    "i14-cpufreq",
    "i14-ui",
    "unbound",
    "i14-wifi",
    "i14-netwatch",
    "sshd",
];
pub const MARKER: &[u8] = b"# i14-soak-sample";
pub const USAGE: &str = "usage: i14-soak-sample [install|uninstall]";

/// All paths and commands, resolved from the environment like the shell's defaults.
#[derive(Clone, Debug)]
pub struct Config {
    pub log: OsString,
    pub lock: OsString,
    pub cron: OsString,
    pub max: OsString,
    pub battery: OsString,
    pub boot: OsString,
    pub uptime: OsString,
    pub loadavg: OsString,
    pub meminfo: OsString,
    pub fg_current: OsString,
    pub fg_percent: OsString,
    pub zcv: OsString,
    pub bcct: OsString,
    pub charge_status: OsString,
    pub readback: OsString,
    pub charger_type: OsString,
    pub usb: OsString,
    pub ac: OsString,
    pub backlight: OsString,
    pub cpu_freq: OsString,
    pub cpu_cap: OsString,
    pub cpu_fix: OsString,
    pub battery_log: OsString,
    pub rc: OsString,
    pub busybox: OsString,
    pub ip: OsString,
    pub timeout: OsString,
}

fn cat(a: &OsStr, b: &str) -> OsString {
    let mut v = a.as_bytes().to_vec();
    v.extend_from_slice(b.as_bytes());
    OsString::from_vec(v)
}

impl Config {
    /// `env` returns a variable's value; empty counts as unset.
    pub fn from(env: impl Fn(&str) -> Option<OsString>) -> Config {
        let var =
            |name: &str, default: OsString| env(name).filter(|v| !v.is_empty()).unwrap_or(default);
        let root = var("I14_SOAK_ROOT", OsString::new());
        let r = |p: &str| cat(&root, p);
        let battery = var("I14_BATTERY", r("/sys/class/power_supply/battery"));
        Config {
            log: var(
                "I14_SOAK_LOG",
                "/mnt/data/media/0/i14-linux/soak/samples.jsonl".into(),
            ),
            lock: var("I14_SOAK_LOCK", "/run/i14-soak-sample.lock".into()),
            cron: var("I14_SOAK_CRONTAB", "/etc/crontabs/root".into()),
            max: var("I14_SOAK_MAX_BYTES", "20971520".into()),
            boot: var("I14_SOAK_BOOT_ID", r("/proc/sys/kernel/random/boot_id")),
            uptime: var("I14_SOAK_UPTIME", r("/proc/uptime")),
            loadavg: var("I14_SOAK_LOADAVG", r("/proc/loadavg")),
            meminfo: var("I14_SOAK_MEMINFO", r("/proc/meminfo")),
            fg_current: var(
                "I14_FG_CURRENT",
                r("/sys/devices/platform/battery_meter/FG_Current"),
            ),
            fg_percent: var(
                "I14_FG_PERCENT",
                r("/sys/devices/platform/battery_meter/FG_g_fg_dbg_percentage_fg"),
            ),
            zcv: var(
                "I14_FG_ZCV",
                r("/sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_zcv"),
            ),
            bcct: var("I14_BCCT_CONFIG", r("/proc/driver/thermal/clbcct")),
            charge_status: var("I14_CHARGE_STATUS", r("/run/i14-charge-status.json")),
            readback: var(
                "I14_CURRENT_READBACK",
                r("/proc/mtk_battery_cmd/battery_cmd"),
            ),
            charger_type: var(
                "I14_CHARGER_TYPE",
                r("/sys/devices/platform/battery/Charger_Type"),
            ),
            usb: var("I14_USB_ONLINE", r("/sys/class/power_supply/usb/online")),
            ac: var("I14_AC_ONLINE", r("/sys/class/power_supply/ac/online")),
            backlight: var(
                "I14_BACKLIGHT",
                r("/sys/class/leds/lcd-backlight/brightness"),
            ),
            cpu_freq: var(
                "I14_SOAK_CPU_FREQ",
                r("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"),
            ),
            cpu_cap: var(
                "I14_SOAK_CPU_CAP",
                r("/proc/cpufreq/cpufreq_limited_max_freq_by_user"),
            ),
            cpu_fix: var(
                "I14_SOAK_CPU_FIX",
                r("/proc/cpufreq/cpufreq_fix_freq_in_es"),
            ),
            battery_log: var("I14_SOAK_BATTERY_LOG", r("/var/log/i14-battery.log")),
            rc: var("I14_SOAK_RC_SERVICE", "rc-service".into()),
            busybox: var("I14_SOAK_BUSYBOX", "busybox".into()),
            ip: var("I14_SOAK_IP", "ip".into()),
            timeout: var("I14_SOAK_TIMEOUT", "timeout".into()),
            battery,
        }
    }

    pub fn from_env() -> Config {
        Config::from(|k| std::env::var_os(k))
    }
}

// ------------------------------------------------------------ shell emulation

/// What `$(...)` makes of command output: NUL bytes dropped, trailing newlines removed.
pub fn subst(mut v: Vec<u8>) -> Vec<u8> {
    v.retain(|&b| b != 0);
    while v.last() == Some(&b'\n') {
        v.pop();
    }
    v
}

/// The bytes `cat` writes before any error.
pub fn raw_file(path: &OsStr) -> Vec<u8> {
    let mut out = Vec::new();
    if let Ok(mut f) = File::open(path) {
        let mut buf = [0u8; 8192];
        loop {
            match f.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => out.extend_from_slice(&buf[..n]),
            }
        }
    }
    out
}

/// `$(cat "$1" 2>/dev/null)`
pub fn read_file(path: &OsStr) -> Vec<u8> {
    subst(raw_file(path))
}

/// The C `%.10g` conversion.
pub fn fmt_g10(x: f64) -> String {
    if x.is_infinite() {
        return if x > 0.0 { "inf" } else { "-inf" }.into();
    }
    if x.is_nan() {
        return "nan".into();
    }
    let sci = format!("{x:.9e}");
    let (mant, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let strip = |s: &str| -> String {
        if s.contains('.') {
            s.trim_end_matches('0').trim_end_matches('.').to_string()
        } else {
            s.to_string()
        }
    };
    if !(-4..10).contains(&exp) {
        format!(
            "{}e{}{:02}",
            strip(mant),
            if exp < 0 { '-' } else { '+' },
            exp.abs()
        )
    } else {
        strip(&format!("{x:.prec$}", prec = (9 - exp) as usize))
    }
}

/// `number()`: the first line if it is a plain decimal, as awk `%.10g`, else null.
pub fn number(v: &[u8]) -> Vec<u8> {
    let line = v.split(|&b| b == b'\n').next().unwrap_or(&[]);
    let body = match line.first() {
        Some(b'+' | b'-') => &line[1..],
        _ => line,
    };
    let (int, frac) = match body.iter().position(|&b| b == b'.') {
        Some(p) => (&body[..p], Some(&body[p + 1..])),
        None => (body, None),
    };
    let digits = |s: &[u8]| !s.is_empty() && s.iter().all(u8::is_ascii_digit);
    if !digits(int) || frac.is_some_and(|f| !digits(f)) {
        return b"null".to_vec();
    }
    let x: f64 = std::str::from_utf8(line)
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(0.0);
    fmt_g10(x + 0.0).into_bytes() // `$0+0`: -0 + 0 is +0
}

/// `string()`: a JSON string as the awk escaper writes it (bytewise), or null.
pub fn string(v: &[u8]) -> Vec<u8> {
    if v.is_empty() {
        return b"null".to_vec();
    }
    let mut out = vec![b'"'];
    for &c in v {
        match c {
            b'\\' => out.extend_from_slice(b"\\\\"),
            b'"' => out.extend_from_slice(b"\\\""),
            b'\n' => out.extend_from_slice(b"\\n"),
            1..=31 => out.extend_from_slice(format!("\\u{c:04x}").as_bytes()),
            _ => out.push(c),
        }
    }
    out.push(b'"');
    out
}

/// awk records (newline-separated, last one may lack a newline), each cut at a
/// NUL byte as awk's C strings do.
fn records(data: &[u8]) -> Vec<&[u8]> {
    let mut recs: Vec<&[u8]> = data.split(|&b| b == b'\n').collect();
    if data.is_empty() || data.ends_with(b"\n") {
        recs.pop();
    }
    recs.into_iter()
        .map(|r| r.split(|&b| b == 0).next().unwrap_or(&[]))
        .collect()
}

/// awk fields with the default FS (blanks).
fn fields(rec: &[u8]) -> Vec<&[u8]> {
    rec.split(|&b| b == b' ' || b == b'\t')
        .filter(|f| !f.is_empty())
        .collect()
}

/// `awk '{print $1; exit}' file`
pub fn first_field(data: &[u8]) -> Vec<u8> {
    match records(data).first() {
        Some(r) => fields(r).first().map_or(Vec::new(), |f| f.to_vec()),
        None => Vec::new(),
    }
}

/// `awk '{print $1}' file`, through `$(...)`.
pub fn all_first_fields(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for r in records(data) {
        if let Some(f) = fields(r).first() {
            out.extend_from_slice(f);
        }
        out.push(b'\n');
    }
    subst(out)
}

/// `awk '$1=="MemAvailable:" {print $2; exit}' file`
pub fn mem_available(data: &[u8]) -> Vec<u8> {
    for r in records(data) {
        let f = fields(r);
        if f.first() == Some(&&b"MemAvailable:"[..]) {
            return f.get(1).map_or(Vec::new(), |x| x.to_vec());
        }
    }
    Vec::new()
}

/// `awk '{for(i=1;i<NF;i++) if($i=="inet") {print $(i+1); exit}}'`
pub fn inet_address(data: &[u8]) -> Vec<u8> {
    for r in records(data) {
        let f = fields(r);
        for i in 0..f.len().saturating_sub(1) {
            if f[i] == b"inet" {
                return f[i + 1].to_vec();
            }
        }
    }
    Vec::new()
}

fn is_posix_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | 0x0b | 0x0c | b'\r')
}

/// The sed extraction for `name=<n>` (or `name<ws>=<ws><n>` with `spaced`):
/// the first line that matches, taking that line's last match (greedy `.*`).
pub fn extract(readback: &[u8], name: &[u8], spaced: bool) -> Vec<u8> {
    for line in readback.split(|&b| b == b'\n') {
        let mut found = None;
        let mut start = 0;
        while let Some(p) = find(&line[start..], name) {
            let at = start + p;
            if let Some(v) = capture(&line[at + name.len()..], spaced) {
                found = Some(v);
            }
            start = at + 1;
        }
        if let Some(v) = found {
            return v;
        }
    }
    Vec::new()
}

/// `[[:space:]]*=[[:space:]]*` (when spaced), then `\([+-]*[0-9][0-9]*\)`.
fn capture(rest: &[u8], spaced: bool) -> Option<Vec<u8>> {
    let mut i = 0;
    let skip = |i: &mut usize| {
        while *i < rest.len() && is_posix_space(rest[*i]) {
            *i += 1;
        }
    };
    if spaced {
        skip(&mut i);
        if rest.get(i) != Some(&b'=') {
            return None;
        }
        i += 1;
        skip(&mut i);
    }
    let from = i;
    while i < rest.len() && (rest[i] == b'+' || rest[i] == b'-') {
        i += 1;
    }
    let digits = i;
    while i < rest.len() && rest[i].is_ascii_digit() {
        i += 1;
    }
    (i > digits).then(|| rest[from..i].to_vec())
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

/// strtod's decimal prefix (awk's string-to-number for arithmetic).
fn awk_atof(s: &[u8]) -> (f64, usize, bool) {
    let mut i = 0;
    while i < s.len() && is_posix_space(s[i]) {
        i += 1;
    }
    let start = i;
    if i < s.len() && (s[i] == b'+' || s[i] == b'-') {
        i += 1;
    }
    let mut digits = 0;
    while i < s.len() && s[i].is_ascii_digit() {
        i += 1;
        digits += 1;
    }
    if i < s.len() && s[i] == b'.' {
        i += 1;
        while i < s.len() && s[i].is_ascii_digit() {
            i += 1;
            digits += 1;
        }
    }
    if digits == 0 {
        return (0.0, 0, false);
    }
    if i < s.len() && (s[i] == b'e' || s[i] == b'E') {
        let mut j = i + 1;
        if j < s.len() && (s[j] == b'+' || s[j] == b'-') {
            j += 1;
        }
        if j < s.len() && s[j].is_ascii_digit() {
            while j < s.len() && s[j].is_ascii_digit() {
                j += 1;
            }
            i = j;
        }
    }
    let v = std::str::from_utf8(&s[start..i])
        .ok()
        .and_then(|t| t.parse().ok())
        .unwrap_or(0.0);
    (v, i, true)
}

/// Whether awk treats a `-v` value as a number in comparisons.
fn awk_strnum(s: &[u8]) -> bool {
    let (v, end, ok) = awk_atof(s);
    ok && v.is_finite()
        && s[end..]
            .iter()
            .all(|&b| matches!(b, b' ' | b'\t' | b'\n' | b'\r'))
}

/// awk's processing of escapes in a `-v name=value` assignment.
fn awk_v(s: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < s.len() {
        if s[i] != b'\\' || i + 1 >= s.len() {
            out.push(s[i]);
            i += 1;
            continue;
        }
        i += 1;
        let c = s[i];
        i += 1;
        match c {
            b'n' => out.push(b'\n'),
            b't' => out.push(b'\t'),
            b'r' => out.push(b'\r'),
            b'f' => out.push(0x0c),
            b'b' => out.push(0x08),
            b'v' => out.push(0x0b),
            b'a' => out.push(0x07),
            b'\\' => out.push(b'\\'),
            b'"' => out.push(b'"'),
            b'/' => out.push(b'/'),
            b'0'..=b'7' => {
                let mut n = (c - b'0') as u32;
                for _ in 0..2 {
                    if i < s.len() && (b'0'..=b'7').contains(&s[i]) {
                        n = n * 8 + (s[i] - b'0') as u32;
                        i += 1;
                    }
                }
                out.push(n as u8);
            }
            _ => {
                out.push(b'\\');
                out.push(c);
            }
        }
    }
    out
}

/// The DNS probe's milliseconds from the two uptime readings (awk -v a b).
pub fn dns_ms(start: &[u8], end: &[u8]) -> Vec<u8> {
    let (a, b) = (awk_v(start), awk_v(end));
    if a.is_empty() || b.is_empty() {
        return b"null".to_vec();
    }
    let (fa, fb) = (awk_atof(&a).0, awk_atof(&b).0);
    let ge = if awk_strnum(&a) && awk_strnum(&b) {
        fb >= fa
    } else {
        b >= a
    };
    if !ge {
        return b"null".to_vec();
    }
    let ms = (fb - fa) * 1000.0;
    if ms.is_nan() {
        b"nan".to_vec()
    } else {
        format!("{ms:.0}").into_bytes()
    }
}

/// `date -u +%Y-%m-%dT%H:%M:%SZ` for a Unix time.
pub fn iso_utc(secs: i64) -> String {
    let (y, m, d) = civil_from_days(secs.div_euclid(86400));
    let s = secs.rem_euclid(86400);
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        s / 3600,
        s / 60 % 60,
        s % 60
    )
}

// ------------------------------------------------------------------ commands

/// Exit status as `$?` reports it.
fn status_code(r: io::Result<std::process::ExitStatus>) -> i32 {
    match r {
        Ok(s) => s.code().unwrap_or_else(|| 128 + s.signal().unwrap_or(0)),
        Err(e) if e.kind() == io::ErrorKind::NotFound => 127,
        Err(_) => 126,
    }
}

fn timed(cfg: &Config, program: &OsStr, args: &[&str]) -> Command {
    timed_for(cfg, "1", program, args)
}

/// The local DNS lookup gets the external probe's 5 s: at the 299 MHz idle cap an
/// answer can take just over 1 s (1050 ms on 8 Nov), which is slow, not down.
fn timed_for(cfg: &Config, secs: &str, program: &OsStr, args: &[&str]) -> Command {
    let mut c = Command::new(&cfg.timeout);
    c.args(["-s", "KILL", secs]).arg(program).args(args);
    c
}

/// `"$TIMEOUT" -s KILL 1 "$RC" "$service" status >/dev/null 2>&1` in a background job.
fn spawn_quiet(mut c: Command) -> io::Result<std::process::Child> {
    c.stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
}

fn wait_code(child: io::Result<std::process::Child>) -> i32 {
    match child {
        Ok(mut ch) => status_code(ch.wait()),
        Err(e) => status_code(Err(e)),
    }
}

fn wlan0_ipv4(cfg: &Config) -> Vec<u8> {
    let child = timed(cfg, &cfg.ip, &["-4", "-o", "addr", "show", "dev", "wlan0"])
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn();
    let Ok(mut child) = child else {
        return Vec::new();
    };
    let mut out = Vec::new();
    if let Some(mut so) = child.stdout.take() {
        let _ = so.read_to_end(&mut out);
    }
    let _ = child.wait();
    subst(inet_address(&out))
}

/// The "services" and "dns" probes, all in one parallel round.
fn probes(cfg: &Config) -> Vec<u8> {
    let dns = std::thread::scope(|scope| {
        let dns = scope.spawn(|| {
            let start = all_first_fields(&raw_file(&cfg.uptime));
            let code = wait_code(spawn_quiet(timed_for(
                cfg,
                "5",
                &cfg.busybox,
                &["nslookup", "example.com", "127.0.0.1"],
            )));
            let end = all_first_fields(&raw_file(&cfg.uptime));
            (code == 0, dns_ms(&start, &end))
        });
        let children: Vec<_> = SERVICES
            .iter()
            .map(|s| spawn_quiet(timed(cfg, &cfg.rc, &[s, "status"])))
            .collect();
        let codes: Vec<i32> = children.into_iter().map(wait_code).collect();
        (codes, dns.join().unwrap_or((false, b"null".to_vec())))
    });
    let (codes, (ok, ms)) = dns;
    let mut out = b"\"services\":{".to_vec();
    for (i, (s, c)) in SERVICES.iter().zip(codes).enumerate() {
        if i > 0 {
            out.push(b',');
        }
        out.extend_from_slice(format!("\"{s}\":{c}").as_bytes());
    }
    out.extend_from_slice(format!("}},\"dns\":{{\"ok\":{ok},\"ms\":").as_bytes());
    out.extend_from_slice(&ms);
    out.extend_from_slice(b"},");
    out
}

fn sub_path(dir: &OsStr, name: &str) -> OsString {
    cat(dir, &format!("/{name}"))
}

/// The sample line (without the trailing newline), measured now.
pub fn measure(cfg: &Config) -> Vec<u8> {
    // Capture CPU/current before the status and DNS probes wake the CPU.
    let readback = read_file(&cfg.readback);
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let mut line = b"{".to_vec();
    let mut field = |name: &str, value: Vec<u8>| {
        line.extend_from_slice(format!("\"{name}\":").as_bytes());
        line.extend_from_slice(&value);
        line.push(b',');
    };
    let b = &cfg.battery;
    field("epoch", number(now.to_string().as_bytes()));
    field("iso", string(iso_utc(now).as_bytes()));
    field("boot_id", string(&read_file(&cfg.boot)));
    field(
        "uptime_seconds",
        number(&subst(first_field(&raw_file(&cfg.uptime)))),
    );
    field("capacity", number(&read_file(&sub_path(b, "capacity"))));
    field("status", string(&read_file(&sub_path(b, "status"))));
    field("batt_vol_mv", number(&read_file(&sub_path(b, "batt_vol"))));
    field(
        "batt_temp_deci_c",
        number(&read_file(&sub_path(b, "batt_temp"))),
    );
    field("fg_current_deci_ma", number(&read_file(&cfg.fg_current)));
    field("fg_percentage", number(&read_file(&cfg.fg_percent)));
    field("zcv_mv", number(&read_file(&cfg.zcv)));
    field("charge_status", string(&read_file(&cfg.charge_status)));
    field("bcct_state", string(&read_file(&cfg.bcct)));
    field(
        "get_usb_current_unlimited",
        number(&extract(&readback, b"get_usb_current_unlimited=", false)),
    );
    field(
        "cmd_discharging",
        number(&extract(&readback, b"cmd_discharging", true)),
    );
    field("charger_type", number(&read_file(&cfg.charger_type)));
    field("usb_online", number(&read_file(&cfg.usb)));
    field("ac_online", number(&read_file(&cfg.ac)));
    field("backlight_brightness", number(&read_file(&cfg.backlight)));
    field("scaling_cur_freq_khz", number(&read_file(&cfg.cpu_freq)));
    field(
        "cpufreq_limited_max_freq_by_user",
        number(&read_file(&cfg.cpu_cap)),
    );
    field("cpufreq_fix_freq_in_es", number(&read_file(&cfg.cpu_fix)));
    field("loadavg", string(&read_file(&cfg.loadavg)));
    field(
        "mem_available_kb",
        number(&subst(mem_available(&raw_file(&cfg.meminfo)))),
    );
    field("wlan0_ipv4", string(&wlan0_ipv4(cfg)));
    line.extend_from_slice(&probes(cfg));
    line.extend_from_slice(b"\"battery_log_last\":");
    line.extend_from_slice(&string(&subst(last_line(&raw_file(&cfg.battery_log)))));
    line.push(b'}');
    line
}

/// `tail -n 1`
pub fn last_line(data: &[u8]) -> Vec<u8> {
    let body = data.strip_suffix(b"\n").unwrap_or(data);
    match body.iter().rposition(|&b| b == b'\n') {
        Some(p) => data[p + 1..].to_vec(),
        None => data.to_vec(),
    }
}

/// `[ "$size" -ge "$MAX" ]` operand parsing (test's integer syntax).
pub fn parse_max(max: &OsStr) -> Option<i64> {
    let s = std::str::from_utf8(max.as_bytes()).ok()?;
    let t = s
        .trim_start_matches([' ', '\t'])
        .trim_end_matches([' ', '\t']);
    let digits = t.strip_prefix(['+', '-']).unwrap_or(t);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    t.parse().ok()
}

fn dirname(p: &[u8]) -> Vec<u8> {
    let mut end = p.len();
    while end > 1 && p[end - 1] == b'/' {
        end -= 1;
    }
    let p = &p[..end];
    match p.iter().rposition(|&b| b == b'/') {
        None => b".".to_vec(),
        Some(i) => {
            let mut d = &p[..i];
            while d.len() > 1 && d.ends_with(b"/") {
                d = &d[..d.len() - 1];
            }
            if d.is_empty() {
                b"/".to_vec()
            } else {
                d.to_vec()
            }
        }
    }
}

fn basename(p: &[u8]) -> Vec<u8> {
    let mut end = p.len();
    while end > 1 && p[end - 1] == b'/' {
        end -= 1;
    }
    let p = &p[..end];
    match p.iter().rposition(|&b| b == b'/') {
        Some(i) if p.len() > 1 => p[i + 1..].to_vec(),
        _ => p.to_vec(),
    }
}

fn os(v: Vec<u8>) -> OsString {
    OsString::from_vec(v)
}

fn fail(what: &str, path: &OsStr, e: &io::Error) {
    eprintln!("i14-soak-sample: {what} {}: {e}", Path::new(path).display());
}

/// Appends the sample to the log with the shell's rotation and partial-line repair.
/// Returns the exit code.
pub fn write_sample(cfg: &Config, line: &[u8]) -> i32 {
    let log = &cfg.log;
    let dir = os(dirname(log.as_bytes()));
    if let Err(e) = fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)
    {
        fail("cannot create", &dir, &e);
        return 1;
    }
    let size = match fs::metadata(log) {
        Ok(m) if m.is_file() => m.len() as i64,
        _ => 0,
    };
    match parse_max(&cfg.max) {
        Some(max) if size >= max => {
            if let Err(e) = fs::rename(log, cat(log, ".1")) {
                fail("cannot rotate", log, &e);
                return 1;
            }
        }
        Some(_) => {}
        None => eprintln!("i14-soak-sample: bad number: {}", cfg.max.to_string_lossy()),
    }
    // A power loss may leave a partial line. Preserve it, separating the next sample.
    if fs::metadata(log).is_ok_and(|m| m.len() > 0)
        && !matches!(last_byte(log), None | Some(b'\n') | Some(0))
    {
        match append(log) {
            Ok(mut f) => {
                if let Err(e) = f.write_all(b"\n") {
                    fail("cannot write", log, &e);
                    return 1;
                }
            }
            Err(e) => {
                fail("cannot create", log, &e);
                return 1;
            }
        }
    }
    let mut out = line.to_vec();
    out.push(b'\n');
    match append(log) {
        Ok(mut f) => match f.write_all(&out) {
            Ok(()) => 0,
            Err(e) => {
                fail("cannot write", log, &e);
                1
            }
        },
        Err(e) => {
            fail("cannot create", log, &e);
            2
        }
    }
}

fn append(path: &OsStr) -> io::Result<File> {
    OpenOptions::new()
        .append(true)
        .create(true)
        .mode(0o600)
        .open(path)
}

fn last_byte(path: &OsStr) -> Option<u8> {
    let mut f = File::open(path).ok()?;
    f.seek(SeekFrom::End(-1)).ok()?;
    let mut b = [0u8];
    match f.read(&mut b) {
        Ok(1) => Some(b[0]),
        _ => None,
    }
}

/// One sample under the lock. Returns the exit code.
pub fn run_sample(cfg: &Config) -> i32 {
    let lock = match OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(&cfg.lock)
    {
        Ok(f) => f,
        Err(e) => {
            fail("cannot create", &cfg.lock, &e);
            return 2;
        }
    };
    if lock.try_lock().is_err() {
        return 0; // another sample is running
    }
    let line = measure(cfg);
    let code = write_sample(cfg, &line);
    drop(lock);
    code
}

/// The crontab with every marked line removed and `line` appended if given,
/// as `awk '!/# i14-soak-sample$/ {print} END {if (line != "") print line}'`.
pub fn edit_crontab(old: &[u8], line: Option<&[u8]>) -> Vec<u8> {
    let mut recs: Vec<&[u8]> = old.split(|&b| b == b'\n').collect();
    if old.is_empty() || old.ends_with(b"\n") {
        recs.pop();
    }
    let mut out = Vec::new();
    for rec in recs {
        if !rec.ends_with(MARKER) {
            out.extend_from_slice(rec);
            out.push(b'\n');
        }
    }
    if let Some(l) = line {
        out.extend_from_slice(l);
        out.push(b'\n');
    }
    out
}

/// The persistent path cron should run: the physical directory of argv[0] plus
/// its name, as `$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")`.
pub fn self_path(argv0: &OsStr) -> io::Result<OsString> {
    let a = argv0.as_bytes();
    if !a.contains(&b'/') {
        return std::env::current_exe().map(PathBuf::into_os_string);
    }
    let dir = fs::canonicalize(os(dirname(a)))?;
    let mut p = dir.into_os_string().into_vec();
    p.push(b'/');
    p.extend(basename(a));
    Ok(os(p))
}

pub fn path_ok(p: &[u8]) -> bool {
    p.iter()
        .all(|&b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'/' | b'-'))
}

/// `install` (with `Some(argv0)`) or `uninstall`. Returns the exit code.
pub fn edit_cron(cfg: &Config, install_from: Option<&OsStr>) -> i32 {
    let mut line = None;
    if let Some(argv0) = install_from {
        let path = match self_path(argv0) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("i14-soak-sample: cannot resolve own path: {e}");
                return 1;
            }
        };
        // Cron treats % specially, even inside quotes. Use a simple persistent path.
        if !path_ok(path.as_bytes()) {
            eprintln!("use a script path without spaces or special characters");
            return 2;
        }
        let mut l = b"* * * * * '".to_vec();
        l.extend_from_slice(path.as_bytes());
        l.extend_from_slice(b"' # i14-soak-sample");
        line = Some(l);
    }
    let cron = &cfg.cron;
    let dir = os(dirname(cron.as_bytes()));
    if let Err(e) = fs::create_dir_all(&dir) {
        fail("cannot create", &dir, &e);
        return 1;
    }
    if !Path::new(cron).exists() {
        if let Err(e) = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(cron)
        {
            fail("cannot create", cron, &e);
            return 1;
        }
    }
    let tmp = cat(cron, &format!(".i14-soak.{}", std::process::id()));
    let result = (|| -> io::Result<()> {
        fs::copy(cron, &tmp)?;
        if let Ok(m) = fs::metadata(cron) {
            let _ = std::os::unix::fs::chown(&tmp, Some(m.uid()), Some(m.gid()));
        }
        let old = fs::read(cron)?;
        fs::write(&tmp, edit_crontab(&old, line.as_deref()))?;
        fs::rename(&tmp, cron)
    })();
    match result {
        Ok(()) => 0,
        Err(e) => {
            let _ = fs::remove_file(&tmp);
            fail("cannot update", cron, &e);
            1
        }
    }
}

/// The whole command line. Returns the exit code.
pub fn main_with(args: &[OsString], cfg: &Config) -> i32 {
    let argv0 = args.first().cloned().unwrap_or_default();
    match args.get(1).map(|a| a.as_bytes()) {
        Some(b"install") => edit_cron(cfg, Some(&argv0)),
        Some(b"uninstall") => edit_cron(cfg, None),
        None | Some(b"") => run_sample(cfg),
        Some(_) => {
            eprintln!("{USAGE}");
            2
        }
    }
}

/// Days since 1970-01-01 to (year, month, day), proleptic Gregorian.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn g10_matches_printf() {
        for (x, s) in [
            (79.0, "79"),
            (1234.56, "1234.56"),
            (-0.0, "-0"),
            (12345678901234.0, "1.23456789e+13"),
            (1794000000.0, "1794000000"),
            (0.0001, "0.0001"),
            (0.00001, "1e-05"),
            (1.23456789012, "1.23456789"),
            (f64::INFINITY, "inf"),
            (9999999999.5, "1e+10"),
        ] {
            assert_eq!(fmt_g10(x), s, "{x}");
        }
    }

    #[test]
    fn number_and_string_follow_awk() {
        assert_eq!(number(b"79"), b"79");
        assert_eq!(number(b"007"), b"7");
        assert_eq!(number(b"+5"), b"5");
        assert_eq!(number(b"-0"), b"0");
        assert_eq!(number(b"-0.0"), b"0");
        assert_eq!(number(b"79\ngarbage"), b"79");
        assert_eq!(number(b"\n79"), b"null");
        assert_eq!(number(b" 79"), b"null");
        assert_eq!(number(b"1."), b"null");
        assert_eq!(number(b"+-5"), b"null");
        assert_eq!(number(b""), b"null");
        assert_eq!(string(b""), b"null");
        assert_eq!(
            string(b"a\"b\\c\nd\te\x01\x7f"),
            b"\"a\\\"b\\\\c\\nd\\u0009e\\u0001\x7f\""
        );
    }

    #[test]
    fn readback_extraction_follows_sed() {
        let rb = b"=> get_usb_current_unlimited=1,\ncmd_discharging = -1";
        assert_eq!(extract(rb, b"get_usb_current_unlimited=", false), b"1");
        assert_eq!(extract(rb, b"cmd_discharging", true), b"-1");
        assert_eq!(
            extract(
                b"cmd_discharging=1 cmd_discharging=2\ncmd_discharging=3",
                b"cmd_discharging",
                true
            ),
            b"2"
        );
        assert_eq!(extract(b"cmd_discharging=x", b"cmd_discharging", true), b"");
        assert_eq!(
            extract(b"cmd_discharging=+-5", b"cmd_discharging", true),
            b"+-5"
        );
    }

    #[test]
    fn crontab_edit_keeps_other_lines() {
        assert_eq!(
            edit_crontab(b"a\nx # i14-soak-sample\nb", Some(b"L")),
            b"a\nb\nL\n"
        );
        assert_eq!(edit_crontab(b"", None), b"");
        assert_eq!(edit_crontab(b"\n\n", None), b"\n\n");
        assert_eq!(
            edit_crontab(b"x # i14-soak-sample\r\n", None),
            b"x # i14-soak-sample\r\n"
        );
    }

    #[test]
    fn dns_ms_follows_awk() {
        assert_eq!(dns_ms(b"1234.56", b"1234.56"), b"0");
        assert_eq!(dns_ms(b"1234.56", b"1234.57"), b"10");
        assert_eq!(dns_ms(b"9.5", b"10.2"), b"700");
        assert_eq!(dns_ms(b"10", b"9"), b"null");
        assert_eq!(dns_ms(b"", b"9"), b"null");
        assert_eq!(dns_ms(b"abc", b"abc"), b"0");
    }

    #[test]
    fn paths_like_dirname() {
        assert_eq!(dirname(b"samples.jsonl"), b".");
        assert_eq!(dirname(b"/a/b/c"), b"/a/b");
        assert_eq!(dirname(b"/c"), b"/");
        assert_eq!(basename(b"/a/b/c"), b"c");
        assert_eq!(last_line(b"a\nb\n"), b"b\n");
        assert_eq!(last_line(b"a\nb"), b"b");
        assert_eq!(last_line(b"a\n\n"), b"\n");
    }
}
