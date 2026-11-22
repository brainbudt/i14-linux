//! CLI validation before opening any stock firmware files.

use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{symlink, OpenOptionsExt, PermissionsExt};
use std::process::Command;

use i14_tools::read_rescue_hash;

#[test]
fn builder_requires_a_private_rescue_password_hash() {
    let run = Command::new(env!("CARGO_BIN_EXE_i14-build-boot"))
        .arg("unused.img")
        .output()
        .unwrap();
    assert_eq!(run.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&run.stderr).contains("--rescue-password-hash-file"));
}

#[test]
fn rescue_hash_file_rejects_public_symlink_and_bad_hash_inputs() {
    let dir = std::env::temp_dir().join(format!("i14-rescue-hash-test-{}", std::process::id()));
    fs::create_dir(&dir).unwrap();
    let hash = dir.join("hash");
    let alias = dir.join("alias");
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&hash)
        .unwrap();
    writeln!(file, "$6$rounds=1000$test.salt${}", "A".repeat(86)).unwrap();
    drop(file);
    assert!(read_rescue_hash(&hash).is_ok());
    fs::set_permissions(&hash, fs::Permissions::from_mode(0o400)).unwrap();
    assert!(read_rescue_hash(&hash).is_ok());
    fs::set_permissions(&hash, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(read_rescue_hash(&hash).is_err());
    fs::set_permissions(&hash, fs::Permissions::from_mode(0o600)).unwrap();
    symlink(&hash, &alias).unwrap();
    assert!(read_rescue_hash(&alias).is_err());
    fs::write(&hash, b"$6$salty$too-short").unwrap();
    assert!(read_rescue_hash(&hash).is_err());
    fs::remove_file(alias).unwrap();
    fs::remove_file(hash).unwrap();
    fs::remove_dir(dir).unwrap();
}
