//! `i14-install-boot` - port of `scripts/install_boot_usb.py`.
//!
//! SIGINT sets a flag instead of killing the process, so an
//! interruption is recorded as `stopped` in the receipt (Python catches
//! `KeyboardInterrupt` the same way).
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

fn main() -> ExitCode {
    let interrupt = Arc::new(AtomicBool::new(false));
    if signal_hook::flag::register(signal_hook::consts::SIGINT, Arc::clone(&interrupt)).is_err() {
        eprintln!("stopped: I/O or interruption; inspect receipt; no retry");
        return ExitCode::from(1);
    }
    let Some(argv) = i14_fastboot::cli::utf8_args("i14-install-boot") else {
        return ExitCode::from(2);
    };
    let code = i14_fastboot::cli::install_main(
        &argv,
        i14_fastboot::usb::connect,
        interrupt,
        &mut std::io::stdout(),
        &mut std::io::stderr(),
    );
    ExitCode::from(u8::try_from(code).unwrap_or(1))
}
