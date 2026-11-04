import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import android_version


class VersionCodeTests(unittest.TestCase):
    # In the order Android must see them: every later build needs a larger versionCode, or the system installer
    # (and the app's own ApkUpdatePolicy) refuses it as not newer.
    ORDER = ['0.2.0-dev.1', '0.3.0-dev.1', '0.3.0-alpha.1', '0.3.0-beta.1', '0.3.0-beta.2', '0.3.0-beta.9',
             '0.3.0-beta.10', '0.3.0-rc.1', '0.3.0', '0.3.1-beta.1', '0.3.1', '0.4.0', '0.10.0', '1.0.0', '12.34.56']

    def test_codes_grow_with_every_later_version(self):
        codes = [android_version.code(v) for v in self.ORDER]
        for (a, ca), (b, cb) in zip(zip(self.ORDER, codes), zip(self.ORDER[1:], codes[1:])):
            self.assertLess(ca, cb, f'{a} -> {ca} is not below {b} -> {cb}')

    def test_the_installed_beta_can_still_be_upgraded(self):
        # The Xiaomi test phone carries 0.3.0-beta.1 at the old formula's 3001.
        self.assertGreater(android_version.code('0.3.0-beta.2'), 3001)
        self.assertGreater(android_version.code('0.3.0-beta.1'), 3001)
        # November setup candidates used 3004xx. The release candidate must also upgrade those installs.
        self.assertGreater(android_version.code('0.3.1-beta.1'), 300441)

    def test_fits_android_and_rejects_what_it_cannot_order(self):
        self.assertLessEqual(android_version.code('99.99.99'), 2_100_000_000)
        for bad in ('0.3', '0.3.0-preview.1', '0.3.0-beta', '0.3.0-beta.100', '0.100.0', 'v0.3.0', '2026.10.31.3'):
            with self.assertRaises(ValueError, msg=bad):
                android_version.code(bad)


if __name__ == '__main__':
    unittest.main()
