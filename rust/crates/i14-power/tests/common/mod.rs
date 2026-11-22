//! Test helpers: temporary trees and the inert Control fakes of the Python tests.
#![allow(dead_code)]

use std::cell::{Cell, RefCell};
use std::fs;
use std::path::{Path, PathBuf};
use std::rc::Rc;
use std::sync::atomic::{AtomicU32, Ordering};

use i14_power::{Control, Error, Guard, Paths};

/// The project checkout (for the Python reference and rootfs files).
pub fn repo() -> PathBuf {
    std::env::var_os("I14_REPO").map_or_else(
        || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."),
        PathBuf::from,
    )
}

pub struct TempDir(pub PathBuf);

impl TempDir {
    pub fn new() -> Self {
        static N: AtomicU32 = AtomicU32::new(0);
        let p = std::env::temp_dir().join(format!(
            "i14-power-test-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&p);
        fs::create_dir_all(&p).unwrap();
        TempDir(p)
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

pub fn write(p: impl AsRef<Path>, text: &str) {
    fs::write(p, text).unwrap();
}

pub fn read(p: impl AsRef<Path>) -> String {
    fs::read_to_string(p).unwrap()
}

pub fn status(d: &Path) -> serde_json::Value {
    serde_json::from_str(&read(d.join("status.json"))).unwrap()
}

/// The fake Controls of test_i14_battery_policy.py (release only when stopped)
/// and test_i14_charge_limit.py (state file option, release always writes).
#[derive(Default)]
pub struct FakeState {
    pub stopped: bool,
    pub changes: Vec<bool>,
    pub state_path: Option<PathBuf>,
    pub release_always: bool,
    /// mock side_effect=OSError(text) on paused()
    pub fail_paused: Option<String>,
    /// raise on set_paused(True) (test_pause_write_failure...)
    pub fail_pause_true: Option<String>,
    pub set_calls: usize,
}

#[derive(Clone, Default)]
pub struct Fake(pub Rc<RefCell<FakeState>>);

impl Fake {
    pub fn policy() -> Self {
        Fake::default()
    }
    pub fn charge_limit() -> Self {
        let f = Fake::default();
        f.0.borrow_mut().release_always = true;
        f
    }
    pub fn stopped(&self) -> bool {
        let s = self.0.borrow();
        match &s.state_path {
            Some(p) => read(p) == "1",
            None => s.stopped,
        }
    }
}

impl Control for Fake {
    fn paused(&mut self) -> Result<bool, Error> {
        if let Some(e) = self.0.borrow().fail_paused.clone() {
            return Err(Error(e));
        }
        Ok(self.stopped())
    }
    fn set_paused(&mut self, value: bool) -> Result<(), Error> {
        let mut s = self.0.borrow_mut();
        s.set_calls += 1;
        if value {
            if let Some(e) = s.fail_pause_true.clone() {
                return Err(Error(e));
            }
        }
        s.changes.push(value);
        s.stopped = value;
        if let Some(p) = &s.state_path {
            write(p, if value { "1" } else { "0" });
        }
        Ok(())
    }
    fn release(&mut self) -> Result<(), Error> {
        let always = self.0.borrow().release_always;
        if always || self.stopped() {
            self.set_paused(false)?;
        }
        Ok(())
    }
}

/// The Python tests' common file layout under `d`.
pub fn paths(d: &Path) -> Paths {
    Paths {
        battery: d.join("battery"),
        charger_type: d.join("type"),
        command: d.join("cmd"),
        readback: d.join("readback"),
        usb: d.join("usb"),
        ac: d.join("ac"),
        settings: d.join("ui.json"),
        log: d.join("log"),
        gauge: d.join("gauge"),
        gauge_activity: d.join("activity"),
        status: d.join("status.json"),
        boot_id: d.join("boot_id"),
        drain_request: d.join("drain-request"),
    }
}

pub fn clock(now: &Rc<Cell<f64>>) -> Box<dyn Fn() -> f64> {
    let now = now.clone();
    Box::new(move || now.get())
}

pub fn guard(d: &Path, control: &Fake, clock: Box<dyn Fn() -> f64>) -> Guard {
    Guard::new(paths(d), Box::new(control.clone()), clock)
}

/// mock.patch.object(guard, "write_command", wraps=guard.write_command): counts calls.
pub fn count_writes(g: &mut Guard) -> Rc<Cell<usize>> {
    let n = Rc::new(Cell::new(0));
    let c = n.clone();
    g.write = Box::new(move |p, t| {
        c.set(c.get() + 1);
        i14_power::bcct::write_command(p, t)
    });
    n
}
