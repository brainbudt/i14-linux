//! /usr/sbin/i14-battery-guard (service i14-power): charge limit and low-battery shutdown.

use std::env;
use std::panic::{self, AssertUnwindSafe};
use std::path::PathBuf;
use std::process::{self, Command};

use i14_power::{guard, serve, Bcct, Guard, Paths};

fn path(var: &str, default: &str) -> PathBuf {
    env::var_os(var).map_or_else(|| default.into(), PathBuf::from)
}

fn main() {
    let paths = Paths {
        battery: path("I14_BATTERY", guard::BATTERY),
        charger_type: path("I14_CHARGER_TYPE", guard::TYPE),
        command: path("I14_CURRENT_CMD", guard::COMMAND),
        readback: path("I14_CURRENT_READBACK", guard::READBACK),
        usb: path("I14_USB_ONLINE", guard::USB),
        ac: path("I14_AC_ONLINE", guard::AC),
        settings: path("I14_UI_CONFIG", i14_config::PATH),
        log: path("I14_LOG", guard::LOG),
        gauge: path("I14_FG_ZCV", guard::GAUGE),
        gauge_activity: path("I14_FG_ACTIVITY", guard::GAUGE_ACTIVITY),
        status: path("I14_CHARGE_STATUS", guard::STATUS),
        boot_id: guard::BOOT_ID.into(),
        drain_request: path("I14_DRAIN_REQUEST", guard::DRAIN_REQUEST),
    };
    let mut guard = Guard::new(
        paths,
        Box::new(Bcct::default()),
        Box::new(i14_sys::monotonic),
    );
    // As in Python, the handlers are installed once the guard exists.
    i14_sys::term_signal::install(); // SIGTERM and SIGINT
    let stop = i14_sys::term_signal::requested;
    let mut poweroff = || match Command::new("poweroff").status() {
        Ok(s) if s.success() => Ok(()),
        Ok(s) => Err(format!(
            "Command '['poweroff']' returned non-zero exit status {}.",
            s.code().unwrap_or(-1)
        )),
        Err(e) => Err(format!("poweroff: {e}")),
    };
    // try/finally: close() also runs after a failed poweroff or a panic (then exit 1, like an exception).
    let result = panic::catch_unwind(AssertUnwindSafe(|| serve(&mut guard, &stop, &mut poweroff)));
    guard.close();
    match result {
        Ok(Ok(())) => {}
        Ok(Err(msg)) => {
            eprintln!("i14-battery-guard: {msg}");
            process::exit(1);
        }
        Err(_) => process::exit(1),
    }
}
