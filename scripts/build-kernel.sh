#!/usr/bin/env bash
# Requires LLVM/clang, make, Python, flex, bison and libelf/zlib.
set -euo pipefail
if (( $# < 3 || $# > 4 )); then
    echo "Usage: $0 KERNEL_SOURCE EMPTY_OUTPUT NURA_CONFIG [mediatek/board.dtb]" >&2
    exit 2
fi
src=$(realpath "$1")
out=$(realpath -m "$2")
base=$(realpath "$3")
repo=$(cd -- "$(dirname -- "$0")/.." && pwd)
revision=7600adcae221a7a3193bfb9c425dd660416bcebc
base_hash=7eca199cbc4d5cd2cd1df0fc4523e8d19e88a746de257a0966f48c3c26a916af24d24e1f3567929a500a902724a344d35ea8643bfca4f4033f80743873586b4d
[[ $(git -c safe.directory="$src" -C "$src" rev-parse HEAD) == "$revision" ]] || { echo 'Kernel revision mismatch' >&2; exit 1; }
git -c safe.directory="$src" -C "$src" diff --quiet HEAD -- || { echo 'Modified tracked kernel source' >&2; exit 1; }
board=arch/arm64/boot/dts/mediatek/mt6735-i14-bringup.dts
board_source="$repo/kernel/mt6735-i14-bringup.dts"
while IFS= read -r path; do
    [[ "$path" == "$board" ]] && cmp -s "$src/$board" "$board_source" || {
        echo "Unexpected or mismatching kernel build input: $path" >&2; exit 1;
    }
done < <(git -c safe.directory="$src" -C "$src" ls-files --others)
printf '%s  %s\n' "$base_hash" "$base" | sha512sum -c -
[[ ! -e "$out" ]] || { echo 'Output must be new; preserve earlier builds' >&2; exit 1; }
jobs=${JOBS:-4}
[[ "$jobs" =~ ^[1-4]$ ]] || { echo 'JOBS must be 1..4' >&2; exit 2; }
dtb=${4:-mediatek/mt6735-i14-bringup.dtb}
[[ "$dtb" =~ ^mediatek/[a-zA-Z0-9_-]+\.dtb$ ]] || { echo 'Invalid DTB target' >&2; exit 2; }
[[ -e "$src/$board" ]] || cp "$board_source" "$src/$board"
mkdir -p "$out"
cp "$base" "$out/.config"
export ARCH=arm64 LLVM=1 KBUILD_BUILD_USER=i14 KBUILD_BUILD_HOST=bringup
export KBUILD_BUILD_TIMESTAMP='2026-10-30 00:00:00 UTC'
export KBUILD_BUILD_VERSION=1
export LOCALVERSION=''
make -C "$src" O="$out" HOSTLDFLAGS=-fuse-ld=lld olddefconfig
"$src/scripts/kconfig/merge_config.sh" -m -O "$out" "$out/.config" "$repo/kernel/bringup.config"
make -C "$src" O="$out" HOSTLDFLAGS=-fuse-ld=lld olddefconfig
# Fail if Kconfig silently drops any required built-in driver or gadget function.
while IFS= read -r option; do
    case "$option" in CONFIG_*=y) grep -Fxq "$option" "$out/.config" || { echo "Missing required $option" >&2; exit 1; } ;; esac
done < "$repo/kernel/bringup.config"
# These pinned-source legacy drivers register a gadget without /init's configfs.
for option in USB_ZERO USB_AUDIO USB_ETH USB_FUNCTIONFS USB_MASS_STORAGE USB_G_SERIAL USB_G_PRINTER USB_MIDI_GADGET USB_CDC_COMPOSITE USB_G_HID USB_G_DBGP USB_G_MULTI USB_G_NOKIA USB_G_WEBCAM USB_G_NCM USB_G_ACM_MS USB_GADGET_TARGET USB_RAW_GADGET; do
    if grep -Eq "^CONFIG_${option}=[ym]$" "$out/.config"; then
        echo "Unexpected legacy gadget CONFIG_${option}" >&2; exit 1
    fi
done
make -C "$src" O="$out" HOSTLDFLAGS=-fuse-ld=lld -j"$jobs" Image "$dtb" modules_prepare
{
    git -c safe.directory="$src" -C "$src" rev-parse HEAD
    git -c safe.directory="$src" -C "$src" status --short
    clang --version
    ld.lld --version
    make --version
    sha256sum "$base" "$repo/kernel/bringup.config" "$board_source" "$src/$board" "$out/.config" "$out/arch/arm64/boot/Image" "$out/arch/arm64/boot/dts/$dtb"
} > "$out/build-provenance.txt"
echo "Built Image and $dtb; no phone boot or compatibility claim."
