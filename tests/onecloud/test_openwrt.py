"""OpenWrt adapter: Wi-Fi chosen from ubus/uci output, settings page without network setup."""
import ipaddress
import json
import re
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import platform_openwrt as ow
from settings_page import SettingsPage
import setup_wizard
import supervisor_openwrt
from supervisor_openwrt import (name_changes, read_config, trusted_networks, shared_lan_changes,
                               shared_lan_conflict, apply_shared_lan, car_audio_args, waiting_trust, waiting_warning)


class ViewerPortConfigTests(unittest.TestCase):
    def test_default_is_empty(self):
        # Empty lets the supervisor adopt an owner's older state/receiver/config.json port; 18443 is the fallback.
        self.assertEqual(read_config("")["viewer_port"], "")

    def test_parsed(self):
        self.assertEqual(read_config("dashplay.main.viewer_port='19000'")["viewer_port"], "19000")

# Shape of `ubus call network.wireless status` on OpenWrt 25.12 (two radios, a 2.4 GHz AP listed first).
WIRELESS = {
    "radio0": {"up": True, "config": {"band": "2g", "channel": "1"}, "interfaces": [
        {"section": "default_radio0", "ifname": "phy0-ap0",
         "config": {"mode": "ap", "ssid": "Home-2G", "encryption": "psk2", "key": "secret-2g", "network": ["lan"]}}]},
    "radio1": {"up": True, "config": {"band": "5g", "channel": "auto"}, "interfaces": [
        {"section": "default_radio1", "ifname": "phy1-ap0",
         "config": {"mode": "ap", "ssid": "Car-5G", "encryption": "sae-mixed", "key": "secret-5g", "network": ["lan"]}},
        {"section": "wwan", "ifname": "phy1-sta0",
         "config": {"mode": "sta", "ssid": "Xpeng", "encryption": "psk2", "key": "car-pass", "network": ["wwan"]}}]},
}
LAN = {"up": True, "l3_device": "br-lan", "ipv4-address": [{"address": "192.168.1.1", "mask": 24}]}
FIREWALL = """firewall.@zone[0]=zone
firewall.@zone[0].name='lan'
firewall.@zone[0].network='lan'
firewall.@zone[0].input='ACCEPT'
firewall.@zone[1]=zone
firewall.@zone[1].name='wan'
firewall.@zone[1].network='wan' 'wan6' 'wwan'
firewall.@zone[1].input='REJECT'
firewall.@rule[0]=rule
firewall.@rule[0].network='wwan'
"""


class HandoffTests(unittest.TestCase):
    def test_security_types_the_iphone_can_be_handed(self):
        self.assertEqual(ow.handoff_security("none"), "open")
        self.assertEqual(ow.handoff_security("psk2+ccmp"), "wpa2")
        self.assertEqual(ow.handoff_security("psk-mixed"), "wpa2")
        self.assertEqual(ow.handoff_security("sae-mixed"), "wpa2")   # transition is announced as WPA2
        self.assertEqual(ow.handoff_security("sae"), "wpa3")
        for enterprise in ("wpa2", "wpa3", "wpa-mixed", "owe"):
            self.assertIsNone(ow.handoff_security(enterprise))

    def test_auto_prefers_an_up_5ghz_access_point(self):
        network, reason = ow.pick_network(ow.wifi_networks(WIRELESS))
        self.assertEqual((network["ssid"], network["ifname"], network["network"]), ("Car-5G", "phy1-ap0", "lan"))
        self.assertEqual(reason, "")

    def test_named_section_may_be_a_client_network(self):
        network, _ = ow.pick_network(ow.wifi_networks(WIRELESS), "wwan")
        self.assertEqual((network["mode"], network["ssid"]), ("sta", "Xpeng"))
        self.assertEqual(ow.pick_network(ow.wifi_networks(WIRELESS), "nope"),
                         (None, "Wi-Fi section nope not found or switched off"))

    def test_waits_while_the_radio_is_down_and_skips_enterprise(self):
        down = json.loads(json.dumps(WIRELESS))
        for radio in down.values():
            radio["up"] = False
        network, reason = ow.pick_network(ow.wifi_networks(down))
        self.assertIsNone(network)
        self.assertIn("is not up yet", reason)
        enterprise = {"radio0": {"up": True, "config": {"band": "5g"}, "interfaces": [
            {"section": "x", "ifname": "phy0-ap0", "config": {"mode": "ap", "ssid": "Corp", "encryption": "wpa2",
                                                               "network": ["lan"]}}]}}
        self.assertIn("password type", ow.pick_network(ow.wifi_networks(enterprise))[1])

    def test_channel_address_and_firewall_parsing(self):
        self.assertEqual(ow.parse_channel("Interface phy1-ap0\n\tchannel 149 (5745 MHz), width: 80 MHz"), 149)
        self.assertIsNone(ow.parse_channel("Interface phy1-ap0\n\ttype AP"))
        self.assertEqual(ow.interface_address(LAN), ("br-lan", ipaddress.IPv4Interface("192.168.1.1/24")))
        self.assertEqual(ow.interface_address({"up": False}), (None, None))
        self.assertEqual(ow.zone_input(FIREWALL, "lan"), "ACCEPT")
        self.assertEqual(ow.zone_input(FIREWALL, "wwan"), "REJECT")
        self.assertIsNone(ow.zone_input(FIREWALL, "guest"))

    def test_handoff_reads_ubus_and_the_live_channel(self):
        calls = {"network.wireless": WIRELESS, "network.interface.lan": LAN}

        class Done:
            def __init__(self, stdout): self.stdout, self.returncode = stdout, 0

        def run(argv, **_):
            if argv[0] == "ubus":
                return Done(json.dumps(calls.get(argv[2], {})))
            return Done("Interface phy1-ap0\n\tchannel 36 (5180 MHz), width: 80 MHz")

        with tempfile.TemporaryDirectory() as tmp, patch.object(ow.subprocess, "run", run):
            network, reason = ow.OpenWrtPlatform(run=Path(tmp)).handoff()
        self.assertEqual(reason, "")
        self.assertEqual((network["channel"], network["security"], network["device"], str(network["address"].ip)),
                         (36, "wpa2", "br-lan", "192.168.1.1"))

    def test_another_routers_wifi_is_handed_as_entered(self):
        # A travel router bridged into the LAN (cable or USB): its Wi-Fi is not in /etc/config/wireless.
        config = read_config("dashplay.main.wifi='external'\ndashplay.main.external_ssid='Travel-5G'\n"
                             "dashplay.main.external_key='travel-pass'\ndashplay.main.external_channel='149'\n")
        network, reason = ow.external_network(config)
        self.assertEqual(reason, "")
        self.assertEqual((network["mode"], network["ssid"], network["key"], network["network"], network["channel"],
                          network["ifname"]), ("external", "Travel-5G", "travel-pass", "lan", 149, ""))
        self.assertEqual(ow.handoff_security(network["encryption"]), "wpa2")
        wpa3 = dict(config, external_security="wpa3", external_network="guest")
        self.assertEqual((ow.external_network(wpa3)[0]["encryption"], ow.external_network(wpa3)[0]["network"]),
                         ("sae", "guest"))
        self.assertEqual(ow.external_network(dict(config, external_security="open", external_key=""))[0]["key"], "")

    def test_another_routers_wifi_needs_its_name_password_and_channel(self):
        config = {"external_ssid": "Travel-5G", "external_key": "travel-pass", "external_security": "wpa2",
                  "external_channel": "149", "external_network": "lan"}
        for change, why in (({"external_ssid": ""}, "Wi-Fi name"), ({"external_ssid": "x" * 33}, "Wi-Fi name"),
                            ({"external_key": "short"}, "password"), ({"external_key": ""}, "password"),
                            ({"external_channel": ""}, "channel"), ({"external_channel": "auto"}, "channel"),
                            ({"external_channel": "0"}, "channel"), ({"external_security": "wep"}, "password type"),
                            ({"external_network": "lan wan"}, "network")):
            network, reason = ow.external_network(dict(config, **change))
            self.assertIsNone(network, change)
            self.assertIn(why, reason)

    def test_handoff_of_another_routers_wifi_uses_the_lan_address_and_entered_channel(self):
        config = {"external_ssid": "Travel-5G", "external_key": "travel-pass", "external_security": "wpa2",
                  "external_channel": "149", "external_network": "lan"}
        commands = []

        class Done:
            def __init__(self, stdout): self.stdout, self.returncode = stdout, 0

        def run(argv, **_):
            commands.append(argv)
            return Done(json.dumps({"network.interface.lan": LAN}.get(argv[2], {})) if argv[0] == "ubus" else "")

        with tempfile.TemporaryDirectory() as tmp, patch.object(ow.subprocess, "run", run):
            network, reason = ow.OpenWrtPlatform(run=Path(tmp)).handoff("external", external=config)
        self.assertEqual(reason, "")
        self.assertEqual((network["ssid"], network["channel"], network["security"], network["device"],
                          str(network["address"].ip)), ("Travel-5G", 149, "wpa2", "br-lan", "192.168.1.1"))
        self.assertNotIn(["ubus", "call", "network.wireless", "status"], commands)   # not one of our radios
        self.assertFalse([argv for argv in commands if argv[0] == "iw"])


class TemperatureTests(unittest.TestCase):
    def test_the_drive_log_takes_the_hottest_readable_zone(self):
        # F50, 2026-10-19: the drive log had no temperature; its zones are named (soc, nr, pa), two cannot be read.
        with tempfile.TemporaryDirectory() as tmp:
            for name, value in (("thermal_zone0", "33987\n"), ("thermal_zone1", "36670\n"), ("thermal_zone2", "")):
                (Path(tmp) / name).mkdir()
                (Path(tmp) / name / "temp").write_text(value)
            (Path(tmp) / "thermal_zone3").mkdir()
            self.assertEqual(ow.hottest(Path(tmp)), 36.7)
            self.assertIsNone(ow.hottest(Path(tmp) / "none"))


class TrustedNetworkTests(unittest.TestCase):
    def test_only_the_network_of_the_wifi_handed_to_the_iphone(self):
        # Every interface used to count, the WAN too (F50: sipa_eth0 10.3.178.29/8, the whole 10/8);
        # only the firewall kept it out (audit 2026-10-13, A5).
        lan = {"network": "lan", "address": ipaddress.IPv4Interface("192.168.77.1/24")}
        self.assertEqual(trusted_networks(lan), [ipaddress.IPv4Network("192.168.77.0/24")])
        self.assertNotIn(ipaddress.ip_address("10.3.178.30"), trusted_networks(lan)[0])
        car = {"network": "wwan", "address": ipaddress.IPv4Interface("192.168.43.123/24")}
        self.assertEqual(trusted_networks(car), [ipaddress.IPv4Network("192.168.43.0/24")])


# `ip -6 -o addr show scope global` and `ip -6 -o route show default` on the F50 (2026-10-13): the public
# address is on the mobile modem; the LAN has a unique local prefix and hands the modem's /64 on by relay.
F50_ADDR = """13: sipa_eth0    inet6 2408:8409:8500:1d4:a00:1ff:fe00:1/64 scope global dynamic mngtmpaddr \\       valid_lft forever preferred_lft forever
34: br-lan    inet6 fd9e:30a4:d99::1/60 scope global noprefixroute \\       valid_lft forever preferred_lft forever
"""
F50_ROUTE = """default via fe80::1 dev sipa_eth0 proto ra metric 1024 expires 62108sec pref medium
default via fe80::1 dev sipa_eth0 proto static metric 4096 pref medium
"""



class WaitingForWifiTests(unittest.TestCase):
    # 2026-10-19 (momo): the F50's own Wi-Fi turned off, the settings page never opened while DashPlay waited for it.
    def test_the_settings_page_trusts_the_planned_wifi_or_else_the_lan(self):
        planned = {"address": ipaddress.IPv4Interface("192.168.77.1/24")}
        self.assertEqual(waiting_trust(planned, None), [ipaddress.IPv4Network("192.168.77.0/24")])
        self.assertEqual(waiting_trust(None, ipaddress.IPv4Interface("192.168.1.1/24")),
                         [ipaddress.IPv4Network("192.168.1.0/24")])
        self.assertEqual(waiting_trust(None, None), [])

    def test_a_switched_off_access_point_is_named_as_such(self):
        # A disabled wifi-iface is left out of `ubus call network.wireless status` altogether (F50, 2026-10-19).
        radio_only = {"radio0": {"up": True, "config": {"band": "5g", "channel": "149"}, "interfaces": []}}
        self.assertEqual(ow.pick_network(ow.wifi_networks(radio_only)),
                         (None, "no Wi-Fi network is switched on (access point or client)"))
        self.assertEqual(ow.pick_network(ow.wifi_networks(radio_only), "ap0"),
                         (None, "Wi-Fi section ap0 not found or switched off"))

    def test_the_page_says_what_it_waits_for(self):
        self.assertIn("还没准备好：「DashPlay-F50」还没开启。", waiting_warning("Wi-Fi DashPlay-F50 is not up yet"))
        self.assertIn("路由器的 WiFi 都关着", waiting_warning("no Wi-Fi network is switched on (access point or client)"))
        self.assertIn("选的 WiFi（ap0）不存在或已关闭", waiting_warning("Wi-Fi section ap0 not found or switched off"))
        self.assertIn("「Car-5G」所在的网络 lan 还没有地址",
                      waiting_warning("network lan for Wi-Fi Car-5G has no IPv4 address yet"))
        self.assertIn("the other router's Wi-Fi channel is missing",   # anything else as it is
                      waiting_warning("the other router's Wi-Fi channel is missing: set a fixed channel there and enter it"))

    def test_a_hanging_ubus_is_no_address(self):
        def hang(argv, **kwargs):
            raise ow.subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
        with patch.object(ow.subprocess, "run", hang):
            self.assertIsNone(ow.OpenWrtPlatform.interface_network("lan"))
            self.assertEqual(ow._ubus("network.wireless"), {})

    def test_the_lan_address_comes_from_ubus(self):
        class Done:
            def __init__(self, stdout): self.stdout, self.returncode = stdout, 0

        with patch.object(ow.subprocess, "run", lambda argv, **_: Done(json.dumps(LAN if argv[2] == "network.interface.lan" else {}))):
            self.assertEqual(ow.OpenWrtPlatform.interface_network("lan"), ipaddress.IPv4Interface("192.168.1.1/24"))
            self.assertIsNone(ow.OpenWrtPlatform.interface_network("guest"))

class PublicIpv6Tests(unittest.TestCase):
    def test_the_modems_address_when_the_lan_has_only_a_local_prefix(self):
        self.assertEqual(ow.public_ipv6(F50_ADDR, F50_ROUTE, "br-lan"), "2408:8409:8500:1d4:a00:1ff:fe00:1")

    def test_an_address_on_the_lan_comes_first(self):
        delegated = F50_ADDR + "34: br-lan    inet6 240e:3b0:1234:5600::1/64 scope global dynamic noprefixroute \\ valid_lft 3000sec\n"
        self.assertEqual(ow.public_ipv6(delegated, F50_ROUTE, "br-lan"), "240e:3b0:1234:5600::1")

    def test_local_temporary_and_unready_addresses_do_not_count(self):
        addr = """13: wan@eth0    inet6 2408:8409::aa/64 scope global temporary dynamic \\ valid_lft 3000sec
13: wan@eth0    inet6 2408:8409::bb/64 scope global deprecated dynamic \\ valid_lft 3000sec
13: wan@eth0    inet6 2408:8409::cc/64 scope global tentative \\ valid_lft 3000sec
34: br-lan    inet6 fd9e:30a4:d99::1/60 scope global noprefixroute \\ valid_lft forever
"""
        self.assertIsNone(ow.public_ipv6(addr, "default via fe80::1 dev wan proto ra\n", "br-lan"))
        self.assertEqual(ow.public_ipv6(addr + "13: wan@eth0    inet6 2408:8409::dd/64 scope global dynamic\n",
                                        "default via fe80::1 dev wan proto ra\n", "br-lan"), "2408:8409::dd")

    def test_none_without_an_ipv6_route_out(self):
        self.assertIsNone(ow.public_ipv6(F50_ADDR, "", "br-lan"))
        self.assertIsNone(ow.public_ipv6("", "", "br-lan"))


DHCP = """dhcp.cfg01411c=dnsmasq
dhcp.cfg01411c.domain='lan'
dhcp.cfg01411c.local='/lan/'
dhcp.lan=dhcp
"""
NAME, LAN_IP, PUBLIC = "car.duckdns.org", "192.168.77.1", "2408:8409:8500:1d4:a00:1ff:fe00:1"


def applied(uci, changes):
    """The `uci show dhcp` that the changes leave behind (enough of uci for these records)."""
    lines = uci.splitlines()
    for change in changes:
        verb, arg = change[0], change[1] if len(change) > 1 else ""
        key, _, value = arg.partition("=")
        if verb == "set":
            lines = [l for l in lines if not l.startswith(key + "=")]
            lines.append(f"{key}={value}" if "." not in key[5:] else f"{key}='{value}'")
        elif verb == "delete":
            lines = [l for l in lines if not (l.startswith(key + "=") or l.startswith(key + "."))]
        elif verb in ("add_list", "del_list"):
            old = next((l for l in lines if l.startswith(key + "=")), None)
            values = re.findall(r"'([^']*)'", old) if old else []
            values = values + [value] if verb == "add_list" else [v for v in values if v != value]
            lines = [l for l in lines if l != old] + ([key + "=" + " ".join(f"'{v}'" for v in values)] if values else [])
    return "\n".join(lines) + "\n"


class NameRecordTests(unittest.TestCase):
    """uci dashplay.main.public_ipv6: the name also (1) or only (2) answers with the router's public IPv6."""

    def test_tesla_online_own_domain_returns_to_public_resolution(self):
        start = applied(DHCP, name_changes(DHCP, NAME, LAN_IP, PUBLIC, only_public=True))
        tesla = applied(start, name_changes(start, NAME, None, rebind=True))
        self.assertNotIn('dhcp.dashplay=', tesla)
        self.assertNotIn('dhcp.dashplay6=', tesla)
        self.assertNotIn(f"dhcp.cfg01411c.server='/{NAME}/'", tesla, 'no local A answer')
        self.assertIn(f"dhcp.cfg01411c.rebind_domain='/{NAME}/'", tesla, 'the upstream answer is let through')
        normal = applied(tesla, name_changes(tesla, NAME, None, previous=NAME))
        self.assertNotIn('rebind_domain', normal)
        self.assertNotIn('server=', normal)
        self.assertEqual(name_changes(normal, NAME, None), [])

    def test_tesla_own_domain_is_whitelisted_for_rebind_protection(self):
        changes = name_changes(DHCP, NAME, None, rebind=True)
        self.assertIn(["add_list", f"dhcp.cfg01411c.rebind_domain=/{NAME}/"], changes)
        after = applied(DHCP, changes)
        self.assertIn(f"dhcp.cfg01411c.rebind_domain='/{NAME}/'", after)
        self.assertNotIn('server=', after, 'the name is still not answered locally')
        self.assertEqual(name_changes(after, NAME, None, rebind=True, previous=NAME), [], 'nothing to do when current')

    def test_a_renamed_domain_replaces_the_whitelist_entry(self):
        tesla = applied(DHCP, name_changes(DHCP, NAME, None, rebind=True))
        renamed = applied(tesla, name_changes(tesla, 'dashplay.lan', None, rebind=True, previous=NAME))
        self.assertNotIn(f'/{NAME}/', renamed)
        self.assertIn("dhcp.cfg01411c.rebind_domain='/dashplay.lan/'", renamed)

    def test_turning_tesla_mode_off_removes_the_whitelist(self):
        tesla = applied(DHCP, name_changes(DHCP, NAME, None, rebind=True))
        back = applied(tesla, name_changes(tesla, NAME, LAN_IP, previous=NAME))
        self.assertNotIn('rebind_domain', back)
        self.assertIn(f"dhcp.dashplay.ip='{LAN_IP}'", back)
        self.assertEqual(name_changes(back, NAME, LAN_IP), [])

    def test_another_programs_whitelist_entries_are_left_alone(self):
        other = DHCP + "dhcp.cfg01411c.rebind_domain='/other.example/'\n"
        tesla = applied(other, name_changes(other, NAME, None, rebind=True))
        self.assertIn("'/other.example/'", tesla)
        back = applied(tesla, name_changes(tesla, NAME, LAN_IP, previous=NAME))
        self.assertNotIn(f'/{NAME}/', back)
        self.assertIn("'/other.example/'", back)

    def test_the_lan_address_by_default(self):
        changes = name_changes(DHCP, NAME, LAN_IP)
        after = applied(DHCP, changes)
        self.assertIn(f"dhcp.dashplay.ip='{LAN_IP}'", after)
        self.assertNotIn("dashplay6", after)
        self.assertNotIn("rebind_domain", after, 'other modes do not touch the rebind whitelist')
        self.assertEqual(name_changes(after, NAME, LAN_IP), [], "nothing to do when current")

    def test_beside_the_lan_address(self):
        after = applied(DHCP, name_changes(DHCP, NAME, LAN_IP, PUBLIC))
        self.assertIn(f"dhcp.dashplay.ip='{LAN_IP}'", after)
        self.assertIn(f"dhcp.dashplay6.ip='{PUBLIC}'", after)
        self.assertNotIn(f"/{NAME}/", after, "both types are answered locally")

    def test_instead_of_the_lan_address_and_back(self):
        start = applied(DHCP, name_changes(DHCP, NAME, LAN_IP))
        alone = applied(start, name_changes(start, NAME, LAN_IP, PUBLIC, only_public=True))
        self.assertNotIn("dhcp.dashplay=", alone)
        self.assertIn(f"dhcp.dashplay6.ip='{PUBLIC}'", alone)
        self.assertIn(f"dhcp.cfg01411c.server='/{NAME}/'", alone, "no A record from upstream")
        self.assertEqual(name_changes(alone, NAME, LAN_IP, PUBLIC, only_public=True), [])
        back = applied(alone, name_changes(alone, NAME, LAN_IP))
        self.assertIn(f"dhcp.dashplay.ip='{LAN_IP}'", back)
        self.assertNotIn("dashplay6", back)
        self.assertNotIn(f"/{NAME}/", back)

    def test_only_public_without_a_public_address_keeps_the_lan_address(self):
        after = applied(DHCP, name_changes(DHCP, NAME, LAN_IP, None, only_public=True))
        self.assertIn(f"dhcp.dashplay.ip='{LAN_IP}'", after)
        self.assertNotIn(f"/{NAME}/", after)

    def test_a_new_prefix_or_name_replaces_the_old_records(self):
        alone = applied(DHCP, name_changes(DHCP, NAME, LAN_IP, PUBLIC, only_public=True))
        moved = applied(alone, name_changes(alone, NAME, LAN_IP, "2408:8409:8500:77::1", only_public=True))
        self.assertIn("dhcp.dashplay6.ip='2408:8409:8500:77::1'", moved)
        renamed = applied(moved, name_changes(moved, "dashplay.lan", LAN_IP, "2408:8409:8500:77::1", only_public=True))
        self.assertNotIn(f"/{NAME}/", renamed)
        self.assertIn("'/dashplay.lan/'", renamed)


# `uci -q show network` and `ip -4 -o addr show` of the F50 (2026-10-14), without its Tailscale line.
NETWORK = """network.lan=interface
network.lan.device='br-lan'
network.lan.proto='static'
network.lan.ipaddr='192.168.77.1'
network.lan.ip6assign='60'
network.lan.netmask='255.255.255.0'
network.wan=interface
network.wan.proto='dhcp'
"""
ADDRESSES = """1: lo    inet 127.0.0.1/8 scope host lo
13: sipa_eth0    inet 10.123.116.156/8 scope global sipa_eth0
34: br-lan    inet 192.168.77.1/24 brd 192.168.77.255 scope global br-lan
"""


class SharedLanTests(unittest.TestCase):
    """uci dashplay.main.shared_lan: the LAN moves to 100.96.0.1/24 for browsers refusing private addresses (Tesla)."""

    def test_on_moves_the_lan_and_remembers_its_address(self):
        changes, remember, why = shared_lan_changes(NETWORK, "lan", True)
        after = applied(NETWORK, changes)
        self.assertEqual(why, "")
        self.assertIn("network.lan.ipaddr='100.96.0.1'", after)
        self.assertIn("network.lan.netmask='255.255.255.0'", after)
        self.assertEqual(remember, {"ipaddr": "192.168.77.1", "netmask": "255.255.255.0"})
        self.assertEqual(shared_lan_changes(after, "lan", True, remember)[0], [], "nothing to do when current")
        self.assertIn("network.wan.proto='dhcp'", after, "other interfaces untouched")

    def test_off_puts_the_address_back_once(self):
        changes, remember, _ = shared_lan_changes(NETWORK, "lan", True)
        moved = applied(NETWORK, changes)
        back_changes, kept, _ = shared_lan_changes(moved, "lan", False, remember)
        back = applied(moved, back_changes)
        self.assertIn("network.lan.ipaddr='192.168.77.1'", back)
        self.assertIsNone(kept)
        self.assertEqual(shared_lan_changes(back, "lan", False, None)[0], [], "off and nothing remembered: no change")

    def test_an_address_with_prefix_length_comes_back_without_netmask(self):
        cidr = "network.lan=interface\nnetwork.lan.proto='static'\nnetwork.lan.ipaddr='192.168.1.1/24'\n"
        changes, remember, _ = shared_lan_changes(cidr, "lan", True)
        moved = applied(cidr, changes)
        back = applied(moved, shared_lan_changes(moved, "lan", False, remember)[0])
        self.assertIn("network.lan.ipaddr='192.168.1.1/24'", back)
        self.assertNotIn("netmask", back)

    def test_an_address_changed_by_hand_since_is_left_alone(self):
        moved = applied(NETWORK, shared_lan_changes(NETWORK, "lan", True)[0])
        by_hand = moved.replace("100.96.0.1", "10.10.0.1")
        changes, kept, _ = shared_lan_changes(by_hand, "lan", False, {"ipaddr": "192.168.77.1", "netmask": "255.255.255.0"})
        self.assertEqual((changes, kept), ([], None))

    def test_a_lan_without_one_static_address_is_not_changed(self):
        for uci in (NETWORK.replace("proto='static'", "proto='dhcp'"),
                    NETWORK.replace("ipaddr='192.168.77.1'", "ipaddr='192.168.77.1/24' '192.168.78.1/24'")):
            changes, _, why = shared_lan_changes(uci, "lan", True)
            self.assertEqual(changes, [])
            self.assertTrue(why)

    def test_tailscale_or_an_overlapping_interface_refuses(self):
        self.assertEqual(shared_lan_conflict(ADDRESSES, "br-lan"), "")
        with_tailscale = ADDRESSES + "37: tailscale0    inet 100.102.205.24/32 scope global tailscale0\n"
        drop = ('\t\tiifname != "tailscale0*" ip saddr 100.64.0.0/10 counter packets 0 bytes 0 drop\n')   # F50, 2026-10-14
        self.assertIn("netfilter-mode=off", shared_lan_conflict(with_tailscale, "br-lan", "table ip ts-filter {\n" + drop))
        self.assertIn("Tailscale", shared_lan_conflict(with_tailscale, "br-lan", None), "rules unreadable: assume the drop")
        self.assertEqual(shared_lan_conflict(with_tailscale, "br-lan", "table inet fw4 {\n}\n"), "",
                         "Tailscale with --netfilter-mode=off adds no drop")
        self.assertEqual(shared_lan_conflict(ADDRESSES, "br-lan", "table ip ts-filter {\n" + drop), "",
                         "rules left behind without Tailscale running")
        self.assertTrue(shared_lan_conflict(ADDRESSES.replace("10.123.116.156/8", "100.96.0.7/22"), "br-lan"))
        moved = ADDRESSES.replace("192.168.77.1/24 brd 192.168.77.255", "100.96.0.1/24 brd 100.96.0.255")
        self.assertEqual(shared_lan_conflict(moved, "br-lan"), "", "the LAN itself already there is fine")

    def test_runtime_overlap_appears_and_clears_without_changing_network(self):
        from types import SimpleNamespace
        status = {"warning": "other warning"}
        address = [ADDRESSES]
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout=address[0] if args[0] == "ip" else "")
        with tempfile.TemporaryDirectory() as tmp, patch.object(supervisor_openwrt.subprocess, "run", run):
            path = Path(tmp) / "network-status.json"
            refresh = supervisor_openwrt.refresh_network_warning
            refresh(True, "br-lan", status, "other warning", path)
            self.assertEqual(status["warning"], "other warning")
            address[0] = ADDRESSES.replace("10.123.116.156/8", "100.96.0.7/24")
            refresh(True, "br-lan", status, "other warning", path)
            self.assertIn("冲突", status["warning"])
            self.assertEqual(json.loads(path.read_text())["warning"], status["warning"])
            address[0] = ADDRESSES
            refresh(True, "br-lan", status, "other warning", path)
            self.assertEqual(status["warning"], "other warning")
            self.assertTrue(all(c[0] in ("ip", "nft") for c in calls))

    def test_apply_changes_uci_reloads_the_network_and_puts_it_back(self):
        uci = {"network": NETWORK}
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            out = ""
            if args[:3] == ["uci", "-q", "show"]:
                out = uci["network"]
            elif args[:4] == ["ip", "-4", "-o", "addr"]:
                out = ADDRESSES
            elif args[:2] == ["nft", "list"]:
                out = "table inet fw4 {\n}\n"
            elif args[:4] == ["ubus", "call", "hostapd.phy0-ap0", "get_clients"]:
                out = json.dumps({"freq": 5180, "clients": {"02:00:00:00:00:a1": {"auth": True}, "02:00:00:00:00:a2": {}}})
            elif args[0] == "uci" and args[1] != "commit":
                uci["network"] = applied(uci["network"], [args[1:]])
            return supervisor_openwrt.subprocess.CompletedProcess(args, 0, out, "")

        network = {"mode": "ap", "network": "lan", "device": "br-lan", "ifname": "phy0-ap0"}
        said = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(supervisor_openwrt.subprocess, "run", run), \
                patch.object(supervisor_openwrt, "ROLLBACK_PENDING", Path(tmp) / "snapshot-files"):
            state = Path(tmp)
            self.assertEqual(apply_shared_lan(True, network, state, said.append), (True, ""))
            self.assertIn("network.lan.ipaddr='100.96.0.1'", uci["network"])
            self.assertEqual(json.loads((state / "lan-before.json").read_text())["ipaddr"], "192.168.77.1")
            self.assertIn(["/etc/init.d/network", "reload"], calls)
            kicked = [json.loads(c[4])["addr"] for c in calls if c[:4] == ["ubus", "call", "hostapd.phy0-ap0", "del_client"]]
            self.assertEqual(sorted(kicked), ["02:00:00:00:00:a1", "02:00:00:00:00:a2"],
                             "every station rejoins for an address in the new LAN")
            self.assertGreater(calls.index(["ubus", "call", "hostapd.phy0-ap0", "get_clients"]),
                               calls.index(["/etc/init.d/network", "reload"]), "after the LAN has its new address")
            self.assertEqual(apply_shared_lan(True, network, state, said.append), (False, ""), "already there")
            self.assertEqual(apply_shared_lan(False, network, state, said.append), (True, ""))
            self.assertIn("network.lan.ipaddr='192.168.77.1'", uci["network"])
            self.assertFalse((state / "lan-before.json").exists())
            changed, warning = apply_shared_lan(True, dict(network, mode="sta"), state, said.append)
            self.assertFalse(changed)
            self.assertIn("路由器自己的 WiFi", warning)
            self.assertIn("network.lan.ipaddr='192.168.77.1'", uci["network"])
            # Another router's Wi-Fi bridged into this LAN: its devices get their address here, so the LAN moves.
            calls.clear()
            external = dict(network, mode="external", ifname="")
            self.assertEqual(apply_shared_lan(True, external, state, said.append), (True, ""))
            self.assertIn("network.lan.ipaddr='100.96.0.1'", uci["network"])
            self.assertFalse([c for c in calls if c[:2] == ["ubus", "call"]], "its stations are not ours to kick")


    def test_waits_while_a_luci_apply_awaits_its_confirm(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(supervisor_openwrt, "ROLLBACK_PENDING", Path(tmp) / "snapshot-files"):
            snapshot = Path(tmp) / "snapshot-files"
            snapshot.mkdir()
            (snapshot / "dashplay").write_text("old")
            stopped = threading.Event()
            done = threading.Event()
            worker = threading.Thread(target=lambda: (supervisor_openwrt.wait_for_confirm(stopped, 30), done.set()))
            worker.start()
            self.assertFalse(done.wait(1.5), "waits while the snapshot is there")
            (snapshot / "dashplay").unlink()   # confirmed: rpcd purges the snapshot
            self.assertTrue(done.wait(3))
            worker.join()
            supervisor_openwrt.wait_for_confirm(stopped, 30)   # no snapshot at all: at once


class ConfigTests(unittest.TestCase):
    def test_idle_release_defaults_to_five_minutes_and_can_be_turned_off(self):
        self.assertEqual(read_config("")["idle_release"], "300")
        self.assertEqual(read_config("dashplay.main.idle_release='0'\n")["idle_release"], "0")

    def test_the_public_ipv6_address_is_off_by_default(self):
        self.assertEqual(read_config("")["public_ipv6"], "0")

    def test_the_shared_lan_is_off_by_default(self):
        self.assertEqual(read_config("")["shared_lan"], "0")

    def test_tesla_mode_is_off_by_default(self):
        self.assertEqual(read_config("")["tesla_mode"], "0")

    def test_the_car_bluetooth_switches_reach_the_receiver_on_their_own(self):
        self.assertEqual(car_audio_args(read_config("")), [])
        self.assertEqual(car_audio_args(read_config("dashplay.main.car_music='1'\n")), ["--car-music"])
        self.assertEqual(car_audio_args(read_config("dashplay.main.car_voice='1'\n")), ["--car-voice"])
        self.assertEqual(car_audio_args(read_config("dashplay.main.car_music='1'\ndashplay.main.car_voice='1'\n")),
                         ["--car-music", "--car-voice"])

    def test_tesla_mode_defaults_the_car_bluetooth_but_does_not_force_it(self):
        # Tesla mode turns both on when it is enabled (LuCI); afterwards the switches are the owner's choice, as on
        # the box (2026-10-28).
        self.assertEqual(car_audio_args(read_config(
            "dashplay.main.tesla_mode='1'\ndashplay.main.car_music='1'\ndashplay.main.car_voice='1'\n")),
            ["--tesla"])
        self.assertEqual(car_audio_args(read_config(
            "dashplay.main.tesla_mode='1'\ndashplay.main.car_music='1'\ndashplay.main.car_voice='0'\n")),
            ["--tesla-browser", "--car-music"])
        self.assertEqual(car_audio_args(read_config(
            "dashplay.main.tesla_mode='1'\ndashplay.main.car_music='0'\ndashplay.main.car_voice='1'\n")),
            ["--tesla-browser", "--car-voice"])
        self.assertEqual(car_audio_args(read_config(
            "dashplay.main.tesla_mode='1'\ndashplay.main.car_music='0'\ndashplay.main.car_voice='0'\n")),
            ["--tesla-browser"])
        # A uci command that only turns Tesla mode on leaves the page as the audio source; the LuCI switch and the
        # install docs turn both switches on with it.
        self.assertEqual(car_audio_args(read_config("dashplay.main.tesla_mode='1'\n")), ["--tesla-browser"])

    def test_uci_options_override_defaults(self):
        config = read_config("dashplay.main=dashplay\ndashplay.main.wifi='wwan'\ndashplay.main.settings_port='18090'\n")
        self.assertEqual((config["wifi"], config["settings_port"], config["java"]),
                         ("wwan", "18090", "/usr/lib/dashplay-jre/bin/java"))
        self.assertEqual(read_config("")["wifi"], "auto")

    def test_another_routers_wifi_defaults_to_wpa2_on_the_lan(self):
        config = read_config("")
        self.assertEqual((config["external_ssid"], config["external_key"], config["external_security"],
                          config["external_channel"], config["external_network"]), ("", "", "wpa2", "", "lan"))



class ManagedPageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"; self.state.mkdir()
        setup_wizard.save(self.state, {"done": True, "mode": "box_hotspot"})
        self.status = {"mode": "openwrt", "ssid": "Car-5G", "home": "http://192.168.1.1:18080/", "identity": True,
                       "entries": ["https://dashplay.lan:18443/", "https://192.168.1.1:18443/"]}
        self.page = SettingsPage(self.state, lambda: self.status, lambda reason: None, lambda: [], None,
                                 Path(tmp.name) / "wired.json",
                                 managed={"by": "OpenWrt", "admin": "/cgi-bin/luci/admin/network/wireless"})

    def test_shows_phones_and_router_wifi_without_network_setup(self):
        html = self.page.render("")
        for text in ("交给 iPhone 的 WiFi", "Car-5G", "在 OpenWrt 后台设置 WiFi", "https://dashplay.lan:18443/",
                     "添加"):
            self.assertIn(text, html)
        for text in ("盒子热点", "重新进行首次设置", "使用方式", "第 1 步"):
            self.assertNotIn(text, html)
        # The settings page runs on 18080; a relative link stayed there and got a 404 (review 2026-10-12).
        self.assertIn('href="http://192.168.1.1/cgi-bin/luci/admin/network/wireless"', html)

    def test_the_tesla_internet_banner_is_shown_once(self):
        self.status["tesla_internet"] = "上游在线，局域网 CarPlay 常在线"
        html = self.page.render("")
        self.assertEqual(html.count("上游在线，局域网 CarPlay 常在线"), 1)

    def test_network_changes_are_refused(self):
        nonce = self.page.pairing.nonce
        for path in ("/setup", "/reset", "/car", "/hotspot", "/retry"):
            with self.assertRaises(ValueError):
                self.page.apply(path, {"nonce": nonce})

    def test_the_daily_check_is_shown_with_the_update_button(self):
        import update
        update.save(self.state / "update-check.json", {"checked_at": 1, "current": "2026.10.19.1", "latest": "2026.10.20",
                                                       "notes": "- 新功能<b>\n- 修复", "available": True, "online": True})
        html = self.page.render("")
        for text in ("程序更新", "有新版本 2026.10.20", "当前 2026.10.19.1", "- 新功能&lt;b&gt;<br>- 修复", "立即升级", "检查更新"):
            self.assertIn(text, html)
        update.save(self.state / "update-check.json", {"checked_at": 1, "latest": "2026.10.20", "available": True,
                                                       "online": False})
        html = self.page.render("")
        self.assertIn("用刷机包升级", html)
        self.assertNotIn("立即升级", html, "a release that changed the system is not offered online")
        update.save(self.state / "update-check.json", {"checked_at": 1, "current": "2026.10.20", "latest": "2026.10.20",
                                                       "available": False, "online": True})
        self.assertIn("已是最新", self.page.render(""))
        self.assertNotIn("本机程序", self.page.render(""), "a development build names no release")
        (self.state.parent / "version.txt").write_text("2026.10.20\n")
        self.assertIn("<b>本机程序</b></span><span class=value>2026.10.20", self.page.render(""))

    def test_a_failed_update_is_not_shown_once_the_box_is_up_to_date(self):
        # F50, 2026-10-19: updated by hand after a failed web update, the page still said it did not finish.
        import time
        import update
        update.save(self.state / "update-status.json", {"phase": "failed", "message": "下载失败", "at": time.time()})
        update.save(self.state / "update-check.json", {"checked_at": 1, "current": "2026.10.19.1", "latest": "2026.10.20",
                                                       "available": True, "online": True})
        self.assertIn("上次升级没有完成", self.page.render(""))
        update.save(self.state / "update-check.json", {"checked_at": time.time() + 1, "current": "2026.10.20",
                                                       "latest": "2026.10.20",
                                                       "available": False, "online": True})
        html = self.page.render("")
        self.assertNotIn("上次升级没有完成", html)
        self.assertIn("已是最新", html)

    def test_update_actions_need_the_page_and_start_apart(self):
        import update
        nonce = self.page.pairing.nonce
        with self.assertRaises(ValueError):
            self.page.apply("/update", {"action": "install", "nonce": "stale"})
        with patch.object(update, "launch", return_value="已开始升级") as launch, \
                patch.object(update, "check") as check:
            self.assertEqual(self.page.apply("/update", {"action": "install", "nonce": nonce}), "已开始升级")
            launch.assert_called_once_with(self.state.parent)
            self.assertIn("正在检查更新", self.page.apply("/update", {"action": "check", "nonce": nonce}))
        update.save(self.state / "update-status.json", {"phase": "installing", "message": "正在安装", "at": time.time()})
        html = self.page.render("")
        self.assertIn("正在升级", html)
        self.assertNotIn("value=install", html)

    def test_firewall_warning_is_shown(self):
        self.status["warning"] = "防火墙区域拒绝入站连接"
        self.assertIn("防火墙区域拒绝入站连接", self.page.render(""))


if __name__ == "__main__":
    unittest.main()
