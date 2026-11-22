//! The real display, as `Screen` in `i14ui/fb.py`: the canvas is copied into the
//! framebuffer, which is panned to buffer 0; blank and backlight via sysfs.

use std::fs::{File, OpenOptions};
use std::io;

use i14_sys::fb;

use crate::app::Display;
use crate::canvas::Canvas;

pub const BLANK: &str = "/sys/class/graphics/fb0/blank";
pub const BACKLIGHT: &str = "/sys/class/leds/lcd-backlight/brightness";

/// Writes a sysfs value; false when that failed (as fb.py's write()).
pub fn write(path: &str, value: &str) -> bool {
    std::fs::write(path, value).is_ok()
}

/// Backlight off and display blanked: for exit, crash and power-off paths.
pub fn off_for_good() {
    write(BACKLIGHT, "0");
    write(BLANK, "4");
}

pub struct Screen {
    file: File,
    var: [u32; 40],
    map: fb::Map,
    pub on: bool,
    pub brightness: u8,
}

impl Screen {
    /// The screen and a canvas of its size (rows as long as the virtual width).
    pub fn open(path: &str) -> io::Result<(Self, Canvas)> {
        let file = OpenOptions::new().read(true).write(true).open(path)?;
        let var = fb::var_raw(&file)?;
        let (xres, yres, xvirt) = (var[0], var[1], var[2]);
        let canvas = Canvas::new(xres, yres, Some(xvirt));
        let map = fb::Map::new(&file, canvas.buf.len())?;
        Ok((
            Self {
                file,
                var,
                map,
                on: false,
                brightness: 120,
            },
            canvas,
        ))
    }
}

impl Display for Screen {
    fn show(&mut self, canvas: &Canvas) {
        self.map.write_at(0, &canvas.buf);
        let _ = fb::pan_to_origin(&self.file, &mut self.var);
    }

    fn set_brightness(&mut self, value: i64) {
        self.brightness = value.clamp(5, 255) as u8;
        if self.on {
            write(BACKLIGHT, &self.brightness.to_string());
        }
    }

    fn power(&mut self, on: bool) {
        if on && !self.on {
            write(BLANK, "0");
            write(BACKLIGHT, &self.brightness.to_string());
        } else if !on && self.on {
            write(BACKLIGHT, "0");
            write(BLANK, "4");
        }
        self.on = on;
    }

    fn is_on(&self) -> bool {
        self.on
    }
}
