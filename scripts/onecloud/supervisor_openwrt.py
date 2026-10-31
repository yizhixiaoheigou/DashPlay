#!/usr/bin/env python3
"""Run DashPlay on OpenWrt as one procd service (/etc/init.d/dashplay, package openwrt/dashplay).

The router owns the network: the person sets up Wi-Fi in LuCI. This supervisor reads the Wi-Fi to hand
the iPhone (uci dashplay.main.wifi: a wifi-iface section, "auto" = the first access point, 5 GHz
first, or "external" = another router's Wi-Fi bridged into the LAN, entered as external_*), starts the
receiver on that network's address and the settings page (iPhones only) on settings_port, and restarts
itself when that Wi-Fi changes. Without a Bluetooth adapter it runs wired CarPlay only. Compared with
supervisor.py: no box hotspot, no joining, no first-run setup.
Optional: a DuckDNS name with a Let's Encrypt certificate (uci duckdns_domain / duckdns_token,
own_domain.py) for car browsers that refuse the local CA; it then replaces the name.
"""
import argparse
import audio_routes
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from boxnet import ensure_leaf
from settings_page import SettingsPage
import setup_wizard
import diagnostics
import own_domain
import update
from tesla_openwrt import OfflineChecks
from platform_openwrt import OpenWrtPlatform, RUN
import viewer_port

DEFAULTS = {"enabled": "1", "wifi": "auto", "settings_port": "18080", "viewer_port": "",
            # wifi "external": the Wi-Fi of another router (a travel router with better radios, its uplink this
            # router over USB or a cable, bridged into external_network) as entered, with its fixed channel.
            "external_ssid": "", "external_key": "", "external_security": "wpa2", "external_channel": "",
            "external_network": "lan",
            "java": "/usr/lib/dashplay-jre/bin/java", "name": "dashplay.lan",
            # Boards whose BlueZ cannot come from the system (ZTE F50: board_zte_f50.py): a command that
            # brings up the controller and its own D-Bus, and that bus's address.
            "bluetooth_service": "", "dbus_address": "",
            # Seconds without a viewer page before wireless CarPlay is ended, so an iPhone near a box left on
            # does not keep sending its screen (0: never; then a reopened page shows the picture at once).
            "idle_release": "300",
            # The name also answers with the router's own public IPv6 address, for car browsers that refuse a
            # private one: 0 no, 1 beside the LAN address, 2 instead of it (LAN address while there is none).
            "public_ipv6": "0",
            # 1: number the LAN of the router's own Wi-Fi in SHARED_LAN, for car browsers that refuse every
            # private address (Tesla); 0 puts the address it had before back.
            "shared_lan": "0",
            # For any car whose own Bluetooth is paired with the iPhone (found in a Tesla, 2026-10-14). car_music 1:
            # the music goes over the car's Bluetooth, the page leaves the steering wheel, media card and volume to
            # the car and plays prompts louder. car_voice 1: calls and Siri too (the receiver offers no microphone).
            "car_music": "0", "car_voice": "0",
            # 1: Tesla mode: shared_lan, the Tesla browser's fullscreen, and both switches above turned on when Tesla
            # mode is enabled (LuCI); the two switches stay the owner's choice afterwards (2026-10-28).
            "tesla_mode": "0", "answer_tesla_checks": "auto",
            # A DuckDNS sub-domain and its token: the name becomes <sub-domain>.duckdns.org, with a Let's Encrypt
            # certificate the router obtains and renews itself (own_domain.py). Empty: off.
            "duckdns_domain": "", "duckdns_token": "",
            # Look for a new release once a day (update.py; installed only when someone clicks it), and the URL
            # prefixes tried after GitHub (read by update.py itself).
            "update_check": "1", "update_mirror": ""}


def car_audio_args(config):
    """run-receiver.py options for what goes over the car's own Bluetooth; Tesla mode turns both switches on when it
    is enabled (LuCI) but they remain the owner's choice, as on the box (2026-10-28)."""
    # One sound switch since 2026-10-31 (audio_routes.resolve), the same as on the Linux boxes; a router whose owner
    # never touched it keeps the older options unchanged.
    tesla = config["tesla_mode"] == "1"
    if config.get(audio_routes.OUTPUT_KEY):
        return audio_routes.arguments(config, tesla)
    if tesla and config["car_music"] == "1" and config["car_voice"] == "1":
        return ["--tesla"]
    return (["--tesla-browser"] if tesla else []) + [
        flag for key, flag in (("car_music", "--car-music"), ("car_voice", "--car-voice")) if config[key] == "1"]


RECHECK_EVERY = 10      # seconds between looks at the Wi-Fi handed to the iPhone
WIRED_STATUS = RUN / "wired-status.json"
# Where browsers refusing private addresses (Tesla) open the router (setup_wizard.SHARED_LAN). Tailscale's own
# firewall drops 100.64.0.0/10 from other interfaces unless told not to (shared_lan_conflict).
SHARED_LAN = setup_wizard.SHARED_LAN
LAN_BEFORE = "lan-before.json"   # in state/: the LAN address to put back when shared_lan is turned off
REBIND_NAME = "dns-rebind-name"  # in state/: the own name allowed through dnsmasq's rebind protection
# LuCI applies with rollback: rpcd keeps the previous configuration here until the browser confirms it still
# reaches the router (about 90 s), else puts it back. A LAN moved before that confirm cannot be confirmed,
# so shared_lan would be rolled back to 0 and the address with it.
ROLLBACK_PENDING = Path("/var/run/rpcd/snapshot-files")


def own_lan(network):
    """Devices on this Wi-Fi get their address and DNS from this router: its access point, or another router's
    Wi-Fi bridged into its LAN (wifi "external"); not a network it joined as a client."""
    return network["mode"] in ("ap", "external")


def trusted_networks(network):
    """Who may use the settings page: the network of the Wi-Fi handed to the iPhone, where the head unit is,
    not every interface of the router (the WAN or a guest network too; audit 2026-10-13, A5)."""
    return [network["address"].network]


def waiting_trust(planned, lan):
    """Who may open the settings page while the Wi-Fi is not up: the network the configured Wi-Fi will use, or the
    LAN when the configuration names none (its access point switched off, 2026-10-19). [] when neither has an address."""
    if planned:
        return trusted_networks(planned)
    return [lan.network] if lan else []


WAITING_REASONS = [   # platform_openwrt's reasons, for the settings page; others are shown as they are
    (r"no Wi-Fi network is switched on.*", "路由器的 WiFi 都关着"),
    (r"Wi-Fi section (\S+) not found or switched off", r"选的 WiFi（\1）不存在或已关闭"),
    (r"Wi-Fi (.+) is not up yet", r"「\1」还没开启"),
    (r"no Wi-Fi network with a password type.*", "没有 iPhone 能用的加密方式（不加密、WPA2 或 WPA3 个人）"),
    (r"network (\S+) for Wi-Fi (.+) has no IPv4 address yet", r"「\2」所在的网络 \1 还没有地址"),
    (r"Wi-Fi (.+) has no channel yet", r"「\1」还没有信道"),
]


def waiting_warning(why):
    """The settings page's notice while it waits for the Wi-Fi handed to the iPhone."""
    for pattern, text in WAITING_REASONS:
        if re.fullmatch(pattern, why):
            why = re.sub(pattern, text, why)
            break
    return (f"交给 iPhone 的 WiFi 还没准备好：{why}。请在 OpenWrt 后台「网络 → 无线」或「服务 → DashPlay」检查；"
            "准备好后 DashPlay 会自动继续。")


def read_config(uci_text):
    """`uci -q show dashplay` -> options of the main section, with defaults."""
    config = dict(DEFAULTS)
    for line in (uci_text or "").splitlines():
        match = re.match(r"^dashplay\.main\.(\w+)='?(.*?)'?$", line)
        if match:
            config[match.group(1)] = match.group(2)
    return config


def name_changes(uci_dhcp, name, host, public=None, only_public=False, rebind=False, previous=None):
    """`uci -q show dhcp` -> the uci commands that make the router's DNS answer name -> host (its LAN address,
    record "dashplay") and name -> public (its own public IPv6 address, record "dashplay6", uci
    dashplay.main.public_ipv6). only_public leaves the LAN address out, and the name is then answered
    locally only: dnsmasq asks upstream for a type the hosts records lack, and a public record of the name
    may point at the LAN (F50, 2026-10-13). Without a public address the LAN address stays. [] when current.

    rebind allows this one name through dnsmasq's DNS-rebind protection (rebind_domain, i.e.
    --rebind-domain-ok). With only_public the answer comes from upstream, and in Tesla mode the name's public
    record points into 100.64.0.0/10, where the LAN lives; dnsmasq 2.93 (OpenWrt 25.12.5) counts such a
    forwarded answer as a rebind attack, so the access point's clients never got the address. previous is the
    name whitelisted last time: a rename has to drop its entry even when no local record is left to read it
    from (only_public deletes them)."""
    kinds, options = {}, {}
    for line in (uci_dhcp or "").splitlines():
        match = re.match(r"^dhcp\.([^.=]+)=(\w+)$", line)
        if match:
            kinds[match.group(1)] = match.group(2)
        match = re.match(r"^dhcp\.([^.=]+)\.(\w+)=(.*)$", line)
        if match:
            options.setdefault(match.group(1), {})[match.group(2)] = re.findall(r"'([^']*)'", match.group(3)) or [match.group(3)]
    alone = bool(only_public and public)
    changes = []
    for section, ip in (("dashplay", None if alone else host), ("dashplay6", public)):
        have = options.get(section, {})
        if ip is None:
            if section in kinds:
                changes.append(["delete", f"dhcp.{section}"])
        elif kinds.get(section) != "domain" or have.get("name") != [name] or have.get("ip") != [ip]:
            changes += [["set", f"dhcp.{section}=domain"], ["set", f"dhcp.{section}.name={name}"],
                        ["set", f"dhcp.{section}.ip={ip}"]]
    dns = next((section for section, kind in kinds.items() if kind == "dnsmasq"), None)
    if dns:
        servers = options.get(dns, {}).get("server", [])
        ours = {f"/{old}/" for section in ("dashplay", "dashplay6") for old in options.get(section, {}).get("name", [])}
        if previous:
            ours.add(f"/{previous}/")
        local = f"/{name}/" if alone else None
        for entry in sorted((ours | {f"/{name}/"}) - {local}):
            if entry in servers:
                changes.append(["del_list", f"dhcp.{dns}.server={entry}"])
        if local and local not in servers:
            changes.append(["add_list", f"dhcp.{dns}.server={local}"])
        # Only this name, and only while it is answered upstream: never the global rebind_protection, and never
        # the names other programs put there.
        allowed = {f"/{name}/"} if rebind else set()
        whitelist = options.get(dns, {}).get("rebind_domain", [])
        for entry in sorted(ours - allowed):
            if entry in whitelist:
                changes.append(["del_list", f"dhcp.{dns}.rebind_domain={entry}"])
        for entry in sorted(allowed - set(whitelist)):
            changes.append(["add_list", f"dhcp.{dns}.rebind_domain={entry}"])
    return changes + [["commit", "dhcp"]] if changes else []


def point_name(name, host, public=None, only_public=False, rebind=False, previous=None):
    """Keep our uci dhcp records answering name -> the router (name_changes), so the head unit can open
    https://dashplay.lan:18443/ whatever the LAN address. Returns True when they changed."""
    shown = subprocess.run(["uci", "-q", "show", "dhcp"], capture_output=True, text=True).stdout
    changes = name_changes(shown, name, host, public, only_public, rebind, previous)
    if not changes:
        return False
    for change in changes:
        subprocess.run(["uci", *change], check=True, capture_output=True)
    def reload():
        try:
            # No pipes: a daemon started by the init script would hold them open (OpenWrt container,
            # 2026-10-12, where the reload also never finished). The name is a convenience: in the
            # background, so CarPlay never waits for it.
            subprocess.run(["/etc/init.d/dnsmasq", "reload"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:
            print("dnsmasq reload timed out", flush=True)

    threading.Thread(target=reload, name="dnsmasq-reload", daemon=True).start()
    return True


def _lan_address(address, netmask):
    try:
        return ipaddress.IPv4Interface(f"{address}/{netmask}" if netmask and "/" not in address else address)
    except ValueError:
        return None


def shared_lan_changes(uci_network, section, on, before=None):
    """`uci -q show network` -> (uci commands, the LAN address to remember or None, why it cannot be done).
    on: number network.<section> in SHARED_LAN, remembering its address; off: put back the remembered one,
    unless the address was changed by hand since (then it is left and forgotten)."""
    options = {}
    for line in (uci_network or "").splitlines():
        match = re.match(rf"^network\.{re.escape(section)}\.(\w+)=(.*)$", line)
        if match:
            options[match.group(1)] = re.findall(r"'([^']*)'", match.group(2)) or [match.group(2)]
    addresses = options.get("ipaddr", [])
    netmask = (options.get("netmask") or [""])[0]
    current = _lan_address(addresses[0], netmask) if len(addresses) == 1 else None
    if on:
        if current == SHARED_LAN:
            return [], before, ""
        if options.get("proto") != ["static"] or current is None:
            return [], before, f"network {section} is not one static IPv4 address"
        return ([["set", f"network.{section}.ipaddr={SHARED_LAN.ip}"],
                 ["set", f"network.{section}.netmask={SHARED_LAN.netmask}"], ["commit", "network"]],
                {"ipaddr": addresses[0], "netmask": netmask}, "")
    if not before or current != SHARED_LAN:
        return [], None, ""
    return ([["set", f"network.{section}.ipaddr={before['ipaddr']}"],
             ["set", f"network.{section}.netmask={before['netmask']}"] if before.get("netmask")
             else ["delete", f"network.{section}.netmask"], ["commit", "network"]], None, "")


TAILSCALE_DROP = re.compile(r'iifname != "tailscale0\*?" ip saddr 100\.64\.0\.0/10 .*\bdrop\b')


def shared_lan_conflict(ip_addresses, lan_device, ruleset=None):
    """`ip -4 -o addr show` (and `nft list ruleset`, None when unreadable) -> why SHARED_LAN cannot be used:
    Tailscale's own firewall (it drops 100.64.0.0/10 arriving on any other interface; with
    `tailscale set --netfilter-mode=off` it adds none, and the F50's fw4 zone for tailscale0 lets it in),
    or another interface already in that /24. "" when free."""
    tailscale = TAILSCALE_DROP.search(ruleset) if ruleset is not None else None
    for line in (ip_addresses or "").splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[2] != "inet":
            continue
        device = fields[1]
        if device.startswith("tailscale") and (ruleset is None or tailscale):
            return ("Tailscale 的防火墙会丢掉别的网口进来的 100.64.0.0/10 地址：先在路由器上运行 "
                    "tailscale set --netfilter-mode=off（OpenWrt 防火墙要有放行 tailscale0 的区域），再勾选")
        try:
            theirs = ipaddress.IPv4Interface(fields[3]).network
        except ValueError:
            continue
        if device != lan_device and theirs.overlaps(SHARED_LAN.network):
            return f"{device} 已在用 {theirs}，与 {SHARED_LAN.network} 冲突"
    return ""


def refresh_network_warning(on, lan_device, status, base_warning="", path=None):
    """Read live routes without changing them; publish only a warning for settings and LuCI."""
    warning = ""
    if on:
        try:
            addresses = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                                       capture_output=True, text=True, timeout=5)
            if addresses.returncode:
                raise OSError("ip failed")
            try:
                nft = subprocess.run(["nft", "list", "ruleset"], capture_output=True, text=True, timeout=10)
                ruleset = nft.stdout if nft.returncode == 0 else None
            except (OSError, subprocess.SubprocessError):
                ruleset = None
            warning = shared_lan_conflict(addresses.stdout, lan_device, ruleset)
            if warning:
                warning += "。请检查运营商/上游网络；不要反复开关特斯拉模式，避免管理连接中断。"
        except (OSError, subprocess.SubprocessError):
            warning = "暂时无法检查特斯拉网段冲突，请检查系统网络状态。"
    status["warning"] = "；".join(x for x in (base_warning, warning) if x)
    path = path or RUN / "network-status.json"
    setup_wizard.save_json(path, {"warning": status["warning"], "checked_at": int(time.time())})


def wait_for_confirm(stopped, seconds=120):
    """Return once no LuCI apply is waiting for its confirm (ROLLBACK_PENDING empty or gone), or after seconds."""
    until = time.monotonic() + seconds
    while time.monotonic() < until and not stopped.is_set():
        try:
            pending = any(ROLLBACK_PENDING.iterdir())
        except OSError:
            pending = False
        if not pending:
            return
        stopped.wait(1)


def reconnect_clients(ifname, say):
    """Deauthenticate every station on the access point, so that each joins again at once and asks DHCP for an
    address in the new LAN; otherwise they keep the old one until the lease runs out, and the iPhone cannot reach
    the receiver (F50, 2026-10-14). Returns how many were asked to reconnect."""
    hostapd = f"hostapd.{ifname}"
    try:
        out = subprocess.run(["ubus", "call", hostapd, "get_clients"], capture_output=True, text=True, timeout=5).stdout
        clients = json.loads(out or "{}").get("clients", {})
    except (OSError, ValueError, subprocess.SubprocessError):
        say(f"{hostapd}: clients unknown, devices keep their old address until they reconnect")
        return 0
    for address in clients:
        subprocess.run(["ubus", "call", hostapd, "del_client", json.dumps({"addr": address, "reason": 3, "deauth": True})],
                       capture_output=True, timeout=5)
    return len(clients)


def apply_shared_lan(on, network, state, say, stopped=None):
    """Make the LAN of the Wi-Fi handed to the iPhone match uci dashplay.main.shared_lan. Returns (changed,
    warning for the settings page). Changed: the network is reloaded and the caller starts over."""
    path = state / LAN_BEFORE
    try:
        before = json.loads(path.read_text())
    except (OSError, ValueError):
        before = None
    if on and not own_lan(network):
        return False, "「车机不让开任何局域网地址」只对路由器自己的 WiFi 有效"
    if on and not before:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        try:
            nft = subprocess.run(["nft", "list", "ruleset"], capture_output=True, text=True, timeout=10)
            ruleset = nft.stdout if nft.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            ruleset = None
        why = shared_lan_conflict(out, network["device"], ruleset)
        if why:
            say(f"shared LAN refused: {why}")
            return False, why
    shown = subprocess.run(["uci", "-q", "show", "network"], capture_output=True, text=True, timeout=5).stdout
    changes, remember, why = shared_lan_changes(shown, network["network"], on, before)
    if why:
        say(f"shared LAN refused: {why}")
        return False, f"局域网地址不是单个静态 IPv4 地址，没有改成 {SHARED_LAN.ip}"
    if remember and remember != before:
        setup_wizard.save_json(path, remember)   # before the change: a crash in between still puts it back
    if changes:
        wait_for_confirm(stopped or threading.Event())
        if stopped is not None and stopped.is_set():
            return False, ""
        for change in changes:
            subprocess.run(["uci", *change], check=True, capture_output=True, timeout=10)
        try:
            subprocess.run(["/etc/init.d/network", "reload"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=60)
        except subprocess.TimeoutExpired:
            say("network reload timed out")   # netifd applies the committed address on its own
        say(f"LAN {network['network']} now {SHARED_LAN.ip}" if on else
            f"LAN {network['network']} back to {before['ipaddr']}")
        if network.get("ifname"):
            say(f"{reconnect_clients(network['ifname'], say)} devices asked to reconnect for a new address")
    if remember is None and before is not None:
        path.unlink(missing_ok=True)
    return bool(changes), ""


def own_config(config):
    """The DuckDNS sub-domain and token from uci, or None when off or invalid (then said once, never fatal)."""
    if not config["duckdns_domain"]:
        return None
    try:
        return {"domain": own_domain.sub_domain(config["duckdns_domain"]),
                "token": own_domain.check_token(config["duckdns_token"])}
    except ValueError:
        print("dashplay.main.duckdns_domain/duckdns_token invalid: own name off", flush=True)
        return None


def plan_of(network):
    """What the receiver depends on: a change in any of these restarts it."""
    return (network["ssid"], network["key"], network["security"], network["channel"], str(network["address"].ip))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    config = read_config(subprocess.run(["uci", "-q", "show", "dashplay"], capture_output=True, text=True).stdout)
    port = int(config["settings_port"])
    # Empty in the uci means 18443, or an owner's older state/receiver/config.json value: an update keeps it.
    viewer = (int(config["viewer_port"]) if str(config.get("viewer_port", "")).isdigit()
              else viewer_port.resolve(root / "state"))
    if (not re.fullmatch(r"[A-Za-z0-9_@\[\]-]{1,64}", config["wifi"]) or not 1024 <= port <= 65535
            or port == viewer or not viewer_port.valid(viewer)):
        raise ValueError("Invalid dashplay.main.wifi, settings_port or viewer_port")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9-]{1,63})+", config["name"]):
        raise ValueError("Invalid dashplay.main.name")
    ddns = own_config(config)
    if ddns:
        config["name"] = own_domain.name_of(ddns)
    for relative in ["run-receiver.py", "phone-presence.py", "pair-phone.py", "pairing_window.py", "settings_page.py",
                     "settings_ui.py", "platform_openwrt.py", "diagnostics.py", "iphone-usb-bridge.py",
                     "linux-bt-bridge/bt-bridge"]:
        if not (root / relative).is_file():
            raise RuntimeError("Missing runtime input: " + relative)
    if not Path(config["java"]).is_file():
        raise RuntimeError(f"Java not found at {config['java']}: install the dashplay-jre package")
    identity = [root / "identity/offline-mfi/identity.pk8", root / "identity/offline-mfi/certificate.p7b"]

    def identity_ok():
        return all(path.is_file() and path.stat().st_size > 0 for path in identity)

    if args.check:
        print("Service configuration and runtime files checked; no services changed")
        return

    os.umask(0o077)
    RUN.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A board helper may put its own bluetoothctl in RUN/bin; every child inherits PATH and the bus.
    os.environ["PATH"] = f"{RUN / 'bin'}:{os.environ.get('PATH', '/usr/sbin:/usr/bin:/sbin:/bin')}"
    if config["dbus_address"]:
        os.environ["DBUS_SYSTEM_BUS_ADDRESS"] = config["dbus_address"]
    state = root / "state"
    # Keep the uci mirroring the effective port, so the LuCI link and field show it (an owner's older
    # state/receiver/config.json value is adopted once; the uci survives package updates).
    if not args.check and str(viewer) != config.get("viewer_port", ""):
        subprocess.run(["uci", "set", f"dashplay.main.viewer_port={viewer}"], check=False, capture_output=True)
        subprocess.run(["uci", "commit", "dashplay"], check=False, capture_output=True)
        config["viewer_port"] = str(viewer)
    state.mkdir(mode=0o700, exist_ok=True)
    platform = OpenWrtPlatform(run=RUN)
    platform.bluetooth_state = state
    stopped = threading.Event()
    reexec = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        # A stop wins over a restart asked for meanwhile (a LAN change under way when procd stops us, 2026-10-14).
        signal.signal(sig, lambda *_: (reexec.clear(), reexec.append(None), stopped.set()))
    have_identity = identity_ok()
    status = {"mode": "openwrt", "entries": [], "identity": have_identity, "home": f"http://{config['name']}:{port}/",
              "own_name": config["name"] if ddns else None}

    def wait_for(predicate, seconds, interval=0.2):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if stopped.is_set():
                raise InterruptedError("Service stopping")
            if predicate():
                return
            stopped.wait(interval)
        raise TimeoutError("Service dependency did not become ready")

    def request_reexec(reason):
        reexec.append(reason)
        stopped.set()

    receiver_refresh = []
    def on_settings_change(reason):
        if reason == 'receiver options changed':
            receiver_refresh.append(reason)
        else:
            request_reexec(reason)

    trusted = []   # the network the settings page serves, once the Wi-Fi is known (it starts after that)
    settings = SettingsPage(state, lambda: status, on_settings_change, lambda: list(trusted), None, WIRED_STATUS,
                            managed={"by": "OpenWrt", "admin": "/cgi-bin/luci/admin/network/wireless"})
    offline_checks = OfflineChecks(state, platform=platform)
    maintainer = []   # own_domain.Maintainer, when a DuckDNS name is set

    def say(text):
        """procd sends stdout to logread, which the F50's Wi-Fi driver floods within seconds: keep our
        own lines in RUN/supervisor.log too (mirrored to state/logs like every RUN/*.log)."""
        print(text, flush=True)
        try:
            with (RUN / "supervisor.log").open("a") as log:
                log.write(text + "\n")
        except OSError:
            pass

    def mark(step):
        say(f"boot {time.monotonic():.2f}s {step}")   # seconds since kernel start

    handoff_file = RUN / "handoff.json"

    def launch(network, wireless, wait=False, keep_epoch=False):
        setup_wizard.save_json(handoff_file, {"ssid": network["ssid"], "passphrase": network["key"]})
        if not keep_epoch:
            os.environ["CARPLAY_NETWORK_EPOCH"] = secrets.token_hex(4)
        platform.start("receiver", [
            sys.executable, str(root / "run-receiver.py"), "--root", str(root), "--bind", str(network["address"].ip),
            "--channel", str(network["channel"]), "--security", network["security"],
            "--hotspot", str(handoff_file), "--interface", network["device"], "--web-host", "0.0.0.0",
            "--java", config["java"], "--settings-port", str(port), "--viewer-port", str(viewer),
            "--wired", "--wired-status", str(WIRED_STATUS), "--cable-off-file", str(RUN / "cable-carplay-off"),
            "--idle-release", str(int(config["idle_release"]) if config["idle_release"].isdigit() else 300)]
            + ([] if wireless else ["--no-wireless"]) + (["--bind-wait", "120"] if wait else [])
            + car_audio_args(config))

    try:
        offline_checks.restore()  # restore a crashed run before updating own-name DNS records
        mark("supervisor start")
        # Full logs on the SD card / flash, so that a drive can be analysed after the power is cut.
        diagnostics.LogMirror(state / "logs", RUN, platform.boot_id(), journal=False).start(stopped)

        # Bluetooth at once, beside the Wi-Fi: the F50's controller takes ~10 s to come up, about as
        # long as its Wi-Fi (supervisor.py does the same). A router without BlueZ runs wired only.
        if config["bluetooth_service"]:
            platform.start("bluetooth", shlex.split(config["bluetooth_service"]))
        has_bluez = bool(config["bluetooth_service"] or shutil.which("bluetoothctl"))
        bluetooth = []

        def power_bluetooth():
            try:   # bluetoothd (or the board helper's bluetoothctl) may come up after us
                return platform.power_bluetooth()
            except (subprocess.SubprocessError, FileNotFoundError):
                return False

        def bring_up_bluetooth():
            if not has_bluez:
                status["bluetooth_error"] = "没有安装 BlueZ（bluez-daemon/bluetoothctl）"
                say("No BlueZ (bluez-daemon not installed): wired CarPlay only")
                return bluetooth.append(False)
            try:
                wait_for(power_bluetooth, 60 if config["bluetooth_service"] else 40, interval=0.5)
                mark("bluetooth powered")
                bluetooth.append(True)
            except TimeoutError:
                # The board helper's own reason (rfkill, btattach) is in state/logs; the page shows this line.
                status["bluetooth_error"] = "蓝牙适配器没有上电（详见诊断/日志）"
                say("Bluetooth adapter did not power on: wired CarPlay only")
                bluetooth.append(False)
            except InterruptedError:
                status["bluetooth_error"] = "蓝牙启动被中断"
                bluetooth.append(False)

        bluetooth_thread = threading.Thread(target=bring_up_bluetooth, name="bluetooth-power", daemon=True)
        bluetooth_thread.start()

        # The JVM needs several seconds before it first invites the iPhone: start it on what the Wi-Fi
        # configuration says while the radio comes up, and check it against the live Wi-Fi below.
        early = None
        if have_identity:
            early, _ = platform.handoff(config["wifi"], live=False, external=config)
            if early:
                ensure_leaf(state, [config["name"], setup_wizard.MDNS_NAME], [str(early["address"].ip)])
                launch(early, has_bluez, wait=True)
                mark(f"receiver launched early for {early['ssid']}")

        # The Wi-Fi comes up some seconds after procd starts us; wait for it rather than fail. The settings page
        # opens meanwhile, so that a Wi-Fi that never comes up (the router's own switched off) still leaves a page
        # saying why (2026-10-19).
        reason = ""
        network = None
        planned = early or platform.handoff(config["wifi"], live=False, external=config)[0]
        settings_open = False
        while not stopped.is_set():
            network, why = platform.handoff(config["wifi"], external=config)
            if network:
                break
            if why != reason:
                say(f"waiting: {why}")
                reason = why
                status["warning"] = waiting_warning(why)
            if not settings_open and not stopped.wait(5):   # a Wi-Fi coming up as usual needs a few seconds more
                lan = None if planned else platform.interface_network("lan")
                trusted[:] = waiting_trust(planned, lan)
                # Without any address the page answers the router itself only: an online update still sees the
                # service up (update.py).
                host = str((planned["address"] if planned else lan).ip) if trusted else "127.0.0.1"
                status["home"] = f"http://{host}:{port}/"
                settings.start(port)
                settings_open = True
                say(f"settings page open on {host} while waiting for the Wi-Fi")
                continue
            stopped.wait(1)
        if stopped.is_set():
            raise InterruptedError("Service stopping")
        status.pop("warning", None)
        mark(f"Wi-Fi {network['ssid']} up on channel {network['channel']}")
        moved, warning = apply_shared_lan("1" in (config["shared_lan"], config["tesla_mode"]), network, state, say, stopped)
        if moved:
            request_reexec("LAN address changed")   # the name, certificate and DuckDNS follow on the new address
            raise InterruptedError("LAN address changed")
        if warning:
            status["warning"] = warning
        address = network["address"]
        platform.interface, platform.ap_interface = network["device"], network["ifname"]
        host = str(address.ip)
        # The name resolves through the router's own DNS, i.e. for devices on its access point (or another
        # router's Wi-Fi bridged into its LAN) only; on a client network (car hotspot, portable Wi-Fi) the head
        # unit asks that network's DNS instead.
        named = own_lan(network)
        public_mode = config["public_ipv6"] if config["public_ipv6"] in ("1", "2") else "0"

        def point(public):
            # The public own name resolves normally online. OfflineChecks owns its A-mode answer.
            upstream_name = config["tesla_mode"] == "1" and ddns is not None
            # No local answer here, and Tesla mode's LAN address is in 100.64.0.0/10: dnsmasq counts a forwarded
            # answer there as a rebind attack (2.93, OpenWrt 25.12.5), so this one name is allowed through while
            # the box itself is the access point's DNS (2026-10-23).
            try:
                previous = (state / REBIND_NAME).read_text().strip() or None
            except OSError:
                previous = None
            if point_name(config["name"], None if upstream_name else host,
                          None if upstream_name else public, public_mode == "2" and not upstream_name,
                          rebind=upstream_name, previous=previous):
                say(f"own name uses upstream DNS (dnsmasq rebind whitelist: /{config['name']}/)" if upstream_name
                    else f"{config['name']} now points at "
                    + " and ".join(a for a in ([] if public and public_mode == "2" else [host]) + [public] if a))
            try:
                if upstream_name:
                    (state / REBIND_NAME).write_text(config["name"] + "\n")
                elif previous:
                    (state / REBIND_NAME).unlink()
            except OSError:
                pass

        public = platform.public_ipv6(network["device"]) if named and public_mode != "0" else None
        if named:
            point(public)
        renewed = ensure_leaf(state, [config["name"], setup_wizard.MDNS_NAME], [host])
        # The DuckDNS name also works on a client network (the head unit asks the public record), once its
        # certificate is in use.
        status.update(ssid=network["ssid"], home=f"http://{host}:{port}/",
                      entries=([f"https://{config['name']}:{viewer}/"] if named or (ddns and own_domain.serving(state)) else [])
                      + [f"https://{host}:{viewer}/"])
        if ddns:
            maintainer.append(own_domain.Maintainer(root, ddns, host, stopped, say, tesla=config["tesla_mode"] == "1"))
            maintainer[0].start()
        if network["mode"] != "ap" and platform.zone_input(network["network"]) not in ("ACCEPT", None):
            # A client network usually sits in the wan zone, which rejects the iPhone's AirPlay connections.
            status["warning"] = (f"「{network['ssid']}」所在的防火墙区域拒绝入站连接，iPhone 连不上 DashPlay。"
                                 f"请在 OpenWrt 后台把网络 {network['network']} 放进允许入站的区域，或改用路由器自己的 WiFi。")
            say(f"warning: firewall zone of {network['network']} rejects input")
        base_warning = status.get("warning", "")
        shared_on = "1" in (config["shared_lan"], config["tesla_mode"])
        refresh_network_warning(shared_on, network["device"], status, base_warning)
        trusted[:] = trusted_networks(network)
        if not settings_open:
            settings.start(port)
        offline_checks.update(config, network, status)
        diagnostics.start(stopped, diagnostics.DiagLog(state / "diag"), RUN / "receiver.log", platform, status,
                          state=state)
        # A new release is looked for once a day and only shown (LuCI, settings page); nothing installs by itself.
        threading.Thread(target=update.checker, args=(root, stopped, config["update_check"] != "0"),
                         name="update-check", daemon=True).start()

        bluetooth_thread.join()
        wireless = bool(bluetooth and bluetooth[0])
        # Keep the owner's way of use: a transient Bluetooth failure must not rewrite it to wired only.
        setup = setup_wizard.load(state)
        setup_wizard.save(state, {**setup, "done": True})
        if have_identity:
            # A renewed certificate (or a new CA) needs a new receiver: it reads the keystore once (audit A6).
            if early and plan_of(early) == plan_of(network) and wireless == has_bluez and not renewed:
                mark("early receiver kept")
            else:
                if early:
                    platform.stop_child("receiver")
                    mark("early receiver stopped: the live Wi-Fi, Bluetooth or certificate differs")
                launch(network, wireless)
        else:
            say("MFi identity missing: settings page only, CarPlay starts once the files are added")
        say(f"DashPlay services started on {network['ssid']} ({host}, channel {network['channel']})")
        plan = plan_of(network)
        next_check = time.monotonic() + RECHECK_EVERY
        while not stopped.wait(2):
            if receiver_refresh:
                receiver_refresh.clear()
                try:
                    latest = audio_routes.read_uci()
                except ValueError:
                    say('Sound settings unavailable; keeping receiver running and retrying')
                    receiver_refresh.append('receiver options changed')
                else:
                    for name in (*audio_routes.NAMES, 'main'):
                        config.pop('audio_route_' + name, None)
                    config.update({key: value for key, value in latest.items() if key.startswith('audio_route_')})
                    platform.stop_child('receiver')
                    if have_identity:
                        launch(network, wireless, keep_epoch=True)
            if not have_identity and identity_ok():
                request_reexec("MFi identity added")
                continue
            for name, child in platform.children:
                if name == 'tesla-dns-log':
                    continue  # optional diagnostic collector is retried by OfflineChecks
                if child.poll() is not None:
                    raise RuntimeError(f"{name} exited with {child.returncode}")
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + RECHECK_EVERY
                offline_checks.update(config, network, status)
                refresh_network_warning(shared_on, network["device"], status, base_warning)
                now, why = platform.handoff(config["wifi"], external=config)
                if now is None or plan_of(now) != plan:
                    request_reexec("Wi-Fi changed" if now else why)
                elif named and public_mode != "0":
                    # The mobile network may hand out a new prefix when it reconnects.
                    latest = platform.public_ipv6(network["device"])
                    if latest != public:
                        public = latest
                        point(public)
    except InterruptedError:
        pass
    finally:
        stopped.set()
        for worker in maintainer:
            worker.join(10)   # acme.sh is stopped first: no signer left running into the restarted service
        offline_checks.restore()
        settings.stop()
        platform.stop_all()
        say("DashPlay services stopped")
    if reexec and reexec[0] is not None:
        say(f"restarting: {reexec[0]}")
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), "--root", str(root)])


if __name__ == "__main__":
    main()
