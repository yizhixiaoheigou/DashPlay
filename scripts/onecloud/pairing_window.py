"""Bounded, operator-confirmed incoming pairing; never accepts a passkey automatically."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
from platform_api import get_platform
import sys
import threading
import time

PHONE = re.compile(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}')
GRANT_SECONDS = 90
# An open window waits up to its timeout (180 s) for someone to start pairing; each attempt (a phone picked
# on the page, or a phone's own request) then gets ATTEMPT_SECONDS from when it begins, within
# WINDOW_LIMIT_SECONDS in all. A retry late in the window was cut off while the iPhone was confirming (F50, 2026-10-19).
ATTEMPT_SECONDS = 120
WINDOW_LIMIT_SECONDS = 600


def attempt_deadline(deadline, now, opened):
    """The window's end once a pairing attempt begins at `now`: never earlier than before, never past the limit."""
    return max(deadline, min(now + ATTEMPT_SECONDS, opened + WINDOW_LIMIT_SECONDS))


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temporary.replace(path)


class PairingWindow:
    def __init__(self, root):
        self.root = root
        self.directory = root / 'state/receiver/pairing-window'
        self.status_file = self.directory / 'status.json'
        self.confirm_file = self.directory / 'confirm.json'
        self.pick_file = self.directory / 'pick.json'
        self.process = None
        self.lock = threading.Lock()
        self.nonce = secrets.token_urlsafe(24)

    def status(self):
        if self.process:
            status = read_json(self.status_file)
            if self.process.poll() is None or status.get('phase') == 'paired':
                return status
        return {'phase': 'closed'}

    def in_use_message(self):
        """Why a phone cannot be added while another one is using CarPlay, or None.

        Shared with the settings page, which shows this text where the add button would be (2026-10-31): the
        same sentence whether it is read before tapping or comes back as an error after a POST.
        """
        active = self.presence().get('active')
        if not active:
            return None
        name = self.devices().get(active, f'iPhone（蓝牙 …{active[-5:]}）')
        return (f'「{name}」正在使用 CarPlay。要换手机，在下面点另一部的「切换到这部手机」；'
                '要添加新手机，先断开或切换。')

    def require_idle(self):
        message = self.in_use_message()
        if message:
            raise ValueError(message)

    def start(self):
        with self.lock:
            self.require_idle()
            if self.process and self.process.poll() is None:
                return
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.confirm_file.unlink(missing_ok=True)
            self.pick_file.unlink(missing_ok=True)
            save_json(self.status_file, {'phase': 'starting'})
            # One bounded worker log per window, private and replaced on the next attempt.
            fd = os.open(self.directory / 'worker.log', os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as log:
                self.process = subprocess.Popen(['python3', str(self.root / 'pair-phone.py'), '--root', str(self.root),
                                                 '--incoming', '--timeout', '180'],
                                                stdout=subprocess.DEVNULL, stderr=log)

    def confirm(self, request_id, accepted):
        with self.lock:
            status = self.status()
            if status.get('phase') != 'confirm' or not request_id or status.get('request_id') != request_id:
                raise ValueError('配对请求已变化，请刷新页面后重新核对。')
            save_json(self.confirm_file, {'request_id': request_id, 'accepted': accepted})

    def pick(self, address):
        """Box-initiated pairing with a phone seen while its Bluetooth settings page is open."""
        with self.lock:
            status = self.status()
            self.require_idle()
            if status.get('phase') != 'waiting' or address not in {d['address'] for d in status.get('found', [])}:
                raise ValueError('这部手机已不在列表中，请刷新页面。')
            save_json(self.pick_file, {'address': address})

    def presence(self):
        state = read_json(self.root / 'state/receiver/phone-presence.json')
        # Read the durable pause list directly: the receiver's snapshot may lag a page action.
        state['paused'] = read_json(self.root / 'state/receiver/phone-paused.json').get('phones', [])
        return state

    def pending_phone(self):
        """A bounded invitation, not proof that Bluetooth or CarPlay is already connected."""
        request = read_json(self.root / 'state/receiver/pairing-request.json')
        if not request.get('phone'):
            return None
        try:
            if request.get('boot') == get_platform().boot_id() and time.monotonic() < request['expires']:
                return request.get('phone')
        except (KeyError, TypeError, OSError):
            pass
        return None

    def switch_pending(self):
        """The phone a settings-page switch is still waiting for, or None.

        The receiver consumes the request within a couple of seconds; until then nothing else on the page shows
        that a switch is under way, so the page stayed still until a manual reload (2026-10-31).
        """
        request = read_json(self.root / 'state/receiver/phone-switch.json')
        phone = str(request.get('phone') or '').upper()
        if phone and time.monotonic() < request.get('expires', 0):
            return phone
        return None

    def devices(self):
        """Paired, unblocked BlueZ devices as {address: name}; empty when BlueZ is unavailable."""
        import asyncio   # only the settings page and pairing need it; keeps it off the boot path
        try:
            return asyncio.run(asyncio.wait_for(paired_devices(self.root), 3))
        except Exception:
            return {}

    def _pause_phone(self, phone, paused):
        """Caller holds the presence lock; shared with the receiver's presence helper."""
        path = self.root / 'state/receiver/phone-paused.json'
        phones = set(read_json(path).get('phones', []))
        if paused:
            phones.add(phone)
        else:
            phones.discard(phone)
        save_json(path, {'phones': sorted(phones)})
        with path.open('rb') as saved:
            os.fsync(saved.fileno())

    def _disconnect(self, phone):
        state_path = self.root / 'state/receiver/phone-presence.json'
        with state_path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = read_json(state_path)
            self._pause_phone(phone, True)
            grant = state_path.with_name('pairing-request.json')
            if read_json(grant).get('phone') == phone:
                grant.unlink(missing_ok=True)
            if state.get('active') == phone:
                save_json(state_path.with_name('phone-disconnect.json'),
                          {'phone':phone, 'boot':state.get('boot')})
        # The receiver closes the actual session and clears active. Never delete a bond on a timeout.
        for _ in range(40):
            if self.presence().get('active') != phone:
                return
            time.sleep(.25)
        raise ValueError('接收端尚未完成断开，配对记录已保留。请稍后重试。')

    def disconnect(self, phone):
        with self.lock:
            if not PHONE.fullmatch(phone) or phone not in self.devices():
                raise ValueError('这部手机已不在已配对列表中，请刷新页面。')
            self._disconnect(phone)

    def use(self, phone):
        """Give one already-paired phone the next bounded invitation, e.g. on a new hotspot."""
        with self.lock:
            if self.presence().get('active'):
                raise ValueError('当前有人使用 CarPlay，不会自动抢占；请对方先断开 Wi-Fi 或 CarPlay。')
            if not PHONE.fullmatch(phone) or phone not in self.devices():
                raise ValueError('这部手机未配对，请先添加新手机。')
            state_path = self.root / 'state/receiver/phone-presence.json'
            with state_path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self._pause_phone(phone, False)
                save_json(state_path.with_name('pairing-request.json'), {
                    'phone': phone, 'boot': get_platform().boot_id(),
                    'expires': time.monotonic() + GRANT_SECONDS})

    def switch_to(self, phone):
        """Hand CarPlay to another paired phone from the settings page, even while one is using it (2026-10-31).

        The receiver does the switch itself (ends that session or the cable's CarPlay, then invites this phone),
        through the same path as tapping its row in the CarPlay box settings; here we only write the request it
        polls. The request is one-shot: the helper deletes it when the receiver asks.
        """
        with self.lock:
            if not PHONE.fullmatch(phone) or phone not in self.devices():
                raise ValueError('这部手机已不在已配对列表中，请刷新页面。')
            if phone == self.presence().get('active'):
                return
            state_path = self.root / 'state/receiver/phone-presence.json'
            with state_path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                # The presence state carries its own boot key (boot id plus network epoch); the receiver's
                # switch-request check compares against that, not the bare boot id (2026-10-31: the first
                # version wrote the bare id, so every request was dropped as another boot's).
                boot = read_json(state_path).get('boot', '')
                save_json(state_path.with_name('phone-switch.json'), {
                    'phone': phone, 'boot': boot, 'expires': time.monotonic() + 30})

    def remove(self, phone):
        """Forget a phone on the box too: BlueZ pairing, Wi-Fi identities, back-off and any grant."""
        with self.lock:
            if not PHONE.fullmatch(phone) or phone not in self.devices():
                raise ValueError('这部手机已不在已配对列表中，请刷新页面。')
            self._disconnect(phone)
            import asyncio
            asyncio.run(asyncio.wait_for(remove_device(self.root, phone), 5))
            state_path = self.root / 'state/receiver/phone-presence.json'
            with state_path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = read_json(state_path)
                self._pause_phone(phone, False)
                state.get('phones', {}).pop(phone, None)
                state.get('declined', {}).pop(phone, None)
                if state.get('last_phone') == phone:
                    state.pop('last_phone')
                if phone in state.get('paired', []):
                    state['paired'].remove(phone)
                save_json(state_path, state)
            grant = self.root / 'state/receiver/pairing-request.json'
            if read_json(grant).get('phone') == phone:
                grant.unlink(missing_ok=True)
            os.sync()

    def reset_all(self):
        """First-run reset: forget every phone on the box, whoever is using it."""
        self.stop()
        with self.lock:
            import asyncio
            for phone in self.devices():
                asyncio.run(asyncio.wait_for(remove_device(self.root, phone), 5))
            state_path = self.root / 'state/receiver/phone-presence.json'
            with state_path.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                # Keep only the boot marker, so the reset does not grant another boot-time invitation.
                save_json(state_path, {key: value for key, value in read_json(state_path).items() if key == 'boot'})
            (self.root / 'state/receiver/pairing-request.json').unlink(missing_ok=True)
            os.sync()

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


async def remove_device(root, phone):
    sys.path.insert(0, str(root / 'linux-bt-bridge/deps'))
    from dbus_next import BusType
    from dbus_next.aio import MessageBus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        node = await bus.introspect('org.bluez', '/')
        manager = bus.get_proxy_object('org.bluez', '/', node).get_interface('org.freedesktop.DBus.ObjectManager')
        suffix = '/dev_' + phone.replace(':', '_')
        for path, interfaces in (await manager.call_get_managed_objects()).items():
            if path.endswith(suffix) and 'org.bluez.Device1' in interfaces:
                adapter_path = path[:-len(suffix)]
                node = await bus.introspect('org.bluez', adapter_path)
                adapter = bus.get_proxy_object('org.bluez', adapter_path, node).get_interface('org.bluez.Adapter1')
                await adapter.call_remove_device(path)
    finally:
        bus.disconnect()


async def paired_devices(root):
    sys.path.insert(0, str(root / 'linux-bt-bridge/deps'))
    from dbus_next import BusType
    from dbus_next.aio import MessageBus
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        node = await bus.introspect('org.bluez', '/')
        manager = bus.get_proxy_object('org.bluez', '/', node).get_interface('org.freedesktop.DBus.ObjectManager')
        result = {}
        for interfaces in (await manager.call_get_managed_objects()).values():
            props = interfaces.get('org.bluez.Device1', {})
            if props.get('Paired') and props['Paired'].value and not (props.get('Blocked') and props['Blocked'].value):
                address = props['Address'].value.upper()
                result[address] = props['Alias'].value if 'Alias' in props else address
        return result
    finally:
        bus.disconnect()


def is_phone(props):
    """Major device class Phone (iPhones report 0x7a020c); hides headsets, TVs and computers."""
    value = props.get('Class')
    return value is not None and (value.value >> 8) & 0x1f == 2


async def run_incoming(root, timeout):
    """One bounded pairing window.

    Either direction ends in the same numeric comparison: the box pairs with a phone picked on the
    page (verified path: iPhones are discoverable while Settings > Bluetooth is open), or a phone
    that lists the box initiates. Every passkey still needs explicit confirmation on the page.
    """
    import asyncio
    import signal
    from dbus_next import BusType, Variant
    from dbus_next.aio import MessageBus
    from dbus_next.errors import DBusError
    from dbus_next.service import ServiceInterface, method

    os.umask(0o077)
    directory = root / 'state/receiver/pairing-window'
    status_file, confirm_file, pick_file = directory / 'status.json', directory / 'confirm.json', directory / 'pick.json'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopped.set)
    loop.add_signal_handler(signal.SIGINT, stopped.set)
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    selected = None
    confirmed = False
    pairing = None
    error = ''
    error_code = ''
    attempt_generation = 0
    opened = time.monotonic()
    deadline = opened + min(timeout, 180)

    def trace(event, code=''):
        # No passkey, device address/name, exception message or Bluetooth key in diagnostics.
        code = str(code).rsplit('.', 1)[-1]
        print(json.dumps({'event': event, 'elapsed': round(time.monotonic() - opened, 3),
                          'code': code if re.fullmatch(r'[A-Za-z_]{1,64}', code) else ''}),
              file=sys.stderr, flush=True)

    def busy():
        return bool(read_json(root / 'state/receiver/phone-presence.json').get('active'))

    async def proxy(path, interface):
        node = await bus.introspect('org.bluez', path)
        return bus.get_proxy_object('org.bluez', path, node).get_interface(interface)

    object_manager = await proxy('/', 'org.freedesktop.DBus.ObjectManager')
    objects = await object_manager.call_get_managed_objects()
    adapters = [path for path, interfaces in objects.items() if 'org.bluez.Adapter1' in interfaces]
    if len(adapters) != 1:
        bus.disconnect()
        raise RuntimeError('Expected a dedicated adapter')
    adapter_path = adapters[0]
    adapter = await proxy(adapter_path, 'org.bluez.Adapter1')
    props = await proxy(adapter_path, 'org.freedesktop.DBus.Properties')
    previous = {key:(await props.call_get('org.bluez.Adapter1', key)).value for key in ('Pairable','Discoverable')}

    class Agent(ServiceInterface):
        def __init__(self):
            super().__init__('org.bluez.Agent1')

        @method()
        async def RequestConfirmation(self, device: 'o', passkey: 'u'):
            nonlocal selected, confirmed, deadline, attempt_generation
            if (not device.startswith(adapter_path + '/dev_') or stopped.is_set() or
                    (selected is not None and selected != device) or busy()):
                raise DBusError('org.bluez.Error.Rejected', 'Receiver busy')
            if selected is None:
                attempt_generation += 1
            generation = attempt_generation
            selected = device
            deadline = attempt_deadline(deadline, time.monotonic(), opened)
            request_id = secrets.token_urlsafe(20)
            trace('confirmation_requested')
            device_props = await proxy(device, 'org.bluez.Device1')
            name = await device_props.get_alias()
            if generation != attempt_generation or stopped.is_set():
                raise DBusError('org.bluez.Error.Rejected', 'Pairing attempt ended')
            save_json(status_file, {'phase':'confirm', 'name':name, 'passkey':f'{passkey:06d}', 'request_id':request_id})
            while time.monotonic() < deadline and not stopped.is_set() and generation == attempt_generation:
                reply = read_json(confirm_file)
                if reply.get('request_id') == request_id:
                    if reply.get('accepted') is True:
                        confirmed = True
                        trace('box_confirmed')
                        save_json(status_file, {'phase':'finishing'})
                        return
                    break
                await asyncio.sleep(0.25)
            if generation == attempt_generation:
                selected = None
            raise DBusError('org.bluez.Error.Rejected', 'Passkey not confirmed')

        @method()
        def RequestAuthorization(self, device: 'o'):
            raise DBusError('org.bluez.Error.Rejected', 'Numeric confirmation required')

        @method()
        def AuthorizeService(self, device: 'o', uuid: 's'):
            trace('authorize_iap2' if uuid.lower() == '00000000-deca-fade-deca-deafdecacafe' else 'authorize_other_service')
            if device != selected or not confirmed or uuid.lower() != '00000000-deca-fade-deca-deafdecacafe':
                raise DBusError('org.bluez.Error.Rejected', 'Unexpected service')

        @method()
        def Cancel(self):
            nonlocal selected, confirmed, error, error_code, attempt_generation
            # BlueZ has its own confirmation timeout. Invalidate the prompt immediately; a delayed
            # coroutine/reply from this attempt must not confirm or clear the next phone's request.
            attempt_generation += 1
            trace('bluez_canceled')
            selected, confirmed = None, False
            error_code = 'Canceled'
            error = '配对请求已取消或超时。请重新发起，并及时在手机和盒子页面确认相同数字。'
            save_json(status_file, {'phase':'waiting', 'found':[], 'error':error, 'error_code':error_code})

        @method()
        def Release(self):
            stopped.set()

    async def pair(path):
        nonlocal selected, confirmed, error, error_code
        generation = attempt_generation
        try:
            await (await proxy(path, 'org.bluez.Device1')).call_pair()
        except Exception as exc:
            trace('pair_failed', getattr(exc, 'type', type(exc).__name__))
            if generation != attempt_generation:
                return
            try:
                done = await (await proxy(path, 'org.bluez.Device1')).get_paired()
            except Exception:
                done = False
            if generation != attempt_generation:
                return
            if not done:
                # A failed Pair call does not tell us that the phone failed to confirm: the box's
                # agent may itself have timed out. Keep the safe BlueZ code, never raw exception text.
                code = str(getattr(exc, 'type', '')).removeprefix('org.bluez.Error.')
                error_code = code if code in {'AuthenticationTimeout', 'AuthenticationCanceled',
                    'AuthenticationRejected', 'AuthenticationFailed', 'ConnectionAttemptFailed',
                    'InProgress', 'Failed'} else 'Failed'
                if error_code == 'AuthenticationTimeout':
                    error = '蓝牙配对确认超时。请重新发起，并及时在手机和盒子页面确认相同数字。'
                else:
                    error = '蓝牙配对未完成或已取消。请保持手机停在蓝牙页面，重试时在手机和盒子页面都确认相同数字。'
                selected, confirmed = None, False
                save_json(status_file, {'phase': 'waiting', 'found': [], 'error': error, 'error_code': error_code})
                confirm_file.unlink(missing_ok=True)

    agent_path = '/onecloud/incoming_pair_agent'
    manager = await proxy('/org/bluez', 'org.bluez.AgentManager1')
    bus.export(agent_path, Agent())
    registered = discovering = False
    try:
        await manager.call_register_agent(agent_path, 'DisplayYesNo')
        registered = True
        await manager.call_request_default_agent(agent_path)
        for key in previous:
            # A Barrot USB dongle (33fa:0001) answers the first change with "Authentication Failed"
            # and accepts the next one (2026-10-12).
            for attempt in range(5):
                try:
                    await props.call_set('org.bluez.Adapter1', key, Variant('b', True))
                    break
                except DBusError:
                    await asyncio.sleep(1)   # the failed change may still be "in progress"
                    if (await props.call_get('org.bluez.Adapter1', key)).value:
                        break
                    if attempt == 4:
                        raise
        for attempt in range(5):   # same dongle: "Operation already in progress" right after the above
            try:
                await adapter.call_start_discovery()
                break
            except DBusError:
                await asyncio.sleep(1)
                if (await props.call_get('org.bluez.Adapter1', 'Discovering')).value:
                    break
                if attempt == 4:
                    raise
        discovering = True
        while time.monotonic() < deadline and not stopped.is_set():
            if selected and confirmed:
                device = await proxy(selected, 'org.bluez.Device1')
                if await device.get_paired():
                    trace('bluez_paired')
                    await device.set_trusted(True)
                    # BlueZ keeps link keys under /var/lib/bluetooth; flush them before the car is switched off.
                    os.sync()
                    phone = (await device.get_address()).upper()
                    save_json(root / 'state/receiver/pairing-request.json', {
                        'phone':phone, 'boot':get_platform().boot_id(),
                        'expires':time.monotonic() + GRANT_SECONDS})
                    save_json(status_file, {'phase':'paired'})
                    return
            elif selected is None:
                objects = await object_manager.call_get_managed_objects()
                found = sorted(({'address': d['Address'].value.upper(), 'name': d['Alias'].value}
                                for path, interfaces in objects.items()
                                for d in [interfaces.get('org.bluez.Device1')]
                                if d and path.startswith(adapter_path + '/dev_') and is_phone(d)
                                and not d['Paired'].value and 'Name' in d), key=lambda item: item['name'])
                pick = read_json(pick_file).get('address')
                pick_file.unlink(missing_ok=True)
                if pick and not busy() and pick in {item['address'] for item in found}:
                    attempt_generation += 1
                    error_code = ''
                    selected, error = f"{adapter_path}/dev_{pick.replace(':', '_')}", ''
                    deadline = attempt_deadline(deadline, time.monotonic(), opened)
                    save_json(status_file, {'phase':'pairing', 'name':next(i['name'] for i in found if i['address'] == pick)})
                    pairing = asyncio.ensure_future(pair(selected))
                else:
                    save_json(status_file, {'phase':'waiting', 'found':found, 'error':error, 'error_code':error_code})
            await asyncio.sleep(1)
        save_json(status_file, {'phase':'closed'})
    except Exception as exc:
        trace('worker_failed', getattr(exc, 'type', type(exc).__name__))
        save_json(status_file, {'phase':'closed', 'error':'配对连接中断，请重新发起。'})
        raise RuntimeError('Bluetooth pairing worker failed; see diagnostic event code') from None
    finally:
        if pairing and not pairing.done():
            pairing.cancel()
        if discovering:
            try:
                await adapter.call_stop_discovery()
            except Exception:
                pass
        for key,value in previous.items():
            try:
                await props.call_set('org.bluez.Adapter1', key, Variant('b', value))
            except Exception:
                pass
        if registered:
            try:
                await manager.call_unregister_agent(agent_path)
            except Exception:
                pass
        confirm_file.unlink(missing_ok=True)
        pick_file.unlink(missing_ok=True)
        bus.disconnect()
