"""USB request layout of the wired-CarPlay helper: no device, fcntl.ioctl is mocked."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'scripts/onecloud/iphone-usb-bridge.py'
spec = importlib.util.spec_from_file_location('iphone_usb_bridge', PATH)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class UsbRequestTests(unittest.TestCase):
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
    (dp-0017, measured); configuration 6 with only ipheth bound comes up. So the bridge still selects the
    CarPlay configuration and then stays out of usbmux/NCM instead of failing when they are absent.
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

    def test_without_carplay_the_bridge_says_tether_and_waits_for_the_unplug(self):
        # "usb tether" keeps the receiver from pausing wireless; returning after the unplug keeps it out of
        # usbmux/NCM (it used to fall through and fail with "CarPlay configuration incomplete").
        source = PATH.read_text()
        self.assertIn("log(f\"usb {'tether' if off else 'iphone'} {serial}\".strip())", source)
        self.assertIn('        wait_for_unplug(device)\n        return\n', source)

    def test_the_cables_carplay_does_not_decide_the_usb_internet(self):
        # The iPhone's USB internet interface is kept or released by release_tethering alone, before the
        # CarPlay question; without USB internet chosen, the cable only charges the iPhone.
        source = PATH.read_text()
        self.assertLess(source.index('    release_tethering(device)\n    if off:'), source.index('report_tether_interface(device)\n        wait_for_unplug'))
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
