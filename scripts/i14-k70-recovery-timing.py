"""Host-only M0 recovery-timing analysis (plan 2026-11-17, section 3).

Reads a normalized saved JSON Lines trace (schema i14-k70-recovery-trace/1,
docs/K70-RECOVERY-TIMING.md) and measures the interval from the crash trigger
to the first confirmed production return, both anchored to the same identified
host monotonic clock session. Optional stage pairs are reported separately in
the returned production boot's uptime domain; the two clock domains are never
subtracted from each other and wall-clock values are never anchors.

A within-limit result proves timing only against the unchanged 40.000 s limit
(the historical crash test returned in 70.6 s). It does not establish M0
acceptance, pstore correspondence, restoration K, or a cause. Until a reviewed
capture exists, traces are generated only by synthetic test fixtures.

Exit codes: 0 within limit (40.000 s inclusive), 1 over limit, 2 incomplete or
inconsistent evidence, including malformed or ambiguous required input.
"""
import json
import math
import sys


LIMIT_S = 40.0
SCHEMA = "i14-k70-recovery-trace/1"
STAGE_ORDER = ("early_init", "sysinit_services", "mdev_coldplug")
MAX_TRACE_BYTES = 1024 * 1024
MAX_RECORDS = 4096
KNOWN_TYPES = {"contract", "crash_trigger", "production_return", "stage"}
MISSING_REASONS = {"missing_contract", "missing_crash_trigger", "missing_return_anchor"}
EXIT_CODES = {"WITHIN_LIMIT": 0, "OVER_LIMIT": 1, "INCOMPLETE": 2, "INCONSISTENT": 2}
SCOPE_NOTE = ("Timing evidence only against the unchanged 40.000 s limit. Does not "
              "establish M0 acceptance, pstore correspondence, restoration K, or a cause.")


def unique_object(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError()
        output[key] = value
    return output


def reject_constant(value):
    raise ValueError()


def is_str(value):
    return isinstance(value, str) and value != ""


def is_num(value):
    if type(value) is int:
        return 0 <= value <= 2**53
    return type(value) is float and math.isfinite(value) and value >= 0


def load_records(path):
    """Return (records, problems); each record is paired with its 1-based line."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TRACE_BYTES + 1)
    except OSError:
        return [], [{"line": None, "reason": "trace_unreadable"}]
    if len(raw) > MAX_TRACE_BYTES:
        return [], [{"line": None, "reason": "trace_too_large"}]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], [{"line": None, "reason": "invalid_encoding"}]
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    records, problems = [], []
    for number, line in enumerate(lines, 1):
        if len(records) >= MAX_RECORDS:
            problems.append({"line": number, "reason": "too_many_records"})
            break
        if line.strip() == "":
            problems.append({"line": number, "reason": "blank_line"})
            continue
        try:
            record = json.loads(line, object_pairs_hook=unique_object,
                                parse_constant=reject_constant)
        except (ValueError, RecursionError):
            problems.append({"line": number, "reason": "malformed_json"})
            continue
        if not isinstance(record, dict):
            problems.append({"line": number, "reason": "record_not_object"})
            continue
        if not is_str(record.get("type")) or record["type"] not in KNOWN_TYPES:
            problems.append({"line": number, "reason": "unknown_record_type"})
            continue
        records.append((number, record))
    return records, problems


def new_result():
    return {"tool": "i14-k70-recovery-timing", "schema": SCHEMA, "limit_s": LIMIT_S,
            "verdict": "INCONSISTENT", "exit_code": 2, "recovery": None,
            "stages": [], "stage_issues": [], "stage_evidence": "unevaluated",
            "problems": [], "scope_note": SCOPE_NOTE,
            "diagnostics": {"records": 0, "by_type": {}, "unconfirmed_returns": 0}}


def analyze_stages(records, boot_id):
    """Pair optional stage records on the returned boot's uptime clock only."""
    starts, ends, issues, seen = {}, {}, [], False
    for number, record in records:
        if record["type"] != "stage":
            continue
        seen = True
        name = record.get("stage")
        issue = {"line": number, "stage": name if is_str(name) else None, "reason": None}
        if not is_str(name):
            issue["reason"] = "malformed_stage"
        elif name not in STAGE_ORDER:
            issue["reason"] = "unknown_stage"
        elif record.get("clock") != "boot_uptime":
            issue["reason"] = "stage_wrong_clock"
        elif record.get("boot_id") != boot_id:
            issue["reason"] = "stage_mixed_boot"
        elif record.get("phase") not in ("start", "end"):
            issue["reason"] = "malformed_stage"
        elif not is_num(record.get("t_s")):
            issue["reason"] = "malformed_stage"
        if issue["reason"] is not None:
            issues.append(issue)
            continue
        bucket = starts if record["phase"] == "start" else ends
        if name in bucket:
            issues.append(dict(issue, reason="duplicate_stage_" + record["phase"]))
            continue
        bucket[name] = (record["t_s"], number)
    stages = []
    for name in STAGE_ORDER:
        if name in starts and name in ends:
            (start, _), (end, end_line) = starts[name], ends[name]
            if end < start:
                issues.append({"line": end_line, "stage": name, "reason": "reversed_stage"})
                continue
            stages.append({"stage": name, "boot_id": boot_id, "clock": "boot_uptime",
                           "start_uptime_s": start, "end_uptime_s": end,
                           "duration_s": round(end - start, 6)})
        elif name in starts or name in ends:
            orphan = starts.get(name) or ends.get(name)
            issues.append({"line": orphan[1], "stage": name, "reason": "unpaired_stage"})
    stages.sort(key=lambda item: item["start_uptime_s"])
    evidence = "absent" if not seen else ("issues" if issues else "valid")
    return stages, issues, evidence


def evaluate(records, problems):
    result = new_result()
    result["problems"] = problems
    counts = result["diagnostics"]["by_type"]
    result["diagnostics"]["records"] = len(records)
    for _, record in records:
        counts[record["type"]] = counts.get(record["type"], 0) + 1

    def problem(line, reason):
        problems.append({"line": line, "reason": reason})

    def single(kind, missing, duplicate):
        found = [(number, record) for number, record in records if record["type"] == kind]
        if not found:
            problem(None, missing)
            return None, None
        if len(found) > 1:
            problem(found[1][0], duplicate)
            return None, found[0][0]
        return found[0][1], found[0][0]

    contract, contract_line = single("contract", "missing_contract", "duplicate_contract")
    if contract is not None and (
            contract.get("schema") != SCHEMA
            or not all(is_str(contract.get(key)) for key in
                       ("host_clock_session", "experimental_boot_id",
                        "expected_kernel", "expected_image"))):
        problem(contract_line, "malformed_contract")
        contract = None

    crash, crash_line = single("crash_trigger", "missing_crash_trigger", "duplicate_crash_trigger")
    if crash is not None:
        if crash.get("clock") != "host_monotonic":
            problem(crash_line, "crash_clock_domain")
        elif not (is_str(crash.get("session")) and is_str(crash.get("boot_id"))
                  and is_num(crash.get("t_s"))):
            problem(crash_line, "malformed_crash_trigger")
        elif contract is not None:
            if crash["session"] != contract["host_clock_session"]:
                problem(crash_line, "crash_session_mismatch")
            if crash["boot_id"] != contract["experimental_boot_id"]:
                problem(crash_line, "crash_boot_mismatch")

    confirmed = []
    for number, record in records:
        if record["type"] != "production_return":
            continue
        if "confirmed" in record and type(record["confirmed"]) is not bool:
            problem(number, "malformed_return")
        elif record.get("confirmed") is True:
            confirmed.append((number, record))
        else:
            result["diagnostics"]["unconfirmed_returns"] += 1
    ret, ret_line = None, None
    if not confirmed:
        problem(None, "missing_return_anchor")
    elif len(confirmed) > 1:
        problem(confirmed[1][0], "ambiguous_return_anchor")
    else:
        ret_line, ret = confirmed[0]
    if ret is not None:
        if ret.get("clock") != "host_monotonic":
            problem(ret_line, "return_clock_domain")
        elif not (is_str(ret.get("session")) and is_str(ret.get("boot_id"))
                  and is_str(ret.get("kernel")) and is_str(ret.get("image"))
                  and is_num(ret.get("t_s"))):
            problem(ret_line, "malformed_return")
        else:
            if contract is not None:
                if ret["session"] != contract["host_clock_session"]:
                    problem(ret_line, "return_session_mismatch")
                if ret["kernel"] != contract["expected_kernel"]:
                    problem(ret_line, "kernel_mismatch")
                if ret["image"] != contract["expected_image"]:
                    problem(ret_line, "image_mismatch")
            experimental = (contract["experimental_boot_id"] if contract is not None
                            else crash.get("boot_id") if crash is not None else None)
            if is_str(experimental) and ret["boot_id"] == experimental:
                problem(ret_line, "no_new_production_boot")
    if (crash is not None and ret is not None and is_num(crash.get("t_s"))
            and is_num(ret.get("t_s")) and ret["t_s"] <= crash["t_s"]):
        problem(ret_line, "return_not_after_crash")

    if problems:
        result["verdict"] = ("INCOMPLETE" if all(
            item["reason"] in MISSING_REASONS for item in problems) else "INCONSISTENT")
        result["exit_code"] = EXIT_CODES[result["verdict"]]
        return result

    duration = round(ret["t_s"] - crash["t_s"], 6)
    result["recovery"] = {"clock": "host_monotonic",
                          "host_clock_session": contract["host_clock_session"],
                          "crash_trigger_s": crash["t_s"],
                          "production_return_s": ret["t_s"], "duration_s": duration,
                          "from_boot_id": crash["boot_id"], "to_boot_id": ret["boot_id"],
                          "kernel": ret["kernel"], "image": ret["image"],
                          "kernel_confirmed": True, "image_confirmed": True}
    result["verdict"] = "WITHIN_LIMIT" if duration <= LIMIT_S else "OVER_LIMIT"
    stages, stage_issues, evidence = analyze_stages(records, ret["boot_id"])
    result["stages"] = stages
    result["stage_issues"] = stage_issues
    result["stage_evidence"] = evidence
    result["stage_clock"] = "boot_uptime"
    result["stage_boot_id"] = ret["boot_id"]
    result["exit_code"] = EXIT_CODES[result["verdict"]]
    return result


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        result = new_result()
        result["problems"].append({"line": None, "reason": "usage_error"})
        print(json.dumps(result, sort_keys=True))
        return 2
    records, problems = load_records(argv[0])
    result = evaluate(records, problems)
    print(json.dumps(result, sort_keys=True))
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
