"""initramfs/i14charge and /i14init: power-off charging, automatic start, the start-loop guard and
the rescue mode that replaced every fallback to Android.

The battery lives in a fake sysfs and time in a fake /proc/uptime: the sleep stub moves the clock
and runs a per-test hook that may change the battery at given times.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = ROOT / "initramfs/i14charge"
GOOD = dict(capacity="20", batt_vol="3900", batt_temp="250", status="Charging", usb="1", ac="0")


class Env:
    def __init__(self, d, hook="", **battery):
        self.d = pathlib.Path(d)
        for sub in ("ps/battery", "ps/usb", "ps/ac", "bin"):
            (self.d / sub).mkdir(parents=True)
        self.set(**dict(GOOD, **battery))
        (self.d / "uptime").write_text("10.00 5.00\n")
        # hook: shell code with $t = the time after the sleep
        (self.d / "hook").write_text(hook + "\n")

    def set(self, **values):
        for k, v in values.items():
            path = {"usb": "ps/usb/online", "ac": "ps/ac/online"}.get(k, "ps/battery/" + k)
            (self.d / path).write_text(v + "\n")

    def run(self, code):
        env = dict(os.environ, PATH=f"{self.d}/bin:/usr/bin:/bin", I14_PS=str(self.d / "ps"),
                   I14_UPTIME=str(self.d / "uptime"), I14_PWRKEY=str(self.d / "pwrkey"), D=str(self.d))
        # sleep and cat as shell functions: thousands of fake seconds without a process each
        fakes = ('log() { echo "$*" >>"$D/log"; }\n'
                 'sleep() { read -r u _ <"$D/uptime"; t=$((${u%.*} + $1)); echo "$t.00 1.00" >"$D/uptime"; '
                 '. "$D/hook"; }\n'
                 'cat() { v=; read -r v <"$1" || [ -n "$v" ]; echo "$v"; }\n')
        script = f'{fakes}. "{LIB}"\n{code}\n'
        r = subprocess.run(["sh", "-c", script], env=env, capture_output=True, text=True, timeout=60)
        self.log = (self.d / "log").read_text() if (self.d / "log").exists() else ""
        return r


def set_cmd(path, value):
    """Shell line for a hook: write VALUE to a fake sysfs file."""
    return f'echo {value} >"$D/ps/{path}"'


class ChargeWait(unittest.TestCase):
    def wait(self, hook="", pre="", **battery):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, hook, **battery)
            r = env.run(pre + 'charge_wait; echo "rc=$? t=$(now) why=$why"')
            self.assertEqual(r.returncode, 0, r.stderr)
            last = r.stdout.strip().splitlines()[-1]
            rc, t, why = last.split(" ", 2)
            return int(rc[3:]), int(t[2:]) - 10, why[4:], env.log  # t: seconds since the start

    def test_starts_after_the_minimum_and_the_hold(self):
        rc, t, why, log = self.wait()
        self.assertEqual((rc, why), (0, "charged"))
        self.assertGreaterEqual(t, 300 + 120)
        self.assertLessEqual(t, 300 + 120 + 5)
        self.assertIn("charging: capacity=20% voltage=3900mV", log)

    def test_a_dip_restarts_the_hold(self):
        hook = (f'[ $t = 350 ] && {set_cmd("battery/batt_vol", 3700)}\n'
                f'[ $t = 355 ] && {set_cmd("battery/batt_vol", 3900)}')
        rc, t, why, _ = self.wait(hook)
        self.assertEqual((rc, why), (0, "charged"))
        self.assertGreaterEqual(t, 345 + 120)  # $t is absolute: 350 = 340 s after the start

    def no_start_until_key(self, pre="", **battery):
        """With these values Linux must not start by itself; a key press at 900 s ends the test."""
        hook = f'[ $t = 910 ] && {set_cmd("battery/batt_vol", 3700)} && : >"$D/pwrkey"'
        rc, t, why, log = self.wait(hook, pre, **battery)
        self.assertEqual((rc, why), (0, "power key"), log)
        self.assertGreaterEqual(t, 900)
        return log

    def test_needs_the_charging_status(self):
        self.no_start_until_key(status="Discharging")
        self.no_start_until_key(status="Not charging")

    def test_full_counts_as_charging(self):
        rc, _, why, _ = self.wait(status="Full")
        self.assertEqual((rc, why), (0, "charged"))

    def test_needs_capacity_voltage_and_a_safe_temperature(self):
        self.no_start_until_key(capacity="14")
        self.no_start_until_key(batt_vol="3799")
        self.no_start_until_key(batt_temp="451")
        self.no_start_until_key(batt_temp="-50")

    def test_no_automatic_start_leaves_only_the_power_key(self):
        # /i14init sets this for the no-autoboot flag and an unconfirmed earlier start
        self.no_start_until_key(pre="AB_PCT=101; ", capacity="100", batt_vol="4350", status="Full")

    def test_wall_charger_counts_as_power(self):
        rc, _, why, _ = self.wait(usb="0", ac="1")
        self.assertEqual((rc, why), (0, "charged"))

    def test_no_charger_powers_off(self):
        rc, t, why, _ = self.wait(usb="0", ac="0")
        self.assertEqual(rc, 2)
        self.assertIn("no charger", why)
        self.assertGreaterEqual(t, 30)
        self.assertLessEqual(t, 35)

    def test_a_short_power_gap_does_not_power_off(self):
        hook = (f'[ $t = 100 ] && {set_cmd("usb/online", 0)}\n'
                f'[ $t = 120 ] && {set_cmd("usb/online", 1)}')
        rc, t, why, _ = self.wait(hook)
        self.assertEqual((rc, why), (0, "charged"))

    def test_unreadable_values_go_to_rescue(self):
        rc, t, why, _ = self.wait(capacity="")
        self.assertEqual(rc, 1)
        self.assertIn("unreadable", why)
        self.assertGreaterEqual(t, 60)
        self.assertLessEqual(t, 65)

    def test_empty_status_is_unreadable_too(self):
        rc, t, why, _ = self.wait(status="")
        self.assertEqual(rc, 1)
        self.assertIn("unreadable", why)
        self.assertLessEqual(t, 65)

    def test_garbage_is_unreadable_too(self):
        rc, _, _, _ = self.wait(batt_vol="3.9V")
        self.assertEqual(rc, 1)

    def test_power_key_starts_at_once_with_enough_voltage(self):
        rc, t, why, _ = self.wait('[ $t = 50 ] && : >"$D/pwrkey"', capacity="3", batt_vol="3650")
        self.assertEqual((rc, why), (0, "power key"))
        self.assertLessEqual(t, 41)  # pressed at 40 s: noticed within the next second

    def test_power_key_below_the_voltage_keeps_charging(self):
        hook = '[ $t = 50 ] && : >"$D/pwrkey"\n' + f'[ $t = 400 ] && {set_cmd("battery/batt_vol", 3900)}'
        rc, t, why, log = self.wait(hook, batt_vol="3500")
        self.assertEqual((rc, why), (0, "charged"))
        self.assertIn("power key, but 3500mV is below 3600mV", log)


class StartLoopGuard(unittest.TestCase):
    def run_state(self, content):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d)
            state = pathlib.Path(d) / "autoboot-state"
            if content is not None:
                state.write_text(content + "\n")
            r = env.run(f'state_on_boot "{state}"; echo rc=$?')
            self.assertEqual(r.stdout.strip(), "rc=0", r.stderr)
            self.assertFalse(pathlib.Path(str(state) + ".new").exists())
            return state.read_text().strip() if state.exists() else None

    def test_handoff_becomes_booting(self):
        self.assertEqual(self.run_state("handoff 2026-11-04 06:00:00"), "booting 2026-11-04 06:00:00")

    def test_booting_stays(self):
        self.assertEqual(self.run_state("booting 2026-11-04 06:00:00"), "booting 2026-11-04 06:00:00")

    def test_nothing_without_a_state(self):
        self.assertIsNone(self.run_state(None))

    def test_write_fails_cleanly_in_a_missing_directory(self):
        with tempfile.TemporaryDirectory() as d:
            r = Env(d).run(f'state_write "{d}/missing/state" handoff; echo rc=$?')
            self.assertNotEqual(r.stdout.strip(), "rc=0")


class LinuxAck(unittest.TestCase):
    """rootfs/overlay/etc/local.d/i14-autoboot-ack.start removes the state after a healthy start."""
    ACK = ROOT / "rootfs/overlay/etc/local.d/i14-autoboot-ack.start"

    def run_ack(self, present):
        import time
        with tempfile.TemporaryDirectory() as d:
            state = pathlib.Path(d) / "autoboot-state"
            if present:
                state.write_text("booting 2026-11-04 06:00:00\n")
            (pathlib.Path(d) / "logger").write_text("#!/bin/sh\necho \"$*\" >>%s/logged\n" % d)
            (pathlib.Path(d) / "logger").chmod(0o755)
            (pathlib.Path(d) / "sync").write_text("#!/bin/sh\n")  # the Mac's sync can take seconds
            (pathlib.Path(d) / "sync").chmod(0o755)
            env = dict(os.environ, PATH=f"{d}:/usr/bin:/bin", I14_AUTOBOOT_STATE=str(state), I14_AUTOBOOT_ACK_S="1")
            r = subprocess.run(["sh", str(self.ACK)], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(state.exists(), present)  # not removed at once: only after the wait
            for _ in range(100):
                if not state.exists() and (pathlib.Path(d) / "logged").exists():
                    break
                time.sleep(0.1)
            logged = (pathlib.Path(d) / "logged").read_text() if (pathlib.Path(d) / "logged").exists() else ""
            return state.exists(), logged

    def test_removes_the_state_after_the_wait(self):
        exists, logged = self.run_ack(True)
        self.assertFalse(exists)
        self.assertIn("start confirmed", logged)

    def test_nothing_to_do_without_a_state(self):
        exists, logged = self.run_ack(False)
        self.assertEqual((exists, logged), (False, ""))


class Rescue(unittest.TestCase):
    """rescue() from /i14init, alone: power key restarts, no charger powers off, else it stays."""
    TEXT = (ROOT / "initramfs/i14init").read_text()
    FUNC = TEXT[TEXT.index("rescue() {"):TEXT.index("\npartition() {")]

    def rescue(self, hook, login_configured=False, **battery):
        with tempfile.TemporaryDirectory() as d:
            env = Env(d, hook, **battery)
            if login_configured:
                (env.d / "rescue-login-enabled").touch()
            fakes = ('gadget() { echo gadget >>"$D/did"; }\nmdev() { :; }\n'
                     'getty() { echo "getty $*" >>"$D/did"; exit 0; }\n'
                     'pwrkey_watch() { :; }\numount() { :; }\nsync() { :; }\n'
                     'reboot() { echo "reboot $* t=$(now)" >>"$D/did"; exit 0; }\n'
                     'poweroff() { echo "poweroff $* t=$(now)" >>"$D/did"; exit 0; }\n'
                     'DIR=$D STATE=$D/state pk=\nRESCUE_NOPOWER_S=600\n')
            func = self.FUNC.replace("/i14/bin/busybox --install", ": --install")
            func = func.replace("/i14/rescue-login-enabled", '"$D/rescue-login-enabled"')
            r = env.run(fakes + func + '\nrescue "test reason"')
            self.assertEqual(r.returncode, 0, r.stderr)
            did = (pathlib.Path(d) / "did").read_text().split("\n")
            return [l for l in did if l], env.log

    def test_rescue_shell_gets_every_busybox_command(self):
        # busybox --install needs an absolute path to itself (seen on the phone: "not an absolute path")
        self.assertIn("/i14/bin/busybox --install -s /i14/bin", self.FUNC)

    def test_power_key_restarts(self):
        did, log = self.rescue('[ $t = 40 ] && : >"$D/pwrkey"')
        self.assertEqual(did, ["gadget", "reboot -f t=40"])
        self.assertIn("rescue mode (test reason)", log)

    def test_configured_rescue_waits_for_key_and_starts_only_authenticated_getty(self):
        did, _ = self.rescue('[ "$t" = 40 ] && [ ! -e "$D/login-armed" ] && '
            ': >"$D/login-armed" && : >"$D/pwrkey"; '
            '[ "$t" != 45 ] || { wait; : >"$D/pwrkey"; }', login_configured=True)
        self.assertEqual(did, ["gadget", "getty -l /i14/bin/login -L 115200 ttyGS0 vt100", "reboot -f t=45"])

    def test_no_charger_powers_off_after_ten_minutes(self):
        did, _ = self.rescue("", usb="0", ac="0")
        self.assertEqual(did[0], "gadget")
        self.assertRegex(did[1], r"^poweroff -f t=6(1|0)\d$")  # 600 s after the start at 10

    def test_a_charger_or_unreadable_values_keep_it_waiting(self):
        for battery in ({"usb": "1"}, {"usb": "", "ac": ""}):
            did, _ = self.rescue('[ $t = 2000 ] && : >"$D/pwrkey"', **battery)
            self.assertEqual(did, ["gadget", "reboot -f t=2000"], battery)


class I14init(unittest.TestCase):
    text = (ROOT / "initramfs/i14init").read_text()

    def test_android_is_never_started(self):
        code = "\n".join(l.split("#")[0] for l in self.text.splitlines())
        self.assertNotIn("exec /init", code)
        self.assertNotRegex(code, r"\bandroid\b")  # android_usb and androidboot are fine
        self.assertIn('rescue "rescue-once flag"', code)

    def test_charging_modes_go_to_charge(self):
        self.assertIn("8|9) charge ;;", self.text)
        self.assertIn("grep -q 'androidboot.mode=charger' /proc/cmdline && charge", self.text)

    def test_both_file_systems_are_checked_before_mounting(self):
        t = self.text
        self.assertLess(t.index('check userdata "$data"'), t.index('mount -t ext4 -o noatime,nosuid,nodev "$data"'))
        self.assertLess(t.index("check rootfs.img $DIR/rootfs.img"), t.index("mount -t ext4 -o loop"))

    def test_guard_blocks_before_any_charging_start(self):
        t = self.text[self.text.index("charge() {"):]
        self.assertLess(t.index('[ -e $STATE ] && noauto='), t.index("AB_PCT=101"))
        self.assertLess(t.index("AB_PCT=101"), t.index("charge_wait"))
        self.assertLess(t.index("state_write $STATE \"handoff"), t.index("reboot -f"))


if __name__ == "__main__":
    unittest.main()
