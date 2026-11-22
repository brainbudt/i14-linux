//! Port of tests/test_i14_battery_policy.py: BCCT guard policy against ordinary
//! files and an inert actuator. Fixture: BOOT_ID patching becomes Paths::boot_id;
//! mocks of write_command/paused become Guard::write and Fake::fail_paused.

mod common;
use common::*;

use std::cell::Cell;
use std::path::PathBuf;
use std::rc::Rc;

use i14_config::charge_status;
use i14_power::Guard;

struct T {
    _tmp: TempDir,
    d: PathBuf,
    now: Rc<Cell<f64>>,
    control: Fake,
    guard: Guard,
}

impl T {
    fn new() -> Self {
        let tmp = TempDir::new();
        let d = tmp.0.clone();
        let b = d.join("battery");
        std::fs::create_dir(&b).unwrap();
        for (k, v) in [
            ("capacity", "100"),
            ("status", "Full"),
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
            (
                "readback",
                "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
            ),
            ("gauge", "4109"),
            ("activity", "1500"),
            ("boot_id", "boot-a"),
        ] {
            write(d.join(k), v);
        }
        write(
            d.join("ui.json"),
            r#"{"charge_limit_enabled":true,"charge_limit":80}"#,
        );
        let now = Rc::new(Cell::new(1000.0));
        let control = Fake::policy();
        let guard = guard(&d, &control, clock(&now));
        T {
            _tmp: tmp,
            d,
            now,
            control,
            guard,
        }
    }

    fn make_guard(&self) -> Guard {
        guard(&self.d, &self.control, clock(&self.now))
    }

    fn tick(&mut self, zcv: i64, activity: i64) -> bool {
        write(self.d.join("gauge"), &zcv.to_string());
        write(self.d.join("activity"), &activity.to_string());
        self.guard.tick()
    }

    fn advance(&self, s: f64) {
        self.now.set(self.now.get() + s);
    }

    fn start_live(&mut self) {
        self.tick(4109, 1500);
        self.advance(60.0);
        self.tick(4109, 1501);
    }
}

#[test]
fn no_capacity100_fallback_before_live_voltage() {
    let mut t = T::new();
    t.tick(4109, 1500);
    assert!(!t.control.stopped());
    assert_eq!(read(t.d.join("cmd")), "sentinel");
    let s = status(&t.d);
    assert!(s["level"].is_null());
    assert_eq!(s["valid"], false);
}

#[test]
fn band_uses_voltage_landmarks_without_legacy_transitions() {
    let mut t = T::new();
    t.start_live();
    assert!(t.control.stopped());
    for (zcv, expected) in [
        (4067, true),
        (4004, true),
        (4003, false),
        (4108, false),
        (4109, true),
    ] {
        t.advance(60.0);
        let a = t.now.get() as i64;
        t.tick(zcv, a);
        assert_eq!(t.control.stopped(), expected, "{zcv}");
    }
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn stale_debug_activity_releases_and_invalidates_status() {
    let mut t = T::new();
    t.start_live();
    t.advance(181.0);
    t.tick(4109, 1501);
    assert!(!t.control.stopped());
    let s = status(&t.d);
    assert!(s["level"].is_null());
    assert_eq!(s["valid"], false);
    assert!(read(t.d.join("log")).contains("stale gauge activity"));
}

#[test]
fn steady_current_changing_zcv_keeps_pause() {
    let mut t = T::new();
    t.start_live();
    for zcv in [4108, 4107, 4106, 4105] {
        t.advance(60.08);
        t.tick(zcv, 1501);
    }
    assert!(t.control.stopped());
}

#[test]
fn steady_current_changing_car_keeps_pause() {
    let mut t = T::new();
    write(t.d.join("FG_g_fg_dbg_bat_car"), "-10");
    t.start_live();
    for car in [-12, -14, -16, -18] {
        t.advance(60.08);
        write(t.d.join("FG_g_fg_dbg_bat_car"), &car.to_string());
        t.tick(4109, 1501);
    }
    assert!(t.control.stopped());
}

#[test]
fn missing_car_does_not_create_activity() {
    let mut t = T::new();
    t.start_live();
    t.advance(181.0);
    t.tick(4109, 1501);
    assert!(!t.control.stopped());
}

/// Live and paused (activity changed at 1060), a quiet tick refreshes the snapshot at 1180
/// without a change, then the guard restarts at `at`.
fn restart_after_quiet_snapshot(t: &mut T, at: f64, legacy: bool) {
    t.start_live();
    t.now.set(1180.0);
    t.tick(4109, 1501);
    if legacy {
        // status written before activity_zcv/activity_car existed
        let mut s = status(&t.d);
        let m = s.as_object_mut().unwrap();
        m.remove("activity_zcv").unwrap();
        m.remove("activity_car").unwrap();
        write(t.d.join("status.json"), &s.to_string());
    }
    t.now.set(at);
    t.guard = t.make_guard();
}

#[test]
fn restart_restores_zcv_comparison_from_legacy_status() {
    let mut t = T::new();
    restart_after_quiet_snapshot(&mut t, 1239.9, true);
    t.tick(4108, 1501); // first tick after restart: ZCV moved
    t.advance(60.08);
    t.tick(4108, 1501);
    assert!(t.control.stopped());
}

#[test]
fn restart_keeps_comparison_values_after_trust_expired() {
    let mut t = T::new();
    restart_after_quiet_snapshot(&mut t, 1240.08, false);
    t.tick(4108, 1501);
    assert!(t.control.stopped());
}

/// Expired trust plus a malformed snapshot value must not renew trust on the first tick.
fn restart_with_raw_snapshot_field(field: &str, raw: &str) -> T {
    let mut t = T::new();
    restart_after_quiet_snapshot(&mut t, 1240.08, false);
    let text = read(t.d.join("status.json"));
    let v = status(&t.d);
    let old = v[field].to_string();
    write(
        t.d.join("status.json"),
        &text
            .replacen(
                &format!("\"{field}\": {old}"),
                &format!("\"{field}\": {raw}"),
                1,
            )
            .replacen(
                &format!("\"{field}\":{old}"),
                &format!("\"{field}\":{raw}"),
                1,
            ),
    );
    assert!(read(t.d.join("status.json")).contains(raw));
    t.guard = t.make_guard();
    t
}

#[test]
fn out_of_range_snapshot_car_is_ignored() {
    let mut t = restart_with_raw_snapshot_field("activity_car", "-9223372036854775808");
    write(t.d.join("FG_g_fg_dbg_bat_car"), "-10");
    t.tick(4109, 1501);
    assert!(!t.control.stopped());
}

#[test]
fn out_of_range_snapshot_zcv_does_not_fall_back_to_zcv_mv() {
    for raw in [
        "9223372036854775808",
        "-9223372036854775809",
        "18446744073709551616",
        "4108.5",
        "\"4108\"",
    ] {
        let mut t = restart_with_raw_snapshot_field("activity_zcv", raw);
        t.tick(4108, 1501);
        assert!(!t.control.stopped(), "{raw}");
    }
}

#[test]
fn restart_inside_band_uses_fresh_status_and_hardware_pause() {
    let mut t = T::new();
    t.start_live();
    t.advance(60.0);
    t.tick(4067, 1502);
    t.guard = t.make_guard();
    t.advance(10.0);
    t.tick(4067, 1502);
    assert!(t.control.stopped());
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn previous_boot_cannot_restore_gauge_trust() {
    let mut t = T::new();
    t.start_live();
    write(t.d.join("boot_id"), "boot-b");
    t.guard = t.make_guard();
    t.tick(4109, 1501);
    assert!(!t.control.stopped());
}

#[test]
fn publishes_one_current_estimate_and_effective_pause() {
    use std::os::unix::fs::PermissionsExt;
    let mut t = T::new();
    t.start_live();
    let path = t.d.join("status.json");
    let s = charge_status::parse(&read(&path), t.now.get(), "boot-a", Some(150.0)).unwrap();
    assert_eq!(
        (
            &s.raw["schema"],
            s.raw["backend"].as_str(),
            s.level,
            s.zcv_mv
        ),
        (&serde_json::json!(1), Some("bcct"), Some(80), Some(4109))
    );
    assert!(s.paused);
    assert!(s.valid);
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o644
    );
    assert!(
        charge_status::parse(&read(&path), t.now.get() + 151.0, "boot-a", Some(150.0)).is_none()
    );
}

#[test]
fn bad_feedback_releases_without_writing_unknown_legacy_state() {
    let mut t = T::new();
    t.start_live();
    write(t.d.join("readback"), "garbage");
    t.advance(60.0);
    t.tick(4109, 1503);
    assert!(!t.control.stopped());
    assert_eq!(read(t.d.join("cmd")), "sentinel");
}

#[test]
fn failed_migration_latch_survives_clean_restart() {
    let mut t = T::new();
    write(
        t.d.join("readback"),
        "get_usb_current_unlimited=1,\ncmd_discharging = 1\n",
    );
    let writes = count_writes(&mut t.guard);
    t.guard.tick();
    assert_eq!(writes.get(), 1);
    t.guard.close();
    t.advance(151.0); // measurement expiry must not erase same-boot command custody
    t.guard = t.make_guard();
    let writes = Rc::new(Cell::new(0));
    let w = writes.clone();
    t.guard.write = Box::new(move |_, _| {
        w.set(w.get() + 1);
        Ok(())
    });
    t.guard.tick();
    assert_eq!(writes.get(), 0);
    assert!(!t.control.stopped());
}

#[test]
fn control_failure_is_visible_and_low_battery_guard_still_runs() {
    let mut t = T::new();
    t.start_live();
    write(t.d.join("battery/capacity"), "3");
    t.control.0.borrow_mut().fail_paused = Some("readback unavailable".into());
    assert!(t.tick(4109, 1503));
    t.control.0.borrow_mut().fail_paused = None;
    let s = status(&t.d);
    assert_eq!(s["valid"], false);
    assert!(s["error"]
        .as_str()
        .unwrap()
        .contains("readback unavailable"));
}
