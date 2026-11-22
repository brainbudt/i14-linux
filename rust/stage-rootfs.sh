#!/bin/sh
# Builds the phone binaries and stages them for rootfs/build-alpine-rootfs.sh in rootfs/rust-bin/
# for the separate Rust rootfs integration. Run from the repo: sh rust/stage-rootfs.sh
set -eu
cd "$(dirname "$0")"
T=aarch64-unknown-linux-musl
BINS="i14-ui i14-splash i14-battery-guard i14-cpufreq i14-netwatch i14-reboot-bootloader"
# The phone packages only (i14-tools is a host tool and links the Mac's zlib).
mise exec -- cargo build --release --locked --target "$T" -p i14-ui -p i14-power -p i14-cpufreq -p i14-netwatch -p i14-util
OUT=../rootfs/rust-bin
rm -rf "$OUT"
mkdir -p "$OUT"
for b in $BINS; do
	cp "target/$T/release/$b" "$OUT/$b"
done
(cd "$OUT" && shasum -a 256 $BINS >SHA256SUMS && cat SHA256SUMS)
