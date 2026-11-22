//! Stock 3.18 BCCT charge limit and low-battery guard, owned by i14-power.
//!
//! Never READ current_cmd: that callback disables charging! battery_cmd is the readback.
//! 0 releases charging to the stock driver; it does not replace its temperature/current limits.

use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use i14_config::charge_status;
use i14_config::Config;
use serde_json::{Number, Value};

use crate::bcct::{self, Control, Writer};
use crate::py::{self, Error, Json};

pub const BATTERY: &str = "/sys/class/power_supply/battery";
// With a charger attached the stock driver holds capacity at 100 after a full charge (battery_common.c
// "recharging"), and resets its coulomb-counter percentage to 100 whenever charging resumes from that state.
// The band therefore uses the driver's open-circuit voltage estimate (battery voltage + current x resistance).
pub const GAUGE: &str = "/sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_zcv";
// The cached current, ZCV and CAR are updated together by fgauge_algo_run().
pub const GAUGE_ACTIVITY: &str = "/sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_current";
/// (percent, open-circuit mV) from the stock kernel's own profile, table T2 (generic; an approximation).
/// 15%/3712 and 80%/4109 interpolate skipped T2 entries (DOD 85 and 20).
pub const OCV: [(i64, i64); 21] = [
    (0, 3400),
    (5, 3668),
    (10, 3685),
    (15, 3712),
    (20, 3736),
    (25, 3756),
    (30, 3772),
    (35, 3789),
    (40, 3808),
    (45, 3825),
    (50, 3850),
    (55, 3876),
    (60, 3924),
    (65, 3962),
    (70, 4003),
    (75, 4067),
    (80, 4109),
    (85, 4158),
    (90, 4213),
    (95, 4260),
    (100, 4324),
];
pub const TYPE: &str = "/sys/devices/platform/battery/Charger_Type";
pub const COMMAND: &str = "/proc/mtk_battery_cmd/current_cmd";
pub const READBACK: &str = "/proc/mtk_battery_cmd/battery_cmd";
pub const USB: &str = "/sys/class/power_supply/usb/online";
pub const AC: &str = "/sys/class/power_supply/ac/online";
pub const LOG: &str = "/var/log/i14-battery.log";
pub const STATUS: &str = charge_status::STATUS;
pub const BOOT_ID: &str = charge_status::BOOT_ID;
/// Drain mode (the nearly-empty chain test): a root-only tmpfs file "<boot_id> <deadline>\n", the deadline in
/// boot-relative uptime seconds (/proc/uptime's clock). While it is valid the guard keeps BCCT paused below its
/// band and reserve, so the battery drains on the cable down to the low-battery poweroff; every other
/// protection stays. A request the guard does not honour is removed, so a removed file never comes back.
pub const DRAIN_REQUEST: &str = "/run/i14-drain-request";
/// A deadline may lie at most 12 h ahead.
pub const DRAIN_MAX_S: i64 = 43200;
/// batt_vol at or below: drain mode ends (charging resumes).
pub const DRAIN_FLOOR_MV: i64 = 3330;
/// batt_temp (0.1 C) at or above: drain mode ends.
pub const DRAIN_MAX_TEMP: i64 = 400;

fn read(path: &Path) -> String {
    charge_status::read_text(&path.to_string_lossy())
}

/// Seconds since boot including suspend (/proc/uptime's clock); None when unavailable.
pub fn boottime() -> Option<f64> {
    #[cfg(any(target_os = "linux", target_os = "android"))]
    {
        let mut ts = libc::timespec {
            tv_sec: 0,
            tv_nsec: 0,
        };
        // SAFETY: clock_gettime writes one timespec into the valid pointer.
        if unsafe { libc::clock_gettime(libc::CLOCK_BOOTTIME, &mut ts) } == 0 {
            return Some(ts.tv_sec as f64 + ts.tv_nsec as f64 * 1e-9);
        }
        None
    }
    #[cfg(not(any(target_os = "linux", target_os = "android")))]
    {
        None
    }
}

fn errno(e: &std::io::Error) -> String {
    format!("unreadable (errno {})", e.raw_os_error().unwrap_or(0))
}

/// re.fullmatch(r"([0-9A-Za-z-]{1,64}) ([0-9]{1,9})\n?", text): (boot id, deadline).
fn drain_format(text: &str) -> Option<(&str, i64)> {
    let text = text.strip_suffix('\n').unwrap_or(text);
    let (boot, deadline) = text.split_once(' ')?;
    let boot_ok = (1..=64).contains(&boot.len())
        && boot.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-');
    let deadline_ok =
        (1..=9).contains(&deadline.len()) && deadline.bytes().all(|b| b.is_ascii_digit());
    if boot_ok && deadline_ok {
        Some((boot, deadline.parse().ok()?))
    } else {
        None
    }
}

/// Ok(Some(deadline)) for a request the guard honours, Err(reason) otherwise; Ok(None) = no request.
pub fn drain_request(
    path: &Path,
    boot_id: &str,
    now: Option<f64>,
    owner: u32,
) -> Result<Option<i64>, String> {
    let mut f = match OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC)
        .open(path)
    {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(errno(&e)),
    };
    let info = f.metadata().map_err(|e| errno(&e))?;
    if !info.file_type().is_file() {
        return Err("not a regular file".into());
    }
    if info.uid() != owner {
        return Err("not owned by root".into());
    }
    if info.mode() & 0o077 != 0 {
        return Err("group or other access".into());
    }
    let mut data = Vec::new();
    (&mut f)
        .take(129)
        .read_to_end(&mut data)
        .map_err(|e| errno(&e))?;
    drop(f);
    if data.len() > 128 || !data.is_ascii() {
        return Err("malformed".into());
    }
    let text = String::from_utf8(data).map_err(|_| "malformed".to_string())?;
    let (boot, deadline) = drain_format(&text).ok_or("malformed")?;
    if boot_id.is_empty() || boot != boot_id {
        return Err("other boot".into());
    }
    let Some(now) = now else {
        return Err("clock unavailable".into());
    };
    if deadline as f64 <= now {
        return Err("expired".into());
    }
    if deadline as f64 - now > DRAIN_MAX_S as f64 {
        return Err("too long".into());
    }
    Ok(Some(deadline))
}

/// Why drain mode must end now despite a valid request, or None. `readings`: everything the normal
/// validity needs except the 3700 mV reserve (gauge live, temperature, charger, status, feedback, config).
pub fn drain_hazard(readings: bool, voltage: Option<i64>, temp: Option<i64>) -> Option<String> {
    let (Some(voltage), Some(temp)) = (voltage, temp) else {
        return Some("invalid reading".into());
    };
    if !readings {
        return Some("invalid reading".into());
    }
    if voltage <= DRAIN_FLOOR_MV {
        return Some(format!(
            "voltage {voltage} mV at or below {DRAIN_FLOOR_MV} mV"
        ));
    }
    if temp >= DRAIN_MAX_TEMP {
        return Some(format!(
            "battery temperature {}.{} C",
            temp.div_euclid(10),
            temp.rem_euclid(10)
        ));
    }
    None
}

/// Open-circuit voltage to percent along OCV, rounded down; None stays None.
pub fn ocv_percent(mv: Option<i64>) -> Option<i64> {
    let mv = mv?;
    for w in OCV.windows(2) {
        let ((p1, v1), (p2, v2)) = (w[0], w[1]);
        if mv < v2 {
            return Some(0.max(p1 + ((mv - v1) * (p2 - p1)).div_euclid(v2 - v1)));
        }
    }
    Some(100)
}

fn ocv(percent: i64) -> i64 {
    OCV.iter()
        .find(|(p, _)| *p == percent)
        .expect("limit from CHARGE_LIMITS")
        .1
}

fn number(path: &Path, low: i64, high: i64) -> Option<i64> {
    let value = read(path);
    if !value.is_empty() && value.len() <= 6 && value.bytes().all(|b| b.is_ascii_digit()) {
        let value: i64 = value.parse().ok()?;
        if (low..=high).contains(&value) {
            return Some(value);
        }
    }
    None
}

/// re.fullmatch(r"-?[0-9]{1,6}", text)
fn activity_number(text: &str) -> Option<i64> {
    let digits = text.strip_prefix('-').unwrap_or(text);
    if (1..=6).contains(&digits.len()) && digits.bytes().all(|b| b.is_ascii_digit()) {
        text.parse().ok()
    } else {
        None
    }
}

/// `\b` after an ASCII digit: end of text or a non-word character.
fn boundary(rest: &str) -> bool {
    rest.chars()
        .next()
        .is_none_or(|c| !(c.is_alphanumeric() || c == '_'))
}

/// re.search(name + r"\s*=\s*(alt|...)\b") (spaces=true) or re.search(name + r"=(alt|...)\b"): group 1.
fn search<'a>(text: &str, name: &str, spaces: bool, alts: &[&'a str]) -> Option<&'a str> {
    let mut from = 0;
    while let Some(i) = text[from..].find(name) {
        let start = from + i;
        from = start + name.len();
        let mut rest = &text[from..];
        if spaces {
            rest = rest.trim_start_matches(py::is_space);
        }
        let Some(mut rest) = rest.strip_prefix('=') else {
            continue;
        };
        if spaces {
            rest = rest.trim_start_matches(py::is_space);
        }
        if let Some(alt) = alts
            .iter()
            .find(|a| rest.strip_prefix(**a).is_some_and(boundary))
        {
            return Some(alt);
        }
    }
    None
}

fn discharging<'a>(text: &str, alts: &[&'a str]) -> Option<&'a str> {
    search(text, "cmd_discharging", true, alts)
}

fn unlimited<'a>(text: &str, alts: &[&'a str]) -> Option<&'a str> {
    search(text, "get_usb_current_unlimited", false, alts)
}

/// datetime.datetime.now() as "%F %T" (local time).
fn timestamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    let t = i14_sys::local_time(secs);
    // LocalTime has no seconds; every current UTC offset is whole minutes.
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        t.year,
        t.month,
        t.day,
        t.hour,
        t.minute,
        secs.rem_euclid(60)
    )
}

/// tempfile's 8 random characters, from the clock, pid and a counter (only uniqueness matters).
fn temp_name(attempt: u32) -> String {
    const CHARS: &[u8] = b"abcdefghijklmnopqrstuvwxyz0123456789_";
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_nanos() as u64);
    let mut x = nanos
        ^ (u64::from(std::process::id()) << 32)
        ^ u64::from(attempt).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    (0..8)
        .map(|_| {
            x ^= x << 13;
            x ^= x >> 7;
            x ^= x << 17;
            CHARS[(x % CHARS.len() as u64) as usize] as char
        })
        .collect()
}

pub struct Paths {
    pub battery: PathBuf,
    pub charger_type: PathBuf,
    pub command: PathBuf,
    pub readback: PathBuf,
    pub usb: PathBuf,
    pub ac: PathBuf,
    pub settings: PathBuf,
    pub log: PathBuf,
    pub gauge: PathBuf,
    pub gauge_activity: PathBuf,
    pub status: PathBuf,
    pub boot_id: PathBuf,
    pub drain_request: PathBuf,
}

impl Default for Paths {
    fn default() -> Self {
        Paths {
            battery: BATTERY.into(),
            charger_type: TYPE.into(),
            command: COMMAND.into(),
            readback: READBACK.into(),
            usb: USB.into(),
            ac: AC.into(),
            settings: i14_config::PATH.into(),
            log: LOG.into(),
            gauge: GAUGE.into(),
            gauge_activity: GAUGE_ACTIVITY.into(),
            status: STATUS.into(),
            boot_id: BOOT_ID.into(),
            drain_request: DRAIN_REQUEST.into(),
        }
    }
}

type State = (bool, bool, i64, bool, Option<String>);

pub struct Guard {
    pub paths: Paths,
    pub control: Box<dyn Control>,
    pub clock: Box<dyn Fn() -> f64>,
    /// Writes the legacy current_cmd during the one-time migration (bcct::write_command).
    pub write: Writer,
    pub boot_id: String,
    pub activity: Option<i64>,
    activity_zcv: Option<i64>,
    activity_car: Option<i64>,
    stale_logged: bool,
    /// A number as read from the status file (int or float) or a clock reading.
    pub activity_changed: Option<Number>,
    pub level: Option<i64>,
    pub level_at: Option<f64>,
    pub paused: bool,
    pub limit: Option<i64>,
    pub previous: Option<State>,
    pub n: u64,
    pub migrated: bool,
    /// Boot-relative clock for the drain deadline (default `boottime`).
    pub uptime: Box<dyn Fn() -> Option<f64>>,
    /// Required owner of the drain request (root; tests use their own uid).
    pub drain_owner: u32,
    /// Deadline (uptime s) while drain mode is on.
    pub drain_until: Option<i64>,
    /// Last drain line logged (each is logged once).
    drain_note: Option<String>,
}

fn int(v: Option<&Value>) -> Option<i64> {
    v.filter(|v| v.is_i64() || v.is_u64())
        .and_then(Value::as_i64)
}

impl Guard {
    pub fn new(paths: Paths, control: Box<dyn Control>, clock: Box<dyn Fn() -> f64>) -> Self {
        let boot_id = read(&paths.boot_id);
        let metadata = charge_status::parse(&read(&paths.status), clock(), &boot_id, None);
        let stamp = |m: &charge_status::ChargeStatus| {
            m.raw["updated_monotonic"].as_f64().expect("checked number")
        };
        let previous = metadata.as_ref().filter(|m| clock() - stamp(m) <= 150.0);
        // Display tracking is separate from the raw estimate used by the actuator.
        let level = previous.and_then(|p| p.level);
        let level_at = previous.map(stamp);
        let (mut activity, mut activity_changed) = (None, None);
        if let Some(p) = previous.filter(|_| !boot_id.is_empty()) {
            let (a, changed) = (
                int(p.raw.get("activity")),
                p.raw.get("activity_changed_monotonic"),
            );
            let changed = changed.and_then(|c| match c {
                Value::Number(n) => Some(n.clone()),
                _ => None,
            });
            // Comparison values come back from any recent snapshot, so a change on the first
            // tick is seen; trust (activity_changed) only while it is still fresh.
            activity = a.filter(|a| (-100_000..=100_000).contains(a));
            if let (Some(_), Some(c)) = (activity, changed) {
                let age = clock() - c.as_f64().expect("JSON number");
                if (0.0..=180.0).contains(&age) {
                    activity_changed = Some(c);
                }
            }
        }
        let snapshot = previous.filter(|_| !boot_id.is_empty());
        let activity_zcv = snapshot
            .and_then(|p| {
                // A status written before activity_zcv existed has only zcv_mv: fall back only
                // when the key is missing or null, whatever else it holds (as the Python does).
                match p.raw.get("activity_zcv") {
                    None | Some(Value::Null) => int(p.raw.get("zcv_mv")),
                    own => int(own),
                }
            })
            .filter(|v| (2500..=5000).contains(v));
        let activity_car = snapshot
            .and_then(|p| int(p.raw.get("activity_car")))
            .filter(|v| (-100_000..=100_000).contains(v));
        let migrated = metadata.as_ref().is_some_and(|m| {
            !boot_id.is_empty() && m.raw.get("migration_attempted") == Some(&Value::Bool(true))
        });
        Guard {
            paths,
            control,
            clock,
            write: Box::new(bcct::write_command),
            boot_id,
            activity,
            activity_zcv,
            activity_car,
            stale_logged: false,
            activity_changed,
            level,
            level_at,
            paused: false,
            limit: None,
            previous: None,
            n: 0,
            migrated,
            uptime: Box::new(boottime),
            drain_owner: 0,
            drain_until: None,
            drain_note: None,
        }
    }

    fn drain_log(&mut self, text: String) {
        if self.drain_note.as_deref() != Some(text.as_str()) {
            self.record(&text);
        }
        self.drain_note = Some(text);
    }

    /// Enter, keep or leave drain mode. A present request the guard does not honour is removed.
    fn drain_step(&mut self, until: Option<i64>, reason: Option<String>) {
        if let (Some(until), None) = (until, &reason) {
            if Some(until) != self.drain_until {
                self.drain_log(format!(
                    "drain mode on: BCCT stays paused until uptime {until} s; low-battery poweroff stays active"
                ));
            }
            self.drain_until = Some(until);
            return;
        }
        let mut suffix = String::new();
        if reason.is_some() {
            match fs::remove_file(&self.paths.drain_request) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => {
                    suffix = format!("; removal failed (errno {})", e.raw_os_error().unwrap_or(0))
                }
            }
        }
        if self.drain_until.is_some() {
            let why = match reason.as_deref() {
                None => "request removed",
                Some("expired") => "deadline reached",
                Some(r) => r,
            };
            self.drain_log(format!("drain mode ended: {why}{suffix}"));
        } else if let Some(r) = &reason {
            self.drain_log(format!("drain request rejected: {r}{suffix}"));
        } else {
            self.drain_note = None;
        }
        self.drain_until = None;
    }

    pub fn record(&self, text: &str) {
        let line = format!("{} {text}\n", timestamp());
        let ok = OpenOptions::new()
            .append(true)
            .create(true)
            .open(&self.paths.log)
            .and_then(|mut f| f.write_all(line.as_bytes()));
        if ok.is_err() {
            eprint!("{line}");
        }
    }

    fn write_command(&mut self, unlimited: bool, paused: bool) -> Result<(), Error> {
        // Only the one-time migration may touch this legacy test interface.
        let text = format!("{} {}", unlimited as u8, paused as u8);
        (self.write)(&self.paths.command, &text)
    }

    pub fn release(&mut self) -> Option<String> {
        match self.control.release() {
            Ok(()) => {
                self.paused = false;
                None
            }
            Err(exc) => {
                self.record(&format!("ERROR BCCT release failed: {exc}"));
                Some(exc.0)
            }
        }
    }

    pub fn close(&mut self) {
        let error = self.release();
        // A clean stop releases hardware; retire its measurement while retaining the migration latch.
        let now = (self.clock)();
        self.publish(&[
            ("schema", 1i64.into()),
            ("backend", "bcct".into()),
            ("boot_id", self.boot_id.as_str().into()),
            ("updated_monotonic", now.into()),
            ("level", Json::Null),
            ("zcv_mv", Json::Null),
            ("paused", self.paused.into()),
            ("valid", false.into()),
            ("enabled", false.into()),
            ("limit", self.limit.unwrap_or(80).into()),
            ("error", error.into()),
            ("online", Json::Null),
            ("stopped", true.into()),
            ("migration_attempted", self.migrated.into()),
            ("activity", Json::Null),
            ("activity_changed_monotonic", Json::Null),
        ]);
    }

    pub fn publish(&self, data: &[(&str, Json)]) {
        let parent = match self.paths.status.parent() {
            Some(p) if !p.as_os_str().is_empty() => p.to_path_buf(),
            _ => PathBuf::from("."),
        };
        let mut temp = None;
        let result = (|| {
            // tempfile.mkstemp(prefix=".i14-charge-", dir=parent): 0600, O_EXCL, retried on a name clash.
            let mut attempt = 0;
            let (mut f, name) = loop {
                let name = parent.join(format!(".i14-charge-{}", temp_name(attempt)));
                match OpenOptions::new()
                    .read(true)
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                    .open(&name)
                {
                    Ok(f) => break (f, name),
                    Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && attempt < 10_000 => {
                        attempt += 1
                    }
                    Err(e) => return Err(Error::os(&e, Some(&name))),
                }
            };
            let name = std::path::absolute(&name).unwrap_or(name);
            temp = Some(name.clone());
            f.write_all(py::dump(data).as_bytes())
                .map_err(|e| Error::os(&e, None))?;
            f.set_permissions(fs::Permissions::from_mode(0o644))
                .map_err(|e| Error::os(&e, None))?;
            drop(f);
            fs::rename(&name, &self.paths.status)
                .map_err(|e| Error::os2(&e, Some(&name), Some(&self.paths.status)))
        })();
        if let Err(exc) = result {
            self.record(&format!("ERROR charge status unavailable: {exc}"));
        }
        if let Some(t) = temp {
            if t.exists() {
                let _ = fs::remove_file(t);
            }
        }
    }

    /// One measurement and control step; true when the battery is low enough to power off.
    pub fn tick(&mut self) -> bool {
        let now = (self.clock)();
        let p = &self.paths;
        let cap = number(&p.battery.join("capacity"), 0, 100);
        let activity = activity_number(&read(&p.gauge_activity)).filter(|a| a.abs() <= 100_000);
        let zcv_read = number(&p.gauge, 2500, 5000);
        let car_path = p.gauge_activity.with_file_name("FG_g_fg_dbg_bat_car");
        let car = activity_number(&read(&car_path)).filter(|a| a.abs() <= 100_000);
        if [
            (self.activity, activity),
            (self.activity_zcv, zcv_read),
            (self.activity_car, car),
        ]
        .iter()
        .any(|(old, new)| matches!((old, new), (Some(a), Some(b)) if a != b))
        {
            self.activity_changed = Some(Number::from_f64(now).expect("finite clock"));
        }
        if activity.is_some() {
            self.activity = activity;
        }
        self.activity_zcv = zcv_read;
        self.activity_car = car;
        let changed = self
            .activity_changed
            .as_ref()
            .map(|c| c.as_f64().expect("JSON number"));
        let live =
            activity.is_some() && changed.is_some_and(|c| (0.0..=180.0).contains(&(now - c)));
        let stale = changed.is_some_and(|c| now - c > 180.0);
        if stale && !self.stale_logged {
            self.record("stale gauge activity");
        }
        self.stale_logged = stale;
        let zcv = if live { zcv_read } else { None };
        let gauge = ocv_percent(zcv);
        let status = read(&p.battery.join("status"));
        let voltage = number(&p.battery.join("batt_vol"), 2500, 5000);
        let temp = number(&p.battery.join("batt_temp"), 0, 450);
        let kind = number(&p.charger_type, 0, 8);
        let (usb, ac) = (number(&p.usb, 0, 1), number(&p.ac, 0, 1));
        let cfg = Config::load(&p.settings);
        let limit = i64::from(cfg.data.charge_limit);
        let enabled = cfg.data.charge_limit_enabled;
        let feedback = read(&p.readback);
        let stopped = discharging(&feedback, &["-1", "0", "1"]);
        let current = unlimited(&feedback, &["0", "1"]);
        let readings = !cfg.damaged
            && enabled
            && cap.is_some()
            && zcv.is_some()
            && temp.is_some()
            && kind.is_some_and(|k| k > 0)
            && usb.is_some()
            && ac.is_some()
            && (usb != Some(0) || ac != Some(0))
            && [
                "Charging",
                "Full",
                "Discharging",
                "Not charging",
                "Cmd discharging",
            ]
            .contains(&status.as_str())
            && stopped.is_some()
            && current.is_some();
        let mut valid = readings && voltage.is_some_and(|v| v > 3700);
        let now_up = (self.uptime)().filter(|v| v.is_finite());
        let (until, reason) = match drain_request(
            &self.paths.drain_request,
            &self.boot_id,
            now_up,
            self.drain_owner,
        ) {
            Ok(Some(d)) => (Some(d), drain_hazard(readings, voltage, temp)),
            Ok(None) => (None, None),
            Err(r) => (None, Some(r)),
        };
        self.drain_step(until, reason);
        let mut error = None;
        if let Err(exc) = self.control_step(stopped, current, limit, valid, gauge, zcv) {
            valid = false;
            self.record(&format!("ERROR charge control failed: {exc}"));
            let mut text = exc.0;
            if let Some(release_error) = self.release() {
                text += &format!("; release failed: {release_error}");
            }
            if self.drain_until.is_some() {
                self.drain_step(None, Some("charge control error".into()));
            }
            error = Some(text);
        }
        self.limit = Some(limit);
        match (gauge, self.level) {
            (Some(g), Some(level)) => {
                if now - self.level_at.expect("set with level") >= 60.0 {
                    // Voltage relaxation must not make the screen rise while paused/discharging.
                    // Match the vendor UI idea: at most one point per minute in the flow direction.
                    self.level = Some(if self.paused || activity.expect("live gauge") >= 0 {
                        level.min(g.max(level - 1))
                    } else {
                        level.max(g.min(level + 1))
                    });
                }
            }
            _ => self.level = gauge,
        }
        self.level_at = Some(now);
        let state = (self.paused, valid, limit, enabled, error.clone());
        if self.previous.as_ref() != Some(&state) || self.n.is_multiple_of(5) {
            let show = |v: Option<i64>| v.map_or("None".to_string(), |v| v.to_string());
            self.record(&format!(
                "capacity={} gauge={} zcv={} status={status} voltage={} usb_unlimited=0 limit={limit} enabled={} \
                 discharge={} valid={} backend=bcct",
                show(cap),
                show(gauge),
                show(zcv),
                show(voltage),
                enabled as u8,
                self.paused as u8,
                valid as u8
            ));
        }
        let online = match (usb, ac) {
            (Some(u), Some(a)) => Some(u != 0 || a != 0),
            _ => None,
        };
        self.publish(&[
            ("schema", 1i64.into()),
            ("backend", "bcct".into()),
            ("boot_id", self.boot_id.as_str().into()),
            ("updated_monotonic", now.into()),
            ("level", self.level.into()),
            ("raw_level", gauge.into()),
            ("zcv_mv", zcv.into()),
            ("paused", self.paused.into()),
            ("valid", valid.into()),
            ("limit", limit.into()),
            ("stop_mv", ocv(limit).into()),
            ("resume_mv", ocv(limit - 10).into()),
            ("enabled", enabled.into()),
            ("error", error.into()),
            ("migration_attempted", self.migrated.into()),
            ("online", online.into()),
            ("activity", activity.into()),
            (
                "activity_changed_monotonic",
                self.activity_changed.clone().into(),
            ),
            ("activity_zcv", self.activity_zcv.into()),
            ("activity_car", self.activity_car.into()),
            ("drain", self.drain_until.is_some().into()),
            ("drain_until", self.drain_until.into()),
        ]);
        self.previous = Some(state);
        self.n += 1;
        let low = if status == "Charging" || status == "Full" {
            3
        } else {
            5
        };
        cap.is_some_and(|c| c <= low)
    }

    /// The `try:` body of charging.py's tick.
    fn control_step(
        &mut self,
        stopped: Option<&str>,
        current: Option<&str>,
        limit: i64,
        valid: bool,
        gauge: Option<i64>,
        zcv: Option<i64>,
    ) -> Result<(), Error> {
        let (Some(stopped), Some(current)) = (stopped, current) else {
            return Err(Error::value("charger feedback unavailable"));
        };
        if current == "1" || stopped == "1" {
            if self.migrated {
                return Err(Error::value(
                    "legacy charger control changed outside i14-power",
                ));
            }
            // Latch before I/O: failed/uncertain migration must not reset the meter every minute.
            self.migrated = true;
            self.write_command(false, false)?;
            let feedback = read(&self.paths.readback);
            if unlimited(&feedback, &["0"]).is_none()
                || discharging(&feedback, &["-1", "0"]).is_none()
            {
                return Err(Error::value("legacy migration readback failed"));
            }
        }
        self.migrated = true;
        let mut paused = self.control.paused()?;
        if self.drain_until.is_some() {
            paused = true; // drain mode: no band, limit-change or reserve resume
        } else {
            if self.limit.is_some_and(|l| l != limit) {
                paused = false;
            }
            let (gauge, zcv) = (gauge.unwrap_or(0), zcv.unwrap_or(0)); // only used when valid (both known)
            if !valid || gauge <= 15 {
                paused = false;
            } else if zcv >= ocv(limit) {
                paused = true;
            } else if zcv <= ocv(limit - 10) {
                paused = false;
            }
        }
        if paused != self.control.paused()? {
            self.control.set_paused(paused)?;
        }
        self.paused = self.control.paused()?;
        Ok(())
    }
}
