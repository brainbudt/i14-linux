//! Port of tests/test_i14_netwatch.py: the same scenarios and assertions, with an in-memory
//! fake world that answers like the Python test's command stubs.
use i14_netwatch::{run, Config, Out, World};
use std::collections::{BTreeMap, HashMap};

const CLIENT: u32 = 4242; // the stand-in DHCP client
type Hook = Box<dyn Fn(u32, &mut Net)>;

/// Fake world: `files` hold what wpa_cli/ip/ping answer; the vectors record what the watchdog did.
struct Net {
    files: HashMap<String, String>,
    checks: u32,
    n: u32,
    hook: Option<Hook>,
    rec: BTreeMap<&'static str, Vec<String>>,
}

#[derive(Default)]
struct Opt {
    state: Option<&'static str>,
    addr: Option<bool>,
    route: Option<bool>,
    ping: Option<bool>,
    dns: Option<bool>, // None: unbound not in the default runlevel
    off: bool,
}

impl Net {
    fn new(checks: u32, o: Opt) -> Net {
        let mut f = HashMap::new();
        let mut put = |k: &str, v: &str| f.insert(k.to_string(), v.to_string());
        put("state", o.state.unwrap_or("COMPLETED"));
        put(
            "addr",
            if o.addr.unwrap_or(true) {
                " inet 192.0.2.167/24"
            } else {
                ""
            },
        );
        put(
            "route",
            if o.route.unwrap_or(true) {
                "default via 192.0.2.1 dev wlan0"
            } else {
                ""
            },
        );
        put("ping", if o.ping.unwrap_or(true) { "0" } else { "1" });
        put("dns", if o.dns.unwrap_or(true) { "ok" } else { "dead" });
        if o.dns.is_some() {
            put("unbound.want", "");
        }
        if o.off {
            put("wifi-off", "off");
        }
        put("pid", &format!("{CLIENT}\n"));
        let rec = ["log", "wpa", "renew", "udhcpc", "rc"]
            .into_iter()
            .map(|k| (k, vec![]))
            .collect();
        Net {
            files: f,
            checks,
            n: 0,
            hook: None,
            rec,
        }
    }

    fn put(&mut self, k: &str, v: &str) {
        self.files.insert(k.into(), v.into());
    }

    fn get(&self, k: &str) -> &str {
        self.files.get(k).map(String::as_str).unwrap_or("")
    }

    fn run(mut self) -> BTreeMap<&'static str, Vec<String>> {
        let c = Config::from_lookup(|k| {
            Some(
                match k {
                    "I14_UDHCPC_PID" => "pid",
                    "I14_NETWATCH_S" => "0",
                    "I14_UNBOUND_WANT" => "unbound.want",
                    "I14_WIFI_OFF" => "wifi-off",
                    _ => return None,
                }
                .to_string(),
            )
        });
        run(&c, &mut self);
        self.rec
    }
}

impl World for Net {
    fn sleep(&mut self, step: &str) -> bool {
        assert_eq!(step, "0");
        self.n += 1;
        if self.n > self.checks {
            return false;
        }
        if let Some(h) = self.hook.take() {
            h(self.n, self);
            self.hook = Some(h);
        }
        true
    }

    fn run(&mut self, argv: &[&str], _out: Out) -> (bool, String) {
        let args = argv[1..].join(" ");
        let rec = |s: &mut Net, k| s.rec.get_mut(k).unwrap().push(args.clone());
        match argv[0] {
            "wpa_cli" if args.ends_with("status") => {
                return (true, format!("wpa_state={}\n", self.get("state")))
            }
            "wpa_cli" => rec(self, "wpa"),
            "ip" if args.contains("route") => return (true, format!("{}\n", self.get("route"))),
            "ip" if args.contains("addr") => return (true, format!("{}\n", self.get("addr"))),
            "ping" => return (self.get("ping") == "0", String::new()),
            "logger" => rec(self, "log"),
            "udhcpc" => rec(self, "udhcpc"),
            "nslookup" if self.get("dns") == "ok" => return (true, "Address: 192.0.2.1\n".into()),
            "nslookup" => return (false, String::new()),
            "rc-service" => rec(self, "rc"),
            other => panic!("unexpected command {other}"),
        }
        (true, String::new())
    }

    fn exists(&self, path: &str) -> bool {
        self.files.contains_key(path)
    }

    fn read(&self, path: &str) -> Option<String> {
        self.files.get(path).cloned()
    }

    fn kill(&mut self, pid: u32, sig: i32) -> bool {
        assert_ne!(pid, 0, "kill 0 would signal netwatch's own process group");
        if pid != CLIENT {
            return false;
        }
        if sig == libc::SIGUSR1 {
            self.rec.get_mut("renew").unwrap().push("renew".into());
        }
        true
    }
}

fn watch(checks: u32, o: Opt) -> BTreeMap<&'static str, Vec<String>> {
    Net::new(checks, o).run()
}

fn watch_hook(
    checks: u32,
    o: Opt,
    hook: impl Fn(u32, &mut Net) + 'static,
) -> BTreeMap<&'static str, Vec<String>> {
    let mut n = Net::new(checks, o);
    n.hook = Some(Box::new(hook));
    n.run()
}

fn empty() -> Vec<String> {
    vec![]
}

#[test]
fn wifi_switched_off_is_left_alone_but_dns_is_still_watched() {
    let r = watch(
        15,
        Opt {
            state: Some("DISCONNECTED"),
            addr: Some(false),
            dns: Some(false),
            off: true,
            ..Default::default()
        },
    );
    assert_eq!(r["wpa"], empty());
    assert_eq!(r["renew"], empty());
    assert_eq!(r["udhcpc"], empty());
    assert!(r["rc"].join(" ").contains("restart"));
    assert!(r["log"]
        .contains(&"-t i14-netwatch Wi-Fi switched off by the user: not checking it".to_string()));
}

#[test]
fn all_well_does_nothing() {
    let r = watch(5, Opt::default());
    assert!(r.values().all(Vec::is_empty), "{r:?}");
    assert_eq!(
        watch(
            5,
            Opt {
                dns: Some(true),
                ..Default::default()
            }
        )["rc"],
        empty()
    );
}

#[test]
fn silent_dns_server_is_restarted_once_then_every_ten_checks() {
    let r = watch(
        6,
        Opt {
            dns: Some(false),
            ..Default::default()
        },
    );
    assert_eq!(r["rc"], ["unbound restart"]); // at the third failure, not again at 4..6
    assert_eq!(
        watch(
            14,
            Opt {
                dns: Some(false),
                ..Default::default()
            }
        )["rc"],
        ["unbound restart"; 2]
    ); // 3 and 13
}

#[test]
fn a_failed_restart_is_retried_until_dns_answers() {
    // restart at 3 failed, at 13 too
    let r = watch_hook(
        18,
        Opt {
            dns: Some(false),
            ..Default::default()
        },
        |n, s| {
            if n == 15 {
                s.put("dns", "ok");
            }
        },
    );
    assert_eq!(r["rc"], ["unbound restart"; 2]);
    assert!(r["log"]
        .last()
        .unwrap()
        .contains("DNS server answers again"));
    assert!(r["log"][0].contains("DNS server does not answer"));
    assert_eq!(r["wpa"], empty()); // Wi-Fi is fine: nothing else touched
}

#[test]
fn no_dns_check_without_unbound() {
    assert_eq!(
        watch(
            4,
            Opt {
                dns: None,
                ..Default::default()
            }
        )["rc"],
        empty()
    );
}

#[test]
fn not_associated_reassociates_after_three_checks_then_every_ten() {
    let r = watch(
        14,
        Opt {
            state: Some("SCANNING"),
            ..Default::default()
        },
    );
    assert_eq!(r["wpa"], ["-i wlan0 reassociate"; 2]); // checks 3 and 13
    assert_eq!(r["renew"], empty());
    assert_eq!(
        r["log"][0],
        "-t i14-netwatch problem: not associated (SCANNING)"
    );
}

#[test]
fn two_bad_checks_do_nothing() {
    let r = watch(
        2,
        Opt {
            state: Some("SCANNING"),
            ..Default::default()
        },
    );
    assert_eq!((&r["wpa"], &r["renew"]), (&empty(), &empty()));
}

#[test]
fn no_address_renews_the_lease() {
    let r = watch(
        3,
        Opt {
            addr: Some(false),
            ..Default::default()
        },
    );
    assert_eq!(r["renew"], ["renew"]);
    assert_eq!(r["wpa"], empty());
}

#[test]
fn no_route_renews_the_lease() {
    assert_eq!(
        watch(
            3,
            Opt {
                route: Some(false),
                ..Default::default()
            }
        )["renew"],
        ["renew"]
    );
}

#[test]
fn silent_gateway_reassociates_and_renews() {
    let r = watch(
        3,
        Opt {
            ping: Some(false),
            ..Default::default()
        },
    );
    assert_eq!(r["wpa"], ["-i wlan0 reassociate"]);
    assert_eq!(r["renew"], ["renew"]);
}

#[test]
fn recovery_is_logged_and_resets_the_count() {
    let r = watch_hook(
        6,
        Opt {
            state: Some("SCANNING"),
            ..Default::default()
        },
        |n, s| {
            if n == 3 {
                s.put("state", "COMPLETED");
            }
        },
    );
    assert_eq!(r["wpa"], empty()); // back before the third bad check was acted on
    assert!(r["log"].last().unwrap().contains("Wi-Fi works again"));
}

#[test]
fn dead_dhcp_client_is_started_again() {
    let mut net = Net::new(1, Opt::default());
    net.put("pid", "999999\n");
    let r = net.run();
    assert_eq!(r["udhcpc"], ["-b -i wlan0 -p pid -x hostname:i14"]);
    assert!(r["log"][0].contains("DHCP client not running"));
}

#[test]
fn pid_zero_counts_as_no_client() {
    // The shell would run `kill -0 0` (true: our own group) and later `kill -USR1 0`.
    let mut net = Net::new(1, Opt::default());
    net.put("pid", "0\n");
    let r = net.run();
    assert_eq!(r["udhcpc"], ["-b -i wlan0 -p pid -x hostname:i14"]);
}

#[test]
fn never_reloads_the_driver_or_reboots() {
    let mut code = String::new();
    for f in ["src/lib.rs", "src/main.rs"] {
        let text =
            std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/").to_string() + f)
                .unwrap();
        for l in text.lines().filter(|l| !l.trim_start().starts_with("//")) {
            code.push_str(l);
            code.push('\n');
        }
    }
    for word in ["wmt_loader", "reboot", "wmtWifi"] {
        assert!(!code.contains(word), "{word}");
    }
    let rc: Vec<&str> = code
        .lines()
        .filter(|l| l.contains("\"rc-service\""))
        .collect();
    assert_eq!(rc.len(), 1);
    assert!(
        rc[0].contains(r#"&["rc-service", "unbound", "restart"]"#),
        "{rc:?}"
    );
}
