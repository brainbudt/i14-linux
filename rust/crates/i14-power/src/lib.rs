//! i14-power: the BCCT charge limit and low-battery guard of the I14 phone.
//! A port of `i14ui/charging.py` (the guard) and `i14ui/bcct.py` (the backend)
//! with the same status file, log lines, error texts and order of hardware writes.

pub mod bcct;
pub mod guard;
pub mod py;

pub use bcct::{Bcct, Control, Writer};
pub use guard::{ocv_percent, Guard, Paths};
pub use py::Error;

use std::time::Duration;

/// The service loop of charging.py's `main()` after the guard is built: tick every
/// 60 s; on low battery release, record and run `poweroff`. Returns when `stop()`
/// turns true (checked after each tick and while waiting) or after `poweroff`; the
/// caller then runs `guard.close()`, Python's `finally`. Err: `poweroff` failed.
pub fn serve(
    guard: &mut Guard,
    stop: &dyn Fn() -> bool,
    poweroff: &mut dyn FnMut() -> Result<(), String>,
) -> Result<(), String> {
    loop {
        let low = guard.tick();
        if stop() {
            return Ok(()); // Python's signal handler would have ended this tick
        }
        if low {
            guard.release();
            guard.record("low battery: powering off");
            return poweroff();
        }
        // time.sleep(60), ended early by a stop request.
        let end = i14_sys::monotonic() + 60.0;
        loop {
            if stop() {
                return Ok(());
            }
            let left = end - i14_sys::monotonic();
            if left <= 0.0 {
                break;
            }
            std::thread::sleep(Duration::from_secs_f64(left.min(1.0)));
        }
    }
}
