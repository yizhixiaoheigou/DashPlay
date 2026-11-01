#!/usr/bin/env python3
"""The update manifest of a release (docs/在线升级方案-20261019.md), written by scripts/prepare-release.sh:

    python3 scripts/make-update-manifest.py VERSION CHANGES.md artifacts/release-VERSION [--owner yizhixiaoheigou] [--offline]
    python3 scripts/make-update-manifest.py --check MANIFEST SHA256SUMS BUNDLE.sha256 VERSION   # verify-release.sh

Writes artifacts/release-VERSION/dashplay/dashplay-update.json: the version, the "what changed" lines, whether a
box may take it online (--offline when the release changes the system and needs the firmware images), and for
each file a box downloads its name, size and SHA-256: the OpenWrt packages (JRE per architecture) and the program
bundle with the SHA-256 of every program file in it (a box flashed from an image has no version: its files say
whether it already runs this release). publish-release.sh signs it on the Mac (dashplay-update.json.sig).
"""
import argparse
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


def entry(path):
    data = Path(path).read_bytes()
    return {"file": Path(path).name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def one(directory, pattern):
    found = [p for p in sorted(Path(directory).iterdir()) if re.fullmatch(pattern, p.name)]
    if len(found) != 1:
        raise SystemExit(f"expected one file like {pattern} in {directory}, found {[p.name for p in found]}")
    return found[0]


def manifest(version, notes, release, owner, online):
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
    return {
        "format": 1, "version": version, "notes": notes, "online": online, "mirrors": list(MIRRORS),
        "openwrt": {"base": f"https://github.com/{owner}/openwrt-dashplay/releases/download/v{version}/",
                    "packages": packages, "jre": jre},
        "debian": {"base": f"https://github.com/{owner}/DashPlay/releases/download/v{version}/",
                   "bundle": entry(bundle), "files": files},
    }


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
    args = parser.parse_args()
    value = manifest(args.version, args.changes.read_text().strip(), args.release, args.owner, not args.offline)
    out = args.release / "dashplay" / "dashplay-update.json"
    out.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n")
    print(f"{out}: {args.version}, online={value['online']}, {len(value['debian']['files'])} program files, "
          f"JRE for {', '.join(value['openwrt']['jre']['files'])}")


if __name__ == "__main__":
    main()
