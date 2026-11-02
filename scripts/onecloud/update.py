#!/usr/bin/env python3
"""Online update of DashPlay from its signed release manifest (docs/在线升级方案-20261019.md).

    update.py check   [--root /opt/dashplay]   # a newer release? -> state/update-check.json
    update.py launch  [--root /opt/dashplay]   # start `install` apart from the service, which it restarts
    update.py install [--root /opt/dashplay]   # download, verify, back up, install, check; go back on failure
    update.py speedtest [--root /opt/dashplay] # measure GitHub and every mirror -> state/update-sources.json

Every release carries dashplay-update.json and its Ed25519 signature. Only a manifest signed by the key in
update-key.pem (beside this file) counts, and every downloaded file must match it, so a mirror needs no trust.
OpenWrt: the dashplay, luci-app-dashplay and zh-cn packages (the JRE only when its version changed) with apk,
refusing a plan with any kernel, kmod or BlueZ package (the ZTE F50 runs a vendor 5.4 kernel); the packages of
the installed version are fetched first to go back to. OneCloud / N1: firmware/upgrade.py with the release's
program bundle (it keeps state/ and identity/ and restores the previous program by itself).
Never while CarPlay is in use, and never by itself: the boxes only check (once a day) and say so.
Progress goes to state/update-status.json, which the settings page and LuCI show (bytes, total, speed, source).
Downloads come from the fastest source: GitHub and every mirror download the first 256 KB at once, an install
always measures again, a check reuses a day-old order; a source that slows below 20 KB/s for 15 s or breaks off
hands over to the next, which resumes where it stopped.
"""
import argparse
import email.utils
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request

OWNER = "yizhixiaoheigou"
# Where the latest manifest and the installed version's OpenWrt packages (to go back to) come from. The environment
# may point both at a test server (scripts/openwrt/lab-test.sh); the signature check is the same.
LATEST = os.environ.get("DASHPLAY_UPDATE_LATEST", f"https://github.com/{OWNER}/DashPlay/releases/latest/download/")
RELEASE_FEED = os.environ.get("DASHPLAY_RELEASE_FEED", f"https://github.com/{OWNER}/DashPlay/releases/download/v{{version}}/")
RELEASE_LIST = f"https://api.github.com/repos/{OWNER}/DashPlay/releases?per_page=100"
SETTINGS_FORMAT = 1
HISTORY_MIN = (2026, 10, 29)
PREVIOUS_FEED = os.environ.get("DASHPLAY_UPDATE_FEED",
                               f"https://github.com/{OWNER}/openwrt-dashplay/releases/download/v{{version}}/")
VERSION_FILE = "version.txt"
MANIFEST = "dashplay-update.json"
SIGNATURE = MANIFEST + ".sig"
KEY = Path(__file__).resolve().with_name("update-key.pem")
OPENSSL = os.environ.get("DASHPLAY_OPENSSL", "openssl")   # OpenSSL 3 on the boxes (a Mac's own is LibreSSL)
# GitHub download mirrors, as URL prefixes. The signature makes any mirror safe. uci dashplay.main.update_mirror
# (OpenWrt) or state/options.json "update_mirror" (OneCloud / N1): empty or "auto" measures GitHub and these and
# takes the fastest, "none" is GitHub only, mirror prefixes are tried in that order and GitHub last. A signed
# manifest may bring a newer list ("mirrors"), kept in SOURCES_FILE, so a dead mirror is replaced by a release.
# Picked 2026-11-01 from 30 public ones by a 256 KB ranged download of a release bundle (all answered 206 in
# 2.6-3.6 s from the US; ghproxy.net, which the version before tried second, sent nothing in 10 s). Not yet measured
# from a network in China: the order matters little, since every box measures for itself.
MIRRORS = ("https://gh-proxy.com/", "https://gh-proxy.org/", "https://edgeone.gh-proxy.com/", "https://ghfast.top/",
           "https://gh.dpik.top/", "https://ghproxy.vip/", "https://tvv.tw/", "https://fastgit.cc/")
DIRECT = ""                    # the source "GitHub itself": no prefix
SOURCES_FILE = "state/update-sources.json"   # the measured order, the manifest's mirrors, the source used last
MEASURE_EVERY = 24 * 3600      # a measured order counts this long; an install always measures again
CONNECT_TIMEOUT = 15           # seconds for a connection or a read that gets nothing at all
SAMPLE = 256 << 10             # bytes per source when measuring
SAMPLE_SECONDS = 6             # all sources at once, so measuring takes at most this long
STALL_SECONDS, STALL_BYTES = 15, 300 << 10   # under 20 KB/s for 15 s: go on from the next source where it stopped
MIRROR_PATTERN = re.compile(r"https?://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~%/-]*)?")
OPENWRT_PACKAGES = ("dashplay", "luci-app-dashplay", "luci-i18n-dashplay-zh-cn")
FORBIDDEN = re.compile(r"^(kernel|kmod-|bluez)")   # never through an online update (ZTE F50: 5.4 kernel)
CHECK_EVERY = 24 * 3600
# What install.sh set up on a OneCloud / N1 comes with the firmware image, and an update replaces programs only. A
# programs-only release keeps these as in the images it links (prepare-release.sh checks), so a box whose own copies
# differ was flashed with older images: it needs the new images, not this update (a OneCloud from 2026-10-12).
SYSTEM_FILES = ("firmware/install.sh", "firmware/prepare-image.sh", "firmware/dashplay-firstboot.service",
                "dashplay.service")
BACKUP = "dashplay-update-backup.tar.gz"   # beside the program directory: the owner's data before the last update
WIRED_STATUS = Path('/var/run/dashplay/wired-status.json')


class UpdateError(Exception):
    """Why an update stops, in words for the owner (Chinese, shown on the page)."""


class NotFound(UpdateError):
    """Every source answered 404: the file is not published, not a network problem."""


class Slow(UpdateError):
    """A source sends, but too slowly to finish: the next one goes on from there."""


def is_openwrt():
    return Path("/etc/openwrt_release").exists()


def run(*args, timeout=300):
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)


def version_key(version):
    """'2026.10.19.1' -> (2026, 10, 19, 1); None for anything else (unknown versions are never newer)."""
    if not version or not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(part) for part in version.split("."))


def newer(latest, current):
    latest, current = version_key(latest), version_key(current)
    return latest is not None and (current is None or latest > current)


def setting(root, openwrt):
    """The owner's update_mirror: "" (automatic), "none" or mirror prefixes separated by spaces."""
    if openwrt:
        try:
            value = run("uci", "-q", "get", "dashplay.main.update_mirror", timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            value = ""
    else:
        try:
            value = str(json.loads((Path(root) / "state/options.json").read_text()).get("update_mirror", ""))
        except (OSError, ValueError, AttributeError):
            value = ""
    return "" if value.strip().lower() == "auto" else value.strip()


def mirror_list(value, https_only=False):
    """Mirror prefixes from a setting or a manifest, each ending in "/", or ValueError for anything else."""
    words = value.split() if isinstance(value, str) else value
    if not isinstance(words, (list, tuple)) or len(words) > 20:
        raise ValueError("镜像地址太多")
    out = []
    for word in words:
        if not isinstance(word, str) or len(word) > 200 or not MIRROR_PATTERN.fullmatch(word) \
                or (https_only and not word.startswith("https://")):
            raise ValueError(f"镜像地址不对：{str(word)[:60]}（要以 https:// 开头，例如 https://gh-proxy.com/）")
        word = word if word.endswith("/") else word + "/"
        if word not in out:
            out.append(word)
    return tuple(out)


def name_of(prefix):
    """How the page names a source: "GitHub" or the mirror's host."""
    return prefix.split("/")[2] if prefix else "GitHub"


def sources_state(root):
    return read(Path(root) / SOURCES_FILE)


def keep(root, **values):
    save(Path(root) / SOURCES_FILE, dict(sources_state(root), **values))


def candidates(root):
    """The mirrors to measure: the latest signed manifest's list, else the built-in one."""
    try:
        return mirror_list(sources_state(root).get("mirrors") or (), https_only=True) or MIRRORS
    except ValueError:
        return MIRRORS


def measure(url, pool, size=None):
    """Download the first SAMPLE bytes of url from every source in pool at once; results fastest first, each
    {"source", "speed" (bytes/s)} or {"source", "error"}. An answer that is not the file (a block page) is an error."""
    results = {}

    def one(prefix):
        start = time.monotonic()
        try:
            request = urllib.request.Request(prefix + url, headers={"User-Agent": "DashPlay-update",
                                                                    "Range": f"bytes=0-{SAMPLE - 1}"})
            with urllib.request.urlopen(request, timeout=SAMPLE_SECONDS) as response:
                total = response.headers.get("Content-Range", "").rpartition("/")[2] if response.status == 206 \
                    else response.headers.get("Content-Length", "")
                if response.status not in (200, 206) or size and total.isdigit() and int(total) != size:
                    raise UpdateError(f"不是要下载的文件（HTTP {response.status}）")
                got = 0
                while got < SAMPLE and time.monotonic() - start < SAMPLE_SECONDS:
                    chunk = response.read1(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
            if not got:
                raise UpdateError("没有数据")
            results[prefix] = {"source": prefix, "speed": int(got / max(time.monotonic() - start, 0.001))}
        except Exception as error:   # whatever a source does, it only drops out of the ranking
            if hasattr(error, "close"):
                error.close()
            results[prefix] = {"source": prefix, "error": str(error)[:80] or type(error).__name__}

    threads = [threading.Thread(target=one, args=(prefix,), daemon=True) for prefix in pool]
    for thread in threads:
        thread.start()
    until = time.monotonic() + SAMPLE_SECONDS + 1
    for thread in threads:
        thread.join(max(until - time.monotonic(), 0))
    ranked = [results.get(prefix) or {"source": prefix, "error": "超时"} for prefix in pool]
    return sorted(ranked, key=lambda r: -r.get("speed", -1))


def sources(root, openwrt, url=None, fresh=False, size=None):
    """Source prefixes in the order to try them ("" is GitHub itself). Automatic: the measured order (measured again
    on url when it is older than a day, or always when fresh), sources that failed last; GitHub is one of them."""
    value = setting(root, openwrt)
    if value == "none":
        return (DIRECT,)
    if value:
        try:
            return mirror_list(value) + (DIRECT,)
        except ValueError:   # a hand-edited setting that is not one: as if empty
            pass
    pool = (DIRECT,) + candidates(root)
    if len(pool) == 1:
        return pool
    state = sources_state(root)
    ranking = state.get("ranking") if isinstance(state.get("ranking"), list) else []
    measured = state.get("measured_at") if isinstance(state.get("measured_at"), (int, float)) else 0
    known = {r.get("source") for r in ranking if isinstance(r, dict)}
    if url and (fresh or time.time() - measured > MEASURE_EVERY or not set(pool) <= known):
        ranking = measure(url, pool, size)
        keep(root, ranking=ranking, measured_at=int(time.time()), measuring=0)
    order = [r["source"] for r in ranking if isinstance(r, dict) and "speed" in r and r.get("source") in pool]
    return tuple(order + [prefix for prefix in pool if prefix not in order])


def transfer(candidate, target, limit, size, stall, report):
    """Append candidate to target, from where target ends (a Range request; a source that ignores it starts over).
    An answer of another length than size (a block page with 200) is refused before anything is written."""
    have = target.stat().st_size if target.exists() else 0
    headers = {"User-Agent": "DashPlay-update", **({"Range": f"bytes={have}-"} if have else {})}
    with urllib.request.urlopen(urllib.request.Request(candidate, headers=headers),
                                timeout=CONNECT_TIMEOUT) as response:
        if have and response.status != 206:
            report(-have)
            have = 0
        elif have and not response.headers.get("Content-Range", "").startswith(f"bytes {have}-"):
            raise UpdateError("续传位置不对")
        announced = (response.headers.get("Content-Range", "").rpartition("/")[2] if response.status == 206
                     else response.headers.get("Content-Length", ""))
        expected = int(announced) if announced.isdigit() and response.status == 206 else \
            have + int(announced) if announced.isdigit() else None
        if size is not None and expected is not None and expected != size:
            raise UpdateError(f"不是要下载的文件（{expected} 字节）")
        window, window_bytes = time.monotonic(), 0
        with open(target, "ab" if have else "wb") as out:
            while True:
                chunk = response.read1(1 << 16)
                if not chunk:
                    break
                have += len(chunk)
                if have > limit:
                    out.truncate(0)
                    raise UpdateError("下载的文件比预期大，已停止")
                out.write(chunk)
                report(len(chunk))
                window_bytes += len(chunk)
                if time.monotonic() - window >= STALL_SECONDS:
                    if stall and window_bytes < STALL_BYTES:
                        raise Slow(f"太慢（{int(window_bytes / STALL_SECONDS) // 1024} KB/s）")
                    window, window_bytes = time.monotonic(), 0
    if expected is not None and have < expected:
        raise UpdateError("连接中断，没有下载完")   # the next source goes on from here
    return have


def fetch(url, prefixes=(DIRECT,), limit=1 << 20, target=None, sha256=None, size=None, report=None):
    """Bytes of url (or, with target, the file written there) from the first source prefix that delivers it ("" is
    GitHub itself). A source that sends too slowly or breaks off hands over to the next, which goes on from the byte
    where it stopped; when every source was too slow, they are tried once more without a speed limit. With sha256
    and size the download must match them exactly. report(bytes, source) follows the download (bytes < 0: started
    over)."""
    keep_bytes = target is None
    if keep_bytes:
        handle, name = tempfile.mkstemp(prefix="dashplay-fetch.")
        os.close(handle)
        work = Path(name)
    else:
        work = Path(target)
        work.unlink(missing_ok=True)
    errors, codes, slow = [], [], []
    try:
        for patient, pool in ((False, tuple(prefixes)), (True, None)):
            for prefix in (pool if pool is not None else tuple(slow)):
                try:
                    total = transfer(prefix + url, work, size if size is not None else limit, size, not patient,
                                     lambda n, prefix=prefix: report and report(n, prefix))
                    if size is not None and total < size:
                        raise UpdateError("没有下载完")   # the next source goes on from here
                    if sha256 and (file_sha256(work) != sha256 or total != size):
                        work.write_bytes(b"")
                        raise UpdateError("下载的文件与签名清单不符")
                    return work.read_bytes() if keep_bytes else work
                except (OSError, UpdateError, ValueError, http.client.HTTPException) as error:
                    if hasattr(error, "close"):   # an HTTPError holds its response open
                        error.close()
                    errors.append(f"{name_of(prefix)}: {error}")
                    codes.append(getattr(error, "code", None))
                    if isinstance(error, Slow) and not patient:
                        slow.append(prefix)
            if not slow:
                break
    finally:
        if keep_bytes:
            work.unlink(missing_ok=True)
    detail = "；".join(errors)[-300:]
    if all(code == 404 for code in codes):
        error = NotFound(f"下载失败（{detail}）")
    else:
        error = UpdateError(f"下载失败（{detail}）。网络恢复后再试，或在设置里点「测速」换一个下载源")
    error.detail = detail
    raise error


class Progress:
    """Bytes of one download step (several files) and the speed over the last 5 s, for state/update-status.json."""

    def __init__(self, status, phase, message, total=0):
        self.status, self.phase, self.message, self.total = status, phase, message, total
        self.done, self.samples, self.shown, self.source = 0, [], 0.0, DIRECT

    def __call__(self, n, source):
        now = time.monotonic()
        self.done, self.source = max(self.done + n, 0), source
        self.samples = [s for s in self.samples if now - s[0] <= 5] + [(now, self.done)]
        if now - self.shown >= 1:
            self.shown = now
            self.show()

    def speed(self):
        if len(self.samples) < 2 or self.samples[-1][0] - self.samples[0][0] < 0.5:
            return 0
        (t0, d0), (t1, d1) = self.samples[0], self.samples[-1]
        return max(int((d1 - d0) / (t1 - t0)), 0)

    def show(self):
        self.status(self.phase, self.message, done=self.done, total=self.total, speed=self.speed(),
                    source=name_of(self.source))


def verify(data, signature, key=KEY):
    """True when signature is the release key's Ed25519 signature of data (OpenSSL 3)."""
    with tempfile.TemporaryDirectory(prefix="dashplay-verify.") as work:
        (Path(work) / "m").write_bytes(data)
        (Path(work) / "s").write_bytes(signature)
        try:
            out = run(OPENSSL, "pkeyutl", "-verify", "-pubin", "-inkey", str(key), "-rawin",
                      "-in", str(Path(work) / "m"), "-sigfile", str(Path(work) / "s"), timeout=30)
        except FileNotFoundError:
            raise UpdateError("系统缺少 openssl，无法核对签名")
    return out.returncode == 0 and "Signature Verified Successfully" in out.stdout


def latest_manifest(root, openwrt, key=KEY, version=None):
    """The latest signed manifest. Its "mirrors", when valid, become the mirrors measured from then on."""
    # One source at a time: a mirror answering fast with a block page must not stop a check another source passes.
    if version is not None and not re.fullmatch(r'20\d{2}\.\d{2}\.\d{2}(?:\.\d+)?', version):
        raise UpdateError('版本号格式不正确')
    base = RELEASE_FEED.format(version=version) if version else LATEST
    errors, forged = [], False
    for prefix in sources(root, openwrt, base + MANIFEST):
        try:
            data = fetch(base + MANIFEST, (prefix,), limit=1 << 20)
            signature = fetch(base + SIGNATURE, (prefix,), limit=4096)
        except UpdateError as error:
            errors.append(error)
            continue
        if verify(data, signature, key):
            break
        forged = True
    else:
        if forged:
            raise UpdateError("更新清单的签名不对，已忽略（可能是镜像或网络被篡改）")
        why = "；".join(getattr(e, "detail", str(e)) for e in errors)[-300:]
        raise (NotFound if all(isinstance(e, NotFound) for e in errors) else UpdateError)(
            f"下载失败（{why}）。网络恢复后再试，或在设置里点「测速」换一个下载源")
    manifest = json.loads(data)
    if manifest.get("format") != 1 or not version_key(manifest.get("version")):
        raise UpdateError("更新清单格式不认识：请先手动升级到较新的版本")
    if version and manifest['version'] != version:
        raise UpdateError('下载的清单不是所选版本，已停止')
    try:
        listed = mirror_list(manifest["mirrors"], https_only=True) if "mirrors" in manifest else None
    except ValueError:
        listed = None   # a list this version cannot read: keep the one before
    keep(root, used=prefix, used_at=int(time.time()),
         **({"mirrors": list(listed)} if listed else {}))
    return manifest


def history(root, openwrt=None):
    """The list is discovery only; every chosen release is authenticated again before installation."""
    rows = json.loads(fetch(RELEASE_LIST, sources(root, bool(openwrt)), limit=2 << 20))
    if not isinstance(rows, list):
        raise UpdateError('暂时无法读取历史版本列表')
    versions = []
    for row in rows:
        version = str(row.get('tag_name', '')).removeprefix('v')
        if row.get('draft') or row.get('prerelease') or not version_key(version):
            continue
        assets = {a.get('name') for a in row.get('assets', [])}
        if version_key(version) >= HISTORY_MIN and {MANIFEST, SIGNATURE} <= assets:
            versions.append(version)
    return sorted(set(versions), key=version_key, reverse=True)


def check_target(root, openwrt, manifest):
    """Older releases used the same JSON settings. Future schema changes must opt into a new contract."""
    if version_key(manifest['version']) < HISTORY_MIN:
        raise UpdateError('这个历史版本尚未核对设置兼容性，不能保留设置切换')
    if manifest.get('compatibility', {}).get('settings', 1) != SETTINGS_FORMAT:
        raise UpdateError('所选版本的设置格式不兼容，已保留当前程序和设置')
    if not manifest.get('online'):
        raise UpdateError('所选版本需要更换系统，不能只切换程序')
    if not openwrt:
        files = manifest.get('debian', {}).get('files', {})
        if not all(name in files for name in SYSTEM_FILES) or not system_matches(root, files):
            raise UpdateError('所选版本与这台盒子的系统不兼容，已停止')
    elif apk_version('dashplay-jre') != manifest.get('openwrt', {}).get('jre', {}).get('version'):
        raise UpdateError('所选版本需要不同的 Java 运行环境，暂不支持保留系统回退')


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def program_matches(root, files):
    """The program under root has every file of the release ({relative path: sha256}) as released."""
    root = Path(root)
    return bool(files) and all((root / path).is_file() and file_sha256(root / path) == digest
                               for path, digest in files.items())


def system_matches(root, files):
    """The box's system files are the ones this release was made for (OneCloud / N1)."""
    return all((Path(root) / path).is_file() and file_sha256(Path(root) / path) == files[path]
               for path in SYSTEM_FILES if path in files)


def apk_version(name, listing=None):
    """'2026.10.19.1-r1' for an installed dashplay-2026.10.19.1-r1, or None. From the list of every installed
    package: `apk info -v NAME` prints the description instead (apk 3, OpenWrt 25.12)."""
    out = (listing if listing is not None else run("apk", "info", "-v", timeout=30).stdout).split()
    for word in out:
        if word.startswith(name + "-") and re.fullmatch(r"[0-9][^-]*-r\d+", word[len(name) + 1:]):
            return word[len(name) + 1:]
    return None


def bundled_version(root):
    """The release a program bundle was built for (build-bundle.sh writes version.txt for a release), or None
    for a development build and the releases before 2026.10.20."""
    try:
        return (Path(root) / VERSION_FILE).read_text().strip() or None
    except OSError:
        return None


def current_version(root, openwrt):
    if openwrt:
        full = apk_version("dashplay")
        return full.rsplit("-r", 1)[0] if full else None
    try:
        return bundled_version(root) or json.loads((Path(root) / "state/update.json").read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def compare(root, openwrt, manifest):
    """What the page shows about the installed program and this release."""
    current = current_version(root, openwrt)
    online, reason = bool(manifest.get("online")), "" if manifest.get("online") else "这一版改了系统"
    if openwrt:
        available = newer(manifest["version"], current)
    else:
        files = manifest.get("debian", {}).get("files", {})
        modified = not program_matches(root, files)
        # Known releases compare by version. Hashes still verify downloads and diagnose local changes;
        # they must not offer the same release repeatedly or offer an older public build.
        available = newer(manifest["version"], current) if version_key(current) else modified
        current = manifest["version"] if not version_key(current) and not modified else current
        if online and not system_matches(root, files):
            online, reason = False, "这台盒子的系统比这一版要求的旧"
    return {"current": current, "latest": manifest["version"], "notes": manifest.get("notes", ""),
            "available": available, "online": online, "reason": reason,
            "modified": modified if not openwrt else False}


def check(root, openwrt=None, key=KEY, manifest=None):
    """Compare the installed program with the latest signed release; write and return state/update-check.json."""
    root = Path(root)
    openwrt = is_openwrt() if openwrt is None else openwrt
    result = {"checked_at": int(time.time())}
    try:
        result.update(compare(root, openwrt, manifest or latest_manifest(root, openwrt, key)))
    except UpdateError as error:
        result["error"] = str(error)
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        result["error"] = f"检查更新失败：{error}"
    save(root / "state/update-check.json", result)
    return result


def carplay_in_use(root):
    # The presence helper's active field tracks wireless sessions only. USB has its own runtime status.
    return (bool(read(Path(root) / "state/receiver/phone-presence.json").get("active")) or
            read(WIRED_STATUS).get('phase') in ('starting', 'live'))


def forbidden_in_plan(simulation):
    """Packages an apk --simulate output would touch that an online update must not (kernel, kmod, BlueZ)."""
    names = re.findall(r"^\(\d+/\d+\) (?:Installing|Upgrading|Downgrading|Replacing|Purging) (\S+)", simulation, re.M)
    return sorted({name for name in names if FORBIDDEN.match(name)})


def previous_packages(version, prefixes, work, report=None):
    """The installed release's three packages (to go back to), checked against that release's SHA256SUMS."""
    base = PREVIOUS_FEED.format(version=version)
    try:
        sums = fetch(base + "SHA256SUMS", prefixes, limit=1 << 16).decode()
    except NotFound:
        raise UpdateError(f"找不到当前版本 {version} 的安装包（它不是正式发布的版本），升级失败时无法退回，已停止。"
                          "请按装机说明手动升级") from None
    files = []
    for line in sums.splitlines():
        digest, _, name = line.partition("  ")
        if any(re.fullmatch(rf"{re.escape(package)}-[0-9][^/]*\.apk", name) for package in OPENWRT_PACKAGES):
            target = Path(work) / "previous" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            fetch(base + name, prefixes, target=target, limit=256 << 20, report=report)   # the hash below decides
            if file_sha256(target) != digest.strip():
                raise UpdateError("当前版本的安装包校验不符，无法保证能退回，已停止")
            files.append(target)
    if len(files) != len(OPENWRT_PACKAGES):
        raise UpdateError(f"找不到当前版本 {version} 的安装包，升级失败时无法退回，已停止")
    return files


def settings_port():
    port = run("uci", "-q", "get", "dashplay.main.settings_port", timeout=5).stdout.strip()
    return int(port) if port.isdigit() else 18080


def service_up(port, seconds=150, steady=15):
    """The dashplay service runs and its settings page answers (it opens even while the Wi-Fi is not up), and still
    does `steady` seconds later: on the F50 it answered 2 s after apk returned, before a crash could show."""
    def answers():
        try:
            socket.create_connection(("127.0.0.1", port), 3).close()
            return True
        except OSError:
            return False

    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if answers():
            time.sleep(steady)
            if answers():
                return True
        time.sleep(3)
    return False


def backup(root, openwrt):
    """The owner's data (pairings, certificates, identity, settings), logs left out; only the latest is kept."""
    root = Path(root)
    target = root.parent / BACKUP
    with tarfile.open(target.with_suffix(".tmp"), "w:gz") as out:
        for name in ("state", "identity"):
            if (root / name).exists():
                out.add(root / name, arcname=name,
                        filter=lambda info: None if re.match(r"state/(logs|diag)(/|$)", info.name) else info)
        for config in ("/etc/config/dashplay", str(root / "service.json")):
            if Path(config).exists():
                out.add(config, arcname="config/" + Path(config).name)
    os.chmod(target.with_suffix(".tmp"), 0o600)
    os.replace(target.with_suffix(".tmp"), target)
    return target


def install_openwrt(root, manifest, status, work):
    current = current_version(root, True)
    if not current:
        raise UpdateError("没有找到已安装的 dashplay 软件包")
    arch = Path("/etc/apk/arch").read_text().strip()
    feed = manifest["openwrt"]
    wanted = {name: feed["packages"][name] for name in OPENWRT_PACKAGES}
    if apk_version("dashplay-jre") != feed["jre"]["version"]:
        if arch not in feed["jre"]["files"]:
            raise UpdateError(f"这一版没有 {arch} 的 Java 运行环境")
        wanted["dashplay-jre"] = feed["jre"]["files"][arch]
    biggest = max(wanted.values(), key=lambda entry: entry["size"])
    status("measuring", "正在测速，选最快的下载源")
    prefixes = sources(root, True, feed["base"] + biggest["file"], fresh=True, size=biggest["size"])
    # The way back first: without it nothing is installed, so the new release is not fetched for nothing.
    progress = Progress(status, "downloading", f"正在下载当前版本 {current} 的安装包（升级失败时用来退回）")
    previous = previous_packages(current, prefixes, work, progress)
    total = sum(entry["size"] for entry in wanted.values())
    progress = Progress(status, "downloading", f"正在下载 {manifest['version']}（{total // 1048576} MB）", total)
    progress.show()
    files = []
    for entry in wanted.values():
        files.append(fetch(feed["base"] + entry["file"], prefixes, target=Path(work) / entry["file"],
                           sha256=entry["sha256"], size=entry["size"], report=progress))
    progress.show()
    keep(root, used=progress.source, used_at=int(time.time()))
    status("checking", "正在核对升级计划")
    run("apk", "update", timeout=300)   # without the index the simulation misses dependencies (F50, 2026-10-19)
    direction = () if newer(current, manifest['version']) else ('--upgrade',)
    simulation = run("apk", "add", *direction, "--simulate", "--allow-untrusted", *map(str, files))
    if simulation.returncode != 0:
        raise UpdateError("升级计划不成立：" + (simulation.stderr or simulation.stdout).strip()[-300:])
    blocked = forbidden_in_plan(simulation.stdout)
    if blocked:
        raise UpdateError("升级会改动内核、内核模块或系统蓝牙（" + "、".join(blocked) + "），已停止，请按说明手动升级")
    status("installing", "正在安装，CarPlay 会断开约 1 分钟")
    port = settings_port()
    result = run("apk", "add", *direction, "--allow-untrusted", *map(str, files), timeout=600)
    if result.returncode == 0 and service_up(port):
        return
    status("restoring", "新版本没有正常启动，正在退回原来的版本")
    back = run("apk", "add", "--allow-untrusted", *map(str, previous), timeout=600)
    up = back.returncode == 0 and service_up(port)
    raise UpdateError(f"新版本没有正常启动，已退回 {current}" if up else
                      "新版本没有正常启动，退回也没有成功：请按说明用电脑恢复（数据备份在 /opt/" + BACKUP + "）")


def install_debian(root, manifest, status, work):
    entry = manifest["debian"]["bundle"]
    url = manifest["debian"]["base"] + entry["file"]
    status("measuring", "正在测速，选最快的下载源")
    prefixes = sources(root, False, url, fresh=True, size=entry["size"])
    progress = Progress(status, "downloading", f"正在下载 {manifest['version']}（{entry['size'] // 1048576} MB）",
                        entry["size"])
    progress.show()
    bundle = fetch(url, prefixes, target=Path(work) / entry["file"], sha256=entry["sha256"], size=entry["size"],
                   report=progress)
    progress.show()
    keep(root, used=progress.source, used_at=int(time.time()))
    status("installing", "正在安装，CarPlay 和盒子热点会断开约 1 分钟")
    # Downgrading must not hand control to an old installer that lacks today's preservation checks.
    retained = Path(root) / 'state/version-manager/upgrade.py'
    upgrader = retained if retained.is_file() else Path(root) / 'firmware/upgrade.py'
    result = run(sys.executable, str(upgrader), str(bundle), timeout=900)
    if result.returncode != 0:
        raise UpdateError("新版本没有正常启动，已自动退回原来的程序（详见 state/upgrade.log）")
    save(Path(root) / "state/update.json", {"version": manifest["version"], "at": int(time.time())})


def install(root, openwrt=None, key=KEY, version=None):
    root = Path(root)
    (root / 'state').mkdir(parents=True, exist_ok=True)
    with open(root / 'state/update.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 1
        return _install(root, openwrt, key, version)


def _install(root, openwrt=None, key=KEY, version=None):
    root = Path(root)
    openwrt = is_openwrt() if openwrt is None else openwrt
    status_file = root / "state/update-status.json"

    def status(phase, message, **extra):
        save(status_file, {"phase": phase, "message": message, "at": int(time.time()), **extra})

    status("checking", "正在核对所选版本" if version is not None else "正在检查最新版本")
    try:
        before = current_version(root, openwrt)
        manifest = latest_manifest(root, openwrt, key, version=version) if version is not None else latest_manifest(root, openwrt, key)
        if version is not None:
            check_target(root, openwrt, manifest)
        found = compare(root, openwrt, manifest)
        if version is None and not found.get("available"):
            status("done", f"已是最新版本 {manifest['version']}")
            return 0
        if not found.get("online"):
            raise UpdateError(f"{found.get('reason') or '这一版改了系统'}，不能在线升级 {manifest['version']}："
                              "请按装机说明用刷机包升级")
        if carplay_in_use(root):
            raise UpdateError("正在使用 CarPlay：停车、断开后再升级")
        if shutil.disk_usage(tempfile.gettempdir()).free < 200 << 20:
            raise UpdateError("临时空间不足 200 MB，无法下载")
        status("backup", "正在备份配对、证书和设置")
        saved = backup(root, openwrt)
        with tempfile.TemporaryDirectory(prefix="dashplay-update.") as work:
            (install_openwrt if openwrt else install_debian)(root, manifest, status, work)
        if before and before != manifest['version']:
            save(root / 'state/version-manager/previous.json', {'version': before, 'installed': manifest['version']})
        status("done", f"已安装 {manifest['version']}", version=manifest["version"], backup=str(saved))
        # A selected old release must not overwrite the latest-release indicator.
        if version is None:
            check(root, openwrt, key, manifest)
        return 0
    except UpdateError as error:
        status("failed", str(error))
    except Exception as error:   # whatever happened, the page must not keep saying "installing"
        status("failed", f"升级出错：{type(error).__name__}: {str(error)[-200:]}")
    return 1


def speedtest(root, openwrt=None, key=KEY):
    """The settings page's "测速": every source on the release's biggest download, whatever the setting; the result
    (state/update-sources.json) is also the automatic order. Returns that state."""
    root = Path(root)
    openwrt = is_openwrt() if openwrt is None else openwrt
    keep(root, measuring=int(time.time()), error="")
    try:
        manifest = latest_manifest(root, openwrt, key)
        if openwrt:
            packages = manifest["openwrt"]["packages"]
            entry, base = packages["dashplay"], manifest["openwrt"]["base"]
        else:
            entry, base = manifest["debian"]["bundle"], manifest["debian"]["base"]
        value = setting(root, openwrt)
        try:
            chosen = mirror_list(value) if value and value != "none" else ()
        except ValueError:
            chosen = ()
        pool = tuple(dict.fromkeys((DIRECT,) + chosen + candidates(root)))
        keep(root, ranking=measure(base + entry["file"], pool, entry["size"]), measured_at=int(time.time()),
             measuring=0)
    except UpdateError as error:
        keep(root, measuring=0, error=str(error))
    except Exception as error:   # the page must not keep saying "measuring"
        keep(root, measuring=0, error=f"测速出错：{type(error).__name__}: {str(error)[-200:]}")
    return sources_state(root)


def measuring(root):
    """A speed test started less than 2 minutes ago that has not finished."""
    started = sources_state(root).get("measuring")
    return isinstance(started, (int, float)) and 0 < time.time() - started < 120


def running(root):
    """An install started less than 20 minutes ago that has not finished."""
    state = read(Path(root) / "state/update-status.json")
    return state.get("phase") not in (None, "done", "failed") and time.time() - state.get("at", 0) < 1200


def launch(root, openwrt=None, version=None):
    """Start an install apart from the dashplay service (it restarts that service, and procd or systemd would end
    a child with it). Returns a message for the page. OpenWrt: a procd service made for this one run over ubus, not
    an init script, since installing a package starts every init script in it (default_postinst; the F50 began an
    update by itself when the package with one was installed, 2026-10-19)."""
    root = Path(root)
    openwrt = is_openwrt() if openwrt is None else openwrt
    if running(root):
        return "正在升级，请稍候"
    if carplay_in_use(root):
        return "正在使用 CarPlay：停车、断开后再升级"
    install = [sys.executable, str(Path(__file__).resolve()), "install", "--root", str(root)]
    if version is not None:
        if not re.fullmatch(r'20\d{2}\.\d{2}\.\d{2}(?:\.\d+)?', version):
            return '版本号格式不正确'
        install.extend(['--version', version])
    if openwrt:
        service = {"name": "dashplay-update", "instances": {"update": {"command": install, "stdout": True, "stderr": True}}}
        command = ["ubus", "call", "service", "set", json.dumps(service)]
    else:
        command = ["systemd-run", "--unit=dashplay-update", "--collect", *install]
    save(root / "state/update-status.json", {"phase": "starting", "message": "正在开始升级", "at": int(time.time())})
    try:
        if openwrt:   # the finished run before, if any: procd would not start an unchanged instance again
            run("ubus", "call", "service", "delete", json.dumps({"name": "dashplay-update"}), timeout=30)
        out = run(*command, timeout=30)
        why = "" if out.returncode == 0 else (out.stderr or out.stdout).strip()[-200:] or f"退出码 {out.returncode}"
    except (OSError, subprocess.SubprocessError) as error:   # e.g. a version without the update service
        why = str(error)
    if why:   # never leave "starting" behind: it would refuse every try for 20 minutes
        save(root / "state/update-status.json", {"phase": "failed", "message": "无法启动升级：" + why,
                                                 "at": int(time.time())})
        return "无法启动升级：" + why
    return "已开始升级，约 2–5 分钟，期间 CarPlay 会断开"


# Plain HTTP on purpose: its Date header needs no clock, while HTTPS fails on a clock off by months. A box without a
# battery-backed clock (OneCloud, N1) starts from its last saved time, and NTP may not get through where the update
# check's HTTPS does; DashPlay OS on 410 sticks has no timesyncd at all (it stalled every shutdown step 30 s, 2026-10-21).
CLOCK_URLS = ("http://www.baidu.com/", "http://connectivitycheck.gstatic.com/generate_204")
CLOCK_SLACK = 600


def clock_from_http(urls=CLOCK_URLS, now=time.time, set_clock=None, opener=urllib.request.urlopen):
    """Set the system clock from the first reachable server's Date header when it is off by more than 10 minutes.
    Returns the offset applied in seconds, 0 when the clock was right, None when no server answered."""
    for url in urls:
        try:
            with opener(urllib.request.Request(url, headers={"User-Agent": "DashPlay-clock"}), timeout=10) as response:
                date = response.headers.get("Date")
        except Exception:
            continue
        try:
            server = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            continue
        offset = server - now()
        if abs(offset) <= CLOCK_SLACK:
            return 0
        (set_clock or (lambda t: time.clock_settime(time.CLOCK_REALTIME, t)))(server)
        return offset
    return None


def checker(root, stopped, enabled=True, first=120):
    """Supervisor thread body: check once soon after start, then daily. Never installs."""
    if not enabled or stopped.wait(first):
        return
    while True:
        try:
            clock_from_http()
        except Exception:   # a wrong clock must not stop the check either
            pass
        try:
            check(root)
        except Exception:   # a check must never take the service down
            pass
        if stopped.wait(CHECK_EVERY):
            return


def main(argv=None):
    os.umask(0o077)   # status, check and backup files hold nothing for others; the backup holds keys
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["check", "launch", "install", "speedtest"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--version', help='explicitly selected release, including a compatible older version')
    args = parser.parse_args(argv)
    if args.command == "check":
        print(json.dumps(check(args.root), ensure_ascii=False))
        return 0
    if args.command == "speedtest":
        print(json.dumps(speedtest(args.root), ensure_ascii=False))
        return 0
    if args.command == "launch":
        print(launch(args.root, version=args.version))
        return 0
    return install(args.root, version=args.version)


if __name__ == "__main__":
    sys.exit(main())
