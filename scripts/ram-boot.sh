#!/bin/bash
# RAM-boot IMAGE via fastboot (nothing is flashed). Phone must be in Android with adb, or already in fastboot.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ADB=$ROOT/artifacts/tooling/platform-tools/adb
IMG=$1
SHA=$(shasum -a 256 "$IMG" | cut -d' ' -f1)
fastboot_present() { ioreg -p IOUSB -l -w0 | grep -A30 '"idVendor" = 2996' | grep -q '"idProduct" = 3073'; }
if ! fastboot_present; then
    # Only reboot the I14 (the generic adb serial is not an identity; the device name is).
    [ "$("$ADB" shell getprop ro.product.device | tr -d '\r')" = I14_Pro_Max ] || { echo "adb device is not the I14"; exit 2; }
    "$ADB" reboot bootloader
fi
for _ in $(seq 1 60); do fastboot_present && break; sleep 1; done
fastboot_present || { echo "no fastboot device"; exit 3; }
sleep 2
DYLD_LIBRARY_PATH=$ROOT/artifacts/tooling/lib "$ROOT/.venv/bin/python" -B "$ROOT/scripts/fastboot_usb.py" --boot "$IMG" --sha256 "$SHA"
