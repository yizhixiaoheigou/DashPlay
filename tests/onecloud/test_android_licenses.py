import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/android-collect-licenses.py'
spec = importlib.util.spec_from_file_location('android_licenses', SCRIPT)
licenses = importlib.util.module_from_spec(spec)
spec.loader.exec_module(licenses)


class LicenseDeliveryTests(unittest.TestCase):
    def test_check_does_not_silently_restore_missing_jar_notice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jar = root / 'fixture.jar'
            with zipfile.ZipFile(jar, 'w') as target:
                target.writestr('META-INF/NOTICE.txt', 'component copyright and distribution notice\r\n' * 3)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3', 'file': str(jar)}]))
            for source in Path(licenses.OUT).iterdir():
                if source.is_file():
                    (root / source.name).write_bytes(source.read_bytes())
            manifest = root / 'third-party.txt'
            notice = root / 'com_google_zxing__core__NOTICE.txt'
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                licenses.main()
                licenses.main(check=True)
                notice.unlink()
                with self.assertRaisesRegex(SystemExit, 'JAR notice missing or differs'):
                    licenses.main(check=True)
                self.assertFalse(notice.exists())

    def test_regeneration_keeps_native_components_without_gradle_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3'}]))
            for source in Path(licenses.OUT).iterdir():
                if source.is_file():
                    (root / source.name).write_bytes(source.read_bytes())
            manifest = root / 'third-party.txt'
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                licenses.main()
            content = manifest.read_text()
            self.assertIn('libdatachannel:0.24.6', content)
            self.assertIn('mbedtls:3.6.7', content)
            for dependency in ('libjuice', 'libsrtp', 'usrsctp', 'plog', 'json'):
                self.assertIn(dependency + ':', content)

    def test_missing_full_text_fails_without_replacing_previous_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3'}]))
            manifest = root / 'third-party.txt'
            manifest.write_text('previous verified manifest')
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                with self.assertRaisesRegex(SystemExit, 'missing full licence text'):
                    licenses.main()
            self.assertEqual('previous verified manifest', manifest.read_text())

    def test_complete_distribution_includes_dependency_and_ported_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3'}]))
            for source in Path(licenses.OUT).iterdir():
                if source.is_file():
                    (root / source.name).write_bytes(source.read_bytes())
            manifest = root / 'third-party.txt'
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                licenses.main()
            self.assertIn('com.google.zxing:core:3.5.3', manifest.read_text())
            self.assertIn('com/shilapi/xcertplay/transport/', manifest.read_text())

    def test_missing_native_notice_fails_without_replacing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3'}]))
            for source in Path(licenses.OUT).iterdir():
                if source.is_file() and source.name != 'native-libjuice-LICENSE.txt':
                    (root / source.name).write_bytes(source.read_bytes())
            manifest = root / 'third-party.txt'
            manifest.write_text('previous verified manifest')
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                with self.assertRaisesRegex(SystemExit, 'missing full licence text: native-libjuice'):
                    licenses.main()
            self.assertEqual('previous verified manifest', manifest.read_text())

    def test_check_rejects_stale_manifest_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3'}]))
            for source in Path(licenses.OUT).iterdir():
                if source.is_file():
                    (root / source.name).write_bytes(source.read_bytes())
            manifest = root / 'third-party.txt'
            manifest.write_text('stale manifest')
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                with self.assertRaisesRegex(SystemExit, 'manifest differs'):
                    licenses.main(check=True)
                self.assertEqual('stale manifest', manifest.read_text())
                licenses.main()
                licenses.main(check=True)

    def test_changed_native_notice_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit = root / 'audit.json'
            audit.write_text(json.dumps([{'coordinate': 'com.google.zxing:core:3.5.3'}]))
            for source in Path(licenses.OUT).iterdir():
                if source.is_file():
                    (root / source.name).write_bytes(source.read_bytes())
            (root / 'native-libjuice-LICENSE.txt').write_text('incorrect replacement')
            manifest = root / 'third-party.txt'
            manifest.write_text('previous verified manifest')
            with patch.multiple(licenses, AUDIT=str(audit), OUT=directory, MANIFEST=str(manifest)):
                with self.assertRaisesRegex(SystemExit, 'native licence digest differs'):
                    licenses.main()
            self.assertEqual('previous verified manifest', manifest.read_text())


if __name__ == '__main__':
    unittest.main()
