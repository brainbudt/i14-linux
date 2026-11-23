# Runs on the phone via `sh -s`. Prints boot id and the start time (clock ticks since boot,
# /proc/<pid>/stat field 22) of each monitored process. Exits nonzero on any incomplete capture.
date -u +%FT%TZ || exit 2
cat /proc/sys/kernel/random/boot_id || exit 2
pids=$(pgrep -f 'supervise-daemon|i14-rs|unbound|wpa_supplicant|sshd|chronyd|crond|syslogd') || exit 3
[ -n "$pids" ] || exit 3
n=0
for p in $pids; do
	# Skip only a process that has exited meanwhile; a failed read of a live one is an error.
	if ! st=$(cat /proc/$p/stat) || ! cmd=$(tr '\0' ' ' </proc/$p/cmdline); then
		[ -d /proc/$p ] && { echo "read failed for live pid $p" >&2; exit 5; }
		continue
	fi
	set -- $st
	shift 21
	echo "$p $1 $cmd"
	n=$((n + 1))
done
[ "$n" -ge 10 ] || exit 4   # fewer than the supervised services + daemons: incomplete
