//! Host-only synthetic fixtures for i14-verify-backup: a tiny-partition
//! 23-entry two-copy GPT built independently from the UEFI 1.0 layout (own
//! bitwise CRC32, own UTF-16 encoder), 4 MiB boot-region files and sha256sum
//! manifests. No private backup data, no phone access. Every fault below is
//! constructed in the fixture, not derived from the verifier's own code.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, SystemTime};

use i14_tools::verify_backup::manifest;
use sha2::{Digest, Sha256};

const SECTOR: u64 = 512;
const DISK_LBAS: u64 = 8192; // 4 MiB synthetic disk
const DISK_SIZE: u64 = DISK_LBAS * SECTOR;
const CAP: usize = 0x100000;
const TAIL_OFFSET: u64 = DISK_SIZE - CAP as u64;
const FIRST_USABLE: u64 = 34;
const LAST_USABLE: u64 = DISK_LBAS - 34;
const SLOTS: usize = 128;
const ARRAY_LEN: usize = SLOTS * 128;
const P_HDR: usize = 512; // primary header at LBA 1
const P_ARR: usize = 1024; // primary array at LBA 2
const B_HDR: usize = CAP - 512; // backup header at the disk's last LBA
const B_ARR: usize = B_HDR - ARRAY_LEN;
const TYPE_GUID: [u8; 16] = [0xAB; 16];
const DISK_GUID: [u8; 16] = [0x14; 16];

/// scripts/phase0-backup.mtk's partitions plus userdata, independently listed.
const NAMES: [&str; 23] = [
    "proinfo",
    "nvram",
    "protect1",
    "protect2",
    "lk",
    "para",
    "boot",
    "recovery",
    "logo",
    "expdb",
    "seccfg",
    "oemkeystore",
    "secro",
    "keystore",
    "tee1",
    "tee2",
    "frp",
    "nvdata",
    "metadata",
    "system",
    "cache",
    "userdata",
    "flashinfo",
];

/// Bitwise IEEE CRC32 (zlib polynomial), deliberately not the crate's table code.
fn crc32(data: &[u8]) -> u32 {
    let mut crc = 0xFFFF_FFFFu32;
    for &b in data {
        crc ^= u32::from(b);
        for _ in 0..8 {
            crc = if crc & 1 != 0 {
                (crc >> 1) ^ 0xEDB8_8320
            } else {
                crc >> 1
            };
        }
    }
    !crc
}

fn put_u32(b: &mut [u8], off: usize, v: u32) {
    b[off..off + 4].copy_from_slice(&v.to_le_bytes());
}
fn put_u64(b: &mut [u8], off: usize, v: u64) {
    b[off..off + 8].copy_from_slice(&v.to_le_bytes());
}
fn get_u32(b: &[u8], off: usize) -> u32 {
    u32::from_le_bytes(b[off..off + 4].try_into().unwrap())
}

struct Part {
    name: &'static str,
    first: u64,
    last: u64,
    guid: [u8; 16],
    raw_name: Option<Vec<u8>>,
}

fn default_parts() -> Vec<Part> {
    NAMES
        .iter()
        .enumerate()
        .map(|(i, &name)| {
            let mut guid = [0u8; 16];
            guid[0] = i as u8 + 1;
            guid[15] = 0x42;
            Part {
                name,
                first: FIRST_USABLE + 2 * i as u64,
                last: FIRST_USABLE + 2 * i as u64 + 1,
                guid,
                raw_name: None,
            }
        })
        .collect()
}

fn utf16_raw(name: &str) -> Vec<u8> {
    let mut v = vec![0u8; 72];
    for (j, u) in name.encode_utf16().enumerate() {
        v[2 * j..2 * j + 2].copy_from_slice(&u.to_le_bytes());
    }
    v
}

fn encode_entry(p: &Part) -> [u8; 128] {
    let mut e = [0u8; 128];
    e[..16].copy_from_slice(&TYPE_GUID);
    e[16..32].copy_from_slice(&p.guid);
    put_u64(&mut e, 32, p.first);
    put_u64(&mut e, 40, p.last);
    let raw = p.raw_name.clone().unwrap_or_else(|| utf16_raw(p.name));
    e[56..128].copy_from_slice(&raw);
    e
}

fn build_array(parts: &[Part]) -> Vec<u8> {
    let mut a = vec![0u8; ARRAY_LEN];
    for (i, p) in parts.iter().enumerate() {
        a[i * 128..(i + 1) * 128].copy_from_slice(&encode_entry(p));
    }
    a
}

fn fix_header_crc(buf: &mut [u8], hdr: usize) {
    let hs = get_u32(buf, hdr + 12) as usize;
    let mut h = buf[hdr..hdr + hs].to_vec();
    h[16..20].fill(0);
    put_u32(buf, hdr + 16, crc32(&h));
}

fn fix_array_crc(buf: &mut [u8], hdr: usize, arr: usize) {
    let len = get_u32(buf, hdr + 80) as usize * get_u32(buf, hdr + 84) as usize;
    put_u32(buf, hdr + 88, crc32(&buf[arr..arr + len]));
    fix_header_crc(buf, hdr);
}

fn write_header(buf: &mut [u8], hdr: usize, primary: bool, array: &[u8]) {
    buf[hdr..hdr + 8].copy_from_slice(b"EFI PART");
    put_u32(buf, hdr + 8, 0x0001_0000);
    put_u32(buf, hdr + 12, 92);
    put_u32(buf, hdr + 20, 0);
    put_u64(buf, hdr + 24, if primary { 1 } else { DISK_LBAS - 1 });
    put_u64(buf, hdr + 32, if primary { DISK_LBAS - 1 } else { 1 });
    put_u64(buf, hdr + 40, FIRST_USABLE);
    put_u64(buf, hdr + 48, LAST_USABLE);
    buf[hdr + 56..hdr + 72].copy_from_slice(&DISK_GUID);
    put_u64(buf, hdr + 72, if primary { 2 } else { DISK_LBAS - 1 - 32 });
    put_u32(buf, hdr + 80, SLOTS as u32);
    put_u32(buf, hdr + 84, 128);
    put_u32(buf, hdr + 88, crc32(array));
    fix_header_crc(buf, hdr);
}

fn build_captures(parts: &[Part]) -> (Vec<u8>, Vec<u8>) {
    let array = build_array(parts);
    let mut primary = vec![0u8; CAP];
    let mut tail = vec![0u8; CAP];
    primary[P_ARR..P_ARR + ARRAY_LEN].copy_from_slice(&array);
    tail[B_ARR..B_ARR + ARRAY_LEN].copy_from_slice(&array);
    write_header(&mut primary, P_HDR, true, &array);
    write_header(&mut tail, B_HDR, false, &array);
    (primary, tail)
}

fn sha256_file(path: &Path) -> String {
    Sha256::digest(fs::read(path).unwrap())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

struct Fixture {
    dir: PathBuf,
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}

impl Fixture {
    fn new(tag: &str) -> Fixture {
        let dir =
            std::env::temp_dir().join(format!("i14-verify-backup-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let f = Fixture { dir };
        fs::create_dir_all(f.p0()).unwrap();
        fs::create_dir_all(f.ud()).unwrap();
        for (i, &n) in NAMES.iter().enumerate() {
            let body = vec![(i as u8).wrapping_mul(31).wrapping_add(7); 1024];
            let path = if n == "userdata" {
                f.ud().join("userdata.img")
            } else {
                f.p0().join(format!("{n}.img"))
            };
            fs::write(path, body).unwrap();
        }
        fs::write(
            f.p0().join("boot-region1.bin"),
            vec![0xA5u8; 4 * 1024 * 1024],
        )
        .unwrap();
        fs::write(
            f.p0().join("boot-region2.bin"),
            vec![0x5Au8; 4 * 1024 * 1024],
        )
        .unwrap();
        let (primary, tail) = build_captures(&default_parts());
        fs::write(f.p0().join("primary-userarea.bin"), primary).unwrap();
        fs::write(f.p0().join("tail-userarea.bin"), tail).unwrap();
        let lines: Vec<String> = f.p0_names().iter().map(|n| f.line_for(n)).collect();
        f.write_phase0_manifest(&(lines.join("\n") + "\n"));
        let hash = sha256_file(&f.ud().join("userdata.img"));
        fs::write(f.ud().join("SHA256SUMS"), format!("{hash}  userdata.img\n")).unwrap();
        f
    }

    fn p0(&self) -> PathBuf {
        self.dir.join("phase0")
    }
    fn ud(&self) -> PathBuf {
        self.dir.join("userdata")
    }
    fn p0_names(&self) -> Vec<String> {
        let mut v: Vec<String> = NAMES
            .iter()
            .filter(|&&n| n != "userdata")
            .map(|n| format!("{n}.img"))
            .collect();
        v.extend(
            [
                "boot-region1.bin",
                "boot-region2.bin",
                "primary-userarea.bin",
                "tail-userarea.bin",
            ]
            .map(String::from),
        );
        v
    }
    fn line_for(&self, name: &str) -> String {
        let marker = if name.ends_with(".bin") { '*' } else { ' ' };
        format!("{} {marker}{name}", sha256_file(&self.p0().join(name)))
    }
    fn write_phase0_manifest(&self, text: &str) {
        fs::write(self.p0().join("SHA256SUMS"), text).unwrap();
    }
    fn phase0_text(&self) -> String {
        fs::read_to_string(self.p0().join("SHA256SUMS")).unwrap()
    }
    fn map_lines(&self, f: impl Fn(&str) -> Option<String>) {
        let text = self.phase0_text();
        let out: Vec<String> = text.lines().filter_map(f).collect();
        self.write_phase0_manifest(&(out.join("\n") + "\n"));
    }
    fn rehash(&self, name: &str) {
        let hash = sha256_file(&self.p0().join(name));
        self.map_lines(|line| {
            if line.len() > 66 && &line[66..] == name {
                Some(format!("{hash}{}", &line[64..]))
            } else {
                Some(line.to_string())
            }
        });
    }
    fn remove_line(&self, name: &str) {
        self.map_lines(|line| {
            if line.len() > 66 && &line[66..] == name {
                None
            } else {
                Some(line.to_string())
            }
        });
    }
    fn append_line(&self, line: &str) {
        let text = self.phase0_text();
        self.write_phase0_manifest(&format!("{text}{line}\n"));
    }
    fn patch_capture(&self, name: &str, f: impl FnOnce(&mut [u8])) {
        let p = self.p0().join(name);
        let mut b = fs::read(&p).unwrap();
        f(&mut b);
        fs::write(&p, b).unwrap();
        self.rehash(name);
    }
    fn patch_header(&self, primary: bool, off: usize, bytes: &[u8]) {
        let (name, hdr) = if primary {
            ("primary-userarea.bin", P_HDR)
        } else {
            ("tail-userarea.bin", B_HDR)
        };
        self.patch_capture(name, |b| {
            b[hdr + off..hdr + off + bytes.len()].copy_from_slice(bytes);
            fix_header_crc(b, hdr);
        });
    }
    fn patch_entry(&self, idx: usize, off: usize, bytes: &[u8]) {
        self.patch_capture("primary-userarea.bin", |b| {
            let at = P_ARR + idx * 128 + off;
            b[at..at + bytes.len()].copy_from_slice(bytes);
            fix_array_crc(b, P_HDR, P_ARR);
        });
    }
    fn rebuild_captures(&self, parts: &[Part]) {
        let (primary, tail) = build_captures(parts);
        fs::write(self.p0().join("primary-userarea.bin"), primary).unwrap();
        fs::write(self.p0().join("tail-userarea.bin"), tail).unwrap();
        self.rehash("primary-userarea.bin");
        self.rehash("tail-userarea.bin");
    }

    fn args(&self) -> Vec<String> {
        vec![
            "--phase0-manifest".into(),
            self.p0().join("SHA256SUMS").display().to_string(),
            "--userdata-manifest".into(),
            self.ud().join("SHA256SUMS").display().to_string(),
            "--primary".into(),
            self.p0().join("primary-userarea.bin").display().to_string(),
            "--tail".into(),
            self.p0().join("tail-userarea.bin").display().to_string(),
            "--tail-offset".into(),
            format!("0x{TAIL_OFFSET:x}"),
            "--disk-size".into(),
            DISK_SIZE.to_string(),
        ]
    }
    fn run(&self) -> Output {
        self.run_args(self.args())
    }
    fn run_args(&self, args: Vec<String>) -> Output {
        Command::new(env!("CARGO_BIN_EXE_i14-verify-backup"))
            .args(args)
            .output()
            .unwrap()
    }
}

fn out(o: &Output) -> String {
    String::from_utf8_lossy(&o.stdout).into_owned()
}
fn err(o: &Output) -> String {
    String::from_utf8_lossy(&o.stderr).into_owned()
}
fn expect_code(o: &Output, code: i32) {
    assert_eq!(
        o.status.code(),
        Some(code),
        "expected exit {code}\nstdout:\n{}\nstderr:\n{}",
        out(o),
        err(o)
    );
}
fn contains(o: &Output, needle: &str) {
    assert!(
        out(o).contains(needle),
        "missing {needle:?}\nstdout:\n{}",
        out(o)
    );
}

#[test]
fn fixture_crc32_matches_the_check_vector() {
    assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
}

#[test]
fn complete_backup_passes() {
    let f = Fixture::new("complete");
    let o = f.run();
    expect_code(&o, 0);
    assert!(err(&o).is_empty(), "stderr: {}", err(&o));
    contains(&o, "files: 27/27 manifest hashes PASS");
    contains(&o, "partitions: 23/23 GPT size matches");
    contains(
        &o,
        "GPT primary: header CRC, array CRC and layout valid; 23 used entries",
    );
    contains(
        &o,
        "GPT backup: header CRC, array CRC and layout valid; 23 used entries",
    );
    contains(
        &o,
        "GPT cross-check: disk GUID, usable bounds, reciprocal LBAs and identical arrays",
    );
    contains(&o, "phase0/boot.img: SHA-256 matches (1024 bytes)");
    contains(&o, "userdata/userdata.img: SHA-256 matches (1024 bytes)");
    contains(&o, "partition userdata: 1024 bytes matches GPT");
    contains(&o, "RESULT: PASS");
    contains(&o, "no restore acceptance");
}

#[test]
fn uppercase_hashes_and_binary_markers_pass() {
    let f = Fixture::new("markers");
    let text = f.phase0_text();
    let upper: String = text
        .lines()
        .map(|l| format!("{} *{}", l[..64].to_uppercase(), &l[66..]))
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    f.write_phase0_manifest(&upper);
    let o = f.run();
    expect_code(&o, 0);
    contains(&o, "RESULT: PASS");
}

#[test]
fn corrupted_image_fails_only_that_file() {
    let f = Fixture::new("corrupt-image");
    let p = f.p0().join("boot.img");
    let mut b = fs::read(&p).unwrap();
    b[10] ^= 0xFF;
    fs::write(&p, b).unwrap();
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "FAIL phase0/boot.img: SHA-256 mismatch");
    contains(&o, "PASS phase0/recovery.img: SHA-256 matches");
    contains(&o, "files: 26/27");
    contains(&o, "partitions: 23/23");
    contains(&o, "RESULT: FAIL");
}

#[test]
fn wrong_partition_size_fails_after_rehash() {
    let f = Fixture::new("wrong-size");
    fs::write(f.p0().join("system.img"), vec![0u8; 512]).unwrap();
    f.rehash("system.img");
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "PASS phase0/system.img: SHA-256 matches");
    contains(&o, "FAIL partition system: file 512 bytes, GPT 1024 bytes");
    contains(&o, "partitions: 22/23");
}

#[test]
fn boot_region_wrong_size_fails() {
    let f = Fixture::new("boot-region-size");
    fs::write(
        f.p0().join("boot-region1.bin"),
        vec![0xA5u8; 4 * 1024 * 1024 - 512],
    )
    .unwrap();
    f.rehash("boot-region1.bin");
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "FAIL boot-region1.bin: 4193792 bytes, expected 4194304");
}

#[test]
fn malformed_manifest_lines_fail() {
    let f = Fixture::new("malformed");
    let good = f.phase0_text();
    let first = good.lines().next().unwrap().to_string();
    let hash = &first[..64];
    for bad in [
        format!("{}x  boot.img\n", &hash[..63]),
        format!("{}g  boot.img\n", &hash[..63]),
        format!("{hash}?boot.img\n"),
        format!("{hash}  boot.img\n\n"),
        format!("\\{hash}  boot\\nimg\n"),
        format!("{hash}  boot.img\r\n"),
        "garbage\n".to_string(),
        String::new(),
    ] {
        f.write_phase0_manifest(&bad);
        let o = f.run();
        expect_code(&o, 1);
        contains(&o, "FAIL phase0 manifest:");
        contains(&o, "RESULT: FAIL");
    }
    f.write_phase0_manifest(&good);
    expect_code(&f.run(), 0);
}

#[test]
fn duplicate_entry_within_one_manifest_fails() {
    let f = Fixture::new("dup-within");
    let line = f.line_for("boot.img");
    f.append_line(&line);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "duplicate entry 'boot.img'");
}

#[test]
fn duplicate_entry_across_manifests_fails() {
    let f = Fixture::new("dup-across");
    let hash = sha256_file(&f.ud().join("userdata.img"));
    f.append_line(&format!("{hash}  userdata.img"));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "duplicate entry across manifests: 'userdata.img'");
}

#[test]
fn traversal_and_absolute_paths_fail() {
    let f = Fixture::new("traversal");
    let hash = sha256_file(&f.ud().join("userdata.img"));
    for name in [
        "../userdata/userdata.img",
        "/etc/passwd",
        "a/../../x",
        "./../x",
    ] {
        f.write_phase0_manifest(&format!("{hash}  {name}\n"));
        let o = f.run();
        expect_code(&o, 1);
        contains(&o, "FAIL phase0 manifest:");
    }
}

#[test]
fn symlink_entries_fail_inside_and_outside() {
    let f = Fixture::new("symlink");
    let outside = f.dir.join("outside.img");
    fs::write(&outside, b"outside").unwrap();
    std::os::unix::fs::symlink(&outside, f.p0().join("alias.img")).unwrap();
    f.append_line(&f.line_for("alias.img"));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "symlink component 'alias.img' refused");

    // A symlink to a file inside the same directory is refused as well.
    fs::remove_file(f.p0().join("alias.img")).unwrap();
    std::os::unix::fs::symlink(f.p0().join("boot.img"), f.p0().join("alias.img")).unwrap();
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "symlink component 'alias.img' refused");

    // A symlinked directory component cannot smuggle files in either.
    let sub = f.dir.join("elsewhere");
    fs::create_dir_all(&sub).unwrap();
    fs::write(sub.join("x.img"), b"x").unwrap();
    std::os::unix::fs::symlink(&sub, f.p0().join("sub")).unwrap();
    f.append_line(&format!("{}  sub/x.img", sha256_file(&sub.join("x.img"))));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "symlink component 'sub' refused");
}

#[test]
fn hardlink_alias_fails() {
    let f = Fixture::new("hardlink");
    fs::hard_link(f.p0().join("boot.img"), f.p0().join("alias.img")).unwrap();
    f.append_line(&f.line_for("alias.img"));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "same file as 'boot.img' (hardlink alias)");
}

#[test]
fn missing_image_fails() {
    let f = Fixture::new("missing-image");
    fs::remove_file(f.p0().join("cache.img")).unwrap();
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "FAIL phase0/cache.img:");
    contains(&o, "files: 26/27");
}

#[test]
fn missing_manifest_entry_fails() {
    let f = Fixture::new("missing-entry");
    f.remove_line("flashinfo.img");
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "missing expected entry 'flashinfo.img'");
}

#[test]
fn extra_manifest_entry_fails() {
    let f = Fixture::new("extra-entry");
    fs::write(f.p0().join("bonus.img"), b"bonus").unwrap();
    f.append_line(&f.line_for("bonus.img"));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "unexpected entry 'bonus.img'");
}

#[test]
fn directory_instead_of_image_fails() {
    let f = Fixture::new("not-regular");
    fs::remove_file(f.p0().join("nvram.img")).unwrap();
    fs::create_dir(f.p0().join("nvram.img")).unwrap();
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "FAIL phase0/nvram.img:");
    contains(&o, "not a regular file");
}

#[test]
fn gpt_cli_path_must_be_the_hashed_file() {
    let f = Fixture::new("gpt-path");
    let copy = f.dir.join("other-primary.bin");
    fs::copy(f.p0().join("primary-userarea.bin"), &copy).unwrap();
    let mut args = f.args();
    let i = args.iter().position(|a| a == "--primary").unwrap();
    args[i + 1] = copy.display().to_string();
    let o = f.run_args(args);
    expect_code(&o, 1);
    contains(&o, "is not the file hashed from the manifest");
}

#[test]
fn gpt_capture_missing_from_manifest_fails() {
    let f = Fixture::new("gpt-uncovered");
    f.remove_line("primary-userarea.bin");
    let o = f.run();
    expect_code(&o, 1);
    contains(
        &o,
        "primary-userarea.bin: not covered by the phase0 manifest",
    );
}

#[test]
fn header_crc_corruption_in_each_copy_fails() {
    for primary in [true, false] {
        let f = Fixture::new(if primary { "hdr-crc-p" } else { "hdr-crc-b" });
        let (name, hdr) = if primary {
            ("primary-userarea.bin", P_HDR)
        } else {
            ("tail-userarea.bin", B_HDR)
        };
        f.patch_capture(name, |b| b[hdr + 16] ^= 1);
        let o = f.run();
        expect_code(&o, 1);
        contains(
            &o,
            if primary {
                "FAIL GPT primary: header CRC mismatch"
            } else {
                "FAIL GPT backup: header CRC mismatch"
            },
        );
    }
}

#[test]
fn array_crc_corruption_in_each_copy_fails() {
    for primary in [true, false] {
        let f = Fixture::new(if primary { "arr-crc-p" } else { "arr-crc-b" });
        let (name, arr) = if primary {
            ("primary-userarea.bin", P_ARR)
        } else {
            ("tail-userarea.bin", B_ARR)
        };
        f.patch_capture(name, |b| b[arr + 60] ^= 1);
        let o = f.run();
        expect_code(&o, 1);
        contains(
            &o,
            if primary {
                "FAIL GPT primary: entry array CRC mismatch"
            } else {
                "FAIL GPT backup: entry array CRC mismatch"
            },
        );
    }
}

#[test]
fn bad_signature_and_unsupported_revision_fail() {
    let f = Fixture::new("signature");
    f.patch_capture("primary-userarea.bin", |b| {
        b[P_HDR..P_HDR + 8].copy_from_slice(b"BAD SIGN")
    });
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "bad GPT signature");

    let f = Fixture::new("revision");
    f.patch_header(true, 8, &0x0002_0000u32.to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "unsupported GPT revision");
}

#[test]
fn inconsistent_backup_pointer_fails() {
    let f = Fixture::new("alt-lba");
    f.patch_header(true, 32, &5u64.to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "alternate_lba 5 != expected");

    let f = Fixture::new("my-lba");
    f.patch_header(false, 24, &(DISK_LBAS - 2).to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "my_lba");
}

#[test]
fn disk_guid_mismatch_fails() {
    let f = Fixture::new("guid");
    f.patch_header(false, 56, &[0x99u8; 16]);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "disk GUID differs between GPT copies");
}

#[test]
fn matching_zero_disk_guids_in_i14_backups_are_accepted() {
    let f = Fixture::new("zero-disk-guid");
    f.patch_header(true, 56, &[0; 16]);
    f.patch_header(false, 56, &[0; 16]);
    let o = f.run();
    expect_code(&o, 0);
    contains(&o, "RESULT: PASS");

    f.patch_header(false, 56, &DISK_GUID);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "disk GUID differs between GPT copies");
}

#[test]
fn usable_bounds_mismatch_fails() {
    let f = Fixture::new("usable");
    f.patch_header(false, 48, &(LAST_USABLE - 2).to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "usable LBA bounds differ between GPT copies");
}

#[test]
fn differing_arrays_fail() {
    let f = Fixture::new("arrays-differ");
    f.patch_capture("tail-userarea.bin", |b| {
        let raw = utf16_raw("bootx");
        b[B_ARR + 56..B_ARR + 128].copy_from_slice(&raw);
        fix_array_crc(b, B_HDR, B_ARR);
    });
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "entry arrays differ between GPT copies");
}

#[test]
fn overlapping_partitions_fail() {
    let f = Fixture::new("overlap");
    f.patch_entry(1, 32, &FIRST_USABLE.to_le_bytes()); // nvram starts where proinfo starts
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "overlapping partitions 'proinfo'");
}

#[test]
fn out_of_bounds_partition_fails() {
    let f = Fixture::new("oob");
    f.patch_entry(0, 40, &(LAST_USABLE + 1).to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "outside usable");

    let f = Fixture::new("reversed");
    f.patch_entry(0, 32, &100u64.to_le_bytes()); // first > last (last is 35)
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "past last LBA");
}

#[test]
fn duplicate_or_zero_partition_guid_fails() {
    let f = Fixture::new("dup-guid");
    let guid0 = default_parts()[0].guid;
    f.patch_entry(1, 16, &guid0);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "duplicate partition GUID");

    let f = Fixture::new("zero-guid");
    f.patch_entry(0, 16, &[0u8; 16]);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "zero partition GUID");
}

#[test]
fn duplicate_partition_name_fails() {
    let f = Fixture::new("dup-name");
    f.patch_entry(1, 56, &utf16_raw("proinfo"));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "duplicate partition name 'proinfo'");
}

#[test]
fn invalid_utf16_names_fail() {
    let f = Fixture::new("empty-name");
    f.patch_entry(0, 56, &[0u8; 72]);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "empty partition name");

    let f = Fixture::new("lone-surrogate");
    let mut raw = vec![0u8; 72];
    raw[..2].copy_from_slice(&0xD83Du16.to_le_bytes());
    f.patch_entry(0, 56, &raw);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "not valid UTF-16");

    let f = Fixture::new("bad-padding");
    let mut raw = utf16_raw("boot");
    raw[71] = 1;
    f.patch_entry(0, 56, &raw);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "nonzero padding after partition name");

    let f = Fixture::new("unterminated");
    let mut raw = vec![0u8; 72];
    for j in 0..36 {
        raw[2 * j..2 * j + 2].copy_from_slice(&u16::from(b'x').to_le_bytes());
    }
    raw[70..72].copy_from_slice(&u16::from(b'y').to_le_bytes());
    f.patch_entry(0, 56, &raw);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "not null-terminated");
}

#[test]
fn missing_or_extra_gpt_partition_fails() {
    let f = Fixture::new("gpt-22");
    let mut parts = default_parts();
    parts.pop(); // drop flashinfo
    f.rebuild_captures(&parts);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "GPT partitions: missing expected entry 'flashinfo'");
    contains(&o, "partitions: 22/23");

    let f = Fixture::new("gpt-24");
    let mut parts = default_parts();
    let mut guid = [0u8; 16];
    guid[0] = 99;
    parts.push(Part {
        name: "bonus",
        first: 200,
        last: 201,
        guid,
        raw_name: None,
    });
    f.rebuild_captures(&parts);
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "GPT partitions: unexpected entry 'bonus'");
}

#[test]
fn entry_count_and_size_bounds_fail() {
    let f = Fixture::new("count-overflow");
    f.patch_header(true, 80, &u32::MAX.to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "entry count 4294967295 out of bounds");

    let f = Fixture::new("entry-size");
    f.patch_header(true, 84, &64u32.to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "unsupported partition entry size 64");

    let f = Fixture::new("zero-count");
    f.patch_header(true, 80, &0u32.to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "entry count 0 out of bounds");
}

#[test]
fn header_bounds_and_reserved_fields_fail() {
    let f = Fixture::new("header-size");
    f.patch_capture("primary-userarea.bin", |b| put_u32(b, P_HDR + 12, 513));
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "header size 513 out of bounds");

    let f = Fixture::new("reserved");
    f.patch_header(true, 20, &1u32.to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "nonzero reserved header field");

    let f = Fixture::new("header-tail");
    f.patch_capture("primary-userarea.bin", |b| {
        put_u32(b, P_HDR + 12, 128);
        b[P_HDR + 100] = 1;
        fix_header_crc(b, P_HDR);
    });
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "nonzero reserved header tail");
}

#[test]
fn unused_slot_must_be_all_zero() {
    let f = Fixture::new("unused-slot");
    f.patch_capture("primary-userarea.bin", |b| {
        b[P_ARR + 23 * 128 + 127] = 1;
        fix_array_crc(b, P_HDR, P_ARR);
    });
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "unused slot is not all-zero");
}

#[test]
fn truncated_capture_fails() {
    let f = Fixture::new("truncated");
    fs::write(f.p0().join("tail-userarea.bin"), vec![0u8; 512]).unwrap();
    f.rehash("tail-userarea.bin");
    let o = f.run();
    expect_code(&o, 1);
    contains(&o, "capture is 512 bytes, expected exactly 1048576");
}

#[test]
fn wrong_disk_geometry_fails() {
    let f = Fixture::new("geometry");
    let mut args = f.args();
    let d = args.iter().position(|a| a == "--disk-size").unwrap();
    let t = args.iter().position(|a| a == "--tail-offset").unwrap();
    args[d + 1] = (DISK_SIZE + SECTOR).to_string();
    args[t + 1] = (TAIL_OFFSET + SECTOR).to_string();
    let o = f.run_args(args);
    expect_code(&o, 1);
    contains(&o, "!= expected 8192");
}

#[test]
fn entry_array_outside_capture_fails() {
    let f = Fixture::new("array-oob");
    // Point the primary array at an LBA whose 16 KiB no longer fits the capture.
    f.patch_header(true, 72, &(DISK_LBAS - 10).to_le_bytes());
    let o = f.run();
    expect_code(&o, 1);
    assert!(
        out(&o).contains("entry array lies outside this capture")
            || out(&o).contains("overlaps the header or usable area"),
        "stdout:\n{}",
        out(&o)
    );
}

#[test]
fn usage_errors_exit_2() {
    let bin = env!("CARGO_BIN_EXE_i14-verify-backup");
    let run = |args: &[&str]| Command::new(bin).args(args).output().unwrap();
    expect_code(&run(&[]), 2);
    expect_code(&run(&["--bogus"]), 2);
    expect_code(&run(&["--primary"]), 2);
    expect_code(&run(&["--disk-size", "abc"]), 2);
    expect_code(&run(&["--disk-size", "1000"]), 2);
    expect_code(&run(&["--tail-offset", "0"]), 2);
    expect_code(
        &run(&["--tail-offset", "0x300000", "--disk-size", "0x400001"]),
        2,
    );
    expect_code(&run(&["--primary", "a", "--primary", "b"]), 2);
    expect_code(&run(&["positional"]), 2);
    let o = run(&["--help"]);
    expect_code(&o, 0);
    contains(&o, "usage:");
    for args in [
        vec!["--disk-size", "abc"],
        vec!["--tail-offset", "0x300000", "--disk-size", "0x400001"],
    ] {
        let o = run(&args);
        assert!(!err(&o).is_empty(), "usage errors belong on stderr");
    }
}

#[test]
fn non_utf8_argument_is_a_usage_error() {
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;

    let output = Command::new(env!("CARGO_BIN_EXE_i14-verify-backup"))
        .arg("--phase0-manifest")
        .arg(OsStr::from_bytes(b"manifest-\xff"))
        .output()
        .unwrap();
    expect_code(&output, 2);
    assert!(err(&output).contains("UTF-8"));
    assert!(!err(&output).contains("panicked"));
}

#[test]
fn missing_manifest_path_fails_with_1() {
    let f = Fixture::new("no-manifest");
    let mut args = f.args();
    let i = args.iter().position(|a| a == "--phase0-manifest").unwrap();
    args[i + 1] = f.dir.join("absent").display().to_string();
    let o = f.run_args(args);
    expect_code(&o, 1);
    contains(&o, "FAIL phase0 manifest:");
}

#[test]
fn streaming_hash_matches_one_shot_over_chunk_boundaries() {
    use std::os::fd::AsRawFd;

    let dir = std::env::temp_dir().join(format!("i14-verify-stream-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let body: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
    fs::write(dir.join("big.img"), &body).unwrap();
    let (file, _) = manifest::open_regular(&dir, "big.img").unwrap();
    // F_GETFL only inspects the flags of this live descriptor.
    let flags = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GETFL) };
    assert!(flags >= 0 && flags & libc::O_NONBLOCK != 0);
    let (id, digest) = manifest::hash_file(&dir, "big.img").unwrap();
    assert_eq!(id.len, body.len() as u64);
    let want: [u8; 32] = Sha256::digest(&body).into();
    assert_eq!(digest, want);
    let _ = fs::remove_dir_all(&dir);
}

#[test]
fn identity_detects_size_and_mtime_changes() {
    let dir = std::env::temp_dir().join(format!("i14-verify-identity-{}", std::process::id()));
    fs::create_dir_all(&dir).unwrap();
    let p = dir.join("x.img");
    fs::write(&p, b"abc").unwrap();
    let (f1, id1) = manifest::open_regular(&dir, "x.img").unwrap();
    drop(f1);

    let mut f = fs::OpenOptions::new().append(true).open(&p).unwrap();
    use std::io::Write;
    f.write_all(b"d").unwrap();
    drop(f);
    let (f2, id2) = manifest::open_regular(&dir, "x.img").unwrap();
    assert_ne!(id1, id2, "size change must change the identity");
    drop(f2);

    // Same size, different mtime: still a different identity.
    let f = fs::OpenOptions::new().write(true).open(&p).unwrap();
    f.set_modified(SystemTime::UNIX_EPOCH + Duration::from_secs(1_000_000))
        .unwrap();
    drop(f);
    let (f3, id3) = manifest::open_regular(&dir, "x.img").unwrap();
    assert_ne!(id2, id3, "mtime change must change the identity");
    drop(f3);
    let _ = fs::remove_dir_all(&dir);
}
