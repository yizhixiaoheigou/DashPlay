"""Real private D-Bus + mock BlueZ + Unix socketpairs, NOT radio/iPhone tests."""
import asyncio
import contextlib
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import unittest

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

ROOT = Path(__file__).resolve().parents[2]
ENTRY = ROOT / 'linux/bt-bridge/bridge.py'
sys.path.insert(0, str(ENTRY.parent))
from bridge import ManagedBus
UUID = '00000000-deca-fade-deca-deafdecacafe'
PROFILE = '/com/playport/LinuxBluetoothBridge'
ADAPTER = '/org/bluez/hci0'
DEVICE = ADAPTER + '/dev_12_34_56_78_9A_BC'
ADDRESS = '12:34:56:78:9A:BC'


def objects():
    return {
        ADAPTER: {'org.bluez.Adapter1': {
            'Address': Variant('s', '02:00:00:00:00:01'), 'Powered': Variant('b', True)}},
        DEVICE: {'org.bluez.Device1': {
            'Address': Variant('s', ADDRESS), 'Alias': Variant('s', 'Test iPhone\nNot another device'),
            'Adapter': Variant('o', ADAPTER), 'Paired': Variant('b', True), 'Blocked': Variant('b', False)}},
    }


class BlueZTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.daemon = await asyncio.create_subprocess_exec(
            os.environ.get('DBUS_DAEMON_BINARY', 'dbus-daemon'),
            '--config-file=' + str(Path(__file__).with_name('session.conf')), '--nofork', '--print-address=1',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.address = (await self.daemon.stdout.readline()).decode().strip()
        self.bus = await ManagedBus(bus_address=self.address, negotiate_unix_fd=True).connect()
        await self.bus.request_name('org.bluez')
        self.data = objects()
        self.registered = None
        self.options = None
        self.disconnected = False
        self.unregistered = False
        self.mode = 'normal'
        self.powered = []
        self.device_disconnects = []
        self.peer = None
        self.connected = asyncio.Event()
        self.tasks = []
        self.processes = []
        self.bus.add_message_handler(self.handle)

    def handle(self, message):
        if message.message_type != MessageType.METHOD_CALL:
            return
        if message.member == 'GetManagedObjects':
            return Message.new_method_return(message, 'a{oa{sa{sv}}}', [self.data])
        if message.member == 'RegisterProfile':
            self.registered = message.sender
            self.options = message.body
            if self.mode == 'register-reject':
                return Message.new_error(message, 'org.bluez.Error.AlreadyExists', 'test duplicate')
            return Message.new_method_return(message)
        if message.member == 'UnregisterProfile':
            self.unregistered = True
            return Message.new_method_return(message)
        if message.member == 'DisconnectProfile':
            self.disconnected = True
            return Message.new_method_return(message)
        if message.member == 'Set' and self.mode in ('stale-once', 'held-request'):
            self.powered.append(message.body[2].value)
            return Message.new_method_return(message)
        if message.member == 'Disconnect' and message.interface == 'org.bluez.Device1':
            self.device_disconnects.append(message.path)
            return Message.new_method_return(message)
        if message.member == 'ConnectProfile':
            self.assertEqual(message.body, [UUID])
            if self.mode == 'stale-once' and not self.powered:
                return Message.new_error(message, 'org.bluez.Error.Failed', 'br-connection-already-connected')
            if self.mode == 'held-request' and not self.device_disconnects:
                # BlueZ still holds a pending connect/browse for the disconnected phone (N1, 2026-10-15).
                return Message.new_error(message, 'org.bluez.Error.InProgress', 'br-connection-busy')
            if self.mode == 'connect-reject':
                return Message.new_error(message, 'org.bluez.Error.NotAvailable', 'test missing SDP service')
            if self.mode == 'connect-hang':
                return True
            if self.mode != 'no-fd':
                self.tasks.append(asyncio.create_task(self.send_fd()))
            return Message.new_method_return(message)

    async def send_fd(self, device=DEVICE):
        channel, peer = socket.socketpair()
        peer.setblocking(False)
        try:
            reply = await self.bus.call(Message(destination=self.registered, path=PROFILE,
                interface='org.bluez.Profile1', member='NewConnection', signature='oha{sv}',
                body=[device, 0, {}], unix_fds=[channel.fileno()]))
        finally:
            channel.close()
        if reply.message_type == MessageType.ERROR:
            peer.close()
        else:
            self.peer = peer
            self.connected.set()
        return reply

    async def launch(self, *args, env_extra=None):
        env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=self.address, **(env_extra or {}))
        process = await asyncio.create_subprocess_exec(sys.executable, str(ENTRY), *args,
            env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self.processes.append(process)
        return process

    async def result(self, *args, env_extra=None):
        process = await self.launch(*args, env_extra=env_extra)
        out, err = await asyncio.wait_for(process.communicate(), 5)
        return process.returncode, out, err

    async def asyncTearDown(self):
        for process in self.processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.peer:
            self.peer.close()
        self.bus.disconnect()
        with contextlib.suppress(Exception):
            await self.bus.wait_for_disconnect()
        if self.daemon.returncode is None:
            self.daemon.terminate()
        await self.daemon.wait()

    async def test_metadata_and_alias_sanitization(self):
        code, out, err = await self.result('--print-address')
        self.assertEqual((code, out, err), (0, b'02:00:00:00:00:01\n', b''))
        code, out, err = await self.result('--list')
        self.assertEqual((code, out, err), (0, (ADDRESS+' Test iPhone Not another device\n').encode(), b''))

    async def test_unpaired_device_not_listed_or_connected(self):
        self.data[DEVICE]['org.bluez.Device1']['Paired'] = Variant('b', False)
        self.assertEqual(await self.result('--list'), (0, b'', b''))
        code, out, err = await self.result('--address', ADDRESS)
        self.assertEqual((code, out), (1, b''))
        self.assertIn(b'not paired', err)
        self.assertIsNone(self.registered)

    async def test_missing_and_ambiguous_adapter(self):
        self.data = {}
        code, out, err = await self.result('--print-address')
        self.assertEqual((code, out), (1, b''))
        self.assertIn(b'no BlueZ adapter', err)
        self.data = objects()
        self.data['/org/bluez/hci1'] = self.data[ADAPTER]
        self.assertEqual((await self.result('--print-address'))[0], 1)
        self.assertEqual((await self.result('--adapter', 'hci0', '--print-address'))[0], 0)

    async def test_powered_off(self):
        self.data[ADAPTER]['org.bluez.Adapter1']['Powered'] = Variant('b', False)
        code, out, err = await self.result('--address', ADDRESS)
        self.assertEqual((code, out), (1, b''))
        self.assertIn(b'powered off', err)
        self.assertIsNone(self.registered)

    async def test_ready_wait_covers_late_adapter_and_power(self):
        # Boot overlap: the receiver may start before bluetoothd lists the adapter or powers it on.
        self.data = {}
        process = await self.launch('--print-address', env_extra={'BT_BRIDGE_READY_WAIT': '5'})
        await asyncio.sleep(0.6)
        self.data = objects()
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual((process.returncode, out), (0, b'02:00:00:00:00:01\n'), err)
        self.data[ADAPTER]['org.bluez.Adapter1']['Powered'] = Variant('b', False)
        process = await self.launch('--address', ADDRESS, env_extra={'BT_BRIDGE_READY_WAIT': '5'})
        await asyncio.sleep(0.6)
        self.assertIsNone(self.registered)
        self.data[ADAPTER]['org.bluez.Adapter1']['Powered'] = Variant('b', True)
        await asyncio.wait_for(self.connected.wait(), 4)
        process.send_signal(signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 5)

    async def test_ready_wait_still_fails_when_never_ready(self):
        self.data[ADAPTER]['org.bluez.Adapter1']['Powered'] = Variant('b', False)
        code, out, err = await self.result('--address', ADDRESS, env_extra={'BT_BRIDGE_READY_WAIT': '0.5'})
        self.assertEqual((code, out), (1, b''))
        self.assertIn(b'powered off', err)
        self.assertIsNone(self.registered)

    async def test_invalid_arguments_do_not_open_connection(self):
        for args in [('--address', 'bad'), ('--timeout', 'nan', '--list'), ('--list', '--print-address')]:
            with self.subTest(args=args):
                code, out, err = await self.result(*args)
                self.assertEqual((code, out), (2, b''))
                self.assertIsNone(self.registered)

    async def test_dbus_error_and_timeout(self):
        for mode in ('register-reject', 'connect-reject', 'no-fd', 'connect-hang'):
            with self.subTest(mode=mode):
                self.mode = mode
                code, out, err = await self.result('--timeout', '0.3', '--address', ADDRESS)
                self.assertEqual((code, out), (1, b''), err)
                self.assertNotIn(b'Traceback', err)
                if mode != 'register-reject':
                    self.assertTrue(self.unregistered)
                    self.assertTrue(self.disconnected)

    async def test_real_fd_binary_roundtrip_and_sigterm(self):
        process = await self.launch('--address', ADDRESS)
        await asyncio.wait_for(self.connected.wait(), 3)
        self.assertEqual(self.options[:2], [PROFILE, UUID])
        options = self.options[2]
        self.assertEqual(options['Role'].value, 'client')
        self.assertTrue(options['RequireAuthentication'].value)
        self.assertFalse(options['AutoConnect'].value)
        self.assertNotIn('Channel', options)
        payload = bytes(range(256))*4096
        loop = asyncio.get_running_loop()
        async def send():
            process.stdin.write(payload)
            await process.stdin.drain()
        async def receive():
            data = bytearray()
            while len(data) < len(payload):
                part = await loop.sock_recv(self.peer, 3107)
                if not part:
                    break
                data.extend(part)
            return bytes(data)
        _, received = await asyncio.wait_for(asyncio.gather(send(), receive()), 5)
        self.assertEqual(received, payload)
        _, received = await asyncio.wait_for(asyncio.gather(
            loop.sock_sendall(self.peer, payload), process.stdout.readexactly(len(payload))), 5)
        self.assertEqual(received, payload)
        process.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual((process.returncode, out), (143, b''), err)
        self.assertNotIn(b'Traceback', err)
        self.assertTrue(self.disconnected)
        self.assertTrue(self.unregistered)

    async def test_stale_link_is_recovered_and_retried_in_place(self):
        self.mode = 'stale-once'
        self.data[DEVICE]['org.bluez.Device1']['Connected'] = Variant('b', True)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        stamp = Path(directory.name) / 'recovery.stamp'
        process = await self.launch('--address', ADDRESS, env_extra={'BT_BRIDGE_RECOVERY_STATE': str(stamp)})
        await asyncio.wait_for(self.connected.wait(), 5)
        self.assertEqual(self.powered, [False, True])
        process.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertIn(b'retrying after stale-link recovery', err)
        self.assertIn(b'RFCOMM connected', err)
        self.assertNotIn(b'Traceback', err)

    async def test_held_request_is_cancelled_without_power_cycle_or_unpairing(self):
        self.mode = 'held-request'
        self.data[DEVICE]['org.bluez.Device1']['Connected'] = Variant('b', False)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        stamp = Path(directory.name) / 'recovery.stamp'
        process = await self.launch('--address', ADDRESS, env_extra={'BT_BRIDGE_RECOVERY_STATE': str(stamp)})
        await asyncio.wait_for(self.connected.wait(), 5)
        self.assertEqual(self.device_disconnects, [DEVICE])
        self.assertEqual(self.powered, [])
        process.send_signal(signal.SIGTERM)
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertIn(b'retrying after cancelling the stale request', err)
        self.assertIn(b'RFCOMM connected', err)
        self.assertNotIn(b'Traceback', err)

    async def test_held_request_without_opt_in_still_fails_plainly(self):
        self.mode = 'held-request'
        code, out, err = await self.result('--address', ADDRESS)
        self.assertEqual((code, out), (1, b''))
        self.assertIn(b'br-connection-busy', err)
        self.assertEqual((self.device_disconnects, self.powered), ([], []))

    async def test_timeout_clears_pending_request_before_next_invitation(self):
        self.mode = 'connect-hang'
        self.data[DEVICE]['org.bluez.Device1']['Connected'] = Variant('b', False)
        with tempfile.TemporaryDirectory() as directory:
            env = {'BT_BRIDGE_RECOVERY_STATE': str(Path(directory) / 'recovery')}
            code, out, err = await self.result('--timeout', '0.3', '--address', ADDRESS, env_extra=env)
            self.assertEqual(code, 1)
            self.assertIn(b'RFCOMM connection timed out', err)
            self.assertEqual(self.device_disconnects, [DEVICE])
            self.assertEqual(self.powered, [])
            self.assertTrue(self.data[DEVICE]['org.bluez.Device1']['Paired'].value)
            self.mode = 'held-request'
            process = await self.launch('--address', ADDRESS, env_extra=env)
            await asyncio.wait_for(self.connected.wait(), 3)
            process.stdin.close()
            out, err = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, err)
            self.assertNotIn(b'retrying', err)

    async def test_timeout_does_not_cancel_during_pairing_or_discovery(self):
        self.mode = 'connect-hang'
        self.data[DEVICE]['org.bluez.Device1']['Connected'] = Variant('b', False)
        with tempfile.TemporaryDirectory() as directory:
            for key in ('Pairable', 'Discoverable', 'Discovering'):
                with self.subTest(key=key):
                    self.data[ADAPTER]['org.bluez.Adapter1'][key] = Variant('b', True)
                    code, out, err = await self.result('--timeout', '0.3', '--address', ADDRESS,
                        env_extra={'BT_BRIDGE_RECOVERY_STATE': str(Path(directory) / 'recovery')})
                    self.assertEqual(code, 1)
                    self.assertIn(b'RFCOMM connection timed out', err)
                    self.assertEqual((self.device_disconnects, self.powered), ([], []))
                    self.data[ADAPTER]['org.bluez.Adapter1'][key] = Variant('b', False)

    async def test_peer_eof_and_stdin_eof(self):
        for mode in ('peer', 'stdin'):
            with self.subTest(mode=mode):
                self.connected.clear()
                process = await self.launch('--address', ADDRESS)
                await asyncio.wait_for(self.connected.wait(), 3)
                if mode == 'peer':
                    self.peer.shutdown(socket.SHUT_WR)
                else:
                    process.stdin.close()
                code = await asyncio.wait_for(process.wait(), 5)
                self.assertEqual(code, 0, await process.stderr.read())
                self.peer.close()
                self.peer = None

    async def test_bluez_disappearance(self):
        process = await self.launch('--address', ADDRESS)
        await asyncio.wait_for(self.connected.wait(), 3)
        await self.bus.release_name('org.bluez')
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual((process.returncode, out), (1, b''))
        self.assertIn(b'BlueZ disappeared', err)
        self.assertNotIn(b'Traceback', err)

    async def test_bluez_requested_disconnect_closes_fd(self):
        process = await self.launch('--address', ADDRESS)
        await asyncio.wait_for(self.connected.wait(), 3)
        reply = await self.bus.call(Message(destination=self.registered, path=PROFILE,
            interface='org.bluez.Profile1', member='RequestDisconnection', signature='o', body=[DEVICE]))
        self.assertEqual(reply.message_type, MessageType.METHOD_RETURN)
        self.assertEqual(await asyncio.wait_for(asyncio.get_running_loop().sock_recv(self.peer, 1), 1), b'')
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual((process.returncode, out), (1, b''))
        self.assertNotIn(b'Traceback', err)

    async def test_spoofed_sender_cannot_disconnect(self):
        process = await self.launch('--address', ADDRESS)
        await asyncio.wait_for(self.connected.wait(), 3)
        outsider = await ManagedBus(bus_address=self.address, negotiate_unix_fd=True).connect()
        try:
            reply = await outsider.call(Message(destination=self.registered, path=PROFILE,
                interface='org.bluez.Profile1', member='RequestDisconnection', signature='o', body=[DEVICE]))
            self.assertEqual(reply.message_type, MessageType.ERROR)
            self.assertIsNone(process.returncode)
        finally:
            outsider.disconnect()
            await outsider.wait_for_disconnect()
        process.stdin.close()
        self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)

    async def test_duplicate_fd_is_rejected(self):
        process = await self.launch('--address', ADDRESS)
        await asyncio.wait_for(self.connected.wait(), 3)
        reply = await self.send_fd()
        self.assertEqual(reply.message_type, MessageType.ERROR)
        process.stdin.close()
        self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)

    @unittest.skipUnless(os.environ.get('JAVA_BRIDGE_TEST_CLASSPATH'), 'optional JVM integration classpath not configured')
    async def test_actual_playport_process_byte_stream(self):
        env = dict(os.environ, DBUS_SYSTEM_BUS_ADDRESS=self.address, BT_BRIDGE_PYTHON=sys.executable)
        process = await asyncio.create_subprocess_exec(
            os.environ['JAVA_BRIDGE_TEST_BIN'], '-cp', os.environ['JAVA_BRIDGE_TEST_CLASSPATH'],
            'JvmBridgeProbe', str(ENTRY.with_name('bt-bridge')), '--address', ADDRESS,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.processes.append(process)
        await asyncio.wait_for(self.connected.wait(), 4)
        loop = asyncio.get_running_loop()
        async def echo():
            count = 0
            while count < 131072:
                data = await loop.sock_recv(self.peer, 1701)
                if not data:
                    break
                await loop.sock_sendall(self.peer, data)
                count += len(data)
            self.assertEqual(count, 131072)
        _, (out, err) = await asyncio.wait_for(asyncio.gather(echo(), process.communicate()), 10)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(out, b'PASS JVM-BlueZ bridge binary roundtrip\n')

    async def test_bus_disappearance(self):
        process = await self.launch('--address', ADDRESS)
        await asyncio.wait_for(self.connected.wait(), 3)
        self.daemon.terminate()
        await self.daemon.wait()
        out, err = await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual((process.returncode, out), (1, b''))
        self.assertRegex(err, b'D-Bus disconnected|BlueZ disappeared')
        self.assertNotIn(b'Traceback', err)

    async def test_wrong_device_callback_rejected(self):
        self.mode = 'no-fd'
        process = await self.launch('--timeout', '1', '--address', ADDRESS)
        for _ in range(100):
            if self.registered:
                break
            await asyncio.sleep(0.01)
        reply = await self.send_fd('/org/bluez/hci0/dev_00_00_00_00_00_00')
        self.assertEqual(reply.message_type, MessageType.ERROR)
        out, err = await asyncio.wait_for(process.communicate(), 4)
        self.assertEqual((process.returncode, out), (1, b''))
        self.assertNotIn(b'Traceback', err)

if __name__ == '__main__':
    unittest.main()
