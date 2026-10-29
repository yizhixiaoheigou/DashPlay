"""USB request layout of the wired-CarPlay helper: no device, fcntl.ioctl is mocked."""
import importlib.util
from pathlib import Path
import struct
import tempfile
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

    def test_wired_device_is_an_iphone_even_when_an_ipad_sorts_first(self):
        with tempfile.TemporaryDirectory() as root:
            bus = Path(root)
            self.write_usb(bus / '1-1', '05ac', '024f', 'Aluminium Keyboard')
            self.write_usb(bus / '1-2', '05ac', '12a8', 'iPad')
            self.write_usb(bus / '1-2:1.0', '05ac', '12a8', 'iPad')
            self.write_usb(bus / '2-1', '05ac', '12a8', 'iPhone')
            self.write_usb(bus / '3-1', '1234', '12a8', 'iPhone')
            with patch.object(bridge.glob, 'glob', return_value=[str(path) for path in sorted(bus.iterdir())]):
                self.assertEqual(bridge.apple_device(), bus / '2-1')
                (bus / '2-1').rename(bus / 'gone')
                self.assertIsNone(bridge.apple_device())

    def write_usb(self, path, vendor, product, name):
        path.mkdir()
        (path / 'idVendor').write_text(vendor + '\n')
        (path / 'idProduct').write_text(product + '\n')
        (path / 'product').write_text(name + '\n')


if __name__ == '__main__':
    unittest.main()
