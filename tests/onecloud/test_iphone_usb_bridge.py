"""USB request layout of the wired-CarPlay helper: no device, fcntl.ioctl is mocked."""
import importlib.util
from pathlib import Path
import struct
import unittest
import tempfile
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'scripts/onecloud/iphone-usb-bridge.py'
spec = importlib.util.spec_from_file_location('iphone_usb_bridge', PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class UsbRequestTests(unittest.TestCase):
    def test_tether_unplug_reports_waiting_even_if_phone_returns_before_next_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / '1-1'
            device.mkdir()
            (device / 'devnum').write_text('8')
            def replug(_):
                (device / 'devnum').write_text('10')
            with patch.object(bridge, 'apple_device', side_effect=[device, device, None]), \
                    patch.object(bridge.time, 'sleep', side_effect=replug) as sleep, \
                    patch.object(bridge, 'log') as log:
                bridge.wait_for_unplug(device)
            self.assertEqual(sleep.call_count, 1)
            log.assert_any_call('usb waiting')

    def test_tether_unplug_reports_waiting_when_device_node_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / '1-1'
            with patch.object(bridge, 'apple_device', return_value=None), patch.object(bridge, 'log') as log:
                bridge.wait_for_unplug(device)
            log.assert_any_call('usb waiting')

    def test_tether_mux_claims_only_usbmux_and_never_starts_carplay_ncm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            device = root / '1-1'
            device.mkdir()
            (device / 'serial').write_text('test-phone')
            interface = root / '1-1:6.0'
            interface.mkdir()
            for name, value in [('bInterfaceClass', 'ff'), ('bInterfaceSubClass', 'fe'),
                                ('bInterfaceProtocol', '02'), ('bInterfaceNumber', '00')]:
                (interface / name).write_text(value)
            for address in ['01', '81']:
                endpoint = interface / ('ep_' + address)
                endpoint.mkdir()
                (endpoint / 'bEndpointAddress').write_text(address)
                (endpoint / 'type').write_text('Bulk')
            mark = root / 'tether'
            mark.touch()
            with patch.object(bridge, 'ROLE', root / 'no-role'), \
                    patch.object(bridge, 'USB_TETHER_MARK', mark), \
                    patch.dict(bridge.os.environ, {'CARPLAY_USB_TETHER_MUX': '1'}), \
                    patch.object(bridge, 'apple_device', return_value=device), \
                    patch.object(bridge, 'wait_device', return_value=device), \
                    patch.object(bridge, 'cable_carplay_off', return_value=True), \
                    patch.object(bridge, 'prepare_carplay', return_value=True), \
                    patch.object(bridge, 'carplay_configuration', return_value=device), \
                    patch.object(bridge, 'release_tethering'), patch.object(bridge, 'release_carplay_ncm'), \
                    patch.object(bridge, 'report_tether_interface'), patch.object(bridge, 'node', return_value='test-node'), \
                    patch.object(bridge.os, 'open', return_value=7), patch.object(bridge, 'claim') as claim, \
                    patch.object(bridge, 'ncm_functions') as ncm, patch.object(bridge, 'open_tap') as tap, \
                    patch.object(bridge.threading, 'Thread'), patch.object(bridge, 'log') as log, \
                    patch.object(bridge, 'relay_usbmux') as relay:
                bridge.main()
                claim.assert_called_once_with(7, device, 0)
                relay.assert_called_once_with(7, (0, 1, 129))
                ncm.assert_not_called()
                tap.assert_not_called()
                log.assert_any_call('tethermux ready')

    def test_large_ntb_is_read_whole_with_all_datagrams(self):
        frames = [bytes([i]) * 1400 for i in range(22)]
        offset = 128
        entries = b''.join(struct.pack('<HH', offset + i * 1400, 1400) for i in range(22))
        table = struct.pack('<4sHH', b'NCM0', 8 + len(entries) + 4, 0) + entries + b'\0' * 4
        header = struct.pack('<4sHHHH', b'NCMH', 12, 1, offset + 30800, 12)
        block = header + table + b'\0' * (offset - 12 - len(table)) + b''.join(frames)
        self.assertLess(len(block), bridge.NCM_READ_CHUNK)
        self.assertEqual(list(bridge.ntb_datagrams(block)), frames)
        self.assertNotEqual(list(bridge.ntb_datagrams(block[:16384])), frames)
        self.assertEqual(bridge.READ_CHUNK, 16384, 'usbmux read size remains unchanged')

    def test_ioctl_numbers_follow_the_cpu(self):
        # struct usbdevfs_ctrltransfer / usbdevfs_bulktransfer end in a user pointer: 16 bytes on the
        # OneCloud's armv7, 24 on the N1's aarch64 (2026-10-12: a 4-byte pointer failed there).
        size = 24 if struct.calcsize('P') == 8 else 16
        self.assertEqual(bridge.USBDEVFS_CONTROL, 0xC0005500 | size << 16)
        self.assertEqual(bridge.USBDEVFS_BULK, 0xC0005502 | size << 16)

    def test_requests_carry_the_full_buffer_address(self):
        seen = []
        def ioctl(fd, number, arg, *rest):
            seen.append((number, bytes(arg)))
            return 3
        with patch.object(bridge.fcntl, 'ioctl', side_effect=ioctl):
            self.assertEqual(len(bridge.control_in(7, 0xC0, 0x52, 0, 4, 1)), 1)
            self.assertEqual(bridge.bulk(7, 0x81, length=8), b'\0\0\0')
        (control, setup), (bulk, request) = seen
        self.assertEqual(len(setup), (control >> 16) & 0x3FFF)
        self.assertEqual(len(request), (bulk >> 16) & 0x3FFF)
        self.assertEqual(struct.unpack(bridge.CONTROL_STRUCT, setup)[:6], (0xC0, 0x52, 0, 4, 1, 1000))
        endpoint, length, timeout, address = struct.unpack(bridge.BULK_STRUCT, request)
        self.assertEqual((endpoint, length, timeout), (0x81, 8, 1000))
        self.assertNotEqual(address, 0)


if __name__ == '__main__':
    unittest.main()


class TetheringTests(unittest.TestCase):
    """options.json car_internet "usb_tether" (2026-10-30): the iPhone's USB hotspot interface stays bound."""
    def device(self, root):
        device = root / '1-1'
        iface = root / '1-1:6.2'
        (iface / 'driver').parent.mkdir(parents=True)
        for name, value in (('bInterfaceNumber', '02'), ('bInterfaceClass', 'ff'), ('bInterfaceSubClass', 'fd'),
                            ('bInterfaceProtocol', '01')):
            (iface / name).write_text(value)
        driver = root / 'drivers/ipheth'
        driver.mkdir(parents=True)
        (driver / 'unbind').write_text('')
        (iface / 'driver').symlink_to(driver)
        device.mkdir()
        return device, driver / 'unbind'

    def test_released_by_default_and_kept_when_the_owner_chose_usb_internet(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            device, unbind = self.device(Path(tmp))
            bridge.release_tethering(device, keep=True)
            self.assertEqual(unbind.read_text(), '')
            bridge.release_tethering(device, keep=False)
            self.assertEqual(unbind.read_text(), '1-1:6.2')

    def test_the_supervisors_marker_decides(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            device, unbind = self.device(Path(tmp))
            marker = Path(tmp) / 'usb-tether'
            with patch.object(bridge, 'USB_TETHER_MARK', marker):
                marker.write_text('on\n')
                bridge.release_tethering(device)
                self.assertEqual(unbind.read_text(), '')
                marker.unlink()
                bridge.release_tethering(device)
                self.assertEqual(unbind.read_text(), '1-1:6.2')

    def test_an_interface_released_earlier_is_bound_again(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            device, unbind = self.device(Path(tmp))
            (Path(tmp) / '1-1:6.2' / 'driver').unlink()   # an earlier session released it
            bind = Path(tmp) / 'bind'
            with patch.object(bridge, 'IPHETH_BIND', bind):
                bridge.release_tethering(device, keep=True)
            self.assertEqual(bind.read_text(), '1-1:6.2')


class CableCarPlayOffTests(unittest.TestCase):
    """No CarPlay on the cable (2026-10-31): the viewer picked a wireless row, or the owner chose wireless only.

    This is about CarPlay only. The receiver writes the marker (CableRole.kt) and starts the bridge again; the
    box's internet over the cable is the separate USB_TETHER_MARK. Before, CarPlay could only leave the cable
    after "car internet" was changed in the settings page, which had nothing to do with it.

    Configuration 4 (the iPhone's own hotspot configuration) has ipheth binding with the carrier stuck at 0
    (dp-0017, measured). Later replug reports also found configuration 6 without USB trust stuck at 0.
    The sharing-only path now authenticates through USBMUX, while leaving CarPlay NCM closed.
    """

    def test_the_receivers_marker_turns_the_cables_carplay_off(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'cable-carplay-off'
            env = {bridge.CABLE_OFF_ENV: str(marker)}
            self.assertFalse(bridge.cable_carplay_off(env))
            marker.write_text('once\n')
            self.assertTrue(bridge.cable_carplay_off(env))
            self.assertFalse(bridge.cable_carplay_off({}), 'no marker path: the cable always carries CarPlay')

    def test_charging_or_old_receiver_waits_for_unplug_without_claiming_usbmux(self):
        for share, capability in [(False, '1'), (True, '0')]:
            with self.subTest(share=share, capability=capability), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                device = root / '1-1'
                device.mkdir()
                (device / 'serial').write_text('test-phone')
                mark = root / 'tether'
                if share:
                    mark.touch()
                with patch.object(bridge, 'ROLE', root / 'no-role'), \
                        patch.object(bridge, 'USB_TETHER_MARK', mark), \
                        patch.dict(bridge.os.environ, {'CARPLAY_USB_TETHER_MUX': capability}), \
                        patch.object(bridge, 'apple_device', return_value=device), \
                        patch.object(bridge, 'wait_device', return_value=device), \
                        patch.object(bridge, 'cable_carplay_off', return_value=True), \
                        patch.object(bridge, 'carplay_configuration', return_value=device), \
                        patch.object(bridge, 'release_tethering') as tethering, \
                        patch.object(bridge, 'release_carplay_ncm') as release_ncm, \
                        patch.object(bridge, 'report_tether_interface'), \
                        patch.object(bridge, 'wait_for_unplug') as wait, \
                        patch.object(bridge, 'claim') as claim, patch.object(bridge, 'log') as log:
                    bridge.main()
                    log.assert_any_call('usb tether test-phone')
                    tethering.assert_called_once_with(device)
                    release_ncm.assert_called_once_with(device)
                    wait.assert_called_once_with(device)
                    claim.assert_not_called()

    def test_the_cables_carplay_does_not_decide_the_usb_internet(self):
        # The iPhone's USB internet interface is kept or released by release_tethering alone, before the
        # CarPlay question; without USB internet chosen, the cable only charges the iPhone.
        source = PATH.read_text()
        self.assertLess(source.index('    release_tethering(device)'), source.index('        report_tether_interface(device)'))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(bridge, 'USB_TETHER_MARK', Path(tmp) / 'usb-tether'), \
                 patch.object(bridge, 'IPHETH_BIND', Path(tmp) / 'bind'):
                bridge.report_tether_interface(Path(tmp) / '1-1')
                self.assertFalse((Path(tmp) / 'bind').exists(), 'no binding without USB internet chosen')

    def test_without_carplay_the_iphones_carplay_ncm_functions_are_released(self):
        # The USB internet profile matches cdc_ncm too (Android, MiFis); the iPhone's CarPlay NCM functions must
        # not ask for a lease, so they are let go. ipheth (ff/fd) is left as it is.
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = root / '1-2'
            device.mkdir()
            driver = root / 'drivers/cdc_ncm'
            driver.mkdir(parents=True)
            (driver / 'unbind').write_text('')
            for number, (cls, sub) in {3: ('02', '0d'), 2: ('ff', 'fd')}.items():
                path = root / f'1-2:6.{number}'
                path.mkdir()
                (path / 'bInterfaceClass').write_text(cls + '\n')
                (path / 'bInterfaceSubClass').write_text(sub + '\n')
                (path / 'bInterfaceNumber').write_text(f'0{number}\n')
                (path / 'bInterfaceProtocol').write_text('00\n')
                (path / 'driver').symlink_to(driver)
            bridge.release_carplay_ncm(device)
            self.assertEqual((driver / 'unbind').read_text(), '1-2:6.3')


class UsbPrepareTests(unittest.TestCase):
    def test_receiver_must_confirm_before_usb_configuration_can_continue(self):
        import io
        import threading
        preparing, release, finished = threading.Event(), threading.Event(), threading.Event()
        class Reply:
            def read(self, size):
                preparing.set()
                if not release.wait(2):
                    raise TimeoutError('test callback was not released')
                return b'RDY!'
        errors = []
        def run():
            try:
                bridge.prepare_carplay('test-phone', False, stream=Reply(),
                                       environ={'CARPLAY_USB_STATE_ACK': '1'})
                finished.set()
            except Exception as error:
                errors.append(error)
                preparing.set()
        with patch.object(bridge, 'log') as log:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(preparing.wait(1))
                self.assertEqual(errors, [])
                self.assertFalse(finished.is_set())
                log.assert_called_once_with('usb prepare test-phone')
            finally:
                release.set()
                worker.join(2)
            self.assertTrue(finished.is_set())

    def test_missing_or_invalid_receiver_reply_aborts_instead_of_switching_usb(self):
        import io
        for reply in (b'', b'nope'):
            with self.subTest(reply=reply), patch.object(bridge, 'log'):
                with self.assertRaises(RuntimeError):
                    bridge.prepare_carplay('test-phone', False, stream=io.BytesIO(reply),
                                           environ={'CARPLAY_USB_STATE_ACK': '1'})

    def test_old_receiver_and_tether_only_do_not_wait_for_new_handshake(self):
        import io
        for off, environ, state in [(False, {}, 'iphone'),
                                    (True, {'CARPLAY_USB_STATE_ACK': '1'}, 'tether')]:
            stream = io.BytesIO(b'unread')
            with patch.object(bridge, 'log') as log:
                bridge.prepare_carplay('test-phone', off, stream=stream, environ=environ)
                log.assert_called_once_with('usb ' + state + ' test-phone')
                self.assertEqual(stream.tell(), 0)

    def test_receiver_can_preserve_wireless_and_return_a_tether_only_cable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'cable-off'
            env = {'CARPLAY_USB_STATE_ACK': '1', bridge.CABLE_OFF_ENV: str(marker)}
            class Reply:
                def read(self, size):
                    marker.write_text('once\n')
                    return b'RDY!'
            with patch.object(bridge, 'log') as log:
                self.assertTrue(bridge.prepare_carplay('test-phone', False, stream=Reply(), environ=env))
                self.assertEqual([x.args[0] for x in log.call_args_list],
                                 ['usb prepare test-phone', 'usb tether test-phone'])
