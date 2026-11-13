"""Keep the controller identity to which the phone's existing BlueZ bonds belong.

Never copy/link keys between controller directories. Adopt one unambiguous bonded
identity on upgrade; persist it before changing the controller, then verify after
power-on. A different current address on the next boot is restored to this target.
"""
import json
from pathlib import Path
import re
import subprocess
import time
from setup_wizard import save_json

ADDRESS = re.compile(r'^[0-9a-f]{2}(?::[0-9a-f]{2}){5}$', re.I)


def bonded_devices(root):
    result = {}
    for adapter in Path(root).glob('*'):
        if not ADDRESS.fullmatch(adapter.name): continue
        for device in adapter.glob('*/info'):
            if not ADDRESS.fullmatch(device.parent.name): continue
            text = device.read_text()
            if '[LinkKey]' in text or '[LongTermKey]' in text:
                result.setdefault(adapter.name.lower(), set()).add(device.parent.name.lower())
    return result


def bonded_adapters(root):
    return set(bonded_devices(root))


def last_wireless_identity(state, bonds, current):
    """Use a complete historical wireless handshake, never an invitation or bond mtime.

    This is only migration from a shared factory address with multiple old identities.
    Read a bounded tail of recent private receiver logs; a truncated/ambiguous success
    cannot authorize an address switch. Both identities must still bond the same phone.
    """
    invite = re.compile(r'^\d+ \[wireless-bootstrap\] INFO wireless - wireless bootstrap: '
                        r'iPhone=([0-9a-f:]{17}) hostBt=([0-9a-f:]{17}) wifi=', re.I)
    accepted = re.compile(r'^boot \d+(?:\.\d+)?s invitation accepted$')
    live = re.compile(r'^\d+ \[wireless-tunnel-control\] INFO playport - '
                      r'wireless iAP2 tunnel ready; CarPlay is live$')
    try:
        paths = sorted(Path(state).glob('logs/*/receiver.log'), key=lambda p: p.stat().st_mtime_ns,
                       reverse=True)[:16]
        successes = []
        for path in paths:
            with path.open('rb') as source:
                size = source.seek(0, 2)
                source.seek(max(0, size - 1024 * 1024))
                if size > 1024 * 1024: source.readline()  # Do not parse a partial first line.
                lines = source.read(1024 * 1024).decode('utf-8', errors='replace').splitlines()
            pending, confirmed, latest = None, False, None
            saw_success = False
            for line in lines:
                match = invite.search(line)
                if match:
                    pending, confirmed = (match[1].lower(), match[2].lower()), False
                elif 'wireless bootstrap failed:' in line or 'receiver listening' in line:
                    pending, confirmed = None, False
                elif accepted.fullmatch(line):
                    confirmed = pending is not None
                elif live.fullmatch(line):
                    saw_success = True
                    latest = pending if confirmed else None
                    pending, confirmed = None, False
            if saw_success: successes.append((path.stat().st_mtime_ns, latest))
    except OSError:
        return None  # Incomplete evidence must not choose a controller.
    if not successes: return None
    newest = max(stamp for stamp, _ in successes)
    matches = {record for stamp, record in successes if stamp == newest}
    if len(matches) != 1 or None in matches: return None
    phone, adapter = matches.pop()
    if phone in bonds.get(adapter, set()) and phone in bonds.get(current, set()):
        return adapter
    return None


def select_address(current, saved, bonded, derived, shared, successful=None):
    if saved is not None:
        if not isinstance(saved, str) or not ADDRESS.fullmatch(saved) or saved.lower() == '00:00:00:00:00:00':
            raise ValueError('Invalid saved Bluetooth identity')
        return saved.lower()
    if current in shared and len(bonded) > 1:
        if successful in bonded: return successful
        raise ValueError('Multiple bonded identities at factory address; verified previous identity required')
    if current in bonded: return current
    if len(bonded) == 1: return next(iter(bonded))
    if bonded: raise ValueError('Multiple old bonded controllers; no safe identity selection')
    return derived if current in shared else current


class BluetoothIdentity:
    def __init__(self, state, bluez=Path('/var/lib/bluetooth'), run=subprocess.run, sleep=time.sleep):
        self.path, self.bluez, self.run, self.sleep = Path(state)/'bluetooth-identity.json', Path(bluez), run, sleep
        self.target = None

    def command(self, *args):
        result = self.run(list(args), input='', capture_output=True, text=True, check=True, timeout=5)
        # btmgmt can exit zero even when the management reply reports a failure.
        if args[0] == 'btmgmt' and re.search(r'failed with status|Invalid Index', result.stdout + result.stderr, re.I):
            raise RuntimeError('Bluetooth management command failed')
        return result.stdout

    def current(self):
        info = self.command('btmgmt', '--index', '0', 'info')
        found = re.search(r'addr ([0-9a-f:]{17})', info, re.I)
        if not found or not ADDRESS.fullmatch(found[1]) or found[1].lower() == '00:00:00:00:00:00':
            raise ValueError('Bluetooth controller address not ready')
        return found[1].lower()

    def configure_unset(self, derived):
        """Snapdragon 410 sticks: btqcomsmd has no address of its own and takes it from the device tree's
        "local-bd-address"; without one the kernel lists the controller as unconfigured and `info` answers
        "Invalid Index" until it is given a public address. Give it the saved one, else derived (a callable).
        True when it did; the rest of prepare() then runs on that address."""
        try:
            listing = self.command('btmgmt', 'config')
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False   # an older btmgmt without "config": the controller is read as before
        if not re.search(r'^hci0:\s+Unconfigured controller$(?:\n\t.*)*?\n\tmissing options:[^\n]*\bpublic-address\b',
                         listing, re.M):
            return False
        saved = json.loads(self.path.read_text()).get('address') if self.path.exists() else None
        target = (saved.lower() if isinstance(saved, str) and ADDRESS.fullmatch(saved) and saved != '00:00:00:00:00:00'
                  else derived())
        self.command('btmgmt', '--index', '0', 'public-addr', target)
        if not self.wait_for_address(target):
            raise RuntimeError('Unconfigured Bluetooth controller did not take an address')
        return True

    def prepare(self):
        from platform_debian import derived_address, machine_key, SHARED_BLUETOOTH_ADDRESSES
        self.configure_unset(lambda: derived_address(machine_key(), 'bluetooth'))
        current = self.current()
        saved = json.loads(self.path.read_text()).get('address') if self.path.exists() else None
        # Derive only for a new unpaired vendor-default device. Keep existing unique hardware as-is.
        bonds = bonded_devices(self.bluez) if saved is None else {}
        bonded = set(bonds)
        successful = last_wireless_identity(self.path.parent, bonds, current) if current in SHARED_BLUETOOTH_ADDRESSES and len(bonded) > 1 else None
        derived = derived_address(machine_key(), 'bluetooth') if current in SHARED_BLUETOOTH_ADDRESSES and not bonded and saved is None else current
        self.target = select_address(current, saved, bonded, derived, SHARED_BLUETOOTH_ADDRESSES, successful)
        if saved is None: save_json(self.path, {'address': self.target})
        if current != self.target:
            self.reconfigure(self.target)
        if self.wait_for_address(self.target): return
        # Linux may already cache the requested public_addr while the controller still
        # reports another address. Setting the cached value succeeds but is a no-op.
        # Reconfigure once through the observed original address (never an invented
        # identity), then retry the saved target. No bond directories are moved.
        if current != self.target and self.current() == current:
            self.reconfigure(current)
            # This first reconfiguration keeps the same address, so an immediate
            # matching read cannot tell whether the async remove/add has finished.
            self.sleep(1)
            if self.wait_for_address(current):
                self.reconfigure(self.target)
                if self.wait_for_address(self.target): return
        raise RuntimeError('Bluetooth address did not settle')

    def reconfigure(self, address):
        # During Linux's boot auto-off window MGMT reports "off" while HCI_UP
        # is still set. Another off request succeeds without closing HCI; a
        # public-addr change can then strand it in HCI_CONFIG (N1, 6.12.111).
        # Complete an explicit on/off transition before changing a configured
        # controller. Unconfigured controllers use configure_unset instead.
        self.command('btmgmt', '--index', '0', 'power', 'on')
        self.command('btmgmt', '--index', '0', 'power', 'off')
        self.command('btmgmt', '--index', '0', 'public-addr', address)

    def wait_for_address(self, address):
        # A successful management command can briefly remove/re-add the adapter.
        for _ in range(20):
            try:
                if self.current() == address:
                    shown = self.command('bluetoothctl', 'show').lower()
                    if re.search(r'controller\s+' + re.escape(address) + r'\b', shown): return True
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                pass
            self.sleep(.2)
        return False

    def verify(self):
        return self.target is not None and self.current() == self.target
