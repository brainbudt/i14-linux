//! i14-verify-backup: offline, read-only integrity audit of an I14 phase-0 +
//! userdata firmware backup (manifest SHA-256, both GPT copies, completeness).
//! Host proof only: no phone/server contact, no writes, no restore acceptance.
//! Exit 0 = complete successful audit, 1 = integrity/validation failure,
//! 2 = usage error. See rust/README.md for example use.

use std::path::PathBuf;
use std::process::exit;

use i14_tools::verify_backup::{self, Inputs, CAPTURE_LEN, SECTOR};

const USAGE: &str =
    "usage: i14-verify-backup --phase0-manifest SHA256SUMS --userdata-manifest SHA256SUMS
                        --primary primary-userarea.bin --tail tail-userarea.bin
                        --tail-offset BYTES --disk-size BYTES
  Manifest entries are relative to their own manifest's directory.
  Sizes accept decimal or 0x hex. Sectors are 512 bytes; both captures must
  be exactly 0x100000 bytes and the tail capture must end at the disk end
  (--tail-offset + 0x100000 == --disk-size). Historical tail offset: 0x3a3d00000.
  Read-only: reports go to stdout; nothing is written, no device is contacted.";

fn usage_error(msg: &str) -> ! {
    eprintln!("{msg}\n{USAGE}");
    exit(2)
}

fn parse_num(s: &str) -> Option<u64> {
    match s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        Some(hex) => u64::from_str_radix(hex, 16).ok(),
        None => s.parse::<u64>().ok(),
    }
}

fn parse(args: &[String]) -> Inputs {
    let mut paths: [Option<PathBuf>; 4] = [None, None, None, None];
    let mut nums: [Option<u64>; 2] = [None, None];
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let slot: usize = match arg.as_str() {
            "--phase0-manifest" => 0,
            "--userdata-manifest" => 1,
            "--primary" => 2,
            "--tail" => 3,
            "--tail-offset" => 4,
            "--disk-size" => 5,
            "--help" | "-h" => {
                println!("{USAGE}");
                exit(0)
            }
            other => usage_error(&format!("unknown argument '{other}'")),
        };
        let Some(value) = it.next() else {
            usage_error(&format!("{arg} needs a value"));
        };
        if value.is_empty() {
            usage_error(&format!("{arg} needs a non-empty value"));
        }
        if slot < 4 {
            if paths[slot].is_some() {
                usage_error(&format!("{arg} given twice"));
            }
            paths[slot] = Some(PathBuf::from(value));
        } else {
            if nums[slot - 4].is_some() {
                usage_error(&format!("{arg} given twice"));
            }
            nums[slot - 4] = Some(
                parse_num(value)
                    .unwrap_or_else(|| usage_error(&format!("{arg}: '{value}' is not a number"))),
            );
        }
    }
    let [Some(phase0_manifest), Some(userdata_manifest), Some(primary), Some(tail)] = paths else {
        usage_error("missing required arguments");
    };
    let [Some(tail_offset), Some(disk_size)] = nums else {
        usage_error("missing required arguments");
    };
    if disk_size % SECTOR != 0 || disk_size < 68 * SECTOR {
        usage_error("--disk-size must be a positive multiple of 512 (at least 68 sectors)");
    }
    if tail_offset == 0 || tail_offset % SECTOR != 0 {
        usage_error("--tail-offset must be a positive multiple of 512");
    }
    if tail_offset.checked_add(CAPTURE_LEN) != Some(disk_size) {
        usage_error(
            "--tail-offset + 0x100000 must equal --disk-size (tail capture ends at disk end)",
        );
    }
    Inputs {
        phase0_manifest,
        userdata_manifest,
        primary,
        tail,
        tail_offset,
        disk_size,
    }
}

fn main() {
    let args: Vec<String> = std::env::args_os()
        .skip(1)
        .map(|arg| {
            arg.into_string()
                .unwrap_or_else(|_| usage_error("arguments must be valid UTF-8"))
        })
        .collect();
    let inputs = parse(&args);
    let audit = verify_backup::audit(&inputs);
    for line in &audit.lines {
        println!("{line}");
    }
    exit(if audit.failures == 0 { 0 } else { 1 })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numbers_decimal_and_hex() {
        assert_eq!(parse_num("0x3a3d00000"), Some(0x3a3d00000));
        assert_eq!(parse_num("15634268160"), Some(15634268160));
        assert_eq!(parse_num("0X200"), Some(512));
        for bad in ["", "0x", "12x", "-1", "1.5", " 1", "1 ", "0xzz"] {
            assert_eq!(parse_num(bad), None, "{bad:?}");
        }
        assert_eq!(
            parse_num("+1"),
            Some(1),
            "std decimal parsing accepts a plus sign"
        );
        assert_eq!(parse_num("18446744073709551616"), None, "u64 overflow");
    }
}
