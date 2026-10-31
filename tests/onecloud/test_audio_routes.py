import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import audio_routes


class AudioRoutesTest(unittest.TestCase):
    """One switch since 2026-10-31: audio_route_navigation keeps CarPlay's sound (carplay) or sends none through it
    (bluetooth). Per-category media/Siri/call choices changed nothing on the iPhone and are ignored."""
    def test_carplay_keeps_every_format_and_old_per_category_choices_are_ignored(self):
        for old in ({}, {'audio_route_main': 'bluetooth'}, {'audio_route_media': 'bluetooth', 'audio_route_siri': 'carplay'},
                    {'audio_route_main': 'carplay', 'audio_route_navigation': 'carplay'}):
            self.assertEqual(audio_routes.resolve(old)['navigation'], 'carplay', old)
            self.assertEqual(audio_routes.resolve(old)['media'], 'carplay', old)

    def test_not_through_carplay_sends_every_sound_away(self):
        self.assertEqual(audio_routes.resolve({'audio_route_navigation': 'bluetooth', 'car_music': '0'}),
                         audio_routes.grouped('bluetooth', 'bluetooth'))

    def test_page_car_music_follows_tesla_mode_alone(self):
        self.assertEqual(audio_routes.resolve({}, tesla=True)['media'], 'bluetooth')
        self.assertEqual(audio_routes.resolve({'car_music': '1'}, tesla=False)['media'], 'carplay',
                         'an old car_music must not keep the prompt boost after Tesla mode is turned off')
        self.assertEqual(audio_routes.resolve({'car_music': True, 'audio_route_navigation': 'carplay'})['media'], 'carplay')

    def test_turning_tesla_off_ends_the_car_music_page_behaviour(self):
        import json, tempfile
        from unittest.mock import Mock
        import settings_page, supervisor
        with tempfile.TemporaryDirectory() as d:
            state = Path(d)
            (state / 'options.json').write_text(json.dumps({'tesla': True, 'car_music': True, 'car_voice': True,
                                                            'audio_route_navigation': 'carplay'}))
            page = settings_page.SettingsPage(state, lambda: {}, lambda _: None, lambda: []); page.apply_later = Mock()
            self.assertIn('--car-music', supervisor.car_audio_args(state))
            page.apply('/audio', {'nonce': page.pairing.nonce, 'scope': 'car'})   # Tesla unticked
            self.assertNotIn('--car-music', supervisor.car_audio_args(state))
            self.assertNotIn('--tesla-browser', supervisor.car_audio_args(state))

    def test_switch_arguments_match_the_tested_declarations(self):
        # carplay -> --audio-routes navigation=carplay (nine formats); bluetooth -> navigation=bluetooth (six).
        on = audio_routes.arguments({'car_music': '1'}, tesla=True)
        self.assertEqual(on[:2], ['--tesla-browser', '--car-music'])
        self.assertIn('navigation=carplay', on[-1])
        off = audio_routes.arguments({'audio_route_navigation': 'bluetooth'})
        self.assertIn('navigation=bluetooth', off[-1])
        with self.assertRaises(ValueError):
            audio_routes.with_output({}, 'media')

    def test_cli_is_complete_and_round_trips(self):
        routes = audio_routes.resolve({'audio_route_navigation': 'bluetooth'})
        self.assertEqual(audio_routes.parse(audio_routes.serialize(routes)), routes)
        with self.assertRaises(ValueError):
            audio_routes.parse('media=carplay,media=bluetooth')


class SharedSettingsTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import Mock
        import settings_page
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.page = settings_page.SettingsPage(self.state, lambda: {}, lambda _: None, lambda: [])
        self.page.apply_later = Mock()

    def test_backend_save_is_read_by_viewer_and_viewer_change_is_rendered_by_backend(self):
        import json
        self.page.options_file.write_text(json.dumps({'idle_release': 300, 'tesla': True,
                                                      'audio_route_main': 'carplay', 'audio_route_media': 'carplay'}))
        self.page.save_car_audio({'scope': 'sound', 'audio_route_navigation': 'bluetooth'})
        self.assertEqual(self.page.sound_routes(), audio_routes.grouped('bluetooth', 'bluetooth'))
        result = self.page.apply('/audio-routes', {'nonce': self.page.pairing.nonce,
                                 'category': 'navigation', 'destination': 'carplay'})
        self.assertEqual(result['navigation'], 'carplay')
        self.assertIn('name="audio_route_navigation"><option value="carplay" selected>留在 CarPlay',
                      self.page.car_audio_block(self.page.pairing.nonce, scope='sound'))
        self.assertNotIn('audio_route_main', self.page.car_audio_block(self.page.pairing.nonce, scope='sound'))
        saved = json.loads(self.page.options_file.read_text())
        self.assertEqual(saved['idle_release'], 300)
        self.assertTrue(saved['tesla'])
        self.assertNotIn('audio_route_main', saved, 'older per-category keys go with the next save')
        self.assertEqual(saved['audio_route_navigation'], 'carplay')
        self.page.apply_later.assert_called_with('receiver options changed')

    def test_an_older_viewer_asking_for_media_must_reload(self):
        old = self.page.sound_routes()
        for category in ('media', 'siri', 'calls'):
            with self.assertRaises(ValueError):
                self.page.apply('/audio-routes', {'nonce': self.page.pairing.nonce, 'category': category, 'destination': 'bluetooth'})
        self.assertEqual(self.page.sound_routes(), old)
        self.page.apply_later.assert_not_called()

    def test_bad_nonce_and_bad_destination_leave_settings_unchanged(self):
        old = self.page.sound_routes()
        for nonce, destination in [('bad', 'bluetooth'), (self.page.pairing.nonce, 'invalid')]:
            with self.assertRaises(ValueError):
                self.page.apply('/audio-routes', {'nonce': nonce, 'category': 'navigation', 'destination': destination})
        self.assertEqual(self.page.sound_routes(), old)
        self.page.apply_later.assert_not_called()

    def test_uci_and_viewer_share_the_switch(self):
        from unittest.mock import patch
        self.page.managed = {'by': 'OpenWrt'}
        config = {'car_music': '1', 'car_voice': '1', 'tesla_mode': '1', 'audio_route_main': 'carplay'}
        def write(routes):
            for name in ('main', 'media', 'alert', 'siri', 'calls'):
                config.pop('audio_route_' + name, None)
            config['audio_route_navigation'] = routes['navigation']
        with patch.object(audio_routes, 'read_uci', lambda: config), patch.object(audio_routes, 'write_uci', write):
            self.assertEqual(self.page.sound_routes(), audio_routes.grouped('bluetooth', 'carplay'), 'Tesla page behaviour')
            self.page.apply('/audio-routes', {'nonce': self.page.pairing.nonce, 'category': 'navigation', 'destination': 'bluetooth'})
            self.assertEqual(config, {'car_music': '1', 'car_voice': '1', 'tesla_mode': '1', 'audio_route_navigation': 'bluetooth'})
            config['audio_route_navigation'] = 'carplay'  # later LuCI save
            self.assertEqual(self.page.sound_routes()['navigation'], 'carplay')
        self.assertFalse(self.page.options_file.exists())

    def test_receiver_arguments_replace_previous_routes_without_changing_network_arguments(self):
        import json
        import supervisor
        import supervisor_openwrt
        options = {'audio_route_navigation': 'bluetooth'}
        routes = audio_routes.resolve(options)
        self.page.options_file.write_text(json.dumps(options))
        old = ['receiver', '--interface', 'wlan0', '--car-voice', '--idle-release', '30', '--audio-routes', 'old']
        args = supervisor.receiver_options(old, self.state)
        self.assertEqual(args[:3], ['receiver', '--interface', 'wlan0'])
        self.assertEqual(args.count('--audio-routes'), 1)
        self.assertNotIn('--car-voice', args)
        self.assertEqual(args[args.index('--idle-release') + 1], '300')
        self.assertEqual(audio_routes.parse(args[-1]), routes)
        uci = supervisor_openwrt.read_config('\n'.join("dashplay.main.%s='%s'" % pair for pair in options.items()))
        self.assertEqual(supervisor_openwrt.car_audio_args(uci), supervisor.car_audio_args(self.state))


class GroupingValidationTest(unittest.TestCase):
    def test_split_choices_are_refused(self):
        split = audio_routes.grouped('carplay'); split['siri'] = 'bluetooth'
        with self.assertRaises(ValueError): audio_routes.validate(split)
        split = audio_routes.grouped('carplay'); split['alert'] = 'bluetooth'
        with self.assertRaises(ValueError): audio_routes.validate(split)


if __name__ == '__main__':
    unittest.main()
