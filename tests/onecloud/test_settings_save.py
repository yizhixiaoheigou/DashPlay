import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/onecloud"))
import settings_page
import setup_wizard


class ConcurrentSaveTests(unittest.TestCase):
    def test_two_saves_at_once_leave_one_whole_file(self):
        # The page serves each request in a thread and every save used the same .tmp: a double-click on save
        # could mix both writes or fail the second rename (review 2026-10-12).
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "car-hotspot.json"
            errors = []

            def save(n):
                try:
                    for i in range(150):
                        settings_page._save(path, {"ssid": f"car-{n}-{i}", "passphrase": "x" * (8 + n * 40)})
                except Exception as error:   # noqa: BLE001 - any failure is the finding
                    errors.append(repr(error))

            with unittest.mock.patch.object(settings_page.os, "sync"), unittest.mock.patch.object(settings_page.os, "fsync"):
                threads = [threading.Thread(target=save, args=(n,)) for n in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(errors, [])
            value = json.loads(path.read_text())
            self.assertIn(value["ssid"].split("-")[1], ("0", "1"))
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["car-hotspot.json"])

    def test_one_save_for_the_settings_and_the_setup_state(self):
        # settings_page._save and setup_wizard.save_json did the same job twice, the second without the lock or
        # its own temporary file (2026-10-29): setup.json is written from the same request threads.
        import setup_wizard
        self.assertIs(settings_page._save, setup_wizard.save_json)


class PageHttpGuardsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.page = settings_page.SettingsPage(self.state, lambda: {}, lambda _: None, lambda: [])

    def test_note_check_table_is_capped(self):
        # A LAN client sending crafted Host/paths must not grow the dedup table (and its eMMC log)
        # without bound: only check names should be here, and they repeat.
        host = setup_wizard.TESLA_CHECK_NAMES[0]
        for i in range(70):
            self.page.note_check(host, f'/generate_204?x={i}', 'off')
        self.assertLessEqual(len(self.page.checks_seen), 64)

    def test_versions_redirect_only_leaves_the_box_itself(self):
        # The version-manager token rides the fragment; a crafted Host must not receive it.
        self.assertIs(self.page.versions_redirect_allowed('evil.example.com'), False)
        self.assertIs(self.page.versions_redirect_allowed('192.0.2.9'), False, 'not loopback, allowed() is empty')
        self.assertIs(self.page.versions_redirect_allowed(''), False)
        self.assertIs(self.page.versions_redirect_allowed('dashplay.local'), True)

    def test_a_broken_render_answers_500_instead_of_dropping_the_connection(self):
        self.page.start(0)
        self.addCleanup(self.page.stop)
        port = self.page.server.server_port
        with unittest.mock.patch.object(self.page, 'render', side_effect=RuntimeError('synthetic')):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=5)
                self.fail('expected 500')
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 500)


if __name__ == "__main__":
    unittest.main()
