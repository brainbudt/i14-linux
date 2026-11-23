# Source code and building

This repository contains a reviewed source snapshot. There is no ready-made ROM
or installation image yet, and this repository does not claim a fully
reproducible end-to-end device build from a fresh clone alone.

## Host tests and CI

The [Code checks workflow](../.github/workflows/ci.yml) runs on GitHub-hosted
Linux (`ubuntu-latest`) for pushes, pull requests and manual runs. Require the
**Code validation** status on the exact candidate revision before treating CI as
accepted. Validation covers Python linting (`ruff`), Python host tests,
boot-install and initramfs tests, and — because `rust/Cargo.toml` is present —
Rust formatting, Clippy, host tests and the `aarch64-unknown-linux-musl` build.

The Python touch interface uses only the Python standard library. Host tests run
without a phone, root privileges or network access to the device:

```sh
ruff check .
python3 -B -m unittest discover -s tests -p 'test_i14_*.py' -v
python3 -B -m unittest discover -s tests -p 'test_boot_install.py' -v
python3 -B initramfs/test-usb-network.py
python3 -B initramfs/test-display-packaging.py
```

For the Rust workspace (pinned to Rust `1.98.1` in `rust/rust-toolchain.toml`
and `rust/mise.toml`; see [rust/README.md](../rust/README.md)):

```sh
cd rust
cargo fmt --all -- --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo build --release --workspace --exclude i14-tools --exclude i14-fastboot \
  --target aarch64-unknown-linux-musl --locked
```

Use an existing project virtual environment if one is configured. Some older
experimental tests and C host tests under `tests/` depend on local firmware,
captures, or optional C/host tooling and are not part of the default CI matrix.

The M0 analyzer reads normalized JSONL traces. The passive host capture writer
and analyzer are tested together using synthetic observations. Capture hooks,
the trace schema and the hardware gates are documented in
[K70 recovery timing](K70-RECOVERY-TIMING.md).
See [ROADMAP.md](ROADMAP.md) for remaining device acceptance gates.

## Current route: Alpine with the stock kernel

`rootfs/build-alpine-rootfs.sh` builds an Alpine aarch64 rootfs inside an
`alpine:3.24` container. The file tree is assembled inside the Linux container
to preserve case sensitivity and Unix ownership.

Required inputs from your own setup:

- `authorized_keys`, containing only the SSH public keys that should have access;
- the device's **own** original `system.img`, mounted read-only, for the Wi-Fi
  files listed in `rootfs/android-wifi.sha256`;
- `I14_SOURCE_COMMIT`, the full commit from the clean source checkout passed
  to the build container;
- optionally `dns-clients.conf` in the rootfs build directory, with one IPv4
  CIDR per line. Blank lines and text following `#` are ignored. An absent or
  empty file allows local health queries only. The builder renders the same list
  for the Wi-Fi firewall and Unbound. Keep the local list out of Git;
- a separate output directory for the generated rootfs image.

These inputs are not supplied. The builder recreates its build tree and output
directory. Run it inside the intended temporary container. It emits a private
`out/build-manifest.json` with the source commit, input and output hashes, and
installed package versions. Compare those versions with [Alpine SecDB](https://secdb.alpinelinux.org/v3.24/main.json)
and upstream advisories before a candidate is accepted. Keep unredacted
manifests with the local build inputs.
`rootfs/overlay/` contains the OpenRC services and `i14ui` module.

The Alpine 3.24 AArch64 package `unbound-1.25.2-r2` includes the fix for
CVE-2026-50046, which applies to the configured DNS-over-TLS forwarders,
and Alpine's backports for CVE-2026-81642 and CVE-2026-82717. It still
lacks the fixes for CVE-2026-81634 (DNSSEC validation) and CVE-2026-96405
(processing upstream DNS replies). Keep the DNS client list empty for
device acceptance until the package manifest shows a build containing the
applicable fixes. Recheck the exact candidate against the [Alpine packaging recipe](https://github.com/alpinelinux/aports/blob/92921ccb60bf1bea0d4666b2c0533ce22c5fae9e/main/unbound/APKBUILD)
and [upstream advisories](https://nlnetlabs.nl/projects/unbound/security-advisories/).

`scripts/build-i14-boot.py` and `initramfs/` implement the current boot route.
They also require the correct original boot components, a static BusyBox,
a suitable e2fsck, the recorded I14 layout and a private owner-supplied
`--rescue-password-hash-file` containing a SHA-512 crypt hash for an independent,
strong recovery password. Keep the hash file and resulting boot image in
private local storage. A boot image for another
MT6735 device is not a compatible substitute.

## Experimental mainline route

`kernel/` contains device trees, patches and driver/observation work based on
[MT6735 Mainline](https://gitlab.com/mt6735-mainline/linux).
`scripts/build-kernel.sh` is the kernel builder. The complete external kernel
tree and toolchain are not included. This route has different limitations
from the current 3.18 installation, particularly around power and charging.
The default initramfs omits active USB diagnostics and console shells.
`scripts/build-busybox.sh OUTPUT --debug` creates a separate debug image
(`I14_DEBUG_INITRAMFS=1`) that enables unauthenticated USB diagnostics (serial
root shell, Telnet and HTTP). Keep it disabled for normal operation and use it
only with a trusted cable and host during isolated bring-up.

The supported-kernel migration depends on the existing M0 acceptance gates.
The stock filesystem check confirms ext4 consistency. The stock build does
not establish a trusted boot chain. Before implementing verified rootfs,
establish that the bootloader can authenticate the kernel, ramdisk and root
hash without removing the known-good recovery path. A supported kernel with
[dm-verity](https://docs.kernel.org/admin-guide/device-mapper/verity.html) can
verify an immutable rootfs against that independently authenticated root
hash. Keep writable settings on their own protected volume and apply the
same trust policy to the rollback image. Offline verification must reject a
modified rootfs and an untrusted root hash, and accept the signed recovery
image independently of an intact production rootfs.

## Device operations

Installation, recovery and diagnostic utilities are specific to this phone and
include historical experiments. Some refer to locally retained evidence or
packages. They are not generic installation instructions ready to run unchanged.

Before any write, establish the device identity, target partition, image hash,
source and recovery status. Original dumps, NVRAM/NVDATA and private keys stay
outside Git. In the DNS image root SSH requires a key and listens on
`172.16.42.1` over the USB management link. The normal console and touchscreen
root terminal are disabled. Following a rescue boot, the first power-key press
starts the authenticated serial login and the next press requests a restart.
Keep the existing boot and rootfs as separately verified rollback targets
until the new rescue session, charging and management route have been proven.

## Local configuration

The published DNS soak script requires an explicit `I14_DNS` environment variable
with the address of the intended phone. No personal LAN address is provided as
a default. Historical backup-volume paths are anonymized as `/Volumes/I14_BACKUP`;
configure these paths for your own environment before using those tools,
including the logo tool's protected backup volume. Helper scripts that invoke
SSH over the USB link accept `I14_SSH` (defaulting to `ssh root@172.16.42.1`).

## Updating documentation

Update [STATUS](STATUS.md) when an open acceptance task has actually been tested.
Record the date, software version, method and limitation. Preserve old raw
evidence unchanged and add a new snapshot. Do not turn a successful host test
into a hardware or long-term stability claim.
