"""Server hardening in the rootfs: log trimming, firewall rules, service supervision, build wiring."""
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
O = ROOT / "rootfs/overlay"


class LogTrim(unittest.TestCase):
    def test_trims_big_logs_to_whole_newest_lines(self):
        with tempfile.TemporaryDirectory() as d:
            big, small = pathlib.Path(d) / "i14-big.log", pathlib.Path(d) / "i14-small.log"
            lines = [f"line {i:07d} " + "x" * 50 for i in range(12000)]  # ~740 KiB
            big.write_text("\n".join(lines) + "\n")
            small.write_text("keep\n")
            env = dict(os.environ, I14_LOGS=f"{big} {small} {d}/missing.log")
            r = subprocess.run(["sh", str(O / "etc/periodic/daily/i14-logtrim")], env=env,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr)
            kept = big.read_text().splitlines()
            self.assertLessEqual(len(big.read_bytes()), 262144)
            self.assertGreater(len(big.read_bytes()), 200000)
            self.assertEqual(kept[-1], lines[-1])
            self.assertIn(kept[0], lines)  # starts at a whole line
            self.assertEqual(small.read_text(), "keep\n")
            self.assertFalse(list(pathlib.Path(d).glob("*.trim")))


class Firewall(unittest.TestCase):
    def rules(self, name):
        return [l for l in (O / "etc/i14" / name).read_text().splitlines() if l and not l.startswith("#")]

    def test_structure(self):
        for name in ("firewall.rules", "firewall6.rules"):
            r = self.rules(name)
            self.assertEqual((r[0], r[-1]), ("*filter", "COMMIT"))
            chains = [l.split()[0][1:] for l in r if l.startswith(":")]
            self.assertEqual(chains[:3], ["INPUT", "FORWARD", "OUTPUT"])
            self.assertIn(":INPUT DROP [0:0]", r)
            first_rule = next(i for i, l in enumerate(r) if l.startswith("-A"))
            self.assertTrue(all(not l.startswith(":") for l in r[first_rule:]), name)  # chains declared first
            for l in r:
                if l.startswith("-A"):
                    self.assertIn(l.split()[1], chains)
                    m = re.search(r"-j (\S+)", l)
                    self.assertTrue(m.group(1) in ("ACCEPT", "DROP") or m.group(1) in chains, l)
                self.assertNotIn("conntrack", l)  # the 3.18 kernel only has the "state" match

    def test_only_usb_dhcp_and_management_are_allowed(self):
        ipv4, ipv6 = (self.rules(name) for name in ("firewall.rules", "firewall6.rules"))
        self.assertIn("-A INPUT -i rndis0 -p udp --sport 68 --dport 67 -j ACCEPT", ipv4)
        self.assertIn("-A INPUT -i rndis0 -s 172.16.42.0/24 -p tcp --dport 22 -j ACCEPT", ipv4)
        self.assertNotIn("-A INPUT -i rndis0 -j ACCEPT", ipv4 + ipv6)
        self.assertFalse(any("--dport 22" in line or "--dport 53" in line for line in ipv6))

    def run_firewall(self, command, failure=""):
        with tempfile.TemporaryDirectory() as directory:
            d = pathlib.Path(directory)
            stub = """#!/bin/sh
name=${0##*/}
echo "$name $*" >>"$CALLS"
case "$name" in
  *-restore)
    content=$(cat)
    echo "$content" >>"$CALLS"
    case "$content" in
      *--dport*|*"-A INPUT ! -i lo -j DROP"*) [ "$name" != "$FAILURE" ] ;;
      *) [ "$FAILURE" != "isolate:$name" ] ;;
    esac
    ;;
esac
"""
            for name in ("iptables-legacy", "ip6tables-legacy", "iptables-legacy-restore",
                         "ip6tables-legacy-restore", "ip", "rc-service"):
                f = d / name
                f.write_text(stub)
                f.chmod(0o755)
            env = dict(os.environ, PATH=f"{d}:/usr/bin:/bin", CALLS=str(d / "calls"),
                       FAILURE=failure, I14_FIREWALL_RULES_DIR=str(O / "etc/i14"))
            result = subprocess.run(["sh", "-c", 'ebegin() { :; }; eend() { return "$1"; }; '
                '. "$1"; "$2"', "sh", O / "etc/init.d/i14-firewall", command],
                env=env, capture_output=True, text=True, timeout=10)
            log = d / "calls"
            return result.returncode, log.read_text() if log.exists() else ""

    def test_family_import_failures_keep_both_guards(self):
        for failure in ("iptables-legacy-restore", "ip6tables-legacy-restore"):
            with self.subTest(failure=failure):
                rc, log = self.run_firewall("start", failure)
                self.assertNotEqual(rc, 0)
                self.assertNotIn(" -D ", log)
                self.assertIn("--dport", log)
                first_candidate = log.index("--dport")
                self.assertIn("ip6tables-legacy-restore", log[:first_candidate])
                self.assertGreaterEqual(log[:first_candidate].count(":INPUT DROP"), 2)
                self.assertGreaterEqual(log[first_candidate:].count(":FORWARD DROP"), 2)

    def test_start_releases_guards_only_after_both_imports_and_stop_closes(self):
        rc, log = self.run_firewall("start")
        self.assertEqual(rc, 0)
        self.assertEqual(log.count("-A INPUT ! -i lo -j DROP"), 2)
        self.assertGreater(log.index(" -D "), log.rindex("COMMIT"))
        rc, log = self.run_firewall("stop")
        self.assertEqual(rc, 0)
        self.assertNotIn("-P INPUT ACCEPT", log)
        self.assertEqual(log.count(":INPUT DROP"), 2)

    def test_services_require_the_firewall_and_normal_boot_has_no_serial_shell(self):
        for service in ("i14-wifi", "i14-usbnet"):
            self.assertRegex((O / "etc/init.d" / service).read_text(), r"need .*i14-firewall")
        for service in ("sshd", "unbound"):
            self.assertRegex((O / "etc/conf.d" / service).read_text(), r'rc_need=.*i14-firewall')
        self.assertNotIn("ttyGS0", "\n".join(
            line for line in (O / "etc/inittab").read_text().splitlines() if not line.startswith("#")))

    def test_isolation_failure_quiesces_interfaces_and_services(self):
        rc, log = self.run_firewall("start", "isolate:ip6tables-legacy-restore")
        self.assertNotEqual(rc, 0)
        for interface in ("wlan0", "rndis0"):
            self.assertIn(f"ip link set {interface} down", log)
        for service in ("i14-netwatch", "sshd", "unbound", "i14-wifi", "i14-usbnet"):
            self.assertIn(f"rc-service -D {service} stop", log)
        self.assertNotIn("--dport", log)


class DnsClientPolicy(unittest.TestCase):
    paths = ("etc/i14/firewall.rules", "etc/unbound/unbound.conf")

    def render(self, contents):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        for path in self.paths:
            (root / path).parent.mkdir(parents=True)
            (root / path).write_bytes((O / path).read_bytes())
        clients = root / "clients"
        if contents is not None:
            clients.write_text(contents)
        result = subprocess.run([sys.executable, ROOT / "rootfs/configure-dns.py", root, clients],
                                capture_output=True, text=True)
        return result, [(root / path).read_text() for path in self.paths]

    def test_dns_sources_render_to_both_services(self):
        result, (firewall, unbound) = self.render("192.0.2.20/32 # example\n198.51.100.8/29\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        for cidr in ("192.0.2.20/32", "198.51.100.8/29"):
            self.assertIn(f"-A I14_DNS -s {cidr} -j ACCEPT", firewall)
            self.assertIn(f"access-control: {cidr} allow", unbound)

    def test_invalid_dns_clients_leave_both_templates_unchanged(self):
        for invalid in ("0.0.0.0/0", "127.0.0.0/8", "2001:db8::1/128", "192.0.2.7/24", "x; echo ignored"):
            with self.subTest(invalid=invalid):
                result, configs = self.render(invalid)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid IPv4 client network at line 1", result.stderr)
                self.assertEqual(configs, [(O / path).read_text() for path in self.paths])

    def test_empty_dns_clients_allow_only_local_queries(self):
        for contents in (None, "", "\n  # no clients\n"):
            with self.subTest(contents=contents):
                result, (firewall, unbound) = self.render(contents)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("# I14_DNS_CLIENTS", firewall + unbound)
                self.assertNotIn("-A I14_DNS ", firewall)
                self.assertEqual([line.strip() for line in unbound.splitlines()
                                  if line.strip().startswith("access-control:")],
                                 ["access-control: 0.0.0.0/0 refuse", "access-control: 127.0.0.0/8 allow"])


class Dns(unittest.TestCase):
    conf = (O / "etc/unbound/unbound.conf").read_text()

    def test_lan_only_with_dnssec_and_tls_upstreams(self):
        c = self.conf
        self.assertIn("access-control: 0.0.0.0/0 refuse", c)
        self.assertIn("auto-trust-anchor-file: \"/var/lib/unbound/root.key\"", c)
        self.assertIn('module-config: "validator iterator"', c)
        self.assertIn("forward-tls-upstream: yes", c)
        addrs = re.findall(r"forward-addr: (\S+)", c)
        self.assertTrue(addrs and all("@853#" in a for a in addrs))  # never plain port 53 (intercepted)
        self.assertIn("do-daemonize: no", c)  # supervise-daemon needs the foreground
        self.assertIn('local-data: "i14-health.invalid. 60 IN A 192.0.2.1"', c)

    def test_supervised(self):
        self.assertIn("supervisor=supervise-daemon", (O / "etc/conf.d/unbound").read_text())


class WifiMac(unittest.TestCase):
    def test_nvdata_is_mounted_read_only_before_the_chip_powers_on(self):
        up = (O / "usr/sbin/i14-wifi-up").read_text()
        mount = up.index("mount -t ext4 -o ro,noload \"$(part nvdata)\" /nvdata")
        self.assertLess(mount, up.index("echo 1 >/dev/wmtWifi"))
        self.assertNotIn("rw", up[mount:mount + 60])

    def test_only_the_nvram_path_is_mapped(self):
        b = (ROOT / "rootfs/build-alpine-rootfs.sh").read_text()
        self.assertIn('ln -s /nvdata "$R/data/nvram"', b)
        self.assertEqual(b.count('"$R/data/'), 1)  # no other /data mapping


class NoAndroid(unittest.TestCase):
    """Android is gone: the Wi-Fi tools live in the rootfs, the Wi-Fi networks on userdata."""
    LIST = ROOT / "rootfs/android-wifi.sha256"

    def test_wifi_tools_come_from_the_rootfs_not_the_system_partition(self):
        up = (O / "usr/sbin/i14-wifi-up").read_text()
        self.assertNotIn("part system", up)
        self.assertIn("[ -x /system/bin/wmt_loader ]", up)

    def test_the_build_copies_and_checks_every_listed_file(self):
        files = [l.split()[1] for l in self.LIST.read_text().splitlines()]
        for f in ("bin/linker64", "bin/wmt_loader", "bin/6620_launcher", "lib64/libc.so",
                  "etc/firmware/WIFI_RAM_CODE_6735", "etc/firmware/WMT_SOC.cfg"):
            self.assertIn(f, files)
        b = (ROOT / "rootfs/build-alpine-rootfs.sh").read_text()
        self.assertIn("done </work/android-wifi.sha256", b)
        self.assertIn('sha256sum -c -s /work/android-wifi.sha256', b)

    def test_networks_are_read_from_userdata_not_android(self):
        w = (O / "etc/init.d/i14-wifi").read_text() + (O / "usr/sbin/i14-wifi-conf").read_text()
        self.assertIn("NETWORKS=${I14_WIFI_NETWORKS:-/mnt/data/media/0/i14-linux/wpa_supplicant.conf}", w)
        self.assertNotIn("misc/wifi", w)

    def test_no_android_helpers_left(self):
        for f in ("usr/sbin/i14-boot-android", "etc/local.d/i14-quiet-android-boot.start"):
            self.assertFalse((O / f).exists(), f)


class Wiring(unittest.TestCase):
    def test_supervised_services(self):
        for name in ("i14-power", "i14-status", "i14-ui", "i14-wifi", "i14-netwatch"):
            text = (O / "etc/init.d" / name).read_text()
            self.assertIn("supervisor=supervise-daemon", text, name)
            self.assertNotIn("command_background", text, name)
        self.assertNotIn(" -B", (O / "etc/init.d/i14-wifi").read_text())  # supervise-daemon needs foreground

    def test_build_installs_and_enables(self):
        b = (ROOT / "rootfs/build-alpine-rootfs.sh").read_text()
        self.assertIn("iptables-legacy", b)
        self.assertRegex(b, r"for s in [^;]*i14-firewall; do")
        self.assertRegex(b, r"for s in [^;]*i14-netwatch[^;]*; do")
        self.assertRegex(b, r"for s in [^;]*unbound[^;]*; do")
        self.assertRegex(b, r"for s in [^;]*\bi14-ui\b[^;]*; do")
        self.assertNotRegex(b, r"for s in [^;]*\bi14-status\b[^;]*; do")  # replaced by i14-ui
        for pkg in ("unbound", "unbound-openrc", "dnssec-root"):
            self.assertRegex(b, r"PKGS=\"[^\"]*\b%s\b" % pkg)
        self.assertIn("inet:x:3003:unbound", b)
        self.assertIn("chrony:x:101:3003:", b)  # chronyd drops supplementary groups
        self.assertIn("var/lib/unbound/root.key", b)

    def test_tree_is_built_inside_the_container(self):
        # A macOS /work mount is case-insensitive: libxt_TOS.so and libxt_tos.so overwrote each other.
        b = (ROOT / "rootfs/build-alpine-rootfs.sh").read_text()
        root = re.search(r"^R=(\S+)$", b, re.M)
        self.assertIsNotNone(root)
        self.assertFalse(root[1].startswith("/work"), root[1])

    def test_wpa_config_is_private(self):
        conf = (O / "usr/sbin/i14-wifi-conf").read_text()
        sub = conf[conf.index("umask 077"):conf.index('>"$tmp"')]
        self.assertNotIn(")", sub.replace("$(", ""))  # the file is made inside the umask subshell
        self.assertIn("/usr/sbin/i14-wifi-conf", (O / "etc/init.d/i14-wifi").read_text())

    def run_conf(self, d, off):
        d = pathlib.Path(d)
        (d / "saved.conf").write_text('network={\n\tssid="ExampleNet"\n\tpsk="geheim123"\n}\n')
        (d / "rootfs.conf").write_text("ctrl_interface=x\n")
        if off:
            (d / "off").write_text("off\n")
        out = d / "run.conf"
        out.write_text("old")
        out.chmod(0o644)
        env = dict(os.environ, I14_WPA_CONF=str(out), I14_WIFI_NETWORKS=str(d / "saved.conf"),
                   I14_WIFI_ROOTFS_NETWORKS=str(d / "rootfs.conf"), I14_WIFI_OFF=str(d / "off"))
        subprocess.run(["sh", str(O / "usr/sbin/i14-wifi-conf")], env=env, check=True)
        return out.read_text(), out.stat().st_mode & 0o777, sorted(p.name for p in d.iterdir())

    def test_start_file_from_saved_networks_and_off_flag(self):
        with tempfile.TemporaryDirectory() as d:
            text, mode, names = self.run_conf(d, off=False)
            self.assertEqual(text, 'ctrl_interface=/run/wpa_supplicant\nupdate_config=0\n'
                                   'network={\n\tssid="ExampleNet"\n\tpsk="geheim123"\n}\n')
            self.assertEqual(mode, 0o600)  # a new file, not the old one with its mode
            self.assertEqual(names, ["rootfs.conf", "run.conf", "saved.conf"])
        with tempfile.TemporaryDirectory() as d:
            text, _, _ = self.run_conf(d, off=True)
            self.assertIn('\tpsk="geheim123"\n\tdisabled=1\n}', text)

if __name__ == "__main__":
    unittest.main()
