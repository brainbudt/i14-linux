# Current status — 23 November 2026

The latest device check covered the v10c/B5 installation on 23 November. The
[2–5 November v9 runtime check](verification/2026-11-05-runtime.txt) remains
the baseline for features outside that check. Window B passed for its measured
version. Soak C collector commissioning passed on B5. The uninterrupted
48-hour window and post-soak H3 measurements remain open.

## Current installation (latest recorded device state)

- Alpine **3.24.3**, aarch64, OpenRC, original **3.18.19** kernel.
- Normal **v10c** boot image with `/i14init`, storage checks, charging mode and
  USB rescue mode (confirmed in the 13 November read-only device capture; the
  earlier **v9** boot image and rootfs v7 belong to the historical 2–5 November
  baseline).
- Linux in an active **B5** 3 GiB ext4 rootfs image on userdata. The boot image
  still contains the original ramdisk, but Android's init is not started.
- Android system partition and application data removed. The kernel and required MediaTek Wi-Fi binaries are retained.
- On 18 November, owner-authorized removal of an inactive failed trial image
  confirmed the active B5 rootfs and reclaimed userdata space to `5,167,890,432`
  bytes (~5.17 GB) available while keeping the active image and historical
  rollback copy intact.
- On 19 November, a scoped UI maintenance update was closed on B5 without a new
  full-image hash; the prior full B5 content hash is therefore historical and
  does not describe every currently installed UI file.

### 23 November B5 verification

- **Installed binaries.** All six programs in `/usr/libexec/i14-rs/`
  (`i14-battery-guard`, `i14-cpufreq`, `i14-netwatch`, `i14-reboot-bootloader`,
  `i14-splash`, `i14-ui`) and `/mnt/data/media/0/i14-linux/soak/i14-soak-sample-rs`
  matched the ARM64 musl release SHA-256 hashes. The build used `9d3d781`,
  whose Rust sources match the `main` merge `fb00a71`. Three binaries needed
  replacement. The `.pre-20261123` rollback copies of `i14-cpufreq`, `i14-splash`
  and `i14-ui` matched their pre-deployment hashes.
- **Services and DNS.** All 12 default OpenRC services reported `[ started ]`.
  All four supervised Rust daemons were running the verified binaries.
  `i14-probe` passed 8/8 checks on hardware. Local Unbound answered
  `i14-health.invalid` with `192.0.2.1`. The installed `unbound-1.25.2-r2`
  lacks the fixes for CVE-2026-81634 and CVE-2026-96405. Keep
  `dns-clients.conf` empty for acceptance until the candidate manifest
  confirms both fixes. [Package details](DEVELOPMENT.md).
- **Battery gauge and home-screen ETA.** PR #2 merged on 22 November and was
  deployed on B5. Both 23 November snapshots reported `gauge_mode: "hw_car"`.
  The hardware coulomb counter tracked remaining charge (`q_rem_mah`) against
  an estimated full capacity (`q_full_mah`). Both reported `anchored: false`,
  so the UI keeps the estimate marker (`~N%`) until the gauge records an
  anchor, then displays `N%`. Bounded two-cycle C13 calibration remains open.
  The home screen estimates time to the charge limit or time remaining on
  discharge. Replaying the two snapshots on the host produced discharge ETAs.
  The SSH check did not observe or photograph the screen.
- **Soak C commissioning.** Root `crond` ran a bounded capture (`PASS`, exit 0).
  The original root crontab was restored byte-for-byte, with no collector
  process or lock left behind. [The phase exporter](../scripts/i14-soak-phase-export.py)
  streamed the capture with `--stream`, and [the receiver](../scripts/i14-soak-receive.py)
  verified all 18 files (9,715,485 bytes) against their hashes.

## Source and host-tooling milestones

- **Security hardening in source** (PR #11): locally activated, authenticated
  USB rescue; normal USB management restricted to SSH keys; fail-closed
  dual-stack firewall with a default-empty DNS client list; private settings
  handling; and hash checks independent of Python `assert`. Deployment and
  hardware acceptance on the phone remain open.
- **Rust workspace and offline backup verifier** (PR #16, #18): integrated Rust
  workspace with formatting, Clippy, host tests, `aarch64-unknown-linux-musl`
  phone binary builds, and offline GPT/hash backup verification.
- **Prepared acceptance and recovery tooling** (PR #13, #14, #15, #17, #19):
  H1 Rust/Python comparison harness, rootfs trial rollback restoration, saved
  Linux 7 recovery timing analysis, offline raw-data audit for acceptance
  window B (`scripts/i14-window-audit.py`), and bounded C13 battery calibration
  tools.
- **Acceptance tooling on main** (PR #3) added offline rootfs trial staging and
  rollback verification, paired H1 sample checks, the Soak C observer and
  streaming export/receiver, C13 launcher storage and deadline checks, M0
  host timing anchors, and matching fastboot installer diagnostics.
- **Additional host tools.** The [offline DNS/power correlator](../scripts/i14-soak-correlate.py)
  processed 200 recorded B5 power samples with 199/199 usable intervals in a
  smoke test using synthetic DNS answers. [Correlator guide](SOAK-CORRELATE.md).
  The Rust `i14-make-android-props`
  writer in `rust/crates/i14-tools` produced a byte-identical 128 KiB property
  area for the same input as [the Python writer](../rootfs/make-android-props.py).
- **CI and host-test hardening**: `.github/workflows/ci.yml` runs Python
  lint/tests, boot-install and initramfs tests, Rust formatting/Clippy/tests,
  and ARM64 musl builds on GitHub-hosted `ubuntu-latest`, with deterministic UI
  PIN-storage host tests.

## Features (historical 2–5 November v9 baseline)

The table below reflects the 2–5 November v9 runtime baseline and has not been
fully re-verified end to end after the v10c/B5 updates. The later B5 checks are recorded above.

| Component | Working | Notes |
| --- | --- | --- |
| Normal startup and reboot | Yes | Installation and several actual reboots recorded; follow-up check confirms Alpine/3.18 |
| Storage and package management | Yes | Writable rootfs and `apk` used; follow-up check sees the mounted rootfs |
| Display and touch | Yes | Framebuffer interface; virtual input tested and a charge-limit change also demonstrated with a real finger |
| On-screen keyboard and terminal | Yes | Shell commands and a text editor exercised; pty, keys and scrollback covered by host tests |
| Wi-Fi | Yes, with limitations | Connection, scanning, saved networks, off/on and DHCP recovery implemented; not every router, security mode or roaming transition physically tested |
| SSH | Yes | Follow-up check used the existing key-authenticated connection; no password login |
| USB networking and serial console | Yes | Demonstrated in earlier device tests; the USB console provides direct root access |
| USB rescue mode | Yes | Forced rescue mode, ordinary shell commands and return to Linux tested |
| DNS / Unbound | Yes | UDP/TCP port 53 tested from another LAN host on 4 November, including DNSSEC and DNS-over-TLS. Follow-up check confirms the local health name; no new external LAN/DNSSEC test |
| Firewall, time synchronization and service supervision | Yes, within tested scope | Private-network access, chrony, OpenRC supervision and netwatch configured and tested; no complete fault or security audit |
| Settings and PIN | Yes | Brightness, screen timeout, persistent settings and PIN flow exercised; this is not disk encryption or protection of the USB root console |
| Bluetooth | Partially | Radio and classic/BLE scanning tested; pairing, audio and file transfer are not implemented |
| Charging | Yes, on 3.18 | Demonstrated in Linux and in the custom powered-off charging mode |
| Charge limit | Partially | BCCT stop/resume actuator, guard crash, clean stop and actual reboot tested. A full unchanged 80% charge-band cycle and hysteresis after a crash inside the band remain open |
| Battery gauge | Voltage estimate on v9 | The v9 display used an open-circuit voltage estimate. B5 now runs the HW-CAR gauge described above. |
| CPU control | Yes | Downclocking to 299 MHz at idle with screen off, higher clocks under activity. A/B/A testing did not establish energy savings |
| Automatic startup after charging | Partially | Return from custom charging mode tested with substantial charge remaining. An actual nearly-empty → charging → unattended Linux cycle was skipped at the owner's request and remains unproven |
| Audio | Not verified as a user feature | DMA playback test had no underruns; audible output, mixer routing and Bluetooth audio have not been demonstrated |
| Camera, GPS, calls, SMS and mobile data | Not verified / incomplete | No working end-to-end user feature demonstrated |
| Modern 7.0.10 kernel | Partially; on hold | Native boot and individual hardware steps demonstrated earlier. Not a complete replacement with reliable charging for the current installation |
| Docker | No | Current stock kernel lacks required namespace/cgroup features and a usable module loader |
| Multi-day unattended use | Not verified | Logging and DNS monitoring exist; maintenance and software changes interrupt measurements. No general availability claim |

## Priorities

1. Deploy and accept the hardened stock boot image and rootfs together (authenticated USB rescue, fail-closed firewall, settings directory `0700` and files `0600`). Keep the DNS client list empty until the candidate build manifest confirms the applicable Unbound fixes (see [DEVELOPMENT.md](DEVELOPMENT.md)).
2. Complete paired H1 Rust/Python performance measurements and resolve the new rootfs candidate (R), currently in `KEEP_B5 / R_DEFERRED_INTEGRITY`, with carry and automatic rollback verification.
3. Run the uninterrupted 48-hour Soak C window on one frozen version, then collect the read-only H3 Rust measurements.
4. Run a full natural 80% charge-band cycle, bounded two-cycle C13 battery calibration (rechecking free space first, as a 3 GiB R staging image can drop userdata below the 2 GiB floor again), and crash-in-band hysteresis checks.
5. Keep nearly-empty → charging → unattended Linux recovery (D) on hold until explicit new owner direction reopens it.
6. Linux 7 mainline recovery (M0) remains a separate track. The recorded return took 70.6 s against a 40 s limit.

See [the roadmap](ROADMAP.md) for the full entry gates and closure criteria.

## Verification scope

Original installation, interface, DNS, CPU and battery reports are retained locally.
Only reviewed summaries and a limited runtime check are published. Raw logs may
contain SSIDs, MAC addresses, device identifiers or other personal data and are excluded.

Host tests check software logic. The initial publication results are recorded in
[the verification summary](verification/publication-checks.json).
The subsequent English documentation and privacy update is recorded in
[the update checks](verification/english-privacy-checks.json).
Host tests do not replace physical radio, battery, flashing or recovery tests.
