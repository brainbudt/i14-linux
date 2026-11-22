#!/bin/sh
# Build rootfs.img (ext4) for I14 Linux: Alpine aarch64 on the stock 3.18 kernel.
# Runs inside an x86_64 alpine:3.24 container with this directory mounted on /work:
#   docker run --rm -e I14_SOURCE_COMMIT -v "$PWD":/work alpine:3.24 sh /work/build-alpine-rootfs.sh
# Needs /work/authorized_keys (SSH public keys for root) and the stock system partition image,
# read-only, for the Wi-Fi tools (Android itself is gone from the phone):
#   -v /path/to/own/system.img:/work/system.img:ro
# Output: /work/out/rootfs.img and a local build manifest
# Packages are installed for aarch64 without running their scripts (this host cannot
# run arm64 code); the few script effects that matter are applied by hand below.
set -eu
case ${I14_SOURCE_COMMIT:-} in
	*[!0-9a-f]*|'') echo "I14_SOURCE_COMMIT must be a full Git SHA" >&2; exit 1 ;;
esac
[ "${#I14_SOURCE_COMMIT}" -eq 40 ] || { echo "I14_SOURCE_COMMIT must be a full Git SHA" >&2; exit 1; }
V=3.24.3
MINI_SHA256=4d3f80afaebd447c5591a63a73bde380a149fe8b2d85488cafabb2ff97551f50
MIRROR=https://dl-cdn.alpinelinux.org/alpine
SIZE=${SIZE:-3G}
PKGS="alpine-base busybox-mdev-openrc busybox-extras busybox-extras-openrc openssh
	wpa_supplicant wpa_supplicant-openrc iw e2fsprogs chrony ca-certificates attr nano htop
	python3 font-terminus tzdata iptables iptables-legacy unbound unbound-openrc dnssec-root"
# The tree is built inside the container, never on the mounted /work: a macOS host folder is
# case-insensitive (libxt_TOS.so and libxt_tos.so would overwrite each other) and drops ownership.
R=/build/root
OUT=/work/out

apk add --no-cache e2fsprogs e2fsprogs-extra curl python3 >/dev/null
rm -rf "$R" "$OUT"
mkdir -p "$R" "$OUT"
curl -fsS -o /work/minirootfs.tar.gz "$MIRROR/v3.24/releases/aarch64/alpine-minirootfs-$V-aarch64.tar.gz"
echo "$MINI_SHA256  /work/minirootfs.tar.gz" | sha256sum -c -
tar -xzf /work/minirootfs.tar.gz -C "$R"
printf '%s\n' "$MIRROR/v3.24/main" "$MIRROR/v3.24/community" >"$R/etc/apk/repositories"
# Keys and arch come from the minirootfs (/etc/apk/keys, /etc/apk/arch).
apk --root "$R" --no-scripts --no-cache add $PKGS

# Script effects that matter: service users (pre-install scripts of openssh, chrony).
grep -q '^sshd:' "$R/etc/passwd" || {
	echo 'sshd:x:22:22:sshd:/var/empty:/sbin/nologin' >>"$R/etc/passwd"
	echo 'sshd:x:22:' >>"$R/etc/group"
	echo 'sshd:!::0:::::' >>"$R/etc/shadow"
}
# The Android kernel only lets group 3003 ("inet", CONFIG_ANDROID_PARANOID_NETWORK) open network
# sockets. chronyd drops all supplementary groups, so inet is its primary group; unbound keeps them.
grep -q '^inet:' "$R/etc/group" || echo 'inet:x:3003:unbound' >>"$R/etc/group"
grep -q '^chrony:' "$R/etc/passwd" || {
	echo 'chrony:x:101:3003:chrony:/var/log/chrony:/sbin/nologin' >>"$R/etc/passwd"
	echo 'chrony:x:101:' >>"$R/etc/group"
	echo 'chrony:!::0:::::' >>"$R/etc/shadow"
}
grep -q '^unbound:' "$R/etc/passwd" || {
	echo 'unbound:x:100:102:unbound:/etc/unbound:/sbin/nologin' >>"$R/etc/passwd"
	echo 'unbound:x:102:unbound' >>"$R/etc/group"
	echo 'unbound:!::0:::::' >>"$R/etc/shadow"
}
ln -sf /bin/busybox-extras "$R/usr/sbin/udhcpd"  # busybox-extras post-install creates this link
mkdir -p "$R/var/empty" "$R/var/lib/udhcpd" "$R/var/log/chrony" "$R/mnt/data" "$R/system" "$R/nvdata" "$R/data"
ln -s /nvdata "$R/data/nvram"  # only this: the Wi-Fi driver's NVRAM path (i14-wifi-up mounts nvdata read-only)
chown 101:101 "$R/var/log/chrony"
# DNSSEC root trust anchor, kept current by unbound itself afterwards (RFC 5011).
install -d -o 100 -g 102 -m 755 "$R/var/lib/unbound"
install -o 100 -g 102 -m 644 "$R/usr/share/dnssec-root/trusted-key.key" "$R/var/lib/unbound/root.key"

cp -R /work/overlay/. "$R/"
python3 /work/configure-dns.py "$R" /work/dns-clients.conf
find "$R" -name __pycache__ -prune -exec rm -rf {} +
# Wi-Fi: Android properties for the MediaTek tools, firmware path of the gen2 driver.
install -d "$R/etc/i14"
python3 /work/make-android-props.py "$R/etc/i14/android-properties"
ln -sfn /system/etc/firmware "$R/etc/firmware"
# Android's MediaTek Wi-Fi tools with their linker and libraries, and the firmware, copied out of
# the stock system image into /system (not mounted: a plain directory). Files and hashes:
# /work/android-wifi.sha256. MediaTek's files, so they never go into git.
while read -r _ f; do
	install -d "$R/system/${f%/*}"
	debugfs -R "dump -p /$f $R/system/$f" /work/system.img 2>/dev/null
done </work/android-wifi.sha256
(cd "$R/system" && sha256sum -c -s /work/android-wifi.sha256)
cp "$R/usr/share/zoneinfo/Europe/Amsterdam" "$R/etc/localtime"
echo Europe/Amsterdam >"$R/etc/timezone"
install -d -m 700 "$R/root/.ssh"
install -m 600 /work/authorized_keys "$R/root/.ssh/authorized_keys"
chown -R 0:0 "$R/root" "$R/etc" "$R/usr/sbin"

# Services. sysinit: /dev is already populated by /i14init (no devtmpfs on 3.18).
for s in devfs dmesg mdev i14-splash; do ln -sf /etc/init.d/$s "$R/etc/runlevels/sysinit/$s"; done
for s in bootmisc hostname sysctl syslog seedrng i14-firewall; do
	[ -e "$R/etc/init.d/$s" ] && ln -sf /etc/init.d/$s "$R/etc/runlevels/boot/$s"
done
for s in networking sshd chronyd crond i14-power i14-cpufreq i14-usbnet i14-wifi i14-netwatch unbound i14-ui local; do
	ln -sf /etc/init.d/$s "$R/etc/runlevels/default/$s"
done
for s in killprocs mount-ro savecache; do ln -sf /etc/init.d/$s "$R/etc/runlevels/shutdown/$s"; done
# Normal boot uses SSH keys for root; authenticated serial rescue lives in the boot image.
sed -i 's/^root:[^:]*:/root:*:/' "$R/etc/shadow"

truncate -s "$SIZE" "$OUT/rootfs.img"
mke2fs -q -t ext4 -L i14-root -O ^metadata_csum,^metadata_csum_seed,^orphan_file,^64bit -d "$R" "$OUT/rootfs.img"
apk --root "$R" info -v >/build/i14-packages.txt
(umask 077; python3 /work/write-build-manifest.py /work /build/i14-packages.txt "$I14_SOURCE_COMMIT")
sha256sum "$OUT/rootfs.img"
du -sh "$R"
