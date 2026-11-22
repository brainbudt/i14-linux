//! Widgets against `i14ui/widgets.py`: the on-screen keyboard layout always,
//! and drawn pixels when I14_FONTS points at the Terminus fonts. Needs `python3`.

use std::path::PathBuf;
use std::process::Command;
use std::time::Instant;

use i14_ui::canvas::Canvas;
use i14_ui::font::Font;
use i14_ui::input::Gesture;
use i14_ui::widgets::*;
use serde_json::{json, Value};

fn python(code: &str, args: &[String]) -> Vec<u8> {
    let lib = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../rootfs/overlay/usr/lib");
    let out = Command::new("python3")
        .arg("-c")
        .arg(code)
        .args(args)
        .env("PYTHONPATH", lib)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .expect("python3");
    assert!(
        out.status.success(),
        "python: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    out.stdout
}

#[test]
fn keyboard_layouts_match_python() {
    let py = python(
        r#"
import json
from i14ui import widgets as w
out = []
for width, term in ((720, True), (720, False), (333, True)):
    k = w.Keyboard(width, 1600, lambda key: None, terminal_row=term)
    for layout in ("letters", "sym", "more"):
        k.layout = layout
        k._build()
        out.append([[list(r), l, o, list(h)] for r, l, o, h in k.keys])
print(json.dumps(out))
"#,
        &[],
    );
    let want: Value = serde_json::from_slice(&py).unwrap();
    let mut got = Vec::new();
    for (width, term) in [(720, true), (720, false), (333, true)] {
        let mut k = Keyboard::new(width, 1600, term);
        for out in ["abc", "sym", "more"] {
            // press the layout keys through the public API
            let target = match out {
                "abc" => Layout::Letters,
                "sym" => Layout::Sym,
                _ => Layout::More,
            };
            k.layout = target;
            k.set_terminal_row(term);
            got.push(
                k.keys
                    .iter()
                    .map(|r| {
                        json!([
                            [r.rect.x, r.rect.y, r.rect.w, r.rect.h],
                            r.label,
                            r.out,
                            [r.hit.x, r.hit.y, r.hit.w, r.hit.h]
                        ])
                    })
                    .collect::<Vec<_>>(),
            );
        }
    }
    assert_eq!(json!(got), want);
}

#[test]
fn widgets_draw_like_python() {
    let Some(dir) = std::env::var_os("I14_FONTS") else {
        eprintln!("skipped: set I14_FONTS to the Terminus font directory");
        return;
    };
    let mid = PathBuf::from(&dir)
        .join("ter-124b.psf.gz")
        .display()
        .to_string();
    let small = PathBuf::from(&dir)
        .join("ter-120n.psf.gz")
        .display()
        .to_string();
    let py = python(
        r#"
import sys
from i14ui import widgets as w, fb
from i14ui.font import Font
mid, small = Font(sys.argv[1]), Font(sys.argv[2])
c = fb.Canvas(720, 1600, 736)
c.clear(w.BG)
b = w.Button((20, 20, 300, 90), "Een heel lange knoptekst die niet past", lambda: None, scale=2)
b.draw(c, mid)
b2 = w.Button((340, 20, 200, 90), "OK", lambda: None, color=w.PANEL_HI, fg=w.RED)
b2.pressed = True
b2.draw(c, mid)
lst = w.ListView((0, 130, 720, 430), lambda i: None)
lst.set_items([(f"Netwerk {i}", f"WPA2, signaal {'|' * (i % 5)}" if i % 3 else "", w.GREEN if i == 2 else w.FG) for i in range(9)])
lst.offset = 137
lst.pressed = 3
lst.draw(c, mid, small)
empty = w.ListView((0, 570, 360, 120), lambda i: None)
empty.draw(c, mid, small)
f = w.TextField((40, 700, 640, 120), "Wachtwoord", secret=True)
f.value = "geheim-wachtwoord-dat-veel-te-lang-is-voor-het-veld"
f.draw(c, mid, small)
k = w.Keyboard(720, 1600, lambda key: None, terminal_row=True)
k.shift = 2
k.ctrl = True
k.active = k.keys[14]
k.draw(c, mid)
sys.stdout.buffer.write(c.buf)
"#,
        &[mid.clone(), small.clone()],
    );
    let (mid, small) = (Font::open(&mid).unwrap(), Font::open(&small).unwrap());
    let mut c = Canvas::new(720, 1600, Some(736));
    c.clear(BG);
    Button::new(
        rect(20, 20, 300, 90),
        "Een heel lange knoptekst die niet past",
    )
    .scale(2)
    .draw(&mut c, &mid);
    let mut b2 = Button::new(rect(340, 20, 200, 90), "OK")
        .color(PANEL_HI)
        .fg(RED);
    b2.pressed = true;
    b2.draw(&mut c, &mid);
    let mut lst = ListView::new(rect(0, 130, 720, 430));
    lst.set_items(
        (0..9)
            .map(|i| Item {
                text: format!("Netwerk {i}"),
                sub: if i % 3 != 0 {
                    format!("WPA2, signaal {}", "|".repeat(i % 5))
                } else {
                    String::new()
                },
                color: if i == 2 { GREEN } else { FG },
            })
            .collect(),
    );
    lst.offset = 137;
    lst.pressed = Some(3);
    lst.draw(&mut c, &mid, &small);
    ListView::new(rect(0, 570, 360, 120)).draw(&mut c, &mid, &small);
    let mut f = TextField::new(rect(40, 700, 640, 120), "Wachtwoord", true, 63);
    f.value = "geheim-wachtwoord-dat-veel-te-lang-is-voor-het-veld".into();
    f.draw(&mut c, &mid, &small);
    let mut k = Keyboard::new(720, 1600, true);
    k.shift = 2;
    k.ctrl = true;
    k.active = Some(k.keys[14]);
    k.draw(&mut c, &mid);
    let _ = (Gesture::Down(0, 0), Instant::now());
    assert_eq!(py.len(), c.buf.len());
    let first = py.iter().zip(&c.buf).position(|(a, b)| a != b);
    assert_eq!(
        first,
        None,
        "first differing byte (row {:?})",
        first.map(|i| i / c.stride)
    );
}
