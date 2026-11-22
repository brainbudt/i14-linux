//! The service binary: env-var paths, SIGTERM/SIGINT → the `finally` path
//! (release + "stopped" status, exit 0), as test_service_sigterm_releases_while_waiting
//! and the i14-power part of test_i14_hardening.py's Wiring test.

mod common;
use common::*;

use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn run_until_signal(sig: libc::c_int) {
    let tmp = TempDir::new();
    let d = &tmp.0;
    std::fs::create_dir(d.join("battery")).unwrap();
    for (k, v) in [
        ("capacity", "80"),
        ("status", "Charging"),
        ("batt_vol", "4100"),
        ("batt_temp", "250"),
    ] {
        write(d.join("battery").join(k), v);
    }
    write(
        d.join("readback"),
        "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
    );
    let mut child = Command::new(env!("CARGO_BIN_EXE_i14-battery-guard"))
        .env("I14_BATTERY", d.join("battery"))
        .env("I14_CHARGER_TYPE", d.join("type"))
        .env("I14_CURRENT_CMD", d.join("cmd"))
        .env("I14_CURRENT_READBACK", d.join("readback"))
        .env("I14_USB_ONLINE", d.join("usb"))
        .env("I14_AC_ONLINE", d.join("ac"))
        .env("I14_UI_CONFIG", d.join("ui.json"))
        .env("I14_LOG", d.join("log"))
        .env("I14_FG_ZCV", d.join("gauge"))
        .env("I14_FG_ACTIVITY", d.join("activity"))
        .env("I14_CHARGE_STATUS", d.join("status.json"))
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let end = Instant::now() + Duration::from_secs(5);
    while !d.join("status.json").exists() && Instant::now() < end {
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(d.join("status.json").exists(), "first tick did not publish");
    std::thread::sleep(Duration::from_millis(200)); // now inside the 60 s wait
    i14_sys::process::kill(child.id(), sig).unwrap();
    let end = Instant::now() + Duration::from_secs(5);
    let code = loop {
        if let Some(s) = child.try_wait().unwrap() {
            break s;
        }
        assert!(Instant::now() < end, "no clean stop within 5 s");
        std::thread::sleep(Duration::from_millis(20));
    };
    assert_eq!(code.code(), Some(0));
    let s = status(d);
    assert_eq!(s["stopped"], true);
    assert_eq!(s["paused"], false);
    assert!(!d.join("cmd").exists(), "current_cmd must not be touched");
    let log = read(d.join("log"));
    assert!(
        log.contains("ERROR charge control failed: BCCT slot 00 missing or ambiguous"),
        "{log}"
    );
}

#[test]
fn sigterm_stops_cleanly_while_waiting() {
    run_until_signal(libc::SIGTERM);
}

#[test]
fn sigint_stops_cleanly_while_waiting() {
    run_until_signal(libc::SIGINT);
}

#[test]
fn power_service_is_supervised() {
    let text = read(repo().join("rootfs/overlay/etc/init.d/i14-power"));
    assert!(text.contains("supervisor=supervise-daemon"));
    assert!(!text.contains("command_background"));
}
