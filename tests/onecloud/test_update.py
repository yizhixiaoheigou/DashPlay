"""Online update: signed manifest, downloads from GitHub or a mirror, guards, apk plan, rollback (update.py)."""
import functools
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/onecloud'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import update

# OpenSSL 3 signs and checks Ed25519 with -rawin; a Mac's own openssl is LibreSSL.
OPENSSL = next((p for p in (os.environ.get('DASHPLAY_OPENSSL'), '/opt/homebrew/opt/openssl@3/bin/openssl',
                            '/usr/local/opt/openssl@3/bin/openssl', shutil.which('openssl'))
                if p and Path(p).exists() and 'OpenSSL 3' in subprocess.run([p, 'version'], capture_output=True,
                                                                              text=True).stdout), None)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Server:
    """GitHub stand-in: /gh/<path> serves files (or fails when listed in `down`); /<name>/<full url> is a mirror of
    the same, behaving as `kinds` says: 'slow' trickles about 20 KB/s, 'cut' breaks off after half of what it
    announced, 'page' answers 200 with a block page. Range requests are answered with 206 except by 'norange'."""

    def __init__(self, root):
        self.root, self.down, self.seen, self.ranges, self.kinds = Path(root), set(), [], [], {}
        self.overrides = {}  # (source, relative path) -> bytes, including coherent malicious mirror responses
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                try:
                    self.answer()
                except (BrokenPipeError, ConnectionResetError):   # the client stopped reading: a measurement, a stall
                    pass

            def answer(self):
                server.seen.append(self.path)
                server.ranges.append((self.path, self.headers.get('Range')))
                path, kind = self.path, 'gh'
                if not path.startswith('/gh/') and '/gh/' in path:
                    kind = path.split('/')[1]
                    path = '/gh/' + path.split('/gh/', 1)[1]
                elif path.removeprefix('/gh/') in server.down:
                    return self.send_error(503)
                kind = server.kinds.get(kind, kind)
                target = server.root / path.removeprefix('/gh/')
                if not path.startswith('/gh/') or not target.is_file():
                    return self.send_error(404)
                if kind == 'page':
                    page = b'<html>blocked</html>'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(page)))
                    self.end_headers()
                    return self.wfile.write(page)
                data, start = server.overrides.get((kind, path.removeprefix('/gh/')), target.read_bytes()), 0
                wanted = self.headers.get('Range')
                if wanted and kind != 'norange':
                    first, _, last = wanted.removeprefix('bytes=').partition('-')
                    start, end = int(first), min(int(last) if last else len(data) - 1, len(data) - 1)
                    self.send_response(206)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{len(data)}')
                    data = data[start:end + 1]
                else:
                    self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if kind == 'cut':
                    self.wfile.write(data[:len(data) // 2])
                    self.close_connection = True
                    return
                step = 512 if kind == 'slow' else 1 << 16
                for i in range(0, len(data), step):
                    self.wfile.write(data[i:i + step])
                    if kind == 'slow':
                        self.wfile.flush()
                        time.sleep(0.025)

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}/gh/'
        self.mirror = self.at('mirror')

    def at(self, name):
        """A mirror prefix on this server (its kind: the name, or kinds[name])."""
        return f'http://127.0.0.1:{self.httpd.server_port}/{name}/'

    def put(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ClockTests(unittest.TestCase):
    """The update checker sets a clock months off (no battery clock, no NTP) from a plain-HTTP Date header first."""

    class Reply:
        def __init__(self, date):
            self.headers = {"Date": date}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(self, dates):
        def open_(request, timeout):
            date = dates.pop(0)
            if isinstance(date, Exception):
                raise date
            return self.Reply(date)
        return open_

    def test_a_clock_months_behind_is_set_from_the_date_header(self):
        set_to = []
        offset = update.clock_from_http(urls=("http://a", "http://b"), now=lambda: 1_700_000_000.0,
                                        set_clock=set_to.append,
                                        opener=self.opener(["Tue, 21 Oct 2026 12:00:00 GMT"]))
        self.assertEqual(set_to, [1792584000.0])
        self.assertAlmostEqual(offset, 92_584_000.0)

    def test_a_clock_within_ten_minutes_is_left_alone(self):
        set_to = []
        self.assertEqual(update.clock_from_http(urls=("http://a",), now=lambda: 1792584000.0 - 300,
                                                set_clock=set_to.append,
                                                opener=self.opener(["Tue, 21 Oct 2026 12:00:00 GMT"])), 0)
        self.assertEqual(set_to, [])

    def test_an_unreachable_or_dateless_server_falls_through_to_the_next(self):
        set_to = []
        offset = update.clock_from_http(urls=("http://a", "http://b", "http://c"), now=lambda: 0.0,
                                        set_clock=set_to.append,
                                        opener=self.opener([OSError("offline"), "not a date",
                                                            "Tue, 21 Oct 2026 12:00:00 GMT"]))
        self.assertEqual(set_to, [1792584000.0])
        self.assertTrue(offset > 0)
        self.assertIsNone(update.clock_from_http(urls=("http://a",), set_clock=set_to.append,
                                                 opener=self.opener([OSError("offline")])))


class VersionTests(unittest.TestCase):
    def test_known_debian_version_does_not_offer_same_or_older_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'version.txt').write_text('2026.10.26\n')
            (root / 'own_domain.py').write_text('local provider support')
            for latest, available in [('2026.10.26', False), ('2026.10.25', False), ('2026.10.26.1', True)]:
                manifest = {'version': latest, 'online': True,
                            'debian': {'files': {'own_domain.py': sha(b'public implementation')}}}
                result = update.compare(root, False, manifest)
                self.assertEqual(result['available'], available)
                self.assertTrue(result['modified'])

    def test_dates_compare_as_numbers(self):
        self.assertTrue(update.newer('2026.10.19.1', '2026.10.19'))
        self.assertTrue(update.newer('2026.10.20', '2026.10.19.1'))
        self.assertTrue(update.newer('2026.11.2', '2026.10.30'))
        self.assertFalse(update.newer('2026.10.19', '2026.10.19'))
        self.assertTrue(update.newer('2026.10.19', None), 'an unknown installed version takes the release')
        self.assertFalse(update.newer('latest', '2026.10.19'), 'a malformed release is never newer')

    def test_the_installed_version_comes_from_the_package_list(self):
        # `apk info -v` on OpenWrt 25.12; `apk info -v dashplay` prints the description instead (lab, 2026-10-19).
        listing = 'busybox-1.37.0-r6\ndashplay-2026.10.19.1-r1\ndashplay-jre-21.0.12.1-r1\nluci-app-dashplay-0.261019.40345\n'
        self.assertEqual(update.apk_version('dashplay', listing), '2026.10.19.1-r1')
        self.assertEqual(update.apk_version('dashplay-jre', listing), '21.0.12.1-r1')
        self.assertIsNone(update.apk_version('dashplay', 'dashplay: Receives CarPlay from an iPhone\n'))

    def test_a_box_names_its_release_from_the_bundle(self):
        # 2026-10-20: a user's N1 still ran the image's 2026.10.15 program; nothing on the box said so.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(update.bundled_version(root), 'a development build or an older release')
            update.save(root / 'state/update.json', {'version': '2026.10.19.2'})
            self.assertEqual(update.current_version(root, False), '2026.10.19.2')
            (root / 'version.txt').write_text('2026.10.20\n')
            self.assertEqual(update.bundled_version(root), '2026.10.20')
            self.assertEqual(update.current_version(root, False), '2026.10.20', 'the files on the box win')

    def test_the_apk_plan_may_not_touch_the_kernel_kmods_or_bluez(self):
        # Shape of `apk add --upgrade --simulate` on the F50 (2026-10-19).
        plan = ('(1/4) Upgrading dashplay (2026.10.16-r1 -> 2026.10.19.1-r1)\n'
                '(2/4) Upgrading luci-base (26.280.64158~a450f0d -> 26.288.56989~75a306d)\n'
                '(3/4) Upgrading luci-app-dashplay (0.261016.36841 -> 0.261019.40345)\n'
                '(4/4) Upgrading luci-i18n-dashplay-zh-cn (0.261016.36841 -> 0.261019.40345)\nOK: 267.2 MiB\n')
        self.assertEqual(update.forbidden_in_plan(plan), [])
        plan += ('(5/7) Installing kmod-bluetooth (6.12.94-r1)\n(6/7) Installing bluez-daemon (5.83-r1)\n'
                 '(7/7) Upgrading kernel (6.12.94~abc-r1)\n')
        self.assertEqual(update.forbidden_in_plan(plan), ['bluez-daemon', 'kernel', 'kmod-bluetooth'])


BUILT_IN_MIRRORS = update.MIRRORS   # setUp patches update.MIRRORS for the tests below


@unittest.skipUnless(OPENSSL, 'needs OpenSSL 3')
class SignedReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.key, self.pub = self.dir / 'key.pem', self.dir / 'pub.pem'
        subprocess.run([OPENSSL, 'genpkey', '-algorithm', 'ed25519', '-out', str(self.key)], check=True, capture_output=True)
        subprocess.run([OPENSSL, 'pkey', '-in', str(self.key), '-pubout', '-out', str(self.pub)], check=True, capture_output=True)
        self.server = Server(self.dir / 'www')
        self.addCleanup(self.server.close)
        self.root = self.dir / 'opt/dashplay'
        (self.root / 'state/receiver').mkdir(parents=True)
        (self.root / 'identity/offline-mfi').mkdir(parents=True)
        (self.root / 'identity/offline-mfi/identity.pk8').write_bytes(b'secret identity')
        (self.root / 'state/logs/boot1').mkdir(parents=True)
        (self.root / 'state/logs/boot1/receiver.log').write_text('log')
        (self.root / 'state/bluetooth').mkdir()
        (self.root / 'state/bluetooth/pairing').write_text('link key')
        for name, value in (('OPENSSL', OPENSSL), ('LATEST', self.server.base + 'latest/'), ('MIRRORS', ()),
                            ('RELEASE_FEED', self.server.base + 'releases/v{version}/'),
                            ('PREVIOUS_FEED', self.server.base + 'owrt/v{version}/'), ('SAMPLE_SECONDS', 2)):
            p = patch.object(update, name, value)
            p.start()
            self.addCleanup(p.stop)

    def sign(self, data):
        (self.dir / 'm').write_bytes(data)
        subprocess.run([OPENSSL, 'pkeyutl', '-sign', '-inkey', str(self.key), '-rawin', '-in', str(self.dir / 'm'),
                        '-out', str(self.dir / 's')], check=True, capture_output=True)
        return (self.dir / 's').read_bytes()

    def publish(self, manifest, signature=None):
        data = json.dumps(manifest).encode()
        self.server.put('latest/dashplay-update.json', data)
        self.server.put('latest/dashplay-update.json.sig', signature if signature is not None else self.sign(data))

    def openwrt_release(self, version, packages, jre='21.0.12.1-r1'):
        """Serve packages {name: bytes} as openwrt-dashplay v<version>; returns the manifest's openwrt part."""
        base = f'owrt/v{version}/'
        entries, sums = {}, []
        for name, data in packages.items():
            file = f'{name}-{version}-r1.apk' if name != 'dashplay-jre' else f'dashplay-jre-{jre}_aarch64_generic.apk'
            self.server.put(base + file, data)
            sums.append(f'{sha(data)}  {file}')
            entries[name] = {'file': file, 'sha256': sha(data), 'size': len(data)}
        self.server.put(base + 'SHA256SUMS', ('\n'.join(sums) + '\n').encode())
        jre_entry = entries.pop('dashplay-jre', {'file': 'x', 'sha256': '0', 'size': 1})
        return {'base': self.server.base + base, 'packages': entries,
                'jre': {'version': jre, 'files': {'aarch64_generic': jre_entry}}}

    def test_only_a_manifest_signed_by_the_release_key_counts(self):
        manifest = {'format': 1, 'version': '2026.10.20', 'online': True, 'notes': '- x'}
        self.publish(manifest)
        self.assertEqual(update.latest_manifest(self.root, True, self.pub)['version'], '2026.10.20')
        data = json.dumps(manifest).encode()
        self.server.put('latest/dashplay-update.json', data.replace(b'2026.10.20', b'2026.10.21'))   # tampered
        with self.assertRaisesRegex(update.UpdateError, '签名不对'):
            update.latest_manifest(self.root, True, self.pub)
        other = self.dir / 'other.pem'
        subprocess.run([OPENSSL, 'genpkey', '-algorithm', 'ed25519', '-out', str(other)], check=True, capture_output=True)
        subprocess.run([OPENSSL, 'pkey', '-in', str(other), '-pubout', '-out', str(self.dir / 'other.pub')],
                       check=True, capture_output=True)
        self.publish(manifest)
        with self.assertRaisesRegex(update.UpdateError, '签名不对'):
            update.latest_manifest(self.root, True, self.dir / 'other.pub')

    def test_the_mirror_is_used_when_github_fails(self):
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True})
        self.server.down.add('latest/dashplay-update.json')
        with patch.object(update, 'MIRRORS', (self.server.mirror,)):
            self.assertEqual(update.latest_manifest(self.root, False, self.pub)['version'], '2026.10.20')
        self.assertTrue(any(p.startswith('/mirror/') for p in self.server.seen))
        with self.assertRaisesRegex(update.UpdateError, '下载失败'):
            update.latest_manifest(self.root, False, self.pub)

    def test_the_built_in_mirrors_are_https_prefixes(self):
        # 2026-11-01: the released list was ghfast.top alone, which timed out at 15 s and 40 s.
        self.assertGreaterEqual(len(BUILT_IN_MIRRORS), 6)
        self.assertEqual(update.mirror_list(BUILT_IN_MIRRORS, https_only=True), BUILT_IN_MIRRORS)

    def test_the_fastest_source_is_measured_and_used_first_even_before_github(self):
        # Before 2026-11-01 GitHub came first: a GitHub that trickles never failed, so no mirror was ever tried.
        # The manifest is padded so the measured file is large: at ~1 KB the fetch is 0.02-0.3 s of connection
        # setup for either source, the 20 KB/s trickle never shows, and the order flipped at random (2026-11-04).
        # 32 KB at 20 KB/s is 1.6 s of trickle, far above that jitter.
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True, 'padding': 'x' * 60_000})
        self.server.kinds['gh'] = 'slow'
        fast, dead = self.server.at('fast'), 'http://127.0.0.1:1/'
        with patch.object(update, 'MIRRORS', (dead, fast)), patch.object(update, 'SAMPLE', 32768):
            self.assertEqual(update.latest_manifest(self.root, False, self.pub)['version'], '2026.10.20')
            state = update.sources_state(self.root)
            self.assertEqual([r['source'] for r in state['ranking'] if 'speed' in r], [fast, ''])
            self.assertIn('error', next(r for r in state['ranking'] if r['source'] == dead))
            self.assertEqual(state['used'], fast)
            self.assertEqual(update.sources(self.root, False), (fast, '', dead), 'failed sources stay, last')
            seen = len(self.server.seen)
            update.sources(self.root, False, update.LATEST + update.MANIFEST)
            self.assertEqual(len(self.server.seen), seen, 'a day-old order is reused')
            update.sources(self.root, False, update.LATEST + update.MANIFEST, fresh=True)
            self.assertGreater(len(self.server.seen), seen, 'an install measures again')

    def test_the_setting_picks_automatic_github_only_or_given_mirrors(self):
        options = self.root / 'state/options.json'
        with patch.object(update, 'MIRRORS', (self.server.mirror,)):
            for value, expected in (('none', ('',)), ('https://a.example/ http://b.example', ('https://a.example/', 'http://b.example/', '')),
                                    ('auto', ('', self.server.mirror)), ('', ('', self.server.mirror)),
                                    ('ftp://x', ('', self.server.mirror))):
                options.write_text(json.dumps({'update_mirror': value}))
                self.assertEqual(update.sources(self.root, False), expected, value)
        with self.assertRaises(ValueError):
            update.mirror_list('https://a.example/ javascript:alert(1)')

    def test_a_slow_source_hands_over_and_the_next_goes_on_where_it_stopped(self):
        data = os.urandom(120_000)
        self.server.put('big.tar', data)
        self.server.kinds['gh'] = 'slow'
        target, seen = self.dir / 'big.tar', []
        with patch.object(update, 'STALL_SECONDS', 0.5), patch.object(update, 'STALL_BYTES', 30_000):
            update.fetch(self.server.base + 'big.tar', ('', self.server.mirror), target=target, sha256=sha(data),
                         size=len(data), report=lambda n, source: seen.append((n, source)))
        self.assertEqual(target.read_bytes(), data)
        resumed = [r for p, r in self.server.ranges if p.startswith('/mirror/')]
        self.assertTrue(resumed[0] and resumed[0] != 'bytes=0-', f'the mirror went on from GitHub: {resumed}')
        self.assertEqual(sum(n for n, _ in seen), len(data))
        self.assertEqual({s for _, s in seen}, {'', self.server.mirror})

    def test_when_every_source_is_slow_the_download_still_finishes(self):
        data = os.urandom(40_000)
        self.server.put('big.tar', data)
        self.server.kinds.update(gh='slow', mirror='slow')
        target = self.dir / 'big.tar'
        with patch.object(update, 'STALL_SECONDS', 0.3), patch.object(update, 'STALL_BYTES', 30_000):
            update.fetch(self.server.base + 'big.tar', ('', self.server.mirror), target=target, sha256=sha(data),
                         size=len(data))
        self.assertEqual(target.read_bytes(), data)

    def test_a_broken_off_download_is_resumed_by_the_next_source(self):
        data = os.urandom(100_000)
        self.server.put('big.tar', data)
        self.server.kinds['gh'] = 'cut'
        target = self.dir / 'big.tar'
        update.fetch(self.server.base + 'big.tar', ('', self.server.mirror), target=target, sha256=sha(data),
                     size=len(data))
        self.assertEqual(target.read_bytes(), data)
        self.assertIn(('/mirror/' + self.server.base + 'big.tar', 'bytes=50000-'), self.server.ranges)
        # Without a size from the manifest, too: the length the source announced decides.
        self.assertEqual(update.fetch(self.server.base + 'big.tar', ('', self.server.mirror), limit=1 << 20), data)

    def test_a_block_page_is_never_taken_for_the_file_or_the_manifest(self):
        data = os.urandom(50_000)
        self.server.put('big.tar', data)
        self.server.kinds['block'] = 'page'
        target = self.dir / 'big.tar'
        update.fetch(self.server.base + 'big.tar', (self.server.at('block'), ''), target=target, sha256=sha(data),
                     size=len(data))
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse([r for p, r in self.server.ranges if p.startswith('/gh/') and r], 'GitHub started over')
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True})
        (self.root / 'state/options.json').write_text(json.dumps({'update_mirror': self.server.at('block')}))
        self.assertEqual(update.latest_manifest(self.root, False, self.pub)['version'], '2026.10.20')
        self.assertEqual(update.sources_state(self.root)['used'], '')
        self.assertEqual([r['source'] for r in update.measure(self.server.base + 'big.tar',
                                                              ('', self.server.at('block')), len(data))
                          if 'speed' in r], [''])

    def test_a_signed_manifest_brings_the_mirrors_to_measure(self):
        with patch.object(update, 'MIRRORS', ('https://127.0.0.1:1/old/',)):
            self.assertEqual(update.candidates(self.root), ('https://127.0.0.1:1/old/',))
            self.publish({'format': 1, 'version': '2026.10.20', 'online': True,
                          'mirrors': ['https://127.0.0.1:1/new/', 'https://127.0.0.1:1/other']})
            update.latest_manifest(self.root, False, self.pub)
            self.assertEqual(update.candidates(self.root), ('https://127.0.0.1:1/new/', 'https://127.0.0.1:1/other/'))
            self.publish({'format': 1, 'version': '2026.10.21', 'online': True, 'mirrors': ['http://127.0.0.1:1/plain/']})
            update.latest_manifest(self.root, False, self.pub)
            self.assertEqual(update.candidates(self.root), ('https://127.0.0.1:1/new/', 'https://127.0.0.1:1/other/'),
                             'a list this version refuses keeps the one before')

    def test_the_speed_test_measures_every_source_on_the_biggest_download(self):
        data = self.debian_release()
        dead = 'http://127.0.0.1:1/'
        with patch.object(update, 'MIRRORS', (self.server.mirror, dead)), patch.object(update, 'SAMPLE', 4096):
            state = update.speedtest(self.root, False, self.pub)
        self.assertEqual({r['source'] for r in state['ranking']}, {'', self.server.mirror, dead})
        self.assertEqual(state['measuring'], 0)
        self.assertFalse(update.measuring(self.root))
        sampled = [r for p, r in self.server.ranges if p.endswith('dashplay-bundle-2026.10.20.tar')]
        self.assertEqual(set(sampled), {'bytes=0-4095'})
        self.assertTrue(sampled)
        self.assertIn(data[:4], (self.server.root / 'dp/v2026.10.20/dashplay-bundle-2026.10.20.tar').read_bytes())

    def test_a_download_must_match_the_manifest(self):
        self.server.put('f.apk', b'package')
        target = self.dir / 'f.apk'
        update.fetch(self.server.base + 'f.apk', target=target, sha256=sha(b'package'), size=7)
        self.assertEqual(target.read_bytes(), b'package')
        with self.assertRaisesRegex(update.UpdateError, '下载失败'):
            update.fetch(self.server.base + 'f.apk', target=target, sha256=sha(b'other'), size=7)

    def test_check_reports_a_newer_openwrt_release(self):
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True, 'notes': '- 新功能'})
        with patch.object(update, 'apk_version', lambda name: '2026.10.19.1-r1'):
            result = update.check(self.root, True, self.pub)
        self.assertEqual((result['current'], result['latest'], result['available'], result['notes']),
                         ('2026.10.19.1', '2026.10.20', True, '- 新功能'))
        self.assertEqual(json.loads((self.root / 'state/update-check.json').read_text())['latest'], '2026.10.20')
        with patch.object(update, 'apk_version', lambda name: '2026.10.20-r1'):
            self.assertFalse(update.check(self.root, True, self.pub)['available'])

    def test_a_box_flashed_from_an_image_is_compared_by_its_files(self):
        (self.root / 'supervisor.py').write_text('new')
        files = {'supervisor.py': sha(b'new')}
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True, 'debian': {'files': files}})
        result = update.check(self.root, False, self.pub)
        self.assertEqual((result['available'], result['current']), (False, '2026.10.20'))
        (self.root / 'supervisor.py').write_text('old')
        result = update.check(self.root, False, self.pub)
        self.assertEqual((result['available'], result['current']), (True, None))

    def test_a_box_with_an_older_system_is_not_updated_online(self):
        # A OneCloud flashed with the 2026-10-12 images: its install.sh is not the one the release was made for.
        (self.root / 'firmware').mkdir()
        (self.root / 'firmware/install.sh').write_text('2026-10-12 system')
        files = {'supervisor.py': sha(b'new'), 'firmware/install.sh': sha(b'2026-10-15 system')}
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True, 'debian': {'files': files}})
        result = update.check(self.root, False, self.pub)
        self.assertEqual((result['available'], result['online']), (True, False))
        self.assertIn('系统比这一版要求的旧', result['reason'])
        self.assertEqual(update.install(self.root, False, self.pub), 1)
        status = json.loads((self.root / 'state/update-status.json').read_text())
        self.assertIn('刷机包', status['message'])
        (self.root / 'firmware/install.sh').write_text('2026-10-15 system')
        self.assertTrue(update.check(self.root, False, self.pub)['online'])

    def test_a_failed_check_is_written_too(self):
        result = update.check(self.root, True, self.pub)   # nothing published
        self.assertIn('下载失败', result['error'])

    def test_no_install_while_carplay_is_in_use_or_when_the_system_changed(self):
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True})
        (self.root / 'state/receiver/phone-presence.json').write_text(json.dumps({'active': '02:00:00:00:00:01'}))
        with patch.object(update, 'apk_version', lambda name: '2026.10.19.1-r1'):
            self.assertEqual(update.install(self.root, True, self.pub), 1)
            self.assertIn('正在使用 CarPlay', json.loads((self.root / 'state/update-status.json').read_text())['message'])
            self.assertIn('正在使用 CarPlay', update.launch(self.root, True))
            (self.root / 'state/receiver/phone-presence.json').write_text('{}')
            self.publish({'format': 1, 'version': '2026.10.20', 'online': False})
            self.assertEqual(update.install(self.root, True, self.pub), 1)
            self.assertIn('刷机包', json.loads((self.root / 'state/update-status.json').read_text())['message'])
        with patch.object(update, 'apk_version', lambda name: '2026.10.20-r1'):
            self.assertEqual(update.install(self.root, True, self.pub), 0)
            self.assertIn('已是最新', json.loads((self.root / 'state/update-status.json').read_text())['message'])

    def openwrt_install(self, simulation_extra='', comes_up=(True,), old_published=True,
                        install_timeout=False, rollback_timeout=False, installed_version='2026.10.19.1',
                        domain_after_plan=False):
        target_version = '2026.10.21' if installed_version == '2026.10.20' else '2026.10.20'
        # Larger than fetch()'s default limit, as real packages are (37 MB; lab, 2026-10-19).
        if old_published:
            if installed_version == '2026.10.20':
                self.rollback_release(installed_version)
            else:
                self.openwrt_release(installed_version, {n: f'old {n}'.encode() * 200000 for n in update.OPENWRT_PACKAGES})
        new = self.openwrt_release(target_version, {n: f'new {n}'.encode() for n in update.OPENWRT_PACKAGES})
        self.publish({'format': 1, 'version': target_version, 'online': True, 'openwrt': new})
        calls, ups = [], list(comes_up)
        configured = []

        def run(*args, timeout=300):
            if args[0] == OPENSSL:   # the signature is checked for real
                return subprocess.run(list(args), capture_output=True, text=True)
            calls.append(list(args))
            if args[:2] == ('apk', 'add') and '--simulate' not in args:
                if (install_timeout and '--upgrade' in args) or (rollback_timeout and '--upgrade' not in args):
                    raise subprocess.TimeoutExpired(args, timeout)
            out = ''
            if args == ('uci', '-q', 'show', 'dashplay') and configured:
                out = "dashplay.main.ddns_provider='dnspod'\ndashplay.main.ddns_domain='car.example.org'\n"
            if args[:3] == ('apk', 'add', '--upgrade') and '--simulate' in args:
                out = '(1/3) Upgrading dashplay (2026.10.19.1-r1 -> 2026.10.20-r1)\n' + simulation_extra
                if domain_after_plan:
                    configured.append(True)
            return subprocess.CompletedProcess(args, 0, out, '')

        arch = self.dir / 'arch'
        arch.write_text('aarch64_generic\n')
        real_path = Path

        def fake_path(*parts):
            return arch if parts == ('/etc/apk/arch',) else real_path(*parts)

        versions = {'dashplay': installed_version + '-r1', 'dashplay-jre': '21.0.12.1-r1'}
        with patch.object(update, 'run', run), patch.object(update, 'run_installer', run), \
                patch.object(update, 'apk_version', versions.get), \
                patch.object(update, 'Path', fake_path), patch.object(update, 'settings_port', lambda: 18080), \
                patch.object(update, 'service_up', lambda port, seconds=150: ups.pop(0)), \
                patch.object(update, 'BACKUP', 'backup.tar.gz'):
            code = update.install(self.root, True, self.pub)
        return code, calls, json.loads((self.root / 'state/update-status.json').read_text())

    def test_openwrt_rechecks_domain_changed_during_download_before_installer(self):
        code, calls, status = self.openwrt_install(domain_after_plan=True)
        self.assertEqual(code, 1)
        self.assertIn('DNSPod', status['message'])
        self.assertTrue(any(c[:2] == ['apk', 'add'] and '--simulate' in c for c in calls))
        self.assertFalse(any(c[:2] == ['apk', 'add'] and '--simulate' not in c for c in calls))

    def test_openwrt_installs_the_three_packages_after_a_clean_plan(self):
        code, calls, status = self.openwrt_install()
        self.assertEqual((code, status['phase']), (0, 'done'), status)
        self.assertIn(['apk', 'update'], calls)
        real = [c for c in calls if c[:3] == ['apk', 'add', '--upgrade'] and '--simulate' not in c]
        self.assertEqual(len(real), 1)
        self.assertEqual(sorted(Path(p).name for p in real[0][4:]),
                         ['dashplay-2026.10.20-r1.apk', 'luci-app-dashplay-2026.10.20-r1.apk',
                          'luci-i18n-dashplay-zh-cn-2026.10.20-r1.apk'], 'no JRE: its version did not change')
        self.assertLess(calls.index(['apk', 'update']), calls.index(real[0]))
        with tarfile.open(self.root.parent / 'backup.tar.gz') as saved:
            names = saved.getnames()
        self.assertIn('identity/offline-mfi/identity.pk8', names)
        self.assertIn('state/bluetooth/pairing', names)
        self.assertFalse([n for n in names if n.startswith('state/logs')], 'logs stay out of the backup')
        self.assertEqual(os.stat(self.root.parent / 'backup.tar.gz').st_mode & 0o777, 0o600)

    def test_openwrt_stops_first_when_the_installed_version_was_never_published(self):
        # F50 on a test build, 2026-10-19: it fetched 35 MB, then stopped with a download error that read like the network.
        code, calls, status = self.openwrt_install(old_published=False)
        self.assertEqual((code, status['phase']), (1, 'failed'))
        self.assertIn('找不到当前版本 2026.10.19.1 的安装包', status['message'])
        self.assertNotIn('网络', status['message'])
        self.assertFalse([p for p in self.server.seen if p.endswith('.apk')], 'stopped before the new release was fetched')
        self.assertFalse([c for c in calls if c[:2] == ['apk', 'add']])

    def test_openwrt_refuses_a_plan_with_kernel_modules(self):
        code, calls, status = self.openwrt_install('(2/3) Installing kmod-bluetooth (6.12.94-r1)\n')
        self.assertEqual((code, status['phase']), (1, 'failed'))
        self.assertIn('kmod-bluetooth', status['message'])
        self.assertFalse([c for c in calls if c[:3] == ['apk', 'add', '--upgrade'] and '--simulate' not in c])

    def test_openwrt_goes_back_when_the_new_version_does_not_come_up(self):
        code, calls, status = self.openwrt_install(comes_up=(False, True))
        self.assertEqual((code, status['phase']), (1, 'failed'))
        self.assertIn('已退回 2026.10.19.1', status['message'])
        back = [c for c in calls if c[:2] == ['apk', 'add'] and '--upgrade' not in c]
        self.assertEqual(sorted(Path(p).name for p in back[0][3:]),
                         ['dashplay-2026.10.19.1-r1.apk', 'luci-app-dashplay-2026.10.19.1-r1.apk',
                          'luci-i18n-dashplay-zh-cn-2026.10.19.1-r1.apk'])

    def rollback_release(self, version='2026.10.20'):
        feed = self.openwrt_release(version, {n: ('official ' + n).encode() for n in update.OPENWRT_PACKAGES})
        manifest = {'format': 1, 'version': version, 'online': True, 'openwrt': feed}
        data = json.dumps(manifest).encode()
        base = f'releases/v{version}/'
        self.server.put(base + update.MANIFEST, data)
        self.server.put(base + update.SIGNATURE, self.sign(data))
        return manifest

    def rollback_files(self, version='2026.10.20', prefixes=None):
        verify = update.verify
        with patch.object(update, 'verify', side_effect=lambda data, sig, key: verify(data, sig, self.pub)):
            return update.previous_packages(version, prefixes or (self.server.mirror,), self.dir / 'rollback')

    def forge_rollback_mirror(self, version):
        sums = []
        for name in update.OPENWRT_PACKAGES:
            filename = f'{name}-{version}-r1.apk'
            fake = ('modified ' + name).encode()  # Same length as official bytes: the hash must catch it.
            self.server.overrides['mirror', f'owrt/v{version}/{filename}'] = fake
            sums.append(f'{sha(fake)}  {filename}')
        self.server.overrides['mirror', f'owrt/v{version}/SHA256SUMS'] = '\n'.join(sums).encode()

    def test_signed_rollback_rejects_coherent_forged_packages_and_unsigned_checksums(self):
        self.rollback_release()
        self.forge_rollback_mirror('2026.10.20')
        with self.assertRaises(update.UpdateError):
            self.rollback_files()
        self.assertFalse(any('SHA256SUMS' in p for p in self.server.seen))

    def test_signed_rollback_works_through_mirror_without_direct_github(self):
        manifest = self.rollback_release()
        self.server.down.update(p.relative_to(self.server.root).as_posix() for p in self.server.root.rglob('*') if p.is_file())
        update.keep(self.root, mirrors=['https://current.example/'], used='current-source')
        before = update.sources_state(self.root)
        files = self.rollback_files()
        self.assertEqual(len(files), 3)
        for path, name in zip(files, update.OPENWRT_PACKAGES):
            self.assertEqual(sha(path.read_bytes()), manifest['openwrt']['packages'][name]['sha256'])
        self.assertEqual(update.sources_state(self.root), before, 'old metadata must not overwrite current mirror settings')
        self.assertTrue(all(p.startswith('/mirror/') for p in self.server.seen))

    def test_signed_rollback_can_recover_from_a_corrupt_mirror(self):
        self.rollback_release()
        self.forge_rollback_mirror('2026.10.20')
        files = self.rollback_files(prefixes=(self.server.mirror, update.DIRECT))
        for path, name in zip(files, update.OPENWRT_PACKAGES):
            self.assertEqual(path.read_bytes(), ('official ' + name).encode())
        self.assertTrue(any(p.startswith('/mirror/') and p.endswith('.apk') for p in self.server.seen))
        self.assertTrue(any(p.startswith('/gh/') and p.endswith('.apk') for p in self.server.seen))

    def test_incomplete_signed_rollback_manifest_downloads_no_packages(self):
        manifest = self.rollback_release()
        del manifest['openwrt']['packages']['luci-app-dashplay']
        data = json.dumps(manifest).encode()
        base = 'releases/v2026.10.20/'
        self.server.put(base + update.MANIFEST, data)
        self.server.put(base + update.SIGNATURE, self.sign(data))
        with self.assertRaisesRegex(update.UpdateError, '回退清单不完整'):
            self.rollback_files()
        self.assertFalse(any(p.endswith('.apk') for p in self.server.seen))

    def test_signed_rollback_never_falls_back_when_signature_is_bad_or_missing(self):
        self.rollback_release()
        signature = 'releases/v2026.10.20/' + update.SIGNATURE
        for invalid in (b'forged signature', b'', None):
            with self.subTest(signature=invalid):
                if invalid is None:
                    (self.server.root / signature).unlink()
                else:
                    self.server.overrides['mirror', signature] = invalid
                self.server.seen.clear()
                with self.assertRaises(update.UpdateError):
                    self.rollback_files()
                self.assertFalse(any(p.endswith('.apk') or 'SHA256SUMS' in p for p in self.server.seen))

    def test_signed_rollback_rejects_replay_of_a_different_release(self):
        manifest = self.rollback_release()
        manifest['version'] = '2026.10.21'
        data = json.dumps(manifest).encode()
        base = 'releases/v2026.10.20/'
        self.server.put(base + update.MANIFEST, data)
        self.server.put(base + update.SIGNATURE, self.sign(data))
        with self.assertRaisesRegex(update.UpdateError, '不是所选版本'):
            self.rollback_files()
        self.assertFalse(any(p.endswith('.apk') for p in self.server.seen))

    def test_legacy_rollback_never_trusts_mirror_supplied_checksums(self):
        self.openwrt_release('2026.10.19.1', {n: ('official ' + n).encode() for n in update.OPENWRT_PACKAGES})
        self.forge_rollback_mirror('2026.10.19.1')
        with self.assertRaisesRegex(update.UpdateError, '校验不符'):
            self.rollback_files('2026.10.19.1')
        self.assertIn('/gh/owrt/v2026.10.19.1/SHA256SUMS', self.server.seen)
        self.assertFalse(any(p.startswith('/mirror/') and 'SHA256SUMS' in p for p in self.server.seen))

    def test_legacy_rollback_stops_when_official_checksums_are_unreachable(self):
        self.openwrt_release('2026.10.19.1', {n: b'official' for n in update.OPENWRT_PACKAGES})
        self.server.down.add('owrt/v2026.10.19.1/SHA256SUMS')
        with self.assertRaises(update.UpdateError):
            self.rollback_files('2026.10.19.1')
        self.assertFalse(any(p.startswith('/mirror/') or p.endswith('.apk') for p in self.server.seen))

    def test_install_authenticates_signed_rollback_and_can_restore_it(self):
        code, calls, status = self.openwrt_install(installed_version='2026.10.20', install_timeout=True)
        self.assertEqual(code, 1)
        self.assertIn('已退回 2026.10.20', status['message'])
        self.assertTrue(any('/releases/v2026.10.20/' in p and p.endswith(update.SIGNATURE) for p in self.server.seen))
        self.assertFalse(any('SHA256SUMS' in p for p in self.server.seen))

    def test_missing_signed_rollback_stops_before_installing_new_packages(self):
        code, calls, status = self.openwrt_install(installed_version='2026.10.20', old_published=False)
        self.assertEqual(code, 1)
        self.assertFalse(any(c[:2] == ['apk', 'add'] for c in calls))

    def test_openwrt_install_timeout_runs_local_rollback(self):
        code, calls, status = self.openwrt_install(install_timeout=True)
        back = [c for c in calls if c[:2] == ['apk', 'add'] and '--upgrade' not in c]
        self.assertEqual(len(back), 1)
        self.assertTrue(all('/previous/' in p for p in back[0][3:]))
        self.assertEqual((code, status['phase']), (1, 'failed'))
        self.assertIn('已退回 2026.10.19.1', status['message'])

    def test_openwrt_rollback_timeout_or_unhealthy_service_does_not_claim_success(self):
        for timed_out, comes_up in ((True, ()), (False, (False,))):
            with self.subTest(rollback_timeout=timed_out):
                code, calls, status = self.openwrt_install(install_timeout=True,
                    rollback_timeout=timed_out, comes_up=comes_up)
                self.assertEqual(code, 1)
                self.assertIn('退回也没有成功', status['message'])
                self.assertNotIn('已退回', status['message'])

    def debian_release(self, padding=0):
        """Publish 2026.10.20 for OneCloud / N1: a bundle with a new upgrader (padded to be big); its bytes."""
        # The installed local upgrader must remain available even if the candidate replaces its path.
        installed = self.root / 'firmware/upgrade.py'
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_text('PROGRAM_BACKUP_FSYNC = 1\nPLATFORM_UPGRADE_CHECK = 1\n# installed upgrader')
        bundle = io.BytesIO()
        with tarfile.open(fileobj=bundle, mode='w') as tar:
            for name, data in (('./firmware/upgrade.py', b'# new upgrader'), ('./supervisor.py', b'new'),
                               ('./padding', os.urandom(padding))):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        data = bundle.getvalue()
        self.server.put('dp/v2026.10.20/dashplay-bundle-2026.10.20.tar', data)
        self.publish({'format': 1, 'version': '2026.10.20', 'online': True,
                      'debian': {'base': self.server.base + 'dp/v2026.10.20/', 'files': {'supervisor.py': sha(b'new')},
                                 'bundle': {'file': 'dashplay-bundle-2026.10.20.tar', 'sha256': sha(data), 'size': len(data)}}})
        return data

    def test_program_upgrader_selection_preserves_durable_tool_across_downgrade(self):
        retained = self.root / 'state/version-manager/upgrade.py'
        installed = self.root / 'firmware/upgrade.py'
        retained.parent.mkdir(parents=True, exist_ok=True)
        installed.parent.mkdir(parents=True, exist_ok=True)
        supported = 'PROGRAM_BACKUP_FSYNC = 1\nPLATFORM_UPGRADE_CHECK = 1\n'
        for retained_text, installed_text, expected in (
                ('# old retained', supported + '# installed', supported + '# installed'),
                ('PROGRAM_BACKUP_FSYNC = 1\n# old gate', supported, supported),
                ('PROGRAM_BACKUP_FSYNC = 1\n', 'PROGRAM_BACKUP_FSYNC = 1\n', None),
                (supported + '# retained', '# old installed after downgrade', supported + '# retained'),
                ('# old retained', '# old installed', None)):
            with self.subTest(retained=retained_text, installed=installed_text):
                retained.write_text(retained_text); installed.write_text(installed_text)
                manifest = {'version':'2026.11.12', 'debian':{'base':'https://example.invalid/',
                    'bundle':{'file':'bundle.tar', 'sha256':'synthetic', 'size':1}}}
                with tempfile.TemporaryDirectory() as work, patch.object(update, 'sources', return_value=[]), \
                        patch.object(update, 'fetch', return_value=Path(work)/'bundle.tar'), \
                        patch.object(update, 'keep'), patch.object(update, 'run_installer') as run:
                    def execute(*args, **kwargs):
                        self.assertEqual(Path(args[1]).read_text(), expected)
                        return subprocess.CompletedProcess(args, 0, '', '')
                    run.side_effect = execute
                    if expected is None:
                        with self.assertRaisesRegex(update.UpdateError, '备份'):
                            update.install_debian(self.root, manifest, lambda *a, **k: None, work)
                        run.assert_not_called()
                    else:
                        update.install_debian(self.root, manifest, lambda *a, **k: None, work)
                        run.assert_called_once()

    def test_wired_debian_downloads_verified_bundle_before_starting_local_upgrader(self):
        bundle = self.debian_release()
        status = self.root / 'wired-status.json'
        save_status = {'phase': 'live'}
        update.save(status, save_status)
        calls = []

        def run(*args, timeout=300):
            if args[0] == OPENSSL:
                return subprocess.run(list(args), capture_output=True, text=True)
            calls.append(list(args))
            self.assertEqual(Path(args[2]).read_bytes(), bundle, 'the whole verified bundle is local before stopping CarPlay')
            self.assertEqual(update.read(status), save_status)
            (self.root / 'supervisor.py').write_text('new')
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(update, 'run', run), patch.object(update, 'run_installer', run), \
                patch.object(update, 'BACKUP', 'backup.tar.gz'), \
                patch.object(update, 'WIRED_STATUS', status):
            self.assertEqual(update.install(self.root, False, self.pub), 0)
        self.assertEqual(len(calls), 1, 'only the local upgrader may stop services')
        self.assertEqual(Path(calls[0][1]).name, 'upgrade.py')
        self.assertEqual(Path(calls[0][2]).name, 'dashplay-bundle-2026.10.20.tar')
        self.assertEqual(json.loads((self.root / 'state/update.json').read_text())['version'], '2026.10.20')
        self.assertFalse(json.loads((self.root / 'state/update-check.json').read_text())['available'])

    def test_wired_download_or_hash_failure_never_starts_the_installer(self):
        bundle = self.debian_release()
        path = 'dp/v2026.10.20/dashplay-bundle-2026.10.20.tar'
        status = self.root / 'wired-status.json'
        update.save(status, {'phase': 'live'})
        commands = []
        def run(*args, **kwargs):
            if args[0] == OPENSSL:
                return subprocess.run(list(args), capture_output=True, text=True)
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')
        for failure in ('unavailable', 'corrupt'):
            with self.subTest(failure=failure):
                if failure == 'unavailable':
                    self.server.down.add(path)
                else:
                    self.server.down.clear()
                    self.server.put(path, b'x' * len(bundle))
                self.server.seen.clear()
                with patch.object(update, 'WIRED_STATUS', status), patch.object(update, 'run', run), \
                        patch.object(update, 'run_installer', run):
                    self.assertEqual(update.install(self.root, False, self.pub), 1)
                self.assertTrue(any(path in p for p in self.server.seen), 'wired session must reach download')
                self.assertEqual(update.read(status), {'phase': 'live'})
                self.assertEqual(update.read(self.root / 'state/update-status.json')['phase'], 'failed')
                self.assertEqual(commands, [], 'no service or installer command before a verified download')

    def test_debian_timeout_restores_real_program_files_using_the_saved_upgrader(self):
        from test_upgrade import upgrade
        self.debian_release()
        (self.root / 'supervisor.py').write_text('old program')
        previous = self.root.parent / 'dashplay-previous.tar'
        calls = []
        def installer(*args, timeout):
            calls.append(args)
            if args[0] == 'systemctl':
                self.assertEqual(args, ('systemctl', 'stop', 'dashplay'))
                return subprocess.CompletedProcess(args, 0, '', '')
            self.assertEqual(Path(args[1]).read_text(), 'PROGRAM_BACKUP_FSYNC = 1\nPLATFORM_UPGRADE_CHECK = 1\n# installed upgrader')
            self.assertNotEqual(Path(args[1]), self.root / 'firmware/upgrade.py')
            if args[2] != '--rollback':
                upgrade.backup(self.root)
                (self.root / 'supervisor.py').write_text('half-installed program')
                (self.root / 'firmware/upgrade.py').write_text('# broken candidate upgrader')
                (self.root / 'candidate-only.py').write_text('new file')
                raise subprocess.TimeoutExpired(args, timeout)
            return subprocess.CompletedProcess(args, upgrade.main(['--rollback']), '', '')
        with patch.object(update, 'run_installer', side_effect=installer), \
                patch.object(upgrade, 'ROOT', self.root), patch.object(upgrade, 'PREVIOUS', previous), \
                patch.object(upgrade.os, 'geteuid', return_value=0), patch.object(upgrade.os, 'chown'), \
                patch.object(upgrade, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')), \
                patch.object(upgrade, 'install_unit'), patch.object(upgrade, 'service_up', return_value=True):
            self.assertEqual(update.install(self.root, False, self.pub), 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1], ('systemctl', 'stop', 'dashplay'))
        self.assertEqual(calls[2][2], '--rollback')
        self.assertEqual(calls[0][1], calls[2][1])
        self.assertEqual((self.root / 'supervisor.py').read_text(), 'old program')
        self.assertEqual((self.root / 'firmware/upgrade.py').read_text(), 'PROGRAM_BACKUP_FSYNC = 1\nPLATFORM_UPGRADE_CHECK = 1\n# installed upgrader')
        self.assertFalse((self.root / 'candidate-only.py').exists())
        self.assertEqual((self.root / 'identity/offline-mfi/identity.pk8').read_bytes(), b'secret identity')
        self.assertIn('已恢复', update.read(self.root / 'state/update-status.json')['message'])
        self.assertFalse((self.root / 'state/update.json').exists())

    def test_debian_timeout_before_new_backup_never_restores_an_older_upgrade(self):
        self.debian_release()
        previous = self.root.parent / 'dashplay-previous.tar'
        for old_backup in (False, True):
            with self.subTest(old_backup=old_backup):
                if old_backup:
                    previous.write_bytes(b'backup from an earlier upgrade')
                with patch.object(update, 'run_installer', side_effect=subprocess.TimeoutExpired('upgrade', 900)) as installer:
                    self.assertEqual(update.install(self.root, False, self.pub), 1)
                self.assertEqual(installer.call_count, 1)
                message = update.read(self.root / 'state/update-status.json')['message']
                self.assertIn('未确认本次', message)
                self.assertNotIn('已恢复', message)
                if old_backup:
                    self.assertEqual(previous.read_bytes(), b'backup from an earlier upgrade')

    def test_debian_failed_or_timed_out_rollback_never_claims_recovery(self):
        self.debian_release()
        previous = self.root.parent / 'dashplay-previous.tar'
        for outcome in ('failed', 'timeout', 'missing', 'stop-failed', 'stop-timeout'):
            with self.subTest(outcome=outcome):
                calls = []
                def installer(*args, timeout):
                    calls.append(args)
                    if args[0] == 'systemctl':
                        if outcome == 'stop-timeout':
                            raise subprocess.TimeoutExpired(args, timeout)
                        return subprocess.CompletedProcess(args, 1 if outcome == 'stop-failed' else 0, '', '')
                    if args[2] != '--rollback':
                        temporary = previous.with_suffix('.tmp')
                        temporary.write_bytes(b'new backup')
                        temporary.replace(previous)
                        raise subprocess.TimeoutExpired(args, timeout)
                    if outcome == 'timeout':
                        raise subprocess.TimeoutExpired(args, timeout)
                    if outcome == 'missing':
                        raise FileNotFoundError('missing interpreter')
                    return subprocess.CompletedProcess(args, 1, '', '')
                with patch.object(update, 'run_installer', side_effect=installer):
                    self.assertEqual(update.install(self.root, False, self.pub), 1)
                self.assertEqual(len(calls), 2 if outcome.startswith('stop-') else 3)
                message = update.read(self.root / 'state/update-status.json')['message']
                self.assertIn('自动恢复没有成功', message)
                self.assertNotIn('已恢复', message)
                self.assertFalse((self.root / 'state/update.json').exists())

    def test_debian_nonzero_exit_does_not_assume_internal_rollback_succeeded(self):
        self.debian_release()
        with patch.object(update, 'run_installer', return_value=subprocess.CompletedProcess([], 1, '', '')) as installer:
            self.assertEqual(update.install(self.root, False, self.pub), 1)
        self.assertEqual(installer.call_count, 1)
        self.assertNotIn('已自动退回', update.read(self.root / 'state/update-status.json')['message'])

    def test_the_page_sees_bytes_speed_and_source_while_downloading(self):
        self.debian_release(padding=60_000)
        self.server.kinds['mirror'] = 'slow'   # about 20 KB/s, some 3 s: progress is written once a second
        self.server.down.add('dp/v2026.10.20/dashplay-bundle-2026.10.20.tar')
        written, real_save = [], update.save

        def save(path, value):
            if path.name == 'update-status.json':
                written.append(dict(value))
            real_save(path, value)

        def run(*args, timeout=300):
            if args[0] == OPENSSL:
                return subprocess.run(list(args), capture_output=True, text=True)
            return subprocess.CompletedProcess(args, 0, '', '')

        with patch.object(update, 'MIRRORS', (self.server.mirror,)), patch.object(update, 'save', save), \
                patch.object(update, 'run', run), patch.object(update, 'run_installer', run), \
                patch.object(update, 'BACKUP', 'backup.tar.gz'), \
                patch.object(update, 'STALL_SECONDS', 60), patch.object(update, 'SAMPLE', 1024):
            self.assertEqual(update.install(self.root, False, self.pub), 0, written[-1])
        phases = [w['phase'] for w in written]
        self.assertIn('measuring', phases)
        moving = [w for w in written if w['phase'] == 'downloading' and 0 < w.get('done', 0) < w['total']]
        self.assertGreaterEqual(len(moving), 2, phases)
        self.assertTrue(all(w['source'] == update.name_of(self.server.mirror) for w in moving), moving)
        self.assertTrue(any(w['speed'] > 0 for w in moving))
        last = [w for w in written if w['phase'] == 'downloading'][-1]
        self.assertEqual(last['done'], last['total'])
        self.assertEqual(update.sources_state(self.root)['used'], self.server.mirror)


class InstallerProcessTests(unittest.TestCase):
    def test_output_and_exit_code_are_preserved(self):
        result = update.run_installer(sys.executable, '-c',
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)", timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, 'out\n', 'err\n'))

    @unittest.skipUnless(os.name == 'posix', 'installer process groups require POSIX')
    def test_timeout_stops_child_processes_before_returning(self):
        import signal
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            child = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); time.sleep(60)"
            with self.assertRaises(subprocess.TimeoutExpired):
                update.run_installer(sys.executable, '-c', parent, child, str(pidfile), timeout=2)
            self.assertTrue(pidfile.is_file(), 'child was running before the timeout')
            pid = int(pidfile.read_text())
            try:
                # A killed orphan can briefly remain as a zombie until init reaps it.
                state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
                self.assertTrue(not state or state.startswith('Z'), state)
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


class ServiceUpTests(unittest.TestCase):
    def test_the_page_must_still_answer_a_while_later(self):
        answers = iter([True, False, False, False])   # up at once, then gone: a crash after the start
        def connect(*_):
            if not next(answers, False):
                raise OSError('refused')
            return io.BytesIO()
        with patch.object(update.socket, 'create_connection', connect), patch.object(update.time, 'sleep'):
            self.assertFalse(update.service_up(18080, seconds=0.05, steady=0))
        answers = iter([False, True, True])
        with patch.object(update.socket, 'create_connection', connect), patch.object(update.time, 'sleep'):
            self.assertTrue(update.service_up(18080, seconds=5, steady=0))


class LaunchTests(unittest.TestCase):
    def test_a_launch_that_cannot_start_says_so_and_leaves_nothing_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def missing(*a, timeout=30):
                raise FileNotFoundError(2, 'No such file or directory', a[0])
            with patch.object(update, 'run', missing):
                self.assertIn('无法启动升级', update.launch(root, True))
            status = json.loads((root / 'state/update-status.json').read_text())
            self.assertEqual(status['phase'], 'failed')
            self.assertFalse(update.running(root), 'the next try is not refused')

    def test_launch_starts_the_separate_service_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            with patch.object(update, 'run', lambda *a, timeout=30: calls.append(a) or subprocess.CompletedProcess(a, 0, '', '')):
                self.assertIn('已开始升级', update.launch(root, True))
                # A procd service for this run over ubus: no init script that installing a package would start.
                self.assertEqual([c[:4] for c in calls], [('ubus', 'call', 'service', 'delete'), ('ubus', 'call', 'service', 'set')])
                service = json.loads(calls[1][4])
                self.assertEqual(service['name'], 'dashplay-update')
                self.assertEqual(service['instances']['update']['command'][2:], ['install', '--root', str(root)])
                self.assertIn('正在升级', update.launch(root, True), 'a second click while it runs starts nothing')
                self.assertEqual(len(calls), 2)
                update.save(root / 'state/update-status.json', {'phase': 'done', 'at': 0})
                update.launch(root, False)
                self.assertEqual(calls[2][0], 'systemd-run')
                self.assertIn('--unit=dashplay-update', calls[2])


class ManifestTests(unittest.TestCase):
    def test_provider_capability_comes_from_bundle_without_executing_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('make_update_manifest', Path(__file__).resolve().parents[2]
                                                      / 'scripts/make-update-manifest.py')
        maker = importlib.util.module_from_spec(spec); spec.loader.exec_module(maker)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'supervisor_openwrt.py'
            self.assertEqual(maker.openwrt_dns_providers(tmp), ['duckdns'])
            source.write_text("raise RuntimeError('must not execute')\nDNS_PROVIDERS = ('duckdns', 'dnspod', 'dynv6')\n")
            self.assertEqual(maker.openwrt_dns_providers(tmp), ['duckdns', 'dnspod', 'dynv6'])
            source.write_text("# an old reader, with no declared capability\n")
            self.assertEqual(maker.openwrt_dns_providers(tmp), ['duckdns'])
            source.write_text("DNS_PROVIDERS = 'dnspod'\n")
            with self.assertRaises(ValueError): maker.openwrt_dns_providers(tmp)

    def test_the_release_manifest_lists_every_download_with_its_hash(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('make_update_manifest', Path(__file__).resolve().parents[2]
                                                      / 'scripts/make-update-manifest.py')
        maker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(maker)
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp)
            (release / 'openwrt').mkdir()
            (release / 'dashplay').mkdir()
            for name in ('dashplay-2026.10.20-r1.apk', 'luci-app-dashplay-0.261020.1.apk',
                         'luci-i18n-dashplay-zh-cn-0.261020.1.apk', 'dashplay-jre-21.0.12.1-r1_aarch64_generic.apk',
                         'dashplay-jre-21.0.12.1-r1_x86_64.apk'):
                (release / 'openwrt' / name).write_bytes(name.encode())
            with tarfile.open(release / 'dashplay/dashplay-bundle-2026.10.20.tar', 'w') as tar:
                for name, data in (('./supervisor.py', b'program'), ('./state/receiver/x', b'owner'),
                                   ('./identity/offline-mfi/identity.pk8', b'secret')):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            value = maker.manifest('2026.10.20', '- 新功能', release, 'owner', True)
        self.assertEqual((value['format'], value['version'], value['online'], value['notes']), (1, '2026.10.20', True, '- 新功能'))
        self.assertEqual(value['openwrt']['packages']['dashplay']['sha256'], sha(b'dashplay-2026.10.20-r1.apk'))
        self.assertEqual(value['openwrt']['jre']['version'], '21.0.12.1-r1')
        self.assertEqual(value['openwrt']['dns_providers'], ['duckdns'])
        self.assertEqual(sorted(value['openwrt']['jre']['files']), ['aarch64_generic', 'x86_64'])
        self.assertEqual(value['openwrt']['base'], 'https://github.com/owner/openwrt-dashplay/releases/download/v2026.10.20/')
        self.assertEqual(value['debian']['files'], {'supervisor.py': sha(b'program')}, "the owner's data is no program file")
        self.assertEqual(value['mirrors'], list(update.MIRRORS), 'boxes from then on measure the mirrors of the release')


if __name__ == '__main__':
    unittest.main()
