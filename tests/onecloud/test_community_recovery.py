"""Regressions for interrupted network cleanup, Wi-Fi queries and USB worker failures."""
import ast
from contextlib import ExitStack
import errno
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/onecloud"))
import platform_debian as deb
import platform_openwrt as ow
import supervisor
import supervisor_openwrt as sow
from test_iphone_usb_bridge import bridge, PATH as BRIDGE_PATH


class SharingRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.p = deb.DebianPlatform('audit0', run=self.root, sta_network=self.root / 'sta.network')

    def test_bad_journals_do_not_abort_startup_or_run_cleanup_commands(self):
        for name, payload in [('phone-uplink.json', '{broken'), ('phone-uplink.json', '{}'),
                              ('phone-uplink.json', '[]'), ('usb-tether.json', '{}'),
                              ('usb-tether.json', '[]'), ('usb-tether.json', '{broken')]:
            with self.subTest(name=name, payload=payload):
                path = self.root / name; path.write_text(payload)
                with patch.object(deb.subprocess, 'run') as run:
                    self.assertFalse(supervisor.recover_network_sharing(self.p, say=Mock()))
                run.assert_not_called()
                self.assertEqual(path.read_text(), payload)
                path.unlink()

    def test_unfinished_cleanup_is_retained_and_other_cleanup_is_attempted(self):
        with patch.object(self.p, 'cleanup_phone_uplink', side_effect=subprocess.TimeoutExpired('iptables', 5)), \
             patch.object(self.p, 'stop_usb_sharing') as usb:
            self.assertFalse(supervisor.recover_network_sharing(self.p, say=Mock()))
            usb.assert_called_once()

    def test_bad_usb_record_cannot_be_overwritten_by_new_sharing(self):
        path = self.root / 'usb-tether.json'; path.write_text('{broken')
        with patch.object(deb.subprocess, 'run') as run, self.assertRaises(ValueError):
            self.p.begin_usb_tether(ipaddress.ip_network('192.0.2.0/24'))
        self.assertEqual(path.read_text(), '{broken'); run.assert_not_called()

    def test_usb_rule_failure_keeps_record_and_restores_adapter_interface(self):
        path = self.root / 'usb-tether.json'
        saved = dict(network='192.0.2.0/24', forward='0', interface='ap0')
        path.write_text(json.dumps(saved))
        def command(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2 if '-D' in argv else 0)
        with patch.object(deb.subprocess, 'run', side_effect=command), self.assertRaises(subprocess.CalledProcessError):
            self.p.stop_usb_sharing()
        self.assertEqual(json.loads(path.read_text()), saved)
        self.assertEqual(self.p.ap_interface, 'audit0')

    def test_startup_with_broken_record_keeps_settings_and_ap_without_retrying_sharing(self):
        # Run the real supervisor entry point with an inert platform and no background threads.
        root = self.root / 'program'; state = root / 'state'; state.mkdir(parents=True)
        files = ['run-receiver.py', 'phone-presence.py', 'pair-phone.py', 'pairing_window.py', 'setup_wizard.py',
                 'settings_ui.py', 'mdns_name.py', 'debug_uplink.py', 'status_light.py', 'platform_api.py',
                 'platform_debian.py', 'connectivity.py', 'diagnostics.py', 'iphone-usb-bridge.py', 'linux-bt-bridge/bt-bridge']
        for relative in files:
            path = root / relative; path.parent.mkdir(exist_ok=True); path.touch()
        (root / 'service.json').write_text(json.dumps(dict(interface='audit0', channel=52, width=80, address='192.0.2.1/24')))
        (state / 'hotspot.json').write_text(json.dumps(dict(ssid='audit', passphrase='password')))
        (state / 'car-hotspot.json').write_text(json.dumps(dict(ssid='upstream', passphrase='password')))
        (state / 'options.json').write_text(json.dumps(dict(car_internet='usb_tether')))
        journal = self.root / 'phone-uplink.json'; journal.write_text('{broken')
        for mode in ('box_hotspot', 'car_hotspot'):
            with self.subTest(mode=mode), ExitStack() as stack:
                platform = Mock(children=[], ap_interface='audit0', local_addresses=Mock(return_value=[]),
                                five_ghz=Mock(return_value=True), wired_capable=Mock(return_value=True),
                                cleanup_phone_uplink=self.p.cleanup_phone_uplink, stop_usb_sharing=self.p.stop_usb_sharing)
                mocks = {}
                for name in ('signal.signal', 'os.umask', 'threading.Thread', 'threading.Event',
                             'SettingsPage', 'NameResponder', 'diagnostics', 'ensure_leaf',
                             'own_domain.unguard', 'setup_wizard.Scanner', 'PhoneUplink', 'LegacyPhoneUplink', 'JoinedPhoneAP'):
                    mocks[name] = stack.enter_context(patch('supervisor.' + name))
                mocks['threading.Event'].return_value.is_set.return_value = False
                mocks['threading.Event'].return_value.wait.return_value = True
                for name, value in [('get_platform', platform), ('direct_capable', (False, 'test')),
                                    ('own_domain.load', None), ('own_domain.name_of', None),
                                    ('debug_uplink.active', None), ('setup_wizard.wireless', False),
                                    ('setup_wizard.load', dict(mode=mode, done=True, iphone='wired'))]:
                    stack.enter_context(patch('supervisor.' + name, return_value=value))
                stack.enter_context(patch.object(supervisor, 'RUN', self.root))
                stack.enter_context(patch.object(supervisor, 'WIRED_STATUS', self.root / 'wired-status.json'))
                stack.enter_context(patch.object(supervisor.sys, 'argv', ['supervisor', '--root', str(root)]))
                stack.enter_context(patch('builtins.print'))
                supervisor.main()
                mocks['SettingsPage'].return_value.start.assert_called_once()
                platform.start_ap.assert_called_once()
                platform.begin_usb_tether.assert_not_called()
                platform.disable_usb_tether.assert_not_called()
                platform.join_wifi.assert_not_called()
                for name in ('PhoneUplink', 'LegacyPhoneUplink', 'JoinedPhoneAP'):
                    mocks[name].assert_not_called()
                status = mocks['SettingsPage'].call_args.args[1]()
                self.assertIn('暂停共享上网', status['warning'])
                self.assertEqual(status['mode'], 'ap')
                self.assertEqual(journal.read_text(), '{broken')
                self.assertEqual(platform.configure_hotspot.call_args.args[:2], (6, 20))

    def test_recovery_warning_is_visible_on_debian_settings_page(self):
        import settings_page
        state = self.root / 'state'; state.mkdir()
        page = settings_page.SettingsPage(state, lambda: dict(mode='ap', warning='Network recovery test'),
                                          lambda _: None, lambda: [])
        with patch.object(page, 'ddns_block', return_value=''):
            self.assertIn('Network recovery test', page.render_full(''))

    def test_usb_partial_cleanup_can_retry_missing_rules_and_preserves_record_on_sysctl_failure(self):
        path = self.root / 'usb-tether.json'
        path.write_text(json.dumps(dict(network='192.0.2.0/24', forward='0', interface='ap0')))
        def command(argv, **kwargs):
            code = 1 if '-C' in argv else 2 if argv[0] == 'sysctl' else 0
            return subprocess.CompletedProcess(argv, code)
        with patch.object(deb.subprocess, 'run', side_effect=command), self.assertRaises(subprocess.CalledProcessError):
            self.p.stop_usb_sharing()
        self.assertTrue(path.exists())
        with patch.object(deb.subprocess, 'run', side_effect=lambda argv, **kw: subprocess.CompletedProcess(argv, 1 if '-C' in argv else 0)):
            self.p.stop_usb_sharing()
        self.assertFalse(path.exists())

    def test_invalid_borrowed_interface_record_is_checked_before_mutations(self):
        path = self.root / 'phone-uplink.json'
        path.write_text(json.dumps(dict(interface='audit0', mac='00:00:5e:00:53:01', forward='0',
                                       ap_vif='p2p0', borrowed_ap={'type': 'managed'})))
        with patch.object(deb.subprocess, 'run') as run, patch.object(self.p, 'remove_sta_network') as remove:
            self.assertFalse(supervisor.recover_network_sharing(self.p, say=Mock()))
        run.assert_not_called(); remove.assert_not_called(); self.assertTrue(path.exists())

    def test_no_records_is_success(self):
        self.assertTrue(supervisor.recover_network_sharing(self.p, say=Mock()))


class HandoffRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.network = dict(network='lan', ifname='audit0', ssid='audit', channel=149, encryption='psk2')
        self.p = ow.OpenWrtPlatform()
        for target, value in [('_ubus', {}), ('wifi_networks', []), ('pick_network', (self.network, '')),
                              ('interface_address', ('br-lan', ipaddress.ip_interface('192.0.2.1/24')))]:
            p = patch.object(ow, target, return_value=value); p.start(); self.addCleanup(p.stop)

    def test_waiting_retries_and_live_query_failure_preserves_last_network(self):
        previous = dict(self.network, channel=36)
        for failure in (subprocess.TimeoutExpired('iw', 5), FileNotFoundError('iw')):
            with self.subTest(failure=type(failure).__name__), patch.object(ow.subprocess, 'run', side_effect=failure):
                network, why = sow.poll_handoff(self.p, 'auto', {})
                self.assertIsNone(network); self.assertTrue(why)
                network, why = sow.poll_handoff(self.p, 'auto', {}, previous=previous)
                self.assertIs(network, previous); self.assertTrue(why)

    def test_failed_command_does_not_use_configured_channel_as_live_fact(self):
        with patch.object(ow.subprocess, 'run', return_value=subprocess.CompletedProcess(['iw'], 1, stdout='')):
            network, why = sow.poll_handoff(self.p, 'auto', {})
        self.assertIsNone(network); self.assertTrue(why)

    def test_success_after_timeout_reports_real_changed_channel(self):
        previous = dict(self.network, channel=149)
        with patch.object(ow.subprocess, 'run', side_effect=[subprocess.TimeoutExpired('iw', 5),
                         subprocess.CompletedProcess(['iw'], 0, stdout='channel 36 (5180 MHz)')]):
            first, _ = sow.poll_handoff(self.p, 'auto', {}, previous)
            second, why = sow.poll_handoff(self.p, 'auto', {}, previous)
        self.assertIs(first, previous); self.assertEqual(second['channel'], 36); self.assertEqual(why, '')

    def test_confirmed_missing_network_still_propagates(self):
        with patch.object(ow, 'pick_network', return_value=(None, 'not up')):
            result = sow.poll_handoff(self.p, 'auto', {}, previous=self.network)
        self.assertEqual(result, (None, 'not up'))


class HotspotChannelTests(unittest.TestCase):
    def test_unsupported_configured_channels_fall_back_before_hostapd(self):
        for channel in (0, 52, 64, 100, 144, 166, 999):
            with self.subTest(channel=channel):
                selected, width = supervisor.radio_hotspot(channel, 80, True)
                self.assertEqual((selected, width), (6, 20))
                config = deb.hostapd_config('audit0', selected, width, 'audit', 'password')
                self.assertIn('channel=6\n', config)
                with self.assertRaises(ValueError):
                    deb.hostapd_config('audit0', channel, 80, 'audit', 'password')

    def test_supported_channels_and_no_5ghz_radio_keep_existing_policy(self):
        self.assertEqual(supervisor.radio_hotspot(149, 80, True), (149, 80))
        self.assertEqual(supervisor.radio_hotspot(36, 40, True), (36, 40))
        self.assertEqual(supervisor.radio_hotspot(149, 80, False, 11), (11, 20))
        self.assertEqual(supervisor.radio_hotspot(6, 20, True), (6, 20))


class TapRecoveryTests(unittest.TestCase):
    def workers(self):
        # Execute production nested bodies with fake endpoints, never open a device.
        tree = ast.parse(BRIDGE_PATH.read_text())
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in ('ncm_to_tap', 'tap_to_ncm')]
        context = dict(vars(bridge), functions=[(0, 1, 2, 3, 4)], behind={}, tap=0, fd=0,
                       bulk=Mock(return_value=b'block'), ntb_datagrams=lambda _: [b'frame'], sequences=[0])
        exec(compile(ast.Module(body=funcs, type_ignores=[]), str(BRIDGE_PATH), 'exec'), context)
        return context

    def test_tap_io_errors_and_eof_end_whole_helper(self):
        for worker, operation, value in [('ncm_to_tap', 'write', OSError(errno.EIO, 'fault')),
                                         ('ncm_to_tap', 'write', 2),
                                         ('tap_to_ncm', 'read', OSError(errno.EIO, 'fault')),
                                         ('tap_to_ncm', 'read', b'')]:
            with self.subTest(worker=worker, value=value), patch.object(bridge, 'log'), \
                 patch.object(bridge.os, '_exit', side_effect=SystemExit) as stop, \
                 patch.object(bridge.os, operation, **({'side_effect': value} if isinstance(value, Exception) else {'side_effect': [value, AssertionError('worker continued after a terminal TAP result')]})):
                context = self.workers()
                with self.assertRaises(SystemExit):
                    context[worker](*((0, 1) if worker == 'ncm_to_tap' else ()))
                stop.assert_called_once()

    def test_log_failure_cannot_prevent_helper_exit(self):
        with patch.object(bridge, 'log', side_effect=OSError('full')), \
             patch.object(bridge.os, '_exit', side_effect=SystemExit) as stop, \
             patch.object(bridge.os, 'read', return_value=b''):
            with self.assertRaises(SystemExit): self.workers()['tap_to_ncm']()
            stop.assert_called_once()


if __name__ == '__main__': unittest.main()
