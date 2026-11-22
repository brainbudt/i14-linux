//! PSF2 bitmap fonts (Terminus) with their unicode table, as `i14ui/font.py`.

use std::collections::HashMap;
use std::io::{self, Read};
use std::path::Path;

const PSF2_MAGIC: [u8; 4] = [0x72, 0xb5, 0x4a, 0x86];
const HAS_UNICODE_TABLE: u32 = 1;

pub struct Font {
    /// Glyph width and height in pixels.
    pub w: usize,
    pub h: usize,
    rowbytes: usize,
    size: usize,
    glyphs: Vec<u8>,
    map: HashMap<char, usize>,
}

pub fn gunzip(path: impl AsRef<Path>) -> io::Result<Vec<u8>> {
    let mut data = Vec::new();
    flate2::read::GzDecoder::new(std::fs::File::open(path)?).read_to_end(&mut data)?;
    Ok(data)
}

fn bad(msg: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.to_string())
}

impl Font {
    /// Loads a gzip-compressed PSF2 file (`/usr/share/consolefonts/*.psf.gz`).
    pub fn open(path: impl AsRef<Path>) -> io::Result<Self> {
        Self::parse(&gunzip(path)?)
    }

    /// Parses an uncompressed PSF2 font (the interface's fonts, as font.py).
    pub fn parse(data: &[u8]) -> io::Result<Self> {
        Self::parse_psf2(data, 256, false)
    }

    /// PSF1 or PSF2, as the splash reads fonts; without a unicode table the first
    /// `plain` glyphs map to the characters with those codes.
    pub fn parse_any(data: &[u8], plain: usize) -> io::Result<Self> {
        if data.len() >= 4 && data[..2] == [0x36, 0x04] {
            return Self::parse_psf1(data, plain);
        }
        Self::parse_psf2(data, plain, true)
    }

    fn parse_psf1(data: &[u8], plain: usize) -> io::Result<Self> {
        let (mode, h) = (data[2], data[3] as usize);
        let (hdr, size, w) = (4, h, 8);
        let count = if mode & 1 != 0 { 512 } else { 256 };
        let end = hdr + count * size;
        if end > data.len() || h == 0 {
            return Err(bad("PSF1 header does not fit the file"));
        }
        let mut map = HashMap::new();
        if mode & 2 != 0 {
            // Per glyph: UCS-2 values, sequences after 0xFFFE, ended by 0xFFFF.
            let (mut index, mut seq) = (0, false);
            for v in data[end..]
                .as_chunks::<2>()
                .0
                .iter()
                .map(|c| u16::from_le_bytes(*c))
            {
                match v {
                    0xffff => (index, seq) = (index + 1, false),
                    0xfffe => seq = true,
                    _ if !seq => {
                        if let Some(ch) = char::from_u32(v as u32) {
                            map.entry(ch).or_insert(index);
                        }
                    }
                    _ => {}
                }
            }
        }
        if map.is_empty() {
            map = (0..count.min(plain))
                .filter_map(|i| char::from_u32(i as u32).map(|c| (c, i)))
                .collect();
        }
        Ok(Self {
            w,
            h,
            rowbytes: 1,
            size,
            glyphs: data[hdr..end].to_vec(),
            map,
        })
    }

    /// `fallback_on_empty`: also use the plain mapping when a unicode table maps nothing (splash).
    fn parse_psf2(data: &[u8], plain: usize, fallback_on_empty: bool) -> io::Result<Self> {
        if data.len() < 32 || data[..4] != PSF2_MAGIC {
            return Err(bad("not a PSF2 font"));
        }
        let u =
            |i: usize| u32::from_le_bytes(data[4 + 4 * i..8 + 4 * i].try_into().unwrap()) as usize;
        let (hdr, flags, count, size, h, w) = (u(1), u(2) as u32, u(3), u(4), u(5), u(6));
        let rowbytes = w.div_ceil(8);
        let end = count
            .checked_mul(size)
            .and_then(|n| n.checked_add(hdr))
            .filter(|&e| e <= data.len() && size >= rowbytes * h && w > 0 && h > 0)
            .ok_or_else(|| bad("PSF2 header does not fit the file"))?;
        let mut map = HashMap::new();
        if flags & HAS_UNICODE_TABLE != 0 {
            // Per glyph: UTF-8 characters, then sequences after 0xFE, ended by 0xFF.
            // The first glyph listing a character wins; invalid UTF-8 is skipped.
            for (index, entry) in data[end..].split(|&b| b == 0xff).take(count).enumerate() {
                let singles = entry.split(|&b| b == 0xfe).next().unwrap_or(&[]);
                for chunk in singles.utf8_chunks() {
                    for ch in chunk.valid().chars() {
                        map.entry(ch).or_insert(index);
                    }
                }
            }
        }
        if map.is_empty() && (fallback_on_empty || flags & HAS_UNICODE_TABLE == 0) {
            map = (0..count.min(plain))
                .filter_map(|i| char::from_u32(i as u32).map(|c| (c, i)))
                .collect();
        }
        Ok(Self {
            w,
            h,
            rowbytes,
            size,
            glyphs: data[hdr..end].to_vec(),
            map,
        })
    }

    pub fn has(&self, ch: char) -> bool {
        self.map.contains_key(&ch)
    }

    /// Glyph index for `ch`, falling back to '?' and then glyph 0.
    fn index(&self, ch: char) -> usize {
        self.map
            .get(&ch)
            .or_else(|| self.map.get(&'?'))
            .copied()
            .unwrap_or(0)
    }

    /// Whether pixel (x, y) of `ch`'s glyph is set (unscaled coordinates).
    pub fn pixel(&self, ch: char, x: usize, y: usize) -> bool {
        let g = &self.glyphs[self.index(ch) * self.size..][..self.size];
        let byte = g[y * self.rowbytes + x / 8];
        byte >> (7 - x % 8) & 1 == 1
    }

    /// One glyph's bit rows, for drawing loops that want to avoid repeated lookups.
    pub fn glyph(&self, ch: char) -> Glyph<'_> {
        Glyph {
            font: self,
            bits: &self.glyphs[self.index(ch) * self.size..][..self.size],
        }
    }
}

pub struct Glyph<'a> {
    font: &'a Font,
    bits: &'a [u8],
}

impl Glyph<'_> {
    pub fn pixel(&self, x: usize, y: usize) -> bool {
        self.bits[y * self.font.rowbytes + x / 8] >> (7 - x % 8) & 1 == 1
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A 2-glyph 8x2 font: glyph 0 = 'A' (top row set), glyph 1 = '?' and 'é' (left column set).
    pub(crate) fn tiny() -> Vec<u8> {
        let mut d = Vec::new();
        d.extend_from_slice(&PSF2_MAGIC);
        for v in [0u32, 32, 1, 2, 2, 2, 8] {
            d.extend_from_slice(&v.to_le_bytes());
        }
        d.extend_from_slice(&[0xff, 0x00, 0x80, 0x80]);
        d.extend_from_slice(b"A\xff?\xc3\xa9\xfe\xff");
        d
    }

    #[test]
    fn parses_unicode_table_and_falls_back_to_question_mark() {
        let f = Font::parse(&tiny()).unwrap();
        assert_eq!((f.w, f.h), (8, 2));
        assert!(f.has('A') && f.has('é') && !f.has('B'));
        assert!(f.pixel('A', 7, 0) && !f.pixel('A', 0, 1));
        assert!(f.pixel('é', 0, 1) && !f.pixel('é', 1, 0));
        assert!(
            f.pixel('B', 0, 0) && f.pixel('B', 0, 1),
            "unknown char draws '?'"
        );
    }

    #[test]
    fn rejects_truncated_font() {
        let mut d = tiny();
        d.truncate(33);
        assert!(Font::parse(&d).is_err());
        assert!(Font::parse(b"nope").is_err());
    }
}
