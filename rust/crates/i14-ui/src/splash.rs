//! Animated boot splash on /dev/fb0, as `/usr/sbin/i14-splash` (Python): the
//! base image (identical to the LK logo), a blinking cursor and "booting" dots,
//! and below them the last kernel log lines from /dev/kmsg, until a stop file
//! appears, SIGTERM, or 60 s. Assets come from scripts/build-splash.py.

use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{self, Read};
use std::os::unix::fs::OpenOptionsExt;
use std::path::Path;

use serde_json::Value;

use crate::font::Font;

pub const LIMIT_S: f64 = 60.0;
/// Just under "booting...", down to the bottom.
pub const LOG_TOP: usize = 1065;
pub const LOG_COLOR: [u8; 3] = [150, 170, 200];
pub const LOG_MARGIN: usize = 12;

pub struct Patch {
    pub period_ticks: usize,
    /// x, y, w, h
    pub rect: [usize; 4],
    pub states: Vec<Vec<u8>>,
}

pub struct Anim {
    pub w: usize,
    pub h: usize,
    pub tick_ms: u64,
    pub cursor: Patch,
    pub dots: Patch,
}

fn bad(msg: impl Into<String>) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, msg.into())
}

/// Standard base64 (with padding), as Python's base64.b64decode for valid input.
pub fn b64decode(s: &str) -> io::Result<Vec<u8>> {
    let val = |c: u8| -> Option<u32> {
        Some(match c {
            b'A'..=b'Z' => c - b'A',
            b'a'..=b'z' => c - b'a' + 26,
            b'0'..=b'9' => c - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        } as u32)
    };
    let s = s.trim_end_matches('=').as_bytes();
    let mut out = Vec::with_capacity(s.len() * 3 / 4);
    for chunk in s.chunks(4) {
        let mut n = 0u32;
        for (i, &c) in chunk.iter().enumerate() {
            n |= val(c).ok_or_else(|| bad("bad base64"))? << (18 - 6 * i);
        }
        let bytes = n.to_be_bytes();
        match chunk.len() {
            4 => out.extend_from_slice(&bytes[1..4]),
            3 => out.extend_from_slice(&bytes[1..3]),
            2 => out.push(bytes[1]),
            _ => return Err(bad("bad base64 length")),
        }
    }
    Ok(out)
}

fn inflate(data: &[u8]) -> io::Result<Vec<u8>> {
    let mut out = Vec::new();
    flate2::read::ZlibDecoder::new(data).read_to_end(&mut out)?;
    Ok(out)
}

pub fn load(assets: &str) -> io::Result<(Anim, Vec<u8>)> {
    let v: Value = serde_json::from_slice(&std::fs::read(Path::new(assets).join("anim.json"))?)
        .map_err(|e| bad(e.to_string()))?;
    let num = |v: &Value, k: &str| {
        v[k].as_u64()
            .map(|n| n as usize)
            .ok_or_else(|| bad(format!("'{k}'")))
    };
    let base = inflate(&std::fs::read(Path::new(assets).join("base.rgba.z"))?)?;
    let (w, h) = (num(&v, "w")?, num(&v, "h")?);
    if base.len() != w * h * 4 {
        return Err(bad("base image size does not match anim.json"));
    }
    let patch = |name: &str| -> io::Result<Patch> {
        let p = &v[name];
        let b: Vec<usize> = p["box"]
            .as_array()
            .ok_or_else(|| bad("'box'"))?
            .iter()
            .filter_map(|x| x.as_u64())
            .map(|x| x as usize)
            .collect();
        let rect: [usize; 4] = b.try_into().map_err(|_| bad("'box'"))?;
        let mut states = Vec::new();
        for s in p["states"].as_array().ok_or_else(|| bad("'states'"))? {
            states.push(inflate(&b64decode(
                s.as_str().ok_or_else(|| bad("'states'"))?,
            )?)?);
        }
        if states.iter().any(|s| s.len() != rect[2] * rect[3] * 4) {
            return Err(bad(format!("{name} patch size does not match its box")));
        }
        Ok(Patch {
            period_ticks: num(p, "period_ticks")?,
            rect,
            states,
        })
    };
    let tick_ms = num(&v, "tick_ms")? as u64;
    Ok((
        Anim {
            w,
            h,
            tick_ms,
            cursor: patch("cursor")?,
            dots: patch("dots")?,
        },
        base,
    ))
}

/// (cursor index, dots index) for a tick; the LK image is (0, 3): cursor on, three dots.
pub fn state(a: &Anim, tick: usize) -> (usize, usize) {
    (
        (tick / a.cursor.period_ticks) % a.cursor.states.len(),
        (3 + tick / a.dots.period_ticks) % a.dots.states.len(),
    )
}

/// RGBA bytes into the framebuffer's byte order (bit offsets of R, G, B, A).
pub fn swizzle(rgba: &[u8], offsets: [u32; 4]) -> Vec<u8> {
    if offsets == [0, 8, 16, 24] {
        return rgba.to_vec();
    }
    let mut out = vec![0u8; rgba.len()];
    for (i, &off) in offsets.iter().enumerate() {
        let o = (off / 8) as usize;
        for (px, dst) in rgba
            .as_chunks::<4>()
            .0
            .iter()
            .zip(out.as_chunks_mut::<4>().0)
        {
            if o < 4 {
                dst[o] = px[i];
            }
        }
    }
    out
}

pub trait Target {
    fn size(&self) -> (usize, usize);
    fn blit(&mut self, x: usize, y: usize, w: usize, h: usize, rgba: &[u8]);
    fn show(&mut self);
}

/// The framebuffer, 32 bpp.
pub struct Fb {
    file: File,
    var: [u32; 40],
    map: i14_sys::fb::Map,
    xres: usize,
    yres: usize,
    line: usize,
    offsets: [u32; 4],
}

impl Fb {
    pub fn open(path: &str) -> io::Result<Self> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let var = i14_sys::fb::var_raw(&file)?;
        let (xres, yres, xvirt, bpp) = (var[0] as usize, var[1] as usize, var[2] as usize, var[6]);
        // red, green, blue, transp: struct fb_bitfield { offset, length, msb_right } from byte 32
        let offsets = [var[8], var[11], var[14], var[17]];
        if bpp != 32 {
            return Err(bad(format!("{bpp} bpp")));
        }
        let line = xvirt * 4;
        let map = i14_sys::fb::Map::new(&file, line * yres)?;
        Ok(Self {
            file,
            var,
            map,
            xres,
            yres,
            line,
            offsets,
        })
    }
}

impl Target for Fb {
    fn size(&self) -> (usize, usize) {
        (self.xres, self.yres)
    }

    fn blit(&mut self, x: usize, y: usize, w: usize, h: usize, rgba: &[u8]) {
        let px = swizzle(rgba, self.offsets);
        let line = self.line;
        for r in 0..h {
            self.map
                .write_at((y + r) * line + x * 4, &px[r * w * 4..(r + 1) * w * 4]);
        }
    }

    fn show(&mut self) {
        let _ = i14_sys::fb::pan_to_origin(&self.file, &mut self.var); // buffer 0, as i14-status
    }
}

/// "(0)[178:wdtk-0]" (optionally with leading space and '-') before each MediaTek line.
fn strip_mtk_prefix(s: &str) -> &str {
    let t = s.trim_start();
    let t = t.strip_prefix('-').unwrap_or(t);
    let Some(t) = t.strip_prefix('(') else {
        return s;
    };
    let digits = t.bytes().take_while(u8::is_ascii_digit).count();
    let Some(t) = t[digits..].strip_prefix(")[").filter(|_| digits > 0) else {
        return s;
    };
    let digits = t.bytes().take_while(u8::is_ascii_digit).count();
    let Some(t) = t[digits..].strip_prefix(':').filter(|_| digits > 0) else {
        return s;
    };
    match t.find(']') {
        Some(i) => &t[i + 1..],
        None => s,
    }
}

/// 'prio,seq,usec,flags;text' -> '[   12.345678] text' without the MediaTek prefix.
pub fn format_record(rec: &[u8]) -> String {
    let rec = String::from_utf8_lossy(rec);
    let (head, text) = rec.split_once(';').unwrap_or((&rec, ""));
    let fields: Vec<&str> = head.split(',').collect();
    if fields.len() < 3 || fields[2].is_empty() || !fields[2].bytes().all(|b| b.is_ascii_digit()) {
        return String::new();
    }
    // kmsg escapes CR/LF as \xNN
    let text = text
        .split('\n')
        .next()
        .unwrap_or("")
        .replace("\\x0d", "")
        .replace("\\x0a", " ");
    let text = strip_mtk_prefix(&text).trim();
    let Ok(usec) = fields[2].parse::<u64>() else {
        return String::new();
    };
    if text.is_empty() {
        return String::new();
    }
    format!("[{:5}.{:06}] {text}", usec / 1_000_000, usec % 1_000_000)
}

/// Kernel log records from /dev/kmsg (the whole ring buffer first), non-blocking.
pub struct Kmsg {
    pub file: Option<File>,
    pub lines: VecDeque<String>,
    pub keep: usize,
}

impl Kmsg {
    pub fn open(path: &str, keep: usize) -> io::Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NONBLOCK)
            .open(path)?;
        Ok(Self {
            file: Some(file),
            lines: VecDeque::new(),
            keep,
        })
    }

    fn push(&mut self, line: String) {
        if self.keep == 0 {
            return;
        }
        if self.lines.len() == self.keep {
            self.lines.pop_front();
        }
        self.lines.push_back(line);
    }

    /// Reads what is new, at most `budget_s` / `max_records` per call (a log flood
    /// must not stall the animation or the stop checks); true if the lines changed.
    pub fn poll(&mut self, clock: &dyn Fn() -> f64, budget_s: f64, max_records: usize) -> bool {
        let mut new = false;
        let end = clock() + budget_s;
        let mut buf = vec![0u8; 8192];
        for _ in 0..max_records {
            if clock() > end {
                return new;
            }
            let Some(f) = &mut self.file else { return new };
            let n = match f.read(&mut buf) {
                Ok(n) => n,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => return new,
                Err(e) if e.raw_os_error() == Some(libc::EPIPE) => continue, // overwritten while we read
                Err(e) => {
                    eprintln!("i14-splash: kernel log stopped: {e}");
                    self.file = None; // the animation goes on without the log
                    return new;
                }
            };
            if n == 0 {
                return new;
            }
            let line = format_record(&buf[..n]);
            if !line.is_empty() {
                self.push(line);
                new = true;
            }
        }
        new
    }
}

/// How many text lines fit between LOG_TOP and the bottom margin.
pub fn log_lines(font: &Font, height: usize) -> usize {
    height.saturating_sub(LOG_MARGIN + LOG_TOP) / font.h
}

/// RGBA rows y0.. of the base image with the lines drawn on it (cut to the width).
pub fn render_log(
    base: &[u8],
    width: usize,
    y0: usize,
    lines: &[String],
    font: &Font,
    nlines: usize,
) -> Vec<u8> {
    let h = font.h * nlines;
    let mut out = base[y0 * width * 4..(y0 + h) * width * 4].to_vec();
    let px = [LOG_COLOR[0], LOG_COLOR[1], LOG_COLOR[2], 0xff];
    let cols = width.saturating_sub(2 * LOG_MARGIN) / font.w;
    for (n, line) in lines.iter().enumerate() {
        for (c, ch) in line.chars().take(cols).enumerate() {
            let g = font.glyph(ch);
            for r in 0..font.h {
                let row = (n * font.h + r) * width;
                for x in 0..font.w {
                    if g.pixel(x, r) {
                        let i = (row + LOG_MARGIN + c * font.w + x) * 4;
                        // Python's slice assignment past the end grows nothing that is shown; stay in bounds
                        if let Some(dst) = out.get_mut(i..i + 4) {
                            dst.copy_from_slice(&px);
                        }
                    }
                }
            }
        }
    }
    out
}

pub struct Run<'a> {
    pub stop: &'a str,
    pub clock: &'a dyn Fn() -> f64,
    pub sleep: &'a mut dyn FnMut(f64),
    pub limit: f64,
    pub log: Option<&'a mut Kmsg>,
    pub font: Option<&'a Font>,
    /// "%F %T" now, for the "kernel log on screen" line.
    pub stamp: &'a dyn Fn() -> String,
    /// True ends the animation like the stop file (SIGTERM).
    pub cancel: &'a dyn Fn() -> bool,
}

/// Animates until the stop file, the time limit; returns the number of ticks.
pub fn run(fb: &mut dyn Target, a: &Anim, base: &[u8], r: Run) -> io::Result<usize> {
    let (w, h) = (a.w, a.h);
    if fb.size() != (w, h) {
        let (x, y) = fb.size();
        return Err(bad(format!("screen {x}x{y}, splash {w}x{h}")));
    }
    fb.blit(0, 0, w, h, base);
    fb.show();
    let n = r.font.map_or(0, |f| log_lines(f, h));
    let t0 = (r.clock)();
    let (mut shown, mut tick, mut drawn) = (state(a, 0), 0usize, false);
    let mut log = r.log;
    while !(r.cancel)() && !Path::new(r.stop).exists() && (r.clock)() - t0 < r.limit {
        if let (Some(log), Some(font)) = (log.as_deref_mut(), r.font) {
            if log.poll(r.clock, 0.2, 5000) {
                let lines: Vec<String> = log.lines.iter().cloned().collect();
                fb.blit(
                    0,
                    LOG_TOP,
                    w,
                    font.h * n,
                    &render_log(base, w, LOG_TOP, &lines, font, n),
                );
                if !drawn {
                    drawn = true;
                    println!(
                        "{} kernel log on screen after {:.2} s",
                        (r.stamp)(),
                        (r.clock)() - t0
                    );
                }
            }
        }
        let now = state(a, tick);
        for (i, p) in [&a.cursor, &a.dots].into_iter().enumerate() {
            let (old, new) = if i == 0 {
                (shown.0, now.0)
            } else {
                (shown.1, now.1)
            };
            if new != old || tick == 0 {
                let [x, y, pw, ph] = p.rect;
                fb.blit(x, y, pw, ph, &p.states[new]);
            }
        }
        shown = now;
        tick += 1;
        (r.sleep)(a.tick_ms as f64 / 1000.0);
    }
    Ok(tick)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    /// The committed assets; on the phone I14_SPLASH_ASSETS=/usr/share/i14-splash.
    fn assets() -> String {
        std::env::var("I14_SPLASH_ASSETS").unwrap_or_else(|_| {
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/../../../rootfs/overlay/usr/share/i14-splash"
            )
            .into()
        })
    }

    struct FakeFb {
        w: usize,
        h: usize,
        canvas: Vec<u8>,
        blits: Vec<(usize, usize, usize, usize)>,
        shown: usize,
    }

    impl FakeFb {
        fn new(w: usize, h: usize) -> Self {
            Self {
                w,
                h,
                canvas: vec![0; w * h * 4],
                blits: Vec::new(),
                shown: 0,
            }
        }
    }

    impl Target for FakeFb {
        fn size(&self) -> (usize, usize) {
            (self.w, self.h)
        }
        fn blit(&mut self, x: usize, y: usize, w: usize, h: usize, rgba: &[u8]) {
            self.blits.push((x, y, w, h));
            for r in 0..h {
                let s = ((y + r) * self.w + x) * 4;
                self.canvas[s..s + w * 4].copy_from_slice(&rgba[r * w * 4..(r + 1) * w * 4]);
            }
        }
        fn show(&mut self) {
            self.shown += 1;
        }
    }

    fn tmp(name: &str) -> std::path::PathBuf {
        let d = std::env::temp_dir().join(format!("i14-splash-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn assets_fit_the_panel() {
        let (a, base) = load(&assets()).unwrap();
        assert_eq!((a.w, a.h, base.len()), (720, 1600, 720 * 1600 * 4));
        assert_eq!((a.cursor.states.len(), a.dots.states.len()), (2, 4));
    }

    #[test]
    fn first_state_is_the_lk_image() {
        let (a, base) = load(&assets()).unwrap();
        let d = tmp("first");
        let stop = d.join("stop");
        let mut fb = FakeFb::new(720, 1600);
        let stop_s = stop.to_str().unwrap().to_string();
        let mut sleep = |_: f64| {
            std::fs::write(&stop_s, "").unwrap();
        };
        let r = Run {
            stop: stop.to_str().unwrap(),
            clock: &|| 0.0,
            sleep: &mut sleep,
            limit: LIMIT_S,
            log: None,
            font: None,
            stamp: &String::new,
            cancel: &|| false,
        };
        run(&mut fb, &a, &base, r).unwrap();
        assert!(fb.canvas == base, "tick 0 is state (cursor on, 3 dots)");
        assert_eq!(fb.blits[0], (0, 0, 720, 1600));
        assert_eq!(fb.shown, 1);
        let _ = std::fs::remove_dir_all(d);
    }

    #[test]
    fn animation_cycles_and_stops() {
        let (a, base) = load(&assets()).unwrap();
        let seen: Vec<_> = (0..16).map(|t| state(&a, t)).collect();
        assert_eq!(seen[..4], [(0, 3), (0, 3), (1, 0), (1, 0)]);
        let now = Cell::new(0.0);
        let mut fb = FakeFb::new(720, 1600);
        let clock = || now.get();
        let mut sleep = |s: f64| now.set(now.get() + s);
        let r = Run {
            stop: "/nonexistent/never",
            clock: &clock,
            sleep: &mut sleep,
            limit: 5.0,
            log: None,
            font: None,
            stamp: &String::new,
            cancel: &|| false,
        };
        assert_eq!(run(&mut fb, &a, &base, r).unwrap(), 20); // 5 s at 250 ms
        let boxes = [a.cursor.rect, a.dots.rect];
        assert!(fb.blits[1..]
            .iter()
            .all(|b| boxes.contains(&[b.0, b.1, b.2, b.3])));
    }

    #[test]
    fn refuses_another_panel() {
        let (a, base) = load(&assets()).unwrap();
        let mut sleep = |_: f64| {};
        let r = Run {
            stop: "/nonexistent",
            clock: &|| 0.0,
            sleep: &mut sleep,
            limit: 0.0,
            log: None,
            font: None,
            stamp: &String::new,
            cancel: &|| false,
        };
        assert!(run(&mut FakeFb::new(1080, 2340), &a, &base, r).is_err());
    }

    #[test]
    fn swizzles() {
        let px = [1, 2, 3, 4];
        assert_eq!(swizzle(&px, [0, 8, 16, 24]), px);
        assert_eq!(swizzle(&px, [16, 8, 0, 24]), [3, 2, 1, 4]); // BGRA
    }

    #[test]
    fn format_record_strips_mediatek_prefix_and_escapes() {
        let rec = b"6,123,157517826,-;(0)[93:bat_thread][BAT_thread]Cable in, CHR_Type_num=2\\x0d\n SUBSYSTEM=x\n";
        assert_eq!(
            format_record(rec),
            "[  157.517826] [BAT_thread]Cable in, CHR_Type_num=2"
        );
        assert_eq!(
            format_record(b"6,5,1234567,-; -(1)[0:swapper/1]hello\n"),
            "[    1.234567] hello"
        );
        assert_eq!(format_record(b"no header"), "");
    }

    fn psf1(unicode: bool) -> Vec<u8> {
        let mut glyphs = vec![0u8; 256 * 4];
        glyphs[65 * 4] = 0x80;
        let mut d = vec![0x36, 0x04, if unicode { 2 } else { 0 }, 4];
        d.extend(glyphs);
        if unicode {
            for i in 0..256u16 {
                d.extend(i.to_le_bytes());
                d.extend(0xffffu16.to_le_bytes());
            }
        }
        d
    }

    fn psf2() -> Vec<u8> {
        let mut d = vec![0x72, 0xb5, 0x4a, 0x86];
        for v in [0u32, 32, 0, 128, 4, 4, 8] {
            d.extend(v.to_le_bytes());
        }
        let mut glyphs = vec![0u8; 128 * 4];
        glyphs[66 * 4] = 0x40;
        d.extend(glyphs);
        d
    }

    #[test]
    fn fonts_psf1_and_psf2() {
        for (data, ch, x) in [(psf1(true), 'A', 0), (psf2(), 'B', 1)] {
            let f = Font::parse_any(&data, 128).unwrap();
            assert_eq!((f.w, f.h), (8, 4));
            assert!(f.pixel(ch, x, 0) && (0..8).filter(|&i| f.pixel(ch, i, 0)).count() == 1);
        }
        assert!(Font::parse_any(b"nope", 128).is_err());
    }

    #[test]
    fn render_draws_on_the_base_below_booting() {
        let (a, base) = load(&assets()).unwrap();
        let font = Font::parse_any(&psf1(true), 128).unwrap();
        let n = log_lines(&font, 1600);
        assert_eq!(n, (1600 - LOG_MARGIN - LOG_TOP) / 4);
        assert!(LOG_TOP > a.dots.rect[1] + a.dots.rect[3]);
        let out = render_log(&base, 720, LOG_TOP, &["A".to_string()], &font, n);
        assert_eq!(out.len(), 720 * 4 * n * 4);
        let i = LOG_MARGIN * 4;
        assert_eq!(out[i..i + 4], [150, 170, 200, 0xff]);
        let y0 = LOG_TOP * 2880;
        let changed: Vec<usize> = (0..out.len())
            .step_by(4)
            .filter(|&k| out[k..k + 4] != base[y0 + k..y0 + k + 4])
            .collect();
        assert_eq!(changed, [i]);
    }

    #[test]
    fn kmsg_reads_records_and_keeps_the_last() {
        let d = tmp("kmsg");
        let fifo = d.join("kmsg");
        // a pipe returns all records in one read: only the first line is used, as in Python
        let (r, mut w) = std::io::pipe().unwrap();
        use std::io::Write;
        for k in 0..5 {
            w.write_all(format!("6,{k},{},-;line {k}\n", k * 1_000_000).as_bytes())
                .unwrap();
        }
        let file = File::from(std::os::fd::OwnedFd::from(r));
        i14_sys::process::set_nonblocking(std::os::fd::AsRawFd::as_raw_fd(&file)).unwrap();
        let mut log = Kmsg {
            file: Some(file),
            lines: VecDeque::new(),
            keep: 3,
        };
        assert!(log.poll(&|| 0.0, 0.2, 5000));
        assert_eq!(log.lines.len(), 1);
        assert!(!log.poll(&|| 0.0, 0.2, 5000));
        drop(w);
        let _ = (fifo, std::fs::remove_dir_all(d));
    }

    #[test]
    fn poll_is_bounded_by_its_time_budget() {
        let (r, _w) = std::io::pipe().unwrap();
        let mut log = Kmsg {
            file: Some(File::from(std::os::fd::OwnedFd::from(r))),
            lines: VecDeque::new(),
            keep: 3,
        };
        let t = Cell::new(0.0);
        let clock = || {
            t.set(t.get() + 1.0);
            t.get()
        };
        assert!(
            !log.poll(&clock, -1.0, 100),
            "budget already spent: no read at all"
        );
    }

    #[test]
    fn base64_matches_python() {
        assert_eq!(b64decode("aGVsbG8=").unwrap(), b"hello");
        assert_eq!(b64decode("aGk=").unwrap(), b"hi");
        assert_eq!(b64decode("YWJj").unwrap(), b"abc");
        assert!(b64decode("a").is_err());
    }
}
