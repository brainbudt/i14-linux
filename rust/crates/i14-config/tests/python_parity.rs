//! A PIN set by the Python `i14ui/config.py` unlocks in Rust and the other way
//! round, and both read the same normalized settings. Needs `python3`.

use std::path::PathBuf;
use std::process::Command;

use i14_config::Config;

fn python(code: &str, path: &PathBuf) -> String {
    let lib = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../rootfs/overlay/usr/lib");
    let out = Command::new("python3")
        .arg("-c")
        .arg(format!(
            "import sys\nfrom i14ui.config import Config\nc = Config(sys.argv[1])\n{code}"
        ))
        .arg(path)
        .env("PYTHONPATH", lib)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .expect("python3");
    assert!(
        out.status.success(),
        "python: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim().to_string()
}

#[test]
fn pin_and_settings_are_shared_with_python() {
    let dir = std::env::temp_dir().join(format!("i14-config-py-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let path = dir.join("ui.json");

    python("assert c.set('brightness', 42) and c.set('charge_limit_enabled', True) and c.set_pin('2468')", &path);
    let mut c = Config::load(&path);
    assert!(!c.damaged && c.check_pin("2468") && !c.check_pin("2469"));
    assert_eq!((c.data.brightness, c.data.charge_limit_enabled), (42, true));

    assert!(c.set_pin(Some("1357")) && c.update(|s| s.timeout = 300));
    let seen = python(
        "print(c.damaged, c.check_pin('1357'), c.check_pin('2468'), c['timeout'], c['brightness'], c['charge_limit_enabled'])",
        &path,
    );
    assert_eq!(seen, "False True False 300 42 True");
    let _ = std::fs::remove_dir_all(&dir);
}
