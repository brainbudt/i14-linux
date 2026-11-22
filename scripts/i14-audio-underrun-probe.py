#!/usr/bin/env python3
"""Host-driven, opt-in A/B ALSA probe. Emits JSONL (two runs and a summary).

Requires phone-side python3 and aplay; never installs packages. Ctrl-C requests
cleanup and a verified policy restart. SIGKILL, power loss or loss of SSH can
prevent host verification: inspect the reported restore error before rerunning.
No mixer routes, charging controls, or battery interfaces are accessed.
"""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave


CPU = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq")
PROC = Path("/proc/cpufreq")
BACKLIGHT = Path("/sys/class/leds/lcd-backlight/brightness")
SERVICE = "rc-service i14-cpufreq"


def select_pcm(text, override=None):
    devices = []
    for line in text.splitlines():
        match = re.match(r"^(\d+)-(\d+):\s*(.*?)\s*:\s*.*\bplayback\s+[1-9]", line, re.I)
        if match:
            card, device, name = match.groups()
            devices.append((int(card), int(device), name.lower()))
    if override:
        match = re.fullmatch(r"(?:plug)?hw:(\d+),(\d+)", override)
        if not match or not any((c, d) == tuple(map(int, match.groups()))
                                for c, d, _ in devices):
            raise ValueError("--device must be a listed playback hw:C,D or plughw:C,D")
        return override
    if not devices:
        raise ValueError("no playback PCM in /proc/asound/pcm")

    def rank(item):
        card, device, name = item
        primary = bool(re.search(r"multimedia(?:1)?_?playback", name))
        dl1 = bool(re.search(r"\bdl1\b", name))
        return (0 if primary else 1 if dl1 else 2, card, device)

    card, device, _ = min(devices, key=rank)
    return f"hw:{card},{device}"


def count_underruns(text):
    lines = text.splitlines()
    return {"underrun_lines": sum("underrun" in s.lower() for s in lines),
            "xrun_lines": sum("xrun" in s.lower() for s in lines),
            "total_lines": sum(bool(re.search("underrun|xrun", s, re.I)) for s in lines)}


def verdict(runs):
    if (len(runs) != 2 or [r["condition"] for r in runs] != ["A", "B"]
            or not all(r["valid"] for r in runs)):
        return "invalid"
    def events(run):
        return (run["underruns"]["total_lines"]
                + run.get("kernel_underrun_lines", {}).get("total_lines", 0))

    if events(runs[1]):
        return "baseline_has_underruns"
    if events(runs[0]):
        return "possible_policy_regression"
    return "no_underruns_observed"


def kernel_delta(before, after):
    old, new = before.splitlines(), after.splitlines()
    for count in range(min(len(old), len(new)), 0, -1):
        if old[-count:] == new[:count]:
            return new[count:], True
    return new, not old


def local_run(command):
    return subprocess.run(command, shell=True, text=True, capture_output=True,
                          check=True, timeout=20)


@contextmanager
def restore_policy(runner):
    """Used on both sides of SSH; failed restoration is an explicit failure."""
    receipt = {"verified": False}
    try:
        yield receipt
    finally:
        # A second termination signal must not interrupt this bounded cleanup.
        handlers = {s: signal.signal(s, signal.SIG_IGN)
                    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        try:
            runner(f"{SERVICE} start")
            runner(f'for i in 1 2 3 4 5; do {SERVICE} status && '
                   'test "$(cat /proc/cpufreq/cpufreq_fix_freq_in_es)" = 0 '
                   '&& exit 0; sleep 1; done; exit 1')
            receipt["verified"] = True
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)


def snapshot():
    state = (PROC / "cpufreq_state").read_text()
    match = re.search(r"^dvfs_disable_by_early_suspend\s*=\s*([01])\s*$", state, re.M)
    return {"monotonic": time.monotonic(), "frequency_khz": int(CPU.read_text()),
            "cap_khz": int((PROC / "cpufreq_limited_max_freq_by_user").read_text()),
            "fix_freq_in_es": int((PROC / "cpufreq_fix_freq_in_es").read_text()),
            "brightness": int(BACKLIGHT.read_text()),
            "early_suspend": int(match[1]) if match else None}


def check_screen(sample):
    if sample["brightness"] != 0 or sample["early_suspend"] != 1:
        raise RuntimeError("invalid: requires brightness=0 and early_suspend=1")


def make_wav(path, duration):
    second = b"".join(struct.pack("<hh", value, value) for value in
                      (int(4000 * math.sin(2 * math.pi * 440 * i / 48000))
                       for i in range(48000)))
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        for _ in range(duration):
            wav.writeframesraw(second)


# Frequency-scaled CPU-time bursts: approximately 15% of one core at 1040 MHz,
# 52% at 299 MHz. Synthetic work only; not a Vorbis decoder or measured calibration.
DECODE_LOAD = """
import time
from pathlib import Path
cpu = Path('/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq')
value = 1
while True:
    start = time.monotonic()
    budget = min(0.09, 0.015 * 1040000 / int(cpu.read_text()))
    deadline = time.process_time() + budget
    while time.process_time() < deadline:
        for _ in range(100):
            value = (value * 1664525 + 1013904223) & 0xffffffff
    time.sleep(max(0, start + 0.1 - time.monotonic()))
"""


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def play(wav, device, duration, decode_load):
    card, number = map(int, device.split(":")[1].split(","))
    status = Path(f"/proc/asound/card{card}/pcm{number}p/sub0/status")
    result = {"device": device, "expected_seconds": duration, "samples": []}
    player = load = None
    with tempfile.TemporaryFile(mode="w+") as stderr, tempfile.TemporaryFile(mode="w+") as stdout:
        start = time.monotonic()
        try:
            if decode_load:
                load = subprocess.Popen(["python3", "-c", DECODE_LOAD], stderr=stderr)
            player = subprocess.Popen(["aplay", "-D", device, "-v", str(wav)],
                                      stdout=stdout, stderr=stderr)
            next_sample = start
            while player.poll() is None:
                now = time.monotonic()
                if now > start + duration + max(15, duration * 0.25):
                    raise RuntimeError("aplay exceeded playback deadline")
                if now >= next_sample:
                    sample = snapshot()
                    sample["elapsed_seconds"] = now - start
                    sample["pcm_status"] = status.read_text()
                    result["samples"].append(sample)
                    check_screen(sample)
                    if load is not None and load.poll() is not None:
                        raise RuntimeError("synthetic decoder load exited")
                    next_sample += 1
                time.sleep(0.05)
            result["exit_status"] = player.returncode
        except Exception as error:
            result["error"] = str(error)
        finally:
            # Playback timing excludes process cleanup and service restoration.
            result["wall_seconds"] = time.monotonic() - start
            stop_process(player)
            stop_process(load)
            stderr.seek(0)
            stdout.seek(0)
            result["stderr"] = stderr.read()
            result["stdout"] = stdout.read()
            result.setdefault("exit_status", player.returncode if player else None)
    result["underruns"] = count_underruns(result["stderr"] + "\n" + result["stdout"])
    result["stderr_underruns"] = count_underruns(result["stderr"])
    return result


def run_condition(config):
    result = {"type": "run", "condition": config["condition"], "valid": False,
              "decode_load": config["decode_load"], "attempts": [], "invalid_reasons": []}
    if config["decode_load"]:
        result["decode_load_model"] = {"reference_khz": 1040000, "core_fraction": 0.15,
                                       "burst_period_seconds": 0.1, "calibrated": False}
    restoration = {"verified": False}
    try:
        with restore_policy(local_run) as restoration:
            check_screen(snapshot())
            with tempfile.TemporaryDirectory(prefix="i14-audio-", dir="/tmp") as tmp:
                wav = Path(tmp) / "probe.wav"
                make_wav(wav, config["duration"])
                local_run(f'{SERVICE} {"start" if config["condition"] == "A" else "stop"}')
                # Wait for a real low-clock starting point in A; avoid WAV-generation load.
                deadline = time.monotonic() + 40
                while True:
                    sample = snapshot()
                    check_screen(sample)
                    ready = (sample["fix_freq_in_es"] == 0 and sample["cap_khz"] == 299000
                             and sample["frequency_khz"] == 299000) if config["condition"] == "A" else (
                                 sample["fix_freq_in_es"] == 1 and sample["cap_khz"] == 0)
                    if ready:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("condition did not settle to expected policy controls")
                    time.sleep(1)
                result["before"] = sample
                before = local_run("dmesg").stdout
                attempt = play(wav, config["device"], config["duration"], config["decode_load"])
                result["attempts"].append(attempt)
                # Retry only a format/setup refusal, never busy/open errors or an xrun.
                format_refusal = re.search(
                    r"(?:sample format|channels count|rate).*not available|"
                    r"(?:sample format|channels count).*non available|unable to install hw params",
                    attempt["stderr"], re.I)
                if (config.get("allow_fallback", False) and config["device"].startswith("hw:")
                        and attempt["exit_status"] != 0 and format_refusal
                        and not attempt.get("error") and not attempt["underruns"]["total_lines"]):
                    attempt = play(wav, "plug" + config["device"], config["duration"],
                                   config["decode_load"])
                    result["attempts"].append(attempt)
                result.update(attempt)
                result["after"] = snapshot()
                check_screen(result["after"])
                delta, overlap = kernel_delta(before, local_run("dmesg").stdout)
                result["kernel_log_overlap"] = overlap
                result["kernel_log_delta"] = [s for s in delta if re.search("afe|audio|underrun|xrun", s, re.I)]
                result["kernel_underrun_lines"] = count_underruns("\n".join(result["kernel_log_delta"]))
                reasons = result["invalid_reasons"]
                if attempt.get("error"):
                    reasons.append(attempt["error"])
                if attempt["exit_status"] != 0:
                    reasons.append("aplay exited unsuccessfully")
                if abs(attempt["wall_seconds"] - config["duration"]) > max(2, config["duration"] * 0.05):
                    reasons.append("wall duration differs from WAV duration")
                samples = attempt["samples"]
                pointers = {m[1] for s in samples if "RUNNING" in s["pcm_status"]
                            for m in [re.search(r"hw_ptr\s*:\s*(\d+)", s["pcm_status"])] if m}
                if len(pointers) < 2:
                    reasons.append("no advancing RUNNING PCM hw_ptr observed")
                if any(s["frequency_khz"] <= 0 for s in samples):
                    reasons.append("invalid CPU frequency sample")
                if not overlap:
                    reasons.append("kernel ring buffer lost the measurement boundary")
                expected_fix = 0 if config["condition"] == "A" else 1
                allowed_caps = (0, 299000) if config["condition"] == "A" else (0,)
                if any(s["fix_freq_in_es"] != expected_fix or
                       s["cap_khz"] not in allowed_caps for s in samples + [result["after"]]):
                    reasons.append("CPU policy controls changed unexpectedly")
                result["valid"] = not reasons
    except Exception as error:
        result["valid"] = False
        result["invalid_reasons"].append(str(error))
    result["restore_verified"] = restoration["verified"]
    return result


def interrupted(signum, _frame):
    raise InterruptedError(f"interrupted by signal {signum}")


def remote_ssh(ssh_cmd, command, timeout):
    """Keep stdin open as a cancellation channel; drain output without pipe deadlocks."""
    prefix = shlex.split(str(ssh_cmd)) if isinstance(ssh_cmd, (str, Path)) else list(ssh_cmd)
    with tempfile.TemporaryFile(mode="w+") as out, tempfile.TemporaryFile(mode="w+") as err:
        with subprocess.Popen([*prefix, command], text=True, stdin=subprocess.PIPE,
                              stdout=out, stderr=err) as process:
            try:
                process.wait(timeout=timeout)
            except BaseException:
                try:
                    process.stdin.write("stop\n")
                    process.stdin.flush()
                    process.wait(timeout=50)
                except (BrokenPipeError, subprocess.TimeoutExpired):
                    stop_process(process)
                raise
            finally:
                process.stdin.close()
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(), err.read()
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            return stdout


def main():
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    if len(sys.argv) == 3 and sys.argv[1] == "--remote":
        # SSH disconnect/host cancellation triggers the same guarded cleanup.
        def cancel_on_input():
            os.read(sys.stdin.fileno(), 1)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=cancel_on_input, daemon=True).start()
        print(json.dumps(run_condition(json.loads(sys.argv[2]))), flush=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="listed hw:C,D or plughw:C,D (default: primary playback)")
    parser.add_argument("--duration", type=int, default=120, help="seconds per condition (default 120)")
    parser.add_argument("--decode-load", action="store_true", help="synthetic ~15%% core at 1040 MHz")
    args = parser.parse_args()
    if not 5 <= args.duration <= 600:
        parser.error("--duration must be between 5 and 600 seconds")
    ssh_cmd = shlex.split(os.environ.get("I14_SSH", "ssh root@172.16.42.1"))

    def ssh(command):
        return subprocess.run([*ssh_cmd, command], text=True,
                              capture_output=True, check=True, timeout=args.duration + 120)

    runs = []
    summary = {"type": "summary", "verdict": "invalid", "restore_verified": False,
               "limitations": "Synthetic DMA/CPU-load probe; no audible-output, real OGG, power or long-term acceptance."}
    restoration = {"verified": False}
    try:
        if ssh("command -v aplay >/dev/null && echo present || echo missing").stdout.strip() != "present":
            print("Missing aplay on phone. Install: apk add alsa-utils\n"
                  "After testing remove: apk del alsa-utils", file=sys.stderr)
            return 2
        ssh("command -v python3 >/dev/null")
        pcm = ssh("cat /proc/asound/pcm").stdout
        device = select_pcm(pcm, args.device)
        source = Path(__file__).read_text()
        with restore_policy(ssh) as restoration:
            for condition in ("A", "B"):
                config = {"condition": condition, "device": device, "duration": args.duration,
                          "decode_load": args.decode_load, "allow_fallback": condition == "A"}
                command = "python3 -u -c " + shlex.quote(source) + " --remote " + shlex.quote(json.dumps(config))
                run = json.loads(remote_ssh(ssh_cmd, command, args.duration + 120))
                runs.append(run)
                print(json.dumps(run), flush=True)
                if not run.get("restore_verified"):
                    raise RuntimeError("phone-side policy restoration was not verified")
                device = run.get("device", device)  # compare the same ALSA interface in B
        summary.update(verdict=verdict(runs), pcm_listing=pcm)
    except Exception as error:
        summary["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            summary["stderr"] = error.stderr
    summary["restore_verified"] = restoration["verified"]
    print(json.dumps(summary), flush=True)
    return 0 if summary["verdict"] == "no_underruns_observed" else 1


if __name__ == "__main__":
    sys.exit(main())
