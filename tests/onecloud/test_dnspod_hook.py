"""Run the DNSPod hook against a fake API. No network and no real token."""
from pathlib import Path
import subprocess
import tempfile
import unittest


HOOK = Path(__file__).resolve().parents[2] / "scripts/onecloud/acme/dnsapi/dns_dp.sh"
TOKEN = "syntheticdnspodtoken1"
STUBS = f'''
_info() {{ :; }}
_debug() {{ :; }}
_err() {{ echo "err: $*" >&2; }}
_post() {{
  printf '%s\\n' "$2" >> calls
  printf '%s\\n' "$1" >> bodies
  case "$2" in
    *Domain.Info)
      case "$1" in
        *"&domain=apeng.fun") printf '%s' '{{"status":{{"code":"1"}},"domain":{{"id":"42"}}}}' ;;
        *) printf '%s' '{{"status":{{"code":"6"}}}}' ;;
      esac ;;
    *Record.Create|*Record.Remove) printf '%s' '{{"status":{{"code":"1"}}}}' ;;
    *Record.List) printf '%s' '{{"status":{{"code":"1"}},"records":[{{"id":"7","value":"challenge-value"}}]}}' ;;
    *) printf '%s' '{{"status":{{"code":"6"}}}}' ;;
  esac
}}
DP_Id=10001
DP_Key={TOKEN}
'''


class DnspodHookTests(unittest.TestCase):
    def run_hook(self, code):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        result = subprocess.run(["sh", "-c", '. "$1"; ' + STUBS + code, "hook-test", str(HOOK)],
                                cwd=tmp.name, capture_output=True, text=True)
        root = Path(tmp.name)
        return result, root

    def test_add_finds_the_zone_and_creates_a_txt_record(self):
        result, root = self.run_hook(
            "dns_dp_add _acme-challenge.dashplay.apeng.fun challenge-value\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(TOKEN, result.stdout)
        self.assertNotIn(TOKEN, result.stderr)
        calls = (root / "calls").read_text().splitlines()
        self.assertEqual(calls, [
            "https://dnsapi.cn/Domain.Info",
            "https://dnsapi.cn/Domain.Info",
            "https://dnsapi.cn/Domain.Info",
            "https://dnsapi.cn/Record.Create",
        ])
        create = (root / "bodies").read_text().splitlines()[-1]
        self.assertIn("domain_id=42", create)
        self.assertIn("sub_domain=_acme-challenge.dashplay", create)
        self.assertIn("record_type=TXT", create)
        self.assertIn("value=challenge-value", create)
        self.assertNotIn("login_token", result.stdout)

    def test_remove_deletes_only_the_matching_txt(self):
        result, root = self.run_hook(
            "dns_dp_rm _acme-challenge.dashplay.apeng.fun challenge-value\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = (root / "calls").read_text().splitlines()
        self.assertEqual(calls[-2:], [
            "https://dnsapi.cn/Record.List",
            "https://dnsapi.cn/Record.Remove",
        ])
        remove = (root / "bodies").read_text().splitlines()[-1]
        self.assertIn("record_id=7", remove)
        self.assertNotIn(TOKEN, result.stdout)
