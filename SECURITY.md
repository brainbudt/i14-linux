# Security Policy

## Reporting a Vulnerability

Please report security issues privately using GitHub **Private Vulnerability
Reporting** ("Report a vulnerability" in the repository's **Security** tab)
rather than opening a public issue.

Include the affected component, exact commit or revision, reproduction steps,
and impact. Do not include private device dumps, NVRAM/NVDATA, SSH keys,
Wi-Fi credentials, or personal identifiers in a report.

## Known Security Considerations & Scope

This is an experimental single-device Linux port for a specific MediaTek MT6735
handset (`Topiky` / `I14 Pro Max`, `magc6735_65u_1_m0`). It is **not** a
hardened general-purpose operating system distribution:

- **Stock 3.18.19 kernel:** The current working Alpine installation runs on the
  vendor's end-of-life Linux 3.18.19 kernel without a verified boot chain.
- **Unbound DNS advisories:** As documented in
  [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), verify the built Alpine package
  manifest against Alpine SecDB and upstream NLnet Labs advisories before
  exposing Unbound to LAN clients (specifically `CVE-2026-81634` and
  `CVE-2026-96405` on `unbound-1.25.2-r2`). Keep `dns-clients.conf` empty
  (local health queries only) until patched packages are verified.
- **Optional debug initramfs (`I14_DEBUG_INITRAMFS=1`):** The default initramfs
  omits active USB diagnostics and console shells. Building a separate debug
  image with `scripts/build-busybox.sh OUTPUT --debug` enables unauthenticated
  USB diagnostics (serial, Telnet, HTTP) intended strictly for isolated
  bring-up over a trusted cable and host. Never deploy a debug initramfs for
  normal operation.
- **Private build inputs stay local:** SSH `authorized_keys`, rescue password
  hashes (`--rescue-password-hash-file`), Wi-Fi credentials, Bluetooth link
  keys (`bt-keys`), firmware dumps and NVRAM/NVDATA must never be committed to
  Git.
