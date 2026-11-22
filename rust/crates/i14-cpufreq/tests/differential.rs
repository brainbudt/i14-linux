//! Differential test: the Python original and this port, each on its own fake
//! sysfs tree, driven through the same long scripted sequence. After every step
//! the outcome, internal state, syslog lines and the whole file tree must match.
//!
//! The Python script is loaded the way tests/test_i14_cpufreq.py loads it;
//! override its path with I14_CPUFREQ_PY.
use i14_cpufreq::{Error, Policy};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

const DEFAULT_SCRIPT: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../rootfs/overlay/usr/sbin/i14-cpufreq"
);

const DRIVER: &str = r#"
import importlib.machinery, importlib.util, sys
script, root = sys.argv[1], sys.argv[2]
loader = importlib.machinery.SourceFileLoader("i14_cpufreq", script)
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
log = []
mod.syslog.syslog = log.append


def limit_fixed(self, value):
    """The Rust fix applied to the Python for this comparison: `applied` after the write
    succeeds and before the readback (the original sets it first, so a failed
    299000 -> 0 write left the cap at 299000 and close() would not reset it)."""
    if int(self.cap.read_text()) != self.applied:
        raise RuntimeError("CPU limit changed externally; leaving that value alone")
    if value != self.applied:
        self.cap.write_text(f"{value}\n")
        self.applied = value
        if int(self.cap.read_text()) != value:
            raise RuntimeError(f"CPU control readback mismatch: {self.cap}")
        mod.syslog.syslog(f"CPU max: {value} kHz" if value else "CPU max: governor control")


mod.Policy.limit = limit_fixed
EXPLICIT = {"invalid per-CPU counters", "no per-CPU counters", "missing driver screen state"}
policy = mod.Policy(root)
for line in sys.stdin:
    op = line.strip()
    err = "ok"
    try:
        if op == "new":
            policy = mod.Policy(root)
        else:
            getattr(policy, op)()
    except OSError as e:
        err = f"OSError:{e.errno}"
    except ValueError as e:
        err = "ValueError:" + (str(e) if str(e) in EXPLICIT else "parse")
    except RuntimeError as e:
        err = "RuntimeError:" + str(e).replace(root, "ROOT")
    prev = ";".join(k + "=" + ",".join(map(str, v)) for k, v in sorted(getattr(policy, "previous", {}).items()))
    of = getattr(policy, "original_fix", None)
    print(f"{err}|{int(policy.started)}|{policy.applied}|{policy.quiet}|{'-' if of is None else of}|{prev}|{' / '.join(log)}", flush=True)
    log.clear()
"#;

const EXPLICIT: [&str; 3] = [
    "invalid per-CPU counters",
    "no per-CPU counters",
    "missing driver screen state",
];

fn rust_line(p: &mut Policy, r: Result<(), Error>, root: &Path) -> String {
    let err = match r {
        Ok(()) => "ok".to_string(),
        Err(Error::Os(e)) => format!("OSError:{}", e.raw_os_error().unwrap()),
        Err(Error::Value(m)) => format!(
            "ValueError:{}",
            if EXPLICIT.contains(&m.as_str()) {
                m.as_str()
            } else {
                "parse"
            }
        ),
        Err(Error::Runtime(m)) => {
            format!("RuntimeError:{}", m.replace(root.to_str().unwrap(), "ROOT"))
        }
    };
    let prev: Vec<String> = p
        .previous
        .iter()
        .map(|(k, v)| {
            format!(
                "{k}={}",
                v.iter()
                    .map(|x| x.to_string())
                    .collect::<Vec<_>>()
                    .join(",")
            )
        })
        .collect();
    let of = p.original_fix.map_or("-".to_string(), |v| v.to_string());
    let log = std::mem::take(&mut p.log).join(" / ");
    format!(
        "{err}|{}|{}|{}|{of}|{}|{log}",
        p.started as u8,
        p.applied,
        p.quiet,
        prev.join(";")
    )
}

struct Py {
    _child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
}

impl Py {
    fn call(&mut self, op: &str) -> String {
        writeln!(self.stdin, "{op}").unwrap();
        let mut line = String::new();
        assert!(
            self.stdout.read_line(&mut line).unwrap() > 0,
            "python driver died"
        );
        line.trim_end_matches('\n').to_string()
    }
}

/// Deterministic LCG, so every run is the same sequence.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }
    fn pick<'a, T>(&mut self, v: &'a [T]) -> &'a T {
        &v[self.below(v.len() as u64) as usize]
    }
}

const PROC: &str = "proc/cpufreq";
const CPU: &str = "sys/devices/system/cpu/cpu0/cpufreq";
const CAP: &str = "proc/cpufreq/cpufreq_limited_max_freq_by_user";
const FIX: &str = "proc/cpufreq/cpufreq_fix_freq_in_es";
const STATE: &str = "proc/cpufreq/cpufreq_state";
const STAT: &str = "proc/stat";
const DRV: &str = "sys/devices/system/cpu/cpu0/cpufreq/scaling_driver";
const GOV: &str = "sys/devices/system/cpu/cpu0/cpufreq/scaling_governor";
const AVAIL: &str = "sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies";
const FILES: [&str; 7] = [CAP, FIX, STATE, STAT, DRV, GOV, AVAIL];

struct World {
    roots: [PathBuf; 2],
    ticks: Vec<[u64; 3]>,
}

impl World {
    /// Applies one file change to both trees.
    fn put(&self, rel: &str, data: &[u8]) {
        for r in &self.roots {
            let p = r.join(rel);
            if p.is_dir() {
                fs::remove_dir_all(&p).unwrap();
            }
            if p.exists() {
                fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
            }
            fs::write(&p, data).unwrap();
        }
    }
    fn each(&self, f: impl Fn(&Path)) {
        for r in &self.roots {
            f(r)
        }
    }
    fn screen(&self, on: bool) {
        let s = format!(
            "[MT_CPU_DVFS_LITTLE/0]\ndvfs_disable_by_early_suspend = {}\n",
            if on { 0 } else { 1 }
        );
        self.put(STATE, s.as_bytes());
    }
    fn advance(&mut self, busy: &[u64], waiting: u64) {
        let mut s = String::from("cpu 0 0 0 0 0 0 0 0 0 0\n");
        for (i, t) in self.ticks.iter_mut().enumerate() {
            let b = busy.get(i).copied().unwrap_or(0);
            t[0] += b;
            t[1] += 100 - b - waiting;
            t[2] += waiting;
            s += &format!("cpu{i} {} 0 0 {} {} 0 0 0 0 0\n", t[0], t[1], t[2]);
        }
        s += "intr 42\n";
        self.put(STAT, s.as_bytes());
    }
    fn sane(&mut self, applied: &str, started: bool) {
        self.put(DRV, b"mt-cpufreq");
        self.put(GOV, b"interactive");
        self.put(AVAIL, b"1300000 819000 598000 442000 299000");
        self.put(FIX, if started { b"0\n" } else { b"1" });
        self.put(CAP, applied.as_bytes());
        self.screen(false);
        self.advance(&[], 0);
    }
}

fn snapshot(root: &Path) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for rel in FILES {
        let p = root.join(rel);
        let v = match fs::symlink_metadata(&p) {
            Err(_) => "<missing>".to_string(),
            Ok(m) if m.is_dir() => "<dir>".to_string(),
            Ok(m) => format!(
                "{:o}:{:?}",
                m.permissions().mode() & 0o777,
                fs::read(&p).unwrap()
            ),
        };
        out.push((rel.to_string(), v));
    }
    out
}

/// One policy call on both sides; the outcome lines must be identical.
fn op(w: &World, rs: &mut Policy, py: &mut Py, name: &str, step: usize) -> String {
    let a = py.call(name);
    let r = match name {
        "new" => {
            *rs = Policy::new(&w.roots[1]);
            Ok(())
        }
        "start" => rs.start(),
        "tick" => rs.tick(),
        "close" => rs.close(),
        _ => unreachable!(),
    };
    let b = rust_line(rs, r, &w.roots[1]);
    assert_eq!(a, b, "step {step}: {name}");
    a
}

#[test]
fn python_and_rust_agree_step_by_step() {
    let script = std::env::var("I14_CPUFREQ_PY").unwrap_or(DEFAULT_SCRIPT.into());
    assert!(
        Path::new(&script).is_file(),
        "Python original not found: {script}"
    );
    let base =
        PathBuf::from(env!("CARGO_TARGET_TMPDIR")).join(format!("diff-{}", std::process::id()));
    let _ = fs::remove_dir_all(&base);
    let roots = [base.join("py"), base.join("rs")];
    for r in &roots {
        fs::create_dir_all(r.join(PROC)).unwrap();
        fs::create_dir_all(r.join(CPU)).unwrap();
    }
    let mut w = World {
        roots: roots.clone(),
        ticks: vec![[0; 3]; 2],
    };
    w.sane("0", false);

    let mut child = Command::new("python3")
        .args(["-I", "-c", DRIVER, &script, roots[0].to_str().unwrap()])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("python3");
    let stdin = child.stdin.take().unwrap();
    let stdout = BufReader::new(child.stdout.take().unwrap());
    let mut py = Py {
        _child: child,
        stdin,
        stdout,
    };
    let mut rs = Policy::new(&roots[1]);

    let mut steps = 0usize;
    let (mut ops, mut errors, mut caps) = (0usize, 0usize, 0usize);
    let mut tally = |a: String| {
        ops += 1;
        errors += !a.starts_with("ok") as usize;
        caps += a.contains("CPU max: 299000") as usize;
    };
    let check = |w: &World, step: usize| {
        assert_eq!(
            snapshot(&w.roots[0]),
            snapshot(&w.roots[1]),
            "step {step}: file trees differ"
        );
    };

    // Scripted prefix: the cases the unit tests cover, then the awkward ones.
    let mut rng = Rng(0x1414_c0de);
    type Step = Box<dyn Fn(&mut World)>;
    let scripted: Vec<(Step, &str)> = vec![
        (Box::new(|_| {}), "close"), // close before start: no-op
        (Box::new(|w| w.put(CAP, b"598000")), "start"),
        (Box::new(|w| w.put(CAP, b"0")), "new"),
        (Box::new(|w| w.put(DRV, b"other")), "start"),
        (Box::new(|w| w.put(DRV, b" mt-cpufreq\n")), "new"),
        (Box::new(|w| w.put(GOV, b"performance")), "start"),
        (Box::new(|w| w.put(GOV, b"ondemand\n")), "new"),
        (Box::new(|w| w.put(AVAIL, b"1300000 598000")), "start"), // short-circuits before reading cap
        (
            Box::new(|w| w.each(|r| fs::remove_file(r.join(CAP)).unwrap())),
            "start",
        ),
        (Box::new(|w| w.put(AVAIL, b"1300000\x1c299000")), "start"), // missing cap now read: OSError
        (Box::new(|w| w.put(CAP, b" 0_0 \n")), "new"),
        (Box::new(|w| w.put(FIX, b"2")), "start"),
        (Box::new(|w| w.put(FIX, b"+1\r\n")), "new"),
        (
            Box::new(|w| w.put(STATE, b"dvfs_disable_by_early_suspend = 1\r\n")),
            "start",
        ), // CRLF still matches
        (Box::new(|w| w.screen(false)), "close"),
        (Box::new(|w| w.put(FIX, b"1")), "new"),
        (
            Box::new(|w| {
                w.each(|r| {
                    fs::set_permissions(r.join(FIX), fs::Permissions::from_mode(0o444)).unwrap()
                })
            }),
            "start",
        ),
        (Box::new(|_| {}), "close"),
        (Box::new(|w| w.put(FIX, b"1")), "new"),
        (Box::new(|_| {}), "start"),
    ];
    for (i, (prep, name)) in scripted.iter().enumerate() {
        prep(&mut w);
        check(&w, i);
        tally(op(&w, &mut rs, &mut py, name, i));
        check(&w, i);
        steps += 1;
    }
    // Thresholds exactly at 0.30 and 0.05, hysteresis, then a failing cap write
    // (applied is updated before the write) followed by close().
    let ro = |w: &World| {
        w.each(|r| fs::set_permissions(r.join(CAP), fs::Permissions::from_mode(0o444)).unwrap())
    };
    let mut boundary: Vec<(Vec<u64>, u64, &str)> = vec![(vec![0, 0], 0, "tick"); 6];
    boundary.push((vec![0, 30], 0, "tick")); // == 0.30 releases
    boundary.extend(vec![(vec![0, 0], 0, "tick"); 5]);
    boundary.push((vec![30, 0], 0, "tick")); // releases from the cap
    boundary.extend(vec![(vec![0, 0], 0, "tick"); 5]);
    boundary.push((vec![5, 0], 0, "tick")); // == 0.05 is not quiet: keeps cap, resets delay
    boundary.push((vec![29, 0], 10, "tick")); // hysteresis keeps cap
    boundary.extend(vec![(vec![4, 4], 50, "tick"); 4]);
    boundary.push((vec![29, 0], 0, "screen-on-ro")); // cap write fails
    boundary.push((vec![0, 0], 0, "close")); // cap no longer "ours": left alone
    for (busy, waiting, name) in boundary {
        w.advance(&busy, waiting);
        let name = if name == "screen-on-ro" {
            w.screen(true);
            ro(&w);
            "tick"
        } else {
            name
        };
        tally(op(&w, &mut rs, &mut py, name, steps));
        check(&w, steps);
        steps += 1;
    }
    w.sane("0", false);
    tally(op(&w, &mut rs, &mut py, "new", steps));
    tally(op(&w, &mut rs, &mut py, "start", steps));

    let stat_garbage: [&[u8]; 12] = [
        b"",
        b"cpu 0 0 0 0\n",
        b"cpu0 1 2 3\n",
        b"cpu0 a b c d e f g h\n",
        b"cpu0 -1 0 0 0 0 0 0 0\n",
        b"cpu0 1_0 0 0 100 0 0 0 0\r\ncpu1 +5 0 0 100 0 0 0 0 0 0\n",
        b"cpu0 1 0 0 \xff 0 0 0 0\n",
        b"cpu0\x0b1 2 3 4 5 6 7 8\ncpu1 1 2 3 4 5 6 7 8\n",
        b"cpux 1 2 3 4 5 6 7 8\ncpu0 1 1 1 1 1 1 1 1\n",
        b"cpu0 1 1 1 1 1 1 1 1\ncpu0 9 9 9 9 9 9 9 9\ncpu1 0 0 0 9 0 0 0 0\n",
        b"cpu0 18446744073709551615 0 0 18446744073709551615 0 0 0 0\ncpu1\x1f1 1 1 1 1 1 1 1\n",
        b"intr 1\n\ncpu0 0 0 0 0 0 0 0 0\n",
    ];
    let cap_values: [&[u8]; 9] = [
        b"0",
        b"299000",
        b"598000",
        b" 299000\n",
        b"0_0",
        b"garbage",
        b"",
        b"\xfe0",
        b"299_000\r\n",
    ];
    let fix_values: [&[u8]; 6] = [b"0", b"1", b"2", b"0\r\n", b"x", b"\x1c0\x1f"];
    let state_values: [&[u8]; 6] = [
        b"dvfs_disable_by_early_suspend = 2\n",
        b" dvfs_disable_by_early_suspend = 1\n",
        b"x\ndvfs_disable_by_early_suspend = 0\ndvfs_disable_by_early_suspend = 1",
        b"dvfs_disable_by_early_suspend = 1\rnext",
        b"dvfs_disable_by_early_suspend = 0 \n",
        b"dvfs_disable_by_early_suspend = 1\x0b",
    ];

    for _ in 0..5000 {
        let (started, applied) = (rs.started, rs.applied.to_string());
        let roll = rng.below(100);
        let name = match roll {
            0..=39 => {
                let n = w.ticks.len();
                let busy: Vec<u64> = (0..n)
                    .map(|_| {
                        if rng.below(6) == 0 {
                            rng.below(70)
                        } else {
                            rng.below(5)
                        }
                    })
                    .collect();
                let waiting = if rng.below(4) == 0 { rng.below(25) } else { 0 };
                w.advance(&busy, waiting);
                "tick"
            }
            40..=42 => "tick", // no time passed: zero delta
            43..=50 => {
                w.screen(rng.below(2) == 0);
                "tick"
            }
            51..=54 => "start",
            55..=58 => "close",
            59..=60 => "new",
            61..=64 => {
                w.put(STAT, rng.pick(&stat_garbage));
                "tick"
            }
            65..=68 => {
                w.put(CAP, rng.pick(&cap_values));
                *rng.pick(&["tick", "close", "start"])
            }
            69..=71 => {
                w.put(FIX, rng.pick(&fix_values));
                *rng.pick(&["tick", "close", "start"])
            }
            72..=74 => {
                w.put(STATE, rng.pick(&state_values));
                *rng.pick(&["tick", "start"])
            }
            75..=77 => {
                match rng.below(3) {
                    0 => w.put(
                        DRV,
                        rng.pick(&[&b"mt-cpufreq\n"[..], b" mt-cpufreq ", b"other"]),
                    ),
                    1 => w.put(
                        GOV,
                        rng.pick(&[&b"ondemand"[..], b"interactive\n", b"performance"]),
                    ),
                    _ => w.put(
                        AVAIL,
                        rng.pick(&[&b"1300000 299000"[..], b"1300000 598000", b"2990000"]),
                    ),
                }
                "start"
            }
            78..=79 => {
                let f = *rng.pick(&FILES);
                w.each(|r| {
                    let _ = fs::remove_file(r.join(f));
                    let _ = fs::remove_dir_all(r.join(f));
                });
                *rng.pick(&["tick", "close", "start"])
            }
            80 => {
                let f = *rng.pick(&[CAP, FIX, STATE, STAT]);
                w.each(|r| {
                    let _ = fs::remove_file(r.join(f));
                    let _ = fs::create_dir(r.join(f));
                });
                *rng.pick(&["tick", "close", "start"])
            }
            81..=84 => {
                let f = *rng.pick(&[CAP, FIX]);
                let mode = if rng.below(2) == 0 { 0o444 } else { 0o644 };
                w.each(|r| {
                    let _ = fs::set_permissions(r.join(f), fs::Permissions::from_mode(mode));
                });
                *rng.pick(&["tick", "close", "start", "tick"])
            }
            85 => {
                let n = 1 + rng.below(4) as usize;
                w.ticks.resize(n, [0; 3]);
                w.advance(&[], 0);
                "tick"
            }
            86..=87 => {
                // counters reset (e.g. after a reboot of the fake kernel)
                w.ticks.fill([0; 3]);
                w.advance(&[], 0);
                "tick"
            }
            _ => {
                w.sane(&applied, started);
                "tick"
            }
        };
        // Python raises AttributeError for tick() before start() ever read the
        // counters; the service never does that (it ticks only after start()).
        let name = if name == "tick" && rs.previous.is_empty() {
            "start"
        } else {
            name
        };
        check(&w, steps);
        tally(op(&w, &mut rs, &mut py, name, steps));
        check(&w, steps);
        steps += 1;
    }
    eprintln!("differential: {steps} steps, {ops} compared calls, {errors} error outcomes, {caps} cap-to-299000 events");
    assert!(
        errors > 100 && caps > 20,
        "sequence did not exercise both paths"
    );
    let _ = fs::remove_dir_all(&base);
}
