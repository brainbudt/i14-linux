//! Port of tests/test_i14_cpufreq.py: exercise the policy against files only.
use i14_cpufreq::{Error, Policy};
use std::fs;
use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};

static N: AtomicUsize = AtomicUsize::new(0);

struct Fx {
    root: PathBuf,
    proc: PathBuf,
    cpu: PathBuf,
    ticks: [[u64; 3]; 2], // user, idle, iowait for each core
    policy: Policy,
}

impl Drop for Fx {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

impl Fx {
    fn new() -> Self {
        let root = PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!(
            "policy-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&root);
        let proc = root.join("proc/cpufreq");
        let cpu = root.join("sys/devices/system/cpu/cpu0/cpufreq");
        fs::create_dir_all(&proc).unwrap();
        fs::create_dir_all(&cpu).unwrap();
        for (n, v) in [
            ("scaling_driver", "mt-cpufreq"),
            ("scaling_governor", "interactive"),
            (
                "scaling_available_frequencies",
                "1300000 819000 598000 442000 299000",
            ),
        ] {
            fs::write(cpu.join(n), v).unwrap();
        }
        fs::write(proc.join("cpufreq_fix_freq_in_es"), "1").unwrap();
        fs::write(proc.join("cpufreq_limited_max_freq_by_user"), "0").unwrap();
        let policy = Policy::new(&root);
        let mut fx = Fx {
            root,
            proc,
            cpu,
            ticks: [[0; 3]; 2],
            policy,
        };
        fx.screen(false);
        fx.advance((0, 0), 0);
        fx
    }

    fn screen(&self, on: bool) {
        fs::write(
            self.proc.join("cpufreq_state"),
            format!(
                "[MT_CPU_DVFS_LITTLE/0]\ndvfs_disable_by_early_suspend = {}\n",
                if on { 0 } else { 1 }
            ),
        )
        .unwrap();
    }

    fn advance(&mut self, busy: (u64, u64), waiting: u64) {
        let busy = [busy.0, busy.1];
        let mut lines = Vec::new();
        for (i, t) in self.ticks.iter_mut().enumerate() {
            t[0] += busy[i];
            t[1] += 100 - busy[i] - waiting;
            t[2] += waiting;
            lines.push(format!("cpu{i} {} 0 0 {} {} 0 0 0 0 0", t[0], t[1], t[2]));
        }
        fs::write(
            self.root.join("proc/stat"),
            format!("cpu 0 0 0 0 0 0 0 0 0 0\n{}\nintr 42\n", lines.join("\n")),
        )
        .unwrap();
    }

    fn limit(&self) -> i64 {
        fs::read_to_string(self.proc.join("cpufreq_limited_max_freq_by_user"))
            .unwrap()
            .trim()
            .parse()
            .unwrap()
    }

    fn fix(&self) -> String {
        fs::read_to_string(self.proc.join("cpufreq_fix_freq_in_es")).unwrap()
    }

    fn tick_with(&mut self, busy: (u64, u64), waiting: u64) -> Result<(), Error> {
        self.advance(busy, waiting);
        self.policy.tick()
    }

    fn tick(&mut self) {
        self.tick_with((0, 0), 0).unwrap();
    }

    fn idle(&mut self) {
        for _ in 0..6 {
            self.tick();
        }
    }
}

#[test]
fn quiet_screen_off_lowers_clock_without_changing_governor() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    for _ in 0..4 {
        f.tick();
    }
    assert_eq!(f.limit(), 0); // no cap on a single idle sample
    f.tick();
    assert_eq!(f.limit(), 299000);
    assert_eq!(
        fs::read_to_string(f.cpu.join("scaling_governor")).unwrap(),
        "interactive"
    );
}

#[test]
fn one_busy_core_releases_limit_without_averaging_it_away() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    f.tick_with((50, 0), 0).unwrap();
    assert_eq!(f.limit(), 0);
}

#[test]
fn screen_on_releases_limit_and_screen_off_can_lower_again() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    f.screen(true);
    f.tick();
    assert_eq!(f.limit(), 0);
    f.screen(false);
    f.idle();
    assert_eq!(f.limit(), 299000);
}

#[test]
fn busy_sample_resets_idle_delay_and_hysteresis_avoids_chatter() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    for _ in 0..4 {
        f.tick();
    }
    f.tick_with((10, 0), 0).unwrap();
    f.tick();
    assert_eq!(f.limit(), 0);
    f.idle();
    f.tick_with((10, 0), 0).unwrap();
    assert_eq!(f.limit(), 299000);
}

#[test]
fn iowait_is_not_cpu_work() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    for _ in 0..6 {
        f.tick_with((0, 0), 80).unwrap();
    }
    assert_eq!(f.limit(), 299000);
}

#[test]
fn new_core_or_reset_counters_release_limit() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    f.ticks = [[0; 3]; 2];
    f.tick();
    assert_eq!(f.limit(), 0);
    f.idle();
    let stat = f.root.join("proc/stat");
    let mut s = fs::read_to_string(&stat).unwrap();
    s.push_str("cpu2 0 0 0 100 0 0 0 0\n");
    fs::write(&stat, s).unwrap();
    f.policy.tick().unwrap();
    assert_eq!(f.limit(), 0);
}

#[test]
fn stop_restores_settings() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    f.policy.close().unwrap();
    assert_eq!(f.limit(), 0);
    assert_eq!(f.fix().trim(), "1");
}

#[test]
fn existing_limit_is_rejected_before_any_write() {
    let mut f = Fx::new();
    fs::write(f.proc.join("cpufreq_limited_max_freq_by_user"), "598000").unwrap();
    assert!(matches!(f.policy.start(), Err(Error::Runtime(_))));
    assert_eq!(f.limit(), 598000);
    assert_eq!(f.fix(), "1");
}

#[test]
fn external_change_is_preserved_on_stop() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    fs::write(f.proc.join("cpufreq_limited_max_freq_by_user"), "598000").unwrap();
    assert!(matches!(f.tick_with((0, 0), 0), Err(Error::Runtime(_))));
    f.policy.close().unwrap();
    assert_eq!(f.limit(), 598000);
}

#[test]
fn missing_measurements_raise_and_cleanup_releases_limit() {
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    fs::write(f.root.join("proc/stat"), "cpu 0 0 0 0\n").unwrap();
    let r = f.policy.tick();
    f.policy.close().unwrap();
    assert!(matches!(r, Err(Error::Value(_))));
    assert_eq!(f.limit(), 0);
}

#[test]
fn wrong_driver_does_not_write() {
    let mut f = Fx::new();
    fs::write(f.cpu.join("scaling_driver"), "other").unwrap();
    assert!(matches!(f.policy.start(), Err(Error::Runtime(_))));
    assert_eq!(f.fix(), "1");
}

#[test]
fn failed_release_write_keeps_the_cap_ours_so_stop_resets_it() {
    // Regression (bug in the Python original): `applied` was set before the write, so a
    // failed 299000 -> 0 write left the cap at 299000 for good and start() refused later.
    use std::os::unix::fs::PermissionsExt;
    let mut f = Fx::new();
    f.policy.start().unwrap();
    f.idle();
    assert_eq!(f.limit(), 299000);
    let cap = f.proc.join("cpufreq_limited_max_freq_by_user");
    fs::set_permissions(&cap, fs::Permissions::from_mode(0o444)).unwrap();
    let r = f.policy.limit(0);
    fs::set_permissions(&cap, fs::Permissions::from_mode(0o644)).unwrap();
    if r.is_ok() {
        eprintln!("skipped: file permissions do not apply to this user");
        return;
    }
    f.policy.close().unwrap();
    assert_eq!(f.limit(), 0);
}
