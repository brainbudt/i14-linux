//! Offline, read-only integrity audit of an I14 phase-0 + userdata firmware
//! backup, as the repeatable host-side prerequisite that
//! `docs/RESTORE-PROCEDURE.md` step 2 describes and that
//! `evidence/2026-11-08/restore-rehearsal.txt` performed with a one-off inline
//! Python script.
//!
//! This is host integrity proof only. It never contacts a phone or server,
//! never opens a block device, never writes anything, and establishes no
//! restore acceptance, recoverability or device state. RPMB and OTP are not
//! covered by the backup set and remain UNKNOWN.
//!
//! Supported layout (as produced by `scripts/phase0-backup.mtk`):
//! - a phase-0 `SHA256SUMS` with exactly 26 entries: the 22 non-userdata
//!   partition images `<name>.img`, `boot-region1.bin`, `boot-region2.bin`
//!   (4 MiB each) and the two 1 MiB GPT captures `primary-userarea.bin`
//!   (from LBA 0) and `tail-userarea.bin` (from an explicit byte offset);
//! - a userdata `SHA256SUMS` with exactly `userdata.img`;
//! - 512-byte sectors; the tail capture must end at the declared disk end.
//!
//! Manifest entry paths are relative to their own manifest's directory.

pub mod gpt;
pub mod manifest;

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

pub const SECTOR: u64 = 512;
/// `ro 0x0 0x100000` / `ro 0x3a3d00000 0x100000` in phase0-backup.mtk.
pub const CAPTURE_LEN: u64 = 0x100000;
pub const BOOT_REGION_LEN: u64 = 4 * 1024 * 1024;
pub const TOTAL_FILES: usize = 27;

/// The 23 GPT partitions of this device (scripts/phase0-backup.mtk plus userdata).
pub const PARTITIONS: [&str; 23] = [
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

pub struct Inputs {
    pub phase0_manifest: PathBuf,
    pub userdata_manifest: PathBuf,
    pub primary: PathBuf,
    pub tail: PathBuf,
    pub tail_offset: u64,
    pub disk_size: u64,
}

#[derive(Default)]
pub struct Audit {
    pub lines: Vec<String>,
    pub failures: usize,
    files_passed: usize,
    sizes_passed: usize,
}

impl Audit {
    fn line(&mut self, s: String) {
        self.lines.push(s);
    }
    fn pass(&mut self, s: String) {
        self.lines.push(format!("PASS {s}"));
    }
    fn fail(&mut self, s: String) {
        self.failures += 1;
        self.lines.push(format!("FAIL {s}"));
    }
    fn summary(mut self) -> Audit {
        self.line(format!(
            "files: {}/{TOTAL_FILES} manifest hashes PASS",
            self.files_passed
        ));
        self.line(format!(
            "partitions: {}/{} GPT size matches",
            self.sizes_passed,
            PARTITIONS.len()
        ));
        if self.failures == 0 {
            self.line(
                "RESULT: PASS - host integrity proof only; no device contact, no restore \
                 acceptance; RPMB/OTP not covered"
                    .into(),
            );
        } else {
            self.line(format!("RESULT: FAIL ({} problem(s))", self.failures));
        }
        self
    }
}

fn expected_phase0() -> Vec<String> {
    let mut v: Vec<String> = PARTITIONS
        .iter()
        .filter(|&&p| p != "userdata")
        .map(|p| format!("{p}.img"))
        .collect();
    for extra in [
        "boot-region1.bin",
        "boot-region2.bin",
        "primary-userarea.bin",
        "tail-userarea.bin",
    ] {
        v.push(extra.into());
    }
    v
}

fn check_set(a: &mut Audit, label: &str, got: &[String], want: &[String]) {
    let g: HashSet<&str> = got.iter().map(String::as_str).collect();
    let w: HashSet<&str> = want.iter().map(String::as_str).collect();
    let mut ok = true;
    for m in want {
        if !g.contains(m.as_str()) {
            a.fail(format!("{label}: missing expected entry '{m}'"));
            ok = false;
        }
    }
    for x in got {
        if !w.contains(x.as_str()) {
            a.fail(format!("{label}: unexpected entry '{x}'"));
            ok = false;
        }
    }
    if ok {
        a.pass(format!(
            "{label}: exact expected file set ({} entries)",
            want.len()
        ));
    }
}

fn parent_dir(p: &Path) -> PathBuf {
    p.parent().unwrap_or(Path::new(".")).to_path_buf()
}

/// Run the full audit; never panics on untrusted input and never writes.
pub fn audit(inp: &Inputs) -> Audit {
    let mut a = Audit::default();
    let disk_lbas = inp.disk_size / SECTOR;
    let tail_base = inp.tail_offset / SECTOR;
    a.line(format!(
        "disk: {} bytes ({disk_lbas} sectors of {SECTOR}), tail capture at byte {} (LBA {tail_base})",
        inp.disk_size, inp.tail_offset
    ));

    let p0_dir = parent_dir(&inp.phase0_manifest);
    let ud_dir = parent_dir(&inp.userdata_manifest);
    let phase0 = match manifest::read_manifest(&inp.phase0_manifest)
        .and_then(|t| manifest::parse_manifest(&t))
    {
        Ok(v) => v,
        Err(e) => {
            a.fail(format!("phase0 manifest: {e}"));
            return a.summary();
        }
    };
    let userdata = match manifest::read_manifest(&inp.userdata_manifest)
        .and_then(|t| manifest::parse_manifest(&t))
    {
        Ok(v) => v,
        Err(e) => {
            a.fail(format!("userdata manifest: {e}"));
            return a.summary();
        }
    };
    a.line(format!(
        "manifests: phase0 {} entries, userdata {} entries",
        phase0.len(),
        userdata.len()
    ));

    let mut all_names: HashSet<&str> = HashSet::new();
    for e in phase0.iter().chain(userdata.iter()) {
        if !all_names.insert(&e.name) {
            a.fail(format!("duplicate entry across manifests: '{}'", e.name));
        }
    }
    let p0_names: Vec<String> = phase0.iter().map(|e| e.name.clone()).collect();
    let ud_names: Vec<String> = userdata.iter().map(|e| e.name.clone()).collect();
    check_set(&mut a, "phase0", &p0_names, &expected_phase0());
    check_set(&mut a, "userdata", &ud_names, &["userdata.img".to_string()]);

    let mut sizes: HashMap<String, u64> = HashMap::new();
    let mut canon: HashMap<String, PathBuf> = HashMap::new();
    let mut idents: HashMap<String, manifest::Identity> = HashMap::new();
    let mut inodes: HashMap<(u64, u64), String> = HashMap::new();
    for (label, dir, entries) in [
        ("phase0", &p0_dir, &phase0),
        ("userdata", &ud_dir, &userdata),
    ] {
        for e in entries {
            match manifest::hash_file(dir, &e.name) {
                Ok((id, digest)) => {
                    sizes.insert(e.name.clone(), id.len);
                    idents.insert(e.name.clone(), id);
                    if let Ok(c) = std::fs::canonicalize(dir.join(&e.name)) {
                        canon.insert(e.name.clone(), c);
                    }
                    if let Some(prev) = inodes.insert((id.dev, id.ino), e.name.clone()) {
                        a.fail(format!(
                            "{label}/{}: same file as '{prev}' (hardlink alias)",
                            e.name
                        ));
                    }
                    if digest == e.hash {
                        a.files_passed += 1;
                        a.pass(format!(
                            "{label}/{}: SHA-256 matches ({} bytes)",
                            e.name, id.len
                        ));
                    } else {
                        a.fail(format!("{label}/{}: SHA-256 mismatch", e.name));
                    }
                }
                Err(err) => a.fail(format!("{label}/{}: {err}", e.name)),
            }
        }
    }

    for (name, want) in [
        ("boot-region1.bin", BOOT_REGION_LEN),
        ("boot-region2.bin", BOOT_REGION_LEN),
        ("primary-userarea.bin", CAPTURE_LEN),
        ("tail-userarea.bin", CAPTURE_LEN),
    ] {
        match sizes.get(name) {
            Some(&len) if len == want => {
                a.pass(format!("{name}: {len} bytes as expected"));
            }
            Some(&len) => a.fail(format!("{name}: {len} bytes, expected {want}")),
            None => {} // missing/unreadable already reported above
        }
    }

    verify_gpt(&mut a, inp, &canon, &idents, &sizes, disk_lbas, tail_base);
    a.summary()
}

fn verify_gpt(
    a: &mut Audit,
    inp: &Inputs,
    canon: &HashMap<String, PathBuf>,
    idents: &HashMap<String, manifest::Identity>,
    sizes: &HashMap<String, u64>,
    disk_lbas: u64,
    tail_base: u64,
) {
    let p0_dir = parent_dir(&inp.phase0_manifest);
    let mut captures: [Option<Vec<u8>>; 2] = [None, None];
    for (i, (name, cli)) in [
        ("primary-userarea.bin", &inp.primary),
        ("tail-userarea.bin", &inp.tail),
    ]
    .into_iter()
    .enumerate()
    {
        let Some(entry_path) = canon.get(name) else {
            a.fail(format!("{name}: not covered by the phase0 manifest"));
            continue;
        };
        let cli_canon = match std::fs::canonicalize(cli) {
            Ok(c) => c,
            Err(e) => {
                a.fail(format!("{}: {e}", cli.display()));
                continue;
            }
        };
        if &cli_canon != entry_path {
            a.fail(format!(
                "{name}: CLI path {} is not the file hashed from the manifest ({})",
                cli.display(),
                entry_path.display()
            ));
            continue;
        }
        match manifest::read_capture(&p0_dir, name) {
            Ok((id, bytes)) => {
                if idents.get(name) != Some(&id) {
                    a.fail(format!("{name}: changed between hashing and GPT read"));
                    continue;
                }
                captures[i] = Some(bytes);
            }
            Err(e) => a.fail(format!("{name}: {e}")),
        }
    }
    let (Some(primary_bytes), Some(tail_bytes)) = (&captures[0], &captures[1]) else {
        return;
    };
    let primary = match gpt::parse_copy(primary_bytes, 0, disk_lbas, true) {
        Ok(c) => {
            a.pass(format!(
                "GPT primary: header CRC, array CRC and layout valid; {} used entries",
                c.entries.len()
            ));
            c
        }
        Err(e) => {
            a.fail(format!("GPT primary: {e}"));
            return;
        }
    };
    let backup = match gpt::parse_copy(tail_bytes, tail_base, disk_lbas, false) {
        Ok(c) => {
            a.pass(format!(
                "GPT backup: header CRC, array CRC and layout valid; {} used entries",
                c.entries.len()
            ));
            c
        }
        Err(e) => {
            a.fail(format!("GPT backup: {e}"));
            return;
        }
    };
    match gpt::cross_check(&primary, &backup) {
        Ok(()) => a.pass(
            "GPT cross-check: disk GUID, usable bounds, reciprocal LBAs and identical arrays"
                .into(),
        ),
        Err(e) => {
            a.fail(format!("GPT cross-check: {e}"));
            return;
        }
    }

    let got: Vec<String> = primary.entries.iter().map(|p| p.name.clone()).collect();
    let want: Vec<String> = PARTITIONS.iter().map(|&p| p.to_string()).collect();
    check_set(a, "GPT partitions", &got, &want);
    for p in &primary.entries {
        let file = format!("{}.img", p.name);
        let Some(&file_len) = sizes.get(&file) else {
            continue; // missing file already reported
        };
        match p.len_bytes() {
            Some(gpt_len) if gpt_len == file_len => {
                a.sizes_passed += 1;
                a.pass(format!(
                    "partition {}: {file_len} bytes matches GPT",
                    p.name
                ));
            }
            Some(gpt_len) => a.fail(format!(
                "partition {}: file {file_len} bytes, GPT {gpt_len} bytes",
                p.name
            )),
            None => a.fail(format!("partition {}: GPT length overflow", p.name)),
        }
    }
}
