#!/usr/bin/env python3
"""Run the real shell flow against a temporary configfs tree and inert commands."""
import os
import pathlib
import re
import signal
import subprocess
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent


class UsbNetworkTests(unittest.TestCase):
    def run_setup(self, udcs=1, fail_ip=False, fail_http=False, fail_dhcp=False, wrong_mac=False, fail_telnet=False,
                  have_tty=False, debug=True):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = pathlib.Path(self.temp.name)
        for directory in ("run", "dev", "proc/sys/net/ipv4", "sys/class/udc",
                          "sys/class/net/usb0", "sys/kernel/config/usb_gadget", "mock"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        for n in range(udcs):
            (root / f"sys/class/udc/controller{n}").mkdir()
        (root / "dev/kmsg").touch()
        if have_tty:
            (root / "dev/ttyGS0").symlink_to("/dev/null")
        (root / "sys/class/net/usb0/address").write_text(
            "02:14:00:00:00:99\n" if wrong_mac else "02:14:00:00:00:02\n")
        (root / "proc/sys/net/ipv4/ip_forward").write_text("1\n")
        mocks = {
            "mount": "exit 0\n",
            "sleep": "/bin/sleep 0.02\n",
            "dmesg": "echo simulated-kernel-log\n",
            "uname": "echo Linux-test-kernel\n",
            "ip": 'echo "ip $*" >>"$TEST_ROOT/calls"\n' + ("exit 1\n" if fail_ip else "exit 0\n"),
            "ln": '/bin/ln "$@"\necho "$*" >>"$TEST_ROOT/links.log"\necho usb0 >"$TEST_ROOT/sys/kernel/config/usb_gadget/i14/functions/ecm.usb0/ifname"\n' +
                  'cat "$TEST_ROOT/sys/kernel/config/usb_gadget/i14/configs/c.1/MaxPower" >>"$TEST_ROOT/pre-bind-power" 2>/dev/null || :\n',
            "setsid": 'echo "$*" >>"$TEST_ROOT/calls"\necho $$ >"$TEST_ROOT/serial-child.pid"\nexec /bin/sleep 120\n',
        }
        for command in ("httpd", "udhcpd", "telnetd"):
            fail = {"httpd": fail_http, "udhcpd": fail_dhcp, "telnetd": fail_telnet}[command]
            mocks[command] = f'echo "{command} $*" >>"$TEST_ROOT/calls"\n' + (
                "exit 1\n" if fail else "exec /bin/sleep 120\n")
        for name, body in mocks.items():
            script = root / "mock" / name
            script.write_text("#!/bin/sh\n" + body)
            script.chmod(0o755)
        original = (HERE / "usb-network").read_text()
        original = original.replace(">/dev/kmsg", ">>/dev/kmsg")
        original = re.sub(r"/proc/sys/|/sys/|/run/|/dev/", lambda m: str(root) + m[0], original)
        script = root / "usb-network"
        script.write_text(original)
        env = dict(os.environ, TEST_ROOT=str(root), PATH=str(root / "mock") + ":" + os.environ["PATH"],
                   I14_DEBUG_INITRAMFS=str(int(debug)))

        def stop_children():
            for file in (root / "run").glob("*.pid"):
                try:
                    os.kill(int(file.read_text()), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
        self.addCleanup(stop_children)
        result = subprocess.run(["/bin/sh", str(script)], env=env, text=True,
                                capture_output=True, timeout=5)
        return root, result

    def test_regular_image_leaves_usb_debug_services_off(self):
        root, result = self.run_setup(debug=False, have_tty=True)
        self.assertEqual(result.returncode, 0)
        self.assertFalse((root / "calls").exists())
        self.assertFalse((root / "sys/kernel/config/usb_gadget/i14").exists())

    def test_no_udc_returns_useful_error_without_network_actions(self):
        root, result = self.run_setup(udcs=0)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no UDC", result.stdout + result.stderr)
        self.assertFalse((root / "calls").exists())

    def test_multiple_udcs_are_refused(self):
        root, result = self.run_setup(udcs=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("multiple UDC", result.stdout + result.stderr)
        self.assertFalse((root / "calls").exists())

    def test_ecm_uses_only_its_interface_and_no_router_or_dns_options(self):
        root, result = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        gadget = root / "sys/kernel/config/usb_gadget/i14"
        self.assertEqual((gadget / "idVendor").read_text().strip(), "0x1d6b")
        self.assertEqual((gadget / "idProduct").read_text().strip(), "0x0104")
        self.assertEqual((gadget / "bDeviceClass").read_text().strip(), "0x02")
        self.assertEqual((gadget / "configs/c.1/bmAttributes").read_text().strip(), "0xc0")
        self.assertTrue((gadget / "configs/c.1/MaxPower").exists(), "explicit pre-bind USB current descriptor missing")
        self.assertEqual((gadget / "configs/c.1/MaxPower").read_text().strip(), "500")
        self.assertEqual((root / "pre-bind-power").read_text().splitlines(), ["500", "500"])
        links = (root / "links.log").read_text().splitlines()
        self.assertTrue(links[0].endswith("/ecm.usb0"))
        self.assertTrue(links[1].endswith("/acm.usb0"))
        self.assertEqual((gadget / "UDC").read_text().strip(), "controller0")
        self.assertEqual((gadget / "functions/ecm.usb0/dev_addr").read_text().strip(), "02:14:00:00:00:02")
        self.assertEqual((gadget / "functions/ecm.usb0/host_addr").read_text().strip(), "02:14:00:00:00:01")
        self.assertEqual((gadget / "strings/0x409/product").read_text().strip(), "I14 Linux Bringup")
        config = (root / "run/udhcpd.conf").read_text()
        self.assertIn("interface usb0\n", config)
        self.assertIn("start 10.77.14.1\nend 10.77.14.1\n", config)
        self.assertNotRegex(config, r"(?m)^opt(?:ion)?\s+(?:router|dns)\s")
        calls = (root / "calls").read_text()
        self.assertIn("ip addr add 10.77.14.2/30 dev usb0", calls)
        self.assertIn("httpd -f -p 10.77.14.2:8080", calls)
        self.assertEqual((root / "proc/sys/net/ipv4/ip_forward").read_text(), "0\n")
        self.assertIn("telnetd -F -b 10.77.14.2:23 -l /bin/sh", calls)
        self.assertIn("I14_USB_ECM_READY", (root / "dev/kmsg").read_text())
        self.assertIn("I14_NATIVE_INIT", (root / "run/www/index.html").read_text())
        self.assertIn("Linux-test-kernel", (root / "run/www/index.html").read_text())
        self.assertIn("simulated-kernel-log", (root / "run/www/dmesg.txt").read_text())

    def test_failed_address_assignment_unbinds_gadget_without_servers(self):
        root, result = self.run_setup(fail_ip=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((root / "sys/kernel/config/usb_gadget/i14/UDC").read_text().strip(), "")
        self.assertNotIn("httpd", (root / "calls").read_text())
        self.assertNotIn("udhcpd", (root / "calls").read_text())

    def test_wrong_ecm_interface_mac_is_refused_before_ip_commands(self):
        root, result = self.run_setup(wrong_mac=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MAC mismatch", result.stdout)
        self.assertFalse((root / "calls").exists())
        self.assertEqual((root / "sys/kernel/config/usb_gadget/i14/UDC").read_text().strip(), "")

    def test_failed_http_startup_unbinds_and_does_not_start_dhcp(self):
        root, result = self.run_setup(fail_http=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HTTP startup failed", result.stdout)
        self.assertNotIn("udhcpd", (root / "calls").read_text())
        self.assertEqual((root / "sys/kernel/config/usb_gadget/i14/UDC").read_text().strip(), "")

    def test_optional_dhcp_failure_retains_http_and_gives_static_peer_address(self):
        root, result = self.run_setup(fail_dhcp=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("set USB peer manually to 10.77.14.1/30", result.stdout)
        self.assertIn("I14_USB_ECM_READY", result.stdout)
        self.assertEqual((root / "sys/kernel/config/usb_gadget/i14/UDC").read_text().strip(), "controller0")

    def test_failed_telnet_startup_unbinds_before_dhcp(self):
        root, result = self.run_setup(fail_telnet=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("telnet startup failed", result.stdout)
        self.assertNotIn("udhcpd", (root / "calls").read_text())
        self.assertEqual((root / "sys/kernel/config/usb_gadget/i14/UDC").read_text().strip(), "")

    def test_acm_wait_does_not_block_network_and_delayed_snapshot_is_scoped(self):
        root, result = self.run_setup()
        self.assertEqual(result.returncode, 0)
        self.assertTrue((root / "run/i14-serial-console.pid").exists())
        time.sleep(0.25)
        log = (root / "dev/kmsg").read_text()
        self.assertIn("I14_SERIAL_WAIT_BEGIN", log)
        self.assertIn("I14_SERIAL_TTY_TIMEOUT", log)
        self.assertIn("I14_USB_DELAYED_SNAPSHOT", log)
        self.assertEqual((root / "sys/kernel/config/usb_gadget/i14/UDC").read_text().strip(), "controller0")
        self.assertIn("ip addr show dev usb0", (root / "calls").read_text())

    def test_critical_failure_stops_serial_loop_and_its_actual_child(self):
        root, result = self.run_setup(have_tty=True, fail_http=True)
        self.assertNotEqual(result.returncode, 0)
        calls = (root / "calls").read_text()
        self.assertIn("sh -c exec sh -i <" + str(root / "dev/ttyGS0"), calls)
        self.assertNotIn("cttyhack", calls)
        child = int((root / "serial-child.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)
        self.assertIn("I14_SERIAL_STOPPED", (root / "dev/kmsg").read_text())


class TrialTimerTests(unittest.TestCase):
    def run_init(self, cmdline="", delay="0.15", *, debug=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name)
        for name in ("dev", "etc", "proc", "sys", "run"):
            (root / name).mkdir()
        if debug:
            (root / "etc/i14-debug").touch()
        (root / "dev/kmsg").touch()
        (root / "dev/console").touch()
        (root / "proc/cmdline").write_text(cmdline)
        code = (HERE / "init").read_text().replace(">/dev/kmsg", ">>/dev/kmsg")
        # End the simulated PID 1 loop while allowing the actual timer child to finish.
        code = code.replace("\n    sleep 2\ndone", "\n    break\ndone")
        code = re.sub(r"/etc/i14-debug|/proc/|/sys/|/run/|/dev/", lambda m: str(root) + m[0], code)
        prelude = ('mount(){ :; }; uname(){ echo Linux-fixture; }; setsid(){ exit 0; }; '
                   'ip(){ echo "ip $*" >>"$TEST_ROOT/calls"; }; '
                   'sh(){ echo "sh $*" >>"$TEST_ROOT/calls"; echo "ERROR: simulated USB failure"; return 1; }; '
                   'reboot(){ echo "reboot $*" >>"$TEST_ROOT/reboots"; }; '
                   'sleep(){ echo "$*" >>"$TEST_ROOT/sleeps"; /bin/sleep "$TEST_DELAY"; };\n')
        script = root / "init-test"
        script.write_text(prelude + code)
        env = dict(os.environ, TEST_ROOT=str(root), TEST_DELAY=delay)
        with (root / "stdout").open("w") as output:
            subprocess.run(["/bin/sh", str(script)], env=env, stdout=output, stderr=output, timeout=3)
        return root

    def test_failed_usb_keeps_default_reboot_and_logs_native_version(self):
        root = self.run_init(debug=True)
        self.assertTrue((root / "run/i14-trial-reboot.pid").exists())
        time.sleep(0.3)
        self.assertEqual((root / "reboots").read_text(), "reboot -f\n")
        self.assertEqual((root / "sleeps").read_text(), "120\n")
        log = (root / "dev/kmsg").read_text()
        self.assertIn("I14_NATIVE_UNAME Linux-fixture", log)
        self.assertIn("I14_TRIAL_AUTO_REBOOT", log)
        self.assertIn("I14_USB_HELPER_FAILED status=1", log)
        self.assertIn("simulated USB failure", log)
        calls = (root / "calls").read_text()
        self.assertLess(calls.index("ip link set dev lo up"), calls.index("sh /storage-readonly"))
        self.assertLess(calls.index("sh /storage-readonly"), calls.index("sh /usb-network"))

    def test_exact_cmdline_opt_out_disables_timer(self):
        root = self.run_init("i14.trial_reboot=off")
        time.sleep(0.2)
        self.assertFalse((root / "run/i14-trial-reboot.pid").exists())
        self.assertFalse((root / "reboots").exists())

    def test_pid_receipt_cancellation_prevents_reboot(self):
        root = self.run_init(delay="0.3")
        pid = int((root / "run/i14-trial-reboot.pid").read_text())
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.4)
        self.assertFalse((root / "reboots").exists())


if __name__ == "__main__":
    unittest.main()
