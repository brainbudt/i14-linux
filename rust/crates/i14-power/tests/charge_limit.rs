//! Port of tests/test_i14_charge_limit.py: charge policy with an inert BCCT.
//! Fixture: mock.patch.object(guard, "write_command") becomes Guard::write;
//! the set_paused mocks become Fake fields. The SIGTERM test drives
//! i14_power::serve with a stop flag here and the real binary with real
//! signals in tests/service.rs.

mod common;
use common::*;

use std::cell::Cell;
use std::path::PathBuf;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use i14_config::charge_status;
use i14_power::{ocv_percent, Guard};

struct T {
    _tmp: TempDir,
    d: PathBuf,
    b: PathBuf,
    control: Fake,
    guard: Guard,
    now: Option<Rc<Cell<f64>>>,
}

impl T {
    fn new() -> Self {
        let tmp = TempDir::new();
        let d = tmp.0.clone();
        let b = d.join("battery");
        std::fs::create_dir(&b).unwrap();
        for (k, v) in [
            ("capacity", "80"),
            ("status", "Charging"),
            ("batt_vol", "4100"),
            ("batt_temp", "250"),
        ] {
            write(b.join(k), v);
        }
        for (k, v) in [
            ("type", "2"),
            ("usb", "1"),
            ("ac", "0"),
            ("cmd", "sentinel"),
            ("activity", "1001"),
            ("gauge", "4109"),
            (
                "readback",
                "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
            ),
        ] {
            write(d.join(k), v);
        }
        let control = Fake::charge_limit();
        save(&d, true, 80);
        let mut guard = guard(&d, &control, Box::new(i14_sys::monotonic));
        // These tests cover steady policy; cold-start proof has its own regression tests.
        guard.activity = Some(1000);
        guard.activity_changed = serde_json::Number::from_f64(i14_sys::monotonic());
        T {
            _tmp: tmp,
            d,
            b,
            control,
            guard,
            now: None,
        }
    }

    fn save(&self, enabled: bool, limit: i64) {
        save(&self.d, enabled, limit);
    }

    fn step_cap(&mut self, zcv: i64, paused: bool, cap: Option<i64>) -> bool {
        write(self.d.join("gauge"), &zcv.to_string());
        if let Some(cap) = cap {
            write(self.b.join("capacity"), &cap.to_string());
        }
        let result = self.guard.tick();
        assert_eq!(self.control.stopped(), paused, "zcv={zcv}");
        result
    }

    fn step(&mut self, zcv: i64, paused: bool) -> bool {
        self.step_cap(zcv, paused, None)
    }

    fn display_sample(
        &mut self,
        zcv: i64,
        current: i64,
        advance: f64,
        paused: bool,
    ) -> serde_json::Value {
        if self.now.is_none() {
            self.now = Some(Rc::new(Cell::new(i14_sys::monotonic())));
            write(self.d.join("boot_id"), "test-boot"); // BOOT_ID is patched to this file
            self.guard.boot_id = "test-boot".into();
        }
        let now = self.now.clone().unwrap();
        now.set(now.get() + advance);
        self.guard.clock = clock(&now);
        write(self.d.join("activity"), &current.to_string());
        self.step(zcv, paused);
        status(&self.d)
    }

    fn sample(&mut self, zcv: i64, current: i64) -> serde_json::Value {
        self.display_sample(zcv, current, 60.0, true)
    }
}

fn save(d: &std::path::Path, enabled: bool, limit: i64) {
    write(
        d.join("ui.json"),
        &serde_json::json!({"charge_limit_enabled": enabled, "charge_limit": limit}).to_string(),
    );
}

const LEGACY: &str = "get_usb_current_unlimited=1,\ncmd_discharging = 1\n";

#[test]
fn hysteresis_stops_and_resumes_at_voltage_landmarks() {
    let mut t = T::new();
    for (v, p) in [
        (4108, false),
        (4109, true),
        (4067, true),
        (4004, true),
        (4003, false),
        (4067, false),
    ] {
        t.step(v, p);
    }
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn band_works_while_capacity_is_held_at_100() {
    let mut t = T::new();
    t.step_cap(4109, true, Some(100));
    t.step_cap(4003, false, Some(100));
    assert_eq!(read(t.b.join("capacity")), "100");
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn all_offered_limits() {
    let mut t = T::new();
    for (limit, stop, resume) in [
        (60, 3924, 3850),
        (70, 4003, 3924),
        (80, 4109, 4003),
        (90, 4213, 4109),
        (100, 4324, 4213),
    ] {
        t.save(true, limit);
        t.step(stop, true);
        t.step(resume, false);
    }
}

#[test]
fn disable_raise_and_reenable() {
    let mut t = T::new();
    t.step(4109, true);
    t.save(true, 90);
    t.step(4109, false);
    t.save(true, 80);
    t.step(4109, true);
    t.save(false, 80);
    t.step(4109, false);
}

#[test]
fn bad_or_missing_settings_release_without_overwriting() {
    let mut t = T::new();
    for text in [
        "{broken",
        "[]",
        r#"{"charge_limit_enabled":"yes"}"#,
        r#"{"charge_limit_enabled":true,"charge_limit":85}"#,
        r#"{"charge_limit_enabled":true,"charge_limit":80.0}"#,
        r#"{"pin":"broken","charge_limit_enabled":true}"#,
    ] {
        t.save(true, 80);
        t.step(4109, true);
        write(t.d.join("ui.json"), text);
        t.step(4109, false);
        assert_eq!(read(t.d.join("ui.json")), text);
    }
    std::fs::remove_file(t.d.join("ui.json")).unwrap();
    t.step(4109, false);
}

#[test]
fn bad_measurements_and_missing_cable_release() {
    let mut t = T::new();
    let mut cases: Vec<(PathBuf, &str)> = ["", "?", "-1", "101", "80.0"]
        .iter()
        .map(|v| (t.b.join("capacity"), *v))
        .collect();
    cases.extend([
        (t.b.join("status"), ""),
        (t.b.join("status"), "Unknown"),
        (t.b.join("batt_vol"), "3600"),
        (t.b.join("batt_temp"), "460"),
        (t.d.join("type"), "?"),
        (t.d.join("readback"), ""),
        (t.d.join("usb"), "0"),
        (t.d.join("usb"), "?"),
    ]);
    for (path, bad) in cases {
        t.step(4109, true);
        let original = read(&path);
        write(&path, bad);
        t.guard.tick();
        assert!(!t.control.stopped(), "{} {bad:?}", path.display());
        write(&path, &original);
    }
    t.step(4109, true);
    std::fs::remove_file(t.b.join("capacity")).unwrap();
    t.guard.tick();
    assert!(!t.control.stopped());
}

#[test]
fn unreadable_or_frozen_gauge_releases_without_capacity_fallback() {
    let mut t = T::new();
    t.step(4109, true);
    write(t.d.join("gauge"), "garbage");
    t.guard.tick();
    assert!(!t.control.stopped());
    std::fs::remove_file(t.d.join("gauge")).unwrap();
    t.guard.tick();
    assert!(!t.control.stopped());
    t.guard.activity_changed = None;
    t.step_cap(4109, false, Some(100));
}

#[test]
fn low_battery_guard_keeps_thresholds() {
    let mut t = T::new();
    for (status, cap, off) in [
        ("Discharging", 5, true),
        ("Discharging", 6, false),
        ("Charging", 4, false),
        ("Charging", 3, true),
        ("Full", 3, true),
        ("Cmd discharging", 5, true),
    ] {
        write(t.b.join("status"), status);
        assert_eq!(t.step_cap(3690, false, Some(cap)), off, "{status} {cap}");
    }
    write(t.b.join("capacity"), "?");
    assert!(!t.guard.tick());
}

#[test]
fn low_voltage_estimate_does_not_poweroff_above_capacity_threshold() {
    let mut t = T::new();
    assert!(!t.step_cap(3690, false, Some(50)));
}

#[test]
fn ocv_table_mapping() {
    let got: Vec<Option<i64>> = [
        None,
        Some(3000),
        Some(3400),
        Some(3712),
        Some(4003),
        Some(4016),
        Some(4101),
        Some(4109),
        Some(4324),
        Some(4400),
    ]
    .into_iter()
    .map(ocv_percent)
    .collect();
    let want = [
        None,
        Some(0),
        Some(0),
        Some(15),
        Some(70),
        Some(71),
        Some(79),
        Some(80),
        Some(100),
        Some(100),
    ];
    assert_eq!(got, want);
}

#[test]
fn display_does_not_rise_during_confirmed_pause() {
    let mut t = T::new();
    assert_eq!(t.display_sample(4113, 1500, 0.0, true)["level"], 80);
    let s = t.sample(4172, 6000);
    assert_eq!(s["level"], 80);
    assert_eq!(s["raw_level"], 86);
    assert_eq!(s["zcv_mv"], 4172);
    assert_eq!(t.sample(4158, 1400)["level"], 80);
}

#[test]
fn display_moves_gradually_with_current_direction() {
    let mut t = T::new();
    assert_eq!(t.display_sample(3962, -6000, 0.0, false)["level"], 65);
    assert_eq!(t.display_sample(3924, -6100, 60.0, false)["level"], 65);
    let s = t.display_sample(4003, -6200, 60.0, false);
    assert_eq!(
        (s["level"].as_i64(), s["raw_level"].as_i64()),
        (Some(66), Some(70))
    );
    assert_eq!(t.display_sample(4016, -6300, 10.0, false)["level"], 66);
    assert_eq!(t.display_sample(4016, -6400, 60.0, false)["level"], 67);
}

#[test]
fn display_lag_does_not_delay_hardware_resume() {
    let mut t = T::new();
    t.display_sample(4113, 1500, 0.0, true);
    assert_eq!(t.sample(4004, 6000)["level"], 79);
    let s = t.display_sample(4003, 6100, 60.0, false);
    assert_eq!(
        (s["level"].as_i64(), s["raw_level"].as_i64()),
        (Some(78), Some(70))
    );
    assert_eq!(s["paused"], false);
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn unknown_measurement_retires_display_tracking() {
    let mut t = T::new();
    t.display_sample(4113, 1500, 0.0, true);
    write(t.d.join("gauge"), "unavailable");
    t.guard.tick();
    assert!(status(&t.d)["level"].is_null());
    assert_eq!(t.display_sample(4003, 1600, 60.0, false)["level"], 70);
}

#[test]
fn fresh_restart_preserves_display_and_rate_limit() {
    let mut t = T::new();
    t.display_sample(3962, -6000, 0.0, false);
    assert_eq!(t.display_sample(4003, -6100, 60.0, false)["level"], 66);
    let now = t.now.clone().unwrap();
    now.set(now.get() + 10.0);
    t.guard = guard(&t.d, &t.control, clock(&now));
    assert_eq!(t.display_sample(4016, -6200, 0.0, false)["level"], 66);
    assert_eq!(t.display_sample(4016, -6300, 60.0, false)["level"], 67);
}

#[test]
fn legacy_migration_happens_once_with_confirmed_readback() {
    let mut t = T::new();
    write(t.d.join("cmd"), "");
    write(t.d.join("readback"), LEGACY);
    let calls = Rc::new(Cell::new(0));
    let (c, rb) = (calls.clone(), t.d.join("readback"));
    t.guard.write = Box::new(move |p, text| {
        c.set(c.get() + 1);
        i14_power::bcct::write_command(p, text)?;
        write(&rb, "get_usb_current_unlimited=0,\ncmd_discharging = 0\n");
        Ok(())
    });
    t.step(4109, true);
    t.step(4003, false);
    t.step(4109, true);
    assert_eq!(calls.get(), 1);
    assert_eq!(read(t.d.join("cmd")), "0 0\n");
}

#[test]
fn legacy_migration_failure_releases_bcct_and_reports_error() {
    let mut t = T::new();
    write(t.d.join("readback"), LEGACY);
    t.guard.write = Box::new(|_, _| Err(i14_power::Error("write failed".into())));
    t.step(4109, false);
    assert!(read(t.d.join("log")).contains("write failed"));
}

#[test]
fn failed_migration_is_not_retried_next_tick() {
    let mut t = T::new();
    write(t.d.join("readback"), LEGACY);
    let calls = count_writes(&mut t.guard);
    t.step(4109, false);
    t.step(4109, false);
    assert_eq!(calls.get(), 1);
    assert!(t.guard.migrated);
}

#[test]
fn pause_write_failure_attempts_release_and_recovers() {
    let mut t = T::new();
    t.control.0.borrow_mut().fail_pause_true = Some("pause failed".into());
    t.step(4109, false);
    t.control.0.borrow_mut().fail_pause_true = None;
    t.step(4109, true);
    t.guard.release();
    assert!(!t.control.stopped());
}

#[test]
fn steady_state_never_reenables_over_stock_safety() {
    let mut t = T::new();
    t.step(4003, false);
    let before = t.control.0.borrow().set_calls;
    t.step(4067, false);
    assert_eq!(t.control.0.borrow().set_calls, before);
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn external_legacy_override_is_visible_without_repeated_resets() {
    let mut t = T::new();
    t.step(4109, true);
    write(t.d.join("readback"), LEGACY);
    let calls = Rc::new(Cell::new(0));
    let c = calls.clone();
    t.guard.write = Box::new(move |_, _| {
        c.set(c.get() + 1);
        Ok(())
    });
    t.step(4109, false);
    assert_eq!(calls.get(), 0);
    assert!(read(t.d.join("log")).contains("outside i14-power"));
}

#[test]
fn service_stop_releases_while_waiting() {
    let mut t = T::new();
    let state = t.d.join("state");
    write(&state, "0");
    t.control.0.borrow_mut().state_path = Some(state.clone());
    let stop = Arc::new(AtomicBool::new(false));
    // The "signal": once the guard has paused, request a stop while it waits.
    let watcher = {
        let (stop, state) = (stop.clone(), state.clone());
        std::thread::spawn(move || {
            let end = std::time::Instant::now() + std::time::Duration::from_secs(5);
            while read(&state) != "1" && std::time::Instant::now() < end {
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
            let paused = read(&state) == "1";
            stop.store(true, Ordering::SeqCst);
            paused
        })
    };
    let started = std::time::Instant::now();
    let result = i14_power::serve(&mut t.guard, &|| stop.load(Ordering::SeqCst), &mut || {
        panic!("no poweroff")
    });
    t.guard.close();
    assert!(watcher.join().unwrap(), "guard never paused");
    assert_eq!(result, Ok(()));
    assert!(started.elapsed().as_secs_f64() < 5.0);
    assert_eq!(read(&state), "0");
    let snapshot = charge_status::read_status(
        &t.d.join("status.json").to_string_lossy(),
        &t.d.join("boot_id").to_string_lossy(),
        i14_sys::monotonic(),
        Some(150.0),
    );
    assert!(snapshot.is_none_or(|s| !s.paused));
}
