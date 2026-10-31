"""Debian/Armbian system adapter (OneCloud; Phicomm N1 prepared, see docs/斐讯N1移植预研.md).
Commands and timings preserved from the deployed runtime.

No operations run at import time. Paths are injectable for isolated adapter tests.
"""
import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import threading
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import time

RUN = Path("/run/dashplay")
STA_NETWORK = Path("/run/systemd/network/60-onecloud-sta.network")
# USB shared network (iPhone ipheth, Android/MiFi rndis_host, cdc_ether, cdc_ncm), options.json car_internet "usb_tether"; sorted
# before any image-wide match so its route stays behind the Ethernet cable (default metric 1024).
USB_TETHER_NETWORK = Path("/run/systemd/network/05-dashplay-usb-tether.network")
# Armbian N1 images hand every Ethernet-like device to NetworkManager (netplan renderer), which would otherwise give
# the iPhone a default profile with the cable's priority; this profile, matched on the driver, wins (dp-0017, NM 1.52).
USB_TETHER_PROFILE = Path("/run/NetworkManager/system-connections/dashplay-usb-tether.nmconnection")
USB_TETHER_METRIC = 1536
# "USB shared network": any phone or MiFi that shares its internet over USB (2026-10-31): iPhone (ipheth), Android
# (rndis_host on most, cdc_ncm on newer ones) and USB MiFis (rndis_host or cdc_ether). All built as modules in the
# N1/onecloud 6.12 kernel (dp-0017, CONFIG_USB_NET_RNDIS_HOST/CDCETHER/CDC_NCM=m). The iPhone's own CarPlay NCM
# functions are cdc_ncm too; the USB bridge claims them for CarPlay or releases them, so they never take a lease.
# The box's Ethernet adapters (r8152, asix, ...) are not listed: their own profile keeps the image's metric.
PUBLIC_DNS = ("223.5.5.5", "1.12.12.12")
USB_TETHER_DRIVERS = ("ipheth", "rndis_host", "cdc_ether", "cdc_ncm")
# Second interface on the same radio for the box hotspot while the first one is a client of the car
# hotspot (one channel shared; needs the rtw88 build from scripts/onecloud/wifi/).
AP_VIF = "dpap0"
JOIN_KNOWN_FREQ_SECONDS = 8   # look for the car hotspot on last time's frequency only, then everywhere
BLUETOOTH_NAME = "DashPlay"
SYS_NET = Path("/sys/class/net")
MMC_DEVICES = Path("/sys/bus/mmc/devices")
MACHINE_ID = Path("/etc/machine-id")
# Addresses that vendor firmware gives every unit alike (2026-10-12, Phicomm N1: the BCM43455 nvram MAC, shared
# by the nvram files of many boards, and the BCM4345C0 patch's Bluetooth default). An iPhone would take two such
# boxes for one; they get an address of their own instead (derived_address). Hardware with its own address keeps it.
SHARED_WIFI_ADDRESSES = {"b8:27:eb:74:f2:6c", "00:90:4c:c5:12:38"}
SHARED_BLUETOOTH_ADDRESSES = {"43:45:c0:00:1f:ac"}


def machine_key(mmc=MMC_DEVICES, machine_id=MACHINE_ID):
    """What tells this box apart: the eMMC's CID, which survives a reflash (so do the iPhone's pairings),
    else the machine-id."""
    for device in sorted(Path(mmc).glob("*")):
        try:
            if (device / "type").read_text().strip() == "MMC":
                return (device / "cid").read_text().strip()
        except OSError:
            continue
    return Path(machine_id).read_text().strip()


def derived_address(key, purpose):
    """A stable address for this box and purpose ("wifi", "bluetooth"): locally administered, unicast."""
    raw = bytearray(hashlib.sha256(f"dashplay:{purpose}:{key}".encode()).digest()[:6])
    raw[0] = (raw[0] | 0x02) & 0xFE
    return ":".join(f"{b:02x}" for b in raw)


def ap_width(channel, width):
    """The widest of width that the channel allows: 2.4 GHz and channel 165 have no 80 MHz block."""
    if channel <= 14 or channel == 165:
        return 20
    return width

def wpa_network_lines(ssid, passphrase):
    """The ssid and psk lines of a wpa_supplicant network block. The name goes in hex (any bytes); the password
    as written: a quoted value runs to its last quote, without escapes, so the backslashes once added for
    \\ and " became part of it and such hotspots never connected (review 2026-10-12)."""
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid + passphrase):
        raise ValueError("control character in the Wi-Fi name or password")
    psk = passphrase if re.fullmatch(r"[0-9a-fA-F]{64}", passphrase) else f'"{passphrase}"'
    return [f" ssid={ssid.encode().hex()}", f" psk={psk}"]


def hostapd_config(interface, channel, width, ssid, passphrase, country="CN"):
    """passphrase None: an open network (first-run setup only). country None: leave the regulatory
    domain to the driver."""
    # One setting per line: a newline in the name or password added its own (review 2026-10-12).
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid + (passphrase or "")):
        raise ValueError("control character in the hotspot name or password")
    lines = [f"interface={interface}", "driver=nl80211", f"ssid={ssid}"]
    if country:
        # Sets the regulatory domain on start; the global "00" domain marks every 5 GHz channel no-IR.
        lines += [f"country_code={country}", "ieee80211d=1"]
    lines += [
        f"hw_mode={'a' if channel > 14 else 'g'}", f"channel={channel}",
        "wmm_enabled=1", "ieee80211n=1", "auth_algs=1",
    ]
    if passphrase is not None:
        lines += ["wpa=2", "wpa_key_mgmt=WPA-PSK", "rsn_pairwise=CCMP", f"wpa_passphrase={passphrase}"]
    width = ap_width(channel, width)
    if channel > 14:
        lines.append("ieee80211ac=1")
        if width >= 40:
            # 149/157 pair upward, 153/161 downward; 165 has no 40 MHz partner.
            lines.append("ht_capab=[HT40+]" if channel in (36, 44, 149, 157) else "ht_capab=[HT40-]")
        if width == 80:
            centre = {149: 155, 153: 155, 157: 155, 161: 155, 36: 42, 40: 42, 44: 42, 48: 42}[channel]
            lines += ["vht_oper_chwidth=1", f"vht_oper_centr_freq_seg0_idx={centre}"]
    return "\n".join(lines) + "\n"

def supports_sta_ap(iw_list):
    """From `iw list`: how many channels a station and an AP together may use (e.g.
    "#{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1"), 0 if no such combination.
    2 (MT7921AU) lets the box hotspot stay on a legal channel when the car hotspot is on a radar one."""
    match = re.search(r"valid interface combinations:\n((?:\t\t.*\n)+)", iw_list)
    if not match:
        return 0
    best = 0
    for combination in re.split(r"\n\t\t \* ", match.group(1)):
        limits = re.findall(r"#\{([^}]*)\} <= (\d+)", combination)
        total = re.search(r"total <= (\d+)", combination)
        channels = re.search(r"#channels <= (\d+)", combination)
        if (total and int(total.group(1)) >= 2 and any("managed" in kinds for kinds, _ in limits)
                and any(re.search(r"\bAP\b", kinds) for kinds, _ in limits)
                and all(int(limit) >= sum(kind in {part.strip() for part in kinds.split(',')} for kind in ('managed', 'AP'))
                        for kinds, limit in limits)):
            best = max(best, int(channels.group(1)) if channels else 1)
    return best


def ap_channel_problem(iw_list, freq):
    """From `iw list`: None when an AP may start on freq, else why not ("disabled", "no IR", "radar detection",
    "not listed"). The N1 lists 52-64 and 100-144 as disabled (2026-10-17)."""
    match = re.search(rf"\* {int(freq)}(?:\.0)? MHz \[\d+\]([^\n]*)", iw_list)
    if not match:
        return "not listed"
    flags = match.group(1)
    for flag in ("disabled", "no IR", "radar detection"):
        if flag in flags:
            return flag
    return None


def five_ghz(iw_list):
    """From `iw list`: True when the radio has a usable 5 GHz channel, False when it lists only 2.4 GHz (the WCN3620
    of Snapdragon 410 sticks registers no 5 GHz band at all), None when no radio was listed (iw failed)."""
    if "Wiphy " not in iw_list:
        return None
    return re.search(r"\* 5\d{3}(?:\.0)? MHz \[\d+\](?![^\n]*disabled)", iw_list) is not None


def missed_on_known_freq(scan_results, ssid):
    """`wpa_cli scan_results` after scanning only last time's frequency: True when that scan found
    networks but not the car hotspot, i.e. the car moved channel (Xpeng: 149 one drive, 36 the next)."""
    from setup_wizard import _unescape_ssid
    rows = [line.split("\t") for line in scan_results.splitlines()[1:] if line.count("\t") >= 4]
    return bool(rows) and all(_unescape_ssid(row[4]) != ssid for row in rows)


def wifi_driver(interface, net=Path("/sys/class/net")):
    """Kernel driver of a network interface (e.g. brcmfmac, rtw88_8821cu), None if unknown."""
    driver = net / interface / "device" / "driver"
    return driver.resolve().name if driver.exists() else None


def wifi_interface(net=Path("/sys/class/net")):
    """The Wi-Fi interface, whatever udev named it (USB dongles get a MAC-based wlx… name)."""
    try:
        names = sorted(entry.name for entry in net.iterdir())
    except OSError:
        return None
    return next((name for name in names if name not in (AP_VIF, "ap0") and
                 ((net / name / "wireless").exists() or (net / name / "phy80211").exists())), None)


LEDS = Path("/sys/class/leds")
BLINK_MS = 500
# OneCloud: green and blue LEDs (red is power and left alone).
STATES = {
    "starting": {"green": "blink", "blue": "off"},
    "waiting": {"green": "off", "blue": "on"},
    "live": {"green": "on", "blue": "off"},
    "fallback": {"green": "off", "blue": "blink"},
}
# Phicomm N1: one white LED (mainline device tree "n1:white:status"); the state is in the rhythm.
SINGLE = {
    "starting": (BLINK_MS, BLINK_MS),   # even blink
    "waiting": (100, 1900),             # short flash every 2 s
    "live": "on",
    "fallback": (150, 150),             # fast blink: the car hotspot was not found
}
SINGLE_LEDS = ("n1:white:status",)
# Snapdragon 410 sticks: green and blue show the states as on the OneCloud, red is power and left alone. Mainline
# msm8916-ufi.dtsi names them colour:function; OpenStick v1's 5.15 kernel (what the sticks run) names them otherwise.
STICK_LEDS = ({"green": "green:wlan", "blue": "blue:wan"}, {"green": "green:internet", "blue": "blue:wifi"})


class StatusLight:
    def __init__(self, root=LEDS):
        self.root = root
        self.shown = None

    def _write(self, led, mode):
        try:
            if isinstance(mode, tuple) or mode == "blink":
                on, off = mode if isinstance(mode, tuple) else (BLINK_MS, BLINK_MS)
                (led / "trigger").write_text("timer\n")
                (led / "delay_on").write_text(f"{on}\n")
                (led / "delay_off").write_text(f"{off}\n")
            else:
                (led / "trigger").write_text("none\n")
                (led / "brightness").write_text("1\n" if mode == "on" else "0\n")
        except OSError:
            pass   # another board or a read-only LED: the light is a convenience only

    def show(self, state):
        if state == self.shown or state not in STATES:
            return
        coloured = [(self.root / f"onecloud:{color}:alive", mode) for color, mode in STATES[state].items()]
        for names in STICK_LEDS:
            if any(led.exists() for led, _ in coloured):
                break
            coloured = [(self.root / names[color], mode) for color, mode in STATES[state].items()]
        if any(led.exists() for led, _ in coloured):
            for led, mode in coloured:
                if led.exists():
                    self._write(led, mode)
        else:
            single = next((self.root / name for name in SINGLE_LEDS if (self.root / name).exists()), None)
            if single is not None:
                self._write(single, SINGLE[state])
        self.shown = state


def _number(pattern, text, cast=float):
    match = re.search(pattern, text, re.M)
    return cast(match.group(1)) if match else None


def parse_link(text):
    """`iw dev X link` while joined to a hotspot: frequency, signal, bit rates."""
    if "Connected to" not in text:
        return {}
    return {"freq": _number(r"freq: ([\d.]+)", text, lambda v: int(float(v))),
            "signal": _number(r"signal: (-?\d+)", text, int),
            "rx_mbps": _number(r"rx bitrate: ([\d.]+)", text), "tx_mbps": _number(r"tx bitrate: ([\d.]+)", text)}


def parse_stations(text):
    """`iw dev X station dump`: per peer signal, bit rates and cumulative retries/failures."""
    peers = []
    for block in re.split(r"^Station ", text, flags=re.M)[1:]:
        peers.append({"peer": block.split()[0][-5:],
                      "signal": _number(r"^\s*signal:\s*(-?\d+)", block, int),
                      "rx_mbps": _number(r"rx bitrate:\s*([\d.]+)", block),
                      "tx_mbps": _number(r"tx bitrate:\s*([\d.]+)", block),
                      "tx_packets": _number(r"tx packets:\s*(\d+)", block, int),
                      "tx_retries": _number(r"tx retries:\s*(\d+)", block, int),
                      "tx_failed": _number(r"tx failed:\s*(\d+)", block, int),
                      "inactive_ms": _number(r"inactive time:\s*(\d+)", block, int)})
    return peers


class DebianPlatform:
    def __init__(self, interface="", run=RUN, sta_network=STA_NETWORK, tether_network=USB_TETHER_NETWORK,
                 tether_profile=USB_TETHER_PROFILE):
        self.interface, self.run, self.sta_network = interface, Path(run), Path(sta_network)
        self.tether_network, self.tether_profile = Path(tether_network), Path(tether_profile)
        # The box hotspot runs here: the Wi-Fi interface itself, or AP_VIF next to a car-hotspot client.
        self.ap_interface = interface
        self.children = []
        self.bluetooth_state = None

    ap_vif = AP_VIF
    wifi_interface = staticmethod(wifi_interface)
    status_light = staticmethod(StatusLight)

    def configure_hotspot(self, channel, width, ssid, passphrase):
        # brcmfmac (Phicomm N1) brings its own domain that allows 36-48 and an AP on 149-165. Once
        # hostapd sets CN, which marks 5150-5350 DFS, it disables 36-48 until reboot, so a car hotspot
        # there was never seen (2026-10-12). rtw88 needs CN: under "00" no 5 GHz AP can start.
        country = None if wifi_driver(self.interface) == "brcmfmac" else "CN"
        (self.run / "hostapd.conf").write_text(
            hostapd_config(self.ap_interface, channel, width, ssid, passphrase, country))

    @staticmethod
    def local_addresses():
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
        return [ipaddress.ip_interface(line.split()[3]) for line in out.splitlines() if " lo " not in line]

    @staticmethod
    def boot_id():
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def diagnostics(self):
        """Box-side health for the drive log: Wi-Fi link, USB dongle resets, load, temperature, memory."""
        data = {}
        try:
            data["load"] = float(Path("/proc/loadavg").read_text().split()[0])
            data["temp_c"] = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
            meminfo = Path("/proc/meminfo").read_text()
            data["mem_mb"] = _number(r"^MemAvailable:\s*(\d+)", meminfo, int) // 1024
        except (OSError, ValueError, TypeError):
            pass
        kernel = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5).stdout
        data["usb_resets"] = kernel.count("USB disconnect")
        data["link"] = parse_link(self.link())
        data["peers"] = parse_stations(self.ap_iw("station", "dump"))
        if self.ap_interface != self.interface:   # the car hotspot as seen by the client side
            data["peers"] += parse_stations(self.iw("station", "dump"))
        return data

    def start(self, name, argv, query_log=None):
        if query_log:
            query_log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            self.children.append((name, child))
            def drain():
                handler = RotatingFileHandler(query_log, maxBytes=1_000_000, backupCount=1)
                try:
                    for line in iter(child.stdout.readline, b""):
                        handler.emit(logging.LogRecord("dns", logging.INFO, "", 0,
                                                       line.decode(errors="replace").rstrip(), (), None))
                finally:
                    child.stdout.close()
                    handler.close()
            threading.Thread(target=drain, name="dns-log", daemon=True).start()
            return child
        with (self.run / (name + ".log")).open("w") as log:
            child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        self.children.append((name, child))
        return child

    def stop_child(self, name):
        for entry in list(self.children):
            if entry[0] == name:
                self.children.remove(entry)
                if entry[1].poll() is None:
                    os.killpg(entry[1].pid, signal.SIGTERM)
                    try:
                        entry[1].wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(entry[1].pid, signal.SIGKILL)
                        entry[1].wait()

    def check_lan_children(self):
        """Catch immediate process exits before declaring the LAN ready.

        Receiver is optional until identity is installed. Later failures remain
        supervised; this short check is not a guarantee of a live CarPlay session.
        """
        time.sleep(0.25)
        present = {name for name, _ in self.children}
        if not {'hostapd', 'dnsmasq'} <= present:
            raise RuntimeError('LAN processes missing')
        for name, child in self.children:
            if name in ('hostapd', 'dnsmasq', 'receiver') and child.poll() is not None:
                raise RuntimeError(name + ' exited during LAN startup')

    def ap_ready(self):
        # hostapd's own word only. Realtek's 8821cu driver (concurrent STA + AP build) reports no channel
        # for an AP while its station interface is idle (2026-10-11), and the N1's brcmfmac reports type
        # AP with a channel as soon as hostapd starts, before it fails on an 80 MHz channel (2026-10-12):
        # iw cannot tell. The log is rewritten on every start.
        if any(name == 'hostapd' and child.poll() is not None for name, child in self.children):
            return False
        try:
            log = (self.run / "hostapd.log").read_text(errors="replace")
            return log.rfind('AP-ENABLED') > log.rfind('AP-DISABLED')
        except OSError:
            return False

    def concurrent_ap(self):
        """Channels a station and an AP may use at once: 0 none, 1 the same (rtw88 built by
        build-rtw88-concurrent.sh), 2 different ones (MT7921AU)."""
        return supports_sta_ap(subprocess.run(["iw", "list"], capture_output=True, text=True, timeout=10).stdout)

    def wired_capable(self, compatible=Path("/proc/device-tree/compatible")):
        """Whether the iPhone can be plugged in: not on a Snapdragon 410 stick, whose only USB port is its plug (device
        side, no power out for a phone)."""
        try:
            return b"qcom,msm8916" not in compatible.read_bytes().split(b"\0")
        except OSError:
            return True

    def five_ghz(self):
        return five_ghz(subprocess.run(["iw", "list"], capture_output=True, text=True, timeout=10).stdout)

    def ap_channel_problem(self, freq):
        return ap_channel_problem(subprocess.run(["iw", "list"], capture_output=True, text=True, timeout=10).stdout, freq)

    def add_ap_interface(self):
        """Create AP_VIF beside the station; its MAC is the dongle's with the locally administered bit
        flipped, so both hardware ports differ. Returns False if the driver refuses."""
        self.remove_ap_interface()
        mac = Path("/sys/class/net", self.interface, "address").read_text().strip()
        first = int(mac[:2], 16) ^ 0x02
        own = f"{first:02x}{mac[2:]}"
        if subprocess.run(["iw", "dev", self.interface, "interface", "add", AP_VIF, "type", "__ap"],
                          capture_output=True, timeout=10).returncode != 0:
            return False
        if subprocess.run(["ip", "link", "set", AP_VIF, "address", own], capture_output=True, timeout=10).returncode != 0:
            self.remove_ap_interface()
            return False
        self.ap_interface = AP_VIF
        return True

    def remove_ap_interface(self):
        if Path("/sys/class/net", AP_VIF).exists():
            subprocess.run(["iw", "dev", AP_VIF, "del"], capture_output=True, timeout=10)
        self.ap_interface = self.interface

    def iw(self, *args):
        return subprocess.run(["iw", "dev", self.interface, *args], capture_output=True, text=True, timeout=20).stdout

    def ap_iw(self, *args):
        return subprocess.run(["iw", "dev", self.ap_interface, *args], capture_output=True, text=True, timeout=20).stdout

    def ipv4_address(self):
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", self.interface],
                             capture_output=True, text=True).stdout.split()
        return out[3].split("/")[0] if len(out) > 3 else None

    def remove_sta_network(self):
        if self.sta_network.exists():
            self.sta_network.unlink()
            subprocess.run(["networkctl", "reload"], capture_output=True, timeout=20)

    def join_wifi(self, car, lease, wait_for, seconds):
        """Associate with the car hotspot and get a DHCP lease; returns True on success, False after timeout cleanup."""
        conf = self.run / "wpa.conf"
        network = "\n".join(wpa_network_lines(car["ssid"], car["passphrase"]))
        # Last time's frequency first: a full 2.4 + 5 GHz scan is ~2.7 s on rtw88, longer while the JVM
        # starts, and the box sat ~10 s between the dongle appearing and authenticating (2026-10-11 cold
        # boot). If the car moved channel, everything is scanned again after JOIN_KNOWN_FREQ_SECONDS.
        known = lease.get("freq") if lease.get("ssid") == car["ssid"] else None

        def write_conf(scan_freq):
            # Car hotspots run WPA2, WPA3 or the transition mode; accept either.
            conf.write_text(f'ctrl_interface={self.run}/wpa\nnetwork={{\n{network}\n'
                            ' key_mgmt=WPA-PSK SAE\n ieee80211w=1\n'
                            + (f' scan_freq={int(scan_freq)}\n' if scan_freq else '') + '}\n')

        write_conf(known)
        request = f"RequestAddress={lease['address']}\n" if lease.get("ssid") == car["ssid"] else ""
        # Ask the car's DHCP server for the previous address, so the head unit's bookmark keeps working.
        self.sta_network.write_text(f"[Match]\nName={self.interface}\n[Network]\nDHCP=ipv4\n"
                               f"[DHCPv4]\nRouteMetric=2048\n{request}")
        self.sta_network.chmod(0o644)   # read by systemd-network; no secrets
        subprocess.run(["ip", "addr", "flush", "dev", self.interface], check=True)
        subprocess.run(["ip", "link", "set", self.interface, "up"], check=True)
        self.start("wpa_supplicant", ["wpa_supplicant", "-D", "nl80211", "-i", self.interface, "-c", str(conf)])
        try:
            wait_for(lambda: subprocess.run(["networkctl", "reload"], capture_output=True,
                                            timeout=20).returncode == 0, 30, interval=0.25)
            subprocess.run(["networkctl", "reconfigure", self.interface], capture_output=True, timeout=20)
            joined = lambda: "Connected to" in self.iw("link")
            if known:
                begun = time.monotonic()

                def missed():
                    # Do not wait the whole JOIN_KNOWN_FREQ_SECONDS once a scan there came back without it.
                    if time.monotonic() - begun < 2:
                        return False
                    results = subprocess.run(["wpa_cli", "-p", str(self.run / "wpa"), "-i", self.interface,
                                              "scan_results"], capture_output=True, text=True, timeout=5)
                    return missed_on_known_freq(results.stdout, car["ssid"])

                try:
                    wait_for(lambda: joined() or missed(), min(JOIN_KNOWN_FREQ_SECONDS, seconds), interval=0.5)
                    if not joined():
                        raise TimeoutError("car hotspot not on last time's frequency")
                except TimeoutError:
                    write_conf(None)
                    subprocess.run(["wpa_cli", "-p", str(self.run / "wpa"), "-i", self.interface, "reconfigure"],
                                   capture_output=True, timeout=10)
                    wait_for(joined, max(1, seconds - JOIN_KNOWN_FREQ_SECONDS), interval=0.25)
            else:
                wait_for(joined, seconds, interval=0.25)
            # rtw88 client power save buffered frames: RTT 6-214 ms, a third of 1400-byte packets
            # lost and browser TLS handshakes stalled (2026-10-10). Box-hotspot mode never enables it.
            self.iw("set", "power_save", "off")
            wait_for(lambda: self.ipv4_address() is not None, 30, interval=0.2)
        except TimeoutError:
            self.stop_child("wpa_supplicant")
            self.remove_sta_network()
            subprocess.run(["ip", "addr", "flush", "dev", self.interface])
            return False
        return True

    def disconnect(self):
        """Leave the car hotspot (wpa_supplicant stays up). With the box hotspot on another channel the
        MT7921AU only served iPhones when the AP started while the station was not associated: started
        after it, iPhones saw the network but never reached it (2026-10-12)."""
        subprocess.run(["wpa_cli", "-p", str(self.run / "wpa"), "-i", self.interface, "disconnect"],
                       capture_output=True, timeout=10)

    def reassociate(self, wait_for, seconds=20):
        """Join the car hotspot again after disconnect()."""
        subprocess.run(["wpa_cli", "-p", str(self.run / "wpa"), "-i", self.interface, "reassociate"],
                       capture_output=True, timeout=10)
        try:
            wait_for(lambda: "Connected to" in self.iw("link"), seconds, interval=0.25)
            return True
        except TimeoutError:
            return False

    def quick_ack(self):
        """ACK at once on the car hotspot's subnet instead of after up to 40 ms: with the iPhone's video
        relayed by the car hotspot (high RTT), its TCP sender is clocked by our ACKs (2026-10-12)."""
        routes = subprocess.run(["ip", "-4", "route", "show", "dev", self.interface, "scope", "link"],
                                capture_output=True, text=True, timeout=5).stdout.split("\n")
        for route in filter(None, (line.strip() for line in routes)):
            if "quickack" not in route:
                subprocess.run(["ip", "route", "change", *route.split(), "dev", self.interface, "quickack", "1"],
                               capture_output=True, timeout=5)

    def connected(self):
        return "Connected to" in self.link()

    def frequency(self):
        match = re.search(r"freq: (\d+)", self.link())
        return int(match.group(1)) if match else None

    def link(self):
        return self.iw("link")

    def security(self, ssid):
        from setup_wizard import security
        return security(self.iw("scan", "dump"), ssid)

    def has_stations(self):
        return "Station" in self.ap_iw("station", "dump")

    def scan(self, ap=False, frequency=None, timeout=30):
        command = ["iw", "dev", self.interface, "scan"]
        if frequency:
            command += ["freq", str(frequency)]
        return subprocess.run(command + (["ap-force"] if ap else []), capture_output=True, text=True, timeout=timeout)

    def survey(self):
        """`iw dev X scan` before the box hotspot starts, for supervisor.quiet_channel; the interface must be up."""
        subprocess.run(["ip", "link", "set", "dev", self.interface, "up"], capture_output=True, timeout=10)
        return self.scan().stdout

    def scan_networks(self, own_ssid, ap=False):
        from setup_wizard import parse_scan
        return parse_scan(self.scan(ap=ap).stdout, own_ssid)

    def find_hotspot(self, ssid, frequency=None, timeout=30):
        from setup_wizard import _unescape_ssid
        began = time.monotonic()
        result = self.scan(ap=True, frequency=frequency, timeout=timeout)
        # iw escapes UTF-8 bytes; compare decoded names, preserving meaningful spaces. Cached results from
        # earlier scans are skipped (see phone_hotspot_frequency): a stale hit would take the AP down to join.
        fresh_ms = (time.monotonic() - began + 1) * 1000
        found = False
        for block in re.split(r"^BSS ", result.stdout, flags=re.M)[1:] or [result.stdout]:
            age = re.search(r"^\s*last seen: (\d+) ms ago", block, re.M)
            freq = re.search(r"^\s*freq: (\d+)", block, re.M)
            if age and int(age[1]) > fresh_ms or frequency and freq and int(freq[1]) != int(frequency):
                continue
            found = found or any(_unescape_ssid(name) == ssid for name in re.findall(r"^\s*SSID: ?(.*)$", block, re.M))
        return result.returncode == 0, found, result.stderr

    def unique_wifi_address(self):
        """Give the Wi-Fi interface an address of its own if it has a vendor default (SHARED_WIFI_ADDRESSES);
        before hostapd or a car-hotspot join brings it up, as the address changes only while it is down."""
        try:
            if (SYS_NET / self.interface / "address").read_text().strip().lower() not in SHARED_WIFI_ADDRESSES:
                return
            own = derived_address(machine_key(), "wifi")
            subprocess.run(["ip", "link", "set", "dev", self.interface, "down"], capture_output=True, timeout=10)
            subprocess.run(["ip", "link", "set", "dev", self.interface, "address", own], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"wifi address unchanged: {type(error).__name__}", flush=True)   # best effort, as for Bluetooth

    def unique_bluetooth_address(self):
        """Same for Bluetooth (SHARED_BLUETOOTH_ADDRESSES). The controller forgets it at every boot, so on every
        start; only while powered off."""
        try:
            # The kernel's view: bluetoothctl would wait for bluetoothd, not yet up this early in boot
            # (2026-10-12, N1: 10 s lost).
            # input="": with stdin on /dev/null (a systemd service) btmgmt never returns; an empty pipe ends it.
            info = subprocess.run(["btmgmt", "--index", "0", "info"], input="", capture_output=True, text=True, timeout=5).stdout
            match = re.search(r"addr ([0-9A-Fa-f:]{17})", info or "")
            if not (match and match.group(1).lower() in SHARED_BLUETOOTH_ADDRESSES):
                return
            own = derived_address(machine_key(), "bluetooth")
            subprocess.run(["btmgmt", "--index", "0", "power", "off"], input="", capture_output=True, text=True, timeout=5)
            subprocess.run(["btmgmt", "--index", "0", "public-addr", own], input="", capture_output=True, text=True, timeout=5)
            # BlueZ then drops the adapter and lists it again under the new address about a second later
            # (2026-10-12, N1); "power on" before that waits for a controller until it times out.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    shown = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=2).stdout
                except subprocess.TimeoutExpired:
                    shown = ""   # bluetoothd still starting
                if own in (shown or "").lower():
                    return
                time.sleep(0.2)
        except (OSError, subprocess.SubprocessError) as error:
            # Best effort: a box with the shared address still works, alone in its car.
            print(f"bluetooth address unchanged: {type(error).__name__}", flush=True)

    def power_bluetooth(self):
        # A board helper's own bus (F50, uci dbus_address) appears a few seconds into the boot; bluetoothctl
        # segfaults until it is there (2026-10-16), so wait for the socket instead of running it.
        bus = os.environ.get("DBUS_SYSTEM_BUS_ADDRESS", "")
        if bus.startswith("unix:path=") and not os.path.exists(bus[len("unix:path="):].split(",", 1)[0]):
            return False
        identity = None
        # Without btmgmt the address is not ours to manage: the F50's board helper fixes it at btattach and its
        # private BlueZ puts only bluetoothctl on PATH (2026.10.15 retried btmgmt for 60 s there: wired only).
        if self.bluetooth_state is None:
            self.unique_bluetooth_address()
        elif shutil.which("btmgmt"):
            from bluetooth_address import BluetoothIdentity
            identity = BluetoothIdentity(self.bluetooth_state)
            try:
                identity.prepare()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"bluetooth identity not ready: {type(error).__name__}", flush=True)
                return False
        if subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=10).returncode != 0:
            return False
        # A phone link left over from the previous run (service restart, network rebuild) fails every
        # invitation with br-connection-already-connected until the bridge's rate-limited recovery
        # power-cycles the adapter (2026-10-11: 69 s). Nothing uses Bluetooth yet, so clear it now.
        connected = subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True, text=True, timeout=10)
        if connected.stdout.lstrip().startswith("Device "):
            subprocess.run(["bluetoothctl", "power", "off"], capture_output=True, timeout=10)
            if subprocess.run(["bluetoothctl", "power", "on"], capture_output=True, timeout=10).returncode != 0:
                return False
        if identity:
            try:
                if not identity.verify():
                    print("bluetooth address changed after power-on; refusing a mismatched invitation", flush=True)
                    return False
            except (OSError, ValueError, subprocess.SubprocessError):
                return False
        # BlueZ names the adapter after the host ("onecloud"), which is what iPhones list under
        # Bluetooth. Set on every start: a reset or a fresh image clears BlueZ's stored alias.
        subprocess.run(["bluetoothctl", "system-alias", BLUETOOTH_NAME], capture_output=True, timeout=10)
        return True

    def prepare(self, wait_for):
        # meson8b frequency switches under the ondemand governor hit RCU stalls in the CPU clock
        # notifier (2026-10-10: three stalls, hostapd HT scan timed out); a fixed rate avoids them.
        # /etc/default/cpufrequtils also says performance, since armbian-hardware-optimize applies it.
        for governor in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"):
            governor.write_text("performance\n")
        # rtw88 USB charges each small datagram a large skb truesize, so the 176 KiB default UDP buffer
        # held only a few packets of the iPhone's ~400 ms audio bursts: ~2/3 dropped (RcvbufErrors),
        # heard as 1 s of music then 5 s of silence. The JVM uses the default, so raise it.
        for name in ("rmem_max", "rmem_default"):
            Path("/proc/sys/net/core", name).write_text(f"{4 * 1024 * 1024}\n")
        # Phicomm N1: its Wi-Fi on the Cypress firmware (board_phicomm_n1.py); after a first change brcmfmac is
        # reloaded, and the wait below brings the interface back.
        import board_phicomm_n1
        board_phicomm_n1.prepare(say=lambda text: print(text, flush=True))
        # The unit starts right after local-fs, possibly before udev has renamed the dongle.
        wait_for(lambda: Path(f"/sys/class/net/{self.interface}").exists(), 30)
        self.unique_wifi_address()
        # USB autosuspend can stall the Realtek combo dongle; keep it powered while the service runs.
        device = Path(f"/sys/class/net/{self.interface}/device/..").resolve()
        if (device / "power/control").exists():
            (device / "power/control").write_text("on\n")

    def ensure_receiver_ipv6(self):
        # JVM decides IPv6 availability at startup. dpncm appears later, too late
        # when Armbian disabled IPv6 and there is no Ethernet link (drive F8).
        result = subprocess.run(["sysctl", "-w", "net.ipv6.conf.lo.disable_ipv6=0"],
                                capture_output=True, text=True, check=True, timeout=10)
        print("receiver IPv6: " + result.stdout.strip(), flush=True)

    def allow_nonlocal_bind(self):
        Path("/proc/sys/net/ipv4/ip_nonlocal_bind").write_text("1\n")

    def flush_address(self):
        subprocess.run(["ip", "addr", "flush", "dev", self.interface])
        self.remove_ap_interface()

    def set_ap_address(self, address):
        subprocess.run(["ip", "addr", "flush", "dev", self.ap_interface], check=True)
        subprocess.run(["ip", "link", "set", self.ap_interface, "up"], check=True)
        subprocess.run(["ip", "addr", "add", str(address), "dev", self.ap_interface], check=True)

    def start_ap(self, wait_for):
        hostapd = self.start("hostapd", ["hostapd", "-t", str(self.run / "hostapd.conf")])
        try:
            wait_for(lambda: hostapd.poll() is not None or self.ap_ready(), 30)
        except TimeoutError:
            # Alive but never enabled: give the next channel or width its turn (it ended the supervisor).
            self.stop_child("hostapd")
            return False
        return hostapd.poll() is None

    def start_dhcp(self, address, portal, names=(), query_log=None, upstream=None, router=False,
                   upstream_file=None):
        """names: answered with the box's address on its hotspot (the owner's own domain, so that a car without
        internet finds it, 2026-10-15); every other name is REFUSED, which sends a resolver on to its next server.
        router: hand out the box as default gateway with those names (Tesla offline checks, supervisor.hotspot_router)."""
        network = address.network
        hosts = list(network.hosts())
        # On dnsmasq >= 2.86 an IPv4 --address alone still forwards other query types.
        # Keep AAAA/HTTPS queries local too: deSEC names intentionally have no public records.
        local_names = [flag for name in names for flag in
                       (f"--address=/{name}/{address.ip}", f"--local=/{name}/")]
        # First-run setup only: a router and a DNS server that answers every name with the box make
        # phones detect a captive portal and open the setup page by themselves.
        if upstream_file is not None:
            dns = ["--port=53", "--no-hosts", f"--resolv-file={upstream_file}", *local_names,
                   f"--dhcp-option=3,{address.ip}" if router else "--dhcp-option=3", f"--dhcp-option=6,{address.ip}"]
        elif upstream:
            dns = ["--port=53", "--no-resolv", "--no-hosts", f"--server={upstream}", "--server=223.5.5.5",
                   *local_names,
                   f"--dhcp-option=3,{address.ip}", f"--dhcp-option=6,{address.ip}"]
        elif portal:
            dns = [f"--port=53", f"--address=/#/{address.ip}", f"--dhcp-option=3,{address.ip}",
                   f"--dhcp-option=6,{address.ip}"]
        elif names:
            dns = ["--port=53", "--no-resolv", "--no-hosts", *local_names,
                   f"--dhcp-option=3,{address.ip}" if router else "--dhcp-option=3", f"--dhcp-option=6,{address.ip}"]
        else:
            dns = ["--port=0", "--dhcp-option=3", "--dhcp-option=6"]
        self.start("dnsmasq", ["dnsmasq", "--keep-in-foreground", "--conf-file=/dev/null", *dns,
                          f"--interface={self.ap_interface}", "--bind-interfaces", "--except-interface=lo",
                          f"--dhcp-range={hosts[9]},{hosts[99]},{network.netmask},12h",
                          "--dhcp-authoritative",
                          f"--dhcp-leasefile={self.run / 'dnsmasq.leases'}", "--log-dhcp",
                          *(["--log-queries", "--log-facility=-"] if query_log else [])],
                   **({"query_log": query_log} if query_log else {}))

    def uplink_gateway(self):
        result = subprocess.run(['ip', '-j', '-4', 'route', 'show', 'default', 'dev', self.interface],
                                capture_output=True, text=True, check=True, timeout=5)
        routes = json.loads(result.stdout)
        return next((str(ipaddress.IPv4Address(route['gateway'])) for route in routes if route.get('gateway')), None)

    @property
    def phone_resolv(self):
        """Upstream resolvers for the hotspot's dnsmasq (--resolv-file). dnsmasq reads it as "nobody", and the run
        directory is 0700 (the service's umask), so it lives beside it ("/run/dashplay-dns"): inside, every forwarded
        name was REFUSED (dp-0017, 2026-10-30). Only public resolver addresses."""
        return self.run.with_name(self.run.name + '-dns') / 'phone-resolv.conf'

    def set_phone_dns(self, gateway):
        """dnsmasq polls this file: upstream changes never restart DHCP or the LAN."""
        servers = ([str(ipaddress.IPv4Address(gateway))] if gateway else []) + list(PUBLIC_DNS)
        path = self.phone_resolv
        text = ''.join(f'nameserver {server}\n' for server in dict.fromkeys(servers))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o755)
        if path.exists() and path.read_text() == text and path.stat().st_mode & 0o004:
            return
        temporary = path.with_suffix('.tmp')
        temporary.write_text(text)
        temporary.chmod(0o644)
        temporary.replace(path)

    def phone_hotspot_frequency(self, ssid, frequency=None, timeout=5):
        """One bounded scan, retaining each SSID's frequency (including escaped UTF-8).
        frequency: scan only that channel (the phone hotspot's last one, phone_uplink.HotspotSearch)."""
        from setup_wizard import _unescape_ssid
        subprocess.run(['ip', 'link', 'set', 'dev', self.interface, 'up'],
                       check=True, capture_output=True, timeout=5)
        began = time.monotonic()
        result = self.scan(frequency=frequency, timeout=timeout) if frequency else self.scan(timeout=timeout)
        if result.returncode:
            raise OSError('station scan unavailable')
        # iw also prints the kernel's cached results: a scan of 5180 listed a hotspot last seen on 5745 9 s earlier
        # (dp-0017, 2026-10-30). Only what this scan saw counts, or a switched-off hotspot is chased every 10 s.
        fresh_ms = (time.monotonic() - began + 1) * 1000
        found = []
        for block in re.split(r'^BSS ', result.stdout, flags=re.M)[1:]:
            name = re.search(r'^\s*SSID: ?(.*)$', block, re.M)
            freq = re.search(r'^\s*freq: (\d+)', block, re.M)
            strength = re.search(r'^\s*signal: (-?[\d.]+)', block, re.M)
            age = re.search(r'^\s*last seen: (\d+) ms ago', block, re.M)
            if age and int(age[1]) > fresh_ms or frequency and freq and int(freq[1]) != int(frequency):
                continue
            if name and freq and _unescape_ssid(name[1]) == ssid:
                found.append((float(strength[1]) if strength else -1000, int(freq[1])))
        return max(found)[1] if found else None

    def start_phone_station(self, hotspot, frequency):
        """Start association/DHCP without waiting for either; never touch the AP address.

        Restrict association as well as scans to the selected frequency, so wpa_supplicant
        cannot silently roam the shared radio onto another channel during CarPlay.
        """
        self.stop_phone_station()
        frequency = int(frequency)
        network = '\n'.join(wpa_network_lines(hotspot['ssid'], hotspot['passphrase']))
        conf = self.run / 'wpa.conf'
        conf.write_text(f'ctrl_interface={self.run}/wpa\nnetwork={{\n{network}\n'
                        f' key_mgmt=WPA-PSK SAE\n ieee80211w=1\n scan_freq={frequency}\n'
                        f' freq_list={frequency}\n}}\n')
        conf.chmod(0o600)
        self.sta_network.write_text(f'[Match]\nName={self.interface}\n[Network]\nDHCP=ipv4\n'
                                    '[DHCPv4]\nRouteMetric=2048\n')
        self.sta_network.chmod(0o644)
        subprocess.run(['ip', 'link', 'set', 'dev', self.interface, 'up'],
                       check=True, capture_output=True, timeout=5)
        self.start('wpa_supplicant', ['wpa_supplicant', '-D', 'nl80211', '-i', self.interface, '-c', str(conf)])
        self.iw('set', 'power_save', 'off')
        for argv in (['networkctl', 'reload'], ['networkctl', 'reconfigure', self.interface]):
            subprocess.run(argv, check=True, capture_output=True, timeout=5)

    def stop_phone_station(self):
        self.stop_child('wpa_supplicant')
        # Removing the .network file alone can leave networkd's old DHCP client alive.
        # Explicitly disable it without bringing the shared radio or AP down.
        self.sta_network.write_text(f'[Match]\nName={self.interface}\n[Network]\n'
                                    'DHCP=no\nLinkLocalAddressing=no\n')
        self.sta_network.chmod(0o644)
        for argv in (['networkctl', 'reload'], ['networkctl', 'reconfigure', self.interface]):
            subprocess.run(argv, check=True, capture_output=True, timeout=5)
        subprocess.run(['ip', '-4', 'route', 'flush', 'dev', self.interface],
                       check=True, capture_output=True, timeout=5)
        subprocess.run(['ip', '-4', 'addr', 'flush', 'dev', self.interface],
                       check=True, capture_output=True, timeout=5)

    def phone_link(self):
        result = subprocess.run(['iw', 'dev', self.interface, 'link'],
                                check=True, capture_output=True, text=True, timeout=3)
        freq = re.search(r'freq: (\d+)', result.stdout)
        signal_dbm = re.search(r'signal: (-?\d+)', result.stdout)
        return {'connected': 'Connected to' in result.stdout,
                'frequency': int(freq[1]) if freq else None,
                'signal_dbm': int(signal_dbm[1]) if signal_dbm else None}

    def phone_station_address(self):
        result = subprocess.run(['ip', '-j', '-4', 'addr', 'show', 'dev', self.interface],
                                check=True, capture_output=True, text=True, timeout=3)
        for item in json.loads(result.stdout):
            for address in item.get('addr_info', []):
                if address.get('scope') == 'global':
                    return ipaddress.ip_interface(f"{address['local']}/{address['prefixlen']}")
        return None

    def default_route_devices(self):
        """Interfaces holding an IPv4 default route; the cable (anything but the Wi-Fi) may be the uplink."""
        try:
            result = subprocess.run(['ip', '-j', '-4', 'route', 'show', 'default'],
                                    capture_output=True, text=True, timeout=5)
            routes = json.loads(result.stdout or '[]')
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        return [route['dev'] for route in routes if route.get('dev')] if isinstance(routes, list) else []

    def prepare_phone_uplink(self):
        from phone_uplink import station_mac
        # Journal before changing anything so a killed supervisor can undo it.
        self.cleanup_phone_uplink()
        result = subprocess.run(['ip', '-j', 'link', 'show', 'dev', self.interface],
                                capture_output=True, text=True, check=True, timeout=5)
        info = json.loads(result.stdout)[0]
        original = info.get('permaddr') or info['address']
        # Some N1 images ship a shared vendor MAC; preserve our existing per-box replacement.
        if original.lower() in SHARED_WIFI_ADDRESSES:
            original = info['address']
        from setup_wizard import save_json
        save_json(self.run / 'phone-uplink.json', {'mac': original, 'interface': self.interface,
                  'forward': Path('/proc/sys/net/ipv4/ip_forward').read_text().strip(), 'network': None})
        for args in (['down'], ['address', station_mac(original)]):
            subprocess.run(['ip', 'link', 'set', 'dev', self.interface, *args], check=True, capture_output=True, timeout=5)

    def add_phone_ap(self):
        saved = json.loads((self.run / 'phone-uplink.json').read_text())
        # STA already has a different MAC. ap0 gets the same MAC as the normal daily AP.
        subprocess.run(['iw', 'dev', self.interface, 'interface', 'add', 'ap0', 'type', '__ap'],
                       check=True, capture_output=True, timeout=10)
        self.ap_interface = 'ap0'
        self.release_phone_ap_from_networkmanager()
        subprocess.run(['ip', 'link', 'set', 'dev', 'ap0', 'address', saved['mac']],
                       check=True, capture_output=True, timeout=5)
        return True

    def release_phone_ap_from_networkmanager(self):
        # Older installs exclude wl*/dpap0 but not ap0. NM can take it over after
        # hostapd starts and remove its address. Claim only our new interface;
        # do not reload NM, change Ethernet, or write permanent system settings.
        if not shutil.which('nmcli'):
            return
        running = subprocess.run(['nmcli', '-t', '-f', 'RUNNING', 'general'],
                                 capture_output=True, text=True, timeout=5,
                                 env={**os.environ, 'LC_ALL': 'C'})
        if running.stdout.strip() == 'not running':
            return
        if running.returncode or running.stdout.strip() != 'running':
            raise RuntimeError('Cannot determine NetworkManager state')
        # Its D-Bus device may appear slightly after iw creates the interface.
        for attempt in range(10):
            subprocess.run(['nmcli', 'device', 'set', 'ap0', 'managed', 'no'],
                           capture_output=True, timeout=3)
            state = subprocess.run(['nmcli', '-g', 'GENERAL.STATE', 'device', 'show', 'ap0'],
                                   capture_output=True, text=True, timeout=3,
                                   env={**os.environ, 'LC_ALL': 'C'})
            if state.returncode == 0 and re.match(r'^10(?:\s|$)', state.stdout.strip()):
                return
            if attempt < 9:
                time.sleep(0.2)
        raise RuntimeError('NetworkManager did not release the phone hotspot interface')

    def n_only_ap(self):
        path = self.run / 'hostapd.conf'
        path.write_text(''.join(line for line in path.read_text().splitlines(True)
                                if not line.startswith(('ieee80211ac=', 'vht_', 'ht_capab='))))

    def phone_nat_rules(self, network):
        # Follow routing (Ethernet at home, phone Wi-Fi in the car), never translate back onto ap0.
        return [(['-t', 'nat'], ['POSTROUTING', '-s', str(network), '!', '-o', 'ap0',
                                '-m', 'comment', '--comment', 'dashplay-phone-uplink', '-j', 'MASQUERADE']),
                ([], ['FORWARD', '-i', 'ap0', '!', '-o', 'ap0', '-s', str(network),
                      '-m', 'comment', '--comment', 'dashplay-phone-uplink', '-j', 'ACCEPT']),
                ([], ['FORWARD', '!', '-i', 'ap0', '-o', 'ap0', '-d', str(network),
                      '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED',
                      '-m', 'comment', '--comment', 'dashplay-phone-uplink', '-j', 'ACCEPT'])]

    def usb_tether_rules(self, network):
        """Box hotspot clients out through whatever holds the default route (the iPhone's USB interface in the car,
        the cable at home), never back onto the hotspot itself."""
        ap, tag = self.ap_interface, ['-m', 'comment', '--comment', 'dashplay-usb-tether']
        return [(['-t', 'nat'], ['POSTROUTING', '-s', str(network), '!', '-o', ap, *tag, '-j', 'MASQUERADE']),
                ([], ['FORWARD', '-i', ap, '!', '-o', ap, '-s', str(network), *tag, '-j', 'ACCEPT']),
                ([], ['FORWARD', '!', '-i', ap, '-o', ap, '-d', str(network),
                      '-m', 'conntrack', '--ctstate', 'ESTABLISHED,RELATED', *tag, '-j', 'ACCEPT'])]

    def begin_usb_tether(self, network):
        """The fast half: the marker the receiver's USB bridge reads at start, and the journal stop_usb_sharing
        needs. The rest (manager profiles, forwarding, NAT) is [finish_usb_tether], run after the receiver is
        up so CarPlay is not held back by it (C5 vs C1 cold boot, 2026-10-31: 5 s)."""
        from setup_wizard import save_json
        self.stop_usb_sharing()
        forward = subprocess.run(['sysctl', '-n', 'net.ipv4.ip_forward'], capture_output=True, text=True,
                                 timeout=5).stdout.strip() or '0'
        save_json(self.run / 'usb-tether.json', {'network': str(network), 'forward': forward,
                                                 'interface': self.ap_interface})
        (self.run / 'usb-tether').write_text('on\n')

    def finish_usb_tether(self):
        """Profiles, forwarding and NAT for the sharing [begin_usb_tether] announced."""
        try:
            saved = json.loads((self.run / 'usb-tether.json').read_text())
            network = ipaddress.ip_network(saved['network'])
        except (OSError, ValueError, KeyError):
            raise ValueError('USB 共享网络未准备好，请稍后重试。') from None
        self._usb_tether_rules_for(network)

    def enable_usb_tether(self, network):
        """Use the iPhone's internet over the CarPlay cable and share it on the box hotspot.

        The marker tells iphone-usb-bridge.py to leave the ipheth interface bound (it releases it otherwise);
        systemd-networkd then takes the lease and the route when the iPhone is plugged in and drops them when it
        is unplugged, so nothing here follows the cable. Journalled in usb-tether.json for stop_usb_sharing."""
        self.begin_usb_tether(network)
        self._usb_tether_rules_for(network)

    def _usb_tether_rules_for(self, network):
        # Only the manager the image runs: with both, NetworkManager and networkd each took a lease on the
        # iPhone's interface (two addresses, dp-0017 2026-10-30). networkd also runs on the N1 images (the
        # car-hotspot client), so ask NetworkManager.
        if not self.network_manager_running():
            self.tether_network.parent.mkdir(parents=True, exist_ok=True)
            # UseDNS=no keeps the phone's own resolver out; DNS= gives the box's own resolver (systemd-resolved)
            # public servers, or it resolved nothing at all when the cable was the only uplink (dp-0017,
            # 2026-10-31). The hotspot's dnsmasq gets the same list through phone-resolv.
            self.tether_network.write_text(f'[Match]\nDriver={" ".join(USB_TETHER_DRIVERS)}\n'
                                           f'[Network]\nDHCP=ipv4\nIPv6AcceptRA=no\nDNS={" ".join(PUBLIC_DNS)}\n'
                                           f'[DHCPv4]\nRouteMetric={USB_TETHER_METRIC}\nUseDNS=no\n')
            self.tether_network.chmod(0o644)
        # multi-connect=3 (multiple): one profile is otherwise active on one device only; with the iPhone on
        # eth1 a Xiaomi plugged into the other port stayed "disconnected" on usb0 (dp-0017 2026-10-31).
        self.tether_profile.parent.mkdir(parents=True, exist_ok=True)
        self.tether_profile.write_text('[connection]\nid=dashplay-usb-tether\ntype=ethernet\nautoconnect=true\nmulti-connect=3\n'
                                       f'autoconnect-priority=100\n[match]\ndriver={";".join(USB_TETHER_DRIVERS)};\n'
                                       f'[ipv4]\nmethod=auto\nroute-metric={USB_TETHER_METRIC}\nignore-auto-dns=true\n'
                                       f'dns={";".join(PUBLIC_DNS)};\n'
                                       '[ipv6]\nmethod=ignore\n')
        self.tether_profile.chmod(0o600)   # NetworkManager ignores keyfiles others can read
        self.reload_network_managers()
        self.reapply_tether_links()
        subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=1'], check=True, capture_output=True, timeout=5)
        for table, rule in self.usb_tether_rules(network):
            subprocess.run(['iptables', *table, '-I', *rule], check=True, capture_output=True, timeout=5)
        self.set_phone_dns(None)   # public resolvers; the hotspot's dnsmasq reads phone_resolv
        (self.run / 'usb-tether').write_text('on\n')

    def wired_carrier(self):
        """True when a wired interface is up with a cable in it.

        At boot the cable's DHCP lease can arrive seconds after dashplay starts, so a route-only check said "no
        cable" and the box joined the phone hotspot anyway, dragging its own hotspot onto 2.4 GHz while the
        cable was the real uplink (C8 cold boot, dp-0017 2026-10-31). Carrier is there immediately.
        """
        try:
            entries = sorted(Path('/sys/class/net').iterdir())
        except OSError:
            return False
        for entry in entries:
            name = entry.name
            if name in (self.interface, self.ap_interface, 'lo') or name.startswith(('wl', 'ap', 'docker', 'veth', 'br-')):
                continue
            try:
                if (entry / 'carrier').read_text().strip() == '1':
                    return True
            except OSError:
                continue
        return False

    def reapply_tether_links(self):
        """Push a rewritten profile onto links that are already up.

        A manager "reload" only re-reads the files; a link that is already active keeps the settings it was
        activated with, so the new DNS never reached the box's own resolver after a service restart with the
        cable plugged in (dp-0017, 2026-10-31). Both commands are no-ops when that manager is not running.
        """
        try:
            links = sorted(Path('/sys/class/net').iterdir())
        except OSError:
            return
        for entry in links:
            try:
                driver = (entry / 'device' / 'driver').resolve().name
            except OSError:
                continue
            if driver not in USB_TETHER_DRIVERS:
                continue
            for command in (['nmcli', 'device', 'reapply', entry.name],
                            ['networkctl', 'reconfigure', entry.name]):
                try:
                    subprocess.run(command, capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass

    @staticmethod
    def network_manager_running():
        try:
            return subprocess.run(['systemctl', 'is-active', '--quiet', 'NetworkManager'],
                                  capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def reload_network_managers():
        for command in (['networkctl', 'reload'], ['nmcli', 'connection', 'reload']):
            try:
                subprocess.run(command, capture_output=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                pass   # that manager is not installed

    def disable_usb_tether(self, ipheth=Path('/sys/bus/usb/drivers/ipheth')):
        """The option is off: stop sharing, release a bound iPhone hotspot interface, then drop the profiles.

        Release first: without our profile NetworkManager gives any new Ethernet-like device a default profile
        with the cable's priority ("Wired connection 2", dp-0017 2026-10-31)."""
        self.stop_usb_sharing()
        if not (self.tether_network.exists() or self.tether_profile.exists()):
            return
        for interface in sorted(ipheth.glob('[0-9]*-*')) if ipheth.is_dir() else []:
            try:
                (ipheth / 'unbind').write_text(interface.name)
            except OSError:
                pass
        self.tether_network.unlink(missing_ok=True)
        self.tether_profile.unlink(missing_ok=True)
        self.reload_network_managers()

    def stop_usb_sharing(self):
        """Undo the hotspot's sharing (NAT, forwarding, the bridge marker); the option's profiles stay. On every
        service stop: the iPhone stays plugged in, and a dropped profile would leave it to NetworkManager's
        default one (see disable_usb_tether)."""
        (self.run / 'usb-tether').unlink(missing_ok=True)
        path = self.run / 'usb-tether.json'
        try:
            saved = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        ap, self.ap_interface = self.ap_interface, saved.get('interface') or self.ap_interface
        try:
            for table, rule in reversed(self.usb_tether_rules(ipaddress.ip_network(saved['network']))):
                subprocess.run(['iptables', *table, '-D', *rule], capture_output=True, timeout=5)
        finally:
            self.ap_interface = ap
        subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=' + str(saved.get('forward', '0'))],
                       capture_output=True, timeout=5)
        path.unlink(missing_ok=True)

    def enable_phone_nat(self, network):
        from setup_wizard import save_json
        path = self.run / 'phone-uplink.json'
        saved = json.loads(path.read_text())
        saved['network'] = str(network)
        save_json(path, saved)
        subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=1'], check=True, capture_output=True, timeout=5)
        for table, rule in self.phone_nat_rules(network):
            subprocess.run(['iptables', *table, '-I', *rule], check=True, capture_output=True, timeout=5)

    def cleanup_phone_uplink(self):
        path = self.run / 'phone-uplink.json'
        if not path.exists():
            return
        saved = json.loads(path.read_text())
        self.stop_child('wpa_supplicant')
        self.remove_sta_network()
        if saved.get('network'):
            for table, rule in reversed(self.phone_nat_rules(ipaddress.ip_network(saved['network']))):
                subprocess.run(['iptables', *table, '-D', *rule], capture_output=True, timeout=5)
        subprocess.run(['sysctl', '-w', 'net.ipv4.ip_forward=' + saved['forward']],
                       capture_output=True, check=True, timeout=5)
        subprocess.run(['iw', 'dev', 'ap0', 'del'], capture_output=True, timeout=5)
        for args in (['down'], ['address', saved['mac']]):
            subprocess.run(['ip', 'link', 'set', 'dev', saved['interface'], *args],
                           check=True, capture_output=True, timeout=5)
        subprocess.run(['ip', 'addr', 'flush', 'dev', saved['interface']], check=True, capture_output=True, timeout=5)
        self.ap_interface = self.interface
        path.unlink()

    def stop_all(self):
        for name, child in reversed(self.children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    # The JVM writes its AppCDS archive while exiting; that took longer than 8 s here. A board's
                    # Bluetooth helper stops its own children within 6 s (board_zte_f50.py).
                    child.wait(timeout={"receiver": 40, "bluetooth": 15}.get(name, 8))
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()

    def join_log(self):
        try:
            return (self.run / "wpa_supplicant.log").read_text(errors="replace")
        except OSError:
            return ""
