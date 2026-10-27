"""Own DuckDNS name with a Let's Encrypt certificate (own_domain.py): for car browsers that refuse the box's local
CA with no "continue" (2026-10-13). acme.sh is replaced by a script that hands out a synthetic certificate."""
import json
import fcntl
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/onecloud"))
import boxnet
import own_domain
import settings_page
from settings_page import SettingsPage
import setup_wizard
import supervisor_openwrt

TOKEN = "0a1b2c3d-1111-2222-3333-444455556666"
FAKE_ACME = """#!/bin/sh
# Records how it was called; --install-cert copies the prepared certificate to the requested files.
echo "$*" >> "$FAKE_DIR/calls"
echo "$PATH" >> "$FAKE_DIR/env"
echo "${ACME_USE_IPV4_REQUESTS:-}" >> "$FAKE_DIR/ipv4"
[ -n "$DuckDNS_Token" ] && echo "token in environment" >> "$FAKE_DIR/calls"
[ -n "$DEDYN_TOKEN" ] && echo "desec token in environment" >> "$FAKE_DIR/calls"
[ -n "$DP_Id" ] && [ -n "$DP_Key" ] && echo "dnspod token in environment" >> "$FAKE_DIR/calls"
case " $* " in *" --issue "*) [ -n "$FAKE_SLEEP" ] && sleep "$FAKE_SLEEP"; exit "${FAKE_ISSUE:-0}" ;; esac
while [ $# -gt 0 ]; do
    case "$1" in --fullchain-file) cp "$FAKE_DIR/fullchain.pem" "$2" ;; --key-file) cp "$FAKE_DIR/key.pem" "$2" ;; esac
    shift
done
"""


def make_certificate(directory, name, days=90, validity=None):
    run = lambda *a: subprocess.run(["openssl", *a], check=True, capture_output=True, cwd=directory)
    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "ca.key",
        "-out", "ca.crt", "-days", "100", "-subj", "/CN=Synthetic CA")
    run("req", "-new", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", "key.pem",
        "-out", "leaf.csr", "-subj", "/CN=" + name)
    (Path(directory) / "ext").write_text(f"subjectAltName=DNS:{name}\n")
    if validity is None:
        run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-days", str(days),
            "-extfile", "ext", "-out", "leaf.crt")
    else:   # ca supports explicit dates on the older OpenSSL 3 versions shipped on boxes too
        (Path(directory) / "index").write_text("")
        (Path(directory) / "serial").write_text("01\n")
        (Path(directory) / "ca.cnf").write_text(
            "[ca]\ndefault_ca=synthetic\n[synthetic]\ndatabase=index\nserial=serial\n"
            "new_certs_dir=.\ndefault_md=sha256\npolicy=subject\nx509_extensions=leaf\n"
            f"[subject]\ncommonName=supplied\n[leaf]\nsubjectAltName=DNS:{name}\n")
        run("ca", "-batch", "-notext", "-config", "ca.cnf", "-cert", "ca.crt", "-keyfile", "ca.key",
            "-in", "leaf.csr", "-startdate", validity[0], "-enddate", validity[1], "-out", "leaf.crt")
    (Path(directory) / "fullchain.pem").write_text((Path(directory) / "leaf.crt").read_text()
                                                   + (Path(directory) / "ca.crt").read_text())


class ConfigTests(unittest.TestCase):
    def test_sub_domain_accepts_the_full_name_and_capitals(self):
        self.assertEqual(own_domain.sub_domain(" MyCar.duckdns.org "), "mycar")
        for bad in ("", "my car", "-x", "a.b", "x" * 64):
            with self.assertRaises(ValueError):
                own_domain.sub_domain(bad)
        with self.assertRaises(ValueError):
            own_domain.check_token("short")

    def test_openwrt_reads_it_from_uci_and_ignores_a_bad_one(self):
        base = supervisor_openwrt.read_config("dashplay.main.duckdns_domain='MyCar'\n"
                                              f"dashplay.main.duckdns_token='{TOKEN}'\n")
        self.assertEqual(supervisor_openwrt.own_config(base), {"domain": "mycar", "token": TOKEN})
        self.assertIsNone(supervisor_openwrt.own_config(supervisor_openwrt.read_config("")))
        bad = supervisor_openwrt.read_config("dashplay.main.duckdns_domain='mycar'\ndashplay.main.duckdns_token='x'\n")
        self.assertIsNone(supervisor_openwrt.own_config(bad), "a wrong token turns it off, CarPlay still starts")


class DesecConfigTests(unittest.TestCase):
    TOKEN = "A1b2C3d4E5f6G7h8J9kMnPqRsTuV"

    def test_full_name_validation(self):
        self.assertEqual(own_domain.domain_name(" BOX01.Car.Example.com. "),
                         "box01.car.example.com")
        for bad in ("", "no-dot", "a..b", "-x.example.com", "a_b.example.com", "http://a.example.com",
                    "a.example.com/path"):
            with self.assertRaises(ValueError):
                own_domain.domain_name(bad)
        with self.assertRaises(ValueError):
            own_domain.check_desec_token("short")

    def test_load_and_name(self):
        config = {"provider": "desec", "domain": "box01.car.example.com",
                  "token": self.TOKEN, "ipv6": True}
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "ddns.json").write_text(json.dumps(config))
            loaded = own_domain.load(state)
            self.assertEqual(loaded, {"provider": "desec", "domain": "box01.car.example.com",
                                      "token": self.TOKEN})
            self.assertEqual(own_domain.name_of(loaded), "box01.car.example.com")

    def test_no_public_record(self):
        config = {"provider": "desec", "domain": "box01.car.example.com", "token": self.TOKEN}
        with patch.object(own_domain.urllib.request, "urlopen",
                          side_effect=AssertionError("deSEC must not publish an A record")):
            self.assertTrue(own_domain.update_record(config, "100.96.0.1"))

    def test_dnspod_name_is_local_and_waits_without_a_token(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "ddns.json").write_text(json.dumps(
                {"provider": "dnspod", "domain": "DashPlay.apeng.fun", "id": "", "token": ""}))
            loaded = own_domain.load(state)
            self.assertEqual(loaded["domain"], "dashplay.apeng.fun")
            self.assertEqual(own_domain.name_of(loaded), "dashplay.apeng.fun")
            self.assertEqual(loaded["id"], "")
            with patch.object(own_domain.urllib.request, "urlopen",
                              side_effect=AssertionError("DNSPod must not publish an A record")):
                self.assertTrue(own_domain.update_record(loaded, "100.96.0.1"))
        for bad in ("", "abc", "12 34"):
            with self.assertRaises(ValueError):
                own_domain.check_dnspod_id(bad)
        with self.assertRaises(ValueError):
            own_domain.check_dnspod_token("short")


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command")
class DesecMaintainerTests(unittest.TestCase):
    TOKEN = "A1b2C3d4E5f6G7h8J9kMnPqRsTuV"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "dashplay"; self.state = self.root / "state"
        (self.root / "acme").mkdir(parents=True); self.state.mkdir()
        self.fake = Path(tmp.name) / "fake"; self.fake.mkdir()
        acme = self.root / "acme" / "acme.sh"
        acme.write_text(FAKE_ACME); acme.chmod(0o755)
        self.name = "box01.car.example.com"
        self.config = {"provider": "desec", "domain": self.name, "token": self.TOKEN}
        env = patch.dict(os.environ, FAKE_DIR=str(self.fake)); env.start(); self.addCleanup(env.stop)
        ready = patch.object(own_domain, "network_ready", lambda provider=None: None)
        ready.start(); self.addCleanup(ready.stop)
        self.stopped = threading.Event()

    def test_obtains_with_desec_and_no_public_record(self):
        make_certificate(self.fake, self.name)
        maintainer = own_domain.Maintainer(self.root, self.config, "100.96.0.1", self.stopped, say=lambda _: None)
        self.assertEqual(maintainer.check_certificate(), own_domain.CERT_CHECK)
        calls = (self.fake / "calls").read_text()
        self.assertIn("--dns dns_desec", calls)
        self.assertIn("desec token in environment", calls)
        self.assertNotIn(self.TOKEN, calls, "the token stays off acme.sh's command line")
        self.assertTrue(own_domain.certificate_for(self.state, self.name, own_domain.RENEW_DAYS))

    def test_record_stays_local(self):
        maintainer = own_domain.Maintainer(self.root, self.config, "100.96.0.1", self.stopped, say=lambda _: None)
        with patch.object(maintainer, "check_certificate", lambda: own_domain.CERT_CHECK), \
                patch.object(own_domain, "update_record", side_effect=AssertionError("no public record")):
            maintainer.start()
            for _ in range(50):
                if own_domain.read_status(self.state).get("record") == "local":
                    break
                time.sleep(0.1)
            self.stopped.set(); maintainer.join(10)
        status = own_domain.read_status(self.state)
        self.assertEqual(status.get("record"), "local")
        self.assertEqual(status.get("address"), "100.96.0.1")


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command")
class DnspodMaintainerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "dashplay"; self.state = self.root / "state"
        (self.root / "acme").mkdir(parents=True); self.state.mkdir()
        self.fake = Path(tmp.name) / "fake"; self.fake.mkdir()
        acme = self.root / "acme" / "acme.sh"
        acme.write_text(FAKE_ACME); acme.chmod(0o755)
        self.name = "dashplay.apeng.fun"
        self.config = {"provider": "dnspod", "domain": self.name, "id": "10001",
                       "token": "syntheticdnspodtoken1"}
        env = patch.dict(os.environ, FAKE_DIR=str(self.fake)); env.start(); self.addCleanup(env.stop)
        ready = patch.object(own_domain, "network_ready", lambda provider=None: None)
        ready.start(); self.addCleanup(ready.stop)
        self.stopped = threading.Event()

    def test_obtains_with_dnspod_and_keeps_the_token_off_the_command_line(self):
        make_certificate(self.fake, self.name)
        maintainer = own_domain.Maintainer(self.root, self.config, "100.96.0.1", self.stopped, say=lambda _: None)
        self.assertEqual(maintainer.check_certificate(), own_domain.CERT_CHECK)
        calls = (self.fake / "calls").read_text()
        self.assertIn("--dns dns_dp", calls)
        self.assertIn("dnspod token in environment", calls)
        self.assertNotIn(self.config["token"], calls)
        self.assertNotIn(self.config["id"], calls)

    def test_missing_token_waits_without_calling_acme(self):
        config = dict(self.config, id="", token="")
        maintainer = own_domain.Maintainer(self.root, config, "100.96.0.1", self.stopped, say=lambda _: None)
        self.assertEqual(maintainer.check_certificate(), own_domain.READY_RETRY)
        self.assertEqual(own_domain.read_status(self.state).get("reason"), "token")
        self.assertFalse((self.fake / "calls").exists())


@unittest.skipUnless(shutil.which("openssl"), "needs the openssl command")
class MaintainerTests(unittest.TestCase):
    def test_busy_signer_waits_without_counting_a_network_failure(self):
        worker = self.maintainer()
        with patch.object(worker, 'obtain', side_effect=BlockingIOError()):
            self.assertEqual(worker.check_certificate(), own_domain.READY_RETRY)
        self.assertEqual(worker.failures, 0)
        self.assertEqual(own_domain.read_status(self.state)['reason'], 'busy')

    def test_signer_keeps_lock_after_supervisor_is_killed(self):
        script = self.root / 'acme/acme.sh'
        script.write_text(FAKE_ACME.replace('echo "$*"', 'echo $$ > "$FAKE_DIR/pid"\necho "$*"', 1))
        code = ('import sys, threading; sys.path.insert(0, sys.argv[1]); import own_domain; '
                'own_domain.Maintainer(sys.argv[2], {"domain":"mycar", "token":"' + TOKEN + '"}, '
                'None, threading.Event()).obtain()')
        parent = subprocess.Popen([sys.executable, '-c', code, str(Path(own_domain.__file__).parent), str(self.root)],
                                  env=dict(os.environ, FAKE_SLEEP='30'), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        signer = None
        try:
            deadline = time.monotonic() + 10
            while not (self.fake / 'pid').exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue((self.fake / 'pid').exists(), 'the signer must actually start')
            signer = int((self.fake / 'pid').read_text())
            parent.kill(); parent.wait(timeout=5)
            with (self.state / 'acme/dashplay.lock').open('a') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            if parent.poll() is None:
                parent.kill(); parent.wait(timeout=5)
            if signer:
                try:
                    os.killpg(signer, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_contending_worker_cannot_rotate_the_active_log(self):
        home = self.state / 'acme'
        home.mkdir()
        log = home / 'acme.log'
        log.write_text('active attempt')
        with (home / 'dashplay.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.maintainer().obtain()
            self.assertTrue(log.exists(), 'the busy worker must leave the active log in place')
            self.assertEqual(log.read_text(), 'active attempt')

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "dashplay"; self.state = self.root / "state"
        (self.root / "acme").mkdir(parents=True); self.state.mkdir()
        self.fake = Path(tmp.name) / "fake"; self.fake.mkdir()
        acme = self.root / "acme" / "acme.sh"
        acme.write_text(FAKE_ACME); acme.chmod(0o755)
        self.config = {"domain": "mycar", "token": TOKEN}
        self.name = "mycar.duckdns.org"
        env = patch.dict(os.environ, FAKE_DIR=str(self.fake)); env.start(); self.addCleanup(env.stop)
        self.records = []
        self.sent = []   # (address, ipv6, clear) as DuckDNS was asked
        record = patch.object(own_domain, "update_record", lambda config, address, ipv6=None, clear=False: (
            self.records.append(address), self.sent.append((address, ipv6, clear)))[0] or True)
        record.start(); self.addCleanup(record.stop)
        self.stopped = threading.Event()
        ready = patch.object(own_domain, "network_ready", lambda: None)   # the test host's network is not the box's
        ready.start(); self.addCleanup(ready.stop)

    def maintainer(self, address="192.168.5.80"):
        return own_domain.Maintainer(self.root, self.config, address, self.stopped, say=lambda _: None)

    def calls(self):
        return (self.fake / "calls").read_text()

    def test_obtains_installs_and_serves_the_certificate(self):
        make_certificate(self.fake, self.name)
        self.assertEqual(self.maintainer().check_certificate(), own_domain.CERT_CHECK)
        own = self.state / "tls" / "own"
        self.assertTrue(own_domain.certificate_for(self.state, self.name, own_domain.RENEW_DAYS))
        self.assertEqual((own / "key.pem").stat().st_mode & 0o777, 0o600)
        self.assertEqual(own_domain.read_status(self.state)["phase"], "ready")
        self.assertIn("--dns dns_duckdns", self.calls())
        self.assertIn("token in environment", self.calls())
        self.assertNotIn(TOKEN, self.calls(), "the token stays off acme.sh's command line")
        self.assertIn("下次开机时启用", own_domain.message(self.state, self.name))
        boxnet.ensure_leaf(self.state, [self.name], ["192.168.5.80"])   # the next start
        self.assertTrue(own_domain.serving(self.state))
        self.assertNotIn("下次开机", own_domain.message(self.state, self.name))

    def test_a_current_certificate_is_left_alone(self):
        make_certificate(self.fake, self.name)
        self.maintainer().check_certificate()
        (self.fake / "calls").unlink()
        self.assertEqual(self.maintainer().check_certificate(), own_domain.CERT_CHECK)
        self.assertFalse((self.fake / "calls").exists(), "acme.sh not run while more than 30 days remain")

    def test_a_certificate_for_another_name_is_refused(self):
        make_certificate(self.fake, "someone-else.duckdns.org")
        self.assertEqual(self.maintainer().check_certificate(), own_domain.CERT_RETRY_STEPS[0])
        self.assertFalse((self.state / "tls" / "own" / "fullchain.pem").exists())
        status = own_domain.read_status(self.state)
        self.assertEqual((status["phase"], status["reason"]), ("retry", "certificate"))
        self.assertIn("会自动重试", own_domain.message(self.state, self.name))

    def test_failures_retry_from_a_minute_up_to_hourly(self):
        with patch.dict(os.environ, FAKE_ISSUE="1"):
            worker = self.maintainer()
            self.assertEqual([worker.check_certificate() for _ in range(6)],
                             [60, 120, 300, 900, 3600, 3600])

    def test_slow_failures_wait_the_full_retry_interval_after_completion(self):
        worker = self.maintainer(None)
        clock, starts, waits = [0], [], []

        def obtain():
            starts.append(clock[0])
            clock[0] += 240
            if len(starts) == 6:
                self.stopped.set()
            raise TimeoutError("synthetic slow failure")

        def wait(deadline):
            waits.append(max(0, deadline - clock[0]))
            clock[0] = max(clock[0], deadline)
            return False

        with patch.object(worker, "obtain", obtain), patch.object(worker, "wait_until", wait), \
                patch.object(own_domain.time, "monotonic", lambda: clock[0]):
            worker.run()
        self.assertEqual(starts, [0, 300, 660, 1200, 2340, 6180])
        self.assertEqual(waits, [60, 120, 300, 900, 3600, 3600])

    def test_no_route_or_dns_waits_without_running_acme(self):
        for reason in ("route", "dns"):
            with patch.object(own_domain, "network_ready", lambda reason=reason: reason):
                self.assertEqual(self.maintainer().check_certificate(), own_domain.READY_RETRY)
            self.assertFalse((self.fake / "calls").exists(), "nothing run while the network is not ready")
            status = own_domain.read_status(self.state)
            self.assertEqual((status["phase"], status["reason"]), ("waiting", reason))
        self.assertIn("等待盒子联网", own_domain.message(self.state, self.name))

    def test_acme_runs_with_the_curl_shim_first_in_path(self):
        make_certificate(self.fake, self.name)
        self.maintainer().check_certificate()
        first = (self.fake / "env").read_text().splitlines()[0].split(os.pathsep)[0]
        self.assertEqual(first, str(self.state / "acme" / "bin"))
        shim = self.state / "acme" / "bin" / "curl"
        self.assertIn("--connect-timeout 10", shim.read_text())
        self.assertIn("--max-time 90", shim.read_text())
        self.assertEqual(shim.stat().st_mode & 0o777, 0o700)

    def test_the_previous_acme_log_is_kept(self):
        make_certificate(self.fake, self.name)
        worker = self.maintainer()
        worker.obtain()
        (self.state / "acme" / "acme.log").write_text("the run that failed")
        worker.obtain()
        self.assertEqual((self.state / "acme" / "acme.log.1").read_text(), "the run that failed")

    def test_ipv4_is_preferred_where_the_box_has_an_ipv4_route(self):
        make_certificate(self.fake, self.name)
        with patch.object(own_domain, "default_route_families", lambda *a, **k: [4]):
            self.maintainer().check_certificate()
        self.assertEqual((self.fake / "ipv4").read_text().splitlines()[0], "1")

    def test_the_curl_shim_aborts_a_server_that_never_answers(self):
        import http.server
        class Silent(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(10)
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Silent)   # daemon handler: shutdown is quick
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        with patch.multiple(own_domain, ACME_REQUEST_TIMEOUT=2, ACME_CONNECT_TIMEOUT=2, ACME_RETRY=0):
            shim = own_domain.curl_shim(self.state / "acme")
            started = time.monotonic()
            result = subprocess.run([shim + "/curl", "-s", f"http://127.0.0.1:{server.server_port}/"],
                                    capture_output=True, timeout=15)
            elapsed = time.monotonic() - started
        server.shutdown()
        self.assertEqual(result.returncode, 28, "curl gives 28 when --max-time ends the request")
        self.assertLess(elapsed, 6)

    def test_a_curl_code_becomes_the_reason_and_the_text(self):
        log = self.state / "acme" / "acme.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("... Please refer to ... error code: 28 ...")
        self.assertEqual(own_domain.failure_reason(TimeoutError("took too long"), log), "curl28")
        self.assertIn("curl 28", own_domain.reason_text("curl28"))
        self.assertEqual(own_domain.failure_reason(RuntimeError("x"), log), "curl28", "the log wins when it has one")
        log.unlink()
        self.assertEqual(own_domain.failure_reason(TimeoutError("x"), log), "timeout")

    def test_imported_certificate_is_checked_before_it_replaces_anything(self):
        make_certificate(self.fake, self.name)
        chain = (self.fake / "fullchain.pem").read_text()
        key = (self.fake / "key.pem").read_text()
        own_domain.import_certificate(self.state, self.name, chain, key)
        self.assertTrue(own_domain.certificate_for(self.state, self.name))
        self.assertEqual((self.state / "tls" / "own" / "key.pem").stat().st_mode & 0o777, 0o600)
        self.assertEqual(own_domain.read_status(self.state)["phase"], "ready")
        with self.assertRaises(ValueError):
            own_domain.import_certificate(self.state, "other.duckdns.org", chain, key)
        with self.assertRaises(ValueError):
            own_domain.import_certificate(self.state, self.name, "no certificate", key)
        with self.assertRaises(ValueError):
            own_domain.import_certificate(self.state, "", chain, key)

    def test_invalid_certificate_dates_preserve_the_working_pair_and_status(self):
        make_certificate(self.fake, self.name)
        own_domain.import_certificate(self.state, self.name, (self.fake / "fullchain.pem").read_text(),
                                      (self.fake / "key.pem").read_text())
        paths = [self.state / "tls/own/fullchain.pem", self.state / "tls/own/key.pem",
                 self.state / "own-domain.json"]
        before = [path.read_bytes() for path in paths]
        now = datetime.now(timezone.utc)
        cases = {"expired": (now - timedelta(days=2), now - timedelta(days=1)),
                 "not_started": (now + timedelta(days=1), now + timedelta(days=90)),
                 "expires_today": (now - timedelta(days=1), now + timedelta(hours=12))}
        for label, dates in cases.items():
            with self.subTest(label=label):
                directory = self.fake / label
                directory.mkdir()
                make_certificate(directory, self.name, validity=[date.strftime("%Y%m%d%H%M%SZ") for date in dates])
                with self.assertRaises(ValueError):
                    own_domain.import_certificate(self.state, self.name, (directory / "fullchain.pem").read_text(),
                                                  (directory / "key.pem").read_text())
                self.assertEqual([path.read_bytes() for path in paths], before)
                self.assertIsNotNone(boxnet._own_certificate(self.state))

    def test_stopping_the_service_stops_acme(self):
        make_certificate(self.fake, self.name)
        with patch.dict(os.environ, FAKE_SLEEP="30"):
            worker = self.maintainer(None)
            worker.start()
            time.sleep(1)
            started = time.monotonic()
            self.stopped.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(self.records, [], "no address: the record is left as it is")

    def test_the_record_follows_the_address(self):
        make_certificate(self.fake, self.name)
        worker = self.maintainer("192.168.78.1")
        worker.start()
        for _ in range(50):
            if self.records:
                break
            time.sleep(0.1)
        self.stopped.set(); worker.join(10)
        self.assertEqual(self.records[:1], ["192.168.78.1"])
        self.assertEqual(own_domain.read_status(self.state)["record"], "ok")

    def run_record(self, config, interface="wlan0", until=lambda sent: sent):
        worker = own_domain.Maintainer(self.root, config, "192.168.5.80", self.stopped, say=lambda _: None,
                                       interface=interface)
        with patch.object(own_domain.Maintainer, "check_certificate", lambda _: own_domain.CERT_CHECK):
            worker.start()
            for _ in range(50):
                if until(self.sent):
                    break
                time.sleep(0.1)
            self.stopped.set(); worker.join(10)
        self.stopped.clear()
        return own_domain.read_status(self.state)

    def test_public_ipv6_is_published_only_behind_the_firewall(self):
        v6 = dict(self.config, ipv6=True)
        with patch.object(own_domain, "public_ipv6", lambda _: ("2408:8409:8450:1d91::5", 64)), \
                patch.object(own_domain, "guard", lambda *a: True):
            status = self.run_record(v6)
        self.assertEqual(self.sent[0], ("192.168.5.80", "2408:8409:8450:1d91::5", False))
        self.assertEqual((status["ipv6"], status["ipv6_state"]), ("2408:8409:8450:1d91::5", "ok"))
        self.assertIn("防火墙只让同一个 WiFi", own_domain.message(self.state, self.name))
        self.sent.clear()
        with patch.object(own_domain, "public_ipv6", lambda _: ("2408:8409:8450:1d91::5", 64)), \
                patch.object(own_domain, "guard", lambda *a: False):
            status = self.run_record(v6)
        self.assertEqual(self.sent[0], ("192.168.5.80", None, True), "no firewall: the old AAAA is cleared, none sent")
        self.assertEqual(status["ipv6_state"], "firewall")
        self.assertIn("没有登记公网 IPv6", own_domain.message(self.state, self.name))

    def test_no_public_ipv6_or_option_off(self):
        with patch.object(own_domain, "public_ipv6", lambda _: None):
            status = self.run_record(dict(self.config, ipv6=True))
        self.assertEqual((self.sent[0][1], status["ipv6_state"]), (None, "none"))
        self.assertIn("没有给盒子分配公网 IPv6", own_domain.message(self.state, self.name))
        self.sent.clear()
        with patch.object(own_domain, "public_ipv6", lambda _: self.fail("not looked up when off")):
            status = self.run_record(self.config)
        self.assertEqual(self.sent[0], ("192.168.5.80", None, False))
        self.assertEqual(status["ipv6_state"], "off")

    def test_box_hotspot_is_told_to_renew_at_home(self):
        make_certificate(self.fake, self.name, days=10)
        own = self.state / "tls" / "own"; own.mkdir(parents=True)
        shutil.copy(self.fake / "fullchain.pem", own); shutil.copy(self.fake / "key.pem", own)
        self.assertIn("回家插上网线", own_domain.message(self.state, self.name, box_hotspot=True))
        self.assertNotIn("回家", own_domain.message(self.state, self.name))


class RouteTests(unittest.TestCase):
    def test_ipv4_proc_rejects_unusable_or_nondefault_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = Path(tmp) / "net"
            net.mkdir()
            no_json = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "unrecognized option: j")
            for flags, device, mask in (("0201", "eth0", "00000000"), ("0000", "eth0", "00000000"),
                                        ("0001", "lo", "00000000"), ("0001", "eth0", "000000FF")):
                with self.subTest(flags=flags, device=device, mask=mask):
                    (net / "route").write_text(
                        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
                        f"{device} 00000000 00000000 {flags} 0 0 100 {mask} 0 0 0\n")
                    self.assertEqual(own_domain.default_route_families(no_json, proc=tmp), [])

    def test_ipv6_proc_rejects_unusable_defaults_but_accepts_an_up_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            net = Path(tmp) / "net"
            net.mkdir()
            no_json = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "unrecognized option: j")
            prefix = "0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32 + " ffffffff 00000001 00000000 "
            for flags, device in (("00200200", "lo"), ("00000201", "eth0"), ("00000000", "eth0"),
                                  ("00000001", "lo")):
                with self.subTest(flags=flags, device=device):
                    (net / "ipv6_route").write_text(prefix + flags + " " + device + "\n")
                    self.assertEqual(own_domain.default_route_families(no_json, proc=tmp), [])
            (net / "ipv6_route").write_text(prefix + "00200200 lo\n" + prefix + "00000003 eth0\n")
            self.assertEqual(own_domain.default_route_families(no_json, proc=tmp), [6])

    def test_ip_json_reject_routes_are_not_an_uplink(self):
        for kind in ("unreachable", "blackhole", "prohibit", "throw"):
            with self.subTest(kind=kind):
                out = json.dumps([{"dst": "default", "type": kind, "dev": "eth0"}])
                run = lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
                self.assertEqual(own_domain.default_route_families(run, proc="/nonexistent"), [])

    def test_failed_or_malformed_ip_json_is_not_an_uplink(self):
        for code, routes in ((1, [{"dev": "eth0"}]), (0, {"dev": "eth0"}), (0, [None, {}, "eth0"])):
            with self.subTest(code=code, routes=routes):
                run = lambda *a, **k: subprocess.CompletedProcess(a, code, json.dumps(routes), "")
                self.assertEqual(own_domain.default_route_families(run, proc="/nonexistent"), [])

    def test_real_ipv6_json_default_is_an_uplink(self):
        def run(argv, **kwargs):
            routes = [{"dst": "default", "gateway": "fe80::1", "dev": "eth0"}] if "-6" in argv else []
            return subprocess.CompletedProcess(argv, 0, json.dumps(routes), "")
        self.assertEqual(own_domain.default_route_families(run, proc="/nonexistent"), [6])

    def test_openwrt_busybox_ip_without_json_falls_back_to_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "net").mkdir()
            (Path(tmp) / "net" / "route").write_text(
                "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
                "eth0\t00000000\t0102A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
            no_json = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "unrecognized option: j")
            self.assertEqual(own_domain.default_route_families(no_json, proc=tmp), [4])

    def test_debian_ip_json_route_is_read(self):
        out = json.dumps([{"dst": "default", "gateway": "192.0.2.1", "dev": "eth0", "flags": []}])

        def run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, out if "-4" in argv else "[]", "")
        self.assertEqual(own_domain.default_route_families(run, proc="/nonexistent"), [4])

    def test_no_route_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "net").mkdir()
            (Path(tmp) / "net" / "route").write_text("Iface\tDestination\tGateway\tFlags\n")
            empty = lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", "")
            self.assertEqual(own_domain.default_route_families(empty, proc=tmp), [])

    def test_readiness_reports_route_before_dns(self):
        def no_dns(*a, **k):
            raise OSError("no dns")
        with patch.object(own_domain, "default_route_families", lambda *a, **k: []):
            self.assertEqual(own_domain.network_ready(resolve=no_dns), "route")
        with patch.object(own_domain, "default_route_families", lambda *a, **k: [4]):
            self.assertEqual(own_domain.network_ready(resolve=no_dns), "dns")
            calls = []
            self.assertIsNone(own_domain.network_ready(resolve=lambda host, *a, **k: calls.append(host)))
            self.assertEqual(calls, ["www.duckdns.org", "acme-v02.api.letsencrypt.org"])


class Ipv6Tests(unittest.TestCase):
    def test_finds_the_stable_public_address(self):
        out = json.dumps([{"ifname": "wlan0", "addr_info": [
            {"family": "inet6", "local": "fd9e:30a4:d99::5", "prefixlen": 64},                       # ULA
            {"family": "inet6", "local": "2408:8409:1::99", "prefixlen": 64, "temporary": True},
            {"family": "inet6", "local": "2408:8409:1::7", "prefixlen": 64, "deprecated": True},
            {"family": "inet6", "local": "2408:8409:1::5", "prefixlen": 64}]}])
        run = lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
        self.assertEqual(own_domain.public_ipv6("wlan0", run), ("2408:8409:1::5", 64))
        only_temporary = json.dumps([{"addr_info": [{"family": "inet6", "local": "2408:8409:1::99", "prefixlen": 64,
                                                     "temporary": True}]}])
        self.assertEqual(own_domain.public_ipv6("wlan0", lambda *a, **k: subprocess.CompletedProcess(a, 0, only_temporary, "")),
                         ("2408:8409:1::99", 64))
        self.assertIsNone(own_domain.public_ipv6("wlan0", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[]", "")))

    def test_firewall_lets_in_only_the_wifi(self):
        rules = own_domain.guard_rules("2408:8409:8450:1d91:200:ff:fe00:5", 64)
        self.assertIn("ip6 saddr 2408:8409:8450:1d91::/64 accept", rules)
        self.assertIn("ip6 saddr fe80::/10 accept", rules)
        self.assertIn("meta nfproto ipv4 accept", rules)
        self.assertTrue(rules.rstrip().endswith("}"))
        self.assertIn("counter drop", rules)

    def test_record_query(self):
        urls = []
        class Answer:
            def __init__(self, url): urls.append(url)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b"OK"
        with patch.object(own_domain.urllib.request, "urlopen", lambda url, timeout: Answer(url)):
            self.assertTrue(own_domain.update_record({"domain": "mycar", "token": TOKEN}, "192.168.5.80", ipv6="2408::5"))
            self.assertTrue(own_domain.update_record({"domain": "mycar", "token": TOKEN}, "192.168.5.80", clear=True))
        self.assertIn("ip=192.168.5.80", urls[0]); self.assertIn("ipv6=2408%3A%3A5", urls[0])
        self.assertIn("clear=true", urls[1]); self.assertNotIn("ipv6", urls[2])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SettingsPageTests(unittest.TestCase):
    def test_supplied_domain_keeps_retry_and_import_without_exposing_or_overwriting_config(self):
        config = {'provider': 'desec', 'domain': 'box01.car.example.com', 'token': 'synthetic-supplied-token-00000'}
        path = self.state / 'ddns.json'; path.write_text(json.dumps(config))
        before = path.read_bytes()
        called = []
        self.page.retry_cert = lambda: called.append(True)
        html = self.page.ddns_block(self.nonce, 'box_hotspot')
        self.assertIn(config['domain'], html)
        self.assertIn('重新申请证书', html)
        self.assertIn('手动导入证书', html)
        self.assertNotIn(config['token'], html)
        self.assertNotIn('name=domain', html)
        self.page.apply('/cert', {'action': 'retry', 'nonce': self.nonce})
        self.assertEqual(called, [True])
        with self.assertRaises(ValueError):
            self.save(action='clear')
        self.assertEqual(path.read_bytes(), before)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / "state"; self.state.mkdir()
        self.certs = Path(tmp.name) / "certs"; self.certs.mkdir()
        (self.state / "hotspot.json").write_text('{"ssid": "DashPlay", "passphrase": "installed1"}')
        setup_wizard.save(self.state, {"done": True, "mode": "box_hotspot"})
        self.status = {"mode": "ap", "box_ssid": "DashPlay", "entries": [], "home": "http://192.168.78.1/"}
        self.changes = []
        self.page = SettingsPage(self.state, lambda: self.status, self.changes.append, lambda: [])
        self.page.apply_later = self.changes.append
        self.nonce = self.page.pairing.nonce

    def save(self, **form):
        return self.page.apply("/ddns", dict(form, nonce=self.nonce))

    def test_the_form_follows_the_provider_and_a_switch_needs_that_providers_token(self):
        html = self.page.render("")
        self.assertIn("id=ddns-name>子域名<", html)
        self.assertIn("id=ddns-id style=display:none", html)
        self.assertIn("token.value=\"\"", html)
        pod = "syntheticdnspodtoken1"
        self.save(action="save", provider="dnspod", domain="dashplay.apeng.fun",
                  dnspod_id="10001", token=pod)
        html = self.page.render("")
        self.assertIn("id=ddns-name>域名<", html)
        self.assertNotIn("id=ddns-id style=display:none", html)
        self.assertNotIn(pod, html)
        with self.assertRaises(ValueError):
            self.save(action="save", provider="duckdns", domain="mycar", token="")
        self.assertEqual(own_domain.load(self.state)["provider"], "dnspod")
        duck = "a1b2c3d4e5f6g7h8"
        self.save(action="save", provider="duckdns", domain="mycar", token=duck)
        self.assertEqual(own_domain.load(self.state), {"domain": "mycar", "token": duck})
        with self.assertRaises(ValueError):
            self.save(action="save", provider="dnspod", domain="dashplay.apeng.fun", token="")

    def test_save_keep_token_change_name_and_turn_off(self):
        self.assertIn("未设置", self.page.render(""))
        self.assertIn("mycar.duckdns.org", self.save(action="save", domain="MyCar", token=TOKEN))
        ddns = self.state / "ddns.json"
        self.assertEqual(ddns.stat().st_mode & 0o777, 0o600)
        self.assertEqual(own_domain.load(self.state), {"domain": "mycar", "token": TOKEN})
        self.assertEqual(self.changes, ["own domain saved"])
        self.assertNotIn(TOKEN, self.page.render(""), "the token is never sent back")
        own = self.state / "tls" / "own"; own.mkdir(parents=True); (own / "fullchain.pem").write_text("x")
        self.save(action="save", domain="mycar", token="")   # empty: keep it, and the certificate
        self.assertEqual(own_domain.load(self.state)["token"], TOKEN)
        self.assertTrue(own.exists())
        with self.assertRaises(ValueError):
            self.save(action="save", domain="other", token="")   # a new name needs its token
        self.save(action="save", domain="other", token=TOKEN)
        self.assertFalse(own.exists(), "the old name's certificate goes")
        self.save(action="clear")
        self.assertFalse(ddns.exists())
        with self.assertRaises(ValueError):
            self.page.apply("/ddns", {"action": "clear", "nonce": "stale"})

    def test_public_ipv6_only_where_the_box_joins_a_hotspot(self):
        self.assertNotIn("name=ipv6", self.page.render(""), "box hotspot: no public address")
        self.save(action="save", domain="mycar", token=TOKEN, ipv6="on")
        self.assertNotIn("ipv6", own_domain.load(self.state))
        setup_wizard.save(self.state, {"done": True, "mode": "mifi"})
        (self.state / "car-hotspot.json").write_text('{"ssid": "MiFi", "passphrase": "12345678"}')
        self.assertIn("name=ipv6", self.page.render(""))
        self.save(action="save", domain="mycar", token="", ipv6="on")
        self.assertEqual(own_domain.load(self.state), {"domain": "mycar", "token": TOKEN, "ipv6": True})
        self.assertIn("checked", self.page.render("").split("name=ipv6")[1][:40])
        self.save(action="save", domain="mycar", token="")
        self.assertNotIn("ipv6", own_domain.load(self.state), "unticked: off")

    def test_retry_button_and_manual_import(self):
        self.save(action="save", domain="mycar", token=TOKEN)
        called = []
        self.page.retry_cert = lambda: called.append(True)
        self.assertIn("重新申请证书", self.page.render(""))
        self.assertIn("name=chain", self.page.render(""))
        self.assertIn("手动导入证书", self.page.render(""))
        self.assertIn("已请求", self.page.apply("/cert", {"action": "retry", "nonce": self.nonce}))
        self.assertEqual(called, [True])
        make_certificate(self.certs, "mycar.duckdns.org")
        chain = (self.certs / "fullchain.pem").read_text()
        key = (self.certs / "key.pem").read_text()
        self.assertIn("已导入", self.page.apply("/cert", {"action": "import", "nonce": self.nonce,
                                                          "chain": chain, "key": key}))
        self.assertEqual(self.changes[-1], "certificate imported")
        self.assertTrue(own_domain.certificate_for(self.state, "mycar.duckdns.org"))
        with self.assertRaises(ValueError):
            self.page.apply("/cert", {"action": "import", "nonce": self.nonce, "chain": "x", "key": "y"})
        with self.assertRaises(ValueError):
            self.page.apply("/cert", {"action": "retry", "nonce": "stale"})

    def test_http_forms_carry_retry_and_a_pasted_certificate(self):
        self.save(action="save", domain="mycar", token=TOKEN)
        called = []
        self.page.retry_cert = lambda: called.append(True)
        real = settings_page.ThreadingHTTPServer

        def loopback(address, handler):   # a test server on 127.0.0.1 only: no firewall prompt
            return real(("127.0.0.1", address[1]), handler)
        with patch.object(settings_page, "ThreadingHTTPServer", loopback):
            self.page.start(port=0)
        self.addCleanup(self.page.stop)
        port = self.page.server.server_address[1]
        opener = urllib.request.build_opener(_NoRedirect)
        messages = []

        def post(**fields):
            request = urllib.request.Request(f"http://127.0.0.1:{port}/cert",
                                             data=urllib.parse.urlencode(fields).encode(), method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                opener.open(request, timeout=10)
            location = caught.exception.headers.get("Location", "")
            messages.append(urllib.parse.parse_qs(urllib.parse.urlsplit(location).query).get("m", [""])[0])
            return caught.exception.code

        self.assertEqual(post(action="retry", nonce=self.nonce), 303)
        self.assertEqual(called, [True])
        make_certificate(self.certs, "mycar.duckdns.org")
        self.assertEqual(post(action="import", nonce=self.nonce,
                              chain=(self.certs / "fullchain.pem").read_text(),
                              key=(self.certs / "key.pem").read_text()), 303)
        self.assertTrue(own_domain.certificate_for(self.state, "mycar.duckdns.org"))
        self.assertIn("certificate imported", self.changes)
        paths = [self.state / "tls/own/fullchain.pem", self.state / "tls/own/key.pem",
                 self.state / "own-domain.json"]
        before = [path.read_bytes() for path in paths]
        changes = list(self.changes)
        make_certificate(self.certs, "mycar.duckdns.org", days=0)
        self.assertEqual(post(action="import", nonce=self.nonce,
                              chain=(self.certs / "fullchain.pem").read_text(),
                              key=(self.certs / "key.pem").read_text()), 303)
        self.assertTrue(messages[-1].startswith("错误："))
        self.assertIn("已过期", messages[-1])
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(self.changes, changes, "a rejected import must not restart the service")

    def test_reset_keeps_the_name(self):
        self.save(action="save", domain="mycar", token=TOKEN)
        (self.state / "receiver").mkdir()
        self.page.apply("/reset", {"nonce": self.nonce})
        self.assertIsNotNone(own_domain.load(self.state))

    def test_openwrt_only_shows_it(self):
        page = SettingsPage(self.state, lambda: dict(self.status, own_name="mycar.duckdns.org"), self.changes.append,
                            lambda: [], managed={"by": "OpenWrt", "admin": "/cgi-bin/luci"})
        with self.assertRaises(ValueError):
            page.apply("/ddns", {"action": "save", "domain": "x", "token": TOKEN, "nonce": page.pairing.nonce})
        html = page.render("")
        self.assertIn("mycar.duckdns.org", html)
        self.assertNotIn("name=token", html)


if __name__ == "__main__":
    unittest.main()
