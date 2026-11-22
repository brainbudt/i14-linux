"""Safety and endpoint checks for the bounded, internal-gauge experiment."""
import importlib.util
import json
import math
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "battery_calibrate", Path(__file__).resolve().parents[1] / "scripts/i14-battery-calibrate.py")
cal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cal)

# Kernel time runs ahead of time.monotonic() here: the script must never compare the two clocks.
KOFF = 50000.0


class Phone:
    """Offline replay of the stock battery thread (10 s iterations at x0.2 s) and its cached gauge.

    Per iteration the vendor thread refreshes the cached CAR, prints AvgVbat, runs the charging
    state machine and then battery_update(), which may reset the meter and prints
    `UI_SOC=(n), resetBatteryMeter=(r)`. The cache therefore shows a reset only from the next
    iteration on. Times are in seconds on the monotonic scale; entries/resets are iteration times.
    """

    def __init__(self, rate=0.0, volts=lambda t: 4350, entries=(), resets=(), ring=200,
                 discharge_from=300, sample_volts=None, status=lambda t: "Full", bcct=lambda t: 1,
                 reset_ui=100, mutate=None):
        self.rate, self.volts, self.ring, self.discharge_from = rate, volts, ring, discharge_from
        self.entries = sorted(self.index(t) for t in entries)
        self.resets = sorted(self.index(t) for t in resets)
        self.sample_volts = sample_volts or volts
        self.status, self.bcct, self.reset_ui, self.mutate = status, bcct, reset_ui, mutate

    @staticmethod
    def index(t):
        return round((t - 0.2) / 10)

    @staticmethod
    def time(k):
        return 10*k + 0.2

    def iteration(self, k):
        t = self.time(k)
        v = self.volts(t)
        lines = []
        def add(text):
            lines.append(f"[{t + KOFF + len(lines)*0.0001:.6f}]  (0)[176:bat_thread_kthr]{text}")
        add(f"AvgVbat=({v},{v}),AvgI=(150,0),VChr=5232,AvgT=(24,24),SOC=(100,100),UI_SOC=100,"
            f"ZCV={v + 20} bcct:{self.bcct(t)}:0 I:7000")
        entry = max((e for e in self.entries if e <= k), default=None)
        last_reset = max((r for r in self.resets if r < k), default=None)
        recharging = entry is not None and (last_reset is None or last_reset < entry)
        if not recharging or entry == k:
            add("[BATTERY] Battery full !!")
        if recharging and entry == k:
            add(f"[BATTERY] Battery Enter Re-charging!! , vbat=({v})")
        elif recharging and k == entry + 1:
            add("[BATTERY] CC mode charge, timer=0 on 0 !!")
        elif recharging and any(entry < r for r in self.resets if r >= k):
            add(f"[BATTERY] Top Off mode charge, timer={10*(k - entry - 2)} on {10*(k - entry - 1)} !!")
        if k in self.resets:
            add(f"UI_SOC=({self.reset_ui}), resetBatteryMeter=(1)")
        else:
            add("[recharging] UI_SOC=100, SOC=100")
            add("UI_SOC=(100), resetBatteryMeter=(0)")
        return self.mutate(t, lines) if self.mutate else lines

    def car(self, k):
        t = self.time(k)
        if t <= self.discharge_from:
            return 0
        start = max([self.discharge_from] + [self.time(r) for r in self.resets if r < k])
        return -round(self.rate * (t - start))

    def row(self, now):
        last = math.floor((now - 0.2) / 10)
        # The real battery thread already runs before the experiment starts.
        lines = [line for k in range(math.floor((now - self.ring) / 10), last + 1)
                 if now - self.ring < self.time(k) for line in self.iteration(k)]
        t = self.time(last)
        v = self.sample_volts(t)
        resets = [line for line in lines if "resetBatteryMeter=(1)" in line]
        return dict(boot_id="boot-a", monotonic=now, voltage_mv=v, temp_deci_c=300, capacity=100,
                    status=self.status(t), car_mah=self.car(last), dod0=0, zcv_mv=v + 20,
                    cached_current_deci_ma=100 if t <= self.discharge_from else round(self.rate*36000),
                    online=True, meter_reset_marker=resets[-1] if resets else None,
                    kernel_lines=lines, kernel_before_s=float(lines[-1][1:lines[-1].index("]")]) if lines else None,
                    bcct_paused_at_sample=now > self.discharge_from)


def replay(phone, until=40000):
    cycle = cal.Cycle("boot-a", 0)
    for now in range(0, until, 30):
        cycle.step(phone.row(now))
        if cycle.phase == "done":
            return cycle
    raise AssertionError("replay did not finish")


def sequence_volts(end, start=400.2, low=4262):
    """4350 mV until the anchor, 4282 mV until the first recharge entry at `start`, then in the
    stock recharge band until the last reset at `end`, then a discharge to 3550 mV."""
    def volts(t):
        if t <= 300:
            return 4350
        if t < start - 0.1:
            return 4282
        if t <= end + 0.1:
            return round(4278 - (t - start) * (4278 - low) / (end - start))
        return max(3500, round(4258 - (t - end - 10) * 0.16))
    return volts


class Calibration(unittest.TestCase):
    def setUp(self):
        self.cycle = cal.Cycle("boot-a", 0)

    def sample(self, now=0, **changes):
        voltage = changes.get("voltage_mv") or 4350
        row = Phone(volts=lambda t: voltage).row(now)
        row.update(car_mah=0, cached_current_deci_ma=100)
        row.update(changes)
        return row

    def full(self):
        for now in range(0, 301, 30):
            self.cycle.step(self.sample(now))
        self.assertEqual(self.cycle.phase, "discharge")

    def drain(self):
        self.full()
        # A 600 mAh discharge over one hour, with no counter reset.
        for n in range(1, 121):
            self.cycle.step(self.sample(300 + 30*n, status="Charging", car_mah=-5*n,
                                       voltage_mv=4350 - 6*n, zcv_mv=4350 - 6*n))

    def test_full_label_while_kernel_still_topoff_does_not_anchor(self):
        # E2: Full/100/DOD0=0 and low cached current while CHR_TOP_OFF still runs.
        def topoff(t, lines):
            return [line.replace("[BATTERY] Battery full !!",
                                 "[BATTERY] Top Off mode charge, timer=3800 on 6400 !!")
                    for line in lines]
        phone = Phone(mutate=topoff)
        cycle = cal.Cycle("boot-a", 0)
        for now in range(0, 601, 30):
            cycle.step(phone.row(now))
        self.assertEqual(cycle.phase, "charge")
        self.assertIsNone(cycle.anchor)

    def test_real_stock_full_after_topoff_requires_a_complete_window(self):
        def topoff(t, lines):
            if t < 600:
                return [line.replace("[BATTERY] Battery full !!", "[BATTERY] Top Off mode charge")
                        for line in lines]
            return lines
        phone = Phone(mutate=topoff, resets=[600.2])
        cycle = cal.Cycle("boot-a", 0)
        for now in range(0, 901, 30):
            cycle.step(phone.row(now))
            self.assertEqual(cycle.phase, "charge")
        cycle.step(phone.row(930))
        self.assertEqual(cycle.phase, "discharge")
        self.assertEqual(cycle.anchor["monotonic"], 930)

    def test_missing_full_action_and_kernel_iteration_gap_do_not_anchor(self):
        for missing in ("full", "iteration"):
            def mutate(t, lines):
                if missing == "full":
                    return [line for line in lines if "Battery full !!" not in line]
                return [] if 200 < t < 230 else lines
            with self.subTest(missing=missing):
                phone = Phone(mutate=mutate)
                cycle = cal.Cycle("boot-a", 0)
                for now in range(0, 481, 30):
                    cycle.step(phone.row(now))
                self.assertEqual(cycle.phase, "charge")

    def test_repeated_old_full_kernel_ring_cannot_anchor(self):
        phone = Phone()
        cycle = cal.Cycle("boot-a", 0)
        for now in range(0, 601, 30):
            row = phone.row(0)
            row["monotonic"] = now
            cycle.step(row)
            self.assertEqual(cycle.phase, "charge")

    def test_full_label_at_low_voltage_is_not_a_full_anchor(self):
        for now in range(0, 601, 30):
            self.cycle.step(self.sample(now, voltage_mv=3700))
        self.assertEqual(self.cycle.phase, "charge")

    def test_charging100_is_not_a_full_anchor(self):
        for now in range(0, 601, 30):
            self.cycle.step(self.sample(now, status="Charging"))
        self.assertEqual(self.cycle.phase, "charge")

    def test_safe_endpoint_measures_usable_charge_without_poweroff(self):
        self.drain()
        self.cycle.step(self.sample(3930, voltage_mv=3550, zcv_mv=3570, car_mah=-606))
        self.assertEqual(self.cycle.phase, "done")
        self.assertEqual(self.cycle.result["usable_capacity_mah"], 606)
        self.assertEqual(self.cycle.result["endpoint"], "voltage_reserve")
        self.assertFalse(self.cycle.result["physical_soc_calibrated"])
        self.assertNotIn("kernel_lines", self.cycle.result["full_anchor"])

    def test_small_discharge_does_not_produce_a_capacity(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "insufficient discharge"):
            self.cycle.step(self.sample(330, voltage_mv=3550, car_mah=-2))

    def test_voltage_reserve_during_initial_charging_does_not_start_drain(self):
        self.cycle.step(self.sample(voltage_mv=3500, capacity=12, status="Charging"))
        self.assertEqual(self.cycle.phase, "charge")

    def test_temperature_cable_boot_and_read_failures_abort(self):
        for changes in ({"temp_deci_c": 401}, {"temp_deci_c": -1}, {"online": False},
                        {"boot_id": "boot-b"}, {"car_mah": None}, {"voltage_mv": None},
                        {"capacity": 6}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                cal.Cycle("boot-a", 0).step(self.sample(**changes))

    def test_counter_reset_during_discharge_invalidates_measurement(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "reset"):
            self.cycle.step(self.sample(330, dod0=20))

    def test_dod0_change_has_its_own_message(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "DOD0"):
            self.cycle.step(self.sample(330, dod0=20))

    def test_counter_jump_is_not_accepted_as_charge(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "counter jump"):
            self.cycle.step(self.sample(330, car_mah=-100))

    def test_capacity_reserve_completes_a_sufficient_discharge(self):
        self.drain()
        self.cycle.step(self.sample(3930, capacity=6, voltage_mv=3600, car_mah=-606))
        self.assertEqual(self.cycle.result["endpoint"], "driver_capacity_reserve")

    def test_gap_and_deadline_cannot_be_treated_as_continuous_calibration(self):
        self.cycle.step(self.sample())
        with self.assertRaisesRegex(ValueError, "gap"):
            self.cycle.step(self.sample(100))
        with self.assertRaisesRegex(ValueError, "deadline"):
            cal.Cycle("boot-a", 0).step(self.sample(32*3600))

    def test_bcct_pause_must_be_in_force_during_discharge(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "BCCT"):
            self.cycle.step(self.sample(330, bcct_paused_at_sample=False))

    def test_silent_battery_thread_aborts_discharge(self):
        self.full()
        with self.assertRaisesRegex(ValueError, "battery thread"):
            self.cycle.step(self.sample(330, kernel_lines=[]))

    def test_kernel_log_coverage_gap_aborts_discharge(self):
        self.full()
        row = self.sample(330)
        row["kernel_lines"] = row["kernel_lines"][-2:]  # Ring flooded: older lines lost.
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.cycle.step(row)


class ResetBoundaries(unittest.TestCase):
    """The stock recharge/top-off reset (diagnosis A.3) is the only permitted boundary."""

    def test_aged_out_reset_line_is_not_a_reset(self):
        # Stock full reset just before the anchor; the line then leaves the kernel ring.
        phone = Phone(rate=0.06, resets=[10.2], ring=350, discharge_from=330,
                      volts=lambda t: 4350 if t < 2200 else 3540)
        cycle = replay(phone)
        self.assertEqual(cycle.result["boundaries"], [])

    def test_reset_without_stock_sequence_aborts(self):
        phone = Phone(rate=0.03, resets=[320.2])
        with self.assertRaisesRegex(ValueError, "no kernel recharge entry"):
            replay(phone)

    def test_stale_car_replay_counts_every_segment(self):
        # Sol S1: each reset line is seen before the cache refreshes (stale CAR ~14 mAh),
        # three times, then a 200 mAh final discharge. Must not report about 200 mAh.
        # Rows come every 30 s, so a reset at x10.2 s is seen at x20 s before the x20.2 s refresh.
        rate = 0.045  # mAh/s, 162 mA
        phone = Phone(rate=rate, volts=sequence_volts(1310.2, start=420.2),
                      entries=[420.2, 720.2, 1020.2, 1320.2], resets=[710.2, 1010.2, 1310.2])
        cycle = replay(phone)
        result = cycle.result
        end = math.floor((result["reserve"]["monotonic"] - 0.2) / 10) * 10 + 0.2
        true_total = rate * (end - 300)
        self.assertAlmostEqual(result["usable_capacity_mah"], true_total - 3 * 60 * rate, delta=3)
        uncounted = sum(b["uncounted_current_integral_mah"] for b in result["boundaries"])
        self.assertAlmostEqual(result["usable_capacity_mah"] + uncounted, true_total, delta=3)
        self.assertEqual(len(result["boundaries"]), 3)
        for boundary in result["boundaries"]:
            # Closed at the last pre-reset sample, opened at the first demonstrably post-reset one.
            self.assertEqual(boundary["uncounted_seconds"], 60)
            self.assertEqual(boundary["reset_kernel_s"] - boundary["recharge_kernel_s"], 290)
            self.assertGreater(boundary["post_reset_kernel_s"], boundary["reset_kernel_s"])
        self.assertEqual(result["uncounted_current_integral_mah"], uncounted)

    def test_large_stale_car_does_not_reject_a_legitimate_reset(self):
        rate = 0.12  # 432 mA: the stale pre-reset CAR (-49) exceeds the counter-jump allowance.
        phone = Phone(rate=rate, volts=sequence_volts(710.2, start=420.2), entries=[420.2, 720.2],
                      resets=[710.2])
        cycle = replay(phone)
        result = cycle.result
        end = math.floor((result["reserve"]["monotonic"] - 0.2) / 10) * 10 + 0.2
        self.assertAlmostEqual(result["usable_capacity_mah"], rate * (end - 300 - 60), delta=2)
        self.assertEqual([b["uncounted_seconds"] for b in result["boundaries"]], [60])

    def test_reset_and_refresh_in_one_sample_open_the_next_segment_at_once(self):
        rate = 0.05
        phone = Phone(rate=rate, volts=sequence_volts(670.2), entries=[400.2, 680.2], resets=[670.2])
        cycle = replay(phone)
        self.assertEqual([b["uncounted_seconds"] for b in cycle.result["boundaries"]], [30])

    def test_no_post_reset_snapshot_in_time_aborts(self):
        def no_refresh(t, lines):  # The battery thread keeps logging but never refreshes the gauge.
            return [line for line in lines if not (t > 690.2 and "AvgVbat" in line)]
        phone = Phone(rate=0.01, volts=sequence_volts(690.2), entries=[400.2, 700.2], resets=[690.2],
                      mutate=no_refresh)
        with self.assertRaisesRegex(ValueError, "post-reset"):
            replay(phone)

    def test_event_voltage_outside_band_aborts_even_with_in_band_sample(self):
        # Sol S2: kernel recharge entry at 4248 mV, sampled batt_vol 4260 mV.
        phone = Phone(rate=0.01, volts=lambda t: 4350 if t <= 300 else 4248,
                      sample_volts=lambda t: 4350 if t <= 300 else 4260, entries=[400.2], resets=[690.2])
        with self.assertRaisesRegex(ValueError, "4248"):
            replay(phone)

    def test_cable_meter_reset_during_discharge_aborts(self):
        # Sol S4: the vendor plug in/out path resets the meter without a "(1)" line.
        def cable(t, lines):
            if abs(t - 1500.2) < 0.1:
                return lines + [f"[{t + KOFF + 0.5:.6f}]  (0)[176:bat_thread_kthr]"
                                "Charger plug in/out, Call battery_meter_reset. (1)"]
            return lines
        phone = Phone(rate=0.06, volts=lambda t: 4350 if t <= 300 else 4200, mutate=cable)
        with self.assertRaisesRegex(ValueError, "plug in/out"):
            replay(phone)

    def test_late_post_reset_proof_aborts(self):
        # Sol S5: proof that arrives after the 75 s bound must not open the next segment.
        def late(t, lines):
            return [line for line in lines if not (690.3 < t < 780.3 and "AvgVbat" in line)]
        phone = Phone(rate=0.01, volts=sequence_volts(690.2), entries=[400.2, 700.2], resets=[690.2],
                      mutate=late)
        with self.assertRaisesRegex(ValueError, "post-reset"):
            replay(phone)

    def test_missing_full_evidence_aborts(self):
        def no_full(t, lines):
            return [line for line in lines if not (t > 300 and "Battery full" in line)]
        for name, changes in (("kernel", dict(mutate=no_full)),
                              ("sysfs", dict(status=lambda t: "Full" if t <= 300 else "Charging"))):
            with self.subTest(name), self.assertRaisesRegex(ValueError, "Full"):
                replay(Phone(rate=0.01, volts=sequence_volts(690.2), entries=[400.2, 700.2],
                             resets=[690.2], **changes))

    def test_missing_pause_evidence_aborts(self):
        def unpaused(row):
            return dict(row, bcct_paused_at_sample=False)
        kernel = Phone(rate=0.01, volts=sequence_volts(690.2), entries=[400.2, 700.2], resets=[690.2],
                       bcct=lambda t: 1 if t < 600 else 0)
        with self.subTest("kernel bcct flag"), self.assertRaisesRegex(ValueError, "BCCT"):
            replay(kernel)
        guard = Phone(rate=0.01, volts=sequence_volts(690.2), entries=[400.2, 700.2], resets=[690.2])
        cycle = cal.Cycle("boot-a", 0)
        with self.subTest("guard readback"), self.assertRaisesRegex(ValueError, "BCCT"):
            for now in range(0, 3000, 30):
                row = guard.row(now)
                cycle.step(unpaused(row) if now >= 600 else row)

    def test_reset_without_ui_soc_100_aborts(self):
        phone = Phone(rate=0.01, volts=sequence_volts(690.2), entries=[400.2, 700.2], resets=[690.2],
                      reset_ui=99)
        with self.assertRaisesRegex(ValueError, "UI_SOC"):
            replay(phone)

    def test_reset_outside_expected_delay_aborts(self):
        phone = Phone(rate=0.01, volts=sequence_volts(600.2), entries=[400.2, 610.2], resets=[600.2])
        with self.assertRaisesRegex(ValueError, "250-320"):
            replay(phone)


class Sampling(unittest.TestCase):
    def capture(self, counters, usb="1"):
        counters = iter(counters)
        values = dict(online=usb, FG_g_fg_dbg_d0="0", batt_vol="4350", batt_temp="300",
                      capacity="100", status="Full", boot_id="boot-a",
                      FG_g_fg_dbg_bat_zcv="4350", FG_g_fg_dbg_bat_current="100")
        def read(path, *args, **kwargs):
            if path.name == "FG_g_fg_dbg_bat_car":
                return str(next(counters))
            return values[path.name]
        reads = iter(["[10.0]  (0)[176:bat_thread_kthr]UI_SOC=(100), resetBatteryMeter=(1)\n",
                      "[10.0]  (0)[176:bat_thread_kthr]UI_SOC=(100), resetBatteryMeter=(1)\n"
                      "[20.0]  (0)[176:bat_thread_kthr]AvgVbat=(4350,4350)\n[20.5] usb: unrelated\n"])
        with mock.patch.object(Path, "read_text", read), mock.patch.object(
                cal.subprocess, "run", side_effect=lambda *a, **k: SimpleNamespace(stdout=next(reads))):
            return cal.sample()

    def test_capture_discards_a_cache_update_and_returns_a_stable_pair(self):
        row = self.capture([10, 12, 14, 14])
        self.assertEqual((row["car_mah"], row["dod0"]), (14, 0))
        self.assertEqual(row["meter_reset_marker"],
                         "[10.0]  (0)[176:bat_thread_kthr]UI_SOC=(100), resetBatteryMeter=(1)")

    def test_kernel_log_is_read_before_and_after_the_counters(self):
        row = self.capture([10, 10])
        self.assertEqual(row["kernel_before_s"], 10.0)  # Newest battery-thread line before the CAR read.
        self.assertEqual(row["kernel_lines"],
                         ["[10.0]  (0)[176:bat_thread_kthr]UI_SOC=(100), resetBatteryMeter=(1)",
                          "[20.0]  (0)[176:bat_thread_kthr]AvgVbat=(4350,4350)"])

    def test_capture_fails_after_three_incoherent_pairs(self):
        with self.assertRaisesRegex(ValueError, "cache changed"):
            self.capture([10, 12, 14, 16, 18, 20])

    def test_invalid_online_value_is_not_a_connected_cable(self):
        with self.assertRaisesRegex(ValueError, "unknown cable"):
            self.capture([], usb="2")


class Cleanup(unittest.TestCase):
    def test_trace_preserves_the_complete_captured_kernel_ring(self):
        row = Calibration().sample(30)
        original = list(row["kernel_lines"])
        directory, *_ = self.run_with(row)
        recorded = json.loads((directory / "samples.jsonl").read_text())
        self.assertEqual(recorded["kernel_lines"], original)

    def test_oversized_escaped_row_aborts_with_complete_rejected_evidence(self):
        row = Calibration().sample(30)
        row["padding"] = "\x00" * (128*1024//6)
        directory, root, service_state, hardware, error = self.run_with(row)
        self.assertIn("sample byte cap", str(error))
        self.assertEqual((directory/"samples.jsonl").read_text(), "")
        self.assertEqual(json.loads((directory/"failure.json").read_text())["rejected_row"], row)
        self.assertEqual(service_state.read_text(), "started")
        self.assertEqual(hardware.read_text(), "0")

    def test_sample_byte_caps_include_json_escaping_and_newline(self):
        value = {"ring": "\x00"}
        encoded = json.dumps(value, allow_nan=False) + "\n"
        with mock.patch.object(cal, "MAX_SAMPLE_BYTES", len(encoded)), \
                mock.patch.object(cal, "MAX_TRACE_BYTES", 2*len(encoded)):
            self.assertEqual(cal.sample_json(value, len(encoded)), encoded)
            with self.assertRaisesRegex(ValueError, "trace byte cap"):
                cal.sample_json(value, len(encoded)+1)
            with self.assertRaisesRegex(ValueError, "sample byte cap"):
                cal.sample_json({"ring": "\x00\x00"}, 0)

    def test_dmesg_capture_aborts_without_truncation_at_byte_cap(self):
        raw = "é"*(128*1024//2+1)
        with mock.patch.object(cal.subprocess, "run", return_value=SimpleNamespace(stdout=raw)):
            with self.assertRaisesRegex(ValueError, "dmesg byte cap"):
                cal.dmesg()

    def test_oversized_state_never_replaces_previous_record(self):
        row = Calibration().sample(30)
        with mock.patch.object(cal, "MAX_RECORD_BYTES", 10):
            directory, root, service_state, hardware, error = self.run_with(row)
        self.assertIn("record byte cap", str(error))
        self.assertFalse((directory/"state.json").exists())
        self.assertFalse((directory/"state.json.tmp").exists())
        self.assertEqual(service_state.read_text(), "started")
        self.assertEqual(hardware.read_text(), "0")

    def test_oversized_service_output_is_not_written_to_wrapper_log(self):
        row = Calibration().sample(30)
        with mock.patch.object(cal, "MAX_SERVICE_BYTES", 10), mock.patch("builtins.print") as output:
            directory, root, service_state, hardware, error = self.run_with(row)
        self.assertIn("service log byte cap", str(error))
        output.assert_not_called()
        self.assertEqual(service_state.read_text(), "started")

    def test_c13_guard_record_counts_complete_utf8_line_before_append(self):
        with tempfile.TemporaryDirectory() as temp:
            guard = SimpleNamespace(log=Path(temp)/"guard.log")
            cal.guard_record(guard, "é"*245+"x")  # 491 body bytes + 21 timestamp/newline bytes.
            before = guard.log.read_bytes()
            self.assertEqual(len(before), 512)
            with self.assertRaisesRegex(ValueError, "guard log byte cap"):
                cal.guard_record(guard, "é"*246)
            self.assertEqual(guard.log.read_bytes(), before)

    def test_c13_guard_record_count_cap_has_no_reset_or_truncated_append(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(cal, "MAX_GUARD_RECORDS", 2):
            guard = SimpleNamespace(log=Path(temp)/"guard.log")
            cal.guard_record(guard, "first")
            cal.guard_record(guard, "second")
            before = guard.log.read_bytes()
            with self.assertRaisesRegex(ValueError, "guard log count cap"):
                cal.guard_record(guard, "third")
            self.assertEqual(guard.log.read_bytes(), before)

    def test_c13_guard_append_error_keeps_the_bounded_stderr_fallback(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch("builtins.print") as output:
            guard = SimpleNamespace(log=Path(temp))  # Actual append fails: this is a directory.
            cal.guard_record(guard, "status unavailable")
        self.assertEqual(output.call_count, 1)
        self.assertLessEqual(len(output.call_args.args[0].encode("utf-8")), 512)
        self.assertEqual(output.call_args.kwargs["end"], "")

    def test_oversized_publish_error_aborts_before_guard_log_and_still_restores(self):
        row = Calibration().sample(30)
        directory, root, service_state, hardware, error = self.run_with(
            row, publish_error=OSError("é"*300))
        self.assertIn("guard log byte cap", str(error))
        self.assertFalse((root/"guard.log").exists())
        self.assertEqual(service_state.read_text(), "started")
        self.assertEqual(hardware.read_text(), "0")
        self.assertIn("guard log byte cap", json.loads((directory/"failure.json").read_text())["error"])

    def test_read_failure_releases_real_guard_and_restores_production(self):
        directory, root, service_state, hardware, error = self.run_with(ValueError("sensor unavailable"))
        self.assertEqual(str(error), "sensor unavailable")
        self.assertEqual(service_state.read_text(), "started")
        self.assertEqual(hardware.read_text(), "0")
        self.assertTrue(json.loads((root / "guard.json").read_text())["stopped"])
        self.assertEqual(json.loads((directory / "failure.json").read_text())["error"], "sensor unavailable")

    def test_abort_saves_rejected_row_and_battery_kernel_lines(self):
        rejected = Calibration().sample(30, temp_deci_c=401)
        dmesg = ("[1.0] usb: unrelated\n"
                 "[57740.2]  (0)[176:bat_thread_kthr]UI_SOC=(100), resetBatteryMeter=(1)\n")
        directory, *_ = self.run_with(rejected, dmesg)
        failure = json.loads((directory / "failure.json").read_text())
        self.assertEqual(failure["rejected_row"]["temp_deci_c"], 401)
        self.assertEqual(failure["battery_kernel_lines"],
                         ["[57740.2]  (0)[176:bat_thread_kthr]UI_SOC=(100), resetBatteryMeter=(1)"])

    def test_unserializable_rejected_row_keeps_the_original_abort(self):
        # Sol S3: a NaN time must not turn the abort record into a JSON error.
        rejected = Calibration().sample(30)
        rejected["monotonic"] = float("nan")
        directory, root, service_state, hardware, error = self.run_with(rejected)
        self.assertEqual(str(error), "invalid measurement time")
        failure = json.loads((directory / "failure.json").read_text())
        self.assertEqual(failure["error"], "invalid measurement time")
        self.assertEqual(failure["rejected_row"]["monotonic"], "nan")
        self.assertEqual(service_state.read_text(), "started")
        self.assertEqual(hardware.read_text(), "0")

    def test_failing_stderr_cannot_mask_the_original_abort(self):
        # Sol S3 (round 2): both failure.json writes and the stderr fallback fail.
        real_dump = cal.json.dump
        def dump(value, f, **kwargs):
            if isinstance(value, dict) and "error" in value:
                raise OSError("disk full")
            return real_dump(value, f, **kwargs)
        class Broken:
            def write(self, _):
                raise OSError("stderr filesystem full")
            def flush(self):
                raise OSError("stderr filesystem full")
        with mock.patch.object(cal.json, "dump", side_effect=dump), mock.patch.object(cal.sys, "stderr", Broken()):
            directory, root, service_state, hardware, error = self.run_with(ValueError("sensor unavailable"))
        self.assertEqual(str(error), "sensor unavailable")
        self.assertEqual(service_state.read_text(), "started")
        self.assertEqual(hardware.read_text(), "0")

    def run_with(self, second, dmesg="", publish_error=None):
        # Only device reads and OpenRC are replaced; use Guard.close's real publication/cleanup.
        lib = str(Path(__file__).resolve().parents[1] / "rootfs/overlay/usr/lib")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with mock.patch.object(sys, "path", [lib, *sys.path]):
            from i14ui import charging
            root = Path(tmp)
            directory = root / "experiment"
            compatible = root / "compatible"
            compatible.write_bytes(b"mediatek,MT6735\x00")
            feedback = root / "feedback"
            feedback.write_text("get_usb_current_unlimited=0\ncmd_discharging = -1\n")
            service_state = root / "service"
            service_state.write_text("started")
            hardware = root / "pause"
            hardware.write_text("1")
            class Control:
                def paused(self):
                    return hardware.read_text() == "1"
                def set_paused(self, paused):
                    hardware.write_text("1" if paused else "0")
                def release(self):
                    hardware.write_text("0")
            original_guard = charging.Guard
            def guard():
                return original_guard(control=Control(), status_path=root / "guard.json", log=root / "guard.log")
            def path(value):
                return {"/mnt/data/media/0/i14-linux": root,
                        "/sys/firmware/devicetree/base/compatible": compatible,
                        "/proc/mtk_battery_cmd/battery_cmd": feedback}.get(str(value), Path(value))
            def service(command, **kwargs):
                action = command[-1]
                if action in ("stop", "start"):
                    service_state.write_text("stopped" if action == "stop" else "started")
                return subprocess.CompletedProcess(command, 0, dmesg if command == ["dmesg"] else "", "")
            first = Calibration().sample()
            original_replace = charging.os.replace
            def replace(source, target):
                if publish_error is not None and str(target) == str(root/"guard.json"):
                    raise publish_error
                return original_replace(source, target)
            with mock.patch.object(cal, "Path", side_effect=path), \
                    mock.patch.object(cal.os, "geteuid", return_value=0), \
                    mock.patch.object(cal.os, "uname", return_value=SimpleNamespace(
                        nodename="i14", release="3.18.19", machine="aarch64")), \
                    mock.patch.object(cal, "sample", side_effect=[first, second, ValueError("end of fixture")]), \
                    mock.patch.object(cal.time, "sleep"), mock.patch.object(cal.signal, "signal"), \
                    mock.patch.object(cal.subprocess, "run", side_effect=service), \
                    mock.patch.object(charging.os, "replace", side_effect=replace), \
                    mock.patch.object(charging, "Guard", side_effect=guard):
                with self.assertRaises(ValueError) as caught:
                    cal.run(directory, "boot-a")
            return directory, root, service_state, hardware, caught.exception


if __name__ == "__main__":
    unittest.main()
