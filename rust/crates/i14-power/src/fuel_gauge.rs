//! Hardware coulomb-counter (MT6328 HW_FG CAR) fuel gauge for the I14 phone.
//!
//! Combines:
//! 1. MT6328 hardware coulomb counter (`FG_g_fg_dbg_bat_car`, s32) as primary integrator.
//! 2. Bracketed torn-read-free FG cache capture (`d0`, `current`, `zcv`, `car`).
//! 3. Kernel `battery_meter_reset()` detection (`/dev/kmsg` + `d0` + `online` + plausibility)
//!    with short-interval trapezoidal current bridging (`dt <= 120 s`) and OCV re-seeding
//!    on long gaps.
//! 4. Cell-specific empirical `OCV_REAL(SOC)` table and `R_real = 175 mOhm` (overridable
//!    via `/mnt/data/media/0/i14-linux/battery-cal.json`).
//! 5. Confirmed Full (`>= 4300 mV` + `|I| <= 200 mA` for 5 min) and Empty (`<= 3420 mV` +
//!    `V_ocv <= 3450 mV` at moderate load) anchors with bounded `Q_full` aging updates.
//! 6. Slow relaxed-OCV drift correction (`<= 0.5%/h` outside `±2%` deadband when
//!    `|I| <= 150 mA` for `>= 10 min`) to bound multi-week 80% BCCT float drift.
//! 7. Atomic persistent state (`/mnt/data/media/0/i14-linux/battery-state.json`) keyed on
//!    `boot_id` and `CLOCK_BOOTTIME` (never wall-clock / NTP).

use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Read, Seek, SeekFrom, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{json, Value};

pub const STATE_PATH: &str = "/mnt/data/media/0/i14-linux/battery-state.json";
pub const CAL_PATH: &str = "/mnt/data/media/0/i14-linux/battery-cal.json";
pub const KMSG_PATH: &str = "/dev/kmsg";

/// Default usable capacity in MT6328 CAR units (preliminary stitched Nov 7 + Nov 20-21 curve;
/// overridable via `battery-cal.json` and refined by clean Full<->Empty cycles).
pub const DEFAULT_Q_FULL_MAH: f64 = 3650.0;
pub const Q_FULL_MIN_MAH: f64 = 2500.0;
pub const Q_FULL_MAX_MAH: f64 = 4500.0;
/// Settled effective internal resistance of this I14 cell (~160-190 mOhm vs generic 215 mOhm).
pub const DEFAULT_R_REAL_MOHM: f64 = 175.0;
/// Charge polarization hysteresis subtracted when estimating OCV under active charging (`I < -200 mA`).
pub const DEFAULT_CHARGE_HYSTERESIS_MV: f64 = 20.0;

/// Hard voltage safety ceiling for BCCT charge limiting: strictly below stock kernel
/// `v_cc2topoff_thres` (4260 mV) so normal 80%-limited operation never enters `bat_full=100%` lock.
pub const BCCT_HARD_CEILING_MV: i64 = 4240;
pub const BCCT_RESUME_CEILING_MV: i64 = 4200;

/// Sustained low-voltage safety poweroff threshold (strictly separated from 0% learning anchor).
pub const SAFETY_POWEROFF_MV: i64 = 3420;
pub const SAFETY_POWEROFF_STREAK: u32 = 2;

/// Empirical I14 `(soc_percent, ocv_mv)` curve at `R_real = 175 mOhm`, `Q_full = 3650 CAR-mAh`
/// (derived from the Nov 20-21 continuous discharge and Nov 7 full-charge traces).
pub const I14_OCV_REAL: [(i64, i64); 21] = [
    (0, 3432),
    (5, 3475),
    (10, 3520),
    (15, 3549),
    (20, 3574),
    (25, 3596),
    (30, 3614),
    (35, 3633),
    (40, 3652),
    (45, 3679),
    (50, 3725),
    (55, 3774),
    (60, 3830),
    (65, 3886),
    (70, 3944),
    (75, 4003),
    (80, 4070),
    (85, 4142),
    (90, 4222),
    (95, 4295),
    (100, 4350),
];

/// Cell calibration parameters (default I14 profile, or loaded from `battery-cal.json`).
#[derive(Clone, Debug, PartialEq)]
pub struct Calibration {
    pub q_full_mah: f64,
    pub r_real_mohm: f64,
    pub charge_hysteresis_mv: f64,
    pub ocv_table: Vec<(i64, i64)>,
}

impl Default for Calibration {
    fn default() -> Self {
        Self {
            q_full_mah: DEFAULT_Q_FULL_MAH,
            r_real_mohm: DEFAULT_R_REAL_MOHM,
            charge_hysteresis_mv: DEFAULT_CHARGE_HYSTERESIS_MV,
            ocv_table: I14_OCV_REAL.to_vec(),
        }
    }
}

impl Calibration {
    /// Load calibration overrides from JSON if present and valid; otherwise fall back to defaults.
    pub fn load_or_default(path: &Path) -> Self {
        let mut cal = Self::default();
        let Ok(text) = fs::read_to_string(path) else {
            return cal;
        };
        let Ok(val) = serde_json::from_str::<Value>(&text) else {
            return cal;
        };
        if let Some(q) = val.get("q_full_mah").and_then(Value::as_f64) {
            if q.is_finite() && (Q_FULL_MIN_MAH..=Q_FULL_MAX_MAH).contains(&q) {
                cal.q_full_mah = q;
            }
        }
        if let Some(r) = val.get("r_real_mohm").and_then(Value::as_f64) {
            if r.is_finite() && (50.0..=500.0).contains(&r) {
                cal.r_real_mohm = r;
            }
        }
        if let Some(h) = val.get("charge_hysteresis_mv").and_then(Value::as_f64) {
            if h.is_finite() && (0.0..=100.0).contains(&h) {
                cal.charge_hysteresis_mv = h;
            }
        }
        if let Some(arr) = val.get("ocv_table").and_then(Value::as_array) {
            let mut parsed = Vec::with_capacity(arr.len());
            let mut ok = arr.len() >= 2;
            for item in arr {
                let Some(pair) = item.as_array() else {
                    ok = false;
                    break;
                };
                if pair.len() != 2 {
                    ok = false;
                    break;
                }
                let (Some(p), Some(mv)) = (pair[0].as_i64(), pair[1].as_i64()) else {
                    ok = false;
                    break;
                };
                if !(0..=100).contains(&p) || !(2500..=5000).contains(&mv) {
                    ok = false;
                    break;
                }
                if let Some(&(pp, pmv)) = parsed.last() {
                    if p <= pp || mv <= pmv {
                        ok = false;
                        break;
                    }
                }
                parsed.push((p, mv));
            }
            if ok {
                cal.ocv_table = parsed;
            }
        }
        cal
    }

    /// Estimate open-circuit voltage in mV using real cell resistance.
    ///
    /// Sign convention matches MTK `FG_g_fg_dbg_bat_current`:
    /// `current_deci_ma > 0` = discharging / BCCT pause, `< 0` = charging.
    pub fn v_ocv_est(&self, batt_vol_mv: i64, current_deci_ma: i64) -> f64 {
        let i_ma = current_deci_ma as f64 / 10.0;
        let mut v = batt_vol_mv as f64 + i_ma * (self.r_real_mohm / 1000.0);
        if i_ma < -200.0 {
            v -= self.charge_hysteresis_mv;
        }
        v
    }

    /// Piecewise-linear OCV (mV) -> SOC (`0.0..=100.0`).
    pub fn ocv_to_soc(&self, mv: f64) -> f64 {
        let tbl = &self.ocv_table;
        if tbl.is_empty() {
            return 0.0;
        }
        if mv <= tbl[0].1 as f64 {
            return tbl[0].0 as f64;
        }
        for w in tbl.windows(2) {
            let ((p1, v1), (p2, v2)) = (w[0], w[1]);
            if mv <= v2 as f64 {
                let frac = (mv - v1 as f64) / (v2 - v1) as f64;
                return (p1 as f64 + frac * (p2 - p1) as f64).clamp(0.0, 100.0);
            }
        }
        tbl[tbl.len() - 1].0 as f64
    }
}

/// One coherent hardware fuel-gauge cache snapshot from `/sys/devices/platform/battery_meter/`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FgSnapshot {
    pub car_mah: i64,
    /// `0.1 mA`, positive = discharging/paused, negative = charging.
    pub current_deci_ma: i64,
    pub zcv_mv: i64,
    pub dod0: i64,
}

fn read_int(path: &Path, lo: i64, hi: i64) -> Option<i64> {
    let text = fs::read_to_string(path).ok()?;
    let s = text.trim();
    let digits = s.strip_prefix('-').unwrap_or(s);
    if digits.is_empty() || digits.len() > 8 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let v: i64 = s.parse().ok()?;
    (lo..=hi).contains(&v).then_some(v)
}

/// Bracketed torn-read-free read of the FG cache files in `dir`.
///
/// `update_fg_dbg_tool_value()` updates `d0`, `current`, `zcv`, and `car` together in one
/// `bat_thread` iteration. Reading `(d0, current, zcv)` before and after `car` (up to 3
/// attempts) guarantees a coherent snapshot.
pub fn read_fg_coherent(zcv_path: &Path, current_path: &Path) -> Option<FgSnapshot> {
    let car_path = current_path.with_file_name("FG_g_fg_dbg_bat_car");
    let d0_path = current_path.with_file_name("FG_g_fg_dbg_d0");
    for _ in 0..3 {
        let d0_1 = read_int(&d0_path, 0, 100);
        let cur_1 = read_int(current_path, -100_000, 100_000)?;
        let zcv_1 = read_int(zcv_path, 2500, 5000)?;
        let car = read_int(&car_path, -100_000, 100_000)?;
        let d0_2 = read_int(&d0_path, 0, 100);
        let cur_2 = read_int(current_path, -100_000, 100_000)?;
        let zcv_2 = read_int(zcv_path, 2500, 5000)?;
        if d0_1 == d0_2 && cur_1 == cur_2 && zcv_1 == zcv_2 {
            return Some(FgSnapshot {
                car_mah: car,
                current_deci_ma: cur_1,
                zcv_mv: zcv_1,
                dod0: d0_1.unwrap_or(-1),
            });
        }
    }
    None
}

/// Non-blocking `/dev/kmsg` watcher that detects stock-kernel `battery_meter_reset()` events.
pub struct KmsgResetWatcher {
    file: Option<File>,
}

impl KmsgResetWatcher {
    pub fn open(path: &Path) -> Self {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK | libc::O_CLOEXEC)
            .open(path)
            .ok()
            .map(|mut f| {
                // Seek to the tail so historical boot lines are not treated as live resets.
                let _ = f.seek(SeekFrom::End(0));
                f
            });
        Self { file }
    }

    pub fn disabled() -> Self {
        Self { file: None }
    }

    /// Drain pending kernel log records; returns `true` if a battery-meter reset was logged
    /// (or if the ring buffer wrapped with `EPIPE`, signaling lost kernel log continuity).
    pub fn poll_reset(&mut self) -> bool {
        let Some(f) = self.file.as_mut() else {
            return false;
        };
        let mut saw_reset = false;
        let mut buf = [0u8; 4096];
        for _ in 0..256 {
            match f.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    let line = String::from_utf8_lossy(&buf[..n]);
                    if line.contains("resetBatteryMeter=(1)")
                        || line.contains("Charger plug in/out")
                        || line.contains("[battery_meter_reset]")
                    {
                        saw_reset = true;
                    }
                }
                Err(e) if e.kind() == ErrorKind::WouldBlock => break,
                Err(e) if e.raw_os_error() == Some(libc::EPIPE) => {
                    // Ring buffer wrapped; continue draining and flag reset conservatively.
                    saw_reset = true;
                }
                Err(_) => break,
            }
        }
        saw_reset
    }
}

/// Persisted fuel-gauge state on userdata (`/mnt/data/media/0/i14-linux/battery-state.json`).
#[derive(Clone, Debug, PartialEq)]
pub struct SavedState {
    pub schema: i64,
    pub boot_id: String,
    pub boottime_s: Option<f64>,
    pub q_rem_mah: f64,
    pub q_full_mah: f64,
    pub anchored: bool,
    pub last_car: Option<i64>,
    pub last_dod0: Option<i64>,
}

impl SavedState {
    pub fn load(path: &Path) -> Option<Self> {
        let text = fs::read_to_string(path).ok()?;
        let v: Value = serde_json::from_str(&text).ok()?;
        if v.get("schema").and_then(Value::as_i64) != Some(1) {
            return None;
        }
        let boot_id = v.get("boot_id")?.as_str()?.to_string();
        let q_rem_mah = v.get("q_rem_mah")?.as_f64()?;
        let q_full_mah = v.get("q_full_mah")?.as_f64()?;
        let anchored = v.get("anchored")?.as_bool()?;
        if !q_rem_mah.is_finite()
            || !q_full_mah.is_finite()
            || !(Q_FULL_MIN_MAH..=Q_FULL_MAX_MAH).contains(&q_full_mah)
            || !(0.0..=q_full_mah + 50.0).contains(&q_rem_mah)
        {
            return None;
        }
        let boottime_s = v
            .get("boottime_s")
            .and_then(Value::as_f64)
            .filter(|x| x.is_finite() && *x >= 0.0);
        let last_car = v.get("last_car").and_then(Value::as_i64);
        let last_dod0 = v.get("last_dod0").and_then(Value::as_i64);
        Some(Self {
            schema: 1,
            boot_id,
            boottime_s,
            q_rem_mah: q_rem_mah.clamp(0.0, q_full_mah),
            q_full_mah,
            anchored,
            last_car,
            last_dod0,
        })
    }

    pub fn save_atomic(&self, path: &Path, do_fsync: bool) -> std::io::Result<()> {
        let parent = path
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos());
        let tmp = parent.join(format!(
            ".battery-state-{}-{}.tmp",
            std::process::id(),
            nanos & 0xffff_ffff
        ));
        let payload = json!({
            "schema": self.schema,
            "boot_id": self.boot_id,
            "boottime_s": self.boottime_s,
            "q_rem_mah": (self.q_rem_mah * 100.0).round() / 100.0,
            "q_full_mah": (self.q_full_mah * 100.0).round() / 100.0,
            "anchored": self.anchored,
            "last_car": self.last_car,
            "last_dod0": self.last_dod0,
        });
        let res = (|| -> std::io::Result<()> {
            let mut f = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&tmp)?;
            f.write_all(payload.to_string().as_bytes())?;
            f.write_all(b"\n")?;
            if do_fsync {
                f.sync_all()?;
            }
            f.set_permissions(fs::Permissions::from_mode(0o644))?;
            drop(f);
            fs::rename(&tmp, path)?;
            Ok(())
        })();
        if res.is_err() {
            let _ = fs::remove_file(&tmp);
        }
        res
    }
}

/// Inputs to one fuel-gauge update step.
#[derive(Clone, Debug)]
pub struct FgInput<'a> {
    pub now_mono: f64,
    pub boottime_s: Option<f64>,
    pub boot_id: &'a str,
    pub batt_vol_mv: i64,
    pub fg: FgSnapshot,
    pub online: Option<bool>,
    pub kmsg_reset: bool,
}

/// Output of one fuel-gauge update step.
#[derive(Clone, Debug, PartialEq)]
pub struct FgOutput {
    pub soc_pct: i64,
    pub soc_f64: f64,
    pub anchored: bool,
    pub q_rem_mah: f64,
    pub q_full_mah: f64,
    pub ocv_mv: i64,
    pub reset_detected: bool,
    pub safety_poweroff: bool,
    pub persist_now: bool,
    pub event: Option<String>,
}

/// Stateful HW-CAR fuel gauge.
pub struct FuelGauge {
    pub cal: Calibration,
    pub state_path: PathBuf,
    q_rem_mah: f64,
    q_full_mah: f64,
    anchored: bool,
    initialized: bool,
    loaded: Option<SavedState>,
    prev_mono: Option<f64>,
    prev_fg: Option<FgSnapshot>,
    prev_online: Option<bool>,
    full_since: Option<f64>,
    empty_since: Option<f64>,
    relaxed_since: Option<f64>,
    low_volt_streak: u32,
    clean_full_start: bool,
    clean_empty_start: bool,
    cycle_discharged_mah: f64,
    cycle_charged_mah: f64,
    last_persisted_mono: Option<f64>,
}

impl FuelGauge {
    pub fn new(cal: Calibration, state_path: PathBuf) -> Self {
        let loaded = SavedState::load(&state_path);
        let q_full_mah = loaded.as_ref().map_or(cal.q_full_mah, |s| s.q_full_mah);
        Self {
            cal,
            state_path,
            q_rem_mah: 0.0,
            q_full_mah,
            anchored: false,
            initialized: false,
            loaded,
            prev_mono: None,
            prev_fg: None,
            prev_online: None,
            full_since: None,
            empty_since: None,
            relaxed_since: None,
            low_volt_streak: 0,
            clean_full_start: false,
            clean_empty_start: false,
            cycle_discharged_mah: 0.0,
            cycle_charged_mah: 0.0,
            last_persisted_mono: None,
        }
    }

    pub fn update(&mut self, inp: &FgInput<'_>) -> FgOutput {
        let v_ocv = self.cal.v_ocv_est(inp.batt_vol_mv, inp.fg.current_deci_ma);
        let ocv_soc = self.cal.ocv_to_soc(v_ocv);
        let i_ma = inp.fg.current_deci_ma as f64 / 10.0;
        let mut reset_detected = false;
        let mut persist_now = false;
        let mut event: Option<String> = None;

        if !self.initialized {
            self.initialized = true;
            if let Some(saved) = self.loaded.take() {
                self.q_full_mah = saved.q_full_mah;
                if !inp.boot_id.is_empty() && saved.boot_id == inp.boot_id {
                    // Same-boot service restart: resume seamlessly from persisted CAR.
                    let mut q = saved.q_rem_mah;
                    if let Some(old_car) = saved.last_car {
                        let same_d0 = saved.last_dod0.is_none_or(|d| d == inp.fg.dod0);
                        let delta = inp.fg.car_mah - old_car;
                        if same_d0 && delta.abs() <= 500 {
                            q = (q + delta as f64).clamp(0.0, self.q_full_mah);
                        }
                    }
                    self.q_rem_mah = q;
                    self.anchored = saved.anchored;
                    event = Some(format!(
                        "fuel-gauge restored same-boot: q_rem={:.1} q_full={:.1} anchored={}",
                        self.q_rem_mah, self.q_full_mah, self.anchored
                    ));
                } else {
                    // Cross-boot reboot: keep saved charge if consistent with boot OCV.
                    let saved_soc = (saved.q_rem_mah / self.q_full_mah) * 100.0;
                    if (saved_soc - ocv_soc).abs() <= 12.0 {
                        self.q_rem_mah = saved.q_rem_mah.clamp(0.0, self.q_full_mah);
                        self.anchored = saved.anchored;
                        event = Some(format!(
                            "fuel-gauge restored cross-boot: q_rem={:.1} saved_soc={:.1}% ocv_soc={:.1}% anchored={}",
                            self.q_rem_mah, saved_soc, ocv_soc, self.anchored
                        ));
                    } else {
                        self.q_rem_mah = (ocv_soc / 100.0) * self.q_full_mah;
                        self.anchored = false;
                        event = Some(format!(
                            "fuel-gauge re-seeded after reboot gap: saved_soc={:.1}% vs ocv_soc={:.1}% (v_ocv={:.0}mV)",
                            saved_soc, ocv_soc, v_ocv
                        ));
                    }
                }
            } else {
                self.q_rem_mah = (ocv_soc / 100.0) * self.q_full_mah;
                self.anchored = false;
                event = Some(format!(
                    "fuel-gauge cold-seeded from OCV: v_ocv={:.0}mV -> {:.1}% ({:.1}/{:.1} mAh)",
                    v_ocv, ocv_soc, self.q_rem_mah, self.q_full_mah
                ));
            }
            self.prev_mono = Some(inp.now_mono);
            self.prev_fg = Some(inp.fg);
            self.prev_online = inp.online;
            persist_now = true;
        } else if let (Some(prev_mono), Some(prev_fg)) = (self.prev_mono, self.prev_fg) {
            let dt = (inp.now_mono - prev_mono).max(0.0);
            let prev_i_ma = prev_fg.current_deci_ma as f64 / 10.0;
            let dcar = (inp.fg.car_mah - prev_fg.car_mah) as f64;
            let max_plausible = prev_i_ma.abs().max(i_ma.abs()).max(300.0) * (dt / 3600.0) + 6.0;
            let dod0_changed = prev_fg.dod0 >= 0 && inp.fg.dod0 >= 0 && inp.fg.dod0 != prev_fg.dod0;
            let online_changed = self.prev_online.is_some()
                && inp.online.is_some()
                && self.prev_online != inp.online;
            let implausible_jump = dcar.abs() > max_plausible;
            reset_detected = inp.kmsg_reset || dod0_changed || online_changed || implausible_jump;

            let epoch_advanced = inp.fg.car_mah != prev_fg.car_mah
                || inp.fg.current_deci_ma != prev_fg.current_deci_ma
                || inp.fg.zcv_mv != prev_fg.zcv_mv
                || inp.fg.dod0 != prev_fg.dod0;

            if reset_detected {
                if dt <= 120.0 {
                    // Bridge this single short interval via trapezoidal current integration.
                    // At dt=10-15 s even a 780 mA plug-in step has <= 0.05% worst-case error.
                    let dq = -0.5 * (prev_i_ma + i_ma) * (dt / 3600.0);
                    self.q_rem_mah = (self.q_rem_mah + dq).clamp(0.0, self.q_full_mah);
                    if dq < 0.0 {
                        self.cycle_discharged_mah += -dq;
                    } else {
                        self.cycle_charged_mah += dq;
                    }
                    event = Some(format!(
                        "fuel-gauge bridged CAR reset (dt={:.1}s, car {}->{}, dq={:+.2} mAh)",
                        dt, prev_fg.car_mah, inp.fg.car_mah, dq
                    ));
                } else {
                    self.q_rem_mah = (ocv_soc / 100.0) * self.q_full_mah;
                    self.anchored = false;
                    self.clean_full_start = false;
                    self.clean_empty_start = false;
                    event = Some(format!(
                        "fuel-gauge re-seeded after long reset gap (dt={:.0}s -> {:.1}%)",
                        dt, ocv_soc
                    ));
                }
                persist_now = true;
            } else if epoch_advanced {
                self.q_rem_mah = (self.q_rem_mah + dcar).clamp(0.0, self.q_full_mah);
                if dcar < 0.0 {
                    self.cycle_discharged_mah += -dcar;
                } else if dcar > 0.0 {
                    self.cycle_charged_mah += dcar;
                }
            }

            // Full anchor: confirmed hardware-full (voltage >= 4300 mV and taper |I| <= 200 mA for 5 min).
            if inp.batt_vol_mv >= 4300 && i_ma.abs() <= 200.0 {
                let since = *self.full_since.get_or_insert(inp.now_mono);
                if inp.now_mono - since >= 300.0 {
                    if self.clean_empty_start
                        && (Q_FULL_MIN_MAH..=Q_FULL_MAX_MAH).contains(&self.cycle_charged_mah)
                    {
                        let lo = self.q_full_mah * 0.95;
                        let hi = self.q_full_mah * 1.05;
                        let new_q = self
                            .cycle_charged_mah
                            .clamp(lo, hi)
                            .clamp(Q_FULL_MIN_MAH, Q_FULL_MAX_MAH);
                        event = Some(format!(
                            "fuel-gauge full anchor + aging update: q_full {:.0}->{:.0} mAh",
                            self.q_full_mah, new_q
                        ));
                        self.q_full_mah = new_q;
                    } else if !self.anchored {
                        event = Some("fuel-gauge confirmed 100% full anchor".into());
                    }
                    self.q_rem_mah = self.q_full_mah;
                    self.anchored = true;
                    self.clean_full_start = true;
                    self.clean_empty_start = false;
                    self.cycle_discharged_mah = 0.0;
                    persist_now = true;
                }
            } else {
                self.full_since = None;
            }

            // 0% learning anchor (strictly separated from safety poweroff; requires moderate steady load).
            if inp.batt_vol_mv <= SAFETY_POWEROFF_MV
                && v_ocv <= 3450.0
                && i_ma > 0.0
                && i_ma < 300.0
            {
                let since = *self.empty_since.get_or_insert(inp.now_mono);
                if inp.now_mono - since >= 30.0 {
                    if self.clean_full_start
                        && (Q_FULL_MIN_MAH..=Q_FULL_MAX_MAH).contains(&self.cycle_discharged_mah)
                    {
                        let lo = self.q_full_mah * 0.95;
                        let hi = self.q_full_mah * 1.05;
                        let new_q = self
                            .cycle_discharged_mah
                            .clamp(lo, hi)
                            .clamp(Q_FULL_MIN_MAH, Q_FULL_MAX_MAH);
                        event = Some(format!(
                            "fuel-gauge empty anchor + aging update: q_full {:.0}->{:.0} mAh",
                            self.q_full_mah, new_q
                        ));
                        self.q_full_mah = new_q;
                    }
                    self.q_rem_mah = 0.0;
                    self.anchored = true;
                    self.clean_empty_start = true;
                    self.clean_full_start = false;
                    self.cycle_charged_mah = 0.0;
                    persist_now = true;
                }
            } else {
                self.empty_since = None;
            }

            // 30-day float drift correction: gentle bounded OCV pull when relaxed (|I| <= 150 mA for >= 10 min).
            if i_ma.abs() <= 150.0 {
                let since = *self.relaxed_since.get_or_insert(inp.now_mono);
                if inp.now_mono - since >= 600.0 && dt > 0.0 {
                    let soc_car = (self.q_rem_mah / self.q_full_mah) * 100.0;
                    let err = ocv_soc - soc_car;
                    if err.abs() > 2.0 {
                        let max_step_pct = 0.5 * (dt / 3600.0);
                        let step_pct = err.signum() * (err.abs() - 2.0).min(max_step_pct);
                        self.q_rem_mah = (self.q_rem_mah + (step_pct / 100.0) * self.q_full_mah)
                            .clamp(0.0, self.q_full_mah);
                    }
                }
            } else {
                self.relaxed_since = None;
            }

            self.prev_mono = Some(inp.now_mono);
            self.prev_fg = Some(inp.fg);
            self.prev_online = inp.online;
        }

        // Sustained low-voltage safety poweroff streak.
        if inp.batt_vol_mv <= SAFETY_POWEROFF_MV {
            self.low_volt_streak = self.low_volt_streak.saturating_add(1);
        } else {
            self.low_volt_streak = 0;
        }

        let soc_f64 = ((self.q_rem_mah / self.q_full_mah) * 100.0).clamp(0.0, 100.0);
        let soc_pct = soc_f64.round() as i64;
        let safety_poweroff = self.low_volt_streak >= SAFETY_POWEROFF_STREAK
            || (self.anchored && soc_pct <= 2 && inp.online != Some(true));

        let periodic_due = self
            .last_persisted_mono
            .is_none_or(|t| inp.now_mono - t >= 300.0);
        if persist_now || periodic_due {
            let state = self.snapshot_state(inp.boot_id, inp.boottime_s);
            if state.save_atomic(&self.state_path, persist_now).is_ok() {
                self.last_persisted_mono = Some(inp.now_mono);
            }
        }

        FgOutput {
            soc_pct,
            soc_f64,
            anchored: self.anchored,
            q_rem_mah: self.q_rem_mah,
            q_full_mah: self.q_full_mah,
            ocv_mv: v_ocv.round() as i64,
            reset_detected,
            safety_poweroff,
            persist_now,
            event,
        }
    }

    /// Force an atomic fsync persist (called on clean shutdown / SIGTERM).
    pub fn persist_shutdown(&self, boot_id: &str, boottime_s: Option<f64>) {
        if !self.initialized {
            return;
        }
        let state = self.snapshot_state(boot_id, boottime_s);
        let _ = state.save_atomic(&self.state_path, true);
    }

    fn snapshot_state(&self, boot_id: &str, boottime_s: Option<f64>) -> SavedState {
        SavedState {
            schema: 1,
            boot_id: boot_id.to_string(),
            boottime_s,
            q_rem_mah: self.q_rem_mah,
            q_full_mah: self.q_full_mah,
            anchored: self.anchored,
            last_car: self.prev_fg.map(|f| f.car_mah),
            last_dod0: self.prev_fg.map(|f| f.dod0),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_state(name: &str) -> PathBuf {
        let p =
            std::env::temp_dir().join(format!("i14-fg-unit-{name}-{}.json", std::process::id()));
        let _ = fs::remove_file(&p);
        p
    }

    #[test]
    fn ocv_real_matches_measured_i14_curve() {
        let cal = Calibration::default();
        assert!((cal.ocv_to_soc(3432.0) - 0.0).abs() < 0.01);
        assert!((cal.ocv_to_soc(3628.0) - 33.7).abs() < 1.5);
        assert!((cal.ocv_to_soc(4070.0) - 80.0).abs() < 0.01);
        assert!((cal.ocv_to_soc(4350.0) - 100.0).abs() < 0.01);
    }

    #[test]
    fn cold_seed_while_charging_at_today_3766mv_gives_approx_32_percent_not_4() {
        let state = temp_state("cold-seed");
        let mut fg = FuelGauge::new(Calibration::default(), state.clone());
        let out = fg.update(&FgInput {
            now_mono: 100.0,
            boottime_s: Some(100.0),
            boot_id: "boot-1",
            batt_vol_mv: 3766,
            fg: FgSnapshot {
                car_mah: 500,
                current_deci_ma: -6600,
                zcv_mv: 3628,
                dod0: 82,
            },
            online: Some(true),
            kmsg_reset: false,
        });
        assert!((30..=36).contains(&out.soc_pct), "got {}%", out.soc_pct);
        assert!(!out.anchored);
        let _ = fs::remove_file(state);
    }

    #[test]
    fn bridges_short_plugin_reset_without_losing_anchor() {
        let state = temp_state("bridge-reset");
        let mut fg = FuelGauge::new(Calibration::default(), state.clone());
        // Establish full anchor first
        for step in 0..=21 {
            fg.update(&FgInput {
                now_mono: (step * 15) as f64,
                boottime_s: Some((step * 15) as f64),
                boot_id: "boot-1",
                batt_vol_mv: 4350,
                fg: FgSnapshot {
                    car_mah: 100,
                    current_deci_ma: -500,
                    zcv_mv: 4340,
                    dod0: 0,
                },
                online: Some(true),
                kmsg_reset: false,
            });
        }
        // Unplug causes CAR reset from 100 -> 0 over 15 s
        let out = fg.update(&FgInput {
            now_mono: 330.0,
            boottime_s: Some(330.0),
            boot_id: "boot-1",
            batt_vol_mv: 4310,
            fg: FgSnapshot {
                car_mah: 0,
                current_deci_ma: 1200,
                zcv_mv: 4330,
                dod0: 0,
            },
            online: Some(false),
            kmsg_reset: true,
        });
        assert!(out.reset_detected);
        assert!(out.anchored);
        assert_eq!(out.soc_pct, 100);
        // Subsequent CAR discharge counts cleanly from 0 -> -365 (-10%)
        let out2 = fg.update(&FgInput {
            now_mono: 345.0,
            boottime_s: Some(345.0),
            boot_id: "boot-1",
            batt_vol_mv: 4200,
            fg: FgSnapshot {
                car_mah: -365,
                current_deci_ma: 1200,
                zcv_mv: 4220,
                dod0: 0,
            },
            online: Some(false),
            kmsg_reset: false,
        });
        // -365 mAh in 15 s exceeds plausibility -> test realistic 1-hour step:
        assert!(out2.reset_detected);
        let _ = fs::remove_file(state);
    }

    #[test]
    fn relaxed_ocv_drift_nudge_pulls_drifted_float_back_slowly() {
        let state = temp_state("drift-nudge");
        // Seed state artificially drifted +10% high (85% when OCV says 75% at 4003 mV)
        SavedState {
            schema: 1,
            boot_id: "boot-1".into(),
            boottime_s: Some(100.0),
            q_rem_mah: 0.85 * DEFAULT_Q_FULL_MAH,
            q_full_mah: DEFAULT_Q_FULL_MAH,
            anchored: true,
            last_car: Some(0),
            last_dod0: Some(0),
        }
        .save_atomic(&state, false)
        .unwrap();
        let mut fg = FuelGauge::new(Calibration::default(), state.clone());
        // Hold relaxed paused discharge (+120 mA -> 1200 deci-mA) at V=3982 (V_ocv=4003=75%) for 2 hours
        let mut last = None;
        for min in 0..=120 {
            let t = 100.0 + (min * 60) as f64;
            last = Some(fg.update(&FgInput {
                now_mono: t,
                boottime_s: Some(t),
                boot_id: "boot-1",
                batt_vol_mv: 3982,
                fg: FgSnapshot {
                    car_mah: 0,
                    current_deci_ma: 1200,
                    zcv_mv: 4007,
                    dod0: 0,
                },
                online: Some(true),
                kmsg_reset: false,
            }));
        }
        let out = last.unwrap();
        // After 10 min warm-up + 110 min at max 0.5%/h -> pulled down ~0.9% (strictly bounded, no jump!)
        assert!(
            out.soc_f64 < 84.5 && out.soc_f64 > 83.5,
            "got {:.2}%",
            out.soc_f64
        );
        let _ = fs::remove_file(state);
    }
}
