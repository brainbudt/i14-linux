# I14 rootfs trial and rollback

The boot image uses `rootfs.img` on userdata. A candidate prepared for trial
must have a matching `rootfs-trial.img` link, a previous image at
`rootfs-prev.img`, and a 512-byte `rootfs-trial` record. Without the record,
the normal boot follows the usual image checks.

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

Host tests exercise the decision and watcher logic with disposable files and
fake system actions. Device acceptance follows the [R section of the
roadmap](ROADMAP.md#restore-integrity-and-accept-the-new-rootfs-r).
