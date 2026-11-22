//! What the status bar and home page show: battery, addresses, disk space,
//! uptime, as the helpers at the top of `i14ui/app.py`.

use std::process::Command;

use i14_config::charge_status::{self, ChargeStatus};

pub const BAT: &str = "/sys/class/power_supply/battery";
/// 0.1 mA, negative = into the battery.
pub const FG_CURRENT: &str = "/sys/devices/platform/battery_meter/FG_Current";

/// Where the battery facts come from (tests point these at a temp dir).
#[derive(Clone, Debug)]
pub struct Paths {
    pub bat: String,
    pub fg_current: String,
    pub charge_status: String,
    pub boot_id: String,
}

impl Default for Paths {
    fn default() -> Self {
        Self {
            bat: BAT.into(),
            fg_current: FG_CURRENT.into(),
            charge_status: charge_status::STATUS.into(),
            boot_id: charge_status::BOOT_ID.into(),
        }
    }
}

/// app.py's `read`: stripped file text, or `default` when it cannot be read.
pub fn read(path: &str, default: &str) -> String {
    match std::fs::read(path) {
        Ok(b) => String::from_utf8_lossy(&b).trim().to_string(),
        Err(_) => default.to_string(),
    }
}

/// Python's int() for a decimal string: surrounding whitespace, a sign, digits.
pub fn py_int(s: &str) -> Option<i64> {
    let s = s.trim();
    let digits = s.strip_prefix(['+', '-']).unwrap_or(s);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    s.parse().ok()
}

pub fn level_text(s: Option<&ChargeStatus>) -> String {
    match s.and_then(|s| s.level.map(|l| (l, s.anchored))) {
        Some((l, true)) => format!("{l}"),
        Some((l, false)) => format!("~{l}"),
        None => "?".into(),
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Battery {
    /// "80", "~80" or "?"
    pub cap: String,
    /// Kernel-style word for colouring: "Charging", "Discharging", ...
    pub state: &'static str,
    /// Dutch description.
    pub word: &'static str,
    pub current: String,
    pub volt: String,
    /// Estimated remaining charge/discharge time (e.g. "~48 min tot 80%"), when HW-CAR data is present.
    pub eta: Option<String>,
}

fn format_duration(total_mins: i64) -> String {
    if total_mins < 1 {
        return "<1 min".into();
    }
    if total_mins < 60 {
        return format!("~{total_mins} min");
    }
    let h = total_mins / 60;
    let m = total_mins % 60;
    if m == 0 {
        format!("~{h} u")
    } else {
        format!("~{h} u {m:02} m")
    }
}

/// Estimate remaining charge time (to `limit` or 100%, accounting for linear-charger duty cycle
/// and CV taper above 85%) or remaining discharge time, from the HW-CAR snapshot.
pub fn battery_eta(s: &ChargeStatus, state: &str) -> Option<String> {
    let q_rem = s.raw.get("q_rem_mah")?.as_f64()?;
    let q_full = s.raw.get("q_full_mah")?.as_f64()?;
    let act = s.raw.get("activity")?.as_i64()?;
    if !q_rem.is_finite() || !q_full.is_finite() || q_full <= 100.0 {
        return None;
    }
    let i_ma = act as f64 / 10.0; // negative = charging, positive = discharging/paused
    if state == "Charging" && i_ma < -50.0 {
        let target_pct = if s.enabled && s.limit < 100 {
            s.limit
        } else {
            100
        };
        let target_mah = (target_pct as f64 / 100.0) * q_full;
        let suffix = if target_pct < 100 {
            format!("tot {target_pct}%")
        } else {
            "tot vol".into()
        };
        if q_rem >= target_mah {
            return Some(if target_pct < 100 {
                format!("bijna {target_pct}%")
            } else {
                "bijna vol".into()
            });
        }
        // MTK linear charger pauses ~1 s per 10 s -> ~87% net CC rate; CV taper above 85% averages ~35%.
        let cc_ma = (-i_ma * 0.87).max(50.0);
        let cv_start = 0.85 * q_full;
        let cc_mah = (target_mah.min(cv_start) - q_rem).max(0.0);
        let cv_mah = (target_mah - q_rem.max(cv_start)).max(0.0);
        let cv_ma = (cc_ma * 0.35).max(80.0);
        let hours = cc_mah / cc_ma + cv_mah / cv_ma;
        if !hours.is_finite() || hours > 99.0 {
            return None;
        }
        let mins = (hours * 60.0).round() as i64;
        return Some(format!("{} {suffix}", format_duration(mins)));
    }
    if (state == "Discharging" || state == "Cmd discharging") && i_ma > 20.0 {
        // Wake-sampled current is slightly above sleep average (~80% continuous factor).
        let disch_ma = (i_ma * 0.80).max(20.0);
        let hours = q_rem.max(0.0) / disch_ma;
        if !hours.is_finite() || hours > 99.0 {
            return None;
        }
        let mins = (hours * 60.0).round() as i64;
        return Some(format!("{} over", format_duration(mins)));
    }
    None
}

impl Paths {
    pub fn status(&self, now: f64) -> Option<ChargeStatus> {
        charge_status::read_status(
            &self.charge_status,
            &self.boot_id,
            now,
            Some(charge_status::MAX_AGE),
        )
    }

    pub fn battery_level(&self, now: f64) -> String {
        level_text(self.status(now).as_ref())
    }

    pub fn battery(&self, now: f64) -> Battery {
        let s = self.status(now);
        let cap = level_text(s.as_ref());
        let (state, word) = match &s {
            None => ("Not charging", "accumeting onbekend"),
            Some(s) if s.level.is_none() => ("Not charging", "accumeting onbekend"),
            Some(s) if s.error.as_deref().is_some_and(|e| !e.is_empty()) => {
                ("Not charging", "regelingfout")
            }
            Some(s) if s.paused => ("Cmd discharging", "laden gepauzeerd"),
            Some(s) if s.online == Some(true) => ("Charging", "laden toegestaan"),
            Some(s) if s.online == Some(false) => ("Discharging", "ontladen"),
            Some(_) => ("Not charging", "laadstatus onbekend"),
        };
        // Python: -int(x) // 10, a floor division of the negated value.
        let current = match py_int(&read(&self.fg_current, "0")) {
            Some(v) => format!("{:+} mA", (-v).div_euclid(10)),
            None => "? mA".into(),
        };
        let volt = match py_int(&read(&format!("{}/batt_vol", self.bat), "0")) {
            Some(v) => format!("{:.2} V", v as f64 / 1000.0),
            None => "? V".into(),
        };
        let eta = s.as_ref().and_then(|st| battery_eta(st, state));
        Battery {
            cap,
            state,
            word,
            current,
            volt,
            eta,
        }
    }
}

/// IPv4 address per interface (not lo), from `ip -4 -o addr`.
pub fn addresses() -> Vec<(String, String)> {
    let Ok(out) = Command::new("ip").args(["-4", "-o", "addr"]).output() else {
        return Vec::new();
    };
    parse_addresses(&String::from_utf8_lossy(&out.stdout))
}

pub fn parse_addresses(text: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for line in text.lines() {
        let parts: Vec<&str> = line.split_whitespace().collect();
        if parts.len() > 3 && parts[1] != "lo" {
            let addr = parts[3].split('/').next().unwrap_or("").to_string();
            // a later line for the same interface wins, as in a Python dict
            match out.iter_mut().find(|(i, _)| i == parts[1]) {
                Some(e) => e.1 = addr,
                None => out.push((parts[1].to_string(), addr)),
            }
        }
    }
    out
}

pub fn address_of<'a>(list: &'a [(String, String)], iface: &str) -> Option<&'a str> {
    list.iter()
        .find(|(i, _)| i == iface)
        .map(|(_, a)| a.as_str())
}

/// "1.2 van 2.9 GB vrij"
pub fn free(path: &str) -> String {
    match i14_sys::disk_space(path) {
        Ok((avail, total)) => {
            let gib = 1u64 << 30;
            format!(
                "{:.1} van {:.1} GB vrij",
                avail as f64 / gib as f64,
                total as f64 / gib as f64
            )
        }
        Err(_) => "?".into(),
    }
}

/// Whole seconds since boot, from /proc/uptime.
pub fn uptime() -> i64 {
    read("/proc/uptime", "0")
        .split_whitespace()
        .next()
        .and_then(|s| s.parse::<f64>().ok())
        .map_or(0, |f| f as i64)
}

pub const TERM_COLORS: [[u8; 3]; 16] = [
    [0, 0, 0],
    [205, 49, 49],
    [13, 188, 121],
    [229, 229, 16],
    [36, 114, 200],
    [188, 63, 188],
    [17, 168, 205],
    [229, 229, 229],
    [102, 102, 102],
    [241, 76, 76],
    [35, 209, 139],
    [245, 245, 67],
    [59, 142, 234],
    [214, 112, 214],
    [41, 184, 219],
    [255, 255, 255],
];

/// xterm 256-colour palette entry.
pub fn palette(index: u8) -> [u8; 3] {
    let i = index as usize;
    if i < 16 {
        return TERM_COLORS[i];
    }
    if i < 232 {
        let i = i - 16;
        let level = |v: usize| [0u8, 95, 135, 175, 215, 255][v];
        return [level(i / 36), level(i / 6 % 6), level(i % 6)];
    }
    let g = (8 + (i - 232) * 10) as u8;
    [g, g, g]
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::fs;

    struct Fixture {
        dir: std::path::PathBuf,
        paths: Paths,
        status: serde_json::Value,
    }

    impl Fixture {
        fn new(name: &str) -> Self {
            let dir =
                std::env::temp_dir().join(format!("i14-status-{name}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            for (k, v) in [
                ("capacity", "100"),
                ("status", "Full"),
                ("batt_vol", "4050"),
                ("current", "-6000"),
                ("boot_id", "boot-a"),
            ] {
                fs::write(dir.join(k), v).unwrap();
            }
            let s = |p: &str| dir.join(p).display().to_string();
            let paths = Paths {
                bat: s(""),
                fg_current: s("current"),
                charge_status: s("charge.json"),
                boot_id: s("boot_id"),
            };
            let paths = Paths {
                bat: paths.bat.trim_end_matches('/').to_string(),
                ..paths
            };
            let status = json!({"schema": 1, "backend": "bcct", "boot_id": "boot-a", "updated_monotonic": 1000.0,
                "level": 80, "zcv_mv": 4109, "paused": true, "valid": true, "enabled": true, "limit": 80,
                "error": null, "online": true});
            let f = Fixture { dir, paths, status };
            f.write();
            f
        }
        fn write(&self) {
            fs::write(&self.paths.charge_status, self.status.to_string()).unwrap();
        }
        fn update(&mut self, v: serde_json::Value) {
            for (k, val) in v.as_object().unwrap() {
                self.status[k] = val.clone();
            }
            self.write();
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.dir);
        }
    }

    #[test]
    fn fresh_guard_estimate_and_pause_override_driver100_full() {
        let f = Fixture::new("fresh");
        let b = f.paths.battery(1000.0);
        assert_eq!(
            (b.cap.as_str(), b.state, b.word),
            ("~80", "Cmd discharging", "laden gepauzeerd")
        );
        assert_eq!(f.paths.battery_level(1000.0), "~80");
        assert_eq!((b.current.as_str(), b.volt.as_str()), ("+600 mA", "4.05 V"));
    }

    #[test]
    fn anchored_status_displays_exact_percent_without_tilde() {
        let mut f = Fixture::new("anchored");
        f.update(json!({"anchored": true, "level": 78}));
        let b = f.paths.battery(1000.0);
        assert_eq!(b.cap.as_str(), "78");
        assert_eq!(f.paths.battery_level(1000.0), "78");
        f.update(json!({"anchored": false, "level": 78}));
        assert_eq!(f.paths.battery_level(1000.0), "~78");
    }

    #[test]
    fn battery_eta_estimates_charge_to_limit_full_and_discharge() {
        let mut f = Fixture::new("eta");
        // Charging at -660 mA from 2441/3650 mAh toward 80% limit (2920 mAh): ~479 mAh / 574 mA ≈ 50 min
        f.update(json!({
            "paused": false, "enabled": true, "limit": 80, "online": true,
            "q_rem_mah": 2441.0, "q_full_mah": 3650.0, "activity": -6600
        }));
        let b = f.paths.battery(1000.0);
        assert_eq!(b.eta.as_deref(), Some("~50 min tot 80%"));

        // Charging to 100% (limit disabled): includes CV taper above 85%
        f.update(json!({"enabled": false}));
        let b_full = f.paths.battery(1000.0);
        assert!(
            b_full
                .eta
                .as_deref()
                .is_some_and(|e| e.ends_with("tot vol") && e.starts_with("~3 u")),
            "got {:?}",
            b_full.eta
        );

        // Unplugged discharging at +120 mA from 2400 mAh -> ~25 u over
        f.update(json!({"online": false, "activity": 1200, "q_rem_mah": 2400.0}));
        let b_dis = f.paths.battery(1000.0);
        assert_eq!(b_dis.eta.as_deref(), Some("~25 u over"));
    }

    #[test]
    fn release_means_allowed_without_claiming_full() {
        let mut f = Fixture::new("release");
        f.update(json!({"paused": false, "level": 72, "zcv_mv": 4030}));
        let b = f.paths.battery(1000.0);
        assert_eq!(
            (b.cap.as_str(), b.state, b.word),
            ("~72", "Charging", "laden toegestaan")
        );
    }

    #[test]
    fn unplugged_and_disabled_limit_keep_estimate() {
        let mut f = Fixture::new("unplugged");
        f.update(json!({"paused": false, "enabled": false, "valid": false, "online": false, "level": 65, "zcv_mv": 3962}));
        let b = f.paths.battery(1000.0);
        assert_eq!(
            (b.cap.as_str(), b.state, b.word),
            ("~65", "Discharging", "ontladen")
        );
    }

    #[test]
    fn missing_stale_bad_and_wrong_boot_status_never_display100() {
        let mut f = Fixture::new("missing");
        fs::remove_file(&f.paths.charge_status).unwrap();
        assert_eq!(f.paths.battery(1000.0).cap, "?");
        let original = f.status.clone();
        for change in [
            json!({"updated_monotonic": 849.0}),
            json!({"updated_monotonic": 1001.0}),
            json!({"boot_id": "other-boot"}),
            json!({"level": true}),
            json!({"level": 101}),
            json!({"paused": "yes"}),
            json!({"backend": "legacy"}),
        ] {
            f.status = original.clone();
            f.update(change.clone());
            assert_eq!(f.paths.battery(1000.0).cap, "?", "{change}");
            assert_eq!(f.paths.battery_level(1000.0), "?", "{change}");
        }
        fs::write(&f.paths.charge_status, "{broken").unwrap();
        assert_eq!(f.paths.battery(1000.0).cap, "?");
    }

    #[test]
    fn backend_error_is_visible() {
        let mut f = Fixture::new("error");
        f.update(json!({"error": "BCCT readback failed", "valid": false, "paused": false}));
        let b = f.paths.battery(1000.0);
        assert_eq!((b.state, b.word), ("Not charging", "regelingfout"));
    }

    #[test]
    fn current_rounds_like_python_floor_division() {
        let f = Fixture::new("current");
        fs::write(&f.paths.fg_current, "123").unwrap();
        assert_eq!(f.paths.battery(1000.0).current, "-13 mA");
        fs::write(&f.paths.fg_current, "x").unwrap();
        assert_eq!(f.paths.battery(1000.0).current, "? mA");
    }

    #[test]
    fn palette_and_addresses() {
        assert_eq!(palette(1), [205, 49, 49]);
        assert_eq!(palette(16 + 36 + 6 + 1), [95, 95, 95]);
        assert_eq!(palette(255), [238, 238, 238]);
        let a = parse_addresses(
            "1: lo    inet 127.0.0.1/8 scope host lo\n3: wlan0    inet 192.0.2.16/24 brd x\n",
        );
        assert_eq!(a, vec![("wlan0".to_string(), "192.0.2.16".to_string())]);
    }
}
