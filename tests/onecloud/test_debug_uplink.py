from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/onecloud"))
import debug_uplink
import setup_wizard
from settings_page import SettingsPage

PHONE = '{"ssid": "My iPhone", "passphrase": "sample-pass1"}'


class WindowTests(unittest.TestCase):
    # Remote debugging from the car (2026-10-15): the box joins the owner's phone hotspot for a window that a
    # reboot always ends, so pulling the power brings back the car's network.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'state'; self.state.mkdir()
        self.run = Path(self.tmp.name) / 'run'; self.run.mkdir()

    def test_window_opens_ends_by_itself_and_needs_a_saved_hotspot(self):
        self.assertIsNone(debug_uplink.active(self.state, self.run, now=100))
        debug_uplink.start(self.run, minutes=30, now=100)
        self.assertEqual(debug_uplink.remaining(self.run, now=100), 1800)
        self.assertIsNone(debug_uplink.active(self.state, self.run, now=200), 'no phone hotspot saved')
        (self.state / 'debug-hotspot.json').write_text(PHONE)
        self.assertEqual(debug_uplink.active(self.state, self.run, now=200), {'ssid': 'My iPhone', 'passphrase': 'sample-pass1'})
        self.assertEqual(debug_uplink.remaining(self.run, now=1900), 0)
        self.assertFalse((self.run / 'debug-uplink.json').exists(), 'an ended window is removed')
        self.assertIsNone(debug_uplink.active(self.state, self.run, now=1900))

    def test_stop_and_unreadable_files(self):
        debug_uplink.start(self.run, now=0)
        debug_uplink.stop(self.run)
        self.assertEqual(debug_uplink.remaining(self.run, now=0), 0)
        (self.run / 'debug-uplink.json').write_text('{"until": "soon"}')
        self.assertEqual(debug_uplink.remaining(self.run, now=0), 0)
        for bad in ('{"ssid": "x", "passphrase": "short"}', '{"ssid": "", "passphrase": "12345678"}', '[]', 'not json'):
            (self.state / 'debug-hotspot.json').write_text(bad)
            self.assertIsNone(debug_uplink.hotspot(self.state), bad)

    def test_tailscale_is_found_in_the_system_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(debug_uplink.reachable(directory))
            tool = Path(directory) / 'tailscaled'
            tool.write_text('#!/bin/sh\n'); tool.chmod(0o755)
            self.assertTrue(debug_uplink.reachable(directory))


class ReceiverInputTests(unittest.TestCase):
    def test_debugging_from_box_hotspot_mode_reads_a_file_that_exists(self):
        # 2026-10-15 on the N1: box-hotspot mode has no car-hotspot.json, the receiver started for the phone hotspot
        # read it, exited at once, and the service restarted every 25 s (the phone saw it join and leave).
        from supervisor import handed_hotspot
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'hotspot.json').write_text('{"ssid": "DashPlay", "passphrase": "installed1"}')
            self.assertTrue(handed_hotspot(state, debug=True).is_file())
            self.assertEqual(handed_hotspot(state, debug=False), state / 'car-hotspot.json')


class PageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / 'state'; self.state.mkdir()
        self.run = root / 'run'; self.run.mkdir()
        (self.state / 'hotspot.json').write_text('{"ssid": "DashPlay", "passphrase": "installed1"}')
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot'})
        self.status = {'mode': 'ap', 'home': 'http://100.96.0.1/', 'box_ssid': 'DashPlay', 'entries': []}
        self.changes = []
        self.page = SettingsPage(self.state, lambda: self.status, self.changes.append, lambda: [], None,
                                 root / 'wired-status.json', run=self.run)
        self.page.apply_later = self.changes.append
        self.nonce = self.page.pairing.nonce
        reachable = patch.object(debug_uplink, 'reachable', return_value=True)
        self.reachable = reachable.start(); self.addCleanup(reachable.stop)

    def debug(self, **form):
        return self.page.apply('/debug', dict(form, nonce=self.nonce))

    def test_save_start_and_end_from_the_page(self):
        html = self.page.render('')
        self.assertIn('远程调试', html)
        self.assertNotIn('连手机热点调试', html, 'nothing to start before a phone hotspot is saved')
        with self.assertRaises(ValueError):
            self.debug(action='start')
        with self.assertRaises(ValueError):
            self.debug(action='save', ssid_manual='My iPhone', password='short')
        self.assertIn('已保存', self.debug(action='save', ssid_manual='My iPhone', password='sample-pass1'))
        self.debug(action='save', ssid_manual='My iPhone', password='')   # empty keeps the saved password
        self.assertEqual(debug_uplink.hotspot(self.state)['passphrase'], 'sample-pass1')
        html = self.page.render('')
        self.assertIn('连手机热点调试 30 分钟', html)
        self.assertNotIn('sample-pass1', html, 'the password is never shown')
        self.assertEqual(self.changes, [], 'saving changes no network')
        kind, page = self.debug(action='start')
        self.assertEqual(kind, 'page')
        self.assertIn('My iPhone', page)
        self.assertIn('给盒子拔电再插上', page)
        self.assertEqual(self.changes, ['remote debugging started'])
        self.assertGreater(debug_uplink.remaining(self.run), 29 * 60)
        html = self.page.render('')
        self.assertIn('远程调试中', html)
        self.assertIn('给盒子拔电再插上', html)
        self.assertIn('结束调试，回到原来的网络', html)
        self.debug(action='stop')
        self.assertEqual(debug_uplink.remaining(self.run), 0)
        self.assertEqual(self.changes, ['remote debugging started', 'remote debugging stopped'])
        self.debug(action='forget')
        self.assertIsNone(debug_uplink.hotspot(self.state))

    def test_not_offered_without_tailscale_but_an_open_window_can_still_end(self):
        # The public images have no Tailscale: the window would only take the box's hotspot away for 30 minutes.
        self.reachable.return_value = False
        self.assertNotIn('远程调试', self.page.render(''))
        (self.state / 'debug-hotspot.json').write_text(PHONE)
        with self.assertRaises(ValueError):
            self.debug(action='start')
        self.assertEqual(self.changes, [])
        debug_uplink.start(self.run)   # opened before Tailscale went away
        self.assertIn('结束调试，回到原来的网络', self.page.render(''))
        self.debug(action='stop')
        self.assertEqual(debug_uplink.remaining(self.run), 0)

    def test_not_offered_without_the_runtime_directory_or_when_managed(self):
        # OpenWrt's LuCI manages the router's network; its page has no runtime directory for the window.
        for page in (SettingsPage(self.state, lambda: self.status, self.changes.append, lambda: []),
                     SettingsPage(self.state, lambda: self.status, self.changes.append, lambda: [],
                                  managed={'by': 'OpenWrt', 'admin': '/cgi-bin/luci'}, run=self.run)):
            self.assertNotIn('远程调试', page.render(''))
            with self.assertRaises(ValueError):
                page.apply('/debug', {'action': 'start', 'nonce': page.pairing.nonce})


if __name__ == '__main__':
    unittest.main()
