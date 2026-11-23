# H1, Soak C and H3 execution sequence

The acceptance evidence comes from the selected production image. The named
commands below are for its execution owner. Paths, dates, process IDs, grants
and digests must come from that run. Keep raw captures, manifests and settings
in a fresh private evidence directory outside Git.

## Entry and H1

1. Obtain an exclusive grant with time for restoration. Record the phone,
   installed boot and target partition, active rootfs, kernel, source revisions,
   rollback disposition, selectors, runtime and helper SHA-256 values. Resolve
   the image choice first. A B5 result applies to B5. Require the project's full
   restoration/health check (K), closed E/R operations and no active soak,
   calibration, drain or conflicting controller. Register H1 maintenance.
2. Stage the reviewed `i14-h1.py`, `i14-h1-input.py` and `i14-touch` exclusively
   in `/run` as root, mode 0700. Check their hashes on the phone. The owner
   supplies root-owned mode-0600 `/run/h1-freeze.json` and
   `/run/i14-h1-permit.json` with the observed boot ID. Freeze both UI
   entrypoints, Python UI modules, selector and helper as required by the
   input harness. Bind any accepted rollback marker by digest. The permit
   expires within 900 s of current uptime. Check the grant before each block.
3. Use the same workload, settings and observers across both arms. Record
   brightness, CPU policy/frequency, USB, temperature and guard state. The owner
   requires USB online, 0–35 °C, the pinned settings, fresh valid charge guard,
   level 50–80, raw level ≤80, limit 80 enabled and matching BCCT state.
   Keep the maintenance logger running. Never read `current_cmd`.
4. Send the hashed `scripts/i14-h1-owner.py` as stdin to the approved managed
   transport for **one** `python3 -B - ACTION SERVICE IMPL [OUT]` at a time.
   Preserve argv, input hash, UTC, exit status, stdout, stderr and raw result.
   Use fresh `/run/h1-*.json` names for each case. Run in the following order.

   | Measurement | Order and invocation | Endpoint |
   |---|---|---|
   | Idle | For each of `ui`, `power`, `cpufreq`, `netwatch`, run `idle SERVICE rust OUT`, then `idle SERVICE python OUT`, then `select SERVICE rust`. Both arms start fresh and wait for natural screen-off. | 60 s at brightness zero. Record child PID/start ticks, actual uptime interval, CLK_TCK, CPU % of one core and ending RSS in KiB. Children excluded. The netwatch fallback is shell. |
   | Taps | Select UI Rust, then Python. For each run `python3 -B /run/i14-h1-input.py taps --impl IMPL --freeze /run/h1-freeze.json --out OUT`. Restore `select ui rust`. | Ten Settings/back pairs. Keep each first CRC change across rows 0–79, ACK separately, timeout and cold first action. The 2 ms poll and 3 s deadline measure framebuffer observation. Optical latency is unknown. |
   | Startup | From restored Rust OpenRC UI run `startup ui IMPL OUT` four times, Python, Rust, Python, Rust. Each action stops the UI, binds its measured child, tears it down and restores the Rust service. | Launch to first positive backlight, polled every 20 ms up to 10 s. Final pixels are unknown. |
   | Terminal | Select UI Rust, then Python. For each run `python3 -B /run/i14-h1-input.py terminal --impl IMPL --freeze /run/h1-freeze.json --out TRACE --terminal-out RAW`. Use distinct fresh paths. | Direct UI child login shell and controlling PTY must be proven before typing. Time `seq 1 20000` through PTY writes and command exit. Final rendering is unknown. |

   Input blocks require the PIN gate, fresh helper, 60 s quiet interval, fresh
   UI bound to that helper and the recorded display geometry. Keep partial data
   on any refusal and stop dependent cases. Renew a still-current permit only
   between blocks after rechecking the owner's grant and readiness.
5. Hash and export every result, including failed cases. Report per-pair Rust
   minus fallback and relative change only with nonzero denominator. Restore
   all five Rust selectors, services, guard and settings, stop owned helper
   and verify its socket/injector and owned temporary files are accounted for.
   Verify full K and close maintenance. H1 remains open for any missing arm.

## Commission the collector before reserving the 48 hours

1. Use a **short separate phase** to prove the production collection and export
   path. Under a fresh grant take the read-only inventory
   (`python3 -B i14-soak-observer-inventory.py`). Establish the boot, sampler
   cron row/implementation, log rotation, real record sizes, available userdata
   and host space. Verify the five-minute external DNS timer targets the
   intended resolver and the minute sampler is already running once. Record
   their exact deployed hashes and settings. Keep the existing sampler in place.
2. The owner supplies a fresh, private `phase-YYYYMMDDTHHMMSSZ` directory under
   `/mnt/data/media/0/i14-linux/soak-c-observer-20261115`. Its `logs/` and
   `receipts/` directories must be root-owned mode 0700. Stage
   `i14-soak-local-collector.py`, `i14-soak-observer-inventory.py` and
   `i14-soak-procs.sh` (installed name `procs.sh`) as root-owned mode 0700.
   Record the original private root crontab as mode-0600 `root-crontab.before`.
   Freeze mode-0600 `observer.json` with schema 2, actual `boot_id`, phase ID
   in `grant_id`, future `expires_epoch`, `max_captures`, `max_bytes`,
   `capture_bytes`, `reserve_bytes` and all three installed `source_sha256`
   entries. Size the phase from observed growth and both boundary captures.
   The export guard admits at most 80 captures and 1,281 MiB, 16 MiB per attempt
   and a 512 MiB free-space reserve. Account separately for sample rotation,
   filesystem allocation and background growth. A limit refusal stops the phase.
3. Using the owner's reviewed crontab installation procedure, add one hourly
   row at the frozen minute running the following command with absolute paths.
   Bound cron diagnostics too. Preserve all other crontab bytes, including the
   existing sampler. Record the installed crontab and owned row hashes.

   ```sh
   /usr/bin/python3 -B "$PHASE/i14-soak-local-collector.py" --run-dir "$PHASE" --origin cron
   ```

   Substitute the actual path before installing the row. Wait for **real root
   cron**. Require a completed PASS/rc 0 receipt, cron ancestry, matching
   boot/hashes, complete index entry, process roles and logs, closed helper
   group and absent capture lock. Preserve missing, failed and timed-out attempts.
4. After the last needed capture, restore the byte-identical original crontab
   using the owner's close procedure. Verify no collector/group or lock,
   preserve `close-receipt.json` with `CRON_RESTORED_NO_ACTOR`, and bind the
   phase, close grant, config/source/cron hashes and receipt digest. Stage the
   verified phase-export program and its two pinned sibling modules outside the
   phase evidence tree. The sender accepts a current, separate ≤900 s export
   bundle on stdin. The receiver needs its four independently frozen binding
   fields, `phase_id`, export `grant_id`, `boot_id`, `config_sha256`.
5. Use the approved foreground transport, with its exit status propagated and
   diagnostics on stderr. The remote command is
   `python3 -B /OWNER-STAGED-PATH/i14-soak-phase-export.py --stream`. Invoke it
   from the host through the receiver with the owner's actual transport argv.

   ```sh
   python3 -B scripts/i14-soak-receive.py --binding binding.json --out NEW_RECEIPT_DIR \
       --timeout REMAINING_SECONDS -- TRANSPORT_ARGV... < closed-phase.json
   ```

   Require receiver exit 0 and a complete `verified.json` with transport exit
   0, matching phase binding and locally verified per-file hashes. Preserve the
   source and any partial transfer on failure. The installation, grants, close
   receipt and managed transport come from the execution owner. Only a real
   unattended capture, teardown and verified export completes commissioning.

## Soak C and H3

1. After H1 restoration perform the single approved reboot, independently verify
   the new boot and full K (at least ten minutes after startup), and allow at
   least one unchanged hour. Prepare the final, separately bound collector on
   the observed boot. Before the window, require actual scheduled capture and
   freeze the exact image/boot, runtime files, root crontab, sampler/probe
   versions, observer config/quotas, log timezone, audit command and future
   UTC endpoints exactly 172,800 s apart. Close maintenance and reserve the
   phone for the interval. Record the start baseline and pre-boundary samples,
   DNS rounds, messages and process captures.
2. Run 48 uninterrupted hours. At/after the end obtain the matching baseline,
   logs, process capture and post-boundary phone/DNS rounds. Close and export
   the observer as commissioned. Acquire both retained sample generations
   (rotated first, current second), external DNS CSV and maintenance file.
   Keep samples and DNS separate from the observer log archive. Preserve raw
   acquisition order, errors and boundary evidence.
3. On immutable private copies run the independent auditor with explicit Soak C
   values. Supply each actual generation once. Put outputs outside the logs tree.

   ```sh
   python3 -B scripts/i14-window-audit.py --samples samples.jsonl.1 samples.jsonl \
       --dns-csv dns.csv --maintenance maintenance.txt --baseline-start baseline-start.txt \
       --baseline-end baseline-end.txt --logs NEW_RECEIPT_DIR/raw/logs \
       --start "$START_UTC" --end "$END_UTC" --boot-id "$BOOT_ID" \
       --log-tz Europe/Amsterdam --max-log-gap 180 --json-out audit.json --summary-out audit.txt
   ```

   Require exit 0 and PASS on all six gates: schema/order, boundary coverage,
   version/process/log continuity and maintenance, seven service statuses and
   phone gaps ≤180 s, local DNS, five external DNS checks and gaps ≤450 s.
   Battery condition 6 is informational. Check the predeclared 48-hour duration
   explicitly because the auditor also supports shorter diagnostic intervals.
4. After final evidence acquisition and collector closure, take four separate
   read-only Rust service measurements on the same boot and image. Keep all
   selectors, services and clocks as found, let the screen turn off naturally,
   and obtain a fresh post-window permit. Bind each supervised Rust process by
   executable/hash, argv, parent, PID and start ticks. For each of `ui`,
   `power`, `cpufreq`, `netwatch`, use the frozen measurement payload directly:

   ```sh
   python3 -B /run/i14-h1.py idle --impl rust --service "$SERVICE" \
       --pid "$PID" --start-ticks "$START_TICKS" --out "/run/h1-postsoak-$SERVICE.json"
   ```

   The H1 **owner** idle action restarts the service and would invalidate H3.
   Keep the raw before/after values and ambient conditions, compare with the
   same-version Rust references, export/hash the results and verify cleanup.
