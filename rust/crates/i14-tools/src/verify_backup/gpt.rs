//! Read-only GPT validation of the two 1 MiB user-area captures from
//! `scripts/phase0-backup.mtk` (512-byte sectors, UEFI 1.0 headers).
//!
//! Both copies are validated independently (signature, revision, header
//! bounds and CRC, declared entry array and its CRC, geometry, reciprocal
//! LBAs) and then cross-checked (disk GUID, usable bounds, byte-identical
//! arrays). One valid copy is never sufficient. Unsupported layouts (entry
//! size other than 128, more than 256 slots, revision other than 1.0) are
//! rejected explicitly. All offsets use checked arithmetic.

use std::collections::HashSet;

use super::{CAPTURE_LEN, SECTOR};

pub const MAX_ENTRIES: u32 = 256;
pub const ENTRY_SIZE: u32 = 128;
const NAME_BYTES: usize = 72;

#[derive(Clone, Copy, Debug)]
pub struct Header {
    pub my_lba: u64,
    pub alternate_lba: u64,
    pub first_usable: u64,
    pub last_usable: u64,
    pub disk_guid: [u8; 16],
    pub entry_lba: u64,
    pub entry_count: u32,
}

#[derive(Clone, Debug)]
pub struct Partition {
    pub name: String,
    pub guid: [u8; 16],
    pub first_lba: u64,
    pub last_lba: u64,
}

impl Partition {
    /// Inclusive GPT length in bytes.
    pub fn len_bytes(&self) -> Option<u64> {
        self.last_lba
            .checked_sub(self.first_lba)?
            .checked_add(1)?
            .checked_mul(SECTOR)
    }
}

pub struct Copy {
    pub header: Header,
    pub array: Vec<u8>,
    pub entries: Vec<Partition>,
}

fn take(b: &[u8], off: usize, n: usize) -> Result<&[u8], String> {
    b.get(off..off + n)
        .ok_or_else(|| format!("truncated: need {n} bytes at offset {off}"))
}

fn le_u32(b: &[u8], off: usize) -> Result<u32, String> {
    Ok(u32::from_le_bytes(
        take(b, off, 4)?.try_into().map_err(|_| "u32 field")?,
    ))
}

fn le_u64(b: &[u8], off: usize) -> Result<u64, String> {
    Ok(u64::from_le_bytes(
        take(b, off, 8)?.try_into().map_err(|_| "u64 field")?,
    ))
}

/// CRC32 (IEEE, as zlib) of the header with its own CRC field zeroed.
fn header_crc(sector: &[u8], header_size: usize) -> u32 {
    let mut buf = sector[..header_size].to_vec();
    buf[16..20].fill(0);
    crc32fast::hash(&buf)
}

fn decode_name(raw: &[u8]) -> Result<String, String> {
    let mut units = [0u16; NAME_BYTES / 2];
    for (i, pair) in raw.chunks(2).enumerate() {
        units[i] = u16::from_le_bytes([pair[0], pair[1]]);
    }
    let end = units
        .iter()
        .position(|&u| u == 0)
        .ok_or("partition name is not null-terminated")?;
    if end == 0 {
        return Err("empty partition name".into());
    }
    if units[end + 1..].iter().any(|&u| u != 0) {
        return Err("nonzero padding after partition name".into());
    }
    char::decode_utf16(units[..end].iter().copied())
        .collect::<Result<String, _>>()
        .map_err(|_| "partition name is not valid UTF-16 (unpaired surrogate)".into())
}

/// Parse and fully validate one GPT copy. `base_lba` is the first LBA covered
/// by `capture` (0 for the head capture, tail_offset/512 for the tail).
pub fn parse_copy(
    capture: &[u8],
    base_lba: u64,
    disk_lbas: u64,
    primary: bool,
) -> Result<Copy, String> {
    if capture.len() as u64 != CAPTURE_LEN {
        return Err(format!(
            "capture is {} bytes, expected {CAPTURE_LEN}",
            capture.len()
        ));
    }
    if disk_lbas < 68 {
        return Err(format!(
            "disk of {disk_lbas} sectors is too small for two GPT copies"
        ));
    }
    let last_lba = disk_lbas - 1;
    let header_lba = if primary { 1 } else { last_lba };
    let header_off = header_lba
        .checked_sub(base_lba)
        .and_then(|d| d.checked_mul(SECTOR))
        .and_then(|o| o.checked_add(SECTOR))
        .filter(|&end| end <= CAPTURE_LEN)
        .ok_or("GPT header lies outside this capture")?
        - SECTOR;
    let sector = take(capture, header_off as usize, SECTOR as usize)?;

    if take(sector, 0, 8)? != b"EFI PART" {
        return Err("bad GPT signature".into());
    }
    if le_u32(sector, 8)? != 0x0001_0000 {
        return Err("unsupported GPT revision (expected 1.0)".into());
    }
    let header_size = le_u32(sector, 12)? as usize;
    if !(92..=SECTOR as usize).contains(&header_size) {
        return Err(format!(
            "header size {header_size} out of bounds (92..=512)"
        ));
    }
    if le_u32(sector, 20)? != 0 {
        return Err("nonzero reserved header field".into());
    }
    if sector[92..header_size].iter().any(|&b| b != 0) {
        return Err("nonzero reserved header tail".into());
    }
    let stored_crc = le_u32(sector, 16)?;
    if header_crc(sector, header_size) != stored_crc {
        return Err("header CRC mismatch".into());
    }

    let h = Header {
        my_lba: le_u64(sector, 24)?,
        alternate_lba: le_u64(sector, 32)?,
        first_usable: le_u64(sector, 40)?,
        last_usable: le_u64(sector, 48)?,
        disk_guid: take(sector, 56, 16)?.try_into().map_err(|_| "guid")?,
        entry_lba: le_u64(sector, 72)?,
        entry_count: le_u32(sector, 80)?,
    };
    let entry_size = le_u32(sector, 84)?;
    if entry_size != ENTRY_SIZE {
        return Err(format!(
            "unsupported partition entry size {entry_size} (expected 128)"
        ));
    }
    if h.entry_count == 0 || h.entry_count > MAX_ENTRIES {
        return Err(format!(
            "entry count {} out of bounds (1..={MAX_ENTRIES})",
            h.entry_count
        ));
    }
    let (want_my, want_alt) = if primary {
        (1, last_lba)
    } else {
        (last_lba, 1)
    };
    if h.my_lba != want_my {
        return Err(format!("my_lba {} != expected {want_my}", h.my_lba));
    }
    if h.alternate_lba != want_alt {
        return Err(format!(
            "alternate_lba {} != expected {want_alt}",
            h.alternate_lba
        ));
    }
    if !(2 <= h.first_usable && h.first_usable <= h.last_usable && h.last_usable < last_lba) {
        return Err(format!(
            "usable LBAs {}..={} out of bounds (disk last LBA {last_lba})",
            h.first_usable, h.last_usable
        ));
    }

    let array_bytes = h.entry_count as u64 * ENTRY_SIZE as u64;
    let entry_sectors = array_bytes.div_ceil(SECTOR);
    let array_end_lba = h
        .entry_lba
        .checked_add(entry_sectors)
        .ok_or("entry array LBA overflow")?;
    if primary {
        if h.entry_lba < 2 || array_end_lba > h.first_usable {
            return Err("primary entry array overlaps the header or usable area".into());
        }
    } else {
        if array_end_lba > h.my_lba || h.last_usable >= h.entry_lba {
            return Err("backup entry array overlaps the header or usable area".into());
        }
    }
    let array_start = h
        .entry_lba
        .checked_sub(base_lba)
        .and_then(|d| d.checked_mul(SECTOR))
        .ok_or("entry array lies before this capture")?;
    if array_start
        .checked_add(array_bytes)
        .is_none_or(|end| end > CAPTURE_LEN)
    {
        return Err("entry array lies outside this capture".into());
    }
    let array = take(capture, array_start as usize, array_bytes as usize)?.to_vec();
    if crc32fast::hash(&array) != le_u32(sector, 88)? {
        return Err("entry array CRC mismatch".into());
    }

    let mut entries = Vec::new();
    let mut guids: HashSet<[u8; 16]> = HashSet::new();
    let mut names: HashSet<String> = HashSet::new();
    for i in 0..h.entry_count as usize {
        let e = take(&array, i * ENTRY_SIZE as usize, ENTRY_SIZE as usize)?;
        let type_guid: [u8; 16] = take(e, 0, 16)?.try_into().map_err(|_| "type guid")?;
        if type_guid == [0u8; 16] {
            if e.iter().any(|&b| b != 0) {
                return Err(format!("entry {i}: unused slot is not all-zero"));
            }
            continue;
        }
        let guid: [u8; 16] = take(e, 16, 16)?.try_into().map_err(|_| "guid")?;
        if guid == [0u8; 16] {
            return Err(format!("entry {i}: zero partition GUID"));
        }
        if !guids.insert(guid) {
            return Err(format!("entry {i}: duplicate partition GUID"));
        }
        let first_lba = le_u64(e, 32)?;
        let last = le_u64(e, 40)?;
        if first_lba > last {
            return Err(format!(
                "entry {i}: first LBA {first_lba} past last LBA {last}"
            ));
        }
        if first_lba < h.first_usable || last > h.last_usable {
            return Err(format!(
                "entry {i}: LBAs {first_lba}..={last} outside usable {}..={}",
                h.first_usable, h.last_usable
            ));
        }
        let name = decode_name(take(e, 56, NAME_BYTES)?).map_err(|e| format!("entry {i}: {e}"))?;
        if !names.insert(name.clone()) {
            return Err(format!("entry {i}: duplicate partition name '{name}'"));
        }
        entries.push(Partition {
            name,
            guid,
            first_lba,
            last_lba: last,
        });
    }
    let mut sorted = entries.clone();
    sorted.sort_by_key(|p| p.first_lba);
    for w in sorted.windows(2) {
        if w[1].first_lba <= w[0].last_lba {
            return Err(format!(
                "overlapping partitions '{}' ({}..={}) and '{}' ({}..={})",
                w[0].name, w[0].first_lba, w[0].last_lba, w[1].name, w[1].first_lba, w[1].last_lba
            ));
        }
    }
    Ok(Copy {
        header: h,
        array,
        entries,
    })
}

/// Both copies must describe the same disk: GUID, usable bounds, reciprocal
/// pointers and byte-identical entry arrays (including unused slots).
pub fn cross_check(primary: &Copy, backup: &Copy) -> Result<(), String> {
    let (p, b) = (&primary.header, &backup.header);
    // The I14 vendor GPT has a zero disk GUID in both copies.
    if p.disk_guid != b.disk_guid {
        return Err("disk GUID differs between GPT copies".into());
    }
    if p.first_usable != b.first_usable || p.last_usable != b.last_usable {
        return Err("usable LBA bounds differ between GPT copies".into());
    }
    if p.alternate_lba != b.my_lba || b.alternate_lba != p.my_lba {
        return Err("GPT copies do not point at each other".into());
    }
    if primary.array != backup.array {
        return Err("entry arrays differ between GPT copies".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn crc32_matches_the_known_check_vector() {
        assert_eq!(crc32fast::hash(b"123456789"), 0xCBF4_3926);
    }

    #[test]
    fn utf16_names() {
        let mut raw = [0u8; NAME_BYTES];
        for (i, u) in "boot".encode_utf16().enumerate() {
            raw[i * 2..i * 2 + 2].copy_from_slice(&u.to_le_bytes());
        }
        assert_eq!(decode_name(&raw).unwrap(), "boot");
        // Surrogate pair (U+1F600) decodes; a lone surrogate does not.
        let mut pair = [0u8; NAME_BYTES];
        pair[..2].copy_from_slice(&0xD83Du16.to_le_bytes());
        pair[2..4].copy_from_slice(&0xDE00u16.to_le_bytes());
        assert_eq!(decode_name(&pair).unwrap(), "\u{1F600}");
        let mut lone = [0u8; NAME_BYTES];
        lone[..2].copy_from_slice(&0xD83Du16.to_le_bytes());
        assert!(decode_name(&lone).is_err());
        assert!(decode_name(&[0u8; NAME_BYTES]).is_err(), "empty");
        let mut unterminated = [0u8; NAME_BYTES];
        for j in 0..NAME_BYTES / 2 {
            unterminated[j * 2..j * 2 + 2].copy_from_slice(&u16::from(b'x').to_le_bytes());
        }
        assert!(decode_name(&unterminated).is_err());
        let mut padding = raw;
        padding[NAME_BYTES - 1] = 1;
        assert!(decode_name(&padding).is_err());
    }

    #[test]
    fn inclusive_length() {
        let p = Partition {
            name: "x".into(),
            guid: [1; 16],
            first_lba: 34,
            last_lba: 35,
        };
        assert_eq!(p.len_bytes(), Some(1024));
        let p = Partition {
            name: "x".into(),
            guid: [1; 16],
            first_lba: 0,
            last_lba: u64::MAX,
        };
        assert_eq!(p.len_bytes(), None);
    }
}
