import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/onecloud"))
import settings_page


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


if __name__ == "__main__":
    unittest.main()
