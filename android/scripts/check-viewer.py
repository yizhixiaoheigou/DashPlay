#!/usr/bin/env python3
"""Exercise the APK's real HTTPS API through adb forward, without logging its token."""
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")
context = ssl._create_unverified_context()  # Emulator's generated LAN certificate.


def request(path, body=None, origin=None):
    headers = {"Sec-Fetch-Site": "same-origin", "Origin": origin or base}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, headers=headers,
                                 data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, context=context, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, None


status, flags = request("/receiver-access.json")
assert status == 200 and len(flags["token"]) == 32, "viewer bootstrap failed"
token = flags["token"]
original = flags["audioRoutes"]
# N1 baseline (scripts/onecloud/audio_routes.py): five destinations, alert always stays in CarPlay, and the
# other four share one output. A wired-only install with no Tesla mode therefore starts all in CarPlay.
assert set(original) == {"media", "navigation", "alert", "siri", "calls"}, "shared output missing"
assert original["alert"] == "carplay", "alert must stay in CarPlay"
assert len({original[name] for name in original if name != "alert"}) == 1, "shared output missing"
assert request("/receiver-access.json", origin="https://foreign.invalid")[0] == 403
assert request("/api/audio-routes?token=invalid")[0] == 403
path = "/api/audio-routes?token=" + token
assert request(path)[1] == {"supported": True, "routes": original}
assert request(path, {"category": "navigation", "destination": "invalid"})[0] == 400
assert request(path, {"category": "media", "destination": "bluetooth"})[0] == 409
assert request(path)[1]["routes"] == original, "legacy category unexpectedly changed output"
assert request(path, {"category": "navigation", "destination": "bluetooth"},
               origin="https://foreign.invalid")[0] == 403


def change(destination):
    status, value = request(path, {"category": "navigation", "destination": destination})
    # Not in Tesla mode here, so every destination except the alert follows the owner's switch.
    wanted = {name: ("carplay" if name == "alert" else destination) for name in original}
    assert status == 200 and value["routes"] == wanted, "sound output not saved"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            _, current = request("/receiver-access.json")
            if current["audioRoutes"] == wanted and request(path)[1]["supported"]:
                assert current["token"] == token, "bookmark token changed on restart"
                assert current.get("tesla") == flags.get("tesla"), "sound output changed Tesla mode"
                return
        except (OSError, ValueError, TypeError, KeyError):
            pass
        time.sleep(0.5)
    raise AssertionError("receiver did not apply sound output and recover HTTPS")


try:
    change("bluetooth" if original["navigation"] == "carplay" else "carplay")
finally:
    change(original["navigation"])
print("VIEWER_OK: same-origin authorization, shared sound output, two service restarts, stable bookmark")
