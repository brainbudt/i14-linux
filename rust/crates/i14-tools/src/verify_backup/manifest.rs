//! sha256sum manifest parsing and read-only, identity-checked file hashing.
//!
//! Supported format: GNU coreutils `sha256sum` lines, one per file:
//! `<64 hex digits> <' ' or '*'><relative path>` (a space, then the text/binary
//! marker, then the name; text mode shows two spaces). Uppercase hex is
//! accepted. A single leading `./` is stripped. Not supported (rejected):
//! comments, blank lines, the backslash-escaped variant for filenames with
//! newlines, absolute paths, `..` traversal, other `.` components, empty path
//! components, backslashes and control characters in names.

use std::collections::HashMap;
use std::fs::{self, File, OpenOptions};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

/// Manifests are tiny (27 lines for I14); refuse anything larger.
pub const MAX_MANIFEST_LEN: u64 = 1024 * 1024;
const CHUNK: usize = 64 * 1024;
const MAX_NAME_LEN: usize = 4096;

pub struct ManifestEntry {
    /// Normalized path relative to the manifest's own directory.
    pub name: String,
    pub hash: [u8; 32],
}

/// Device/inode/size/mtime/ctime snapshot; any observed change invalidates a hash.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub struct Identity {
    pub dev: u64,
    pub ino: u64,
    pub len: u64,
    mtime: i64,
    mtime_nsec: i64,
    ctime: i64,
    ctime_nsec: i64,
}

impl Identity {
    fn new(m: &fs::Metadata) -> Identity {
        Identity {
            dev: m.dev(),
            ino: m.ino(),
            len: m.len(),
            mtime: m.mtime(),
            mtime_nsec: m.mtime_nsec(),
            ctime: m.ctime(),
            ctime_nsec: m.ctime_nsec(),
        }
    }
}

fn hex_digit(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        b'A'..=b'F' => Some(c - b'A' + 10),
        _ => None,
    }
}

/// Reject unsafe or unsupported manifest paths; strip a single leading `./`.
pub fn clean_name(raw: &str) -> Result<String, String> {
    if raw.is_empty() || raw.len() > MAX_NAME_LEN {
        return Err("empty or overlong filename".into());
    }
    if raw.starts_with('/') {
        return Err("absolute path".into());
    }
    if raw.contains('\\') {
        return Err("backslash in filename (escaped lines unsupported)".into());
    }
    if raw.chars().any(char::is_control) {
        return Err("control character in filename".into());
    }
    let rest = raw.strip_prefix("./").unwrap_or(raw);
    if rest.is_empty() || rest.ends_with('/') {
        return Err("empty filename component".into());
    }
    for comp in rest.split('/') {
        if comp.is_empty() || comp == "." || comp == ".." {
            return Err("unsafe path component ('.', '..' or empty)".into());
        }
    }
    Ok(rest.to_string())
}

/// Parse sha256sum text; every line must be well-formed, names unique.
pub fn parse_manifest(text: &str) -> Result<Vec<ManifestEntry>, String> {
    let mut entries = Vec::new();
    let mut seen: HashMap<String, usize> = HashMap::new();
    let mut body = text;
    if let Some(rest) = body.strip_suffix('\n') {
        body = rest;
    }
    if body.is_empty() {
        return Err("empty manifest".into());
    }
    for (i, line) in body.split('\n').enumerate() {
        let at = i + 1;
        if line.is_empty() {
            return Err(format!("line {at}: blank line"));
        }
        if line.starts_with('\\') {
            return Err(format!("line {at}: escaped filename lines are unsupported"));
        }
        let bytes = line.as_bytes();
        if bytes.len() < 67 {
            return Err(format!(
                "line {at}: expected 64 hex digits, a marker and a name"
            ));
        }
        let mut hash = [0u8; 32];
        for (j, pair) in bytes[..64].chunks(2).enumerate() {
            let hi = hex_digit(pair[0]).ok_or_else(|| format!("line {at}: bad hex digit"))?;
            let lo = hex_digit(pair[1]).ok_or_else(|| format!("line {at}: bad hex digit"))?;
            hash[j] = (hi << 4) | lo;
        }
        if bytes[64] != b' ' {
            return Err(format!("line {at}: expected a space after the hash"));
        }
        if bytes[65] != b' ' && bytes[65] != b'*' {
            return Err(format!(
                "line {at}: expected ' ' or '*' marker after the hash"
            ));
        }
        let name = clean_name(&line[66..]).map_err(|e| format!("line {at}: {e}"))?;
        if let Some(prev) = seen.insert(name.clone(), at) {
            return Err(format!("line {at}: duplicate entry '{name}' (line {prev})"));
        }
        entries.push(ManifestEntry { name, hash });
    }
    Ok(entries)
}

/// Read a manifest file (regular file, bounded size) as UTF-8.
pub fn read_manifest(path: &Path) -> Result<String, String> {
    let meta = fs::metadata(path).map_err(|e| format!("{}: {e}", path.display()))?;
    if !meta.is_file() {
        return Err(format!("{}: not a regular file", path.display()));
    }
    if meta.len() > MAX_MANIFEST_LEN {
        return Err(format!(
            "{}: manifest larger than {MAX_MANIFEST_LEN} bytes",
            path.display()
        ));
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    let opened = file
        .metadata()
        .map_err(|e| format!("{}: {e}", path.display()))?;
    if !opened.is_file() || Identity::new(&opened) != Identity::new(&meta) {
        return Err(format!(
            "{}: manifest changed while opening",
            path.display()
        ));
    }
    let mut buf = Vec::new();
    file.take(MAX_MANIFEST_LEN + 1)
        .read_to_end(&mut buf)
        .map_err(|e| format!("{}: {e}", path.display()))?;
    if buf.len() as u64 > MAX_MANIFEST_LEN {
        return Err(format!("{}: manifest grew while reading", path.display()));
    }
    String::from_utf8(buf).map_err(|_| format!("{}: manifest is not valid UTF-8", path.display()))
}

/// Open `root/rel` for reading only. Every path component must be a real
/// directory (no symlinks) and the target a regular file (no symlink), so a
/// manifest can never reach outside its own directory tree.
pub fn open_regular(root: &Path, rel: &str) -> Result<(File, Identity), String> {
    let mut cur = PathBuf::from(root);
    let comps: Vec<&str> = rel.split('/').collect();
    for (i, comp) in comps.iter().enumerate() {
        cur.push(comp);
        let m = fs::symlink_metadata(&cur).map_err(|e| format!("{rel}: {e}"))?;
        let last = i + 1 == comps.len();
        if m.file_type().is_symlink() {
            return Err(format!("{rel}: symlink component '{comp}' refused"));
        }
        if last {
            if !m.is_file() {
                return Err(format!("{rel}: not a regular file"));
            }
        } else if !m.is_dir() {
            return Err(format!("{rel}: '{comp}' is not a directory"));
        }
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK | libc::O_NOFOLLOW)
        .open(&cur)
        .map_err(|e| format!("{rel}: {e}"))?;
    let fm = file.metadata().map_err(|e| format!("{rel}: {e}"))?;
    if !fm.is_file() {
        return Err(format!("{rel}: not a regular file"));
    }
    let id = Identity::new(&fm);
    let lm = fs::symlink_metadata(&cur).map_err(|e| format!("{rel}: {e}"))?;
    if Identity::new(&lm) != id {
        return Err(format!("{rel}: file changed while opening"));
    }
    Ok((file, id))
}

fn stat_path(root: &Path, rel: &str) -> Result<Identity, String> {
    let m = fs::symlink_metadata(root.join(rel)).map_err(|e| format!("{rel}: {e}"))?;
    if m.file_type().is_symlink() || !m.is_file() {
        return Err(format!("{rel}: no longer a plain regular file"));
    }
    Ok(Identity::new(&m))
}

/// Stream SHA-256 in bounded chunks; refuse any identity/size change observed
/// before, during or after the read. Returns the pre-hash identity and digest.
pub fn hash_file(root: &Path, rel: &str) -> Result<(Identity, [u8; 32]), String> {
    let (file, before) = open_regular(root, rel)?;
    hash_open_file(root, rel, file, before)
}

fn hash_open_file(
    root: &Path,
    rel: &str,
    mut file: File,
    before: Identity,
) -> Result<(Identity, [u8; 32]), String> {
    let mut hasher = Sha256::new();
    let mut buf = vec![0u8; CHUNK];
    let mut total: u64 = 0;
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| format!("{rel}: read: {e}"))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
        total += n as u64;
    }
    let after_file = Identity::new(&file.metadata().map_err(|e| format!("{rel}: fstat: {e}"))?);
    let after_path = stat_path(root, rel)?;
    if before != after_file || before != after_path || total != before.len {
        return Err(format!(
            "{rel}: file identity, size or mtime changed during hashing"
        ));
    }
    Ok((before, hasher.finalize().into()))
}

/// Read a whole GPT capture (exactly [`super::CAPTURE_LEN`] bytes) with the
/// same identity checks; returns the stable identity and the bytes read.
pub fn read_capture(root: &Path, rel: &str) -> Result<(Identity, Vec<u8>), String> {
    let (file, before) = open_regular(root, rel)?;
    if before.len != super::CAPTURE_LEN {
        return Err(format!(
            "{rel}: capture is {} bytes, expected exactly {} (phase0-backup.mtk layout)",
            before.len,
            super::CAPTURE_LEN
        ));
    }
    let mut buf = Vec::new();
    (&file)
        .take(super::CAPTURE_LEN + 1)
        .read_to_end(&mut buf)
        .map_err(|e| format!("{rel}: read: {e}"))?;
    let after_file = Identity::new(&file.metadata().map_err(|e| format!("{rel}: fstat: {e}"))?);
    let after_path = stat_path(root, rel)?;
    if before != after_file || before != after_path || buf.len() as u64 != before.len {
        return Err(format!(
            "{rel}: file identity, size or mtime changed during reading"
        ));
    }
    Ok((before, buf))
}

#[cfg(test)]
mod tests {
    use super::*;

    const H: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    #[test]
    fn parses_text_and_binary_markers_and_uppercase() {
        let text = format!("{H}  a.img\n{} *b.bin\n", H.to_uppercase());
        let e = parse_manifest(&text).unwrap();
        assert_eq!(e.len(), 2);
        assert_eq!(e[0].name, "a.img");
        assert_eq!(e[1].name, "b.bin");
        assert_eq!(hex(&e[0].hash), H);
        assert_eq!(hex(&e[1].hash), H);
        assert!(
            parse_manifest(&format!("{H}  a.img")).is_ok(),
            "no trailing newline"
        );
    }

    #[test]
    fn strips_one_leading_dot_slash_only() {
        assert_eq!(clean_name("./boot.img").unwrap(), "boot.img");
        assert!(clean_name("././boot.img").is_err());
        assert!(clean_name("a/./b").is_err());
    }

    #[test]
    fn rejects_unsafe_names() {
        for bad in [
            "",
            "/etc/passwd",
            "../x",
            "a/../b",
            "a/..",
            "a//b",
            "a/",
            "a\\b",
            "a\tb",
            "a\nb",
            ".",
            "..",
            "./",
            "a/./b",
        ] {
            assert!(clean_name(bad).is_err(), "{bad:?}");
        }
        assert!(clean_name(&"x".repeat(MAX_NAME_LEN + 1)).is_err());
    }

    #[test]
    fn rejects_malformed_lines() {
        for bad in [
            "",
            "\n",
            "not a hash  x\n",
            &format!("{H}x  a\n"),
            &format!("{H}  \n"),
            &format!("{H}?a\n"),
            &format!("{H}  a\n\n"),
            &format!("\\{H}  a\\nb\n"),
            &format!("{H}  a\r\n"),
            &format!("{H}  ../a\n"),
            &format!("{H}  a\n{H}  a\n"),
            &format!("{H}  a\n{H}  ./a\n"),
            &format!("{}  a\n", &H[..63]),
            &format!("{}g  a\n", &H[..63]),
        ] {
            assert!(parse_manifest(bad).is_err(), "{bad:?}");
        }
    }

    #[test]
    fn hashing_rejects_a_file_changed_after_open() {
        let dir = std::env::temp_dir().join(format!("i14-verify-changed-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        let path = dir.join("test.img");
        fs::write(&path, b"original").unwrap();

        let (file, before) = open_regular(&dir, "test.img").unwrap();
        File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(2)
            .unwrap();
        let resized = hash_open_file(&dir, "test.img", file, before);

        fs::write(&path, b"original").unwrap();
        fs::write(dir.join("replacement"), b"original").unwrap();
        let (file, before) = open_regular(&dir, "test.img").unwrap();
        fs::rename(dir.join("replacement"), &path).unwrap();
        let replaced = hash_open_file(&dir, "test.img", file, before);

        fs::remove_file(&path).unwrap();
        fs::remove_dir(&dir).unwrap();
        for result in [resized, replaced] {
            assert!(result.unwrap_err().contains("changed during hashing"));
        }
    }

    #[test]
    fn hash_is_the_empty_sha256() {
        let e = parse_manifest(&format!("{H}  a\n")).unwrap();
        assert_eq!(hex(&e[0].hash), H);
    }

    fn hex(b: &[u8]) -> String {
        b.iter().map(|x| format!("{x:02x}")).collect()
    }
}
