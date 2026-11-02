"""Exercise the production reader against the captured USB cancellation boundary."""
import ast
import errno
import io
import struct
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock


class ReaderEnded(BaseException):
    pass


class ReceiveTests(unittest.TestCase):
    def reader(self, bulk, stdout):
        source = Path(__file__).resolve().parents[2] / 'scripts/onecloud/iphone-usb-bridge.py'
        tree = ast.parse(source.read_text())
        function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'from_phone')
        namespace = dict(bulk=bulk, fd=7, in_ep=0x85, errno=errno, struct=struct,
                         stdout=stdout, stdout_lock=threading.Lock(), log=lambda _: None,
                         os=Mock(_exit=Mock(side_effect=ReaderEnded)))
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        return namespace['from_phone']

    def test_cancel_boundary_delivers_both_packets_in_order(self):
        # A timeout cancels the first 249-byte completion with 213 payload bytes.
        # The synchronous kernel error path cannot copy that packet to userspace.
        packets = [b'A' * 249, b'B' * 249]
        def bulk(fd, endpoint, timeout):
            if not packets:
                raise OSError(errno.ENODEV, 'removed')
            packet = packets.pop(0)
            if packet.startswith(b'A') and timeout:
                raise OSError(errno.ETIMEDOUT, 'cancelled with data')
            return packet
        out = io.BytesIO()
        with self.assertRaises(ReaderEnded):
            self.reader(bulk, out)()
        self.assertEqual(out.getvalue(), b''.join(struct.pack('>I', 249) + p * 249 for p in (b'A', b'B')))

    def test_device_errors_exit_instead_of_spinning(self):
        for code in (errno.ENODEV, errno.ESHUTDOWN):
            bulk = Mock(side_effect=OSError(code, 'device ended'))
            with self.assertRaises(ReaderEnded):
                self.reader(bulk, io.BytesIO())()
            bulk.assert_called_once()
