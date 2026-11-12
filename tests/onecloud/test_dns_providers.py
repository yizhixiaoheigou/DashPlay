"""Synthetic provider replies only: no accounts, live DNS writes or CA issuance."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

import dns_provider as dns
import own_domain as own
import setup_wizard
import supervisor
import supervisor_openwrt
import upgrade_checks
import test_own_domain as fixtures
from test_own_domain import make_certificate
import test_phone_uplink as uplink_fixtures

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "syntheticTokenForProvider12345"


def config(provider, name="box.example.com"):
    return own.provider_config(provider, name, TOKEN, "10001")


class ConfigTests(unittest.TestCase):
    def test_load_full_names_without_duckdns_suffix_or_ipv6(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for provider in ("dnspod", "dynv6"):
                saved = config(provider, "Box.Example.com.")
                (state / "ddns.json").write_text(json.dumps(dict(saved, ipv6=True)))
                self.assertEqual(own.load(state), saved)
                self.assertEqual(own.name_of(saved), "box.example.com")
                self.assertTrue(setup_wizard.answers_own_name({"own_name_on_hotspot": False}, saved))
                self.assertEqual(supervisor.hotspot_names(state, "box.example.com", "box_hotspot"), ["box.example.com"])

    def test_reject_bad_credentials_and_unknown_providers(self):
        for provider in ("dnspod", "dynv6"):
            for token in ("", "short", TOKEN + "\nInjected: yes", "x&password=" + TOKEN):
                with self.subTest(provider=provider, token=token), self.assertRaises(ValueError):
                    own.provider_config(provider, "box.example.com", token, "10001")
        for token_id in ("", "１２３", "AKIDexample", "10&1"):
            with self.assertRaises(ValueError):
                own.provider_config("dnspod", "box.example.com", TOKEN, token_id)
        with self.assertRaises(ValueError):
            own.provider_config("other", "box.example.com", TOKEN)

    def test_openwrt_uses_separate_credentials_and_keeps_old_duckdns(self):
        for provider in ("dnspod", "dynv6"):
            values = dict(supervisor_openwrt.DEFAULTS, ddns_provider=provider,
                          ddns_domain="box.example.com", dnspod_id="10001")
            values[provider + "_token"] = TOKEN
            self.assertEqual(supervisor_openwrt.own_config(values), config(provider))
            values["ddns_provider"] = "dynv6" if provider == "dnspod" else "dnspod"
            self.assertIsNone(supervisor_openwrt.own_config(values))

    def test_readiness_queries_selected_provider(self):
        for provider, host in (("dnspod", "dnsapi.cn"), ("dynv6", "dynv6.com")):
            with patch.object(own, "default_route_families", return_value=[4]), patch.object(own.socket, "getaddrinfo") as resolve:
                self.assertIsNone(own.network_ready(resolve=resolve, provider=provider))
                self.assertEqual(resolve.call_args_list[0].args[0], host)

    def test_upgrade_requires_both_hook_and_python_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "state").mkdir(); (root / "acme/dnsapi").mkdir(parents=True)
            for provider in ("dnspod", "dynv6"):
                saved = config(provider)
                (root / "state/ddns.json").write_text(json.dumps(saved))
                with patch.object(upgrade_checks, "__file__", str(root / "upgrade_checks.py")):
                    with self.assertRaises(ValueError):
                        upgrade_checks.inspect(root)
                    (root / "acme/dnsapi" / (own.DNS_HOOKS[provider] + ".sh")).touch()
                    with self.assertRaises(ValueError):
                        upgrade_checks.inspect(root)
                    (root / "dns_provider.py").touch()
                    self.assertEqual(upgrade_checks.inspect(root), saved)
                    (root / "dns_provider.py").unlink()


class FormTests(unittest.TestCase):
    setUp = fixtures.SettingsPageTests.setUp
    save = fixtures.SettingsPageTests.save

    def test_save_keep_and_switch_requires_new_credentials(self):
        self.save(action="save", provider="dnspod", domain="box.example.com", dnspod_id="10001", token=TOKEN)
        path = self.state / "ddns.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.save(action="save", provider="dnspod", domain="box.example.com", token="")
        self.assertEqual(own.load(self.state), config("dnspod"))
        for changes in ({"provider": "dynv6"}, {"domain": "other.example.com"}, {"dnspod_id": "10002"}):
            before = path.read_bytes()
            form = dict(action="save", provider="dnspod", domain="box.example.com", token="")
            form.update(changes)
            with self.assertRaises(ValueError):
                self.save(**form)
            self.assertEqual(path.read_bytes(), before)
        self.save(action="save", provider="dynv6", domain="box.example.com", token=TOKEN, ipv6="on")
        self.assertEqual(own.load(self.state), config("dynv6"))
        self.assertNotIn(TOKEN, self.page.render(""))
        self.assertNotIn("10001", self.page.render(""))
        self.save(action="clear")
        self.assertIsNone(own.load(self.state))

    def test_provider_change_same_name_keeps_certificate_new_name_drops_it(self):
        self.save(action="save", provider="dnspod", domain="box.example.com", dnspod_id="10001", token=TOKEN)
        own_dir = self.state / "tls/own"; own_dir.mkdir(parents=True)
        (own_dir / "fullchain.pem").write_text("synthetic marker")
        self.save(action="save", provider="dynv6", domain="box.example.com", token=TOKEN)
        self.assertTrue(own_dir.exists())
        self.save(action="save", provider="dynv6", domain="other.example.com", token=TOKEN)
        self.assertFalse(own_dir.exists())

    def test_storage_failure_does_not_drop_old_certificate(self):
        self.save(action="save", domain="mycar", token=TOKEN)
        own_dir = self.state / "tls/own"; own_dir.mkdir(parents=True)
        (own_dir / "fullchain.pem").write_text("synthetic marker")
        with patch("settings_page._save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.save(action="save", provider="dynv6", domain="box.example.com", token=TOKEN)
        self.assertTrue(own_dir.exists())
        self.assertEqual(own.name_of(own.load(self.state)), "mycar.duckdns.org")


class ProviderAPITests(unittest.TestCase):
    def replies(self, client, replies):
        calls = []
        def open_request(request, timeout):
            self.assertGreater(timeout, 0); self.assertLessEqual(timeout, 15)
            calls.append(request)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps(reply).encode() if reply is not None else b"")
        client.opener.open = open_request
        return calls

    def pod(self, records):
        client = dns.DNSPod(config("dnspod"))
        replies = [{"status": {"code": "6"}},
                   {"status": {"code": "1"}, "domain": {"id": "42", "name": "example.com", "ttl": "600"}},
                   {"status": {"code": "1"}, "records": records}, {"status": {"code": "1"}}]
        return client, self.replies(client, replies)

    def fields(self, request):
        return {k: v[0] for k, v in urllib.parse.parse_qs(request.data.decode()).items()}

    def test_dnspod_creates_address_and_keeps_credentials_out_of_url(self):
        client, calls = self.pod([])
        client.address("box.example.com", "192.168.43.10")
        self.assertEqual(calls[-1].full_url, "https://dnsapi.cn/Record.Create")
        self.assertEqual(self.fields(calls[-1])["record_type"], "A")
        self.assertEqual(self.fields(calls[-1])["value"], "192.168.43.10")
        self.assertEqual(self.fields(calls[-1])["sub_domain"], "box")
        self.assertEqual(self.fields(calls[-1])["login_token"], "10001," + TOKEN)
        self.assertTrue(all(TOKEN not in r.full_url for r in calls))

    def test_dnspod_updates_only_exact_default_address_and_skips_unchanged(self):
        record = dict(id="8", name="box", type="A", line="默认", value="192.168.43.9", enabled="1")
        client, calls = self.pod([record, dict(record, id="9", name="other")])
        client.address("box.example.com", "192.168.43.10")
        self.assertTrue(calls[-1].full_url.endswith("Record.Modify"))
        self.assertEqual(self.fields(calls[-1])["record_id"], "8")
        client, calls = self.pod([record])
        client.address("box.example.com", record["value"])
        self.assertEqual(len(calls), 3)

    def test_dnspod_conflicts_never_mutate_records(self):
        a = dict(id="8", name="box", type="A", line="默认", value="192.168.43.9")
        for records in ([a, a], [dict(a, type="AAAA")], [dict(a, type="CNAME")], [dict(a, line="电信")]):
            client, calls = self.pod(records)
            with self.assertRaises(dns.ProviderError) as raised:
                client.address("box.example.com", "192.168.43.10")
            self.assertEqual(raised.exception.reason, "conflict")
            self.assertEqual(len(calls), 3)

    def test_dnspod_txt_cleanup_preserves_other_challenges_and_other_types(self):
        record = dict(id="8", name="_acme-challenge.box", type="TXT", value="current")
        client = dns.DNSPod(config("dnspod"))
        calls = self.replies(client, [
            {"status": {"code": "6"}},
            {"status": {"code": "1"}, "domain": {"id": "42", "name": "example.com"}},
            {"status": {"code": "1"}, "records": [record, dict(record, id="9", value="other"),
                                                       dict(record, id="10", type="A")]},
            {"status": {"code": "1"}}])
        client.txt("_acme-challenge.box.example.com", "current", remove=True)
        self.assertTrue(calls[-1].full_url.endswith("Record.Remove"))
        self.assertEqual(self.fields(calls[-1])["record_id"], "8")
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.fields(calls[0])["domain"], "box.example.com")

    def test_dnspod_txt_add_and_repeated_cleanup_are_idempotent(self):
        client = dns.DNSPod(config("dnspod"))
        replies = [{"status": {"code": "6"}},
                   {"status": {"code": "1"}, "domain": {"id": "42", "name": "example.com"}},
                   {"status": {"code": "10"}}, {"status": {"code": "1"}}]
        calls = self.replies(client, replies)
        client.txt("_acme-challenge.box.example.com", "current")
        self.assertEqual(self.fields(calls[-1])["record_type"], "TXT")
        self.assertEqual(self.fields(calls[-1])["value"], "current")
        self.assertEqual(self.fields(calls[-1])["sub_domain"], "_acme-challenge.box")
        client = dns.DNSPod(config("dnspod"))
        calls = self.replies(client, [{"status": {"code": "6"}},
                   {"status": {"code": "1"}, "domain": {"id": "42", "name": "example.com"}},
                   {"status": {"code": "10"}}])
        client.txt("_acme-challenge.box.example.com", "current", remove=True)
        self.assertEqual(len(calls), 3)

    def test_dnspod_records_paginate_before_mutation(self):
        client = dns.DNSPod(config("dnspod"))
        calls = self.replies(client, [{"status": {"code": "1"}, "records": [dict(name="box")] * 100},
                                      {"status": {"code": "1"}, "records": [dict(name="box")]}])
        self.assertEqual(len(client.records("42", "box")), 101)
        self.assertEqual(self.fields(calls[1])["offset"], "100")

    def dyn(self, sub="", records=(), **zone_fields):
        client = dns.Dynv6(config("dynv6"))
        zones = [dict(id=7, name="example.com"), dict(id=42, name="box.example.com", **zone_fields)]
        calls = self.replies(client, [zones, list(records), None])
        return client, calls

    def test_dynv6_apex_and_nested_names_update_correct_resource(self):
        client, calls = self.dyn(ipv4address="192.168.43.9")
        client.address("box.example.com", "192.168.43.10")
        self.assertEqual(calls[-1].full_url, "https://dynv6.com/api/v2/zones/42")
        self.assertEqual(calls[-1].method, "PATCH")
        self.assertEqual(json.loads(calls[-1].data), {"ipv4address": "192.168.43.10"})
        self.assertEqual(calls[-1].get_header("Authorization"), "Bearer " + TOKEN)
        client, calls = self.dyn()
        client.address("car.box.example.com", "192.168.43.10")
        self.assertEqual(calls[-1].full_url, "https://dynv6.com/api/v2/zones/42/records")
        self.assertEqual(json.loads(calls[-1].data), {"name": "car", "type": "A", "data": "192.168.43.10"})

    def test_dynv6_conflicting_zone_ipv6_is_not_erased(self):
        client, calls = self.dyn(ipv6prefix="2001:db8::/64")
        with self.assertRaises(dns.ProviderError) as raised:
            client.address("box.example.com", "192.168.43.10")
        self.assertEqual(raised.exception.reason, "conflict")
        self.assertEqual(len(calls), 2)

    def test_dynv6_updates_existing_subdomain_and_rejects_false_suffix(self):
        client, calls = self.dyn(records=[dict(name="car", id=8, type="A", data="192.168.43.9")])
        client.address("car.box.example.com", "192.168.43.10")
        self.assertEqual(calls[-1].method, "PATCH")
        self.assertTrue(calls[-1].full_url.endswith("/records/8"))
        client, calls = self.dyn()
        with self.assertRaises(dns.ProviderError):
            client.address("notexample.com", "192.168.43.10")
        self.assertEqual(len(calls), 1)

    def test_dynv6_txt_add_and_cleanup_only_exact_value(self):
        client, calls = self.dyn()
        client.txt("_acme-challenge.box.example.com", "current")
        self.assertEqual(json.loads(calls[-1].data), {"name": "_acme-challenge", "type": "TXT", "data": "current"})
        record = dict(id=8, name="_acme-challenge", type="TXT", data="current")
        client, calls = self.dyn(records=[record, dict(record, id=9, data="other"), dict(record, id=10, name="other")])
        client.txt("_acme-challenge.box.example.com", "current", remove=True)
        self.assertEqual(calls[-1].method, "DELETE")
        self.assertTrue(calls[-1].full_url.endswith("/records/8"))
        self.assertEqual(len(calls), 3)

    def test_timeouts_bad_replies_auth_errors_and_redirects_are_safe(self):
        for provider in ("dnspod", "dynv6"):
            for code, expected in ((401, "denied"), (403, "denied"), (422, "invalid"), (429, "failed"), (302, "failed")):
                client = dns.client(config(provider))
                self.replies(client, [urllib.error.HTTPError("https://provider/", code, TOKEN, {}, io.BytesIO())])
                with patch.object(dns, "client", return_value=client):
                    self.assertEqual(dns.update_address(config(provider), "192.168.43.10"), expected)
            for reply in (None, [], {}, OSError(TOKEN)):
                client = dns.client(config(provider)); self.replies(client, [reply])
                with patch.object(dns, "client", return_value=client):
                    self.assertIn(dns.update_address(config(provider), "192.168.43.10"), ("failed", "denied"))
        self.assertIsNone(dns.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other/"))
        client = dns.client(config("dynv6")); client.deadline = 0
        with patch.object(client.opener, "open") as request, self.assertRaises(dns.ProviderError):
            client.address("box.example.com", "192.168.43.10")
        request.assert_not_called()


class MaintainerTests(unittest.TestCase):
    TOKEN = TOKEN
    setUp = fixtures.DesecMaintainerTests.setUp
    run_until = fixtures.DesecMaintainerTests.run_until

    def test_both_providers_install_valid_certificate_and_pass_environment(self):
        for provider in ("dnspod", "dynv6"):
            with self.subTest(provider=provider):
                own.forget_certificate(self.state)
                saved = config(provider, self.name)
                make_certificate(self.fake, self.name)
                worker = own.Maintainer(self.root, saved, "100.96.0.1", self.stopped, say=lambda _: None)
                real_popen = subprocess.Popen
                environments = []
                def intercept(*args, **kwargs):
                    if "env" in kwargs:
                        environments.append(kwargs["env"])
                    return real_popen(*args, **kwargs)
                with patch.object(own.subprocess, "Popen", side_effect=intercept):
                    self.assertEqual(worker.check_certificate(), own.CERT_CHECK)
                self.assertTrue(own.certificate_for(self.state, self.name, own.RENEW_DAYS))
                calls = (self.fake / "calls").read_text()
                self.assertIn("--dns " + own.DNS_HOOKS[provider], calls)
                self.assertNotIn(TOKEN, calls)
                self.assertEqual(environments[0]["DP_Key" if provider == "dnspod" else "DYNV6_TOKEN"], TOKEN)
                self.assertEqual(environments[0]["DASHPLAY_DNS_DOMAIN"], self.name)
                self.assertEqual(environments[0]["DASHPLAY_DNS_HELPER"], str(self.root / "dns_provider.py"))

    def test_publish_on_joined_network_only_and_keep_local_answer(self):
        for provider in ("dnspod", "dynv6"):
            for interface in (None, "wlan-test"):
                self.stopped = threading.Event()
                own.forget_certificate(self.state)
                worker = own.Maintainer(self.root, config(provider), "192.168.43.10", self.stopped,
                                       say=lambda _: None, interface=interface)
                with patch.object(own, "update_record", return_value="ok") as update:
                    status = self.run_until(worker, "ok" if interface else "")
                self.assertEqual(status["record"], "local")
                self.assertEqual(update.call_count, int(bool(interface)))

    def test_failed_issuance_retries_and_later_succeeds(self):
        for provider in ("dnspod", "dynv6"):
            own.forget_certificate(self.state)
            make_certificate(self.fake, self.name)
            worker = own.Maintainer(self.root, config(provider, self.name), "100.96.0.1", self.stopped, say=lambda _: None)
            with patch.dict(os.environ, FAKE_ISSUE="1"):
                self.assertEqual(worker.check_certificate(), own.CERT_RETRY_STEPS[0])
            self.assertFalse(own.certificate_for(self.state, self.name))
            self.assertEqual(worker.check_certificate(), own.CERT_CHECK)
            self.assertTrue(own.certificate_for(self.state, self.name))
            self.assertEqual(worker.failures, 0)


class UplinkTests(unittest.TestCase):
    setUp = uplink_fixtures.IntegratedLegacyDnsTests.setUp

    def test_names_remain_local_when_phone_upstream_changes(self):
        for provider in ("dnspod", "dynv6"):
            (self.state / "ddns.json").write_text(json.dumps(config(provider)))
            self.status.update(tesla=False, own_name="box.example.com")
            self.assertEqual(self.u.own_names(), ["box.example.com"])
            self.now = self.u.next_try
            self.u.tick()
            self.assertEqual(self.u.phase, "online")
            self.assertEqual(self.p.start_dhcp.call_args.args[2], ["box.example.com"])
            self.u.fallback("test")
            self.assertIn("box.example.com", self.p.start_dhcp.call_args.args[2])


class HookTests(unittest.TestCase):
    def test_shell_hooks_pass_arguments_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            helper.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
            for provider in ("dnspod", "dynv6"):
                hook = own.DNS_HOOKS[provider]
                for action in ("add", "rm"):
                    result = subprocess.run(["sh", "-c", '. "$1"; ' + hook + '_' + action + ' "$2" "$3"',
                        "test", str(ROOT / "scripts/onecloud/acme/dnsapi" / (hook + ".sh")),
                        "_acme-challenge.box.example.com", "challenge-value"], capture_output=True, text=True,
                        env=dict(os.environ, DASHPLAY_DNS_HELPER=str(helper), DP_Key=TOKEN, DYNV6_TOKEN=TOKEN))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), [provider, action, "_acme-challenge.box.example.com", "challenge-value"])
                    self.assertNotIn(TOKEN, result.stdout + result.stderr)

    def test_helper_refuses_other_names_before_any_network_and_redacts_errors(self):
        with patch.dict(os.environ, DASHPLAY_DNS_DOMAIN="box.example.com", DYNV6_TOKEN=TOKEN), \
                patch.object(dns.sys, "argv", ["helper", "dynv6", "rm", "_acme-challenge.other.example.com", "value"]), \
                patch.object(dns, "client") as client, patch.object(dns.sys, "stderr", new_callable=io.StringIO) as error:
            self.assertEqual(dns.main(), 1)
            client.assert_not_called()
            self.assertNotIn(TOKEN, error.getvalue())


if __name__ == "__main__":
    unittest.main()
