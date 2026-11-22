//! Differential test: the Python guard (charging.Guard + bcct.Bcct from the repo)
//! and this port run the same long, deterministic script of file changes, clock
//! steps, restarts and stops on the same fake sysfs/proc tree (same paths: the
//! tree is rebuilt between the two runs). After every step the published status
//! file (text, byte for byte), the kernel writes, the return value, the new log
//! lines (minus timestamp) and the guard's state must be identical.
//!
//! The fake kernel stands in for `bcct.write_command` on both sides (in Python
//! by replacing the module attribute, as test_i14_bcct.py does): it records each
//! write and reacts as the Python tests' fakes do (BCCT re-render, readback after
//! a current_cmd write), unless the `fault` file says to fail or ignore writes.

mod common;
use common::*;

use std::cell::RefCell;
use std::fs;
use std::os::unix::fs::symlink;
use std::path::Path;
use std::process::Command;
use std::rc::Rc;

use i14_power::{Bcct, Error, Guard, Paths};
use serde_json::{json, Value};

const DRIVER: &str = r#"
import json, os, sys
from pathlib import Path
from i14ui import charging, bcct
T = Path(sys.argv[1])
steps = json.load(open(sys.argv[2]))
charging.BOOT_ID = str(T / "boot_id")
now = [0.0]
writes = []
CONFIG, CMD, DEV = str(T / "clbcct"), str(T / "cmd"), T / "thermal/cooling_device3"

def render(klog, limits, states):
    eff = min([l for l, s in zip(limits, states) if s and l > 0], default=65535)
    lines = [f"klog {klog}", f"curr_limit {eff}"]
    lines += [f"mtk-cl-bcct{i:02} {l} mA, state {s}" for i, (l, s) in enumerate(zip(limits, states))]
    (T / "clbcct").write_text("\n".join(lines) + "\n")
    (DEV / "cur_state").write_text(f"{states[0]}\n")

def fake(path, text):
    p = os.fspath(path)
    writes.append([p, text + "\n"])
    mode = charging.read(T / "fault") or "ok"
    if p == CMD:
        if mode in ("eio", "cmd-eio"):
            raise OSError(5, "Input/output error")
        if mode in ("ignore", "cmd-ignore"):
            return
        u, d = text.split()
        (T / "readback").write_text(f"get_usb_current_unlimited={u},\ncmd_discharging = {d}\n")
        return
    if mode in ("eio", "bcct-eio"):
        raise OSError(5, "Input/output error")
    if mode in ("ignore", "bcct-ignore"):
        return
    lines = (T / "clbcct").read_text().splitlines()
    klog = int(lines[0].split()[1])
    limits = [int(l.split()[1]) for l in lines[2:5]]
    states = [int(l.split()[4]) for l in lines[2:5]]
    if p == CONFIG:
        klog, *limits = map(int, text.split())
    else:
        states[0] = int(text)
    render(klog, limits, states)

bcct.write_command = fake

def make():
    return charging.Guard(battery=str(T / "battery"), charger_type=str(T / "type"), command=CMD,
                          readback=str(T / "readback"), usb=str(T / "usb"), ac=str(T / "ac"),
                          settings=str(T / "ui.json"), log=str(T / "log"), gauge=str(T / "gauge"),
                          gauge_activity=str(T / "activity"),
                          control=bcct.Bcct(config=CONFIG, thermal_root=str(T / "thermal")),
                          status_path=str(T / "status.json"), clock=lambda: now[0],
                          drain_path=str(T / "drain-request"), uptime=up, drain_owner=os.getuid())

def up():
    mode = charging.read(T / "uptime")
    if mode == "fail":
        raise OSError(5, "Input/output error")
    return now[0] + int(mode or "0")

def apply(op):
    kind, p = op[0], T / op[1]
    if kind == "write":
        p.write_text(op[2])
    elif kind == "rm":
        if p.is_dir() and not p.is_symlink():
            os.rmdir(p)
        elif os.path.lexists(p):
            os.unlink(p)
    elif kind == "mkdir":
        p.mkdir(exist_ok=True)
    elif kind == "rmdir":
        if p.is_dir() and not p.is_symlink():
            os.rmdir(p)
    elif kind == "symlink":
        if os.path.lexists(p):
            os.unlink(p)
        os.symlink(op[2], p)
    elif kind == "chmod":
        if os.path.lexists(p):
            os.chmod(p, int(op[2], 8))

out, nlog, guard = [], 0, None
for step in steps:
    for op in step["ops"]:
        apply(op)
    now[0] = step["now"]
    writes.clear()
    ret = None
    action = step["action"]
    if action == "restart":
        guard = make()
    elif action == "tick":
        ret = guard.tick()
    elif action == "close":
        guard.close()
    try:
        status = (T / "status.json").read_text()
    except OSError:
        status = None
    try:
        lines = (T / "log").read_text().splitlines()
    except OSError:
        lines = []
    new, nlog = lines[nlog:], len(lines)
    out.append(dict(status=status, writes=list(writes), ret=ret, log=new,
                    state=[guard.paused, guard.migrated, guard.level, guard.activity, guard.limit, guard.n,
                           guard.activity_changed, guard.drain_until]))
json.dump(out, open(sys.argv[3], "w"))
"#;

// ---------- the fake tree and kernel (Rust side) ----------

fn render(t: &Path, klog: i64, limits: &[i64], states: &[i64]) {
    write(t.join("clbcct"), &render_text(klog, limits, states, None));
    write(
        t.join("thermal/cooling_device3/cur_state"),
        &format!("{}\n", states[0]),
    );
}

fn render_text(klog: i64, limits: &[i64], states: &[i64], effective: Option<i64>) -> String {
    let eff = effective.unwrap_or_else(|| {
        limits
            .iter()
            .zip(states)
            .filter(|(l, s)| **s != 0 && **l > 0)
            .map(|(l, _)| *l)
            .min()
            .unwrap_or(65535)
    });
    let mut lines = vec![format!("klog {klog}"), format!("curr_limit {eff}")];
    for (i, (l, s)) in limits.iter().zip(states).enumerate() {
        lines.push(format!("mtk-cl-bcct{i:02} {l} mA, state {s}"));
    }
    lines.join("\n") + "\n"
}

fn init_tree(t: &Path) {
    let _ = fs::remove_dir_all(t);
    fs::create_dir_all(t.join("battery")).unwrap();
    for (k, v) in [
        ("capacity", "80"),
        ("status", "Charging"),
        ("batt_vol", "4100"),
        ("batt_temp", "250"),
    ] {
        write(t.join("battery").join(k), v);
    }
    for (k, v) in [
        ("type", "2"),
        ("usb", "1"),
        ("ac", "0"),
        ("cmd", "sentinel"),
        (
            "readback",
            "get_usb_current_unlimited=0,\ncmd_discharging = -1\n",
        ),
        ("gauge", "4109"),
        ("activity", "1000"),
        ("boot_id", "boot-a"),
        ("fault", "ok"),
        ("uptime", "0"),
        (
            "ui.json",
            r#"{"charge_limit_enabled":true,"charge_limit":80}"#,
        ),
    ] {
        write(t.join(k), v);
    }
    fs::create_dir_all(t.join("thermal/cooling_device0")).unwrap();
    write(t.join("thermal/cooling_device0/type"), "other\n");
    fs::create_dir_all(t.join("thermal/cooling_device3")).unwrap();
    write(t.join("thermal/cooling_device3/type"), "mtk-cl-bcct00\n");
    write(t.join("thermal/cooling_device3/max_state"), "1\n");
    fs::create_dir_all(t.join("thermal/thermal_zone0")).unwrap();
    symlink("../cooling_device0", t.join("thermal/thermal_zone0/cdev0")).unwrap();
    render(t, 0, &[0, -1, 0], &[0, 0, 0]);
}

fn fake_kernel(
    t: &Path,
    writes: Rc<RefCell<Vec<Value>>>,
) -> impl FnMut(&Path, &str) -> Result<(), Error> + Clone {
    let t = t.to_path_buf();
    move |path: &Path, text: &str| {
        let p = path.to_string_lossy().to_string();
        writes.borrow_mut().push(json!([p, format!("{text}\n")]));
        let mode = i14_config::charge_status::read_text(&t.join("fault").to_string_lossy());
        let mode = if mode.is_empty() {
            "ok".to_string()
        } else {
            mode
        };
        let eio = || Err(Error("[Errno 5] Input/output error".into()));
        if p == t.join("cmd").to_string_lossy() {
            if mode == "eio" || mode == "cmd-eio" {
                return eio();
            }
            if mode == "ignore" || mode == "cmd-ignore" {
                return Ok(());
            }
            let w: Vec<&str> = text.split_whitespace().collect();
            write(
                t.join("readback"),
                &format!(
                    "get_usb_current_unlimited={},\ncmd_discharging = {}\n",
                    w[0], w[1]
                ),
            );
            return Ok(());
        }
        if mode == "eio" || mode == "bcct-eio" {
            return eio();
        }
        if mode == "ignore" || mode == "bcct-ignore" {
            return Ok(());
        }
        let config = read(t.join("clbcct"));
        let lines: Vec<&str> = config.lines().collect();
        let mut klog: i64 = lines[0].split_whitespace().nth(1).unwrap().parse().unwrap();
        let mut limits: Vec<i64> = lines[2..5]
            .iter()
            .map(|l| l.split_whitespace().nth(1).unwrap().parse().unwrap())
            .collect();
        let mut states: Vec<i64> = lines[2..5]
            .iter()
            .map(|l| l.split_whitespace().nth(4).unwrap().parse().unwrap())
            .collect();
        let words: Vec<i64> = text
            .split_whitespace()
            .map(|w| w.parse().unwrap())
            .collect();
        if p == t.join("clbcct").to_string_lossy() {
            klog = words[0];
            limits = words[1..].to_vec();
        } else {
            states[0] = words[0];
        }
        render(&t, klog, &limits, &states);
        Ok(())
    }
}

fn apply(t: &Path, op: &Value) {
    let kind = op[0].as_str().unwrap();
    let p = t.join(op[1].as_str().unwrap());
    match kind {
        "write" => write(&p, op[2].as_str().unwrap()),
        "rm" => match fs::symlink_metadata(&p) {
            Ok(m) if m.is_dir() => fs::remove_dir(&p).unwrap(),
            Ok(_) => fs::remove_file(&p).unwrap(),
            Err(_) => {}
        },
        "mkdir" => {
            let _ = fs::create_dir(&p);
        }
        "rmdir" => {
            if fs::symlink_metadata(&p).is_ok_and(|m| m.is_dir()) {
                fs::remove_dir(&p).unwrap();
            }
        }
        "symlink" => {
            if fs::symlink_metadata(&p).is_ok() {
                fs::remove_file(&p).unwrap();
            }
            symlink(op[2].as_str().unwrap(), &p).unwrap();
        }
        "chmod" => {
            use std::os::unix::fs::PermissionsExt;
            let mode = u32::from_str_radix(op[2].as_str().unwrap(), 8).unwrap();
            if fs::symlink_metadata(&p).is_ok() {
                fs::set_permissions(&p, fs::Permissions::from_mode(mode)).unwrap();
            }
        }
        _ => panic!("op {kind}"),
    }
}

fn run_rust(t: &Path, steps: &[Value]) -> Vec<Value> {
    let now = Rc::new(std::cell::Cell::new(0.0));
    let writes = Rc::new(RefCell::new(Vec::new()));
    let kernel = fake_kernel(t, writes.clone());
    let make = || {
        let paths = Paths {
            battery: t.join("battery"),
            charger_type: t.join("type"),
            command: t.join("cmd"),
            readback: t.join("readback"),
            usb: t.join("usb"),
            ac: t.join("ac"),
            settings: t.join("ui.json"),
            log: t.join("log"),
            gauge: t.join("gauge"),
            gauge_activity: t.join("activity"),
            status: t.join("status.json"),
            boot_id: t.join("boot_id"),
            drain_request: t.join("drain-request"),
        };
        let mut bcct = Bcct::new(t.join("clbcct"), t.join("thermal"));
        bcct.write = Box::new(kernel.clone());
        let mut g = Guard::new(paths, Box::new(bcct), clock(&now));
        g.write = Box::new(kernel.clone());
        let (up_now, up_file) = (now.clone(), t.join("uptime"));
        g.uptime = Box::new(move || {
            let mode = i14_config::charge_status::read_text(&up_file.to_string_lossy());
            if mode == "fail" {
                return None;
            }
            let offset: i64 = if mode.is_empty() {
                0
            } else {
                mode.parse().unwrap()
            };
            Some(up_now.get() + offset as f64)
        });
        // SAFETY: getuid has no preconditions.
        g.drain_owner = unsafe { libc::getuid() };
        g
    };
    let (mut out, mut nlog, mut guard): (Vec<Value>, usize, Option<Guard>) = (Vec::new(), 0, None);
    for step in steps {
        for op in step["ops"].as_array().unwrap() {
            apply(t, op);
        }
        now.set(step["now"].as_f64().unwrap());
        writes.borrow_mut().clear();
        let mut ret = Value::Null;
        match step["action"].as_str().unwrap() {
            "restart" => guard = Some(make()),
            "tick" => ret = Value::Bool(guard.as_mut().unwrap().tick()),
            "close" => guard.as_mut().unwrap().close(),
            a => panic!("action {a}"),
        }
        let status = fs::read_to_string(t.join("status.json")).map_or(Value::Null, Value::String);
        let text = fs::read_to_string(t.join("log")).unwrap_or_default();
        let lines: Vec<&str> = text.lines().collect();
        let new: Vec<&str> = lines[nlog.min(lines.len())..].to_vec();
        nlog = lines.len();
        let g = guard.as_ref().unwrap();
        out.push(json!({
            "status": status,
            "writes": writes.borrow().clone(),
            "ret": ret,
            "log": new,
            "state": [g.paused, g.migrated, g.level, g.activity, g.limit, g.n, g.activity_changed, g.drain_until],
        }));
    }
    out
}

fn run_python(t: &Path, steps_file: &Path, out_file: &Path) -> Vec<Value> {
    let out = Command::new("python3")
        .arg("-B")
        .arg("-c")
        .arg(DRIVER)
        .arg(t)
        .arg(steps_file)
        .arg(out_file)
        .env("PYTHONPATH", repo().join("rootfs/overlay/usr/lib"))
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .output()
        .expect("python3");
    assert!(
        out.status.success(),
        "python driver: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    serde_json::from_str(&read(out_file)).unwrap()
}

// ---------- the script ----------

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
    fn chance(&mut self, percent: usize) -> bool {
        self.below(100) < percent
    }
    fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len())]
    }
}

const OCV: [(i64, i64); 21] = i14_power::guard::OCV;

fn ocv(p: i64) -> i64 {
    OCV.iter().find(|x| x.0 == p).unwrap().1
}

fn w(path: &str, text: &str) -> Value {
    json!(["write", path, text])
}

fn rm(path: &str) -> Value {
    json!(["rm", path])
}

/// A fault that is set now and restored after a few steps.
struct Pending {
    due: usize,
    ops: Vec<Value>,
}

fn excursion(
    r: &mut Rng,
    pending: &mut Vec<Pending>,
    ops: &mut Vec<Value>,
    i: usize,
    set: Vec<Value>,
    restore: Vec<Value>,
) {
    ops.extend(set);
    pending.push(Pending {
        due: i + 1 + r.below(6),
        ops: restore,
    });
}

fn sensor(
    r: &mut Rng,
    pending: &mut Vec<Pending>,
    ops: &mut Vec<Value>,
    i: usize,
    path: &str,
    good: &str,
    bad: &[&str],
) {
    let v = *r.pick(bad);
    let set = if v == "rm" { rm(path) } else { w(path, v) };
    excursion(r, pending, ops, i, vec![set], vec![w(path, good)]);
}

fn script(seed: u64, n: usize) -> Vec<Value> {
    let mut r = Rng(seed);
    let mut d = Rng(seed ^ 0xd3a1_5eed_0bad_cafe);
    let mut now = 12345.678 + seed as f64 * 0.001;
    let (mut limit, mut zcv, mut activity, mut frozen) = (80i64, 4050i64, 1000i64, 0usize);
    let mut pending: Vec<Pending> = Vec::new();
    // The fake uptime (drain deadlines) is the script clock minus `base`: about 1000 s at the start.
    let base = now as i64 - 1000;
    let mut steps =
        vec![json!({"now": now, "ops": [w("uptime", &(-base).to_string())], "action": "restart"})];
    let mut closed = false;
    for i in 0..n {
        let mut ops: Vec<Value> = Vec::new();
        let (due, rest): (Vec<Pending>, Vec<Pending>) = pending.drain(..).partition(|p| p.due <= i);
        pending = rest;
        for p in due {
            ops.extend(p.ops);
        }
        // Clock: mostly the 60 s loop, sometimes short, long, stale or backwards steps.
        now += *r.pick(&[
            60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 10.0, 30.0, 61.0, 59.9, 0.0, 0.1,
            120.0, 151.0, 181.0, 200.0, -5.0,
        ]);
        // Gauge: a random walk with jumps to the band edges of the current limit.
        let (stop, resume) = (ocv(limit), ocv(limit - 10));
        zcv = match r.below(10) {
            0..=4 => (zcv + r.below(41) as i64 - 20).clamp(3300, 4450),
            5..=7 => *r.pick(&[
                stop - 1,
                stop,
                stop + 1,
                resume - 1,
                resume,
                resume + 1,
                (stop + resume) / 2,
            ]),
            8 => *r.pick(&[3712, 3713, 3711, 3600, 2500, 5000, 4324, 4400, 3400, 3399]),
            _ => 3400 + r.below(1000) as i64,
        };
        match r.below(40) {
            0 => ops.push(w("gauge", "garbage")),
            1 => ops.push(rm("gauge")),
            2 => ops.push(w(
                "gauge",
                r.pick(&[
                    "", "5001", "2499", " 4109\n", "04109", "4109.0", "-4109", "1234567",
                ]),
            )),
            _ => ops.push(w("gauge", &zcv.to_string())),
        }
        // Gauge activity: changing, frozen for a while, or unreadable.
        if frozen > 0 {
            frozen -= 1;
        } else if r.chance(3) {
            frozen = 1 + r.below(5);
        } else {
            match r.below(30) {
                0 => ops.push(w(
                    "activity",
                    r.pick(&["abc", "", "100001", "-100001", "1234567", "+5", "1.5"]),
                )),
                1 => ops.push(rm("activity")),
                2 => ops.push(w(
                    "activity",
                    r.pick(&["-0", "0", "100000", "-100000", "-000001", " 42 \n"]),
                )),
                _ => {
                    activity = (activity + r.below(3000) as i64 - 1500).clamp(-99999, 99999);
                    ops.push(w("activity", &activity.to_string()));
                }
            }
        }
        match i % 40 {
            0 => ops.push(w("FG_g_fg_dbg_bat_car", &i.to_string())),
            1 => ops.push(w("FG_g_fg_dbg_bat_car", &(i + 1).to_string())),
            2 => ops.push(w("FG_g_fg_dbg_bat_car", "invalid")),
            3 => ops.push(rm("FG_g_fg_dbg_bat_car")),
            _ => {}
        }
        let (pd, o) = (&mut pending, &mut ops);
        if r.chance(5) {
            let bad = [
                "100", "3", "4", "5", "6", "0", "101", "", "x", "50", " 7\n", "-1", "rm",
            ];
            sensor(&mut r, pd, o, i, "battery/capacity", "80", &bad);
        }
        if r.chance(5) {
            let bad = [
                "Full",
                "Discharging",
                "Not charging",
                "Cmd discharging",
                "Unknown",
                "",
                "charging",
            ];
            sensor(&mut r, pd, o, i, "battery/status", "Charging", &bad);
        }
        if r.chance(3) {
            sensor(
                &mut r,
                pd,
                o,
                i,
                "battery/batt_vol",
                "4100",
                &["3700", "3701", "2499", "5001", "x"],
            );
        }
        if r.chance(3) {
            sensor(
                &mut r,
                pd,
                o,
                i,
                "battery/batt_temp",
                "250",
                &["450", "451", "0", "-1"],
            );
        }
        if r.chance(3) {
            sensor(&mut r, pd, o, i, "type", "2", &["0", "8", "9", "x", "rm"]);
        }
        if r.chance(3) {
            sensor(&mut r, pd, o, i, "usb", "1", &["0", "2", "x", "rm"]);
        }
        if r.chance(3) {
            sensor(&mut r, pd, o, i, "ac", "0", &["1", "2", "x", "rm"]);
        }
        if r.chance(6) {
            let l = *r.pick(&[60i64, 70, 80, 90, 100]);
            let enabled = r.chance(85);
            match r.below(14) {
                0 => ops.push(w("ui.json", "{broken")),
                1 => ops.push(w("ui.json", "[]")),
                2 => ops.push(w(
                    "ui.json",
                    r#"{"charge_limit_enabled":true,"charge_limit":85}"#,
                )),
                3 => ops.push(w(
                    "ui.json",
                    r#"{"charge_limit_enabled":true,"charge_limit":80.0}"#,
                )),
                4 => ops.push(rm("ui.json")),
                5 => {
                    ops.push(w("ui.json", r#"{"charge_limit_enabled":true}"#));
                    limit = 80;
                }
                6 => ops.push(w(
                    "ui.json",
                    r#"{"pin":"broken","charge_limit_enabled":true}"#,
                )),
                _ => {
                    ops.push(w(
                        "ui.json",
                        &json!({"charge_limit_enabled": enabled, "charge_limit": l}).to_string(),
                    ));
                    limit = l;
                }
            }
        }
        if r.chance(4) {
            let normal = "get_usb_current_unlimited=0,\ncmd_discharging = -1\n";
            let v = *r.pick(&[
                "get_usb_current_unlimited=0,\ncmd_discharging = 0\n",
                "get_usb_current_unlimited=1,\ncmd_discharging = -1\n",
                "get_usb_current_unlimited=0,\ncmd_discharging = 1\n",
                "get_usb_current_unlimited=1,\ncmd_discharging = 1\n",
                "get_usb_current_unlimited=1,\ncmd_discharging = 1\n",
                "garbage",
                "",
                "rm",
                "cmd_discharging = 1x\ncmd_discharging=0\nget_usb_current_unlimited=0\n",
                "get_usb_current_unlimited=01\ncmd_discharging=0\n",
                "get_usb_current_unlimited=0_ get_usb_current_unlimited=1.\ncmd_discharging\t=\t-10 cmd_discharging=-1\n",
                "cmd_discharging = -1\n",
            ]);
            let set = if v == "rm" {
                rm("readback")
            } else {
                w("readback", v)
            };
            // Legacy feedback stays until the fake kernel (or this restore) acknowledges it.
            excursion(
                &mut r,
                &mut pending,
                &mut ops,
                i + 4,
                vec![set],
                vec![w("readback", normal)],
            );
        }
        if r.chance(5) {
            let v = *r.pick(&[
                "ignore",
                "eio",
                "cmd-eio",
                "cmd-ignore",
                "bcct-eio",
                "bcct-ignore",
                "bcct-ignore",
            ]);
            excursion(
                &mut r,
                &mut pending,
                &mut ops,
                i,
                vec![w("fault", v)],
                vec![w("fault", "ok")],
            );
        }
        // BCCT tampering, restored later.
        if r.chance(5) {
            let k = r.below(13);
            excursion(&mut r, &mut pending, &mut ops, i, tamper(k), undo(k));
        }
        // Status file tampering (seen by the next restart).
        if r.chance(2) {
            let stamp = (now as i64 - 5).to_string();
            let crafted = format!(
                r#"{{"schema":1,"backend":"bcct","boot_id":"boot-a","updated_monotonic":{stamp},"level":55,"zcv_mv":3876,"paused":false,"valid":true,"enabled":true,"limit":80,"error":null,"activity":1234,"activity_changed_monotonic":{stamp},"migration_attempted":true}}"#
            );
            ops.push(rm("status.json")); // also a directory left by case 3
            match r.below(5) {
                0 => ops.push(w("status.json", "{garbage")),
                1 => ops.push(w("status.json", &crafted)),
                2 => ops.push(w("status.json", &crafted.replace("boot-a", "boot-b"))),
                3 => excursion(
                    &mut r,
                    &mut pending,
                    &mut ops,
                    i,
                    vec![json!(["mkdir", "status.json"])],
                    vec![rm("status.json")],
                ),
                _ => {}
            }
        }
        // Drain additions use their own generator, so the rest of the script stays as before.
        if d.chance(12) {
            drain_ops(&mut d, &mut pending, &mut ops, i, now as i64 - base, base);
        }
        if d.chance(3) {
            let v = *d.pick(&["3330", "3331", "3600", "3699"]);
            excursion(
                &mut d,
                &mut pending,
                &mut ops,
                i,
                vec![w("battery/batt_vol", v)],
                vec![w("battery/batt_vol", "4100")],
            );
        }
        if d.chance(2) {
            let v = *d.pick(&["400", "399"]);
            excursion(
                &mut d,
                &mut pending,
                &mut ops,
                i,
                vec![w("battery/batt_temp", v)],
                vec![w("battery/batt_temp", "250")],
            );
        }
        if r.chance(1) {
            ops.push(w("boot_id", r.pick(&["boot-a", "boot-b", "", "boot-a"])));
        }
        let action = if closed || r.chance(3) {
            closed = false;
            "restart"
        } else if r.chance(2) {
            closed = true;
            "close"
        } else {
            "tick"
        };
        steps.push(json!({"now": now, "ops": ops, "action": action}));
    }
    steps
}

/// A drain request (mostly valid, 0600) or one of its faults; the guard removes what it does not honour.
fn drain_ops(
    r: &mut Rng,
    pending: &mut Vec<Pending>,
    ops: &mut Vec<Value>,
    i: usize,
    up: i64,
    base: i64,
) {
    let req = |text: String| {
        vec![
            w("drain-request", &text),
            json!(["chmod", "drain-request", "600"]),
        ]
    };
    ops.push(rm("drain-request")); // also a directory or symlink left by an earlier case
    match r.below(22) {
        0..=6 => ops.extend(req(format!(
            "boot-a {}\n",
            up + *r.pick(&[600i64, 3600, 20000, 43000])
        ))),
        7 => ops.extend(req(format!("boot-a {}\n", up + 90))), // expires within two ticks
        8 => ops.extend(req(format!("boot-a {}", up + 3600))), // no newline: valid
        9 => ops.extend(req(format!("boot-a {}\n", up + 43201))),
        10 => ops.extend(req(format!("boot-b {}\n", up + 3600))),
        11 => ops.extend(req(format!("boot-a {}\n", up - 5))),
        12 => ops.extend(req(r
            .pick(&[
                "boot-a 12.5\n",
                "boot-a  123\n",
                "",
                "boot-a 1234567890\n",
                "boot-a\n",
                "caf\u{e9} 99\n",
            ])
            .to_string())),
        13 => ops.extend(req(format!("boot-a {}\n{}", up + 3600, "x".repeat(130)))),
        14 => {
            ops.push(w("drain-request", &format!("boot-a {}\n", up + 3600)));
            ops.push(json!(["chmod", "drain-request", "644"]));
        }
        15 => {
            excursion(
                r,
                pending,
                ops,
                i,
                vec![json!(["mkdir", "drain-request"])],
                vec![rm("drain-request")],
            );
        }
        16 => {
            ops.extend(req(format!("boot-a {}\n", up + 3600)));
            ops.push(w("real-request", &format!("boot-a {}\n", up + 3600)));
            ops.push(json!(["chmod", "real-request", "600"]));
            ops.push(json!(["symlink", "drain-request", "real-request"]));
        }
        17 => excursion(
            r,
            pending,
            ops,
            i,
            vec![w("uptime", "fail")],
            vec![w("uptime", &(-base).to_string())],
        ),
        _ => {} // only the removal above: the Mac's abort
    }
}

fn tamper(k: usize) -> Vec<Value> {
    match k {
        0 => vec![w("clbcct", &render_text(0, &[70, -1, 0], &[0, 0, 0], None))],
        1 => vec![w("thermal/cooling_device3/cur_state", "1\n")],
        2 => vec![w(
            "clbcct",
            &render_text(0, &[0, -1, 0], &[0, 0, 0], None).replace("klog 0", "klog 2"),
        )],
        3 => vec![rm("clbcct")],
        4 => vec![
            json!(["mkdir", "thermal/cooling_device7"]),
            w("thermal/cooling_device7/type", "mtk-cl-bcct00\n"),
        ],
        5 => vec![json!([
            "symlink",
            "thermal/thermal_zone0/cdev5",
            "../cooling_device3"
        ])],
        6 => vec![
            w(
                "clbcct",
                &render_text(1, &[1, 500, 0], &[1, 1, 0], Some(65535)),
            ),
            w("thermal/cooling_device3/cur_state", "1\n"),
        ],
        7 => vec![rm("thermal/cooling_device0/type")],
        8 => vec![w("thermal/cooling_device0/type", "caf\u{e9}\n")],
        9 => vec![w("thermal/cooling_device3/max_state", "2\n")],
        10 => vec![rm("thermal/cooling_device3/cur_state")],
        11 => vec![w(
            "clbcct",
            &render_text(1, &[-1, 500, 300], &[0, 1, 1], None),
        )],
        _ => vec![
            w("clbcct", &render_text(0, &[1, -1, 0], &[1, 0, 0], None)),
            w("thermal/cooling_device3/cur_state", "1\n"),
        ],
    }
}

fn undo(k: usize) -> Vec<Value> {
    let good = |l0: i64| {
        vec![
            w("clbcct", &render_text(0, &[l0, -1, 0], &[0, 0, 0], None)),
            w("thermal/cooling_device3/cur_state", "0\n"),
        ]
    };
    match k {
        1 | 10 => vec![w("thermal/cooling_device3/cur_state", "0\n")],
        4 => vec![
            rm("thermal/cooling_device7/type"),
            json!(["rmdir", "thermal/cooling_device7"]),
        ],
        5 => vec![rm("thermal/thermal_zone0/cdev5")],
        7 | 8 => vec![w("thermal/cooling_device0/type", "other\n")],
        9 => vec![w("thermal/cooling_device3/max_state", "1\n")],
        11 | 12 => good(1),
        _ => good(0),
    }
}

/// tempfile names are random: ".i14-charge-" + 8 characters.
fn mask(text: &str) -> String {
    let key = ".i14-charge-";
    let mut out = String::new();
    let mut rest = text;
    while let Some(i) = rest.find(key) {
        out.push_str(&rest[..i + key.len()]);
        rest = &rest[i + key.len()..];
        let n = rest
            .char_indices()
            .take_while(|(_, c)| c.is_ascii_lowercase() || c.is_ascii_digit() || *c == '_')
            .count();
        if n == 8 {
            out.push_str("XXXXXXXX");
            rest = &rest[8..];
        }
    }
    out.push_str(rest);
    out
}

fn normalize(v: &Value) -> Value {
    let mut v = v.clone();
    if let Some(s) = v["status"].as_str() {
        v["status"] = Value::String(mask(s));
    }
    let lines: Vec<Value> = v["log"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| {
            let l = l.as_str().unwrap();
            let b = l.as_bytes();
            let stamp = b.len() > 20
                && b[4] == b'-'
                && b[7] == b'-'
                && b[10] == b' '
                && b[13] == b':'
                && b[16] == b':'
                && b[19] == b' ';
            assert!(stamp, "log line without '%F %T ' timestamp: {l:?}");
            Value::String(mask(&l[20..]))
        })
        .collect();
    v["log"] = Value::Array(lines);
    v
}

fn compare(seed: u64, n: usize) -> Vec<Value> {
    compare_steps(seed, script(seed, n))
}

fn compare_steps(seed: u64, steps: Vec<Value>) -> Vec<Value> {
    let tmp = TempDir::new();
    let t = tmp.0.join("tree");
    let steps_file = tmp.0.join("steps.json");
    write(&steps_file, &serde_json::to_string(&steps).unwrap());
    init_tree(&t);
    let py = run_python(&t, &steps_file, &tmp.0.join("py.json"));
    init_tree(&t);
    let rs = run_rust(&t, &steps);
    assert_eq!(py.len(), steps.len());
    for (i, (p, r)) in py.iter().zip(&rs).enumerate() {
        let (p, r) = (normalize(p), normalize(r));
        if p != r {
            let lo = i.saturating_sub(3);
            let context: Vec<&Value> = steps[lo..=i].iter().collect();
            panic!(
                "seed {seed} step {i} differs\nsteps {lo}..={i}: {}\npython: {}\nrust:   {}",
                serde_json::to_string_pretty(&context).unwrap(),
                serde_json::to_string_pretty(&p).unwrap(),
                serde_json::to_string_pretty(&r).unwrap()
            );
        }
    }
    py.iter().map(normalize).collect()
}

#[test]
fn rust_guard_matches_python_guard() {
    let mut all = Vec::new();
    let seeds = [
        0x9e37_79b9_7f4a_7c15u64,
        0x1234_5678_9abc_def1,
        0x0dd_ba11_cafe_f00d,
        0x5eed_0000_0000_0042,
    ];
    for seed in seeds {
        all.extend(compare(seed, 600));
    }
    // The script must actually reach the interesting paths.
    let text: Vec<String> = all
        .iter()
        .map(|v| {
            let log: Vec<&str> = v["log"]
                .as_array()
                .unwrap()
                .iter()
                .map(|l| l.as_str().unwrap())
                .collect();
            format!(
                "{}\n{}\n{}",
                v["status"].as_str().unwrap_or(""),
                log.join("\n"),
                v["writes"]
            )
        })
        .collect();
    let count = |needle: &str| text.iter().filter(|t| t.contains(needle)).count();
    let paused = all
        .iter()
        .filter(|v| {
            v["status"]
                .as_str()
                .is_some_and(|s| s.contains("\"paused\": true"))
        })
        .count();
    let low = all.iter().filter(|v| v["ret"] == true).count();
    let writes = all
        .iter()
        .map(|v| v["writes"].as_array().unwrap().len())
        .sum::<usize>();
    println!(
        "steps {} paused {paused} low-battery {low} kernel writes {writes}",
        all.len()
    );
    let needles = [
        "BCCT slot 00 missing or ambiguous",
        "BCCT slot 00 belongs to a thermal zone",
        "BCCT slot 00 is occupied",
        "BCCT state readback disagrees",
        "BCCT pause is not effective",
        "unexpected BCCT max_state",
        "bad BCCT configuration",
        "BCCT configuration write did not read back",
        "BCCT state write did not read back",
        "charger feedback unavailable",
        "legacy migration readback failed",
        "outside i14-power",
        "release failed",
        "[Errno 2] No such file or directory",
        "[Errno 5] Input/output error",
        "'ascii' codec can't decode byte 0xc3",
        "ERROR charge status unavailable",
        "\"stopped\": true",
        "\"migration_attempted\": true",
        "\"level\": 55",
        "1 1 500 300",
        "\"valid\": true",
        "\"drain\": true",
        "drain mode on: BCCT stays paused until uptime",
        "drain mode ended: invalid reading",
        "drain request rejected: malformed",
        "drain request rejected: other boot",
        "drain request rejected: too long",
        "drain request rejected: expired",
        "drain request rejected: group or other access",
        "rejected: not a regular file; removal failed (errno",
        "rejected: unreadable (errno",
    ];
    for needle in needles {
        let n = count(needle);
        println!("{n:5}  {needle}");
        assert!(n > 0, "script never reached: {needle}");
    }
    assert!(paused > 50 && low > 0 && writes > 50);
}

#[test]
fn mask_only_hides_temp_names() {
    assert_eq!(
        mask("x '/a/.i14-charge-ab_9zz0q' -> '/a/s'"),
        "x '/a/.i14-charge-XXXXXXXX' -> '/a/s'"
    );
    assert_eq!(mask(".i14-charge-ab"), ".i14-charge-ab");
}

/// A calm drain run: live gauge, a valid request most of the time, the battery falling from about 4100 mV
/// to the reserve and the low-battery rule, with the events that must end or survive drain mode.
fn drain_script(seed: u64, n: usize) -> Vec<Value> {
    let mut r = Rng(seed);
    let mut now = 5000.25;
    let base = 4000i64; // uptime = now - base
    let (mut zcv, mut activity, mut cap) = (4100i64, -900i64, 80i64);
    let mut pending: Vec<Pending> = Vec::new();
    let req = |up: i64, ahead: i64| {
        vec![
            rm("drain-request"),
            w("drain-request", &format!("boot-a {}\n", up + ahead)),
            json!(["chmod", "drain-request", "600"]),
        ]
    };
    let mut steps =
        vec![json!({"now": now, "ops": [w("uptime", &(-base).to_string())], "action": "restart"})];
    let (mut closed, mut frozen, mut force_restart) = (false, 0usize, false);
    for i in 0..n {
        let mut ops: Vec<Value> = Vec::new();
        let (due, rest): (Vec<Pending>, Vec<Pending>) = pending.drain(..).partition(|p| p.due <= i);
        pending = rest;
        for p in due {
            ops.extend(p.ops);
        }
        now += *r.pick(&[
            60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 60.0, 30.0, 61.0, 0.0, 120.0,
        ]);
        let up = now as i64 - base;
        if frozen > 0 {
            frozen -= 1; // gauge, current and CAR unchanged: the stale rule
        } else {
            zcv = (zcv - r.below(8) as i64 + 1).clamp(3300, 4200);
            if r.chance(4) {
                zcv = *r.pick(&[4109, 4003, 3712, 3713, 3700, 3500, 3420]);
            }
            activity = (activity - 1 - r.below(50) as i64).max(-99999);
        }
        cap = (cap - (r.below(3) as i64)).max(0);
        if r.chance(3) {
            cap = *r.pick(&[80, 16, 15, 7, 6, 5, 4, 3]);
        }
        ops.push(w("gauge", &zcv.to_string()));
        ops.push(w("activity", &activity.to_string()));
        ops.push(w("battery/capacity", &cap.to_string()));
        ops.push(w(
            "battery/batt_vol",
            &(zcv - 40 + r.below(30) as i64)
                .clamp(2500, 5000)
                .to_string(),
        ));
        ops.push(w(
            "battery/status",
            r.pick(&[
                "Discharging",
                "Discharging",
                "Cmd discharging",
                "Not charging",
                "Charging",
            ]),
        ));
        if i == 2 || r.chance(8) {
            ops.extend(req(up, *r.pick(&[3600i64, 20000, 43000, 43200])));
        }
        let (pd, o) = (&mut pending, &mut ops);
        match r.below(44) {
            0 => o.push(rm("drain-request")),
            1 => o.extend(req(up, 100)), // reaches its deadline two ticks later
            2 => excursion(&mut r, pd, o, i, vec![w("uptime", "fail")], vec![w("uptime", &(-base).to_string())]),
            3 => excursion(&mut r, pd, o, i, vec![w("battery/batt_temp", "400")], vec![w("battery/batt_temp", "250")]),
            4 => excursion(&mut r, pd, o, i, vec![w("battery/batt_temp", "399")], vec![w("battery/batt_temp", "250")]),
            5 => o.push(w("battery/batt_vol", r.pick(&["3330", "3331", "3699"]))),
            6 => excursion(&mut r, pd, o, i, vec![w("gauge", "garbage")], vec![w("gauge", "3700")]),
            7 => excursion(&mut r, pd, o, i, vec![w("fault", "bcct-eio")], vec![w("fault", "ok")]),
            8 => excursion(&mut r, pd, o, i, vec![w("usb", "0")], vec![w("usb", "1")]),
            9 => excursion(&mut r, pd, o, i, vec![w("type", "0")], vec![w("type", "2")]),
            10 => o.push(w("ui.json", &json!({"charge_limit_enabled": true, "charge_limit": *r.pick(&[60i64, 70, 90, 100])}).to_string())),
            11 => excursion(
                &mut r, pd, o, i,
                vec![w("ui.json", r#"{"charge_limit_enabled":false,"charge_limit":80}"#)],
                vec![w("ui.json", r#"{"charge_limit_enabled":true,"charge_limit":80}"#)],
            ),
            12 => excursion(&mut r, pd, o, i, vec![w("activity", "abc")], vec![]),
            13 => frozen = 4,
            14 => o.push(json!(["chmod", "drain-request", "644"])),
            15 => {
                // a guard restart that reads another boot id (restored, with a restart, a few steps later)
                force_restart = true;
                excursion(&mut r, pd, o, i, vec![w("boot_id", "boot-b")], vec![w("boot_id", "boot-a")]);
            }
            _ => {}
        }
        let restore_boot = ops.iter().any(|op| op == &w("boot_id", "boot-a"));
        let action = if closed || force_restart || restore_boot || r.chance(4) {
            closed = false;
            force_restart = false;
            "restart" // supervise-daemon respawn: the new guard re-reads the request
        } else if r.chance(1) {
            closed = true;
            "close"
        } else {
            "tick"
        };
        steps.push(json!({"now": now, "ops": ops, "action": action}));
    }
    steps
}

#[test]
fn rust_guard_matches_python_guard_in_drain_mode() {
    let mut all = Vec::new();
    for seed in [
        0x0d4a_1e55_0000_0001u64,
        0x7777_aaaa_5555_3333,
        0x1357_9bdf_2468_ace0,
    ] {
        all.extend(compare_steps(seed, drain_script(seed, 500)));
    }
    let text: Vec<String> = all
        .iter()
        .map(|v| format!("{}\n{}", v["status"].as_str().unwrap_or(""), v["log"]))
        .collect();
    let count = |needle: &str| text.iter().filter(|t| t.contains(needle)).count();
    // Drain mode held the pause where the normal rules would charge: reserve (invalid) and gauge <= 15.
    let held = |f: &dyn Fn(&Value) -> bool| {
        all.iter()
            .filter(|v| {
                v["status"]
                    .as_str()
                    .and_then(|s| serde_json::from_str::<Value>(s).ok())
                    .is_some_and(|s| s["drain"] == true && s["paused"] == true && f(&s))
            })
            .count()
    };
    let reserve = held(&|s| s["valid"] == false);
    let low_gauge = held(&|s| s["raw_level"].as_i64().is_some_and(|g| g <= 15));
    let low_in_drain = all
        .iter()
        .filter(|v| {
            v["ret"] == true
                && v["status"]
                    .as_str()
                    .is_some_and(|s| s.contains("\"drain\": true"))
        })
        .count();
    println!(
        "steps {} reserve-held {reserve} low-gauge-held {low_gauge} low-in-drain {low_in_drain}",
        all.len()
    );
    for needle in [
        "drain mode on: BCCT stays paused until uptime",
        "drain mode ended: request removed",
        "drain mode ended: deadline reached",
        "drain mode ended: invalid reading",
        "drain mode ended: voltage 3330 mV at or below 3330 mV",
        "drain mode ended: battery temperature 40.0 C",
        "drain mode ended: charge control error",
        "drain mode ended: clock unavailable",
        "drain mode ended: group or other access",
        "drain request rejected: other boot",
        "stale gauge activity",
    ] {
        let n = count(needle);
        println!("{n:5}  {needle}");
        assert!(n > 0, "drain script never reached: {needle}");
    }
    assert!(reserve > 10 && low_gauge > 10 && low_in_drain > 0);
}
