#!/usr/bin/env python3
"""The update manifest of a release (docs/在线升级方案-20261019.md), written by scripts/prepare-release.sh:

    python3 scripts/make-update-manifest.py VERSION CHANGES.md artifacts/release-VERSION [--owner yizhixiaoheigou] [--offline]
        [--android-apk PATH [--android-version VERSION] [--android-notes TEXT]]
    python3 scripts/make-update-manifest.py --check MANIFEST SHA256SUMS BUNDLE.sha256 VERSION   # verify-release.sh

Writes artifacts/release-VERSION/dashplay/dashplay-update.json: the version, the "what changed" lines, whether a
box may take it online (--offline when the release changes the system and needs the firmware images), and for
each file a box downloads its name, size and SHA-256: the OpenWrt packages (JRE per architecture) and the program
bundle with the SHA-256 of every program file in it (a box flashed from an image has no version: its files say
whether it already runs this release). publish-release.sh signs it on the Mac (dashplay-update.json.sig).

With --android-apk the manifest also gets an "android" section carrying the app's own version (0.3.0-beta.1). The app
only ever compares that version with its own; the box version (2026.10.31.3) belongs to the other channel and would
sort above every app version forever.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent / "onecloud/firmware"))
from upgrade import extract_bundle, program_files   # noqa: E402  the files an in-place upgrade replaces
sys.path.insert(0, str(Path(__file__).resolve().parent / "onecloud"))
from update import MIRRORS   # noqa: E402  boxes measure these; the signed list replaces an older box's own


APK_NAME = re.compile(r"^DashPlay-android-\d+\.\d+.*\.apk$")


def entry(path):
    data = Path(path).read_bytes()
    return {"file": Path(path).name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def one(directory, pattern):
    found = [p for p in sorted(Path(directory).iterdir()) if re.fullmatch(pattern, p.name)]
    if len(found) != 1:
        raise SystemExit(f"expected one file like {pattern} in {directory}, found {[p.name for p in found]}")
    return found[0]


def openwrt_dns_providers(staging):
    """Read the bundle's declared UCI capabilities without importing or executing its code."""
    source = Path(staging) / 'supervisor_openwrt.py'
    if source.is_file():
        for node in ast.parse(source.read_text()).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'DNS_PROVIDERS' for t in node.targets):
                providers = ast.literal_eval(node.value)
                if (not isinstance(providers, (tuple, list)) or not providers or
                        not all(isinstance(p, str) and re.fullmatch(r'[a-z][a-z0-9_]*', p) for p in providers)):
                    raise ValueError('invalid OpenWrt DNS provider declaration')
                return list(providers)
    return ['duckdns']


def manifest(version, notes, release, owner, online, android=None):
    openwrt, dashplay = Path(release) / "openwrt", Path(release) / "dashplay"
    packages = {name: entry(one(openwrt, rf"{re.escape(name)}-[0-9][^_]*\.apk"))
                for name in ("dashplay", "luci-app-dashplay", "luci-i18n-dashplay-zh-cn")}
    if not packages["dashplay"]["file"].startswith(f"dashplay-{version}-r"):
        raise SystemExit(f"{packages['dashplay']['file']} is not version {version}")
    jre = {}
    for path in sorted(openwrt.glob("dashplay-jre-*_*.apk")):
        match = re.fullmatch(r"dashplay-jre-(.+-r\d+)_(.+)\.apk", path.name)
        jre.setdefault("version", match.group(1))
        if match.group(1) != jre["version"]:
            raise SystemExit("JRE packages of different versions")
        jre.setdefault("files", {})[match.group(2)] = entry(path)
    if not jre:
        raise SystemExit(f"no JRE packages in {openwrt}")
    bundle = one(dashplay, rf"dashplay-bundle-{re.escape(version)}\.tar")
    with tempfile.TemporaryDirectory(prefix="dashplay-manifest.") as staging:
        extract_bundle(bundle, staging)
        files = {path: hashlib.sha256((Path(staging) / path).read_bytes()).hexdigest()
                 for path in sorted(program_files(staging)) if not (Path(staging) / path).is_symlink()}
        providers = openwrt_dns_providers(staging)
    value = {
        "format": 1, "version": version, "notes": notes, "online": online, "mirrors": list(MIRRORS),
        "compatibility": {"settings": 1},
        "openwrt": {"base": f"https://github.com/{owner}/openwrt-dashplay/releases/download/v{version}/",
                    "packages": packages, "jre": jre, "dns_providers": providers},
        "debian": {"base": f"https://github.com/{owner}/DashPlay/releases/download/v{version}/",
                   "bundle": entry(bundle), "files": files},
    }
    if android:
        version_name, apk_path, app_notes = android
        if not APK_NAME.search(Path(apk_path).name):
            raise SystemExit(f"{Path(apk_path).name}: the app APK must be named DashPlay-android-<version>.apk")
        value["android"] = {
            "version": version_name,
            "notes": app_notes or notes,
            "base": f"https://github.com/{owner}/DashPlay/releases/download/v{version}/",
            "apk": entry(apk_path),
        }
    return value


def check_android(manifest, release_url):
    """An android section must name an APK that the release page really carries, with the manifest's SHA-256.

    Returns None when the manifest has no android section, else True/False.
    """
    android = manifest.get("android")
    if not isinstance(android, dict):
        return None
    apk = android.get("apk") or {}
    name, want = apk.get("file"), apk.get("sha256")
    version = android.get("version")
    if not name or not want or not version:
        print("update manifest android section is incomplete", name or "?", version or "?")
        return False
    base = android.get("base") or release_url
    if not base.endswith("/"):
        base += "/"
    try:
        import urllib.request
        with urllib.request.urlopen(base + name, timeout=60) as response:
            got = hashlib.sha256(response.read()).hexdigest()
    except Exception as error:  # noqa: BLE001  a missing asset is the failure we are looking for
        print(f"android APK {name} is not published ({error})")
        return False
    ok = got == want
    print(f"android APK {name} ({version})", "ok" if ok else "SHA-256 MISMATCH")
    return ok


def check(manifest_path, sums_path, bundle_sha_path, version):
    """verify-release.sh: the published manifest names exactly the published files (after its signature checked)."""
    manifest = json.loads(Path(manifest_path).read_text())
    published = dict(reversed(line.split()) for line in Path(sums_path).read_text().splitlines() if line.strip())
    feed = manifest["openwrt"]
    entries = list(feed["packages"].values()) + list(feed["jre"]["files"].values())
    bad = [e["file"] for e in entries if published.get(e["file"]) != e["sha256"]]
    if manifest["debian"]["bundle"]["sha256"] != Path(bundle_sha_path).read_text().split()[0]:
        bad.append(manifest["debian"]["bundle"]["file"])
    ok = manifest["version"] == version and not bad
    print("update manifest", "ok" if ok else "NOT ok", f"(signed, {len(entries) + 1} files, online={manifest['online']})",
          " ".join(bad))
    android = check_android(manifest, manifest["debian"]["base"])
    if android is False:
        ok = False
    return ok


def main():
    if sys.argv[1:2] == ["--check"]:
        sys.exit(0 if check(*sys.argv[2:6]) else 1)
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("changes", type=Path)
    parser.add_argument("release", type=Path)
    parser.add_argument("--owner", default="yizhixiaoheigou")
    parser.add_argument("--offline", action="store_true", help="the release changes the system: images only")
    parser.add_argument("--android-apk", type=Path, help="the app APK to publish with this release")
    parser.add_argument("--android-version", help="the app version inside that APK (default: from its file name)")
    parser.add_argument("--android-notes", default="", help="what changed in the app, defaults to the release notes")
    args = parser.parse_args()
    android = None
    if args.android_apk:
        version_name = args.android_version or (APK_NAME.match(args.android_apk.name) and
                                                args.android_apk.name[len("DashPlay-android-"):-len(".apk")])
        if not version_name or not re.fullmatch(r"\d+(\.\d+)*(-[0-9A-Za-z.]+)?", version_name):
            raise SystemExit(f"cannot tell the app version from {args.android_apk.name}; pass --android-version")
        android = (version_name, args.android_apk, args.android_notes)
    value = manifest(args.version, args.changes.read_text().strip(), args.release, args.owner, not args.offline, android)
    out = args.release / "dashplay" / "dashplay-update.json"
    out.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    android_note = f", android {value['android']['version']} ({value['android']['apk']['file']})" if android else ""
    print(f"{out}: {args.version}, online={value['online']}, {len(value['debian']['files'])} program files, "
          f"JRE for {', '.join(value['openwrt']['jre']['files'])}{android_note}")


if __name__ == "__main__":
    main()
