import ipaddress
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
import connectivity as net

NAMES = (*net.CHECKS, 'www.baidu.com', 'www.qq.com')


def sample(online=True, checks=None, link=True, route='usb0'):
    return dict(link=link, internet=online, checks=dict.fromkeys(net.CHECKS, online) if checks is None else checks,
                route=route if link else None, elapsed_ms=1)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.files = net.DnsFiles(Path(temp.name) / 'dns', '100.96.0.1', NAMES)
        self.files.prepare()
        self.probe = Mock(return_value=sample())
        self.reload = Mock()
        self.monitor = net.Monitor(self.probe, self.files, self.reload)

    def online(self):
        for _ in range(3): self.monitor.step()
        self.assertEqual(self.files.applied, ())

    def test_boot_protects_then_recovers_only_after_three_successes(self):
        for _ in range(2):
            self.monitor.step(); self.assertEqual(self.files.applied, NAMES)
        self.monitor.step()
        self.assertEqual(self.files.applied, ())
        self.reload.assert_called_once()
        self.assertEqual(self.files.hosts.read_text(), '')
        self.files.prepare()  # service restart must not inherit a permanent online latch
        self.assertEqual(self.files.applied, NAMES)

    def test_mobile_data_loss_with_usb_link_still_up_recovers_and_repeats(self):
        for _ in range(2):
            self.probe.return_value = sample(); self.online()
            self.probe.return_value = sample(False)
            self.assertTrue(self.monitor.step(), 'first failure must request an immediate recheck')
            self.assertEqual(self.files.applied, ())
            self.assertFalse(self.monitor.step())
            self.assertEqual(self.files.applied, NAMES)
            self.assertEqual(self.monitor.status['mode'], 'protect')

    def test_short_failure_does_not_toggle_dns(self):
        self.online(); self.reload.reset_mock()
        self.probe.return_value = sample(False); self.monitor.step()
        self.probe.return_value = sample(); self.monitor.step()
        self.reload.assert_not_called()

    def test_hard_link_loss_protects_immediately(self):
        self.online()
        self.probe.return_value = sample(False, link=False)
        self.monitor.step(); self.assertEqual(self.files.applied, NAMES)

    def test_new_uplink_must_be_verified_again(self):
        self.online(); self.probe.return_value = sample(route='wlan0')
        self.monitor.step(); self.assertEqual(self.files.applied, NAMES)
        self.monitor.step(); self.monitor.step(); self.assertEqual(self.files.applied, ())

    def test_domestic_fallback_releases_websites_but_not_failed_tesla_checks(self):
        self.probe.return_value = sample(checks={})
        for _ in range(3): self.monitor.step()
        self.assertEqual(self.files.applied, tuple(net.CHECKS))
        self.assertEqual(self.monitor.status['mode'], 'partial')
        first = next(iter(net.CHECKS))
        self.probe.return_value = sample(checks={first: True})
        for _ in range(3): self.monitor.step()
        self.assertEqual(self.files.applied, tuple(name for name in net.CHECKS if name != first))

    def test_event_during_probe_discards_obsolete_result(self):
        self.online()
        def old_result():
            self.monitor.changed()
            return sample(False, link=False)
        self.probe.side_effect = old_result
        self.assertTrue(self.monitor.step())
        self.assertEqual(self.files.applied, ())

    def test_reload_error_is_visible_and_retried_without_restart(self):
        self.reload.side_effect = subprocess.CalledProcessError(1, ['ubus'])
        for _ in range(3): self.monitor.step()
        self.assertEqual(self.files.applied, NAMES)
        self.assertEqual(self.monitor.status['error'], 'CalledProcessError')
        self.reload.side_effect = None; self.monitor.step()
        self.assertEqual(self.files.applied, ())
        self.assertIsNone(self.monitor.status['error'])

    def test_close_prevents_late_reload(self):
        self.monitor.step(); self.monitor.step()
        self.probe.side_effect = lambda: (self.monitor.close(), sample())[1]
        self.monitor.step(); self.reload.assert_not_called()

    def test_failed_release_followed_by_outage_rewrites_protection(self):
        # A signal may arrive even when ubus times out. Never assume the old
        # process state, or leave empty files for its next restart.
        self.reload.side_effect = subprocess.TimeoutExpired(['ubus'], 1)
        for _ in range(3): self.monitor.step()
        self.assertEqual(self.files.hosts.read_text(), '')
        self.reload.side_effect = None
        self.probe.return_value = sample(False, link=False)
        self.monitor.step()
        self.assertEqual(self.files.hosts.read_text().count('100.96.0.1'), len(NAMES))
        self.assertEqual(self.reload.call_count, 2)

    def test_close_waits_for_a_dns_write_already_in_progress(self):
        self.monitor.step(); self.monitor.step()
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        original = self.files.apply
        def delayed(*args):
            entered.set(); release.wait(5)
            return original(*args)
        def close():
            self.monitor.close(); closed.set()
        with patch.object(self.files, 'apply', side_effect=delayed), patch.object(net, 'BUDGET', .01):
            worker = threading.Thread(target=self.monitor.step)
            self.monitor.thread = worker
            closer = threading.Thread(target=close)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                closer.start()
                self.assertFalse(closed.wait(1.5), 'close must drain writes even after the probe join timeout')
            finally:
                release.set(); worker.join(2)
                if closer.ident: closer.join(2)
        self.assertTrue(closed.is_set())
        calls = self.reload.call_count
        self.monitor.step()
        self.assertEqual(self.reload.call_count, calls)


class ProbeTests(unittest.TestCase):
    def test_forwarding_loss_protects_without_spending_probe_traffic(self):
        probe = net.Probe(None, 'ap0', ready=lambda: False)
        policy = net.Policy(NAMES)
        for _ in range(3): policy.update(sample())
        with patch.object(probe, 'route', return_value=('usb0', [], 'usb0')), \
                patch.object(probe, 'endpoint') as endpoint:
            result = probe()
        self.assertTrue(result['link']); self.assertFalse(result['forwarding_ready'])
        self.assertFalse(result['internet'])
        self.assertEqual(policy.update(result)[0], NAMES)
        endpoint.assert_not_called()

    def test_route_cache_expiry_is_not_a_new_uplink(self):
        run = Mock()
        probe = net.Probe(None, 'ap0', run, ['192.0.2.1'])
        run.return_value.stdout = '1.1.1.1 via 192.0.2.1 dev usb0 src 192.0.2.2\n cache expires 590sec mtu 1400'
        first = probe.route()[2]
        run.return_value.stdout = '1.1.1.1 via 192.0.2.1 dev usb0 src 192.0.2.2 uid 0\n cache expires 588sec mtu 1400'
        self.assertEqual(probe.route()[2], first)
        run.return_value.stdout = '1.1.1.1 via 192.0.2.9 dev usb0 src 192.0.2.2 uid 0\n cache'
        self.assertNotEqual(probe.route()[2], first)

    def test_endpoint_uses_direct_dns_and_bound_http_with_host_header(self):
        host = next(iter(net.CHECKS))
        question = b''.join(bytes([len(label)]) + label.encode() for label in host.split('.')) + b'\0\0\1\0\1'
        answer = (struct.pack('!6H', 42, 0x8180, 1, 1, 0, 0) + question + b'\xc0\x0c' +
                  struct.pack('!HHIH', 1, 1, 60, 4) + socket.inet_aton('8.8.4.4'))
        dns, http = Mock(), Mock()
        for sock in (dns, http):
            sock.__enter__ = Mock(return_value=sock); sock.__exit__ = Mock(return_value=False)
        dns.recv.return_value = answer
        http.recv.return_value = b'HTTP/1.1 200 OK\r\nX-ConnMan-Status: online\r\n\r\n'
        probe = net.Probe(None, 'ap0')
        with patch.object(probe, 'bound_socket', side_effect=[dns, http]) as bound, \
                patch.object(net.secrets, 'randbelow', return_value=42):
            self.assertTrue(probe.endpoint(host, *net.CHECKS[host], 'usb0', ['192.0.2.1'], time.monotonic() + 2))
        dns.connect.assert_called_once_with(('192.0.2.1', 53))
        http.connect.assert_called_once_with(('8.8.4.4', 80))
        self.assertIn(f'Host: {host}\r\n'.encode(), http.sendall.call_args.args[0])
        self.assertEqual([call.args[1] for call in bound.call_args_list], ['usb0', 'usb0'])

    def test_only_expected_http_response_counts(self):
        self.assertTrue(net.http_success(b'HTTP/1.1 200 OK\r\nX-ConnMan-Status: online\r\n\r\n', 'connman'))
        self.assertTrue(net.http_success(b'HTTP/1.1 204 No Content\r\n\r\n', '204'))
        self.assertTrue(net.http_success(b'HTTP/1.0 200 OK\r\n\r\n<TITLE>Success</TITLE><BODY>Success</BODY>', 'apple'))
        for data in (b'HTTP/1.1 302 Found\r\nLocation: /login\r\n\r\n',
                     b'HTTP/1.1 200 OK\r\n\r\nlogin', b'not HTTP', b'HTTP/1.1 503 Down\r\n\r\n'):
            for kind in ('connman', '204', 'apple'):
                self.assertFalse(net.http_success(data, kind))

    def test_dns_rejects_local_spoof_mismatch_truncation_and_loops(self):
        host = 'check.example'
        question = b'\x05check\x07example\0\0\1\0\1'
        def answer(ip, flags=0x8180, ident=42):
            return (struct.pack('!6H', ident, flags, 1, 1, 0, 0) + question + b'\xc0\x0c' +
                    struct.pack('!HHIH', 1, 1, 0, 4) + socket.inet_aton(ip))
        self.assertEqual(net.dns_answer(answer('8.8.4.4'), 42, host), '8.8.4.4')
        for packet in (answer('100.96.0.1'), answer('192.168.1.1'), answer('127.0.0.1'),
                       answer('8.8.4.4', 0x8380), answer('8.8.4.4', ident=43), answer('8.8.4.4')[:-1]):
            with self.assertRaises(ValueError): net.dns_answer(packet, 42, host)
        with self.assertRaises(ValueError): net.dns_name(b'\xc0\0', 0)

    def test_route_and_resolvers_do_not_use_local_dns_or_lan(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'resolv'
            path.write_text('nameserver ::1\nnameserver 127.0.0.1\nnameserver 192.0.2.1\n')
            run = Mock(return_value=Mock(stdout='1.1.1.1 via 192.0.2.1 dev usb0 src 192.0.2.2 uid 0\n cache'))
            probe = net.Probe(path, 'ap0', run)
            interface, servers, _ = probe.route()
            self.assertEqual((interface, servers), ('usb0', ['192.0.2.1']))
            for interface in ('ap0', 'lo', 'tailscale0'):
                run.return_value.stdout = f'1.1.1.1 dev {interface}'
                self.assertIsNone(probe.route())
            run.return_value.stdout = 'unreachable 1.1.1.1'
            self.assertFalse(probe()['link'])

    def test_parallel_round_has_one_deadline_and_does_not_overlap(self):
        probe = net.Probe(None, 'ap0')
        deadlines = []; active = []; done = []
        def endpoint(host, path, kind, interface, servers, deadline):
            deadlines.append(deadline); active.append(host)
            time.sleep(max(0, deadline - time.monotonic()))
            done.append(host)
            return False
        with patch.object(probe, 'route', return_value=('usb0', ['192.0.2.1'], 'usb0')), \
                patch.object(probe, 'endpoint', side_effect=endpoint), patch.object(net, 'BUDGET', .12), \
                patch.object(net, 'FALLBACK_AFTER', .02):
            began = time.monotonic(); result = probe()
        self.assertFalse(result['internet']); self.assertTrue(result['link'])
        self.assertEqual(len(active), len(net.PROBES)); self.assertCountEqual(done, active)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertLess(time.monotonic() - began, .4)

    def test_probe_success_is_not_a_claim_all_checks_work(self):
        probe = net.Probe(None, 'ap0')
        with patch.object(probe, 'route', return_value=('usb0', [], 'usb0')), \
                patch.object(probe, 'endpoint', side_effect=lambda host, *args: host == 'connectivitycheck.platform.hicloud.com'):
            result = probe()
        self.assertTrue(result['internet']); self.assertFalse(any(result['checks'].values()))

    def test_normal_connection_uses_one_domestic_probe_per_round(self):
        self.assertEqual(set(net.PROBES), {'connman.vn.cloud.tesla.cn', 'connectivitycheck.platform.hicloud.com'})
        probe = net.Probe(None, 'ap0')
        with patch.object(probe, 'route', return_value=('usb0', [], 'usb0')), \
                patch.object(probe, 'endpoint', return_value=True) as endpoint:
            for _ in range(3):
                result = probe()
                self.assertTrue(result['checks']['connman.vn.cloud.tesla.cn'])
                self.assertFalse(result['checks']['connman.vn.tesla.services'])
                self.assertFalse(result['checks']['connectivitycheck.gstatic.com'])
        self.assertEqual(endpoint.call_count, 3)
        self.assertTrue(all(call.args[0] == 'connman.vn.cloud.tesla.cn' for call in endpoint.call_args_list))

    def test_login_page_stops_after_headers_without_downloading_the_body(self):
        probe = net.Probe(None, 'ap0')
        dns, http = Mock(), Mock()
        for sock in (dns, http):
            sock.__enter__ = Mock(return_value=sock); sock.__exit__ = Mock(return_value=False)
        http.recv.side_effect = [b'HTTP/1.1 302 Found\r\nLocation: /login\r\n\r\n', AssertionError('must not read body')]
        with patch.object(probe, 'bound_socket', side_effect=[dns, http]), \
                patch.object(net, 'dns_answer', return_value='8.8.4.4'):
            self.assertFalse(probe.endpoint('check.example', '/', 'connman', 'usb0', ['192.0.2.1'], time.monotonic() + 2))
        http.recv.assert_called_once()


if __name__ == '__main__': unittest.main()
