#!/bin/sh
# Build a static aarch64 e2fsck (musl) for /i14init with the pinned zig, as the other phone tools.
# Source: e2fsprogs 1.47.3 from kernel.org (sha256 pinned below).
# Output: artifacts/build-tools/e2fsprogs/out/{e2fsck,SHA256SUMS}
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
zig_tools=$root/artifacts/build-tools
zig_stage=$(mktemp -d)
trap 'rm -r "$zig_stage"' EXIT
python3 "$root/scripts/i14_build_inputs.py" "$zig_tools/zig-aarch64-macos-0.15.2.tar.xz" \
    3cc2bab367e185cdfb27501c4b30b1b0653c28d9f73df8dc91488e66ece5fa6b "$zig_stage"
zig=$zig_stage/zig-aarch64-macos-0.15.2/zig
dir=$root/artifacts/build-tools/e2fsprogs
tar=$dir/e2fsprogs-1.47.3.tar.xz
sha=857e6ef800feaa2bb4578fbc810214be5d3c88b072ea53c5384733a965737329
[ -f "$tar" ] || curl -fsSL -o "$tar" \
    https://mirrors.edge.kernel.org/pub/linux/kernel/people/tytso/e2fsprogs/v1.47.3/e2fsprogs-1.47.3.tar.xz
echo "$sha  $tar" | shasum -a 256 -c -
rm -rf "$dir/src" "$dir/out"
mkdir -p "$dir/src" "$dir/out"
tar -xJf "$tar" -C "$dir/src" --strip-components 1
cd "$dir/src"
export ZIG_GLOBAL_CACHE_DIR=$root/artifacts/build-tools/zig-cache SOURCE_DATE_EPOCH=1725494400
# A few helper programs (gen_crc32ctable, subst, ...) are built and run on the Mac with its cc.
export BUILD_CC=cc BUILD_LDFLAGS= CC="$zig cc -target aarch64-linux.3.18.19-musl -mcpu=cortex_a53"
export AR="$zig ar" RANLIB="$zig ranlib" CFLAGS="-Os -ffile-prefix-map=$dir/src=SRC" LDFLAGS="-static -s"
./configure --host=aarch64-linux-musl --build="$(./config/config.guess)" \
    --disable-nls --disable-fuse2fs --disable-defrag --disable-imager --disable-debugfs \
    --disable-tdb --disable-uuidd --disable-elf-shlibs --disable-bsd-shlibs \
    --without-libarchive --without-pthread >"$dir/out/configure.log" 2>&1
make -j8 libs >"$dir/out/make-libs.log" 2>&1
make -j8 -C e2fsck e2fsck.static >"$dir/out/make-e2fsck.log" 2>&1
cp e2fsck/e2fsck.static "$dir/out/e2fsck"
cd "$dir/out" && shasum -a 256 e2fsck >SHA256SUMS && cat SHA256SUMS && file e2fsck && ls -l e2fsck
