from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

MAIN = Path(__file__).resolve().parents[2] / 'android/app/src/main'
ANDROID = '{http://schemas.android.com/apk/res/android}'


class BackupBoundaryTests(unittest.TestCase):
    def test_system_backups_remain_disabled(self):
        app = ET.parse(MAIN / 'AndroidManifest.xml').getroot().find('application')
        self.assertEqual('false', app.get(ANDROID + 'allowBackup'))
        self.assertEqual('false', app.get(ANDROID + 'fullBackupContent'))

    def test_cloud_and_device_transfer_exclude_credential_storage(self):
        app = ET.parse(MAIN / 'AndroidManifest.xml').getroot().find('application')
        resource = app.get(ANDROID + 'dataExtractionRules')
        self.assertIsNotNone(resource, 'Android 12+ device transfer must have explicit exclusions')
        self.assertTrue(resource.startswith('@xml/'))
        rules = ET.parse(MAIN / 'res/xml' / (resource.removeprefix('@xml/') + '.xml')).getroot()
        domains = {'root', 'file', 'database', 'sharedpref', 'external',
                   'device_root', 'device_file', 'device_database', 'device_sharedpref'}
        for transport in ('cloud-backup', 'device-transfer'):
            mode = rules.find(transport)
            self.assertIsNotNone(mode, transport)
            excluded = {e.get('domain') for e in mode.findall('exclude') if e.get('path') in ('.', './')}
            self.assertTrue(domains <= excluded, (transport, domains - excluded))
            self.assertEqual([], mode.findall('include'))


if __name__ == '__main__':
    unittest.main()
