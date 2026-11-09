import json
import threading
import urllib.request
import urllib.error
import unittest
import io
import tarfile
from unittest.mock import patch, Mock
import test_update as fixtures
import update
import version_manager
from test_upgrade import upgrade, make


class SelectedReleaseTests(unittest.TestCase):
    setUp = fixtures.SignedReleaseTests.setUp
    sign = fixtures.SignedReleaseTests.sign

    def publish(self, version, body=None, signature=None):
        value = body or {'format': 1, 'version': version, 'online': True}
        data = json.dumps(value).encode()
        self.server.put('releases/v' + version + '/' + update.MANIFEST, data)
        self.server.put('releases/v' + version + '/' + update.SIGNATURE,
                        self.sign(data) if signature is None else signature)
        return value

    def test_selected_older_release_is_verified_and_not_replaced_with_latest(self):
        self.publish('2026.11.01.1')
        with patch.object(update, 'RELEASE_FEED', self.server.base + 'releases/v{version}/'):
            result = update.latest_manifest(self.root, False, self.pub, version='2026.11.01.1')
        self.assertEqual(result['version'], '2026.11.01.1')
        self.assertFalse(any('/latest/' in p for p in self.server.seen))

    def test_signed_wrong_version_and_bad_signature_are_rejected(self):
        for value, sig in [({'format': 1, 'version': '2026.11.02'}, None),
                           ({'format': 1, 'version': '2026.11.01.1'}, b'forged')]:
            self.publish('2026.11.01.1', value, sig)
            with patch.object(update, 'RELEASE_FEED', self.server.base + 'releases/v{version}/'):
                with self.assertRaises(update.UpdateError):
                    update.latest_manifest(self.root, False, self.pub, version='2026.11.01.1')

    def test_invalid_version_cannot_be_used_as_a_url_or_command(self):
        for value in ('../../latest', 'https://example.com', '', '2026.11.01;reboot'):
            with self.assertRaises(update.UpdateError):
                update.latest_manifest(self.root, False, self.pub, version=value)
        self.assertEqual(self.server.seen, [])

    def manifest(self):
        files = {}
        for name in update.SYSTEM_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('unchanged')
            files[name] = update.file_sha256(path)
        return {'format': 1, 'version': '2026.10.29', 'online': True, 'debian': {'files': files}}

    def test_compatible_old_version_installs_and_keeps_previous_version_metadata(self):
        manifest = self.manifest()
        (self.root / 'version.txt').write_text('2026.11.02')
        with patch.object(update, 'latest_manifest', return_value=manifest), \
                patch.object(update, 'install_debian') as install:
            self.assertEqual(update.install(self.root, False, self.pub, version='2026.10.29'), 0)
        install.assert_called_once()
        self.assertEqual(update.read(self.root / 'state/version-manager/previous.json')['version'], '2026.11.02')
        self.assertEqual(update.read(self.root / 'state/version-manager/previous.json')['installed'], '2026.10.29')

    def test_empty_selected_version_never_installs_latest(self):
        with patch.object(update, 'install_debian') as install:
            self.assertEqual(update.install(self.root, False, self.pub, version=''), 1)
        install.assert_not_called()
        self.assertEqual(self.server.seen, [])

    def test_wired_carplay_blocks_version_changes_without_a_wireless_active_marker(self):
        status = self.root / 'wired-status.json'
        manifest = self.manifest()
        # The real N1 presence file has paired phones, but no top-level active field on a USB session.
        update.save(self.root / 'state/receiver/phone-presence.json', {'phones': {}, 'paired': []})
        with patch.object(update, 'WIRED_STATUS', status, create=True):
            for phase in ('starting', 'live'):
                update.save(status, {'phase': phase})
                self.assertTrue(update.carplay_in_use(self.root), phase)
                self.assertIn('正在使用 CarPlay', update.launch(self.root, False))
                with patch.object(update, 'latest_manifest', return_value=manifest), \
                        patch.object(update, 'install_debian') as install:
                    self.assertEqual(update.install(self.root, False, self.pub, version='2026.10.29'), 1)
                install.assert_not_called()
            update.save(status, {'phase': 'idle'})
            self.assertFalse(update.carplay_in_use(self.root))

    def test_downgrade_preserves_settings_and_the_live_version_entrance(self):
        # Exercise the real upgrader's filesystem replacement while the independent HTTP service is live.
        retained = {'state/options.json': '{"usb_hotspot_width":"40"}',
                    'state/receiver/pairings.properties': 'synthetic-pair',
                    'state/tls/key.pem': 'synthetic-key', 'service.json': '{"channel":149}',
                    'identity/test.txt': 'synthetic-identity',
                    'state/version-manager/version_manager.py': 'retained-manager',
                    'state/version-manager/access.json': '{"token":"synthetic-token"}'}
        make(self.root, {**retained, 'version.txt': '2026.11.02', 'version_manager.py': 'new feature'})
        old = self.root.parent / 'old-release'
        make(old, {'version.txt': '2026.10.29', 'supervisor.py': 'old program',
                   'state/options.json': '{}', 'service.json': '{}'})
        server = version_manager.make_server(self.root, 'synthetic-token', ('127.0.0.1', 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        upgrade.apply(self.root, old, *upgrade.plan(self.root, old))
        self.assertFalse((self.root / 'version_manager.py').exists())
        for name, data in retained.items():
            self.assertEqual((self.root / name).read_text(), data)
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/status',
                                         headers={'Authorization': 'Bearer synthetic-token'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)['current'], '2026.10.29')

    def test_future_schema_or_system_mismatch_stops_before_install(self):
        manifest = self.manifest()
        for change in ({'compatibility': {'settings': 2}}, {'debian': {'files': {}}}, {'online': False}):
            with patch.object(update, 'latest_manifest', return_value={**manifest, **change}), \
                    patch.object(update, 'install_debian') as install, patch.object(update, 'backup') as backup:
                self.assertEqual(update.install(self.root, False, self.pub, version='2026.10.29'), 1)
            install.assert_not_called()
            backup.assert_not_called()

    def test_version_server_requires_token_for_reads_and_writes(self):
        server = version_manager.make_server(self.root, 'synthetic-token', ('127.0.0.1', 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_port)
        for path in ('/api/status', '/api/history'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            self.assertEqual(error.exception.code, 403)
        request = urllib.request.Request(base + '/api/install', data=b'{"version":"2026.10.29"}',
                                         headers={'Content-Type': 'application/json'})
        with patch.object(update, 'install') as install, self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(request)
        install.assert_not_called()
        request = urllib.request.Request(base + '/api/status', headers={'Authorization': 'Bearer synthetic-token'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(json.load(response)['manager'], 1)

    def test_service_runs_retained_copy_not_the_replaced_program(self):
        for openwrt in (False, True):
            value = version_manager.service_text(self.root, self.root / 'state/version-manager', 8091, openwrt)
            self.assertIn('/state/version-manager/version_manager.py', value)
            self.assertNotIn('ExecStart=/opt/dashplay/version_manager.py', value)

    def test_first_upgrade_from_legacy_updater_recovers_previous_version(self):
        (self.root / 'version.txt').write_text('2026.11.02')
        for name, member, data in [('dashplay-previous.tar', 'version.txt', b'2026.11.01.1\n'),
                                   (update.BACKUP, 'state/update-check.json', b'{"current":"2026.10.29"}')]:
            archive = self.root.parent / name
            with tarfile.open(archive, 'w') as tar:
                entry = tarfile.TarInfo(member); entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
            version_manager.recover_previous(self.root)
            expected = '2026.10.29' if name == update.BACKUP else '2026.11.01.1'
            target = self.root / 'state/version-manager/previous.json'
            self.assertEqual(update.read(target)['version'], expected)
            # Existing metadata belongs to the latest switch and must not be overwritten by an older backup.
            update.save(target, {'version': '2026.10.31.3', 'installed': '2026.11.02'})
            version_manager.recover_previous(self.root)
            self.assertEqual(update.read(target)['version'], '2026.10.31.3')
            target.unlink()
            archive.unlink()

    def test_legacy_reupgrade_replaces_previous_metadata_from_the_earlier_downgrade(self):
        (self.root / 'version.txt').write_text('2026.11.02')
        target = self.root / 'state/version-manager/previous.json'
        update.save(target, {'version': '2026.11.02', 'installed': '2026.10.29'})
        with tarfile.open(self.root.parent / 'dashplay-previous.tar', 'w') as tar:
            data = b'2026.10.29\n'
            entry = tarfile.TarInfo('version.txt'); entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
        version_manager.recover_previous(self.root)
        self.assertEqual(update.read(target), {'version': '2026.10.29', 'installed': '2026.11.02'})


class PageServingTests(unittest.TestCase):
    def test_page_answers_with_an_etag_and_304s_on_a_match(self):
        import tempfile
        from pathlib import Path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = version_manager.make_server(Path(tmp.name), 'synthetic-token', ('127.0.0.1', 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        base = f'http://127.0.0.1:{server.server_port}/'
        with urllib.request.urlopen(base, timeout=5) as first:
            etag = first.headers.get('ETag')
        self.assertTrue(etag)
        request = urllib.request.Request(base, headers={'If-None-Match': etag})
        try:
            urllib.request.urlopen(request, timeout=5)
            self.fail('expected 304')
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 304)
