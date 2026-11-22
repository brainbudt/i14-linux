"""rootfs/overlay/usr/sbin/i14-netwatch: Wi-Fi watchdog, repairs layer by layer, never reloads the driver."""
import os
import re
import pathlib
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WATCH = ROOT / "rootfs/overlay/usr/sbin/i14-netwatch"


class Net:
    """Fake world: files hold what wpa_cli/ip/ping answer; stubs record what the watchdog did."""

    def __init__(self, d, checks, state="COMPLETED", addr=True, route=True, ping=True, hook="", dns=None, off=False):
        self.d = pathlib.Path(d)
        if off:
            (self.d / "wifi-off").write_text("off\n")
        b = self.d / "bin"
        b.mkdir()
        self.put(state=state, addr=" inet 192.0.2.167/24" if addr else "",
                 route="default via 192.0.2.1 dev wlan0" if route else "", ping="0" if ping else "1",
                 dns="ok" if dns in (None, True) else "dead")
        if dns is not None:  # None: unbound not in the default runlevel
            (self.d / "unbound.want").write_text("")
        (self.d / "hook").write_text(hook + "\n")
        stubs = {
            "sleep": f'n=$(cat {d}/n 2>/dev/null || echo 0); n=$((n + 1)); echo $n >{d}/n\n'
                     f'[ $n -le {checks} ] || exit 1\n. {d}/hook\nexit 0',
            "wpa_cli": f'case "$*" in *status) echo "wpa_state=$(cat {d}/state)" ;; *) echo "$*" >>{d}/wpa ;; esac',
            "ip": f'case "$*" in *route*) cat {d}/route ;; *addr*) cat {d}/addr ;; esac',
            "ping": f'exit $(cat {d}/ping)',
            "logger": f'echo "$*" >>{d}/log',
            "udhcpc": f'echo "$*" >>{d}/udhcpc',
            "nslookup": f'[ "$(cat {d}/dns)" = ok ] && echo "Address: 192.0.2.1"',
            "rc-service": f'echo "$*" >>{d}/rc',
        }
        for name, body in stubs.items():
            (b / name).write_text("#!/bin/sh\n" + body + "\n")
            (b / name).chmod(0o755)
        # a stand-in DHCP client: records SIGUSR1 (renew requests)
        self.client = subprocess.Popen(["sh", "-c", f'trap "echo renew >>{d}/renew" USR1; '
                                                    'while :; do sleep 0.05; done'])
        time.sleep(0.2)
        (self.d / "pid").write_text(f"{self.client.pid}\n")

    def put(self, **files):
        for k, v in files.items():
            (self.d / k).write_text(v + "\n")

    def run(self):
        env = dict(os.environ, PATH=f"{self.d}/bin:/usr/bin:/bin", I14_UDHCPC_PID=str(self.d / "pid"),
                   I14_NETWATCH_S="0", I14_UNBOUND_WANT=str(self.d / "unbound.want"),
                   I14_WIFI_OFF=str(self.d / "wifi-off"))
        try:
            subprocess.run(["sh", str(WATCH)], env=env, capture_output=True, text=True, timeout=30)
            time.sleep(0.2)  # let the stand-in client write its renew lines
        finally:
            self.client.send_signal(signal.SIGKILL)
            self.client.wait()

    def read(self, name):
        p = self.d / name
        return p.read_text().splitlines() if p.exists() else []


def watch(checks, **kw):
    with tempfile.TemporaryDirectory() as d:
        net = Net(d, checks, **kw)
        net.run()
        return {k: net.read(k) for k in ("log", "wpa", "renew", "udhcpc", "rc")}


class NetWatch(unittest.TestCase):
    def test_wifi_switched_off_is_left_alone_but_dns_is_still_watched(self):
        r = watch(15, state="DISCONNECTED", addr=False, dns=False, off=True)
        self.assertEqual(r["wpa"], [])
        self.assertEqual(r["renew"], [])
        self.assertEqual(r["udhcpc"], [])
        self.assertIn("restart", " ".join(r["rc"]))
        self.assertIn("-t i14-netwatch Wi-Fi switched off by the user: not checking it", r["log"])

    def test_all_well_does_nothing(self):
        r = watch(5)
        self.assertEqual(r, {"log": [], "wpa": [], "renew": [], "udhcpc": [], "rc": []})
        self.assertEqual(watch(5, dns=True)["rc"], [])

    def test_silent_dns_server_is_restarted_once_then_every_ten_checks(self):
        r = watch(6, dns=False)
        self.assertEqual(r["rc"], ["unbound restart"])  # at the third failure, not again at 4..6
        self.assertEqual(watch(14, dns=False)["rc"], ["unbound restart"] * 2)  # 3 and 13

    def test_a_failed_restart_is_retried_until_dns_answers(self):
        hook = '[ $n = 15 ] && echo ok >"$(dirname "$0")/../dns"'  # restart at 3 failed, at 13 too
        r = watch(18, dns=False, hook=hook)
        self.assertEqual(r["rc"], ["unbound restart"] * 2)
        self.assertIn("DNS server answers again", r["log"][-1])
        self.assertIn("DNS server does not answer", r["log"][0])
        self.assertEqual(r["wpa"], [])  # Wi-Fi is fine: nothing else touched

    def test_no_dns_check_without_unbound(self):
        self.assertEqual(watch(4, dns=None)["rc"], [])

    def test_not_associated_reassociates_after_three_checks_then_every_ten(self):
        r = watch(14, state="SCANNING")
        self.assertEqual(r["wpa"], ["-i wlan0 reassociate"] * 2)  # checks 3 and 13
        self.assertEqual(r["renew"], [])
        self.assertEqual(r["log"][0], "-t i14-netwatch problem: not associated (SCANNING)")

    def test_two_bad_checks_do_nothing(self):
        r = watch(2, state="SCANNING")
        self.assertEqual((r["wpa"], r["renew"]), ([], []))

    def test_no_address_renews_the_lease(self):
        r = watch(3, addr=False)
        self.assertEqual(r["renew"], ["renew"])
        self.assertEqual(r["wpa"], [])

    def test_no_route_renews_the_lease(self):
        r = watch(3, route=False)
        self.assertEqual(r["renew"], ["renew"])

    def test_silent_gateway_reassociates_and_renews(self):
        r = watch(3, ping=False)
        self.assertEqual(r["wpa"], ["-i wlan0 reassociate"])
        self.assertEqual(r["renew"], ["renew"])

    def test_recovery_is_logged_and_resets_the_count(self):
        d_hook = '[ $n = 3 ] && echo COMPLETED >"$(dirname "$0")/../state"'
        r = watch(6, state="SCANNING", hook=d_hook)
        self.assertEqual(r["wpa"], [])  # back before the third bad check was acted on
        self.assertIn("Wi-Fi works again", r["log"][-1])

    def test_dead_dhcp_client_is_started_again(self):
        with tempfile.TemporaryDirectory() as d:
            net = Net(d, 1)
            (net.d / "pid").write_text("999999\n")
            net.run()
            self.assertEqual(net.read("udhcpc"), ["-b -i wlan0 -p %s/pid -x hostname:i14" % d])
            self.assertIn("DHCP client not running", net.read("log")[0])

    def test_never_reloads_the_driver_or_reboots(self):
        text = WATCH.read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for word in ("wmt_loader", "reboot", "wmtWifi"):
            self.assertNotIn(word, code)
        self.assertEqual(re.findall(r"rc-service \S+ \S+", code), ["rc-service unbound restart"])


if __name__ == "__main__":
    unittest.main()
