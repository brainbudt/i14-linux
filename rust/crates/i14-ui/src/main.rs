//! i14-ui: the on-device interface (status, terminal, Wi-Fi, Bluetooth, settings).
//!
//! Power button: short press = screen on/off; hold 3 s = power off.

use i14_config::Config;
use i14_ui::app::{App, Fonts};
use i14_ui::bt::Bluetooth;
use i14_ui::font::Font;
use i14_ui::input::classify;
use i14_ui::runner::{open_device, Runner, SYSFS};
use i14_ui::screen::{self, Screen};
use i14_ui::system::System;
use i14_ui::wifi::Wifi;

const FONTS: &str = "/usr/share/consolefonts/";

fn fail(msg: String) -> ! {
    eprintln!("i14-ui: {msg}");
    std::process::exit(1);
}

fn main() {
    // Release builds abort on panic, so no cleanup runs: never leave the display on.
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        screen::off_for_good();
        default_hook(info);
    }));
    i14_sys::term_signal::install();
    i14_sys::process::ignore(libc::SIGHUP);

    let (screen, canvas) =
        Screen::open("/dev/fb0").unwrap_or_else(|e| fail(format!("/dev/fb0: {e}")));
    let font = |name: &str| {
        Font::open(format!("{FONTS}{name}")).unwrap_or_else(|e| fail(format!("{name}: {e}")))
    };
    let fonts = Fonts {
        big: font("ter-132b.psf.gz"),
        mid: font("ter-124b.psf.gz"),
        small: font("ter-120n.psf.gz"),
        term: font("ter-120n.psf.gz"),
    };
    let devices = classify(SYSFS);
    if devices.power.is_empty() {
        fail("power key input (mtk-kpd) not found".into());
    }
    let open = |p: &String| open_device(p).unwrap_or_else(|e| fail(format!("{p}: {e}")));
    let power = devices.power.iter().map(open).collect();
    let touch = devices.touch.iter().map(open).collect();
    let kbds = devices
        .keyboard
        .iter()
        .map(|p| (open(p), p.clone()))
        .collect();
    let app = App::new(
        canvas,
        Box::new(screen),
        fonts,
        Config::load(i14_config::PATH),
        Box::new(Wifi::system()),
        Bluetooth::new(),
        Box::new(System::default()),
    );
    let mut runner = Runner::new(app, power, touch, kbds);
    runner.screen_on(true); // show once at start (locked when a PIN is set)
    while !i14_sys::term_signal::requested() {
        runner.loop_once();
    }
    // never leave the display on (stop, error, failed poweroff)
    if let Some(s) = &mut runner.app.cx.session {
        s.close();
    }
    runner.app.cx.bt.power(false);
    screen::off_for_good();
}
