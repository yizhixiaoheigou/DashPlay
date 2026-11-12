#!/bin/sh
# Build dashplay-bundle.tar (the /opt/dashplay tree for install.sh) from this repository alone, without a
# reference box: for a new board (Phicomm N1) or any fresh install. make-bundle.sh is the OneCloud
# variant that also copies the AppCDS startup cache from a running box.
#   sh scripts/onecloud/firmware/build-bundle.sh OUT.tar
# Needs on the host: the receiver built (PlayPort patched, :server:installDist; see PLAYPORT below), javac 21, pip,
# curl, unzip. Downloads two pinned, hash-checked inputs: dbus-next 0.2.3 (the Bluetooth bridge's private
# dependency, MIT) and acme.sh 3.1.6 (the optional own name's certificate, own_domain.py; GPL-3.0).
set -eu
out=${1:?output tar}
repo=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
# The PlayPort checkout with the patches applied and :server:installDist run: PLAYPORT, or playport/ (the
# public README's layout), or research/playport (the private workspace).
playport=${PLAYPORT:-}
if [ -z "$playport" ]; then
    for candidate in "$repo/playport" "$repo/research/playport"; do
        [ -d "$candidate/server/build/install/server/lib" ] && { playport=$candidate; break; }
    done
fi
lib="$playport/server/build/install/server/lib"
ACME_VERSION=3.1.6
ACME_SHA256=c7d68b021cfd6380ea83a82962abde5b484779fee0b97d38681dfa1396bbc8d7
DUCKDNS_SHA256=8be535b8d6270b7a37671da954d89c896df7af29c2f38d5029054ec315fe77af
DESEC_SHA256=8e31d236594fd50d6629f324c0dca8a6fed80b030bf6ae708fe23332e4055d6b
[ -s "$lib/server-0.1.0.jar" ] || { echo "build the receiver first (:server:installDist)" >&2; exit 1; }
stage=$(mktemp -d "${TMPDIR:-/tmp}/dashplay-bundle.XXXXXX")
work=$(mktemp -d "${TMPDIR:-/tmp}/dashplay-build.XXXXXX")
trap 'rm -rf "$stage" "$work"' EXIT
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

# Program and service.
# Committed files only: work in progress in the tree must not ship.
for file in $(git -C "$repo" ls-files 'scripts/onecloud/*.py') scripts/onecloud/dashplay.service scripts/onecloud/tls.security \
            scripts/onecloud/update-key.pem; do   # update.py checks release manifests with this public key
    case "$file" in scripts/onecloud/provision.py|scripts/onecloud/*/*) continue ;; esac
    cp "$repo/$file" "$stage/"
done
mkdir -p "$stage/playport/server/build/install/server" "$stage/playport/web" "$stage/identity/offline-mfi" \
    "$stage/firmware" "$stage/state/receiver" "$stage/launcher" "$stage/linux-bt-bridge/deps" "$stage/acme/dnsapi"
cp -R "$lib" "$stage/playport/server/build/install/server/lib"
cp -R "$playport/web/dist" "$stage/playport/web/dist"
rm -rf "$stage/playport/web/dist/receiver-access.js" "$stage/playport/web/dist/receiver-access.json" "$stage/playport/web/dist/experiments"   # private tablet tests

# Launcher: warms the handshake crypto once AirPlay listens (docs/玩客云移植预研.md).
javac --release 21 -cp "$lib/*" -d "$work/launcher" "$repo/scripts/onecloud/launcher/onecloud/Launcher.java"
jar --create --file "$stage/launcher/onecloud-launcher.jar" -C "$work/launcher" .

# Bluetooth bridge with its private dbus-next (never installed into the system Python).
for file in bt-bridge bridge.py relay.py probe_adapter.py; do cp "$repo/linux/bt-bridge/$file" "$stage/linux-bt-bridge/"; done
chmod 755 "$stage/linux-bt-bridge/bt-bridge"
python3 -m pip download --quiet --no-deps --only-binary=:all: --require-hashes \
    -r "$repo/linux/bt-bridge/requirements.txt" -d "$work/wheels"
unzip -q "$work"/wheels/dbus_next-0.2.3-*.whl 'dbus_next/*' -d "$stage/linux-bt-bridge/deps"
cp "$repo/licenses/dbus-next-0.2.3-MIT.txt" "$stage/linux-bt-bridge/deps/dbus_next-LICENSE.txt"

# acme.sh, pinned: the main script and the DuckDNS/deSEC hooks (own_domain.py).
curl -fsSL -o "$work/acme.tar.gz" "https://github.com/acmesh-official/acme.sh/archive/refs/tags/$ACME_VERSION.tar.gz"
tar -xzf "$work/acme.tar.gz" -C "$work"
cp "$work/acme.sh-$ACME_VERSION/acme.sh" "$stage/acme/acme.sh"
cp "$work/acme.sh-$ACME_VERSION/dnsapi/dns_duckdns.sh" "$stage/acme/dnsapi/dns_duckdns.sh"
cp "$repo/scripts/onecloud/acme/dnsapi/dns_desec.sh" "$stage/acme/dnsapi/dns_desec.sh"
cp "$repo/scripts/onecloud/acme/dnsapi/dns_dp.sh" "$repo/scripts/onecloud/acme/dnsapi/dns_dynv6.sh" "$stage/acme/dnsapi/"
cp "$work/acme.sh-$ACME_VERSION/LICENSE.md" "$stage/acme/LICENSE.md"
[ "$(sha256 "$stage/acme/acme.sh")" = "$ACME_SHA256" ] || { echo "acme.sh hash mismatch" >&2; exit 1; }
[ "$(sha256 "$stage/acme/dnsapi/dns_duckdns.sh")" = "$DUCKDNS_SHA256" ] || { echo "dns_duckdns.sh hash mismatch" >&2; exit 1; }
[ "$(sha256 "$stage/acme/dnsapi/dns_desec.sh")" = "$DESEC_SHA256" ] || { echo "dns_desec.sh hash mismatch" >&2; exit 1; }
chmod 755 "$stage/acme/acme.sh"

cp "$repo"/scripts/onecloud/firmware/install.sh "$repo"/scripts/onecloud/firmware/prepare-image.sh \
   "$repo"/scripts/onecloud/firmware/dashplay-firstboot.service "$repo"/scripts/onecloud/firmware/upgrade.py \
   "$repo/scripts/onecloud/board-probe.sh" "$stage/firmware/"
printf '{\n  "interface": "auto",\n  "address": "192.168.78.1/24",\n  "channel": 149,\n  "width": 80\n}\n' > "$stage/service.json"
printf '{"ssid": "DashPlay"}\n' > "$stage/state/hotspot.json"
# The release this bundle is (prepare-release.sh sets it): the settings page and the diagnostics show it.
[ -z "${DASHPLAY_VERSION:-}" ] || printf '%s\n' "$DASHPLAY_VERSION" > "$stage/version.txt"
find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +

# Nothing personal may ship: no identity, pairing, token, Wi-Fi password or certificate key.
leaks=$(cd "$stage" && find . \( -name '*.pk8' -o -name '*.p7b' -o -name '*.p12' -o -name '*.key' -o -name 'browser-token' \
    -o -name 'airplay-identity.properties' -o -name 'pairings.properties' -o -name 'car-hotspot.json' -o -name 'ddns.json' \
    -o -name 'account.conf' -o -name 'lockdown-pair-record*' \) -print)
[ -z "$leaks" ] || { echo "refusing to bundle personal files: $leaks" >&2; exit 1; }
# macOS tar would add Apple extended attributes that GNU tar on the box warns about.
xattrs=""
tar --version 2>/dev/null | grep -q bsdtar && xattrs="--no-xattrs --no-mac-metadata"
COPYFILE_DISABLE=1 tar $xattrs -C "$stage" -cpf "$out" .
echo "bundle $out: $(tar -tf "$out" | wc -l | tr -d ' ') entries, $(du -h "$out" | cut -f1)"
