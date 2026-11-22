# Current status — 22 November 2026

This overview covers the Topiky/I14 device examined by this project. It separates
the historical 2–5 November v9 runtime baseline ([runtime check](verification/2026-11-05-runtime.txt)),
narrower later device measurements (13 November v10c capture, 18 November
userdata cleanup, and 19 November UI maintenance), and source/host-tooling
milestones integrated on `main` through 22 November 2026.
Window B was accepted for its measured version; the 48-hour Soak C test remains
open. Source merges, host tests, and CI builds do not establish that a change
has been deployed to or accepted on the phone.

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

## Source and host-tooling milestones on `main` (not yet device-accepted)

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
- **CI and host-test hardening**: `.github/workflows/ci.yml` runs Python
  lint/tests, boot-install and initramfs tests, Rust formatting/Clippy/tests,
  and ARM64 musl builds on GitHub-hosted `ubuntu-latest`, with deterministic UI
  PIN-storage host tests.

## Features (historical 2–5 November v9 baseline)

The table below reflects the 2–5 November v9 runtime baseline and has not been
fully re-verified end to end after the v10c/B5 updates. Window B was later
accepted for its measured version; the 48-hour Soak C test remains open.

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
| Battery gauge | Estimate only | `~N%` uses estimated open-circuit voltage and a generic stock profile. No calibrated capacity or exact state of charge; the earlier raw 100% reading was unreliable |
| CPU control | Yes | Downclocking to 299 MHz at idle with screen off, higher clocks under activity. A/B/A testing did not establish energy savings |
| Automatic startup after charging | Partially | Return from custom charging mode tested with substantial charge remaining. An actual nearly-empty → charging → unattended Linux cycle was skipped at the owner's request and remains unproven |
| Audio | Not verified as a user feature | DMA playback test had no underruns; audible output, mixer routing and Bluetooth audio have not been demonstrated |
| Camera, GPS, calls, SMS and mobile data | Not verified / incomplete | No working end-to-end user feature demonstrated |
| Modern 7.0.10 kernel | Partially; on hold | Native boot and individual hardware steps demonstrated earlier. Not a complete replacement with reliable charging for the current installation |
| Docker | No | Current stock kernel lacks required namespace/cgroup features and a usable module loader |
| Multi-day unattended use | Not verified | Logging and DNS monitoring exist; maintenance and software changes interrupt measurements. No general availability claim |

## Priorities

1. Verify device baseline and deploy/accept the security-hardened stock-kernel image; keep the DNS client list empty until the candidate build manifest confirms the applicable Unbound fixes (see [DEVELOPMENT.md](DEVELOPMENT.md)).
2. Complete paired H1 Rust/Python performance measurements and resolve the new rootfs candidate (R), currently in `KEEP_B5 / R_DEFERRED_INTEGRITY`, with carry and automatic rollback verification.
3. Commission unattended collection and run the uninterrupted 48-hour Soak C test (H2/H3) on one frozen software version (Window B was accepted for its measured version; Soak C remains open).
4. Run a full natural charge-band cycle, bounded C13 battery calibration (rechecking free space first, as a 3 GiB R staging image can drop userdata below the 2 GiB floor again), and crash-in-band hysteresis checks.
5. Keep nearly-empty → charging → unattended Linux recovery (D) on hold until explicit new owner direction reopens it.
6. Continue Linux 7 mainline recovery (M0) and charging work as a separate track; do not attribute 3.18 results to it.

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
