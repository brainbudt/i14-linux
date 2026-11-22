//! Port of tests/test_fastboot_usb.py. Offline protocol tests: never connect to USB.

mod common;

use std::cell::Cell;

use common::*;
use i14_fastboot::fake::{FakeDevice, Reply};
use i14_fastboot::protocol::{Candidate, EndpointInfo, InterfaceInfo};
use i14_fastboot::{Error, Fastboot, Result, select_device, validate_image};

fn is_fastboot_err<T: std::fmt::Debug>(result: Result<T>) -> bool {
    matches!(result, Err(Error::Fastboot(_)))
}

fn responses(tail: &[Reply], unlocked: &[u8], size: &[u8], maximum: &[u8]) -> Vec<Reply> {
    let mut list = vec![
        okay(b"MAGC6735_65U_1_M0"),
        okay(unlocked),
        okay(size),
        okay(maximum),
    ];
    list.extend_from_slice(tail);
    list
}

fn default_responses(tail: &[Reply]) -> Vec<Reply> {
    responses(tail, b"yes", b"0x1000000", b"0x2000000")
}

// ---- ImageTests ----

#[test]
fn test_legacy_v0_image_and_hash_are_required() {
    let valid = image();
    assert_eq!(
        validate_image(&valid, Some(&sha(&valid))).unwrap(),
        &valid[..]
    );
    let mut short = valid.clone();
    short.pop();
    let invalid = vec![
        Vec::new(),
        vec![b'x'; 6144],
        Img {
            page: 0,
            ..Img::default()
        }
        .build(),
        Img {
            page: 1024,
            ..Img::default()
        }
        .build(),
        Img {
            version: 1,
            ..Img::default()
        }
        .build(),
        Img {
            kernel: 0,
            ..Img::default()
        }
        .build(),
        Img {
            kernel: 100_000,
            ..Img::default()
        }
        .build(),
        Img {
            ramdisk: 100_000,
            ..Img::default()
        }
        .build(),
        Img {
            second: 100_000,
            ..Img::default()
        }
        .build(),
        short,
        vec![b'x'; 0x100_0000 + 1],
    ];
    for data in &invalid {
        assert!(
            is_fastboot_err(validate_image(data, Some(&sha(data)))),
            "length {}",
            data.len()
        );
    }
    for digest in ["0".repeat(64), "0".repeat(63), "z".repeat(64)] {
        assert!(
            is_fastboot_err(validate_image(&valid, Some(&digest))),
            "{digest}"
        );
    }
}

#[test]
fn test_bad_image_never_connects_to_usb() {
    let dir = tempdir("bad-image");
    let path = dir.join("bad.img");
    std::fs::write(&path, b"bad").unwrap();
    let called = Cell::new(false);
    let connect = || -> Result<Fastboot<FakeDevice>> {
        called.set(true);
        Err(Error::Other("must not connect".into()))
    };
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let code = i14_fastboot::cli::fastboot_main(
        &args(&[
            "--boot",
            path.to_str().unwrap(),
            "--sha256",
            &"0".repeat(64),
        ]),
        connect,
        &mut out,
        &mut err,
    );
    assert_eq!(code, 1);
    assert!(!called.get());
}

/// Python checks that importing the module does not load pyusb. The Rust
/// analogue: running the CLI front end without an action (help/usage) never
/// invokes the USB connector.
#[test]
fn test_import_does_not_load_pyusb() {
    for argv in [vec!["--help"], vec![]] {
        let connect = || -> Result<Fastboot<FakeDevice>> { panic!("USB connector invoked") };
        let (mut out, mut err) = (Vec::new(), Vec::new());
        let code = i14_fastboot::cli::fastboot_main(&args(&argv), connect, &mut out, &mut err);
        assert!(code == 0 || code == 2);
    }
}

#[test]
fn test_cleanup_error_is_concise_and_returns_failure() {
    let device = FakeDevice::new([okay(b"MAGC6735_65U_1_M0")]);
    device.state().close_error = true;
    let handle = device.clone();
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let code = i14_fastboot::cli::fastboot_main(
        &args(&["--query", "product"]),
        move || Ok(Fastboot::new(device)),
        &mut out,
        &mut err,
    );
    assert_eq!(code, 1);
    let stderr = String::from_utf8(err).unwrap();
    assert!(stderr.contains("cleanup"));
    assert!(!stderr.contains("private diagnostic"));
    assert_eq!(handle.state().closed, 1);
}

// ---- IdentityTests ----

#[derive(Clone, Debug)]
struct Descriptor {
    vendor: u16,
    product: u16,
    interfaces: Vec<InterfaceInfo>,
}

impl Candidate for Descriptor {
    fn vendor_product(&self) -> (u16, u16) {
        (self.vendor, self.product)
    }
    fn active_interfaces(&mut self) -> Result<Vec<InterfaceInfo>> {
        Ok(self.interfaces.clone())
    }
}

fn descriptor() -> Descriptor {
    Descriptor {
        vendor: 0x0bb4,
        product: 0x0c01,
        interfaces: vec![InterfaceInfo::expected()],
    }
}

#[test]
fn test_unique_vid_pid_and_exact_descriptor_required() {
    let device = descriptor();
    assert!(select_device(vec![device.clone()]).is_ok());
    for devices in [vec![], vec![device.clone(), device.clone()]] {
        assert!(is_fastboot_err(select_device(devices)));
    }
    type Mutate = fn(&mut Descriptor);
    let mutations: [(&str, Mutate); 10] = [
        ("idVendor", |d| d.vendor = 1),
        ("idProduct", |d| d.product = 1),
        ("bInterfaceNumber", |d| d.interfaces[0].number = 1),
        ("bAlternateSetting", |d| {
            d.interfaces[0].alternate_setting = 1
        }),
        ("bInterfaceClass", |d| d.interfaces[0].class = 1),
        ("bInterfaceSubClass", |d| d.interfaces[0].subclass = 1),
        ("bInterfaceProtocol", |d| d.interfaces[0].protocol = 1),
        ("bEndpointAddress", |d| {
            d.interfaces[0].endpoints[0].address = 2
        }),
        ("bmAttributes", |d| {
            d.interfaces[0].endpoints[1].attributes = 3
        }),
        ("wMaxPacketSize", |d| {
            d.interfaces[0].endpoints[0].max_packet_size = 64
        }),
    ];
    for (name, mutate) in mutations {
        let mut changed = descriptor();
        mutate(&mut changed);
        assert!(is_fastboot_err(select_device(vec![changed])), "{name}");
    }
    // Endpoint order does not matter (Python sorts); extra interfaces do.
    let mut reordered = descriptor();
    reordered.interfaces[0].endpoints.reverse();
    assert!(select_device(vec![reordered]).is_ok());
    let mut two = descriptor();
    two.interfaces.push(InterfaceInfo::expected());
    assert!(is_fastboot_err(select_device(vec![two])));
    let mut extra_ep = descriptor();
    extra_ep.interfaces[0].endpoints.push(EndpointInfo {
        address: 0x82,
        attributes: 3,
        max_packet_size: 64,
    });
    assert!(is_fastboot_err(select_device(vec![extra_ep])));
}

#[test]
fn test_wrong_product_stops_before_action() {
    let device = FakeDevice::new([r(b"OKAYother")]);
    assert!(is_fastboot_err(Fastboot::new(device.clone()).reboot()));
    assert_eq!(device.writes(), vec![b"getvar:product".to_vec()]);
}

// ---- ProtocolTests ----

#[test]
fn test_queries_and_reboot_check_product_each_time() {
    let device = FakeDevice::new([
        r(b"INFOhello"),
        r(b"OKAYMAGC6735_65U_1_M0"),
        r(b"OKAYno"),
        r(b"OKAYMAGC6735_65U_1_M0"),
        r(b"OKAY"),
    ]);
    let mut client = Fastboot::new(device.clone());
    assert_eq!(client.query("unlocked").unwrap(), "no");
    client.reboot().unwrap();
    assert_eq!(
        device.writes(),
        [
            &b"getvar:product"[..],
            b"getvar:unlocked",
            b"getvar:product",
            b"reboot"
        ]
    );
}

#[test]
fn test_unapproved_query_never_writes() {
    let device = FakeDevice::new(Vec::<Reply>::new());
    assert!(is_fastboot_err(
        Fastboot::new(device.clone()).query("serialno")
    ));
    assert!(device.writes().is_empty());
}

#[test]
fn test_failure_malformed_reply_timeout_and_short_write_stop() {
    let mut long = b"OKAY".to_vec();
    long.extend(vec![b'x'; 61]);
    for reply in [
        r(b"FAILnot allowed"),
        r(b"OK"),
        r(b""),
        r(b"WHATunknown"),
        r(b"DATA00000000"),
        Reply::Bytes(long),
        Reply::Timeout,
    ] {
        let device = FakeDevice::new([reply.clone()]);
        assert!(
            is_fastboot_err(Fastboot::new(device.clone()).reboot()),
            "{reply:?}"
        );
        assert_eq!(device.writes(), vec![b"getvar:product".to_vec()]);
    }
    let device = FakeDevice::new(Vec::<Reply>::new()).short_write(1);
    assert!(is_fastboot_err(Fastboot::new(device.clone()).reboot()));
    assert_eq!(device.reads(), 0);
}

#[test]
fn test_info_flood_is_bounded() {
    let device = FakeDevice::new(vec![r(b"INFOx"); 257]);
    assert!(is_fastboot_err(Fastboot::new(device.clone()).reboot()));
    assert_eq!(device.reads(), 256);
    assert_eq!(device.writes(), vec![b"getvar:product".to_vec()]);
}

#[test]
fn test_boot_downloads_exact_bytes_then_boots_after_final_okay() {
    let data = image();
    let device = FakeDevice::new(default_responses(&[
        r(b"DATA00001800"),
        r(b"INFOdownloaded"),
        r(b"OKAY"),
        r(b"OKAY"),
    ]));
    Fastboot::new(device.clone())
        .boot(&data, Some(&sha(&data)))
        .unwrap();
    let writes = device.writes();
    assert_eq!(
        writes[..5],
        [
            &b"getvar:product"[..],
            b"getvar:unlocked",
            b"getvar:partition-size:boot",
            b"getvar:max-download-size",
            b"download:00001800"
        ]
    );
    assert_eq!(writes[5..writes.len() - 1].concat(), data);
    assert_eq!(writes.last().unwrap(), b"boot");
}

#[test]
fn test_large_image_uses_complete_chunks_and_stops_at_second_chunk_error() {
    let data = Img {
        kernel: 20000,
        length: 24576,
        ..Img::default()
    }
    .build();
    let digest = sha(&data);
    let script = default_responses(&[r(b"DATA00006000"), r(b"OKAY"), r(b"OKAY")]);
    let device = FakeDevice::new(script.clone());
    Fastboot::new(device.clone())
        .boot(&data, Some(&digest))
        .unwrap();
    let writes = device.writes();
    let chunks: Vec<usize> = writes[5..writes.len() - 1].iter().map(Vec::len).collect();
    assert_eq!(chunks, [16384, 8192]);
    assert_eq!(writes[5..writes.len() - 1].concat(), data);
    for short in [true, false] {
        let device = FakeDevice::new(script.clone());
        let device = if short {
            device.short_write(7)
        } else {
            device.failed_write(7)
        };
        assert!(is_fastboot_err(
            Fastboot::new(device.clone()).boot(&data, Some(&digest))
        ));
        assert_eq!(device.writes().len(), 7);
        assert_eq!(device.reads(), 5);
        assert!(!device.writes().contains(&b"boot".to_vec()));
    }
}

#[test]
fn test_boot_guards_stop_before_download() {
    let data = image();
    let cases: [(&[u8], &[u8], &[u8]); 5] = [
        (b"no", b"0x1000000", b"0x2000000"),
        (b"yes", b"0x800000", b"0x2000000"),
        (b"yes", b"unknown", b"0x2000000"),
        (b"yes", b"0x1000000", b"0x1000"),
        (b"yes", b"0x1000000", b"unknown"),
    ];
    for (unlocked, size, maximum) in cases {
        let device = FakeDevice::new(responses(&[], unlocked, size, maximum));
        assert!(is_fastboot_err(
            Fastboot::new(device.clone()).boot(&data, Some(&sha(&data)))
        ));
        assert!(!device.writes().iter().any(|w| w.starts_with(b"download:")));
    }
    let device = FakeDevice::new(Vec::<Reply>::new());
    assert!(is_fastboot_err(
        Fastboot::new(device.clone()).boot(&data, Some(&"0".repeat(64)))
    ));
    assert!(device.writes().is_empty());
}

#[test]
fn test_boot_never_continues_after_download_error() {
    let data = image();
    let cases: Vec<(Vec<Reply>, Option<usize>)> = vec![
        (vec![r(b"FAILdenied")], None),
        (vec![r(b"DATA00000001")], None),
        (vec![r(b"DATAgarbage!")], None),
        (vec![r(b"OKAY")], None),
        (vec![r(b"DATA00001800"), r(b"FAILinvalid")], None),
        (vec![r(b"DATA00001800"), Reply::Timeout], None),
        (vec![r(b"DATA00001800")], Some(6)),
    ];
    for (tail, short) in cases {
        let mut device = FakeDevice::new(default_responses(&tail));
        if let Some(n) = short {
            device = device.short_write(n);
        }
        assert!(is_fastboot_err(
            Fastboot::new(device.clone()).boot(&data, Some(&sha(&data)))
        ));
        assert!(!device.writes().contains(&b"boot".to_vec()));
        if tail[0] != r(b"DATA00001800") {
            assert_eq!(device.writes().len(), 5);
        }
    }
}

// ---- Rust-only additions ----

#[test]
fn response_deadline_and_timeouts_are_bounded() {
    let device = FakeDevice::new([r(b"OKAYMAGC6735_65U_1_M0")]);
    let mut client = Fastboot::new(device.clone()).with_response_budget(std::time::Duration::ZERO);
    assert_eq!(
        client.reboot(),
        Err(Error::Fastboot(
            "Fastboot response deadline exceeded".into()
        ))
    );
    assert_eq!(device.reads(), 0);
    let device = FakeDevice::new([r(b"OKAYMAGC6735_65U_1_M0"), r(b"OKAY")]);
    Fastboot::new(device.clone()).reboot().unwrap();
    assert!(device.state().timeouts.iter().all(|t| *t > 0 && *t <= 3000));
}

#[test]
fn interrupt_flag_stops_at_next_usb_call() {
    let interrupt = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
    let device = FakeDevice::new([r(b"OKAYMAGC6735_65U_1_M0")]);
    let mut client = Fastboot::new(device.clone()).with_interrupt(interrupt);
    assert_eq!(client.reboot(), Err(Error::Interrupted));
    assert!(device.writes().is_empty());
}
