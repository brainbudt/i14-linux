//! Off-screen drawing in the framebuffer's pixel format, as `i14ui/fb.py`'s Canvas.
//!
//! Pixels are 4 bytes, R G B 0xFF, rows `stride` pixels apart (the framebuffer's
//! virtual width). Works without a device, for tests and screenshots.

use crate::font::Font;

pub type Rgb = [u8; 3];

pub struct Canvas {
    pub w: i32,
    pub h: i32,
    /// Bytes per row.
    pub stride: usize,
    pub buf: Vec<u8>,
}

impl Canvas {
    /// `stride_px` is the row length in pixels (defaults to `width`).
    pub fn new(width: u32, height: u32, stride_px: Option<u32>) -> Self {
        let stride = stride_px.unwrap_or(width) as usize * 4;
        Self {
            w: width as i32,
            h: height as i32,
            stride,
            buf: vec![0; stride * height as usize],
        }
    }

    fn px(color: Rgb) -> [u8; 4] {
        [color[0], color[1], color[2], 0xff]
    }

    pub fn fill(&mut self, x: i32, y: i32, w: i32, h: i32, color: Rgb) {
        let (x0, y0) = (x.max(0), y.max(0));
        let (x1, y1) = (
            self.w.min(x.saturating_add(w)),
            self.h.min(y.saturating_add(h)),
        );
        if x1 <= x0 || y1 <= y0 {
            return;
        }
        let p = Self::px(color);
        for yy in y0..y1 {
            let s = yy as usize * self.stride + x0 as usize * 4;
            for c in self.buf[s..s + (x1 - x0) as usize * 4]
                .as_chunks_mut::<4>()
                .0
            {
                c.copy_from_slice(&p);
            }
        }
    }

    /// Fills the whole buffer, row padding included.
    pub fn clear(&mut self, color: Rgb) {
        let p = Self::px(color);
        for c in self.buf.as_chunks_mut::<4>().0 {
            c.copy_from_slice(&p);
        }
    }

    /// Draws `s` with its top-left corner at (x, y), clipped to `maxw` pixels and
    /// the canvas; only whole characters are drawn. Returns the drawn width.
    #[allow(clippy::too_many_arguments)]
    pub fn text(
        &mut self,
        x: i32,
        y: i32,
        s: &str,
        font: &Font,
        color: Rgb,
        bg: Rgb,
        scale: usize,
        maxw: Option<i32>,
    ) -> i32 {
        let cw = (font.w * scale) as i32;
        let limit = match maxw {
            None => self.w - x,
            Some(m) => m.min(self.w - x),
        };
        if cw == 0 || x < 0 || limit < cw {
            return 0;
        }
        let n = s.chars().count().min((limit / cw) as usize);
        if n == 0 {
            return 0;
        }
        let (on, off) = (Self::px(color), Self::px(bg));
        let rows = ((font.h * scale) as i32).min(self.h - y);
        for (i, ch) in s.chars().take(n).enumerate() {
            let g = font.glyph(ch);
            let left = x as usize + i * cw as usize;
            for r in 0..rows.max(0) {
                let yy = y + r;
                if yy < 0 {
                    continue;
                }
                let gy = r as usize / scale;
                let row = &mut self.buf[yy as usize * self.stride + left * 4..][..cw as usize * 4];
                for (px, c) in row.as_chunks_mut::<4>().0.iter_mut().enumerate() {
                    c.copy_from_slice(if g.pixel(px / scale, gy) { &on } else { &off });
                }
            }
        }
        n as i32 * cw
    }

    pub fn text_width(&self, s: &str, font: &Font, scale: usize) -> i32 {
        (s.chars().count() * font.w * scale) as i32
    }

    pub fn pixel(&self, x: i32, y: i32) -> Rgb {
        let s = y as usize * self.stride + x as usize * 4;
        [self.buf[s], self.buf[s + 1], self.buf[s + 2]]
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::font::tests::tiny;

    #[test]
    fn fill_clips_to_canvas() {
        let mut c = Canvas::new(4, 3, Some(5));
        c.fill(-2, 1, 4, 10, [1, 2, 3]);
        assert_eq!(c.pixel(0, 1), [1, 2, 3]);
        assert_eq!(c.pixel(1, 2), [1, 2, 3]);
        assert_eq!(c.pixel(2, 1), [0, 0, 0]);
        assert_eq!(c.pixel(0, 0), [0, 0, 0]);
        assert_eq!(c.buf[c.stride + 3], 0xff, "alpha byte set");
        c.fill(10, 0, 5, 5, [9, 9, 9]); // fully outside: no panic, no change
    }

    #[test]
    fn text_draws_whole_characters_only() {
        let f = Font::parse(&tiny()).unwrap();
        let mut c = Canvas::new(20, 4, None);
        // 20 px wide, 8 px glyphs at x=1: two characters fit.
        assert_eq!(c.text(1, 0, "AAA", &f, [9, 9, 9], [1, 1, 1], 1, None), 16);
        assert_eq!(c.pixel(1, 0), [9, 9, 9]);
        assert_eq!(c.pixel(1, 1), [1, 1, 1]);
        assert_eq!(c.pixel(17, 0), [0, 0, 0], "third char clipped away");
        assert_eq!(c.text(-1, 0, "A", &f, [9, 9, 9], [1, 1, 1], 1, None), 0);
        assert_eq!(c.text(0, 0, "A", &f, [9, 9, 9], [1, 1, 1], 1, Some(7)), 0);
    }

    #[test]
    fn text_scales_and_clips_vertically() {
        let f = Font::parse(&tiny()).unwrap();
        let mut c = Canvas::new(16, 3, None);
        assert_eq!(c.text(0, -1, "A", &f, [9, 9, 9], [1, 1, 1], 2, None), 16);
        // scale 2: glyph rows 0,0,1,1; y=-1 skips the first, so canvas rows = glyph rows 0,1,1.
        assert_eq!(c.pixel(15, 0), [9, 9, 9]);
        assert_eq!(c.pixel(15, 1), [1, 1, 1]);
        assert_eq!(c.pixel(15, 2), [1, 1, 1]);
    }
}
