import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[2] / "scripts/onecloud/run-receiver.py"
spec = importlib.util.spec_from_file_location("run_receiver", HERE)
run_receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_receiver)


class BrowserAccessTest(unittest.TestCase):
    def test_heap_ceiling_follows_the_board_memory(self):
        self.assertEqual(run_receiver.heap_limit("MemTotal:         402316 kB\nMemFree: 1 kB\n"), "-Xmx224m")   # 410 stick
        self.assertEqual(run_receiver.heap_limit("MemTotal:         855012 kB\n"), "-Xmx384m")   # OneCloud
        self.assertEqual(run_receiver.heap_limit("MemTotal:        1897396 kB\n"), "-Xmx384m")   # N1
        self.assertEqual(run_receiver.heap_limit(""), "-Xmx384m")

    def test_token_goes_to_a_file_only_the_box_page_reads_and_the_old_script_goes(self):
        # A script any site could include gave the viewer token away (audit 2026-10-13, A2); the receiver serves
        # receiver-access.json to the box's own page only.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "playport/web/dist"
            dist.mkdir(parents=True)
            (dist / "receiver-access.js").write_text("localStorage.setItem('playport-token','old');\n")
            run_receiver.write_browser_access(root, "0123456789abcdef0123456789abcdef", 18080)
            self.assertFalse((dist / "receiver-access.js").exists())
            access = dist / "receiver-access.json"
            self.assertEqual(json.loads(access.read_text()),
                             {"token": "0123456789abcdef0123456789abcdef", "settingsPort": 18080})
            self.assertEqual(access.stat().st_mode & 0o777, 0o644)
            self.assertEqual(sorted(p.name for p in dist.iterdir()), ["receiver-access.json"])

    def page_flags(self, **flags):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "playport/web/dist").mkdir(parents=True)
            run_receiver.write_browser_access(root, "0123456789abcdef0123456789abcdef", 18080, **flags)
            access = json.loads((root / "playport/web/dist/receiver-access.json").read_text())
            return {key: value for key, value in access.items() if key not in ("token", "settingsPort")}

    def test_music_over_the_car_bluetooth_reaches_the_page_alone(self):
        self.assertEqual(self.page_flags(car_music=True), {"carMusic": True})

    def test_tesla_mode_brings_the_car_music_with_it(self):
        self.assertEqual(self.page_flags(tesla=True), {"carMusic": True, "tesla": True})

    def test_tesla_browser_can_leave_audio_in_the_webpage(self):
        self.assertEqual(self.page_flags(tesla_browser=True), {"tesla": True, "carMusic": False})

    def test_calls_over_the_car_bluetooth_reach_the_page(self):
        # The page greys out its Siri and call levels then: those sounds are the car's (2026-10-29).
        self.assertEqual(self.page_flags(car_voice=True), {"carVoice": True})
        self.assertEqual(self.page_flags(tesla=True, car_voice=True), {"carMusic": True, "tesla": True, "carVoice": True})
        self.assertEqual(self.page_flags(tesla_browser=True, car_voice=True), {"tesla": True, "carMusic": False, "carVoice": True})

    def test_rejects_an_unexpected_token(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_receiver.write_browser_access(Path(directory), "not-a-token")


class RedactionTest(unittest.TestCase):
    def test_a_password_the_trace_escaped_is_still_hidden(self):
        # The iAP2 trace prints strings escaped (\" and \\): a password with those characters was not found by a
        # plain replace and stayed in receiver.log (review 2026-10-12).
        password = 'ab"c\\d e'
        line = '  [2] passphrase: "ab\\"c\\\\d e"\n'
        self.assertEqual(run_receiver.redact(line, "f" * 32, password), '  [2] passphrase: "[REDACTED]"\n')

    def test_token_password_and_raw_preview(self):
        token, password = "0123456789abcdef0123456789abcdef", "secret-pass"
        self.assertEqual(run_receiver.redact(f"url ?token={token} pw={password}\n", token, password),
                         "url ?token=[REDACTED] pw=[REDACTED]\n")
        self.assertEqual(run_receiver.redact("  raw-body=[00 01 02]\n", token, password), "  raw-body=[not logged]\n")


if __name__ == "__main__":
    unittest.main()
