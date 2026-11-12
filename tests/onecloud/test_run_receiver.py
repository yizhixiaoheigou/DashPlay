import importlib.util
import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[2] / "scripts/onecloud/run-receiver.py"
spec = importlib.util.spec_from_file_location("run_receiver", HERE)
run_receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_receiver)


class BrowserAccessTest(unittest.TestCase):
    def test_heap_ceiling_follows_the_board_memory(self):
        self.assertEqual(run_receiver.heap_limit("MemTotal:         402316 kB\nMemFree: 1 kB\n"), "-Xmx224m")   # 410 stick
        self.assertEqual(run_receiver.heap_limit("MemTotal:         855012 kB\n"), "-Xmx384m")   # OneCloud
        self.assertEqual(run_receiver.heap_limit("MemTotal:        1897396 kB\n"), "-Xmx384m")   # N1
        self.assertEqual(run_receiver.heap_limit(""), "-Xmx384m")

    def test_token_goes_to_a_file_only_the_box_page_reads_and_the_old_script_goes(self):
        # A script any site could include gave the viewer token away (audit 2026-10-13, A2); the receiver serves
        # receiver-access.json to the box's own page only.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "playport/web/dist"
            dist.mkdir(parents=True)
            (dist / "receiver-access.js").write_text("localStorage.setItem('playport-token','old');\n")
            run_receiver.write_browser_access(root, "0123456789abcdef0123456789abcdef", 18080)
            self.assertFalse((dist / "receiver-access.js").exists())
            access = dist / "receiver-access.json"
            self.assertEqual(json.loads(access.read_text()),
                             {"token": "0123456789abcdef0123456789abcdef", "settingsPort": 18080})
            self.assertEqual(access.stat().st_mode & 0o777, 0o644)
            self.assertEqual(sorted(p.name for p in dist.iterdir()), ["receiver-access.json"])

    def page_flags(self, **flags):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "playport/web/dist").mkdir(parents=True)
            run_receiver.write_browser_access(root, "0123456789abcdef0123456789abcdef", 18080, **flags)
            access = json.loads((root / "playport/web/dist/receiver-access.json").read_text())
            return {key: value for key, value in access.items() if key not in ("token", "settingsPort")}

    def test_music_over_the_car_bluetooth_reaches_the_page_alone(self):
        self.assertEqual(self.page_flags(car_music=True), {"carMusic": True})

    def test_tesla_mode_brings_the_car_music_with_it(self):
        self.assertEqual(self.page_flags(tesla=True), {"carMusic": True, "tesla": True})

    def test_tesla_browser_can_leave_audio_in_the_webpage(self):
        self.assertEqual(self.page_flags(tesla_browser=True), {"tesla": True, "carMusic": False})

    def test_calls_over_the_car_bluetooth_reach_the_page(self):
        # The page greys out its Siri and call levels then: those sounds are the car's (2026-10-29).
        self.assertEqual(self.page_flags(car_voice=True), {"carVoice": True})
        self.assertEqual(self.page_flags(tesla=True, car_voice=True), {"carMusic": True, "tesla": True, "carVoice": True})
        self.assertEqual(self.page_flags(tesla_browser=True, car_voice=True), {"tesla": True, "carMusic": False, "carVoice": True})

    def test_rejects_an_unexpected_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_receiver.write_browser_access(Path(directory), "not-a-token")


class RedactionTest(unittest.TestCase):
    def test_a_password_the_trace_escaped_is_still_hidden(self):
        # The iAP2 trace prints strings escaped (\" and \\): a password with those characters was not found by a
        # plain replace and stayed in receiver.log (review 2026-10-12).
        password = 'ab"c\\d e'
        line = '  [2] passphrase: "ab\\"c\\\\d e"\n'
        self.assertEqual(run_receiver.redact(line, "f" * 32, password), '  [2] passphrase: "[REDACTED]"\n')

    def test_token_password_and_raw_preview(self):
        token, password = "0123456789abcdef0123456789abcdef", "secret-pass"
        self.assertEqual(run_receiver.redact(f"url ?token={token} pw={password}\n", token, password),
                         "url ?token=[REDACTED] pw=[REDACTED]\n")
        self.assertEqual(run_receiver.redact("  raw-body=[00 01 02]\n", token, password), "  raw-body=[not logged]\n")


class CacheStampTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        state = self.root / 'state/receiver'
        state.mkdir(parents=True)
        (self.root / 'playport/web/dist').mkdir(parents=True)
        (self.root / 'launcher').mkdir()
        jar = self.root / 'launcher/onecloud-launcher.jar'
        jar.write_bytes(b'synthetic jar')
        self.fingerprint = f'{jar.name} {jar.stat().st_size} {jar.stat().st_mtime_ns}'
        self.stamp, self.archive = state / 'app-cds.classpath', state / 'app-cds.jsa'
        self.stamp.write_text('old fingerprint')
        self.archive.write_bytes(b'old cache')
        (self.root / 'state/hotspot.json').write_text(json.dumps({'ssid': 'test', 'passphrase': 'synthetic-password'}))
        (state / 'browser-token').write_text('f' * 32)
        read_text = Path.read_text
        def read(path, *args, **kwargs):
            return 'MemTotal: 1024000 kB\n' if str(path) == '/proc/meminfo' else read_text(path, *args, **kwargs)
        argv = ['run-receiver.py', '--root', str(self.root), '--bind', '127.0.0.1',
                '--interface', 'test0', '--channel', '6', '--java', str(self.root / 'jre/bin/java')]
        for replacement in (patch.object(sys, 'argv', argv), patch.object(Path, 'read_text', new=read),
                            patch.object(run_receiver.signal, 'signal'), patch.object(run_receiver.os, 'umask')):
            replacement.start()
            self.addCleanup(replacement.stop)
        replacement = patch.object(run_receiver.subprocess, 'Popen')
        self.popen = replacement.start()
        self.addCleanup(replacement.stop)
        process = self.popen.return_value.__enter__.return_value
        process.pid, process.stdout = 123, []
        process.wait.return_value = 0

    def start_receiver(self):
        with self.assertRaises(SystemExit) as stopped:
            run_receiver.main()
        self.assertEqual(stopped.exception.code, 0)

    def test_stamp_is_synced_and_published_after_cache_invalidation(self):
        events, stamp_fd = [], None
        open_file, sync, replace = os.open, os.fsync, os.replace
        def opened(path, *args, **kwargs):
            nonlocal stamp_fd
            fd = open_file(path, *args, **kwargs)
            if Path(path) == self.stamp.with_name(self.stamp.name + '.tmp'):
                stamp_fd = fd
            return fd
        def synced(fd):
            if fd == stamp_fd and stat.S_ISREG(os.fstat(fd).st_mode):
                self.assertEqual(self.stamp.read_text(), 'old fingerprint')
                events.append('file sync')
            elif events and stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append('directory sync')
            sync(fd)
        def publish(source, target):
            if Path(target) == self.stamp:
                self.assertFalse(self.archive.exists(), 'invalidate stale cache before publishing its new fingerprint')
                self.assertEqual(Path(source).read_text(), self.fingerprint)
                events.append('replace')
            replace(source, target)
        with patch.object(os, 'open', side_effect=opened), patch.object(os, 'fsync', side_effect=synced), \
                patch.object(os, 'replace', side_effect=publish):
            self.start_receiver()
        self.assertEqual(events, ['file sync', 'replace', 'directory sync'])
        self.assertEqual(self.stamp.read_text(), self.fingerprint)
        self.archive.write_bytes(b'rebuilt cache')
        self.start_receiver()
        self.assertEqual(self.archive.read_bytes(), b'rebuilt cache', 'unchanged programs must keep the cache')

    def test_failed_stamp_replace_preserves_complete_old_record_and_can_retry(self):
        replace = os.replace
        def publish(source, target):
            if Path(target) == self.stamp:
                raise OSError('synthetic stamp replace failure')
            replace(source, target)
        with patch.object(os, 'replace', side_effect=publish):
            with self.assertRaisesRegex(OSError, 'stamp replace failure'):
                run_receiver.main()
        self.assertEqual(self.stamp.read_text(), 'old fingerprint')
        self.popen.assert_not_called()
        self.start_receiver()
        self.assertEqual(self.stamp.read_text(), self.fingerprint)

    def test_failed_stamp_sync_preserves_complete_old_record(self):
        open_file, sync, stamp_fd = os.open, os.fsync, None
        def opened(path, *args, **kwargs):
            nonlocal stamp_fd
            fd = open_file(path, *args, **kwargs)
            if Path(path) == self.stamp.with_name(self.stamp.name + '.tmp'):
                stamp_fd = fd
            return fd
        def synced(fd):
            if fd == stamp_fd:
                raise OSError('synthetic stamp sync failure')
            sync(fd)
        with patch.object(os, 'open', side_effect=opened), patch.object(os, 'fsync', side_effect=synced):
            with self.assertRaisesRegex(OSError, 'stamp sync failure'):
                run_receiver.main()
        self.assertEqual(self.stamp.read_text(), 'old fingerprint')
        self.popen.assert_not_called()


class ReceiverOutputTest(unittest.TestCase):
    TOKEN = 'f' * 32
    PASSWORD = 'synthetic-hotspot-password'

    def run_with_output(self, output, write, exit_code=0):
        """Exercise main with a real pipe producer in place of Java; never open receiver ports."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state/receiver'
            state.mkdir(parents=True)
            (root / 'playport/web/dist').mkdir(parents=True)
            (root / 'launcher').mkdir()
            (root / 'launcher/onecloud-launcher.jar').write_bytes(b'synthetic fixture')
            (root / 'state/hotspot.json').write_text(json.dumps({'ssid': 'test', 'passphrase': self.PASSWORD}))
            (state / 'browser-token').write_text(self.TOKEN)
            done = root / 'producer-finished'
            producer = ('import sys\nfrom pathlib import Path\n'
                        f'sys.stdout.write({output!r})\nsys.stdout.flush()\n'
                        f'Path({str(done)!r}).write_text("finished")\nsys.exit({exit_code})\n')
            script = root / 'producer.py'
            script.write_text(producer)
            popen, read_text = subprocess.Popen, Path.read_text
            processes = []
            def launch(_command, **kwargs):
                process = popen([sys.executable, str(script)], **kwargs)
                processes.append(process)
                return process
            def read(path, *args, **kwargs):
                if str(path) == '/proc/meminfo':
                    return 'MemTotal: 1024000 kB\n'
                return read_text(path, *args, **kwargs)
            argv = ['run-receiver.py', '--root', str(root), '--bind', '127.0.0.1',
                    '--interface', 'test0', '--channel', '6', '--java', str(root / 'jre/bin/java')]
            try:
                with patch.object(sys, 'argv', argv), patch.object(Path, 'read_text', new=read), \
                        patch.object(run_receiver.signal, 'signal'), patch.object(run_receiver.os, 'umask'), \
                        patch.object(run_receiver.subprocess, 'Popen', side_effect=launch), \
                        patch('builtins.print', side_effect=write), patch.object(run_receiver.time, 'monotonic', return_value=12.5):
                    with self.assertRaises(SystemExit) as stopped:
                        run_receiver.main()
                self.assertEqual(stopped.exception.code, exit_code)
                self.assertEqual(len(processes), 1)
                self.assertEqual(processes[0].returncode, exit_code)
                self.assertEqual(done.read_text(), 'finished', 'the producer must finish draining before main exits')
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)

    def test_unwritable_output_keeps_draining_and_preserves_exit_code(self):
        # More than a pipe buffer: stopping the read loop cannot let this producer finish normally.
        output = ('synthetic log ' + 'x' * 500 + '\n') * 2048
        for error in (OSError(errno.ENOSPC, 'synthetic full disk'),
                      BrokenPipeError('synthetic broken output'), ValueError('synthetic closed output')):
            with self.subTest(error=type(error).__name__):
                def fail(*args, **kwargs):
                    raise error
                self.run_with_output(output, fail, exit_code=23)

    def test_output_recovers_on_later_lines_and_stays_redacted(self):
        written = []
        attempts = 0
        def write(text, *, end='\n', flush=False):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(errno.ENOSPC, 'synthetic full disk')
            self.assertTrue(flush)
            written.append(text + end)
        self.run_with_output(f'first\ntoken={self.TOKEN} password={self.PASSWORD}\nCarPlay is live\n', write)
        self.assertEqual(''.join(written), 'token=[REDACTED] password=[REDACTED]\nCarPlay is live\nboot 12.50s CarPlay live\n')

    def test_failed_milestone_output_does_not_stop_later_lines(self):
        written = []
        def write(text, *, end='\n', flush=False):
            if text == 'boot 12.50s receiver listening':
                raise OSError(errno.ENOSPC, 'synthetic milestone write failure')
            written.append(text + end)
        self.run_with_output('RTSP listening\nCarPlay is live\nlast line\n', write)
        self.assertEqual(''.join(written), 'RTSP listening\nCarPlay is live\nboot 12.50s CarPlay live\nlast line\n')


if __name__ == "__main__":
    unittest.main()
