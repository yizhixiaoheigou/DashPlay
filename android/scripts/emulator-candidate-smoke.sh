#!/bin/sh
# Install the exact release candidate on a disposable emulator without granting permissions or importing identity.
# Usage: sh scripts/emulator-candidate-smoke.sh API APK OUTPUT_DIR
set -eu
api=${1:?API required}
candidate=${2:?APK required}
out=${3:?output directory required}
[ -f "$candidate" ] || { echo "candidate missing" >&2; exit 2; }
. "$(dirname -- "$0")/emulator-lib.sh"
apk=$candidate
[ "$("$adb" -s "$serial" shell getprop ro.kernel.qemu | tr -d '\r')" = 1 ]
shasum -a 256 "$apk" > "$out/apk.sha256"
"$adb" -s "$serial" install "$apk" > "$out/install.txt"
"$adb" -s "$serial" shell am start -n com.dashplay.android/.MainActivity > "$out/start.txt"
wait_for 90 "first setup HTTP entry" logged "page on"
# adb forward targets guest loopback, while BrowserFront binds the selected LAN address.
# Read the actual listener and send HTTP on-device; do not widen app listening for a test.
python3 - "$adb" "$serial" "$out" <<'PYHTTP'
import pathlib,re,subprocess,sys,select,time,os
adb,serial,out=sys.argv[1:]; out=pathlib.Path(out)
command=[adb,'-P','5039','-s',serial]
log=subprocess.check_output(command+['logcat','-d','-s','DashPlay:I']).decode()
address,port=re.findall(r'setup page on http://([0-9.]+):(\d+)/manage',log)[-1]
request=f'GET /manage HTTP/1.1\r\nHost: {address}:{port}\r\nConnection: close\r\n\r\n'.encode()
process=subprocess.Popen(command+['shell','nc','-w','10',address,port],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
try:
    process.stdin.write(request); process.stdin.flush()
    # Keep stdin open: netcat can close the socket at EOF before HTTP has answered.
    response=b''; deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if not select.select([process.stdout],[],[],1)[0]: continue
        chunk=os.read(process.stdout.fileno(),65536)
        if not chunk: break
        response+=chunk
        if b'</html>' in response: break
    (out/'manage-response.txt').write_bytes(response)
    # API 21 shell uses a PTY and echoes the request; match the actual response status line.
    status=re.search(rb'(?:^|\n)HTTP/1\.[01] ([0-9]{3})[^\n]*\n',response)
    assert status and status[1] == b'200',response[:200]
    assert b'<html' in response
finally:
    process.stdin.close()
    try: process.wait(timeout=3)
    except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=3)
    (out/'manage-errors.txt').write_bytes(process.stderr.read())
PYHTTP
"$adb" -s "$serial" shell dumpsys package com.dashplay.android > "$out/package.txt"
"$adb" -s "$serial" shell uiautomator dump /data/local/tmp/candidate-ui.xml > "$out/ui-dump.txt"
"$adb" -s "$serial" exec-out cat /data/local/tmp/candidate-ui.xml > "$out/ui.xml"
"$adb" -s "$serial" logcat -d > "$out/logcat.txt"
# A process-specific crash marker is emitted with the package name by AndroidRuntime.
if grep -q 'Process: com.dashplay.android' "$out/logcat.txt"; then
    echo 'CANDIDATE_SMOKE_FAIL application crash'; exit 1
fi
echo "CANDIDATE_SMOKE_OK api=$api fresh_install=true http_manage=200 identity_imported=false permissions_pregranted=false"
