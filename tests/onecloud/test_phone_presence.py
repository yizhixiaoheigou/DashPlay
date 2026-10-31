import importlib.util
import json
import socket
import struct
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
spec = importlib.util.spec_from_file_location('presence', Path(__file__).resolve().parents[2] / 'scripts/onecloud/phone-presence.py')
presence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(presence)


class PresenceTests(unittest.TestCase):
    def test_boot_grant_not_repeated_by_service_restart(self):
        state = {}
        self.assertTrue(presence.grant_boot(state, 'boot-one'))
        self.assertFalse(presence.grant_boot(state, 'boot-one'))
        self.assertTrue(presence.grant_boot(state, 'boot-two'))

    def test_invite_without_presence_falls_back_to_the_only_paired_phone(self):
        # A phone used on the USB cable only was never remembered as the last wireless user.
        self.assertEqual(presence.invite_phone({}, {'A'}), 'A')
        self.assertIsNone(presence.invite_phone({}, {'A', 'B'}))
        self.assertIsNone(presence.invite_phone({}, set()))
        self.assertEqual(presence.invite_phone({'last_phone': 'B'}, {'A', 'B'}), 'B')
        self.assertEqual(presence.invite_phone({'last_phone': 'C'}, {'A'}), 'A')   # C was removed

    def test_new_network_epoch_invites_the_last_phone_again(self):
        state = {}
        self.assertTrue(presence.grant_boot(state, presence.boot_key('boot-one', 'e1')))
        self.assertFalse(presence.grant_boot(state, presence.boot_key('boot-one', 'e1')))   # same receiver run
        self.assertTrue(presence.grant_boot(state, presence.boot_key('boot-one', 'e2')))    # network rebuilt
        self.assertEqual(presence.boot_key('boot-one'), 'boot-one')   # without a supervisor: per boot only

    def test_manual_pause_blocks_boot_selection_and_stale_pairing_grants(self):
        state = {'phones':{'A':{},'B':{}}, 'last_phone':'A', 'paused':['A']}
        self.assertEqual(presence.invite_phone(state, {'A','B'}), 'B')
        self.assertIsNone(presence.invite_phone(state, {'A'}))
        self.assertEqual(presence.choose_phone(state, {'A','B'}, {'A','B'}, newly_paired='A'), 'B')
        self.assertIsNone(presence.choose_phone(state, {'A'}, {'A'}, newly_paired='A'))

    def test_pause_persists_across_receiver_boot_and_explicit_switch_resumes(self):
        import os, tempfile, types
        phone = '02:00:00:00:00:01'
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'phone-presence.json'
            state.write_text(json.dumps({'phones':{phone:{}}, 'last_phone':phone}))
            paused = state.with_name('phone-paused.json')
            paused.write_text(json.dumps({'phones':[phone]}))
            env = {'CARPLAY_PRESENCE_STATE':str(state), 'CARPLAY_PRESENCE_INTERFACE':'test0'}
            with patch.dict(os.environ,env), patch.object(presence,'paired_cached',return_value={phone}), \
                    patch.object(presence,'get_platform',return_value=types.SimpleNamespace(boot_id=lambda:'new-boot')):
                with patch.object(presence.sys,'argv',['x','boot']):
                    self.assertEqual(presence.main(),1)
                with patch.object(presence.sys,'argv',['x','allowed',phone]):
                    self.assertEqual(presence.main(),1)
                with patch.object(presence.sys,'argv',['x','switch',phone]):
                    self.assertEqual(presence.main(),0)
                self.assertEqual(json.loads(paused.read_text())['phones'],[])
                with patch.object(presence.sys,'argv',['x','allowed',phone]):
                    self.assertEqual(presence.main(),0)

    def test_disconnect_request_is_scoped_to_current_receiver_and_paused_owner(self):
        import os, tempfile, types
        phone = '02:00:00:00:00:01'
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'phone-presence.json'
            state.write_text(json.dumps({'phones':{phone:{}},'boot':'epoch','active':phone}))
            state.with_name('phone-paused.json').write_text(json.dumps({'phones':[phone]}))
            request = state.with_name('phone-disconnect.json')
            request.write_text(json.dumps({'phone':phone,'boot':'old-epoch'}))
            with patch.dict(os.environ,{'CARPLAY_PRESENCE_STATE':str(state),'CARPLAY_PRESENCE_INTERFACE':'test0'}), \
                    patch.object(presence.sys,'argv',['x','disconnect-request']), \
                    patch.object(presence,'get_platform',return_value=types.SimpleNamespace(boot_id=lambda:'boot')):
                self.assertEqual(presence.main(),1)
                self.assertFalse(request.exists())
                request.write_text(json.dumps({'phone':phone,'boot':'epoch'}))
                with patch('builtins.print') as output:
                    self.assertEqual(presence.main(),0)
                output.assert_called_with(phone)
                self.assertEqual(json.loads(state.read_text())['active'],phone)

    def test_switch_request_names_the_phone_once_and_expires(self):
        import os, tempfile, time, types
        phone = '02:00:00:00:00:02'
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)/'phone-presence.json'
            state.write_text(json.dumps({'phones':{phone:{}},'boot':'epoch','active':None}))
            request = state.with_name('phone-switch.json')
            request.write_text(json.dumps({'phone':phone,'boot':'old-epoch','expires':time.monotonic()+30}))
            with patch.dict(os.environ,{'CARPLAY_PRESENCE_STATE':str(state),'CARPLAY_PRESENCE_INTERFACE':'test0'}), \
                    patch.object(presence.sys,'argv',['x','switch-request']), \
                    patch.object(presence,'get_platform',return_value=types.SimpleNamespace(boot_id=lambda:'boot')):
                self.assertEqual(presence.main(),1)   # another boot's request is dropped, not served
                self.assertFalse(request.exists())
                request.write_text(json.dumps({'phone':phone,'boot':'epoch','expires':time.monotonic()+30}))
                with patch('builtins.print') as output:
                    self.assertEqual(presence.main(),0)
                output.assert_called_with(phone)
                self.assertFalse(request.exists(), 'one switch per request')
                request.write_text(json.dumps({'phone':phone,'boot':'epoch','expires':time.monotonic()-1}))
                self.assertEqual(presence.main(),1)

    def test_reply_requires_arp_reply_to_our_interface_and_ip(self):
        local = bytes.fromhex('020000000001')
        phone = bytes.fromhex('020000000002')
        ip = '192.0.2.1'
        frame = (local + phone + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2) +
                 phone + socket.inet_aton('192.0.2.2') + local + socket.inet_aton(ip))
        self.assertEqual(presence.sender(frame, local, ip), ('192.0.2.2', phone.hex()))
        self.assertIsNone(presence.sender(frame, phone, ip))
        self.assertIsNone(presence.sender(frame, local, '192.0.2.3'))
        self.assertIsNone(presence.sender(frame[:20], local, ip))
        self.assertIsNone(presence.sender(frame[:20] + b'\x00\x01' + frame[22:], local, ip))

    def test_probe_rejects_another_device_reusing_the_phone_ip(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = [b'wrong', b'right']
        with patch.object(presence.socket, 'AF_PACKET', 17, create=True), \
             patch.object(presence.Path, 'read_text', return_value='02:00:00:00:00:01'), \
             patch.object(presence.socket, 'socket', return_value=sock), \
             patch.object(presence.select, 'select', side_effect=[([sock], [], []), ([sock], [], [])]), \
             patch.object(presence, 'sender', side_effect=[('192.0.2.2', '020000000099'), ('192.0.2.2', '020000000002')]):
            result = presence.probe('test0', '192.0.2.1', ['192.0.2.2'], '020000000002')
        self.assertEqual(result, {'ip': '192.0.2.2', 'mac': '020000000002'})
        self.assertEqual(sock.recv.call_count, 2)

    def test_request_is_link_local_broadcast_with_no_credentials(self):
        frame = presence.request(bytes.fromhex('020000000001'), '192.0.2.1', '192.0.2.2')
        self.assertEqual(len(frame), 42)
        self.assertEqual(frame[:6], b'\xff' * 6)
        self.assertEqual(frame[-4:], socket.inet_aton('192.0.2.2'))

class StateFileTests(unittest.TestCase):
    """A power cut (every drive ends with one) while the frequently rewritten state file was not on disk left it
    empty; every presence command then failed and no iPhone was invited again (review 2026-10-12)."""

    def setUp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'presence.json'

    def test_known_phones_survive_a_broken_state_file(self):
        state = {'phones': {'AA:BB:CC:DD:EE:FF': {'wifi': ['02:00:00:00:00:01']}}, 'last_phone': 'AA:BB:CC:DD:EE:FF', 'probe': 1}
        presence.save_state(self.path, state, lasting_before=json.dumps([state['phones'], state['last_phone']], sort_keys=True))
        self.path.write_text('')                       # what the power cut left
        loaded = presence.load_state(self.path)
        self.assertEqual(loaded['phones'], state['phones'])
        self.assertEqual(loaded['last_phone'], 'AA:BB:CC:DD:EE:FF')

    def test_garbage_or_nothing_starts_empty(self):
        self.path.write_text('{"phones": ')
        self.assertEqual(presence.load_state(self.path), {})
        self.path.unlink()
        self.assertEqual(presence.load_state(self.path), {})

class BluezNotReadyTests(unittest.TestCase):
    """Right after a restart the Bluetooth bus socket is not there yet: logged as an error (FileNotFoundError)
    though the iPhone cannot be invited before Bluetooth is up anyway (F50, 2026-10-12)."""

    def test_missing_bus_socket_is_a_note_not_an_error(self):
        import os, sys as system, tempfile, types
        fake = types.ModuleType('dbus_next'); aio = types.ModuleType('dbus_next.aio')
        class Bus:
            def __init__(self, **_): pass
            async def connect(self): raise FileNotFoundError(2, 'No such file or directory')
        fake.BusType = types.SimpleNamespace(SYSTEM=1); fake.Message = fake.MessageType = object; aio.MessageBus = Bus
        with patch.dict(system.modules, {'dbus_next': fake, 'dbus_next.aio': aio}):
            with self.assertRaises(presence.BluezNotReady):
                presence.asyncio.run(presence.paired_phones())
        with tempfile.TemporaryDirectory() as directory:
            state = os.path.join(directory, 'phone-presence.json')
            with patch.dict(os.environ, {'CARPLAY_PRESENCE_STATE': state}), patch.object(presence.sys, 'argv', ['x', 'admit']), \
                    patch.object(presence, 'main', side_effect=presence.BluezNotReady()):
                self.assertEqual(presence.entry(), 1)
            log = Path(directory, 'presence-errors.log').read_text()
        self.assertIn('admit BlueZ not ready yet', log)
        self.assertNotIn('Error', log)

    def test_a_renumbered_lan_finds_the_phone_by_its_known_wifi_address(self):
        # F50, 2026-10-14: the LAN moved from 192.168.77.0/24 to 100.96.0.0/24 (Tesla option); the iPhone kept its
        # private Wi-Fi address on the same Wi-Fi name but had no identity for the new subnet, so it was never invited.
        import os, tempfile, types
        phone, mac = '02:00:00:00:00:B1', '0200000000a1'
        address = json.dumps([{'addr_info': [{'scope': 'global', 'local': '100.96.0.1', 'prefixlen': 24}]}])
        probes = []

        def probe(interface, source, targets, expected_mac=None, collect=False):
            probes.append(list(targets))
            return {mac: '100.96.0.234'} if '100.96.0.234' in targets else {}

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory, 'phone-presence.json')
            state.write_text(json.dumps({'phones': {phone: {'networks': {'192.168.77.0/24': {'ip': '192.168.77.234', 'mac': mac}}}},
                                         'last_phone': phone}))
            with patch.dict(os.environ, {'CARPLAY_PRESENCE_STATE': str(state), 'CARPLAY_PRESENCE_INTERFACE': 'br-lan'}), \
                    patch.object(presence.sys, 'argv', ['x', 'select']), \
                    patch.object(presence.subprocess, 'check_output', return_value=address), \
                    patch.object(presence, 'paired_cached', return_value={phone}), \
                    patch.object(presence, 'get_platform', return_value=types.SimpleNamespace(boot_id=lambda: 'b')), \
                    patch.object(presence, 'probe', side_effect=probe), \
                    patch('builtins.print') as printed:
                self.assertEqual(presence.main(), 0)
            printed.assert_called_with(phone)
            self.assertNotIn('192.168.77.234', [ip for targets in probes for ip in targets], 'the old subnet is not probed')
            networks = json.loads(state.read_text())['phones'][phone]['networks']
        self.assertEqual(networks['100.96.0.0/24'], {'ip': '100.96.0.234', 'mac': mac})
        self.assertEqual(networks['192.168.77.0/24']['ip'], '192.168.77.234', 'the old identity is kept for going back')

if __name__ == '__main__':
    unittest.main()
