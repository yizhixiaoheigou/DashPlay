"""Offline checks use a real local HTTP server, never the car or DuckDNS."""
import http.client
import ipaddress
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import setup_wizard as wizard
from settings_page import SettingsPage
from platform_debian import DebianPlatform
import supervisor


class TeslaOfflineTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / 'setup.json').write_text('{"done":true,"mode":"box_hotspot"}')
        (self.state / 'options.json').write_text('{"tesla":true}')

    def test_defaults_and_completed_setup_never_reopens(self):
        for mode, iphone in [('car_hotspot', 'wired'), ('mifi', 'any'), ('box_hotspot', 'any')]:
            (self.state / 'setup.json').write_text(json.dumps({'done': True, 'mode': mode}))
            loaded = wizard.load(self.state)
            self.assertTrue(loaded['done'])
            self.assertEqual(loaded['iphone'], iphone)
        self.assertEqual(wizard.options(self.state)['car_internet'], 'none')
        self.assertTrue(wizard.wireless({'mode': 'box_hotspot', 'iphone': 'wireless'},
                                      {'car_internet': 'phone_hotspot', 'uplink_phone': 'same'}))
        self.assertTrue(wizard.wireless({'mode': 'box_hotspot', 'iphone': 'any'}, {}))

    def test_dns_answer_scope(self):
        names = supervisor.hotspot_names(self.state, 'example.duckdns.org', 'box_hotspot')
        self.assertEqual(set(names), set(wizard.TESLA_CHECK_NAMES) | {'example.duckdns.org'})
        self.assertEqual(supervisor.hotspot_names(self.state, 'example.duckdns.org', 'mifi'), [])
        platform = DebianPlatform('wlan-test', self.state)
        with patch.object(platform, 'start') as start:
            platform.start_dhcp(ipaddress.ip_interface('100.96.0.1/24'), False, names, router=True)
            argv = start.call_args.args[1]
        # Two N1 users' Teslas (2026-10-16) left the hotspot ~30 s after every join without a default route,
        # having asked for one in DHCP; with the box as router they stayed.
        for flag in ('--dhcp-option=3,100.96.0.1', '--dhcp-option=6,100.96.0.1', '--no-hosts', '--no-resolv'):
            self.assertIn(flag, argv)
        self.assertNotIn('--dhcp-option=3', argv)
        self.assertNotIn('--server', ' '.join(argv))

    def test_router_only_for_offline_tesla(self):
        self.assertTrue(supervisor.hotspot_router(self.state, 'box_hotspot'))
        self.assertFalse(supervisor.hotspot_router(self.state, 'mifi'))
        # Other cars keep a hotspot without a router even with the own name answered (local_dns): a head unit could
        # otherwise make the box its default network and lose its own LTE.
        (self.state / 'options.json').write_text(json.dumps({'tesla': False, 'local_dns': True}))
        self.assertFalse(supervisor.hotspot_router(self.state, 'box_hotspot'))

    def test_http_exact_reply_precedes_access_and_captive_checks(self):
        status = {'mode': 'ap', 'home': 'http://100.96.0.1/'}
        page = SettingsPage(self.state, lambda: status, lambda _: None, lambda: [])
        page.start(0); self.addCleanup(page.stop)
        port = page.server.server_address[1]
        def get(host, path='/online/status.html'):
            conn = http.client.HTTPConnection('127.0.0.1', port)
            conn.request('GET', path, headers={'Host': host})
            response = conn.getresponse(); data = response.read(); headers = dict(response.getheaders()); conn.close()
            return response.status, headers, data
        with patch.object(page, 'portal_reply', side_effect=AssertionError('must precede captive')):
            code, headers, body = get('CONNMAN.VN.CLOUD.TESLA.CN:80')
        self.assertEqual(code, 200)
        self.assertEqual(headers['X-ConnMan-Status'], 'online')
        self.assertEqual(headers['Content-Type'], 'text/html')
        self.assertEqual(body, b'<html>\n<head>\n</head>\n<body>\n</body>\n</html>\n\n')
        self.assertEqual(headers['X-Cache'], 'Hit from cloudfront')
        # The overseas connman name gets the same reply (a user's fix, 2026-10-16); the real server's own.
        code, headers, body = get('connman.vn.tesla.services')
        self.assertEqual((code, headers['X-ConnMan-Status'], body), (200, 'online', wizard.CONNMAN_BODY))
        for name in wizard.TESLA_CHECK_NAMES:
            self.assertEqual(get(name)[0], 200)
        # Chromium/Android: an empty 204 for its check paths, not a redirect or a page.
        code, _, body = get('connectivitycheck.gstatic.com', '/generate_204')
        self.assertEqual((code, body), (204, b''))
        self.assertEqual(get('connectivitycheck.gstatic.com', '/gen_204?x=1')[0], 204)
        self.assertEqual(get('other.example')[0], 404)
        status['uplink'] = {'phase':'online'}
        self.assertEqual(get(wizard.TESLA_CHECK_NAMES[0])[0], 404, 'real upstream must never receive a fake HTTP check')
        # Persistent mode-three LAN uses the same baseline as F50; upstream state is independent.
        status.update(tesla_lan_stable=True, tesla_offline=True)
        for phase in ('online', 'wired', 'recovering', 'offline'):
            status['uplink'] = {'phase': phase}
            code, headers, _ = get(wizard.TESLA_CHECK_NAMES[0])
            self.assertEqual((code, headers['X-ConnMan-Status']), (200, 'online'))
            self.assertEqual(get('other.example')[0], 404)
        status.pop('tesla_lan_stable')
        status.pop('tesla_offline')
        status.pop('uplink')
        (self.state / 'options.json').write_text('{"tesla":false}')
        self.assertEqual(get(wizard.TESLA_CHECK_NAMES[0])[0], 404)


class CheckLogTests(unittest.TestCase):
    """Which checks the car makes and what it got go to state/diag/, a line per check per minute."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.page = SettingsPage(self.state, lambda: {}, lambda _: None, lambda: [])

    def records(self):
        path = self.state / 'diag' / 'diag.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_one_line_per_check_and_reply_with_repeats(self):
        with patch('settings_page.time.monotonic', side_effect=[100.0, 110.0, 120.0, 130.0, 170.0]):
            self.page.note_check('CONNMAN.VN.CLOUD.TESLA.CN:80', '/online/status.html?a=1', 200)
            self.page.note_check('connman.vn.cloud.tesla.cn', '/online/status.html', 200)
            self.page.note_check('connman.vn.cloud.tesla.cn', '/online/status.html', 200)
            self.page.note_check('connman.vn.cloud.tesla.cn', '/online/status.html', 'off')
            self.page.note_check('connman.vn.cloud.tesla.cn', '/online/status.html', 200)
        lines = [{k: r[k] for k in ('host', 'path', 'reply', 'repeats') if k in r} for r in self.records()]
        self.assertEqual(lines, [
            {'host': 'connman.vn.cloud.tesla.cn', 'path': '/online/status.html', 'reply': 200},
            {'host': 'connman.vn.cloud.tesla.cn', 'path': '/online/status.html', 'reply': 'off'},
            {'host': 'connman.vn.cloud.tesla.cn', 'path': '/online/status.html', 'reply': 200, 'repeats': 2}])
        self.assertTrue(all(r['event'] == 'tesla_check' for r in self.records()))

    def test_other_hosts_are_not_logged(self):
        self.page.note_check('dashplay.local', '/?view=car', 200)
        self.page.note_check('100.96.0.1', '/', 200)
        self.assertEqual(self.records(), [])
