import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import diagnostics
import platform_debian

LINK = """Connected to 02:00:00:00:00:a1 (on wlx020000000010)
\tSSID: 小鹏汽车
\tfreq: 5745.0
\tsignal: -61 dBm
\trx bitrate: 433.3 MBit/s VHT-MCS 9 80MHz short GI VHT-NSS 1
\ttx bitrate: 292.5 MBit/s VHT-MCS 7 80MHz VHT-NSS 1
"""
STATIONS = """Station 02:00:00:00:00:a1 (on wlx020000000010)
\tinactive time:\t40 ms
\ttx packets:\t12000
\ttx retries:\t3500
\ttx failed:\t12
\tsignal:  \t-61 [-61] dBm
\ttx bitrate:\t292.5 MBit/s
\trx bitrate:\t433.3 MBit/s
Station 02:00:00:00:00:a4 (on wlx020000000010)
\tinactive time:\t0 ms
\tsignal:  \t-40 dBm
"""


class ParseTests(unittest.TestCase):
    def test_link_and_stations(self):
        self.assertEqual(platform_debian.parse_link(LINK), {'freq': 5745, 'signal': -61, 'rx_mbps': 433.3, 'tx_mbps': 292.5})
        self.assertEqual(platform_debian.parse_link('Not connected.'), {})
        peers = platform_debian.parse_stations(STATIONS)
        self.assertEqual(peers[0], {'peer': '00:a1', 'signal': -61, 'rx_mbps': 433.3, 'tx_mbps': 292.5,
                                    'tx_packets': 12000, 'tx_retries': 3500, 'tx_failed': 12, 'inactive_ms': 40})
        self.assertEqual(peers[1]['peer'], '00:a4')
        self.assertIsNone(peers[1]['tx_retries'])


class DiagLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / 'diag'

    def test_append_rotates_and_reads_both_files(self):
        log = diagnostics.DiagLog(self.dir, max_bytes=60)
        for n in range(6):
            log.append({'n': n, 'pad': 'x' * 20})
        self.assertTrue((self.dir / 'diag.1.jsonl').exists())
        numbers = [json.loads(line)['n'] for line in log.text().splitlines()]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers[-1], 5)
        self.assertEqual((self.dir / 'diag.jsonl').stat().st_mode & 0o777, 0o600)

    def test_tail_follows_a_rewritten_log_and_keeps_partial_lines(self):
        path = Path(self.tmp.name) / 'receiver.log'
        tail = diagnostics.LogTail(path)
        self.assertEqual(tail.read(), [])
        path.write_text('a\nb\npart')
        self.assertEqual(tail.read(), ['a', 'b'])
        with path.open('a') as out:
            out.write('ial\nc\n')
        self.assertEqual(tail.read(), ['partial', 'c'])
        path.unlink(); path.write_text('new\n')   # the receiver restarted and rewrote its log
        self.assertEqual(tail.read(), ['new'])

    def test_keep_filters_and_caps(self):
        lines = ['noise'] * 5 + [f'123 [viewer] INFO viewer - viewer stats client=1 fps={n}' for n in range(50)]
        kept = diagnostics.keep(lines)
        self.assertEqual(len(kept), diagnostics.MAX_LINES)
        self.assertTrue(kept[-1].endswith('fps=49'))
        self.assertEqual(diagnostics.keep(['boot 31.2s invitation sent', 'DEBUG x']), ['boot 31.2s invitation sent'])

    def test_loop_writes_records_with_platform_and_log(self):
        class Platform:
            def boot_id(self): return 'abcdef0123'
            def diagnostics(self): return {'usb_resets': 0, 'link': {'signal': -60}}
        receiver = Path(self.tmp.name) / 'receiver.log'
        receiver.write_text('x WARN something\n')
        stopped = threading.Event()
        log = diagnostics.DiagLog(self.dir)
        program = Path(self.tmp.name) / 'program'; program.mkdir()
        (program / 'version.txt').write_text('2026.10.20\n')
        diagnostics.start(stopped, log, receiver, Platform(), {'mode': 'sta'}, interval=0.05, program=program)
        for _ in range(100):
            if len(log.text().splitlines()) >= 2:
                break
            stopped.wait(0.02)
        stopped.set()
        records = [json.loads(line) for line in log.text().splitlines()]
        self.assertEqual(records[0]['event'], 'start')
        self.assertEqual(records[0]['boot'], 'abcdef01')
        self.assertEqual(records[0]['version'], '2026.10.20', 'which release the box runs opens every start')
        self.assertEqual(records[1]['mode'], 'sta')
        self.assertEqual(records[1]['link'], {'signal': -60})
        self.assertEqual(records[1]['log'], ['x WARN something'])

    def test_direct_phone_diagnostic_samples_ap_instead_of_upstream(self):
        platform = Mock(interface='wlan-test')
        platform.boot_id.return_value = 'abcdef01'
        platform.diagnostics.return_value = {}
        stopped = Mock(); stopped.wait.side_effect = [False, True]
        log = diagnostics.DiagLog(self.dir)
        status = {'mode':'sta', 'phone_interface':'ap0', 'direct_phone':'validation', 'tesla_dns_log':'stopped'}
        # Run exactly one real loop iteration, synchronously, without network probes.
        with patch('diagnostics.threading.Thread') as thread, patch('diagnostics.ping_targets', return_value={'phone':'192.0.2.2'}), \
             patch('diagnostics.ping_stats', return_value=None), patch('diagnostics.tcp_stats', return_value=[]), \
             patch('diagnostics.time.monotonic', return_value=30), patch('diagnostics.phone_traffic', return_value={'bytes':42}) as traffic:
            thread.side_effect = lambda target, **kw: Mock(start=target)
            diagnostics.start(stopped, log, Path(self.tmp.name)/'receiver.log', platform, status, state=Path(self.tmp.name))
        traffic.assert_called_once_with('ap0', '192.0.2.2')
        record = json.loads(log.text().splitlines()[-1])
        self.assertEqual(record['phone_interface'], 'ap0')
        self.assertEqual(record['direct_phone'], 'validation')
        self.assertEqual(record['tesla_dns_log'], 'stopped')



class LogMirrorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.run = Path(tmp.name) / 'logs', Path(tmp.name) / 'run'
        self.root.mkdir()
        self.run.mkdir()

    @patch('diagnostics.subprocess.run')
    def test_full_logs_survive_restarts_and_truncation(self, run):
        run.return_value = Mock(stdout=b'[ 1.0] kernel: usb 1-1: new device\n')
        receiver = self.run / 'receiver.log'
        receiver.write_text('one\ntwo\npartial')
        mirror = diagnostics.LogMirror(self.root, self.run, 'abcdef0123456789')
        mirror.directory.mkdir()
        offsets, handles = {}, {}
        mirror.cycle(offsets, handles)
        self.assertEqual((mirror.directory / 'receiver.log').read_text(), 'one\ntwo\n')   # partial line waits
        self.assertIn('usb 1-1', (mirror.directory / 'journal.log').read_text())
        self.assertIn('--cursor-file=', ' '.join(run.call_args.args[0]))
        # A supervisor restart in the same boot resumes from the saved offsets: nothing twice.
        with receiver.open('a') as log:
            log.write('\nthree\n')
        again = diagnostics.LogMirror(self.root, self.run, 'abcdef0123456789')
        again.cycle({k: tuple(v) for k, v in again.load().items()}, {})
        self.assertEqual((mirror.directory / 'receiver.log').read_text(), 'one\ntwo\npartial\nthree\n')
        # Each receiver start rewrites its log: start over and say so.
        receiver.unlink()
        receiver.write_text('fresh\n')
        offsets = {k: tuple(v) for k, v in again.load().items()}
        again.cycle(offsets, {})
        self.assertTrue((mirror.directory / 'receiver.log').read_text().endswith('restarted]\nfresh\n'))

    @patch('diagnostics.subprocess.run')
    def test_without_a_journal_only_run_logs_are_kept(self, run):
        (self.run / 'supervisor.log').write_text('boot 12.0s supervisor start\n')
        mirror = diagnostics.LogMirror(self.root, self.run, 'abcdef0123456789', journal=False)
        mirror.directory.mkdir()
        mirror.cycle({}, {})
        run.assert_not_called()   # OpenWrt has no journalctl
        self.assertEqual((mirror.directory / 'supervisor.log').read_text(), 'boot 12.0s supervisor start\n')
        self.assertFalse((mirror.directory / 'journal.log').exists())

    def test_old_boots_are_pruned(self):
        for index in range(12):
            (self.root / f'boot{index:02d}').mkdir()
            os.utime(self.root / f'boot{index:02d}', (index, index))
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0')
        mirror.directory.mkdir()
        mirror.prune()
        left = sorted(d.name for d in self.root.iterdir())
        self.assertEqual(len(left), diagnostics.LOG_BOOTS)
        self.assertIn('current0', left)
        self.assertNotIn('boot00', left)

    def test_network_counters_parse_snmp(self):
        with patch('diagnostics.Path.read_text', return_value=(
                'Tcp: RtoAlgorithm OutSegs RetransSegs\nTcp: 1 500 7\n'
                'Udp: InDatagrams RcvbufErrors\nUdp: 900 3\n')):
            self.assertEqual(diagnostics.net_counters(),
                             {'tcp_out': 500, 'tcp_retrans': 7, 'udp_in': 900, 'udp_rcvbuf_err': 3})



    def test_prune_keeps_total_under_the_byte_budget(self):
        # Eight boots of five 40 MB logs would ask 1.6 GB of a small eMMC; the oldest go first.
        for index in range(4):
            boot = self.root / f'boot{index:02d}'
            boot.mkdir()
            (boot / 'receiver.log').write_text('x' * 200)
            os.utime(boot, (index, index))
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0')
        mirror.directory.mkdir()
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 500):
            mirror.prune()
        left = sorted(d.name for d in self.root.iterdir())
        self.assertIn('current0', left)
        self.assertNotIn('boot00', left, 'the oldest boot goes when the budget is exceeded')
        kept = sum(f.stat().st_size for f in self.root.rglob('*') if f.is_file())
        self.assertLessEqual(kept, 500)

    def test_offsets_are_written_atomically_and_the_journal_waits_its_turn(self):
        receiver = self.run / 'receiver.log'
        receiver.write_text('one\n')
        mirror = diagnostics.LogMirror(self.root, self.run, 'abcdef0123456789')
        mirror.directory.mkdir()
        with patch('diagnostics.subprocess.run') as run:
            run.return_value = Mock(stdout=b'')
            mirror.cycle({}, {})
            mirror.cycle({}, {})
        self.assertEqual(run.call_count, 1, 'journalctl runs at most once per JOURNAL_EVERY')
        self.assertEqual(list(mirror.directory.glob('*.tmp')), [], 'no partial offsets file left behind')
        inode = (self.run / 'receiver.log').stat().st_ino
        self.assertEqual(mirror.load(), {'receiver.log': [inode, 4]})


class CarHotspotLagTests(unittest.TestCase):
    @patch('diagnostics.subprocess.run')
    def test_ping_burst_and_tcp_state(self, run):
        run.return_value = Mock(stdout='5 packets transmitted, 4 received, 20% packet loss, time 410ms\n'
                                       'rtt min/avg/max/mdev = 2.1/48.5/180.0/60.2 ms\n')
        self.assertEqual(diagnostics.ping_stats('10.0.0.5'), {'loss': 20.0, 'min': 2.1, 'avg': 48.5, 'max': 180.0})
        run.return_value = Mock(stdout='ESTAB 0 0 10.0.0.2:7000 10.0.0.5:51000\n'
                                       '\t cubic rtt:85.2/20.1 ato:40 bytes_received:123456 retrans:0/7 rcv_rtt:120.5 lost:2\n')
        self.assertEqual(diagnostics.tcp_stats('10.0.0.5'),
                         [{'local': '7000', 'rtt': 85.2, 'rcv_rtt': 120.5, 'retrans': 7.0, 'rx_bytes': 123456.0, 'lost': 2.0}])
        run.return_value = Mock(stdout='5 packets transmitted, 0 received, 100% packet loss, time 4100ms\n')
        self.assertEqual(diagnostics.ping_stats('10.0.0.5'), {'loss': 100.0})

    @patch('diagnostics.subprocess.run')
    def test_phone_address_on_the_current_network(self, run):
        import ipaddress
        run.return_value = Mock(stdout='default via 192.168.43.1 dev wlx proto dhcp\n')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'receiver').mkdir()
            (state / 'receiver/phone-presence.json').write_text(json.dumps({'phones': {'02:00:00:00:00:E2': {'networks': {
                '192.168.78.0/24': {'ip': '192.168.78.63'}, '192.168.43.0/24': {'ip': '192.168.43.123'}}}}}))
            targets = diagnostics.ping_targets(state, [ipaddress.ip_network('192.168.43.0/24')])
        self.assertEqual(targets, {'gateway': '192.168.43.1', 'phone': '192.168.43.123'})


if __name__ == '__main__':
    unittest.main()
