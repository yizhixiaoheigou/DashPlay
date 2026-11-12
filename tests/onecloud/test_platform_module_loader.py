"""External firmware selection and offline public-upgrade checks; no hardware is touched."""
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import platform_api
from platform_debian import DebianPlatform
from test_upgrade import upgrade, make

PROGRAM = Path(platform_api.__file__).parent
NAME = 'test_firmware_adapter'
ADAPTER = '''from platform_debian import DebianPlatform
class Platform(DebianPlatform):
    def __init__(self, interface='', **paths):
        super().__init__(interface, **paths)
        self.received_paths = paths
'''


class ExternalPlatformTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.firmware = self.directory / 'firmware'
        self.firmware.mkdir()
        self.module = self.firmware / (NAME + '.py')
        self.module.write_text(ADAPTER)
        paths = patch.object(sys, 'path', [str(self.firmware), *sys.path])
        paths.start(); self.addCleanup(paths.stop)
        environment = patch.dict(os.environ, {'DASHPLAY_PLATFORM_MODULE': NAME})
        environment.start(); self.addCleanup(environment.stop)
        sys.modules.pop(NAME, None)
        self.addCleanup(sys.modules.pop, NAME, None)
        self.box = self.directory / 'box'; self.box.mkdir()
        self.candidate = self.directory / 'candidate'; self.candidate.mkdir()
        for path in PROGRAM.glob('*.py'):
            shutil.copyfile(path, self.candidate / path.name)
        self.selection = {'DASHPLAY_PLATFORM_MODULE': NAME,
                          'PYTHONPATH': str(self.firmware) + os.pathsep + str(self.box)}

    def check(self):
        with patch.object(upgrade, 'platform_environment', return_value=self.selection):
            upgrade.platform_check(self.candidate, self.box)

    def test_default_and_selected_adapter_receive_arguments(self):
        with patch.dict(os.environ):
            del os.environ['DASHPLAY_PLATFORM_MODULE']
            self.assertIs(type(platform_api.get_platform('wlan-test', run=self.directory)), DebianPlatform)
        actual = platform_api.get_platform('wlan-test', run=self.directory)
        self.assertEqual(type(actual).__module__, NAME)
        self.assertEqual(actual.interface, 'wlan-test')
        self.assertEqual(actual.received_paths, {'run': self.directory})

    def test_invalid_selection_cannot_fall_back_to_default(self):
        for name in ('', 'adapter.module', '../adapter', 'adapter-name', '0adapter'):
            with self.subTest(name=name), patch.dict(os.environ, DASHPLAY_PLATFORM_MODULE=name), \
                    patch.object(platform_api.importlib, 'import_module') as load:
                with self.assertRaises(ValueError): platform_api.get_platform()
                load.assert_not_called()
        with patch.dict(os.environ, DASHPLAY_PLATFORM_MODULE='nonexistent_test_adapter'):
            with self.assertRaises(ModuleNotFoundError): platform_api.get_platform()

    def test_current_and_explicit_api_pass_without_constructing_hardware_adapter(self):
        for version in ('', 'PLATFORM_API_VERSION = 1\n'):
            self.module.write_text(version + ADAPTER.replace('super().__init__(interface, **paths)',
                                                           'raise RuntimeError("constructor must not run")'))
            self.check()

    def test_bad_or_future_api_and_missing_methods_are_rejected(self):
        for code in ('PLATFORM_API_VERSION = 2\n' + ADAPTER,
                     'PLATFORM_API_VERSION = True\n' + ADAPTER,
                     'class Platform: pass\n', 'from platform_debian import DebianPlatform as Platform\n'):
            self.module.write_text(code)
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, '不兼容'):
                self.check()

    def test_no_external_adapter_keeps_old_generic_bundles_usable(self):
        with patch.object(upgrade, 'platform_environment', return_value={}):
            upgrade.platform_check(self.directory / 'absent', self.box)

    def test_missing_or_future_public_entry_is_rejected(self):
        for code in ('# old program', 'PLATFORM_API_VERSION = 2\n', 'PLATFORM_API_VERSION = True\n'):
            (self.candidate / 'platform_api.py').write_text(code)
            with self.subTest(code=code), self.assertRaises(ValueError): self.check()

    def test_installed_dependencies_cannot_hide_missing_candidate_dependency(self):
        (self.box / 'old_only_dependency.py').write_text('value = 1\n')
        self.module.write_text('import old_only_dependency\n' + ADAPTER)
        with self.assertRaisesRegex(ValueError, '不兼容'): self.check()

    def test_public_bundle_cannot_supply_or_shadow_the_external_adapter(self):
        (self.candidate / (NAME + '.py')).write_text(ADAPTER)
        with self.assertRaisesRegex(ValueError, '不兼容'): self.check()

    def test_private_import_error_does_not_leak_details(self):
        self.module.write_text('print("private configuration")\nraise RuntimeError("private details")\n')
        with self.assertRaises(ValueError) as failure: self.check()
        self.assertNotIn('private', str(failure.exception))

    def test_incompatible_install_stops_before_backup_or_service_stop(self):
        for name in upgrade.NEEDED: make(self.candidate, {name: 'candidate'})
        make(self.box, {'supervisor.py': 'old', 'state/placeholder': ''})
        (self.candidate / 'platform_api.py').write_text('# no external loader')
        archive = self.directory / 'candidate.tar'
        with tarfile.open(archive, 'w') as out: out.add(self.candidate, arcname='.')
        with patch.object(upgrade, 'platform_environment', return_value=self.selection), \
                patch.object(upgrade, 'ROOT', self.box), patch.object(upgrade.os, 'geteuid', return_value=0), \
                patch.object(upgrade, 'backup') as backup, patch.object(upgrade, 'run') as run:
            self.assertEqual(upgrade.main([str(archive)]), 1)
        backup.assert_not_called(); run.assert_not_called()
        self.assertEqual((self.box / 'supervisor.py').read_text(), 'old')

    def test_install_and_automatic_rollback_preserve_external_files_and_dropin(self):
        for healthy in (True, False):
            with self.subTest(healthy=healthy):
                for name in upgrade.NEEDED: make(self.candidate, {name: 'candidate'})
                make(self.box, {'supervisor.py': 'old', 'state/placeholder': ''})
                dropin = self.directory / 'service.d/local.conf'
                make(self.directory, {'service.d/local.conf': 'private selection',
                                      'firmware/settings.json': '{"example":true}'})
                before = {p: p.read_bytes() for p in (self.module, dropin, self.firmware / 'settings.json')}
                archive = self.directory / 'candidate.tar'
                with tarfile.open(archive, 'w') as out: out.add(self.candidate, arcname='.')
                with patch.object(upgrade, 'platform_environment', return_value=self.selection), \
                        patch.object(upgrade, 'ROOT', self.box), patch.object(upgrade, 'PREVIOUS', self.directory / 'previous.tar'), \
                        patch.object(upgrade.os, 'geteuid', return_value=0), patch.object(upgrade.os, 'chown'), \
                        patch.object(upgrade, 'UNIT', self.directory / 'dashplay.service'), patch.object(upgrade, 'domain_check', return_value={}), \
                        patch.object(upgrade, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                        patch.object(upgrade, 'service_up', return_value=healthy):
                    self.assertEqual(upgrade.main([str(archive)]), 0 if healthy else 1)
                self.assertEqual((self.box / 'supervisor.py').read_text(), 'candidate' if healthy else 'old')
                self.assertEqual({p: p.read_bytes() for p in before}, before)
                with tarfile.open(self.directory / 'previous.tar') as saved:
                    self.assertNotIn(NAME + '.py', saved.getnames())

    def test_existing_directory_symlink_cannot_redirect_public_write_into_firmware(self):
        (self.box / 'extension').symlink_to(self.firmware, target_is_directory=True)
        make(self.candidate, {'extension/' + NAME + '.py': 'overwrite'})
        before = self.module.read_bytes()
        with self.assertRaisesRegex(ValueError, '符号链接'): upgrade.plan(self.box, self.candidate)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            upgrade.apply(self.box, self.candidate, [], ['extension/' + NAME + '.py'])
        self.assertEqual(self.module.read_bytes(), before)


class ServicePlatformSelectionTests(unittest.TestCase):
    def show(self, text, returncode=0):
        return patch.object(upgrade.subprocess, 'run', return_value=subprocess.CompletedProcess([], returncode, text, ''))

    def test_stopped_service_reads_unit_selection_without_inheriting_versions_environment(self):
        with self.show('MainPID=0\nEnvironment=OTHER=ignored DASHPLAY_PLATFORM_MODULE=test_adapter "PYTHONPATH=/path with spaces"\n'), \
                patch.dict(os.environ, DASHPLAY_PLATFORM_MODULE='wrong_adapter'):
            self.assertEqual(upgrade.platform_environment(), {'DASHPLAY_PLATFORM_MODULE': 'test_adapter',
                                                              'PYTHONPATH': '/path with spaces'})

    def test_running_service_covers_environment_files_without_disclosing_other_values(self):
        with self.show('MainPID=123\nEnvironmentFiles=/test.env (ignore_errors=no)\n'), \
                patch.object(Path, 'read_bytes', return_value=b'DASHPLAY_PLATFORM_MODULE=test_adapter\0PYTHONPATH=/external\0SECRET=hidden\0'):
            self.assertEqual(upgrade.platform_environment(), {'DASHPLAY_PLATFORM_MODULE':'test_adapter', 'PYTHONPATH':'/external'})

    def test_unknown_or_changed_service_settings_fail_closed(self):
        for output, code in (('', 1), ('', 0), ('MainPID=-1\n', 0), ('EnvironmentFiles=/test.env\nMainPID=0\n', 0),
                             ('Environment=DASHPLAY_PLATFORM_MODULE=new_adapter\nMainPID=123\n', 0)):
            with self.subTest(output=output), self.show(output, code), \
                    patch.object(Path, 'read_bytes', return_value=b'DASHPLAY_PLATFORM_MODULE=old_adapter\0'):
                with self.assertRaises(ValueError): upgrade.platform_environment()


if __name__ == '__main__':
    unittest.main()
