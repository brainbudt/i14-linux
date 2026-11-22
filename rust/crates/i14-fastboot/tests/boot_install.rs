//! Port of tests/test_boot_install.py: synthetic images and fake USB only.

mod common;

use std::cell::Cell;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

use common::*;
use i14_fastboot::fake::{FakeDevice, Reply};
use i14_fastboot::{
    Error, Fastboot, Receipt, Result, Value, install_boot, validate_install_cmdline, validate_stock,
};

struct Replies<'a> {
    product: &'a [u8],
    unlocked: &'a [u8],
    secure: &'a [u8],
    partition: &'a [u8],
    maximum: &'a [u8],
}

const DEFAULT: Replies<'static> = Replies {
    product: b"MAGC6735_65U_1_M0",
    unlocked: b"yes",
    secure: b"no",
    partition: b"0x1000000",
    maximum: b"0x2000000",
};

fn replies(tail: &[Reply], r: &Replies) -> Vec<Reply> {
    let mut list: Vec<Reply> = [r.product, r.unlocked, r.secure, r.partition, r.maximum]
        .iter()
        .map(|v| okay(v))
        .collect();
    list.extend_from_slice(tail);
    list
}

fn is_fastboot_err(result: Result<()>) -> bool {
    matches!(result, Err(Error::Fastboot(_)))
}

#[test]
fn test_stock_backup_must_match_pinned_full_partition() {
    for stock in [Vec::new(), vec![b'x'; 16384], vec![0u8; 0x100_0000]] {
        assert!(
            is_fastboot_err(validate_stock(&stock)),
            "size {}",
            stock.len()
        );
    }
}

#[test]
fn test_invalid_local_inputs_never_connect_or_create_attempt() {
    let root = tempdir("invalid-local");
    let candidate = root.join("candidate.img");
    let stock = root.join("stock.img");
    let attempt = root.join("attempt.jsonl");
    std::fs::write(&candidate, image()).unwrap();
    std::fs::write(&stock, b"bad").unwrap();
    for digest in ["0".repeat(64), sha(&image())] {
        let called = Cell::new(false);
        let connect = || -> Result<Fastboot<FakeDevice>> {
            called.set(true);
            Err(Error::Other("must not connect".into()))
        };
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = i14_fastboot::cli::install_main(
            &args(&[
                "--image",
                candidate.to_str().unwrap(),
                "--sha256",
                &digest,
                "--stock-backup",
                stock.to_str().unwrap(),
                "--attempt",
                attempt.to_str().unwrap(),
            ]),
            connect,
            Arc::new(AtomicBool::new(false)),
            &mut out,
            &mut err,
        );
        assert_eq!(code, 1);
        assert!(!called.get());
        assert!(!attempt.exists());
    }
}

#[test]
fn test_ram_boot_test_images_are_refused_before_connect() {
    let with_cmdline = |text: &[u8], offset: usize| {
        let mut data = image();
        data[offset..offset + text.len()].copy_from_slice(text);
        data
    };
    let refused: [&[u8]; 5] = [
        b"bootopt=64S3,32N2,64N2 rdinit=/i14init androidboot.mode=charger i14.autoboot=test",
        b"bootopt=64S3,32N2,64N2 rdinit=/i14init androidboot.mode=charger i14.test=charge-limit",
        b"rdinit=/i14init i14.autoboot=test",
        b"i14.test=x",
        b"androidboot.mode=normal",
    ];
    let is_refusal = |r: Result<()>| matches!(r, Err(Error::Fastboot(m)) if m.starts_with("RAM-boot-only test image"));
    for text in refused {
        assert!(
            is_refusal(validate_install_cmdline(&with_cmdline(text, 64))),
            "{text:?}"
        );
    }
    assert!(is_refusal(validate_install_cmdline(&with_cmdline(
        b"i14.test=charge-limit",
        608
    ))));
    for text in [
        &b""[..],
        b"bootopt=64S3,32N2,64N2 rdinit=/i14init",
        b"xi14.test=1 my.androidboot.mode=charger",
    ] {
        assert_eq!(
            validate_install_cmdline(&with_cmdline(text, 64)),
            Ok(()),
            "{text:?}"
        );
    }
    let dir = tempdir("test-image");
    let (candidate, attempt) = (dir.join("candidate.img"), dir.join("attempt.jsonl"));
    let data = with_cmdline(refused[1], 64);
    std::fs::write(&candidate, &data).unwrap();
    let called = Cell::new(false);
    let connect = || -> Result<Fastboot<FakeDevice>> {
        called.set(true);
        Err(Error::Other("must not connect".into()))
    };
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let code = i14_fastboot::cli::install_main(
        &args(&[
            "--image",
            candidate.to_str().unwrap(),
            "--sha256",
            &sha(&data),
            "--stock-backup",
            dir.join("missing").to_str().unwrap(),
            "--attempt",
            attempt.to_str().unwrap(),
        ]),
        connect,
        Arc::new(AtomicBool::new(false)),
        &mut out,
        &mut err,
    );
    assert_eq!(code, 1);
    assert!(!called.get());
    assert!(!attempt.exists());
    let err = String::from_utf8(err).unwrap();
    assert!(
        err.contains("RAM-boot-only test image (androidboot.mode=charger)"),
        "{err}"
    );
}

#[test]
fn test_receipt_is_exclusive_and_binds_exact_complete_bytes() {
    let data = image();
    let dir = tempdir("receipt");
    let path = dir.join("attempt.jsonl");
    {
        let mut record = Receipt::create(&path, Path::new("candidate.img"), &data).unwrap();
        record
            .record("stopped", &[("nv_write", Value::Str("UNKNOWN".into()))])
            .unwrap();
    }
    let entries = lines(&path);
    assert_eq!(field(&entries[0], "partition"), "boot");
    assert_eq!(field(&entries[0], "candidate_sha256"), sha(&data));
    assert_eq!(field(&entries[0], "candidate_bytes"), "6144");
    assert_eq!(
        field(&entries[0], "stock_sha256"),
        "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492"
    );
    assert_eq!(field(&entries[0], "padding"), "none; exact candidate bytes");
    assert_eq!(field(&entries[0], "readback"), "UNKNOWN");
    assert_eq!(
        std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    let original = std::fs::read(&path).unwrap();
    assert!(matches!(
        Receipt::create(&path, Path::new("candidate.img"), &data),
        Err(Error::Exists)
    ));
    assert_eq!(std::fs::read(&path).unwrap(), original);
}

#[test]
fn test_guard_failures_never_download_or_flash() {
    let cases = [
        Replies {
            product: b"other",
            ..DEFAULT
        },
        Replies {
            unlocked: b"no",
            ..DEFAULT
        },
        Replies {
            secure: b"yes",
            ..DEFAULT
        },
        Replies {
            secure: b"",
            ..DEFAULT
        },
        Replies {
            partition: b"0x800000",
            ..DEFAULT
        },
        Replies {
            partition: b"unknown",
            ..DEFAULT
        },
        Replies {
            maximum: b"0x1000",
            ..DEFAULT
        },
    ];
    let dir = tempdir("guards");
    for (number, overrides) in cases.iter().enumerate() {
        let device = FakeDevice::new(replies(&[], overrides)).port(Some(&[1, 1]));
        let path = dir.join(format!("attempt-{number}.jsonl"));
        let mut record = Receipt::create(&path, Path::new("candidate.img"), &image()).unwrap();
        assert!(is_fastboot_err(install_boot(
            &mut Fastboot::new(device.clone()),
            &image(),
            &mut record
        )));
        assert!(
            !device
                .writes()
                .iter()
                .any(|w| w.starts_with(b"download:") || w.starts_with(b"flash:"))
        );
    }
}

#[test]
fn test_wrong_or_unknown_physical_port_stops_before_commands() {
    let dir = tempdir("ports");
    let ports: [Option<&[u8]>; 3] = [Some(&[1, 2]), Some(&[]), None];
    for (number, port) in ports.into_iter().enumerate() {
        let device = FakeDevice::new(Vec::<Reply>::new()).port(port);
        let mut record = Receipt::create(
            &dir.join(format!("{number}.jsonl")),
            Path::new("x"),
            &image(),
        )
        .unwrap();
        assert!(is_fastboot_err(install_boot(
            &mut Fastboot::new(device.clone()),
            &image(),
            &mut record
        )));
        assert!(device.writes().is_empty());
    }
}

#[test]
fn install_boot_itself_refuses_test_images() {
    let mut data = image();
    let text = b"rdinit=/i14init androidboot.mode=charger i14.test=charge-limit";
    data[64..64 + text.len()].copy_from_slice(text);
    let dir = tempdir("install-refuses-test");
    let path = dir.join("attempt.jsonl");
    let device = FakeDevice::new(replies(&[], &DEFAULT)).port(Some(&[1, 1]));
    let mut record = Receipt::create(&path, Path::new("candidate.img"), &data).unwrap();
    let result = install_boot(&mut Fastboot::new(device.clone()), &data, &mut record);
    assert!(matches!(result, Err(Error::Fastboot(m)) if m.starts_with("RAM-boot-only test image")));
    assert!(device.writes().is_empty());
}

#[test]
fn test_exact_download_then_durable_intent_then_only_flash_boot() {
    let data = Img {
        kernel: 20000,
        length: 24576,
        ..Img::default()
    }
    .build();
    let dir = tempdir("exact");
    let path = dir.join("attempt.jsonl");
    let checked = path.clone();
    let device = FakeDevice::new(replies(
        &[r(b"DATA00006000"), r(b"OKAY"), r(b"OKAY")],
        &DEFAULT,
    ))
    .port(Some(&[1, 1]))
    .on_write(move |payload| {
        if payload == b"flash:boot" {
            let entries = lines(&checked);
            let last = entries.last().unwrap();
            assert_eq!(field(last, "event"), "flash_intent");
            assert_eq!(field(last, "nv_write"), "UNKNOWN");
        }
    });
    {
        let mut record = Receipt::create(&path, Path::new("candidate.img"), &data).unwrap();
        install_boot(&mut Fastboot::new(device.clone()), &data, &mut record).unwrap();
    }
    let writes = device.writes();
    assert_eq!(
        writes[..6],
        [
            &b"getvar:product"[..],
            b"getvar:unlocked",
            b"getvar:secure",
            b"getvar:partition-size:boot",
            b"getvar:max-download-size",
            b"download:00006000"
        ]
    );
    assert_eq!(writes[6..writes.len() - 1].concat(), data);
    assert_eq!(writes.last().unwrap(), b"flash:boot");
    let entries = lines(&path);
    assert_eq!(
        field(entries.last().unwrap(), "event"),
        "protocol_acknowledged"
    );
    assert_eq!(field(entries.last().unwrap(), "readback"), "UNKNOWN");
    assert!(!writes.contains(&b"reboot".to_vec()));
}

#[test]
fn test_uncertain_flash_never_retries_and_receipt_blocks_reuse() {
    let dir = tempdir("uncertain");
    for (number, last) in [Reply::Timeout, r(b"FAILdenied"), r(b"OK")]
        .into_iter()
        .enumerate()
    {
        let path = dir.join(format!("attempt-{number}.jsonl"));
        let device = FakeDevice::new(replies(&[r(b"DATA00001800"), r(b"OKAY"), last], &DEFAULT))
            .port(Some(&[1, 1]));
        {
            let mut record = Receipt::create(&path, Path::new("candidate.img"), &image()).unwrap();
            assert!(is_fastboot_err(install_boot(
                &mut Fastboot::new(device.clone()),
                &image(),
                &mut record
            )));
            let before_retry = device.writes();
            assert!(is_fastboot_err(install_boot(
                &mut Fastboot::new(device.clone()),
                &image(),
                &mut record
            )));
            assert_eq!(device.writes(), before_retry);
        }
        let writes = device.writes();
        assert_eq!(
            writes
                .iter()
                .filter(|w| w.as_slice() == b"flash:boot")
                .count(),
            1
        );
        assert!(!writes.contains(&b"reboot".to_vec()));
        let entries = lines(&path);
        assert_eq!(field(entries.last().unwrap(), "event"), "stopped");
        assert_eq!(field(entries.last().unwrap(), "nv_write"), "UNKNOWN");
        assert!(matches!(
            Receipt::create(&path, Path::new("candidate.img"), &image()),
            Err(Error::Exists)
        ));
    }
}

#[test]
fn test_download_failure_never_flashes() {
    let dir = tempdir("download-failure");
    let device = FakeDevice::new(replies(&[r(b"DATA00000001")], &DEFAULT)).port(Some(&[1, 1]));
    let path = dir.join("attempt.jsonl");
    {
        let mut record = Receipt::create(&path, Path::new("candidate.img"), &image()).unwrap();
        assert!(is_fastboot_err(install_boot(
            &mut Fastboot::new(device.clone()),
            &image(),
            &mut record
        )));
    }
    assert!(!device.writes().contains(&b"flash:boot".to_vec()));
    let entries = lines(&path);
    assert_eq!(field(entries.last().unwrap(), "nv_write"), "NOT_STARTED");
}

#[test]
fn test_other_partition_cli_option_is_rejected_before_connect() {
    let called = Cell::new(false);
    let connect = || -> Result<Fastboot<FakeDevice>> {
        called.set(true);
        Err(Error::Other("must not connect".into()))
    };
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let code = i14_fastboot::cli::install_main(
        &args(&[
            "--image",
            "x",
            "--sha256",
            &"0".repeat(64),
            "--stock-backup",
            "x",
            "--attempt",
            "x",
            "--partition",
            "recovery",
        ]),
        connect,
        Arc::new(AtomicBool::new(false)),
        &mut out,
        &mut err,
    );
    assert_eq!(code, 2);
    assert!(!called.get());
}
