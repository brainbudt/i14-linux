//! Differential test: the ORIGINAL shell script (run with `sh`) and the Rust policy (through the
//! real `Sys` runner) go through the same long scenarios against the same command stubs. Every
//! stub appends its argv to `trace`; the traces, the files left behind and the number of SIGUSR1
//! renew requests the stand-in DHCP clients saw must be identical.
use i14_netwatch::{Config, Out, Sys, World};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::time::Duration;

const SCRIPT: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../../rootfs/overlay/usr/sbin/i14-netwatch"
);

struct Scenario {
    name: &'static str,
    checks: u32,
    iface: Option<&'static str>,
    /// Initial files on top of the healthy defaults (None: remove).
    init: Vec<(&'static str, Option<&'static str>)>,
    /// (check number, file, content): written just before that check (None: remove).
    events: Vec<(u32, &'static str, Option<&'static str>)>,
}

/// A stand-in DHCP client that appends "renew" on SIGUSR1; it touches ready/<pid> once its trap is set.
fn client_cmd(d: &Path) -> String {
    format!(
        r#"trap "echo renew >>{d}/renew" USR1; touch {d}/ready/$$; while :; do /bin/sleep 0.05; done"#,
        d = d.display()
    )
}

fn stubs(d: &Path, checks: u32) -> Vec<(&'static str, String)> {
    let d = d.display();
    let cc = client_cmd(Path::new(&d.to_string()))
        .replace('"', "\\\"")
        .replace('$', "\\$");
    vec![
        ("sleep", format!("echo \"sleep $*\" >>{d}/trace\nn=$(cat {d}/n 2>/dev/null || echo 0); n=$((n + 1)); echo $n >{d}/n\n[ $n -le {checks} ] || exit 1\n. {d}/hook\nexit 0")),
        ("wpa_cli", format!("echo \"wpa_cli $*\" >>{d}/trace\ncase \"$*\" in *status)\n\t[ -e {d}/state ] || {{ echo 'Failed to connect to non-global ctrl_ifname' >&2; exit 1; }}\n\techo bssid=00:11:22:33:44:55; echo ssid=home; echo \"wpa_state=$(cat {d}/state)\"; echo ip_address=192.0.2.167 ;;\nesac")),
        ("ip", format!("echo \"ip $*\" >>{d}/trace\ncase \"$*\" in *route*) cat {d}/route ;; *addr*) cat {d}/addr ;; esac")),
        ("ping", format!("echo \"ping $*\" >>{d}/trace\nexit $(cat {d}/ping)")),
        ("logger", format!("echo \"logger $*\" >>{d}/trace")),
        ("rc-service", format!("echo \"rc-service $*\" >>{d}/trace\necho restarting")),
        ("nslookup", format!("echo \"nslookup $*\" >>{d}/trace\necho 'Server:    127.0.0.1'\ncase $(cat {d}/dns) in\n\tok) echo 'Address: 192.0.2.1' ;;\n\tnear) echo 'Address: 192.0.2.10' ;;\n\tjunk) echo 'Address: 192.0.2.2' ;;\n\t*) echo \"** server can't find i14-health.invalid\" >&2; exit 1 ;;\nesac")),
        ("udhcpc", format!("echo \"udhcpc $*\" >>{d}/trace\n[ \"$(cat {d}/udhcpc.mode 2>/dev/null)\" = start ] || exit 1\npf=; prev=; for a; do [ \"$prev\" = -p ] && pf=$a; prev=$a; done\nsh -c \"{cc}\" </dev/null >/dev/null 2>&1 &\nc=$!\nwhile [ ! -e {d}/ready/$c ]; do /bin/sleep 0.01; done\necho $c >>{d}/clients\necho $c >\"$pf\"\necho 'udhcpc: started, v1.36.1'")),
    ]
}

fn hook(d: &Path, events: &[(u32, &str, Option<&str>)], client: u32) -> String {
    let mut by_n: BTreeMap<u32, Vec<String>> = BTreeMap::new();
    for &(n, f, v) in events {
        let p = d.join(f);
        let cmd = match v {
            None => format!("rm -f {}", p.display()),
            Some(v) => {
                let v = v
                    .replace("@client", &client.to_string())
                    .replace("@dead", "999999");
                if v.is_empty() {
                    format!(": >{}", p.display())
                } else {
                    format!("printf '%s\\n' '{v}' >{}", p.display())
                }
            }
        };
        by_n.entry(n).or_default().push(cmd);
    }
    let mut h = String::from("case $n in\n");
    for (n, cmds) in by_n {
        h += &format!("{n}) {} ;;\n", cmds.join("; "));
    }
    h + "esac\n"
}

struct Run {
    dir: PathBuf,
    client: Child,
}

fn setup(root: &Path, tag: &str, s: &Scenario) -> Run {
    let d = root.join(format!("{}-{tag}", s.name));
    fs::create_dir_all(d.join("bin")).unwrap();
    fs::create_dir_all(d.join("ready")).unwrap();
    for (name, body) in stubs(&d, s.checks) {
        let p = d.join("bin").join(name);
        fs::write(&p, format!("#!/bin/sh\n{body}\n")).unwrap();
        Command::new("chmod").arg("755").arg(&p).status().unwrap();
    }
    let client = Command::new("sh")
        .arg("-c")
        .arg(client_cmd(&d))
        .spawn()
        .unwrap();
    while !d.join("ready").join(client.id().to_string()).exists() {
        std::thread::sleep(Duration::from_millis(5));
    }
    let mut files: BTreeMap<&str, Option<String>> = [
        ("state", "COMPLETED"),
        ("addr", "2: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP>\n    inet 192.0.2.167/24 brd 192.0.2.255 scope global wlan0"),
        ("route", "default via 192.0.2.1  metric 303"),
        ("ping", "0"),
        ("dns", "ok"),
        ("unbound.want", ""),
        ("udhcpc.mode", "start"),
        ("pid", "@client"),
    ]
    .into_iter()
    .map(|(k, v)| (k, Some(v.to_string())))
    .collect();
    for &(k, v) in &s.init {
        files.insert(k, v.map(str::to_string));
    }
    for (k, v) in files {
        if let Some(v) = v {
            let v = v
                .replace("@client", &client.id().to_string())
                .replace("@dead", "999999");
            fs::write(d.join(k), if v.is_empty() { v } else { v + "\n" }).unwrap();
        }
    }
    fs::write(d.join("hook"), hook(&d, &s.events, client.id())).unwrap();
    Run { dir: d, client }
}

fn env(d: &Path, s: &Scenario) -> Vec<(&'static str, String)> {
    let mut e = vec![
        ("I14_UDHCPC_PID", d.join("pid").display().to_string()),
        ("I14_NETWATCH_S", "0".to_string()),
        (
            "I14_UNBOUND_WANT",
            d.join("unbound.want").display().to_string(),
        ),
        ("I14_WIFI_OFF", d.join("wifi-off").display().to_string()),
    ];
    if let Some(i) = s.iface {
        e.push(("I14_WIFI_IF", i.to_string()));
    }
    e
}

/// Sys with the program looked up in the scenario's stub directory and `sleep` run as that stub.
struct Stubbed(PathBuf);

impl World for Stubbed {
    fn sleep(&mut self, step: &str) -> bool {
        self.run(&["sleep", step], Out::Quiet).0
    }
    fn run(&mut self, argv: &[&str], out: Out) -> (bool, String) {
        let p = self.0.join("bin").join(argv[0]);
        assert!(
            p.exists(),
            "Rust ran a program the shell script does not: {}",
            argv[0]
        );
        let p = p.display().to_string();
        let mut a = argv.to_vec();
        a[0] = &p;
        Sys.run(&a, out)
    }
    fn exists(&self, path: &str) -> bool {
        Sys.exists(path)
    }
    fn read(&self, path: &str) -> Option<String> {
        Sys.read(path)
    }
    fn kill(&mut self, pid: u32, sig: i32) -> bool {
        Sys.kill(pid, sig)
    }
}

/// What a run left behind, with pids replaced by stable names.
fn result(mut r: Run) -> BTreeMap<String, String> {
    std::thread::sleep(Duration::from_millis(300)); // let the clients write their renew lines
    let d = r.dir.clone();
    let clients: Vec<String> = fs::read_to_string(d.join("clients"))
        .unwrap_or_default()
        .lines()
        .map(String::from)
        .collect();
    for c in &clients {
        let _ = Sys.kill(c.parse().unwrap(), libc::SIGKILL);
    }
    let main = r.client.id().to_string();
    r.client.kill().unwrap();
    r.client.wait().unwrap();
    let norm = |s: String| {
        let mut s = s.replace(&main, "<client>");
        for (i, c) in clients.iter().enumerate() {
            s = s.replace(c, &format!("<client{i}>"));
        }
        s.replace(&d.display().to_string(), "<dir>")
    };
    let mut out = BTreeMap::new();
    for e in fs::read_dir(&d).unwrap() {
        let e = e.unwrap();
        let name = e.file_name().to_string_lossy().into_owned();
        if ["bin", "ready", "hook"].contains(&name.as_str()) {
            continue;
        }
        let text = fs::read_to_string(e.path()).unwrap();
        let v = match name.as_str() {
            "renew" => format!("{} renew requests", text.lines().count()),
            _ => norm(text),
        };
        out.insert(name, v);
    }
    out
}

fn scenarios() -> Vec<Scenario> {
    vec![
        Scenario {
            name: "link-flap",
            checks: 80,
            iface: None,
            init: vec![],
            events: vec![
                (3, "state", Some("SCANNING")),
                (5, "state", Some("COMPLETED")),
                (10, "state", Some("DISCONNECTED")),
                (26, "state", Some("ASSOCIATING")),
                (35, "state", Some("COMPLETED")),
                (40, "state", Some("SCANNING")),
                (44, "state", Some("4WAY_HANDSHAKE")),
                (47, "state", Some("COMPLETED")),
                (50, "state", Some("INACTIVE")),
                (78, "state", Some("COMPLETED")),
            ],
        },
        Scenario {
            name: "no-lease",
            checks: 70,
            iface: None,
            init: vec![(
                "addr",
                Some(
                    "2: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP>\n    inet6 fe80::1/64 scope link",
                ),
            )],
            events: vec![
                (
                    25,
                    "addr",
                    Some("    inet 192.0.2.167/24 brd 192.0.2.255 scope global wlan0"),
                ),
                (25, "route", Some("")),
                (50, "route", Some("default via 192.0.2.1")),
                (60, "ping", Some("1")),
                (66, "ping", Some("0")),
            ],
        },
        Scenario {
            name: "gateway-silent",
            checks: 60,
            iface: None,
            init: vec![("ping", Some("1"))],
            events: vec![
                (30, "ping", Some("0")),
                (40, "ping", Some("1")),
                (45, "route", Some("default via 10.0.0.1")),
                (58, "ping", Some("0")),
            ],
        },
        Scenario {
            name: "dns-failing",
            checks: 90,
            iface: None,
            init: vec![("dns", Some("dead"))],
            events: vec![
                (20, "dns", Some("ok")),
                (25, "dns", Some("dead")),
                (40, "dns", Some("near")),
                (45, "dns", Some("junk")),
                (60, "unbound.want", None),
                (70, "unbound.want", Some("")),
                (80, "dns", Some("ok")),
            ],
        },
        Scenario {
            name: "wifi-off-flag",
            checks: 80,
            iface: None,
            init: vec![
                ("wifi-off", Some("off")),
                ("state", Some("DISCONNECTED")),
                ("dns", Some("dead")),
            ],
            events: vec![
                (15, "wifi-off", None),
                (25, "wifi-off", Some("off")),
                (30, "state", Some("COMPLETED")),
                (31, "dns", Some("ok")),
                (40, "wifi-off", None),
                (50, "addr", Some("")),
                (52, "wifi-off", Some("off")),
                (55, "wifi-off", None),
                (70, "addr", Some("    inet 192.0.2.167/24")),
            ],
        },
        Scenario {
            name: "wpa-supplicant-missing",
            checks: 50,
            iface: None,
            init: vec![("state", None)],
            events: vec![
                (20, "state", Some("")),
                (30, "state", Some("COMPLETED")),
                (35, "state", None),
                (45, "state", Some("COMPLETED")),
            ],
        },
        Scenario {
            name: "dhcp-client",
            checks: 70,
            iface: None,
            init: vec![("pid", None)],
            events: vec![
                (10, "pid", Some("@dead")),
                (20, "udhcpc.mode", Some("none")),
                (20, "pid", Some("@dead")),
                (20, "addr", Some("")),
                (35, "pid", Some("")),
                (45, "pid", Some("abc")),
                (50, "udhcpc.mode", Some("start")),
                (60, "addr", Some("    inet 192.0.2.167/24")),
                (64, "ping", Some("1")),
            ],
        },
        Scenario {
            name: "mixed-long-wlan1",
            checks: 150,
            iface: Some("wlan1"),
            init: vec![("state", Some("SCANNING"))],
            events: vec![
                (12, "state", Some("COMPLETED")),
                (12, "addr", Some("")),
                (20, "addr", Some("    inet 10.1.1.5/24")),
                (20, "route", Some("")),
                (27, "route", Some("default via 10.1.1.1 dev wlan1")),
                (27, "ping", Some("1")),
                (30, "dns", Some("dead")),
                (33, "pid", Some("@dead")),
                (40, "wifi-off", Some("off")),
                (48, "wifi-off", None),
                (55, "ping", Some("0")),
                (56, "dns", Some("ok")),
                (60, "state", None),
                (75, "state", Some("SCANNING")),
                (90, "state", Some("COMPLETED")),
                (95, "dns", Some("dead")),
                (100, "unbound.want", None),
                (110, "unbound.want", Some("")),
                (120, "ping", Some("1")),
                (125, "pid", Some("@client")),
                (140, "ping", Some("0")),
                (141, "dns", Some("ok")),
            ],
        },
    ]
}

#[test]
fn shell_and_rust_do_the_same() {
    assert!(
        Path::new(SCRIPT).exists(),
        "original script not found: {SCRIPT}"
    );
    let root = std::env::temp_dir().join(format!("i14-netwatch-diff-{}", std::process::id()));
    let _ = fs::remove_dir_all(&root);
    let mut steps = 0;
    let all = scenarios();
    for s in &all {
        let sh = setup(&root, "sh", s);
        let mut path = sh.dir.join("bin").display().to_string();
        path += ":/usr/bin:/bin";
        let o = Command::new("sh")
            .arg(SCRIPT)
            .env("PATH", &path)
            .envs(env(&sh.dir, s))
            .output()
            .unwrap();
        let stderr = String::from_utf8_lossy(&o.stderr).into_owned();
        let a = result(sh);

        let rs = setup(&root, "rs", s);
        let e = env(&rs.dir, s);
        let c = Config::from_lookup(|k| e.iter().find(|(n, _)| *n == k).map(|(_, v)| v.clone()));
        i14_netwatch::run(&c, &mut Stubbed(rs.dir.clone()));
        let b = result(rs);

        assert_eq!(
            a.keys().collect::<Vec<_>>(),
            b.keys().collect::<Vec<_>>(),
            "{}: files differ",
            s.name
        );
        for k in a.keys() {
            if a[k] != b[k] {
                let (x, y): (Vec<&str>, Vec<&str>) =
                    (a[k].lines().collect(), b[k].lines().collect());
                let i = x
                    .iter()
                    .zip(&y)
                    .position(|(p, q)| p != q)
                    .unwrap_or(x.len().min(y.len()));
                panic!(
                    "{}: {k} differs at line {i}\nshell: {:?}\nrust:  {:?}\nshell stderr: {stderr}",
                    s.name,
                    &x[i.saturating_sub(3)..(i + 3).min(x.len())],
                    &y[i.saturating_sub(3)..(i + 3).min(y.len())]
                );
            }
        }
        assert_eq!(a["n"].trim(), (s.checks + 1).to_string());
        let lines = a["trace"].lines().count();
        eprintln!(
            "{}: {} checks, {} commands, {}, {} files identical",
            s.name,
            s.checks,
            lines,
            a.get("renew").map_or("0 renew requests", String::as_str),
            a.len()
        );
        steps += s.checks;
    }
    eprintln!(
        "{} scenarios, {steps} checks: shell and Rust identical",
        all.len()
    );
    let _ = fs::remove_dir_all(&root);
}
