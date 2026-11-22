//! Build the I14 boot image with the authenticated serial recovery overlay.
use std::ffi::OsString;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::PathBuf;
use std::process::exit;

use i14_tools::{
    build, read_rescue_hash, repo_root, sha256_hex, CMDLINE_CHARGE_TEST, CMDLINE_EXTRA, STOCK,
    STOCK_SHA256,
};

fn usage() -> ! {
    eprintln!("usage: i14-build-boot OUTPUT --rescue-password-hash-file FILE [--charge-test]");
    exit(2)
}

fn parse(mut args: impl Iterator<Item = OsString>) -> (PathBuf, PathBuf, bool) {
    let mut output = None;
    let mut hash_file = None;
    let mut charge_test = false;
    while let Some(arg) = args.next() {
        match arg.to_str() {
            Some("--rescue-password-hash-file") if hash_file.is_none() => {
                hash_file = Some(PathBuf::from(args.next().unwrap_or_else(|| usage())));
            }
            Some("--charge-test") if !charge_test => charge_test = true,
            _ if output.is_none() && !arg.as_encoded_bytes().starts_with(b"-") => {
                output = Some(PathBuf::from(arg));
            }
            _ => usage(),
        }
    }
    (
        output.unwrap_or_else(|| usage()),
        hash_file.unwrap_or_else(|| usage()),
        charge_test,
    )
}

fn main() {
    let (output, hash_file, charge_test) = parse(std::env::args_os().skip(1));
    let rescue = read_rescue_hash(&hash_file).unwrap_or_else(|error| {
        eprintln!("{error}");
        exit(2)
    });
    let root = repo_root();
    let stock = std::fs::read(root.join(STOCK)).unwrap_or_else(|error| {
        eprintln!("{STOCK}: {error}");
        exit(1)
    });
    if sha256_hex(&stock) != STOCK_SHA256 {
        eprintln!("stock boot backup hash mismatch");
        exit(1);
    }
    let cmdline = if charge_test {
        CMDLINE_CHARGE_TEST
    } else {
        CMDLINE_EXTRA
    };
    let image = build(&root, &stock, &rescue, cmdline).unwrap_or_else(|error| {
        eprintln!("{error}");
        exit(1)
    });
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&output)
        .and_then(|mut file| file.write_all(&image))
        .unwrap_or_else(|error| {
            eprintln!("{}: {error}", output.display());
            exit(1)
        });
    println!(
        "{} {} bytes sha256={}",
        output.display(),
        image.len(),
        sha256_hex(&image)
    );
}
