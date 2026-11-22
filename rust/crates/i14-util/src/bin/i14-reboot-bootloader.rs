//! i14-reboot-bootloader: reboot straight into the bootloader's fastboot mode,
//! as /usr/sbin/i14-reboot-bootloader (Python). Syncs first.

#[cfg(target_os = "linux")]
fn main() {
    let _ = std::process::Command::new("sync").status();
    let e = i14_sys::reboot_bootloader(); // returns only on failure
    eprintln!("{e}");
    std::process::exit(1);
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("i14-reboot-bootloader only runs on the phone");
    std::process::exit(1);
}
