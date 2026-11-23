#!/usr/bin/env python3
"""Measure usable charge from stock-full to a reserve; restore i14-power on exit.

Runs only on the pinned I14, under an external timeout. No current_cmd reads,
fuel-gauge resets, firmware writes or intentional shutdown. CAR units and the
result retain the stock meter's unverified physical calibration.
"""
import argparse
import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


RESET_LINE = "resetBatteryMeter=(1)"
BATTERY_LINE = re.compile(r"BATTERY|UI_SOC|resetBatteryMeter|AvgVbat|Re-charging|Top Off|CC mode|bat_thread|plug in/out")
# The vendor cable path resets the meter without a "(1)" line: any new one during discharge aborts.
CABLE_RESET = "Charger plug in/out"
STAMP = re.compile(r"\s*(?:<\d+>)?\[\s*(\d+(?:\.\d+)?)\]")
AVERAGE = re.compile(r"AvgVbat=\((-?\d+),.*,UI_SOC=(-?\d+),.* bcct:(-?\d+):(-?\d+)")
RECHARGE = re.compile(r"Battery Enter Re-charging!! , vbat=\((-?\d+)\)")
RESET = re.compile(r"UI_SOC=\((-?\d+)\), resetBatteryMeter=\(1\)")
# The only permitted reset during discharge: the stock recharge/top-off reset while stock Full,
# cable attached and BCCT paused (protocol amendment, 8 November; diagnosis report section A.3).
RESET_BAND_MV = (4255, 4285)  # batt_vol from the recharge entry until the reset
RESET_DELAY_S = (250, 320)    # "Enter Re-charging" to the reset; about 280 s predicted from the source
ITERATION_GAP_S = 15          # longest gap between battery-thread iterations inside that sequence
POST_RESET_WAIT_S = 75        # bounded wait for a fuel-gauge refresh after the reset
KERNEL_HISTORY_S = 900
# Storage overflow is a terminal experiment failure, never permission to truncate evidence.
MAX_DMESG_BYTES = 128*1024+1
MAX_SAMPLE_BYTES = 128*1024
MAX_TRACE_BYTES = 3840*MAX_SAMPLE_BYTES
MAX_RECORD_BYTES = 8*1024**2
MAX_SERVICE_BYTES = 1024**2
MAX_GUARD_LOG_BYTES = 512
MAX_GUARD_RECORDS = 3842  # 3840 sample publications + close publication + release error.


def guard_record(guard, text):
    """C13-only replacement for Guard.record; never truncate or alter production code."""
    line = f"{datetime.datetime.now():%F %T} {text}\n"
    if len(line.encode("utf-8")) > MAX_GUARD_LOG_BYTES:
        raise ValueError("guard log byte cap exceeded")
    count = getattr(guard, "_calibration_records", 0)
    if count >= MAX_GUARD_RECORDS:
        raise ValueError("guard log count cap exceeded")
    guard._calibration_records = count + 1  # Failed appends also consume this bound.
    try:
        with open(guard.log, "a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError:
        print(line, file=sys.stderr, end="")


def sample_json(row, used=0):
    data = json.dumps(row, allow_nan=False) + "\n"  # ensure_ascii makes len equal UTF-8 bytes.
    if len(data) > MAX_SAMPLE_BYTES:
        raise ValueError("sample byte cap exceeded; no truncated row accepted")
    if used + len(data) > MAX_TRACE_BYTES:
        raise ValueError("trace byte cap exceeded")
    return data


def kernel_stamp(line):
    match = STAMP.match(line)
    return float(match.group(1)) if match else None


def json_safe(value):
    """Abort evidence must serialize: non-finite floats and unknown types become strings."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return repr(value)


class Cycle:
    def __init__(self, boot_id, started):
        self.boot_id, self.started = boot_id, started
        self.phase = "charge"
        self.result = None
        self.previous = None
        self.full_since = None
        self.anchor = None
        self.points = 0
        self.segments = []
        self.boundaries = []
        self.awaiting = None        # Permitted reset seen; next segment waits for a post-reset snapshot.
        self.kernel = {}            # Battery kernel line -> kernel time, across samples.
        self.kernel_newest = None   # Newest battery-thread kernel time already read.
        self.covered_from = None    # Kernel time since which reads have overlapped without a gap.
        self.new_kernel_lines = []  # Lines first seen in the latest sample, for the sample log.
        self.recent = []            # Recent accepted rows (without kernel lines).

    def read_kernel(self, row):
        """Merge the sample's battery kernel lines; return the reset lines new since the last read."""
        lines = row.get("kernel_lines")
        if lines is None:
            lines = []
        before = row.get("kernel_before_s")
        if (type(lines) is not list or any(type(line) is not str for line in lines) or
                (before is not None and (type(before) not in (int, float) or not math.isfinite(before)))):
            raise ValueError("invalid kernel log capture")
        parsed = []
        for line in lines:
            stamp = kernel_stamp(line)
            if stamp is None:
                if RESET_LINE in line:
                    raise ValueError("kernel reset line without timestamp")
                continue
            parsed.append((stamp, line))
        thread = [stamp for stamp, line in parsed if "bat_thread" in line]
        previous = self.kernel_newest
        if self.phase == "discharge":
            if not thread or previous is None or max(thread) <= previous:
                raise ValueError("battery thread silent in the kernel log")
            if min(thread) > previous:
                raise ValueError("kernel log coverage gap: battery lines may be missing")
        if not thread:
            self.covered_from = None
        elif self.covered_from is None or previous is None or min(thread) > previous:
            self.covered_from = min(thread)
        resets = [(stamp, line) for stamp, line in parsed
                  if RESET_LINE in line and previous is not None and stamp > previous]
        if self.phase == "discharge" and any(CABLE_RESET in line and stamp > previous for stamp, line in parsed):
            raise ValueError("fuel-gauge reset during discharge: charger plug in/out meter reset")
        self.new_kernel_lines = [line for stamp, line in parsed
                                 if line not in self.kernel and (previous is None or stamp > previous)]
        self.kernel.update((line, stamp) for stamp, line in parsed)
        if thread:
            self.kernel_newest = max(thread + ([previous] if previous is not None else []))
        if self.kernel_newest is not None:
            self.kernel = {line: stamp for line, stamp in self.kernel.items()
                           if stamp >= self.kernel_newest - KERNEL_HISTORY_S}
        return resets

    def history(self, low, high):
        return sorted((stamp, line) for line, stamp in self.kernel.items() if low <= stamp <= high)

    def identify(self, reset, row):
        """Positively identify the stock recharge/top-off reset from kernel-timestamped evidence."""
        stamp, line = reset
        def fail(reason):
            raise ValueError(f"fuel-gauge reset during discharge: {reason}")
        match = RESET.search(line)
        if not match or int(match.group(1)) != 100:
            fail("reset line without UI_SOC=(100)")
        entries = [(at, int(RECHARGE.search(text).group(1)))
                   for at, text in self.history(stamp - KERNEL_HISTORY_S, stamp)
                   if RECHARGE.search(text) and at < stamp]
        if not entries:
            fail("no kernel recharge entry before the reset")
        entered, vbat = entries[-1]
        delay = stamp - entered
        if not RESET_DELAY_S[0] <= delay <= RESET_DELAY_S[1]:
            fail(f"reset {delay:.1f} s after the recharge entry, expected "
                 f"{RESET_DELAY_S[0]}-{RESET_DELAY_S[1]} s")
        if self.covered_from is None or self.covered_from > entered - 10:
            fail("kernel log does not cover the recharge entry")
        if entered <= self.anchor["kernel_after_s"]:
            fail("recharge entry before the full anchor")
        if not RESET_BAND_MV[0] <= vbat <= RESET_BAND_MV[1]:
            fail(f"recharge entry at batt_vol {vbat} mV, outside {RESET_BAND_MV[0]}-{RESET_BAND_MV[1]}")
        entry = self.history(entered - 1, entered)
        if not any("[BATTERY] Battery full !!" in text for at, text in entry):
            fail("no stock Full state at the recharge entry")
        averages = [(at, AVERAGE.search(text)) for at, text in self.history(entered - 1, stamp)
                    if "bat_thread" in text and AVERAGE.search(text)]
        if not averages or averages[0][0] > entered or int(averages[0][1].group(1)) != vbat:
            fail("kernel voltage at the recharge entry is missing or disagrees")
        times = [at for at, _ in averages] + [stamp]
        if max(b - a for a, b in zip(times, times[1:])) > ITERATION_GAP_S:
            fail("battery-thread iterations missing inside the stock sequence")
        for at, average in averages:
            volts, ui, bcct, unlimited = (int(value) for value in average.groups())
            if bcct != 1 or unlimited != 0:
                fail(f"BCCT pause not in force at kernel time {at:.1f} (bcct:{bcct}:{unlimited})")
            if ui != 100:
                fail(f"UI_SOC {ui} at kernel time {at:.1f}")
            if not RESET_BAND_MV[0] <= volts <= RESET_BAND_MV[1]:
                fail(f"batt_vol {volts} mV at kernel time {at:.1f}, outside "
                     f"{RESET_BAND_MV[0]}-{RESET_BAND_MV[1]}")
        sequence = self.history(entered, stamp)
        topoff = [at for at, text in sequence if "Top Off mode charge" in text]
        if not topoff or topoff[-1] < stamp - 20:
            fail("no stock top-off before the reset")
        if any(RESET_LINE in text for at, text in sequence if at != stamp):
            fail("another reset inside the stock sequence")
        known = [r for r in self.recent if r["kernel_after_s"] is not None]
        before = [r for r in known if r["kernel_after_s"] < entered]
        if not before:
            fail("no sample before the recharge entry")
        for sampled in [before[-1]] + [r for r in known if r["kernel_after_s"] >= entered] + [row]:
            if sampled["status"] != "Full" or sampled["capacity"] != 100:
                fail(f"sample status {sampled['status']} capacity {sampled['capacity']}, expected Full 100")
            if not RESET_BAND_MV[0] <= sampled["voltage_mv"] <= RESET_BAND_MV[1]:
                fail(f"sampled batt_vol {sampled['voltage_mv']} mV, outside "
                     f"{RESET_BAND_MV[0]}-{RESET_BAND_MV[1]}")
            if sampled.get("bcct_paused_at_sample") is not True:
                fail("BCCT pause not verified for a sample in the stock sequence")
        return dict(reset_kernel_s=stamp, recharge_kernel_s=entered, recharge_vbat_mv=vbat,
                    first_seen_monotonic=row["monotonic"], closed_monotonic=self.previous["monotonic"],
                    closed_car_mah=self.previous["car_mah"], uncounted_current_integral_mah=0.0)

    def post_reset(self, row):
        """Kernel time of an AvgVbat line printed after the reset and before the CAR read, if any.

        Each battery-thread iteration refreshes the cached CAR before it prints AvgVbat, so such a
        line proves the cache already reflects the reset (vendor battery_common.c).
        """
        before = row.get("kernel_before_s")
        if before is None:
            return None
        lines = self.history(self.awaiting["reset_kernel_s"], before)
        return next((at for at, text in lines
                     if at > self.awaiting["reset_kernel_s"] and "bat_thread" in text and "AvgVbat=" in text),
                    None)

    def stock_full_window(self):
        """Sysfs Full can outlive CHR_BATFULL: require 300 s of actual Full actions."""
        end = self.kernel_newest
        if (end is None or self.covered_from is None or self.covered_from > end - 300 or
                not self.previous or self.previous["kernel_after_s"] is None or
                end <= self.previous["kernel_after_s"]):
            return False
        lines = self.history(end - 300 - ITERATION_GAP_S, end)
        averages = [at for at, text in lines if "bat_thread" in text and AVERAGE.search(text)]
        before = [at for at in averages if at <= end - 300]
        if not before:
            return False
        start = before[-1]
        averages = [at for at in averages if at >= start]
        if max(b - a for a, b in zip(averages, averages[1:] + [end])) > ITERATION_GAP_S:
            return False
        window = [(at, text) for at, text in lines if at >= start]
        if any(RESET_LINE in text or CABLE_RESET in text or RECHARGE.search(text) or
               "Top Off mode charge" in text or "CC mode charge" in text for at, text in window):
            return False
        return all(any(at <= full_at <= at + 1 and "bat_thread" in text and
                       "[BATTERY] Battery full !!" in text for full_at, text in window)
                   for at in averages)

    def step(self, row):
        now = row["monotonic"]
        if type(now) not in (int, float) or not math.isfinite(now) or now < self.started:
            raise ValueError("invalid measurement time")
        if now - self.started >= 32*3600:
            raise ValueError("calibration deadline reached")
        if row["boot_id"] != self.boot_id or row["online"] is not True:
            raise ValueError("boot changed or cable removed")
        for key, low, high in (("voltage_mv", 3300, 4450), ("temp_deci_c", 0, 400),
                               ("capacity", 0, 100), ("car_mah", -20000, 20000),
                               ("dod0", 0, 100), ("cached_current_deci_ma", -20000, 20000),
                               ("zcv_mv", 2500, 5000)):
            if type(row.get(key)) is not int or not low <= row[key] <= high:
                raise ValueError(f"unsafe or missing measurement: {key}")
        elapsed = None
        if self.previous:
            elapsed = now - self.previous["monotonic"]
            if not 0 < elapsed <= 90:
                raise ValueError("measurement gap")
        resets = self.read_kernel(row)
        if self.phase == "charge" and resets:
            self.full_since = None  # A full anchor needs 300 s without a new stock reset.
        if self.previous and self.phase == "discharge":
            if row.get("bcct_paused_at_sample") is not True:
                raise ValueError("BCCT pause not in force during discharge")
            if row["dod0"] != self.anchor["dod0"]:
                raise ValueError("fuel-gauge reset during discharge: DOD0 changed")
            allowance = 3 + 1800*elapsed/3600
            step_ok = abs(row["car_mah"] - self.previous["car_mah"]) <= allowance
            if resets:
                if self.awaiting:
                    raise ValueError("fuel-gauge reset during discharge: second reset before the "
                                     "fuel gauge refreshed")
                if len(resets) > 1:
                    raise ValueError("fuel-gauge reset during discharge: several new reset lines")
                self.awaiting = self.identify(resets[0], row)
            if self.awaiting:
                # The cache shows either the stale pre-reset value or a value restarted near 0.
                since = now - self.awaiting["closed_monotonic"]
                if not step_ok and abs(row["car_mah"]) > 3 + 1800*since/3600:
                    raise ValueError("fuel-gauge counter jump")
            elif not step_ok:
                raise ValueError("fuel-gauge counter jump")
        if self.phase == "charge":
            if now - self.started >= 8*3600:
                raise ValueError("full-charge deadline reached")
            if row["capacity"] <= 6:
                raise ValueError("low-battery reserve before full anchor")
            full = (row["status"] == "Full" and row["voltage_mv"] >= 4300 and
                    row["dod0"] == 0 and abs(row["cached_current_deci_ma"]) <= 1500)
            if not full:
                self.full_since = None
            elif self.full_since is None:
                self.full_since = now
            elif now - self.full_since >= 300 and self.stock_full_window():
                self.phase = "discharge"
                self.anchor = self.stored(row)
                self.segments = [self.segment(row)]
        elif self.phase == "discharge":
            self.points += 1
            # Software cross-check: cached current (deci-mA) integrated over the same samples.
            integral = (self.previous["cached_current_deci_ma"] + row["cached_current_deci_ma"]) / 20 * elapsed / 3600
            if self.awaiting:
                self.awaiting["uncounted_current_integral_mah"] += integral
                if now - self.awaiting["first_seen_monotonic"] > POST_RESET_WAIT_S:
                    raise ValueError(f"no post-reset fuel-gauge snapshot within {POST_RESET_WAIT_S} s of the reset")
                refreshed = self.post_reset(row)
                if refreshed is not None:
                    since = now - self.awaiting["closed_monotonic"]
                    if abs(row["car_mah"]) > 3 + 1800*since/3600:
                        raise ValueError("fuel-gauge counter did not restart at the kernel reset")
                    boundary = self.awaiting
                    boundary.update(post_reset_kernel_s=refreshed, opened_monotonic=now,
                                    opened_car_mah=row["car_mah"], uncounted_seconds=now - boundary["closed_monotonic"])
                    del boundary["first_seen_monotonic"]
                    self.boundaries.append(boundary)
                    self.segments.append(self.segment(row))
                    self.awaiting = None
            else:
                last = self.segments[-1]
                last["current_integral_mah"] += integral
                last.update(end_monotonic=now, car_end=row["car_mah"],
                            car_change_mah=last["car_start"] - row["car_mah"])
            if row["voltage_mv"] <= 3550 or row["capacity"] <= 6:
                if self.awaiting:
                    raise ValueError("reserve reached before the fuel gauge refreshed after a reset")
                amount = sum(s["car_change_mah"] for s in self.segments)
                if amount < 100 or self.points < 60 or now - self.anchor["monotonic"] < 1800:
                    raise ValueError("insufficient discharge for usable capacity")
                uncounted = sum(b["uncounted_current_integral_mah"] for b in self.boundaries)
                self.result = dict(usable_capacity_mah=amount, full_anchor=self.anchor, reserve=self.stored(row),
                                   segments=[dict(s) for s in self.segments],
                                   boundaries=[dict(b) for b in self.boundaries],
                                   uncounted_current_integral_mah=uncounted,
                                   endpoint="voltage_reserve" if row["voltage_mv"] <= 3550 else "driver_capacity_reserve",
                                   physical_soc_calibrated=False,
                                   basis="stock CAR between stock-full and a software reserve; not chemical 0..100%; "
                                         "excludes discharge between the closing and opening sample of each "
                                         "stock-reset boundary (see uncounted_current_integral_mah)")
                self.phase = "done"
        self.previous = self.stored(row)
        self.recent = (self.recent + [self.previous])[-40:]

    def stored(self, row):
        kept = {key: value for key, value in row.items() if key != "kernel_lines"}
        kept["kernel_after_s"] = self.kernel_newest
        return kept

    @staticmethod
    def segment(row):
        return dict(start_monotonic=row["monotonic"], end_monotonic=row["monotonic"], car_start=row["car_mah"],
                    car_end=row["car_mah"], car_change_mah=0, current_integral_mah=0.0)


def battery_lines(messages):
    return [line for line in messages.splitlines() if BATTERY_LINE.search(line)]


def dmesg():
    messages = subprocess.run(["dmesg"], capture_output=True, text=True, check=True, timeout=5).stdout
    if len(messages.encode("utf-8")) > MAX_DMESG_BYTES:
        raise ValueError("dmesg byte cap exceeded; no truncated capture accepted")
    return messages


def battery_kernel_lines():
    return battery_lines(dmesg())[-200:]


def sample():
    meter = Path("/sys/devices/platform/battery_meter")
    battery = Path("/sys/class/power_supply/battery")
    def integer(path):
        return int(path.read_text().strip())
    usb, ac = (integer(Path(f"/sys/class/power_supply/{name}/online")) for name in ("usb", "ac"))
    if usb not in (0, 1) or ac not in (0, 1):
        raise ValueError("unknown cable state")
    # Kernel log before the counters: a battery-thread line here was printed before the CAR read.
    thread = [kernel_stamp(line) for line in battery_lines(dmesg()) if "bat_thread" in line]
    thread = [stamp for stamp in thread if stamp is not None]
    # Cached getters only: avoid another active FG capture racing the stock battery thread.
    for _ in range(3):
        car = integer(meter / "FG_g_fg_dbg_bat_car")
        dod = integer(meter / "FG_g_fg_dbg_d0")
        row = dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(), monotonic=time.monotonic(),
                   boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                   voltage_mv=integer(battery / "batt_vol"), temp_deci_c=integer(battery / "batt_temp"),
                   capacity=integer(battery / "capacity"), status=(battery / "status").read_text().strip(),
                   car_mah=car, dod0=dod, zcv_mv=integer(meter / "FG_g_fg_dbg_bat_zcv"),
                   cached_current_deci_ma=integer(meter / "FG_g_fg_dbg_bat_current"), online=bool(usb or ac))
        if car == integer(meter / "FG_g_fg_dbg_bat_car") and dod == integer(meter / "FG_g_fg_dbg_d0"):
            break
    else:
        raise ValueError("fuel-gauge cache changed during capture")
    # Kernel log after the counters: a reset line absent here had not happened before the CAR read.
    lines = battery_lines(dmesg())
    resets = [line for line in lines if RESET_LINE in line]
    row.update(kernel_lines=lines, kernel_before_s=max(thread) if thread else None,
               meter_reset_marker=resets[-1] if resets else None)  # Informational only.
    return row


def run(directory, expected_boot):
    sys.path.insert(0, "/usr/lib")
    from i14ui.charging import Guard, ocv_percent
    if (os.geteuid() != 0 or os.uname().nodename != "i14" or os.uname().release != "3.18.19" or
            os.uname().machine != "aarch64" or
            Path("/sys/firmware/devicetree/base/compatible").read_bytes() != b"mediatek,MT6735\x00"):
        raise ValueError("unexpected I14 identity")
    first = sample()
    if first["boot_id"] != expected_boot:
        raise ValueError("preflight boot changed")
    sample_json(first)  # Bound the retained initial row before stopping production.
    if not directory.is_absolute() or directory.parent != Path("/mnt/data/media/0/i14-linux"):
        raise ValueError("use a new experiment directory on userdata")
    directory.mkdir(mode=0o700)  # Never reuse or overwrite an experiment.
    (directory / "python.pid").write_text(str(os.getpid()) + "\n")
    def save(name, value):
        if len(json.dumps(value, indent=2, allow_nan=False)) > MAX_RECORD_BYTES:
            raise ValueError("record byte cap exceeded")
        tmp = directory / (name + ".tmp")
        with tmp.open("w") as f:
            json.dump(value, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(directory / name)
    def service(action):
        r = subprocess.run(["rc-service", "i14-power", action], capture_output=True, text=True, timeout=30)
        data = json.dumps(dict(service=action, rc=r.returncode, stdout=r.stdout, stderr=r.stderr))
        if len(data)+1 > MAX_SERVICE_BYTES:
            raise ValueError("service log byte cap exceeded")
        print(data, flush=True)
        if r.returncode:
            raise RuntimeError(f"i14-power {action} failed")
    def stop(*_):
        raise KeyboardInterrupt("calibration cancelled")
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    cycle = Cycle(expected_boot, first["monotonic"])
    cycle.step(first)  # Validate low-battery/temperature/identity before stopping production.
    guard = None
    stopped = False
    pending = None  # A sampled row not yet written to samples.jsonl.
    try:
        service("status")
        stopped = True  # An uncertain stop must still attempt production recovery.
        service("stop")
        guard = Guard()
        guard.record = lambda text: guard_record(guard, text)
        # A changed legacy policy is not repaired by a calibration experiment.
        feedback = Path("/proc/mtk_battery_cmd/battery_cmd").read_text()
        if "get_usb_current_unlimited=0" not in feedback or "cmd_discharging = -1" not in feedback:
            raise ValueError("unexpected legacy charger policy")
        with (directory / "samples.jsonl").open("a", buffering=1) as log:
            while True:
                time.sleep(30)
                row = sample()
                pending = row
                # Read-only: the pause that was in force while this sample was taken.
                row["bcct_paused_at_sample"] = guard.control.paused()
                cycle.step(row)  # Safety and deadline checks always precede the actuator.
                # False is the longer boolean: bound the complete row before changing BCCT.
                sample_json(dict(row, phase=cycle.phase, bcct_paused=False), log.tell())
                guard.control.set_paused(cycle.phase == "discharge")
                row["phase"] = cycle.phase
                row["bcct_paused"] = guard.control.paused()
                # Retain the full ring so an offline audit can verify the original coverage.
                log.write(sample_json(row, log.tell()))
                os.fsync(log.fileno())
                pending = None
                guard.publish(dict(schema=1, backend="bcct", boot_id=expected_boot,
                                   updated_monotonic=row["monotonic"], level=ocv_percent(row["zcv_mv"]),
                                   raw_level=ocv_percent(row["zcv_mv"]), zcv_mv=row["zcv_mv"],
                                   paused=row["bcct_paused"], valid=False, enabled=False, limit=80,
                                   error=None, online=True, calibration_phase=cycle.phase,
                                   migration_attempted=True))
                save("state.json", dict(phase=cycle.phase, sample=row, started=first,
                                        deadline_monotonic=first["monotonic"]+32*3600, result=cycle.result))
                if cycle.phase == "done":
                    save("result.json", cycle.result)
                    break
    except BaseException as exc:
        # Best effort: the abort record must never mask the original abort or delay its cleanup.
        try:
            lines = battery_kernel_lines()
        except BaseException as read_error:
            lines = [f"dmesg unavailable: {read_error!r}"]
        record = dict(phase=cycle.phase, error=str(exc), error_type=type(exc).__name__, time=time.time(),
                      rejected_row=json_safe(pending), battery_kernel_lines=json_safe(lines))
        try:
            save("failure.json", record)
        except BaseException as save_error:
            try:
                save("failure.json", dict(phase=json_safe(cycle.phase), error=str(exc),
                                          record_error=repr(save_error), rejected_row=repr(pending)))
            except BaseException as last_error:
                try:
                    data = json.dumps(dict(error=str(exc), record_error=repr(last_error)))
                    print(data if len(data)+1 <= MAX_SERVICE_BYTES else '{"error":"abort record unavailable; log byte cap"}',
                          file=sys.stderr, flush=True)
                except BaseException:
                    pass  # nothing left to report to; the original abort is re-raised below
        raise
    finally:
        try:
            if guard:
                guard.close()  # Release BCCT even if cancellation happened while discharging.
        finally:
            if stopped:
                service("start")  # Original config and 80% band were never changed.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True, metavar="NEW_USERDATA_DIRECTORY")
    parser.add_argument("--expected-boot", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    with open("/run/i14-battery-calibration.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args.run, args.expected_boot)


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        data = json.dumps(dict(error_type=type(exc).__name__, error=str(exc)))
        print(data if len(data)+1 <= MAX_SERVICE_BYTES else '{"error":"exception exceeds log byte cap; see failure.json"}',
              file=sys.stderr, flush=True)
        raise SystemExit(1)
