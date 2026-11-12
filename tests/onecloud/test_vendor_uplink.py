"""RTL8188FU compatibility: synthetic sysfs and commands, never real networking."""
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import platform_debian as platform

COMBINATIONS = 'valid interface combinations:\n\t\t * #{ managed } <= 1, #{ AP } <= 1, total <= 2, #channels <= 1\n'


class VendorUplinkTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.net = self.root / 'net'; self.net.mkdir()
        for name in ('wlan0', 'p2p0'):
            (self.net / name / 'wireless').mkdir(parents=True)
            (self.net / name / 'phy80211').symlink_to(self.root / 'phy0')
        (self.root / 'phy0').mkdir()
        self.p = platform.DebianPlatform('wlan0', self.root, self.root / 'network/sta.network')
        self.commands = []
        self.rules = set(); self.chains = set()
        self.kind = 'managed'; self.flags = []; self.addresses = []
        self.nm = False; self.managed = 'yes'
        self.fail_delete = False
        self.fail_insert = None
        self.fail_check = False
        self.journal = self.root / 'phone-uplink.json'
        for patcher in (patch.object(platform, 'SYS_NET', self.net),
                        patch.object(platform, 'wifi_driver', return_value='8188fu'),
                        patch.object(platform.subprocess, 'run', side_effect=self.command),
                        patch.object(platform.shutil, 'which', side_effect=lambda _: '/bin/nmcli' if self.nm else None),
                        patch('setup_wizard.os.sync')):
            patcher.start(); self.addCleanup(patcher.stop)
        read = Path.read_text
        patcher = patch.object(Path, 'read_text', lambda p,*a,**k: '0' if str(p)=='/proc/sys/net/ipv4/ip_forward' else read(p,*a,**k))
        patcher.start(); self.addCleanup(patcher.stop)

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        code, out = 0, ''
        if argv[0] == 'iw':
            if argv[-1] == 'info' and argv[1] == 'dev':
                out = f'Interface {argv[2]}\n\t\ttype {self.kind}\n'
            elif argv[-1] == 'info' or argv == ['iw', 'list']:
                out = 'Wiphy phy0\n'  # vendor omits combinations
        elif argv[:3] == ['ip', '-j', 'link']:
            out = json.dumps([{'address': '02:00:5e:00:53:02' if argv[-1] == 'p2p0' else '00:00:5e:00:53:01', 'flags': self.flags}])
        elif argv[:3] == ['ip', '-j', 'addr']:
            out = json.dumps([{'address': '02:00:5e:00:53:02', 'flags': self.flags, 'addr_info': self.addresses}])
        elif argv[0] == 'nmcli':
            if 'RUNNING' in argv: out = 'running'
            elif 'GENERAL.MANAGED' in argv: out = self.managed
            elif 'GENERAL.STATE' in argv: out = '10 (unmanaged)'
        elif argv[0] == 'iptables':
            table = argv[2] if argv[1] == '-t' else 'filter'
            pos = 3 if argv[1] == '-t' else 1
            action, args = argv[pos], tuple(argv[pos+1:])
            key = (table, args)
            if action == '-N': self.chains.add((table, args[0]))
            elif action == '-S': code = 0 if (table, args[0]) in self.chains else 1
            elif action == '-X': self.chains.discard((table, args[0]))
            elif action == '-C': code = 2 if self.fail_check else (0 if key in self.rules else 1)
            elif action in ('-I', '-A'):
                if self.fail_insert == len(self.rules): code = 2
                else: self.rules.add(key)
            elif action == '-D':
                code = 2 if self.fail_delete else (0 if key in self.rules else 1)
                if code == 0: self.rules.remove(key)
        if code and kwargs.get('check'): raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr='synthetic error' if code else '')

    def start(self):
        self.p.prepare_phone_uplink()
        self.assertTrue(self.p.add_phone_ap())

    def test_wifi_selection_excludes_vendor_p2p(self):
        self.assertEqual(platform.wifi_interface(self.net), 'wlan0')

    def test_known_vendor_same_phy_has_same_channel_support(self):
        self.assertEqual(self.p.concurrent_ap(), 1)

    def test_unknown_driver_or_other_phy_is_not_vendor_support(self):
        with patch.object(platform, 'wifi_driver', return_value='unknown'):
            self.assertEqual(self.p.concurrent_ap(), 0)
        (self.net / 'p2p0/phy80211').unlink()
        (self.net / 'p2p0/phy80211').symlink_to(self.root / 'phy1')
        (self.root / 'phy1').mkdir()
        self.assertEqual(self.p.concurrent_ap(), 0)

    def test_other_radios_capability_is_not_used(self):
        def run(argv, **kwargs):
            return Mock(returncode=0, stdout=COMBINATIONS if argv == ['iw', 'list'] else '')
        with patch.object(platform, 'wifi_driver', return_value='unknown'), \
                patch.object(platform.subprocess, 'run', side_effect=run):
            self.assertEqual(self.p.concurrent_ap(), 0)

    def test_vendor_borrow_retains_mac_and_records_ownership_before_handoff(self):
        self.p.prepare_phone_uplink()
        def handoff():
            saved = json.loads(self.journal.read_text())
            self.assertEqual(saved['ap_vif'], 'p2p0')
            self.assertIn('borrowed_ap', saved)
        with patch.object(self.p, 'release_phone_ap_from_networkmanager', side_effect=handoff):
            self.assertTrue(self.p.add_phone_ap())
        self.assertEqual(self.p.ap_interface, 'p2p0')
        self.assertFalse(any(c[:3] == ['ip', 'link', 'set'] and 'address' in c for c in self.commands))
        self.assertFalse(any('add' in c and c[0] == 'iw' for c in self.commands))

    def test_busy_vendor_interface_is_not_borrowed(self):
        for flags, addresses, kind in [(['UP'], [], 'managed'), ([], [{'family':'inet'}], 'managed'), ([], [], 'monitor')]:
            with self.subTest(flags=flags, addresses=addresses, kind=kind):
                self.flags, self.addresses, self.kind = flags, addresses, kind
                self.p.prepare_phone_uplink()
                with self.assertRaises(RuntimeError): self.p.add_phone_ap()
                self.p.cleanup_phone_uplink()

    def test_restart_restores_borrowed_interface_and_exact_nat_rules(self):
        self.nm = True
        self.start()
        self.p.enable_phone_nat(ipaddress.ip_network('100.96.0.0/24'))
        self.assertTrue(self.rules)
        with patch('connectivity.ipv4_forwarding', return_value=True):
            self.assertTrue(self.p.sharing_ready('p2p0'))
            self.assertFalse(self.p.sharing_ready('ap0'))
        self.assertFalse(any('-m' in c and 'comment' in c for c in self.commands if c[0]=='iptables'))
        fresh = platform.DebianPlatform('wlan0', self.root, self.root / 'network/sta.network')
        fresh.cleanup_phone_uplink()
        self.assertFalse(self.rules); self.assertFalse(self.chains)
        self.assertFalse(self.journal.exists())
        self.assertNotIn(['iw','dev','p2p0','del'], self.commands)
        self.assertIn(['iw','dev','p2p0','set','type','managed'], self.commands)
        self.assertIn(['nmcli','device','set','p2p0','managed','yes'], self.commands)
        self.assertEqual(fresh.ap_interface, 'wlan0')

    def test_failed_nat_cleanup_retains_journal_for_retry(self):
        self.start(); self.p.enable_phone_nat(ipaddress.ip_network('100.96.0.0/24'))
        self.fail_delete = True
        with self.assertRaises(subprocess.CalledProcessError): self.p.cleanup_phone_uplink()
        self.assertTrue(self.journal.exists())
        self.fail_delete = False
        self.p.cleanup_phone_uplink()
        self.assertFalse(self.journal.exists()); self.assertFalse(self.rules)

    def test_failed_nm_handoff_can_restore_without_deleting_vendor_interface(self):
        self.p.prepare_phone_uplink()
        with patch.object(self.p, 'release_phone_ap_from_networkmanager', side_effect=RuntimeError('NM failure')):
            with self.assertRaises(RuntimeError): self.p.add_phone_ap()
        self.p.cleanup_phone_uplink()
        self.assertIn(['iw','dev','p2p0','set','type','managed'], self.commands)
        self.assertNotIn(['iw','dev','p2p0','del'], self.commands)

    def test_runtime_network_directory_created_without_moving_to_etc(self):
        with patch.object(self.p, 'start'):
            self.p.start_phone_station({'ssid':'Phone','passphrase':'synthetic-pass'}, 2412)
        self.assertTrue(self.p.sta_network.exists())
        self.assertTrue(str(platform.STA_NETWORK).startswith('/run/'))
        self.assertTrue(str(platform.USB_TETHER_NETWORK).startswith('/run/'))

    def test_partial_rule_install_is_recoverable_without_marking_dns_ready(self):
        self.start(); self.fail_insert = 2
        with self.assertRaises(subprocess.CalledProcessError):
            self.p.enable_phone_nat(ipaddress.ip_network('100.96.0.0/24'))
        with patch('connectivity.ipv4_forwarding', return_value=True):
            self.assertFalse(self.p.sharing_ready('p2p0'))
        self.p.cleanup_phone_uplink()
        self.assertFalse(self.rules); self.assertFalse(self.chains)

    def test_existing_chains_are_never_claimed_or_deleted(self):
        self.start(); self.chains.add(('nat', 'DP_PHONE_NAT'))
        with self.assertRaises(RuntimeError):
            self.p.enable_phone_nat(ipaddress.ip_network('100.96.0.0/24'))
        self.p.cleanup_phone_uplink()
        self.assertEqual(self.chains, {('nat', 'DP_PHONE_NAT')})

    def test_rule_query_error_keeps_recovery_record(self):
        self.start(); self.p.enable_phone_nat(ipaddress.ip_network('100.96.0.0/24'))
        self.fail_check = True
        with self.assertRaises(subprocess.CalledProcessError): self.p.cleanup_phone_uplink()
        self.assertTrue(self.journal.exists()); self.assertTrue(self.rules)
        self.fail_check = False
        self.p.cleanup_phone_uplink()
        self.assertFalse(self.rules)

    def test_repeated_cleanup_and_old_ap0_journal(self):
        network = ipaddress.ip_network('100.96.0.0/24')
        saved = {'mac':'00:00:5e:00:53:01', 'interface':'wlan0', 'forward':'0', 'network':str(network)}
        self.journal.write_text(json.dumps(saved))
        for table, rule in self.p.phone_nat_rules(network):
            self.command(['iptables', *table, '-I', *rule])
        self.p.cleanup_phone_uplink()
        self.assertFalse(self.rules)
        self.assertIn(['iw', 'dev', 'ap0', 'del'], self.commands)
        count = len(self.commands)
        self.p.cleanup_phone_uplink()
        self.assertEqual(len(self.commands), count)

    def test_no_vendor_fallback_for_unrelated_driver(self):
        with patch.object(platform, 'wifi_driver', return_value='brcmfmac'):
            self.start()
        self.assertEqual(self.p.ap_interface, 'ap0')
        self.assertIn(['iw', 'dev', 'wlan0', 'interface', 'add', 'ap0', 'type', '__ap'], self.commands)
        self.assertNotIn('borrowed_ap', json.loads(self.journal.read_text()))

    def test_advertised_standard_radio_support_is_preserved(self):
        with patch.object(platform, 'wifi_driver', return_value='brcmfmac'), \
                patch.object(platform.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, COMBINATIONS)):
            self.assertEqual(self.p.concurrent_ap(), 1)

    def test_vendor_exception_never_promises_two_channel_operation(self):
        with patch.object(platform.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, COMBINATIONS.replace('#channels <= 1', '#channels <= 2'))):
            self.assertEqual(self.p.concurrent_ap(), 1)

    def test_vendor_cleanup_does_not_rewrite_an_unchanged_mac(self):
        self.start(); self.p.cleanup_phone_uplink()
        self.assertFalse(any(c[:3] == ['ip', 'link', 'set'] and 'address' in c for c in self.commands))

if __name__ == '__main__': unittest.main()
