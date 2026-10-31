import json
import socket
import struct
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import setup_wizard
import mdns_name
import settings_page
from pairing_window import save_json, read_json
from settings_page import SettingsPage

SCAN = """BSS 02:00:00:00:00:a1(on wlan0) -- associated
\tfreq: 2462.0
\tsignal: -24.00 dBm
\tSSID: xiaomi
BSS 02:00:00:00:00:a2(on wlan0)
\tfreq: 5180.0
\tsignal: -60.00 dBm
\tSSID: xiaomi
BSS 02:00:00:00:00:a3(on wlan0)
\tfreq: 2422.0
\tsignal: -30.00 dBm
BSS 02:00:00:00:00:b1(on wlan0)
\tfreq: 5745.0
\tsignal: -50.00 dBm
\tSSID: \\xe5\\xb0\\x8f\\xe9\\xb9\\x8f G6
BSS 02:00:00:00:00:b2(on wlan0)
\tfreq: 5745.0
\tsignal: -40.00 dBm
\tSSID: DashPlay
"""


class WizardStateTests(unittest.TestCase):
    def test_scan_keeps_strongest_named_network_and_decodes_utf8(self):
        result = setup_wizard.parse_scan(SCAN, own_ssid='DashPlay')
        self.assertEqual([n['ssid'] for n in result], ['xiaomi', '小鹏 G6'])
        self.assertEqual(result[0], {'ssid': 'xiaomi', 'signal': -24, 'band': '2.4 GHz'})
        self.assertEqual(result[1]['band'], '5 GHz')

    def test_steps_and_open_portal(self):
        self.assertEqual(setup_wizard.step({'done': False}, 'ap'), 'mode')
        self.assertTrue(setup_wizard.open_hotspot({'done': False}))
        self.assertTrue(setup_wizard.captive({'done': False}, 'ap'))
        for mode in ('car_hotspot', 'mifi'):
            chosen = {'done': False, 'mode': mode}
            self.assertEqual(setup_wizard.step(chosen, 'ap'), 'car')
            car = dict(chosen, car='x')
            self.assertTrue(setup_wizard.captive(dict(car, failed='not_found'), 'ap'))
            self.assertEqual(setup_wizard.step(car, 'sta'), 'phone')
            self.assertFalse(setup_wizard.captive(car, 'sta'))
        box = {'done': False, 'mode': 'box_hotspot'}
        self.assertEqual(setup_wizard.step(box, 'ap'), 'box')
        self.assertTrue(setup_wizard.captive(box, 'ap'))
        box['box'] = True
        self.assertEqual(setup_wizard.step(box, 'ap'), 'phone')
        self.assertFalse(setup_wizard.open_hotspot(box))
        done = {'done': True, 'mode': 'car_hotspot'}
        self.assertEqual(setup_wizard.step(done, 'ap'), 'done')
        self.assertFalse(setup_wizard.open_hotspot(done))

    def test_wireless_carplay_except_on_the_car_hotspot(self):
        self.assertFalse(setup_wizard.wireless({'mode': 'car_hotspot'}))
        self.assertTrue(setup_wizard.wireless({'mode': 'mifi'}))
        self.assertTrue(setup_wizard.wireless({'mode': 'box_hotspot'}))
        self.assertTrue(setup_wizard.wireless({}))

    def test_old_setup_files_migrate_to_the_car_hotspot_way(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            cases = [
                ({'done': False, 'car': 'skip'}, {'done': False, 'iphone': 'any'}),
                ({'done': False, 'role': 'car', 'car': 'xiaomi'},
                 {'done': False, 'car': 'xiaomi', 'mode': 'car_hotspot', 'iphone': 'wired'}),
                ({'done': True, 'role': 'portable', 'hotspot': True}, {'done': True, 'iphone': 'any'}),
                ({'done': True, 'mode': 'mifi'}, {'done': True, 'mode': 'mifi', 'iphone': 'any'}),
                ({'done': True, 'mode': 'box_hotspot', 'box': True},
                 {'done': True, 'mode': 'box_hotspot', 'box': True, 'iphone': 'any'}),
                ({'done': False, 'mode': 'other'}, {'done': False, 'iphone': 'any'}),
            ]
            for old, expected in cases:
                setup_wizard.save(state, old)
                self.assertEqual(setup_wizard.load(state), expected)
            (state / 'car-hotspot.json').write_text('{}')
            setup_wizard.save(state, {'done': True, 'role': 'car'})
            self.assertEqual(setup_wizard.load(state), {'done': True, 'mode': 'car_hotspot', 'iphone': 'wired'})

    def test_car_hotspot_security(self):
        dump = ("BSS 02:00:00:00:00:a1(on wlan0) -- associated\n\tSSID: xiaomi\n\tRSN:\t * Version: 1\n"
                "\t\t * Authentication suites: PSK SAE\n"
                "BSS 02:00:00:00:00:a2(on wlan0)\n\tSSID: xiaomi\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
                "BSS 02:00:00:00:00:c1(on wlan0)\n\tSSID: car3\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: SAE\n"
                "BSS 02:00:00:00:00:c2(on wlan0)\n\tSSID: legacy\n\tWPA:\t * Version: 1\n"
                "BSS 02:00:00:00:00:c3(on wlan0)\n\tSSID: cafe\n")
        self.assertEqual(setup_wizard.security(dump, 'xiaomi'), 'wpa3-transition')
        self.assertEqual(setup_wizard.security(dump, 'car3'), 'wpa3')
        self.assertEqual(setup_wizard.security(dump, 'legacy'), 'wpa2')
        self.assertEqual(setup_wizard.security(dump, 'cafe'), 'open')
        self.assertIsNone(setup_wizard.security(dump, 'missing'))
        self.assertEqual(setup_wizard.handoff_security('wpa3-transition'), 'wpa2')
        self.assertEqual(setup_wizard.handoff_security(None), 'wpa2')
        self.assertEqual(setup_wizard.handoff_security('wpa3'), 'wpa3')
        self.assertEqual(setup_wizard.handoff_security('open'), 'open')

    def test_installed_box_without_setup_file_counts_as_done_only_with_car_hotspot(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(setup_wizard.load(state), {'done': False, 'wizard_version': 2, 'iphone': 'any'})
            (state / 'car-hotspot.json').write_text('{}')
            self.assertEqual(setup_wizard.load(state), {'done': True, 'mode': 'car_hotspot', 'iphone': 'wired'})
            setup_wizard.save(state, {'done': False})
            self.assertFalse(setup_wizard.load(state)['done'])
            self.assertEqual((state / 'setup.json').stat().st_mode & 0o777, 0o600)

    def test_join_failure_reasons(self):
        self.assertEqual(setup_wizard.join_failure('WPA: 4-Way Handshake failed - pre-shared key may be incorrect'), 'wrong_password')
        self.assertEqual(setup_wizard.join_failure('CTRL-EVENT-CONNECTED - Connection to x completed'), 'no_address')
        self.assertEqual(setup_wizard.join_failure(''), 'not_found')
        sae = 'wlan0: CTRL-EVENT-AUTH-REJECT 02:00:00:00:00:a1 auth_type=3 auth_transaction=2 status_code=15'
        self.assertEqual(setup_wizard.join_failure(sae), 'wrong_password')


class DirectPhoneTests(unittest.TestCase):
    """The direct iPhone hotspot as a normal choice (the user, 2026-10-17): by the radio and the upstream's channel,
    never by car make."""

    def test_on_by_default_for_the_car_hotspot_when_the_radio_can_and_the_owner_decides(self):
        d = setup_wizard.direct_phone
        self.assertTrue(d({}, True, 'car_hotspot'))
        self.assertFalse(d({}, True, 'mifi'), 'a MiFi keeps its own wireless way unless asked')
        self.assertTrue(d({'direct_phone': True}, True, 'mifi'))
        self.assertFalse(d({'direct_phone': False}, True, 'car_hotspot'), 'turned off stays off')
        self.assertFalse(d({'direct_phone': True}, False, 'car_hotspot'), 'not without the radio')
        self.assertFalse(d({'direct_phone': True}, True, 'box_hotspot'))
        w = setup_wizard.wireless
        self.assertTrue(w({'mode': 'car_hotspot', 'iphone': 'any'}, {}, capable=True))
        self.assertFalse(w({'mode': 'car_hotspot', 'iphone': 'any'}, {}, capable=False))
        self.assertFalse(w({'mode': 'car_hotspot', 'iphone': 'wired'}, {}, capable=True))
        self.assertTrue(w({'mode': 'mifi', 'iphone': 'any'}, {}))
        self.assertTrue(w({'mode': 'box_hotspot', 'iphone': 'wireless'}, {}))

    def test_the_radio_says_which_channels_an_ap_may_use(self):
        from platform_debian import ap_channel_problem
        iw = ("\t\tFrequencies:\n\t\t\t* 2437.0 MHz [6] (20.0 dBm)\n\t\t\t* 5240.0 MHz [48] (20.0 dBm)\n"
              "\t\t\t* 5260.0 MHz [52] (disabled)\n\t\t\t* 5500 MHz [100] (20.0 dBm) (no IR, radar detection)\n"
              "\t\t\t* 5745.0 MHz [149] (20.0 dBm)\n")
        self.assertEqual([ap_channel_problem(iw, f) for f in (2437, 5240, 5260, 5500, 5745, 5825)],
                         [None, None, 'disabled', 'no IR', None, 'not listed'])


class WizardPageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root / 'state'; self.state.mkdir()
        (self.state / 'hotspot.json').write_text('{"ssid": "DashPlay", "passphrase": "installed1"}')
        self.status = {'mode': 'ap', 'home': 'http://192.168.78.1/', 'box_ssid': 'DashPlay', 'entries': [],
                       'sim': False, 'modes': {'AP', 'managed'}}
        self.changes = []
        self.wired = self.root / 'wired-status.json'
        class FakeScanner:
            def results(self, fresh=False): return setup_wizard.parse_scan(SCAN, 'DashPlay')
        self.page = SettingsPage(self.state, lambda: self.status, self.changes.append, lambda: [], FakeScanner(), self.wired)
        self.page.apply_later = self.changes.append
        self.nonce = self.page.pairing.nonce
        setup_wizard.save(self.state, {"done": False})  # legacy unfinished setup

    def choose(self, mode):
        """The way of use as an unfinished setup holds it before its network is set (the old step 1 is gone)."""
        setup_wizard.save(self.state, {'done': False, 'mode': mode})

    def set_up_done(self, mode='car_hotspot'):
        (self.state / 'car-hotspot.json').write_text('{"ssid": "xiaomi", "passphrase": "12345678"}')
        setup_wizard.save(self.state, {'done': True, 'mode': mode})

    def test_an_unfinished_older_setup_continues_in_first_run_setup(self):
        # The three-step setup (way of use -> network -> iPhone) gave way to one question per page on 2026-10-15;
        # its first two pages are gone. A box left half way, before its network was set, asks again from the start.
        for leftover in ({'done': False}, {'done': False, 'mode': 'box_hotspot'}):
            setup_wizard.save(self.state, leftover)
            html = self.page.render('')
            self.assertIn('什么车', html)
            self.assertNotIn('第 1 步，共 3 步', html)
            with self.assertRaises(ValueError):
                self.page.apply('/setup', {'action': 'mode', 'mode': 'mifi', 'nonce': self.nonce})
            with self.assertRaises(ValueError):
                self.page.apply('/setup', {'action': 'hotspot', 'ssid': 'Box', 'password': 'boxpass1', 'nonce': self.nonce})
        self.assertEqual(self.changes, [])   # nothing on the air changes until a network is set
        # Joining a hotspot (car or MiFi) still goes through its own page: the box shows what it tried.
        self.choose('mifi')
        html = self.page.render('')
        self.assertIn('<h1>连接随身 WiFi</h1>', html)
        self.assertIn('选取随身 WiFi', html)

    def test_music_and_calls_can_go_over_the_car_bluetooth(self):
        # Found in a Tesla (2026-10-14), for any car paired with the iPhone: the choice lands in state/options.json
        # beside idle_release, and the supervisor hands it to the receiver.
        import json
        from supervisor import car_audio_args
        self.set_up_done()
        (self.state / 'options.json').write_text(json.dumps({'idle_release': 0}))
        html = self.page.render('')
        self.assertIn('name="audio_route_navigation"', html)
        self.assertNotIn('name="audio_route_main"', html, 'the media/Siri/calls choice changed nothing on the iPhone')
        self.assertEqual(car_audio_args(self.state), [])
        self.page.apply('/audio', {'car_music': 'on', 'nonce': self.nonce})
        self.assertEqual(json.loads((self.state / 'options.json').read_text()),
                         {'idle_release': 0, 'tesla': False, 'car_music': True, 'car_voice': False})
        self.assertEqual(car_audio_args(self.state), ['--car-music'])
        self.assertEqual(self.changes, ['car audio changed'])
        # The sound group changes the receiver's options only: the receiver restarts, Wi-Fi stays (2026-10-29).
        message = self.page.apply('/audio', {'scope': 'sound', 'car_voice': 'on', 'nonce': self.nonce})
        self.assertEqual(self.changes[-1], 'receiver options changed')
        self.assertIn('WiFi 不断', message)
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'box': True})
        self.page.apply('/audio', {'scope': 'car', 'tesla': 'on', 'nonce': self.nonce})
        self.assertEqual(self.changes[-1], 'car audio changed', 'Tesla mode moves the hotspot: a full restart')
        self.assertIn('name="audio_route_navigation"><option value="carplay" selected>', self.page.render(''),
                      'Tesla mode keeps the sound on CarPlay; the iPhone Control Center picks the Tesla')
        self.page.apply('/audio', {'car_voice': 'on', 'nonce': self.nonce})
        self.assertEqual(car_audio_args(self.state), ['--car-voice'])
        with self.assertRaises(ValueError):
            self.page.apply('/audio', {'car_music': 'on', 'nonce': 'bad'})

    def test_tesla_mode_moves_the_box_hotspot_and_brings_the_car_bluetooth(self):
        import json
        from supervisor import car_audio_args
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'box': True})
        html = self.page.render('')
        self.assertIn('特斯拉模式', html)
        self.assertIn('盒子热点的网段改成 100.96.0.1', html)
        message = self.page.apply('/audio', {'tesla': 'on', 'nonce': self.nonce})
        self.assertIn('热点网段改为 100.96.0.1', message)
        self.assertEqual(json.loads((self.state / 'options.json').read_text())['tesla'], True)
        self.assertEqual(car_audio_args(self.state), ['--tesla'])
        # Saved again unchanged: no talk of a new address.
        self.assertNotIn('网段', self.page.apply('/audio', {'tesla': 'on', 'car_music': 'on', 'nonce': self.nonce}))

    def test_tesla_mode_leaves_the_sound_switch_alone(self):
        # 2026-10-31: the sound output is one switch (CarPlay keeps its sound / none through CarPlay); turning Tesla
        # on does not change it. The page still plays Tesla's way (prompt boost, no media session).
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'box': True})
        self.page.apply('/audio', {'scope': 'sound', 'audio_route_navigation': 'carplay', 'nonce': self.nonce})
        self.page.apply('/audio', {'scope': 'car', 'tesla': 'on', 'nonce': self.nonce})
        self.assertEqual(self.page.sound_routes()['navigation'], 'carplay')
        self.assertEqual(self.page.sound_routes()['media'], 'bluetooth', 'car-music page behaviour')
        self.assertIn('控制中心', self.page.render(''))

    def test_box_answers_its_own_domain_on_its_hotspot_by_default(self):
        # A car that gets no DNS of its own on the box hotspot (no internet; the Xpeng did not fall back to its
        # LTE, 2026-10-12) still opens the own domain with the trusted certificate (2026-10-15). On by default since
        # 2026-10-29: an own domain set in this way of use is meant to open; the switch only turns it off.
        import json
        from supervisor import hotspot_names
        name = 'mycar.duckdns.org'
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'box': True})
        self.assertNotIn('name=own_name_on_hotspot', self.page.render(''), 'no own domain, no switch')
        self.assertEqual(hotspot_names(self.state, None, 'box_hotspot'), [])
        (self.state / 'ddns.json').write_text(json.dumps({'domain': 'mycar', 'token': '0a1b2c3d-1111-2222-3333-444455556666'}))
        # Older pages wrote local_dns=false with every save of the car or sound group: not the owner's choice.
        (self.state / 'options.json').write_text(json.dumps({'local_dns': False}))
        html = self.page.render('')
        self.assertIn('name=own_name_on_hotspot value=on checked', html)
        self.assertNotIn('>域名解析<', html)
        self.assertEqual(hotspot_names(self.state, name, 'box_hotspot'), [name])
        self.page.apply('/audio', {'scope': 'own_name', 'nonce': self.nonce})   # unticked
        self.assertIs(json.loads((self.state / 'options.json').read_text())['own_name_on_hotspot'], False)
        self.assertEqual(hotspot_names(self.state, name, 'box_hotspot'), [])
        self.page.apply('/audio', {'scope': 'sound', 'car_music': 'on', 'nonce': self.nonce})
        self.assertIs(json.loads((self.state / 'options.json').read_text())['own_name_on_hotspot'], False,
                      'saving another group keeps the choice')
        self.page.apply('/audio', {'tesla': 'on', 'scope': 'car', 'nonce': self.nonce})
        self.assertNotIn('name=own_name_on_hotspot', self.page.render(''), 'Tesla mode always answers it')
        self.assertEqual(hotspot_names(self.state, name, 'box_hotspot'), list(setup_wizard.TESLA_CHECK_NAMES) + [name])
        self.assertEqual(hotspot_names(self.state, None, 'box_hotspot'), list(setup_wizard.TESLA_CHECK_NAMES), 'checks without own domain')
        self.assertEqual(hotspot_names(self.state, 'mycar.duckdns.org', 'mifi'), [],
                         'a joined hotspot has its own DNS; the fallback hotspot is not where the car looks')

    def test_the_own_name_switch_is_only_where_it_works(self):
        import json
        (self.state / 'ddns.json').write_text(json.dumps({'domain': 'mycar', 'token': '0a1b2c3d-1111-2222-3333-444455556666'}))
        self.set_up_done('car_hotspot')
        self.assertNotIn('name=own_name_on_hotspot', self.page.render(''), 'the car hotspot answers names there')
        # deSEC names have no public address of their own to fall back to: always answered, no switch.
        (self.state / 'ddns.json').write_text(json.dumps({'provider': 'desec', 'domain': 'box01.car.example.com',
                                                          'token': 'A1b2C3d4E5f6G7h8J9kMnPqRsTuV'}))
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'box': True})
        (self.state / 'options.json').write_text(json.dumps({'own_name_on_hotspot': False}))
        self.assertNotIn('name=own_name_on_hotspot', self.page.render(''))
        from supervisor import hotspot_names
        self.assertEqual(hotspot_names(self.state, 'box01.car.example.com', 'box_hotspot'), ['box01.car.example.com'])

    def test_tesla_mode_on_a_joined_hotspot_says_where_the_address_is_set(self):
        self.set_up_done('mifi')
        self.assertIn('自己的后台把局域网地址改成 100.96.0.1', self.page.render(''))
        self.status['tesla_warning'] = '随身 WiFi分给盒子的地址是 192.168.0.5'
        self.assertIn('分给盒子的地址是 192.168.0.5', self.page.render(''))

    def test_tesla_domain_advice_goes_once_the_own_certificate_is_served(self):
        import hashlib
        setup_wizard.save(self.state, {'done': True, 'mode': 'box_hotspot', 'box': True})
        self.page.apply('/audio', {'tesla': 'on', 'nonce': self.nonce})
        self.assertIn('特斯拉建议回家插网线后设置自己的网址', self.page.render(''))
        own = self.state / 'tls' / 'own'; own.mkdir(parents=True)
        (own / 'fullchain.pem').write_bytes(b'chain')
        (self.state / 'tls' / 'leaf.san.json').write_text(json.dumps({'own': hashlib.sha256(b'chain').hexdigest()[:16]}))
        self.assertNotIn('特斯拉建议回家插网线后设置自己的网址', self.page.render(''))

    def test_car_hotspot_with_the_direct_hotspot_does_not_ask_for_the_cable(self):
        self.set_up_done('car_hotspot')
        setup_wizard.save(self.state, dict(setup_wizard.load(self.state), iphone='any'))
        self.status.update(mode='sta', direct_capable=True)
        self.assertNotIn('插数据线使用', self.page.render(''))
        # Chosen cable only (also what an older setup without the choice means here).
        setup_wizard.save(self.state, dict(setup_wizard.load(self.state), iphone='wired'))
        self.assertIn('插数据线使用', self.page.render(''))
        setup_wizard.save(self.state, dict(setup_wizard.load(self.state), iphone='any'))
        # The direct hotspot fell back (2.4 GHz car hotspot): the cable again.
        self.status['direct_phone'] = '车机热点在 2.4 GHz，这次已改用数据线。'
        self.assertIn('插数据线使用', self.page.render(''))
        # A box that cannot keep both hotspots: cable only.
        self.status.update(direct_capable=False, direct_phone='')
        self.assertIn('插数据线使用', self.page.render(''))

    def test_the_screen_port_is_under_advanced(self):
        self.set_up_done('box_hotspot')
        self.status['entries'] = ['https://192.168.78.1:18443/']
        html = self.page.render('')
        carplay, advanced = html.index('>CarPlay 画面<'), html.index('>高级<')
        self.assertGreater(html.index('name=viewer_port'), advanced)
        self.assertNotIn('name=viewer_port', html[carplay:advanced])
        self.assertIn('https://192.168.78.1:18443/', html[carplay:advanced])
        self.assertIn('action=/viewer-port', html, 'still offered without a CarPlay address yet')
        # The button had no handler since the setting came in (9c5c6fc): saving it raised AttributeError.
        import json, viewer_port
        self.assertIn('1 分钟', self.page.apply('/viewer-port', {'viewer_port': '18444', 'nonce': self.nonce}))
        self.assertEqual(viewer_port.resolve(self.state), 18444)
        self.assertEqual(self.changes, ['viewer port changed'])
        for bad in ('80', '443', '7000', '1023', 'x'):
            with self.assertRaises(ValueError):
                self.page.apply('/viewer-port', {'viewer_port': bad, 'nonce': self.nonce})
        self.assertEqual(json.loads((self.state / 'options.json').read_text())['viewer_port'], 18444)

    def test_cable_steps_name_the_port_for_each_box(self):
        self.set_up_done('box_hotspot')
        html = self.page.render('')
        self.assertIn('玩客云插<b>远离网口</b>的那个 USB 口', html)
        self.assertIn('N1 两个 USB 口都行', html)

    def test_the_box_hotspot_address_in_tesla_mode(self):
        import ipaddress
        usual = ipaddress.IPv4Interface('192.168.78.1/24')
        self.assertEqual(setup_wizard.box_hotspot_address(usual, False, []), (usual, None))
        self.assertEqual(setup_wizard.box_hotspot_address(usual, True, [ipaddress.ip_interface('192.168.5.20/24')]),
                         (setup_wizard.SHARED_LAN, None))
        # Already ours (a restart before the address was flushed): still the shared one.
        self.assertEqual(setup_wizard.box_hotspot_address(usual, True, [setup_wizard.SHARED_LAN])[0], setup_wizard.SHARED_LAN)
        kept, why = setup_wizard.box_hotspot_address(usual, True, [ipaddress.ip_interface('100.96.0.7/16')])
        self.assertEqual(str(kept), '100.97.0.1/24')
        self.assertIn('100.96.0.7/16', why)
        self.assertTrue(setup_wizard.tesla_reaches('100.96.0.23'))
        self.assertFalse(setup_wizard.tesla_reaches('192.168.0.5'))
        self.assertFalse(setup_wizard.tesla_reaches(None))

    def test_advanced_offers_the_drive_diagnostics(self):
        self.set_up_done()
        self.assertIn('href="/diag.txt"', self.page.render(''))

    def test_missing_identity_is_explained_on_every_page(self):
        self.status['identity'] = False
        self.assertIn('还缺 CarPlay 认证文件', self.page.render(''))
        self.assertIn('盒子地址 192.168.78.1', self.page.render(''))
        self.status['identity'] = True
        self.assertNotIn('认证文件', self.page.render(''))

    def test_wifi_interface_is_found_by_type_not_name(self):
        with tempfile.TemporaryDirectory() as directory:
            net = Path(directory)
            for name in ('eth0', 'lo', 'wlx020000000010'):
                (net / name).mkdir()
            self.assertIsNone(setup_wizard.wifi_interface(net))
            (net / 'wlx020000000010' / 'phy80211').mkdir()
            self.assertEqual(setup_wizard.wifi_interface(net), 'wlx020000000010')
        self.assertIsNone(setup_wizard.wifi_interface(Path('/nonexistent-net')))

    def test_portal_sends_the_phone_to_its_browser_and_then_passes_its_checks(self):
        # Two users (2026-10-17): "next" often did nothing until they left and joined the Wi-Fi again. The setup ran in
        # the phone's captive-portal window, which the phone closes - leaving the Wi-Fi - once the user switches away
        # (the sound step even sent them to Bluetooth settings). The window now only shows the address to open in a browser.
        phone, other = '192.168.78.20', '192.168.78.21'
        for host, path in (('captive.apple.com', '/hotspot-detect.html'), ('connectivitycheck.gstatic.com', '/generate_204'),
                           ('192.168.78.1', '/generate_204')):
            code, body, kind, _ = self.page.portal_reply(host, path, phone)
            self.assertEqual((code, kind), (200, 'text/html; charset=utf-8'))
            self.assertIn('value="http://192.168.78.1/"', body)
            self.assertIn('复制地址', body)
            self.assertNotIn('/setup-flow', body)
        self.assertIsNone(self.page.portal_reply('192.168.78.1', '/', phone), 'the address typed in a browser: the setup page')
        self.assertIsNone(self.page.portal_reply('dashplay.local', '/?m=x', phone))
        # The window drew the page (its pixel): from then on that phone's own checks pass, so it keeps the Wi-Fi.
        self.assertEqual(self.page.portal_reply('captive.apple.com', '/portal-seen.gif', phone)[:1], (200,))
        self.assertEqual(self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', phone)[1],
                         b'<HTML><HEAD><TITLE>Success</TITLE></HEAD><BODY>Success</BODY></HTML>')
        for host in ('connectivitycheck.gstatic.com', 'connect.rom.miui.com', 'connectivitycheck.platform.hicloud.com'):
            self.assertEqual(self.page.portal_reply(host, '/generate_204', phone)[:2], (204, b''))
        self.assertEqual(self.page.portal_reply('www.msftconnecttest.com', '/connecttest.txt', phone)[1], b'Microsoft Connect Test')
        self.assertIn('复制地址', self.page.portal_reply('www.baidu.com', '/', phone)[1], 'another site: the address again')
        self.assertIn('复制地址', self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', other)[1])
        # Through the whole first setup, not only its first two questions.
        setup_wizard.save(self.state, {'done': False, 'wizard_version': 2})
        (self.state / 'setup-draft.json').write_text(json.dumps({'tesla': False, 'car_can_hotspot': False, 'mode': 'box_hotspot',
                                                                 'answered': ['vehicle', 'mode', 'mifi', 'network', 'internet']}))
        self.assertIn('复制地址', self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', other)[1])
        self.assertEqual(self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', phone)[0], 200)
        self.status['mode'] = 'sta'
        self.assertIsNone(self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', other), 'not on a joined hotspot')
        self.status['mode'] = 'ap'
        self.set_up_done()
        self.assertIsNone(self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', other))
        self.assertIsNone(self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', phone))

    def test_retyping_the_car_hotspot_keeps_the_answers_of_first_run_setup(self):
        # After the one-question-per-page setup the box tries the car hotspot; a wrong password brings the car step
        # back. Saving it again dropped the confirmed answers, and the iPhone fell back to cable only.
        setup_wizard.save(self.state, {'done': False, 'wizard_version': 2, 'confirmed': True, 'mode': 'car_hotspot',
                                       'iphone': 'any', 'box': False, 'car': 'xiaomi', 'failed': 'wrong_password'})
        self.assertIn('没有连上「xiaomi」', self.page.render(''))
        self.page.apply('/setup', {'action': 'car', 'ssid': 'xiaomi', 'password': '12345678', 'nonce': self.nonce})
        self.assertEqual(setup_wizard.load(self.state), {'done': False, 'wizard_version': 2, 'confirmed': True,
                                                        'mode': 'car_hotspot', 'iphone': 'any', 'box': False, 'car': 'xiaomi'})

    def test_car_hotspot_step_lists_networks_and_saves_choice(self):
        self.choose('car_hotspot')
        html = self.page.render('')
        self.assertIn('<h1>连接车机热点</h1>', html)
        self.assertIn('value="小鹏 G6"', html)
        self.assertNotIn('value="DashPlay"', html)
        with self.assertRaises(ValueError):
            self.page.apply('/setup', {'action': 'car', 'ssid': 'xiaomi', 'password': '12345678', 'nonce': 'bad'})
        with self.assertRaises(ValueError):
            self.page.apply('/setup', {'action': 'car', 'ssid': 'xiaomi', 'password': 'short', 'nonce': self.nonce})
        kind, html = self.page.apply('/setup', {'action': 'car', 'ssid': 'xiaomi', 'password': '12345678', 'nonce': self.nonce})
        self.assertEqual(kind, 'page')
        self.assertIn('http://dashplay.local/', html)
        self.assertIn('手机也连接「xiaomi」', html)
        self.assertEqual(read_json(self.state / 'car-hotspot.json'), {'ssid': 'xiaomi', 'passphrase': '12345678'})
        self.assertEqual((self.state / 'car-hotspot.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(setup_wizard.load(self.state), {'done': False, 'mode': 'car_hotspot', 'car': 'xiaomi', 'iphone': 'wired'})
        self.assertEqual(self.changes, ['setup car hotspot chosen'])
        self.assertNotIn('12345678', html)

    def test_joining_box_sets_its_fallback_hotspot_password_when_the_firmware_has_none(self):
        (self.state / 'hotspot.json').write_text('{"ssid": "DashPlay"}')
        self.choose('mifi')
        html = self.page.render('')
        self.assertIn('name=box_password', html)
        self.assertIn('找不到随身 WiFi时', html)
        with self.assertRaises(ValueError):
            self.page.apply('/setup', {'action': 'car', 'ssid': 'xiaomi', 'password': '12345678', 'nonce': self.nonce})
        self.page.apply('/setup', {'action': 'car', 'ssid': 'xiaomi', 'password': '12345678', 'box_password': 'boxpass9', 'nonce': self.nonce})
        self.assertEqual(read_json(self.state / 'hotspot.json'), {'ssid': 'DashPlay', 'passphrase': 'boxpass9'})
        setup_wizard.save(self.state, {'done': False, 'mode': 'mifi', 'car': 'xiaomi', 'failed': 'wrong_password'})
        self.assertNotIn('name=box_password', self.page.render(''))   # asked once only

    def test_manual_name_and_failure_banner(self):
        self.choose('car_hotspot')
        self.page.apply('/setup', {'action': 'car', 'ssid': '__manual__', 'ssid_manual': 'Hidden Car', 'password': '12345678', 'nonce': self.nonce})
        self.assertEqual(read_json(self.state / 'car-hotspot.json')['ssid'], 'Hidden Car')
        setup_wizard.save(self.state, {'done': False, 'mode': 'car_hotspot', 'car': 'Hidden Car', 'failed': 'wrong_password'})
        html = self.page.render('')
        self.assertIn('没有连上「Hidden Car」', html)
        self.assertIn('密码不对', html)

    def test_car_hotspot_way_connects_the_iphone_by_cable_only(self):
        (self.state / 'car-hotspot.json').write_text('{"ssid": "xiaomi", "passphrase": "12345678"}')
        setup_wizard.save(self.state, {'done': False, 'mode': 'car_hotspot', 'car': 'xiaomi'})
        self.status.update(mode='sta', ssid='xiaomi', entries=['https://192.168.5.80:18443/'])
        html = self.page.render('')
        self.assertIn('<h1>连接 iPhone</h1>', html)
        self.assertIn('有线 CarPlay', html)
        self.assertIn('远离网口', html)
        self.assertIn('未启动', html)
        self.assertIn('只能用数据线', html)
        self.assertNotIn('value=start', html)   # no Bluetooth pairing in this way of use
        self.assertIn('完成设置', html)
        save_json(self.wired, {'phase': 'cable'})
        html = self.page.render('')
        self.assertIn('等待插线', html)
        self.assertIn('http-equiv=refresh', html)
        save_json(self.wired, {'phase': 'trust'})
        self.assertIn('等待信任', self.page.render(''))
        save_json(self.wired, {'phase': 'live'})
        html = self.page.render('')
        self.assertIn('已连接', html)
        self.assertIn('>完成设置<', html)
        self.assertIn('https://192.168.5.80:18443/', html)
        self.assertNotIn('http-equiv=refresh', html)
        self.page.apply('/setup', {'action': 'finish', 'nonce': self.nonce})
        self.assertTrue(setup_wizard.load(self.state)['done'])
        full = self.page.render('')
        self.assertIn('<h1 class=span>盒子设置</h1>', full)
        self.assertIn('方式一：车机热点', full)
        self.assertIn('href="/?view=car"', full)
        self.assertIn('当前通过车机热点连接', full)
        self.assertNotIn('添加 iPhone', full)
        self.assertNotIn('连接方式（测试）', full)
        self.assertIn('重新进行首次设置', full)
        self.assertIn('<h1>车机热点</h1>', self.page.render('view=car'))

    def test_mifi_way_offers_cable_and_wireless(self):
        (self.state / 'car-hotspot.json').write_text('{"ssid": "mifi", "passphrase": "12345678"}')
        setup_wizard.save(self.state, {'done': False, 'mode': 'mifi', 'car': 'mifi'})
        self.status.update(mode='sta', ssid='mifi')
        html = self.page.render('')
        self.assertIn('用数据线（推荐）', html)
        self.assertIn('value=start', html)
        self.assertIn('有的随身 WiFi 不让设备互相访问', html)
        self.assertIn('在车机屏幕上操作', html)
        self.assertNotIn('http-equiv=refresh', html)   # nothing plugged in: no reloading while reading
        self.page.apply('/setup', {'action': 'finish', 'nonce': self.nonce})
        full = self.page.render('')
        self.assertIn('方式二：随身 WiFi', full)
        self.assertIn('<b>随身 WiFi</b>', full)
        self.assertIn('<h1>随身 WiFi</h1>', self.page.render('view=car'))

    def test_box_hotspot_way_sets_the_box_hotspot_then_the_iphone(self):
        # First-run setup has set the box hotspot (setup_flow): the next page is the iPhone.
        (self.state / 'hotspot.json').write_text('{"ssid": "Box", "passphrase": "boxpass1"}')
        setup_wizard.save(self.state, {'done': False, 'wizard_version': 2, 'confirmed': True, 'mode': 'box_hotspot',
                                       'iphone': 'any', 'box': True})
        self.assertIsNone(self.page.portal_reply('captive.apple.com', '/hotspot-detect.html', '192.168.78.20'))
        self.status.update(box_ssid='Box', entries=['https://192.168.78.1:18443/'])
        html = self.page.render('')
        self.assertIn('<h1>连接 iPhone</h1>', html)
        self.assertIn('用数据线（推荐）', html)
        self.assertIn('value=start', html)
        with self.assertRaises(ValueError):   # chose wireless or either: an iPhone has to be added first
            self.page.apply('/setup', {'action': 'finish', 'nonce': self.nonce})
        with patch.object(self.page.pairing, 'devices', return_value={'02:00:00:00:00:01': 'A'}):
            self.page.apply('/setup', {'action': 'finish', 'nonce': self.nonce})
        full = self.page.render('')
        self.assertIn('方式三：车机连盒子热点', full)
        self.assertNotIn('href="/?view=car"', full)
        self.assertIn('是否能上网请看「车机上网」当前状态', full)
        self.assertIn('盒子设置', self.page.render('view=car'))   # no car hotspot page in this way

    def test_pairing_cancel_and_car_page_errors_return_to_their_page(self):
        self.set_up_done()
        with patch.object(self.page.pairing, 'stop') as stop:
            self.assertEqual(self.page.apply('/pair', {'action': 'cancel', 'nonce': self.nonce}), '已取消添加。')
            stop.assert_called_once()
        with self.assertRaises(ValueError):
            self.page.apply('/car', {'action': 'save', 'ssid': 'xiaomi', 'password': 'x', 'nonce': self.nonce})
        kind, html = self.page.apply('/car', {'action': 'save', 'ssid': 'xiaomi', 'password': '12345678', 'nonce': self.nonce})
        self.assertIn('正在连接「xiaomi」', html)
        self.assertEqual(self.changes, ['car hotspot saved'])
        # A saved password may be kept on the change page only.
        self.assertNotIn(' required minlength', self.page.render('view=car'))
        kind, html = self.page.apply('/car', {'action': 'save', 'ssid': 'xiaomi', 'password': '', 'nonce': self.nonce})
        self.assertEqual(read_json(self.state / 'car-hotspot.json')['passphrase'], '12345678')

    def test_finish_offered_once_the_paired_phone_is_in_use(self):
        (self.state / 'car-hotspot.json').write_text('{"ssid": "xiaomi", "passphrase": "12345678"}')
        setup_wizard.save(self.state, {'done': False, 'mode': 'mifi', 'car': 'xiaomi'})
        self.status.update(mode='sta', ssid='xiaomi')
        save_json(self.page.pairing.status_file, {'phase': 'paired'})
        class Done:
            def poll(self): return 0
        self.page.pairing.process = Done()
        A = '02:00:00:00:00:01'
        with patch.object(self.page.pairing, 'devices', return_value={A: 'A'}), \
                patch.object(self.page.pairing, 'pending_phone', return_value=A):
            html = self.page.render('')
            self.assertIn('蓝牙已配对，正在尝试连接 CarPlay', html)
            self.assertIn('value=finish', html)
            save_json(self.root / 'state/receiver/phone-presence.json', {'active': A})
            html = self.page.render('')
        self.assertNotIn('正在尝试连接 CarPlay', html)
        self.assertNotIn('http-equiv=refresh', html)
        self.assertIn('>完成设置<', html)

    def test_adding_a_phone_is_explained_in_one_place_where_the_button_was(self):
        # Two reasons stop adding: a phone using CarPlay over Wi-Fi, or CarPlay on the cable (which pauses
        # every wireless invitation). Both used to show up elsewhere (a flash at the top of the page), so a
        # tap looked like nothing happened (2026-10-31, owner's XS). Both now replace the add button.
        self.set_up_done('box_hotspot')
        self.status.update(mode='ap', box_ssid='DashPlay')
        (self.state / 'hotspot.json').write_text('{"ssid": "DashPlay", "passphrase": "boxpass1"}')
        save_json(self.page.pairing.status_file, {'phase': 'paired'})
        class Done:
            def poll(self): return 0
        self.page.pairing.process = Done()
        A = '02:00:00:00:00:01'
        save_json(self.root / 'state/receiver/phone-presence.json', {'active': A})
        # A phone is using CarPlay over Wi-Fi: the reason takes the button's place, in the phone block.
        with patch.object(self.page.pairing, 'devices', return_value={A: 'XS'}):
            html = self.page.render('')
            self.assertIn('正在使用 CarPlay。要换手机，在下面点另一部的「切换到这部手机」', html)
            self.assertNotIn('value=start', html, 'nothing to tap while adding is impossible')
            # Where the button is: below the list, not in the space above it.
            self.assertGreater(html.index('正在使用 CarPlay。要换手机'),
                               html.index('我的手机'), 'the reason sits where the button was')
        # The POST still refuses, with the same sentence (require_idle guards start()).
        with self.assertRaises(ValueError) as caught:
            self.page.pairing.require_idle()
        self.assertIn('正在使用 CarPlay。要换手机', str(caught.exception))
        # Nobody using CarPlay: the button is back.
        save_json(self.root / 'state/receiver/phone-presence.json', {'active': None})
        with patch.object(self.page.pairing, 'devices', return_value={A: 'XS'}):
            self.assertIn('value=start', self.page.render(''))
        # CarPlay live on the cable: the same place, the cable's own reason and the two ways out.
        save_json(self.wired, {'phase': 'live'})
        with patch.object(self.page.pairing, 'devices', return_value={A: 'XS'}):
            html = self.page.render('')
            self.assertIn('现在用数据线连着 CarPlay，无线连接是暂停的，所以暂时不能添加手机', html)
            self.assertIn('拔掉数据线', html)
            # The way out is this page's own switch button, not a trip to the viewer.
            self.assertIn('切换到这部手机', html)
            self.assertNotIn('线只用来上网', html)
            self.assertNotIn('value=start', html)
        with patch.object(self.page.pairing, 'start') as start:
            with self.assertRaises(ValueError) as caught:
                self.page.apply('/pair', {'action': 'start', 'nonce': self.nonce})
            # Same sentence as the banner above: one situation, one wording, one place.
            self.assertEqual(str(caught.exception), settings_page.CABLE_BLOCKS_ADDING)
            start.assert_not_called()
        # Off the cable: adding works again.
        save_json(self.wired, {'phase': 'cable'})
        with patch.object(self.page.pairing, 'start') as start:
            self.assertEqual(self.page.apply('/pair', {'action': 'start', 'nonce': self.nonce}), '')
            start.assert_called_once()
        with patch.object(self.page.pairing, 'devices', return_value={A: 'XS'}):
            self.assertIn('value=start', self.page.render(''))

    def test_the_supervisor_hands_the_receiver_the_cable_marker_not_a_network_setting(self):
        # The receiver owns whether the cable carries CarPlay (CableRole.kt); "wireless only" starts it off.
        source = (Path(__file__).resolve().parents[2] / 'scripts/onecloud/supervisor.py').read_text()
        self.assertIn('"--cable-off-file", str(CABLE_OFF)', source)
        self.assertIn('"--cable-carplay", "off" if wireless_only else "on"', source)
        self.assertIn("wireless_only = (setup.get('iphone') == 'wireless'", source)
        self.assertNotIn('usb_tether_only', source)
        receiver = (Path(__file__).resolve().parents[2] / 'scripts/onecloud/run-receiver.py').read_text()
        self.assertIn('env["CARPLAY_CABLE_OFF_FILE"]', receiver)

    def test_settings_change_box_hotspot(self):
        (self.state / 'hotspot.json').write_text('{"ssid": "Box", "passphrase": "boxpass1"}')
        self.set_up_done()
        self.status.update(mode='sta', ssid='xiaomi', channel=149)
        full = self.page.render('')
        self.assertIn('href="/?view=hotspot"', full)
        self.assertIn('href="/?view=car"', full)
        page = self.page.render('view=hotspot')
        self.assertIn('轻点显示', page)
        self.assertIn('boxpass1', page)
        self.assertIn('连不上车机热点时', page)
        # Joined to the car hotspot the box hotspot is off: saved for the next time it opens.
        message = self.page.apply('/hotspot', {'ssid': 'Box2', 'password': '', 'nonce': self.nonce})
        self.assertIn('Box2', message)
        self.assertEqual(read_json(self.state / 'hotspot.json'), {'ssid': 'Box2', 'passphrase': 'boxpass1'})
        self.assertEqual(self.changes, [])
        # On the air (box_hotspot way, or the fallback): a change restarts it.
        self.status.update(mode='ap')
        kind, html = self.page.apply('/hotspot', {'ssid': 'Box3', 'password': 'newpass1', 'nonce': self.nonce})
        self.assertEqual(self.changes, ['box hotspot changed'])
        self.assertIn('没有连上车机热点「xiaomi」', self.page.render(''))
        with self.assertRaises(ValueError):
            self.page.apply('/hotspot', {'ssid': 'Box3', 'password': 'short', 'nonce': self.nonce})

    def test_reset_clears_the_way_of_use_networks_and_phones(self):
        self.set_up_done()
        (self.state / 'car-lease.json').write_text('{}')
        (self.state / 'network-mode.json').write_text('{"mode": "relay"}')
        (self.state / 'receiver').mkdir()
        (self.state / 'receiver/lockdown-pair-record.properties').write_text('hostId=x')
        (self.state / 'receiver/lockdown-pair-record-00008030-001A2B3C4D5E802E.properties').write_text('hostId=y')
        (self.state / 'receiver/pairings.properties').write_text('phone=key')   # the receiver's CarPlay pairings
        with patch.object(self.page.pairing, 'reset_all') as reset:
            with self.assertRaises(ValueError):
                self.page.apply('/reset', {'nonce': 'bad'})
            reset.assert_not_called()
            self.page.apply('/reset', {'nonce': self.nonce})
            reset.assert_called_once()
        for gone in ('car-hotspot.json', 'car-lease.json', 'network-mode.json', 'receiver/lockdown-pair-record.properties',
                     'receiver/lockdown-pair-record-00008030-001A2B3C4D5E802E.properties', 'receiver/pairings.properties'):
            self.assertFalse((self.state / gone).exists(), gone)
        self.assertEqual(setup_wizard.load(self.state), {'done': False, 'wizard_version': 2, 'iphone': 'any'})
        self.assertEqual(self.changes, ['setup reset'])
        self.assertIn('什么车？', self.page.render(''))

    def test_reset_all_keeps_only_boot_marker(self):
        presence = self.root / 'state/receiver/phone-presence.json'
        save_json(presence, {'boot': 'b1', 'active': 'A', 'last_phone': 'A', 'phones': {'A': {}}})
        save_json(self.root / 'state/receiver/pairing-request.json', {'phone': 'A'})
        removed = []
        async def fake_remove(root, phone): removed.append(phone)
        with patch.object(self.page.pairing, 'devices', return_value={'A': 'a', 'B': 'b'}), \
             patch('pairing_window.remove_device', fake_remove):
            self.page.pairing.reset_all()
        self.assertEqual(sorted(removed), ['A', 'B'])
        self.assertEqual(read_json(presence), {'boot': 'b1'})
        self.assertFalse((self.root / 'state/receiver/pairing-request.json').exists())


class HostapdTests(unittest.TestCase):
    def test_open_only_without_passphrase(self):
        import platform_debian
        secured = platform_debian.hostapd_config('wlan0', 6, 20, 'Box', 'boxpass1')
        self.assertIn('wpa=2', secured)
        self.assertIn('wpa_passphrase=boxpass1', secured)
        opened = platform_debian.hostapd_config('wlan0', 6, 20, 'Box', None)
        self.assertNotIn('wpa', opened.replace('wmm', ''))
        self.assertIn('ssid=Box', opened)


class MdnsTests(unittest.TestCase):
    def query(self, name, qtype=1, query_id=7):
        return struct.pack('!HHHHHH', query_id, 0, 1, 0, 0, 0) + mdns_name.encode_name(name) + struct.pack('!HH', qtype, 0x8001)

    def test_answers_only_our_a_record(self):
        response = mdns_name.answer(self.query('DashPlay.local'), 'dashplay.local', '192.168.78.1', legacy=False)
        self.assertEqual(struct.unpack('!HHHH', response[:8]), (0, 0x8400, 0, 1))
        self.assertTrue(response.endswith(socket.inet_aton('192.168.78.1')))
        self.assertIsNone(mdns_name.answer(self.query('other.local'), 'dashplay.local', '192.168.78.1', legacy=False))
        self.assertIsNone(mdns_name.answer(self.query('dashplay.local', qtype=28), 'dashplay.local', '192.168.78.1', legacy=False))
        self.assertIsNone(mdns_name.answer(response, 'dashplay.local', '192.168.78.1', legacy=False))

    def test_legacy_unicast_echoes_id(self):
        response = mdns_name.answer(self.query('dashplay.local', query_id=99), 'dashplay.local', '10.0.0.2', legacy=True)
        self.assertEqual(struct.unpack('!H', response[:2])[0], 99)

    def test_malformed_packets_raise_parse_errors_only(self):
        self.assertIsNone(mdns_name.answer(b'\x00' * 5, 'dashplay.local', '10.0.0.2', legacy=False))
        bad = struct.pack('!HHHHHH', 1, 0, 1, 0, 0, 0) + b'\xc0\x20'
        with self.assertRaises((ValueError, IndexError, struct.error)):
            mdns_name.answer(bad, 'dashplay.local', '10.0.0.2', legacy=False)



class IdleReleaseOptionTests(unittest.TestCase):
    def test_owner_choice_in_state_survives_program_updates(self):
        import json, tempfile
        sys_path = str(Path(__file__).resolve().parents[2] / "scripts/onecloud")
        import sys as system
        if sys_path not in system.path: system.path.insert(0, sys_path)
        from supervisor import idle_release
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(idle_release(state), 300)
            (state / "options.json").write_text(json.dumps({"idle_release": 0}))
            self.assertEqual(idle_release(state), 0)
            (state / "options.json").write_text("not json")
            self.assertEqual(idle_release(state), 300)

    def test_a_running_receiver_takes_new_options_without_the_network(self):
        # Sound and idle release are receiver options: the supervisor restarts the receiver with them and keeps
        # the hotspot (2026-10-29); everything else in its command line stays.
        import json, tempfile
        from supervisor import receiver_options
        argv = ["python3", "run-receiver.py", "--root", "/opt/dashplay", "--bind", "192.168.78.1", "--wired",
                "--idle-release", "300", "--tesla-browser", "--car-music", "--channel", "149"]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "options.json").write_text(json.dumps({"tesla": False, "car_voice": True, "idle_release": 0}))
            self.assertEqual(receiver_options(argv, state),
                             ["python3", "run-receiver.py", "--root", "/opt/dashplay", "--bind", "192.168.78.1", "--wired",
                              "--channel", "149", "--idle-release", "0", "--car-voice"])

    def test_receiver_restart_after_channel_change_uses_saved_sound_options(self):
        # Exercise the supervisor's actual start closure: a channel-recovery callback
        # still holds the original argv after a settings-only receiver restart.
        import ast
        import supervisor
        from unittest.mock import Mock
        source = ast.parse(Path(supervisor.__file__).read_text())
        main = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
        start = next(n for n in main.body if isinstance(n, ast.FunctionDef) and n.name == 'start')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / 'options.json').write_text(json.dumps({'car_voice': True, 'idle_release': 0}))
            platform = Mock()
            scope = dict(vars(supervisor), state=state, platform=platform, have_identity=True,
                         receiver_argv_now=[], mark=lambda _step: None)
            exec(compile(ast.Module(body=[start], type_ignores=[]), supervisor.__file__, 'exec'), scope)
            stale = ['python3', 'run-receiver.py', '--channel', '36', '--idle-release', '300', '--car-music']
            scope['start']('receiver', stale)
            actual = platform.start.call_args.args[1]
            self.assertIn('--car-voice', actual)
            self.assertNotIn('--car-music', actual)
            self.assertEqual(actual[actual.index('--idle-release') + 1], '0')
            self.assertEqual(actual[actual.index('--channel') + 1], '36')
            self.assertEqual(scope['receiver_argv_now'], actual)

    def test_idle_release_is_set_on_the_page(self):
        # It was only in options.json on the N1 (LuCI has it on OpenWrt).
        import json
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'; state.mkdir()
            (state / 'hotspot.json').write_text('{"ssid": "DashPlay", "passphrase": "installed1"}')
            setup_wizard.save(state, {'done': True, 'mode': 'box_hotspot'})
            changes = []
            page = SettingsPage(state, lambda: {'mode': 'ap', 'home': 'http://192.168.78.1/', 'entries': []},
                                changes.append, lambda: [])
            page.apply_later = changes.append
            html = page.render('')
            self.assertGreater(html.index('name=idle_release'), html.index('>高级<'))
            self.assertIn('value=300', html[html.index('name=idle_release'):][:80])
            self.assertIn('WiFi 不断', page.apply('/idle-release', {'idle_release': '0', 'nonce': page.pairing.nonce}))
            self.assertEqual(json.loads((state / 'options.json').read_text())['idle_release'], 0)
            self.assertEqual(changes, ['receiver options changed'])
            for bad in ('-1', 'x', '86401'):
                with self.assertRaises(ValueError):
                    page.apply('/idle-release', {'idle_release': bad, 'nonce': page.pairing.nonce})

if __name__ == '__main__':
    unittest.main()
