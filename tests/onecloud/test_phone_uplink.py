"""Phone sharing state machine and actual adapter command order with fake kernel I/O."""
import ipaddress
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import phone_uplink
from phone_uplink import PhoneUplink, JoinedPhoneAP, station_mac, wired_uplink, LegacyPhoneUplink
from platform_debian import DebianPlatform
import setup_wizard

class PhoneUplinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state/'uplink-hotspot.json').write_text('{"ssid":"Phone","passphrase":"synthetic-pass"}')
        self.p = Mock()
        self.p.concurrent_ap.return_value = 1
        self.p.find_hotspot.return_value = (True, True, '')
        self.p.join_wifi.return_value = True
        self.p.local_addresses.return_value = []
        self.p.frequency.return_value = 5745
        self.p.uplink_gateway.return_value = '192.0.2.1'
        self.p.add_phone_ap.return_value = True
        self.p.start_ap.return_value = True
        self.p.connected.return_value = True
        self.p.ap_ready.return_value = True
        self.p.ap_channel_problem.return_value = None
        self.p.interface, self.p.ap_interface = 'wlan0', 'wlan0'
        self.p.run = self.state
        self.p.phone_resolv = self.state / 'dns/phone-resolv.conf'
        self.p.default_route_devices.return_value = []
        self.p.wired_carrier.return_value = False
        self.now = 0
        self.status = {'tesla':True,'own_name':'example.duckdns.org'}
        self.receiver = Mock()
        self.u = PhoneUplink(self.p, self.state, ipaddress.ip_interface('100.96.0.1/24'),
                            {'ssid':'Box','passphrase':'synthetic-box'},149,40,Mock(),self.receiver,
                            self.status, say=lambda _:None, clock=lambda:self.now)

    def boot(self, frequency=5745):
        self.p.phone_hotspot_frequency.return_value = frequency
        self.p.phone_link.return_value = {'connected': False, 'frequency': None}
        self.p.phone_station_address.return_value = None
        self.assertTrue(self.u.start())

    def connected(self, frequency=5745):
        self.p.phone_link.return_value = {'connected': True, 'frequency': frequency}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        self.u.tick()
        self.assertEqual(self.u.phase, 'online')

    def test_boot_on_discovered_channel_starts_receiver_only_once(self):
        self.boot(5180)
        self.receiver.assert_called_once_with(36)
        self.p.start_phone_station.assert_called_once_with(self.u.saved, 5180)
        self.connected(5180)
        self.receiver.assert_called_once_with(36)
        self.assertTrue(self.status['tesla_offline'], 'Tesla LAN stays available even online')
        self.p.cleanup_phone_uplink.assert_not_called()

    def test_late_same_channel_uplink_does_not_restart_lan(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5745
        self.p.stop_child.reset_mock()
        self.now = self.u.next_try
        self.u.tick()
        self.connected()
        self.receiver.assert_called_once_with(149)
        self.p.stop_child.assert_not_called()
        self.p.start_dhcp.assert_called_once()

    def test_brief_and_long_loss_keep_lan_and_recover(self):
        self.boot()
        self.connected()
        self.p.stop_child.reset_mock()
        self.p.phone_link.return_value = {'connected': False, 'frequency': None}
        self.u.tick()
        self.now += 4
        self.connected()
        self.p.stop_phone_station.assert_not_called()
        self.p.phone_link.return_value = {'connected': False, 'frequency': None}
        self.u.tick()
        self.now += 20
        self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.p.stop_phone_station.assert_called_once()
        self.now = self.u.next_try
        self.u.tick()
        self.connected()
        self.receiver.assert_called_once_with(149)
        self.p.start_dhcp.assert_called_once()
        self.p.stop_child.assert_not_called()

    def test_late_different_channel_rebuilds_once_but_keeps_dns(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5180
        self.now = self.u.next_try
        self.u.tick()
        self.connected(5180)
        self.assertEqual([c.args[0] for c in self.receiver.call_args_list], [149, 36])
        self.p.start_dhcp.assert_called_once()
        self.p.cleanup_phone_uplink.assert_not_called()

    def test_same_band_channel_change_still_rebuilds(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5180   # another 5 GHz channel
        self.now = self.u.next_try
        self.u.tick()
        self.p.start_phone_station.assert_called_once_with(self.u.saved, 5180)

    def test_boot_scan_timeout_still_starts_carplay_and_retries(self):
        self.p.phone_hotspot_frequency.side_effect = subprocess.TimeoutExpired('iw', 5)
        self.boot()
        self.receiver.assert_called_once_with(149)
        self.p.start_phone_station.assert_not_called()
        self.p.phone_hotspot_frequency.side_effect = None
        self.p.phone_hotspot_frequency.return_value = 5745
        self.now = self.u.next_try
        self.u.tick()
        self.connected()
        self.receiver.assert_called_once()

    def test_bad_password_or_no_dhcp_never_restarts_carplay(self):
        self.boot()
        self.now = 31
        self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.p.stop_phone_station.assert_called_once()
        self.receiver.assert_called_once()
        self.p.start_dhcp.assert_called_once()
        self.assertGreater(self.u.next_try, self.now)

    def test_route_and_dhcp_can_arrive_after_association(self):
        self.boot()
        self.p.phone_link.return_value = {'connected': True, 'frequency': 5745}
        self.u.tick()
        self.assertEqual(self.u.phase, 'connecting')
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        self.p.uplink_gateway.return_value = None
        self.u.tick()
        self.assertEqual(self.u.phase, 'connecting')
        self.p.uplink_gateway.return_value = '192.0.2.1'
        self.connected()
        self.receiver.assert_called_once()

    def test_link_query_error_is_not_a_disconnection(self):
        self.boot()
        self.connected()
        self.p.phone_link.side_effect = subprocess.TimeoutExpired('iw', 3)
        self.now += 30
        self.u.tick()
        self.assertEqual(self.u.phase, 'online')
        self.p.stop_phone_station.assert_not_called()
        self.receiver.assert_called_once()
        self.assertEqual(self.status['uplink_error'], 'TimeoutExpired')

    def test_overlapping_uplink_does_not_change_lan_address(self):
        self.boot()
        self.p.phone_link.return_value = {'connected': True, 'frequency': 5745}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('100.96.0.8/24')
        self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.assertIn('冲突', self.status['uplink']['reason'])
        self.p.stop_phone_station.assert_called_once()
        self.p.set_ap_address.assert_called_once_with(self.u.address)
        self.receiver.assert_called_once()

    def test_no_concurrency_or_prepare_failure_leaves_normal_ap_to_supervisor(self):
        self.p.concurrent_ap.return_value = 0
        self.assertFalse(self.u.start())
        self.p.prepare_phone_uplink.assert_not_called()
        self.p.concurrent_ap.return_value = 1
        self.p.add_phone_ap.side_effect = OSError('synthetic failure')
        self.p.phone_hotspot_frequency.return_value = 5745
        self.assertFalse(self.u.start())
        self.p.cleanup_phone_uplink.assert_called_once()
        self.receiver.assert_not_called()

    def test_working_cable_skips_scan_and_leaving_it_joins_phone(self):
        self.p.default_route_devices.return_value = ['eth0']
        with patch('phone_uplink.subprocess.run', return_value=Mock(returncode=0)) as probe:
            self.boot()
        self.assertEqual(probe.call_args.kwargs['timeout'], 2)
        self.assertEqual(self.u.phase, 'wired')
        self.p.phone_hotspot_frequency.assert_not_called()
        self.p.default_route_devices.return_value = []
        self.p.wired_carrier.return_value = False
        self.now = self.u.next_try
        self.u.tick()
        self.connected()
        self.receiver.assert_called_once()
        self.p.start_dhcp.assert_called_once()

    def test_no_route_cable_does_not_block_phone(self):
        self.p.default_route_devices.return_value = ['eth0']
        with patch('phone_uplink.subprocess.run', side_effect=subprocess.TimeoutExpired('resolver', 2)):
            self.boot()
        self.p.start_phone_station.assert_called_once()

    def test_wired_dns_failure_is_not_a_working_uplink(self):
        self.p.default_route_devices.return_value = ['eth0']
        with patch('phone_uplink.subprocess.run', return_value=Mock(returncode=1)):
            self.assertFalse(wired_uplink(self.p))

    def test_only_repeated_ap_failure_restarts_receiver(self):
        self.boot()
        self.connected()
        self.p.ap_ready.return_value = False
        self.u.tick(); self.u.tick()
        self.receiver.assert_called_once()
        self.u.tick()
        self.assertEqual(self.receiver.call_count, 2)
        self.p.start_dhcp.assert_called_once()

    def test_unusable_channel_never_interrupts_downstream(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5260
        self.p.ap_channel_problem.return_value = 'DFS'
        self.now = self.u.next_try
        self.u.tick()
        self.p.start_phone_station.assert_not_called()
        self.receiver.assert_called_once()

    def test_failed_channel_switch_restores_previous_ap(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5180
        self.p.start_ap.side_effect = [False, False, True]
        self.now = self.u.next_try
        self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.assertEqual(self.status['channel'], 149)
        self.p.start_phone_station.assert_not_called()
        self.assertEqual([c.args[0] for c in self.receiver.call_args_list], [149, 149])
        self.p.start_dhcp.assert_called_once()

    def test_desec_and_tesla_dns_are_stable_online_and_offline(self):
        name = 'box01.car.example.com'
        (self.state / 'ddns.json').write_text(json.dumps({
            'provider': 'desec', 'domain': name, 'token': 'synthetic-test-token'}))
        self.status['own_name'] = name
        self.u.address = ipaddress.ip_interface('100.97.0.1/24')
        self.boot()
        self.connected()
        call = self.p.start_dhcp.call_args
        self.assertEqual(call.args[2], list(setup_wizard.TESLA_CHECK_NAMES) + [name])
        platform = DebianPlatform('wlan-test', self.state)
        with patch.object(platform, 'start') as start:
            platform.start_dhcp(*call.args, **call.kwargs)
        argv = start.call_args.args[1]
        self.assertIn('--dhcp-option=3,100.97.0.1', argv)
        self.assertIn('--dhcp-option=6,100.97.0.1', argv)
        self.assertIn(f'--address=/{name}/100.97.0.1', argv)
        self.assertIn(f'--local=/{name}/', argv)
        self.assertIn('--resolv-file=' + str(self.state / 'dns/phone-resolv.conf'), argv)
        self.assertNotIn('--no-resolv', argv)
        self.p.start_dhcp.assert_called_once()

    def test_non_tesla_desec_has_local_name_without_fake_checks(self):
        name = 'box01.car.example.com'
        self.status.update(tesla=False, own_name=name)
        (self.state / 'ddns.json').write_text(json.dumps({
            'provider': 'desec', 'domain': name, 'token': 'synthetic-test-token'}))
        self.boot()
        self.connected()
        self.assertEqual(self.p.start_dhcp.call_args.args[2], [name])
        self.assertFalse(self.status['tesla_offline'])

    def test_no_domain_does_not_add_a_dns_wildcard(self):
        self.status.update(tesla=False, own_name=None)
        self.boot()
        self.assertEqual(self.p.start_dhcp.call_args.args[2], [])

    def test_runtime_scan_failure_never_rebuilds_network(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.side_effect = OSError('busy')
        self.now = self.u.next_try
        self.u.tick()
        self.receiver.assert_called_once()
        self.p.stop_child.assert_not_called()
        self.assertGreater(self.u.next_try, self.now)

    def test_mac_pair_is_stable_distinct_and_unicast(self):
        for mac in ('00:00:5e:00:53:01','02:00:5e:00:53:01'):
            derived=station_mac(mac)
            self.assertNotEqual(derived,mac)
            self.assertEqual(int(derived[:2],16)&3,2)
            self.assertEqual(derived,station_mac(mac))

    def test_platform_mac_order_nat_cleanup_and_restart_recovery(self):
        p=DebianPlatform('wlan-test',self.state,self.state/'sta.network')
        def run(argv, **kwargs):
            return Mock(returncode=0,stdout=json.dumps([{'address':'00:00:5e:00:53:01','permaddr':'00:00:5e:00:53:01'}]))
        real_read=Path.read_text
        def read(path,*a,**k):
            return '0\n' if str(path)=='/proc/sys/net/ipv4/ip_forward' else real_read(path,*a,**k)
        with patch('platform_debian.subprocess.run', side_effect=run) as command, patch.object(Path,'read_text',read), \
                patch('shutil.which', return_value=None):
            p.prepare_phone_uplink(); p.add_phone_ap(); p.enable_phone_nat(ipaddress.ip_network('100.96.0.0/24'))
            commands=[c.args[0] for c in command.call_args_list]
            sta=['ip','link','set','dev','wlan-test','address','02:00:5e:00:53:01']
            ap=['iw','dev','wlan-test','interface','add','ap0','type','__ap']
            self.assertLess(commands.index(sta),commands.index(ap))
            self.assertIn(['ip','link','set','dev','ap0','address','00:00:5e:00:53:01'],commands)
            p2=DebianPlatform('wlan-test',self.state,self.state/'sta.network')
            p2.cleanup_phone_uplink()
            commands=[c.args[0] for c in command.call_args_list]
            self.assertIn(['sysctl','-w','net.ipv4.ip_forward=0'],commands)
            adds=[c for c in commands if c[0]=='iptables' and '-I' in c]
            deletes=[c for c in commands if c[0]=='iptables' and '-D' in c]
            self.assertEqual(len(adds),3)
            self.assertEqual(sorted([['-D' if v=='-I' else v for v in c] for c in adds]),sorted(deletes))
            self.assertFalse((self.state/'phone-uplink.json').exists())

    def test_shared_managed_ap_limit_does_not_mean_concurrency(self):
        from platform_debian import supports_sta_ap
        text = ('valid interface combinations:\n\t\t * #{ managed, AP } <= 1, #{ P2P-client, P2P-GO } <= 1,\n'
                '\t\t   total <= 3, #channels <= 2\n')
        self.assertEqual(supports_sta_ap(text), 0)

    def test_nat_follows_ethernet_or_wifi_routes_and_excludes_client_ap(self):
        p = DebianPlatform('wlan-test', self.state)
        rules = p.phone_nat_rules(ipaddress.ip_network('192.0.2.0/24'))
        for table, rule in rules:
            self.assertNotIn('wlan-test', rule)
            self.assertIn('192.0.2.0/24', rule)
        self.assertIn(['!', '-o', 'ap0'], [rules[0][1][i:i+3] for i in range(len(rules[0][1])-2)])
        self.assertIn(['!', '-i', 'ap0'], [rules[2][1][i:i+3] for i in range(len(rules[2][1])-2)])

    def test_n_only_config_keeps_both_bands(self):
        p=DebianPlatform('wlan-test',self.state)
        with patch('platform_debian.wifi_driver',return_value='brcmfmac'):
            for channel, band in [(149,'a'),(6,'g')]:
                p.configure_hotspot(channel,20,'Box','synthetic-box'); p.n_only_ap()
                conf=(self.state/'hostapd.conf').read_text()
                self.assertIn('hw_mode='+band,conf); self.assertIn('ieee80211n=1',conf)
                self.assertNotIn('ieee80211ac',conf)

class UsbShareUplinkTests(unittest.TestCase):
    """车机上网 "USB 共享网络" (2026-10-31): the cable first, the saved phone hotspot as the fallback."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state/'uplink-hotspot.json').write_text('{"ssid":"Phone","passphrase":"synthetic-pass"}')
        self.p = Mock()
        self.p.concurrent_ap.return_value = 1
        self.p.phone_hotspot_frequency.return_value = None
        self.p.join_wifi.return_value = True
        self.p.local_addresses.return_value = []
        self.p.frequency.return_value = 5745
        self.p.uplink_gateway.return_value = '192.0.2.1'
        self.p.add_phone_ap.return_value = True
        self.p.start_ap.return_value = True
        self.p.connected.return_value = True
        self.p.ap_ready.return_value = True
        self.p.ap_channel_problem.return_value = None
        self.p.interface, self.p.ap_interface = 'wlan0', 'wlan0'
        self.p.run = self.state
        self.p.phone_resolv = self.state / 'dns/phone-resolv.conf'
        self.p.default_route_devices.return_value = []
        self.p.wired_carrier.return_value = False
        self.now = 0
        self.receiver = Mock()

    def uplink(self, share):
        self.status = {}
        return PhoneUplink(self.p, self.state, ipaddress.ip_interface('100.96.0.1/24'),
                           {'ssid':'Box','passphrase':'synthetic-box'}, 149, 40, Mock(), self.receiver,
                           self.status, say=lambda _:None, clock=lambda:self.now, share=share)

    def test_share_makes_the_box_the_car_router(self):
        u = self.uplink(share=True)
        self.assertTrue(u.start())
        self.assertTrue(self.p.start_dhcp.call_args.kwargs['router'],
                        'the car needs the box as its default gateway, Tesla mode or not')

    def test_plain_uplink_keeps_the_old_no_router_policy(self):
        u = self.uplink(share=False)
        self.assertTrue(u.start())
        self.assertFalse(self.p.start_dhcp.call_args.kwargs['router'])

    def test_the_cable_puts_the_ap_back_on_its_own_channel(self):
        # The AP followed the phone hotspot onto 2.4 GHz; on the cable nothing needs that channel and
        # 2.4 GHz makes CarPlay stutter, so it goes back to the default (5 GHz) one (2026-10-31).
        u = self.uplink(share=True)
        self.p.phone_hotspot_frequency.return_value = 2437
        self.assertTrue(u.start())
        self.assertEqual(u.channel, 149, 'no scan at startup in share mode')
        self.now = u.next_try
        u.tick()                                   # grace over, no cable: join the 2.4 GHz hotspot
        self.assertEqual(u.channel, 6)
        self.p.phone_link.return_value = {'connected': True, 'frequency': 2437}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        u.tick()
        self.assertEqual(u.phase, 'online')
        self.p.default_route_devices.return_value = ['usb0']
        u.tick()
        self.assertEqual(u.phase, 'wired')
        self.assertEqual(u.channel, 149, 'back on the default channel')

    def test_share_mode_keeps_the_cable_instead_of_chasing_the_hotspot(self):
        # The cable's default route is enough while sharing: no DNS probe, so the controller cannot decide
        # "no cable" on a DNS hiccup and leave for the hotspot (dp-0017, 2026-10-31).
        u = self.uplink(share=True)
        self.p.phone_hotspot_frequency.return_value = 2437
        self.assertTrue(u.start())
        self.assertEqual(u.channel, 149)
        self.p.default_route_devices.return_value = ['usb0']
        self.now = u.next_try
        u.tick()                                   # grace over with the cable present: never joins
        self.assertEqual(u.phase, 'wired')
        self.assertEqual(u.channel, 149)
        self.p.start_phone_station.assert_not_called()
        self.p.stop_child.assert_not_called()

    def test_the_cable_moves_a_two_ghz_ap_back_even_with_no_station(self):
        # Plugging Ethernet while the hotspot had already gone: no station branch runs, yet the AP must
        # still leave 2.4 GHz (dp-0017, 2026-10-31).
        u = self.uplink(share=True)
        self.p.phone_hotspot_frequency.return_value = 2437
        self.assertTrue(u.start())
        self.now = u.next_try
        u.tick()                                  # joins the hotspot, AP moves to 6
        self.assertEqual(u.channel, 6)
        u.disconnect_station()                    # the hotspot went away before the cable arrived
        u.tick()
        self.p.default_route_devices.return_value = ['eth0']
        self.now = u.next_try
        u.tick()
        self.assertEqual(u.phase, 'wired')
        self.assertEqual(u.channel, 149)

    def test_an_ap_already_on_five_ghz_is_left_alone(self):
        # Owner's rule (2026-10-31): only an AP that followed the hotspot onto 2.4 GHz moves back; one that
        # is already on 5 GHz must not be restarted for nothing.
        u = self.uplink(share=True)
        self.p.phone_hotspot_frequency.return_value = 5745
        self.assertTrue(u.start())
        self.now = u.next_try
        u.tick()                                  # joins on 5 GHz; the AP stays on 149
        self.assertEqual(u.channel, 149)
        self.p.phone_link.return_value = {'connected': True, 'frequency': 5745}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        u.tick()
        self.p.stop_child.reset_mock()
        self.p.default_route_devices.return_value = ['usb0']
        u.tick()
        self.assertEqual(u.phase, 'wired')
        self.assertEqual(u.channel, 149)
        self.p.stop_child.assert_not_called()

    def test_the_cable_coming_back_leaves_the_phone_hotspot(self):
        u = self.uplink(share=True)
        self.p.phone_hotspot_frequency.return_value = 5745
        self.assertTrue(u.start())
        self.now = u.next_try                    # share mode does no scan at startup
        u.tick()                                 # joins the hotspot
        self.p.phone_link.return_value = {'connected': True, 'frequency': 5745}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        u.tick()
        self.assertEqual(u.phase, 'online')
        self.p.default_route_devices.return_value = ['usb0']
        u.tick()
        self.assertEqual(u.phase, 'wired')
        self.p.stop_phone_station.assert_called_once()
        self.p.stop_child.assert_not_called()   # the AP, dnsmasq and the receiver stay up

    def test_without_share_the_hotspot_stays_when_a_cable_appears(self):
        u = self.uplink(share=False)
        self.p.phone_hotspot_frequency.return_value = 5745
        self.assertTrue(u.start())
        self.p.phone_link.return_value = {'connected': True, 'frequency': 5745}
        self.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        u.tick()
        self.p.default_route_devices.return_value = ['usb0']
        u.tick()
        self.assertEqual(u.phase, 'online')
        self.p.stop_phone_station.assert_not_called()


class FastSearchTests(unittest.TestCase):
    """2026-10-30 request: the hotspot is usually switched on after the box booted; look every 10 s, remember its
    channel, and never wait minutes."""
    setUp = PhoneUplinkTests.setUp
    boot = PhoneUplinkTests.boot
    connected = PhoneUplinkTests.connected

    def attempts(self, until):
        """Times at which the controller scanned, stepping the clock one second at a time."""
        seen = []
        while self.now < until:
            self.now += 1
            before = self.p.phone_hotspot_frequency.call_count
            self.u.tick()
            if self.p.phone_hotspot_frequency.call_count > before:
                seen.append(self.now)
        return seen

    def test_hotspot_switched_on_late_is_found_within_ten_seconds(self):
        self.boot(None)
        seen = self.attempts(300)
        self.assertEqual(seen[0], 10)
        self.assertTrue(all(b - a == 10 for a, b in zip(seen, seen[1:])), seen)
        self.p.phone_hotspot_frequency.return_value = 5745
        self.now = 45
        self.u.next_try = min(self.u.next_try, 50)
        self.attempts(51)
        self.assertEqual(self.u.phase, 'connecting')

    def test_after_the_fast_window_waits_grow_but_never_beyond_a_minute(self):
        self.boot(None)
        seen = self.attempts(1200)
        gaps = [b - a for a, b in zip(seen, seen[1:]) if a >= 300]
        self.assertTrue(gaps and max(gaps) == phone_uplink.RETRY_MAX == 60, gaps)

    def test_long_loss_starts_a_new_fast_window(self):
        self.boot()
        self.connected()
        self.now = 2000
        self.p.phone_link.return_value = {'connected': False, 'frequency': None}
        self.u.tick()
        self.now += phone_uplink.LINK_LOSS_GRACE
        self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.assertEqual(self.u.next_try - self.now, 10)

    def test_channel_is_remembered_without_the_password(self):
        self.boot(5180)
        self.connected(5180)
        cache = self.state / phone_uplink.LAST_HOTSPOT
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        text = cache.read_text()
        self.assertNotIn('synthetic-pass', text)
        self.assertNotIn('Phone', text)
        self.assertEqual(json.loads(text)['frequency'], 5180)

    def fresh(self):
        return PhoneUplink(self.p, self.state, self.u.address, self.u.box, 149, 40, Mock(), self.receiver,
                           self.status, say=lambda _: None, clock=lambda: self.now)

    def test_boot_opens_the_box_hotspot_on_the_remembered_5ghz_channel(self):
        phone_uplink.HotspotSearch(self.state, {'ssid': 'Phone'}, lambda: 0).remember(5180)
        self.u = self.fresh()
        self.boot(None)
        self.receiver.assert_called_once_with(36)
        # The phone comes up on that channel later: joined without rebuilding anything.
        self.p.phone_hotspot_frequency.return_value = 5180
        self.p.stop_child.reset_mock()
        self.attempts(11)
        self.p.start_phone_station.assert_called_once_with(self.u.saved, 5180)
        self.p.stop_child.assert_not_called()

    def test_a_remembered_24ghz_channel_or_another_phone_does_not_move_the_box(self):
        for ssid, frequency in (('Phone', 2437), ('Other', 5180)):
            with self.subTest(ssid=ssid):
                phone_uplink.HotspotSearch(self.state, {'ssid': ssid}, lambda: 0).remember(frequency)
                self.receiver.reset_mock()
                self.u = self.fresh()
                self.boot(None)
                self.receiver.assert_called_once_with(149)

    def test_retries_scan_the_remembered_channel_with_a_full_scan_every_third_try(self):
        phone_uplink.HotspotSearch(self.state, {'ssid': 'Phone'}, lambda: 0).remember(5745)
        self.u = self.fresh()
        self.boot(None)
        self.attempts(60)
        kinds = [c.kwargs.get('frequency') for c in self.p.phone_hotspot_frequency.call_args_list]
        self.assertEqual(kinds, [None, 5745, 5745, None, 5745, 5745, None])

    def test_platform_scans_only_the_given_frequency(self):
        p = DebianPlatform('wlan-test', self.state)
        with patch('platform_debian.subprocess.run'), patch.object(p, 'scan') as query:
            query.return_value = Mock(returncode=0, stdout='')
            self.assertIsNone(p.phone_hotspot_frequency('Phone', frequency=5180))
            query.assert_called_with(frequency=5180, timeout=5)

    def test_cached_scan_results_do_not_count(self):
        # Real iw output on dp-0017: the scan of 5180 also listed the hotspot from an earlier scan on 5745.
        cached = ('BSS 00:00:5e:00:53:01(on wlan0)\n\tlast seen: 3878.569s [boottime]\n\tfreq: 5745.0\n'
                  '\tlast seen: 9147 ms ago\n\tsignal: -40.00 dBm\n\tSSID: Phone\n')
        current = cached.replace('9147 ms', '0 ms')
        p = DebianPlatform('wlan-test', self.state)
        with patch('platform_debian.subprocess.run'), patch.object(p, 'scan') as query:
            query.return_value = Mock(returncode=0, stdout=cached)
            self.assertIsNone(p.phone_hotspot_frequency('Phone'), 'seen 9 s ago: maybe switched off since')
            query.return_value = Mock(returncode=0, stdout=current)
            self.assertIsNone(p.phone_hotspot_frequency('Phone', frequency=5180), 'not on the scanned channel')
            self.assertEqual(p.phone_hotspot_frequency('Phone', frequency=5745), 5745)
            self.assertEqual(p.phone_hotspot_frequency('Phone'), 5745)

    def test_legacy_cars_also_retry_every_ten_seconds_and_remember_the_channel(self):
        self.status['tesla'] = False
        self.p.find_hotspot.return_value = (True, False, '')
        u = LegacyPhoneUplink(self.p, self.state, self.u.address, self.u.box, 149, 40,
                              Mock(), self.receiver, self.status, say=lambda _: None, clock=lambda: self.now)
        seen = []
        while self.now < 120:
            self.now += 1
            before = self.p.find_hotspot.call_count
            u.tick()
            if self.p.find_hotspot.call_count > before:
                seen.append(self.now)
        self.assertEqual(seen[:3], [5, 15, 25])
        self.assertTrue(all(b - a == 10 for a, b in zip(seen, seen[1:])), seen)
        self.p.find_hotspot.return_value = (True, True, '')
        self.now = u.next_try
        u.tick()
        self.assertEqual(u.phase, 'online')
        self.assertEqual(json.loads((self.state / phone_uplink.LAST_HOTSPOT).read_text())['frequency'], 5745)
        self.p.connected.return_value = False
        u.tick()
        self.assertEqual(u.phase, 'offline')
        self.assertEqual(u.next_try - self.now, 10)
        self.p.connected.return_value = True
        self.now = u.next_try
        u.tick()
        self.assertEqual(self.p.find_hotspot.call_args.kwargs.get('frequency'), 5745)


class UplinkAuditTests(unittest.TestCase):
    setUp = PhoneUplinkTests.setUp
    boot = PhoneUplinkTests.boot

    def test_each_partial_start_failure_rolls_back_before_normal_ap(self):
        for name in ('set_ap_address', 'configure_hotspot', 'start_dhcp',
                     'enable_phone_nat', 'check_lan_children'):
            with self.subTest(name=name):
                self.setUp()
                getattr(self.p, name).side_effect = OSError('synthetic failure')
                self.p.phone_hotspot_frequency.return_value = 5745
                self.assertFalse(self.u.start())
                self.assertFalse(self.u.started)
                self.assertFalse(self.status.get('tesla_lan_stable', False))
                self.p.cleanup_phone_uplink.assert_called_once()
                for child in ('receiver', 'dnsmasq', 'hostapd'):
                    self.p.stop_child.assert_any_call(child)

    def test_receiver_start_failure_does_not_advertise_stable_lan(self):
        self.receiver.side_effect = OSError('synthetic failure')
        self.p.phone_hotspot_frequency.return_value = 5745
        self.assertFalse(self.u.start())
        self.assertFalse(self.status.get('tesla_lan_stable', False))
        self.p.cleanup_phone_uplink.assert_called_once()

    def test_unsupported_concurrency_never_sets_stable_marker(self):
        self.p.concurrent_ap.return_value = 0
        self.assertFalse(self.u.start())
        self.assertFalse(self.status.get('tesla_lan_stable', False))

    def test_both_channels_fail_then_plain_ap_recovers(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5180
        self.p.start_ap.side_effect = [False, False, False, False, True]
        self.now = self.u.next_try
        self.u.tick()
        self.assertFalse(self.u.started)
        self.assertFalse(self.status.get('tesla_lan_stable', False))
        self.p.cleanup_phone_uplink.assert_called_once()
        self.p.start_phone_station.assert_not_called()
        self.assertEqual(self.p.start_dhcp.call_count, 2)
        self.assertIn('普通', self.status['uplink']['reason'])

    def test_health_recovery_failure_also_tries_plain_ap(self):
        self.boot(None)
        self.p.ap_ready.return_value = False
        self.p.start_ap.side_effect = [False, False, False, False, True]
        self.u.tick(); self.u.tick(); self.u.tick()
        self.assertFalse(self.u.started)
        self.assertFalse(self.status.get('tesla_lan_stable', False))
        self.p.cleanup_phone_uplink.assert_called_once()

    def test_total_ap_failure_is_not_hidden_as_working_lan(self):
        self.boot(None)
        self.p.phone_hotspot_frequency.return_value = 5180
        self.p.start_ap.return_value = False
        self.now = self.u.next_try
        with self.assertRaises(RuntimeError):
            self.u.tick()
        self.assertFalse(self.status.get('tesla_lan_stable', False))

    def test_dynamic_resolver_respects_router_false(self):
        p = DebianPlatform('wlan-test', self.state)
        with patch.object(p, 'start') as launch:
            p.start_dhcp(self.u.address, False, upstream_file=self.state/'resolver', router=False)
        argv = launch.call_args.args[1]
        self.assertIn('--dhcp-option=3', argv)
        self.assertNotIn('--dhcp-option=3,100.96.0.1', argv)


class LegacyUplinkPolicyTests(unittest.TestCase):
    setUp = PhoneUplinkTests.setUp

    def test_non_tesla_online_router_offline_no_router_keeps_desec_name(self):
        self.status.update(tesla=False, own_name='box.car.example.com')
        (self.state/'ddns.json').write_text(json.dumps({'provider':'desec',
            'domain':'box.car.example.com', 'token':'synthetic-test-token'}))
        u = LegacyPhoneUplink(self.p, self.state, self.u.address, self.u.box, 149, 40,
                              Mock(), self.receiver, self.status, say=lambda _:None, clock=lambda:self.now)
        self.assertFalse(self.status.get('tesla_lan_stable', False))
        self.now = u.next_try
        u.tick()
        self.assertEqual(u.phase, 'online')
        online = self.p.start_dhcp.call_args
        self.p.connected.return_value = False
        u.tick()
        self.assertEqual(u.phase, 'offline')
        offline = self.p.start_dhcp.call_args
        p = DebianPlatform('wlan-test', self.state)
        for call, router in ((online, True), (offline, False)):
            with patch.object(p, 'start') as launch:
                p.start_dhcp(*call.args, **call.kwargs)
            argv = launch.call_args.args[1]
            self.assertIn('--dhcp-option=3,100.96.0.1' if router else '--dhcp-option=3', argv)
            self.assertIn('--address=/box.car.example.com/100.96.0.1', argv)
            self.assertIn('--local=/box.car.example.com/', argv)
        self.assertFalse(self.status.get('tesla_lan_stable', False))


class PersistentStationAdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        import shutil
        self.addCleanup(shutil.rmtree, self.root.with_name(self.root.name + '-dns'), True)   # platform.phone_resolv
        self.p = DebianPlatform('wlan-test', self.root, self.root / 'sta.network')
        self.p.ap_interface = 'ap0'

    def test_lan_check_detects_immediate_exit_and_allows_no_identity(self):
        self.p.children = [('hostapd', Mock(poll=Mock(return_value=None))),
                           ('dnsmasq', Mock(poll=Mock(return_value=1)))]
        with patch('platform_debian.time.sleep'):
            with self.assertRaises(RuntimeError):
                self.p.check_lan_children()
            self.p.children[1][1].poll.return_value = None
            self.p.check_lan_children()  # no receiver when MFi identity is absent
            self.p.children.append(('receiver', Mock(poll=Mock(return_value=1))))
            with self.assertRaises(RuntimeError):
                self.p.check_lan_children()

    def test_scan_keeps_ssid_and_frequency_together(self):
        scan = ('BSS 00:00:5e:00:53:01(on wlan-test)\n freq: 5745\n signal: -40.0 dBm\n SSID: Other\n'
                'BSS 00:00:5e:00:53:02(on wlan-test)\n freq: 5180\n signal: -60.0 dBm\n SSID: Phone\n'
                'BSS 00:00:5e:00:53:03(on wlan-test)\n freq: 5220\n signal: -70.0 dBm\n SSID: Phone\n')
        with patch('platform_debian.subprocess.run') as run, patch.object(self.p, 'scan') as query:
            query.return_value = Mock(returncode=0, stdout=scan)
            self.assertEqual(self.p.phone_hotspot_frequency('Phone'), 5180)
            query.assert_called_with(timeout=5)
            query.return_value = Mock(returncode=1, stdout='')
            with self.assertRaises(OSError):
                self.p.phone_hotspot_frequency('Phone')

    def test_station_start_is_frequency_locked_and_does_not_wait_for_dhcp(self):
        with patch('platform_debian.subprocess.run') as run, patch.object(self.p, 'start') as start:
            self.p.start_phone_station({'ssid': 'Phone', 'passphrase': 'synthetic-test'}, 5180)
            commands = [c.args[0] for c in run.call_args_list]
        conf = (self.root / 'wpa.conf').read_text()
        self.assertIn('scan_freq=5180', conf)
        self.assertIn('freq_list=5180', conf)
        self.assertEqual((self.root / 'wpa.conf').stat().st_mode & 0o777, 0o600)
        self.assertIn('DHCP=ipv4', (self.root / 'sta.network').read_text())
        self.assertEqual(start.call_args.args[0], 'wpa_supplicant')
        self.assertFalse(any('ap0' in cmd or 'down' in cmd or 'del' in cmd for cmd in commands))
        self.assertFalse(any(cmd[:2] == ['iw', 'dev'] and cmd[-1] == 'link' for cmd in commands))
        self.assertIn(['iw', 'dev', 'wlan-test', 'set', 'power_save', 'off'], commands)

    def test_stop_disables_dhcp_and_only_flushes_station(self):
        with patch('platform_debian.subprocess.run') as run, patch.object(self.p, 'stop_child') as stop:
            self.p.stop_phone_station()
        stop.assert_called_once_with('wpa_supplicant')
        self.assertIn('DHCP=no', (self.root / 'sta.network').read_text())
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(['ip', '-4', 'route', 'flush', 'dev', 'wlan-test'], commands)
        self.assertFalse(any('ap0' in cmd or 'down' in cmd or 'del' in cmd for cmd in commands))

    def test_dns_changes_only_resolver_file(self):
        with patch.object(self.p, 'stop_child') as stop, patch.object(self.p, 'start') as start:
            self.p.set_phone_dns(None)
            path = self.p.phone_resolv
            self.assertFalse(path.is_relative_to(self.root), 'not inside the 0700 run directory')
            before = path.read_text()
            self.p.set_phone_dns('192.0.2.1')
            self.assertIn('nameserver 192.0.2.1', path.read_text())
            self.p.set_phone_dns(None)
            self.assertEqual(before, path.read_text())
            # dnsmasq reads it as "nobody" although the run directory is 0700 (dp-0017, 2026-10-30).
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o755)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            stop.assert_not_called(); start.assert_not_called()

    def test_failed_iw_is_not_reported_as_disconnected(self):
        with patch('platform_debian.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'iw')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.p.phone_link()
        with patch('platform_debian.subprocess.run', return_value=Mock(stdout='Connected to synthetic\n freq: 5745\n signal: -57 dBm')):
            self.assertEqual(self.p.phone_link(), {'connected': True, 'frequency': 5745, 'signal_dbm': -57})

    def test_old_ap_enabled_log_does_not_hide_dead_process(self):
        (self.root / 'hostapd.log').write_text('AP-ENABLED')
        self.p.children = [('hostapd', Mock(poll=Mock(return_value=1)))]
        self.assertFalse(self.p.ap_ready())

    def test_disabled_ap_log_requires_a_new_enabled_event(self):
        log = self.root / 'hostapd.log'
        log.write_text('AP-ENABLED\nAP-DISABLED\n')
        self.assertFalse(self.p.ap_ready())
        log.write_text('AP-ENABLED\nAP-DISABLED\nAP-ENABLED\n')
        self.assertTrue(self.p.ap_ready())


class UsbTetherPlatformTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        import shutil
        self.addCleanup(shutil.rmtree, self.root.with_name(self.root.name + '-dns'), True)   # platform.phone_resolv
        self.p = DebianPlatform('wlan-test', self.root, self.root / 'sta.network', self.root / 'net/05-tether.network',
                                self.root / 'nm/tether.nmconnection')

    nm_running = False

    def run_fake(self, *args, **kwargs):
        if args[0][:2] == ['systemctl', 'is-active']:
            return subprocess.CompletedProcess(args[0], 0 if self.nm_running else 3, '', '')
        if args[0][:2] == ['sysctl', '-n']:
            return subprocess.CompletedProcess(args[0], 0, '0\n', '')
        return subprocess.CompletedProcess(args[0], 0, '', '')

    def test_enable_shares_the_route_and_disable_undoes_everything(self):
        with patch('platform_debian.subprocess.run', side_effect=self.run_fake) as run:
            self.p.enable_usb_tether(ipaddress.ip_network('100.96.0.0/24'))
        network = (self.root / 'net/05-tether.network').read_text()
        # Any phone or MiFi sharing its internet over USB, not only the iPhone (2026-10-31).
        self.assertIn('Driver=ipheth rndis_host cdc_ether cdc_ncm\n', network)
        self.assertIn('RouteMetric=1536', network, 'the Ethernet cable at home keeps priority')
        self.assertIn('DNS=223.5.5.5 1.12.12.12', network, "the box's own resolver needs an upstream too")
        profile = self.root / 'nm/tether.nmconnection'
        # NetworkManager's keyfile list syntax, as nmcli writes it (NM 1.52 on dp-0017).
        self.assertIn('driver=ipheth;rndis_host;cdc_ether;cdc_ncm;\n', profile.read_text())
        self.assertNotIn('r8152', profile.read_text(), "the box's own Ethernet adapter is not a phone")
        self.assertIn('multi-connect=3\n', profile.read_text(), 'iPhone and Android plugged in at once both get a lease')
        self.assertIn('route-metric=1536', profile.read_text())
        self.assertIn('dns=223.5.5.5;1.12.12.12;\n', profile.read_text())
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.root / 'usb-tether').exists(), 'the USB bridge keeps ipheth bound')
        self.assertIn('nameserver 223.5.5.5', self.p.phone_resolv.read_text())
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(['sysctl', '-w', 'net.ipv4.ip_forward=1'], commands)
        inserted = [c for c in commands if c[0] == 'iptables' and '-I' in c]
        self.assertEqual(len(inserted), 3)
        self.assertTrue(any('MASQUERADE' in c and '!' in c and 'wlan-test' in c for c in inserted))
        with patch('platform_debian.subprocess.run', side_effect=self.run_fake) as run:
            self.p.disable_usb_tether(self.root / 'no-ipheth')
        commands = [c.args[0] for c in run.call_args_list]
        self.assertEqual(len([c for c in commands if c[0] == 'iptables' and '-D' in c]), 3)
        self.assertIn(['sysctl', '-w', 'net.ipv4.ip_forward=0'], commands)
        self.assertFalse((self.root / 'usb-tether').exists())
        self.assertFalse((self.root / 'net/05-tether.network').exists())
        self.assertFalse((self.root / 'nm/tether.nmconnection').exists())
        self.assertFalse((self.root / 'usb-tether.json').exists())

    def test_with_network_manager_only_its_profile_takes_the_iphone(self):
        # Both managers took a lease on dp-0017: two addresses on the iPhone's interface.
        self.nm_running = True
        with patch('platform_debian.subprocess.run', side_effect=self.run_fake):
            self.p.enable_usb_tether(ipaddress.ip_network('100.96.0.0/24'))
        self.assertFalse((self.root / 'net/05-tether.network').exists())
        self.assertTrue((self.root / 'nm/tether.nmconnection').exists())

    def test_a_service_stop_keeps_the_profile_and_turning_off_releases_the_iphone_first(self):
        # Without our profile NetworkManager made "Wired connection 2" with the cable's priority (dp-0017).
        self.nm_running = True
        with patch('platform_debian.subprocess.run', side_effect=self.run_fake):
            self.p.enable_usb_tether(ipaddress.ip_network('100.96.0.0/24'))
            self.p.stop_usb_sharing()
        profile = self.root / 'nm/tether.nmconnection'
        self.assertTrue(profile.exists())
        self.assertFalse((self.root / 'usb-tether').exists())
        self.assertFalse((self.root / 'usb-tether.json').exists())
        ipheth = self.root / 'ipheth'
        (ipheth / '1-2:6.2').mkdir(parents=True)
        (ipheth / 'unbind').write_text('')
        with patch('platform_debian.subprocess.run', side_effect=self.run_fake):
            self.p.disable_usb_tether(ipheth)
        self.assertEqual((ipheth / 'unbind').read_text(), '1-2:6.2')
        self.assertFalse(profile.exists())

    def test_disable_without_a_previous_run_does_nothing(self):
        with patch('platform_debian.subprocess.run') as run:
            self.p.disable_usb_tether(self.root / 'no-ipheth')
        run.assert_not_called()


class JoinedPhoneAPTests(unittest.TestCase):
    setUp = PhoneUplinkTests.setUp
    def test_direct_car_or_mifi_preserves_viewer_side_and_hands_box_credentials(self):
        from supervisor import handed_hotspot, joined_receiver_network
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock())
        self.assertTrue(direct.prepare())
        self.assertEqual(direct.start(), 149)
        self.assertTrue(direct.active)
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ())
        # Off by default: no router, no DNS, no NAT for the iPhone, even with a gateway upstream (its cellular data)
        self.assertIsNone(self.p.start_dhcp.call_args.kwargs['upstream'])
        self.p.enable_phone_nat.assert_not_called()
        self.assertFalse(direct.sharing)
        self.assertEqual(handed_hotspot(self.state, False, direct=True), self.state/'hotspot.json')
        args = joined_receiver_network(self.state, ('192.0.2.8',149,'wpa3'), 'wlan-test', direct_address=self.u.address.ip)
        network = dict(zip(args[::2], args[1::2]))
        self.assertEqual(network['--bind'], '100.96.0.1')
        self.assertEqual(network['--interface'], 'ap0')
        self.assertEqual(network['--web-host'], '0.0.0.0')
        self.assertEqual(network['--security'], 'wpa2')
        normal = joined_receiver_network(self.state, ('192.0.2.8',149,'wpa3'), 'wlan-test')
        normal = dict(zip(normal[::2], normal[1::2]))
        self.assertEqual(normal['--bind'], '192.0.2.8')
        self.assertEqual(normal['--interface'], 'wlan-test')
        self.assertEqual(normal['--security'], 'wpa3')
        self.p.frequency.return_value = 2412
        self.assertTrue(direct.changed())
        direct.close()
        self.p.cleanup_phone_uplink.assert_called_once()
        self.assertFalse(direct.active)

    def test_sharing_the_cars_internet_routes_the_iphone_out_through_the_car_hotspot(self):
        # 2026-10-17, the user: some cars have their own data plan; the iPhone's internet check must pass for real.
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock(), share_internet=True)
        direct.prepare(); direct.start()
        self.p.enable_phone_nat.assert_called_once_with(self.u.address.network)
        self.assertEqual(self.p.start_dhcp.call_args.kwargs['upstream'], '192.0.2.1', 'DNS forwarded upstream, the box as router')
        self.assertTrue(direct.sharing)
        direct.close()
        self.p.cleanup_phone_uplink.assert_called_once()   # which takes the NAT rules and ip_forward back
        self.assertFalse(direct.sharing)

    def test_sharing_waits_for_the_default_route_and_without_one_the_iphone_keeps_its_own_data(self):
        waits = []
        def wait_for(predicate, seconds, interval=0.2):   # tries a few times, like the supervisor's within its seconds
            waits.append(seconds)
            if not any(predicate() for _ in range(3)): raise TimeoutError
        gateways = iter([None, None, '192.0.2.1'])
        self.p.uplink_gateway.side_effect = lambda: next(gateways)
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, wait_for, share_internet=True)
        direct.prepare(); direct.start()
        self.assertIn(10, waits)
        self.assertTrue(direct.sharing, 'the route came within the wait')
        self.p.uplink_gateway.side_effect = None; self.p.uplink_gateway.return_value = None
        self.p.enable_phone_nat.reset_mock()
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, wait_for, share_internet=True)
        direct.prepare(); direct.start()
        self.p.enable_phone_nat.assert_not_called()
        self.assertIsNone(self.p.start_dhcp.call_args.kwargs['upstream'])
        self.assertFalse(direct.sharing)
        self.assertTrue(direct.active, 'CarPlay goes on without it')

    def test_a_channel_the_direct_hotspot_cannot_use_says_which_and_never_opens_it(self):
        # 2026-10-17: on 2.4 GHz the N1's direct picture came 9-12 s late; 52-64 and 100-144 are disabled for its AP.
        from phone_uplink import UnsuitableChannel
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock())
        direct.prepare()
        self.p.frequency.return_value = 2437
        with self.assertRaisesRegex(UnsuitableChannel, r'车机热点在 2.4 GHz（信道 6），直连会很卡.*36–48 或 149–165'):
            direct.start()
        self.p.frequency.return_value = 5500; self.p.ap_channel_problem.return_value = 'disabled'
        mifi = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock(), label='随身 WiFi')
        mifi.prepare()
        with self.assertRaisesRegex(UnsuitableChannel, r'随身 WiFi在 5 GHz 信道 100，盒子不能在这个信道开热点（disabled）'):
            mifi.start()
        self.p.add_phone_ap.assert_not_called()
        self.p.frequency.return_value = 5240; self.p.ap_channel_problem.return_value = None
        self.assertEqual(direct.start(), 48)

    def test_unsupported_adapter_and_collision_cannot_start_a_second_ap(self):
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock())
        self.p.concurrent_ap.return_value = 0
        self.assertFalse(direct.prepare())
        self.p.prepare_phone_uplink.assert_not_called()
        self.p.concurrent_ap.return_value = 1
        direct.prepare()
        self.p.local_addresses.return_value = [ipaddress.ip_interface('100.96.0.8/24')]
        with self.assertRaises(ValueError): direct.start()
        self.p.add_phone_ap.assert_not_called()
        direct.close()

    def test_no_gateway_still_gives_direct_phone_link_without_fake_dns_or_nat(self):
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock())
        direct.prepare()
        self.p.uplink_gateway.return_value = None
        direct.start()
        self.p.enable_phone_nat.assert_not_called()
        self.p.n_only_ap.assert_called_once()
        self.assertIsNone(self.p.start_dhcp.call_args.kwargs['upstream'])
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ())
        self.p.connected.return_value = False
        self.assertTrue(direct.changed())

    def test_ap_failure_stays_on_the_same_channel_then_cleanup_restores_station(self):
        direct = JoinedPhoneAP(self.p, self.u.address, self.u.box, Mock())
        direct.prepare()
        self.p.start_ap.return_value = False
        with self.assertRaises(RuntimeError): direct.start()
        self.assertEqual(self.p.start_ap.call_count, 2)
        self.assertEqual(self.p.configure_hotspot.call_count, 1)
        direct.close()
        self.p.cleanup_phone_uplink.assert_called_once()


class IntegratedLegacyDnsTests(unittest.TestCase):
    def setUp(self):
        PhoneUplinkTests.setUp(self)
        self.u = LegacyPhoneUplink(self.p, self.state, self.u.address, self.u.box, 149, 40,
            Mock(), self.receiver, self.status, say=lambda _:None, clock=lambda:self.now)

    def online(self):
        self.now = self.u.next_try
        self.u.tick()
        self.assertEqual(self.u.phase, "online")

    def test_duckdns_name_is_answered_through_the_uplink_unless_turned_off(self):
        # Any car (not only a Tesla) on the box hotspot with the iPhone uplink: the box answers its own name, as
        # without the uplink, so a stale or missing public record cannot break it (2026-10-29).
        (self.state / 'ddns.json').write_text(json.dumps({'domain': 'example', 'token': '0a1b2c3d-1111-2222-3333-444455556666'}))
        (self.state / 'options.json').write_text('{"local_dns":false}')   # written by older pages, not a choice
        self.status['tesla'] = False
        self.online()
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ['example.duckdns.org'])
        (self.state / 'options.json').write_text('{"own_name_on_hotspot":false}')
        self.u.fallback('test')
        self.assertEqual(self.p.start_dhcp.call_args.args[2], [])


    def test_desec_name_survives_uplink_and_fallback_without_fake_online_checks(self):
        name = 'box01.car.example.com'
        (self.state / 'ddns.json').write_text(json.dumps({
            'provider': 'desec', 'domain': name, 'token': 'synthetic-test-token'}))
        (self.state / 'options.json').write_text('{"local_dns":false}')
        self.status['own_name'] = name
        self.u.address = ipaddress.ip_interface('100.97.0.1/24')
        for tesla in (True, False):
            with self.subTest(tesla=tesla):
                self.status['tesla'] = tesla
                self.u.phase = 'offline'
                self.p.connected.return_value = True
                self.now = self.u.next_try
                self.u.tick()
                self.assertEqual(self.u.phase, 'online')
                call = self.p.start_dhcp.call_args
                self.assertEqual(call.args[2], [name])
                self.assertFalse(self.status['tesla_offline'])
                platform = DebianPlatform('wlan-test', self.state)
                with patch.object(platform, 'start') as start:
                    platform.start_dhcp(*call.args, **call.kwargs)
                    argv = start.call_args.args[1]
                self.assertIn(f'--address=/{name}/100.97.0.1', argv)
                self.assertIn(f'--local=/{name}/', argv, 'AAAA must not leak to public DNS either')
                self.assertIn('--server=192.0.2.1', argv)
                self.assertIn('--server=223.5.5.5', argv)
                self.assertFalse(any(check in arg for check in setup_wizard.TESLA_CHECK_NAMES for arg in argv))
                self.p.connected.return_value = False
                self.u.tick()
                expected = list(setup_wizard.TESLA_CHECK_NAMES) if tesla else []
                self.assertEqual(self.p.start_dhcp.call_args.args[2], expected + [name])
                self.p.connected.return_value = True
                self.now = self.u.next_try
                self.u.tick()
                self.assertEqual(self.p.start_dhcp.call_args.args[2], [name])


    def test_uplink_without_own_domain_only_forwards_dns(self):
        self.status['own_name'] = None
        self.online()
        self.assertEqual(self.p.start_dhcp.call_args.args[2], [])
        self.assertFalse(self.status['tesla_offline'])


    def test_overlapping_phone_network_falls_back_without_nat(self):
        self.p.local_addresses.return_value = [ipaddress.ip_interface('100.96.0.8/24')]
        self.now = 5; self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.p.enable_phone_nat.assert_not_called()


    def test_other_cars_get_phone_sharing_without_tesla_dns(self):
        self.status['tesla'] = False
        self.now = 5; self.u.tick()
        self.assertEqual(self.u.phase, 'online')
        # No Tesla check names; the own name is answered by default since 2026-10-29.
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ['example.duckdns.org'])
        self.p.connected.return_value = False; self.u.tick()
        self.assertFalse(self.status['tesla_offline'])
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ['example.duckdns.org'])
