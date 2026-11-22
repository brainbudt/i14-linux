# Which phone is this?

The commercial product name differs from the name used by the firmware.

| Identity | Value | Source |
| --- | --- | --- |
| Brand | **Topiky** | Listing supplied by the owner |
| Model name | **Topikyg3im86nzcv-13** | Supplied product text |
| Product page | [Amazon.nl — B0CJ64KTBN](https://www.amazon.nl/dp/B0CJ64KTBN) | Purchase link supplied by the owner |
| Listing title, shortened and translated | Unlocked Android 13 smartphone, 6.6-inch, 4G LTE, FHD Face ID, 4GB/64GB, 128GB expandable, dual SIM, black | Supplied product text |
| Firmware model name | **I14 Pro Max** (`I14_Pro_Max`) | Inventory from 30 October 2026 |
| Platform | **MediaTek MT6735**, ARM64 | Platform properties and Linux |
| Board/project | `magc6735_65u_1_m0`, firmware branch `P45` | Original board/build information |

“I14 Pro Max” is a name used by the clone firmware, not an Apple model identifier.
Other phones with the same sales name may have different motherboards.

## Sales claims versus measurements

The product page could not be retrieved automatically during preparation.
The sales claims come from text and screenshots supplied by the owner on
**5 November 2026**. They are not specifications verified by this project.

| Component | Listing claim | Observed on this device |
| --- | --- | --- |
| Operating system | Android 13 / 13.0 | Originally **Android 6**, API 23; now **Alpine 3.24.3** |
| CPU | “MT6735 Octa Core” | MT6735, Linux exposes **CPUs 0–3**, four possible cores |
| RAM | 8 GB; title also says 4 GB | `MemTotal: 1970112 kB`, about **1.88 GiB available to Linux**, consistent with roughly 2 GB including reserved memory |
| Storage | Title says 64 GB, specification 128 GB, product table 512 GB | eMMC reports **15,634,268,160 bytes**: about **15.63 GB / 14.56 GiB**; no independent full-capacity test |
| Display | 6.6 inches, 2520 × 3088 | Interface and framebuffer captures use **720 × 1600**; diagonal not measured separately |
| Battery | 7000 mAh | Capacity not independently measured; Linux gauge is uncalibrated |
| Rear camera | 24 MP in text, **2 MP** in the product-information screenshot | Resolution and usable Linux camera pipeline not established |
| Front camera | 8 MP | Not independently established |
| Wi-Fi / Bluetooth | Dual-band Wi-Fi, Bluetooth 5.0 | Wi-Fi and Bluetooth scanning tested; bands and version not independently accepted |
| Face recognition / “Face ID” | Advertised feature | Vendor apps found; biometric operation and security not independently assessed |

The listing is internally inconsistent as well. A firmware property or kernel
report is not, in turn, a physical laboratory measurement.

## Original product information

The listing claims in the table above were transcribed from the Dutch Amazon.nl
product page supplied by the owner on 5 November 2026. The third-party
marketplace screenshots are retained locally and omitted from the public
repository. [Device screenshot provenance](SCREENSHOTS.md).

## Current installation

The current route uses the original **3.18.19 kernel**, a custom startup program
and Alpine. The Android application environment and system partition have been
removed; the kernel and a few MediaTek Wi-Fi files are retained.
The experimental **7.0.10** route is kept separately under `kernel/`.

Firmware, partition backups and individual identifiers are not included.
[Current feature status](STATUS.md).
