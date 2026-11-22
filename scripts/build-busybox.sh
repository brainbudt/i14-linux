#!/bin/sh
# macOS ARM64 host; builds only local, ignored artifacts. No device access.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
out=${1:-"$root/builds/busybox-$(date -u +%Y%m%dT%H%M%SZ)"}
case "$#:${2:-}" in 0:|1:|2:--debug) ;; *) echo "Usage: $0 [output [--debug]]" >&2; exit 2 ;; esac
case "$out" in /*) ;; *) out="$root/$out" ;; esac
test ! -e "$out" || { echo "Refusing existing output: $out" >&2; exit 1; }
mkdir -p "$out"
export SOURCE_DATE_EPOCH=1725494400 TZ=UTC LC_ALL=C
tools="$root/artifacts/build-tools"
export ZIG_GLOBAL_CACHE_DIR="$tools/zig-cache"
if [ -n "${LLVM_BIN:-}" ]; then
    llvm="$LLVM_BIN"
elif command -v llvm-strip >/dev/null 2>&1; then
    llvm=$(dirname -- "$(command -v llvm-strip)")
else
    echo "Missing llvm-strip; set LLVM_BIN to your LLVM bin directory." >&2
    exit 1
fi
test -x "$llvm/llvm-strip"
python3 - "$tools" "$out" "$root/scripts" <<'PY'
import pathlib, sys, urllib.request
sys.path.insert(0, sys.argv[3])
from i14_build_inputs import verified_archive
tools, out = map(pathlib.Path, sys.argv[1:3]); tools.mkdir(parents=True, exist_ok=True)
sources = [
 ("https://busybox.net/downloads/busybox-1.37.0.tar.bz2", "3311dff32e746499f4df0d5df04d7eb396382d7e108bb9250e7b519b837043a4"),
 ("https://ziglang.org/download/0.15.2/zig-aarch64-macos-0.15.2.tar.xz", "3cc2bab367e185cdfb27501c4b30b1b0653c28d9f73df8dc91488e66ece5fa6b"),
]
for url, sha in sources:
    archive = tools / url.rsplit('/', 1)[1]
    if not archive.exists():
        urllib.request.urlretrieve(url, archive)
    # Fresh extraction binds compiler and libraries to the verified archive too.
    with verified_archive(archive, sha) as source:
        source.extractall(out, filter='data')
source = out / 'busybox-1.37.0/scripts/trylink'
text = source.read_text()
old = 'echo "-Wl,--warn-common -Wl,-Map,$EXE.map -Wl,--verbose"'
assert text.count(old) == 1
source.write_text(text.replace(old, ': # Zig does not accept GNU ld informational flags.'))
PY
src="$out/busybox-1.37.0"
make -C "$src" allnoconfig >"$out/config.log" 2>&1
python3 - "$src/.config" "$root/initramfs/busybox.config" <<'PY'
import pathlib, re, sys
config, seed = map(pathlib.Path, sys.argv[1:]); text = config.read_text()
for setting in seed.read_text().splitlines():
    key, value = setting.split('=', 1)
    assert re.fullmatch(r'CONFIG_[A-Z0-9_]+', key), key
    pattern = rf'^(?:{re.escape(key)}=.*|# {re.escape(key)} is not set)$'
    text, count = re.subn(pattern, lambda match: setting, text, flags=re.M)
    assert count == 1, f'unknown or duplicate config key: {key}'
config.write_text(text)
PY
yes '' | make -C "$src" oldconfig >>"$out/config.log" 2>&1
python3 - "$src/.config" "$root/initramfs/busybox.config" <<'PY'
import pathlib, sys
actual, seed = map(pathlib.Path, sys.argv[1:])
settings = set(actual.read_text().splitlines())
for setting in seed.read_text().splitlines():
    assert setting in settings, f'Kconfig did not retain {setting}'
PY
make -C "$src" -j4 ARCH=arm64 \
    CC="$out/zig-aarch64-macos-0.15.2/zig cc -target aarch64-linux.3.18.19-musl -mcpu=cortex_a53" \
    AR="$llvm/llvm-ar" NM="$llvm/llvm-nm" STRIP="$llvm/llvm-strip" \
    busybox busybox.links >"$out/build.log" 2>&1
cp "$src/busybox" "$out/busybox"
cp "$src/busybox_unstripped" "$out/busybox_unstripped"
cp "$src/.config" "$out/busybox.config"
cp "$src/busybox.links" "$out/busybox.links"
cp "$root/initramfs/busybox.config" "$out/busybox.seed.config"
cp "$root/scripts/build-busybox.sh" "$out/build-busybox.sh"
"$out/zig-aarch64-macos-0.15.2/zig" cc --version >"$out/compiler-version.txt"
shasum -a 256 "$out/busybox" "$out/busybox_unstripped"
python3 "$root/initramfs/make-cpio.py" "$out" "$root/initramfs/init" ${2:+"$2"}
sh -c 'cd "$1" && shasum -a 256 busybox busybox_unstripped busybox.config busybox.seed.config busybox.links build-busybox.sh compiler-version.txt initramfs.cpio.gz > SHA256SUMS' sh "$out"
cat "$out/SHA256SUMS"
printf 'Build output: %s\n' "$out"
