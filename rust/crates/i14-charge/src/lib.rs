//! Power-off charging (KPOC) in the initramfs: a port of `charge_wait` and
//! `pwrkey_watch` from `initramfs/i14charge` (BusyBox ash), with the same
//! decisions, timings, log lines and return codes, but no `cat`, `sleep`, `dd`
//! or `od` processes. Everything else in `i14charge` and `/i14init` stays shell.
//!
//! Only sleeping and logging go through [`Host`], so the loop runs on a host
//! with a fake clock; the battery, uptime and flag files are real files whose
//! paths can be overridden exactly like the shell's (`I14_PS`, `I14_UPTIME`,
//! `I14_PWRKEY`).
#![forbid(unsafe_code)]

use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::path::{Path, PathBuf};

/// The thresholds of `i14charge`. `/i14init` changes AB_PCT (no automatic
/// start), AB_MIN_S and AB_HOLD_S (`i14.autoboot=test`) as shell variables, so
/// the binary takes them from the environment under the same names.
///
/// AB_MAX_S has no counterpart in the shell `charge_wait`: the whole of charging
/// mode ends in rescue mode after this long (default 8 h, see [`charge_wait`]).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Config {
    pub ab_min_s: i64,
    pub ab_hold_s: i64,
    pub ab_max_s: i64,
    pub ab_mv: i64,
    pub ab_pct: i64,
    pub ab_temp: i64,
    pub key_mv: i64,
    pub nopower_s: i64,
    pub badread_s: i64,
    pub step_s: i64,
    pub log_s: i64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            ab_min_s: 300,
            ab_hold_s: 120,
            ab_max_s: 8 * 3600,
            ab_mv: 3800,
            ab_pct: 15,
            ab_temp: 450,
            key_mv: 3600,
            nopower_s: 30,
            badread_s: 60,
            step_s: 5,
            log_s: 60,
        }
    }
}

impl Config {
    /// Defaults, overridden by the shell names (AB_PCT=101, ...). An empty value
    /// keeps the default; anything that is not a whole number is an error.
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Result<Self, String> {
        let mut c = Self::default();
        for (name, slot) in [
            ("AB_MIN_S", &mut c.ab_min_s),
            ("AB_HOLD_S", &mut c.ab_hold_s),
            ("AB_MAX_S", &mut c.ab_max_s),
            ("AB_MV", &mut c.ab_mv),
            ("AB_PCT", &mut c.ab_pct),
            ("AB_TEMP", &mut c.ab_temp),
            ("KEY_MV", &mut c.key_mv),
            ("NOPOWER_S", &mut c.nopower_s),
            ("BADREAD_S", &mut c.badread_s),
            ("STEP_S", &mut c.step_s),
            ("LOG_S", &mut c.log_s),
        ] {
            match get(name) {
                Some(v) if !v.is_empty() => {
                    *slot = to_int(&v).ok_or_else(|| format!("{name}={v} is not a number"))?
                }
                _ => {}
            }
        }
        Ok(c)
    }
}

/// Every file the shell touches, with its override.
#[derive(Clone, Debug)]
pub struct Paths {
    /// `$PS`, /sys/class/power_supply (I14_PS)
    pub ps: PathBuf,
    /// /proc/uptime (I14_UPTIME)
    pub uptime: PathBuf,
    /// `$PWRKEY`, /i14/pwrkey (I14_PWRKEY): the "power key pressed" flag
    pub pwrkey: PathBuf,
    /// /sys/class/input (I14_INPUT; fixed in the shell)
    pub input: PathBuf,
    /// /dev (I14_DEV; fixed in the shell)
    pub dev: PathBuf,
    /// /dev/kmsg (I14_KMSG; log() in /i14init)
    pub kmsg: PathBuf,
    /// /i14/i14init.log, the log in RAM (I14_RAMLOG)
    pub ramlog: PathBuf,
    /// `$DIR` of /i14init, the userdata directory with the second log (I14_DIR)
    pub dir: PathBuf,
}

impl Paths {
    /// `${I14_PS:-/sys/class/power_supply}` and so on: unset or empty means the default.
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> Self {
        let p = |name: &str, default: &str| {
            PathBuf::from(
                get(name)
                    .filter(|v| !v.is_empty())
                    .unwrap_or_else(|| default.into()),
            )
        };
        Self {
            ps: p("I14_PS", "/sys/class/power_supply"),
            uptime: p("I14_UPTIME", "/proc/uptime"),
            pwrkey: p("I14_PWRKEY", "/i14/pwrkey"),
            input: p("I14_INPUT", "/sys/class/input"),
            dev: p("I14_DEV", "/dev"),
            kmsg: p("I14_KMSG", "/dev/kmsg"),
            ramlog: p("I14_RAMLOG", "/i14/i14init.log"),
            dir: p("I14_DIR", "/i14/data/media/0/i14-linux"),
        }
    }
}

/// What the loop needs from the outside world besides files.
pub trait Host {
    /// `sleep SECS`
    fn sleep(&mut self, secs: u64);
    /// `log MESSAGE` of /i14init
    fn log(&mut self, msg: &str);
}

/// The result of [`charge_wait`]: the return code and the shell variables
/// `/i14init` reads afterwards (`$why`, and `$cap`/`$vol` of the last sample).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Outcome {
    /// 0 start Linux, 1 rescue mode, 2 power off
    pub code: i32,
    pub why: String,
    pub cap: String,
    pub vol: String,
}

impl Outcome {
    /// Four plain lines for `charge_wait_rs` (no `eval`): `why=`, `cap=`, `vol=`,
    /// then `end`, so a cut-off output is recognisably incomplete. Line breaks inside
    /// a value become spaces, so every value stays on its own line.
    pub fn to_lines(&self) -> String {
        let one = |s: &str| s.replace(['\n', '\r'], " ");
        format!(
            "why={}\ncap={}\nvol={}\nend\n",
            one(&self.why),
            one(&self.cap),
            one(&self.vol)
        )
    }
}

/// `$(cat FILE)`: the content without trailing newlines; "" if unreadable.
pub fn read_value(path: &Path) -> String {
    match fs::read(path) {
        Ok(b) => {
            let mut s = String::from_utf8_lossy(&b).into_owned();
            while s.ends_with('\n') {
                s.pop();
            }
            s
        }
        Err(_) => String::new(),
    }
}

/// `now`: `read -r u _ <$UPTIME; echo ${u%.*}` (whole seconds). An unreadable
/// or odd uptime counts as 0, as an empty or non-numeric value does in `$(( ))`.
pub fn now(uptime: &Path) -> i64 {
    let s = fs::read(uptime).unwrap_or_default();
    let s = String::from_utf8_lossy(&s);
    let first = s.split('\n').next().unwrap_or("");
    let u = first
        .split([' ', '\t'])
        .find(|f| !f.is_empty())
        .unwrap_or("");
    let whole = match u.rfind('.') {
        Some(i) => &u[..i],
        None => u,
    };
    to_int(whole).unwrap_or(0)
}

/// The clock of [`charge_wait`]: the whole seconds of /proc/uptime as [`now`] reads
/// them, but an unreadable file or a value that is not `SECONDS[.FRACTION]` (digits
/// only, no sign, within 64 bits) is an error instead of 0, so the time limits cannot
/// fail open.
pub fn clock(uptime: &Path) -> Result<i64, String> {
    let s = fs::read(uptime).map_err(|_| "uptime unreadable (no file)".to_string())?;
    let s = String::from_utf8_lossy(&s);
    let first = s.split('\n').next().unwrap_or("");
    let u = first
        .split([' ', '\t'])
        .find(|f| !f.is_empty())
        .unwrap_or("");
    let (whole, frac) = u.split_once('.').unwrap_or((u, "0"));
    let digits = |v: &str| !v.is_empty() && v.bytes().all(|b| b.is_ascii_digit());
    match whole.parse::<i64>() {
        Ok(t) if digits(whole) && digits(frac) => Ok(t),
        _ => Err("uptime unreadable (not a number)".to_string()),
    }
}

/// `num VALUE...`: each is a whole number, optionally with one leading `-`.
pub fn num(values: &[&str]) -> bool {
    values.iter().all(|v| {
        let d = v.strip_prefix('-').unwrap_or(v);
        !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())
    })
}

/// A whole number as `[ ... -ge ... ]` reads it (strtoll base 10, 64 bit); None
/// where test(1) fails ("out of range", "bad number"), which makes the test false.
fn to_int(v: &str) -> Option<i64> {
    if !num(&[v]) {
        return None;
    }
    v.parse().ok()
}

/// `[ "$v" -ge N ]`
fn ge(v: &str, n: i64) -> bool {
    to_int(v).is_some_and(|x| x >= n)
}

/// `[ "$v" -le N ]`
fn le(v: &str, n: i64) -> bool {
    to_int(v).is_some_and(|x| x <= n)
}

/// `charge_wait`: sample the battery every STEP_S seconds until Linux should
/// start (0), rescue mode (1, values unreadable) or power off (2, no charger).
///
/// Beyond the shell: once AB_MAX_S seconds of uptime have passed since the start
/// without any of those, it ends in rescue mode too (1, "charge time limit"), so a
/// battery that never meets the start gates cannot keep the phone in charging mode
/// forever with nobody there to press a key; rescue mode keeps the USB shell up for
/// the Mac. The check comes last in each sample, so every other decision stays as
/// in the shell. The default, 8 h, is well beyond a healthy charge to the gates
/// (15 %, 3800 mV, held 120 s after 300 s) and still ends a stuck charge the same day.
///
/// Both that limit and BADREAD_S are measured on /proc/uptime, so the clock must not
/// fail open: where the shell counts an unreadable or odd uptime as 0 (which stops
/// both clocks and kept charging mode up for good), this ends in rescue mode (1,
/// "uptime unreadable (...)") at the start or at the sample that reads it, before any
/// other decision of that sample; so does an uptime that goes backwards (1, "uptime
/// went backwards (OLD -> NEW)"). An unchanged uptime is fine.
pub fn charge_wait(cfg: &Config, paths: &Paths, host: &mut impl Host) -> Outcome {
    let bat = paths.ps.join("battery");
    let t0 = match clock(&paths.uptime) {
        Ok(t) => t,
        Err(why) => return clock_failed(why, &bat),
    };
    let mut last = t0;
    let mut since: Option<i64> = None;
    let (mut good, mut nopower, mut bad) = (0i64, 0i64, 0i64);
    let mut logged = -cfg.log_s;
    let mut after_bad = false;
    loop {
        // Deviation (documented): the shell skips every sleep while $PWRKEY exists, and
        // only a readable sample removes it, so a key press during unreadable values makes
        // it spin through cat at full speed until they read again or BADREAD_S passes.
        // One second's pause instead gives the same decisions without the spin.
        if after_bad && paths.pwrkey.exists() {
            host.sleep(1);
        }
        let mut i = 0;
        while i < cfg.step_s && !paths.pwrkey.exists() {
            host.sleep(1);
            i += 1;
        }
        let t = match clock(&paths.uptime) {
            Ok(t) if t >= last => t,
            Ok(t) => return clock_failed(format!("uptime went backwards ({last} -> {t})"), &bat),
            Err(why) => return clock_failed(why, &bat),
        };
        let dt = t - last;
        last = t;
        let cap = read_value(&bat.join("capacity"));
        let vol = read_value(&bat.join("batt_vol"));
        let temp = read_value(&bat.join("batt_temp"));
        let st = read_value(&bat.join("status"));
        let usb = read_value(&paths.ps.join("usb/online"));
        let ac = read_value(&paths.ps.join("ac/online"));
        if !num(&[&cap, &vol, &temp, &usb, &ac]) || st.is_empty() {
            bad += dt;
            since = None;
            good = 0;
            if bad < cfg.badread_s {
                after_bad = true;
                if t - t0 >= cfg.ab_max_s {
                    return time_limit(cap, vol);
                }
                continue;
            }
            let why = format!("battery values unreadable for {bad}s");
            return Outcome {
                code: 1,
                why,
                cap,
                vol,
            };
        }
        after_bad = false;
        bad = 0;
        if t - logged >= cfg.log_s {
            host.log(&format!(
                "charging: capacity={cap}% voltage={vol}mV temp={temp} status={st} usb={usb} ac={ac} ok_for={good}s"
            ));
            logged = t;
        }
        if usb == "1" || ac == "1" {
            nopower = 0;
        } else {
            nopower += dt;
            if nopower >= cfg.nopower_s {
                let why = format!("no charger for {nopower}s");
                return Outcome {
                    code: 2,
                    why,
                    cap,
                    vol,
                };
            }
        }
        if paths.pwrkey.exists() {
            let _ = fs::remove_file(&paths.pwrkey);
            if ge(&vol, cfg.key_mv) {
                return Outcome {
                    code: 0,
                    why: "power key".into(),
                    cap,
                    vol,
                };
            }
            host.log(&format!(
                "charger: power key, but {vol}mV is below {}mV: charging on",
                cfg.key_mv
            ));
        }
        let ok = st == "Charging" || st == "Full";
        if nopower == 0
            && ok
            && t - t0 >= cfg.ab_min_s
            && ge(&vol, cfg.ab_mv)
            && ge(&cap, cfg.ab_pct)
            && ge(&temp, 0)
            && le(&temp, cfg.ab_temp)
        {
            good = t - *since.get_or_insert(t);
        } else {
            since = None;
            good = 0;
        }
        if good >= cfg.ab_hold_s {
            return Outcome {
                code: 0,
                why: "charged".into(),
                cap,
                vol,
            };
        }
        if t - t0 >= cfg.ab_max_s {
            return time_limit(cap, vol);
        }
    }
}

/// No usable clock: rescue mode, with the battery values as they are now.
fn clock_failed(why: String, bat: &Path) -> Outcome {
    Outcome {
        code: 1,
        why,
        cap: read_value(&bat.join("capacity")),
        vol: read_value(&bat.join("batt_vol")),
    }
}

/// AB_MAX_S reached: rescue mode, with the values of the last sample.
fn time_limit(cap: String, vol: String) -> Outcome {
    Outcome {
        code: 1,
        why: "charge time limit".into(),
        cap,
        vol,
    }
}

fn is_input_device(path: &Path, accept_fifo: bool) -> bool {
    fs::metadata(path).is_ok_and(|m| {
        let t = m.file_type();
        t.is_char_device() || (accept_fifo && t.is_fifo())
    })
}

/// The device node `pwrkey_watch` reads: for every `$INPUT/event*` (in glob
/// order) named mtk-kpd, the last of /dev/input/eventN and /dev/eventN that is a
/// character device. `accept_fifo` is for host tests only.
pub fn find_pwrkey(paths: &Paths, accept_fifo: bool) -> Option<PathBuf> {
    let mut names: Vec<Vec<u8>> = fs::read_dir(&paths.input)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.file_name().into_encoded_bytes())
        .filter(|n| n.starts_with(b"event"))
        .collect();
    names.sort();
    let mut ev = None;
    for n in names {
        let n = String::from_utf8_lossy(&n).into_owned();
        if read_value(&paths.input.join(&n).join("device/name")) != "mtk-kpd" {
            continue;
        }
        for cand in [paths.dev.join("input").join(&n), paths.dev.join(&n)] {
            if is_input_device(&cand, accept_fifo) {
                ev = Some(cand);
            }
        }
    }
    ev
}

/// The tail of one `struct input_event` (24 bytes on arm64) for EV_KEY (1),
/// KEY_POWER (116), value 1 (pressed): the shell's `*0100740001000000`.
const PRESS: [u8; 8] = [0x01, 0x00, 0x74, 0x00, 0x01, 0x00, 0x00, 0x00];

/// Whether one read from the device ends like a power key press (the shell
/// matches the hex of whatever `dd bs=24 count=1` returned against that tail).
pub fn is_press(read: &[u8]) -> bool {
    read.ends_with(&PRESS)
}

/// `pwrkey_watch`: create `$PWRKEY` on every power key press. Returns 0 only
/// when there is no mtk-kpd device ("charger: no power key input"); otherwise
/// runs until killed. An empty read or error waits a second and opens the
/// device again, as the shell's `''` branch does.
pub fn pwrkey_watch(paths: &Paths, host: &mut impl Host, accept_fifo: bool) -> i32 {
    let Some(ev) = find_pwrkey(paths, accept_fifo) else {
        host.log("charger: no power key input");
        return 0;
    };
    let mut dev: Option<File> = None;
    loop {
        if dev.is_none() {
            dev = File::open(&ev).ok();
        }
        let mut buf = [0u8; 24];
        let n = match dev.as_mut().map(|f| f.read(&mut buf)) {
            Some(Ok(n)) => n,
            Some(Err(e)) if e.kind() == ErrorKind::Interrupted => continue,
            _ => 0,
        };
        if n == 0 {
            dev = None;
            host.sleep(1);
            continue;
        }
        if is_press(&buf[..n]) {
            let _ = File::create(&paths.pwrkey); // : >"$PWRKEY"
        }
    }
}

/// `date '+%F %T'` in UTC: the initramfs has no TZ and no zoneinfo, so BusyBox
/// date (musl localtime) prints UTC there.
pub fn utc_stamp(unix_secs: i64) -> String {
    let days = unix_secs.div_euclid(86_400);
    let secs = unix_secs.rem_euclid(86_400);
    // Days since 1970-01-01 to a civil date (H. Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02} {:02}:{:02}:{:02}",
        secs / 3600,
        secs / 60 % 60,
        secs % 60
    )
}

/// `log` of /i14init: `<6>i14init: MSG` to /dev/kmsg (opened as `>`, one write),
/// `MSG` appended to the RAM log, and `DATE MSG` appended to `$DIR/i14init.log`
/// when `$DIR` is a directory. Every failure is ignored, as there.
pub fn write_log(paths: &Paths, date: &str, msg: &str) {
    if let Ok(mut k) = File::create(&paths.kmsg) {
        let _ = k.write_all(format!("<6>i14init: {msg}\n").as_bytes());
    }
    append(&paths.ramlog, &format!("{msg}\n"));
    if paths.dir.is_dir() {
        append(&paths.dir.join("i14init.log"), &format!("{date} {msg}\n"));
    }
}

fn append(path: &Path, text: &str) {
    if let Ok(mut f) = OpenOptions::new().append(true).create(true).open(path) {
        let _ = f.write_all(text.as_bytes());
    }
}

/// The phone: real sleeps, the three logs of /i14init, the date in UTC.
pub struct RealHost {
    pub paths: Paths,
}

impl Host for RealHost {
    fn sleep(&mut self, secs: u64) {
        std::thread::sleep(std::time::Duration::from_secs(secs));
    }

    fn log(&mut self, msg: &str) {
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        write_log(&self.paths, &utc_stamp(secs), msg);
    }
}

#[cfg(any(test, feature = "test-clock"))]
pub mod fake {
    //! The host tests' clock: `sleep N` adds N to the fake uptime (written as
    //! `T.00 1.00`, like the sleep stub of tests/test_i14_autoboot.py) and then
    //! replays the timeline entries for the new time, in order.
    use super::*;

    /// One timeline entry: `T set REL VALUE`, `T rm REL` or `T touch REL`, with REL
    /// relative to the test directory (the shell hook does the same at `$t = T`).
    #[derive(Clone, Debug, PartialEq, Eq)]
    pub enum Op {
        Set(String, String),
        Rm(String),
        Touch(String),
    }

    pub fn parse_timeline(text: &str) -> Result<Vec<(i64, Op)>, String> {
        let mut out = Vec::new();
        for line in text.lines().filter(|l| !l.trim().is_empty()) {
            let mut it = line.splitn(4, ' ');
            let t = it.next().and_then(|t| t.parse().ok());
            let (op, rel) = (it.next(), it.next());
            let op = match (t, op, rel) {
                (Some(_), Some("set"), Some(r)) => {
                    Op::Set(r.into(), it.next().unwrap_or("").into())
                }
                (Some(_), Some("rm"), Some(r)) => Op::Rm(r.into()),
                (Some(_), Some("touch"), Some(r)) => Op::Touch(r.into()),
                _ => return Err(format!("bad timeline line: {line}")),
            };
            out.push((t.unwrap_or(0), op));
        }
        Ok(out)
    }

    pub fn apply(root: &Path, op: &Op) {
        match op {
            Op::Set(r, v) => {
                let _ = fs::write(root.join(r), format!("{v}\n"));
            }
            Op::Rm(r) => {
                let _ = fs::remove_file(root.join(r));
            }
            Op::Touch(r) => {
                let _ = File::create(root.join(r));
            }
        }
    }

    /// Fake time with a timeline; logs like [`RealHost`] but with a fixed date.
    pub struct FakeHost {
        pub paths: Paths,
        pub root: PathBuf,
        pub timeline: Vec<(i64, Op)>,
        pub date: String,
    }

    impl FakeHost {
        pub fn advance(uptime: &Path, secs: u64) -> i64 {
            let t = now(uptime) + secs as i64;
            let _ = fs::write(uptime, format!("{t}.00 1.00\n"));
            t
        }
    }

    impl Host for FakeHost {
        fn sleep(&mut self, secs: u64) {
            let t = Self::advance(&self.paths.uptime, secs);
            for (at, op) in &self.timeline {
                if *at == t {
                    apply(&self.root, op);
                }
            }
        }

        fn log(&mut self, msg: &str) {
            write_log(&self.paths, &self.date, msg);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn num_matches_the_shell() {
        for ok in ["0", "20", "-50", "-0", "007", "99999999999999999999"] {
            assert!(num(&[ok]), "{ok}");
        }
        for bad in [
            "", "-", "--5", "+5", "3.9V", " 20", "20 ", "1 2", "2\n0", "abc",
        ] {
            assert!(!num(&[bad]), "{bad:?}");
        }
        assert!(!num(&["1", "", "2"]));
    }

    #[test]
    fn test_comparisons_fail_like_test_out_of_range() {
        assert!(ge("007", 7));
        assert!(ge("-0", 0));
        assert!(!ge("99999999999999999999", 0)); // test: out of range -> false
        assert!(le("-99999999999999999", 450));
    }

    #[test]
    fn now_takes_the_whole_seconds() {
        let d = std::env::temp_dir().join(format!("i14-charge-now-{}", std::process::id()));
        fs::write(&d, "1234.56 99.00\n").unwrap();
        assert_eq!(now(&d), 1234);
        fs::write(&d, "77\n").unwrap();
        assert_eq!(now(&d), 77);
        fs::write(&d, "x.5 1\n").unwrap();
        assert_eq!(now(&d), 0);
        fs::remove_file(&d).unwrap();
        assert_eq!(now(&d), 0);
    }

    #[test]
    fn clock_refuses_what_now_counts_as_0() {
        let d = std::env::temp_dir().join(format!("i14-charge-clock-{}", std::process::id()));
        for (v, want) in [
            ("1234.56 99.00\n", 1234),
            ("77\n", 77),
            ("0.00 0.00\n", 0),
            ("  9.5", 9),
        ] {
            fs::write(&d, v).unwrap();
            assert_eq!(clock(&d), Ok(want), "{v:?}");
        }
        for v in [
            "",
            "\n",
            "x.5 1\n",
            "-5.00 1\n",
            "+5.00 1\n",
            "5. 1\n",
            ".5 1\n",
            "5.x 1\n",
            "1.2.3 1\n",
            "99999999999999999999.00 1\n",
        ] {
            fs::write(&d, v).unwrap();
            assert_eq!(
                clock(&d),
                Err("uptime unreadable (not a number)".into()),
                "{v:?}"
            );
        }
        fs::remove_file(&d).unwrap();
        assert_eq!(clock(&d), Err("uptime unreadable (no file)".into()));
    }

    #[test]
    fn read_value_strips_only_trailing_newlines() {
        let d = std::env::temp_dir().join(format!("i14-charge-rv-{}", std::process::id()));
        fs::write(&d, " 20 \n\n").unwrap();
        assert_eq!(read_value(&d), " 20 ");
        fs::write(&d, "\n20").unwrap();
        assert_eq!(read_value(&d), "\n20");
        fs::remove_file(&d).unwrap();
        assert_eq!(read_value(&d), "");
    }

    #[test]
    fn power_key_press_is_the_shell_pattern() {
        let mut ev = [0u8; 24];
        ev[16..24].copy_from_slice(&PRESS);
        assert!(is_press(&ev));
        ev[20] = 0; // release
        assert!(!is_press(&ev));
        ev[20] = 2; // autorepeat
        assert!(!is_press(&ev));
        ev[20] = 1;
        ev[18] = 115; // another key
        assert!(!is_press(&ev));
        ev[18] = 116;
        ev[16] = 3; // EV_ABS
        assert!(!is_press(&ev));
        assert!(is_press(&PRESS)); // a short read that ends like one, as the glob
        assert!(!is_press(&PRESS[1..]));
    }

    #[test]
    fn utc_stamp_is_date_f_t() {
        assert_eq!(utc_stamp(0), "1970-01-01 00:00:00");
        assert_eq!(utc_stamp(1_762_516_800), "2025-11-07 12:00:00");
        assert_eq!(utc_stamp(951_782_400), "2000-02-29 00:00:00");
        assert_eq!(utc_stamp(1_798_761_599), "2026-12-31 23:59:59");
    }

    #[test]
    fn lines_keep_one_value_per_line() {
        let o = Outcome {
            code: 0,
            why: "a\nb'c".into(),
            cap: "1\r2".into(),
            vol: String::new(),
        };
        assert_eq!(o.to_lines(), "why=a b'c\ncap=1 2\nvol=\nend\n");
    }

    #[test]
    fn config_from_env_uses_the_shell_names() {
        let c = Config::from_env(|k| match k {
            "AB_PCT" => Some("101".into()),
            "AB_MIN_S" => Some("30".into()),
            "AB_HOLD_S" => Some("".into()),
            _ => None,
        })
        .unwrap();
        assert_eq!((c.ab_pct, c.ab_min_s, c.ab_hold_s), (101, 30, 120));
        assert_eq!(c.ab_max_s, 8 * 3600);
        let c = Config::from_env(|k| (k == "AB_MAX_S").then(|| "600".into())).unwrap();
        assert_eq!(c.ab_max_s, 600);
        assert!(Config::from_env(|k| (k == "STEP_S").then(|| "5s".into())).is_err());
    }

    #[test]
    fn timeline_parses() {
        let t = fake::parse_timeline(
            "350 set ps/battery/batt_vol 3700\n12 rm x\n9 touch pwrkey\n5 set a\n",
        )
        .unwrap();
        assert_eq!(
            t[0],
            (
                350,
                fake::Op::Set("ps/battery/batt_vol".into(), "3700".into())
            )
        );
        assert_eq!(t[1], (12, fake::Op::Rm("x".into())));
        assert_eq!(t[2], (9, fake::Op::Touch("pwrkey".into())));
        assert_eq!(t[3], (5, fake::Op::Set("a".into(), "".into())));
        assert!(fake::parse_timeline("x set a b").is_err());
    }
}
