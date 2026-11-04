"""Real settings handler round trips explicit open networks without losing their security type."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import debug_uplink
import setup_wizard
import test_setup_flow as fixture

class DebugOpenSettingsTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SetupFlowTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.page,self.state=self.f.page,self.f.state
        self.page.run=self.state/"run";self.page.run.mkdir()
    def save(self, **kw):
        return self.page.apply_debug(dict(action='save',**kw))
    def test_saved_open_uplink_can_be_saved_for_debug_with_blank_password(self):
        setup_wizard.save_json(self.state/'uplink-hotspot.json',{'ssid':'Open','security':'open','passphrase':''})
        self.save(ssid_manual='Open',password='')
        self.assertEqual(debug_uplink.hotspot(self.state),{'ssid':'Open','security':'open','passphrase':''})
    def test_explicit_open_network_roundtrip_and_form(self):
        self.save(ssid_manual='Guest',security='open',password='')
        self.assertEqual(debug_uplink.hotspot(self.state)['security'],'open')
        self.assertIn('value=open selected',self.page.debug_block('synthetic-nonce'))
    def test_wpa_name_change_cannot_borrow_password(self):
        self.save(ssid_manual='Old',password='test-password')
        before=(self.state/debug_uplink.HOTSPOT).read_bytes()
        with self.assertRaises(ValueError):self.save(ssid_manual='New',password='')
        self.assertEqual((self.state/debug_uplink.HOTSPOT).read_bytes(),before)
        self.save(ssid_manual='Old',password='')
        self.assertEqual(debug_uplink.hotspot(self.state)['passphrase'],'test-password')
    def test_reject_invalid_type_and_ssid_without_overwriting(self):
        self.save(ssid_manual='Old',password='test-password')
        before=(self.state/debug_uplink.HOTSPOT).read_bytes()
        for values in [dict(ssid_manual='Bad\nName',security='open'),dict(ssid_manual='Old',security='unknown')]:
            with self.assertRaises(ValueError): self.save(**values)
            self.assertEqual((self.state/debug_uplink.HOTSPOT).read_bytes(),before)
