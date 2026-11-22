//! Explicitly invoked host-only rehearsal; transport is always the in-memory fake.
use i14_fastboot::{
    Fastboot, cli,
    fake::{FakeDevice, Reply},
};
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    sync::{Arc, atomic::AtomicBool},
};

#[test]
#[ignore = "requires private stock backup and a new receipt path"]
fn stock_boot_through_fake_only() {
    let image = PathBuf::from(std::env::var_os("I14_REHEARSAL_IMAGE").unwrap());
    let receipt = PathBuf::from(std::env::var_os("I14_REHEARSAL_RECEIPT").unwrap());
    let bytes = std::fs::read(&image).unwrap();
    let digest = format!("{:x}", Sha256::digest(&bytes));
    let replies = [
        b"OKAYMAGC6735_65U_1_M0".as_slice(),
        b"OKAYyes",
        b"OKAYno",
        b"OKAY0x1000000",
        b"OKAY0x2000000",
        b"DATA01000000",
        b"OKAY",
        b"OKAY",
    ];
    let fake = FakeDevice::new(replies.into_iter().map(Reply::from)).port(Some(&[1, 1]));
    let clone = fake.clone();
    let argv = vec![
        "--image".into(),
        image.display().to_string(),
        "--sha256".into(),
        digest.clone(),
        "--stock-backup".into(),
        image.display().to_string(),
        "--attempt".into(),
        receipt.display().to_string(),
    ];
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let result = cli::install_main(
        &argv,
        || Ok(Fastboot::new(clone)),
        Arc::new(AtomicBool::new(false)),
        &mut out,
        &mut err,
    );
    assert_eq!(result, 0, "{}", String::from_utf8_lossy(&err));
    let writes = fake.writes();
    assert_eq!(writes[5], b"download:01000000");
    assert_eq!(writes.last().unwrap(), b"flash:boot");
    let received: Vec<u8> = writes[6..writes.len() - 1].concat();
    assert_eq!(received.len(), bytes.len());
    assert_eq!(format!("{:x}", Sha256::digest(&received)), digest);
    assert_eq!(received, bytes);
    println!(
        "fake received {} bytes, SHA-256 {}; exact match; flash:boot acknowledged by fake; physical readback UNKNOWN",
        received.len(),
        digest
    );
}
