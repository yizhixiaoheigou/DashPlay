"""Real local HTTP requests exercising validation before settings actions and redirects."""
import http.client
import ipaddress
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

import settings_page


class SettingsHttpGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        state = Path(temporary.name) / 'state'; state.mkdir()
        self.page = settings_page.SettingsPage(state, lambda: {'home': 'http://192.0.2.1/'},
                                               lambda _: None, lambda: [ipaddress.ip_network('192.0.2.0/24')])
        self.page.apply = Mock(return_value=('versions', {'port': 18081, 'token': 'synthetic-access-token'}))
        self.page.start(0)
        self.addCleanup(self.page.stop)
        self.port = self.page.server.server_port

    def request(self, headers, body=b'action=versions', half_close=False):
        with socket.create_connection(('127.0.0.1', self.port), timeout=2) as client:
            client.sendall(b'POST /update HTTP/1.0\r\n' +
                           ''.join(f'{key}: {value}\r\n' for key, value in headers).encode() + b'\r\n' + body)
            if half_close:
                client.shutdown(socket.SHUT_WR)
            with http.client.HTTPResponse(client) as response:
                response.begin()
                return response.status, dict(response.getheaders()), response.read()

    def test_bad_lengths_are_rejected_without_running_actions(self):
        for headers, expected in (([('Content-Length', '-1')], 400),
                                  ([('Content-Length', 'no-number')], 400),
                                  ([('Content-Length', str(settings_page.MAX_BODY + 1))], 413),
                                  ([('Content-Length', '15'), ('Content-Length', '15')], 400),
                                  ([('Transfer-Encoding', 'chunked')], 400)):
            with self.subTest(headers=headers):
                status, _, _ = self.request([('Host', 'dashplay.local'), *headers], half_close=True)
                self.assertEqual(status, expected)
        self.page.apply.assert_not_called()

    def test_incomplete_or_invalid_body_never_runs_an_action(self):
        for length, body in ((50, b'action=versions'), (1, b'\xff')):
            with self.subTest(body=body):
                status, _, _ = self.request([('Host', 'dashplay.local'), ('Content-Length', str(length))],
                                             body=body, half_close=True)
                self.assertEqual(status, 400)
        self.page.apply.assert_not_called()

    def test_idle_request_body_times_out(self):
        # Shorten only the test's wait; the production handler must have a finite default too.
        handler = self.page.server.RequestHandlerClass
        self.assertGreater(handler.timeout, 0)
        self.assertLessEqual(handler.timeout, 30)
        with patch.object(handler, 'timeout', 0.05):
            status, _, _ = self.request([('Host', 'dashplay.local'), ('Content-Length', '50')])
        self.assertEqual(status, 408)
        self.page.apply.assert_not_called()

    def test_foreign_and_malformed_hosts_cannot_receive_version_token(self):
        hosts = ('evil.example', '192.0.2.99', '127.0.0.2', 'dashplay.local.evil.example',
                 'dashplay.local@127.0.0.1', 'dashplay.local/evil', '[broken',
                 'dashplay.local:99999', 'dashplay.local#fragment', 'dashplay.local\\evil')
        for host in hosts:
            with self.subTest(host=host):
                status, headers, body = self.request([('Host', host), ('Content-Length', '15')])
                self.assertEqual(status, 400)
                self.assertNotIn('Location', headers)
                self.assertNotIn(b'synthetic-access-token', body)
        self.page.apply.assert_not_called()

    def test_duplicate_host_is_rejected(self):
        status, headers, _ = self.request([('Host', 'dashplay.local'), ('Host', 'evil.example'),
                                          ('Content-Length', '15')])
        self.assertEqual(status, 400)
        self.assertNotIn('Location', headers)
        self.page.apply.assert_not_called()

    def test_box_addresses_and_configured_names_still_redirect(self):
        with patch.object(settings_page.own_domain, 'load', return_value={'provider': 'desec', 'domain': 'box.example.com'}):
            for host, target in (('127.0.0.1', '127.0.0.1'), ('192.0.2.1:1234', '192.0.2.1'),
                                 ('DASHPLAY.LOCAL.', 'dashplay.local'), ('box.example.com', 'box.example.com')):
                with self.subTest(host=host):
                    status, headers, _ = self.request([('Host', host), ('Content-Length', '15')])
                    self.assertEqual(status, 303)
                    self.assertEqual(headers['Location'], f'http://{target}:18081/#synthetic-access-token')

    def test_ipv6_hosts_require_exact_local_address(self):
        self.assertEqual(self.page.version_host('[2001:db8::1]:80', '2001:db8::1'), '2001:db8::1')
        self.assertIsNone(self.page.version_host('[2001:db8::2]', '2001:db8::1'))
