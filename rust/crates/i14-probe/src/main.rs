//! i14-probe: checks that Rust's runtime and the kernel interfaces the I14
//! software needs work on this phone's 3.18 kernel.
//!
//! Read-only towards hardware: it never writes to the framebuffer, never grabs
//! input devices and never touches charging, Wi-Fi or backlight files.
//! Prints one `PASS|FAIL <check> <detail>` line per check; exit code 1 if any check failed.

#[cfg(target_os = "linux")]
mod linux {
    use std::fs::{self, File, OpenOptions};
    use std::io::{Read, Write};
    use std::net::{TcpListener, TcpStream, UdpSocket};
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::OpenOptionsExt;
    use std::os::unix::net::UnixStream;
    use std::os::unix::process::CommandExt;
    use std::path::Path;
    use std::process::{Command, Stdio};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{mpsc, Arc, Condvar, Mutex};
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    use i14_sys::{fb, input, pty, random};

    type Check = Result<String, String>;
    type Probe = (&'static str, fn() -> Check);

    fn e<E: std::fmt::Display>(what: &str) -> impl Fn(E) -> String + '_ {
        move |err| format!("{what}: {err}")
    }

    fn files() -> Check {
        let dir = std::env::temp_dir().join(format!("i14-probe-{}", std::process::id()));
        fs::create_dir_all(&dir).map_err(e("mkdir"))?;
        let a = dir.join("a");
        let b = dir.join("b");
        let data: Vec<u8> = (0..65536u32).map(|i| (i % 251) as u8).collect();
        let result = (|| {
            let mut f = File::create(&a).map_err(e("create"))?;
            f.write_all(&data).map_err(e("write"))?;
            f.sync_all().map_err(e("fsync"))?;
            drop(f);
            fs::rename(&a, &b).map_err(e("rename"))?;
            let back = fs::read(&b).map_err(e("read"))?;
            if back != data {
                return Err("read back differs".into());
            }
            let meta = fs::metadata(&b).map_err(e("metadata"))?;
            Ok(format!(
                "64 KiB write/fsync/rename/read in {}, size {}",
                dir.display(),
                meta.len()
            ))
        })();
        let _ = fs::remove_dir_all(&dir);
        result
    }

    fn timers() -> Check {
        let wall = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(e("clock"))?;
        let t = Instant::now();
        std::thread::sleep(Duration::from_millis(50));
        let slept = t.elapsed();
        if slept < Duration::from_millis(50) || slept > Duration::from_millis(250) {
            return Err(format!("sleep(50ms) took {slept:?}"));
        }
        Ok(format!(
            "sleep(50ms) took {:.1} ms, wall clock {}",
            slept.as_secs_f64() * 1e3,
            wall.as_secs()
        ))
    }

    fn randomness() -> Check {
        let mut a = [0u8; 32];
        let n = random::getrandom(&mut a).map_err(e("getrandom syscall"))?;
        let mut b = [0u8; 32];
        File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut b))
            .map_err(e("/dev/urandom"))?;
        if a == [0; 32] || b == [0; 32] || a == b {
            return Err("random bytes look constant".into());
        }
        // std's HashMap seeds itself from the kernel too.
        let m: std::collections::HashMap<u8, u8> = (0..10).map(|i| (i, i)).collect();
        Ok(format!(
            "getrandom gave {n} bytes, /dev/urandom ok, HashMap ok ({})",
            m.len()
        ))
    }

    fn sockets() -> Check {
        let l = TcpListener::bind("127.0.0.1:0").map_err(e("tcp bind"))?;
        let addr = l.local_addr().map_err(e("local_addr"))?;
        let server = std::thread::spawn(move || -> std::io::Result<()> {
            let (mut s, _) = l.accept()?;
            let mut buf = [0u8; 4];
            s.read_exact(&mut buf)?;
            s.write_all(&buf)
        });
        let mut c = TcpStream::connect(addr).map_err(e("tcp connect"))?;
        c.set_read_timeout(Some(Duration::from_secs(2)))
            .map_err(e("timeout"))?;
        c.write_all(b"ping").map_err(e("tcp write"))?;
        let mut back = [0u8; 4];
        c.read_exact(&mut back).map_err(e("tcp read"))?;
        server
            .join()
            .map_err(|_| "server panicked".to_string())?
            .map_err(e("tcp server"))?;

        let u1 = UdpSocket::bind("127.0.0.1:0").map_err(e("udp bind"))?;
        let u2 = UdpSocket::bind("127.0.0.1:0").map_err(e("udp bind"))?;
        u2.set_read_timeout(Some(Duration::from_secs(2)))
            .map_err(e("timeout"))?;
        u1.send_to(b"dns?", u2.local_addr().unwrap())
            .map_err(e("udp send"))?;
        let mut ub = [0u8; 8];
        let (n, _) = u2.recv_from(&mut ub).map_err(e("udp recv"))?;

        let (mut x, mut y) = UnixStream::pair().map_err(e("unix pair"))?;
        x.write_all(b"u").map_err(e("unix write"))?;
        let mut yb = [0u8; 1];
        y.read_exact(&mut yb).map_err(e("unix read"))?;

        if &back != b"ping" || &ub[..n] != b"dns?" || &yb != b"u" {
            return Err("echo mismatch".into());
        }
        Ok("tcp loopback, udp loopback and unix socketpair ok".into())
    }

    fn threads() -> Check {
        let counter = Arc::new(AtomicU64::new(0));
        let pair = Arc::new((Mutex::new(0u32), Condvar::new()));
        let (tx, rx) = mpsc::channel();
        let handles: Vec<_> = (0..4)
            .map(|i| {
                let (counter, pair, tx) = (counter.clone(), pair.clone(), tx.clone());
                std::thread::spawn(move || {
                    for _ in 0..10_000 {
                        counter.fetch_add(1, Ordering::Relaxed);
                    }
                    let (m, cv) = &*pair;
                    *m.lock().unwrap() += 1;
                    cv.notify_all();
                    tx.send(i).unwrap();
                })
            })
            .collect();
        drop(tx);
        let (m, cv) = &*pair;
        let guard = cv
            .wait_timeout_while(m.lock().unwrap(), Duration::from_secs(5), |n| *n < 4)
            .map_err(|_| "poisoned".to_string())?;
        if guard.1.timed_out() {
            return Err("condvar timed out".into());
        }
        drop(guard);
        for h in handles {
            h.join().map_err(|_| "thread panicked".to_string())?;
        }
        let got: Vec<_> = rx.iter().collect();
        let total = counter.load(Ordering::Relaxed);
        if total != 40_000 || got.len() != 4 {
            return Err(format!("counter {total}, messages {}", got.len()));
        }
        Ok(format!(
            "4 threads, atomics {total}, mutex/condvar/channel ok"
        ))
    }

    fn pty_shell() -> Check {
        let (master, slave_path) = pty::open().map_err(e("openpty"))?;
        pty::set_size(master.as_raw_fd(), 40, 20).map_err(e("TIOCSWINSZ"))?;
        let slave = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&slave_path)
            .map_err(e("open slave"))?;
        let sfd = slave.as_raw_fd();
        let mut cmd = Command::new("/bin/sh");
        cmd.arg("-c").arg("stty size; tty; echo i14-probe-ok");
        cmd.stdin(Stdio::from(slave.try_clone().map_err(e("dup"))?));
        cmd.stdout(Stdio::from(slave.try_clone().map_err(e("dup"))?));
        cmd.stderr(Stdio::from(slave));
        // SAFETY: make_controlling only performs async-signal-safe syscalls.
        unsafe { cmd.pre_exec(move || pty::make_controlling(sfd)) };
        let mut child = cmd.spawn().map_err(e("spawn"))?;
        drop(cmd); // closes our copies of the slave so EOF/EIO arrives at exit
        let status = child.wait().map_err(e("wait"))?;
        let mut out = Vec::new();
        let mut m = File::from(master);
        let mut buf = [0u8; 512];
        loop {
            match m.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => out.extend_from_slice(&buf[..n]),
                Err(err) if err.raw_os_error() == Some(libc::EIO) => break, // slave closed
                Err(err) => return Err(format!("read master: {err}")),
            }
        }
        let text = String::from_utf8_lossy(&out).replace("\r\n", " | ");
        if !status.success() || !text.contains("20 40") || !text.contains("i14-probe-ok") {
            return Err(format!("status {status}, output: {}", text.trim()));
        }
        Ok(format!("{slave_path}: {}", text.trim()))
    }

    fn input_devices() -> Check {
        let mut found = Vec::new();
        let mut entries: Vec<_> = fs::read_dir("/sys/class/input")
            .map_err(e("/sys/class/input"))?
            .filter_map(|d| d.ok())
            .map(|d| d.file_name().to_string_lossy().into_owned())
            .filter(|n| n.starts_with("event"))
            .collect();
        entries.sort();
        for n in entries {
            let path = format!("/dev/input/{n}");
            if !Path::new(&path).exists() {
                return Err(format!("{path}: no device node"));
            }
            let f = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_NONBLOCK)
                .open(&path)
                .map_err(e(&path))?;
            found.push(format!("{n}={}", input::name(&f).map_err(e("EVIOCGNAME"))?));
        }
        if found.is_empty() {
            return Err("no event devices".into());
        }
        Ok(found.join(", "))
    }

    fn framebuffer() -> Check {
        let path = ["/dev/fb0", "/dev/graphics/fb0"]
            .into_iter()
            .find(|p| Path::new(p).exists());
        let Some(path) = path else {
            return Err("no framebuffer node".into());
        };
        let f = File::open(path).map_err(e(path))?;
        let v = fb::var_info(&f).map_err(e("FBIOGET_VSCREENINFO"))?;
        let (line, smem) = fb::fix_info(&f).map_err(e("FBIOGET_FSCREENINFO"))?;
        let map = fb::ReadMap::new(&f, 4096).map_err(e("mmap"))?;
        let lit = map.snapshot().iter().filter(|&&b| b != 0).count();
        Ok(format!(
            "{path} {}x{} virtual {}x{} {} bpp, line {line} B, smem {smem} B, read 4 KiB ({lit} non-zero)",
            v.xres, v.yres, v.xres_virtual, v.yres_virtual, v.bits_per_pixel
        ))
    }

    pub fn main() {
        let start = Instant::now();
        println!(
            "i14-probe {} on {}",
            env!("CARGO_PKG_VERSION"),
            i14_sys::uname().unwrap_or_default()
        );
        let checks: [Probe; 8] = [
            ("files", files),
            ("timers", timers),
            ("random", randomness),
            ("sockets", sockets),
            ("threads", threads),
            ("pty", pty_shell),
            ("input", input_devices),
            ("framebuffer", framebuffer),
        ];
        let only: Vec<String> = std::env::args().skip(1).collect();
        let mut failed = 0;
        for (name, check) in checks {
            if !only.is_empty() && !only.iter().any(|o| o == name) {
                continue;
            }
            match check() {
                Ok(detail) => println!("PASS {name} {detail}"),
                Err(detail) => {
                    failed += 1;
                    println!("FAIL {name} {detail}")
                }
            }
        }
        println!(
            "{} failed, {:.0} ms",
            failed,
            start.elapsed().as_secs_f64() * 1e3
        );
        std::process::exit(if failed > 0 { 1 } else { 0 });
    }
}

#[cfg(target_os = "linux")]
fn main() {
    linux::main()
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("i14-probe only runs on Linux");
    std::process::exit(2);
}
