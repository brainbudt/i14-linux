#!/usr/bin/env python3
"""Report minute samples, in acquisition order (.1 before current after rotation).

Usage: python3 -B scripts/i14-soak-report.py samples.jsonl.1 samples.jsonl [--json]
Durations use left-sample state and uptime, only within one boot and <=180 s.
They are estimates, not service SLA or physical charge/auto-boot acceptance.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import re
import math
from pathlib import Path
import statistics
import sys


SERVICES = ("i14-power", "i14-cpufreq", "i14-ui", "unbound", "i14-wifi", "i14-netwatch", "sshd")
NOTES = [
    "Durations are left-sample estimates for same-boot intervals <=180 s; gaps and unknown states are excluded.",
    "Service down-minutes mean nonzero rc-service status, not independently measured process downtime.",
    "Charging means BCCT pause released (or legacy cmd_discharging 0/-1) with cable online: permission, not proof of net charge.",
    "FG_Current / 10 is signed mA: positive leaves the battery; negative enters it. Temperature is batt_temp / 10 C.",
    "Current and CPU statistics describe samples, not continuous measurements; backlight 0 is the screen-off proxy.",
    "Threshold checks assume the guard was enabled at limit 80/resume 70; a flag transition does not establish its cause.",
    "The guard level comes from its last log entry (up to about five minutes old); zcv-based percentages are uncalibrated estimates.",
    "MemAvailable first/last can span reboots and cache changes; the delta alone does not prove a leak.",
    "Reboot downtime upper bounds run from the last old-boot sample to epoch minus new-boot uptime, assuming a stable wall clock.",
    "Power-off charging, shutdown reason and automatic boot are not proven by missing samples or a changed boot ID.",
]


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def get_number(row, key):
    value = row.get(key)
    return value if numeric(value) else None



def bcct_mode(row):
    return "charge_status" in row or "backend=bcct" in (row.get("battery_log_last") or "")


def guard_of(row):
    try:
        status = json.loads(row.get("charge_status") or "")
        if not isinstance(status, dict):
            return None
        # BCCT is bypassed by unlimited USB and overridden by a legacy forced stop.
        if get_number(row,"get_usb_current_unlimited") != 0 or get_number(row,"cmd_discharging") not in (0,-1):
            return None
        stamp, uptime = status.get("updated_monotonic"), row.get("uptime_seconds")
        if (type(status.get("schema")) is not int or status["schema"] != 1 or status.get("backend") != "bcct" or
                not isinstance(row.get("boot_id"), str) or not row["boot_id"] or
                status.get("boot_id") != row["boot_id"] or not numeric(stamp) or not numeric(uptime) or
                not 0 <= uptime - stamp <= 150 or status.get("error") is not None or
                any(type(status.get(key)) is not bool for key in ("paused", "valid", "enabled")) or
                type(status.get("limit")) is not int or status["limit"] not in (60,70,80,90,100)):
            return None
        for key,low,high in (("level",0,100),("raw_level",0,100),("zcv_mv",2500,5000)):
            value=status.get(key)
            if value is not None and (type(value) is not int or not low <= value <= high):
                return None
        readback=row.get("bcct_state") or ""
        state=re.search(r"mtk-cl-bcct00 (-?[0-9]+) mA, state ([01])\b",readback)
        if state is None or int(state[2]) != int(status["paused"]):
            return None
        if status["paused"] and (int(state[1]) != 1 or not re.search(r"curr_limit 1\b",readback)):
            return None
        return status
    except (ValueError,TypeError):
        return None


def level_of(row):
    """The level the guard itself used, from its last log line: gauge=None (capacity) or gauge=<n>.
    The guard logs on every state change, so at a transition the value is current; otherwise up to ~5 min old."""
    if bcct_mode(row):
        status=guard_of(row)
        return status.get("raw_level",status.get("level")) if status else None
    source = re.search(r"\bgauge=(\S+)", row.get("battery_log_last") or "")
    if source and source[1] != "None":
        return int(source[1]) if source[1].isdigit() else None  # the guard's own level; bad -> unobserved
    return get_number(row, "capacity")  # gauge not trusted, or a guard from before the gauge fix

def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def read_samples(paths):
    rows, ignored = [], 0
    for path in paths:
        with Path(path).open(encoding="utf-8", errors="replace") as source:
            for line in source:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not numeric(row.get("epoch")) or not 0 <= row["epoch"] <= 253402300799:
                        raise ValueError("invalid epoch")
                    rows.append(row)
                except (ValueError, TypeError):
                    ignored += 1
    return rows, ignored


def charge_state(row):
    if bcct_mode(row):
        status=guard_of(row)
        if status is None:
            return "unknown"
        if row.get("usb_online") == 1 or row.get("ac_online") == 1:
            return "paused" if status["paused"] else "charging"
        if row.get("usb_online") == 0 and row.get("ac_online") == 0:
            return "unplugged"
        return "unknown"
    if row.get("usb_online") == 1 or row.get("ac_online") == 1:
        if row.get("cmd_discharging") == 1:
            return "paused"
        if row.get("cmd_discharging") in (0, -1):
            return "charging"
    elif row.get("usb_online") == 0 and row.get("ac_online") == 0:
        return "unplugged"
    return "unknown"


def stats(values):
    return {"samples": len(values), "min": min(values), "max": max(values),
            "mean": statistics.fmean(values), "median": statistics.median(values)} if values else {
                "samples": 0, "min": None, "max": None, "mean": None, "median": None}


def interval(previous, row):
    same_boot = bool(previous.get("boot_id")) and previous.get("boot_id") == row.get("boot_id")
    before, after = get_number(previous, "uptime_seconds"), get_number(row, "uptime_seconds")
    elapsed = after - before if same_boot and before is not None and after is not None else None
    return same_boot, elapsed, elapsed is not None and 0 < elapsed <= 180


def analyze(rows):
    result = {"samples": len(rows), "period": None, "gaps": [], "reboots": [], "clock_jumps": [],
              "observed_seconds": 0, "services_down_minutes": dict.fromkeys(SERVICES, 0),
              "services_observed_minutes": dict.fromkeys(SERVICES, 0),
              "dns": {"failures": 0, "checked": 0, "failure_times": []},
              "charge": {"capacity_min": None, "capacity_max": None, "transitions": [], "threshold_checks": [],
                         "observed_seconds": dict.fromkeys(("paused", "charging", "unplugged", "unknown"), 0)},
              "current_ma": {}, "cpu_screen_off": {}, "temperature_c": {"min": None, "max": None},
              "memory_kb": {"first": None, "last": None, "min": None, "delta": None}, "notes": NOTES}
    if not rows:
        return result
    result["period"] = {"start": iso(rows[0]["epoch"]), "end": iso(rows[-1]["epoch"]),
                        "wall_seconds": rows[-1]["epoch"] - rows[0]["epoch"]}
    capacities, temps, memory, cpu = [], [], [], Counter()
    currents = {key: [] for key in ("screen_off", "screen_on", "paused", "charging")}
    for index, row in enumerate(rows):
        stamp = iso(row["epoch"])
        capacity = get_number(row, "capacity")
        if capacity is not None:
            capacities.append(capacity)
        # Follow the guard's logged level; capacity is held at 100 with a charger.
        level = level_of(row)
        temp = get_number(row, "batt_temp_deci_c")
        if temp is not None:
            temps.append(temp / 10)
        mem = get_number(row, "mem_available_kb")
        if mem is not None:
            memory.append(mem)
        current = get_number(row, "fg_current_deci_ma")
        light = get_number(row, "backlight_brightness")
        state = charge_state(row)
        if current is not None:
            if light is not None and light >= 0:
                currents["screen_off" if light == 0 else "screen_on"].append(current / 10)
            if state in ("paused", "charging"):
                currents[state].append(current / 10)
        freq = get_number(row, "scaling_cur_freq_khz")
        if light == 0 and freq is not None:
            cpu[str(int(freq))] += 1
        dns = row.get("dns")
        if isinstance(dns, dict) and isinstance(dns.get("ok"), bool):
            result["dns"]["checked"] += 1
            if not dns["ok"]:
                result["dns"]["failures"] += 1
                result["dns"]["failure_times"].append(stamp)
        if not index:
            continue
        previous = rows[index - 1]
        wall = row["epoch"] - previous["epoch"]
        same_boot, elapsed, covered = interval(previous, row)
        reboot = bool(previous.get("boot_id") and row.get("boot_id") and not same_boot)
        clock_jump = same_boot and elapsed is not None and abs(wall - elapsed) > 5
        if clock_jump:
            result["clock_jumps"].append({"at": stamp, "wall_seconds": wall, "uptime_seconds": elapsed})
        # Uptime distinguishes missing samples from an NTP wall-clock step.
        gap_seconds = elapsed if same_boot and elapsed is not None else wall
        if gap_seconds > 180:
            result["gaps"].append({"from": iso(previous["epoch"]), "to": stamp,
                                   "seconds": gap_seconds,
                                   "kind": "reboot" if reboot else "missed_samples" if same_boot else "unknown"})
        if reboot:
            up = get_number(row, "uptime_seconds")
            upper = wall - up if up is not None and up >= 0 and wall >= up else None
            result["reboots"].append({"last_sample": iso(previous["epoch"]), "first_sample": stamp,
                                      "from_boot": previous["boot_id"], "to_boot": row["boot_id"],
                                      "sample_gap_seconds": wall, "new_boot_uptime_seconds": up,
                                      "last_capacity": get_number(previous, "capacity"), "first_capacity": capacity,
                                      "last_status": previous.get("status"), "first_status": row.get("status"),
                                      "last_battery_log": previous.get("battery_log_last"),
                                      "downtime_upper_bound_seconds": upper})
        if covered:
            result["observed_seconds"] += elapsed
            result["charge"]["observed_seconds"][charge_state(previous)] += elapsed
            statuses = previous.get("services")
            if isinstance(statuses, dict):
                for service in SERVICES:
                    rc = statuses.get(service)
                    if numeric(rc):
                        result["services_observed_minutes"][service] += elapsed / 60
                        if rc != 0:
                            result["services_down_minutes"][service] += elapsed / 60
        old, new = previous.get("cmd_discharging"), row.get("cmd_discharging")
        if not bcct_mode(row) and old is not None and new is not None and old != new:
            result["charge"]["transitions"].append({"at": stamp, "from": old, "to": new,
                                                     "level": level, "continuous": covered})
        # -1 is the stock driver's acknowledgement of 0, not a pause/resume.
        old_state = charge_state(previous)
        if old_state in ("paused", "charging") and state in ("paused", "charging") and old_state != state:
            new_guard, old_guard = guard_of(row), guard_of(previous)
            limit = new_guard["limit"] if new_guard else 80
            threshold = limit if state == "paused" else limit - 10
            before = level_of(previous)
            if bcct_mode(row):
                result["charge"]["transitions"].append({"at":stamp,"from":old_state,"to":state,
                    "level":level,"continuous":covered,"backend":"bcct"})
            verdict = "unobserved"
            if covered and not clock_jump and level is not None and before is not None:
                if bcct_mode(row) or bcct_mode(previous):
                    active = (new_guard and old_guard and new_guard["valid"] and old_guard["valid"] and
                              new_guard["enabled"] and old_guard["enabled"] and new_guard["limit"] == old_guard["limit"])
                    key="stop_mv" if state == "paused" else "resume_mv"
                    boundary=new_guard.get(key) if new_guard else None
                    current_zcv=new_guard.get("zcv_mv") if new_guard else None
                    previous_zcv=old_guard.get("zcv_mv") if old_guard else None
                    if active and all(numeric(v) for v in (boundary,current_zcv,previous_zcv)):
                        reached=current_zcv >= boundary if state == "paused" else current_zcv <= boundary
                        crossed=previous_zcv <= boundary <= current_zcv if state == "paused" else current_zcv <= boundary <= previous_zcv
                        verdict="observed" if reached and crossed else "outside_band" if not reached else "unobserved"
                else:
                    reached = level >= 80 if state == "paused" else level <= 70
                    crossed = before <= 80 <= level if state == "paused" else level <= 70 <= before
                    verdict = "observed" if reached and crossed else "outside_band" if not reached else "unobserved"
            result["charge"]["threshold_checks"].append({"at": stamp, "action": state, "level": level,
                                                          "previous_level": before, "threshold": threshold,
                                                          "result": verdict})
    if capacities:
        result["charge"].update(capacity_min=min(capacities), capacity_max=max(capacities))
    result["current_ma"] = {key: stats(values) for key, values in currents.items()}
    result["cpu_screen_off"] = {key: {"samples": count, "percent": 100 * count / cpu.total()}
                                 for key, count in sorted(cpu.items(), key=lambda item: int(item[0]))}
    if temps:
        result["temperature_c"] = {"min": min(temps), "max": max(temps)}
    if memory:
        result["memory_kb"] = {"first": memory[0], "last": memory[-1], "min": min(memory), "delta": memory[-1] - memory[0]}
    return result


def human(result):
    period = result["period"]
    print(f"Samples: {result['samples']}; ignored malformed/truncated lines: {result['ignored_lines']}")
    if period:
        print(f"Period: {period['start']} to {period['end']} ({period['wall_seconds'] / 3600:.2f} wall hours)")
    print(f"Observed intervals: {result['observed_seconds'] / 3600:.2f} hours")
    print(f"Gaps >3 min: {len(result['gaps'])}; reboots: {len(result['reboots'])}; clock steps: {len(result['clock_jumps'])}")
    for gap in result["gaps"]:
        print(f"  {gap['kind']}: {gap['from']} -> {gap['to']}, {gap['seconds']} s")
    for boot in result["reboots"]:
        print(f"  Reboot {boot['from_boot']} -> {boot['to_boot']} at {boot['first_sample']}: "
              f"sample gap {boot['sample_gap_seconds']} s; downtime upper bound {boot['downtime_upper_bound_seconds']} s")
        print(f"    Capacity {boot['last_capacity']} -> {boot['first_capacity']}%; "
              f"status {boot['last_status']} -> {boot['first_status']}; last battery log: {boot['last_battery_log']}")
    print("Services: down minutes / observed minutes")
    for service in SERVICES:
        print(f"  {service}: {result['services_down_minutes'][service]:.1f} / {result['services_observed_minutes'][service]:.1f}")
    print(f"DNS failures: {result['dns']['failures']} / {result['dns']['checked']}")
    for stamp in result["dns"]["failure_times"]:
        print(f"  DNS failure: {stamp}")
    charge = result["charge"]
    print(f"Capacity: {charge['capacity_min']}..{charge['capacity_max']} %")
    for key, seconds in charge["observed_seconds"].items():
        print(f"  {key}: {seconds / 3600:.3f} observed hours")
    for transition in charge["transitions"]:
        print(f"  {transition['at']}: {transition.get('backend', 'cmd_discharging')} {transition['from']} -> {transition['to']}, "
              f"level {transition['level']}%, continuous={transition['continuous']}")
    print(f"80/70 threshold checks: {len(charge['threshold_checks'])} (none means no observed cycle evidence)")
    for check in charge["threshold_checks"]:
        print(f"  {check['at']}: {check['action']} at {check['level']}%, threshold {check['threshold']}%: {check['result']}")
    print("Signed current (mA): samples / min / max / mean / median")
    for key, values in result["current_ma"].items():
        print(f"  {key}: " + " / ".join(str(values[k]) for k in ("samples", "min", "max", "mean", "median")))
    print("CPU screen-off distribution (kHz):")
    for freq, values in result["cpu_screen_off"].items():
        print(f"  {freq}: {values['samples']} samples ({values['percent']:.1f}%)")
    print(f"Temperature (C): {result['temperature_c']['min']}..{result['temperature_c']['max']}")
    print("MemAvailable (kB): " + ", ".join(f"{key}={value}" for key, value in result["memory_kb"].items()))
    for note in result["notes"]:
        print(f"Note: {note}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("samples", nargs="+", help="JSONL files in acquisition order, rotated .1 first")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of prose")
    args = parser.parse_args()
    try:
        rows, ignored = read_samples(args.samples)
    except OSError as error:
        parser.error(str(error))
    result = analyze(rows)
    result["ignored_lines"] = ignored
    if args.json:
        json.dump(result, sys.stdout, indent=2, allow_nan=False)
        print()
    else:
        human(result)


if __name__ == "__main__":
    main()
