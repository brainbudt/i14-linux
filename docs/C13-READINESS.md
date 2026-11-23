# C13 campaign and 80% band checks

C13 measures usable charge from stock Full to a software reserve on the
internal meter. The natural 80% band and the guard crash inside that band each
need their own observations under the production guard. Schedule them outside
the calibration attempts.

1. **Build on the host.** Run `python3 -B scripts/i14-calibration-launch.py
   --build NEW_DIRECTORY`, then `--verify NEW_DIRECTORY` with the same script
   and `sh -n NEW_DIRECTORY/wrapper.sh`. Independently review the exact package
   and measurement hashes, runtime pins, retention budgets and launch gate.
   Changed bytes need a fresh review and a grant bound to the new manifest.
2. **Reserve one operator and prove supervision.** The coordinator grants an
   exclusive window up to 72 hours including restoration. Verify export and a
   real unattended continuation through the deadline. The probe must be at
   most 15 minutes old, the next checkpoint within one hour, with a boundary
   check within five minutes. Retain the actual receipts behind the gate hashes.
3. **Preflight immediately before each launch.** Require fresh K1–K7, pinned
   board, normal stock 3.18.19 boot ID, verified recovery routes, workload,
   software/configuration and services. Require two valid same-boot guard
   readings at least ten minutes apart, the latest and K at most 150 s old.
   USB/AC must be online, voltage ≥3700 mV, stock capacity ≥10%, temperature
   0–40 °C, and the production guard valid with no drain or competing writer.
   Read charging state from the approved cached getters and BCCT readback.
   Reading `current_cmd` disables charging.
4. **Budget both cycles and all writers.** Verify actual userdata backing
   and available bytes after any image staging. The start gate requires the
   larger of 2 GiB and **3040 MiB + fixed observer bytes + background growth
   bytes**. Fixed observer overhead includes at least 1 MiB. Background
   growth includes at least 3,934,208 bytes for both guard logs plus the
   reviewed ceilings of other writers. Verify quota and retention on each
   backing filesystem through closure. Rows are capped at 128 KiB and each
   sample trace at 480 MiB. Stop at ≤512 MiB free or on an unreadable budget.
5. **Attempt 1.** Place the reviewed bundle and newly bound gate in the
   launcher's fixed code directory. From that directory the approved entry
   point is `sh ./wrapper.sh gate-1.json`. Each attempt consumes a slot
   before the child starts. Allow at most eight hours to reach the 300 s
   stock Full anchor and 32 hours for the whole measurement. Keep the cable
   attached. Stop discharge at 3550 mV or stock capacity 6%, whichever is
   reached first. Use `python3 -B launcher.py --checkpoint` to inspect and
   `--ack-checkpoint ACK_FILE` for a fresh matching scheduler receipt.
   A missed checkpoint beyond its five-minute grace stops the attempt.
   `--stop` targets the identified measurement process. Let the owning
   supervisor finish teardown and restart production.
6. **Restore and review before attempt 2.** Export raw samples, state,
   results or failure, wrapper receipts and hashes. Verify child and wrapper
   exit, actual BCCT release, fresh production guard, services, unchanged
   settings/hashes and full K1–K7. Recover to ≥3700 mV and ≥10% within four
   hours. Incomplete teardown retains ownership. A safety abort ends the
   campaign. For a complete accepted cycle, replay the trace and freeze the
   cycle-1-only fit and input hashes in the independent review. With the
   same boot, package and runtime, a fresh matching gate and at least
   32 h + 4 h + 30 s remaining, launch `sh ./wrapper.sh gate-2.json`
   within the four-hour restoration/start window. Repeat restoration.
7. **Score the holdout offline.** The reviewer checks complete traces,
   coverage, comparable workload, reset segments and the same reserve type.
   Require reserve voltages within 50 mV, discharge median temperatures
   within 3 °C, and usable-charge difference ≤5% of the pair's mean.
   Unknown-interval charge must be ≤1% of usable charge per cycle. Evaluate
   the frozen fit against cycle 2's own CAR reference at all common 10–90%
   landmarks without extrapolation or refitting. Require mean absolute error
   ≤5 percentage points and maximum ≤10. A result file or launcher success
   only releases the data for review. A failed gate ends the campaign without
   a profile or a third attempt.
8. **Production band and crash-in-band.** With a separate grant, normal
   production ownership and unchanged 80% settings, capture the natural ZCV
   rise to ≥4109 mV and verified BCCT pause, descent through the band,
   release at ≤4003 mV and continued charging back inside the band. For the
   crash check, first establish a fresh same-boot hardware pause strictly
   inside 4003–4109 mV. Under the reviewed guard-crash procedure capture
   the old and respawned guard PIDs, same-boot ZCV/status and matching BCCT
   readbacks across the crash while still inside the band. Verify the pause
   survives and the natural lower crossing releases it. Finish with full
   restoration. A threshold crossing during the crash leaves hysteresis
   unproven. Keep raw observations for both checks.
