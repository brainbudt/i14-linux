#!/usr/bin/env python3
"""Build the I14 boot splash "terminal" (design 2) for both layers, from one drawing function.

  python3 scripts/build-splash.py PNG_OUT ASSET_DIR [CHARGING_PNG]

PNG_OUT    720x1600 image for the LK logo (feed it to i14-logo.py replace 0,88 and i14-logo-sign.py)
ASSET_DIR  base.rgba.z + anim.json for /usr/share/i14-splash (the Linux layer, scripts: i14-splash)
CHARGING_PNG  optional: logo #2, the first picture of charging while off,
           a neutral battery with a bolt instead of the stock "empty" battery; LK also shows #2 when the
           battery is too low to boot, so it must not claim a level

The LK image is exactly animation state (cursor on, 3 dots), so the hand-over from LK to Linux shows
no jump. Animation: blinking cursor and "booting" dots. Patches hold only the pixels that change.
Runs on the Mac (Pillow, macOS system fonts); the phone only needs the Python stdlib.
"""
import base64
import json
from pathlib import Path
import sys
import zlib

from PIL import Image, ImageChops, ImageDraw, ImageFont

W, H = 720, 1600
AVENIR, MENLO = "/System/Library/Fonts/Avenir Next.ttc", "/System/Library/Fonts/Menlo.ttc"
GREEN, BOX, TEXT, SUB = (90, 200, 140), (20, 30, 45), (255, 255, 255), (150, 170, 200)
DOT_STATES, CURSOR_STATES = (0, 1, 2, 3), (True, False)


def font(path, size, index):
    return ImageFont.truetype(path, size, index=index)


def background():
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(10 + 10 * t), int(25 + 20 * t), int(60 + 50 * t)))
    return im


def draw_charging():
    im = background()
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([270, 690, 450, 1030], radius=26, fill=BOX, outline=GREEN, width=8)
    d.rounded_rectangle([325, 662, 395, 694], radius=10, fill=GREEN)
    d.polygon([(375, 745), (318, 875), (358, 875), (335, 975), (402, 830), (362, 830), (392, 745)], fill=GREEN)
    return im


def draw(cursor=True, dots=3):
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(10 + 10 * t), int(25 + 20 * t), int(60 + 50 * t)))
    s, x0, y0 = 260, (W - 260) // 2, 560
    d.rounded_rectangle([x0, y0, x0 + s, y0 + s], radius=56, fill=BOX, outline=GREEN, width=6)
    mono = font(MENLO, 130, 1)
    prompt = ">" + ("_" if cursor else " ")
    d.text(((W - d.textlength(">_", font=mono)) / 2, y0 + 62), prompt, font=mono, fill=GREEN)
    title = font(AVENIR, 66, 0)  # Bold
    d.text(((W - d.textlength("i14 linux", font=title)) / 2, y0 + s + 70), "i14 linux", font=title, fill=TEXT)
    sub = font(AVENIR, 38, 5)  # Medium
    full = "booting..."
    x = (W - d.textlength(full, font=sub)) / 2  # centred on the full text: the dots never shift it
    d.text((x, y0 + s + 160), "booting" + "." * dots, font=sub, fill=SUB)
    return im


def patch_region(states):
    """Union bounding box of where the given images differ from each other."""
    box = None
    for a in states[1:]:
        b = ImageChops.difference(states[0], a).getbbox()
        if b:
            box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
    return box


def rgba(im):
    return im.convert("RGBA").tobytes()


def build(png_out, asset_dir):
    base = draw()
    regions = {}
    for name, states, render in (("cursor", CURSOR_STATES, lambda v: draw(cursor=v)),
                                 ("dots", DOT_STATES, lambda v: draw(dots=v))):
        images = [render(v) for v in states]
        box = patch_region(images)
        regions[name] = {"box": [box[0], box[1], box[2] - box[0], box[3] - box[1]],
                         "states": [base64.b64encode(zlib.compress(rgba(im.crop(box)), 9)).decode()
                                    for im in images]}
    # The two regions must not overlap: each patch is drawn on its own.
    (ax, ay, aw, ah), (bx, by, bw, bh) = regions["cursor"]["box"], regions["dots"]["box"]
    assert ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay, "patch regions overlap"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "base.rgba.z").write_bytes(zlib.compress(rgba(base), 9))
    anim = {"w": W, "h": H, "format": "RGBA", "tick_ms": 250,
            "cursor": {"period_ticks": 2, **regions["cursor"]},   # on 0.5 s, off 0.5 s
            "dots": {"period_ticks": 2, **regions["dots"]}}       # one more dot every 0.5 s
    (asset_dir / "anim.json").write_text(json.dumps(anim, indent=1) + "\n")
    base.save(png_out)
    return anim


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    a = build(Path(sys.argv[1]), Path(sys.argv[2]))
    if len(sys.argv) == 4:
        draw_charging().save(sys.argv[3])
    print(json.dumps({"cursor_box": a["cursor"]["box"], "dots_box": a["dots"]["box"]}))
