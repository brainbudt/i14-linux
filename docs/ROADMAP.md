# I14 Linux Roadmap & Acceptance Gates

This roadmap summarizes the active engineering milestones and remaining device
acceptance gates as of 22 November 2026. The near-term goal is a maintained
source baseline on `main`, verified offline backup tooling, and measured
acceptance of one production Alpine image on the stock 3.18.19 kernel. Linux 7
mainline bring-up is a separate track with its own recovery gate.

Window B was accepted for its measured version. `B5` is the latest recorded
restoration baseline. Python remains in the image alongside the Rust workspace
for parity comparisons and recovery.

## 1. Source baseline & host verification

- Maintain `main` with clean public CI on GitHub-hosted Linux (`ubuntu-latest`)
  covering Python lint (`ruff`) and host tests, boot-install and initramfs
  tests, Rust formatting, Clippy, host tests, and `aarch64-unknown-linux-musl`
  phone binary builds.
- Maintain the offline firmware backup verifier (`i14-verify-backup` in
  `rust/crates/i14-tools`) covering SHA-256 manifests, primary/backup GPT
  headers and entry arrays, and all 23 partition lengths. File integrity checks
  and physical restore remain separate acceptance criteria.

## 2. Stock-kernel device acceptance items

Before any device write or hardware test, verify device identity, target
partition, image hash, source revision and rollback status. Never read
`current_cmd` during charging tests (reading it disables charging). Keep
firmware dumps, images, credentials and raw captures outside Git.

### Unattended collector & 48-hour Soak C (H2/H3)

- Commission a phone-local collector and export path with bounded retention and
  clean process teardown before starting a 48-hour acceptance window.
- Freeze exact boot/rootfs identity, runtime hashes, sampler and probe settings,
  and audit conditions 0–5 (`scripts/i14-window-audit.py`), then run 48
  uninterrupted hours and record the verdict plus read-only Rust CPU/memory
  measurements (H3).

### Paired Rust vs. Python performance comparison (H1)

- Run paired Rust/Python measurements under the same image version, workload
  and method for idle CPU/RSS, startup, touch taps and terminal latency,
  recording per-pair raw measurements and endpoint limitations.

### Restore integrity and accept the new rootfs (R)

- Owner-authorized removal of an inactive failed `rootfs-trial.img` on
  18 November 2026 reclaimed userdata free space from `1,946,660,864` to
  `5,167,890,432` bytes while keeping the active `B5` image, historical
  rollback copy and trial records intact.
- Current disposition remains `KEEP_B5 / R_DEFERRED_INTEGRITY` until a fresh
  candidate image, pre-Unbound `root.key` carry proof, automatic rollback
  rehearsal (`docs/ROOTFS-SWAP-ROLLBACK.md`), and full restoration verification
  are accepted.

### Battery calibration (C13)

- Verify userdata free space, log retention budget and unattended supervision
  (`scripts/i14-calibration-launch.py`) before running a bounded two-cycle
  calibration campaign (fit on cycle 1, independent holdout on cycle 2).
  See the [C13 and production charge-band sequence](C13-READINESS.md).

### Nearly-empty recovery (D)

- Kept on hold until explicitly reopened. Bounded drain/recovery host tooling
  and unit tests remain in source for review; closure requires a full
  nearly-empty power-off, charging and automatic return to normal Linux with
  full restoration checks.

## 3. Linux 7 mainline recovery (M0)

- The historical crash-to-production-return test took 70.6 s against the
  unchanged 40 s limit. Use `scripts/i14-k70-recovery-timing.py`
  ([docs/K70-RECOVERY-TIMING.md](K70-RECOVERY-TIMING.md)) to instrument
  host-monotonic and boot-uptime stages before running fresh RAM-only crash and
  keeper-stop recovery trials.
- Further mainline kernel work on power, storage, connectivity, userspace and
  Docker feasibility follows verified recovery and kernel support.

## Tracking and closure

Update [STATUS.md](STATUS.md) only after an open acceptance task has actually
been measured on hardware. Record the date, version, method, observed output
and limitations; never treat host tests or CI builds as device acceptance.
