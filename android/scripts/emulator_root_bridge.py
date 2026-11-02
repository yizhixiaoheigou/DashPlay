"""Test-only execution bridge, absent from the APK. Never accepts a physical device."""
import pathlib
import subprocess
import threading
import time


class EmulatorRootBridge:
    def __init__(self, adb, output, requests='requests'):
        assert any(str(part).startswith('emulator-') for part in adb)
        assert subprocess.check_output(adb + ['shell', 'getprop', 'ro.kernel.qemu']).strip() == b'1'
        assert subprocess.check_output(adb + ['shell', 'id', '-u']).strip() == b'0'
        self.adb = adb
        assert requests in ('requests', 'recovery-requests')
        self.requests = requests
        directory = pathlib.Path(output)
        directory.mkdir(exist_ok=True)
        self.output = directory / str(time.time_ns())
        self.output.mkdir()
        (directory/'current-run').write_text(self.output.name)
        self.stopped = threading.Event()
        self.errors = []
        self.worker = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.worker.start()

    def run(self):
        sequence = 1
        try:
            while not self.stopped.is_set():
                base = f'cache/usb-share-manager/{self.requests}/{sequence}'
                exists = subprocess.run(self.adb + ['shell', 'run-as', 'com.dashplay.android', 'test', '-f', base + '.ready'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if exists.returncode:
                    self.stopped.wait(.05)
                    continue
                script = subprocess.check_output(self.adb + ['exec-out', 'run-as', 'com.dashplay.android', 'cat', base + '.script'])
                (self.output/f'{sequence:03}.sh').write_bytes(script)
                remote = '/data/local/tmp/dashplay-share-manager/root-request.sh'
                subprocess.run(self.adb + ['shell', 'sh', '-c', f"'cat > {remote}'"], input=script, check=True)
                try:
                    result = subprocess.run(self.adb + ['shell', 'sh', remote], stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, timeout=35)
                    response = str(result.returncode).encode() + b'\n' + result.stdout
                except subprocess.TimeoutExpired as error:
                    response = b'124\n' + (error.stdout or b'')
                (self.output/f'{sequence:03}.response').write_bytes(response)
                command = f'cat > {base}.tmp && mv {base}.tmp {base}.response'
                subprocess.run(self.adb + ['shell', 'run-as', 'com.dashplay.android', 'sh', '-c', "'" + command + "'"], input=response, check=True)
                sequence += 1
        except Exception as error:
            self.errors.append(str(error))

    def close(self):
        self.stopped.set()
        self.worker.join(40)
        assert not self.worker.is_alive(), 'test Root bridge did not finish'
        assert not self.errors, self.errors
