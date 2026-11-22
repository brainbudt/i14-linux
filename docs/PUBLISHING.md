# What this GitHub publication includes

Destination: **[brainbudt/i14-linux](https://github.com/brainbudt/i14-linux)**,
visibility **public**, default branch `main`.

This public repository starts from a clean source snapshot with its own initial
commit. The complete local research history is kept private and is not pushed:
it contains raw device captures, old inventories, internal build/runner logs and
personal device identifiers.

## Included

- Source from `rust/`, `rootfs/`, `initramfs/`, `kernel/`, `tools/`, `scripts/`
  and `tests/`.
- README, roadmap, security policy, and reviewed status, hardware, history and
  development documentation.
- Two unmodified native device framebuffer captures (`assets/screenshots/boot.png`
  and `assets/screenshots/settings.png`) and the project icon (`assets/icon.svg`).
- Historical verification records (`docs/verification/`).

Machine-specific local SSH/soak entry points and third-party marketplace listing
screenshots are excluded. Historical scripts that expect local firmware or
evidence remain identifiable as source, but their private inputs are not
supplied. Personal backup paths, host references, MAC addresses and LAN IP
addresses are replaced with neutral or RFC 5737 examples; DNS monitoring
requires explicit target configuration.

## Retained locally

Firmware and partition dumps, APKs, NVRAM/NVDATA, rootfs/boot images, personal
identifiers, Wi-Fi configuration, Bluetooth link keys (`bt-keys`), PIN settings,
SSH keys, backups, raw logs and internal automation notes. Published images are
limited to custom UI assets and reviewed device screenshots.

Files are selected explicitly; `.gitignore` alone cannot exclude previously
tracked files or old commits. Publication checks cover secrets, sensitive file
types, local Markdown links, screenshot hashes and Git history cleanliness.
Synthetic test networks and RFC 5737 documentation IP ranges are examples, not a
record of the owner's local networks.

## Licensing

Original userspace code, Rust crates, scripts, tools, initramfs scripts and
documentation are distributed under the [MIT License](../LICENSE).
Kernel modules, headers and upstream kernel patches under `kernel/` are
`GPL-2.0-only`, and device tree sources (`kernel/*.dts`) are `BSD-3-Clause`.
Existing third-party licenses still apply to external components.

[Historical English documentation and privacy verification](verification/english-privacy-checks.json).
