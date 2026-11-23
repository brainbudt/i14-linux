# K70 recovery-timing analyzer (host-only, M0)

`scripts/i14-k70-recovery-timing.py` measures the crash-to-production-return
interval for the Linux 7 recovery gate (M0) in
[the roadmap](ROADMAP.md#3-linux-7-mainline-recovery-m0), section 3.
The historical crash test returned in 70.6 s against the unchanged 40 s limit.
The analyzer reads a saved JSON Lines trace. A passive writer,
`scripts/i14_k70_capture.py`, can record either terminal trigger inside a
host observer. Both paths have been exercised with synthetic observations.

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

The limit applies to the unrounded host interval (`duration_unrounded_s`).
`duration_s` is its display value rounded to six decimal places.

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

New captures also include `previous_production_boot_id` in the contract and
`method` (`sysrq-crash` or `keeper-stop`) in the trigger. Both methods use
the historical wire name `crash_trigger`. A confirmed return carrying the
pre-trial production boot ID invalidates the trace. Older traces remain readable.

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
`duplicate_stage_end` (the entire ambiguous pair is excluded), `unpaired_stage`,
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

## Capture and trial order

1. **Commission the observer offline.** The live trip runner and returned-boot
   stage producers are outside this checkout. Bind the writer to the reviewed
   runner's dispatch and first identity-probe completion in one host process.
   For each trial, open a new private JSONL file in exclusive creation mode.
   Construct `RecoveryCapture(stream, previous_boot=..., experimental_boot=...,
   kernel=..., image=...)` using the observed pre-trial and RAM-boot IDs and
   the reviewed production pins. Wrap just the terminal dispatch in
   `with capture.trigger(method):`. Raise if dispatch is refused or uncertain.
   Call `observe_return(boot_id=..., kernel=..., image=...)` at probe completion
   with observed identities. Preserve raw probe successes and failures. Reading
   a saved log later cannot recreate either host timestamp.
2. Bind both edges of `early_init`, `sysinit_services` and `mdev_coldplug` to
   the corresponding operations in the pinned production startup. Export each
   observed edge with its original production boot ID and uptime through
   `capture.stage(name, phase, boot_id, uptime_s)`. Exercise the entire path
   with fixtures and review the exact runner, marker sources and artifact pins.
   The writer performs no boot instrumentation. Missing edges leave that
   interval unknown. Keep raw evidence, capture errors and restoration handling
   independent so a logging failure cannot skip recovery.
3. **Fresh M0 grant and preflight.** Reserve one actor and a window clear of
   calibration or soak. Verify the board, normal 3.18.19 boot ID, production
   boot/rootfs and rollback hashes, serial recovery, healthy services and
   fresh same-boot guard matching BCCT. Require USB power, 3800–4100 mV,
   10–40 °C, no drain or experiment, and the reviewed RAM image and keeper hashes.
4. **Crash trial first.** RAM-boot once. Confirm the 7.0 board/kernel and
   experimental boot ID, no-touch settings, read-only eMMC, armed watchdog and
   keeper, and its battery margin. Dispatch the reviewed `sysrq-crash` check.
   Observe the first confirmed new production return. Run
   `python3 scripts/i14-k70-recovery-timing.py TRACE.jsonl` on the host. Save
   raw trace, exit code, last_kmsg/pstore evidence of the same trial and full
   production restoration K1–K7, including guard/service continuity.
5. **Keeper-stop is conditional.** On missing evidence, a late return, a
   pstore mismatch or failed restoration, stop and retain the failure. An
   unconfirmed return goes to the authorized recovery procedure. Only after
   independent crash acceptance and renewed preflight under a covering grant
   run a separate RAM-only `keeper-stop` trial in a new directory. Apply the
   same 40 s bound and full restoration checks. Require evidence of the keeper
   stopping its feeds and the watchdog reset; a fallback reboot fails that check.

Historical status remains 70.6 s for the crash trial, with no accepted
keeper-stop trial. The source of the delay remains unknown.
