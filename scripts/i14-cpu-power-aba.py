#!/usr/bin/env python3
"""Measure i14-cpufreq using battery-side stock fuel-gauge current.

Defaults: A/B/A/B/A, 600 s each, sampling every 5 s, first 90 s excluded.
A = service started; B = stopped. Stop with Ctrl-C or SIGTERM on this host
process; finally starts the service and checks its status. SIGKILL, host loss
or lost SSH connectivity cannot guarantee restoration: check the saved receipt.
No charge controls are read or written. The sampler runs from stdin, without
installing files on the phone. Run from the host, never directly on the phone.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import statistics
import subprocess
import sys
import time


def parse_sampler_line(line):
    """Keep raw fields alongside explicit, unit-labelled numeric values."""
    try:
        s = json.loads(line)
        if not isinstance(s, dict) or s.get("event") not in ("sample", "end"):
            raise ValueError("unknown sampler record")
        for key in ("elapsed_s",) if s["event"] == "end" else ("elapsed_s", "monotonic"):
            s[key] = float(s[key])
            if not math.isfinite(s[key]) or s[key] < 0:
                raise ValueError("invalid time")
        if s["event"] == "end":
            if type(s["complete"]) is not bool:
                raise ValueError("invalid completion marker")
            return s
        datetime.fromisoformat(s["wall_time"])
        if not s["boot_id"].strip():
            raise ValueError("missing boot ID")
        s["current_ma"] = int(s["FG_Current"]) / 10
        s["voltage_v"] = int(s["batt_vol"]) / 1000
        for key in ("capacity", "backlight", "charger_type", "batt_temp",
                    "cpufreq_limited_max_freq_by_user", "cpufreq_fix_freq_in_es", "scaling_cur_freq"):
            s[key] = int(s[key])
        physical = re.fullmatch(r"(\d+)\s+[kK][hH][zZ]", s["physical_clock"].strip())
        suspend = re.search(r"\bdvfs_disable_by_early_suspend\s*=\s*([01])\b", s["cpufreq_state"])
        if not physical or not suspend:
            raise ValueError("unrecognised clock or early-suspend readback")
        s["physical_khz"] = int(physical[1])
        s["early_suspend"] = int(suspend[1])
        s["battery_flags"] = {k: int(v) for k, v in
                              re.findall(r"\b(\w+)\s*=\s*(-?\d+)\b", s["battery_cmd"])}
        if not {"cmd_discharging", "get_usb_current_unlimited"} <= s["battery_flags"].keys():
            raise ValueError("missing battery command flags")
        if not 0 <= s["capacity"] <= 100 or not 2.5 <= s["voltage_v"] <= 5:
            raise ValueError("implausible capacity or voltage")
        if s["physical_khz"] <= 0 or s["scaling_cur_freq"] <= 0:
            raise ValueError("missing CPU clock")
        if not s["status"] or len(s["loadavg"].split()) < 3:
            raise ValueError("missing status or loadavg")
        return s
    except (KeyError, TypeError, AttributeError, OverflowError, ValueError) as exc:
        raise ValueError(f"bad sampler line: {exc}") from exc


def invalid_reasons(s, baseline=None):
    reasons = []
    if s["backlight"] != 0 or s["early_suspend"] != 1:
        reasons.append("screen is not off")
    if s["battery_flags"]["cmd_discharging"] != 1 or s["status"] != "Cmd discharging":
        reasons.append("charging is not paused")
    if s["capacity"] <= 20:
        reasons.append("capacity must be >20%")
    if not 1 <= s["charger_type"] <= 8:
        reasons.append("charger is absent or unknown")
    if baseline is not None:
        for key in ("charger_type", "battery_flags", "boot_id"):
            if s[key] != baseline[key]:
                reasons.append(f"{key} changed")
    return reasons


def read_sample(start):
    battery = "/sys/class/power_supply/battery/"
    cpu = "/proc/cpufreq/"
    paths = {"FG_Current": "/sys/devices/platform/battery_meter/FG_Current",
             "battery_cmd": "/proc/mtk_battery_cmd/battery_cmd",
             "charger_type": "/sys/devices/platform/battery/Charger_Type",
             "backlight": "/sys/class/leds/lcd-backlight/brightness",
             "cpufreq_state": cpu + "cpufreq_state",
             "cpufreq_limited_max_freq_by_user": cpu + "cpufreq_limited_max_freq_by_user",
             "cpufreq_fix_freq_in_es": cpu + "cpufreq_fix_freq_in_es",
             "physical_clock": cpu + "cpufreq_freq",
             "scaling_cur_freq": "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
             "boot_id": "/proc/sys/kernel/random/boot_id", "loadavg": "/proc/loadavg"}
    paths.update({name: battery + name for name in ("batt_vol", "capacity", "status", "batt_temp")})
    now = time.monotonic()
    s = dict(event="sample", monotonic=now, elapsed_s=now - start,
             wall_time=datetime.now(timezone.utc).isoformat())
    for key, path in paths.items():
        try:
            s[key] = Path(path).read_text().strip()
        except (OSError, UnicodeError) as exc:
            s[key] = "ERROR: " + str(exc)
    s["read_duration_s"] = time.monotonic() - now
    return s


def sampler(duration, interval, baseline):
    """One sleeping process per block; check guards even during settling."""
    start = time.monotonic()
    complete = False
    while True:
        raw = read_sample(start)
        try:
            s = parse_sampler_line(json.dumps(raw))
            reasons = invalid_reasons(s, baseline)
            if baseline is None:
                baseline = s
        except ValueError as exc:
            reasons = [str(exc)]
        raw["violations"] = reasons
        print(json.dumps(raw), flush=True)
        if reasons:
            break
        if raw["elapsed_s"] >= duration:
            complete = True
            break
        # Account for read time without busy-waiting or accumulating drift.
        target = min(duration, (math.floor(raw["elapsed_s"] / interval) + 1) * interval)
        time.sleep(max(0, start + target - time.monotonic()))
    print(json.dumps(dict(event="end", complete=complete,
                          elapsed_s=time.monotonic() - start)), flush=True)


def summarize_block(lines, label, settle, duration, baseline, error=None, interval=None):
    samples, ends, reasons = [], [], []
    for line in lines:
        try:
            s = parse_sampler_line(line)
            if s["event"] == "end":
                ends.append(s)
            else:
                if ends:
                    reasons.append("sample after end marker")
                reasons.extend(invalid_reasons(s, baseline))
                reasons.extend(s.get("violations", []))
                samples.append(s)
        except ValueError as exc:
            reasons.append(str(exc))
    if error:
        reasons.append(error)
    if len(ends) != 1 or not ends[0]["complete"] or ends[0]["elapsed_s"] < duration:
        reasons.append("block incomplete")
    if not samples or samples[-1]["elapsed_s"] < duration:
        reasons.append("missing final sample")
    if any(b["monotonic"] <= a["monotonic"] for a, b in zip(samples, samples[1:])):
        reasons.append("sample clock did not advance")
    kept = [s for s in samples if s["elapsed_s"] >= settle]
    if not kept:
        reasons.append("no samples after settling")
    # The service sets fix=0 when started; stopped, the driver default fix=1 and no cap apply.
    if label == "A" and any(s["cpufreq_fix_freq_in_es"] != 0 for s in kept):
        reasons.append("policy not active in A block")
    if label == "B" and any(s["cpufreq_fix_freq_in_es"] != 1 or s["cpufreq_limited_max_freq_by_user"] != 0
                            for s in kept):
        reasons.append("policy still active in B block")
    if interval and kept:
        if any(b["elapsed_s"] - a["elapsed_s"] > 2 * interval for a, b in zip(samples, samples[1:])):
            reasons.append("sampling gap over twice the interval")
        if len(kept) < 0.8 * (duration - settle) / interval:
            reasons.append("too few samples after settling")
    result = dict(label=label, valid=not reasons, reasons=sorted(set(reasons)),
                  n_raw=len(samples), n=len(kept), settle_seconds=settle)
    if reasons:
        return result
    currents = [s["current_ma"] for s in kept]
    result.update(mean_current_ma=statistics.mean(currents),
                  median_current_ma=statistics.median(currents),
                  mean_voltage_v=statistics.mean(s["voltage_v"] for s in kept),
                  mean_power_mw=statistics.mean(s["current_ma"] * s["voltage_v"] for s in kept),
                  negative_current_samples=sum(c < 0 for c in currents),
                  mean_temperature_c=statistics.mean(s["batt_temp"] / 10 for s in kept))
    for key, name in (("physical_khz", "physical_clock_distribution"),
                      ("scaling_cur_freq", "scaling_clock_distribution")):
        result[name] = {str(k): dict(count=n, fraction=n / len(kept))
                        for k, n in sorted(Counter(s[key] for s in kept).items())}
    return result


def contrast(blocks):
    """Positive B - mean(adjacent A) means savings with the service on."""
    pairs = []
    for i in range(1, len(blocks) - 1):
        left, b, right = blocks[i - 1:i + 2]
        if [left["label"], b["label"], right["label"]] != ["A", "B", "A"]:
            continue
        if not all(s["valid"] for s in (left, b, right)):
            continue
        p = dict(block_indices=[i - 1, i, i + 1])
        for field, name in (("mean_current_ma", "saved_current_ma"), ("mean_power_mw", "saved_power_mw")):
            p[name] = b[field] - (left[field] + right[field]) / 2
        pairs.append(p)
    result = dict(pairs=pairs, sign="positive = lower draw with i14-cpufreq started (A)")
    for name in ("saved_current_ma", "saved_power_mw"):
        values = [p[name] for p in pairs]
        result[name] = (dict(mean=statistics.mean(values), min=min(values), max=max(values),
                             range=max(values) - min(values),
                             stdev=statistics.stdev(values) if len(values) > 1 else None)
                        if values else None)
    return result


def ssh_run(command, *, source=None, output=None, timeout=30):
    ssh_cmd = shlex.split(os.environ.get("I14_SSH", "ssh root@172.16.42.1"))
    return subprocess.run([*ssh_cmd, command], input=source, text=True,
                          stdout=output if output is not None else subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout)


def checked(result):
    if result.returncode:
        raise RuntimeError(f"SSH command failed ({result.returncode}): {result.stderr or result.stdout}")
    return result


def restore_service(runner):
    receipt = dict(verified_started=False)
    # A second Ctrl-C must not interrupt cleanup. This cannot handle SIGKILL.
    previous = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        try:
            checked(runner("rc-service i14-cpufreq start"))
        except Exception as exc:
            receipt["start_error"] = str(exc)
        try:
            status = runner("rc-service i14-cpufreq status")
            receipt.update(status_rc=status.returncode, status_stdout=status.stdout, status_stderr=status.stderr)
            receipt["verified_started"] = status.returncode == 0 and bool(
                re.search(r"\bstatus:\s*started\b", status.stdout or ""))
        except Exception as exc:
            receipt["status_error"] = str(exc)
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
    return receipt


def run_experiment(args, runner=ssh_run):
    args.output_dir.mkdir(parents=True, exist_ok=False)
    summary = dict(sequence=args.sequence, block_seconds=args.block_seconds,
                   sample_interval=args.sample_interval, settle_seconds=args.settle_seconds,
                   measurement="Battery-side current from the stock fuel gauge, NOT an external USB meter. "
                   "FG_Current / 10 = mA, positive out of battery; batt_vol / 1000 = V.",
                   limitations=["Guards checked at sample times; brief intervening transitions may be missed.",
                                "Neighbouring A means remove approximately linear slow drift only.",
                                "Pairs share A blocks; spread is descriptive, not independent replicates or a confidence interval.",
                                "Sampled clock distributions are counts, not time-in-state; sampler/SSH adds some load.",
                                "Negative settled current warrants investigation despite pause flags; inspect raw data.",
                                "No external calibration, controlled workload or constant temperature is guaranteed."],
                   blocks=[])
    source = Path(__file__).read_text()

    def capture(path, duration, baseline):
        command = "python3 -B - --sampler " + shlex.join(
            [str(duration), str(args.sample_interval), json.dumps(baseline)])
        with path.open("x") as output:
            checked(runner(command, source=source, output=output, timeout=duration + 60))

    def precheck(path, baseline):
        capture(path, 0, baseline)
        lines = path.read_text().splitlines()
        check = summarize_block(lines, "precondition", 0, 0, baseline)
        if not check["valid"]:
            raise RuntimeError("preconditions failed: " + "; ".join(check["reasons"]))
        return parse_sampler_line(lines[0])

    try:
        baseline = precheck(args.output_dir / "preflight.jsonl", None)
        summary["baseline"] = baseline
        for i, label in enumerate(args.sequence):
            prefix = f"{i + 1:02d}-{label}"
            path = args.output_dir / (prefix + ".jsonl")
            error = None
            try:
                precheck(args.output_dir / (prefix + "-before.jsonl"), baseline)
                action = "start" if label == "A" else "stop"
                checked(runner("rc-service i14-cpufreq " + action))
                print(f"{prefix}: {action}, sampling {args.block_seconds:g} s", flush=True)
                capture(path, args.block_seconds, baseline)
            except (Exception, KeyboardInterrupt) as exc:
                error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                lines = path.read_text().splitlines() if path.exists() else []
                block = summarize_block(lines, label, args.settle_seconds, args.block_seconds, baseline, error,
                                        args.sample_interval)
                block["raw_file"] = path.name if path.exists() else None
                summary["blocks"].append(block)
            if not block["valid"]:
                raise RuntimeError(prefix + " invalid: " + "; ".join(block["reasons"]))
    except (Exception, KeyboardInterrupt) as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        summary["restore"] = restore_service(runner)
        summary["contrast"] = contrast(summary["blocks"])
        (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--block-seconds", type=float, default=600)
    parser.add_argument("--sequence", default="A,B,A,B,A")
    parser.add_argument("--sample-interval", type=float, default=5)
    parser.add_argument("--settle-seconds", type=float, default=90)
    parser.add_argument("--output-dir", type=Path,
                        default=Path("artifacts") / datetime.now(timezone.utc).strftime("cpu-power-aba-%Y%m%dT%H%M%SZ"))
    args = parser.parse_args()
    args.sequence = args.sequence.upper().replace(",", "").replace(" ", "")
    if not re.fullmatch(r"A(?:BA)+", args.sequence):
        parser.error("sequence must be A,B,A (optionally repeated B,A)")
    values = (args.block_seconds, args.sample_interval, args.settle_seconds)
    if (not all(math.isfinite(v) for v in values) or args.sample_interval <= 0 or args.settle_seconds < 0
            or args.block_seconds < args.settle_seconds + args.sample_interval):
        parser.error("require finite times, interval >0, settle >=0, block >= settle + interval")

    def interrupted(signum, frame):
        raise InterruptedError(f"signal {signum}")

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, interrupted)
    result = run_experiment(args)
    print(result["measurement"])
    for i, block in enumerate(result["blocks"], 1):
        text = (f"n={block['n']}, mean {block['mean_current_ma']:.2f} mA, "
                f"median {block['median_current_ma']:.2f} mA, {block['mean_power_mw']:.2f} mW"
                if block["valid"] else "INVALID: " + "; ".join(block["reasons"]))
        print(f"{i:02d}-{block['label']}: {text}")
        if block.get("negative_current_samples"):
            print("  WARNING: negative settled current; inspect raw gauge readings before interpreting savings.")
    c = result["contrast"]
    if c["pairs"]:
        current, power = c["saved_current_ma"], c["saved_power_mw"]
        print(f"B - neighbouring A: {current['mean']:.2f} mA / {power['mean']:.2f} mW; "
              f"{len(c['pairs'])} pairs, current range {current['min']:.2f}..{current['max']:.2f} mA. "
              "Positive means savings with service on; see JSON for spread and limits.")
    else:
        print("No valid A/B/A contrast.")
    print("Restore:", "verified started" if result["restore"]["verified_started"] else "FAILED / UNVERIFIED")
    if result.get("error"):
        print(result["error"], file=sys.stderr)
    print("Output:", args.output_dir)
    return int(bool(result.get("error")) or not result["restore"]["verified_started"] or not c["pairs"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--sampler":
        sampler(float(sys.argv[2]), float(sys.argv[3]), json.loads(sys.argv[4]))
    else:
        sys.exit(main())
