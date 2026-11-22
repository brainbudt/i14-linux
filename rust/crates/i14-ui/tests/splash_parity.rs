//! The splash's kernel-log lines and their rendering against the Python i14-splash.
//! Needs `python3` and I14_FONTS (ter-118n.psf.gz).

use std::path::PathBuf;
use std::process::Command;

use i14_ui::font::{gunzip, Font};
use i14_ui::splash::{format_record, load, log_lines, render_log, LOG_TOP};

const RECORDS: &[&[u8]] = &[
    b"6,1,5,-;(0)[1:swapper/0]Booting Linux on physical CPU 0x0\n",
    b"4,2,1234567,-; -(2)[45:kworker/2:1][name:spm&][SPM] wake up by \\x0a timer\\x0d\n DEVICE=+x\n",
    b"3,3,99999999999,c;\xff\xfe broken utf8 and a very long line that will certainly be cut at the right edge of the screen ......\n",
    b"6,4,abc,-;not a timestamp\n",
    b"6,5,7,-;(0)[93:bat_thread]\n",
    b"6,6,8,-;  plain text, no prefix  \n",
    b"6,7,9,-;(x)[1:a]not a mediatek prefix\n",
];

#[test]
fn log_matches_python() {
    let Some(fonts) = std::env::var_os("I14_FONTS") else {
        eprintln!("skipped: set I14_FONTS");
        return;
    };
    let font_path = PathBuf::from(&fonts).join("ter-118n.psf.gz");
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../..");
    let py = r#"
import importlib.machinery, importlib.util, sys, json
loader = importlib.machinery.SourceFileLoader("s", sys.argv[1])
spec = importlib.util.spec_from_loader("s", loader); s = importlib.util.module_from_spec(spec); loader.exec_module(s)
recs = [bytes.fromhex(h) for h in json.loads(sys.argv[3])]
lines = [s.format_record(r) for r in recs]
anim, base = s.load(sys.argv[4])
font = s.Font(sys.argv[2])
n = s.log_lines(font, anim["h"])
out = s.render_log(base, anim["w"], s.LOG_TOP, [l for l in lines if l], font, n)
sys.stdout.buffer.write(json.dumps(lines).encode() + b"\n" + out)
"#;
    let hex: Vec<String> = RECORDS
        .iter()
        .map(|r| r.iter().map(|b| format!("{b:02x}")).collect())
        .collect();
    let assets = root.join("rootfs/overlay/usr/share/i14-splash");
    let out = Command::new("python3")
        .args(["-c", py])
        .arg(root.join("rootfs/overlay/usr/sbin/i14-splash"))
        .arg(&font_path)
        .arg(serde_json::to_string(&hex).unwrap())
        .arg(&assets)
        .output()
        .unwrap();
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let nl = out.stdout.iter().position(|&b| b == b'\n').unwrap();
    let want_lines: Vec<String> = serde_json::from_slice(&out.stdout[..nl]).unwrap();
    let got_lines: Vec<String> = RECORDS.iter().map(|r| format_record(r)).collect();
    assert_eq!(got_lines, want_lines);

    let (anim, base) = load(assets.to_str().unwrap()).unwrap();
    let font = Font::parse_any(&gunzip(&font_path).unwrap(), 128).unwrap();
    let n = log_lines(&font, anim.h);
    let shown: Vec<String> = got_lines.into_iter().filter(|l| !l.is_empty()).collect();
    let got = render_log(&base, anim.w, LOG_TOP, &shown, &font, n);
    assert!(got == out.stdout[nl + 1..], "rendered log differs");
}
