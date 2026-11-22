//! A login shell in a pseudo-terminal, fed into a VT, as `i14ui/term.py`.

use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::AsRawFd;
use std::process::Child;
use std::sync::Mutex;

use i14_sys::process;
#[cfg(target_os = "linux")]
use i14_sys::pty;

use crate::vt::Vt;

pub const SHELL: [&str; 2] = ["/bin/sh", "-l"];

/// Shells that got SIGHUP but were not reaped yet.
static ENDED: Mutex<Vec<Child>> = Mutex::new(Vec::new());

/// Collects ended shells (called from the main loop).
pub fn reap() {
    if let Ok(mut ended) = ENDED.lock() {
        ended.retain_mut(|c| !matches!(c.try_wait(), Ok(Some(_)) | Err(_)));
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub struct Session {
    pub vt: Vt,
    argv: Vec<String>,
    env: Option<Vec<(String, String)>>,
    master: Option<File>,
    child: Option<Child>,
}

impl Session {
    /// `env` None: this process's environment.
    pub fn new(
        cols: usize,
        rows: usize,
        argv: Option<Vec<String>>,
        env: Option<Vec<(String, String)>>,
    ) -> Self {
        let mut s = Self {
            vt: Vt::new(cols, rows, 1000),
            argv: argv.unwrap_or_else(|| SHELL.iter().map(|s| s.to_string()).collect()),
            env,
            master: None,
            child: None,
        };
        s.start();
        s
    }

    fn start(&mut self) {
        // A shell that cannot start leaves the session ended ("tap to restart"), as a
        // failed exec does in Python (the child exits 127 and the pty closes).
        let _ = self.spawn();
    }

    #[cfg(not(target_os = "linux"))]
    fn spawn(&mut self) -> io::Result<()> {
        Err(io::ErrorKind::Unsupported.into()) // pty code is Linux-only; host tests get an ended session
    }

    #[cfg(target_os = "linux")]
    fn spawn(&mut self) -> io::Result<()> {
        use std::ffi::CString;
        use std::fs::OpenOptions;
        use std::os::unix::fs::OpenOptionsExt;
        use std::os::unix::process::CommandExt;
        use std::process::{Command, Stdio};

        let mut env: Vec<(String, String)> = self
            .env
            .clone()
            .unwrap_or_else(|| std::env::vars().collect());
        let set = |env: &mut Vec<(String, String)>, k: &str, v: String| {
            env.retain(|(key, _)| key != k);
            env.push((k.to_string(), v));
        };
        let home = env
            .iter()
            .find(|(k, _)| k == "HOME")
            .map_or("/root".to_string(), |(_, v)| v.clone());
        set(&mut env, "TERM", "vt102".into());
        set(&mut env, "HOME", home.clone());
        set(&mut env, "LANG", "C.UTF-8".into());

        let (master, slave_path) = pty::open()?;
        let slave = OpenOptions::new()
            .read(true)
            .write(true)
            // Leave the controlling terminal for the child, even when OpenRC made us a session leader.
            .custom_flags(libc::O_NOCTTY)
            .open(&slave_path)?;
        let sfd = slave.as_raw_fd();
        let dir = CString::new(home).unwrap_or_default();
        let mut cmd = Command::new(&self.argv[0]);
        cmd.args(&self.argv[1..]).env_clear().envs(env);
        cmd.stdin(Stdio::from(slave.try_clone()?))
            .stdout(Stdio::from(slave.try_clone()?))
            .stderr(Stdio::from(slave));
        // SAFETY: only async-signal-safe calls run between fork and exec.
        unsafe {
            cmd.pre_exec(move || {
                pty::make_controlling(sfd)?;
                // The app ignores SIGHUP; an ignored signal survives exec, so give the shell its default back.
                process::child_defaults(
                    &[
                        libc::SIGHUP,
                        libc::SIGTERM,
                        libc::SIGINT,
                        libc::SIGPIPE,
                        libc::SIGCHLD,
                    ],
                    &dir,
                );
                Ok(())
            })
        };
        let child = cmd.spawn()?;
        drop(cmd); // our copies of the slave: EOF/EIO reaches us when the shell ends
        let master = File::from(master);
        process::set_nonblocking(master.as_raw_fd())?;
        self.master = Some(master);
        self.child = Some(child);
        let (c, r) = (self.vt.cols, self.vt.rows);
        self.set_size(c, r);
        Ok(())
    }

    pub fn fd(&self) -> Option<i32> {
        self.master.as_ref().map(|m| m.as_raw_fd())
    }

    pub fn alive(&self) -> bool {
        self.master.is_some()
    }

    pub fn set_size(&mut self, cols: usize, rows: usize) {
        self.vt.resize(cols, rows);
        #[cfg(target_os = "linux")]
        if let Some(m) = &self.master {
            let _ = pty::set_size(m.as_raw_fd(), cols as u16, rows as u16);
        }
    }

    /// Reads what is waiting; false when the shell has ended.
    pub fn read(&mut self) -> io::Result<bool> {
        let Some(m) = &mut self.master else {
            return Ok(false);
        };
        let mut buf = vec![0u8; 65536];
        let n = match m.read(&mut buf) {
            Ok(n) => n,
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => return Ok(true),
            Err(e) if e.raw_os_error() == Some(libc::EIO) => 0,
            Err(e) => return Err(e),
        };
        if n == 0 {
            self.close();
            return Ok(false);
        }
        self.vt.feed(&buf[..n]);
        // Answers to the program's queries (cursor position, device attributes) go back into the pty.
        let replies = std::mem::take(&mut self.vt.replies);
        self.write(&replies);
        Ok(self.alive())
    }

    pub fn write(&mut self, mut data: &[u8]) {
        let Some(m) = &mut self.master else { return };
        while !data.is_empty() {
            match m.write(data) {
                Ok(n) => data = &data[n..],
                // the shell is not reading; drop the rest rather than block the screen
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => return,
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => {
                    self.close();
                    return;
                }
            }
        }
    }

    pub fn close(&mut self) {
        self.master = None;
        if let Some(child) = self.child.take() {
            let _ = process::kill(child.id(), libc::SIGHUP);
            if let Ok(mut ended) = ENDED.lock() {
                ended.push(child);
            }
            reap();
        }
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::time::{Duration, Instant};

    fn run_until_ended(s: &mut Session) {
        let end = Instant::now() + Duration::from_secs(10);
        while s.alive() && Instant::now() < end {
            s.read().unwrap();
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    #[test]
    fn the_shell_gets_hangup_back_even_though_the_app_ignores_it() {
        process::ignore(libc::SIGHUP); // as main() does
        let script = "sed -n 's/^SigIgn:[[:space:]]*//p' /proc/self/status";
        let mut s = Session::new(
            80,
            24,
            Some(vec!["/bin/sh".into(), "-c".into(), script.into()]),
            None,
        );
        run_until_ended(&mut s);
        let mask = u64::from_str_radix(s.vt.text().trim(), 16).expect("SigIgn mask");
        assert_eq!(
            mask & 1,
            0,
            "SIGHUP (bit 0) still ignored in the shell: {mask:x}"
        );
        s.close();
        reap();
    }

    #[test]
    fn real_shell_in_a_pty() {
        let env = vec![
            ("HOME".into(), "/tmp".into()),
            ("PATH".into(), "/usr/bin:/bin".into()),
            ("PS1".into(), "$ ".into()),
        ];
        let mut s = Session::new(40, 10, Some(vec!["/bin/sh".into()]), Some(env));
        s.write(b"echo hel''lo; exit\r");
        run_until_ended(&mut s);
        assert!(s.vt.text().contains("hello"), "{}", s.vt.text());
        assert!(!s.alive());
        reap();
    }

    #[test]
    fn service_session_leader_can_start_and_restart_a_shell() {
        const TEST: &str = "term::tests::service_session_leader_can_start_and_restart_a_shell";
        const CHILD: &str = "I14_UI_TEST_SESSION_LEADER";
        if std::env::var_os(CHILD).is_none() {
            // OpenRC starts the UI in its own session, with no controlling terminal.
            let status = std::process::Command::new("setsid")
                .arg(std::env::current_exe().unwrap())
                .args(["--exact", TEST, "--nocapture"])
                .env(CHILD, "1")
                .status()
                .unwrap();
            assert!(status.success(), "service-context test failed: {status}");
            return;
        }
        process::ignore(libc::SIGHUP); // same disposition as the UI
        let env = vec![
            ("HOME".into(), "/tmp".into()),
            ("PATH".into(), "/usr/bin:/bin".into()),
            ("PS1".into(), "$ ".into()),
        ];
        for _ in 0..2 {
            let mut shell = Session::new(80, 24, None, Some(env.clone()));
            assert!(shell.alive(), "login shell failed to start");
            shell.write(b"echo $((17+25)); exit\r");
            run_until_ended(&mut shell);
            assert!(!shell.alive());
            assert!(
                shell.vt.text().contains("42"),
                "expected shell output 42, got: {:?}",
                shell.vt.text()
            );
        }
    }

    #[test]
    fn queries_get_answers_through_the_pty() {
        // The program asks for the cursor position and reads the answer from its terminal.
        let script =
            "stty -icanon -echo; printf '\\033[6n'; dd bs=1 count=6 2>/dev/null | od -An -tx1";
        let mut s = Session::new(
            20,
            4,
            Some(vec!["/bin/sh".into(), "-c".into(), script.into()]),
            None,
        );
        run_until_ended(&mut s);
        let text = s.vt.text().split_whitespace().collect::<Vec<_>>().join(" ");
        assert!(
            text.contains("1b 5b 31 3b 31 52"),
            "no ESC[1;1R back: {text:?}"
        );
        assert!(s.vt.replies.is_empty());
    }

    #[test]
    fn a_missing_program_leaves_an_ended_session() {
        let s = Session::new(10, 2, Some(vec!["/nonexistent/i14-shell".into()]), None);
        assert!(!s.alive());
    }
}
