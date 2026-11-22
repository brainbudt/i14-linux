//! Port of tests/test_i14_soak.py::Sampler; all device paths and commands are isolated.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

const BIN: &str = env!("CARGO_BIN_EXE_i14-soak-sample");

struct Env {
    _dir: TempDir,
    d: PathBuf,
    tree: PathBuf,
    bin: PathBuf,
    log: PathBuf,
    cron: PathBuf,
    vars: BTreeMap<String, String>,
    files: Vec<(&'static str, &'static str)>,
}

struct TempDir(PathBuf);
impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn tempdir() -> TempDir {
    use std::sync::atomic::{AtomicU32, Ordering};
    static N: AtomicU32 = AtomicU32::new(0);
    let base = std::env::temp_dir().join(format!(
        "i14-soak-test-{}-{}",
        std::process::id(),
        N.fetch_add(1, Ordering::SeqCst)
    ));
    let _ = fs::remove_dir_all(&base);
    fs::create_dir_all(&base).unwrap();
    TempDir(fs::canonicalize(&base).unwrap())
}

fn python() -> String {
    let out = Command::new("sh")
        .args(["-c", "command -v python3"])
        .output()
        .unwrap();
    String::from_utf8(out.stdout).unwrap().trim().to_string()
}

impl Env {
    fn new() -> Env {
        let dir = tempdir();
        let d = dir.0.clone();
        let tree = d.join("device");
        let bin = d.join("bin");
        fs::create_dir(&bin).unwrap();
        let log = d.join("soak/samples.jsonl");
        let cron = d.join("root.crontab");
        let mut vars = BTreeMap::new();
        vars.insert("I14_SOAK_ROOT".into(), tree.display().to_string());
        vars.insert("I14_SOAK_LOG".into(), log.display().to_string());
        vars.insert("I14_SOAK_LOCK".into(), d.join("lock").display().to_string());
        vars.insert("I14_SOAK_CRONTAB".into(), cron.display().to_string());
        vars.insert(
            "PATH".into(),
            format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        );
        let files = vec![
            ("proc/sys/kernel/random/boot_id", "boot-a\n"),
            ("proc/uptime", "1234.56 890.00\n"),
            ("proc/loadavg", "0.01 0.02 0.03 1/42 123\n"),
            (
                "proc/meminfo",
                "MemTotal: 1000000 kB\nMemAvailable: 654321 kB\n",
            ),
            (
                "proc/mtk_battery_cmd/battery_cmd",
                "=> get_usb_current_unlimited=1,\ncmd_discharging = -1\n",
            ),
            ("sys/class/power_supply/battery/capacity", "79\n"),
            (
                "sys/class/power_supply/battery/status",
                "Charging \"ok\"\\test\nsecond\tline\x01\n",
            ),
            ("sys/class/power_supply/battery/batt_vol", "4100\n"),
            ("sys/class/power_supply/battery/batt_temp", "250\n"),
            ("sys/devices/platform/battery_meter/FG_Current", "-1234\n"),
            (
                "sys/devices/platform/battery_meter/FG_g_fg_dbg_bat_zcv",
                "4109\n",
            ),
            (
                "run/i14-charge-status.json",
                "{\"schema\":1,\"backend\":\"bcct\",\"paused\":true}\n",
            ),
            (
                "proc/driver/thermal/clbcct",
                "klog 0\ncurr_limit 1\nmtk-cl-bcct00 1 mA, state 1\n",
            ),
            ("sys/devices/platform/battery/Charger_Type", "2\n"),
            ("sys/class/power_supply/usb/online", "1\n"),
            ("sys/class/power_supply/ac/online", "0\n"),
            ("sys/class/leds/lcd-backlight/brightness", "0\n"),
            (
                "sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
                "299000\n",
            ),
            ("proc/cpufreq/cpufreq_limited_max_freq_by_user", "299000\n"),
            ("proc/cpufreq/cpufreq_fix_freq_in_es", "0\n"),
            (
                "var/log/i14-battery.log",
                "old\nlast \"quoted\" \\ line\t\x02\n",
            ),
        ];
        let env = Env {
            _dir: dir,
            d,
            tree,
            bin,
            log,
            cron,
            vars,
            files,
        };
        env.command("rc-service", "#!/bin/sh\n[ \"$1\" != i14-ui ]\n");
        env.command(
            "busybox",
            "#!/bin/sh\n[ \"$1\" = nslookup ] && [ \"$3\" = 127.0.0.1 ]\n",
        );
        env.command(
            "ip",
            "#!/bin/sh\nprintf \"3: wlan0 inet 192.0.2.14/24 scope global wlan0\\n\"\n",
        );
        // macOS has no timeout; emulate only its invocation here, with a real deadline.
        env.command(
            "timeout",
            &format!(
                "#!{}\nimport subprocess, sys\ntry:\n    sys.exit(subprocess.run(sys.argv[4:], timeout=float(sys.argv[3])).returncode)\nexcept subprocess.TimeoutExpired:\n    sys.exit(137)\n",
                python()
            ),
        );
        for (p, v) in &env.files {
            env.write(p, v);
        }
        // A read would block the test: the dangerous node must never be opened.
        let fifo = env.tree.join("proc/mtk_battery_cmd/current_cmd");
        assert!(Command::new("mkfifo")
            .arg(&fifo)
            .status()
            .unwrap()
            .success());
        env
    }

    fn command(&self, name: &str, source: &str) {
        let p = self.bin.join(name);
        fs::write(&p, source).unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o755)).unwrap();
    }

    fn write(&self, path: &str, value: &str) {
        let dest = self.tree.join(path);
        fs::create_dir_all(dest.parent().unwrap()).unwrap();
        fs::write(dest, value).unwrap();
    }

    fn run(&self, args: &[&str]) -> Output {
        let mut c = Command::new(BIN);
        c.args(args).env_clear();
        for (k, v) in std::env::vars() {
            if !k.starts_with("I14_") {
                c.env(k, v);
            }
        }
        c.envs(&self.vars);
        let out = c.output().unwrap();
        assert!(
            out.status.success(),
            "{:?} {}",
            out.status,
            String::from_utf8_lossy(&out.stderr)
        );
        out
    }

    fn run_sample(&self) {
        let start = Instant::now();
        self.run(&[]);
        assert!(start.elapsed() < Duration::from_secs(10));
    }

    fn sample(&self) -> serde_json_lite::Json {
        self.run_sample();
        let text = fs::read_to_string(&self.log).unwrap();
        serde_json_lite::parse(text.lines().last().unwrap())
    }
}

/// JSON assertions on the written line.
mod serde_json_lite {
    pub use serde_json::Value as Json;
    pub fn parse(s: &str) -> Json {
        serde_json::from_str(s).unwrap()
    }
}

use serde_json::Value;

fn get<'a>(v: &'a Value, k: &str) -> &'a Value {
    v.get(k).unwrap_or_else(|| panic!("missing {k}"))
}

fn int(v: &Value) -> i64 {
    match v {
        Value::Number(n) if n.is_i64() => n.as_i64().unwrap(),
        other => panic!("not int: {other:?}"),
    }
}

fn st(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        other => panic!("not str: {other:?}"),
    }
}

#[test]
fn measurements_and_escaping_without_device_writes() {
    let e = Env::new();
    let row = e.sample();
    assert_eq!(st(get(&row, "boot_id")), "boot-a");
    assert_eq!(get(&row, "uptime_seconds"), &serde_json::json!(1234.56));
    assert_eq!(int(get(&row, "capacity")), 79);
    assert_eq!(
        st(get(&row, "status")),
        "Charging \"ok\"\\test\nsecond\tline\x01"
    );
    assert_eq!(int(get(&row, "fg_current_deci_ma")), -1234);
    assert_eq!(int(get(&row, "zcv_mv")), 4109);
    let cs = serde_json_lite::parse(&st(get(&row, "charge_status")));
    assert_eq!(get(&cs, "paused"), &Value::Bool(true));
    assert!(st(get(&row, "bcct_state")).contains("curr_limit 1"));
    assert_eq!(int(get(&row, "cmd_discharging")), -1);
    assert_eq!(int(get(&row, "get_usb_current_unlimited")), 1);
    assert_eq!(int(get(&row, "batt_vol_mv")), 4100);
    assert_eq!(int(get(&row, "batt_temp_deci_c")), 250);
    assert_eq!(int(get(&row, "charger_type")), 2);
    assert_eq!(
        (int(get(&row, "usb_online")), int(get(&row, "ac_online"))),
        (1, 0)
    );
    assert_eq!(int(get(&row, "backlight_brightness")), 0);
    assert_eq!(int(get(&row, "scaling_cur_freq_khz")), 299000);
    assert_eq!(int(get(&row, "cpufreq_limited_max_freq_by_user")), 299000);
    assert_eq!(int(get(&row, "cpufreq_fix_freq_in_es")), 0);
    assert_eq!(st(get(&row, "loadavg")), "0.01 0.02 0.03 1/42 123");
    assert_eq!(int(get(&row, "mem_available_kb")), 654321);
    assert_eq!(st(get(&row, "wlan0_ipv4")), "192.0.2.14/24");
    assert_eq!(int(get(get(&row, "services"), "i14-ui")), 1);
    assert_eq!(int(get(get(&row, "services"), "i14-cpufreq")), 0);
    assert_eq!(get(get(&row, "dns"), "ok"), &Value::Bool(true));
    assert!(int(get(get(&row, "dns"), "ms")) >= 0);
    assert!(st(get(&row, "battery_log_last")).contains("last \"quoted\" \\ line\t\x02"));
    assert!(get(&row, "epoch").is_i64());
    assert!(st(get(&row, "iso")).ends_with('Z'));
    for (p, v) in &e.files {
        assert_eq!(fs::read_to_string(e.tree.join(p)).unwrap(), *v);
    }
}

#[test]
fn missing_files_and_commands_are_null() {
    let e = Env::new();
    fs::remove_dir_all(&e.tree).unwrap();
    e.command("ip", "#!/bin/sh\nexit 1\n");
    e.command("rc-service", "#!/bin/sh\nexit 127\n");
    e.command("busybox", "#!/bin/sh\nexit 1\n");
    let row = e.sample();
    for key in [
        "boot_id",
        "uptime_seconds",
        "capacity",
        "status",
        "cmd_discharging",
        "mem_available_kb",
        "wlan0_ipv4",
        "battery_log_last",
        "scaling_cur_freq_khz",
    ] {
        assert_eq!(get(&row, key), &Value::Null, "{key}");
    }
    assert_eq!(get(get(&row, "dns"), "ok"), &Value::Bool(false));
    assert_eq!(int(get(get(&row, "services"), "sshd")), 127);
}

#[test]
fn rotation_and_lock() {
    let mut e = Env::new();
    e.sample();
    let original = fs::read(&e.log).unwrap();
    e.vars.insert("I14_SOAK_MAX_BYTES".into(), "1".into());
    e.sample();
    assert_eq!(
        fs::read(format!("{}.1", e.log.display())).unwrap(),
        original
    );
    let before = fs::read(&e.log).unwrap();
    let lock = File::create(e.d.join("lock")).unwrap();
    lock.try_lock().unwrap();
    e.run_sample();
    drop(lock);
    assert_eq!(fs::read(&e.log).unwrap(), before);
}

#[test]
fn partial_log_tail_is_separated_from_next_sample() {
    let e = Env::new();
    fs::create_dir(e.log.parent().unwrap()).unwrap();
    fs::write(&e.log, "{\"partial\":").unwrap();
    let row = e.sample();
    assert_eq!(int(get(&row, "capacity")), 79);
    assert_eq!(fs::read_to_string(&e.log).unwrap().lines().count(), 2);
}

#[test]
fn flock_is_held_for_the_whole_sample() {
    // The shell test checks that fd 9 stays locked; here the lock is observed
    // from outside while a slow probe keeps the sample running.
    let e = Env::new();
    e.sample();
    e.command("rc-service", "#!/bin/sh\nsleep 0.6\n");
    let child = {
        let mut c = Command::new(BIN);
        c.envs(&e.vars);
        c.spawn().unwrap()
    };
    std::thread::sleep(Duration::from_millis(300));
    let probe = File::create(e.d.join("lock")).unwrap();
    assert!(probe.try_lock().is_err(), "lock not held during the sample");
    drop(probe);
    let out = child.wait_with_output().unwrap();
    assert!(out.status.success());
    let lock = File::create(e.d.join("lock")).unwrap();
    lock.try_lock().unwrap();
    let before = fs::read(&e.log).unwrap();
    e.run_sample();
    assert_eq!(fs::read(&e.log).unwrap(), before);
}

#[test]
fn slow_probes_run_in_parallel_and_time_out() {
    let e = Env::new();
    let py = python();
    e.command(
        "rc-service",
        &format!("#!{py}\nimport time\ntime.sleep(5)\n"),
    );
    e.command("busybox", &format!("#!{py}\nimport time\ntime.sleep(8)\n"));
    let start = Instant::now();
    let row = e.sample();
    // Services stop after 1 s, DNS after 5 s, all in parallel.
    assert!(start.elapsed() < Duration::from_secs(7));
    match get(&row, "services") {
        Value::Object(d) => assert!(d.values().all(|v| int(v) != 0)),
        _ => panic!(),
    }
    assert_eq!(get(get(&row, "dns"), "ok"), &Value::Bool(false));
}

#[test]
fn local_dns_answer_after_two_seconds_is_ok() {
    let e = Env::new();
    let py = python();
    e.command(
        "busybox",
        &format!("#!{py}\nimport sys, time\ntime.sleep(2)\nsys.exit(0 if sys.argv[1:4] == ['nslookup', 'example.com', '127.0.0.1'] else 1)\n"),
    );
    let row = e.sample();
    assert_eq!(get(get(&row, "dns"), "ok"), &Value::Bool(true));
}

#[test]
fn install_uninstall_preserve_other_cron_jobs() {
    let e = Env::new();
    let original = "# keep this\n5 * * * * echo other\n";
    fs::write(&e.cron, original).unwrap();
    e.run(&["install"]);
    e.run(&["install"]);
    let installed = fs::read_to_string(&e.cron).unwrap();
    assert_eq!(installed.matches("# i14-soak-sample").count(), 1);
    assert!(installed.contains(original));
    let exe = fs::canonicalize(BIN).unwrap();
    assert!(
        installed.contains(&exe.display().to_string()),
        "{installed}"
    );
    e.run(&["uninstall"]);
    e.run(&["uninstall"]);
    assert_eq!(fs::read_to_string(&e.cron).unwrap(), original);
    assert!(!Path::new(&e.log).exists());
}

#[test]
fn bad_argument_is_usage_error() {
    let out = Command::new(BIN).arg("bogus").output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    assert_eq!(out.stderr, b"usage: i14-soak-sample [install|uninstall]\n");
}
