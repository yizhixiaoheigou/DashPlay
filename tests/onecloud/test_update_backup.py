"""User-data backup durability: preserve the old archive until the new one is synced; fail before installing."""
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from test_update import update


class BackupDurabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'dashplay'
        (self.root / 'state').mkdir(parents=True)
        (self.root / 'state/options.json').write_text('{"fixture": true}')
        self.target = self.root.parent / update.BACKUP
        self.target.write_bytes(b'previous backup')

    def test_complete_archive_is_synced_before_replacement_and_directory_before_success(self):
        events = []
        fsync, replace = os.fsync, os.replace
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                events.append('directory')
                archive = self.target
            else:
                events.append('file')
                self.assertEqual(self.target.read_bytes(), b'previous backup')
                archive = self.target.with_suffix('.tmp')
            # The tar/gzip footer is already complete when sync is requested.
            with tarfile.open(archive) as saved:
                self.assertEqual(saved.extractfile('state/options.json').read(), b'{"fixture": true}')
            fsync(fd)
        def publish(source, target):
            events.append('replace')
            replace(source, target)
        with patch.object(update.os, 'fsync', side_effect=sync), patch.object(update.os, 'replace', side_effect=publish):
            self.assertEqual(update.backup(self.root, False), self.target)
        self.assertEqual(events, ['file', 'replace', 'directory'])
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_file_sync_failure_preserves_the_previous_backup(self):
        with patch.object(update.os, 'fsync', side_effect=OSError('synthetic storage failure')):
            with self.assertRaisesRegex(OSError, 'synthetic storage failure'):
                update.backup(self.root, False)
        self.assertEqual(self.target.read_bytes(), b'previous backup')

    def test_directory_sync_failure_is_reported_and_descriptor_closed(self):
        fsync = os.fsync
        directory_fds = []
        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                directory_fds.append(fd)
                raise OSError('synthetic directory failure')
            fsync(fd)
        with patch.object(update.os, 'fsync', side_effect=sync):
            with self.assertRaisesRegex(OSError, 'synthetic directory failure'):
                update.backup(self.root, True)
        self.assertEqual(len(directory_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(directory_fds[0])
        # Rename already happened, but the caller must not treat this archive as durably published.
        with tarfile.open(self.target) as saved:
            self.assertIn('state/options.json', saved.getnames())

    def test_any_backup_sync_failure_stops_both_installers(self):
        fsync = os.fsync
        for openwrt in (False, True):
            for fail_directory in (False, True):
                with self.subTest(openwrt=openwrt, fail_directory=fail_directory):
                    def sync(fd):
                        if stat.S_ISDIR(os.fstat(fd).st_mode) == fail_directory:
                            raise OSError('synthetic sync failure')
                        fsync(fd)
                    with patch.object(update, 'latest_manifest', return_value={'version': '2026.11.12'}), \
                            patch.object(update, 'current_version', return_value='2026.11.04'), \
                            patch.object(update, 'compare', return_value={'available': True, 'online': True}), \
                            patch.object(update, 'carplay_blocks_update', return_value=False), \
                            patch.object(update.os, 'fsync', side_effect=sync), \
                            patch.object(update, 'install_debian') as debian, \
                            patch.object(update, 'install_openwrt') as router:
                        self.assertEqual(update.install(self.root, openwrt), 1)
                    debian.assert_not_called()
                    router.assert_not_called()
                    status = update.read(self.root / 'state/update-status.json')
                    self.assertEqual(status['phase'], 'failed')
                    self.assertIn('synthetic sync failure', status['message'])


if __name__ == '__main__':
    unittest.main()
