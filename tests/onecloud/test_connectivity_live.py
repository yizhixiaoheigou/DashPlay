"""Real isolated dnsmasq on loopback high ports; no DHCP or production changes."""
import http.server
import ssl
import signal
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/onecloud'))
from connectivity import DnsFiles, Monitor, dns_name, local_scope
from test_connectivity import sample

def skip_name(data, offset):
    return dns_name(data, offset)[1]


@unittest.skipUnless(sys.platform=='linux' and shutil.which('dnsmasq'), 'Linux dnsmasq required')
class LiveDnsTests(unittest.TestCase):
    def dns_flags(self, flags):
        return flags

    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.directory=Path(temp.name); self.directory.chmod(0o755)
        self.up=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.up.bind(('127.0.0.1',0))
        self.up.settimeout(.1);self.addCleanup(self.up.close)
        self.stop=threading.Event();self.queries=0
        self.online = threading.Event(); self.online.set()
        def upstream():
            while not self.stop.is_set():
                try: data,peer=self.up.recvfrom(4096)
                except socket.timeout:continue
                self.queries+=1
                if not self.online.is_set(): continue
                end=skip_name(data,12)+4
                qtype=struct.unpack('!H',data[end-4:end-2])[0]
                tail=b'\xc0\x0c'+struct.pack('!HHIH',1,1,60,4)+socket.inet_aton('8.8.4.4') if qtype==1 else b''
                self.up.sendto(data[:2]+struct.pack('!5H',0x8180,1,int(qtype==1),0,0)+data[12:end]+tail,peer)
        worker=threading.Thread(target=upstream);worker.start()
        def stop_worker():self.stop.set();worker.join(1)
        self.addCleanup(stop_worker)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1',0));self.port=reserve.getsockname()[1]
        config = self.directory / 'dnsmasq.d'; config.mkdir(mode=0o755)
        self.files = DnsFiles(config / 'dynamic', '100.96.0.1', ['check.example'])
        flags = self.dns_flags(self.files.prepare())
        self.probe = Mock(return_value=sample())
        self.log=(self.directory/'dns.log').open('wb');self.addCleanup(self.log.close)
        self.child=subprocess.Popen(['dnsmasq','--keep-in-foreground','--conf-file=/dev/null',
            '--no-hosts','--no-resolv','--bind-interfaces','--listen-address=127.0.0.1',
            '--pid-file='+str(self.directory/'pid'),'--port='+str(self.port),
            '--server=127.0.0.1#'+str(self.up.getsockname()[1]),
            '--address=/viewer.example/127.0.0.1','--local=/viewer.example/',*flags],
            stdout=self.log,stderr=subprocess.STDOUT)
        self.policy = Monitor(self.probe, self.files, lambda: self.child.send_signal(signal.SIGHUP))
        def stop_child():
            self.policy.close()
            if self.child.poll() is None:self.child.terminate()
            self.child.wait(timeout=5)
        self.addCleanup(stop_child)
        for _ in range(30):
            if self.child.poll() is not None:self.fail((self.directory/'dns.log').read_text())
            try:self.ask('check.example');break
            except (TimeoutError,ConnectionRefusedError):time.sleep(.05)
        else:self.fail('isolated DNS did not start')

    def ask(self,name,kind=1):
        question=b''.join(bytes([len(x)])+x.encode() for x in name.split('.'))+b'\0'+struct.pack('!2H',kind,1)
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
            sock.settimeout(.2);sock.sendto(struct.pack('!6H',42,0x100,1,0,0,0)+question,('127.0.0.1',self.port))
            data=sock.recv(4096)
        _,flags,_,count,_,_=struct.unpack('!6H',data[:12]);self.assertEqual(flags&15,0)
        answers=[];offset=skip_name(data,12)+4
        for _ in range(count):
            offset=skip_name(data,offset);rtype,_,ttl,size=struct.unpack('!HHIH',data[offset:offset+10]);offset+=10
            if rtype==1:answers.append((socket.inet_ntoa(data[offset:offset+size]),ttl))
            offset+=size
        return answers

    def wait_answer(self, name, expected):
        for _ in range(30):
            if self.ask(name) == expected: return
            time.sleep(.02)
        self.fail('DNS did not converge: ' + (self.directory / 'dns.log').read_text())

    def test_repeated_outage_and_recovery_keep_local_dns_tls_and_same_process(self):
        cert = self.directory / 'cert.pem'; key = self.directory / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '1',
                        '-subj', '/CN=viewer.example', '-addext', 'subjectAltName=DNS:viewer.example'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'local viewer')
            def log_message(self, *args): pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Page)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        def close_server(): server.shutdown(); server.server_close(); worker.join(2)
        self.addCleanup(close_server)
        trusted = ssl.create_default_context(cafile=str(cert))
        def local_https():
            self.assertEqual(self.ask('viewer.example'), [('127.0.0.1', 0)])
            for kind in (28, 65): self.assertEqual(self.ask('viewer.example', kind), [])
            with socket.create_connection(('127.0.0.1', server.server_address[1]), timeout=2) as connection:
                with trusted.wrap_socket(connection, server_hostname='viewer.example') as tls:
                    tls.sendall(b'GET / HTTP/1.0\r\nHost: viewer.example\r\n\r\n')
                    self.assertIn(b'200 OK', tls.recv(4096))
        pid = self.child.pid
        local_https()
        self.assertEqual(self.ask('check.example'), [('100.96.0.1', 0)])
        for _ in range(3):
            self.probe.return_value = sample()
            for _ in range(3): self.policy.step()
            self.wait_answer('check.example', [('8.8.4.4', 60)])
            before = self.queries
            for kind in (28, 65): self.assertEqual(self.ask('check.example', kind), [])
            self.assertEqual(self.queries, before + 2)
            local_https()
            # USB stays connected but mobile data fails. Stop replying upstream.
            self.online.clear()
            self.probe.return_value = sample(False)
            self.assertTrue(self.policy.step())
            self.policy.step()
            self.wait_answer('check.example', [('100.96.0.1', 0)])
            before = self.queries
            for kind in (28, 65): self.assertEqual(self.ask('check.example', kind), [])
            self.assertEqual(self.queries, before, 'protected names must not leak AAAA/HTTPS upstream')
            local_https()
            self.assertIsNone(self.child.poll()); self.assertEqual(self.child.pid, pid)
            self.online.set()


class OpenWrtDirectoryDnsTests(LiveDnsTests):
    def dns_flags(self, flags):
        # Exercise the actual conf-dir/subdirectory layout that OpenWrt mounts
        # into the jail, rather than individual file mounts with stale inodes.
        directory = self.files.directory.parent
        (directory / 'dashplay-tesla-checks.conf').write_text(''.join(flag[2:] + '\n' for flag in flags))
        return [f'--conf-dir={directory}']


class StaticProtectionDnsTests(LiveDnsTests):
    test_repeated_outage_and_recovery_keep_local_dns_tls_and_same_process = None

    def dns_flags(self, flags):
        return ['--address=/check.example/100.96.0.1', '--local-ttl=0',
                '--local=' + local_scope('/lan/', ['check.example'])]

    def test_static_fallback_never_forwards_aaaa_or_https_queries(self):
        self.assertEqual(self.ask('check.example'), [('100.96.0.1', 0)])
        before = self.queries
        for kind in (28, 65):
            self.assertEqual(self.ask('check.example', kind), [])
        self.assertEqual(self.queries, before)
        # The local restriction must not block ordinary, unrelated websites.
        self.assertEqual(self.ask('unrelated.example'), [('8.8.4.4', 60)])
        self.assertGreater(self.queries, before)


if __name__ == '__main__': unittest.main()
