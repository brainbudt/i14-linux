//! Host-side I14 tools: building the installable boot image, as
//! `scripts/build-i14-boot.py` with `scripts/build-stock-linux-bootimg.py` and
//! `initramfs/make-stock-charge-cpio.py` (its archive writer), and the offline
//! read-only backup verifier ([`verify_backup`]). Runs on the Mac.

pub mod verify_backup;

use std::fs::OpenOptions;
use std::io::{self, Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use sha1::Sha1;
use sha2::{Digest, Sha256};

pub const BUSYBOX: &str = "artifacts/tooling/alpine-3.24/busybox.static"; // Alpine 3.24 busybox-static 1.37.0-r31
pub const BUSYBOX_SHA256: &str = "999cb969d09093a71716cfc747bb53cdada3f332c05eb5046c56e0f66a4d6d22";
pub const E2FSCK: &str = "artifacts/build-tools/e2fsprogs/out/e2fsck"; // scripts/build-e2fsck.sh, e2fsprogs 1.47.3
pub const E2FSCK_SHA256: &str = "fa1adeb5ce925a31e31323b382437569b39e0a572a70078946695be343331fe6";
pub const STOCK: &str = "backups/20261030T011515Z-native-serial/boot.img";
pub const STOCK_SHA256: &str = "5192fc2e8fe20ce37a4e93d212ecd282134085eceefaf4fea1eabf5f6ef98492";
/// The RTC can be wrong after a dead battery; that alone must not force a full check or a stop.
pub const E2FSCK_CONF: &[u8] = b"[options]\n\tbroken_system_clock = 1\n";
pub const APPLETS: [&str; 24] = [
    "sh",
    "cat",
    "date",
    "echo",
    "grep",
    "ls",
    "mkdir",
    "rm",
    "sleep",
    "mount",
    "umount",
    "mdev",
    "readlink",
    "switch_root",
    "sync",
    "mv",
    "reboot",
    "poweroff",
    "dd",
    "od",
    "tr",
    "tail",
    "getty",
    "login",
];
pub const CMDLINE_EXTRA: &str = "rdinit=/i14init";
/// RAM-boot trial of the charging path: pretend power-off charging, with short waits.
pub const CMDLINE_CHARGE_TEST: &str = "rdinit=/i14init androidboot.mode=charger i14.autoboot=test";
pub const MAX_SIZE: usize = 16 * 1024 * 1024;

const S_IFDIR: u32 = 0o040000;
const S_IFREG: u32 = 0o100000;
const S_IFLNK: u32 = 0o120000;

fn bad(msg: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.into())
}

pub fn sha256_hex(data: &[u8]) -> String {
    Sha256::digest(data)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// One archive member: mode (with file type), body, device major/minor.
pub struct Entry {
    pub mode: u32,
    pub body: Vec<u8>,
    pub major: u32,
    pub minor: u32,
}

fn entry(mode: u32, body: &[u8]) -> Entry {
    Entry {
        mode,
        body: body.to_vec(),
        major: 0,
        minor: 0,
    }
}

/// gzip as Python's `gzip.compress(data, mtime=0)` (level 9, no name, OS 255).
pub fn gzip9(data: &[u8]) -> io::Result<Vec<u8>> {
    let mut e = flate2::GzBuilder::new()
        .mtime(0)
        .operating_system(255)
        .write(Vec::new(), flate2::Compression::best());
    e.write_all(data)?;
    e.finish()
}

/// newc cpio of `entries` sorted by name (inode = position from 1), TRAILER!!!,
/// padded to 512 bytes, gzipped: make-stock-charge-cpio.py's archive().
pub fn archive(entries: &[(String, Entry)]) -> io::Result<Vec<u8>> {
    let mut sorted: Vec<&(String, Entry)> = entries.iter().collect();
    sorted.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
    let trailer = ("TRAILER!!!".to_string(), entry(0, b""));
    let mut data = Vec::new();
    for (ino, (name, e)) in sorted
        .into_iter()
        .chain(std::iter::once(&trailer))
        .enumerate()
    {
        let mut encoded = name.as_bytes().to_vec();
        encoded.push(0);
        let nlink = if e.mode & 0o170000 == S_IFDIR { 2 } else { 1 };
        let fields = [
            ino as u32 + 1,
            e.mode,
            0,
            0,
            nlink,
            0,
            e.body.len() as u32,
            0,
            0,
            e.major,
            e.minor,
            encoded.len() as u32,
            0,
        ];
        data.extend_from_slice(b"070701");
        for f in fields {
            data.extend_from_slice(format!("{f:08x}").as_bytes());
        }
        data.extend_from_slice(&encoded);
        data.resize(data.len().div_ceil(4) * 4, 0);
        data.extend_from_slice(&e.body);
        data.resize(data.len().div_ceil(4) * 4, 0);
    }
    data.resize(data.len().div_ceil(512) * 512, 0);
    gzip9(&data)
}

fn u32_at(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes(b[o..o + 4].try_into().unwrap())
}

/// The stock image with `ramdisk` instead of its own and `cmdline_extra` appended:
/// build-stock-linux-bootimg.py's repack().
pub fn repack(stock: &[u8], ramdisk: &[u8], cmdline_extra: &str) -> io::Result<Vec<u8>> {
    if stock.len() < 2048 || &stock[..8] != b"ANDROID!" {
        return Err(bad("missing Android boot magic"));
    }
    let (ks, ss, page) = (
        u32_at(stock, 8) as usize,
        u32_at(stock, 24),
        u32_at(stock, 36) as usize,
    );
    if page != 2048 || ss != 0 {
        return Err(bad("unexpected stock header (page size / second stage)"));
    }
    let mut header = stock[..page].to_vec();
    header[16..20].copy_from_slice(&(ramdisk.len() as u32).to_le_bytes());
    if !cmdline_extra.is_empty() {
        let old = header[64..576]
            .split(|&b| b == 0)
            .next()
            .unwrap_or(&[])
            .to_vec();
        let mut cmdline = old;
        cmdline.push(b' ');
        cmdline.extend_from_slice(cmdline_extra.as_bytes());
        if cmdline.len() >= 512 {
            return Err(bad("cmdline too long"));
        }
        cmdline.resize(512, 0);
        header[64..576].copy_from_slice(&cmdline);
    }
    let kernel = stock
        .get(page..page + ks)
        .ok_or_else(|| bad("kernel past the end"))?;
    // mkbootimg v0 id: kernel, size, ramdisk, size, second, size
    let mut sha = Sha1::new();
    for blob in [kernel, ramdisk, &[]] {
        sha.update(blob);
        sha.update((blob.len() as u32).to_le_bytes());
    }
    header[576..596].copy_from_slice(&sha.finalize());
    header[596..608].fill(0);
    let pad = |n: usize| (page - n % page) % page;
    let mut out = header;
    out.extend_from_slice(kernel);
    out.resize(out.len() + pad(ks), 0);
    out.extend_from_slice(ramdisk);
    out.resize(out.len() + pad(ramdisk.len()), 0);
    if out.len() > MAX_SIZE {
        return Err(bad("image exceeds the 16 MiB boot partition"));
    }
    Ok(out)
}

/// The stock Android ramdisk inside the stock image.
pub fn stock_ramdisk(stock: &[u8]) -> io::Result<&[u8]> {
    if stock.len() < 2048 {
        return Err(bad("stock boot header is truncated"));
    }
    let (ks, rs, page) = (u32_at(stock, 8) as usize, u32_at(stock, 16) as usize, 2048);
    let start = page + ks + (page - ks % page) % page;
    stock
        .get(start..start + rs)
        .ok_or_else(|| bad("ramdisk past the end"))
}

/// Validated SHA-512 crypt hash for the rescue account.
pub struct RescueHash(Vec<u8>);

fn valid_crypt_hash(value: &[u8]) -> bool {
    let Some(fields) = value.strip_prefix(b"$6$") else {
        return false;
    };
    let mut fields = fields.split(|&b| b == b'$');
    let first = fields.next().unwrap_or_default();
    let salt = if let Some(rounds) = first.strip_prefix(b"rounds=") {
        if !(4..=6).contains(&rounds.len())
            || rounds.first() == Some(&b'0')
            || !rounds.iter().all(u8::is_ascii_digit)
        {
            return false;
        }
        fields.next().unwrap_or_default()
    } else {
        first
    };
    let digest = fields.next().unwrap_or_default();
    let crypt_byte = |b: &u8| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'/');
    (1..=16).contains(&salt.len())
        && salt.iter().all(crypt_byte)
        && digest.len() == 86
        && digest.iter().all(crypt_byte)
        && fields.next().is_none()
}

/// Read the same private-file format used by the Python builder.
pub fn read_rescue_hash(path: &Path) -> io::Result<RescueHash> {
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| bad("unable to read the private rescue password hash file"))?;
    let meta = file.metadata()?;
    // geteuid has no arguments and reads only this process's effective UID.
    let owner = unsafe { libc::geteuid() };
    if !meta.is_file() || meta.uid() != owner || !matches!(meta.mode() & 0o7777, 0o400 | 0o600) {
        return Err(bad(
            "rescue password hash file must be private and owned by the build user",
        ));
    }
    let mut value = Vec::new();
    file.take(160).read_to_end(&mut value)?;
    if value.last() == Some(&b'\n') {
        value.pop();
    }
    if !valid_crypt_hash(&value) {
        return Err(bad(
            "rescue password must be configured with a SHA-512 crypt hash",
        ));
    }
    Ok(RescueHash(value))
}

fn entries_from_inputs(
    rescue: &RescueHash,
    init: &[u8],
    charge: &[u8],
    busybox: &[u8],
    e2fsck: &[u8],
) -> Vec<(String, Entry)> {
    let mut entries: Vec<_> = ["etc", "i14", "i14/bin", "i14/data", "i14/root"]
        .into_iter()
        .map(|name| (name.into(), entry(S_IFDIR | 0o755, b"")))
        .collect();
    let mut shadow = b"root:".to_vec();
    shadow.extend_from_slice(&rescue.0);
    shadow.extend_from_slice(b":0:0:99999:7:::\n");
    for (name, mode, body) in [
        (
            "etc/passwd",
            0o644,
            b"root:x:0:0:I14 rescue:/i14:/i14/bin/sh\n".as_slice(),
        ),
        ("etc/group", 0o644, b"root:x:0:\n"),
        ("etc/shadow", 0o600, &shadow),
        ("etc/securetty", 0o600, b"ttyGS0\n"),
        ("i14/rescue-login-enabled", 0o600, b""),
        ("i14init", 0o755, init),
        ("i14/charge", 0o644, charge),
        ("i14/e2fsck.conf", 0o644, E2FSCK_CONF),
        ("i14/bin/busybox", 0o755, busybox),
        ("i14/bin/e2fsck", 0o755, e2fsck),
    ] {
        entries.push((name.into(), entry(S_IFREG | mode, body)));
    }
    entries.extend(APPLETS.map(|name| {
        (
            format!("i14/bin/{name}"),
            entry(S_IFLNK | 0o777, b"busybox"),
        )
    }));
    entries
}

/// Build the initramfs members from the pinned tools and checkout sources.
pub fn entries(root: &Path, rescue: &RescueHash) -> io::Result<Vec<(String, Entry)>> {
    let busybox = std::fs::read(root.join(BUSYBOX))?;
    if sha256_hex(&busybox) != BUSYBOX_SHA256 {
        return Err(bad("busybox.static hash mismatch"));
    }
    let e2fsck = std::fs::read(root.join(E2FSCK))?;
    if sha256_hex(&e2fsck) != E2FSCK_SHA256 {
        return Err(bad("e2fsck hash mismatch"));
    }
    let init = std::fs::read(root.join("initramfs/i14init"))?;
    let charge = std::fs::read(root.join("initramfs/i14charge"))?;
    Ok(entries_from_inputs(
        rescue, &init, &charge, &busybox, &e2fsck,
    ))
}

/// The stock kernel and ramdisk with the authenticated rescue overlay appended.
pub fn build(root: &Path, stock: &[u8], rescue: &RescueHash, cmdline: &str) -> io::Result<Vec<u8>> {
    build_with_entries(stock, &entries(root, rescue)?, cmdline)
}

fn build_with_entries(
    stock: &[u8],
    entries: &[(String, Entry)],
    cmdline: &str,
) -> io::Result<Vec<u8>> {
    let ours = archive(entries)?;
    let android = stock_ramdisk(stock)?;
    let mut ramdisk = android.to_vec();
    ramdisk.resize(android.len().div_ceil(4) * 4, 0);
    ramdisk.extend_from_slice(&ours);
    repack(stock, &ramdisk, cmdline)
}

/// The checkout root (two levels above this crate, or $I14_REPO).
pub fn repo_root() -> PathBuf {
    std::env::var_os("I14_REPO").map_or_else(
        || PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../.."),
        PathBuf::from,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gzip_header_like_python() {
        let g = gzip9(b"x").unwrap();
        assert_eq!(&g[..10], [0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 2, 255]);
    }

    #[test]
    fn authenticated_boot_images_match_python_on_synthetic_inputs() {
        use std::fs;
        use std::process::Command;

        let root = repo_root();
        let dir = std::env::temp_dir().join(format!("i14-boot-parity-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        let busybox = b"synthetic busybox";
        let e2fsck = b"synthetic e2fsck";
        fs::write(dir.join("busybox"), busybox).unwrap();
        fs::write(dir.join("e2fsck"), e2fsck).unwrap();
        let mut stock = vec![0u8; 3 * 2048];
        stock[..8].copy_from_slice(b"ANDROID!");
        stock[8..12].copy_from_slice(&6u32.to_le_bytes());
        stock[16..20].copy_from_slice(&7u32.to_le_bytes());
        stock[36..40].copy_from_slice(&2048u32.to_le_bytes());
        stock[64..68].copy_from_slice(b"test");
        stock[2048..2054].copy_from_slice(b"kernel");
        stock[4096..4103].copy_from_slice(b"ramdisk");
        fs::write(dir.join("stock"), &stock).unwrap();
        let rescue = RescueHash(format!("$6$test.salt${}", "A".repeat(86)).into_bytes());
        let entries = entries_from_inputs(
            &rescue,
            &fs::read(root.join("initramfs/i14init")).unwrap(),
            &fs::read(root.join("initramfs/i14charge")).unwrap(),
            busybox,
            e2fsck,
        );
        let python = r#"
import hashlib, importlib.util, pathlib, sys
root, tmp, cmdline = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location('builder', root / 'scripts/build-i14-boot.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
builder.BUSYBOX, builder.E2FSCK = tmp / 'busybox', tmp / 'e2fsck'
builder.BUSYBOX_SHA256 = hashlib.sha256(builder.BUSYBOX.read_bytes()).hexdigest()
builder.E2FSCK_SHA256 = hashlib.sha256(builder.E2FSCK.read_bytes()).hexdigest()
sys.stdout.buffer.write(builder.build((tmp / 'stock').read_bytes(), b'$6$test.salt$' + b'A' * 86, cmdline))
"#;
        for cmdline in [CMDLINE_EXTRA, CMDLINE_CHARGE_TEST] {
            let expected = Command::new("python3")
                .args(["-B", "-c", python])
                .arg(&root)
                .arg(&dir)
                .arg(cmdline)
                .output()
                .unwrap();
            assert!(
                expected.status.success(),
                "{}",
                String::from_utf8_lossy(&expected.stderr)
            );
            let image = build_with_entries(&stock, &entries, cmdline).unwrap();
            assert_eq!(
                sha256_hex(&image),
                sha256_hex(&expected.stdout),
                "{cmdline}"
            );
        }
        for file in ["stock", "e2fsck", "busybox"] {
            fs::remove_file(dir.join(file)).unwrap();
        }
        fs::remove_dir(dir).unwrap();
    }

    #[test]
    fn repack_checks_the_header() {
        assert!(repack(b"nope", b"", "").is_err());
        let mut stock = vec![0u8; 4096];
        stock[..8].copy_from_slice(b"ANDROID!");
        stock[36..40].copy_from_slice(&4096u32.to_le_bytes());
        assert!(repack(&stock, b"", "").is_err(), "page size 4096 refused");
    }
}
