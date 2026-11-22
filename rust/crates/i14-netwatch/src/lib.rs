//! Wi-Fi watchdog policy (port of rootfs/overlay/usr/sbin/i14-netwatch). Every STEP seconds,
//! layer by layer:
//!  - DHCP client gone: start it again.
//!  - not associated for 3 checks: ask wpa_supplicant to reassociate (again every 10 checks).
//!  - associated, but no IPv4 address or default route, or the gateway does not answer ping,
//!    for 3 checks: renew the DHCP lease (SIGUSR1), and reassociate when the gateway is silent.
//!  - the local DNS server (unbound, when it is in the default runlevel) does not answer its own
//!    health name for 3 checks: restart only unbound, again every 10 checks while it stays silent.
//!
//! Never reloads the Wi-Fi driver (a second wmt_loader crashes the kernel) and never reboots;
//! wpa_supplicant itself is restarted by supervise-daemon (i14-wifi). While the user has switched
//! Wi-Fi off (flag file from i14-ui) only the DNS check runs.
//!
//! The same external programs as the shell script are run with the same arguments (logger,
//! nslookup, rc-service, udhcpc, wpa_cli, ip, ping); the text filters the script piped them
//! through (grep, sed, awk, cat) and the `kill` builtin are done in-process.

use std::fs;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::Duration;

/// Where a command's output goes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Out {
    /// stdout captured (piped into a filter in the script), stderr to /dev/null.
    Capture,
    /// `>/dev/null 2>&1`. Also what lets `udhcpc -b` fork away without holding a pipe.
    Quiet,
    /// The service's own stdout/stderr (logger).
    Inherit,
}

/// Everything the policy does to the outside world, so tests can fake it.
pub trait World {
    /// `sleep "$STEP"`: false ends the loop.
    fn sleep(&mut self, step: &str) -> bool;
    /// Runs argv; returns (exit status 0, captured stdout or "").
    fn run(&mut self, argv: &[&str], out: Out) -> (bool, String);
    /// `[ -e path ]`.
    fn exists(&self, path: &str) -> bool;
    /// `cat path 2>/dev/null` (None: unreadable).
    fn read(&self, path: &str) -> Option<String>;
    /// `kill -SIG pid`: true on success.
    fn kill(&mut self, pid: u32, sig: i32) -> bool;
}

/// The script's environment knobs, with the same defaults (`${VAR:-default}`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub iface: String,
    pub pid: String,
    pub step: String,
    pub dns_want: String,
    pub off: String,
}

impl Config {
    pub fn from_lookup(get: impl Fn(&str) -> Option<String>) -> Self {
        let v = |k: &str, d: &str| get(k).filter(|s| !s.is_empty()).unwrap_or_else(|| d.into());
        Config {
            iface: v("I14_WIFI_IF", "wlan0"),
            pid: v("I14_UDHCPC_PID", "/run/i14-udhcpc-wlan0.pid"),
            step: v("I14_NETWATCH_S", "30"),
            dns_want: v("I14_UNBOUND_WANT", "/etc/runlevels/default/unbound"),
            off: v("I14_WIFI_OFF", "/mnt/data/media/0/i14-linux/wifi-off"),
        }
    }
}

/// Counters carried from one check to the next (`fail=0 last=ok dnsfail=0`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Watch {
    pub fail: u64,
    pub last: String,
    pub dnsfail: u64,
}

impl Default for Watch {
    fn default() -> Self {
        Watch {
            fail: 0,
            last: "ok".into(),
            dnsfail: 0,
        }
    }
}

/// Act at the 3rd failure, then every 10th.
fn due(n: u64) -> bool {
    n >= 3 && (n - 3).is_multiple_of(10)
}

fn log(w: &mut impl World, msg: &str) {
    w.run(&["logger", "-t", "i14-netwatch", msg], Out::Inherit);
}

/// `grep -q pat`: a line contains pat, where `.` matches any one byte.
fn grep(text: &str, pat: &str) -> bool {
    let p = pat.as_bytes();
    text.split('\n').any(|l| {
        l.as_bytes()
            .windows(p.len())
            .any(|win| win.iter().zip(p).all(|(c, q)| *q == b'.' || c == q))
    })
}

/// `$(... | sed -n 's/^wpa_state=//p')`.
fn wpa_state(text: &str) -> String {
    let v: Vec<&str> = text
        .split('\n')
        .filter_map(|l| l.strip_prefix("wpa_state="))
        .collect();
    v.join("\n").trim_end_matches('\n').to_string()
}

/// `$(... | awk '{print $3; exit}')`.
fn third_field(text: &str) -> String {
    match text.split('\n').next() {
        Some(l) if !text.is_empty() => l
            .split([' ', '\t'])
            .filter(|f| !f.is_empty())
            .nth(2)
            .unwrap_or("")
            .to_string(),
        _ => String::new(),
    }
}

/// `kill -SIG "$p" 2>/dev/null` with p from the pid file; a malformed pid fails like the builtin.
/// Unlike the shell, pid 0 counts as no process: `kill 0` would signal our own process group.
fn signal(w: &mut impl World, p: &str, sig: i32) -> bool {
    match p.parse::<i32>() {
        Ok(n) if n > 0 => w.kill(n as u32, sig),
        _ => false,
    }
}

fn read_pid(w: &impl World, path: &str) -> String {
    w.read(path)
        .unwrap_or_default()
        .trim_end_matches('\n')
        .to_string()
}

impl Watch {
    /// One pass of the loop body.
    pub fn check(&mut self, c: &Config, w: &mut impl World) {
        let i = c.iface.as_str();
        if w.exists(&c.dns_want) {
            let (_, out) = w.run(
                &["nslookup", "i14-health.invalid", "127.0.0.1"],
                Out::Capture,
            );
            if grep(&out, "192.0.2.1") {
                if self.dnsfail >= 3 {
                    log(w, "DNS server answers again");
                }
                self.dnsfail = 0;
            } else {
                self.dnsfail += 1;
                if due(self.dnsfail) {
                    log(w, "DNS server does not answer: restarting unbound");
                    w.run(&["rc-service", "unbound", "restart"], Out::Quiet);
                }
            }
        }
        if w.exists(&c.off) {
            if self.last != "off" {
                log(w, "Wi-Fi switched off by the user: not checking it");
            }
            self.fail = 0;
            self.last = "off".into();
            return;
        }
        let mut p = read_pid(w, &c.pid);
        if p.is_empty() || !signal(w, &p, 0) {
            log(w, "DHCP client not running: starting it");
            w.run(
                &["udhcpc", "-b", "-i", i, "-p", &c.pid, "-x", "hostname:i14"],
                Out::Quiet,
            );
            p = read_pid(w, &c.pid);
        }
        let state = wpa_state(&w.run(&["wpa_cli", "-i", i, "status"], Out::Capture).1);
        let gw = third_field(
            &w.run(
                &["ip", "-4", "route", "show", "default", "dev", i],
                Out::Capture,
            )
            .1,
        );
        let now = if state != "COMPLETED" {
            let s = if state.is_empty() {
                "no answer from wpa_supplicant"
            } else {
                &state
            };
            format!("not associated ({s})")
        } else if !w
            .run(&["ip", "-4", "addr", "show", "dev", i], Out::Capture)
            .1
            .contains(" inet ")
        {
            "no IPv4 address".into()
        } else if gw.is_empty() {
            "no default route".into()
        } else if !w
            .run(&["ping", "-c", "1", "-W", "3", "-I", i, &gw], Out::Quiet)
            .0
        {
            format!("gateway {gw} does not answer")
        } else {
            "ok".into()
        };
        if now == "ok" {
            if self.last != "ok" {
                log(
                    w,
                    &format!(
                        "Wi-Fi works again (after {} checks: {})",
                        self.fail, self.last
                    ),
                );
            }
            self.fail = 0;
            self.last = now;
            return;
        }
        self.fail += 1;
        if now != self.last {
            log(w, &format!("problem: {now}"));
        }
        self.last = now;
        if !due(self.fail) {
            return;
        }
        if self.last.starts_with("not associated") {
            log(w, "reassociate");
            w.run(&["wpa_cli", "-i", i, "reassociate"], Out::Quiet);
        } else if self.last.starts_with("gateway") {
            log(w, "reassociate and renew the DHCP lease");
            w.run(&["wpa_cli", "-i", i, "reassociate"], Out::Quiet);
            if !p.is_empty() {
                signal(w, &p, libc::SIGUSR1);
            }
        } else {
            log(w, "renew the DHCP lease");
            if !p.is_empty() {
                signal(w, &p, libc::SIGUSR1);
            }
        }
    }
}

/// `while sleep "$STEP"; do ...; done`
pub fn run(c: &Config, w: &mut impl World) {
    let mut s = Watch::default();
    while w.sleep(&c.step) {
        s.check(c, w);
    }
}

/// The real system: programs from PATH, real files, real signals.
pub struct Sys;

impl World for Sys {
    fn sleep(&mut self, step: &str) -> bool {
        match step.parse::<f64>() {
            Ok(s) if s.is_finite() && s >= 0.0 => {
                std::thread::sleep(Duration::from_secs_f64(s));
                true
            }
            _ => {
                eprintln!("sleep: invalid number '{step}'");
                false
            }
        }
    }

    fn run(&mut self, argv: &[&str], out: Out) -> (bool, String) {
        let mut cmd = Command::new(argv[0]);
        cmd.args(&argv[1..]);
        let r = match out {
            Out::Capture => cmd
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .output()
                .map(|o| {
                    (
                        o.status.success(),
                        String::from_utf8_lossy(&o.stdout).into_owned(),
                    )
                }),
            Out::Quiet => cmd
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map(|s| (s.success(), String::new())),
            Out::Inherit => cmd.status().map(|s| (s.success(), String::new())),
        };
        r.unwrap_or_else(|e| {
            if out == Out::Inherit {
                eprintln!("{}: {e}", argv[0]);
            }
            (false, String::new())
        })
    }

    fn exists(&self, path: &str) -> bool {
        Path::new(path).exists()
    }

    fn read(&self, path: &str) -> Option<String> {
        fs::read(path)
            .ok()
            .map(|b| String::from_utf8_lossy(&b).into_owned())
    }

    fn kill(&mut self, pid: u32, sig: i32) -> bool {
        i14_sys::process::kill(pid, sig).is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filters_match_the_shell_tools() {
        assert!(grep("Server: 127.0.0.1\nAddress: 192.0.2.1\n", "192.0.2.1"));
        assert!(grep("192x0y2z1", "192.0.2.1")); // grep's `.` is any character
        assert!(!grep("192.0.\n2.1", "192.0.2.1"));
        assert!(!grep("", "192.0.2.1"));
        assert_eq!(
            wpa_state("bssid=x\nwpa_state=COMPLETED\nip_address=1\n"),
            "COMPLETED"
        );
        assert_eq!(wpa_state("wpa_state=A\r\n"), "A\r");
        assert_eq!(wpa_state("wpa_state=A\nwpa_state=\n"), "A");
        assert_eq!(wpa_state(""), "");
        assert_eq!(
            third_field("default via 192.0.2.1 dev wlan0\nother\n"),
            "192.0.2.1"
        );
        assert_eq!(third_field("  default\tvia  10.0.0.1"), "10.0.0.1");
        assert_eq!(third_field("default via\nx y z\n"), "");
        assert_eq!(third_field(""), "");
    }

    #[test]
    fn due_at_three_then_every_ten() {
        let d: Vec<u64> = (0..40).filter(|&n| due(n)).collect();
        assert_eq!(d, [3, 13, 23, 33]);
    }

    #[test]
    fn config_defaults_and_empty_means_default() {
        let c = Config::from_lookup(|k| (k == "I14_NETWATCH_S").then(String::new));
        assert_eq!(c.iface, "wlan0");
        assert_eq!(c.pid, "/run/i14-udhcpc-wlan0.pid");
        assert_eq!(c.step, "30");
        assert_eq!(c.dns_want, "/etc/runlevels/default/unbound");
        assert_eq!(c.off, "/mnt/data/media/0/i14-linux/wifi-off");
    }
}
