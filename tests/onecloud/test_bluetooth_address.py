import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).parents[2]/'scripts/onecloud'))
from bluetooth_address import BluetoothIdentity, select_address
from platform_debian import DebianPlatform

class BluetoothIdentityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name); self.state=self.root/'state'; self.bluez=self.root/'bluez'
        self.state.mkdir()
        self.old='02:00:00:00:00:11'; self.changed='02:00:00:00:00:22'; self.current=self.changed
        self.calls=[]

    def fake_run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[-2] == 'public-addr': self.current=argv[-1]
        text=('addr '+self.current if argv[-1]=='info' else 'Controller '+self.current+' (public)' if argv[-1]=='show' else '')
        return subprocess.CompletedProcess(argv,0,text,'')

    def bond(self, address):
        info=self.bluez/address/'02:00:00:00:00:33'/'info'; info.parent.mkdir(parents=True)
        info.write_text('[LinkKey]\n# synthetic fixture, no key\n')

    def test_existing_pairing_identity_wins_and_survives_three_new_boots(self):
        self.bond(self.old)
        for _ in range(3):
            self.current=self.changed
            identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
            identity.prepare()
            self.assertEqual(self.current,self.old)
            self.assertTrue(identity.verify())
        self.assertEqual(json.loads((self.state/'bluetooth-identity.json').read_text())['address'],self.old)
        self.assertEqual((self.state/'bluetooth-identity.json').stat().st_mode & 0o777,0o600)
        self.assertEqual(sum('public-addr' in c for c in self.calls),3)
        self.assertEqual(len(list(self.bluez.glob('*/02:00:00:00:00:33/info'))),1,'never copy pairing secrets')

    def test_unique_usb_adapter_keeps_address_and_detects_later_change(self):
        self.current=self.old
        identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
        identity.prepare(); self.assertFalse(any('public-addr' in c for c in self.calls))
        self.current=self.changed; self.assertFalse(identity.verify())
        BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None).prepare()
        self.assertEqual(self.current,self.old)

    def test_boot_auto_off_must_finish_before_changing_public_address(self):
        self.bond(self.old)
        # Linux can expose an apparently powered-off controller while HCI_UP is
        # still set during its boot auto-off window. An off request is then a
        # no-op; changing public-addr strands the controller in HCI_CONFIG.
        boot_auto_off = True
        physically_on = True
        def run(argv, **kwargs):
            nonlocal boot_auto_off, physically_on
            if argv[-2:] == ['power', 'on']:
                boot_auto_off, physically_on = False, True
            elif argv[-2:] == ['power', 'off'] and not boot_auto_off:
                physically_on = False
            elif 'public-addr' in argv:
                self.assertFalse(physically_on, 'must perform a real power-off before reconfiguration')
            return self.fake_run(argv, **kwargs)
        identity = BluetoothIdentity(self.state, self.bluez, run, lambda _: None)
        identity.prepare()
        self.assertTrue(identity.verify())
        self.assertEqual(json.loads((self.state/'bluetooth-identity.json').read_text())['address'], self.old)

    def test_ambiguous_or_corrupt_identity_never_silently_replaces_bonds(self):
        with self.assertRaises(ValueError): select_address(self.changed,None,{self.old,'02:00:00:00:00:44'},self.changed,set())
        with self.assertRaises(ValueError): select_address(self.changed,'invalid',set(),self.changed,set())

    def test_failed_write_and_post_power_change_block_wireless_startup(self):
        p=DebianPlatform('wlan-test',run=self.root); p.bluetooth_state=self.state
        with patch('bluetooth_address.BluetoothIdentity') as cls, patch('platform_debian.subprocess.run') as run, \
                patch('platform_debian.shutil.which', return_value='/usr/bin/btmgmt'):
            cls.return_value.prepare.side_effect=RuntimeError('not ready')
            self.assertFalse(p.power_bluetooth()); run.assert_not_called()
            cls.return_value.prepare.side_effect=None; cls.return_value.verify.return_value=False
            run.return_value=subprocess.CompletedProcess([],0,'','')
            self.assertFalse(p.power_bluetooth())
            self.assertFalse(any('system-alias' in c.args[0] for c in run.call_args_list))

    def test_without_btmgmt_the_adapter_still_powers_on(self):
        # F50: the board helper fixes the address at btattach and its private BlueZ puts only bluetoothctl on
        # PATH. 2026.10.15 waited for btmgmt every try and gave up after 60 s: wired only on a wireless-only box.
        p=DebianPlatform('wlan-test',run=self.root); p.bluetooth_state=self.state
        def run(argv, **kwargs):
            if argv[0] == 'btmgmt': raise FileNotFoundError(argv[0])
            return subprocess.CompletedProcess(argv,0,'','')
        with patch('platform_debian.shutil.which', side_effect=lambda name: None if name == 'btmgmt' else '/usr/bin/'+name), \
                patch('bluetooth_address.subprocess.run', side_effect=run), \
                patch('platform_debian.subprocess.run', side_effect=run) as calls:
            self.assertTrue(p.power_bluetooth())
        self.assertIn(['bluetoothctl', 'power', 'on'], [c.args[0] for c in calls.call_args_list])
        self.assertFalse((self.state/'bluetooth-identity.json').exists())

    def test_cached_public_address_success_does_not_mean_controller_changed(self):
        self.bond(self.old)
        cached = self.old  # Previous program configured this, but hardware still has changed.
        def mgmt(argv, **kwargs):
            nonlocal cached
            self.calls.append(argv)
            if argv[-2] == 'public-addr' and argv[-1] != cached:
                cached = argv[-1]
                self.current = cached
            text = ('addr '+self.current if argv[-1]=='info' else
                    'Controller '+self.current+' (public)' if argv[-1]=='show' else '')
            return subprocess.CompletedProcess(argv, 0, text, '')
        identity = BluetoothIdentity(self.state, self.bluez, mgmt, lambda _:None)
        identity.prepare()
        self.assertTrue(identity.verify())
        self.assertEqual([c[-1] for c in self.calls if 'public-addr' in c],
                         [self.old, self.changed, self.old])
        identity.prepare()
        self.assertEqual(sum('public-addr' in c for c in self.calls), 3)
        self.assertEqual(len(list(self.bluez.glob('*/*/info'))), 1)

    def test_controller_that_never_changes_fails_after_one_recovery(self):
        self.bond(self.old)
        def unchanged(argv, **kwargs):
            self.calls.append(argv)
            text = 'addr '+self.current if argv[-1]=='info' else 'Controller '+self.current+' (public)'
            return subprocess.CompletedProcess(argv, 0, text, '')
        identity = BluetoothIdentity(self.state, self.bluez, unchanged, lambda _:None)
        with self.assertRaises(RuntimeError): identity.prepare()
        self.assertEqual(sum('public-addr' in c for c in self.calls), 3)
        self.assertFalse(identity.verify())
        self.assertTrue((self.bluez/self.old/'02:00:00:00:00:33/info').exists())

    def test_btmgmt_failure_with_zero_exit_is_not_success(self):
        self.bond(self.old)
        def refused(argv, **kwargs):
            if argv[-2] == 'public-addr':
                return subprocess.CompletedProcess(argv, 0, 'hci0 Set Public Address failed with status 0x0b (Rejected)', '')
            return self.fake_run(argv, **kwargs)
        identity = BluetoothIdentity(self.state, self.bluez, refused, lambda _:None)
        with self.assertRaisesRegex(RuntimeError, 'management command failed'): identity.prepare()

    def history(self, name, address, timestamp, complete=True, peer='02:00:00:00:00:33'):
        p = self.state/'logs'/name/'receiver.log';p.parent.mkdir(parents=True)
        p.write_text('5 [wireless-bootstrap] INFO wireless - wireless bootstrap: iPhone='+peer+' hostBt='+address+' wifi=Example ch=149\n'
                     + 'boot 10.00s invitation accepted\n'
                     + ('12 [wireless-tunnel-control] INFO playport - wireless iAP2 tunnel ready; CarPlay is live\n' if complete else 'wireless bootstrap failed: iAP2 link ended\n'))
        os.utime(p, (timestamp, timestamp))

    def test_shared_boot_address_uses_last_successful_existing_pairing(self):
        self.bond(self.old);self.bond(self.changed)
        self.history('old-success', self.changed, 10)
        self.history('last-success', self.old, 20)
        self.history('current-failed', self.changed, 30, complete=False)
        with patch('platform_debian.SHARED_BLUETOOTH_ADDRESSES', {self.changed}):
            identity = BluetoothIdentity(self.state, self.bluez, self.fake_run, lambda _:None)
            identity.prepare()
        self.assertEqual(self.current, self.old)
        self.assertEqual(json.loads(identity.path.read_text())['address'], self.old)
        self.assertEqual(len(list(self.bluez.glob('*/*/info'))), 2)

    def test_multiple_shared_address_bonds_need_confirmed_matching_history(self):
        self.bond(self.old);self.bond(self.changed)
        for case in ['failed', 'other-phone', 'same-time']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                self.state=Path(temp)
                if case=='failed': self.history('a', self.old, 10, complete=False)
                if case=='other-phone': self.history('a', self.old, 10, peer='02:00:00:00:00:44')
                if case=='same-time':
                    self.history('a', self.old, 10);self.history('b', self.changed, 10)
                with patch('platform_debian.SHARED_BLUETOOTH_ADDRESSES', {self.changed}):
                    identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
                    with self.assertRaises(ValueError): identity.prepare()
                self.assertFalse(identity.path.exists())
        self.assertFalse(any('public-addr' in c for c in self.calls))

    def test_saved_target_overrides_later_history(self):
        self.bond(self.old);self.bond(self.changed)
        (self.state/'bluetooth-identity.json').write_text(json.dumps({'address':self.old}))
        self.history('newer',self.changed,20)
        identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
        identity.prepare();self.assertEqual(self.current,self.old)

    def test_bluez_delay_and_transient_index_removal_do_not_reset_identity(self):
        self.bond(self.old)
        info_failures = 0
        shown = self.changed
        def asynchronous(argv, **kwargs):
            nonlocal info_failures, shown
            if argv[-2]=='public-addr': info_failures=2
            elif argv[-1]=='info' and info_failures:
                info_failures-=1
                raise subprocess.CalledProcessError(1, argv, 'Invalid Index')
            if argv[-1]=='show':
                result=subprocess.CompletedProcess(argv,0,'Controller '+shown+' (public)','')
                shown=self.old
                return result
            return self.fake_run(argv,**kwargs)
        identity=BluetoothIdentity(self.state,self.bluez,asynchronous,lambda _:None)
        identity.prepare();self.assertTrue(identity.verify())
        self.assertEqual(sum('public-addr' in c for c in self.calls),1)

    def test_unique_current_controller_is_not_replaced_by_another_adapters_history(self):
        self.bond(self.old);self.bond(self.changed)
        self.history('other-adapter',self.old,20)
        identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
        identity.prepare();self.assertEqual(self.current,self.changed)
        self.assertFalse(any('public-addr' in c for c in self.calls))

    def test_wired_or_incomplete_history_cannot_authorize_migration(self):
        self.bond(self.old);self.bond(self.changed)
        for case in ['wired', 'missing-accept', 'missing-invite', 'failed-before-live']:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                self.state=Path(temp);self.history('a',self.old,10)
                p=self.state/'logs/a/receiver.log';text=p.read_text()
                if case=='wired':text=text.replace('[wireless-tunnel-control]','[wired-bootstrap]')
                if case=='missing-accept':text=text.replace('boot 10.00s invitation accepted\n','')
                if case=='missing-invite':text='\n'.join(text.splitlines()[1:])
                if case=='failed-before-live':text=text.replace('12 [wireless-tunnel-control]', 'wireless bootstrap failed: ended\n12 [wireless-tunnel-control]')
                p.write_text(text)
                with patch('platform_debian.SHARED_BLUETOOTH_ADDRESSES', {self.changed}):
                    identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
                    with self.assertRaises(ValueError):identity.prepare()
                self.assertFalse(identity.path.exists())

    def unconfigured_run(self, argv, **kwargs):
        """btmgmt on a Snapdragon 410 stick (btqcomsmd, no local-bd-address): unconfigured until public-addr."""
        self.calls.append(argv)
        if argv[-2] == 'public-addr':
            self.current = argv[-1]
        if argv[-1] == 'config':
            text = ('Unconfigured index list with 1 item\nhci0:\tUnconfigured controller\n\tmanufacturer 29\n'
                    '\tsupported options: public-address \n\tmissing options: public-address \n' if self.current is None
                    else 'Unconfigured index list with 0 items\n')
        elif argv[-1] == 'info':
            text = ('Reading info for index 0 failed with status 0x11 (Invalid Index)' if self.current is None
                    else 'addr ' + self.current)
        elif argv[-1] == 'show':
            text = 'Controller ' + self.current + ' (public)' if self.current else 'No default controller available'
        else:
            text = ''
        return subprocess.CompletedProcess(argv, 0, text, '')

    def test_controller_without_an_address_gets_this_boxes_own_and_keeps_it(self):
        derived = '02:00:00:00:00:44'
        with patch('platform_debian.machine_key', return_value='synthetic-cid'), \
                patch('platform_debian.derived_address', return_value=derived):
            for _ in range(2):   # it forgets the address at every boot
                self.current = None
                identity = BluetoothIdentity(self.state, self.bluez, self.unconfigured_run, lambda _: None)
                identity.prepare()
                self.assertEqual(self.current, derived)
                self.assertTrue(identity.verify())
        self.assertEqual(json.loads((self.state / 'bluetooth-identity.json').read_text())['address'], derived)
        self.assertEqual([c[-1] for c in self.calls if 'public-addr' in c], [derived, derived])

    def test_controller_without_an_address_returns_to_the_paired_identity(self):
        self.bond(self.old)
        self.current = None
        with patch('platform_debian.machine_key', return_value='synthetic-cid'), \
                patch('platform_debian.derived_address', return_value='02:00:00:00:00:44'):
            BluetoothIdentity(self.state, self.bluez, self.unconfigured_run, lambda _: None).prepare()
        self.assertEqual(self.current, self.old, 'the iPhone is paired with this one')
        self.current = None
        BluetoothIdentity(self.state, self.bluez, self.unconfigured_run, lambda _: None).prepare()
        self.assertEqual([c[-1] for c in self.calls if 'public-addr' in c][-1], self.old)
        self.assertEqual(len([c for c in self.calls if 'public-addr' in c]), 3, 'from the second boot the saved one at once')

    def test_btmgmt_without_config_reads_the_controller_as_before(self):
        self.current = self.old
        def no_config(argv, **kwargs):
            if argv[-1] == 'config':
                raise subprocess.CalledProcessError(1, argv, 'Invalid command')
            return self.fake_run(argv, **kwargs)
        identity = BluetoothIdentity(self.state, self.bluez, no_config, lambda _: None)
        identity.prepare()
        self.assertTrue(identity.verify())

    def test_unconfigured_controller_that_never_takes_the_address_blocks_wireless(self):
        def stuck(argv, **kwargs):
            result = self.unconfigured_run(argv, **kwargs)
            self.current = None
            return result
        self.current = None
        (self.state / 'bluetooth-identity.json').write_text('{"address":"02:00:00:00:00:44"}')
        with self.assertRaisesRegex(RuntimeError, 'did not take an address'):
            BluetoothIdentity(self.state, self.bluez, stuck, lambda _: None).prepare()

    def test_zero_saved_address_is_rejected_before_touching_controller(self):
        (self.state/'bluetooth-identity.json').write_text('{"address":"00:00:00:00:00:00"}')
        identity=BluetoothIdentity(self.state,self.bluez,self.fake_run,lambda _:None)
        with self.assertRaises(ValueError):identity.prepare()
        self.assertFalse(any('public-addr' in c for c in self.calls))
