#!/usr/bin/env python3
"""Record a real-device test session from the Android app's management API (read-only).

Polls /api/android/settings every second and writes each change of receiver status, CarPlay session and cable
state, with the time, to timeline.jsonl. On Ctrl-C (or --seconds) it saves the drive log (/api/android/logs) and
the diagnostics. Nothing is changed on the device. The admin credential is read from DASHPLAY_ADMIN_TOKEN or
--token-file and never written to the output.

  DASHPLAY_ADMIN_TOKEN=... python3 android/scripts/device-session-record.py https://192.0.2.10:18743 OUT_DIR
"""
import argparse
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument("base")
parser.add_argument("out", type=pathlib.Path)
parser.add_argument("--token-file", type=pathlib.Path)
parser.add_argument("--seconds", type=float, default=0, help="stop after this long (0: until Ctrl-C)")
args = parser.parse_args()
token = (args.token_file.read_text().strip() if args.token_file else os.environ.get("DASHPLAY_ADMIN_TOKEN", "")).strip()
if len(token) != 32:
    sys.exit("set DASHPLAY_ADMIN_TOKEN (32 characters, from the manage link after #token=) or --token-file")
base = args.base.rstrip("/")
context = ssl._create_unverified_context()   # the device's own certificate
args.out.mkdir(parents=True, exist_ok=True)


def get(path):
    request = urllib.request.Request(base + path, headers={"X-DashPlay-Admin": token, "Origin": base,
                                                           "Sec-Fetch-Site": "same-origin"})
    with urllib.request.urlopen(request, context=context, timeout=5) as response:
        return response.read()


def view(state):
    usb = state.get("usb", {})
    return {"status": state.get("status"), "session": state.get("sessionActive"), "usb": usb.get("state"),
            "usbMessage": usb.get("message"), "usbInternet": usb.get("internetReady"),
            "hotspot": state.get("localHotspot", {}).get("state")}


def stamp():
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


started, last, errors = time.monotonic(), None, 0
timeline = (args.out / "timeline.jsonl").open("a")
print(f"recording {base} -> {args.out} (Ctrl-C to stop)")
try:
    while not args.seconds or time.monotonic() - started < args.seconds:
        try:
            now = view(json.loads(get("/api/android/settings")))
            errors = 0
        except (OSError, ValueError, urllib.error.URLError) as error:
            errors += 1
            now = {"unreachable": type(error).__name__}
        if now != last:
            line = {"time": stamp(), **now}
            timeline.write(json.dumps(line, ensure_ascii=False) + "\n"); timeline.flush()
            print(" ".join(f"{k}={v}" for k, v in line.items()))
            last = now
        time.sleep(1)
except KeyboardInterrupt:
    pass
finally:
    timeline.close()
    for path, name in (("/api/android/logs", "drive-log.txt"), ("/api/android/diagnostics", "diagnostics.json")):
        try:
            (args.out / name).write_bytes(get(path))
            print(f"saved {name}")
        except (OSError, urllib.error.URLError) as error:
            print(f"could not save {name}: {error}")
