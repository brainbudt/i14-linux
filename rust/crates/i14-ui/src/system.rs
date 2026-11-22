//! The real `Env`: clocks, battery files, `ip`, disk space and power commands.

use std::time::{SystemTime, UNIX_EPOCH};

use i14_sys::LocalTime;

use crate::app::{Env, DATA};
use crate::status::{self, Battery, Paths};

#[derive(Default)]
pub struct System {
    pub paths: Paths,
}

impl Env for System {
    fn monotonic(&self) -> f64 {
        i14_sys::monotonic()
    }

    fn local_time(&self) -> LocalTime {
        let secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_secs() as i64);
        i14_sys::local_time(secs)
    }

    fn battery(&self) -> Battery {
        self.paths.battery(self.monotonic())
    }

    fn battery_level(&self) -> String {
        self.paths.battery_level(self.monotonic())
    }

    fn addresses(&self) -> Vec<(String, String)> {
        status::addresses()
    }

    fn free(&self, path: &str) -> String {
        status::free(path)
    }

    fn uptime(&self) -> i64 {
        status::uptime()
    }

    fn run(&self, program: &str) {
        let _ = std::process::Command::new(program).status();
    }

    fn data_dir(&self) -> String {
        DATA.into()
    }
}
