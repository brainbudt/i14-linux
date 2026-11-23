<p align="center"><img src="assets/icon.svg" width="80" height="80" alt="I14 Linux"></p>
<h1 align="center">I14 Linux</h1>
<p align="center">Turning an inexpensive Topiky phone into a standalone Linux computer.</p>
<p align="center"><a href="docs/STATUS.md">What works?</a> · <a href="docs/HARDWARE.md">The phone</a> · <a href="docs/HISTORY.md">History</a> · <a href="docs/DEVELOPMENT.md">Building</a> · <a href="docs/ROADMAP.md">Roadmap</a></p>

I14 Linux runs Alpine directly on a phone with a MediaTek MT6735.
A custom touch interface, terminal, Wi-Fi settings and charging controller turn
it into a small Linux computer. One practical use is a backup DNS server running Unbound.

The device was purchased as a **Topiky**, model **Topikyg3im86nzcv-13**, through
[Amazon.nl, ASIN B0CJ64KTBN](https://www.amazon.nl/dp/B0CJ64KTBN).
Its original firmware identifies it as **I14 Pro Max**. This project targets
that specific device and motherboard (`magc6735_65u_1_m0`, firmware branch `P45`).

> [!IMPORTANT]
> Experimental device port. The current installation runs **Alpine 3.24.3 on the
> original MediaTek 3.18.19 kernel**. Android is no longer started.
> The modern **7.0.10 mainline port** is a separate experiment, currently on hold.
> There is no universal installation image for other devices sold as “I14”.

> [!WARNING]
> **Hardware, data-loss and battery risk:** Flashing boot, recovery or partition
> images, modifying preloader/seccfg state, or experimenting with charging/PMIC
> controls can permanently brick a phone, erase NVRAM/IMEI calibration data, or
> cause unsafe battery behavior. Make and verify your own full backup first.
> Everything in this repository is provided **AS IS**, without warranty of any
> kind; you are solely responsible for any damage to your hardware.

## On the screen

<p align="center">
  <img src="assets/screenshots/boot.png" width="260" alt="Actual framebuffer capture of the Linux boot screen">
  &nbsp;&nbsp;
  <img src="assets/screenshots/settings.png" width="260" alt="Settings for brightness, screen timeout, PIN and estimated charge limit">
</p>

These earlier framebuffer captures show the boot screen and charge-limit settings.
The `~80%` label in the screenshot comes from the voltage-based interface
that preceded the HW-CAR fuel gauge. The original interface is in Dutch.
[Screenshot provenance and limitations](docs/SCREENSHOTS.md).

## Why this project?

The listing advertised Android 13, eight CPU cores and 8 GB of RAM.
The device examined had Android 6, four available CPU cores and roughly
2 GB of RAM; its eMMC is about 16 GB. The listing itself gives conflicting
storage capacities. Measurements are therefore documented separately from sales claims.
[The listing versus the device](docs/HARDWARE.md).

The project began with inventory and cleanup. Backups, native Linux, custom boot
and rescue paths, network services and local touchscreen controls followed.
The original software included an unclear updater, an `AutoDialer`, and factory
and logging tools. Their presence is documented; that alone does not establish
malicious behavior. [A short history](docs/HISTORY.md).

## What works now?

On **23 November 2026**, all six Rust programs in B5's `/usr/libexec/i14-rs/`
and its soak sampler matched the ARM64 musl release hashes. All 12 default
OpenRC services and four supervised Rust daemons were running. The hardware
probe passed 8/8 checks. Earlier tests still supply the evidence for several
features below. [Dated checks and limitations](docs/STATUS.md).

| Component | Working | Notes |
| --- | --- | --- |
| Native Alpine, storage and reboot | Yes | Linux rootfs (`B5`) on eMMC with `v10c` boot image, OpenRC and `apk` |
| Display and touch controls | Yes | Custom framebuffer interface and on-screen keyboard |
| On-device terminal | Historical / disabled in hardened DNS image | Exercised in the 2–5 Nov v9 baseline; disabled in the hardened DNS image on `main` (see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)) |
| Wi-Fi and SSH | Yes | On-screen network selection and SSH key authentication |
| USB management and rescue mode | Yes | USB networking and rescue mode; hardened source restricts normal USB management to SSH keys and uses locally activated, authenticated serial rescue |
| Backup DNS | Local health check passed | B5 answered `i14-health.invalid` with `192.0.2.1`. Keep the candidate DNS client list empty until the installed Unbound security gaps are fixed. [Security policy](SECURITY.md). |
| Brightness, screen timeout, PIN and power menu | Yes | Implemented and exercised on the device |
| Charging and configurable charge limit | Partially | Charging works; BCCT pause/resume tested, full calibration/charge-band acceptance still pending |
| Battery gauge and home-screen ETA | Deployed on B5 | The HW-CAR coulomb counter tracks remaining charge. The UI estimates time to the charge limit or time left on discharge. |
| CPU control with the screen off | Yes | Downclocking works; energy savings have not been established |
| Rust userspace & host tools | Phone binaries verified on B5 | The UI, charge guard, CPU controller and netwatch ran under OpenRC supervision. Paired Rust/Python performance measurements remain open. |
| Bluetooth | Partially | Radio and scanning work; pairing and device use are not implemented |
| Audio, camera, GPS, calls and mobile data | Not verified | No complete, accepted Linux support |
| Modern mainline kernel | Partially | Earlier native boot demonstrated; not a complete replacement for 3.18 |
| Docker | No | Not supported by the current kernel |
| Multi-day reliability / recovery from an empty battery | Partially | Window B passed for its measured version. Soak C collector commissioning passed on 23 November. The uninterrupted 48-hour window and post-soak H3 measurements remain open. Nearly-empty recovery is on owner hold. |

On 23 November the gauge reported `gauge_mode: "hw_car"`, remaining charge
(`q_rem_mah`) and estimated full capacity (`q_full_mah`). Both snapshots
reported `anchored: false`. The UI shows `~N%` until the gauge records an
anchor, then `N%`. Replaying those snapshots on the host produced discharge
ETAs. The SSH check did not observe the screen. Two-cycle C13 calibration
remains open.

## How does it work?

The existing bootloader loads a boot image containing the original kernel and
our `/i14init` startup program. It checks storage and starts Alpine from an
ext4 image on userdata. OpenRC manages network services, the touch interface,
CPU control and the charging guard. A custom USB rescue mode handles startup failures.

The original interface uses the Python standard library and draws directly to
`/dev/fb0`; the `rust/` workspace implements native Rust replacements and host
tools. Wi-Fi uses the original MediaTek driver and a few vendor files extracted
from the device's own firmware backup. Those binaries are not distributed here.

## Exploring or building

There is no ready-made release yet. Source code, configuration and host tests
are included; firmware, rootfs images, keys and personal device data remain local.
Building requires additional tools and files from the device itself.
[Development and building](docs/DEVELOPMENT.md).

| Directory | Contents |
| --- | --- |
| `rust/` | 12-crate Rust workspace: native phone services/UI, offline backup verifier and host fastboot/boot tools ([rust/README.md](rust/README.md)) |
| `rootfs/` | Alpine builder, OpenRC services and Python touch interface |
| `initramfs/` | Startup, charging and rescue programs |
| `kernel/` | Experimental mainline device trees, patches and driver work |
| `scripts/` | Build, diagnostic, backup, soak-audit and installation tools |
| `tools/` | Small native C utilities |
| `tests/` | Host tests for the interface, charging policy, boot images, rollback and recovery |
| `assets/` | Project icon and unmodified device framebuffer captures |
| `docs/` | Hardware, current status, roadmap, history and verification |

## Documentation

- [Current status and remaining work](docs/STATUS.md)
- [Roadmap and acceptance gates](docs/ROADMAP.md)
- [Topiky listing and observed hardware](docs/HARDWARE.md)
- [History and suspicious preinstalled software](docs/HISTORY.md)
- [Screenshots and provenance](docs/SCREENSHOTS.md)
- [Development environment and build limitations](docs/DEVELOPMENT.md)
- [Offline DNS and power soak correlator](docs/SOAK-CORRELATE.md)
- [Rootfs trial and rollback](docs/ROOTFS-SWAP-ROLLBACK.md)
- [K70 recovery-timing analyzer](docs/K70-RECOVERY-TIMING.md)
- [Security policy](SECURITY.md)
- [What this GitHub publication includes](docs/PUBLISHING.md)

## Licensing

Original userspace code, Rust crates, scripts, tools, initramfs scripts and
documentation in this repository are licensed under the [MIT License](LICENSE).

Files under `kernel/` follow their respective upstream-compatible licenses:
- Kernel modules and headers (`kernel/*.c`, `kernel/*.h`, `kernel/dtb-shim.S`): `GPL-2.0-only`
- Upstream Linux kernel patches (`kernel/patches/*.patch`): `GPL-2.0-only`
- Device tree sources (`kernel/*.dts`): `BSD-3-Clause`

The kernel work builds on [MT6735 Mainline](https://gitlab.com/mt6735-mainline).
Alpine, Linux, BusyBox, OpenRC, Unbound and other third-party components retain
their own licenses.
