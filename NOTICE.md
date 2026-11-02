# DashPlay — notices and attribution

DashPlay is distributed under the GNU General Public License v3.0 (see `LICENSE`). It is a set of
patches and tools for PlayPort and must stay under the same license.

- **PlayPort** — <https://github.com/youcci/playport> (GPL-3.0). The patches in `patches/` modify
  PlayPort at commit `9a0882dd0ffe48e467b59d58b12d81391df55ade`; PlayPort source is not copied here.
- **DiPlay** by shihabal3amri and **xcertplay** by shilapi (GPL-3.0) — the protocol stack PlayPort
  derives from. The CarPlay view areas in `patches/playport-view-areas.patch` (several areas in /info and
  `updateViewArea` to switch between them without reconnecting) follow DiPlay pull request #246 and its
  `docs/VIEW_AREAS.md` <https://github.com/shihabal3amri/DiPlay>.
- **WheelPlay** — <https://github.com/fython/wheelplay> (GPL-3.0), commit
  `c1bd07717c8e87a3130ef03477d2ca318fb66caa`. The iAP2 file-transfer (album artwork) receiver in
  `patches/playport-car-viewer.patch` is ported from it, and so are the wired CarPlay transport
  classes in `patches/playport-wired.patch` (USBMUX host, Lockdown pairing and CarKit service, the
  TLS duplex channel and the wired iAP2 control client; Android logging and XML replaced).

- **Android app** (`android/`): the Bluetooth RFCOMM link, the USB (NCM/iAP2) transport and the
  Ethernet/IPv6 + iAP2 codecs under `android/app/src/main/java/com/shilapi/xcertplay/transport/`
  are ported from **DiPlay** by shihabal3amri (GPL-3.0) <https://github.com/shihabal3amri/DiPlay>.
  The rest of the app (receiver, USB host, hotspot, Root, certificate handling, UI) is DashPlay's
  own code on the same PlayPort baseline that the boxes use. The app ships without any CarPlay
  identity and imports one the user already has.

No accessory authentication identity, accessory certificate or private key is included in the
repository, packages, documentation or tests. Tests generate synthetic identities at runtime.
DashPlay does not automatically download identities or provide a one-click APK importer. Local
identity mode requires the user-supplied PKCS#8 EC P-256 private key `identity.pk8` and matching
`certificate.p7b`; the receiver refuses to start without them.

PlayPort documents DiPlay release APKs, Carlinkit firmware, the user's own MFi identity and an
`--mfi-server` remote service as possible sources/options. Mentioning these is attribution, not a
grant to redistribute their contents. Shared experimental identities can be revoked. DashPlay is
not an Apple-certified product and is intended for personal experimentation; do not sell or
redistribute authentication identities.

The **box settings icon** is original geometric artwork generated for DashPlay by
`scripts/generate-box-settings-icon.py` (2026-10-11), distributed under GPL-3.0 with this project.
It uses no third-party icon or font assets. See [icon provenance](assets/ICON-SOURCE.md).
The historical receiver resource name `f50-settings.png` is retained for compatibility.

The OneCloud and Phicomm N1 **firmware images** (distributed separately, not in this repository) additionally contain:
the Armbian-unofficial 25.11 OneCloud base image (Debian 13 armhf packages, Linux 6.12; sources from
Debian and the Armbian build framework) or, for the N1, the ophub amlogic-s9xxx-armbian image
(<https://github.com/ophub/amlogic-s9xxx-armbian>, Debian 13 arm64, Linux 6.12 ophub kernel), Debian's OpenJDK 21 runtime (GPL-2.0 with Classpath
Exception), PlayPort built from the patches here and its Maven dependencies (licenses inside each JAR),
**acme.sh** 3.1.6 (<https://github.com/acmesh-official/acme.sh>, GPL-3.0, unmodified main script and DuckDNS
hook, with its LICENSE.md; used only when the owner sets up an own DuckDNS name) and
**dbus-next** 0.2.3 (<https://github.com/altdesktop/python-dbus-next>, MIT,
unmodified). The same acme.sh files are in the program bundle that the OpenWrt package installs. No identity, pairing record, password, token or host key is included; SSH host keys and
the machine id are generated on the first boot.

Each DashPlay release also carries the **program bundle** `dashplay-bundle-<version>.tar` (the
`scripts/onecloud` program, PlayPort built from the patches here with its Maven dependencies, and
dbus-next 0.2.3), which the OpenWrt packages of the sister repository openwrt-dashplay install.

The **macOS helpers** in `tools/mac-arm64/` (used only by the installation guide on the user's Mac, built
by `scripts/build-mac-tools.sh` from the pinned sources in `skills/dashplay-install/downloads.json`):
`spd_dump` from **spreadtrum_flash** <https://github.com/TomKing062/spreadtrum_flash> commit
`d24c21a0c93a545c0130668caa1826d59ceebe48` (a fork of ilyakurdyukov/spreadtrum_flash, Unlicense) with
`patches/spreadtrum-read-user-area.patch`, statically linked with **libusb** 1.0.29
<https://github.com/libusb/libusb> (LGPL-2.1; its unmodified source is the pinned release tarball and the
build script relinks it), and the **lz4** 1.10.0 command line tool <https://github.com/lz4/lz4> (GPL-2.0
for the program, BSD-2-Clause for the library; unmodified, source is the pinned release tarball). The guide
also downloads, without redistributing it, a standalone CPython 3.12 build from
<https://github.com/astral-sh/python-build-standalone> (PSF and bundled-library licenses inside it).

The **Windows helpers** in `tools/windows-x64/` are the same `spd_dump` (with
`patches/spreadtrum-windows-libusb.patch` as well), libusb and lz4 from the same pinned sources,
cross-compiled with MinGW-w64 by `scripts/build-windows-tools.sh`. On Windows the guide also downloads, without
redistributing it, **Zadig** 2.9 <https://zadig.akeo.ie/> (GPL-3.0) to install the WinUSB driver for the F50
in download mode.

CarPlay, iPhone, AirPlay and MFi are trademarks of Apple Inc. This is an independent project; no
Apple, PlayPort, DiPlay, xcertplay or WheelPlay affiliation or endorsement is implied.
