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
from phone_uplink import PhoneUplink, JoinedPhoneAP, station_mac
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
        self.p.default_route_devices.return_value = []
        self.now = 0
        self.status = {'tesla':True,'own_name':'example.duckdns.org'}
        self.receiver = Mock()
        self.u = PhoneUplink(self.p, self.state, ipaddress.ip_interface('100.96.0.1/24'),
                            {'ssid':'Box','passphrase':'synthetic-box'},149,40,Mock(),self.receiver,
                            self.status, say=lambda _:None, clock=lambda:self.now)

    def online(self):
        self.now = 5; self.u.tick()
        self.assertEqual(self.status['uplink']['phase'], 'online')

    def test_start_offline_then_join_same_channel_and_forward(self):
        self.u.tick(); self.p.find_hotspot.assert_not_called()
        self.online()
        calls = [c[0] for c in self.p.mock_calls]
        self.assertLess(calls.index('prepare_phone_uplink'), calls.index('join_wifi'))
        self.assertLess(calls.index('join_wifi'), calls.index('add_phone_ap'))
        self.assertLess(calls.index('add_phone_ap'), calls.index('enable_phone_nat'))
        self.assertEqual(self.p.configure_hotspot.call_args.args[:2], (149,20))
        self.p.n_only_ap.assert_called_once()
        self.assertEqual(self.p.start_dhcp.call_args.kwargs['upstream'], '192.0.2.1')
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ['example.duckdns.org'],
                         'online DNS forwards other names but the box name stays on the box')

    def test_dnspod_name_stays_local_while_other_names_follow_upstream(self):
        self.status['tesla'] = False
        self.status['own_name'] = 'dashplay.apeng.fun'
        (self.state / 'ddns.json').write_text(json.dumps({
            'provider': 'dnspod', 'domain': 'dashplay.apeng.fun',
            'id': '10001', 'token': 'syntheticdnspodtoken1'}))
        self.online()
        self.assertEqual(self.p.start_dhcp.call_args.args[2], ['dashplay.apeng.fun'])
        self.assertEqual(self.p.start_dhcp.call_args.kwargs['upstream'], '192.0.2.1')
        self.receiver.assert_called_with(149)
        self.assertFalse(self.status['tesla_offline'])

    def test_loss_and_changed_channel_remove_nat_and_restore_checks(self):
        for change in ('loss','channel'):
            self.p.connected.return_value=True; self.p.frequency.return_value=5745
            self.now = self.u.next_try; self.u.tick()
            if change == 'loss': self.p.connected.return_value=False
            else: self.p.frequency.return_value=2412
            self.u.tick()
            self.assertEqual(self.u.phase,'offline')
            self.p.cleanup_phone_uplink.assert_called()
            self.assertIsNone(self.p.start_dhcp.call_args.kwargs['upstream'])
            self.assertIn(setup_wizard.TESLA_CHECK_NAMES[0], self.p.start_dhcp.call_args.args[2])
            self.assertTrue(self.p.start_dhcp.call_args.kwargs['router'], 'offline Tesla checks need a default route')
            self.assertGreater(self.u.next_try,self.now)

    def test_scan_errors_never_restart_network_or_receiver(self):
        for error in (subprocess.TimeoutExpired('iw', 10), OSError('busy'),
                      subprocess.CalledProcessError(1, 'iw')):
            with self.subTest(error=type(error).__name__):
                self.p.find_hotspot.side_effect = error
                self.now = self.u.next_try; self.u.tick()
                self.p.stop_child.assert_not_called()
                self.p.cleanup_phone_uplink.assert_not_called()
                self.receiver.assert_not_called()
                self.assertEqual(self.u.phase, 'offline')
                self.assertGreater(self.u.next_try, self.now)
        self.p.find_hotspot.side_effect = None
        self.now = self.u.next_try; self.u.tick()
        self.assertEqual(self.u.phase, 'online', 'scan errors must not prevent recovery')
        self.p.find_hotspot.assert_called_with('Phone', timeout=10)

    def test_unsuccessful_scan_does_not_restart_the_ap(self):
        for why in ('Device or resource busy (-16)', 'scan unsupported'):
            with self.subTest(why=why):
                self.p.find_hotspot.return_value = (False, False, why)
                self.now = self.u.next_try; self.u.tick()
                self.p.stop_child.assert_not_called()
                self.receiver.assert_not_called()
                self.assertEqual(self.u.phase, 'offline')
                self.assertGreater(self.u.next_try, self.now)

    def test_failure_after_network_changes_still_restores_offline_ap(self):
        self.p.prepare_phone_uplink.side_effect = OSError('synthetic failure')
        self.now = self.u.next_try; self.u.tick()
        self.p.cleanup_phone_uplink.assert_called_once()
        self.receiver.assert_called_once_with(149)
        self.assertEqual(self.u.phase, 'offline')
        self.assertTrue(self.status['tesla_offline'])

    def test_missing_hotspot_never_tears_down_ap_and_retries(self):
        self.p.find_hotspot.return_value = (True,False,'')
        self.now=5; self.u.tick()
        self.p.stop_child.assert_not_called()
        self.p.find_hotspot.return_value=(True,True,'')
        self.now=self.u.next_try; self.u.tick()
        self.assertEqual(self.u.phase,'online')

    def test_two_ap_failures_fall_back_and_auth_failure_does_too(self):
        self.p.start_ap.side_effect=[False,False,True]
        self.now=5; self.u.tick()
        self.assertEqual(self.p.start_ap.call_count,3)
        self.assertEqual(self.u.phase,'offline')
        self.p.start_ap.side_effect=None; self.p.join_wifi.return_value=False
        self.now=self.u.next_try; self.u.tick()
        self.assertEqual(self.u.phase,'offline')
        self.u.close(); self.p.cleanup_phone_uplink.assert_called()

    def test_a_working_cable_keeps_the_ap_and_skips_the_hotspot(self):
        self.p.default_route_devices.return_value = ['eth0']
        with patch('socket.getaddrinfo', return_value=[('AF_INET', 0, 0, '', ('192.0.2.9', 443))]):
            self.now = 5; self.u.tick()
        self.assertEqual(self.status['uplink']['phase'], 'wired')
        self.p.find_hotspot.assert_not_called()
        self.p.stop_child.assert_not_called()
        self.assertFalse(self.status['tesla_offline'], 'the cable has internet, no need for local Tesla answers')
        # the cable goes: the next look joins the phone hotspot
        self.p.default_route_devices.return_value = ['wlan0']
        self.now = self.u.next_try; self.u.tick()
        self.assertEqual(self.status['uplink']['phase'], 'online')

    def test_a_cable_route_without_name_resolution_still_joins(self):
        self.p.default_route_devices.return_value = ['eth0']
        with patch('socket.getaddrinfo', side_effect=OSError('no dns')):
            self.now = 5; self.u.tick()
        self.assertEqual(self.status['uplink']['phase'], 'online')

    def test_one_missing_ap_status_does_not_rebuild_the_ap(self):
        self.online()
        self.p.stop_child.reset_mock()   # the join itself replaces the AP once
        self.p.ap_ready.return_value = False
        self.u.tick(); self.u.tick()
        self.assertEqual(self.u.phase, 'online', 'two misses are not a dead AP')
        self.p.stop_child.assert_not_called()
        self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.p.stop_child.assert_called()

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

    def test_overlapping_phone_network_falls_back_without_nat(self):
        self.p.local_addresses.return_value = [ipaddress.ip_interface('100.96.0.8/24')]
        self.now = 5; self.u.tick()
        self.assertEqual(self.u.phase, 'offline')
        self.p.enable_phone_nat.assert_not_called()

    def test_other_cars_get_phone_sharing_without_tesla_dns(self):
        self.status['tesla'] = False
        self.now = 5; self.u.tick()
        self.assertEqual(self.u.phase, 'online')
        self.assertEqual(self.p.start_dhcp.call_args.args[2], [])
        self.p.connected.return_value = False; self.u.tick()
        self.assertFalse(self.status['tesla_offline'])
        self.assertEqual(self.p.start_dhcp.call_args.args[2], [])

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
