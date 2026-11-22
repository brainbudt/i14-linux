"""Drawing into the MediaTek framebuffer (/dev/fb0, 32-bit, R G B A byte order as i14-status used).

Canvas draws into a bytearray; Screen copies it to the framebuffer, pans to buffer 0 and controls
blank/backlight. Canvas alone works without a device (tests, screenshots).
"""
import fcntl
import mmap
import os
import struct

FBIOGET_VSCREENINFO, FBIOPAN_DISPLAY = 0x4600, 0x4606
BLANK = "/sys/class/graphics/fb0/blank"
BACKLIGHT = "/sys/class/leds/lcd-backlight/brightness"


def write(path, value):
    try:
        with open(path, "w") as f:
            f.write(value)
        return True
    except OSError:
        return False


class Canvas:
    def __init__(self, width, height, stride=None):
        self.w, self.h = width, height
        self.stride = (stride or width) * 4
        self.buf = bytearray(self.stride * height)

    def fill(self, x, y, w, h, color):
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.w, x + w), min(self.h, y + h)
        if x1 <= x0 or y1 <= y0:
            return
        row = (bytes(color) + b"\xff") * (x1 - x0)
        for yy in range(y0, y1):
            s = yy * self.stride + x0 * 4
            self.buf[s:s + len(row)] = row

    def clear(self, color=(0, 0, 0)):
        self.buf[:] = (bytes(color) + b"\xff") * (self.stride // 4) * self.h

    def text(self, x, y, s, font, color, bg=(0, 0, 0), scale=1, maxw=None):
        """Draw s with its top-left corner at (x, y); clipped to maxw pixels and the canvas. Returns width."""
        cw = font.w * scale
        limit = self.w - x if maxw is None else min(maxw, self.w - x)
        n = max(0, min(len(s), limit // cw)) if cw else 0
        if n == 0 or x < 0:
            return 0
        glyphs = [font.rows(ch, scale, color, bg) for ch in s[:n]]
        for r in range(min(font.h * scale, self.h - y)):
            if y + r < 0:
                continue
            row = b"".join(g[r] for g in glyphs)
            st = (y + r) * self.stride + x * 4
            self.buf[st:st + len(row)] = row
        return n * cw

    def text_width(self, s, font, scale=1):
        return len(s) * font.w * scale

    def pixel(self, x, y):
        s = y * self.stride + x * 4
        return tuple(self.buf[s:s + 3])


class Screen:
    """The real display."""

    def __init__(self, path="/dev/fb0"):
        self.fd = os.open(path, os.O_RDWR)
        self.var = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, self.var)
        xres, yres, xvirt, _ = struct.unpack_from("4I", self.var, 0)
        self.canvas = Canvas(xres, yres, xvirt)
        self.mem = mmap.mmap(self.fd, self.canvas.stride * yres)
        self.on = False
        self.brightness = 120

    def show(self):
        self.mem[:] = self.canvas.buf
        struct.pack_into("2I", self.var, 16, 0, 0)  # show buffer 0
        try:
            fcntl.ioctl(self.fd, FBIOPAN_DISPLAY, self.var)
        except OSError:
            pass

    def set_brightness(self, value):
        self.brightness = max(5, min(255, int(value)))
        if self.on:
            write(BACKLIGHT, str(self.brightness))

    def power(self, on):
        if on and not self.on:
            write(BLANK, "0")
            write(BACKLIGHT, str(self.brightness))
        elif not on and self.on:
            write(BACKLIGHT, "0")
            write(BLANK, "4")
        self.on = on

    def off_for_good(self):
        write(BACKLIGHT, "0")
        write(BLANK, "4")
