//! Differential tests: the original Python tools (tests/diff/harness.py, fake
//! USB, every `usb` import blocked) versus this crate (FakeDevice). Compared:
//! exact bytes written to the device, read count, exit code, stdout/stderr,
//! connect/close calls, receipt lines (time_utc and temp dir normalised) and
//! receipt mode.

mod common;

use std::cell::{Cell, RefCell};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::rc::Rc;
use std::sync::Arc;
use std::sync::atomic::AtomicBool;

use common::tempdir;
use i14_fastboot::fake::{FakeDevice, Reply};
use i14_fastboot::image::{py_int16, sha256_hex};
use i14_fastboot::protocol::{Candidate, EndpointInfo, InterfaceInfo};
use i14_fastboot::{Error, Fastboot, Result, select_device, validate_image};
use serde_json::{Value, json};

const PROJECT: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../..");
const STOCK: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../backups/20261030T011515Z-native-serial/boot.img"
);

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}

fn python(request: &Value) -> Value {
    let harness = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/diff/harness.py");
    let mut child = Command::new("python3")
        .arg("-I")
        .arg(harness)
        .env("I14_SCRIPTS", format!("{PROJECT}/scripts"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python3");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(request.to_string().as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "harness failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

// ---------- file specs (mirrors make_file in harness.py) ----------

fn make_file(spec: &Value) -> Vec<u8> {
    let mut data = if let Some(image) = spec.get("image") {
        let get =
            |key: &str, default: u64| image.get(key).and_then(Value::as_u64).unwrap_or(default);
        common::Img {
            kernel: get("kernel", 32) as u32,
            ramdisk: get("ramdisk", 16) as u32,
            second: get("second", 0) as u32,
            page: get("page", 2048) as u32,
            version: get("version", 0) as u32,
            length: get("length", 6144) as usize,
        }
        .build()
    } else if let Some(repeat) = spec.get("repeat") {
        vec![repeat[0].as_u64().unwrap() as u8; repeat[1].as_u64().unwrap() as usize]
    } else {
        unhex(spec.get("hex").and_then(Value::as_str).unwrap_or(""))
    };
    if let Some(patch) = spec.get("patch") {
        let offset = patch[0].as_u64().unwrap() as usize;
        let bytes = unhex(patch[1].as_str().unwrap());
        let mut patched = data[..offset.min(data.len())].to_vec();
        patched.extend_from_slice(&bytes);
        if offset + bytes.len() < data.len() {
            patched.extend_from_slice(&data[offset + bytes.len()..]);
        }
        data = patched;
    }
    data
}

// ---------- Rust side ----------

#[derive(Debug)]
struct Desc {
    vendor: u16,
    product: u16,
    interfaces: Vec<InterfaceInfo>,
}

impl Candidate for Desc {
    fn vendor_product(&self) -> (u16, u16) {
        (self.vendor, self.product)
    }
    fn active_interfaces(&mut self) -> Result<Vec<InterfaceInfo>> {
        Ok(self.interfaces.clone())
    }
}

fn descriptors(mutation: &str) -> Vec<Desc> {
    let make = || Desc {
        vendor: 0x0bb4,
        product: 0x0c01,
        interfaces: vec![InterfaceInfo::expected()],
    };
    let mut device = make();
    match mutation {
        "none" => return vec![],
        "two" => return vec![make(), make()],
        "vid" => device.vendor = 0x18d1,
        "pid" => device.product = 0x4ee0,
        "two_interfaces" => device.interfaces.push(InterfaceInfo::expected()),
        "class" => device.interfaces[0].class = 8,
        "ep_address" => device.interfaces[0].endpoints[0].address = 2,
        "ep_packet" => device.interfaces[0].endpoints[1].max_packet_size = 64,
        "ep_extra" => device.interfaces[0].endpoints.push(EndpointInfo {
            address: 0x82,
            attributes: 3,
            max_packet_size: 64,
        }),
        "ok" => {}
        other => panic!("{other}"),
    }
    vec![device]
}

fn fake_from(spec: &Value) -> FakeDevice {
    let replies: Vec<Reply> = spec
        .get("responses")
        .and_then(Value::as_array)
        .map(|list| {
            list.iter()
                .map(|item| {
                    if item.get("timeout").is_some() {
                        Reply::Timeout
                    } else if item.get("interrupt").is_some() {
                        Reply::Interrupt
                    } else {
                        Reply::Bytes(unhex(item["b"].as_str().unwrap()))
                    }
                })
                .collect()
        })
        .unwrap_or_default();
    let device = FakeDevice::new(replies);
    {
        let mut state = device.state();
        let n = |key: &str| spec.get(key).and_then(Value::as_u64).map(|v| v as usize);
        state.short_write = n("short_write");
        state.failed_write = n("failed_write");
        state.interrupt_write = n("interrupt_write");
        state.port_numbers = spec
            .get("port_numbers")
            .and_then(Value::as_array)
            .map(|list| list.iter().map(|v| v.as_u64().unwrap() as u8).collect());
        state.close_error = spec
            .get("close_error")
            .and_then(Value::as_bool)
            .unwrap_or(false);
    }
    device
}

fn rust(request: &Value) -> Value {
    let dir = request["dir"].as_str().unwrap();
    if let Some(files) = request.get("files").and_then(Value::as_object) {
        for (name, spec) in files {
            std::fs::write(Path::new(dir).join(name), make_file(spec)).unwrap();
        }
    }
    if let Some(existing) = request.get("existing").and_then(Value::as_array) {
        for name in existing {
            std::fs::write(Path::new(dir).join(name.as_str().unwrap()), "old\n").unwrap();
        }
    }
    let argv: Vec<String> = request["argv"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| a.as_str().unwrap().replace("{dir}", dir))
        .collect();
    let connected = Cell::new(false);
    let device: Rc<RefCell<Option<FakeDevice>>> = Rc::default();
    let connect = || -> Result<Fastboot<FakeDevice>> {
        connected.set(true);
        let mode = request
            .get("connect")
            .and_then(Value::as_str)
            .unwrap_or("ok");
        match mode {
            "io" => return Err(Error::Io("private diagnostic".into())),
            "other" => return Err(Error::Other("private diagnostic".into())),
            _ => {}
        }
        if let Some(mutation) = mode.strip_prefix("select:") {
            select_device(descriptors(mutation))?;
        }
        let fake = fake_from(request);
        *device.borrow_mut() = Some(fake.clone());
        Ok(Fastboot::new(fake))
    };
    let (mut out, mut err) = (Vec::new(), Vec::new());
    let code = if request["tool"] == "fastboot" {
        i14_fastboot::cli::fastboot_main(&argv, connect, &mut out, &mut err)
    } else {
        i14_fastboot::cli::install_main(
            &argv,
            connect,
            Arc::new(AtomicBool::new(false)),
            &mut out,
            &mut err,
        )
    };
    let attempt = Path::new(dir).join("attempt.jsonl");
    let (receipt, mode) = if attempt.exists() {
        use std::os::unix::fs::PermissionsExt;
        let lines: Vec<Value> = std::fs::read_to_string(&attempt)
            .unwrap()
            .lines()
            .map(|l| Value::String(l.to_owned()))
            .collect();
        let mode = std::fs::metadata(&attempt).unwrap().permissions().mode() & 0o777;
        (Value::Array(lines), Value::String(format!("0o{mode:o}")))
    } else {
        (Value::Null, Value::Null)
    };
    let device = device.borrow();
    let (writes, reads, closed) = match device.as_ref() {
        Some(fake) => {
            let state = fake.state();
            (
                state.writes.iter().map(|w| hex(w)).collect::<Vec<_>>(),
                state.reads,
                state.closed,
            )
        }
        None => (Vec::new(), 0, 0),
    };
    json!({
        "exit": code,
        "stdout": String::from_utf8(out).unwrap(),
        "stderr": String::from_utf8(err).unwrap(),
        "writes": writes,
        "reads": reads,
        "connected": connected.get(),
        "closed": closed,
        "receipt": receipt,
        "receipt_mode": mode,
    })
}

// ---------- normalisation & comparison ----------

fn normalise_text(text: &str, dir: &str) -> String {
    let mut text = text.replace(dir, "{dir}");
    let key = "\"time_utc\": \"";
    let mut from = 0;
    while let Some(pos) = text[from..].find(key) {
        let start = from + pos + key.len();
        let end = start + text[start..].find('"').unwrap();
        assert!(
            text[start..end].ends_with("+00:00"),
            "time format: {}",
            &text[start..end]
        );
        text.replace_range(start..end, "T");
        from = start;
    }
    text
}

fn normalise(value: &Value, dir: &str) -> Value {
    match value {
        Value::String(s) => Value::String(normalise_text(s, dir)),
        Value::Array(items) => Value::Array(items.iter().map(|v| normalise(v, dir)).collect()),
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| (k.clone(), normalise(v, dir)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn canonical_dir(tag: &str) -> PathBuf {
    std::fs::canonicalize(tempdir(tag)).unwrap()
}

/// Run one scenario on both sides; returns a mismatch description.
fn compare(name: &str, scenario: &Value) -> Option<String> {
    let py_dir = canonical_dir("py");
    let rs_dir = canonical_dir("rs");
    let mut py_req = scenario.clone();
    py_req["dir"] = json!(py_dir.to_str().unwrap());
    let mut rs_req = scenario.clone();
    rs_req["dir"] = json!(rs_dir.to_str().unwrap());
    let py = normalise(&python(&py_req), py_dir.to_str().unwrap());
    let rs = normalise(&rust(&rs_req), rs_dir.to_str().unwrap());
    let short = |v: &Value| {
        let mut v = v.clone();
        if let Some(writes) = v["writes"].as_array_mut() {
            for w in writes.iter_mut() {
                let text = w.as_str().unwrap().to_owned();
                if text.len() > 80 {
                    *w = json!(format!(
                        "<{} bytes sha256 {}>",
                        text.len() / 2,
                        &sha256_hex(&unhex(&text))[..16]
                    ));
                }
            }
        }
        serde_json::to_string(&v).unwrap()
    };
    (py != rs).then(|| format!("{name}\n  python: {}\n  rust:   {}", short(&py), short(&rs)))
}

// ---------- scenario builders ----------

fn b(bytes: &[u8]) -> Value {
    json!({ "b": hex(bytes) })
}

fn okay(value: &str) -> Value {
    b(format!("OKAY{value}").as_bytes())
}

fn timeout() -> Value {
    json!({ "timeout": true })
}

fn interrupt() -> Value {
    json!({ "interrupt": true })
}

fn img(fields: Value) -> Value {
    json!({ "image": fields })
}

fn sha_of(spec: &Value) -> String {
    sha256_hex(&make_file(spec))
}

struct InstallVars<'a> {
    product: &'a str,
    unlocked: &'a str,
    secure: &'a str,
    partition: &'a str,
    maximum: &'a str,
}

const IV: InstallVars<'static> = InstallVars {
    product: "MAGC6735_65U_1_M0",
    unlocked: "yes",
    secure: "no",
    partition: "0x1000000",
    maximum: "0x2000000",
};

fn install_replies(v: &InstallVars, tail: Vec<Value>) -> Vec<Value> {
    let mut list = vec![
        okay(v.product),
        okay(v.unlocked),
        okay(v.secure),
        okay(v.partition),
        okay(v.maximum),
    ];
    list.extend(tail);
    list
}

fn install(candidate: Value, sha: Option<&str>, responses: Vec<Value>, extra: Value) -> Value {
    let digest = sha.map_or_else(|| sha_of(&candidate), str::to_owned);
    let mut scenario = json!({
        "tool": "install",
        "argv": ["--image", "{dir}/candidate.img", "--sha256", digest,
                 "--stock-backup", STOCK, "--attempt", "{dir}/attempt.jsonl"],
        "files": { "candidate.img": candidate },
        "responses": responses,
        "port_numbers": [1, 1],
    });
    if let Value::Object(map) = extra {
        for (k, v) in map {
            scenario[k] = v;
        }
    }
    scenario
}

fn data_reply(len: usize) -> Value {
    b(format!("DATA{len:08x}").as_bytes())
}

fn install_scenarios() -> Vec<(String, Value)> {
    let small = img(json!({}));
    let large = img(json!({"kernel": 20000, "length": 24576}));
    let ok_tail = |len: usize| vec![data_reply(len), okay(""), okay("")];
    let mut list: Vec<(String, Value)> = vec![
        (
            "install: normal small".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({}),
            ),
        ),
        (
            "install: normal multi-chunk".into(),
            install(
                large.clone(),
                None,
                install_replies(&IV, ok_tail(24576)),
                json!({}),
            ),
        ),
        (
            "install: normal with INFO packets".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &IV,
                    vec![
                        b(b"INFOhi"),
                        data_reply(6144),
                        b(b"INFOwriting"),
                        okay(""),
                        b(b"INFOflashing"),
                        okay(""),
                    ],
                ),
                json!({}),
            ),
        ),
        (
            "install: RAM-boot-only test image refused".into(),
            install(
                json!({"image": {}, "patch": [64, hex(
                    b"bootopt=64S3,32N2,64N2 rdinit=/i14init androidboot.mode=charger i14.test=charge-limit"
                )]}),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({}),
            ),
        ),
        (
            "install: wrong product".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        product: "other",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: locked".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        unlocked: "no",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: unlocked with trailing space".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        unlocked: "yes ",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: secure=yes".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        secure: "yes",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: partition small".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        partition: "0x800000",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: partition unknown".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        partition: "unknown",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: max unknown".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        maximum: "zz",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: max too small".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        maximum: "0x1000",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: max == len".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        maximum: "1800",
                        ..IV
                    },
                    ok_tail(6144),
                ),
                json!({}),
            ),
        ),
        (
            "install: partition 0x_1000000 underscore".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        partition: "0x_1000000",
                        ..IV
                    },
                    ok_tail(6144),
                ),
                json!({}),
            ),
        ),
        (
            "install: partition +0X1000000 and spaces".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        partition: " +0X1000000\t",
                        ..IV
                    },
                    ok_tail(6144),
                ),
                json!({}),
            ),
        ),
        (
            "install: partition with \\x1c separator".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        partition: "\u{1c}1000000",
                        ..IV
                    },
                    ok_tail(6144),
                ),
                json!({}),
            ),
        ),
        (
            "install: max negative".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        maximum: "-0x2000000",
                        ..IV
                    },
                    vec![],
                ),
                json!({}),
            ),
        ),
        (
            "install: max huge".into(),
            install(
                small.clone(),
                None,
                install_replies(
                    &InstallVars {
                        maximum: "0xffffffffffffffffffffffffffffffffffffffff",
                        ..IV
                    },
                    ok_tail(6144),
                ),
                json!({}),
            ),
        ),
        (
            "install: wrong port (1,2)".into(),
            install(small.clone(), None, vec![], json!({"port_numbers": [1, 2]})),
        ),
        (
            "install: unknown port".into(),
            install(small.clone(), None, vec![], json!({"port_numbers": null})),
        ),
        (
            "install: empty port".into(),
            install(small.clone(), None, vec![], json!({"port_numbers": []})),
        ),
        (
            "install: short read at product".into(),
            install(small.clone(), None, vec![b(b"OK")], json!({})),
        ),
        (
            "install: empty read at secure".into(),
            install(
                small.clone(),
                None,
                vec![okay(IV.product), okay("yes"), b(b"")],
                json!({}),
            ),
        ),
        (
            "install: oversized read".into(),
            install(
                small.clone(),
                None,
                vec![b(&b"OKAY"
                    .iter()
                    .copied()
                    .chain(std::iter::repeat_n(b'x', 61))
                    .collect::<Vec<u8>>())],
                json!({}),
            ),
        ),
        (
            "install: short read after download".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), b(b"OK")]),
                json!({}),
            ),
        ),
        (
            "install: short read at flash".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), okay(""), b(b"OK")]),
                json!({}),
            ),
        ),
        (
            "install: FAIL at product".into(),
            install(small.clone(), None, vec![b(b"FAILno")], json!({})),
        ),
        (
            "install: FAIL at download".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![b(b"FAILdenied")]),
                json!({}),
            ),
        ),
        (
            "install: FAIL after data".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), b(b"FAILcrc")]),
                json!({}),
            ),
        ),
        (
            "install: FAIL at flash".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), okay(""), b(b"FAILdenied")]),
                json!({}),
            ),
        ),
        (
            "install: unexpected status at flash".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), okay(""), b(b"DATA00000000")]),
                json!({}),
            ),
        ),
        (
            "install: timeout at unlocked".into(),
            install(
                small.clone(),
                None,
                vec![okay(IV.product), timeout()],
                json!({}),
            ),
        ),
        (
            "install: timeout after data".into(),
            install(
                large.clone(),
                None,
                install_replies(&IV, vec![data_reply(24576), timeout()]),
                json!({}),
            ),
        ),
        (
            "install: timeout at flash".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), okay(""), timeout()]),
                json!({}),
            ),
        ),
        (
            "install: device accepted wrong length".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6143)]),
                json!({}),
            ),
        ),
        (
            "install: device accepted 7-digit length".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![b(b"DATA0001800")]),
                json!({}),
            ),
        ),
        (
            "install: device accepted uppercase length".into(),
            install(
                img(json!({"length": 6400})),
                None,
                install_replies(&IV, vec![b(b"DATA00001900"), okay(""), okay("")]),
                json!({}),
            ),
        ),
        (
            "install: non-ASCII getvar".into(),
            install(small.clone(), None, vec![b(b"OKAYMAGC\xff")], json!({})),
        ),
        (
            "install: missing response (fake exhausted)".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144)]),
                json!({}),
            ),
        ),
        (
            "install: INFO flood".into(),
            install(small.clone(), None, vec![b(b"INFOx"); 257], json!({})),
        ),
        (
            "install: short write at chunk".into(),
            install(
                large.clone(),
                None,
                install_replies(&IV, ok_tail(24576)),
                json!({"short_write": 8}),
            ),
        ),
        (
            "install: failed write at command".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({"failed_write": 3}),
            ),
        ),
        (
            "install: failed write at flash".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({"failed_write": 8}),
            ),
        ),
        (
            "install: interrupted during chunk".into(),
            install(
                large.clone(),
                None,
                install_replies(&IV, ok_tail(24576)),
                json!({"interrupt_write": 7}),
            ),
        ),
        (
            "install: interrupted at flash write".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({"interrupt_write": 8}),
            ),
        ),
        (
            "install: interrupted read at flash".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, vec![data_reply(6144), okay(""), interrupt()]),
                json!({}),
            ),
        ),
        (
            "install: interrupted read at product".into(),
            install(small.clone(), None, vec![interrupt()], json!({})),
        ),
        (
            "install: close error after success".into(),
            install(
                small.clone(),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({"close_error": true}),
            ),
        ),
        (
            "install: close error after failure".into(),
            install(
                small.clone(),
                None,
                vec![b(b"FAILx")],
                json!({"close_error": true}),
            ),
        ),
        (
            "install: connect I/O error".into(),
            install(small.clone(), None, vec![], json!({"connect": "io"})),
        ),
        (
            "install: connect other error".into(),
            install(small.clone(), None, vec![], json!({"connect": "other"})),
        ),
        (
            "install: hash mismatch".into(),
            install(small.clone(), Some(&"0".repeat(64)), vec![], json!({})),
        ),
        (
            "install: uppercase hash accepted".into(),
            install(
                small.clone(),
                Some(&sha_of(&small).to_uppercase()),
                install_replies(&IV, ok_tail(6144)),
                json!({}),
            ),
        ),
        (
            "install: invalid hash syntax".into(),
            install(small.clone(), Some("abc"), vec![], json!({})),
        ),
        (
            "install: empty hash".into(),
            install(small.clone(), Some(""), vec![], json!({})),
        ),
        (
            "install: image too large".into(),
            install(
                json!({"repeat": [0, 0x100_0001]}),
                Some(&"0".repeat(64)),
                vec![],
                json!({}),
            ),
        ),
        (
            "install: image too small".into(),
            install(json!({"hex": "414e44524f494421"}), None, vec![], json!({})),
        ),
        (
            "install: bad magic".into(),
            install(json!({"repeat": [120, 6144]}), None, vec![], json!({})),
        ),
        (
            "install: header v1".into(),
            install(img(json!({"version": 1})), None, vec![], json!({})),
        ),
        (
            "install: segments exceed image".into(),
            install(img(json!({"ramdisk": 100000})), None, vec![], json!({})),
        ),
        (
            "install: image exactly 16 MiB".into(),
            install(
                img(json!({"length": 0x100_0000})),
                None,
                install_replies(&IV, ok_tail(0x100_0000)),
                json!({}),
            ),
        ),
    ];
    // Candidates whose local validation fails never connect nor create a receipt.
    let mut bad_stock = install(small.clone(), None, vec![], json!({}));
    bad_stock["files"]["stock.img"] = json!({"hex": "626164"});
    bad_stock["argv"][5] = json!("{dir}/stock.img");
    list.push(("install: stock mismatch".into(), bad_stock));
    let mut missing_stock = install(small.clone(), None, vec![], json!({}));
    missing_stock["argv"][5] = json!("{dir}/nope.img");
    list.push(("install: stock missing".into(), missing_stock));
    let mut missing_image = install(small.clone(), None, vec![], json!({}));
    missing_image["argv"][1] = json!("{dir}/nope.img");
    list.push(("install: image missing".into(), missing_image));
    let mut existing = install(
        small.clone(),
        None,
        install_replies(&IV, ok_tail(6144)),
        json!({}),
    );
    existing["existing"] = json!(["attempt.jsonl"]);
    list.push(("install: receipt already exists".into(), existing));
    let mut no_dir = install(small.clone(), None, vec![], json!({}));
    no_dir["argv"][7] = json!("{dir}/missing/attempt.jsonl");
    list.push(("install: receipt directory missing".into(), no_dir));
    let mut relative = install(
        small.clone(),
        None,
        install_replies(&IV, ok_tail(6144)),
        json!({}),
    );
    relative["argv"][1] = json!("{dir}/./sub/../candidate.img");
    list.push((
        "install: image path with dot segments (nonexistent sub)".into(),
        relative,
    ));
    for mutation in [
        "none",
        "two",
        "vid",
        "pid",
        "two_interfaces",
        "class",
        "ep_address",
        "ep_packet",
        "ep_extra",
        "ok",
    ] {
        list.push((
            format!("install: identity {mutation}"),
            install(
                small.clone(),
                None,
                install_replies(&IV, ok_tail(6144)),
                json!({"connect": format!("select:{mutation}")}),
            ),
        ));
    }
    list
}

fn fastboot(argv: Vec<&str>, responses: Vec<Value>, extra: Value) -> Value {
    let mut scenario = json!({ "tool": "fastboot", "argv": argv, "responses": responses });
    if let Value::Object(map) = extra {
        for (k, v) in map {
            scenario[k] = v;
        }
    }
    scenario
}

fn boot_replies(tail: Vec<Value>, unlocked: &str, size: &str, maximum: &str) -> Vec<Value> {
    let mut list = vec![okay(IV.product), okay(unlocked), okay(size), okay(maximum)];
    list.extend(tail);
    list
}

fn fastboot_scenarios() -> Vec<(String, Value)> {
    let small = img(json!({}));
    let large = img(json!({"kernel": 20000, "length": 24576}));
    let sha_small = sha_of(&small);
    let sha_large = sha_of(&large);
    let boot_argv = |sha: &str| -> Vec<String> {
        vec![
            "--boot".into(),
            "{dir}/c.img".into(),
            "--sha256".into(),
            sha.into(),
        ]
    };
    let with_file = |mut scenario: Value, spec: &Value| {
        scenario["files"] = json!({ "c.img": spec });
        scenario
    };
    let boot = |sha: &str, responses: Vec<Value>, extra: Value| {
        let argv = boot_argv(sha);
        fastboot(argv.iter().map(String::as_str).collect(), responses, extra)
    };
    let ok_boot = |len: usize| vec![data_reply(len), okay(""), okay("")];
    let mut list: Vec<(String, Value)> = Vec::new();
    for query in [
        "product",
        "unlocked",
        "secure",
        "max-download-size",
        "partition-size:boot",
        "partition-size:recovery",
    ] {
        list.push((
            format!("fastboot: query {query}"),
            fastboot(
                vec!["--query", query],
                vec![b(b"INFOx"), okay(IV.product), okay("0x123")],
                json!({}),
            ),
        ));
    }
    list.extend([
        (
            "fastboot: query wrong product".into(),
            fastboot(vec!["--query", "unlocked"], vec![okay("other")], json!({})),
        ),
        (
            "fastboot: query non-ASCII".into(),
            fastboot(
                vec!["--query", "secure"],
                vec![okay(IV.product), b(b"OKAY\xc3\xa9")],
                json!({}),
            ),
        ),
        (
            "fastboot: reboot ok".into(),
            fastboot(
                vec!["--reboot"],
                vec![okay(IV.product), okay("")],
                json!({}),
            ),
        ),
        (
            "fastboot: reboot FAIL".into(),
            fastboot(
                vec!["--reboot"],
                vec![okay(IV.product), b(b"FAILno")],
                json!({}),
            ),
        ),
        (
            "fastboot: reboot timeout".into(),
            fastboot(
                vec!["--reboot"],
                vec![okay(IV.product), timeout()],
                json!({}),
            ),
        ),
        (
            "fastboot: reboot short write".into(),
            fastboot(
                vec!["--reboot"],
                vec![okay(IV.product)],
                json!({"short_write": 2}),
            ),
        ),
        (
            "fastboot: reboot interrupted".into(),
            fastboot(
                vec!["--reboot"],
                vec![okay(IV.product), interrupt()],
                json!({}),
            ),
        ),
        (
            "fastboot: reboot interrupted + close error".into(),
            fastboot(
                vec!["--reboot"],
                vec![interrupt()],
                json!({"close_error": true}),
            ),
        ),
        (
            "fastboot: close error".into(),
            fastboot(
                vec!["--query", "product"],
                vec![okay(IV.product)],
                json!({"close_error": true}),
            ),
        ),
        (
            "fastboot: connect io".into(),
            fastboot(vec!["--reboot"], vec![], json!({"connect": "io"})),
        ),
        (
            "fastboot: connect other".into(),
            fastboot(vec!["--reboot"], vec![], json!({"connect": "other"})),
        ),
        (
            "fastboot: boot ok".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(ok_boot(6144), "yes", "0x1000000", "0x2000000"),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot multi-chunk".into(),
            with_file(
                boot(
                    &sha_large,
                    boot_replies(ok_boot(24576), "yes", "0x1000000", "0x2000000"),
                    json!({}),
                ),
                &large,
            ),
        ),
        (
            "fastboot: boot locked".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(vec![], "no", "0x1000000", "0x2000000"),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot partition mismatch".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(vec![], "yes", "0x800000", "0x2000000"),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot partition unknown".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(vec![], "yes", "?", "0x2000000"),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot max too small".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(vec![], "yes", "0x1000000", "0x100"),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot download FAIL".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(vec![b(b"FAILx")], "yes", "0x1000000", "0x2000000"),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot final FAIL".into(),
            with_file(
                boot(
                    &sha_small,
                    boot_replies(
                        vec![data_reply(6144), okay(""), b(b"FAILboot")],
                        "yes",
                        "0x1000000",
                        "0x2000000",
                    ),
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: boot failed chunk write".into(),
            with_file(
                boot(
                    &sha_large,
                    boot_replies(ok_boot(24576), "yes", "0x1000000", "0x2000000"),
                    json!({"failed_write": 7}),
                ),
                &large,
            ),
        ),
        (
            "fastboot: boot interrupted chunk".into(),
            with_file(
                boot(
                    &sha_large,
                    boot_replies(ok_boot(24576), "yes", "0x1000000", "0x2000000"),
                    json!({"interrupt_write": 6}),
                ),
                &large,
            ),
        ),
        (
            "fastboot: boot hash mismatch".into(),
            with_file(boot(&"0".repeat(64), vec![], json!({})), &small),
        ),
        (
            "fastboot: boot image too large".into(),
            with_file(
                boot(&"0".repeat(64), vec![], json!({})),
                &json!({"repeat": [0, 0x100_0001]}),
            ),
        ),
        (
            "fastboot: boot missing file".into(),
            boot(&sha_small, vec![], json!({})),
        ),
        ("fastboot: boot directory".into(), {
            let mut s = boot(&sha_small, vec![], json!({}));
            s["argv"][1] = json!("{dir}");
            s
        }),
        (
            "fastboot: boot empty sha".into(),
            with_file(boot("", vec![], json!({})), &small),
        ),
        (
            "fastboot: boot with --query too".into(),
            with_file(
                fastboot(
                    vec!["--boot", "{dir}/c.img", "--query", "product"],
                    vec![],
                    json!({}),
                ),
                &small,
            ),
        ),
        (
            "fastboot: query with sha".into(),
            fastboot(
                vec!["--query", "product", "--sha256", "ab"],
                vec![okay(IV.product)],
                json!({}),
            ),
        ),
    ]);
    for mutation in ["none", "two", "vid", "ep_packet", "ok"] {
        list.push((
            format!("fastboot: identity {mutation}"),
            fastboot(
                vec!["--reboot"],
                vec![okay(IV.product), okay("")],
                json!({"connect": format!("select:{mutation}")}),
            ),
        ));
    }
    // argparse behaviour (exit 2 / help 0); valid ones fall through to the fake.
    let argvs: Vec<Vec<&str>> = vec![
        vec![],
        vec!["--help"],
        vec!["-h"],
        vec!["--query", "x"],
        vec!["--query"],
        vec!["--reboot", "--query", "product"],
        vec!["--query", "product", "--reboot"],
        vec!["--boot", "a"],
        vec!["--sha256", "a"],
        vec!["--reboot", "--sha256", "-x"],
        vec!["--reb=1"],
        vec!["--q", "product", "x"],
        vec!["-x", "--reboot"],
        vec!["--reboot", "--reboot"],
        vec!["--query=product"],
        vec!["--qu=unlocked"],
        vec!["--query", "product", "--", "x"],
        vec!["--reboot", "-5"],
        vec!["--reboot", "x y"],
        vec!["--reboot", "-"],
        vec!["--sha256=", "--reboot"],
        vec!["-hx"],
        vec!["--he"],
        vec!["--reboot", "--help"],
        vec!["--query", "-1"],
        vec!["--query", "serialno"],
        vec!["--boot", "--reboot"],
        vec!["--reboot", "--x=1"],
        vec!["--reboot", "--"],
    ];
    for argv in argvs {
        list.push((
            format!("fastboot argv {argv:?}"),
            fastboot(
                argv,
                vec![okay(IV.product), okay("no"), okay("")],
                json!({}),
            ),
        ));
    }
    let install_argvs: Vec<Vec<&str>> = vec![
        vec![],
        vec!["--help"],
        vec!["--image", "x"],
        vec!["--s", "x"],
        vec!["--st", "x"],
        vec![
            "--image",
            "x",
            "--sha256",
            "0",
            "--stock-backup",
            "x",
            "--attempt",
            "x",
            "--partition",
            "recovery",
        ],
        vec![
            "--image",
            "x",
            "--sha256",
            "--stock-backup",
            "x",
            "--attempt",
            "x",
        ],
        vec!["--image=x", "--sha=0", "--stock=x", "--att=x"],
        vec!["--image", "x", "--image"],
    ];
    for argv in install_argvs {
        list.push((
            format!("install argv {argv:?}"),
            json!({ "tool": "install", "argv": argv, "responses": [] }),
        ));
    }
    list
}

/// The install sessions need the stock boot backup, a local file outside Git.
fn stock_present() -> bool {
    let present = std::path::Path::new(STOCK).exists();
    if !present {
        eprintln!("skipped install sessions: {STOCK} missing (local backup, not in Git)");
    }
    present
}

#[test]
fn differential_cli_sessions() {
    let install = if stock_present() {
        install_scenarios()
    } else {
        Vec::new()
    };
    let scenarios: Vec<(String, Value)> = install.into_iter().chain(fastboot_scenarios()).collect();
    let total = scenarios.len();
    let failures: Vec<String> = scenarios
        .iter()
        .filter_map(|(name, scenario)| compare(name, scenario))
        .collect();
    eprintln!(
        "differential CLI sessions: {} of {total} identical",
        total - failures.len()
    );
    assert!(
        failures.is_empty(),
        "{} mismatches:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

#[test]
fn differential_receipt_matches_v9_evidence_shape() {
    // The real v9 receipt's key sets per event, compared to ours.
    if !stock_present() {
        return;
    }
    let evidence = std::fs::read_to_string(format!(
        "{PROJECT}/evidence/2026-11-04/i14-boot-install/attempt-v9b.jsonl"
    ))
    .unwrap();
    let dir = canonical_dir("v9shape");
    let scenario = install(
        img(json!({})),
        None,
        install_replies(&IV, vec![data_reply(6144), okay(""), okay("")]),
        json!({}),
    );
    let mut request = scenario.clone();
    request["dir"] = json!(dir.to_str().unwrap());
    let result = rust(&request);
    let ours: Vec<Value> = result["receipt"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| serde_json::from_str(l.as_str().unwrap()).unwrap())
        .collect();
    let theirs: Vec<Value> = evidence
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(ours.len(), theirs.len());
    for (a, b) in ours.iter().zip(&theirs) {
        let keys = |v: &Value| v.as_object().unwrap().keys().cloned().collect::<Vec<_>>();
        assert_eq!(keys(a), keys(b));
        for key in [
            "event",
            "nv_write",
            "readback",
            "partition",
            "product",
            "expected_port",
            "padding",
            "stock_bytes",
            "stock_sha256",
            "command",
        ] {
            assert_eq!(a.get(key), b.get(key), "{key}");
        }
    }
}

#[test]
fn differential_int16() {
    let mut values: Vec<String> = [
        "0x1000000",
        "1000000",
        "0X1000000",
        " 0x10 ",
        "+0x10",
        "-0x10",
        "0x_10",
        "0x__10",
        "1_0",
        "_10",
        "10_",
        "0x",
        "",
        " ",
        "0",
        "-0",
        "0x0",
        "unknown",
        "0b1",
        "0o7",
        "1e3",
        "\u{1c}10\u{1f}",
        "\u{b}10\u{c}",
        "10\u{0}",
        "0x1000000\n",
        "- 1",
        "+-1",
        "ffffffffffffffffffffffffffffffffffffffffff",
        "-ffffffffffffffffffffffffffffffffffffffff",
        "\u{85}1",
        "١٢",
        "0x 1",
        "1 0",
        "0_x1",
        "0x1_",
        "FFFFFFFF",
        "deadbeef",
    ]
    .iter()
    .map(|s| (*s).to_owned())
    .collect();
    let mut seed = 0x1234_5678_u64;
    let alphabet = b"0123456789abcdefABCDEFxX_+- \t";
    for _ in 0..400 {
        let mut text = String::new();
        seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
        let len = (seed >> 60) as usize;
        for _ in 0..len {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            text.push(char::from(alphabet[(seed >> 33) as usize % alphabet.len()]));
        }
        values.push(text);
    }
    let py = python(&json!({ "tool": "int16", "values": values }));
    // Rust only ever sees ASCII payloads (getvar is ASCII-checked first).
    let rs: Vec<Value> = values
        .iter()
        .map(|v| {
            if v.is_ascii() {
                py_int16(v).map_or(Value::Null, |n| json!(n.to_string()))
            } else {
                Value::Null
            }
        })
        .collect();
    let mut mismatches = Vec::new();
    for ((value, a), b) in values.iter().zip(py.as_array().unwrap()).zip(&rs) {
        if value.is_ascii() && a != b {
            mismatches.push(format!("{value:?}: python {a} rust {b}"));
        }
    }
    eprintln!("differential int16: {} values", values.len());
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
}

#[test]
fn differential_validate_image() {
    let mut cases = Vec::new();
    let mut seed = 0x9e37_79b9_u64;
    let mut next = move || {
        seed = seed
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1442695040888963407);
        seed >> 33
    };
    let pages = [
        0u64, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072, 2049,
    ];
    for _ in 0..300 {
        let page = if next() % 2 == 0 {
            2048
        } else {
            pages[next() as usize % pages.len()]
        };
        let length = [1631u64, 1632, 2048, 4096, 6144, 8192, 24576, 70000][next() as usize % 8];
        let size = |n: u64| match n % 8 {
            0 => 0,
            1 | 2 => n % 64,
            3..=5 => n % 3000,
            6 => n % 70000,
            _ => 0xffff_ffff,
        };
        let fields = json!({
            "kernel": size(next()), "ramdisk": size(next()), "second": size(next()),
            "page": page, "version": if next() % 6 == 0 { 1 } else { 0 }, "length": length,
        });
        let mut spec = img(fields);
        if next() % 10 == 0 {
            spec["patch"] = json!([0, "414e44524f494422"]);
        }
        let sha = match next() % 8 {
            0 => "0".repeat(64),
            1 => sha_of(&spec).to_uppercase(),
            2 => sha_of(&spec)[..63].to_owned(),
            _ => sha_of(&spec),
        };
        cases.push(json!({ "file": spec, "sha256": sha }));
    }
    let py = python(&json!({ "tool": "validate", "cases": cases }));
    let mut mismatches = Vec::new();
    let mut accepted = 0;
    for (case, expected) in cases.iter().zip(py.as_array().unwrap()) {
        let data = make_file(&case["file"]);
        let got = match validate_image(&data, case["sha256"].as_str()) {
            Ok(_) => "ok".to_owned(),
            Err(Error::Fastboot(message)) => message,
            Err(other) => format!("{other:?}"),
        };
        if got == "ok" {
            accepted += 1;
        }
        if json!(got) != *expected {
            mismatches.push(format!("{case}: python {expected} rust {got}"));
        }
    }
    eprintln!(
        "differential validate_image: {} cases, {accepted} accepted",
        cases.len()
    );
    assert!(accepted > 10, "fuzz should accept some images");
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
}
