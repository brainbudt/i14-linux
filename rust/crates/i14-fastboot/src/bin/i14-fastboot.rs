//! `i14-fastboot` - port of `scripts/fastboot_usb.py`. Ctrl-C stops the host
//! (default signal action), like the Python tool.
use std::process::ExitCode;

fn main() -> ExitCode {
    let Some(argv) = i14_fastboot::cli::utf8_args("i14-fastboot") else {
        return ExitCode::from(2);
    };
    let code = i14_fastboot::cli::fastboot_main(
        &argv,
        i14_fastboot::usb::connect,
        &mut std::io::stdout(),
        &mut std::io::stderr(),
    );
    ExitCode::from(u8::try_from(code).unwrap_or(1))
}
