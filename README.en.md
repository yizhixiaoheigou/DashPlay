# DashPlay

[中文](README.md)

DashPlay receives iPhone CarPlay on a Linux box, a supported OpenWrt device, or an Android device. A browser on the head unit or another device displays the picture, plays audio, and provides touch control. Android phones support wireless CarPlay only; connection options on boxes depend on their platform and hardware. The browser and receiver must be reachable over the network.

DashPlay is built on [PlayPort](https://github.com/youcci/playport): patches plus the programs around it, namely wired CarPlay (the box as USB host), a Linux BlueZ Bluetooth bridge, a lean page for head-unit browsers with system media controls, first-run setup and a settings page, and a supervisor that starts everything at boot.

> **This is not an Apple-certified product.** You have to supply your own accessory authentication identity; see "Authentication identity" below. Flashing carries a risk of bricking the device.

## Choose your platform

| Platform | Download and guide |
|---|---|
| Android phone or TV box | **DashPlay for Android**: one `DashPlay-android-<version>.apk` for both. Keep Android; no Linux flashing is needed. See the [Android guide](docs/DashPlay安卓使用说明.md) and [installation skill](skills/dashplay-android-install/SKILL.md) (Chinese) |
| Linux (N1 / OneCloud) | Program bundles and applicable firmware images from this repository; see the Linux instructions below |
| OpenWrt | Packages, feeds, and LuCI UI in [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay), using the core program from this repository |

Find Android APKs in the [release list](https://github.com/yizhixiaoheigou/DashPlay/releases). Android versions are separate from dated Linux/OpenWrt versions. Check that a release actually includes the Android asset; OpenWrt files can also end in `.apk` and cannot be installed on Android.

## Installing

**No technical background needed**: send this sentence to your AI assistant (Claude Code, Codex, WorkBuddy, Doubao, DeepSeek and the like, anything that can run commands on your computer). It asks you one thing at a time (the device, its current system, the car, whether the head unit can raise a hotspot, whether the head unit needs internet) and installs DashPlay step by step, calling you only to plug or unplug something or tap on your phone:

> Please download https://raw.githubusercontent.com/yizhixiaoheigou/DashPlay/main/skills/dashplay-install/SKILL.md with curl in a terminal and read all of it (not through a web summarizer, which may return an old or shortened copy), then follow it step by step to install DashPlay for me.

The guide itself is in Chinese; assistants follow it in whatever language you write to them. If your N1 runs stock Phicomm firmware or Android TV, or does not boot, just tell the assistant. The N1 route tested by this project starts from Linux; the two stock-firmware routes (2.19 or earlier, later than 2.19) come from public guides and have not been tested here.

## Which box

| | ZTE F50 (SIM version) | Phicomm N1 |
|---|---|---|
| Price (for reference) | about 400–500 CNY with a heatsink, plus mobile data | about 100 CNY, plus a 5 V to 12 V step-up cable |
| Head unit internet | with a SIM card, everything in the car works online | the box has no internet of its own: join the car's hotspot if it has one, otherwise a portable Wi-Fi or a phone's hotspot |
| Works without internet | yes (no SIM; the head unit's own apps stay offline; not yet tested in a car) | yes (tested in a car) |
| iPhone | wireless only (the F50's USB port cannot take an iPhone) | wired and wireless, see below |
| You need | an erasable SD card of 8 GB or more, a USB cable that carries data, a heatsink; a SIM card with data if the head unit should be online | an erasable USB drive of 8 GB or more, an Ethernet cable (the computer stays online through it while it is on the box's Wi-Fi); stock firmware also needs a display and a USB mouse, and later than 2.19 a USB-A male-to-male cable |
| Computer | Mac (Apple silicon) or 64-bit Windows 10/11, 15 GB free | Mac / Windows / Linux |
| Installation | done by the AI, about 25 minutes (measured) | about 30–60 minutes |

The OneCloud (玩客云) runs the same program as the N1 and connects the same ways; it also needs an RTL8821CU USB adapter (Wi-Fi and Bluetooth in one) and a 12 V supply. Other routers already on OpenWrt 25.12 use the plugin in the sister repository [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay). Every option also needs an iPhone.

## Connecting in the car

**F50**: the head unit and the iPhone join the F50's hotspot, and the iPhone goes wireless. With a SIM card the head unit is online; without one it still works, but the head unit's own apps stay offline.

**N1 / OneCloud**: chosen in first-run setup according to the head unit:

| Situation | Connections | Head unit's own apps online | Wired | Wireless |
|---|---|---|---|---|
| ① Head unit can raise a hotspot (way 1) | box joins the car's hotspot | yes | ✅ | ❌ cable only for now |
| ② You have a portable Wi-Fi (way 2) | box, head unit and iPhone join the portable Wi-Fi | yes, through the portable Wi-Fi | ✅ | ✅ |
| ③ Head unit can only join Wi-Fi (way 3) | head unit and iPhone join the box hotspot `DashPlay` | no | ✅ | ✅ |
| ③ plus a phone's hotspot | box joins a phone's hotspot and keeps `DashPlay` up for the head unit | yes, on the phone's data | ✅ | ✅ |

- ① is cable only for now, and only in this setup: a wireless iPhone would also have to join the car hotspot, which relays the picture between them at about 0.1 Mbit/s (measured in an Xpeng), so the program offers no wireless in ①. Letting the box raise its own hotspot for the iPhone would allow wireless later.
- With a phone's hotspot, the phone keeps its hotspot on and pays the data; when the hotspot is not found the box falls back to ③. The hotspot can come from the same iPhone that runs CarPlay.
- When both work: plugging in starts wired CarPlay if no wireless session is active. If wireless CarPlay is already connected, plugging in keeps it connected; the cable charges the phone and shares internet when configured. Select the wired entry in the phone list to switch to wired CarPlay. After unplugging, the last iPhone is invited to go wireless. Wireless needs one Bluetooth pairing first ("Add iPhone" on the settings page); after that it connects by itself.
- If the portable Wi-Fi in ② isolates its clients, wireless stalls or fails; use a cable.

**When the head unit cannot show the page or will not connect**, go by what you see, whatever the car (settings page group "When the head unit cannot open the picture"; steps in [AI install notes (Chinese)](docs/DashPlay-AI安装说明.md), section 4):

- The head unit refuses Wi-Fi without internet: the box answers its connectivity check, CarPlay works, and the head unit's own apps stay offline while connected; for those, use a SIM card (F50) or a phone's hotspot (N1).
- The browser refuses local addresses, or its certificate warning has no "continue": use your own address (free DuckDNS plus a real certificate; the AI walks you through signing up).
- The connection drops when leaving Park: turn on "stay connected while driving" or similar in the head unit's Wi-Fi settings.
- The head unit cannot see the box's 5 GHz hotspot: on the F50 / OpenWrt switch to channel 149, or else 2.4 GHz channel 6 (the N1 and OneCloud use 149 already).

A Tesla runs into all four. Tell the AI it is a Tesla and it turns on "Tesla mode" (which covers the first two) and sets up your own address with you.

## Hardware and tests

### Hardware

- **OneCloud (S805, Armbian / Debian 13 armhf) with an RTL8821CU USB adapter** (Wi-Fi and Bluetooth in one, `0bda:c820`), 12 V supply. Wired CarPlay uses the USB port away from the Ethernet port (OTG, switched to USB host). After flashing, a phone joins the box hotspot `DashPlay` for first-run setup, and the user's own AI assistant puts the authentication files in place. Flashing and use: [firmware guide (Chinese)](docs/DashPlay玩客云固件使用说明.md); steps for an AI: [AI install notes (Chinese)](docs/DashPlay-AI安装说明.md); firmware build tools: `scripts/onecloud/firmware/`.
- **Phicomm N1 (S905D, aarch64, built-in BCM43455 Wi-Fi/Bluetooth)**: the image is a USB-stick image with DashPlay preinstalled (Debian trixie from [ophub amlogic-s9xxx-armbian](https://github.com/ophub/amlogic-s9xxx-armbian)); after booting from the stick, `armbian-install` copies it to the eMMC. Same [firmware guide](docs/DashPlay玩客云固件使用说明.md). Both USB ports are host ports, so the iPhone can use either. `scripts/onecloud/firmware/make-n1-image.sh` makes the image on an N1.
- **Routers or portable Wi-Fi already running OpenWrt 25.12 (aarch64 / x86_64, including the ZTE F50)**: the OpenWrt plugin lives in the sister repository [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay) (apk packages and a package feed); the network stays configured in OpenWrt as usual. The plugin runs this repository's program, the `dashplay-bundle-<version>.tar` attached to each release. The [installation skill](skills/dashplay-install/SKILL.md) includes the stock F50 B16 backup, root and SD-card OpenWrt procedure and supporting scripts. This weakens boot verification and recovery may require opening the device. Stop on other firmware unless a reliable downgrade route is verified.

### Tested

- **In cars**: Xpeng, OneCloud way ① wired: first-run setup, about 27 minutes continuous, power off and on, the picture back 8–12 s after switching screens, 30–50 fps with no dropped frames. Tesla, F50 with a SIM card (wireless); N1 way ③ without internet (wired and wireless pictures, also after a reboot and after forgetting and re-adding the Wi-Fi) and way ③ with a phone's hotspot (wired). Both N1 setups were assembled by hand on the box at the time; they are in 2026.10.15 and the released version still has to be retested in a car.
- **At home**: OneCloud, each way from factory reset through first-run setup (a phone hotspot standing in for the car hotspot and the portable Wi-Fi), switching between cable and wireless by plugging and unplugging, power cycles, reset; N1 way ① picture, touch, sound, artwork; N1 on the hotspot of the same iPhone, wireless CarPlay at about 30 fps; F50 in CarPlay about 37–44 s after power-on, no invitations after the iPhone leaves the Wi-Fi, back in about 4 s when it returns, and the last iPhone invited after a reboot; the F50 from stock B16 to Tesla mode with the installation skill, once from a Mac and once from Windows 11.
- Wired music is lossless 48 kHz (the page wraps it in FLAC for the media element); album information and artwork reach the system media controls, and the steering-wheel buttons change tracks. Several iPhones take turns (wireless).
- **Not yet tested in a car**: the F50 without a SIM card; ② on the road; wireless with a phone's hotspot; the stock-firmware N1 routes.

### Other Linux boxes

They need a Wi-Fi client (ways 1 and 2) or access point (way 3, hostapd); classic Bluetooth (BlueZ, for wireless CarPlay only); a USB host port (wired CarPlay); Java 21 (Debian trixie / Ubuntu noble). `scripts/onecloud/platform_api.py` is the platform interface and `platform_debian.py` the Debian/Armbian implementation; `scripts/onecloud/board-probe.sh` checks a new board for these, read-only.

## Authentication identity

An iPhone only connects to a receiver that presents a valid accessory certificate and signs with the matching private key. Like PlayPort, DashPlay **neither includes nor distributes any identity**. The repository, packages, documentation and tests contain no real identity files; tests generate synthetic ones at runtime. In local authentication mode the receiver refuses to start without these two files:

```
identity/offline-mfi/identity.pk8       # PKCS#8 EC P-256 private key
identity/offline-mfi/certificate.p7b    # the matching accessory certificate
```

The easiest source is the Android APK that [DiPlay](https://github.com/shihabal3amri/DiPlay/releases) publishes: take the newest `DiPlay-*.apk` in its release list; the two files are under `assets/offline-mfi/` in it. The steps (including the check that key and certificate match) are in section 2 of the [install notes for AI assistants (Chinese)](docs/DashPlay-AI安装说明.md), which your AI assistant can follow for you. Other sources are in the [Accessory identity section of PlayPort's README](https://github.com/youcci/playport#accessory-identity-required): Carlinkit firmware, an identity you hold as an MFi licensee, and the `--mfi-server` remote authentication service (upstream's option; DashPlay's start scripts use the local files above). DashPlay itself ships no identity and downloads none. Please note:

- publicly available identities are shared experimental ones, not issued to this project or to PlayPort, and Apple may revoke them at any time;
- use them for personal experiments only: do not sell them, redistribute the files or commit them to any repository;
- keep identity files in the device's private directory, readable by root only.

## Build

Needs JDK 25 (build), Node.js (web page) and Git.

```sh
git clone https://github.com/youcci/playport.git
git -C playport checkout 9a0882dd0ffe48e467b59d58b12d81391df55ade
sh scripts/apply-playport-patches.sh playport
(cd playport && ./gradlew :protocol:test :server:test :server:installDist)
(cd playport/web && npm ci && npm test && npm run build)   # the FLAC check needs ffmpeg and is skipped without it
sh scripts/onecloud/firmware/build-bundle.sh dashplay-bundle.tar   # the program bundle (box installs and the OpenWrt packages)
```

`scripts/build-playport.sh` runs the same Gradle build and tests in an isolated Docker container. On the box DashPlay needs Java 21 and Python 3 with asyncio: OneCloud and N1 get them from Debian through `install.sh`; OpenWrt from the plugin's `dashplay-jre` (Temurin 21, musl build) and the system package feeds.

## DashPlay for Android (beta)

Android phones and TV boxes use the same APK. Phones support wireless CarPlay only; boxes support wired, wireless, or automatic selection according to their hardware. Source is in [`android/`](android/), using the pinned PlayPort upstream and Android’s frozen common patches in `android/receiver-baseline/`, followed by its platform adapters. Linux keeps its own root patch series.

- Use an actual `DashPlay-android-<version>.apk` release asset. If none is available, wait for a release or explicitly choose a source build (JDK 25, Android SDK 35, Node.js). Android 5.0+ and an ARMv7/ARM64 system ABI are required; x86 is not supported by the current native libraries.
- Setup asks one question at a time. Device, car, network, HTTP/HTTPS access, iPhone connection, Internet, identity, audio and startup choices determine the next questions. Missing prerequisites explain the reason and alternatives. See the [usage guide](docs/DashPlay安卓使用说明.md) and [decision tree](skills/dashplay-android-install/references/capability-routing.md) (Chinese).
- Wireless CarPlay requires classic Bluetooth and a reachable Wi-Fi network. Wired CarPlay is available only on Android boxes with USB host and USB/VPN consent; Bluetooth is not required for wired mode. Even wired CarPlay needs a LAN between the browser and receiver. A SIM is not required for local use.
- Ordinary browser use does not universally require Root. Tesla can try the experimental **HTTP** route without a domain or certificate: WebRTC video first, JPEG fallback with a maximum image edge of 1280 pixels. A receiver-hosted hotspot needs VPN address validation; an external network uses the receiver's reachable LAN address. **HTTPS** still requires a reachable address, trusted certificate, and working DNS; the receiver-hosted Tesla address path needs Root usable by the app. Both routes require actual head-unit verification, including Wi-Fi retention.
- The default shared HTTP/HTTPS port is **18743**. Management always uses `http://device-address:18743/manage`, independently of certificates and the HTTP picture option. Open the picture URL shown by the app. Keep the full `http://` or `https://` prefix; do not omit or replace it.
- Use an existing screen/remote or the management page once the app is running on a reachable LAN; no management key is required. System consent remains local. The [Android skill](skills/dashplay-android-install/SKILL.md) explains prerequisites without teaching flashing or how to obtain ADB/Root.
- No CarPlay identity is bundled; import your own. Upgrade with an APK signed by the same key, preserving app data. Do not uninstall or clear data to bypass a signature mismatch.
- Old wired/automatic settings on Android phones migrate to wireless; USB Internet is a separate opt-in, off by default. New USB-host boxes preselect wired CarPlay and USB Internet, subject to consent; explicit opt-outs persist. Plugging in during an active wireless session preserves wireless.
- When using one receiver with an iPhone, stop other DashPlay receivers to avoid competing wireless invitations. Build tests and desktop browser tests do not establish compatibility with every ROM or vehicle.

## Running (OneCloud / Phicomm N1)

- `scripts/onecloud/dashplay.service` starts `supervisor.py` (installed in `/opt/dashplay`): the network for the chosen way of use (joining the car hotspot or portable Wi-Fi, or raising the box hotspot; when the hotspot cannot be joined, the box hotspot opens temporarily for settings), Bluetooth, the receiver and the settings page (port 80, `http://dashplay.local/`).
- `setup_wizard.py`, `settings_page.py`, `settings_ui.py`: first-run setup (way of use → network → iPhone) and the box settings page; `phone-presence.py`, `pairing_window.py`, `pair-phone.py`: adding iPhones and taking turns (wireless).
- `linux/bt-bridge/`: PlayPort's Linux Bluetooth bridge, which opens the iAP2 RFCOMM channel through BlueZ D-Bus; `BT_BRIDGE_READY_WAIT` lets it wait when Bluetooth comes up after the receiver.
- `iphone-usb-bridge.py`: the USB side of wired CarPlay. It switches the iPhone to its CarPlay configuration, forwards USBMUX and bridges the NCM network in user space; the first time an iPhone is plugged in, tap "Trust" on it; trust records are kept per iPhone.
- `run-receiver.py` writes the receiver configuration and starts PlayPort through `launcher/onecloud/Launcher.java`; `diagnostics.py` records Wi-Fi, temperature and playback statistics to `state/diag/` every 10 s, and the full logs of each boot go to `state/logs/` (kept across power cuts; the settings page offers a diagnostics download).
- Firmware and installation: `scripts/onecloud/firmware/README.md`. `build-bundle.sh` makes the install bundle from this repository alone, `install.sh` installs it on Armbian, `prepare-image.sh` removes all personal data, and `pack-image.sh` rebuilds the OneCloud burn image.

## Running (OpenWrt)

- `supervisor_openwrt.py` and `platform_openwrt.py`: the supervisor and platform layer on OpenWrt. They only read OpenWrt's network (`ubus`), hand the chosen Wi-Fi to the iPhone over Bluetooth and follow it when it changes; without Bluetooth only wired CarPlay is offered. `board_zte_f50.py`: the ZTE F50's built-in Bluetooth bring-up with its own BlueZ. Packaging, installation and use: [openwrt-dashplay](https://github.com/yizhixiaoheigou/openwrt-dashplay).
- The bundle that `scripts/onecloud/firmware/build-bundle.sh` makes (`dashplay-bundle-<version>.tar` on the release page) is what the OpenWrt package installs.
- `scripts/carplay-night-probe.mjs`: captures the current CarPlay screen (night mode checks).

## Tests

```sh
python3 -m unittest discover -s tests/bluetooth     # needs dbus-daemon and dbus-next; all test identities are synthetic
python3 -m unittest discover -s tests/onecloud      # settings, several phones, platforms, diagnostics, firmware packing; certificate tests need OpenSSL 3
```

## Known issues (Linux/OpenWrt)

- When the player window and the CarPlay picture have a similar shape (normal view and full screen, for example), the picture is stretched to fill without reconnecting; the first time full screen is entered the box reconnects once at that size (about 8–12 s) and remembers it; very different shapes (portrait, split screen) reconnect at once.
- Way 1 is cable only; in way 3 the head unit has no internet while it is on the box hotspot; in way 2 a portable Wi-Fi with client isolation makes wireless stutter or fail, so use the cable.
- The head unit's browser warns about the certificate the first time; choose "continue". For a head unit whose address changes, a browser that blocks self-signed certificates outright, or one that refuses a LAN address, the settings page group "When the head unit cannot open the page" has the options; the steps are in section 4 of the [AI install notes (Chinese)](docs/DashPlay-AI安装说明.md).
- Calls and Siri use the head unit's microphone through the browser and play on its media channel (not the headrest speakers). The microphone reaches the browser very quiet; raised by the box, the other side hears you well but may hear its own echo. While the iPhone is also on the car's own Bluetooth, the car takes the call and the microphone too and the other side hears nothing: disconnect the iPhone from the car's own Bluetooth to call over CarPlay.
- When an app of the head unit itself (a video app, say) plays sound, the iPhone pauses and does not resume on its own: press play once on the steering wheel or the car's media card.
- A weak supply (an ordinary 5 V USB-to-DC cable) makes the OneCloud's USB adapter drop out again and again; use a 12 V supply or a 5→12 V boost cable.
- The Phicomm N1's 5 GHz box hotspot is limited to 40 MHz.
- SSH defaults to `root` / `1234` so that the user's AI assistant can add the authentication files; anyone on the same network can log in.
- The page's night mode command is wired up, but it is not yet confirmed that the iPhone switches its appearance accordingly.

## Box settings icon

![Box settings](assets/box-settings.png)

The icon is regenerated by the project's own geometric drawing script and uses no third-party icon assets. Editable source, reproduction and license: [icon provenance](assets/ICON-SOURCE.md). The receiver keeps the internal resource name `f50-settings.png` for compatibility; the product name is DashPlay.

## License and trademarks

GPL-3.0; see [LICENSE](LICENSE) and [NOTICE.md](NOTICE.md). CarPlay, iPhone, AirPlay and MFi are trademarks of Apple Inc.; this project is not affiliated with or endorsed by Apple, PlayPort, DiPlay, xcertplay or WheelPlay.
