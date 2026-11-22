//! The main loop around App: devices, poll, screen on/off and the power button,
//! as `Runner` in `i14ui/app.py`.

use std::fs::{File, OpenOptions};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use crate::app::{App, Page, HOLD_SECONDS};
use crate::input::{self, Keyboard, Touch, EV_KEY, EV_SYN, KEY_POWER, SYN_DROPPED, SYN_REPORT};

pub const SYSFS: &str = "/sys/class/input";

/// Opens an input device without grabbing it; makes its node from sysfs first if
/// /dev lacks it (static /dev, no devtmpfs), with monotonic timestamps.
pub fn open_device(path: &str) -> io::Result<File> {
    if !Path::new(path).exists() {
        let name = path.rsplit('/').next().unwrap_or("");
        let dev = std::fs::read_to_string(format!("{SYSFS}/{name}/dev")).unwrap_or_default();
        if let Some((ma, mi)) = dev.trim().split_once(':') {
            if let (Ok(ma), Ok(mi)) = (ma.parse(), mi.parse()) {
                let _ = i14_sys::mknod_char(path, ma, mi, 0o600);
            }
        }
    }
    let f = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)?;
    i14_sys::input::use_monotonic_clock(f.as_raw_fd());
    Ok(f)
}

struct PowerDev {
    file: File,
    dropped: bool,
}

struct TouchDev {
    file: File,
    touch: Touch,
}

struct KbdDev {
    file: File,
    kbd: Keyboard,
    path: String,
}

pub struct Runner {
    pub app: App,
    power: Vec<PowerDev>,
    touch: Vec<TouchDev>,
    kbds: Vec<KbdDev>,
    pressed: Option<f64>,
    next_scan: f64,
    last_render: f64,
    minute: Option<String>,
    sysfs: String,
}

impl Runner {
    pub fn new(app: App, power: Vec<File>, touch: Vec<File>, kbds: Vec<(File, String)>) -> Self {
        Self {
            app,
            power: power
                .into_iter()
                .map(|file| PowerDev {
                    file,
                    dropped: false,
                })
                .collect(),
            touch: touch
                .into_iter()
                .map(|file| TouchDev {
                    file,
                    touch: Touch::new(),
                })
                .collect(),
            kbds: kbds
                .into_iter()
                .map(|(file, path)| KbdDev {
                    file,
                    kbd: Keyboard::new(),
                    path,
                })
                .collect(),
            pressed: None,
            next_scan: 0.0,
            last_render: 0.0,
            minute: None,
            sysfs: SYSFS.into(),
        }
    }

    fn now(&self) -> f64 {
        self.app.cx.env.monotonic()
    }

    pub fn screen_on(&mut self, on: bool) {
        let is_on = self.app.cx.screen.is_on();
        if on && !is_on {
            self.app.lock();
            self.app.cx.last_activity = self.now();
            self.app.cx.dirty = true;
            self.app.render();
            self.app.cx.screen.power(true);
        } else if !on && is_on {
            self.app.cx.screen.power(false);
            self.app.cancel_input();
            for t in &mut self.touch {
                t.touch.cancel(); // the touch chip sleeps now: a held finger may never lift
            }
        }
    }

    fn shutdown(&mut self) {
        self.screen_on(true);
        self.app.power_action("poweroff");
    }

    fn power_events(&mut self, i: usize, data: &[u8]) {
        for ev in input::events(data) {
            if ev.kind == EV_SYN && ev.code == SYN_DROPPED {
                self.power[i].dropped = true;
                self.pressed = None;
                continue;
            }
            if self.power[i].dropped {
                if ev.kind == EV_SYN && ev.code == SYN_REPORT {
                    self.power[i].dropped = false;
                    let down = i14_sys::input::key_down(self.power[i].file.as_raw_fd(), KEY_POWER);
                    self.pressed = down.then(|| self.now());
                }
                continue;
            }
            if ev.kind != EV_KEY || ev.code != KEY_POWER {
                continue;
            }
            if ev.value == 1 {
                self.pressed = Some(ev.time);
            } else if ev.value == 0 {
                if let Some(p) = self.pressed.take() {
                    if ev.time - p >= HOLD_SECONDS {
                        self.shutdown();
                        return;
                    }
                    let on = self.app.cx.screen.is_on();
                    self.screen_on(!on);
                }
            }
        }
    }

    fn rescan_keyboards(&mut self) {
        for path in input::classify(&self.sysfs).keyboard {
            if self.kbds.iter().any(|k| k.path == path) {
                continue;
            }
            if let Ok(file) = open_device(&path) {
                self.kbds.push(KbdDev {
                    file,
                    kbd: Keyboard::new(),
                    path,
                });
            }
        }
    }

    /// Everything to wait on now, and the Wi-Fi event socket (or None).
    fn fds(&mut self) -> (Vec<i32>, Option<i32>) {
        let mut fds: Vec<i32> = self.power.iter().map(|d| d.file.as_raw_fd()).collect();
        fds.extend(self.touch.iter().map(|d| d.file.as_raw_fd()));
        fds.extend(self.kbds.iter().map(|d| d.file.as_raw_fd()));
        let cx = &mut self.app.cx;
        if let Some(fd) = cx
            .session
            .as_ref()
            .filter(|s| s.alive())
            .and_then(|s| s.fd())
        {
            fds.push(fd);
        }
        let mon = cx.wifi.monitor_fd();
        fds.extend(mon);
        if cx.bt.on() {
            fds.extend(cx.bt.fd());
        }
        (fds, mon)
    }

    fn drop_fd(&mut self, fd: i32) {
        self.power.retain(|d| d.file.as_raw_fd() != fd);
        self.touch.retain(|d| d.file.as_raw_fd() != fd);
        self.kbds.retain(|d| d.file.as_raw_fd() != fd);
    }

    fn input(&mut self, fd: i32) {
        let mut buf = [0u8; input::EVENT_SIZE * 64];
        let read = |f: &mut File, buf: &mut [u8]| f.read(buf);
        if let Some(i) = self.power.iter().position(|d| d.file.as_raw_fd() == fd) {
            match read(&mut self.power[i].file, &mut buf) {
                Ok(n) => self.power_events(i, &buf[..n]),
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                Err(_) => self.drop_fd(fd),
            }
        } else if let Some(i) = self.touch.iter().position(|d| d.file.as_raw_fd() == fd) {
            match read(&mut self.touch[i].file, &mut buf) {
                Ok(n) => {
                    // always parsed; acts only while the screen is on
                    for ev in input::events(&buf[..n]) {
                        let g = self.touch[i].touch.feed(ev.kind, ev.code, ev.value);
                        if let Some(g) = g.filter(|_| self.app.cx.screen.is_on()) {
                            self.app.touch(g);
                        }
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                Err(_) => self.drop_fd(fd),
            }
        } else if let Some(i) = self.kbds.iter().position(|d| d.file.as_raw_fd() == fd) {
            match read(&mut self.kbds[i].file, &mut buf) {
                Ok(n) => {
                    for ev in input::events(&buf[..n]) {
                        let k = self.kbds[i].kbd.feed(ev.kind, ev.code, ev.value);
                        if let Some(k) = k.filter(|_| self.app.cx.screen.is_on()) {
                            self.app.key(k);
                        }
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                Err(_) => self.drop_fd(fd),
            }
        }
    }

    pub fn loop_once(&mut self) {
        let now = self.now();
        if self.pressed.is_some_and(|p| now - p >= HOLD_SECONDS) {
            if self
                .power
                .iter()
                .any(|d| i14_sys::input::key_down(d.file.as_raw_fd(), KEY_POWER))
            {
                self.shutdown();
                return;
            }
            self.pressed = None;
        }
        if now >= self.next_scan {
            self.rescan_keyboards();
            self.next_scan = now + 5.0;
        }
        crate::term::reap();
        let (fds, mon) = self.fds();
        let mut timeout = 30.0f64;
        if self.app.cx.screen.is_on() {
            let t = self.app.timeout();
            timeout = if t == 0 {
                0.5
            } else {
                (self.app.cx.last_activity + t as f64 - now).clamp(0.05, 0.5)
            };
            if self
                .app
                .page()
                .keyboard()
                .is_some_and(|k| k.active.is_some())
            {
                timeout = 0.05;
            }
        }
        let cx = &self.app.cx;
        if self.pressed.is_some()
            || cx.wifi.attempt_active()
            || cx.wifi.scanning()
            || cx.bt.needs_tick()
        {
            timeout = timeout.min(if self.pressed.is_some() { 0.2 } else { 1.0 });
        }
        let ready = i14_sys::poll_readable(&fds, (timeout * 1000.0) as i32).unwrap_or_default();
        for fd in ready {
            let session_fd = self.app.cx.session.as_ref().and_then(|s| s.fd());
            if session_fd == Some(fd) {
                let s = self.app.cx.session.as_mut().unwrap();
                // A read error other than the end of the shell ends the session too
                // (Python would let it end the whole program).
                let alive = s.read().unwrap_or_else(|_| {
                    s.close();
                    false
                });
                if !alive || matches!(self.app.stack.last(), Some(Page::Terminal(_))) {
                    self.app.cx.dirty = true;
                }
            } else if Some(fd) == mon {
                self.app.cx.wifi.events();
            } else if self.app.cx.bt.on() && self.app.cx.bt.fd() == Some(fd) {
                self.app.cx.bt.read();
            } else {
                self.input(fd);
            }
        }
        let _ = self.app.cx.wifi.tick();
        self.app.cx.bt.step();
        if std::mem::take(&mut self.app.cx.bt.wake) {
            // a pairing code arrived: show it, whatever page is open
            self.screen_on(true);
            self.app.cx.last_activity = self.now();
            self.app.cx.dirty = true;
        }
        // published only now: waking a PIN-protected phone locks it, and the state must say so
        self.app.cx.bt.ui_locked = self.app.locked();
        self.app.cx.bt.publish();
        if self.app.cx.screen.is_on() {
            if self.app.tick() {
                self.app.cx.dirty = true;
            }
            let t = self.app.cx.env.local_time();
            let minute = format!("{:02}:{:02}", t.hour, t.minute);
            if self.minute.as_ref() != Some(&minute) {
                self.minute = Some(minute);
                self.app.cx.dirty = true;
            }
            let timeout = self.app.timeout();
            let now = self.now();
            if timeout != 0 && now - self.app.cx.last_activity > timeout as f64 {
                self.screen_on(false);
            } else if self.app.cx.dirty && now - self.last_render > 0.03 {
                // More input waiting (fast typing, a burst of output): draw once it is
                // read, or every 0.2 s. The list again: reading may have closed an fd.
                let busy = if now - self.last_render < 0.2 {
                    let fds = self.fds().0;
                    !i14_sys::poll_readable(&fds, 0)
                        .unwrap_or_default()
                        .is_empty()
                } else {
                    false
                };
                if !busy {
                    self.app.render();
                    self.last_render = self.now();
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use i14_config::Config;
    use std::io::Write;

    #[test]
    fn s7_bluetooth_state_is_published_after_waking_locks_the_phone() {
        let crate::app::tests::H { mut app, mut b } = crate::app::tests::harness();
        let dir = std::env::temp_dir().join(format!("i14-ui-runner-s7-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        app.cx.cfg = Config::load(dir.join("ui.json"));
        assert!(app.cx.cfg.set_pin(Some("1234")));
        app.cx.bt.state_path = Some(dir.join("bt.json"));
        app.cx.bt.ready_for_tests();
        app.cx.bt.set_visible(true);
        app.cx.screen.power(false); // asleep, not yet locked: waking locks it
        let mut r = Runner::new(app, Vec::new(), Vec::new(), Vec::new());
        r.sysfs = dir.to_string_lossy().into_owned();
        // a User Confirmation Request from the controller wakes the phone
        let mut ev = vec![
            4,
            crate::bt::EV_USER_CONFIRM,
            10,
            0x2B,
            0x69,
            0xD5,
            0xE5,
            0x11,
            0xD0,
        ];
        ev.extend_from_slice(&1u32.to_le_bytes());
        b.write_all(&ev).unwrap();
        r.loop_once();
        assert!(r.app.cx.screen.is_on() && r.app.locked());
        let state = std::fs::read_to_string(dir.join("bt.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&state).unwrap();
        assert_eq!(v["pending"]["locked"], true, "{state}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
