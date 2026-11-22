"""Exercise CPU policy against files; never touch the host's CPU controls."""
import importlib.machinery
import importlib.util
import pathlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "rootfs/overlay/usr/sbin/i14-cpufreq"


class CpuPolicy(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), "CPU policy is not implemented")
        loader = importlib.machinery.SourceFileLoader("i14_cpufreq", str(SCRIPT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.proc = self.root / "proc/cpufreq"
        self.cpu = self.root / "sys/devices/system/cpu/cpu0/cpufreq"
        self.proc.mkdir(parents=True)
        self.cpu.mkdir(parents=True)
        for name, val in {"scaling_driver": "mt-cpufreq", "scaling_governor": "interactive",
                          "scaling_available_frequencies": "1300000 819000 598000 442000 299000"}.items():
            (self.cpu / name).write_text(val)
        for name, val in {"cpufreq_fix_freq_in_es": "1", "cpufreq_limited_max_freq_by_user": "0"}.items():
            (self.proc / name).write_text(val)
        self.screen(False)
        self.ticks = [[0, 0, 0], [0, 0, 0]]  # user, idle, iowait for each core
        self.advance()
        self.policy = mod.Policy(self.root)

    def screen(self, on):
        (self.proc / "cpufreq_state").write_text(
            f"[MT_CPU_DVFS_LITTLE/0]\ndvfs_disable_by_early_suspend = {0 if on else 1}\n")

    def advance(self, busy=(0, 0), waiting=0):
        lines = []
        for i, ticks in enumerate(self.ticks):
            ticks[0] += busy[i]
            ticks[1] += 100 - busy[i] - waiting
            ticks[2] += waiting
            lines.append(f"cpu{i} {ticks[0]} 0 0 {ticks[1]} {ticks[2]} 0 0 0 0 0")
        (self.root / "proc/stat").write_text("cpu 0 0 0 0 0 0 0 0 0 0\n" + "\n".join(lines) + "\nintr 42\n")

    def limit(self):
        return int((self.proc / "cpufreq_limited_max_freq_by_user").read_text())

    def tick(self, busy=(0, 0), waiting=0):
        self.advance(busy, waiting)
        self.policy.tick()

    def idle(self):
        for _ in range(6):
            self.tick()

    def test_quiet_screen_off_lowers_clock_without_changing_governor(self):
        self.policy.start()
        for _ in range(4):
            self.tick()
        self.assertEqual(self.limit(), 0)  # no cap on a single idle sample
        self.tick()
        self.assertEqual(self.limit(), 299000)
        self.assertEqual((self.cpu / "scaling_governor").read_text(), "interactive")

    def test_one_busy_core_releases_limit_without_averaging_it_away(self):
        self.policy.start()
        self.idle()
        self.tick((50, 0))
        self.assertEqual(self.limit(), 0)

    def test_screen_on_releases_limit_and_screen_off_can_lower_again(self):
        self.policy.start()
        self.idle()
        self.screen(True)
        self.tick()
        self.assertEqual(self.limit(), 0)
        self.screen(False)
        self.idle()
        self.assertEqual(self.limit(), 299000)

    def test_busy_sample_resets_idle_delay_and_hysteresis_avoids_chatter(self):
        self.policy.start()
        for _ in range(4):
            self.tick()
        self.tick((10, 0))
        self.tick()
        self.assertEqual(self.limit(), 0)
        self.idle()
        self.tick((10, 0))
        self.assertEqual(self.limit(), 299000)

    def test_iowait_is_not_cpu_work(self):
        self.policy.start()
        for _ in range(6):
            self.tick(waiting=80)
        self.assertEqual(self.limit(), 299000)

    def test_new_core_or_reset_counters_release_limit(self):
        self.policy.start()
        self.idle()
        self.ticks = [[0, 0, 0], [0, 0, 0]]
        self.tick()
        self.assertEqual(self.limit(), 0)
        self.idle()
        with (self.root / "proc/stat").open("a") as f:
            f.write("cpu2 0 0 0 100 0 0 0 0\n")
        self.policy.tick()
        self.assertEqual(self.limit(), 0)

    def test_stop_restores_settings(self):
        self.policy.start()
        self.idle()
        self.policy.close()
        self.assertEqual(self.limit(), 0)
        self.assertEqual((self.proc / "cpufreq_fix_freq_in_es").read_text().strip(), "1")

    def test_existing_limit_is_rejected_before_any_write(self):
        (self.proc / "cpufreq_limited_max_freq_by_user").write_text("598000")
        with self.assertRaises(RuntimeError):
            self.policy.start()
        self.assertEqual(self.limit(), 598000)
        self.assertEqual((self.proc / "cpufreq_fix_freq_in_es").read_text(), "1")

    def test_external_change_is_preserved_on_stop(self):
        self.policy.start()
        self.idle()
        (self.proc / "cpufreq_limited_max_freq_by_user").write_text("598000")
        with self.assertRaises(RuntimeError):
            self.tick()
        self.policy.close()
        self.assertEqual(self.limit(), 598000)

    def test_missing_measurements_raise_and_cleanup_releases_limit(self):
        self.policy.start()
        self.idle()
        (self.root / "proc/stat").write_text("cpu 0 0 0 0\n")
        with self.assertRaises(ValueError):
            try:
                self.policy.tick()
            finally:
                self.policy.close()
        self.assertEqual(self.limit(), 0)

    def test_wrong_driver_does_not_write(self):
        (self.cpu / "scaling_driver").write_text("other")
        with self.assertRaises(RuntimeError):
            self.policy.start()
        self.assertEqual((self.proc / "cpufreq_fix_freq_in_es").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
