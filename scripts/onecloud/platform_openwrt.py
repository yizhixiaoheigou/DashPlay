"""OpenWrt adapter: the router owns the network (netifd, uci); DashPlay only reads it.

The person sets up Wi-Fi in LuCI as usual. DashPlay hands the iPhone one of the router's Wi-Fi
networks over Bluetooth (an access point by default, or the network the router joined as a client, or
another router's Wi-Fi bridged into its LAN, as entered) and serves the viewer on that network's address.
Nothing here creates hotspots or joins networks.

No operations run at import time; the parsers take command output so they are testable anywhere.
"""
import ipaddress
import json
from pathlib import Path
import re
import subprocess

from platform_debian import DebianPlatform, parse_stations

RUN = Path("/var/run/dashplay")


def _ubus(path, method="status"):
    """`ubus call path method` as a dict; {} when it fails or hangs: netifd may not answer for a while (an
    interface it cannot set up took 30 s in the OpenWrt container, 2026-10-19), which must not end the service."""
    try:
        out = subprocess.run(["ubus", "call", path, method], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    try:
        return json.loads(out.stdout or "{}")
    except ValueError:
        return {}


def handoff_security(encryption):
    """uci wireless encryption -> what the receiver tells the iPhone; None when it cannot be handed over.
    WPA2/WPA3 transition goes out as WPA2: with "WPA3 transition" the iPhone accepted the handoff but
    never joined (OneCloud, 2026-10-11)."""
    value = (encryption or "none").split("+")[0]
    if value == "none":
        return "open"
    if value in ("psk", "psk2", "psk-mixed", "sae-mixed"):
        return "wpa2"
    if value == "sae":
        return "wpa3"
    return None   # enterprise (wpa, wpa2, wpa3...) and owe: the iPhone cannot be handed these


def wifi_networks(status):
    """`ubus call network.wireless status` -> the configured Wi-Fi networks, up ones first, 5 GHz first."""
    found = []
    for radio, data in sorted((status or {}).items()):
        config = data.get("config") or {}
        band = config.get("band") or ("5g" if str(config.get("channel", "")).isdigit()
                                     and int(config["channel"]) > 14 else "2g")
        for entry in data.get("interfaces") or []:
            iface = entry.get("config") or {}
            networks = iface.get("network") or []
            if isinstance(networks, str):
                networks = networks.split()
            found.append({
                "section": entry.get("section", ""), "ifname": entry.get("ifname", ""), "radio": radio,
                "up": bool(data.get("up")) and bool(entry.get("ifname")), "band": band,
                "mode": iface.get("mode", ""), "ssid": iface.get("ssid", ""), "key": iface.get("key", ""),
                "encryption": iface.get("encryption", "none"), "network": networks[0] if networks else "",
                "channel": config.get("channel", ""),
            })
    rank = {"5g": 0, "6g": 1, "2g": 2}
    return sorted(found, key=lambda n: (not n["up"], n["mode"] != "ap", rank.get(n["band"], 3)))


def pick_network(networks, wanted="auto", live=True):
    """The network to hand the iPhone: the named uci section, or the first usable access point (5 GHz
    preferred), else a client network. Returns (network, reason when none fits)."""
    candidates = [n for n in networks if n["mode"] in ("ap", "sta")]
    if wanted and wanted != "auto":
        candidates = [n for n in candidates if n["section"] == wanted]
        if not candidates:   # ubus leaves a disabled wifi-iface out altogether
            return None, f"Wi-Fi section {wanted} not found or switched off"
    if not candidates:
        return None, "no Wi-Fi network is switched on (access point or client)"
    usable = [n for n in candidates if handoff_security(n["encryption"]) and n["ssid"]]
    if not usable:
        return None, "no Wi-Fi network with a password type the iPhone accepts (open, WPA2 or WPA3 personal)"
    up = [n for n in usable if (n["up"] or not live) and n["network"]]
    if not up:
        return None, f"Wi-Fi {usable[0]['ssid']} is not up yet"
    return up[0], ""


EXTERNAL_ENCRYPTION = {"open": "none", "wpa2": "psk2", "wpa3": "sae"}


def external_network(config):
    """uci dashplay.main.external_* -> (network, reason when unusable): the Wi-Fi of another router (a travel
    router given the uplink over USB or a cable and bridged into this router's LAN), handed to the iPhone as
    entered. Its radio is not ours, so the channel comes from the configuration: set a fixed one there."""
    ssid, key = config.get("external_ssid", ""), config.get("external_key", "")
    security = config.get("external_security", "wpa2")
    channel = config.get("external_channel", "")
    lan = config.get("external_network", "lan")
    if not 1 <= len(ssid.encode()) <= 32:
        return None, "the other router's Wi-Fi name is missing or longer than 32 bytes"
    if security not in EXTERNAL_ENCRYPTION:
        return None, "the other router's password type must be open, wpa2 or wpa3"
    if security != "open" and not (8 <= len(key) <= 63 or re.fullmatch(r"[0-9A-Fa-f]{64}", key)):
        return None, "the other router's Wi-Fi password must be 8 to 63 characters"
    if not channel.isdigit() or not 1 <= int(channel) <= 196:
        return None, "the other router's Wi-Fi channel is missing: set a fixed channel there and enter it"
    if not re.fullmatch(r"[A-Za-z0-9_]+", lan):
        return None, f"network {lan} for the other router's Wi-Fi is not an OpenWrt interface name"
    return {"section": "external", "ifname": "", "radio": "", "up": True, "band": "5g" if int(channel) > 14 else "2g",
            "mode": "external", "ssid": ssid, "key": "" if security == "open" else key,
            "encryption": EXTERNAL_ENCRYPTION[security], "network": lan, "channel": int(channel)}, ""


def parse_channel(iw_info):
    match = re.search(r"\bchannel (\d+)", iw_info or "")
    return int(match.group(1)) if match else None


def interface_address(status):
    """`ubus call network.interface.<name> status` -> (layer-3 device, IPv4 interface) or (None, None)."""
    addresses = (status or {}).get("ipv4-address") or []
    if not status or not status.get("up") or not addresses:
        return None, None
    first = addresses[0]
    return status.get("l3_device") or status.get("device"), ipaddress.IPv4Interface(
        f"{first['address']}/{first.get('mask', 32)}")


def zone_input(uci_firewall, network):
    """`uci show firewall` -> the input policy of the zone that holds `network` (None when no zone does)."""
    zones, kinds = {}, {}
    for line in (uci_firewall or "").splitlines():
        match = re.match(r"^firewall\.([^.=]+)=(\w+)$", line)
        if match:
            kinds[match.group(1)] = match.group(2)
        match = re.match(r"^firewall\.([^.=]+)\.(\w+)='?(.*?)'?$", line)
        if match:
            zones.setdefault(match.group(1), {})[match.group(2)] = match.group(3)
    for name, zone in zones.items():
        if kinds.get(name) == "zone" and network in re.split(r"'?\s+'?", zone.get("network", "")):
            return zone.get("input", "").upper() or None
    return None


def public_ipv6(addresses, routes, lan_device):
    """`ip -6 -o addr show scope global` and `ip -6 -o route show default` -> the router's own public IPv6
    address, or None. One on the LAN device first (a delegated prefix), else one on the device of an IPv6
    default route (a mobile modem: the F50's sipa_eth0 holds 2408:…, its LAN only a unique local prefix).
    The router answers its own address itself, so a head unit on its Wi-Fi opening the page there stays
    on the Wi-Fi, while a car browser that refuses private addresses sees a public one (F50, 2026-10-13).
    Temporary, deprecated or tentative addresses change or may not answer yet: skipped."""
    found = {}
    for line in (addresses or "").splitlines():
        match = re.match(r"^\d+:\s+([^\s@]+)\S*\s+inet6\s+([0-9a-fA-F:]+)/\d+\s+(.*)$", line)
        if not match or re.search(r"\b(temporary|deprecated|tentative|dadfailed)\b", match.group(3)):
            continue
        try:
            address = ipaddress.IPv6Address(match.group(2))
        except ValueError:
            continue
        if address.is_global:   # not unique local (fc00::/7), link-local or documentation
            found.setdefault(match.group(1), []).append(str(address))
    for device in [lan_device] + re.findall(r"\bdev (\S+)", routes or ""):
        if found.get(device):
            return found[device][0]
    return None


def hottest(zones=Path("/sys/class/thermal")):
    """The hottest readable thermal zone in degrees C, for the drive log (the F50 has some 25, a few unreadable)."""
    temps = []
    for zone in zones.glob("thermal_zone*"):
        try:
            temps.append(int((zone / "temp").read_text().strip()))
        except (OSError, ValueError):
            pass
    return round(max(temps) / 1000, 1) if temps else None


class OpenWrtPlatform(DebianPlatform):
    """Process handling, Bluetooth and addresses as on Debian; Wi-Fi is read from OpenWrt."""

    def __init__(self, interface="", run=RUN):
        super().__init__(interface, run=run)

    def handoff(self, wanted="auto", live=True, external=None):
        """(network dict with channel, address and device, reason when not ready).
        live=False: what the configuration says before the radio is up (the access point's configured
        channel, the LAN address), so that the receiver can start while the Wi-Fi comes up.
        wanted "external": another router's Wi-Fi from the dashplay options in `external` (external_network)."""
        if wanted == "external":
            network, reason = external_network(external or {})
        else:
            network, reason = pick_network(wifi_networks(_ubus("network.wireless")), wanted, live)
        if not network:
            return None, reason
        device, address = interface_address(_ubus(f"network.interface.{network['network']}"))
        if not address:
            return None, f"network {network['network']} for Wi-Fi {network['ssid']} has no IPv4 address yet"
        info = ""
        if live and network["ifname"]:
            try:   # like _ubus above: a stuck call must not take the waiting service down with it
                info = subprocess.run(["iw", "dev", network["ifname"], "info"], capture_output=True,
                                      text=True, timeout=5).stdout
            except (OSError, subprocess.SubprocessError):
                pass
        channel = parse_channel(info) or (int(network["channel"]) if str(network["channel"]).isdigit() else None)
        if not channel:
            return None, f"Wi-Fi {network['ssid']} has no channel yet"
        return dict(network, channel=channel, address=address, device=device,
                    security=handoff_security(network["encryption"])), ""

    @staticmethod
    def interface_network(name):
        """The IPv4 address of netifd interface `name` (e.g. the LAN), or None."""
        return interface_address(_ubus(f"network.interface.{name}"))[1]

    @staticmethod
    def zone_policy(network):
        """The module-level zone_input(): named apart so the one-argument method cannot be mistaken
        for the two-argument function it delegates to."""
        out = subprocess.run(["uci", "-q", "show", "firewall"], capture_output=True, text=True, timeout=5).stdout
        return zone_input(out, network)

    @staticmethod
    def public_ipv6(lan_device):
        def ip(*args):
            return subprocess.run(["ip", "-6", "-o", *args], capture_output=True, text=True, timeout=5).stdout
        return public_ipv6(ip("addr", "show", "scope", "global"), ip("route", "show", "default"), lan_device)

    def diagnostics(self):
        data = {}
        try:
            data["load"] = float(Path("/proc/loadavg").read_text().split()[0])
            meminfo = Path("/proc/meminfo").read_text()
            data["mem_mb"] = int(re.search(r"^MemAvailable:\s*(\d+)", meminfo, re.M).group(1)) // 1024
        except (OSError, ValueError, AttributeError):
            pass
        temp = hottest()
        if temp is not None:
            data["temp_c"] = temp
        if self.ap_interface:
            data["peers"] = parse_stations(subprocess.run(["iw", "dev", self.ap_interface, "station", "dump"],
                                                          capture_output=True, text=True, timeout=5).stdout)
        return data
