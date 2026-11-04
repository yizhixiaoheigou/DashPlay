import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class ReleaseSignatureTests(unittest.TestCase):
    def run_fixture(self, signed, verify_code, notice_code=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            (root / 'android').mkdir()
            sdk = root / 'sdk'
            tool = sdk / 'build-tools/36.0.0/apksigner'
            tool.parent.mkdir(parents=True)
            tool.write_text('#!/bin/sh\nexit ' + str(verify_code) + '\n')
            tool.chmod(0o755)
            for name in ('android-release.sh', 'android_version.py'):
                shutil.copy(ROOT / 'scripts' / name, root / 'scripts' / name)
            (root / 'scripts/android-collect-licenses.py').write_text(
                'import sys\nsys.exit(' + str(notice_code) + ')\n')
            apk = 'app-release.apk' if signed else 'app-release-unsigned.apk'
            gradle = root / 'android/gradlew'
            gradle.write_text('#!/bin/sh\nmkdir -p app/build/outputs/apk/release\n'
                              'touch app/build/outputs/apk/release/' + apk + '\n')
            gradle.chmod(0o755)
            result = subprocess.run(['sh', str(root / 'scripts/android-release.sh'), '0.3.0-beta.1'],
                                    env=dict(os.environ, ANDROID_HOME=str(sdk), JAVA_HOME=str(root)),
                                    text=True, capture_output=True)
            delivered = (root / 'artifacts/android-release-0.3.0-beta.1/DashPlay-android-0.3.0-beta.1.apk').exists()
            return result, delivered

    def test_unsigned_output_is_not_delivered(self):
        result, delivered = self.run_fixture(False, 0)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('no signed release APK', result.stderr)
        self.assertFalse(delivered)

    def test_invalid_signature_is_not_delivered(self):
        result, delivered = self.run_fixture(True, 1)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(delivered)

    def test_missing_notices_are_not_delivered(self):
        result, delivered = self.run_fixture(True, 0, notice_code=1)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(delivered)

    def test_signed_output_with_notices_is_delivered(self):
        result, delivered = self.run_fixture(True, 0)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(delivered)


if __name__ == '__main__':
    unittest.main()
