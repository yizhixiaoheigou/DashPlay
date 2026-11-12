import json
import threading
import urllib.request
import urllib.error
import unittest
import io
import tarfile
import fcntl
import subprocess
from unittest.mock import patch, Mock
import test_update as fixtures
import update
import version_manager
from test_upgrade import upgrade, make


class SelectedReleaseTests(unittest.TestCase):
    setUp = fixtures.SignedReleaseTests.setUp
    sign = fixtures.SignedReleaseTests.sign

    def test_openwrt_new_domain_blocks_old_release_before_backup_or_install(self):
        manifest = {'version': '2026.11.04', 'online': True, 'openwrt': {'jre': {'version': '21-r3'}}}
        for provider in ('dnspod', 'dynv6'):
            config = f"dashplay.main.ddns_provider='{provider}'\ndashplay.main.ddns_domain='car.example.org'\n"
            with self.subTest(provider=provider), patch.object(update, 'latest_manifest', return_value=manifest), \
                    patch.object(update, 'current_version', return_value='2026.11.12'), \
                    patch.object(update, 'apk_version', return_value='21-r3'), \
                    patch.object(update, 'run', return_value=Mock(returncode=0, stdout=config)), \
                    patch.object(update, 'backup') as backup, patch.object(update, 'install_openwrt') as install:
                self.assertEqual(update.install(self.root, True, self.pub, version='2026.11.04'), 1)
                self.assertIn('自有域名', update.read(self.root / 'state/update-status.json')['message'])
                backup.assert_not_called(); install.assert_not_called()

    def test_openwrt_provider_support_is_explicit_and_disabled_domain_can_downgrade(self):
        old = {'version': '2026.11.04', 'online': True, 'openwrt': {'jre': {'version': '21-r3'}}}
        supported = {**old, 'openwrt': {**old['openwrt'], 'dns_providers': ['duckdns', 'dnspod', 'dynv6']}}
        for provider, domain, target, allowed in (
                ('duckdns', 'unused.example.org', old, True), ('dnspod', '', old, True),
                ('dnspod', 'car.example.org', supported, True), ('dynv6', 'car.example.org', supported, True),
                ('dynv6', 'car.example.org', old, False)):
            with self.subTest(provider=provider, domain=domain, allowed=allowed), \
                    patch.object(update, 'apk_version', return_value='21-r3'), \
                    patch.object(update, 'run', return_value=Mock(returncode=0, stdout=
                        f"dashplay.main.ddns_provider='{provider}'\ndashplay.main.ddns_domain='{domain}'\n")):
                if allowed:
                    update.check_target(self.root, True, target)
                else:
                    with self.assertRaises(update.UpdateError): update.check_target(self.root, True, target)

    def test_openwrt_unreadable_domain_configuration_never_silently_allows_downgrade(self):
        manifest = {'version': '2026.11.04', 'online': True, 'openwrt': {'jre': {'version': '21-r3'}}}
        for outcome in (OSError('synthetic-secret'), subprocess.TimeoutExpired('uci', 5),
                        Mock(returncode=1, stdout='synthetic-secret'),
                        Mock(returncode=0, stdout="dashplay.main.ddns_provider='synthetic-secret")):
            with self.subTest(outcome=type(outcome).__name__), patch.object(update, 'apk_version', return_value='21-r3'), \
                    patch.object(update, 'run', side_effect=outcome if isinstance(outcome, Exception) else None,
                                 return_value=outcome):
                with self.assertRaises(update.UpdateError) as error:
                    update.check_target(self.root, True, manifest)
                self.assertNotIn('synthetic-secret', str(error.exception))

    def test_openwrt_version_preview_reports_unsupported_domain(self):
        server = version_manager.make_server(self.root, 'synthetic-token', ('127.0.0.1', 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/preview',
            data=b'{"version":"2026.11.04"}', headers={'Content-Type': 'application/json',
            'Authorization': 'Bearer synthetic-token'})
        manifest = {'version': '2026.11.04', 'online': True, 'openwrt': {'jre': {'version': '21-r3'}}}
        with patch.object(update, 'is_openwrt', return_value=True), patch.object(update, 'apk_version', return_value='21-r3'), \
                patch.object(update, 'latest_manifest', return_value=manifest), patch.object(update, 'run',
                return_value=Mock(returncode=0, stdout="dashplay.main.ddns_provider='dynv6'\ndashplay.main.ddns_domain='car.example.org'\n")):
            with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
            self.assertIn('dynv6', json.load(error.exception)['error'])

    def test_openwrt_refreshes_old_manager_and_keeps_bookmark(self):
        directory = self.root / 'state/version-manager'
        info = {'port': 8091, 'token': 'synthetic-token'}
        update.save(directory / 'access.json', info)
        (directory / 'update.py').write_text('# old updater')
        states = [{'manager': 1, 'busy': False}, {'manager': 1, 'busy': False},
                  {'manager': 1, 'openwrt_dns_compatibility': 1}]
        with patch.object(version_manager, 'manager_state', side_effect=states), \
                patch.object(update, 'is_openwrt', return_value=True), patch.object(version_manager, 'recover_previous'), \
                patch.object(version_manager.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(version_manager.ensure(self.root), info)
        self.assertEqual(update.read(directory / 'access.json'), info)
        self.assertIn('OPENWRT_DNS_COMPATIBILITY = 1', (directory / 'update.py').read_text())
        run.assert_called_once_with(['/etc/init.d/dashplay-versions', 'restart'],
                                    capture_output=True, text=True, timeout=15)

    def test_openwrt_old_manager_is_not_replaced_while_busy(self):
        directory = self.root / 'state/version-manager'
        update.save(directory / 'access.json', {'port': 8091, 'token': 'synthetic-token'})
        (directory / 'update.py').write_text('# old updater')
        with patch.object(version_manager, 'manager_state', return_value={'manager': 1, 'busy': True}), \
                patch.object(update, 'is_openwrt', return_value=True), patch.object(version_manager, 'recover_previous'), \
                patch.object(version_manager, 'copy_retained') as copy, patch.object(version_manager.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '正在安装'): version_manager.ensure(self.root)
            copy.assert_not_called(); run.assert_not_called()
        self.assertEqual((directory / 'update.py').read_text(), '# old updater')

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

    def test_wired_carplay_allows_debian_updates_but_still_blocks_openwrt(self):
        status = self.root / 'wired-status.json'
        manifest = self.manifest()
        # The real N1 presence file has paired phones, but no top-level active field on a USB session.
        update.save(self.root / 'state/receiver/phone-presence.json', {'phones': {}, 'paired': []})
        with patch.object(update, 'WIRED_STATUS', status, create=True):
            for phase in ('starting', 'live'):
                update.save(status, {'phase': phase})
                self.assertTrue(update.carplay_in_use(self.root), phase)
                self.assertIn('正在使用 CarPlay', update.launch(self.root, True))
                with patch.object(update, 'run', return_value=Mock(returncode=0)):
                    self.assertIn('已开始升级', update.launch(self.root, False))
                with patch.object(update, 'latest_manifest', return_value=manifest), \
                        patch.object(update, 'install_debian') as install:
                    self.assertEqual(update.install(self.root, False, self.pub, version='2026.10.29'), 0)
                install.assert_called_once()
            update.save(status, {'phase': 'idle'})
            self.assertFalse(update.carplay_in_use(self.root))

    def test_wireless_carplay_still_blocks_both_platforms(self):
        update.save(self.root / 'state/receiver/phone-presence.json', {'active': True})
        with patch.object(update, 'run') as run:
            for openwrt in (False, True):
                self.assertIn('正在使用 CarPlay', update.launch(self.root, openwrt))
        run.assert_not_called()

    def test_version_server_allows_only_debian_wired_install(self):
        server = version_manager.make_server(self.root, 'synthetic-token', ('127.0.0.1', 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        status = self.root / 'wired-status.json'
        update.save(status, {'phase': 'live'})
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/install',
            data=b'{"version":"2026.10.29"}', headers={'Content-Type': 'application/json',
            'Authorization': 'Bearer synthetic-token'})
        finished = threading.Event()
        with patch.object(update, 'WIRED_STATUS', status), \
                patch.object(update, 'install', side_effect=lambda *a, **kw: finished.set()) as install:
            with patch.object(update, 'is_openwrt', return_value=True):
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                    self.assertNotIn('有线 CarPlay 可保持连接', response.read().decode())
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 409)
                install.assert_not_called()
            with patch.object(update, 'is_openwrt', return_value=False):
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/') as response:
                    self.assertIn('有线 CarPlay 可保持连接', response.read().decode())
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(finished.wait(2))
                install.assert_called_once_with(self.root, version='2026.10.29')

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

    def test_status_reports_the_retained_tools_actual_backup_capability(self):
        directory = self.root / 'state/version-manager'
        directory.mkdir(parents=True, exist_ok=True)
        server = version_manager.make_server(self.root, 'synthetic-token', ('127.0.0.1', 0))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/status',
                                         headers={'Authorization':'Bearer synthetic-token'})
        with patch.object(version_manager, '__file__', str(directory / 'version_manager.py')):
            for contents, expected, gate in (('# legacy tool', False, False),
                    ('PROGRAM_BACKUP_FSYNC = 1\n', True, False),
                    ('PROGRAM_BACKUP_FSYNC = 1\nPLATFORM_UPGRADE_CHECK = 1\n', True, True)):
                (directory / 'upgrade.py').write_text(contents)
                with urllib.request.urlopen(request) as response:
                    state = json.load(response)
                    self.assertEqual(state['openwrt_dns_compatibility'], 1)
                    self.assertEqual(state['durable_program_backup'], expected)
                    self.assertEqual(state['platform_upgrade_check'], gate)

    def test_open_refreshes_idle_legacy_manager_without_changing_its_bookmark(self):
        directory = self.root / 'state/version-manager'
        info = {'port': 8091, 'token': 'synthetic-token'}
        update.save(directory / 'access.json', info)
        (directory / 'update.py').write_text('old updater')
        states = [{'manager': 1, 'busy': False}, {'manager': 1, 'busy': False},
                  {'manager': 1, 'wired_debian_update': True, 'durable_program_backup': True, 'platform_upgrade_check': True}]
        with patch.object(version_manager, 'manager_state', side_effect=states), \
                patch.object(update, 'is_openwrt', return_value=False), \
                patch.object(version_manager, 'recover_previous'), \
                patch.object(version_manager.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(version_manager.ensure(self.root), info)
        self.assertEqual(update.read(directory / 'access.json'), info)
        self.assertIn('def carplay_blocks_update', (directory / 'update.py').read_text())
        self.assertIn('wired_debian_update', (directory / 'version_manager.py').read_text())
        run.assert_called_once_with(['systemctl', 'restart', 'dashplay-versions.service'],
                                    capture_output=True, text=True, timeout=15)

    def test_backup_capable_manager_still_refreshes_its_old_platform_check_tool(self):
        directory = self.root / 'state/version-manager'
        info = {'port': 8091, 'token': 'synthetic-token'}
        update.save(directory / 'access.json', info)
        (directory / 'upgrade.py').write_text('# legacy tool without durable backup')
        old = {'manager':1, 'wired_debian_update':True, 'durable_program_backup':True, 'busy':False}
        with patch.object(version_manager, 'manager_state', side_effect=[old, old, dict(old, durable_program_backup=True, platform_upgrade_check=True)]), \
                patch.object(update, 'is_openwrt', return_value=False), patch.object(version_manager, 'recover_previous'), \
                patch.object(version_manager.subprocess, 'run', return_value=Mock(returncode=0)) as run:
            self.assertEqual(version_manager.ensure(self.root), info)
        self.assertIn('PROGRAM_BACKUP_FSYNC = 1', (directory / 'upgrade.py').read_text())
        run.assert_called_once()
        self.assertEqual(update.read(directory / 'access.json'), info)

    def test_open_never_refreshes_a_manager_during_an_install(self):
        directory = self.root / 'state/version-manager'
        update.save(directory / 'access.json', {'port': 8091, 'token': 'synthetic-token'})
        (directory / 'update.py').write_text('old updater')
        with patch.object(version_manager, 'manager_state', return_value={'manager': 1, 'busy': True}), \
                patch.object(update, 'is_openwrt', return_value=False), \
                patch.object(version_manager, 'recover_previous'), \
                patch.object(version_manager.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, '正在安装'):
                version_manager.ensure(self.root)
            run.assert_not_called()
        with open(self.root / 'state/update.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(version_manager, 'manager_state', return_value={'manager': 1, 'busy': False}), \
                    patch.object(update, 'is_openwrt', return_value=False), \
                    patch.object(version_manager, 'recover_previous'), \
                    patch.object(version_manager.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, '正在安装'):
                    version_manager.ensure(self.root)
                run.assert_not_called()
        self.assertEqual((directory / 'update.py').read_text(), 'old updater')

    def test_open_keeps_current_manager_and_openwrt_manager_running(self):
        directory = self.root / 'state/version-manager'
        info = {'port': 8091, 'token': 'synthetic-token'}
        update.save(directory / 'access.json', info)
        for openwrt, state in ((False, {'manager': 1, 'wired_debian_update': True, 'durable_program_backup': True, 'platform_upgrade_check': True}),
                               (True, {'manager': 1, 'openwrt_dns_compatibility': 1})):
            with patch.object(version_manager, 'manager_state', return_value=state), \
                    patch.object(update, 'is_openwrt', return_value=openwrt), \
                    patch.object(version_manager, 'recover_previous'), \
                    patch.object(version_manager, 'copy_retained') as copy, \
                    patch.object(version_manager.subprocess, 'run') as run:
                self.assertEqual(version_manager.ensure(self.root), info)
                copy.assert_not_called()
                run.assert_not_called()

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
