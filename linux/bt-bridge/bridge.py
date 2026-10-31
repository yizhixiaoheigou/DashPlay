"""BlueZ iAP2 RFCOMM client for PlayPort. stdout is exclusively the data plane."""
import argparse
import asyncio
import contextlib
import fcntl
import time
import math
import os
import re
import signal
import sys
import threading

from dbus_next import BusType, DBusError, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method
from relay import relay

UUID = '00000000-deca-fade-deca-deafdecacafe'
PROFILE = '/com/playport/LinuxBluetoothBridge'
ADAPTER = 'org.bluez.Adapter1'
DEVICE = 'org.bluez.Device1'
MANAGER = 'org.bluez.ProfileManager1'
MAC = re.compile(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\Z')


class ManagedBus(MessageBus):
    # dbus-next 0.2.3 disconnects but does not close its makefile/socket.
    # Keep this compatibility fix local to the pinned dependency.
    def disconnect(self):
        if not self._disconnected:
            super().disconnect()

    def _finalize(self, err=None):
        if self._disconnected:
            return
        try:
            super()._finalize(err)
        finally:
            self._stream.close()
            self._sock.close()


class BridgeError(Exception):
    pass


def address(value):
    if not MAC.fullmatch(value):
        raise argparse.ArgumentTypeError('expected Bluetooth address AA:BB:CC:DD:EE:FF')
    return value.upper()


def seconds(value):
    result = float(value)
    if not math.isfinite(result) or not 0.1 <= result <= 120:
        raise argparse.ArgumentTypeError('timeout must be between 0.1 and 120 seconds')
    return result


def properties(objects, path, interface):
    return {key: value.value for key, value in objects.get(path, {}).get(interface, {}).items()}


def choose_adapter(objects, requested):
    candidates = [path for path, interfaces in objects.items() if ADAPTER in interfaces]
    if requested:
        candidates = [path for path in candidates if path == requested or path.rsplit('/', 1)[-1] == requested]
    if not candidates:
        raise BridgeError('no BlueZ adapter found; check the F50 driver and bluetoothd')
    if len(candidates) != 1:
        raise BridgeError('multiple adapters found; set BT_BRIDGE_ADAPTER or --adapter')
    return candidates[0]


class Profile(ServiceInterface):
    def __init__(self, target):
        super().__init__('org.bluez.Profile1')
        self.target = target
        self.ready = asyncio.get_running_loop().create_future()
        self.closed = asyncio.Event()
        self.reason = None
        self.fd = None
        self.worker = None
        self.stop_thread = threading.Event()

    @method()
    def NewConnection(self, device: 'o', fd: 'h', options: 'a{sv}'):
        if device != self.target or self.fd is not None or self.closed.is_set():
            os.close(fd)
            raise DBusError('org.bluez.Error.Rejected', 'unexpected or duplicate connection')
        self.fd = fd
        self.ready.set_result(fd)

    @method()
    async def RequestDisconnection(self, device: 'o'):
        if device != self.target:
            raise DBusError('org.bluez.Error.Rejected', 'unexpected device')
        self.reason = 'BlueZ requested disconnection'
        self.closed.set()
        await self.close_channel()

    @method()
    async def Release(self):
        self.reason = 'BlueZ released the profile'
        self.closed.set()
        await self.close_channel()

    async def close_channel(self):
        self.stop_thread.set()
        if self.worker:
            with contextlib.suppress(Exception):
                await self.worker
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


async def call(bus, path, interface, member, signature='', body=None, timeout=15, destination='org.bluez'):
    reply = await asyncio.wait_for(bus.call(Message(
        destination=destination, path=path, interface=interface, member=member,
        signature=signature, body=body or [],
    )), timeout)
    if reply.message_type == MessageType.ERROR:
        raise BridgeError(f'{member}: {reply.error_name}: {reply.body[0] if reply.body else ""}')
    return reply.body


STALE_RETRY_DELAY = 1.0   # seconds for BlueZ to settle after the adapter powers back on


async def recover_stale_link(bus, adapter, target):
    """Opt-in recovery for a dedicated receiver's stuck BlueZ connection, rate-limited across helpers."""
    state = os.environ.get('BT_BRIDGE_RECOVERY_STATE')
    if not state:
        return False
    # A configured private stamp opts this device into adapter recovery. Never affect another
    # connected device or an active discovery/pairing operation on a shared adapter.
    fd = os.open(state, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'r+') as stamp:
        try:
            fcntl.flock(stamp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        previous = stamp.read().strip()
        now = time.monotonic()
        if previous:
            try:
                age = now - float(previous)
            except ValueError:
                return False
            if 0 <= age < 120:
                return False
        objects = (await call(bus, '/', 'org.freedesktop.DBus.ObjectManager',
                              'GetManagedObjects', timeout=5))[0]
        props = properties(objects, target, DEVICE)
        controller = properties(objects, adapter, ADAPTER)
        if (not props.get('Paired') or props.get('Blocked') or not props.get('Connected') or
                props.get('Adapter') != adapter or not controller.get('Powered') or controller.get('Discovering')):
            return False
        if any(path != target and properties(objects, path, DEVICE).get('Adapter') == adapter and
               properties(objects, path, DEVICE).get('Connected') for path in objects):
            return False
        stamp.seek(0); stamp.truncate(); stamp.write(str(now)); stamp.flush()
        print('bt-bridge: recovering stale dedicated-adapter connection; pairing retained', file=sys.stderr)
        try:
            await call(bus, adapter, 'org.freedesktop.DBus.Properties', 'Set', 'ssv',
                       [ADAPTER, 'Powered', Variant('b', False)], 5)
        finally:
            # Restore power even when powering down fails or the caller is cancelled.
            await call(bus, adapter, 'org.freedesktop.DBus.Properties', 'Set', 'ssv',
                       [ADAPTER, 'Powered', Variant('b', True)], 5)
        return True


async def clear_stale_request(bus, adapter, target):
    """Opt-in: cancel a request BlueZ still holds for the disconnected phone; the pairing stays.

    BlueZ answers InProgress br-connection-busy while it keeps a pending connect, SDP browse or
    service queue for the device. With nothing connected that never clears by itself (N1, 2026-10-15:
    every invitation failed until Device1.Disconnect). On a disconnected device Disconnect only
    cancels those (BlueZ 5.82 device_request_disconnect); it is skipped while pairing or scanning.
    """
    if not os.environ.get('BT_BRIDGE_RECOVERY_STATE'):
        return False
    objects = (await call(bus, '/', 'org.freedesktop.DBus.ObjectManager',
                          'GetManagedObjects', timeout=5))[0]
    props = properties(objects, target, DEVICE)
    controller = properties(objects, adapter, ADAPTER)
    if (not props.get('Paired') or props.get('Blocked') or props.get('Connected') or
            props.get('Adapter') != adapter or not controller.get('Powered') or controller.get('Discovering') or
            controller.get('Pairable') or controller.get('Discoverable')):
        return False
    print('bt-bridge: BlueZ holds a stale request for the disconnected phone; cancelling it, pairing retained',
          file=sys.stderr)
    await call(bus, target, DEVICE, 'Disconnect', timeout=5)
    return True


async def connect(bus, objects, adapter, args, stopped, owner):
    if not properties(objects, adapter, ADAPTER).get('Powered'):
        raise BridgeError('adapter is powered off; enable it explicitly before connecting')
    targets = [path for path in objects if
               properties(objects, path, DEVICE).get('Adapter') == adapter and
               properties(objects, path, DEVICE).get('Address', '').upper() == args.address]
    if len(targets) != 1 or not properties(objects, targets[0], DEVICE).get('Paired'):
        raise BridgeError('device is not paired on this adapter; pair using bluetoothctl first')
    target = targets[0]
    if properties(objects, target, DEVICE).get('Blocked'):
        raise BridgeError('device is blocked in BlueZ')
    profile = Profile(target)
    registered = attempted = False
    worker = None
    tasks = []

    def messages(message):
        if message.message_type == MessageType.METHOD_CALL and message.path == PROFILE:
            rejected = message.sender != owner
            if message.member == 'NewConnection':
                rejected = rejected or (message.signature != 'oha{sv}' or
                    len(message.body) != 3 or len(message.unix_fds) != 1 or
                    message.body[0] != target or
                    profile.fd is not None or profile.closed.is_set())
            if message.member == 'RequestDisconnection':
                rejected = rejected or message.signature != 'o' or message.body != [target]
            if rejected:
                for fd in message.unix_fds:
                    os.close(fd)
                return Message.new_error(message, 'org.bluez.Error.Rejected', 'unexpected caller, device or connection')
        if (message.message_type == MessageType.SIGNAL and
                message.sender == 'org.freedesktop.DBus' and
                message.interface == 'org.freedesktop.DBus' and
                message.member == 'NameOwnerChanged' and
                message.body[0] == 'org.bluez' and message.body[1] == owner):
            profile.reason = 'BlueZ disappeared or restarted'
            profile.closed.set()

    bus.add_message_handler(messages)
    bus.export(PROFILE, profile)
    try:
        await call(bus, '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'AddMatch', 's', [
            "type='signal',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.bluez'"
        ], args.timeout, 'org.freedesktop.DBus')
        await call(bus, '/org/bluez', MANAGER, 'RegisterProfile', 'osa{sv}', [PROFILE, UUID, {
            'Name': Variant('s', 'PlayPort iAP2 client'),
            'Role': Variant('s', 'client'),
            'RequireAuthentication': Variant('b', True),
            'AutoConnect': Variant('b', False),
        }], args.timeout)
        registered = True
        async def open_channel():
            nonlocal attempted
            attempted = True
            try:
                await call(bus, target, DEVICE, 'ConnectProfile', 's', [UUID], args.timeout + 1)
            except BridgeError as error:
                if 'br-connection-busy' in str(error) and await clear_stale_request(bus, adapter, target):
                    print('bt-bridge: retrying after cancelling the stale request', file=sys.stderr)
                elif (any(code in str(error) for code in ('br-connection-already-connected', 'br-connection-busy'))
                        and await recover_stale_link(bus, adapter, target)):
                    # The adapter was power-cycled: retry here instead of failing the whole bootstrap,
                    # which cost ~11 s of supervisor back-off on a returning phone (2026-10-11).
                    print('bt-bridge: retrying after stale-link recovery', file=sys.stderr)
                else:
                    raise
                await asyncio.sleep(STALE_RETRY_DELAY)
                await call(bus, target, DEVICE, 'ConnectProfile', 's', [UUID], args.timeout + 1)
            return await profile.ready
        opening = asyncio.create_task(open_channel())
        closed = asyncio.create_task(profile.closed.wait())
        signaled = asyncio.create_task(stopped.wait())
        async def watch_disconnect():
            await asyncio.shield(bus.wait_for_disconnect())
        disconnected = asyncio.create_task(watch_disconnect())
        tasks = [opening, closed, signaled, disconnected]
        done, _ = await asyncio.wait(tasks, timeout=args.timeout, return_when=asyncio.FIRST_COMPLETED)
        if signaled in done:
            return
        if closed in done or disconnected in done:
            raise BridgeError(profile.reason or 'D-Bus disconnected')
        if opening not in done:
            opening.cancel()
            await asyncio.gather(opening, return_exceptions=True)
            # A timed-out ConnectProfile can leave a pending BlueZ request even though the
            # phone is disconnected. Clear that now, so the next normal invitation need not
            # fail with br-connection-busy first. Keep the existing connected-link fallback.
            if not await clear_stale_request(bus, adapter, target):
                await recover_stale_link(bus, adapter, target)
            raise BridgeError('RFCOMM connection timed out')
        fd = opening.result()
        print('bt-bridge: RFCOMM connected', file=sys.stderr)
        worker = profile.worker = asyncio.create_task(asyncio.to_thread(relay, fd, 0, 1, profile.stop_thread))
        done, _ = await asyncio.wait([worker, closed, signaled, disconnected], return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            worker.result()
        elif signaled not in done:
            raise BridgeError(profile.reason or 'D-Bus disconnected')
    finally:
        profile.closed.set()
        await profile.close_channel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if attempted:
            with contextlib.suppress(Exception):
                await call(bus, target, DEVICE, 'DisconnectProfile', 's', [UUID], 2)
        if registered:
            with contextlib.suppress(Exception):
                await call(bus, '/org/bluez', MANAGER, 'UnregisterProfile', 'o', [PROFILE], 2)
        bus.unexport(PROFILE)
        bus.remove_message_handler(messages)


def ready_wait():
    """BT_BRIDGE_READY_WAIT seconds (0-120) to wait for D-Bus/BlueZ/adapter during boot; default 0."""
    try:
        value = float(os.environ.get('BT_BRIDGE_READY_WAIT', '0'))
    except ValueError:
        return 0.0
    return value if math.isfinite(value) and 0 <= value <= 120 else 0.0


async def attach(args):
    """Return bus, BlueZ owner, objects and adapter, retrying while Bluetooth is still starting."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ready_wait()
    while True:
        bus = None
        try:
            bus = await asyncio.wait_for(ManagedBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect(), args.timeout)
            owner = (await call(bus, '/org/freedesktop/DBus', 'org.freedesktop.DBus', 'GetNameOwner',
                                's', ['org.bluez'], args.timeout, 'org.freedesktop.DBus'))[0]
            objects = (await call(bus, '/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects', timeout=args.timeout))[0]
            adapter = choose_adapter(objects, args.adapter)
            if args.address and not properties(objects, adapter, ADAPTER).get('Powered'):
                raise BridgeError('adapter is powered off; enable it explicitly before connecting')
            return bus, owner, objects, adapter
        except (BridgeError, OSError, asyncio.TimeoutError, DBusError):
            if bus is not None:
                bus.disconnect()
                with contextlib.suppress(Exception):
                    await bus.wait_for_disconnect()
            if loop.time() >= deadline:
                raise
            await asyncio.sleep(0.25)


async def run(args):
    bus, owner, objects, adapter = await attach(args)
    stopped = asyncio.Event()
    received_signal = []
    loop = asyncio.get_running_loop()
    def stop(number):
        received_signal.append(number)
        stopped.set()
    for number in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(number, stop, number)
    try:
        if args.print_address:
            value = properties(objects, adapter, ADAPTER).get('Address', '')
            if not MAC.fullmatch(value):
                raise BridgeError('BlueZ returned an invalid adapter address')
            print(value.upper())
        elif args.list:
            entries = []
            for path in objects:
                props = properties(objects, path, DEVICE)
                value = props.get('Address', '')
                if props.get('Adapter') == adapter and props.get('Paired') and MAC.fullmatch(value):
                    name = ' '.join(str(props.get('Alias') or props.get('Name') or '').split())
                    entries.append(f'{value.upper()} {name}')
            for entry in sorted(entries):
                print(entry)
        else:
            await connect(bus, objects, adapter, args, stopped, owner)
        return 128 + received_signal[0] if received_signal else 0
    finally:
        bus.disconnect()
        with contextlib.suppress(Exception):
            await bus.wait_for_disconnect()
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(number)


def main():
    parser = argparse.ArgumentParser(description='Linux BlueZ iAP2 bridge; diagnostics go to stderr')
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--print-address', action='store_true')
    modes.add_argument('--list', action='store_true')
    modes.add_argument('--address', type=address)
    parser.add_argument('--adapter', default=os.environ.get('BT_BRIDGE_ADAPTER'))
    parser.add_argument('--timeout', type=seconds, default=os.environ.get('BT_BRIDGE_CONNECT_TIMEOUT', '15'))
    args = parser.parse_args()
    try:
        return asyncio.run(run(args))
    except (BridgeError, OSError, asyncio.TimeoutError, DBusError) as error:
        print(f'bt-bridge: {error or "D-Bus operation timed out"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
