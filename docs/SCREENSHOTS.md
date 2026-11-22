# Screenshots and provenance

Both published images are unmodified native device framebuffer captures.
Original files remain stored locally. Hashes and provenance are recorded in
[the screenshot register](verification/screenshots.json).

| File | Content | Source / limitation |
| --- | --- | --- |
| [boot.png](../assets/screenshots/boot.png) | Custom Linux boot screen with actual kernel output | Existing framebuffer capture from the boot-screen investigation on 3 November 2026; not a photograph or a capture of the latest boot |
| [settings.png](../assets/screenshots/settings.png) | Brightness, screen timeout, PIN settings, storage and estimated charge limit | Actual framebuffer capture from 5 November 2026 after the battery UI update, around 20:27 CET; virtual input was used to navigate for this capture |

The device captures are **720 × 1600**, not generated mockups. Text within the
original Dutch interface captures has deliberately not been edited.
The English documentation explains their content.

The settings capture establishes the interface visible at that time, not a full
charge cycle, physical touch test or battery-percentage accuracy. The boot capture
shows an earlier boot phase; it does not establish that the current installation
produces exactly the same logs.

Home-screen and scanning-page captures are excluded because existing copies
contain personal or neighboring network/device names. PINs, Wi-Fi passwords,
MAC addresses and personal identifiers are not published as screenshots.
Third-party Amazon product listing screenshots are also retained locally rather
than redistributed here; the sales claims are summarized in
[Sales claims versus measurements](HARDWARE.md).
