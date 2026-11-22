//! charge_wait_rs (i14charge-rs.sh), the proposed eval-free caller in /i14init: only
//! exit 0/1/2 with complete output may count; everything else is rescue (1).

use std::path::PathBuf;
use std::process::Command;

/// Runs charge_wait_rs with a stand-in i14-charge-wait made from `body` (sh script),
/// or with none at all; returns (rc, why, cap, vol).
fn run(body: Option<&str>, shell: &str) -> (i32, String, String, String) {
    run_pre("", body, shell)
}

/// As [`run`], with shell code `pre` run after sourcing initramfs/i14charge.
fn run_pre(pre: &str, body: Option<&str>, shell: &str) -> (i32, String, String, String) {
    let dir = std::env::temp_dir().join(format!(
        "i14-wrap-{}-{:?}",
        std::process::id(),
        std::thread::current().id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    if let Some(b) = body {
        let p = dir.join("i14-charge-wait");
        std::fs::write(&p, format!("#!/bin/sh\n{b}\n")).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    let func = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../initramfs/i14charge");
    let script = format!(
        ". '{}'; {pre} why=old; charge_wait_rs; rc=$?; printf '%s\\n%s\\n%s\\n%s\\n' \"$rc\" \"$why\" \"$cap\" \"$vol\"",
        func.display()
    );
    let out = Command::new(shell)
        .args(["-c", &script])
        .env("PATH", format!("{}:/usr/bin:/bin", dir.display()))
        .output()
        .unwrap();
    let _ = std::fs::remove_dir_all(&dir);
    let text = String::from_utf8(out.stdout).unwrap();
    let l: Vec<&str> = text.split('\n').collect();
    (l[0].parse().unwrap(), l[1].into(), l[2].into(), l[3].into())
}

fn shells() -> Vec<&'static str> {
    ["/bin/sh", "/bin/dash", "/bin/ash"]
        .into_iter()
        .filter(|s| std::path::Path::new(s).exists())
        .collect()
}

#[test]
fn complete_output_passes_through() {
    for sh in shells() {
        let r = run(
            Some("printf 'why=charged to 80%%\\ncap=80\\nvol=3900\\nend\\n'; exit 0"),
            sh,
        );
        assert_eq!(
            r,
            (0, "charged to 80%".into(), "80".into(), "3900".into()),
            "{sh}"
        );
        assert_eq!(
            run(
                Some("printf 'why=no charger\\ncap=5\\nvol=3500\\nend\\n'; exit 2"),
                sh
            )
            .0,
            2,
            "{sh}"
        );
    }
}

#[test]
fn anything_else_is_rescue() {
    let cases = [
        None,                                                    // binary missing
        Some("printf \"why='half\"; kill -TERM $$"),             // interrupted mid-output
        Some("printf 'why=x\\ncap=1\\nvol=2\\nend\\n'; exit 7"), // odd exit code
        Some("printf 'why=x\\ncap=1\\nvol=2\\n'; exit 0"),       // no end line
        Some("printf 'why=x\\ncap=1\\nend\\n'; exit 0"),         // a line missing
        Some("exit 0"),                                          // no output
        // malformed frames: `*` in a case pattern also matches newlines
        Some("printf 'why=charged\\nEXTRA\\ncap=80\\nvol=3900\\nend\\n'; exit 0"),
        Some("printf 'why=a\\ncap=1\\ncap=2\\nvol=3\\nend\\n'; exit 2"),
        Some("printf 'why=a\\ncap=1\\nvol=2\\nend\\nwhy=b\\ncap=1\\nvol=2\\nend\\n'; exit 0"),
        Some("printf 'why=a\\nvol=2\\ncap=1\\nend\\n'; exit 0"),
        Some("printf 'cap=1\\nwhy=a\\ncap=1\\nvol=2\\nend\\n'; exit 0"),
        Some("printf 'why=a\\ncap=1\\nvol=2\\nend\\nextra\\n'; exit 0"),
        // a CR, which i14-charge-wait never prints
        Some("printf 'why=char\\rged\\ncap=80\\nvol=3900\\nend\\n'; exit 0"),
        Some("printf 'why=a\\ncap=8\\r\\nvol=3\\nend\\n'; exit 2"),
    ];
    for sh in shells() {
        for c in cases {
            let (rc, why, _, _) = run(c, sh);
            assert_eq!(rc, 1, "{sh} {c:?}");
            assert!(
                why.starts_with("i14-charge-wait failed"),
                "{sh} {c:?}: {why}"
            );
        }
    }
}

#[test]
fn values_are_never_evaluated() {
    for sh in shells() {
        let body = "printf 'why=$(touch /tmp/i14-wrap-pwned) `id` '\\''q\\ncap=1;rc=0\\nvol=x\\nend\\n'; exit 1";
        let (rc, why, cap, _) = run(Some(body), sh);
        assert_eq!(rc, 1, "{sh}");
        assert_eq!(why, "$(touch /tmp/i14-wrap-pwned) `id` 'q", "{sh}");
        assert_eq!(cap, "1;rc=0", "{sh}");
        assert!(!std::path::Path::new("/tmp/i14-wrap-pwned").exists());
    }
}

#[test]
fn the_time_limit_is_passed_and_its_rescue_frame_kept() {
    let echo = "printf 'why=%s\\ncap=1\\nvol=2\\nend\\n' \"$AB_MAX_S\"; exit 1";
    for sh in shells() {
        // the default of initramfs/i14charge, 8 h, unless /i14init overrides it like AB_MIN_S
        assert_eq!(run(Some(echo), sh).1, "28800", "{sh}");
        assert_eq!(run_pre("AB_MAX_S=600;", Some(echo), sh).1, "600", "{sh}");
        let r = run(
            Some("printf 'why=charge time limit\\ncap=14\\nvol=3700\\nend\\n'; exit 1"),
            sh,
        );
        assert_eq!(
            r,
            (1, "charge time limit".into(), "14".into(), "3700".into()),
            "{sh}"
        );
    }
}
