//! Draws the same scene with the Python Canvas (`i14ui/fb.py`, `font.py`) and with
//! this crate, and requires identical bytes.
//!
//! Needs `python3` and the Terminus fonts; set I14_FONTS to the directory holding
//! ter-118n/120n/124b/132b `.psf.gz` (on the phone: /usr/share/consolefonts).
//! Skipped with a message when I14_FONTS is unset.

use std::path::PathBuf;
use std::process::Command;

use i14_ui::canvas::Canvas;
use i14_ui::font::Font;

const FONTS: [&str; 4] = ["ter-118n", "ter-120n", "ter-124b", "ter-132b"];
const TEXTS: [&str; 4] = [
    "Wi-Fi 80% ok",
    "héllo wörld €~",
    "\u{2588}\u{2591} ? \u{263a}",
    "clip me at the edge of the screen please",
];

fn python_lib() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../rootfs/overlay/usr/lib")
}

/// The scene, as Python source and as Rust calls; kept in step by hand.
const PY: &str = r#"
import sys
from i14ui.fb import Canvas
from i14ui.font import Font
fonts, texts = sys.argv[1].split(","), sys.argv[2].split("\x1f")
c = Canvas(300, 120, 310)
c.clear((10, 20, 30))
c.fill(-5, 100, 50, 50, (200, 0, 0))
c.fill(250, -3, 100, 10, (0, 200, 0))
y = -4
for i, path in enumerate(fonts):
    f = Font(path)
    for scale in (1, 2):
        c.text(3 + i * 7, y, texts[i], f, (255, 255, 255), (i * 40, 0, 99), scale, None if scale == 1 else 250)
        y += f.h * scale // 2
sys.stdout.buffer.write(c.buf)
"#;

#[test]
fn canvas_matches_python_byte_for_byte() {
    let Some(dir) = std::env::var_os("I14_FONTS") else {
        eprintln!("skipped: set I14_FONTS to the Terminus font directory");
        return;
    };
    let paths: Vec<String> = FONTS
        .iter()
        .map(|n| {
            PathBuf::from(&dir)
                .join(format!("{n}.psf.gz"))
                .display()
                .to_string()
        })
        .collect();
    let out = Command::new("python3")
        .arg("-c")
        .arg(PY)
        .arg(paths.join(","))
        .arg(TEXTS.join("\x1f"))
        .env("PYTHONPATH", python_lib())
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .expect("python3");
    assert!(
        out.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let mut c = Canvas::new(300, 120, Some(310));
    c.clear([10, 20, 30]);
    c.fill(-5, 100, 50, 50, [200, 0, 0]);
    c.fill(250, -3, 100, 10, [0, 200, 0]);
    let mut y = -4;
    for (i, path) in paths.iter().enumerate() {
        let f = Font::open(path).unwrap();
        for scale in [1usize, 2] {
            let maxw = if scale == 1 { None } else { Some(250) };
            c.text(
                3 + i as i32 * 7,
                y,
                TEXTS[i],
                &f,
                [255, 255, 255],
                [i as u8 * 40, 0, 99],
                scale,
                maxw,
            );
            y += (f.h * scale / 2) as i32;
        }
    }
    assert_eq!(out.stdout.len(), c.buf.len());
    let first = out.stdout.iter().zip(&c.buf).position(|(a, b)| a != b);
    assert_eq!(first, None, "first differing byte");
}
