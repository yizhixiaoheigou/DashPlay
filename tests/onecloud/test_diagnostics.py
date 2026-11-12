import json
import os
import stat
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

    def test_write_failure_and_broken_error_output_do_not_stop_diagnostics(self):
        for fail_at in (1, 2):  # First startup event, or the first periodic record.
            with self.subTest(fail_at=fail_at):
                log = diagnostics.DiagLog(self.dir / str(fail_at))
                append, calls = log.append, []
                def write(record):
                    calls.append(record)
                    if len(calls) == fail_at:
                        raise OSError(28, 'synthetic disk full')
                    append(record)
                platform = Mock()
                platform.boot_id.return_value = 'synthetic-boot'
                platform.diagnostics.return_value = {'sample': 'recovered'}
                stopped = Mock()
                stopped.wait.side_effect = [False, False, True]
                with patch.object(log, 'append', side_effect=write), \
                        patch('diagnostics.threading.Thread') as thread, \
                        patch('diagnostics.net_counters', return_value={}), \
                        patch('builtins.print', side_effect=BrokenPipeError('synthetic closed output')):
                    diagnostics.start(stopped, log, Path(self.tmp.name) / 'receiver.log', platform, {'mode': 'sta'})
                    thread.return_value.start.assert_called_once()
                    thread.call_args.kwargs['target']()
                self.assertEqual(len(calls), 3)
                self.assertEqual(json.loads(log.text().splitlines()[-1])['sample'], 'recovered')



class LogMirrorTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.run = Path(tmp.name) / 'logs', Path(tmp.name) / 'run'
        self.root.mkdir()
        self.run.mkdir()

    def test_startup_storage_failures_are_retried_in_worker_and_logs_resume(self):
        (self.run / 'receiver.log').write_text('synthetic recovered log\n')
        for stage in ('mkdir', 'utime', 'prune'):
            with self.subTest(stage=stage):
                mirror = diagnostics.LogMirror(self.root, self.run, 'case-' + stage, journal=False)
                stopped = Mock()
                stopped.wait.side_effect = [False, True]
                target = {'mkdir': Path, 'utime': diagnostics.os, 'prune': mirror}[stage]
                original = getattr(target, stage)
                attempts = []
                def fail_once(*args, **kwargs):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise OSError(28, 'synthetic disk full')
                    return original(*args, **kwargs)
                opened = []
                open_file = os.open
                def track_open(*args, **kwargs):
                    fd = open_file(*args, **kwargs)
                    opened.append((Path(args[0]), fd))
                    return fd
                with patch.object(target, stage, autospec=True, side_effect=fail_once), \
                        patch('diagnostics.threading.Thread') as thread, \
                        patch('diagnostics.os.open', side_effect=track_open), \
                        patch('builtins.print', side_effect=OSError('synthetic output full')):
                    mirror.start(stopped)
                    thread.return_value.start.assert_called_once()
                    self.assertEqual(attempts, [], 'storage setup must not run on the supervisor thread')
                    thread.call_args.kwargs['target']()
                self.assertGreaterEqual(len(attempts), 2)
                self.assertEqual((mirror.directory / 'receiver.log').read_text(), 'synthetic recovered log\n')
                self.assertEqual(sum(path.name == 'receiver.log' for path, _ in opened), 1)
                for _, fd in opened:
                    with self.assertRaises(OSError):
                        os.fstat(fd)

    def test_persistent_storage_failure_can_stop_without_running_a_copy_cycle(self):
        mirror = diagnostics.LogMirror(self.root, self.run, 'unwritable', journal=False)
        stopped = Mock()
        stopped.wait.return_value = True
        with patch('diagnostics.Path.mkdir', side_effect=OSError(30, 'synthetic readonly disk')), \
                patch('diagnostics.threading.Thread') as thread, patch.object(mirror, 'cycle') as cycle, \
                patch('builtins.print', side_effect=BrokenPipeError('synthetic closed output')):
            mirror.start(stopped)
            thread.call_args.kwargs['target']()
        cycle.assert_not_called()
        stopped.wait.assert_called_once_with(diagnostics.MIRROR_INTERVAL)

    def test_cycle_failure_and_broken_output_do_not_end_mirror_worker(self):
        mirror = diagnostics.LogMirror(self.root, self.run, 'runtime', journal=False)
        stopped = Mock()
        stopped.wait.side_effect = [False, True]
        with patch.object(mirror, 'cycle', side_effect=[OSError(28, 'synthetic full disk'), None, None]) as cycle, \
                patch('diagnostics.threading.Thread') as thread, \
                patch('builtins.print', side_effect=OSError('synthetic output full')):
            mirror.start(stopped)
            thread.call_args.kwargs['target']()
        self.assertEqual(cycle.call_count, 3, 'the next periodic cycle and final shutdown cycle still run')

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

    def offset_fixture(self):
        source = self.run / 'receiver.log'
        source.write_text('first\n')
        mirror = diagnostics.LogMirror(self.root, self.run, 'offsets', journal=False)
        mirror.directory.mkdir()
        offsets, handles = {}, {}
        self.addCleanup(lambda: [os.close(fd) for fd in handles.values()])
        mirror.cycle(offsets, handles)
        before = mirror.state_file.read_bytes()
        with source.open('a') as out:
            out.write('second\n')
        return mirror, offsets, handles, before

    def test_partial_progress_write_preserves_last_complete_checkpoint(self):
        mirror, offsets, handles, before = self.offset_fixture()
        open_file = Path.open
        class FailingWriter:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def __getattr__(self, key): return getattr(self.stream, key)
            def write(self, text):
                self.stream.write(text[:3])
                self.stream.flush()
                raise OSError('synthetic interrupted progress write')
        def open_(path, mode='r', *args, **kwargs):
            stream = open_file(path, mode, *args, **kwargs)
            if 'w' in mode and path.parent == mirror.directory and path.name.startswith('.offsets'):
                return FailingWriter(stream)
            return stream
        with patch.object(Path, 'open', new=open_):
            with self.assertRaisesRegex(OSError, 'interrupted progress write'):
                mirror.cycle(offsets, handles)
        self.assertEqual(mirror.state_file.read_bytes(), before)
        self.assertEqual(mirror.load()['receiver.log'][1], len(b'first\n'))
        # A retry in this process publishes the already-copied bytes without appending them again.
        mirror.cycle(offsets, handles)
        self.assertEqual(mirror.load()['receiver.log'][1], len(b'first\nsecond\n'))
        self.assertEqual((mirror.directory / 'receiver.log').read_bytes(), b'first\nsecond\n')

    def test_progress_replace_failure_keeps_old_checkpoint(self):
        mirror, offsets, handles, before = self.offset_fixture()
        with patch.object(diagnostics.os, 'replace', side_effect=OSError('synthetic rename failure')):
            with self.assertRaisesRegex(OSError, 'rename failure'):
                mirror.cycle(offsets, handles)
        self.assertEqual(mirror.state_file.read_bytes(), before)

    def test_logs_then_progress_then_directory_are_synced(self):
        mirror, offsets, handles, before = self.offset_fixture()
        events = []
        fsync, replace = os.fsync, os.replace
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append('directory')
            elif fd in handles.values():
                events.append('log')
            else:
                events.append('progress')
                self.assertEqual(mirror.state_file.read_bytes(), before)
            fsync(fd)
        def publish(source, target):
            events.append('replace')
            self.assertEqual(json.loads(Path(source).read_text())['receiver.log'][1], len(b'first\nsecond\n'))
            replace(source, target)
        with patch.object(diagnostics.os, 'fsync', side_effect=sync), \
                patch.object(diagnostics.os, 'replace', side_effect=publish):
            mirror.cycle(offsets, handles)
        self.assertEqual(events, ['log', 'progress', 'replace', 'directory'])

    def test_progress_sync_failure_keeps_old_checkpoint(self):
        mirror, offsets, handles, before = self.offset_fixture()
        fsync = os.fsync
        def sync(fd):
            if fd not in handles.values() and not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('synthetic checkpoint sync failure')
            fsync(fd)
        with patch.object(diagnostics.os, 'fsync', side_effect=sync):
            with self.assertRaisesRegex(OSError, 'checkpoint sync failure'):
                mirror.cycle(offsets, handles)
        self.assertEqual(mirror.state_file.read_bytes(), before)

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

    def saved_boot(self, name, size, modified):
        directory = self.root / name
        (directory / 'nested').mkdir(parents=True)
        (directory / 'nested/receiver.log').write_bytes(b'x' * size)
        os.utime(directory, (modified, modified))
        return directory

    def test_byte_budget_keeps_exact_fit_then_removes_oldest_boot(self):
        oldest = self.saved_boot('z-oldest', 60, 1)
        newest = self.saved_boot('a-newest', 25, 2)
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0', journal=False)
        mirror.directory.mkdir()
        current = mirror.directory / 'receiver.log'
        current.write_bytes(b'x' * 15)
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 100, create=True):
            mirror.prune()
            self.assertTrue(oldest.exists(), 'an exact fit must not discard history')
            current.write_bytes(b'x' * 16)
            mirror.prune()
        self.assertFalse(oldest.exists())
        self.assertTrue(newest.exists())
        self.assertEqual(current.stat().st_size, 16)

    def test_byte_and_boot_count_budgets_keep_newest_history(self):
        for index in range(11):
            self.saved_boot(f'boot{index:02d}', 10, index)
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0', journal=False)
        mirror.directory.mkdir()
        (mirror.directory / 'receiver.log').write_bytes(b'x' * 15)
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 35, create=True):
            mirror.prune()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ['boot09', 'boot10', 'current0'])

    def test_running_mirror_reclaims_history_as_current_logs_grow(self):
        for journal in (False, True):
            with self.subTest(journal=journal), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                old = root / 'old-boot'
                old.mkdir()
                (old / 'receiver.log').write_bytes(b'x' * 600)
                source = self.run / 'receiver.log'
                source.write_bytes(b'x' * 99 + b'\n')
                mirror = diagnostics.LogMirror(root, self.run, 'current0', journal=journal)
                mirror.directory.mkdir()
                offsets, handles = {}, {}
                try:
                    with patch.object(diagnostics, 'LOG_TOTAL_MAX', 1000, create=True), \
                            patch('diagnostics.subprocess.run', return_value=Mock(stdout=b'journal\n')):
                        mirror.cycle(offsets, handles)
                        self.assertTrue(old.exists())
                        with source.open('ab') as log:
                            log.write(b'x' * 499 + b'\n')
                        mirror.cycle(offsets, handles)
                        mirror.cycle(offsets, handles)  # the next round checks the new total before writing
                    self.assertFalse(old.exists(), 'cleanup must also run after startup')
                    self.assertEqual((mirror.directory / 'receiver.log').read_bytes(), source.read_bytes())
                    self.assertEqual(mirror.load()['receiver.log'][1], 600)
                finally:
                    for fd in handles.values():
                        os.close(fd)

    def test_current_boot_is_preserved_even_if_it_alone_exceeds_budget(self):
        old = self.saved_boot('old-boot', 60, 1)
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0', journal=False)
        mirror.directory.mkdir()
        current = mirror.directory / 'receiver.log'
        current.write_bytes(b'x' * 150)
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 100, create=True):
            mirror.prune()
        self.assertFalse(old.exists())
        self.assertEqual(current.read_bytes(), b'x' * 150)

    def test_failed_deletion_is_not_counted_as_freed_space(self):
        oldest = self.saved_boot('oldest', 600, 1)
        next_oldest = self.saved_boot('next-old', 400, 2)
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0', journal=False)
        mirror.directory.mkdir()
        (mirror.directory / 'receiver.log').write_bytes(b'x' * 100)
        remove = diagnostics.shutil.rmtree
        def fail_oldest(path, *args, **kwargs):
            if path == oldest:
                if kwargs.get('ignore_errors'):
                    return
                raise OSError('synthetic deletion failure')
            return remove(path, *args, **kwargs)
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 1000, create=True), \
                patch('diagnostics.shutil.rmtree', side_effect=fail_oldest), \
                patch('builtins.print', side_effect=OSError('synthetic output full')):
            mirror.prune()
        self.assertTrue(oldest.exists())
        self.assertFalse(next_oldest.exists(), 'failed removal must not pretend the budget is satisfied')

    def test_cleanup_does_not_follow_directory_symlinks(self):
        outside = self.run / 'outside'
        outside.mkdir()
        (outside / 'keep.log').write_bytes(b'x' * 1000)
        link = self.root / 'linked'
        link.symlink_to(outside, target_is_directory=True)
        old = self.saved_boot('old-boot', 60, 1)
        mirror = diagnostics.LogMirror(self.root, self.run, 'current0', journal=False)
        mirror.directory.mkdir()
        (mirror.directory / 'receiver.log').write_bytes(b'x' * 30)
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 100, create=True):
            mirror.prune()
            self.assertTrue(old.exists(), 'external content is not part of the mirror budget')
        with patch.object(diagnostics, 'LOG_TOTAL_MAX', 50, create=True):
            mirror.prune()
        self.assertFalse(old.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / 'keep.log').stat().st_size, 1000)

    def test_network_counters_parse_snmp(self):
        with patch('diagnostics.Path.read_text', return_value=(
                'Tcp: RtoAlgorithm OutSegs RetransSegs\nTcp: 1 500 7\n'
                'Udp: InDatagrams RcvbufErrors\nUdp: 900 3\n')):
            self.assertEqual(diagnostics.net_counters(),
                             {'tcp_out': 500, 'tcp_retrans': 7, 'udp_in': 900, 'udp_rcvbuf_err': 3})



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
