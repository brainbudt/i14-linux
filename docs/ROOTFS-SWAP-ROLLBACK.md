# I14 rootfs trial and rollback

The boot image uses `rootfs.img` on userdata. A candidate prepared for trial
must have a matching `rootfs-trial.img` link, a previous image at
`rootfs-prev.img`, and a 512-byte `rootfs-trial` record. Without the record,
the normal boot follows the usual image checks.

Before releasing a staged trial for boot, check that `rootfs.img` and
`rootfs-trial.img` are regular files with the same device and inode, and that
`rootfs-prev.img` has the independently verified previous image on a different
inode. Reconcile old markers and receipts first. If the trial link is missing
or points elsewhere, rollback can replace the candidate's last name. Verify
the restored image independently of the `rollback` record.

The initramfs records each candidate boot before checking or mounting the
image. It falls back after at most two starts, or immediately if the candidate
fails the filesystem, mount or init check. If an image cannot be unmounted, the
boot enters rescue with both image paths in place. The rollback renames the prepared
previous image onto `rootfs.img` and retains the failed image as
`rootfs-trial.img`. The preallocated record and in-place rename allow the
rollback to work when userdata has no free allocation.

Before OpenRC, `i14-rootfs-trial` captures the carried DNS root key for the
host's check. Confirmation also requires the configured services, SSH on port
22, and a nonce acknowledgement bound to the active trial and boot ID. A trial
without confirmation is restarted by the watcher so the initramfs can roll
back. Recovery login and power-off charging follow the existing boot paths.

The base rootfs builder seeds the packaged DNS trust anchor. Trial preparation
must carry the agreed baseline bytes, write their SHA-256 to
`/etc/i14-rootfs-root-key.sha256`, and write a fresh candidate stamp to
`/etc/i14-rootfs-id`. Verify the staged image after these writes. Before
acknowledging a trial, compare the retained `rootfs-trial-root-key.<boot-id>`
bytes and the receipt's boot ID, stamp and digest with the independent host
evidence. During the automatic rollback rehearsal, withhold the acknowledgement
on both starts and verify the subsequent boot against the restoration baseline.
`scripts/i14-r2-handoff-preflight.py` and `scripts/i14-r2-host-handoff.py`
manage a historical host supervisor.

Host tests exercise the decision and watcher logic with disposable files and
fake system actions. Device acceptance follows the [R section of the
roadmap](ROADMAP.md#restore-integrity-and-accept-the-new-rootfs-r).
