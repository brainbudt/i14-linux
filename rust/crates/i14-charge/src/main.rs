//! i14-charge-wait: the power-off charging loop of /i14init.
//!
//!   i14-charge-wait                 charge_wait: prints why=/cap=/vol=/end lines for charge_wait_rs, exits 0/1/2
//!   i14-charge-wait --pwrkey-watch  pwrkey_watch: creates $PWRKEY on every power key press
//!
//! Settings and paths come from the environment under the shell's names
//! (AB_PCT, AB_MIN_S, AB_HOLD_S, ..., I14_PS, I14_UPTIME, I14_PWRKEY, ...).
#![forbid(unsafe_code)]

use i14_charge::{charge_wait, pwrkey_watch, Config, Host, Outcome, Paths, RealHost};
use std::io::Write;
use std::process::exit;

fn env(name: &str) -> Option<String> {
    std::env::var(name).ok()
}

enum Mode {
    Charge,
    Pwrkey,
}

fn run(mode: Mode, paths: &Paths, host: &mut impl Host, accept_fifo: bool) -> i32 {
    match mode {
        Mode::Pwrkey => pwrkey_watch(paths, host, accept_fifo),
        Mode::Charge => {
            let out = match Config::from_env(env) {
                Ok(cfg) => charge_wait(&cfg, paths, host),
                Err(e) => Outcome {
                    code: 1,
                    why: format!("i14-charge-wait: {e}"),
                    cap: String::new(),
                    vol: String::new(),
                },
            };
            let mut so = std::io::stdout().lock();
            if so
                .write_all(out.to_lines().as_bytes())
                .and_then(|_| so.flush())
                .is_err()
            {
                return 1; // /i14init would not see $why: rescue mode, never a start
            }
            out.code
        }
    }
}

// The fake clock and FIFO input must never reach the phone: no test-clock for musl.
#[cfg(all(feature = "test-clock", target_env = "musl"))]
compile_error!("feature test-clock is for host differential tests only, never for the phone build");

/// Host tests only: the fake clock replaying I14_TEST_TIMELINE.
#[cfg(feature = "test-clock")]
fn fake_host(paths: &Paths) -> Option<i14_charge::fake::FakeHost> {
    let tl = env("I14_TEST_TIMELINE")?;
    let text = std::fs::read_to_string(&tl).unwrap_or_default();
    let timeline = i14_charge::fake::parse_timeline(&text).unwrap_or_else(|e| {
        eprintln!("i14-charge-wait: {e}");
        exit(64)
    });
    Some(i14_charge::fake::FakeHost {
        paths: paths.clone(),
        root: env("I14_TEST_ROOT").unwrap_or_default().into(),
        timeline,
        date: env("I14_TEST_DATE").unwrap_or_default(),
    })
}

fn main() {
    let paths = Paths::from_env(env);
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mode = match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        [] => Mode::Charge,
        ["--pwrkey-watch"] => Mode::Pwrkey,
        _ => {
            eprintln!("usage: i14-charge-wait [--pwrkey-watch]");
            exit(64)
        }
    };
    #[cfg(feature = "test-clock")]
    {
        let fifo = env("I14_TEST_FIFO").is_some();
        if let Some(mut h) = fake_host(&paths) {
            exit(run(mode, &paths, &mut h, fifo));
        }
        if fifo {
            exit(run(
                mode,
                &paths,
                &mut RealHost {
                    paths: paths.clone(),
                },
                true,
            ));
        }
    }
    exit(run(
        mode,
        &paths,
        &mut RealHost {
            paths: paths.clone(),
        },
        false,
    ))
}
