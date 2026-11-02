#!/usr/bin/env python3
"""One real LAN peer for the emulator VPN probe; loopback only, no Internet proxy."""
import argparse
import pathlib
import socketserver
import subprocess
import threading
import time
from emulator_root_bridge import EmulatorRootBridge

parser = argparse.ArgumentParser()
parser.add_argument('--adb', required=True)
parser.add_argument('--serial', required=True)
parser.add_argument('--output', type=pathlib.Path, required=True)
parser.add_argument('--share', action='store_true')
parser.add_argument('--manager', action='store_true')
parser.add_argument('--crash', action='store_true')
parser.add_argument('--late', action='store_true')
args = parser.parse_args()
assert not (args.share and args.manager)
assert not args.crash or args.manager
assert not args.late or args.crash
assert args.serial.startswith('emulator-')
observed = threading.Event()
adb = [args.adb, '-s', args.serial]
root = None
root_log = None
bridge = None
root_base = '/data/local/tmp/dashplay-share-manager' if args.manager else '/data/local/tmp/dashplay-shared-vpn'
root_fixture = 'usb-share-manager-fixture.sh' if args.manager else 'usb-share-vpn-fixture.sh'
root_output = 'manager-kernel.txt' if args.manager else 'shared-kernel.txt'

class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        expected = b'dashplay-lan-outside-vpn'
        self.request.settimeout(5)
        data = b''
        while len(data) < len(expected):
            chunk = self.request.recv(len(expected) - len(data))
            if not chunk:
                return
            data += chunk
        if data == expected:
            observed.set()
            self.request.sendall(data)

with socketserver.TCPServer(('127.0.0.1', 0), Echo) as server:
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        if args.share or args.manager:
            root_log = args.output.with_name(root_output).open('wb')
            root = subprocess.Popen(adb + ['shell', 'sh', root_base + '/' + root_fixture, str(args.crash).lower(), str(args.late).lower()], stdout=root_log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            while subprocess.run(adb + ['shell', 'test', '-f', root_base + '/prepared'], stdout=subprocess.DEVNULL).returncode:
                assert root.poll() is None and time.monotonic() < deadline, 'client namespace setup failed'
                time.sleep(.2)
        if args.manager:
            bridge = EmulatorRootBridge(adb, args.output.with_name('manager-root-requests'))
            bridge.start()
        result = subprocess.run(adb + ['shell', 'am', 'instrument', '-w',
            '-e', 'probe', 'usb-internet', '-e', 'lanPort', str(server.server_address[1]),
            '-e', 'share', str(args.share).lower(),
            '-e', 'manager', str(args.manager).lower(),
            '-e', 'crash', str(args.crash).lower(),
            'com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe'],
            check=True, stdout=subprocess.PIPE, timeout=360 if args.manager else 240 if args.share else 120)
        if args.crash:
            args.output.with_name('crash-first-instrumentation.txt').write_bytes(result.stdout)
            assert b'success=true' not in result.stdout and b'Process crashed' in result.stdout, result.stdout.decode()
            assert observed.is_set(), 'original process never reached actual Mac LAN'
            deadline = time.monotonic() + 40
            while subprocess.run(adb + ['shell', 'test', '-f', root_base + '/crash-dead-ready'], stdout=subprocess.DEVNULL).returncode:
                assert root.poll() is None and time.monotonic() < deadline, 'process death or guard not verified'
                time.sleep(.2)
            bridge.close()
            bridge = EmulatorRootBridge(adb, args.output.with_name('manager-recovery-root-requests'), 'recovery-requests')
            bridge.start()
            result = subprocess.run(adb + ['shell', 'am', 'instrument', '-w', '-e', 'probe', 'usb-share-recover',
                'com.dashplay.android.test/com.dashplay.android.CertificateNetworkProbe'],
                check=True, stdout=subprocess.PIPE, timeout=150)
        args.output.write_bytes(result.stdout)
        print(result.stdout.decode(), end='')
        assert b'success=true' in result.stdout and observed.is_set(), 'VPN or actual remote LAN peer failed'
        print('REMOTE_LAN_PEER_OK actual Mac TCP echo; not a synthetic IP reply')
        if root:
            assert root.wait(timeout=10) == 0, 'shared VPN kernel client failed'
            root_log.flush()
            output = args.output.with_name(root_output).read_text()
            marker = 'MANAGER_PROCESS_RESTART_EXACT_CLEANUP_OK' if args.crash else 'MANAGER_VPN_CLOSE_CLEANUP_OK' if args.manager else 'SHARED_VPN_CLEANUP_OK'
            assert marker in output
            print(output, end='')
    finally:
        if root is not None:
            if root.poll() is None:
                pid = subprocess.check_output(adb + ['shell','cat',root_base + '/runner.pid']).decode().strip()
                assert pid.isdigit()
                subprocess.run(adb + ['shell','kill',pid], check=False)
                root.wait(timeout=10)
            root_log.close()
            target = args.output.parent / ('manager-fixture' if args.manager else 'shared-fixture'); target.mkdir(exist_ok=True)
            subprocess.run(adb + ['pull',root_base + '/.',str(target) + '/'], check=True, stdout=subprocess.DEVNULL)
        if bridge is not None:
            bridge.close()
        server.shutdown()
        worker.join(5)
