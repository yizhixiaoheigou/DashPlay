#!/usr/bin/env python3
"""Authenticated management integration test on an explicitly selected debug device/emulator.

Reads the debug app's credential through run-as without printing it. Restores all edited settings.
No system Wi-Fi, Bluetooth pairing, VPN, or root settings are changed.
"""
import argparse
import hashlib
import io
import json
import pathlib
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--adb", required=True)
parser.add_argument("--serial", required=True)
parser.add_argument("--adb-port", default="5039")
parser.add_argument("--base", default="http://127.0.0.1:18888")
parser.add_argument("--screens", type=pathlib.Path)
parser.add_argument("--browser-only", action="store_true")
args = parser.parse_args()
adb = [args.adb, "-P", args.adb_port, "-s", args.serial]
base = args.base.rstrip("/")
context = ssl._create_unverified_context()


def private(path):
    result = subprocess.run(adb + ["exec-out", "run-as", "com.dashplay.android", "cat", path],
                            capture_output=True, check=True)
    return result.stdout


def request(path, data=None, token=None, origin=base, content_length=None):
    headers = {"Origin": origin, "Sec-Fetch-Site": "same-origin"}
    if token is not None:
        headers["X-DashPlay-Admin"] = token
    if isinstance(data, dict):
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    req = urllib.request.Request(base + path, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, context=context, timeout=10) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


# Wait for this installed generation's listener, then create its admin credential through a denied request.
deadline = time.monotonic() + 60
while True:
    try:
        if request("/api/android/settings", token="0" * 32)[0] == 403:
            break
    except OSError:
        pass
    if time.monotonic() >= deadline:
        raise AssertionError("management listener did not become ready")
    time.sleep(0.5)
preferences = ET.fromstring(private("shared_prefs/dashplay.xml"))
values = {item.attrib["name"]: item.text for item in preferences.findall("string")}
token = values["admin_token"]
viewer = values["access_token"]
assert token != viewer


def state():
    code, body, _ = request("/api/android/settings", token=token)
    assert code == 200, "management state unavailable"
    return json.loads(body)


def wait_name(name):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            code, body, _ = request("/api/status")
            if code == 200 and json.loads(body)["deviceName"] == name:
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise AssertionError("receiver did not apply management settings")


if not args.browser_only:
    initial = state()
    original = initial["settings"]
    assert isinstance(original["usbInternet"], bool)
    assert isinstance(original["usbHotspotShare"], bool)
    assert "passphrase" not in original and "token" not in original
    for credential in (None, viewer, "f" * 32):
        for endpoint in ("settings", "diagnostics", "logs"):
            assert request("/api/android/" + endpoint, token=credential)[0] == 403
        assert request("/api/android/settings", {"deviceName": "unauthorized"}, token=credential)[0] == 403
        assert request("/api/android/tesla/disable", {}, token=credential)[0] == 403
    # The drive log: written by the running service, downloadable with the admin credential, no credentials inside.
    code, log, headers = request("/api/android/logs", token=token)
    assert code == 200 and "attachment" in headers["Content-Disposition"], "drive log not downloadable"
    assert b"[android] service start" in log and b"AirPlay" in log, "drive log missing service/receiver lines"
    assert token.encode() not in log and viewer.encode() not in log, "drive log leaks a credential"
    if args.screens:
        (args.screens / "drive-log.txt").write_bytes(log)
    print(f"drive_log=ok bytes={len(log)}")
    phone_state = json.loads(request("/api/android/phones", token=token)[1])
    assert isinstance(phone_state["phones"], list)
    for credential in (None, viewer, "f" * 32):
        assert request("/api/android/phones", token=credential)[0] == 403
        assert request("/api/android/phones/action", {"address":"02:00:00:00:00:FE", "action":"disconnect"}, token=credential)[0] == 403
    assert request("/api/android/phones", token=token, origin="https://other.invalid")[0] == 403
    for op in ("connect", "disconnect", "remove", "unsupported"):
        assert request("/api/android/phones/action", {"address":"invalid", "action":op}, token=token)[0] == 409
    assert request("/api/android/phones/action", {"address":123,"action":"connect"}, token=token)[0] == 400
    assert json.loads(request("/api/android/phones", token=token)[1]) == phone_state
    assert request("/api/android/settings", token=token, origin="https://other.invalid")[0] == 403
    assert request("/api/android/settings", {"deviceName": "foreign"}, token=token,
                   origin="https://other.invalid")[0] == 403
    public = request("/receiver-access.json")[1]
    assert token.encode() not in public and json.loads(public)["settingsPath"] == "/manage"
    code, page, headers = request("/manage")
    assert code == 200 and token.encode() not in page and b"app.js" in page
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    invalid = [
        {"deviceName": "must-not-save", "idleReleaseSeconds": -1},
        {"deviceName": "must-not-save", "ssid": "x", "passphrase": "short", "wifiSecurity": "WPA2"},
        {"deviceName": "must-not-save", "wifiChannel": 999},
        {"deviceName": "x" * 33},
        {"deviceName": "must-not-save", "idleReleaseSeconds": "not-a-number"},
        {"unknown": "secret-must-not-echo"},
    ]
    if not initial["usb"].get("internetAllowed", False):
        invalid.append({"deviceName": "must-not-save", "usbInternet": True})
    if not initial["usbShare"].get("allowed", False):
        invalid.append({"deviceName": "must-not-save", "usbHotspotShare": True})
    if not initial["capabilities"].get("managedHotspot", False):
        invalid.append({"deviceName": "must-not-save", "autoHotspot": True, "network": "THIS_HOTSPOT"})
    for update in invalid:
        code, body, _ = request("/api/android/settings", update, token=token)
        assert code == 400 and b"secret-must-not-echo" not in body
        assert state()["settings"] == original, "invalid update partially changed settings"
    assert request("/api/android/settings", b" " * 8193, token=token)[0] == 413
    assert state()["settings"] == original

    try:
        changed = {"deviceName": "DashPlay management test", "idleReleaseSeconds": 42}
        assert request("/api/android/settings", changed, token=token)[0] == 200
        wait_name(changed["deviceName"])
        assert state()["settings"]["idleReleaseSeconds"] == 42
        persisted = ET.fromstring(private("shared_prefs/dashplay.xml"))
        assert next(x for x in persisted if x.attrib["name"] == "idle_release").attrib["value"] == "42"
    finally:
        assert request("/api/android/settings", {"deviceName": original["deviceName"],
                       "idleReleaseSeconds": original["idleReleaseSeconds"]}, token=token)[0] == 200
        wait_name(original["deviceName"])

    if initial["identity"]:
        identity = {name: private("files/identity/offline-mfi/" + name)
                    for name in ("identity.pk8", "certificate.p7b")}
        assert request("/api/android/identity", b"not an identity", token=token)[0] == 400
        for name, value in identity.items():
            assert hashlib.sha256(private("files/identity/offline-mfi/" + name)).digest() == hashlib.sha256(value).digest()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zip:
            for name, value in identity.items():
                zip.writestr(name, value)
        assert request("/api/android/identity", archive.getvalue(), token=viewer)[0] == 403
        assert request("/api/android/identity", b"", token=token, content_length=32 * 1024 * 1024 + 1)[0] == 413
        assert request("/api/android/identity", archive.getvalue(), token=token)[0] == 200
        time.sleep(3)
        wait_name(original["deviceName"])
        assert state()["identity"]
        for name, value in identity.items():
            assert private("files/identity/offline-mfi/" + name) == value

    code, diagnostic, _ = request("/api/android/diagnostics", token=token)
    assert code == 200 and token.encode() not in diagnostic and viewer.encode() not in diagnostic
    assert b'"passphrase"' not in diagnostic
    assert state()["settings"] == original
    print("MANAGEMENT_OK: separate authorization, atomic validation/save, live restart, identity import/rollback, redacted diagnostics")

if args.screens:
    from playwright.sync_api import sync_playwright
    args.screens.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        browser_context = browser.new_context(ignore_https_errors=True, viewport={"width": 375, "height": 812})
        page = browser_context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/manage")
        page.locator("#admin-link").fill(token)
        page.get_by_role("button", name="连接", exact=True).click()
        page.locator("#content").wait_for(state="visible")
        if not state()["usb"].get("internetAllowed", False):
            assert page.locator("#usbHotspotShare").is_disabled()
            assert "Root" in page.locator("#usb-share-state").inner_text()
            assert page.locator("#usbInternet").is_disabled()
            assert "安卓设备" in page.locator("#usb-internet-state").inner_text()
        page.screenshot(path=str(args.screens / "management-phone.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), "phone page overflows"
        original_name = page.locator("#deviceName").input_value()
        page.locator("#deviceName").fill("Unsubmitted draft")
        page.get_by_role("button", name="刷新状态", exact=True).click()
        page.wait_for_timeout(300)
        assert page.locator("#deviceName").input_value() == "Unsubmitted draft", "refresh lost unsaved input"
        page.locator("#deviceName").fill(original_name)
        page.set_viewport_size({"width": 1180, "height": 820})
        page.emulate_media(color_scheme="dark", reduced_motion="reduce")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(args.screens / "management-desktop-dark.png"), full_page=True)
        page.get_by_role("button", name="退出管理", exact=True).click()
        assert page.locator("#login").is_visible() and page.locator("#content").is_hidden()
        assert not errors, "management page JavaScript errors"
        browser.close()
    print("BROWSER_OK: phone/desktop, dark mode, draft preservation, login/logout")
