//! i14-netwatch: the Wi-Fi watchdog service (OpenRC, under supervise-daemon). Never reloads the
//! Wi-Fi driver and never reboots; SIGTERM ends it (default disposition, as the shell script).
fn main() {
    let c = i14_netwatch::Config::from_lookup(|k| std::env::var(k).ok());
    i14_netwatch::run(&c, &mut i14_netwatch::Sys);
}
