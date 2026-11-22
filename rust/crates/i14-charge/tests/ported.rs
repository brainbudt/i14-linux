//! tests/test_i14_autoboot.py, class ChargeWait, ported with the same assertions.
//!
//! As there, the battery lives in a fake sysfs and time in a fake /proc/uptime:
//! the host's sleep moves the clock (`T.00 1.00`) and runs a per-test hook that
//! may change the battery at given (absolute) times; the uptime starts at 10.
use i14_charge::{charge_wait, Config, Host, Paths};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

const GOOD: [(&str, &str); 6] = [
    ("capacity", "20"),
    ("batt_vol", "3900"),
    ("batt_temp", "250"),
    ("status", "Charging"),
    ("usb", "1"),
    ("ac", "0"),
];

type Hook = Box<dyn FnMut(i64, &Path)>;

struct Dir(PathBuf);

impl Drop for Dir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn tempdir() -> Dir {
    static N: AtomicUsize = AtomicUsize::new(0);
    let d = std::env::temp_dir().join(format!(
        "i14-charge-ported-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir_all(&d).unwrap();
    Dir(d)
}

/// `set_cmd` / Env.set: write VALUE to a fake sysfs file.
fn set(d: &Path, key: &str, value: &str) {
    let rel = match key {
        "usb" => "ps/usb/online".to_string(),
        "ac" => "ps/ac/online".to_string(),
        k if k.contains('/') => format!("ps/{k}"),
        k => format!("ps/battery/{k}"),
    };
    fs::write(d.join(rel), format!("{value}\n")).unwrap();
}

/// Simulated seconds after which a test that has not ended fails instead of hanging.
const NEVER_ENDS: i64 = 10 + 9 * 3600;

struct TestHost {
    uptime: PathBuf,
    d: PathBuf,
    hook: Hook,
    log: Vec<String>,
    /// The fake clock itself, so a hook can break the uptime file without stopping time.
    t: i64,
}

impl Host for TestHost {
    fn sleep(&mut self, secs: u64) {
        self.t += secs as i64;
        let t = self.t;
        assert!(
            t <= NEVER_ENDS,
            "charge_wait did not end after {} simulated s",
            t - 10
        );
        fs::write(&self.uptime, format!("{t}.00 1.00\n")).unwrap();
        (self.hook)(t, &self.d);
    }
    fn log(&mut self, msg: &str) {
        self.log.push(msg.to_string());
    }
}

/// ChargeWait.wait: (rc, seconds since the start, why, log).
fn wait(
    hook: Hook,
    pre: impl Fn(&mut Config),
    battery: &[(&str, &str)],
) -> (i32, i64, String, String) {
    wait_from(|_| {}, hook, pre, battery)
}

/// As [`wait`], with `setup` run on the test directory just before the start (the
/// fake clock still starts at 10, whatever it does to the uptime file).
fn wait_from(
    setup: impl FnOnce(&Path),
    hook: Hook,
    pre: impl Fn(&mut Config),
    battery: &[(&str, &str)],
) -> (i32, i64, String, String) {
    let dir = tempdir();
    let d = dir.0.clone();
    for sub in ["ps/battery", "ps/usb", "ps/ac", "bin"] {
        fs::create_dir_all(d.join(sub)).unwrap();
    }
    for (k, v) in GOOD {
        set(&d, k, v);
    }
    for (k, v) in battery {
        set(&d, k, v);
    }
    fs::write(d.join("uptime"), "10.00 5.00\n").unwrap();
    let paths = Paths::from_env(|k| match k {
        "I14_PS" => Some(d.join("ps").to_string_lossy().into()),
        "I14_UPTIME" => Some(d.join("uptime").to_string_lossy().into()),
        "I14_PWRKEY" => Some(d.join("pwrkey").to_string_lossy().into()),
        _ => None,
    });
    let mut cfg = Config::default();
    pre(&mut cfg);
    setup(&d);
    let mut host = TestHost {
        uptime: paths.uptime.clone(),
        d: d.clone(),
        hook,
        log: Vec::new(),
        t: 10,
    };
    let out = charge_wait(&cfg, &paths, &mut host);
    let t = host.t - 10;
    (out.code, t, out.why, host.log.join("\n"))
}

fn none() -> Hook {
    Box::new(|_, _| {})
}

fn nopre(_: &mut Config) {}

#[test]
fn test_starts_after_the_minimum_and_the_hold() {
    let (rc, t, why, log) = wait(none(), nopre, &[]);
    assert_eq!((rc, why.as_str()), (0, "charged"));
    assert!(t >= 300 + 120);
    assert!(t <= 300 + 120 + 5);
    assert!(log.contains("charging: capacity=20% voltage=3900mV"));
}

#[test]
fn test_a_dip_restarts_the_hold() {
    let hook: Hook = Box::new(|t, d| {
        if t == 350 {
            set(d, "battery/batt_vol", "3700")
        }
        if t == 355 {
            set(d, "battery/batt_vol", "3900")
        }
    });
    let (rc, t, why, _) = wait(hook, nopre, &[]);
    assert_eq!((rc, why.as_str()), (0, "charged"));
    assert!(t >= 345 + 120); // $t is absolute: 350 = 340 s after the start
}

/// With these values Linux must not start by itself; a key press at 900 s ends the test.
fn no_start_until_key(pre: impl Fn(&mut Config), battery: &[(&str, &str)]) -> String {
    let hook: Hook = Box::new(|t, d| {
        if t == 910 {
            set(d, "battery/batt_vol", "3700");
            fs::write(d.join("pwrkey"), "").unwrap();
        }
    });
    let (rc, t, why, log) = wait(hook, pre, battery);
    assert_eq!((rc, why.as_str()), (0, "power key"), "{log}");
    assert!(t >= 900);
    log
}

#[test]
fn test_needs_the_charging_status() {
    no_start_until_key(nopre, &[("status", "Discharging")]);
    no_start_until_key(nopre, &[("status", "Not charging")]);
}

#[test]
fn test_full_counts_as_charging() {
    let (rc, _, why, _) = wait(none(), nopre, &[("status", "Full")]);
    assert_eq!((rc, why.as_str()), (0, "charged"));
}

#[test]
fn test_needs_capacity_voltage_and_a_safe_temperature() {
    no_start_until_key(nopre, &[("capacity", "14")]);
    no_start_until_key(nopre, &[("batt_vol", "3799")]);
    no_start_until_key(nopre, &[("batt_temp", "451")]);
    no_start_until_key(nopre, &[("batt_temp", "-50")]);
}

#[test]
fn test_no_automatic_start_leaves_only_the_power_key() {
    // /i14init sets this for the no-autoboot flag and an unconfirmed earlier start
    no_start_until_key(
        |c| c.ab_pct = 101,
        &[
            ("capacity", "100"),
            ("batt_vol", "4350"),
            ("status", "Full"),
        ],
    );
}

#[test]
fn test_wall_charger_counts_as_power() {
    let (rc, _, why, _) = wait(none(), nopre, &[("usb", "0"), ("ac", "1")]);
    assert_eq!((rc, why.as_str()), (0, "charged"));
}

#[test]
fn test_no_charger_powers_off() {
    let (rc, t, why, _) = wait(none(), nopre, &[("usb", "0"), ("ac", "0")]);
    assert_eq!(rc, 2);
    assert!(why.contains("no charger"));
    assert!(t >= 30);
    assert!(t <= 35);
}

#[test]
fn test_a_short_power_gap_does_not_power_off() {
    let hook: Hook = Box::new(|t, d| {
        if t == 100 {
            set(d, "usb/online", "0")
        }
        if t == 120 {
            set(d, "usb/online", "1")
        }
    });
    let (rc, _, why, _) = wait(hook, nopre, &[]);
    assert_eq!((rc, why.as_str()), (0, "charged"));
}

#[test]
fn test_unreadable_values_go_to_rescue() {
    let (rc, t, why, _) = wait(none(), nopre, &[("capacity", "")]);
    assert_eq!(rc, 1);
    assert!(why.contains("unreadable"));
    assert!(t >= 60);
    assert!(t <= 65);
}

#[test]
fn test_empty_status_is_unreadable_too() {
    let (rc, t, why, _) = wait(none(), nopre, &[("status", "")]);
    assert_eq!(rc, 1);
    assert!(why.contains("unreadable"));
    assert!(t <= 65);
}

#[test]
fn test_garbage_is_unreadable_too() {
    let (rc, _, _, _) = wait(none(), nopre, &[("batt_vol", "3.9V")]);
    assert_eq!(rc, 1);
}

#[test]
fn test_power_key_starts_at_once_with_enough_voltage() {
    let hook: Hook = Box::new(|t, d| {
        if t == 50 {
            fs::write(d.join("pwrkey"), "").unwrap();
        }
    });
    let (rc, t, why, _) = wait(hook, nopre, &[("capacity", "3"), ("batt_vol", "3650")]);
    assert_eq!((rc, why.as_str()), (0, "power key"));
    assert!(t <= 41); // pressed at 40 s: noticed within the next second
}

#[test]
fn test_power_key_below_the_voltage_keeps_charging() {
    let hook: Hook = Box::new(|t, d| {
        if t == 50 {
            fs::write(d.join("pwrkey"), "").unwrap();
        }
        if t == 400 {
            set(d, "battery/batt_vol", "3900")
        }
    });
    let (rc, _, why, log) = wait(hook, nopre, &[("batt_vol", "3500")]);
    assert_eq!((rc, why.as_str()), (0, "charged"));
    assert!(log.contains("power key, but 3500mV is below 3600mV"));
}

// Beyond the Python tests: the one intentional deviation. The shell never finishes this
// under a fake clock (it spins without sleeping); on the phone it ends in rescue mode once
// BADREAD_S of real time has passed, which is what the port does without the spin.
#[test]
fn key_press_while_unreadable_ends_in_rescue_without_spinning() {
    let hook: Hook = Box::new(|t, d| {
        if t == 40 {
            set(d, "capacity", "");
            fs::write(d.join("pwrkey"), "").unwrap();
        }
    });
    let (rc, t, why, _) = wait(hook, nopre, &[]);
    assert_eq!((rc, why.as_str()), (1, "battery values unreadable for 60s"));
    assert_eq!(t, 85); // bad from the good sample at 35 s (25 s) to 85 s
}

#[test]
fn key_press_while_unreadable_is_taken_once_readable_again() {
    let hook: Hook = Box::new(|t, d| {
        if t == 40 {
            set(d, "capacity", "");
            fs::write(d.join("pwrkey"), "").unwrap();
        }
        if t == 70 {
            set(d, "capacity", "20");
        }
    });
    let (rc, t, why, _) = wait(hook, nopre, &[]);
    assert_eq!((rc, why.as_str()), (0, "power key"));
    assert_eq!(t, 60);
}

// Beyond the Python tests: the overall bound of charging mode (AB_MAX_S, default 8 h). Nobody
// may be there to press a key, so values that never meet the start gates end in rescue mode
// (USB shell reachable from the Mac) instead of charging forever.
#[test]
fn gates_never_met_end_in_rescue_at_the_time_limit() {
    let (rc, t, why, log) = wait(none(), nopre, &[("capacity", "14")]);
    assert_eq!((rc, why.as_str()), (1, "charge time limit"));
    assert_eq!(t, 8 * 3600);
    assert!(log.contains("charging: capacity=14%"));
}

#[test]
fn no_automatic_start_ends_in_rescue_at_the_time_limit() {
    let (rc, t, why, _) = wait(
        none(),
        |c| {
            c.ab_pct = 101;
            c.ab_max_s = 600;
        },
        &[("capacity", "100"), ("status", "Full")],
    );
    assert_eq!((rc, why.as_str()), (1, "charge time limit"));
    assert_eq!(t, 600);
}

#[test]
fn the_time_limit_comes_after_every_other_decision_of_the_same_sample() {
    // charged at 420 s: a limit of 420 s does not take it away, one of 415 s comes first
    let (rc, t, why, _) = wait(none(), |c| c.ab_max_s = 420, &[]);
    assert_eq!((rc, t, why.as_str()), (0, 420, "charged"));
    let (rc, t, why, _) = wait(none(), |c| c.ab_max_s = 415, &[]);
    assert_eq!((rc, t, why.as_str()), (1, 415, "charge time limit"));
    // no charger for 30 s at 30 s: still power off
    let (rc, t, why, _) = wait(none(), |c| c.ab_max_s = 30, &[("usb", "0"), ("ac", "0")]);
    assert_eq!((rc, t, why.as_str()), (2, 30, "no charger for 30s"));
    // a key press with enough voltage at the limit: still a start
    let hook: Hook = Box::new(|t, d| {
        if t == 610 {
            fs::write(d.join("pwrkey"), "").unwrap();
        }
    });
    let (rc, t, why, _) = wait(hook, |c| c.ab_max_s = 600, &[("capacity", "14")]);
    assert_eq!((rc, t, why.as_str()), (0, 600, "power key"));
}

#[test]
fn the_time_limit_also_bounds_unreadable_stretches() {
    let (rc, t, why, _) = wait(none(), |c| c.ab_max_s = 30, &[("capacity", "")]);
    assert_eq!((rc, t, why.as_str()), (1, 30, "charge time limit"));
}

// The bound must not fail open: an uptime that cannot be read, does not parse, or moves
// backwards would otherwise count as 0 (as in the shell) and keep below-gate values in
// charging mode for good, without the rescue shell. Any such clock ends in rescue mode.
fn clock_breaks_at(at: i64, how: &'static str) -> Hook {
    Box::new(move |t, d| {
        if t >= at {
            match how {
                "missing" => {
                    let _ = fs::remove_file(d.join("uptime"));
                }
                "back" => {
                    if t == at {
                        fs::write(d.join("uptime"), "50.00 1.00\n").unwrap()
                    }
                }
                v => fs::write(d.join("uptime"), v).unwrap(),
            }
        }
    })
}

#[test]
fn an_unreadable_uptime_ends_in_rescue() {
    for how in [
        "missing",
        "",
        "x.5 1\n",
        "-5.00 1.00\n",
        "99999999999999999999.00 1\n",
    ] {
        let (rc, t, why, _) = wait(clock_breaks_at(100, how), nopre, &[("capacity", "14")]);
        assert_eq!((rc, t), (1, 90), "{how:?}: {why}");
        assert!(why.starts_with("uptime unreadable"), "{how:?}: {why}");
    }
}

#[test]
fn an_uptime_going_backwards_ends_in_rescue() {
    let (rc, t, why, _) = wait(clock_breaks_at(100, "back"), nopre, &[("capacity", "14")]);
    assert_eq!(
        (rc, t, why.as_str()),
        (1, 90, "uptime went backwards (95 -> 50)")
    );
}

#[test]
fn an_unreadable_uptime_at_the_start_ends_in_rescue() {
    for start in [None, Some(""), Some("abc 1\n")] {
        let setup = |d: &Path| match start {
            None => fs::remove_file(d.join("uptime")).unwrap(),
            Some(v) => fs::write(d.join("uptime"), v).unwrap(),
        };
        let (rc, t, why, _) = wait_from(setup, none(), nopre, &[]);
        assert_eq!((rc, t), (1, 0), "{start:?}: {why}");
        assert!(why.starts_with("uptime unreadable"), "{start:?}: {why}");
    }
}

#[test]
fn an_unreadable_uptime_also_ends_unreadable_values_in_rescue() {
    // the first timeout check: bad += dt never reached BADREAD_S with a clock stuck at 0
    let (rc, t, why, _) = wait(clock_breaks_at(30, "x\n"), nopre, &[("capacity", "")]);
    assert_eq!((rc, t), (1, 20), "{why}");
    assert!(why.starts_with("uptime unreadable"), "{why}");
}

#[test]
fn an_uptime_standing_still_is_not_backwards() {
    // a key press before the start makes the first sample come without a sleep, at t0
    let setup = |d: &Path| fs::write(d.join("pwrkey"), "").unwrap();
    let hook: Hook = Box::new(|t, d| {
        if t == 400 {
            set(d, "batt_vol", "3900")
        }
    });
    let (rc, _, why, log) = wait_from(setup, hook, nopre, &[("batt_vol", "3500")]);
    assert_eq!((rc, why.as_str()), (0, "charged"), "{log}");
    assert!(
        log.contains("power key, but 3500mV is below 3600mV"),
        "{log}"
    );
}
