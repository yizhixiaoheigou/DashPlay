"""USB fallback settings through the real form/wizard and uplink controller, with synthetic radios."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import settings_page
import setup_wizard
import test_setup_flow as flow_fixture
import test_phone_uplink as uplink_fixture


class UsbHotspotSettingsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = flow_fixture.SetupFlowTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state, self.page = self.fixture.state, self.fixture.page
        self.file = self.state / 'uplink-hotspot.json'
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'iphone': 'any'})

    def submit(self, **fields):
        return self.page.apply('/internet', dict(car_internet='usb_tether', nonce=self.page.pairing.nonce, **fields))

    def test_usb_saves_credentials_and_fresh_page_reads_them_without_password(self):
        reply = self.submit(ssid='Fallback', password='synthetic-password')
        self.assertEqual(json.loads(self.file.read_text()), {'ssid': 'Fallback', 'passphrase': 'synthetic-password'})
        self.assertIn('备用', reply)
        page = settings_page.SettingsPage(self.state, lambda: {}, lambda _: None, lambda: [])
        html = page.internet_block(page.pairing.nonce)
        self.assertIn('name=ssid value="Fallback"', html)
        self.assertNotIn('synthetic-password', html)
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)

    def test_legacy_mode_only_post_preserves_hotspot(self):
        setup_wizard.save_json(self.file, {'ssid': 'Old', 'passphrase': 'old-password'})
        self.submit()
        self.assertEqual(json.loads(self.file.read_text())['ssid'], 'Old')

    def test_encrypted_hotspot_keeps_same_name_password_but_new_name_requires_one(self):
        setup_wizard.save_json(self.file, {'ssid': 'Old', 'passphrase': 'old-password'})
        self.submit(ssid='Old', password='')
        before = self.file.read_bytes()
        with self.assertRaises(ValueError): self.submit(ssid='New', password='')
        self.assertEqual(self.file.read_bytes(), before)

    def test_open_network_saves_reloads_and_never_reuses_old_password(self):
        import debug_uplink
        setup_wizard.save_json(self.file, {'ssid': 'Old', 'passphrase': 'old-password'})
        for name in ('Old', 'New'):
            self.submit(ssid=name, password='', security='open')
            expected = {'ssid': name, 'passphrase': '', 'security': 'open'}
            self.assertEqual(json.loads(self.file.read_text()), expected)
            self.assertEqual(debug_uplink.hotspot(self.state, filename=self.file.name), expected)
            html = self.page.internet_block(self.page.pairing.nonce)
            self.assertIn('value=open selected', html)
            self.assertNotIn('old-password', html)
        f, ctl = self.controller(); self.assertTrue(ctl.start())
        f.p.phone_hotspot_frequency.return_value = 5745
        f.now = ctl.next_try; ctl.tick()
        self.assertEqual(f.p.start_phone_station.call_args.args[0], expected)
        with self.assertRaises(ValueError): self.submit(ssid='New', password='', security='wpa')

    def test_wifi_primary_and_usb_wizard_accept_explicit_open_network(self):
        self.page.apply('/internet', dict(car_internet='phone_hotspot', ssid='Public', password='',
                                         security='open', nonce=self.page.pairing.nonce))
        self.assertEqual(json.loads(self.file.read_text())['security'], 'open')
        self.wizard_to_uplink()
        self.assertIn('value=open selected', self.page.render(''))
        self.fixture.answer('uplink', ssid='Public', password='', security='open')
        self.assertIn('开放网络', self.finish_wizard())
        self.fixture.answer('summary', value='confirm')
        self.assertEqual(json.loads(self.file.read_text())['passphrase'], '')

    def test_invalid_security_and_corrupt_open_config_are_not_accepted(self):
        import debug_uplink
        with self.assertRaises(ValueError): self.submit(ssid='Public', password='', security='invalid')
        for value in ({'ssid':'Public','passphrase':''},
                      {'ssid':'Public','passphrase':'old-password','security':'open'}):
            setup_wizard.save_json(self.file,value)
            self.assertIsNone(debug_uplink.hotspot(self.state,filename=self.file.name))

    def test_empty_usb_fallback_is_optional_and_can_be_cleared(self):
        self.submit(ssid='', password='')
        self.assertFalse(json.loads(self.file.read_text()) if self.file.exists() else None)
        setup_wizard.save_json(self.file, {'ssid': 'Old', 'passphrase': 'old-password'})
        self.submit(ssid='', password='')
        self.assertFalse(json.loads(self.file.read_text()) if self.file.exists() else None)
        with self.assertRaises(ValueError): self.submit(ssid='', password='synthetic-password')

    def test_invalid_credentials_do_not_save_mode_or_schedule_restart(self):
        before = (self.state / 'options.json').read_bytes() if (self.state / 'options.json').exists() else None
        for ssid, password in [('New', 'short'), ('x\ny', 'valid-password')]:
            with self.subTest(ssid=ssid), self.assertRaises(ValueError): self.submit(ssid=ssid, password=password)
        after = (self.state / 'options.json').read_bytes() if (self.state / 'options.json').exists() else None
        self.assertEqual(before, after)
        self.assertEqual(self.fixture.changes, [])

    def test_disk_failure_is_visible_and_does_not_restart_or_report_success(self):
        with patch.object(setup_wizard, 'save_json', side_effect=OSError('synthetic disk error')):
            with self.assertRaisesRegex(ValueError, '保存失败'):
                self.submit(ssid='Fallback', password='synthetic-password')
        self.assertEqual(self.fixture.changes, [])

    def test_later_write_failure_restores_old_hotspot_and_options(self):
        setup_wizard.save_json(self.file, {'ssid': 'Old', 'passphrase': 'old-password'})
        options = self.state / 'options.json'
        setup_wizard.save_json(options, {'car_internet': 'none'})
        originals = {p: json.loads(p.read_text()) for p in (self.file, options)}
        save = setup_wizard.save_json
        def fail_options(path, value):
            if path == options and value.get('car_internet') == 'usb_tether': raise OSError('synthetic disk full')
            save(path, value)
        with patch.object(setup_wizard, 'save_json', side_effect=fail_options):
            with self.assertRaisesRegex(ValueError, '保存失败'):
                self.submit(ssid='New', password='new-password')
        self.assertEqual({p: json.loads(p.read_text()) for p in originals}, originals)
        self.assertEqual(self.fixture.changes, [])

    def test_failed_readback_cannot_report_saved(self):
        with patch.object(setup_wizard, 'save_json', return_value=None):
            with self.assertRaisesRegex(ValueError, '保存失败'):
                self.submit(ssid='Fallback', password='synthetic-password')
        self.assertEqual(self.fixture.changes, [])

    def test_http_post_redirects_to_saved_fields_or_visible_failure(self):
        import urllib.request, urllib.parse
        self.page.start(port=0)
        self.addCleanup(self.page.server.server_close)
        self.addCleanup(self.page.server.shutdown)
        url = 'http://127.0.0.1:%s/internet' % self.page.server.server_port
        form = dict(car_internet='usb_tether', ssid='Fallback', password='synthetic-password', nonce=self.page.pairing.nonce)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, urllib.parse.urlencode(form).encode()) as response: html = response.read().decode()
        self.assertIn('USB 上网方式和备用热点已保存', html)
        self.assertIn('name=ssid value="Fallback"', html)
        self.assertNotIn('synthetic-password', html)
        self.fixture.changes.clear()
        with patch.object(setup_wizard, 'save_json_many', side_effect=OSError('synthetic failure')):
            with opener.open(url, urllib.parse.urlencode(form).encode()) as response: html = response.read().decode()
        self.assertIn('保存失败', html)
        self.assertNotIn('USB 上网方式和备用热点已保存', html)
        self.assertEqual(self.fixture.changes, [])

    def test_no_fallback_banner_does_not_reuse_stale_not_found_reason(self):
        self.submit(ssid='', password='')
        self.fixture.status.update(tesla_lan_stable=True, uplink={'phase': 'offline', 'reason': '没找到手机热点；局域网保持可用'})
        html = self.page.internet_block(self.page.pairing.nonce)
        self.assertIn('未设置备用', html)
        self.assertNotIn('没找到手机热点', html)

    def wizard_to_uplink(self):
        setup_wizard.save(self.state, {'done': False, 'wizard_version': 2})
        self.fixture.answer('vehicle', value='tesla')
        self.fixture.answer('mode', value='no')
        self.fixture.answer('mifi', value='box_hotspot')
        self.fixture.answer('network', ssid='Box', password='synthetic-box')
        self.fixture.answer('internet', value='usb_tether')
        self.assertEqual(self.fixture.flow.current(self.fixture.flow.load()), 'uplink')
        html = self.page.render('')
        self.assertIn('备用', html)
        self.assertIn('跳过，只用 USB', html)

    def finish_wizard(self):
        self.fixture.answer('sound', value='no')
        self.fixture.answer('iphone', value='any')
        return self.page.render('')

    def test_usb_wizard_can_save_fallback_and_summary_names_it(self):
        self.wizard_to_uplink()
        self.fixture.answer('uplink', ssid='Fallback', password='synthetic-password')
        self.assertIn('Fallback', self.finish_wizard())
        self.fixture.answer('summary', value='confirm')
        self.assertEqual(json.loads(self.file.read_text())['ssid'], 'Fallback')
        self.assertEqual(setup_wizard.options(self.state)['car_internet'], 'usb_tether')

    def test_enter_in_optional_fields_cannot_activate_skip(self):
        from html.parser import HTMLParser
        class Forms(HTMLParser):
            def __init__(self): super().__init__(); self.forms = []; self.form = None
            def handle_starttag(self, tag, attrs):
                if tag == 'form': self.form = []; self.forms.append(self.form)
                elif self.form is not None and tag in ('input', 'button'): self.form.append((tag, dict(attrs)))
            def handle_endtag(self, tag):
                if tag == 'form': self.form = None
        self.wizard_to_uplink()
        parser = Forms(); parser.feed(self.page.render(''))
        credentials = next(f for f in parser.forms if any(a.get('name') == 'ssid' for _, a in f))
        self.assertFalse(any(a.get('value') == 'skip' for _, a in credentials))
        self.assertFalse(any('required' in a for _, a in credentials if a.get('name') in ('ssid', 'password')))
        self.assertTrue(any(any(a.get('value') == 'skip' for _, a in f) for f in parser.forms))

    def test_usb_wizard_can_skip_and_summary_says_no_fallback(self):
        setup_wizard.save_json(self.file, {'ssid': 'Old', 'passphrase': 'old-password'})
        self.wizard_to_uplink()
        self.fixture.answer('uplink', value='skip')
        self.assertIn('未设置备用', self.finish_wizard())
        self.fixture.answer('summary', value='confirm')
        self.assertFalse(json.loads(self.file.read_text()) if self.file.exists() else None)

    def controller(self):
        fixture = uplink_fixture.UsbShareUplinkTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.state = self.state
        return fixture, fixture.uplink(share=True)

    def test_saved_form_reloads_then_switches_usb_to_wifi_and_back(self):
        self.submit(ssid='Fallback', password='synthetic-password')
        f, ctl = self.controller()
        f.p.default_route_devices.return_value = ['usb0']
        self.assertTrue(ctl.start())
        self.assertEqual(ctl.phase, 'wired')
        f.p.default_route_devices.return_value = []
        f.p.wired_carrier.return_value = False
        f.p.phone_hotspot_frequency.return_value = 5745
        f.now = ctl.next_try; ctl.tick()
        self.assertEqual(f.p.start_phone_station.call_args.args[0]['ssid'], 'Fallback')
        f.p.phone_link.return_value = {'connected': True, 'frequency': 5745}
        import ipaddress
        f.p.phone_station_address.return_value = ipaddress.ip_interface('192.0.2.8/24')
        ctl.tick(); self.assertEqual(ctl.phase, 'online')
        f.p.default_route_devices.return_value = ['usb0']
        ctl.tick(); self.assertEqual(ctl.phase, 'wired')

    def test_no_saved_hotspot_does_not_scan_or_claim_not_found(self):
        f, ctl = self.controller()
        self.assertTrue(ctl.start())
        for _ in range(4): f.now = ctl.next_try; ctl.tick()
        f.p.phone_hotspot_frequency.assert_not_called()
        self.assertIn('未设置备用', f.status['uplink']['reason'])
        self.assertNotIn('没找到', self.page.internet_block(self.page.pairing.nonce))

    def test_configured_but_absent_hotspot_is_distinct(self):
        self.submit(ssid='Fallback', password='synthetic-password')
        f, ctl = self.controller(); self.assertTrue(ctl.start())
        f.now = ctl.next_try; ctl.tick()
        self.assertIn('未找到备用热点', f.status['uplink']['reason'])
        f.p.phone_hotspot_frequency.assert_called_once()


if __name__ == '__main__': unittest.main()
