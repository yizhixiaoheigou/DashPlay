"""First-run setup: choose how the box is used, set up its network, then the iPhone.

Three ways of use (user decision 2026-10-12, docs/三种使用方式实施计划.md), state/setup.json "mode":
- "car_hotspot": the box joins the car hotspot, which the head unit's browser uses. The iPhone uses
  the USB cable only: the car hotspot relayed iPhone-to-box video at 0.12 Mbit/s (Xpeng, 2026-10-12).
- "mifi": the box, the head unit and the iPhone all join a portable Wi-Fi (MiFi); iPhone on the cable
  if plugged in, otherwise wireless through the MiFi.
- "box_hotspot": the box raises its own hotspot and the head unit joins it (the head unit then has no
  internet); iPhone on the cable or wireless on the box hotspot.
state/setup.json: {"done": bool, "mode": ..., "car": <ssid being set up>, "failed": reason, "box": bool}.
Steps: mode -> car (car_hotspot/mifi: pick the hotspot) or box (box_hotspot: name/password of the box
hotspot) -> phone -> done. Until that network step is done the box hotspot is open (no password) and
DNS answers every name with the box, so a phone joins without a password and opens this page by
itself (captive portal). The daily box hotspot is always WPA2 and never captive. Older files migrate:
a box with a saved car hotspot becomes "car_hotspot".
"""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time

MDNS_NAME = "dashplay.local"
SCAN_CACHE_SECONDS = 20
# One scan with the box hotspot up misses hotspots, a different few each time (2026-10-12, N1: 9, 8, 11 and 11
# names in four scans, 12 together; the OneCloud listed 7-10). The list adds scans up and keeps a hotspot until
# it was not seen for this long.
SCAN_KEEP_SECONDS = 120


# Tesla's browser opens no private address (10/8, 172.16/12, 192.168/16), by name or not, and its Wi-Fi does
# not use IPv6 there; the shared address space 100.64.0.0/10 (RFC 6598) is not private, and a LAN numbered
# 100.96.0.1/24 opens (a friend's F50, 2026-10-14). OpenWrt moves its LAN there (shared_lan), the box its own
# hotspot (Tesla mode, box_hotspot_address); a joined hotspot is numbered by its owner.
SHARED_LAN = ipaddress.IPv4Interface("100.96.0.1/24")
SHARED_SPACE = ipaddress.IPv4Network("100.64.0.0/10")


# F2, Tesla drive 2026-10-15: connman alone is insufficient. The China car asked for the .cn connman name and the
# next three; the others (2026-10-16, from a user's working fix) are the overseas firmware's connman name, baidu.com
# and the Chromium/Android check, answered in case a car or browser asks for them.
CONNMAN_NAMES = ("connman.vn.cloud.tesla.cn", "connman.vn.tesla.services")
TESLA_CHECK_NAMES = (CONNMAN_NAMES[0], "www.baidu.cn", "www.qq.com", "www.tesla.cn",
                     CONNMAN_NAMES[1], "www.baidu.com", "connectivitycheck.gstatic.com")
# The real connman servers' reply (2026-10-16: .services 200 with this body; .cn a 403 that still carries the
# status header, so ConnMan reads only the header). X-Cache is copied from them.
CONNMAN_BODY = b"<html>\n<head>\n</head>\n<body>\n</body>\n</html>\n\n"
CONNMAN_HEADERS = {"X-ConnMan-Status": "online", "X-Cache": "Hit from cloudfront"}


def options(state):
    try:
        value = json.loads((Path(state) / "options.json").read_text())
    except (OSError, ValueError):
        value = {}
    value = dict(value) if isinstance(value, dict) else {}
    # usb_tether (2026-10-30): the iPhone on the CarPlay cable shares its mobile data (platform_debian.enable_usb_tether).
    if value.get("car_internet") not in ("none", "phone_hotspot", "usb_tether"):
        value["car_internet"] = "none"
    # The identity is for explanatory text; both phones may use wireless CarPlay.
    if value.get("uplink_phone") not in ("same", "other"):
        value["uplink_phone"] = "same"
    return value


def answers_own_name(chosen, ddns):
    """Whether the box answers its own domain on its own hotspot (supervisor.hotspot_names, phone_uplink). On unless
    the owner turned it off ("own_name_on_hotspot": false, settings page, 2026-10-29); always for Tesla mode and for a
    deSEC name. The older "local_dns" is not read: older pages saved it false with every car or sound group."""
    if (ddns or {}).get("provider") == "desec" or chosen.get("tesla"):
        return True
    return chosen.get("own_name_on_hotspot") is not False


def tesla_offline(setup, chosen, online=False):
    return chosen.get("tesla") is True and setup.get("mode") == "box_hotspot" and not online


def check_name(host):
    name = host.split(":", 1)[0].lower().rstrip(".")
    return name if name in TESLA_CHECK_NAMES else None


def check_response(host, path="/"):
    """(status, body, headers) for a connectivity check, or None when host is not a check name."""
    name = check_name(host)
    if name in CONNMAN_NAMES:
        return 200, CONNMAN_BODY, dict(CONNMAN_HEADERS)
    if name == "connectivitycheck.gstatic.com" and path.split("?")[0] in ("/generate_204", "/gen_204"):
        return 204, b"", {}
    if name:
        return 200, b"<html><body>online</body></html>\n", {}
    return None


# The phones' and computers' own connectivity checks, answered "online" for a phone that was shown the setup portal
# (settings_page.portal_reply). Android makers' names vary (gstatic, miui, hicloud, vivo...); all ask for /generate_204.
APPLE_CHECK_NAMES = ("captive.apple.com", "www.apple.com", "www.appleiphonecell.com", "www.itools.info", "www.ibook.info",
                     "www.airport.us", "www.thinkdifferent.us")
APPLE_SUCCESS = b"<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>"


def online_reply(name, path):
    """(status, body, content type, headers) of a connectivity check's "online" answer, or None for another request."""
    path = path.split("?")[0]
    if path in ("/generate_204", "/gen_204"):
        return 204, b"", "text/plain", {}
    if name in APPLE_CHECK_NAMES or path in ("/hotspot-detect.html", "/library/test/success.html"):
        return 200, APPLE_SUCCESS, "text/html", {}
    if path == "/connecttest.txt":
        return 200, b"Microsoft Connect Test", "text/plain", {}
    if path == "/ncsi.txt":
        return 200, b"Microsoft NCSI", "text/plain", {}
    if name == "detectportal.firefox.com":
        return 200, b"success\n", "text/plain", {}
    return None


def box_hotspot_address(usual, tesla, local_addresses):
    """The box hotspot's address and why it is not the one asked for (None when it is): SHARED_LAN in Tesla mode,
    unless another interface (the Ethernet at home, say) already has an address in that network."""
    if not tesla:
        return usual, None
    # F13: a USB MiFi can occupy 100.96. Keep every interface working by choosing
    # another /24 in shared space, never a private fallback (design 5.5, alternative 2).
    occupied = [a.network for a in local_addresses if a.version == 4 and a.ip != SHARED_LAN.ip]
    candidates = [SHARED_LAN] + [ipaddress.IPv4Interface(f"100.{n}.0.1/24") for n in range(97, 128)]
    for candidate in candidates:
        if not any(candidate.network.overlaps(network) for network in occupied):
            warning = None if candidate == SHARED_LAN else f"100.96 网段被占用（{", ".join(str(a) for a in local_addresses)}），热点改用 {candidate.ip}；请重新连接热点"
            return candidate, warning
    raise RuntimeError("没有可用的特斯拉共享网段；保留域名记录，请断开冲突的上游")


def tesla_reaches(address):
    """Whether a Tesla's browser opens this box address (not a private one)."""
    try:
        return ipaddress.IPv4Address(address) in SHARED_SPACE
    except ValueError:
        return False


MODES = ("car_hotspot", "mifi", "box_hotspot")
JOINS = ("car_hotspot", "mifi")   # modes in which the box joins a hotspot (state/car-hotspot.json)


def load(state: Path):
    try:
        value = json.loads((state / "setup.json").read_text())
    except (OSError, ValueError):
        value = None
    car_saved = (state / "car-hotspot.json").exists()
    if not isinstance(value, dict):
        # Installed before the wizard: set up exactly when a car hotspot is saved.
        value = {"done": car_saved, **({} if car_saved else {"wizard_version": 2})}
    value = {key: item for key, item in value.items() if key not in ("role", "hotspot")}
    if value.get("car") == "skip":   # the old "use the box hotspot" choice
        value.pop("car")
    if value.get("mode") not in MODES:
        value.pop("mode", None)
        # Before 2026-10-12 there was one way of use: the box joined the car hotspot.
        if car_saved or value.get("car"):
            value["mode"] = "car_hotspot"
    if value.get("iphone") not in ("any", "wired", "wireless"):
        value["iphone"] = "wired" if value.get("mode") == "car_hotspot" else "any"
    return value


_SAVING = threading.Lock()


def save_json(path: Path, value):
    """Replace a small state file (0600) so that a power cut leaves the old or the new content. The settings page
    saves through here too (settings_page._save): requests run in threads, so one save at a time, each with its own
    temporary file (a double-click on save wrote both into one .tmp and failed the second rename; review 2026-10-12)."""
    with _SAVING:
        fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
            out.flush()
            os.fsync(out.fileno())
        Path(name).replace(path)
        os.sync()   # the root fs commits only every 120 s; saved settings must survive switching the car off


def save(state: Path, value):
    save_json(state / "setup.json", value)


def step(setup, network, draft=None):
    """mode -> car (box joins a hotspot) or box (box hotspot) -> phone -> done.
    network: "sta" once the box has joined the chosen hotspot, else "ap"/"starting"."""
    if setup.get("done"):
        return "done"
    if setup.get("wizard_version") == 2 and not setup.get("confirmed"):
        if draft is None:
            return "vehicle"
        from setup_flow import sequence
        return next((s for s in sequence(draft) if s not in draft.get("answered", [])), "summary")
    mode = setup.get("mode")
    if mode not in MODES:
        return "mode"
    if mode == "box_hotspot":
        return "phone" if setup.get("box") else "box"
    return "phone" if network == "sta" else "car"


def open_hotspot(setup):
    """The box hotspot has no password only until the network step of first-run setup is done."""
    return step(setup, "ap") in ("vehicle", "mode", "car", "box")


def captive(setup, network):
    """Hijack DNS only on the box hotspot and only while it is open."""
    return network == "ap" and open_hotspot(setup)


def direct_phone(chosen, capable, mode):
    """Whether the iPhone joins the box's own hotspot beside the joined car hotspot or MiFi (design 5.2a), so the
    picture does not go through the car's Wi-Fi. capable: the box can be a station and an AP at once (supervisor
    direct_capable; an N1 only on the Cypress firmware). state/options.json "direct_phone" decides when set; else on
    for the car hotspot, where the only other way is the cable, and off for a MiFi, whose wireless way works as it is."""
    if not capable or mode not in JOINS:
        return False
    value = (chosen or {}).get("direct_phone")
    return value is True if value is not None else mode == "car_hotspot"


def wireless(setup, chosen=None, capable=False):
    """Wireless CarPlay for this setup. The car hotspot carries it only through the direct hotspot; a MiFi or the box
    hotspot always. (2026-10-15 home test: the same iPhone can host the uplink AND join ap0 for wireless CarPlay.)"""
    if setup.get("iphone") == "wired":
        return False
    return setup.get("mode") != "car_hotspot" or direct_phone(chosen, capable, "car_hotspot")


from platform_api import get_platform


def wifi_interface(net=Path("/sys/class/net")):
    return get_platform().wifi_interface(net)


def security(scan_text, ssid):
    """How the car hotspot is secured, for the Wi-Fi handed to the iPhone: open | wpa2 | wpa3-transition | wpa3."""
    best = None
    for block in re.split(r"^BSS ", scan_text, flags=re.M)[1:]:
        name = re.search(r"^\s*SSID: ?(.*)$", block, re.M)
        if not name or _unescape_ssid(name.group(1)) != ssid:
            continue
        suites = " ".join(re.findall(r"Authentication suites: (.*)", block))
        if "SAE" in suites:
            found = "wpa3-transition" if "PSK" in suites else "wpa3"
        elif suites or "WPA:" in block or "RSN:" in block:
            found = "wpa2"
        else:
            found = "open"
        if "associated" in block.split("\n", 1)[0] or best is None:
            best = found
    return best


def handoff_security(found):
    """Security announced to the iPhone. WPA2/WPA3 transition is announced as WPA2: announced as
    transition (iAP2 value 3), iPhone XS accepted the invitation three times and never joined
    (2026-10-11), while WPA2 always worked; iOS then only warns about the mode when joined by hand."""
    return {"wpa3-transition": "wpa2", None: "wpa2"}.get(found, found)


def _unescape_ssid(text):
    """iw prints non-printable SSID bytes as \\xNN (UTF-8 names arrive that way)."""
    raw = bytearray()
    index = 0
    while index < len(text):
        if text.startswith("\\x", index) and re.fullmatch(r"[0-9a-fA-F]{2}", text[index + 2:index + 4]):
            raw.append(int(text[index + 2:index + 4], 16))
            index += 4
        elif text.startswith("\\\\", index):
            raw.append(0x5c)
            index += 2
        else:
            raw.extend(text[index].encode())
            index += 1
    return raw.decode("utf-8", errors="replace")


def parse_scan(text, own_ssid=None):
    """Strongest entry per visible SSID, strongest first: [{ssid, signal, band}]."""
    best = {}
    for block in re.split(r"^BSS ", text, flags=re.M)[1:]:
        ssid = re.search(r"^\s*SSID: ?(.*)$", block, re.M)
        signal = re.search(r"^\s*signal: (-?[\d.]+) dBm", block, re.M)
        freq = re.search(r"^\s*freq: ([\d.]+)", block, re.M)
        if not ssid or not signal:
            continue
        name = _unescape_ssid(ssid.group(1))
        if not name or name == own_ssid or "\x00" in name:
            continue
        entry = {"ssid": name, "signal": round(float(signal.group(1))),
                 "band": "5 GHz" if freq and float(freq.group(1)) > 3000 else "2.4 GHz"}
        if name not in best or entry["signal"] > best[name]["signal"]:
            best[name] = entry
    return sorted(best.values(), key=lambda item: -item["signal"])


class Scanner:
    """On-demand scan; rtw88 scans off-channel briefly while keeping the box hotspot up. Not in the background:
    every scan takes the box hotspot off its channel for a moment, under the phone doing the setup."""

    def __init__(self, interface, own_ssid, mode, platform=None):
        self.platform = platform or get_platform(interface)
        self.interface, self.own_ssid, self.mode = interface, own_ssid, mode
        self.lock = threading.Lock()
        self.seen = {}   # ssid -> (entry, last seen)
        self.at = None
        self.busy = False   # a scan runs on its own thread (refresh)
        self.waited_for = False   # that scan is the first list or an asked-for one: the page shows "scanning"

    def scan_once(self):
        try:
            found = self.platform.scan_networks(self.own_ssid, ap=self.mode() == "ap")
        except (OSError, subprocess.TimeoutExpired):
            return   # keep what was just seen
        now = time.monotonic()
        for entry in found:
            self.seen[entry["ssid"]] = (entry, now)

    def results(self, fresh=False):
        with self.lock:
            now = time.monotonic()
            if self.at is None:
                self.scan_once()   # the first list: two scans, as one misses hotspots
                self.scan_once()
                self.at = time.monotonic()
            elif fresh or now - self.at >= SCAN_CACHE_SECONDS:
                self.scan_once()
                self.at = time.monotonic()
            self.seen = {ssid: kept for ssid, kept in self.seen.items() if self.at - kept[1] <= SCAN_KEEP_SECONDS}
            return self.sorted()

    def sorted(self):
        return sorted((entry for entry, _ in list(self.seen.values())), key=lambda item: -item["signal"])

    def refresh(self, fresh=False):
        """results() on its own thread. The first list is two scans, several seconds (N1, 2026-10-17: "next" to the
        network step took seconds); a page shows "scanning" meanwhile instead of waiting for it."""
        with self.lock:
            if self.busy:
                return
            self.busy, self.waited_for = True, fresh or self.at is None

        def run():
            try:
                self.results(fresh=fresh)
            finally:
                self.busy = False
        threading.Thread(target=run, name="wifi-scan", daemon=True).start()

    def listing(self):
        """The hotspots now, without waiting: None while the first or an asked-for scan runs (refresh). A list older
        than SCAN_CACHE_SECONDS is shown as it is while a new scan runs for the next page."""
        if self.at is None or self.busy and self.waited_for:
            if not self.busy:
                self.refresh()
            return None
        if time.monotonic() - self.at >= SCAN_CACHE_SECONDS:
            listed = self.sorted()
            self.refresh()
            return listed
        return self.sorted()


def join_failure(log_text):
    """Classify a failed join from the wpa_supplicant log without exposing any of it."""
    # WPA2: the 4-way handshake fails. WPA3/SAE (a transition hotspot is joined with SAE): the AP rejects
    # the SAE exchange with status 15 (2026-10-11, Xiaomi hotspot with a wrong password).
    if re.search(r"WRONG_KEY|pre-shared key may be incorrect|4-Way Handshake failed"
                 r"|AUTH-REJECT .*auth_type=3 .*status_code=15", log_text):
        return "wrong_password"
    if re.search(r"CTRL-EVENT-CONNECTED", log_text):
        return "no_address"
    return "not_found"


FAILURES = {
    "wrong_password": "密码不对，请重新输入。",
    "not_found": "没有找到这个热点。请确认热点已打开、名称正确，并且盒子离它不远。",
    "no_address": "连上了热点，但没有拿到网络地址。请重试。",
}
