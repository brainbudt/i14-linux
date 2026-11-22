//! i14-cpufreq service: same lock, signals, syslog lines and exit codes as the Python.
use i14_cpufreq::Policy;
use std::fs::File;

/// LOCAL STAND-IN for a syslog wrapper i14-sys does not have yet.
/// Python: openlog("i14-cpufreq") + syslog(msg) = LOG_USER|LOG_INFO, no PID.
/// Sent without a timestamp: busybox syslogd then stamps it itself with the
/// current local time, which is what the musl timestamp would have said.
fn syslog(msg: &str) {
    let line = format!("<14>i14-cpufreq: {msg}");
    if let Ok(s) = std::os::unix::net::UnixDatagram::unbound() {
        let _ = s.send_to(line.as_bytes(), "/dev/log");
    }
}

fn stop_requested() -> bool {
    i14_sys::term_signal::requested() // SIGTERM or SIGINT
}

/// time.sleep(1), but a SIGTERM/SIGINT ends it at once (Python raises SystemExit there).
fn sleep_1s() {
    let deadline = i14_sys::monotonic() + 1.0;
    while !stop_requested() {
        let left = deadline - i14_sys::monotonic();
        if left <= 0.0 {
            break;
        }
        let _ = i14_sys::poll_readable(&[], (left * 1000.0).ceil() as i32);
    }
}

fn flush(policy: &mut Policy) {
    for line in policy.log.drain(..) {
        syslog(&line);
    }
}

fn run() -> i32 {
    i14_sys::term_signal::install();
    i14_sys::process::ignore(libc::SIGHUP);
    let lock = match File::create("/run/i14-cpufreq.lock") {
        Ok(f) => f,
        Err(e) => {
            eprintln!("OSError: {e}");
            return 1;
        }
    };
    if let Err(e) = lock.try_lock() {
        eprintln!("BlockingIOError: {e}");
        return 1;
    }
    let mut policy = Policy::new("/");
    let result = (|| -> i14_cpufreq::Result<()> {
        policy.start()?;
        syslog("screen-off CPU policy started");
        loop {
            sleep_1s();
            if stop_requested() {
                return Ok(());
            }
            let r = policy.tick();
            flush(&mut policy);
            r?;
        }
    })();
    flush(&mut policy);
    if let Err(e) = &result {
        eprintln!("{e}");
    }
    let closed = policy.close();
    flush(&mut policy);
    if let Err(e) = closed {
        eprintln!("{e}");
        return 1;
    }
    syslog("screen-off CPU policy stopped; settings restored where still owned");
    drop(lock);
    if result.is_err() {
        1
    } else {
        0
    }
}

fn main() {
    std::process::exit(run());
}
