# K70 recovery-timing analyzer (host-only, M0)

`scripts/i14-k70-recovery-timing.py` measures the crash-to-production-return
interval for the Linux 7 recovery gate (M0) in
[the roadmap](ROADMAP.md#3-linux-7-mainline-recovery-m0), section 3.
The historical crash test returned in 70.6 s against the unchanged 40 s limit.
This tool is host-only: it reads a normalized saved JSON Lines trace and never
touches the phone, firmware or live evidence. Until a reviewed capture exists,
traces are generated only by the synthetic fixtures in
`tests/test_i14_k70_recovery_timing.py`.

A within-limit result proves **timing only**. It does not establish M0
acceptance, pstore correspondence, restoration K, or a cause; those remain
separate device acceptance items under a scoped grant.

## Usage

```sh
python3 scripts/i14-k70-recovery-timing.py TRACE.jsonl
```

Exactly one argument: the trace file. stdout is one machine-readable JSON
object; stderr stays empty and malformed input never produces a traceback.

| Exit code | Verdict | Meaning |
| --- | --- | --- |
| 0 | `WITHIN_LIMIT` | Observed duration ≤ 40.000 s (inclusive). Timing proof only. |
| 1 | `OVER_LIMIT` | Valid measurement, duration > 40.000 s. |
| 2 | `INCOMPLETE` | A required anchor is absent (contract, crash trigger, or confirmed return). Nothing is inferred. |
| 2 | `INCONSISTENT` | Malformed, ambiguous or conflicting evidence, including bad usage or an unreadable trace. |

Durations are computed in one clock domain and reported at microsecond
resolution (`round(x, 6)`); the limit comparison uses that reported value.

## Trace schema `i14-k70-recovery-trace/1`

UTF-8 JSON Lines, one JSON object per line, at most 1 MiB and 4096 records.
Strict JSON: duplicate keys, `NaN`/`Infinity`, blank lines, non-object lines
and unknown record types fail closed. Only the fields below are read; any
extra field (including wall-clock fields such as `utc`) is ignored and can
never serve as an anchor. Timestamps `t_s` are finite numbers ≥ 0 (integers
bounded by 2^53). File order is irrelevant; records are identified by `type`.

| `type` | Required fields | Role |
| --- | --- | --- |
| `contract` | `schema`, `host_clock_session`, `experimental_boot_id`, `expected_kernel`, `expected_image` (all non-empty strings; `schema` must equal the schema name) | Exactly one. Pins the identified host monotonic clock session, the experimental boot, and the expected production kernel and image identity. |
| `crash_trigger` | `clock: "host_monotonic"`, `session`, `boot_id`, `t_s` | Exactly one. The crash trigger on the experimental boot, in the contract's host session. |
| `production_return` | `clock: "host_monotonic"`, `session`, `boot_id`, `kernel`, `image`, `t_s`, `confirmed: true` | Exactly one confirmed record: the first confirmed production return. Unconfirmed records (`confirmed` absent or `false`) are counted as diagnostics, never anchors; several confirmed returns are ambiguous. |
| `stage` | `stage`, `phase` (`start`/`end`), `clock: "boot_uptime"`, `boot_id`, `t_s` | Optional, paired. Timed on the returned production boot's uptime clock only. |

### Transition and identity rules

The measurement requires a valid transition: the crash trigger sits on the
contract's experimental boot, the confirmed return carries a **different**
boot identity in the **same** host clock session, and its `kernel` and `image`
explicitly match the contract. The return must be strictly after the crash
trigger. Any violation is `INCONSISTENT` with a specific reason code
(`crash_session_mismatch`, `return_session_mismatch`, `no_new_production_boot`,
`kernel_mismatch`, `image_mismatch`, `crash_boot_mismatch`,
`return_not_after_crash`, ...). Wall-clock or uptime values on required
anchors are rejected (`crash_clock_domain`, `return_clock_domain`); a missing
anchor is never inferred from stages or later completions.

### Clock domains

Host monotonic seconds (`host_monotonic`, anchored to `host_clock_session`)
and returned-boot uptime seconds (`boot_uptime`) are separate domains. The
tool never subtracts across them: `recovery.duration_s` uses only the two host
anchors; stage intervals use only uptime pairs.

### Optional stages

Stage vocabulary, in boot order: `early_init` (early init/dependency setup),
`sysinit_services` (sysinit service execution), `mdev_coldplug` (mdev/coldplug).
Each name needs exactly one `start` and one `end` on the returned boot, with
`end ≥ start`. Valid pairs appear in `stages`, sorted by start uptime, with
`start_uptime_s`, `end_uptime_s` and `duration_s`.

Problematic stage records are called out separately in `stage_issues`
(`{line, stage, reason}`) and are excluded from `stages`, so they cannot become
measurement evidence: `malformed_stage`, `unknown_stage`, `stage_wrong_clock`,
`stage_mixed_boot` (not the returned boot), `duplicate_stage_start`,
`duplicate_stage_end` (first occurrence kept), `unpaired_stage`,
`reversed_stage`. Stage issues do not alter the timing verdict;
`stage_evidence` reports `absent`, `valid`, `issues`, or `unevaluated` (when the
core evidence is invalid).

## Output

One JSON object: `verdict`, `exit_code`, `limit_s`, `recovery` (host-domain
anchors, `duration_s`, boot transition, confirmed kernel/image; `null` unless
the core evidence is fully valid), `stages`, `stage_issues`, `stage_evidence`,
`problems` (`{line, reason}` for required-evidence failures), `diagnostics`
(record counts, unconfirmed returns) and `scope_note` stating the timing-only
scope.

## Limitations

Synthetic fixtures only; no capture pipeline or device evidence exists yet.
The analyzer cannot verify pstore, restoration K, power state or the cause of
the slow return, and a within-limit fixture run is not a recovery acceptance.
