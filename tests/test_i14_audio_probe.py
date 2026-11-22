"""Offline tests; these never connect to the phone."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/i14-audio-underrun-probe.py"

# Read-only ssh root@172.16.42.1 'cat /proc/asound/pcm', 2026-11-05.
PCM = """00-00: MultiMedia1_PLayback mt-soc-codec-tx-dai-0 :  : playback 1
00-01: MultiMedia1_Capture mt-soc-codec-rx-dai-1 :  : capture 1
00-02: Voice_MD1_PLayback mt-soc-codec-voicemd1-dai-2 :  : playback 1 : capture 1
00-03: HMDI_PLayback mt-soc-hdmi-dummy-dai-codec-3 :  : playback 1 : capture 1
00-08: I2S0DL1_PLayback mt-soc-codec-I2s0tx-dai-8 :  : playback 1
00-09: DL1_AWB_Record mt-soc-codec-dl1awb-dai-9 :  : capture 1
00-18: MultiMedia_Routing mt-soc-dummy-dai-codec-18 :  : playback 1
00-24: MultiMedia2_PLayback mt-soc-codec-tx-dai2-24 :  : playback 1
"""


class AudioProbe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("audio_probe", SCRIPT)
        cls.probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.probe)

    def test_real_pcm_prefers_primary_multimedia(self):
        self.assertEqual(self.probe.select_pcm(PCM), "hw:0,0")
        self.assertEqual(self.probe.select_pcm("\n".join(reversed(PCM.splitlines()))), "hw:0,0")

    def test_dl1_and_generic_playback(self):
        self.assertEqual(self.probe.select_pcm(
            "01-02: Voice : playback 1\n01-03: DL1 : playback 1"), "hw:1,3")
        self.assertEqual(self.probe.select_pcm("00-04: PCM : playback 1"), "hw:0,4")
        with self.assertRaises(ValueError):
            self.probe.select_pcm("00-01: DL1_AWB_Record : capture 1")

    def test_override_must_be_a_listed_playback_pcm(self):
        self.assertEqual(self.probe.select_pcm(PCM, "plughw:0,8"), "plughw:0,8")
        for value in ("hw:0,1", "hw:0,99", "default", "hw:0,0; reboot"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.probe.select_pcm(PCM, value)

    def test_count_lines_once_even_if_both_words_occur(self):
        self.assertEqual(self.probe.count_underruns(
            "Playing WAVE\nunderrun!!!\nXRUN recovered\nunderrun / xrun\n"),
            {"underrun_lines": 2, "xrun_lines": 2, "total_lines": 3})
        self.assertEqual(self.probe.count_underruns("Playing WAVE")["total_lines"], 0)

    def test_verdict_requires_valid_complete_pair_and_baseline(self):
        a = {"condition": "A", "valid": True, "underruns": {"total_lines": 0}}
        b = {"condition": "B", "valid": True, "underruns": {"total_lines": 0}}
        self.assertEqual(self.probe.verdict([a, b]), "no_underruns_observed")
        a["underruns"]["total_lines"] = 1
        self.assertEqual(self.probe.verdict([a, b]), "possible_policy_regression")
        b["underruns"]["total_lines"] = 1
        self.assertEqual(self.probe.verdict([a, b]), "baseline_has_underruns")
        b["valid"] = False
        self.assertEqual(self.probe.verdict([a, b]), "invalid")
        self.assertEqual(self.probe.verdict([a]), "invalid")

    def test_kernel_underruns_also_affect_verdict(self):
        runs = [{"condition": c, "valid": True, "underruns": {"total_lines": 0}}
                for c in ("A", "B")]
        runs[0]["kernel_underrun_lines"] = {"total_lines": 2}
        self.assertEqual(self.probe.verdict(runs), "possible_policy_regression")
        runs[1]["kernel_underrun_lines"] = {"total_lines": 1}
        self.assertEqual(self.probe.verdict(runs), "baseline_has_underruns")

    def test_screen_precondition_failure_still_restores(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        with patch.object(self.probe, "local_run", runner), patch.object(
                self.probe, "snapshot", return_value={"brightness": 1, "early_suspend": 1}):
            result = self.probe.run_condition({"condition": "A", "decode_load": False})
        self.assertFalse(result["valid"])
        self.assertTrue(result["restore_verified"])
        self.assertIn("brightness=0", result["invalid_reasons"][0])

    def test_restore_on_exception_and_keyboard_interrupt(self):
        for error in (RuntimeError("playback failed"), KeyboardInterrupt()):
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                with self.probe.restore_policy(runner):
                    raise error
            self.assertEqual(runner.call_args_list[0].args[0],
                             "rc-service i14-cpufreq start")
            self.assertIn("rc-service i14-cpufreq status", runner.call_args_list[1].args[0])
            self.assertIn("cpufreq_fix_freq_in_es", runner.call_args_list[1].args[0])

    def test_restore_verification_failure_is_not_swallowed(self):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, "", ""),
                                  RuntimeError("service is not running")])
        with self.assertRaisesRegex(RuntimeError, "not running"):
            with self.probe.restore_policy(runner):
                pass

    def test_kernel_ring_delta_and_lost_overlap(self):
        self.assertEqual(self.probe.kernel_delta("a\nb\nc\n", "b\nc\naudio IRQ\n"),
                         (["audio IRQ"], True))
        self.assertEqual(self.probe.kernel_delta("old\n", "new\n"), (["new"], False))


if __name__ == "__main__":
    unittest.main()
