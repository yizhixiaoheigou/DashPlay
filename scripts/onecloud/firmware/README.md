# DashPlay OneCloud firmware build

Produces `DashPlay_onecloud-<date>.burn.img` (USB Burning Tool; latest 2026-10-12) from the base
`Armbian-unofficial_25.11.0-trunk_Onecloud_trixie_current_6.12.43_minimal.burn.img`, keeping its
bootloader and kernel and replacing only the rootfs.

| File | Runs on | Does |
|---|---|---|
| `build-bundle.sh` | host | the same tree from this repository alone (launcher compiled, dbus-next and acme.sh downloaded with pinned hashes), no AppCDS cache: new boards such as the Phicomm N1 |
| `make-n1-image.sh` | Phicomm N1 | installs a bundle into the ophub trixie image in a chroot on an N1 and cleans it: a USB-bootable DashPlay image (`armbian-install` then copies it to the eMMC) |
| `make-bundle.sh` | host | `/opt/dashplay` tree from this repo + the reference box (launcher, bridge deps, jars with their AppCDS cache); refuses personal files; no acme.sh (the optional own name then reports that it cannot get a certificate) |
| `install.sh` | build box | the 4 packages and system changes that separate the working box from the base image (2026-10-11 diff), the services |
| `dashplay-firstboot.service` | firmware | new SSH host keys on the first boot |
| `prepare-image.sh --yes` | build box | wipes user data, logs, keys, machine-id; root/1234; re-arms Armbian's filesystem grow; zeroes free space |
| `pack-image.sh` | host | copies the root partition under fsfreeze, `e2fsck` + `resize2fs -M`, sparse, rebuilt burn image |
| `burnimg.py` | host | reads/rebuilds Amlogic v2 burn images (header CRC, per-partition sha1 VERIFY items, Android sparse) |
| `update-programs.py` | host | brings `/opt/dashplay` inside an image's root filesystem up to a bundle with debugfs, no box or root (below) |

The image also carries rtw88 rebuilt by `scripts/onecloud/wifi/build-rtw88-concurrent.sh` (station +
AP on one channel for the 8821C, 16 reserved pages) in `/lib/modules/…/updates/rtw88`, and blacklists
Realtek's vendor `8821cu`, which while merely loaded hides that combination. Pass its output directory
as `RTW88_MODULES` to `make-bundle.sh`.

Steps (build box = a OneCloud flashed with the base image, on Ethernet next to the host; the first
firmware, 2026-10-11, used the reference box itself after backing up its personal data, since it is
exactly base + install.sh's changes):

1. `RTW88_MODULES=DIR make-bundle.sh REFERENCE_BOX dashplay-bundle.tar [JUMP]`
2. copy the bundle to the build box, unpack `firmware/` from it, `sh firmware/install.sh dashplay-bundle.tar`
3. optional check boot; then `sh /opt/dashplay/firmware/prepare-image.sh --yes`
4. `pack-image.sh BUILD_BOX BASE.burn.img DashPlay_onecloud.burn.img [JUMP]`, then `xz -T0`
5. flash the result onto a box and walk through first-run setup, AI identity import and CarPlay.

Verified: rebuilding the base image with `burnimg.py` is byte-identical; its rootfs unpacks to a
1.2 GB ext4 and re-encodes losslessly. The first image (2026-10-11) was packed from the reference box
(steps 1, 3, 4); a fresh-box run of `install.sh` and a flash-and-walk-through of step 5 are still open.

Without a box (only program files changed since the image, i.e. the same `install.sh` and services; 2026-10-12
`20261012b` images were made this way, not yet booted): `build-bundle.sh` from a clean export of the commit
(`git archive`; it copies working-tree files), then take the root partition out (OneCloud: `burnimg.py extract IMAGE
rootfs X.sparse` + `unsparse`; N1: `dd bs=512 skip=1056768 count=6144000`), `update-programs.py ROOTFS BUNDLE_DIR`
to review and again with `--apply`, `e2fsck -fy -E discard`, and pack (OneCloud: `resize2fs -M`, truncate,
`burnimg.py sparse` + `replace` into the previous image, as `pack-image.sh` does; N1: `dd conv=notrunc` back).
Needs e2fsprogs 1.47+ (the N1 root uses orphan_file). The AppCDS cache is dropped and rebuilt on first boot.

Before publishing an image, rebuild it from the current commit (the bundle compares the box's jars with
this repository's build), and inspect the unpacked rootfs: `burnimg.py extract IMAGE rootfs X.sparse`,
`burnimg.py unsparse X.sparse X.ext4`, then `debugfs -R "rdump /opt /etc /root /var/log …"` and
`scripts/scan-sensitive.py` on the dump. Expect empty `identity/offline-mfi`, `state/` holding only
`hotspot.json` and the AppCDS cache, no `/etc/ssh/ssh_host_*`, an empty `/etc/machine-id`, empty logs.

## Firmware platform adapters

The default adapter stays Debian/Armbian; OpenWrt selects its own adapter. Firmware may set
`DASHPLAY_PLATFORM_MODULE` to a single Python module name in the main service environment and make it
importable through `PYTHONPATH`. The module defines `Platform(interface="", **paths)`, implements the
methods in `platform_api.Platform`, and may declare `PLATFORM_API_VERSION = 1`. Existing modules without
a declaration are API 1. Module import must not change hardware state; initialization belongs in the
constructor or lifecycle methods. Breaking interface changes require a new API version.

Keep firmware modules, configuration and service drop-ins outside the application directory. They are
not part of public program bundles or the previous-program archive. Program upgrade/rollback changes
the main service unit, retaining its external drop-ins. A program update does not update firmware.

The current Debian upgrader reads the main service's adapter selection independently of the process
that launched the upgrade. Before stopping the service, it requires the candidate loader/API and imports
the installed adapter against the candidate's dependencies without constructing it. Missing methods,
unsupported APIs and adapters supplied from inside the application/candidate directory are rejected.
This is an offline interface check, not proof of hardware behavior; normal post-install health checks and
automatic rollback still apply. Firmware configuration must remain stable during an upgrade.

A stopped service with EnvironmentFile settings must be started before checking; unreadable or changed
service settings stop the upgrade. Old releases without the loader remain installable on default-adapter
boxes, but are rejected on boxes selecting an external adapter. The retained version-selection tool also
needs this capability: reopen it from the updated settings page to refresh an older copy. A first upgrade
performed by an old tool still has that old tool's checks. Automatic recovery and `--rollback` restore the
known previous-program archive rather than downloading another release; they do not replace firmware.
