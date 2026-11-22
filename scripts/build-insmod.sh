#!/bin/sh
# Build tools/i14-insmod.c (static aarch64, musl) with the pinned zig, as the other phone tools.
# Output: artifacts/phase4-e3e4-20261102/insmod/{insmod,SHA256SUMS}
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
zig_stage=$(mktemp -d)
trap 'rm -r "$zig_stage"' EXIT
python3 "$root/scripts/i14_build_inputs.py" "$root/artifacts/build-tools/zig-aarch64-macos-0.15.2.tar.xz" \
    3cc2bab367e185cdfb27501c4b30b1b0653c28d9f73df8dc91488e66ece5fa6b "$zig_stage"
zig=$zig_stage/zig-aarch64-macos-0.15.2/zig
out=$root/artifacts/phase4-e3e4-20261102/insmod
mkdir -p "$out"
ZIG_GLOBAL_CACHE_DIR=$root/artifacts/build-tools/zig-cache SOURCE_DATE_EPOCH=1725494400 \
    "$zig" cc -target aarch64-linux.3.18.19-musl -mcpu=cortex_a53 -std=c11 -static -Oz \
    -Wall -Wextra -Werror -s "-ffile-prefix-map=$root=SRC" "$root/tools/i14-insmod.c" -o "$out/insmod"
cd "$out" && shasum -a 256 insmod >SHA256SUMS && cat SHA256SUMS
