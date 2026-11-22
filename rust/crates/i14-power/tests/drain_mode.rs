//! Drain mode of the production charge guard: the request file and the decisions.
//! The same cases as tests/test_i14_drain_mode.py (Python original); differential.rs compares
//! both guards step by step in drain mode.

mod common;
use common::*;

use std::cell::{Cell, RefCell};
use std::fs;
use std::os::unix::fs::{symlink, PermissionsExt};
use std::path::{Path, PathBuf};
use std::rc::Rc;

use i14_power::guard::{drain_request, DRAIN_FLOOR_MV, DRAIN_MAX_S};
use i14_power::{serve, Guard};

fn uid() -> u32 {
    // SAFETY: getuid has no preconditions.
    unsafe { libc::getuid() }
}

fn put(p: &Path, text: &[u8], mode: u32) {
    if fs::symlink_metadata(p).is_ok() {
        fs::remove_file(p).unwrap();
    }
    fs::write(p, text).unwrap();
    fs::set_permissions(p, fs::Permissions::from_mode(mode)).unwrap();
}

fn check(p: &Path, now: Option<f64>, boot: &str, owner: u32) -> Result<Option<i64>, String> {
    drain_request(p, boot, now, owner)
}

fn err(text: &str) -> Result<Option<i64>, String> {
    Err(text.to_string())
}

// ---------- the request ----------

#[test]
fn missing_is_no_request() {
    let t = TempDir::new();
    assert_eq!(
        check(&t.0.join("r"), Some(1000.0), "boot-a", uid()),
        Ok(None)
    );
}

#[test]
fn valid_with_and_without_newline() {
    let t = TempDir::new();
    let p = t.0.join("r");
    put(&p, b"boot-a 4600\n", 0o600);
    assert_eq!(check(&p, Some(1000.0), "boot-a", uid()), Ok(Some(4600)));
    put(&p, b"boot-a 4600", 0o600);
    assert_eq!(check(&p, Some(1000.0), "boot-a", uid()), Ok(Some(4600)));
    let boot = "4257b01c-e114-476f-8ed6-5d82e8d59f21";
    put(&p, format!("{boot} 4600\n").as_bytes(), 0o600);
    assert_eq!(check(&p, Some(1000.0), boot, uid()), Ok(Some(4600)));
}

#[test]
fn other_boot_and_unknown_boot() {
    let t = TempDir::new();
    let p = t.0.join("r");
    put(&p, b"boot-b 4600\n", 0o600);
    assert_eq!(check(&p, Some(1000.0), "boot-a", uid()), err("other boot"));
    put(&p, b"boot-a 4600\n", 0o600);
    assert_eq!(check(&p, Some(1000.0), "", uid()), err("other boot"));
}

#[test]
fn expired_at_and_after_the_deadline() {
    let t = TempDir::new();
    let p = t.0.join("r");
    put(&p, b"boot-a 1000\n", 0o600);
    assert_eq!(check(&p, Some(1000.0), "boot-a", uid()), err("expired"));
    assert_eq!(check(&p, Some(999.5), "boot-a", uid()), Ok(Some(1000)));
    assert_eq!(check(&p, Some(2000.0), "boot-a", uid()), err("expired"));
}

#[test]
fn at_most_twelve_hours_ahead() {
    let t = TempDir::new();
    let p = t.0.join("r");
    put(
        &p,
        format!("boot-a {}\n", 1000 + DRAIN_MAX_S).as_bytes(),
        0o600,
    );
    assert_eq!(
        check(&p, Some(1000.0), "boot-a", uid()),
        Ok(Some(1000 + DRAIN_MAX_S))
    );
    assert_eq!(check(&p, Some(999.5), "boot-a", uid()), err("too long"));
}

#[test]
fn unreadable_clock() {
    let t = TempDir::new();
    let p = t.0.join("r");
    put(&p, b"boot-a 4600\n", 0o600);
    assert_eq!(check(&p, None, "boot-a", uid()), err("clock unavailable"));
}

#[test]
fn malformed() {
    let t = TempDir::new();
    let p = t.0.join("r");
    let long_boot = "a".repeat(65) + " 4600\n";
    let mut texts: Vec<Vec<u8>> = [
        "",
        "\n",
        "boot-a\n",
        "boot-a 12.5\n",
        "boot-a  4600\n",
        " boot-a 4600\n",
        "boot-a 4600 \n",
        "boot-a 4600\n\n",
        "boot-a -4600\n",
        "boot-a +4600\n",
        "boot-a 1234567890\n",
        "boot-a 4600\r\n",
        "boot_a 4600\n",
        "boot-a 4600\nboot-a 4600\n",
        &long_boot,
        "boot-a 4\u{660}\n",
    ]
    .iter()
    .map(|s| s.as_bytes().to_vec())
    .collect();
    let mut big = b"boot-a 4600\n".to_vec();
    big.extend(std::iter::repeat_n(b'x', 120));
    texts.push(big);
    texts.push(b"caf\xc3\xa9 4600\n".to_vec());
    for text in texts {
        put(&p, &text, 0o600);
        assert_eq!(
            check(&p, Some(1000.0), "boot-a", uid()),
            err("malformed"),
            "{text:?}"
        );
    }
}

#[test]
fn mode_and_owner() {
    let t = TempDir::new();
    let p = t.0.join("r");
    for mode in [0o644, 0o640, 0o604, 0o601, 0o610] {
        put(&p, b"boot-a 4600\n", mode);
        assert_eq!(
            check(&p, Some(1000.0), "boot-a", uid()),
            err("group or other access"),
            "{mode:o}"
        );
    }
    put(&p, b"boot-a 4600\n", 0o400);
    assert_eq!(check(&p, Some(1000.0), "boot-a", uid()), Ok(Some(4600)));
    put(&p, b"boot-a 4600\n", 0o600);
    assert_eq!(
        check(&p, Some(1000.0), "boot-a", uid() + 1),
        err("not owned by root")
    );
}

#[test]
fn symlink_directory_and_fifo() {
    let t = TempDir::new();
    let p = t.0.join("r");
    let real = t.0.join("real");
    put(&real, b"boot-a 4600\n", 0o600);
    symlink(&real, &p).unwrap();
    assert_eq!(
        check(&p, Some(1000.0), "boot-a", uid()),
        Err(format!("unreadable (errno {})", libc::ELOOP))
    );
    fs::remove_file(&p).unwrap();
    fs::create_dir(&p).unwrap();
    assert_eq!(
        check(&p, Some(1000.0), "boot-a", uid()),
        err("not a regular file")
    );
    fs::remove_dir(&p).unwrap();
    let c = std::ffi::CString::new(p.to_str().unwrap()).unwrap();
    // SAFETY: a valid NUL-terminated path.
    assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
    assert_eq!(
        check(&p, Some(1000.0), "boot-a", uid()),
        err("not a regular file")
    ); // O_NONBLOCK: no hang
}

// ---------- the guard ----------

struct T {
    tmp: TempDir,
    now: Rc<Cell<f64>>,
    up: Rc<Cell<Option<f64>>>,
    activity: i64,
    fake: Fake,
    guard: Guard,
}

impl T {
    fn new() -> Self {
        let tmp = TempDir::new();
        let d = tmp.0.clone();
        fs::create_dir(d.join("battery")).unwrap();
        for (k, v) in [
            ("capacity", "60"),
            ("status", "Charging"),
            ("batt_vol", "3900"),
            ("batt_temp", "250"),
        ] {
            write(d.join("battery").join(k), v);
        }
        for (k, v) in [
            ("type", "4"),
            ("usb", "1"),
            ("ac", "0"),
            ("cmd", "sentinel"),
            (
                "readback",
                "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
            ),
            ("gauge", "3900"),
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
        let up = Rc::new(Cell::new(Some(500.0)));
        let fake = Fake::policy();
        let guard = Self::make(&d, &fake, &now, &up);
        T {
            tmp,
            now,
            up,
            activity: 1500,
            fake,
            guard,
        }
    }

    fn make(d: &Path, fake: &Fake, now: &Rc<Cell<f64>>, up: &Rc<Cell<Option<f64>>>) -> Guard {
        let mut g = guard(d, fake, clock(now));
        let up = up.clone();
        g.uptime = Box::new(move || up.get());
        g.drain_owner = uid();
        g
    }

    fn restart(&mut self) {
        let d = self.d();
        self.guard = Self::make(&d, &self.fake, &self.now, &self.up);
    }

    fn d(&self) -> PathBuf {
        self.tmp.0.clone()
    }

    fn request(&self) -> PathBuf {
        self.d().join("drain-request")
    }

    fn ask_with(&self, ahead: i64, boot: &str, mode: u32) {
        let up = self.up.get().unwrap() as i64;
        put(
            &self.request(),
            format!("{boot} {}\n", up + ahead).as_bytes(),
            mode,
        );
    }

    fn ask(&self) {
        self.ask_with(3600, "boot-a", 0o600);
    }

    fn tick_with(&mut self, zcv: Option<i64>, battery: &[(&str, &str)]) -> bool {
        self.now.set(self.now.get() + 60.0);
        if let Some(u) = self.up.get() {
            self.up.set(Some(u + 60.0));
        }
        self.activity += 1;
        let d = self.d();
        write(d.join("activity"), &self.activity.to_string());
        if let Some(z) = zcv {
            write(d.join("gauge"), &z.to_string());
        }
        for (k, v) in battery {
            write(d.join("battery").join(k), v);
        }
        self.guard.tick()
    }

    fn tick(&mut self, zcv: i64) -> bool {
        self.tick_with(Some(zcv), &[])
    }

    fn stopped(&self) -> bool {
        self.fake.stopped()
    }

    fn status(&self) -> serde_json::Value {
        status(&self.d())
    }

    fn drain_lines(&self) -> Vec<String> {
        fs::read_to_string(self.d().join("log"))
            .unwrap_or_default()
            .lines()
            .filter(|l| l.contains("drain"))
            .map(|l| l[20..].to_string())
            .collect()
    }

    fn last_drain(&self) -> String {
        self.drain_lines().last().cloned().unwrap_or_default()
    }

    /// Live gauge, the band pause reached, then a request.
    fn start_drain(&mut self) {
        self.tick(4109);
        self.tick(4109);
        assert!(self.stopped());
        self.ask();
        self.tick_with(Some(4100), &[("status", "Discharging")]);
        assert_eq!(self.status()["drain"], true);
    }

    fn ends(&mut self, why: &str, change: impl FnOnce(&Path), battery: &[(&str, &str)]) {
        self.start_drain();
        change(&self.d());
        self.tick_with(Some(3800), battery);
        assert!(!self.stopped(), "{why}");
        assert_eq!(self.status()["drain"], false);
        assert!(
            !self.request().exists(),
            "a request the guard no longer honours is removed"
        );
        assert_eq!(self.last_drain(), format!("drain mode ended: {why}"));
    }
}

fn on_line(until: i64) -> String {
    format!("drain mode on: BCCT stays paused until uptime {until} s; low-battery poweroff stays active")
}

#[test]
fn without_request_nothing_changes() {
    let mut t = T::new();
    t.tick(4109);
    t.tick(4109);
    assert!(t.stopped());
    t.tick(4003);
    assert!(!t.stopped());
    let s = t.status();
    assert_eq!(
        (s["drain"].clone(), s["drain_until"].clone()),
        (false.into(), serde_json::Value::Null)
    );
    assert!(t.drain_lines().is_empty());
}

#[test]
fn stays_paused_below_the_band_the_reserve_and_15_percent() {
    let mut t = T::new();
    t.start_drain();
    let until = t.status()["drain_until"].as_i64().unwrap();
    for (zcv, volt) in [
        (4003, "3950"),
        (3800, "3780"),
        (3712, "3700"),
        (3690, "3650"),
        (3500, "3400"),
        (3420, "3331"),
    ] {
        let low = t.tick_with(
            Some(zcv),
            &[
                ("batt_vol", volt),
                ("capacity", "20"),
                ("status", "Discharging"),
            ],
        );
        assert!(!low);
        assert!(t.stopped(), "{zcv} {volt}");
        let s = t.status();
        assert_eq!(
            (s["paused"].clone(), s["drain"].clone()),
            (true.into(), true.into())
        );
        assert_eq!(s["drain_until"].as_i64(), Some(until));
    }
    assert_eq!(t.status()["valid"], false);
    assert_eq!(t.fake.0.borrow().changes, vec![true]);
    assert_eq!(t.drain_lines(), vec![on_line(until)]);
}

#[test]
fn a_drain_can_start_unpaused() {
    let mut t = T::new();
    t.tick(3800);
    t.tick(3800);
    assert!(!t.stopped());
    t.ask();
    t.tick(3800);
    assert!(t.stopped());
}

#[test]
fn still_powers_off_at_the_low_battery_rule() {
    let mut t = T::new();
    t.start_drain();
    let dis = |cap| {
        [
            ("batt_vol", "3400"),
            ("capacity", cap),
            ("status", "Discharging"),
        ]
    };
    assert!(!t.tick_with(Some(3420), &dis("6")));
    assert!(t.tick_with(Some(3420), &dis("5")));
    assert!(t.stopped());
    assert!(t.tick_with(Some(3420), &[("capacity", "3"), ("status", "Charging")]));
}

#[test]
fn serve_releases_then_powers_off_in_drain_mode() {
    let mut t = T::new();
    t.start_drain();
    write(t.d().join("battery/capacity"), "5");
    write(t.d().join("battery/status"), "Discharging");
    t.activity += 1;
    write(t.d().join("activity"), &t.activity.to_string());
    let calls = RefCell::new(Vec::new());
    let fake = t.fake.clone();
    let mut poweroff = || {
        calls.borrow_mut().push(fake.stopped());
        Ok(())
    };
    let stop = || false;
    serve(&mut t.guard, &stop, &mut poweroff).unwrap();
    assert_eq!(
        calls.into_inner(),
        vec![false],
        "one poweroff, after the release"
    );
    assert!(read(t.d().join("log")).contains("low battery: powering off"));
}

#[test]
fn invalid_reading_releases() {
    T::new().ends("invalid reading", |_| {}, &[("capacity", "x")]);
}

#[test]
fn unreadable_temperature_releases() {
    T::new().ends("invalid reading", |_| {}, &[("batt_temp", "451")]);
}

#[test]
fn charger_offline_releases() {
    T::new().ends("invalid reading", |d| write(d.join("usb"), "0"), &[]);
}

#[test]
fn stale_gauge_releases() {
    let mut t = T::new();
    t.start_drain();
    for _ in 0..4 {
        t.now.set(t.now.get() + 60.0);
        t.up.set(Some(t.up.get().unwrap() + 60.0));
        t.guard.tick(); // activity, ZCV and CAR unchanged
    }
    assert!(!t.stopped());
    assert!(read(t.d().join("log")).contains("stale gauge activity"));
    assert_eq!(t.last_drain(), "drain mode ended: invalid reading");
}

#[test]
fn temperature_releases_at_40_degrees() {
    let mut t = T::new();
    t.start_drain();
    t.tick_with(Some(3800), &[("batt_temp", "399")]);
    assert!(t.stopped());
    t.tick_with(Some(3800), &[("batt_temp", "400")]);
    assert!(!t.stopped());
    assert_eq!(
        t.last_drain(),
        "drain mode ended: battery temperature 40.0 C"
    );
    t.tick_with(Some(3800), &[("batt_temp", "250")]);
    assert!(!t.stopped(), "latched off: the request is gone");
}

#[test]
fn voltage_floor_releases() {
    T::new().ends(
        &format!("voltage 3330 mV at or below {DRAIN_FLOOR_MV} mV"),
        |_| {},
        &[("batt_vol", "3330")],
    );
}

#[test]
fn disabled_limit_releases() {
    T::new().ends(
        "invalid reading",
        |d| {
            write(
                d.join("ui.json"),
                r#"{"charge_limit_enabled":false,"charge_limit":80}"#,
            )
        },
        &[],
    );
}

#[test]
fn deadline_ends_drain_mode() {
    let mut t = T::new();
    t.tick(3800);
    t.tick(3800);
    t.ask_with(150, "boot-a", 0o600);
    t.tick(3800);
    t.tick(3800);
    assert!(t.stopped());
    t.tick(3800);
    assert!(!t.stopped());
    assert!(!t.request().exists());
    assert_eq!(t.last_drain(), "drain mode ended: deadline reached");
}

#[test]
fn removal_ends_drain_mode_at_the_next_tick() {
    let mut t = T::new();
    t.start_drain();
    t.tick(3800);
    fs::remove_file(t.request()).unwrap();
    t.tick(3800);
    assert!(!t.stopped());
    assert_eq!(t.last_drain(), "drain mode ended: request removed");
    t.tick(3800);
    assert_eq!(
        t.drain_lines()
            .iter()
            .filter(|l| l.contains("ended"))
            .count(),
        1,
        "logged once"
    );
}

#[test]
fn removal_above_the_resume_point_keeps_the_normal_band() {
    let mut t = T::new();
    t.start_drain();
    fs::remove_file(t.request()).unwrap();
    t.tick(4050);
    assert!(t.stopped());
    t.tick(4003);
    assert!(!t.stopped());
}

#[test]
fn clock_failure_ends_drain_mode() {
    let mut t = T::new();
    t.start_drain();
    t.up.set(None);
    t.tick(3800);
    assert!(!t.stopped());
    assert_eq!(t.last_drain(), "drain mode ended: clock unavailable");
}

#[test]
fn mode_change_ends_drain_mode() {
    let mut t = T::new();
    t.start_drain();
    fs::set_permissions(t.request(), fs::Permissions::from_mode(0o644)).unwrap();
    t.tick(3800);
    assert!(!t.stopped());
    assert_eq!(t.last_drain(), "drain mode ended: group or other access");
}

#[test]
fn control_error_releases_and_ends() {
    let mut t = T::new();
    t.start_drain();
    {
        let mut s = t.fake.0.borrow_mut();
        s.stopped = false; // someone released BCCT behind the guard's back
        s.fail_pause_true = Some("[Errno 5] pause write failed".into());
    }
    t.tick(3800);
    assert!(!t.request().exists());
    assert_eq!(t.status()["drain"], false);
    assert!(read(t.d().join("log"))
        .contains("ERROR charge control failed: [Errno 5] pause write failed"));
    assert_eq!(t.last_drain(), "drain mode ended: charge control error");
}

#[test]
fn restart_rereads_the_request() {
    let mut t = T::new();
    t.start_drain();
    let until = t.status()["drain_until"].as_i64().unwrap();
    t.restart(); // supervise-daemon respawn after a crash
    t.tick_with(Some(3700), &[("batt_vol", "3650")]);
    assert!(t.stopped());
    assert_eq!(t.status()["drain_until"].as_i64(), Some(until));
    assert_eq!(
        t.drain_lines()
            .iter()
            .filter(|l| **l == on_line(until))
            .count(),
        2
    );
}

#[test]
fn clean_stop_releases_and_keeps_the_request() {
    let mut t = T::new();
    t.start_drain();
    t.guard.close();
    assert!(!t.stopped());
    assert!(t.request().exists());
}

#[test]
fn rejected_requests_are_removed_and_logged_once() {
    let mut t = T::new();
    t.tick(3800);
    for (ahead, boot, mode, why) in [
        (3600, "boot-b", 0o600, "other boot"),
        (43300, "boot-a", 0o600, "too long"),
        (-10, "boot-a", 0o600, "expired"),
        (3600, "boot-a", 0o644, "group or other access"),
    ] {
        t.ask_with(ahead, boot, mode);
        t.tick(3800);
        assert!(!t.request().exists(), "{why}");
        assert!(!t.stopped());
        assert_eq!(t.last_drain(), format!("drain request rejected: {why}"));
        t.tick(3800);
    }
    t.ask();
    t.tick_with(Some(3800), &[("batt_temp", "410")]);
    assert_eq!(
        t.last_drain(),
        "drain request rejected: battery temperature 41.0 C"
    );
    assert!(!t.request().exists());
}

#[test]
fn removal_failure_is_logged_once() {
    let mut t = T::new();
    t.tick(3800);
    fs::create_dir(t.request()).unwrap();
    t.tick(3800);
    t.tick(3800);
    let lines = t.drain_lines();
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert!(
        lines[0].starts_with("drain request rejected: not a regular file; removal failed (errno ")
    );
}
