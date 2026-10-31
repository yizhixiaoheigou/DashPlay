"""Adapter contract tests: only temporary paths and mocked system commands, never live networking."""
import ipaddress
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
from platform_api import Platform, get_platform
from platform_debian import AP_VIF, DebianPlatform, ap_width, derived_address, five_ghz, hostapd_config, machine_key, supports_sta_ap, wifi_driver, wpa_network_lines
from setup_wizard import Scanner


class PlatformTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.platform = get_platform('wlan-test', run=self.root, sta_network=self.root / 'sta.network')

    def test_adapter_implements_contract_without_import_side_effects(self):
        self.assertIsInstance(self.platform, DebianPlatform)
        for name in Platform.__dict__:
            if not name.startswith('_'):
                self.assertTrue(callable(getattr(self.platform, name)), name)
        self.assertEqual(self.platform.children, [])

    @patch('platform_debian.subprocess.run')
    def test_scan_and_scanner_cache(self, run):
        run.return_value = Mock(returncode=0, stdout='BSS 00\n\tSSID: Test\n\tsignal: -40 dBm\n\tfreq: 5180\n')
        scan = Scanner('wlan-test', 'Box', lambda: 'ap', platform=self.platform)
        self.assertEqual(scan.results()[0]['ssid'], 'Test')
        scan.results()
        # The first list takes two scans (one misses hotspots, see the next test), later ones come from the cache.
        self.assertEqual(run.call_args_list, [call(['iw', 'dev', 'wlan-test', 'scan', 'ap-force'], capture_output=True,
                                                   text=True, timeout=30)] * 2)
        self.platform.scan(ap=True, frequency=5180)
        self.assertEqual(run.call_args.args[0][-3:], ['freq', '5180', 'ap-force'])
        run.side_effect = subprocess.TimeoutExpired('iw', 30)
        self.assertEqual([n['ssid'] for n in scan.results(fresh=True)], ['Test'], 'a failed scan keeps what was just seen')

    def test_scanner_adds_up_scans_that_each_miss_some_hotspots(self):
        # 2026-10-12, N1 with its own hotspot up: four scans in a row listed 9, 8, 11 and 11 names, 12 together.
        clock = [1000.0]
        results = [[{'ssid': 'Car', 'signal': -50, 'band': '5 GHz'}, {'ssid': 'A', 'signal': -70, 'band': '2.4 GHz'}],
                   [{'ssid': 'B', 'signal': -60, 'band': '5 GHz'}, {'ssid': 'A', 'signal': -65, 'band': '2.4 GHz'}],
                   [{'ssid': 'Car', 'signal': -45, 'band': '5 GHz'}]]
        platform = Mock(scan_networks=Mock(side_effect=lambda own, ap=False: results.pop(0)))
        scan = Scanner('wlan-test', 'Box', lambda: 'ap', platform=platform)
        with patch('setup_wizard.time.monotonic', side_effect=lambda: clock[0]):
            self.assertEqual([(n['ssid'], n['signal']) for n in scan.results()], [('Car', -50), ('B', -60), ('A', -65)])
            clock[0] += 30
            self.assertEqual([(n['ssid'], n['signal']) for n in scan.results(fresh=True)], [('Car', -45), ('B', -60), ('A', -65)])
            # Not seen for over two minutes: gone from the list.
            results.append([{'ssid': 'Car', 'signal': -44, 'band': '5 GHz'}])
            clock[0] += 125
            self.assertEqual([n['ssid'] for n in scan.results(fresh=True)], ['Car'])

    def test_find_hotspot_decodes_iw_names_and_matches_exactly(self):
        for name in ('测试热点', 'Phone’s WiFi', r'Phone\test', ' Phone '):
            with self.subTest(name=name):
                encoded = ''.join(f'\\x{byte:02x}' for byte in name.encode())
                scan = f'BSS 02:00:00:00:00:01\n\tSSID: {encoded}\n\tsignal: -40 dBm\n'
                with patch.object(self.platform, 'scan', return_value=Mock(returncode=0, stdout=scan, stderr='')) as run:
                    self.assertEqual(self.platform.find_hotspot(name, 5745), (True, True, ''))
                    run.assert_called_with(ap=True, frequency=5745, timeout=30)
                    self.assertFalse(self.platform.find_hotspot(name + ' other')[1])

    def test_find_hotspot_does_not_trim_a_different_name_or_hide_scan_failure(self):
        with patch.object(self.platform, 'scan', return_value=Mock(
                returncode=0, stdout='BSS 02:00:00:00:00:01\n\tSSID: Phone \n', stderr='')):
            self.assertFalse(self.platform.find_hotspot('Phone')[1])
        with patch.object(self.platform, 'scan', return_value=Mock(returncode=1, stdout='', stderr='scan failed')):
            self.assertEqual(self.platform.find_hotspot('Phone'), (False, False, 'scan failed'))

    def test_find_hotspot_ignores_results_cached_from_earlier_scans(self):
        block = 'BSS 02:00:00:00:00:01(on wlan0)\n\tfreq: 5745.0\n\tlast seen: {} ms ago\n\tSSID: Phone\n'
        for age, frequency, expected in ((9147, None, False), (0, 5180, False), (0, 5745, True), (0, None, True)):
            with self.subTest(age=age, frequency=frequency):
                scan = Mock(returncode=0, stdout=block.format(age), stderr='')
                with patch.object(self.platform, 'scan', return_value=scan):
                    self.assertEqual(self.platform.find_hotspot('Phone', frequency)[1], expected)

    def test_uplink_scan_has_a_shorter_deadline_without_changing_setup_scans(self):
        with patch('platform_debian.subprocess.run', return_value=Mock(returncode=0, stdout='', stderr='')) as run:
            self.platform.find_hotspot('Phone', timeout=10)
            run.assert_called_once_with(['iw', 'dev', 'wlan-test', 'scan', 'ap-force'],
                                        capture_output=True, text=True, timeout=10)
            self.platform.scan()
            self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_phone_ap_waits_for_networkmanager_release_before_assigning_mac(self):
        (self.root / 'phone-uplink.json').write_text('{"mac":"00:00:5e:00:53:01"}')
        states = iter(['100 (connected)', '10 (unmanaged)'])
        def run(argv, **kwargs):
            if argv[:2] == ['nmcli', '-t']:
                return Mock(returncode=0, stdout='running\n')
            if argv[:2] == ['nmcli', '-g']:
                return Mock(returncode=0, stdout=next(states))
            return Mock(returncode=0, stdout='')
        with patch('shutil.which', return_value='/usr/bin/nmcli'), \
                patch('platform_debian.subprocess.run', side_effect=run) as command, \
                patch('platform_debian.time.sleep'):
            self.assertTrue(self.platform.add_phone_ap())
        calls = [c.args[0] for c in command.call_args_list]
        release = ['nmcli', 'device', 'set', 'ap0', 'managed', 'no']
        address = ['ip', 'link', 'set', 'dev', 'ap0', 'address', '00:00:5e:00:53:01']
        self.assertEqual(calls.count(release), 2)
        self.assertLess(calls.index(release), calls.index(address))
        self.assertEqual(calls[-1], address)
        self.assertFalse(any('--permanent' in c or 'reload' in c for c in calls))

    def test_phone_ap_refuses_to_configure_when_networkmanager_keeps_ownership(self):
        (self.root / 'phone-uplink.json').write_text('{"mac":"00:00:5e:00:53:01"}')
        def run(argv, **kwargs):
            return Mock(returncode=0, stdout='running' if argv[:2] == ['nmcli', '-t'] else '100 (connected)')
        with patch('shutil.which', return_value='/usr/bin/nmcli'), \
                patch('platform_debian.subprocess.run', side_effect=run) as command, \
                patch('platform_debian.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'NetworkManager'):
                self.platform.add_phone_ap()
        calls = [c.args[0] for c in command.call_args_list]
        self.assertFalse(any(c[:3] == ['ip', 'link', 'set'] for c in calls))
        self.assertEqual(self.platform.ap_interface, 'ap0')  # lifecycle cleanup can remove it
        self.assertTrue((self.root / 'phone-uplink.json').exists())

    def test_phone_ap_waits_for_networkmanager_to_discover_new_interface(self):
        (self.root / 'phone-uplink.json').write_text('{"mac":"00:00:5e:00:53:01"}')
        states = iter([Mock(returncode=10, stdout=''), Mock(returncode=0, stdout='10 (unmanaged)')])
        def run(argv, **kwargs):
            if argv[:2] == ['nmcli', '-t']:
                return Mock(returncode=0, stdout='running')
            if argv[:2] == ['nmcli', '-g']:
                return next(states)
            return Mock(returncode=10 if argv[:2] == ['nmcli', 'device'] else 0)
        with patch('shutil.which', return_value='/usr/bin/nmcli'), \
                patch('platform_debian.subprocess.run', side_effect=run), patch('platform_debian.time.sleep'):
            self.assertTrue(self.platform.add_phone_ap())

    def test_phone_ap_does_not_ignore_networkmanager_probe_error_or_timeout(self):
        (self.root / 'phone-uplink.json').write_text('{"mac":"00:00:5e:00:53:01"}')
        for response in (Mock(returncode=1, stdout=''), subprocess.TimeoutExpired('nmcli', 5)):
            def run(argv, **kwargs):
                if argv[0] == 'nmcli':
                    if isinstance(response, Exception):
                        raise response
                    return response
                return Mock(returncode=0)
            with self.subTest(response=response), patch('shutil.which', return_value='/usr/bin/nmcli'), \
                    patch('platform_debian.subprocess.run', side_effect=run) as command:
                with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                    self.platform.add_phone_ap()
                self.assertFalse(any(c.args[0][:3] == ['ip', 'link', 'set'] for c in command.call_args_list))

    def test_phone_ap_without_networkmanager_needs_no_extra_management(self):
        (self.root / 'phone-uplink.json').write_text('{"mac":"00:00:5e:00:53:01"}')
        for binary, running in ((None, None), ('/usr/bin/nmcli', 'not running')):
            with self.subTest(binary=binary), patch('shutil.which', return_value=binary), \
                    patch('platform_debian.subprocess.run', return_value=Mock(returncode=0, stdout=running)) as command:
                self.assertTrue(self.platform.add_phone_ap())
                self.assertFalse(any(c.args[0][:3] == ['nmcli', 'device', 'set'] for c in command.call_args_list))

    @patch('platform_debian.subprocess.run')
    def test_bluetooth_and_link_observation(self, run):
        run.return_value = Mock(returncode=0, stdout='Connected to synthetic\n freq: 5180\n')
        self.assertTrue(self.platform.power_bluetooth())
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [['btmgmt', '--index', '0', 'info'], ['bluetoothctl', 'power', 'on'], ['bluetoothctl', 'devices', 'Connected'],
                          ['bluetoothctl', 'system-alias', 'DashPlay']])
        self.assertTrue(self.platform.connected())
        self.assertEqual(self.platform.frequency(), 5180)
        run.return_value = Mock(returncode=1, stdout='Not connected.')
        self.assertFalse(self.platform.power_bluetooth())
        self.assertFalse(self.platform.connected())
        self.assertIsNone(self.platform.frequency())

    def test_bluetooth_waits_for_its_bus_without_running_bluetoothctl(self):
        # 2026-10-16, F50: bluetoothctl segfaults while the board helper's D-Bus socket is not there yet; polled
        # every 0.5 s it crashed a dozen times per boot, each with a register dump in the kernel log
        bus = self.root / 'system_bus_socket'
        with patch.dict('os.environ', {'DBUS_SYSTEM_BUS_ADDRESS': f'unix:path={bus}'}), \
                patch('platform_debian.subprocess.run', return_value=Mock(returncode=0, stdout='')) as run:
            self.assertFalse(self.platform.power_bluetooth())
            run.assert_not_called()
            bus.touch()
            self.assertTrue(self.platform.power_bluetooth())
            self.assertIn(['bluetoothctl', 'power', 'on'], [c.args[0] for c in run.call_args_list])

    @patch('platform_debian.subprocess.run')
    def test_bluetooth_start_clears_a_leftover_phone_link(self, run):
        run.side_effect = lambda argv, **_: Mock(returncode=0, stdout=(
            'Device 02:00:00:00:00:55 Phone\n' if argv[1:] == ['devices', 'Connected'] else ''))
        self.assertTrue(self.platform.power_bluetooth())
        self.assertEqual([c.args[0][1:] for c in run.call_args_list],
                         [['--index', '0', 'info'], ['power', 'on'], ['devices', 'Connected'], ['power', 'off'], ['power', 'on'],
                          ['system-alias', 'DashPlay']])

    def test_machine_key_prefers_the_emmc_over_machine_id(self):
        mmc = self.root / 'mmc'
        for name, kind, cid in (('mmc0:0001', 'SDIO', ''), ('mmc2:0001', 'MMC', 'cid-of-the-emmc')):
            (mmc / name).mkdir(parents=True)
            (mmc / name / 'type').write_text(kind + '\n')
            (mmc / name / 'cid').write_text(cid + '\n')
        (self.root / 'machine-id').write_text('machine-id-test\n')
        self.assertEqual(machine_key(mmc, self.root / 'machine-id'), 'cid-of-the-emmc')
        self.assertEqual(machine_key(self.root / 'none', self.root / 'machine-id'), 'machine-id-test')
        a, b = derived_address('k1', 'wifi'), derived_address('k1', 'bluetooth')
        self.assertRegex(a, r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$')
        self.assertEqual(int(a[:2], 16) & 0x03, 0x02, 'locally administered unicast')
        self.assertNotEqual(a, b)
        self.assertEqual(a, derived_address('k1', 'wifi'), 'the same box keeps its address')
        self.assertNotEqual(a, derived_address('k2', 'wifi'), 'another box gets another one')

    @patch('platform_debian.subprocess.run')
    def test_vendor_default_addresses_become_unique_per_box(self, run):
        # Phicomm N1: every unit has the BCM43455 nvram MAC and the BCM4345C0 Bluetooth default.
        net = self.root / 'net/wlan-test'
        net.mkdir(parents=True)
        (net / 'address').write_text('b8:27:eb:74:f2:6c\n')
        # The kernel's own view (btmgmt) works before bluetoothd is up early in boot. BlueZ drops the adapter
        # when its address changes and lists it again under the new one a moment later.
        shown = ['No default controller available\n', f'Controller {derived_address("box1", "bluetooth").upper()} (public)\n']
        def fake(argv, **_):
            if argv == ['btmgmt', '--index', '0', 'info']:
                return Mock(returncode=0, stdout='hci0:\tPrimary controller\n\taddr 43:45:C0:00:1F:AC version 9\n')
            if argv == ['bluetoothctl', 'show']:
                return Mock(returncode=0, stdout=shown.pop(0) if len(shown) > 1 else shown[0])
            return Mock(returncode=0, stdout='')
        run.side_effect = fake
        with patch('platform_debian.SYS_NET', self.root / 'net'), patch('platform_debian.machine_key', return_value='box1'), \
             patch('platform_debian.time.sleep'):
            self.platform.unique_wifi_address()
            self.assertTrue(self.platform.power_bluetooth())
        wifi, bluetooth = derived_address('box1', 'wifi'), derived_address('box1', 'bluetooth')
        argv = [c.args[0] for c in run.call_args_list]
        self.assertEqual(argv[:2], [['ip', 'link', 'set', 'dev', 'wlan-test', 'down'],
                                    ['ip', 'link', 'set', 'dev', 'wlan-test', 'address', wifi]])
        self.assertEqual(argv[2:8], [['btmgmt', '--index', '0', 'info'], ['btmgmt', '--index', '0', 'power', 'off'],
                                     ['btmgmt', '--index', '0', 'public-addr', bluetooth],
                                     ['bluetoothctl', 'show'], ['bluetoothctl', 'show'], ['bluetoothctl', 'power', 'on']])
        self.assertTrue(all(c.kwargs.get('input') == '' for c in run.call_args_list if c.args[0][0] == 'btmgmt'),
                        'btmgmt hangs with stdin on /dev/null, as in the service')
        # A failing step leaves the address as it is but never stops Bluetooth from starting.
        run.reset_mock()
        run.side_effect = lambda argv, **_: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 10)) \
            if argv[0] == 'btmgmt' else Mock(returncode=0, stdout='')
        with patch('platform_debian.machine_key', return_value='box1'):
            self.assertTrue(self.platform.power_bluetooth())
        with patch('platform_debian.SYS_NET', self.root / 'net'), patch('platform_debian.machine_key', side_effect=OSError):
            (net / 'address').write_text('b8:27:eb:74:f2:6c\n')
            self.platform.unique_wifi_address()   # no key to derive from: left as it is, no exception
        # A unique address from the hardware (an OneCloud dongle) is left as it is.
        run.reset_mock()
        (net / 'address').write_text('00:00:5e:00:53:01\n')
        run.side_effect = lambda argv, **_: Mock(returncode=0, stdout=(
            'hci0:\tPrimary controller\n\taddr 00:00:5E:00:53:02 version 9\n' if argv[0] == 'btmgmt' else ''))
        with patch('platform_debian.SYS_NET', self.root / 'net'):
            self.platform.unique_wifi_address()
            self.assertTrue(self.platform.power_bluetooth())
        self.assertNotIn(['btmgmt', '--index', '0', 'power', 'off'], [c.args[0] for c in run.call_args_list])
        self.assertNotIn('ip', [c.args[0][0] for c in run.call_args_list])

    @patch('platform_debian.subprocess.run')
    def test_quick_ack_on_the_car_subnet(self, run):
        run.return_value = Mock(returncode=0, stdout='10.239.116.0/24 proto kernel src 10.239.116.212 metric 2048\n')
        self.platform.quick_ack()
        self.assertEqual(run.call_args.args[0], ['ip', 'route', 'change', '10.239.116.0/24', 'proto', 'kernel', 'src',
                                                  '10.239.116.212', 'metric', '2048', 'dev', 'wlan-test', 'quickack', '1'])

    def test_interface_and_optional_leds(self):
        (self.root / 'wlan-test' / 'wireless').mkdir(parents=True)
        self.assertEqual(self.platform.wifi_interface(self.root), 'wlan-test')
        # The box-hotspot interface created beside the client is never taken for the dongle itself.
        (self.root / 'dpap0' / 'phy80211').mkdir(parents=True)
        self.assertEqual(self.platform.wifi_interface(self.root), 'wlan-test')
        self.platform.status_light(self.root / 'missing').show('live')

    def test_brcmfmac_hotspot_keeps_the_driver_regulatory_domain(self):
        # Phicomm N1 (2026-10-12): once hostapd sets CN, brcmfmac disables 36-48 (CN marks them DFS),
        # so a car hotspot there is never seen; its own domain allows 36-48 and an AP on 149.
        with patch('platform_debian.wifi_driver', return_value='brcmfmac'):
            self.platform.configure_hotspot(149, 40, 'Box', 'synthetic-password')
        config = (self.root / 'hostapd.conf').read_text()
        self.assertNotIn('country_code', config)
        self.assertNotIn('ieee80211d', config)
        with patch('platform_debian.wifi_driver', return_value='rtw88_8821cu'):
            self.platform.configure_hotspot(149, 40, 'Box', 'synthetic-password')
        self.assertIn('country_code=CN', (self.root / 'hostapd.conf').read_text())

    def test_wifi_driver_from_sysfs(self):
        (self.root / 'drivers' / 'brcmfmac').mkdir(parents=True)
        (self.root / 'wlan-test' / 'device').mkdir(parents=True)
        (self.root / 'wlan-test' / 'device' / 'driver').symlink_to(self.root / 'drivers' / 'brcmfmac')
        self.assertEqual(wifi_driver('wlan-test', self.root), 'brcmfmac')
        self.assertIsNone(wifi_driver('missing', self.root))

    @patch('platform_debian.subprocess.run')
    def test_ap_address_config_and_dhcp_modes(self, run):
        self.platform.configure_hotspot(149, 80, 'Box', 'synthetic-password')
        config = (self.root / 'hostapd.conf').read_text()
        self.assertIn('vht_oper_centr_freq_seg0_idx=155', config)
        self.assertIn('wpa_passphrase=synthetic-password', config)
        self.platform.configure_hotspot(6, 20, 'Box', None)
        self.assertNotIn('wpa=', (self.root / 'hostapd.conf').read_text())
        address = ipaddress.ip_interface('192.0.2.1/24')
        self.platform.set_ap_address(address)
        self.assertEqual([c.args[0][1:3] for c in run.call_args_list], [['addr', 'flush'], ['link', 'set'], ['addr', 'add']])
        with patch.object(self.platform, 'start') as start:
            self.platform.start_dhcp(address, True)
            self.assertIn('--address=/#/192.0.2.1', start.call_args.args[1])
            self.assertIn('--dhcp-range=192.0.2.10,192.0.2.100,255.255.255.0,12h', start.call_args.args[1])
            self.platform.start_dhcp(address, False)
            self.assertIn('--port=0', start.call_args.args[1])
            self.assertIn('--dhcp-option=3', start.call_args.args[1])
            # The own domain only: the box is the hotspot's DNS server, still no router; no upstream, so every
            # other name is REFUSED and a car's resolver moves on to its next server (dnsmasq 2.91 on the N1, 2026-10-15).
            self.platform.start_dhcp(address, False, ['mycar.duckdns.org'])
            argv = start.call_args.args[1]
            for flag in ('--port=53', '--no-resolv', '--no-hosts', '--address=/mycar.duckdns.org/192.0.2.1',
                         '--dhcp-option=3', '--dhcp-option=6,192.0.2.1'):
                self.assertIn(flag, argv)
            self.assertNotIn('--address=/#/192.0.2.1', argv)
            self.platform.start_dhcp(address, True, ['mycar.duckdns.org'])
            self.assertIn('--address=/#/192.0.2.1', start.call_args.args[1], 'first-run setup answers every name')

    def test_station_plus_ap_detection_from_iw_list(self):
        rebuilt = ('\tvalid interface combinations:\n\t\t * #{ managed } <= 1, #{ AP } <= 1,\n'
                   '\t\t   total <= 2, #channels <= 1\n\tHT Capability overrides:\n')
        vendor = ('\tvalid interface combinations:\n\t\t * #{ managed, P2P-client } <= 2, #{ AP, P2P-GO } <= 1,\n'
                  '\t\t   total <= 2, #channels <= 1\n')
        self.assertTrue(supports_sta_ap(rebuilt))
        self.assertTrue(supports_sta_ap(vendor))
        mt7921 = ('\tvalid interface combinations:\n\t\t * #{ managed, P2P-client } <= 2, #{ AP, P2P-GO } <= 1,\n'
                  '\t\t   total <= 2, #channels <= 2\n')
        self.assertEqual((supports_sta_ap(rebuilt), supports_sta_ap(mt7921)), (1, 2))
        self.assertFalse(supports_sta_ap('\tinterface combinations are not supported\n'))   # stock rtw88
        self.assertFalse(supports_sta_ap(''))
        self.assertFalse(supports_sta_ap('\tvalid interface combinations:\n\t\t * #{ managed } <= 2,\n\t\t   total <= 2\n'))

    def test_a_snapdragon_410_stick_has_no_port_for_the_iphone(self):
        tree = self.platform.run / 'compatible'
        tree.write_bytes(b'thwc,ufi001c\0qcom,msm8916\0')
        self.assertFalse(self.platform.wired_capable(tree))
        tree.write_bytes(b'phicomm,n1\0amlogic,s905d\0amlogic,meson-gxl\0')
        self.assertTrue(self.platform.wired_capable(tree))
        self.assertTrue(self.platform.wired_capable(self.platform.run / 'absent'))

    def test_five_ghz_band_from_iw_list(self):
        # Shaped like `iw list` of the WCN3620 in Snapdragon 410 sticks: wcn36xx registers Band 1 only.
        wcn3620 = ('Wiphy phy0\n\tBand 1:\n\t\tFrequencies:\n\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n'
                   '\t\t\t* 2484.0 MHz [14] (disabled)\n')
        dual = wcn3620 + '\tBand 2:\n\t\tFrequencies:\n\t\t\t* 5180.0 MHz [36] (20.0 dBm)\n'
        off = wcn3620 + '\tBand 2:\n\t\tFrequencies:\n\t\t\t* 5180.0 MHz [36] (disabled)\n'
        self.assertEqual((five_ghz(wcn3620), five_ghz(dual), five_ghz(off)), (False, True, False))
        self.assertIsNone(five_ghz(''), 'iw failed: unknown, not 2.4 GHz only')

    def test_box_hotspot_width_follows_the_car_channel(self):
        self.assertEqual(ap_width(149, 80), 80)
        self.assertEqual(ap_width(36, 80), 80)
        self.assertEqual(ap_width(165, 80), 20)   # no 40/80 MHz partner
        self.assertEqual(ap_width(6, 80), 20)
        self.assertNotIn('vht_oper', hostapd_config('ap', 165, 80, 'Box', 'synthetic-password'))
        self.assertNotIn('HT40', hostapd_config('ap', 165, 80, 'Box', 'synthetic-password'))
        self.assertIn('hw_mode=g', hostapd_config('ap', 11, 80, 'Box', 'synthetic-password'))
        self.assertIn('vht_oper_centr_freq_seg0_idx=42', hostapd_config('ap', 44, 80, 'Box', 'synthetic-password'))

    @patch('platform_debian.subprocess.run')
    def test_second_interface_carries_the_box_hotspot(self, run):
        net = self.root / 'net'
        (net / 'wlan-test').mkdir(parents=True)
        (net / 'wlan-test' / 'address').write_text('00:00:5e:00:53:01\n')
        real = Path
        def fake_path(*parts):
            if parts and parts[0] == '/sys/class/net':
                return real(net, *parts[1:])
            return real(*parts)
        run.return_value = Mock(returncode=0, stdout='')
        with patch('platform_debian.Path', side_effect=fake_path):
            self.assertTrue(self.platform.add_ap_interface())
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(['iw', 'dev', 'wlan-test', 'interface', 'add', AP_VIF, 'type', '__ap'], commands)
        self.assertIn(['ip', 'link', 'set', AP_VIF, 'address', '02:00:5e:00:53:01'], commands)   # local bit flipped
        self.assertEqual(self.platform.ap_interface, AP_VIF)
        self.platform.configure_hotspot(149, 80, 'Box', 'synthetic-password')
        self.assertIn(f'interface={AP_VIF}', (self.root / 'hostapd.conf').read_text())
        run.reset_mock()
        self.platform.set_ap_address(ipaddress.ip_interface('192.0.2.1/24'))
        self.assertTrue(all(AP_VIF in c.args[0] for c in run.call_args_list))
        with patch.object(self.platform, 'start') as start:
            self.platform.start_dhcp(ipaddress.ip_interface('192.0.2.1/24'), False)
            self.assertIn(f'--interface={AP_VIF}', start.call_args.args[1])
        run.reset_mock()
        self.platform.has_stations()
        self.assertEqual(run.call_args.args[0][:3], ['iw', 'dev', AP_VIF])
        # The driver refuses: nothing is left behind and the hotspot stays on the dongle itself.
        run.reset_mock()
        run.return_value = Mock(returncode=1, stdout='')
        with patch('platform_debian.Path', side_effect=fake_path):
            self.assertFalse(self.platform.add_ap_interface())
        self.assertEqual(self.platform.ap_interface, 'wlan-test')

    @patch('platform_debian.subprocess.run')
    def test_join_scans_last_frequency_first_then_everything(self, run):
        run.return_value = Mock(returncode=0, stdout='Connected to synthetic\nfreq: 5805')
        car = {'ssid': 'Car', 'passphrase': 'synthetic1'}
        lease = {'ssid': 'Car', 'address': '192.0.2.9', 'freq': 5805}
        confs, waits = [], []
        def wait(predicate, seconds, interval):
            waits.append(seconds)
            confs.append((self.root / 'wpa.conf').read_text())
            if seconds == 8:   # not found on the old channel: the car moved
                raise TimeoutError
        with patch.object(self.platform, 'start'), patch.object(self.platform, 'ipv4_address', return_value='192.0.2.9'):
            self.assertTrue(self.platform.join_wifi(car, lease, wait, 60))
        self.assertEqual(waits, [30, 8, 52, 30])
        self.assertIn('scan_freq=5805', confs[1])
        self.assertNotIn('scan_freq', confs[2])
        self.assertIn(['wpa_cli', '-p', str(self.root / 'wpa'), '-i', 'wlan-test', 'reconfigure'],
                      [c.args[0] for c in run.call_args_list])
        # Another car hotspot than last time: no frequency hint.
        with patch.object(self.platform, 'start'), patch.object(self.platform, 'ipv4_address', return_value='192.0.2.9'):
            self.platform.join_wifi(dict(car, ssid='Other'), lease, lambda *a, **k: None, 60)
        self.assertNotIn('scan_freq', (self.root / 'wpa.conf').read_text())

    @patch('platform_debian.subprocess.run')
    def test_join_preserves_lease_escaping_and_timeout_cleanup(self, run):
        run.return_value = Mock(returncode=0, stdout='Connected to synthetic\nfreq: 5180')
        car = {'ssid': 'Test"\\', 'passphrase': 'synthetic"\\'}
        waits = []
        def wait(predicate, seconds, interval):
            waits.append((seconds, interval))
            self.assertTrue(predicate())
        with patch.object(self.platform, 'start') as start, patch.object(self.platform, 'ipv4_address', return_value='192.0.2.9'):
            self.assertTrue(self.platform.join_wifi(car, {'ssid': car['ssid'], 'address': '192.0.2.9'}, wait, 60))
        self.assertEqual(waits, [(30, .25), (60, .25), (30, .2)])
        self.assertIn('RequestAddress=192.0.2.9', self.platform.sta_network.read_text())
        self.assertEqual(self.platform.sta_network.stat().st_mode & 0o777, 0o644)
        # The name in hex and the password as written (wpa_supplicant has no escapes in quoted values).
        conf = (self.root / 'wpa.conf').read_text()
        self.assertIn(' ssid=' + 'Test"\\'.encode().hex() + '\n', conf)
        self.assertIn(' psk="synthetic"\\"\n', conf)
        self.assertIn('key_mgmt=WPA-PSK SAE', (self.root / 'wpa.conf').read_text())
        with patch.object(self.platform, 'start'), patch.object(self.platform, 'stop_child') as stop:
            self.assertFalse(self.platform.join_wifi(car, {}, Mock(side_effect=TimeoutError), 60))
            stop.assert_called_once_with('wpa_supplicant')
        self.assertFalse(self.platform.sta_network.exists())
        self.assertEqual(run.call_args.args[0], ['ip', 'addr', 'flush', 'dev', 'wlan-test'])

    @patch('platform_debian.os.killpg')
    @patch('platform_debian.subprocess.Popen')
    def test_service_stop_escalation_and_receiver_shutdown_allowance(self, popen, kill):
        child = popen.return_value
        child.poll.return_value = None
        child.pid = 123
        self.assertIs(self.platform.start('receiver', ['synthetic']), child)
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        child.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 8), None]
        self.platform.stop_child('receiver')
        self.assertEqual(kill.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertEqual(self.platform.children, [])
        child.wait.side_effect = None
        self.platform.start('receiver', ['synthetic'])
        self.platform.stop_all()
        child.wait.assert_called_with(timeout=40)

    def test_ap_exit_or_ready(self):
        child = Mock()
        child.poll.return_value = None
        with patch.object(self.platform, 'start', return_value=child), patch.object(self.platform, 'ap_ready', return_value=True):
            def wait(predicate, seconds):
                self.assertEqual(seconds, 30)
                self.assertTrue(predicate())
            self.assertTrue(self.platform.start_ap(wait))
            child.poll.return_value = 1
            self.assertFalse(self.platform.start_ap(wait))

    def test_ap_that_never_comes_up_lets_the_next_attempt_run(self):
        # hostapd alive but never AP-ENABLED: the timeout ended the supervisor, so the 40 MHz and 2.4 GHz
        # fallbacks never ran (review 2026-10-12).
        child = Mock()
        child.poll.return_value = None
        def wait(predicate, seconds):
            raise TimeoutError("Service dependency did not become ready")
        with patch.object(self.platform, 'start', return_value=child), patch.object(self.platform, 'ap_ready', return_value=False), \
                patch.object(self.platform, 'stop_child') as stop:
            self.assertFalse(self.platform.start_ap(wait))
        stop.assert_called_once_with('hostapd')

    @patch('platform_debian.subprocess.run')
    def test_ap_ready_trusts_hostapd_when_driver_reports_no_channel(self, run):
        # Realtek 8821cu concurrent build: an AP without a busy station interface shows no channel.
        run.return_value = Mock(returncode=0, stdout='Interface wlan-test\n\tssid DashPlay\n\ttype AP\n')
        (self.platform.run / 'hostapd.log').write_text('wlan-test: interface state COUNTRY_UPDATE->HT_SCAN\n')
        self.assertFalse(self.platform.ap_ready())
        (self.platform.run / 'hostapd.log').write_text('wlan-test: AP-ENABLED \n')
        self.assertTrue(self.platform.ap_ready())

    @patch('platform_debian.subprocess.run')
    def test_ap_ready_ignores_driver_while_hostapd_starts(self, run):
        # Phicomm N1 brcmfmac: type AP with a channel as soon as hostapd starts, before it fails to set
        # an 80 MHz channel and logs AP-DISABLED.
        run.return_value = Mock(returncode=0, stdout='Interface wlan-test\n\ttype AP\n\tchannel 153 (5765 MHz)\n')
        (self.platform.run / 'hostapd.log').write_text('wlan-test: interface state COUNTRY_UPDATE->HT_SCAN\n')
        self.assertFalse(self.platform.ap_ready())
        (self.platform.run / 'hostapd.log').write_text(
            'nl80211: kernel reports: (extension) channel is disabled\nwlan-test: AP-DISABLED \n')
        self.assertFalse(self.platform.ap_ready())

    def test_hotspot_attempts_narrow_before_leaving_5ghz(self):
        import supervisor
        self.assertEqual(supervisor.hotspot_attempts(149, 80), [(149, 80), (149, 40), (6, 20)])
        self.assertEqual(supervisor.hotspot_attempts(149, 40), [(149, 40), (6, 20)])
        self.assertEqual(supervisor.hotspot_attempts(165, 80), [(165, 80), (6, 20)])
        self.assertEqual(supervisor.hotspot_attempts(6, 20), [(6, 20)])

    def test_radio_without_5ghz_starts_its_hotspot_on_channel_6(self):
        import supervisor
        self.assertEqual(supervisor.radio_hotspot(149, 80, False), (6, 20))
        self.assertEqual(supervisor.radio_hotspot(149, 80, True), (149, 80))
        self.assertEqual(supervisor.radio_hotspot(149, 80, None), (149, 80), 'iw failed: keep the configured channel')
        self.assertEqual(supervisor.radio_hotspot(11, 20, False), (11, 20))
        self.assertEqual(supervisor.radio_hotspot(149, 80, False, 11), (11, 20))
        self.assertEqual(supervisor.radio_hotspot(11, 20, False, 1), (11, 20), 'a 2.4 GHz channel set by hand stays')

    @staticmethod
    def bss(n, freq, signal, offset='no secondary'):
        return (f'BSS 00:11:22:33:44:{n:02x}(on wlan0)\n\tfreq: {freq}.0\n\tsignal: {signal}.00 dBm\n\tSSID: net{n}\n'
                f'\tHT operation:\n\t\t * secondary channel offset: {offset}\n')

    def test_quiet_channel_leaves_a_crowded_6(self):
        import supervisor
        # The 410 stick at home: a strong router on 3 (overlapping 1 and 6), three networks on 6, a 40 MHz one on
        # 8 (secondary above), two faint ones on 11 and a 5 GHz one that does not count.
        scan = (self.bss(1, 2422, -31) + self.bss(2, 2437, -45) + self.bss(3, 2437, -60) + self.bss(4, 2437, -70)
                + self.bss(5, 2447, -50, 'above') + self.bss(6, 2462, -71) + self.bss(7, 2462, -81)
                + self.bss(8, 5745, -20))
        self.assertEqual(supervisor.quiet_channel(scan), 11)

    def test_quiet_channel_stays_on_6_unless_another_is_clearly_quieter(self):
        import supervisor
        self.assertEqual(supervisor.quiet_channel(''), 6, 'no scan: channel 6 as before')
        self.assertEqual(supervisor.quiet_channel(self.bss(1, 2412, -60) + self.bss(2, 2437, -60) + self.bss(3, 2462, -60)), 6)
        self.assertEqual(supervisor.quiet_channel(self.bss(1, 2412, -62) + self.bss(2, 2437, -60) + self.bss(3, 2462, -62)), 6,
                         'only a little quieter')
        self.assertEqual(supervisor.quiet_channel(self.bss(1, 2437, -60) + self.bss(2, 2462, -62)), 1, '1 is empty')
        self.assertEqual(supervisor.quiet_channel(self.bss(1, 2437, -60) + self.bss(2, 2462, -60) + self.bss(3, 2412, -70)), 1)
        self.assertEqual(supervisor.quiet_channel(self.bss(1, 2437, -60, 'below')), 11, 'a 40 MHz secondary on 2 loads 1')

class KnownFrequencyTests(unittest.TestCase):
    def test_car_moved_channel_is_seen_from_one_scan(self):
        import platform_debian
        header = "bssid / frequency / signal level / flags / ssid\n"
        other = "02:00:00:00:00:01\t5745\t-60\t[WPA2-PSK-CCMP][ESS]\tneighbour\n"
        car = "02:00:00:00:00:02\t5745\t-40\t[WPA2-PSK-CCMP][ESS]\t\\xe5\\xb0\\x8f\\xe9\\xb9\\x8f\\xe6\\xb1\\xbd\\xe8\\xbd\\xa6\n"
        self.assertTrue(platform_debian.missed_on_known_freq(header + other, "小鹏汽车"))
        self.assertFalse(platform_debian.missed_on_known_freq(header + other + car, "小鹏汽车"))
        self.assertFalse(platform_debian.missed_on_known_freq(header, "小鹏汽车"))   # no scan finished yet

class HotspotInjectionTests(unittest.TestCase):
    """A newline in the hotspot name or password added its own hostapd line: "abcdefgh\nwpa=0" passed the
    length check and turned the hotspot open (review 2026-10-12)."""

    def test_hostapd_config_refuses_control_characters(self):
        with self.assertRaises(ValueError):
            hostapd_config("wlan1", 149, 80, "DashPlay", "abcdefgh\nwpa=0")
        with self.assertRaises(ValueError):
            hostapd_config("wlan1", 149, 80, "Dash\nwpa=0", "abcdefgh")
        self.assertIn("wpa_passphrase=a b'c\"d\\e", hostapd_config("wlan1", 149, 80, "Dash Play", "a b'c\"d\\e"))

    def test_settings_page_asks_for_a_wpa_password(self):
        from settings_page import _hotspot_credentials
        with self.assertRaises(ValueError):
            _hotspot_credentials({"ssid": "DashPlay", "password": "abcdefgh\nwpa=0"}, {})
        with self.assertRaises(ValueError):
            _hotspot_credentials({"ssid": "DashPlay", "password": "密码密码密码密码"}, {})   # not ASCII
        with self.assertRaises(ValueError):
            _hotspot_credentials({"ssid": "Dash\tPlay", "password": "abcdefgh"}, {})
        self.assertEqual(_hotspot_credentials({"ssid": "小鹏 G6", "password": "a b'c\"de"}, {}), ("小鹏 G6", "a b'c\"de"))



class WpaSupplicantQuotingTests(unittest.TestCase):
    """wpa_supplicant takes a quoted value up to its last quote, without escapes: the backslashes added for \\
    and " became part of the password, and such car hotspots never connected (review 2026-10-12)."""

    def test_name_in_hex_and_password_as_written(self):
        self.assertEqual(wpa_network_lines('小鹏 "G6"', 'pa\\ss"word'),
                         [" ssid=" + '小鹏 "G6"'.encode().hex(), ' psk="pa\\ss"word"'])
        hex_key = "0123456789abcdef" * 4
        self.assertEqual(wpa_network_lines("Car", hex_key)[1], " psk=" + hex_key)

    def test_control_characters_are_refused(self):
        with self.assertRaises(ValueError):
            wpa_network_lines("Car", 'abcdefgh"\n key_mgmt=NONE')
        with self.assertRaises(ValueError):
            wpa_network_lines("Car\n", "abcdefgh")


if __name__ == '__main__':
    unittest.main()
