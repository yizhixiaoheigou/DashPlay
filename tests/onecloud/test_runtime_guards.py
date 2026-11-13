"""Failure boundaries for the small 11.12 runtime fixes; no hardware is changed."""
import errno
from html import unescape
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import diagnostics
import own_domain
import platform_debian
import settings_ui
import supervisor


class RuntimeGuardTests(unittest.TestCase):
    def test_disappearing_child_does_not_interrupt_stop_or_reap(self):
        for method in ('stop_child', 'stop_all'):
            for stage in ('term', 'kill'):
                with self.subTest(method=method, stage=stage):
                    platform = platform_debian.DebianPlatform('test0')
                    child = Mock(pid=123, poll=Mock(return_value=None))
                    child.wait.side_effect = ([subprocess.TimeoutExpired('test', 8), 0]
                                              if stage == 'kill' else [0])
                    platform.children = [('receiver', child)]
                    signals = [None, ProcessLookupError()] if stage == 'kill' else [ProcessLookupError()]
                    with patch.object(platform_debian.os, 'killpg', side_effect=signals) as send:
                        getattr(platform, method)(*(['receiver'] if method == 'stop_child' else []))
                    self.assertEqual(child.wait.call_count, 2 if stage == 'kill' else 1)
                    self.assertEqual(send.call_args.args[1], signal.SIGKILL if stage == 'kill' else signal.SIGTERM)

    def test_unrelated_signal_errors_are_not_hidden(self):
        platform = platform_debian.DebianPlatform('test0')
        platform.children = [('receiver', Mock(pid=123, poll=Mock(return_value=None)))]
        with patch.object(platform_debian.os, 'killpg', side_effect=PermissionError('not ours')):
            with self.assertRaises(PermissionError):
                platform.stop_all()

    def test_certificate_probe_failure_can_retry_without_issuing(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = own_domain.Maintainer(Path(directory), {'domain': 'test', 'token': 'synthetic'},
                                          '192.0.2.1', threading.Event(), say=lambda _: None)
            worker.state.mkdir()
            for failed_probe in (0, 1):
                for error in (OSError('synthetic openssl failure'), subprocess.TimeoutExpired('openssl', 5)):
                    with self.subTest(probe=failed_probe, error=type(error).__name__):
                        answers = [error] if failed_probe == 0 else [False, error]
                        with patch.object(own_domain, 'certificate_for', side_effect=answers + [True]), \
                                patch.object(own_domain, 'network_ready', return_value=None), \
                                patch.object(worker, 'obtain') as obtain:
                            self.assertEqual(worker.check_certificate(), own_domain.CERT_CHECK)
                            obtain.assert_not_called()
                            self.assertEqual(worker.check_certificate(), own_domain.CERT_CHECK)
                        self.assertEqual(own_domain.read_status(worker.state)['phase'], 'ready')

    def test_phone_confirmation_escapes_old_javascript_line_separators(self):
        name = 'Phone\u2028line\u2029end"<&'
        html = settings_ui.phone_rows({'AA:BB:CC:DD:EE:FF': name}, None, None, 'nonce', True)
        handler = unescape(re.search(r'onclick="([^"]+)"', html)[1])
        self.assertNotIn('\u2028', handler)
        self.assertNotIn('\u2029', handler)
        self.assertTrue(handler.startswith('return confirm('))
        self.assertIn(name, json.loads(handler[len('return confirm('):-1]))

    def test_one_failed_log_does_not_skip_others_or_checkpoint(self):
        for failure in ('read', 'append', 'sync'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run = root / 'run'; run.mkdir()
                source = run / 'a.log'; source.write_bytes(b'a\n')
                (run / 'b.log').write_bytes(b'b\n')
                mirror = diagnostics.LogMirror(root / 'logs', run, 'boot')
                mirror.directory.mkdir(parents=True)
                offsets, handles = {}, {}
                original_open, original_append, original_sync = Path.open, mirror.append, os.fsync
                def open_(path, *args, **kwargs):
                    if failure == 'read' and path == source:
                        raise OSError(errno.EIO, 'synthetic read error')
                    return original_open(path, *args, **kwargs)
                def append(name, data, files):
                    if failure == 'append' and name == 'a.log':
                        raise OSError(errno.ENOSPC, 'synthetic write error')
                    original_append(name, data, files)
                def sync(fd):
                    if failure == 'sync' and fd == handles.get('a.log'):
                        raise OSError(errno.EIO, 'synthetic sync error')
                    original_sync(fd)
                try:
                    with patch.object(Path, 'open', new=open_), patch.object(mirror, 'append', side_effect=append), \
                            patch.object(os, 'fsync', side_effect=sync), \
                            patch.object(diagnostics.subprocess, 'run', return_value=Mock(stdout=b'journal\n')), \
                            patch('builtins.print', side_effect=OSError('output failed too')):
                        mirror.cycle(offsets, handles)
                    self.assertEqual((mirror.directory / 'b.log').read_bytes(), b'b\n')
                    self.assertEqual((mirror.directory / 'journal.log').read_bytes(), b'journal\n')
                    self.assertEqual(mirror.load()['b.log'][1], 2)
                    self.assertNotIn('a.log', mirror.load(), 'failed data must not advance its saved offset')
                finally:
                    for fd in handles.values():
                        os.close(fd)

    def test_stuck_udev_and_missing_interface_share_one_deadline(self):
        now = [0.0]
        def settle(command, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 11)
            now[0] += kwargs['timeout']
            raise subprocess.TimeoutExpired(command, kwargs['timeout'])
        def sleep(seconds):
            now[0] += seconds
        with patch.object(supervisor.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(supervisor.time, 'sleep', side_effect=sleep), \
                patch.object(supervisor.subprocess, 'run', side_effect=settle), \
                patch.object(supervisor.setup_wizard, 'wifi_interface', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'No Wi-Fi interface'):
                supervisor.wait_for_wifi_interface()
        self.assertEqual(now[0], 60)

    def test_interface_appearing_during_wait_is_selected(self):
        with patch.object(supervisor.subprocess, 'run'), patch.object(supervisor.time, 'sleep'), \
                patch.object(supervisor.setup_wizard, 'wifi_interface', side_effect=[None, 'test0']), \
                patch.object(supervisor.Path, 'exists', return_value=True):
            self.assertEqual(supervisor.wait_for_wifi_interface(), 'test0')

    def test_wifi_discovery_preserves_the_device_settle_window(self):
        now = [0.0]
        def settle(command, **kwargs):
            limit = int(command[-1].split('=')[1])
            now[0] += min(limit, max(0, 4-now[0]))
            return Mock(returncode=0 if now[0] >= 4 else 1)
        with patch.object(supervisor.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(supervisor.subprocess, 'run', side_effect=settle), \
                patch.object(supervisor.setup_wizard, 'wifi_interface', return_value='wlan-test'), \
                patch.object(Path, 'exists', return_value=True):
            self.assertEqual(supervisor.wait_for_wifi_interface(), 'wlan-test')
        self.assertGreaterEqual(now[0], 4, 'a visible interface is not evidence that udev finished')
