"""Read-only local evidence intake, never an executor or hardware admission.

Default: inspect the fixed 2026-11-02 package. Optional --receipt-dir reads
exactly intake-receipt.json, result.json and cached-check.txt from a local copy.
Exit 1 and BLOCKED are intentional, including for a complete hypothetical copy.
"""
import argparse
import datetime
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


ROOT = Path(__file__).absolute().parents[1]
PACKET = "artifacts/pmic-pair-build-prep-20261102"
FINAL = "artifacts/pmic-pair-final-20261102/result.json"
WORKER_SHA = "8103f7ffea390e6e8ca60cabafad712722ab893ba02c39333ba003807b60a802"
FIXED_PINS = {
    "kernel/i14-pmic-observer-core.h": "8ee43a11667fad7147d44b4a1f91f5e093298febf05a929b6f35853e2da2b4b5",
    "kernel/i14-pmic-observer.c": "a8de089daf4006e65a99274f9acb0b80e3880a354be3590d29bd148d9d48ae0d",
    "kernel/i14-pmic-observer-pair.c": "e065ada2f279fb6139dcacb9800669259031ae441b302a6676d3c04b651a1a2b",
    PACKET + "/SHA256SUMS": "7573f8d45840d0858e8c4bcf25383f0cadfc22ef3a78b27695e1ca77358d612a",
    FINAL: "6cc979bb92adc440005a263866ae1429efb38a8443eef5d5b516aa0715357b33",
    "artifacts/pmic-pair-final-20261102/REPORT.md": "510a876f07140b2e8bba1e4017fa22a9b293a25f220fdd076f4d4698ae4bf633",
    "artifacts/pmic-pair-code-review-20261102/REPORT.md": "c95a64a5861284382b84bac21810f93dc8af3e7412ecd384c2395448896b7633",
    "scripts/wait-native-cached-once.py": WORKER_SHA,
}
PROFILE = "fixed-pair-f48-f7a-v1"
KERNEL = "7.0.10-i14-bringup"
MODEL = "9282e73850f4d515884c1a11a270f1d3f218a302b35875aa6e17639bfe38726a"
COMPAT = "867d5ca5770c5871fe10fdfdd4a8523764877c7246b9c21bd9714380bafccd9a"
RO_NAMES = {"mmcblk0", "mmcblk0boot0", "mmcblk0boot1"} | {"mmcblk0p%d" % n for n in range(1, 24)}
POWER_ORDER = ["identity", "boot_id", "uptime", "kernel_log", "cmdline", "mounts", "chosen_bootargs",
               "chosen_bootargs_override", "usb_startup_log", "storage_startup_log", "usb_snapshot_log",
               "modules", "mmcblk0_size", "mmcblk_ro", "mmcblk0_stat", "cached_i14_charger",
               "cached_battery_sample", "maxpower", "udc_state", "usb0_carrier", "boot_id_end", "uptime_end"]
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CAP = 2 * 1024 * 1024
MAX_AGE_SECONDS = 300


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, cap=CAP):
    """Descriptor walk rejects symlinks in every component, including parents."""
    path = Path(path).absolute()
    need(".." not in path.parts and len(path.parts) > 1, "nonphysical path")
    directory = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(fd)
            need(stat.S_ISREG(before.st_mode), "not a regular file")
            need(before.st_size <= cap, "file exceeds byte cap")
            chunks, count = [], 0
            while count <= cap:
                chunk = os.read(fd, min(65536, cap + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
            after = os.fstat(fd)
            need(count <= cap, "file exceeds byte cap")
            need((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "file changed while reading")
            return b"".join(chunks)
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, "duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("nonfinite JSON number")

    def finite_float(value):
        number = float(value)
        need(math.isfinite(number), "nonfinite JSON number")
        return number

    need(type(data) is bytes and len(data) <= CAP, "invalid JSON byte input")
    result = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                        parse_constant=constant, parse_float=finite_float)
    need(type(result) is dict, "JSON root must be an object")
    return result


def inspect_package(root=ROOT):
    """Hash the pinned packet as data; no builder or worker is imported."""
    issues, checked, contents = [], {}, {}

    def check(relative, digest):
        try:
            data = read_regular(Path(root) / relative)
            checked[relative] = sha(data)
            need(checked[relative] == digest, "hash mismatch")
            contents[relative] = data
        except (OSError, ValueError) as error:
            issues.append(relative + ": " + str(error))

    for relative, digest in FIXED_PINS.items():
        check(relative, digest)
    sums = contents.get(PACKET + "/SHA256SUMS")
    if sums is not None:
        try:
            names = set()
            for line in sums.decode("ascii").splitlines():
                match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./-]+)", line)
                need(match is not None, "invalid frozen checksum line")
                digest, name = match.groups()
                parts = Path(name).parts
                need(not Path(name).is_absolute() and ".." not in parts and name not in names,
                     "invalid or duplicate frozen checksum path")
                names.add(name)
                check(PACKET + "/" + name, digest)
            need(len(names) == 35, "wrong frozen packet inventory")
        except (ValueError, UnicodeError) as error:
            issues.append(str(error))
    result = {"integrity": "MISMATCH" if issues else "MATCH", "profile": PROFILE,
              "native_module": "UNKNOWN", "read_qualified": None, "observer_words": 2,
              "module_source_review": "UNKNOWN", "checked_files": len(checked), "reasons": issues}
    if not issues:
        final = strict_json(contents[FINAL])
        manifest = strict_json(contents[PACKET + "/input-manifest.json"])
        need(final["profile"] == manifest["profile"] == PROFILE, "frozen profile contradiction")
        need(type(final["READ_QUALIFIED"]) is int and final["READ_QUALIFIED"] == 0,
             "frozen read gate contradiction")
        need(type(manifest["read_qualified"]) is int and manifest["read_qualified"] == 0,
             "frozen manifest read gate contradiction")
        need(final["native_module"] == manifest["native_module"] == "UNBUILT", "frozen build contradiction")
        result.update(native_module="UNBUILT", read_qualified=0,
                      module_source_review="APPROVE; offline source preparation only")
    return result


def frames(raw):
    """Standalone ordered response frames qualify; echoed command text cannot."""
    lines = raw.decode("utf-8").replace("\r\n", "\n").split("\n")
    found, active, body = {}, None, []
    for line in lines:
        if line.startswith("@@I14_BEGIN "):
            name = line[len("@@I14_BEGIN "):]
            need(active is None and name not in found, "duplicate or nested response frame")
            active, body = name, []
        elif line.startswith("@@I14_END "):
            need(active is not None and line == "@@I14_END " + active, "broken response frame")
            found[active], active = body, None
        elif active is not None:
            body.append(line)
    need(active is None and list(found) == POWER_ORDER, "missing, reordered or partial response frames")
    for marker in ("I14_IDENTITY_OK", "I14_CACHED_CAPTURE_DONE", "I14_CHECK_END"):
        need(lines.count(marker) == 1, "missing or duplicate completion marker")
    need("I14_STOP_IDENTITY" not in lines, "contradictory identity marker")
    need(lines.index("@@I14_END identity") < lines.index("I14_IDENTITY_OK")
         < lines.index("@@I14_BEGIN boot_id"), "identity marker order")
    need(lines.index("@@I14_END uptime_end") < lines.index("I14_CACHED_CAPTURE_DONE")
         < lines.index("I14_CHECK_END"), "completion marker order")
    log = found["kernel_log"]
    need(log == ["I14_ABSENT"] or
         (sum(bool(re.fullmatch(r"@@I14_DMESG_RC [0-9]+", line)) for line in log) == 1
          and re.fullmatch(r"@@I14_DMESG_RC [0-9]+", log[-1]) is not None), "kernel-log read status missing")
    return found


def typed_equal(actual, expected, label):
    need(type(actual) is type(expected) and actual == expected, label + " missing, wrong type or contradictory")


def validate_copy(envelope_bytes, result_bytes, raw, *, now):
    """Validate a copied local claim, never authenticity or current live state."""
    answer = {"status": "UNKNOWN", "once_status": "UNKNOWN", "new_ap_boot": "UNKNOWN",
              "pmic_reset": "UNKNOWN", "reasons": []}
    try:
        need(type(raw) is bytes and len(raw) <= CAP, "invalid raw byte input")
        envelope, result = strict_json(envelope_bytes), strict_json(result_bytes)
        need(set(envelope) == {"schema", "captured_at", "worker_sha256", "raw_sha256", "result_sha256",
                               "previous_boot_id", "once_status", "historical_pmic_outcome", "global_block"},
             "unknown or missing intake-receipt fields")
        typed_equal(envelope["schema"], 1, "receipt schema")
        typed_equal(envelope["worker_sha256"], WORKER_SHA, "worker source binding")
        typed_equal(envelope["raw_sha256"], sha(raw), "raw receipt binding")
        typed_equal(envelope["result_sha256"], sha(result_bytes), "result receipt binding")
        need(type(envelope["once_status"]) is str and envelope["once_status"] in {"CONSUMED", "UNKNOWN"},
             "historical guard cannot be cleared by a copy")
        answer["once_status"] = envelope["once_status"]
        typed_equal(envelope["historical_pmic_outcome"], "UNKNOWN", "historical PMIC uncertainty")
        typed_equal(envelope["global_block"], "INTEGRITY_BLOCK", "current global block")
        need(type(envelope["captured_at"]) is str
             and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", envelope["captured_at"]),
             "capture timestamp must be UTC seconds")
        captured = datetime.datetime.strptime(envelope["captured_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
        need(isinstance(now, datetime.datetime) and now.utcoffset() is not None, "aware clock required")
        age = (now - captured).total_seconds()
        need(0 <= age <= MAX_AGE_SECONDS, "stale or future copied receipt")
        typed_equal(result["transport_rc"], 0, "transport rc")
        typed_equal(result["exit_code"], 0, "result exit code")
        typed_equal(result["capture_complete"], True, "complete capture")
        typed_equal(result["verdict"], "COMPLETE_POWER_EVIDENCE", "complete boot-bound worker profile")
        found = frames(raw)
        # The fixed worker cannot emit a complete capture with these opposite
        # claims. Matching hashes alone do not make copied JSON consistent.
        for key, expected in {"attempted": True, "charging_blocked": True,
                              "charging_status": "UNKNOWN", "ready_for_charging": False,
                              "stop_reasons": []}.items():
            typed_equal(result[key], expected, "worker " + key)
        expected_framing = {"I14_IDENTITY_OK": 1, "I14_STOP_IDENTITY": 0,
                            "I14_CACHED_CAPTURE_DONE": 1, "I14_CHECK_END": 1}
        need(type(result["framing"]) is dict and set(result["framing"]) == set(expected_framing),
             "worker framing fields missing or unexpected")
        for marker, count in expected_framing.items():
            typed_equal(result["framing"][marker], count, "worker framing " + marker)
        identity = found["identity"]
        need(len(identity) == 5 and identity[:2] == [KERNEL, "aarch64"]
             and re.fullmatch(r"Uid:\s+0\s+0\s+0\s+0", identity[2]) is not None
             and identity[3:] == [MODEL + "  /proc/device-tree/model",
                                  COMPAT + "  /proc/device-tree/compatible"], "exact I14/root identity failed")
        for key, expected in {"ok": True, "kernel": KERNEL, "device_lines": identity}.items():
            typed_equal(result["identity"][key], expected, "identity " + key)
        boot, end = found["boot_id"], found["boot_id_end"]
        need(len(boot) == 1 and re.fullmatch(UUID, boot[0]) is not None and end == boot,
             "missing or changed begin/end AP boot")
        previous = envelope["previous_boot_id"]
        need(previous is None or (type(previous) is str and re.fullmatch(UUID, previous) is not None),
             "invalid previous AP boot")
        uptimes = []
        for name in ("uptime", "uptime_end"):
            need(len(found[name]) == 1 and re.fullmatch(r"\d+\.\d+ \d+\.\d+", found[name][0]) is not None,
                 "invalid uptime")
            uptimes.append(Decimal(found[name][0].split()[0]))
        need(uptimes[1] >= uptimes[0], "regressed uptime")
        for key, expected in {"boot_id": boot[0], "end_boot_id": boot[0], "same_boot": True,
                              "uptime": found["uptime"], "end_uptime": found["uptime_end"],
                              "modules": found["modules"]}.items():
            typed_equal(result["boot"][key], expected, "boot " + key)
        typed_equal(found["mmcblk0_size"], ["30535680"], "capacity response")
        ro = {}
        for line in found["mmcblk_ro"]:
            match = re.fullmatch(r"(mmcblk0(?:boot[01]|p[0-9]+)?) ([01])", line)
            need(match is not None and match[1] not in ro, "invalid or duplicate block RO response")
            ro[match[1]] = match[2]
        need(set(ro) == RO_NAMES and len(ro) == 26 and set(ro.values()) == {"1"}, "exact 26 RO nodes required")
        stats = found["mmcblk0_stat"]
        need(len(stats) == 1 and re.fullmatch(r"\s*\d+(?:\s+\d+){10,}\s*", stats[0]) is not None,
             "invalid block stat")
        need(all(int(value) == 0 for value in stats[0].split()[4:8]), "zero block writes required")
        mounts = found["mounts"]
        need(bool(mounts) and all(len(line.split()) == 6 for line in mounts), "incomplete mounts")
        need(any(line.split()[1:3] == ["/", "rootfs"] for line in mounts), "rootfs mount missing")
        need(not any("mmcblk" in line.split()[0] for line in mounts), "block-device mount is outside baseline")
        expected_storage = {"capacity_sectors": 30535680, "capacity_ok": True, "ro_node_count": 26,
                            "ro_nodes": ro, "names_ok": True, "all_ro": True, "stat": stats,
                            "sectors_written": 0, "mounts": mounts}
        for key, expected in expected_storage.items():
            typed_equal(result["storage"][key], expected, "storage " + key)
        answer.update(status="VALID_LOCAL_COPY", boot_id=boot[0], age_seconds=age,
                      new_ap_boot=(previous != boot[0]) if previous is not None else "UNKNOWN")
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError) as error:
        answer["reasons"].append(str(error))
    return answer


def inspect_copy(directory, *, now):
    try:
        return validate_copy(*(read_regular(Path(directory) / name) for name in
                               ("intake-receipt.json", "result.json", "cached-check.txt")), now=now)
    except (OSError, ValueError) as error:
        return {"status": "UNKNOWN", "once_status": "UNKNOWN", "new_ap_boot": "UNKNOWN",
                "pmic_reset": "UNKNOWN", "reasons": [str(error)]}


def decide(package, baseline):
    reasons = ["OFFLINE_ONLY_NO_RUNTIME_INTEGRATION", "GLOBAL_INTEGRITY_BLOCK_RETAINED",
               "HISTORICAL_PMIC_UNKNOWN_RETAINED", "CUSTODY_AND_DURABLE_ONCE_NOT_ACQUIRED"]
    if package.get("integrity") != "MATCH":
        reasons.append("PACKAGE_INTEGRITY_UNKNOWN_OR_MISMATCH")
    if package.get("native_module") == "UNBUILT":
        reasons.append("NATIVE_MODULE_UNBUILT")
    if package.get("read_qualified") == 0:
        reasons.append("READ_QUALIFIED_ZERO")
    if baseline.get("status") != "VALID_LOCAL_COPY":
        reasons.append("BASELINE_UNKNOWN")
    return {"schema": 1, "status": "BLOCKED", "dispatch_allowed": False, "first_read_permission": False,
            "custody": "NOT_ACQUIRED", "charging": "UNKNOWN", "package": package, "baseline": baseline,
            "historical_pmic_outcome": "UNKNOWN", "global_block": "INTEGRITY_BLOCK",
            "live_state_inspected": False, "once_obligation": "PRESERVE_EXISTING_CONSUMED_OR_UNKNOWN_GUARDS",
            "reasons": reasons}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt-dir", type=Path, help="local copied data directory; fixed three filenames")
    args = parser.parse_args(argv)
    package = inspect_package()
    baseline = {"status": "UNKNOWN", "once_status": "UNKNOWN", "new_ap_boot": "UNKNOWN",
                "pmic_reset": "UNKNOWN", "reasons": ["NO_LOCAL_RECEIPT_SUPPLIED"]}
    if args.receipt_dir is not None:
        baseline = inspect_copy(args.receipt_dir, now=datetime.datetime.now(datetime.timezone.utc))
    print(json.dumps(decide(package, baseline), sort_keys=True, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
