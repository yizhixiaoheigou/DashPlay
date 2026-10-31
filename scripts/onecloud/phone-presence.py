#!/usr/bin/env python3
"""OneCloud return detection: fresh ARP on its Wi-Fi, pinned to the session's MAC.

No Wi-Fi credential handoff and no Bluetooth actions. State stays private to the box.
Unknown identity/network/errors fail closed. A boot grant is consumed only once per boot.
"""
import asyncio
import fcntl
import re
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
from platform_api import get_platform
import sys
import time
import traceback


def request(mac, source, target):
    return (b'\xff' * 6 + mac + b'\x08\x06' +
            struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1) + mac + socket.inet_aton(source) +
            b'\0' * 6 + socket.inet_aton(target))


def sender(frame, local_mac, local_ip):
    if len(frame) < 42 or frame[12:14] != b'\x08\x06':
        return None
    if frame[14:22] != struct.pack('!HHBBH', 1, 0x0800, 6, 4, 2):
        return None
    if frame[32:38] != local_mac or frame[38:42] != socket.inet_aton(local_ip):
        return None
    return socket.inet_ntoa(frame[28:32]), frame[22:28].hex()


def probe(interface, source, targets, expected_mac=None, collect=False):
    found = {}
    mac = bytes.fromhex(Path('/sys/class/net', interface, 'address').read_text().strip().replace(':', ''))
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0806)) as sock:
        sock.bind((interface, 0))
        for target in targets:
            sock.send(request(mac, source, target))
        deadline = time.monotonic() + 0.7
        while time.monotonic() < deadline:
            if not select.select([sock], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            response = sender(sock.recv(2048), mac, source)
            if response and response[0] in targets and (expected_mac is None or response[1] == expected_mac):
                if not collect:
                    return {'ip': response[0], 'mac': response[1]}
                found[response[1]] = response[0]
    return found if collect else None


def durable_replace(temporary, path):
    """Rename into place and flush both: the root fs commits only every 120 s (Armbian fstab), so a
    car switched off soon after a change would otherwise boot with the previous file (2026-10-11)."""
    fd = os.open(temporary, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def note(state_path, text):
    """Private diagnostics next to the state: command outcome categories, no addresses."""
    try:
        log = state_path.parent / 'presence-errors.log'
        lines = log.read_text().splitlines()[-19:] if log.exists() else []
        lines.append(f'{time.monotonic():.1f} {text}')
        log.write_text('\n'.join(lines) + '\n')
    except OSError:
        pass


def boot_key(boot_id, epoch=''):
    """One invitation of the last phone per boot, and again for each network epoch the supervisor
    starts a receiver in (CARPLAY_NETWORK_EPOCH). When the box rebuilds its network (car hotspot lost
    or back, its channel moved, settings changed) its own hotspot drops and the iPhone falls back to
    another Wi-Fi; without a new invitation it never returned (2026-10-11)."""
    return f'{boot_id}/{epoch}' if epoch else boot_id


def grant_boot(state, boot_id):
    if state.get('boot') == boot_id:
        return False
    state['boot'] = boot_id
    state['scan_at'] = -15
    return True


DECLINE_BASE, DECLINE_MAX = 120, 900
MAX_NETWORKS = 8


def choose_phone(state, present, paired, newly_paired=None, now=0.0):
    """No preemption. A failed network session prefers its owner; another online phone may follow.

    A phone that ignored its last invitation while staying online is skipped until its back-off
    expires, so it cannot block the next person indefinitely.
    """
    if state.get('active'):
        return None
    paired = paired - set(state.get('paused', []))
    if newly_paired in paired:
        return newly_paired
    declined = state.get('declined', {})
    eligible = {phone for phone in present & paired if now >= declined.get(phone, {}).get('until', 0)}
    previous = state.get('last_phone')
    if previous in eligible:
        return previous
    return next((phone for phone in state.get('phones', {}) if phone in eligible), None)


def invite_phone(state, paired):
    """Whom to invite without a presence proof (boot, cable just pulled): the last wireless user, or
    the only paired phone. A phone used on the USB cable only was never remembered (2026-10-12)."""
    paired = paired - set(state.get('paused', []))
    if state.get('last_phone') in paired:
        return state['last_phone']
    return next(iter(paired)) if len(paired) == 1 else None


def switch_grant(state, paired, phone, now):
    """Explicit switch from the viewer: the receiver ends the active session itself, so unlike
    choose_phone this may replace a phone that is using CarPlay. The old phone stays on the Wi-Fi;
    only its CarPlay session ends. None when the phone is not paired."""
    if phone not in paired:
        return None
    state.get('declined', {}).pop(phone, None)
    return {'phone': phone, 'expires': now + GRANT_SECONDS}


def phone_rows(state, names):
    """Paired phones for the viewer's switch list; BlueZ aliases with what the box remembers."""
    active, last = state.get('active'), state.get('last_phone')
    return [{'address': address, 'name': names.get(address, address), 'active': address == active,
             'last': address == last}
            for address in sorted(names, key=lambda item: (names[item].lower(), item))]


def grant_phone(path, value):
    """One bounded invitation, the same file the receiver's pairing window writes (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
    os.replace(temporary, path)


def decline(state, phone, now):
    count = state.setdefault('declined', {}).get(phone, {}).get('count', 0) + 1
    state['declined'][phone] = {'count': count, 'until': now + min(DECLINE_BASE * 2 ** (count - 1), DECLINE_MAX)}


def identity(record, network):
    """Wi-Fi identity per network: iPhone private addresses differ between hotspots."""
    return record.get('networks', {}).get(network)


def known_identity(record, network):
    """This network's identity, else the newest one from another network: on the same Wi-Fi name the iPhone keeps
    its private address when the subnet changes (router LAN renumbered, 2026-10-14). Only this subnet is probed."""
    return identity(record, network) or next(reversed(record.get('networks', {}).values()), None)


def learn(record, network, found):
    networks = record.setdefault('networks', {})
    networks.pop(network, None)
    networks[network] = found
    while len(networks) > MAX_NETWORKS:
        networks.pop(next(iter(networks)))


def migrate(state, default_phone):
    """Preserve identities recorded by the single-phone and first multi-phone formats."""
    if 'phones' not in state:
        state['phones'] = {}
        if default_phone and state.get('mac') and state.get('network'):
            state['phones'][default_phone] = {'networks': {state['network']: {'ip': state['ip'], 'mac': state['mac']}}}
            state['last_phone'] = default_phone
    for record in state['phones'].values():
        if 'network' in record:
            network = record.pop('network')
            found = {key: record.pop(key) for key in ('ip', 'mac') if key in record}
            if network and found.get('mac') and found.get('ip'):
                learn(record, network, found)
        if not record.get('controller'):
            record.pop('controller', None)


class BluezNotReady(Exception):
    """The Bluetooth bus is not up yet (right after a restart): nobody can be invited before it is."""


def is_phone(props):
    """Major device class Phone (iPhones report 0x7a020c); headsets, TVs and computers stay out."""
    value = props.get('Class')
    return value is not None and (value.value >> 8) & 0x1f == 2


async def bluez_devices(phones_only=False, known=()):
    """Paired, unblocked devices as {address: name}; BlueZ aliases are what the settings page shows.
    phones_only keeps headsets and computers out of the viewer's switch list: Major class Phone, or a
    phone this box has already carried CarPlay for (known addresses)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'linux-bt-bridge/deps'))
    from dbus_next import BusType, Message, MessageType
    from dbus_next.aio import MessageBus
    try:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise BluezNotReady() from error
    try:
        # BlueZ answers slowly while it is bootstrapping a phone over RFCOMM.
        response = await asyncio.wait_for(bus.call(Message(destination='org.bluez', path='/',
            interface='org.freedesktop.DBus.ObjectManager', member='GetManagedObjects')), 2.5)
        if response.message_type == MessageType.ERROR:
            return {}
        result = {}
        for interfaces in response.body[0].values():
            props = interfaces.get('org.bluez.Device1', {})
            address = props['Address'].value.upper() if 'Address' in props else ''
            if (address and props.get('Paired') and props['Paired'].value and
                    not (props.get('Blocked') and props['Blocked'].value) and
                    (not phones_only or is_phone(props) or address in known)):
                result[address] = props['Alias'].value if 'Alias' in props else address
        return result
    finally:
        bus.disconnect()


async def paired_phones():
    return set(await bluez_devices())


PAIRED_CACHE_SECONDS = 300
GRANT_SECONDS = 90


def paired_cached(state, now, lookup):
    """Live BlueZ pairing state; a recent answer covers a busy or briefly unreachable BlueZ."""
    try:
        paired = lookup()
    except Exception:
        if now - state.get('paired_at', -PAIRED_CACHE_SECONDS) < PAIRED_CACHE_SECONDS:
            return set(state.get('paired', []))
        raise
    state['paired'], state['paired_at'] = sorted(paired), now
    return paired


def pending_phone(path, boot, now):
    try:
        request = json.loads(path.read_text())
        if request.get('boot') == boot and now < request['expires']:
            return request['phone']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


LASTING = ('phones', 'last_phone')


def _read_json(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state(path):
    """The presence state. A car's power cut can leave the often rewritten file empty, which made every
    command fail and no iPhone was invited again (review 2026-10-12): then start empty, with who is
    known and who used the box last from their durable copy."""
    state = _read_json(path)
    known = _read_json(path.with_name(path.stem + '.known.json'))
    for key in LASTING:
        if key not in state and known.get(key) is not None:
            state[key] = known[key]
    return state


def save_state(path, state, lasting_before):
    """Probes are rewritten cheaply; who is known and who used the box last also go to a flushed copy."""
    lasting = json.dumps([state.get(key) for key in LASTING], sort_keys=True)
    known = path.with_name(path.stem + '.known.json')
    if lasting != lasting_before or not known.exists():
        temporary = known.with_suffix('.tmp')
        temporary.write_text(json.dumps({key: state.get(key) for key in LASTING}) + '\n')
        durable_replace(temporary, known)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state) + '\n')
    temporary.replace(path)


def main():
    interface = os.environ['CARPLAY_PRESENCE_INTERFACE']
    state_path = Path(os.environ['CARPLAY_PRESENCE_STATE'])
    default_phone = os.environ.get('CARPLAY_DEFAULT_PHONE', '').upper()
    os.umask(0o077)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(state_path)
        paused_path = state_path.with_name('phone-paused.json')
        state['paused'] = _read_json(paused_path).get('phones', [])
        # Only who is known and who used the box last must survive a power cut; probes stay cheap.
        lasting = json.dumps([state.get(key) for key in LASTING], sort_keys=True)
        migrate(state, default_phone)
        command = sys.argv[1]
        boot = get_platform().boot_id()
        request_path = state_path.parent / 'pairing-request.json'
        now = time.monotonic()
        output = None
        ok = False
        if command == 'boot':
            # Monotonic back-off deadlines and the active owner do not survive a receiver restart.
            state.pop('active', None)
            state.pop('declined', None)
            ok = grant_boot(state, boot_key(boot, os.environ.get('CARPLAY_NETWORK_EPOCH', '')))
            if ok:
                # The configured default phone only seeds the migration above; a reset box has none.
                previous = state.get('last_phone')
                output = (previous if previous not in state['paused'] else None) or invite_phone(state, paired_cached(
                    state, now, lambda: asyncio.run(paired_phones())))
                ok = bool(output)
        elif command == 'unplugged':
            output = invite_phone(state, paired_cached(state, now, lambda: asyncio.run(paired_phones())))
            ok = bool(output)
        elif command == 'idle':
            state.pop('active', None)
            ok = True
        elif command == 'decline':
            phone = sys.argv[2].upper()
            if phone in state['phones'] or phone == pending_phone(request_path, boot, now):
                decline(state, phone, now)
                ok = True
        elif command == 'disconnect-request':
            path = state_path.with_name('phone-disconnect.json')
            request = _read_json(path)
            phone = request.get('phone')
            if request.get('boot') == state.get('boot') and phone in state['paused'] and state.get('active') == phone:
                output, ok = phone, True
            else:
                path.unlink(missing_ok=True)
        elif command == 'switch-request':
            # The settings page asking to hand CarPlay to another paired phone (2026-10-31). The receiver performs
            # the switch; this only consumes the request once and names the phone.
            path = state_path.with_name('phone-switch.json')
            request = _read_json(path)
            phone = str(request.get('phone') or '').upper()
            path.unlink(missing_ok=True)
            if request.get('boot') == state.get('boot') and phone and now <= request.get('expires', 0):
                output, ok = phone, True
        elif command == 'allowed':
            phone = sys.argv[2].upper()
            ok = phone not in state['paused'] and phone in paired_cached(state, now, lambda: asyncio.run(paired_phones()))
        elif command == 'devices':
            names = asyncio.run(asyncio.wait_for(
                bluez_devices(phones_only=True, known=set(state.get('phones', {}))), 3))
            output = json.dumps(phone_rows(state, names), ensure_ascii=False)
            ok = True
        elif command == 'switch':
            phone = sys.argv[2].upper()
            paired = paired_cached(state, now, lambda: asyncio.run(paired_phones()))
            grant = switch_grant(state, paired, phone, now)
            if grant:
                state['paused'] = [item for item in state['paused'] if item != phone]
                grant_phone(paused_path, {'phones':state['paused']})
                grant_phone(request_path, dict(grant, boot=boot))
                ok = True
            else:
                note(state_path, 'switch request for an unpaired phone')
        else:
            data = json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show', 'dev', interface], timeout=1))
            address = next((a for d in data for a in d['addr_info'] if a['scope'] == 'global'), None)
            if address is None:
                # No address while the car hotspot is gone: no phone can be on it. Not an error.
                return 1
            source = address['local']
            network = str(ipaddress.ip_network(f"{source}/{address['prefixlen']}", strict=False))
            paired = paired_cached(state, now, lambda: asyncio.run(paired_phones()))
            new_phone = pending_phone(request_path, boot, now)
            # Bind remembered Wi-Fi identities to this hotspot subnet. Never probe routed LANs.
            if command == 'remember':
                peer = str(ipaddress.ip_address(sys.argv[2]))
                phone = sys.argv[3].upper()
                controller = sys.argv[4] if len(sys.argv) > 4 else ''
                owner = next((key for key, record in state['phones'].items()
                              if controller and record.get('controller') == controller), None)
                found = (probe(interface, source, [peer])
                         if (owner or phone) in paired and ipaddress.ip_address(peer) in ipaddress.ip_network(network) else None)
                ok = bool(found)
                if found:
                    # The verified AirPlay controller is authoritative; otherwise a known Wi-Fi MAC.
                    phone = owner or next((key for key, record in state['phones'].items() if key in paired and
                                           (known_identity(record, network) or {}).get('mac') == found['mac']), phone)
                    record = state['phones'].setdefault(phone, {})
                    learn(record, network, found)
                    if controller:
                        record['controller'] = controller
                    state['last_phone'] = state['active'] = phone
                    state.get('declined', {}).pop(phone, None)
                    if new_phone == phone:
                        request_path.unlink(missing_ok=True)
            elif command == 'admit':
                peer = str(ipaddress.ip_address(sys.argv[2]))
                found = None
                if ipaddress.ip_address(peer) in ipaddress.ip_network(network):
                    # A phone that has only just joined may miss the first ARP request.
                    found = probe(interface, source, [peer]) or probe(interface, source, [peer])
                if found:
                    output = next((key for key, record in state['phones'].items() if key in paired and
                                   (known_identity(record, network) or {}).get('mac') == found['mac']), None)
                    if output is None and new_phone in paired:
                        output = new_phone
                    ok = output is not None
                    if not ok:
                        note(state_path, 'admit reply from an unregistered Wi-Fi address')
                else:
                    note(state_path, 'admit no ARP reply' if ipaddress.ip_address(peer) in ipaddress.ip_network(network)
                         else 'admit peer outside the hotspot subnet')
            elif command in ('present', 'select'):
                records = {phone: known_identity(record, network) for phone, record in state['phones'].items()
                           if phone in paired and known_identity(record, network)}
                targets = list({record['ip'] for phone, record in records.items()
                                if identity(state['phones'][phone], network)})
                found = probe(interface, source, targets, collect=True) if targets else {}
                hosts = ipaddress.ip_network(network)
                if len(found) < len(records) and hosts.num_addresses <= 256 and now - state.get('scan_at', -15) >= 15:
                    state['scan_at'] = now
                    found.update(probe(interface, source, [str(ip) for ip in hosts.hosts()
                                                         if str(ip) != source], collect=True))
                present = set()
                for phone, record in records.items():
                    if record['mac'] in found:
                        if identity(state['phones'][phone], network):
                            record['ip'] = found[record['mac']]
                        else:
                            learn(state['phones'][phone], network, {'ip': found[record['mac']], 'mac': record['mac']})
                        present.add(phone)
                    else:
                        # Leaving the Wi-Fi ends a refusal back-off; returning is a fresh request.
                        state.get('declined', {}).pop(phone, None)
                if command == 'select':
                    output = choose_phone(state, present, paired, new_phone, now)
                    ok = output is not None
                else:
                    phone = sys.argv[2].upper()
                    ok = phone not in state['paused'] and phone in paired and (phone in present or phone == new_phone)
            else:
                raise ValueError('Unknown presence command')
        save_state(state_path, state, lasting)
        if ok and output:
            # devices is a JSON list; every other command answers with one Bluetooth address.
            if command != 'devices' and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', output):
                return 1
            print(output)
        return 0 if ok else 1


def entry():
    try:
        return main()
    except BluezNotReady:
        # A normal moment, not an error: the bus socket appears a few seconds after a restart (2026-10-12).
        if os.environ.get('CARPLAY_PRESENCE_STATE'):
            note(Path(os.environ['CARPLAY_PRESENCE_STATE']), f'{sys.argv[1] if len(sys.argv) > 1 else "-"} BlueZ not ready yet')
        return 1
    except Exception as error:
        # No credentials, peer identifiers or raw messages: the command, the error type, the file it could
        # not find (a system path) and where in this script it failed.
        try:
            log = Path(os.environ['CARPLAY_PRESENCE_STATE']).parent / 'presence-errors.log'
            lines = log.read_text().splitlines()[-19:] if log.exists() else []
            frames = traceback.extract_tb(error.__traceback__)
            ours = [f for f in frames if Path(f.filename).name == Path(__file__).name][-1]
            deepest = frames[-1]
            missing = f' {error.filename}' if isinstance(error, OSError) and error.filename else ''
            via = f' via {Path(deepest.filename).name}:{deepest.lineno}' if deepest is not ours else ''
            lines.append(f'{time.monotonic():.1f} {sys.argv[1] if len(sys.argv) > 1 else "-"} {type(error).__name__}'
                         f'{missing} at line {ours.lineno} {ours.name}{via}')
            log.write_text('\n'.join(lines) + '\n')
        except Exception:
            pass
        # Absence is the safe default.
        return 1


if __name__ == '__main__':
    sys.exit(entry())
