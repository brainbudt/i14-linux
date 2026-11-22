# From a Topiky smartphone to I14 Linux

This history describes one purchased device. It does not automatically apply
to every phone from this brand or to other units sold under the same name.

## A different phone from the one the listing suggested

The phone was purchased through [Amazon.nl, ASIN B0CJ64KTBN](https://www.amazon.nl/dp/B0CJ64KTBN),
under the **Topiky** brand and model **Topikyg3im86nzcv-13**. The listing advertises
Android 13, an octa-core MT6735, 8 GB of RAM and conflicting storage capacities.

The inventory on **30 October 2026** found Android 6 with kernel 3.18.19.
The firmware called the device **I14 Pro Max**. Linux reports four possible CPU
cores, roughly 2 GB of RAM and an eMMC of about 16 GB.
[Sales claims, measurements and listing screenshots](HARDWARE.md).

## What suspicious software was preinstalled?

The original cleanup list and restore script identify the following examples:

| Package / APK path | Why it drew attention | Evidence limit |
| --- | --- | --- |
| `com.abfota.systemUpdate` — `/system/app/Rsota/Rsota.apk` | Unclear vendor updater | Presence documented; update behavior and data transmission not established through forensic analysis |
| `com.example` — `/system/app/AutoDialer/AutoDialer.apk` | AutoDialer with a generic development package name | No demonstrated covert calls, billing fraud or active backdoor |
| `com.kingsentime.factorytest`, `com.kst.factorytest_aging`, `com.kingsentime.deviceinfo` | Factory, aging-test and device-information tools | Shipped test software; names do not establish malicious behavior |
| `com.mediatek.mtklogger` | Vendor logging tool, unnecessary for our use case | No recorded network exfiltration or independent analysis of collected data |
| `com.magcomm.facelaunchericon`, `com.elephanttek.faceunlock` | Launcher and face-unlock components | No independent validation of advertised biometric security |
| WhatsApp, Facebook and YouTube in `/system/vendor/operator/app/`, including a folder named `FM_WhatsApp` | Pre-bundled vendor/operator apps | The folder name alone does not prove a modified or infected APK |

These are **unclear and unwanted preinstalled components**. The retained inventory
does not contain a proven malware classification based on an antivirus report,
demonstrated exfiltration, APK reverse-engineering or malicious calls.
We therefore do not label them spyware or trojans without that evidence.
Ordinary Google, communication and wallpaper apps on the same cleanup list
are not automatically malware either.

The original Android build fingerprint ends in `user/test-keys`.
That is relevant build information, not standalone evidence of an active attacker.
Corruption later found in the Google app on the system partition was investigated
as a storage/integrity problem, not as proven malware.

## What happened next?

| Date | Milestone |
| --- | --- |
| **30 October 2026** | Inventory and cleanup: the result records **42 packages removed for the user** and three disabled Google core packages. APK backups retained locally. Debian initially ran through Termux/PRoot, not native Linux. |
| **30 October – 2 November** | Boot chain, partitions and hardware investigated. Experimental 7.0.10 kernel booted natively. For usable power management and vendor hardware, native Alpine on the original 3.18 kernel became the practical route, with USB and Wi-Fi management. |
| **3 November** | Custom boot and charging screens; further work on power, mainline, recovery and long-term use. |
| **4 November** | Android system partition and application data removed. Custom charging and USB rescue modes, storage checks, firewall, service supervision and Unbound added. Required Wi-Fi binaries retained from the device's own backup. |
| **5 November** | Touch interface, terminal, keyboard, Wi-Fi settings, Bluetooth scanning and device settings. Charge limit and CPU control investigated and tested. Initial private GitHub publication prepared. |

“Removed for the user” during the initial cleanup does not mean all system APKs
were erased at that point. Android as an environment was removed on 4 November.
The old kernel and a few MediaTek binaries remain in use; this is not an
independently proven clean or fully rebuilt firmware platform.

## Where the project stands

The phone boots Alpine, provides local controls and can be managed over Wi-Fi
and USB. DNS works in the tests performed. Battery accuracy, complete charging
cycles, recovery from an actually empty battery and multi-day reliability
remain separate acceptance tasks. [Current status](STATUS.md).

## Sources

Based on the original hardware inventory, `removed_pkgs.txt`, `restore.sh`,
the cleanup result from 30 October, and installation/test reports from 2–5 November.
Raw sources and APKs remain local. A limited summary is published in
[the cleanup record](verification/android-cleanup-summary.json).
