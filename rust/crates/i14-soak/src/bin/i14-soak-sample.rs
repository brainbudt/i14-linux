//! Minute sampler for stock 3.18 / Alpine. No device writes, no current_cmd reads.
//! Usage: i14-soak-sample [install|uninstall]

fn main() {
    let args: Vec<std::ffi::OsString> = std::env::args_os().collect();
    let cfg = i14_soak::sample::Config::from_env();
    std::process::exit(i14_soak::sample::main_with(&args, &cfg));
}
