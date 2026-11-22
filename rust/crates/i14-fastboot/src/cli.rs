//! CLI front ends with argparse-compatible parsing, messages and exit codes.

use std::collections::HashMap;
use std::fs::File;
use std::io::{Read, Write};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::image::{MAX_IMAGE, validate_image};
use crate::install::{Receipt, install_boot, validate_install_cmdline, validate_stock};
use crate::protocol::{Fastboot, PRODUCT, QUERIES, Transport};
use crate::{Error, Result};

struct Opt {
    names: &'static [&'static str],
    value: bool,
    choices: Option<&'static [&'static str]>,
}

impl Opt {
    fn key(&self) -> &'static str {
        self.names[self.names.len() - 1]
    }

    fn display(&self) -> String {
        self.names.join("/")
    }
}

struct Spec {
    prog: &'static str,
    usage: &'static str,
    help: &'static str,
    options: &'static [Opt],
    required: &'static [&'static str],
    exclusive_required: &'static [&'static str],
}

const HELP: Opt = Opt {
    names: &["-h", "--help"],
    value: false,
    choices: None,
};

const FASTBOOT_USAGE: &str = "usage: i14-fastboot [-h]
                    (--query {product,unlocked,secure,max-download-size,partition-size:boot,partition-size:recovery} |
                    --reboot | --boot IMAGE) [--sha256 HEX]
";

const FASTBOOT_HELP: &str = "
Bounded Fastboot fallback for this I14; no flash, erase or unlock commands.
Run with the project's PyUSB environment (imports never open USB):
.venv/bin/python scripts/fastboot_usb.py --query product .venv/bin/python
scripts/fastboot_usb.py --reboot .venv/bin/python scripts/fastboot_usb.py
--boot IMAGE --sha256 HEX Boot uploads only to RAM and requires an unlocked
bootloader. A successful Fastboot acknowledgement does not prove Linux booted.
Ctrl-C stops the host; after a transfer error, inspect device state before
starting another action. The measured product and USB descriptors are guards,
not a unique serial.

options:
  -h, --help            show this help message and exit
  --query {product,unlocked,secure,max-download-size,partition-size:boot,partition-size:recovery}
  --reboot
  --boot IMAGE
  --sha256 HEX
";

const FASTBOOT_SPEC: Spec = Spec {
    prog: "i14-fastboot",
    usage: FASTBOOT_USAGE,
    help: FASTBOOT_HELP,
    options: &[
        HELP,
        Opt {
            names: &["--query"],
            value: true,
            choices: Some(&QUERIES),
        },
        Opt {
            names: &["--reboot"],
            value: false,
            choices: None,
        },
        Opt {
            names: &["--boot"],
            value: true,
            choices: None,
        },
        Opt {
            names: &["--sha256"],
            value: true,
            choices: None,
        },
    ],
    required: &[],
    exclusive_required: &["--query", "--reboot", "--boot"],
};

const INSTALL_USAGE: &str = "usage: i14-install-boot [-h] --image IMAGE --sha256 SHA256
                        --stock-backup STOCK_BACKUP --attempt ATTEMPT
";

const INSTALL_HELP: &str = "
Fixed boot-only install; see docs/BOOT-INSTALL-2026-10-30.md before use. No
automatic reboot, restore, erase, retry or other partition writes. Run only
after the exact candidate's native RAM boot has been independently verified.

options:
  -h, --help            show this help message and exit
  --image IMAGE
  --sha256 SHA256
  --stock-backup STOCK_BACKUP
  --attempt ATTEMPT     new exclusive JSONL receipt; existing paths are never
                        reused
";

const INSTALL_SPEC: Spec = Spec {
    prog: "i14-install-boot",
    usage: INSTALL_USAGE,
    help: INSTALL_HELP,
    options: &[
        HELP,
        Opt {
            names: &["--image"],
            value: true,
            choices: None,
        },
        Opt {
            names: &["--sha256"],
            value: true,
            choices: None,
        },
        Opt {
            names: &["--stock-backup"],
            value: true,
            choices: None,
        },
        Opt {
            names: &["--attempt"],
            value: true,
            choices: None,
        },
    ],
    required: &["--image", "--sha256", "--stock-backup", "--attempt"],
    exclusive_required: &[],
};

/// Early exit from parsing: exit code plus text for stdout/stderr.
#[derive(Debug, PartialEq, Eq)]
pub struct Exit {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

type Parsed = HashMap<&'static str, Option<String>>;

enum Classified<'a> {
    Positional,
    /// Option, explicit argument, and whether it came from a combined short
    /// flag such as `-hx` (no `=`).
    Known(&'a Opt, Option<String>, bool),
    Unknown,
}

/// Python `repr(str)` for the printable ASCII strings argparse quotes.
fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::from(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

fn is_negative_number(arg: &str) -> bool {
    // argparse: '^-\d+$|^-\d*\.\d+$'
    let Some(rest) = arg.strip_prefix('-') else {
        return false;
    };
    let all_digits = |s: &str| !s.is_empty() && s.chars().all(|c| c.is_ascii_digit());
    if all_digits(rest) {
        return true;
    }
    match rest.split_once('.') {
        Some((int, frac)) => (int.is_empty() || all_digits(int)) && all_digits(frac),
        None => false,
    }
}

impl Spec {
    fn error(&self, message: &str) -> Exit {
        Exit {
            code: 2,
            stdout: String::new(),
            stderr: format!("{}{}: error: {message}\n", self.usage, self.prog),
        }
    }

    fn find(&self, name: &str) -> Option<&Opt> {
        self.options.iter().find(|opt| opt.names.contains(&name))
    }

    /// argparse `_parse_optional`.
    fn classify(&self, arg: &str) -> Result<Classified<'_>, Exit> {
        if !arg.starts_with('-') {
            return Ok(Classified::Positional);
        }
        if let Some(opt) = self.find(arg) {
            return Ok(Classified::Known(opt, None, false));
        }
        if arg.len() == 1 {
            return Ok(Classified::Positional);
        }
        if let Some((name, explicit)) = arg.split_once('=')
            && let Some(opt) = self.find(name)
        {
            return Ok(Classified::Known(opt, Some(explicit.to_owned()), false));
        }
        // argparse `_get_option_tuples` (abbreviations).
        let mut matches: Vec<(&Opt, &str, Option<String>, bool)> = Vec::new();
        let prefix;
        if arg.starts_with("--") {
            let (name, explicit) = match arg.split_once('=') {
                Some((name, explicit)) => (name, Some(explicit.to_owned())),
                None => (arg, None),
            };
            prefix = name;
            for opt in self.options {
                for option_string in opt.names {
                    if option_string.starts_with(name) {
                        matches.push((opt, option_string, explicit.clone(), false));
                    }
                }
            }
        } else {
            prefix = arg;
            let short = &arg[..2.min(arg.len())];
            for opt in self.options {
                for option_string in opt.names {
                    if *option_string == short {
                        matches.push((opt, option_string, Some(arg[2..].to_owned()), true));
                    } else if option_string.starts_with(arg) {
                        matches.push((opt, option_string, None, false));
                    }
                }
            }
        }
        if matches.len() > 1 {
            let names: Vec<&str> = matches.iter().map(|m| m.1).collect();
            return Err(self.error(&format!(
                "ambiguous option: {prefix} could match {}",
                names.join(", ")
            )));
        }
        if let Some((opt, _, explicit, combined)) = matches.pop() {
            return Ok(Classified::Known(opt, explicit, combined));
        }
        if is_negative_number(arg) || arg.contains(' ') {
            return Ok(Classified::Positional);
        }
        Ok(Classified::Unknown)
    }

    fn parse(&self, argv: &[String]) -> Result<Parsed, Exit> {
        let mut parsed = Parsed::new();
        let mut extras: Vec<String> = Vec::new();
        let mut exclusive_seen: Option<&Opt> = None;
        let mut index = 0;
        let mut only_positional = false;
        while index < argv.len() {
            let arg = &argv[index];
            index += 1;
            if only_positional {
                extras.push(arg.clone());
                continue;
            }
            if arg == "--" {
                // argparse 3.13 with no positionals reports `--` itself too.
                only_positional = true;
                extras.push(arg.clone());
                continue;
            }
            let (opt, explicit) = match self.classify(arg)? {
                Classified::Positional | Classified::Unknown => {
                    extras.push(arg.clone());
                    continue;
                }
                Classified::Known(opt, explicit, combined) => {
                    // `-hx`: argparse queues -h, then treats `-x` as the next
                    // flag; help runs (and exits) before `-x` matters.
                    if combined
                        && opt.key() == "--help"
                        && !explicit.as_deref().unwrap_or("-").starts_with('-')
                    {
                        return Err(Exit {
                            code: 0,
                            stdout: format!("{}{}", self.usage, self.help),
                            stderr: String::new(),
                        });
                    }
                    (opt, explicit)
                }
            };
            let value = if opt.value {
                match explicit {
                    Some(value) => Some(value),
                    None => {
                        let next = argv.get(index).filter(|next| {
                            next.as_str() != "--"
                                && matches!(self.classify(next), Ok(Classified::Positional))
                        });
                        let Some(next) = next else {
                            return Err(self.error(&format!(
                                "argument {}: expected one argument",
                                opt.display()
                            )));
                        };
                        index += 1;
                        Some(next.clone())
                    }
                }
            } else {
                if let Some(explicit) = explicit {
                    return Err(self.error(&format!(
                        "argument {}: ignored explicit argument {}",
                        opt.display(),
                        py_repr(&explicit)
                    )));
                }
                None
            };
            if opt.key() == "--help" {
                return Err(Exit {
                    code: 0,
                    stdout: format!("{}{}", self.usage, self.help),
                    stderr: String::new(),
                });
            }
            if let (Some(choices), Some(value)) = (opt.choices, value.as_deref())
                && !choices.contains(&value)
            {
                let quoted: Vec<String> = choices.iter().map(|c| py_repr(c)).collect();
                return Err(self.error(&format!(
                    "argument {}: invalid choice: {} (choose from {})",
                    opt.display(),
                    py_repr(value),
                    quoted.join(", ")
                )));
            }
            if self.exclusive_required.contains(&opt.key()) {
                if let Some(other) = exclusive_seen
                    && other.key() != opt.key()
                {
                    return Err(self.error(&format!(
                        "argument {}: not allowed with argument {}",
                        opt.display(),
                        other.display()
                    )));
                }
                exclusive_seen = Some(opt);
            }
            parsed.insert(opt.key(), value);
        }
        let missing: Vec<&str> = self
            .required
            .iter()
            .copied()
            .filter(|name| !parsed.contains_key(name))
            .collect();
        if !missing.is_empty() {
            return Err(self.error(&format!(
                "the following arguments are required: {}",
                missing.join(", ")
            )));
        }
        if !self.exclusive_required.is_empty() && exclusive_seen.is_none() {
            return Err(self.error(&format!(
                "one of the arguments {} is required",
                self.exclusive_required.join(" ")
            )));
        }
        if !extras.is_empty() {
            return Err(self.error(&format!("unrecognized arguments: {}", extras.join(" "))));
        }
        Ok(parsed)
    }
}

/// Process arguments as UTF-8. Python would accept undecodable bytes via
/// `surrogateescape`; this port refuses them with a usage error instead.
pub fn utf8_args(prog: &str) -> Option<Vec<String>> {
    let args: Option<Vec<String>> = std::env::args_os()
        .skip(1)
        .map(|arg| arg.into_string().ok())
        .collect();
    if args.is_none() {
        eprintln!("{prog}: error: arguments must be valid UTF-8");
    }
    args
}

/// `source.read(MAX_IMAGE + 1)`: never read more than one byte past the limit.
fn read_limited(path: &Path) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    File::open(path)?
        .take(MAX_IMAGE as u64 + 1)
        .read_to_end(&mut data)?;
    Ok(data)
}

fn emit(exit: Exit, out: &mut dyn Write, err: &mut dyn Write) -> i32 {
    let _ = out.write_all(exit.stdout.as_bytes());
    let _ = err.write_all(exit.stderr.as_bytes());
    exit.code
}

/// Python `fastboot_usb.main`. Returns the exit code; 130 stands for an
/// uncaught `KeyboardInterrupt`.
pub fn fastboot_main<T, C>(
    argv: &[String],
    connect: C,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32
where
    T: Transport,
    C: FnOnce() -> Result<Fastboot<T>>,
{
    let parsed = match FASTBOOT_SPEC.parse(argv) {
        Ok(parsed) => parsed,
        Err(exit) => return emit(exit, out, err),
    };
    let get = |key: &str| parsed.get(key).cloned().flatten();
    let boot = get("--boot");
    let sha256 = get("--sha256");
    // Python: bool(args.boot) != bool(args.sha256); a Path is always truthy.
    if boot.is_some() != sha256.as_deref().is_some_and(|s| !s.is_empty()) {
        return emit(
            FASTBOOT_SPEC.error("--boot and --sha256 must be used together"),
            out,
            err,
        );
    }
    let query = get("--query");
    let mut client: Option<Fastboot<T>> = None;
    let outcome = (|| -> Result<String> {
        let mut data = Vec::new();
        if let Some(boot) = &boot {
            data = read_limited(Path::new(boot))?;
            validate_image(&data, sha256.as_deref())?;
        }
        let client = client.insert(connect()?);
        if let Some(query) = &query {
            Ok(format!("{query}={}", client.query(query)?))
        } else if parsed.contains_key("--reboot") {
            client.reboot()?;
            Ok(format!("{PRODUCT}: reboot acknowledged"))
        } else {
            client.boot(&data, sha256.as_deref())?;
            Ok(format!(
                "{PRODUCT}: RAM boot command acknowledged; verify actual boot separately"
            ))
        }
    })();
    let mut interrupted = false;
    let mut result = match outcome {
        Ok(line) => {
            let _ = writeln!(out, "{line}");
            0
        }
        Err(Error::Fastboot(message)) => {
            let _ = writeln!(err, "stopped: {message}");
            1
        }
        Err(Error::Io(_) | Error::Exists) => {
            let _ = writeln!(err, "stopped: I/O failed; no retry");
            1
        }
        Err(Error::Other(_)) => {
            let _ = writeln!(err, "stopped: USB setup failed; no retry");
            1
        }
        Err(Error::Interrupted) => {
            interrupted = true;
            1
        }
    };
    if let Some(mut client) = client
        && client.close().is_err()
    {
        let _ = writeln!(err, "stopped: USB cleanup failed");
        result = 1;
    }
    if interrupted {
        let _ = writeln!(err, "KeyboardInterrupt");
        return 130;
    }
    result
}

/// Python `install_boot_usb.main`. `interrupt` is the Ctrl-C flag (the
/// binary sets it from SIGINT); it is honoured between steps and at
/// every USB call, like a Python `KeyboardInterrupt`.
pub fn install_main<T, C>(
    argv: &[String],
    connect: C,
    interrupt: Arc<AtomicBool>,
    out: &mut dyn Write,
    err: &mut dyn Write,
) -> i32
where
    T: Transport,
    C: FnOnce() -> Result<Fastboot<T>>,
{
    let parsed = match INSTALL_SPEC.parse(argv) {
        Ok(parsed) => parsed,
        Err(exit) => return emit(exit, out, err),
    };
    let get = |key: &str| parsed.get(key).cloned().flatten().unwrap_or_default();
    let (image, sha256) = (get("--image"), get("--sha256"));
    let (stock, attempt) = (get("--stock-backup"), get("--attempt"));
    let check = || {
        if interrupt.load(Ordering::SeqCst) {
            Err(Error::Interrupted)
        } else {
            Ok(())
        }
    };
    let mut client: Option<Fastboot<T>> = None;
    let outcome = (|| -> Result<()> {
        let data = read_limited(Path::new(&image))?;
        validate_image(&data, Some(&sha256))?;
        validate_install_cmdline(&data)?;
        validate_stock(&read_limited(Path::new(&stock))?)?;
        check()?;
        let mut receipt = Receipt::create(Path::new(&attempt), Path::new(&image), &data)?;
        let inner = (|| -> Result<()> {
            check()?;
            let connected = connect()?.with_interrupt(interrupt.clone());
            let client = client.insert(connected);
            install_boot(client, &data, &mut receipt)
        })();
        if let Err(error) = inner {
            if client.is_none() {
                receipt.record_stopped_not_started()?;
            }
            return Err(error);
        }
        Ok(())
    })();
    let mut result = match outcome {
        Ok(()) => {
            let _ = writeln!(
                out,
                "{PRODUCT}: flash:boot acknowledged; readback=UNKNOWN; no reboot sent"
            );
            0
        }
        Err(Error::Fastboot(message)) => {
            let _ = writeln!(
                err,
                "stopped: {message}; inspect receipt before any further attempt"
            );
            1
        }
        Err(Error::Exists) => {
            let _ = writeln!(
                err,
                "stopped: attempt receipt already exists; reuse prohibited"
            );
            1
        }
        Err(Error::Io(_) | Error::Other(_) | Error::Interrupted) => {
            let _ = writeln!(
                err,
                "stopped: I/O or interruption; inspect receipt; no retry"
            );
            1
        }
    };
    if let Some(mut client) = client
        && client.close().is_err()
    {
        let _ = writeln!(err, "stopped: USB cleanup failed; inspect receipt");
        result = 1;
    }
    result
}
