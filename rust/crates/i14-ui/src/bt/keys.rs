//! Link keys of paired devices, one line per device:
//! `AA:BB:CC:DD:EE:FF <32 hex key> <key type> <name, rest of line>`.
//!
//! The file is a secret: written 0600 through a temporary file, fsync and rename, so a crash leaves
//! either the old or the new file. Errors become messages for the interface, never panics.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

pub const KEYS_FILE: &str = "/mnt/data/media/0/i14-linux/bt-keys";

#[derive(Clone, PartialEq, Eq)]
pub struct Paired {
    pub addr: String,
    pub key: [u8; 16],
    pub kind: u8,
    pub name: String,
}

/// Never prints the key.
impl std::fmt::Debug for Paired {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Paired")
            .field("addr", &self.addr)
            .field("kind", &self.kind)
            .field("name", &self.name)
            .finish_non_exhaustive()
    }
}

/// "aa:bb:..." in any case -> "AA:BB:...", or None when it is not an address.
pub fn norm_addr(s: &str) -> Option<String> {
    let parts: Vec<&str> = s.split(':').collect();
    if parts.len() != 6
        || parts
            .iter()
            .any(|p| p.len() != 2 || !p.bytes().all(|b| b.is_ascii_hexdigit()))
    {
        return None;
    }
    Some(parts.join(":").to_ascii_uppercase())
}

/// "AA:BB:CC:DD:EE:FF" -> the 6 bytes in HCI (little-endian) order.
pub fn addr_bytes(s: &str) -> Option<[u8; 6]> {
    let n = norm_addr(s)?;
    let mut out = [0u8; 6];
    for (i, h) in n.split(':').rev().enumerate() {
        out[i] = u8::from_str_radix(h, 16).ok()?;
    }
    Some(out)
}

fn hex(key: &[u8]) -> String {
    key.iter().map(|b| format!("{b:02x}")).collect()
}

fn unhex(s: &str) -> Option<[u8; 16]> {
    if s.len() != 32 || !s.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let mut out = [0u8; 16];
    for (i, o) in out.iter_mut().enumerate() {
        *o = u8::from_str_radix(&s[2 * i..2 * i + 2], 16).ok()?;
    }
    Some(out)
}

/// A remote name as one safe line: control characters become spaces.
pub fn clean_name(name: &str) -> String {
    name.chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect::<String>()
        .trim()
        .chars()
        .take(64)
        .collect()
}

#[derive(Debug, Default)]
pub struct KeyStore {
    /// None keeps the keys in memory only (tests that do not care about the file).
    pub path: Option<PathBuf>,
    pub keys: Vec<Paired>,
}

impl KeyStore {
    pub fn new(path: Option<PathBuf>) -> Self {
        Self {
            path,
            keys: Vec::new(),
        }
    }

    /// Reads the file again. A missing file is an empty store; a damaged line is skipped and
    /// reported in the returned message.
    pub fn load(&mut self) -> Option<String> {
        let Some(path) = &self.path else {
            return None;
        };
        let text = match fs::read_to_string(path) {
            Ok(t) => t,
            Err(e) if e.kind() == io::ErrorKind::NotFound => {
                self.keys.clear();
                return None;
            }
            Err(e) => {
                self.keys.clear();
                return Some(format!(
                    "koppelingen lezen mislukt: {}",
                    super::strerror(&e)
                ));
            }
        };
        let mut keys = Vec::new();
        let mut bad = Vec::new();
        for (n, line) in text.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            match Self::parse(line) {
                Some(p) if !keys.iter().any(|k: &Paired| k.addr == p.addr) => keys.push(p),
                Some(_) => {}
                None => bad.push((n + 1).to_string()),
            }
        }
        self.keys = keys;
        (!bad.is_empty())
            .then(|| format!("bt-keys: regel {} beschadigd, overgeslagen", bad.join(",")))
    }

    fn parse(line: &str) -> Option<Paired> {
        let mut it = line.splitn(4, ' ');
        let addr = norm_addr(it.next()?)?;
        let key = unhex(it.next()?)?;
        let kind = it.next()?.parse::<u8>().ok()?;
        let name = clean_name(it.next().unwrap_or(""));
        Some(Paired {
            addr,
            key,
            kind,
            name,
        })
    }

    pub fn get(&self, addr: &str) -> Option<&Paired> {
        self.keys.iter().find(|k| k.addr == addr)
    }

    /// Stores (or replaces) a key. On a write error the old state is kept.
    pub fn put(&mut self, addr: &str, key: [u8; 16], kind: u8, name: &str) -> Result<(), String> {
        let addr = norm_addr(addr).ok_or_else(|| format!("ongeldig adres {addr}"))?;
        let mut keys = self.keys.clone();
        let entry = Paired {
            addr: addr.clone(),
            key,
            kind,
            name: clean_name(name),
        };
        match keys.iter_mut().find(|k| k.addr == addr) {
            Some(k) => {
                if entry.name.is_empty() {
                    let name = std::mem::take(&mut k.name);
                    *k = Paired { name, ..entry };
                } else {
                    *k = entry;
                }
            }
            None => keys.push(entry),
        }
        self.commit(keys)
    }

    /// Fills in a name learned later; nothing happens for unknown devices or unchanged names.
    pub fn set_name(&mut self, addr: &str, name: &str) -> Result<(), String> {
        let name = clean_name(name);
        let mut keys = self.keys.clone();
        match keys.iter_mut().find(|k| k.addr == addr) {
            Some(k) if !name.is_empty() && k.name != name => k.name = name,
            _ => return Ok(()),
        }
        self.commit(keys)
    }

    /// Removes one device; Ok(false) when it was not paired.
    pub fn remove(&mut self, addr: &str) -> Result<bool, String> {
        let mut keys = self.keys.clone();
        let before = keys.len();
        keys.retain(|k| k.addr != addr);
        if keys.len() == before {
            return Ok(false);
        }
        self.commit(keys).map(|_| true)
    }

    fn commit(&mut self, keys: Vec<Paired>) -> Result<(), String> {
        if let Some(path) = &self.path {
            write_atomic(path, &render(&keys))
                .map_err(|e| format!("koppeling opslaan mislukt: {}", super::strerror(&e)))?;
        }
        self.keys = keys;
        Ok(())
    }
}

fn render(keys: &[Paired]) -> String {
    keys.iter()
        .map(|k| format!("{} {} {} {}\n", k.addr, hex(&k.key), k.kind, k.name))
        .collect()
}

/// tmp (0600, fresh) + fsync + rename + fsync of the directory.
pub fn write_atomic(path: &Path, text: &str) -> io::Result<()> {
    write_atomic_mode(path, text, 0o600)
}

pub fn write_atomic_mode(path: &Path, text: &str, mode: u32) -> io::Result<()> {
    let dir = path.parent().unwrap_or(Path::new("."));
    let mut tmp_name = path.file_name().unwrap_or_default().to_os_string();
    tmp_name.push(".tmp");
    let tmp = dir.join(tmp_name);
    let _ = fs::remove_file(&tmp); // a stale tmp from a crash: never reuse its mode or content
    let r = (|| {
        let mut f = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(mode)
            .open(&tmp)?;
        f.set_permissions(fs::Permissions::from_mode(mode))?; // umask must not widen or narrow it
        f.write_all(text.as_bytes())?;
        f.sync_all()?;
        fs::rename(&tmp, path)?;
        if let Ok(d) = fs::File::open(dir) {
            let _ = d.sync_all();
        }
        Ok(())
    })();
    if r.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    r
}

#[cfg(test)]
pub(crate) fn tests_tmpdir(tag: &str) -> PathBuf {
    tests::tmpdir(tag)
}

#[cfg(test)]
mod tests {
    use super::*;

    pub(crate) fn tmpdir(tag: &str) -> PathBuf {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static N: AtomicUsize = AtomicUsize::new(0);
        let d = std::env::temp_dir().join(format!(
            "i14-bt-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    const KEY: [u8; 16] = [
        0x10, 0x32, 0x54, 0x76, 0x98, 0xba, 0xdc, 0xfe, 0, 1, 2, 3, 4, 5, 6, 0xff,
    ];

    #[test]
    fn missing_file_is_empty() {
        let d = tmpdir("missing");
        let mut s = KeyStore::new(Some(d.join("bt-keys")));
        assert_eq!(s.load(), None);
        assert!(s.keys.is_empty());
    }

    #[test]
    fn save_then_load_roundtrip() {
        let d = tmpdir("rt");
        let p = d.join("bt-keys");
        let mut s = KeyStore::new(Some(p.clone()));
        s.put("02:11:22:33:44:55", KEY, 5, "Test Host").unwrap();
        s.put("01:02:03:04:05:06", KEY, 8, "").unwrap();
        let text = fs::read_to_string(&p).unwrap();
        assert_eq!(
            text,
            "02:11:22:33:44:55 1032547698badcfe00010203040506ff 5 Test Host\n\
             01:02:03:04:05:06 1032547698badcfe00010203040506ff 8 \n"
        );
        let mut t = KeyStore::new(Some(p));
        assert_eq!(t.load(), None);
        assert_eq!(t.keys, s.keys);
        assert_eq!(t.get("02:11:22:33:44:55").unwrap().name, "Test Host");
        // a new key without a name keeps the old name
        t.put("02:11:22:33:44:55", [1; 16], 6, "").unwrap();
        let k = t.get("02:11:22:33:44:55").unwrap();
        assert_eq!((k.key, k.kind, k.name.as_str()), ([1; 16], 6, "Test Host"));
        assert_eq!(t.keys.len(), 2);
    }

    #[test]
    fn file_mode_is_0600() {
        let d = tmpdir("mode");
        let p = d.join("bt-keys");
        fs::write(&p, "").unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
        let mut s = KeyStore::new(Some(p.clone()));
        s.put("01:02:03:04:05:06", KEY, 5, "x").unwrap();
        let mode = fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
    }

    #[test]
    fn write_is_atomic() {
        let d = tmpdir("atomic");
        let p = d.join("bt-keys");
        // a stale tmp from an earlier crash is ignored when loading and replaced when saving
        fs::write(d.join("bt-keys.tmp"), "garbage that must not survive\n").unwrap();
        fs::set_permissions(d.join("bt-keys.tmp"), fs::Permissions::from_mode(0o666)).unwrap();
        let mut s = KeyStore::new(Some(p.clone()));
        assert_eq!(s.load(), None);
        assert!(s.keys.is_empty());
        s.put("01:02:03:04:05:06", KEY, 5, "a").unwrap();
        assert!(!d.join("bt-keys.tmp").exists());
        assert!(fs::read_to_string(&p)
            .unwrap()
            .starts_with("01:02:03:04:05:06 "));
        // a failing write (directory gone) keeps the old state in memory and reports it
        fs::remove_dir_all(&d).unwrap();
        let e = s.put("0A:0B:0C:0D:0E:0F", KEY, 5, "b").unwrap_err();
        assert!(e.starts_with("koppeling opslaan mislukt: "), "{e}");
        assert_eq!(s.keys.len(), 1);
    }

    #[test]
    fn damaged_line_is_skipped_and_reported() {
        let d = tmpdir("damaged");
        let p = d.join("bt-keys");
        fs::write(
            &p,
            "01:02:03:04:05:06 1032547698badcfe00010203040506ff 5 Goed\n\
             zz:02:03:04:05:06 1032547698badcfe00010203040506ff 5 Fout adres\n\
             0A:0B:0C:0D:0E:0F 1234 5 te korte sleutel\n\
             \n\
             0A:0B:0C:0D:0E:10 1032547698badcfe00010203040506ff\n",
        )
        .unwrap();
        let mut s = KeyStore::new(Some(p));
        assert_eq!(
            s.load().as_deref(),
            Some("bt-keys: regel 2,3,5 beschadigd, overgeslagen")
        );
        assert_eq!(s.keys.len(), 1);
        assert_eq!(s.keys[0].name, "Goed");
    }

    #[test]
    fn remove_deletes_only_that_address() {
        let d = tmpdir("remove");
        let p = d.join("bt-keys");
        let mut s = KeyStore::new(Some(p.clone()));
        s.put("01:02:03:04:05:06", KEY, 5, "a").unwrap();
        s.put("0A:0B:0C:0D:0E:0F", KEY, 5, "b").unwrap();
        assert_eq!(s.remove("01:02:03:04:05:06"), Ok(true));
        assert_eq!(s.remove("01:02:03:04:05:06"), Ok(false));
        let text = fs::read_to_string(&p).unwrap();
        assert!(!text.contains("01:02:03:04:05:06") && text.contains("0A:0B:0C:0D:0E:0F"));
    }

    #[test]
    fn names_are_one_line_and_debug_hides_the_key() {
        let mut s = KeyStore::new(None);
        s.put("01:02:03:04:05:06", KEY, 5, "a\nb\x00c").unwrap();
        assert_eq!(s.keys[0].name, "a b c");
        let dbg = format!("{:?}", s.keys[0]);
        assert!(
            !dbg.contains("1032547698") && !dbg.contains("16, 50"),
            "{dbg}"
        );
        assert_eq!(addr_bytes("01:02:03:04:05:06"), Some([6, 5, 4, 3, 2, 1]));
        assert_eq!(addr_bytes("01:02:03"), None);
    }
}
