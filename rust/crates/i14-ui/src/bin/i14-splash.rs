//! i14-splash: the animated boot splash (sysinit), as the Python i14-splash.
//! Any problem with the screen or the assets: exit quietly, the boot goes on.
//! At the end the display goes off, as i14-power would have done.

use std::time::{SystemTime, UNIX_EPOCH};

use i14_ui::font::{gunzip, Font};
use i14_ui::screen::{self, BACKLIGHT, BLANK};
use i14_ui::splash::{self, Fb, Kmsg, Run};

fn env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.into())
}

fn stamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    let t = i14_sys::local_time(secs);
    let s = secs.rem_euclid(60);
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{s:02}",
        t.year, t.month, t.day, t.hour, t.minute
    )
}

fn main() {
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        screen::off_for_good();
        default_hook(info);
    }));
    i14_sys::term_signal::install();
    let fb_path = env("I14_FB", "/dev/fb0");
    let assets = env("I14_SPLASH_ASSETS", "/usr/share/i14-splash");
    let stop = env("I14_SPLASH_STOP", "/run/i14-splash.stop");
    let kmsg = env("I14_KMSG", "/dev/kmsg");
    let up = std::fs::read_to_string("/proc/uptime").unwrap_or_default();
    println!(
        "{} start at kernel uptime {} s",
        stamp(),
        up.split_whitespace().next().unwrap_or("?")
    );

    let result = (|| -> std::io::Result<()> {
        let (anim, base) = splash::load(&assets)?;
        let mut fb = Fb::open(&fb_path)?;
        screen::write(BLANK, "0"); // on already after LK; also when started by hand
        screen::write(BACKLIGHT, "120");
        // the kernel log is a bonus: without font or /dev/kmsg the animation still runs
        let (font, mut log) = match gunzip("/usr/share/consolefonts/ter-118n.psf.gz")
            .and_then(|d| Font::parse_any(&d, 128))
            .and_then(|f| Kmsg::open(&kmsg, splash::log_lines(&f, anim.h)).map(|k| (f, k)))
        {
            Ok((f, k)) => (Some(f), Some(k)),
            Err(e) => {
                eprintln!("i14-splash: no kernel log on screen: {e}");
                (None, None)
            }
        };
        let clock = || i14_sys::monotonic();
        let mut sleep = |s: f64| std::thread::sleep(std::time::Duration::from_secs_f64(s));
        let r = Run {
            stop: &stop,
            clock: &clock,
            sleep: &mut sleep,
            limit: splash::LIMIT_S,
            log: log.as_mut(),
            font: font.as_ref(),
            stamp: &stamp,
            cancel: &i14_sys::term_signal::requested,
        };
        splash::run(&mut fb, &anim, &base, r).map(|_| ())
    })();
    if let Err(e) = result {
        eprintln!("i14-splash: {e}");
    }
    // display off at the end, as i14-power does at boot
    screen::off_for_good();
    println!("{} display off, end", stamp());
}
