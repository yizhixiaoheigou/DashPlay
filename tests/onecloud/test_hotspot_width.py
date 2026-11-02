import test_phone_uplink as fixtures
from unittest import TestCase
from unittest.mock import call
import json


class UsbWidthTests(TestCase):
    setUp = fixtures.PhoneUplinkTests.setUp
    boot = fixtures.PhoneUplinkTests.boot

    def test_auto_tries_80_then_40_and_reports_the_actual_width(self):
        self.u.share = True
        self.p.default_route_devices.return_value = ['usb0']
        self.p.start_ap.side_effect = [False, True]
        self.boot()
        self.assertEqual([c.args[1] for c in self.p.configure_hotspot.call_args_list], [80, 40])
        self.assertEqual(self.status['hotspot_width'], 40)
        self.p.n_only_ap.assert_not_called()

    def test_owner_can_keep_20_or_40_and_40_falls_back_when_unavailable(self):
        for wanted, working in [('20', 20), ('40', 40), ('40', 20)]:
            with self.subTest(wanted=wanted, working=working):
                self.setUp()
                (self.state / 'options.json').write_text(json.dumps({'usb_hotspot_width': wanted}))
                self.u.share = True
                self.p.default_route_devices.return_value = ['usb0']
                self.p.start_ap.side_effect = [False, True] if int(wanted) != working else [True]
                self.boot()
                self.assertEqual(self.p.configure_hotspot.call_args_list[0].args[1], int(wanted))
                self.assertEqual(self.status['hotspot_width'], working)

    def test_same_channel_wireless_fallback_narrows_before_association_then_usb_restores(self):
        self.u.share = True
        self.p.default_route_devices.return_value = ['usb0']
        self.boot()
        self.p.reset_mock()
        self.p.default_route_devices.return_value = []
        self.u.connect(5745)
        self.assertEqual(self.status['hotspot_width'], 20)
        self.p.n_only_ap.assert_called_once()
        calls = self.p.mock_calls
        self.assertLess(next(i for i,c in enumerate(calls) if c[0] == 'configure_hotspot'),
                        next(i for i,c in enumerate(calls) if c[0] == 'start_phone_station'))
        self.p.default_route_devices.return_value = ['usb0']
        self.u.tick()
        self.assertEqual(self.status['hotspot_width'], 80)
        count = self.p.configure_hotspot.call_count
        self.now += 40
        self.u.tick()
        self.assertEqual(self.p.configure_hotspot.call_count, count, 'no rebuild loop')

    def test_usb_without_saved_phone_hotspot_can_use_wide_ap(self):
        self.u.share = True
        self.u.saved = None
        self.p.default_route_devices.return_value = ['usb0']
        self.boot()
        self.assertEqual(self.status['hotspot_width'], 80)
        self.p.default_route_devices.return_value = []
        self.now += 40
        self.u.tick()
        self.p.phone_hotspot_frequency.assert_not_called()

    def test_failed_widening_does_not_restart_carplay_on_every_cable_check(self):
        self.u.share = True
        self.p.default_route_devices.return_value = ['usb0']
        self.boot()
        self.p.default_route_devices.return_value = []
        self.u.connect(5745)
        self.p.start_ap.side_effect = [False, False, False, True]
        self.p.default_route_devices.return_value = ['usb0']
        self.u.tick()
        self.assertEqual(self.status['hotspot_width'], 20)
        count = self.p.configure_hotspot.call_count
        self.now += 60
        self.u.tick()
        self.assertEqual(self.p.configure_hotspot.call_count, count)

    def test_channel_165_stays_20_even_in_auto(self):
        self.u.share = True
        self.u.channel = self.u.default_channel = 165
        self.p.default_route_devices.return_value = ['usb0']
        self.boot()
        self.assertEqual(self.status['hotspot_width'], 20)
