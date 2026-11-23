# I14 Rust workspace

Native replacements for the I14 phone's own software. The
[roadmap](../docs/ROADMAP.md) tracks
image integration and device acceptance.

| Crate | What |
| --- | --- |
| `i14-sys` | the only place with `unsafe`: framebuffer, evdev, pty, getrandom, uname |
| `i14-probe` | read-only check of Rust's runtime and those interfaces on the phone |
| `i14-config` | the shared settings file (`ui.json`, PIN) and the charge guard's status file |
| `i14-ui` | the interface (`i14-ui` binary) and the boot splash (`i14-splash` binary) |
| `i14-power` | the charge guard (`i14-battery-guard`, service `i14-power`) |
| `i14-cpufreq` | the screen-off CPU clock policy (`i14-cpufreq`) |
| `i14-netwatch` | the Wi-Fi watchdog (`i14-netwatch`) |
| `i14-soak` | the minute soak sampler (`i14-soak-sample`, run from cron on userdata) |
| `i14-util` | small phone tools: `i14-reboot-bootloader` (reboot into fastboot) |
| `i14-charge` | prepared, not in the boot image: `i14-charge-wait`, the power-off charging loop of the initramfs |
| `i14-tools` | host tools for boot images (`i14-build-boot`), firmware backups (`i14-verify-backup`) and Android properties (`i14-make-android-props`) |
| `i14-fastboot` | host tools for the Mac: `i14-fastboot`, `i14-install-boot` (= `scripts/fastboot_usb.py`, `install_boot_usb.py`); USB via `nusb` |

The rootfs builder on `main` still installs the existing services. The plan for
Rust uses `/usr/libexec/i14-rs/` and an `i14_impl` setting per migrated service.
`sh rust/stage-rootfs.sh` prepares the binaries and `SHA256SUMS` in the ignored
`rootfs/rust-bin/` directory for that future integration. Compare binary hashes
using the same package set: building one package alone can select different Cargo
features, such as `serde_json`'s `float_roundtrip` from `i14-power`.

The pixel parity tests compare with the Python sources in `rootfs/overlay` and need
`python3` and `I14_FONTS` set to a directory with the phone's Terminus fonts
(`ter-118n`, `ter-120n`, `ter-124b`, `ter-132b` `.psf.gz` from `/usr/share/consolefonts`);
without it they print "skipped".

## Build

The toolchain is pinned in `rust-toolchain.toml` and `mise.toml` (Rust 1.98.1).
The phone binary is static musl, linked by the toolchain's own `rust-lld`, so no
C cross-compiler is needed on the Mac.

```sh
cd rust
mise exec -- cargo test                     # host tests (policy code, constants)
mise exec -- cargo clippy --workspace --all-targets
# i14-tools and i14-fastboot are Mac tools: leave them out for the phone target
mise exec -- cargo clippy --workspace --exclude i14-tools --exclude i14-fastboot --all-targets --target aarch64-unknown-linux-musl
mise exec -- cargo build --release --workspace --exclude i14-tools --exclude i14-fastboot --target aarch64-unknown-linux-musl
shasum -a 256 target/aarch64-unknown-linux-musl/release/i14-probe
```

`Cargo.lock` pins dependency versions. Two clean builds of `i14-probe` on the
same Mac gave the same hash.

## Write the Android property area

From `rust/`, run the host writer with an output path. It creates parent
directories and writes the 128 KiB bionic property area used by the Wi-Fi tools.

```sh
mise exec -- cargo run --locked -p i14-tools --bin i14-make-android-props -- ./android-properties
```

## Verify a firmware backup offline

The checker streams SHA-256 over the phase-0 and userdata manifests, checks
both GPT headers and entry arrays, and compares the 23 partition lengths with
the captured geometry. The I14 vendor disk GUID is zero in both copies. The
copies still have to match, and each partition must have a distinct nonzero
GUID.

From `rust/`, build the host checker and point it at one backup set. Use the
offset and disk size from the capture record for that set.

```sh
mise exec -- cargo build --release --locked -p i14-tools --bin i14-verify-backup
target/release/i14-verify-backup \
  --phase0-manifest "$phase0/SHA256SUMS" \
  --userdata-manifest "$userdata/SHA256SUMS" \
  --primary "$phase0/primary-userarea.bin" \
  --tail "$phase0/tail-userarea.bin" \
  --tail-offset "$tail_offset" --disk-size "$disk_size"
```

Exit 0 indicates successful host integrity checks, exit 1 indicates an audit
failure and exit 2 indicates a usage error. Run it against a quiescent copy on
the host. It requires no connection to the phone. Physical restore, RPMB and OTP
remain outside the check.

## Running the probe on the phone

```sh
cat target/aarch64-unknown-linux-musl/release/i14-probe |
  ssh root@172.16.42.1 'cat > /tmp/i14-probe && chmod 755 /tmp/i14-probe && /tmp/i14-probe; rc=$?; rm -f /tmp/i14-probe; exit $rc'
```

It prints one `PASS`/`FAIL` line per check and exits 1 if any failed; the SSH
command keeps that exit status (also when upload or chmod failed). Pass
check names (`files timers random sockets threads pty input framebuffer`) to
run only those. It writes only a temporary directory under `/tmp`.
