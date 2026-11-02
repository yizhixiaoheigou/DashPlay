#!/usr/bin/env python3
"""USB settings UI check on a disposable emulator, never on a user's device.

Proves denied consent leaves preferences unchanged and retry remains available.
No iPhone/media/USB traffic is simulated or claimed as real CarPlay acceptance.
"""
import argparse
import pathlib
import re
import subprocess
import time
import xml.etree.ElementTree as ET

p = argparse.ArgumentParser()
p.add_argument('--adb', required=True)
p.add_argument('--serial', required=True)
p.add_argument('--screens', type=pathlib.Path, required=True)
a = p.parse_args()
assert a.serial.startswith('emulator-'), 'This test only permits disposable emulators'
adb = [a.adb, '-P', '5039', '-s', a.serial]
a.screens.mkdir(parents=True, exist_ok=True)


def run(*parts):
    return subprocess.check_output(adb + list(parts))


def tree():
    for _ in range(3):
        dump = run('shell', 'uiautomator', 'dump', '/data/local/tmp/dashplay-usb-ui.xml')
        data = run('exec-out', 'cat', '/data/local/tmp/dashplay-usb-ui.xml')
        try:
            parsed = ET.fromstring(data)
            a.screens.joinpath("usb-ui.xml").write_bytes(data)
            return parsed
        except ET.ParseError:
            time.sleep(.5)
    raise AssertionError('UI dump failed: ' + dump.decode(errors='replace')[:200])


def find(rid):
    return next((n for n in tree().iter('node') if n.get('resource-id') == rid), None)


def click(node):
    assert node is not None, 'control missing'
    x1, y1, x2, y2 = map(int, re.findall(r'\d+', node.get('bounds')))
    run('shell', 'input', 'tap', str((x1+x2)//2), str((y1+y2)//2))
    time.sleep(.4)


def pref(key='usb_enabled'):
    data = ET.fromstring(run('exec-out', 'run-as', 'com.dashplay.android', 'cat', 'shared_prefs/dashplay.xml'))
    return next((n.get('value') == 'true' for n in data if n.get('name') == key), False)


run('shell', 'am', 'start', '-f', '0x04000000', '-n', 'com.dashplay.android/.MainActivity')
time.sleep(.5)
click(find('com.dashplay.android:id/settings_button'))
for _ in range(7):
    row = find('com.dashplay.android:id/row_tesla_access')
    if row is not None:
        click(row)
        break
    run('shell', 'input', 'swipe', '500', '1700', '500', '600', '250')
else:
    raise AssertionError('Tesla access entry missing')
assert find('com.dashplay.android:id/tesla_root_enable') is not None
checks = find('com.dashplay.android:id/tesla_offline_checks')
assert checks is not None and checks.get('enabled') == 'false'
assert not pref('tesla_offline_checks')
assert not pref('tesla_root_access'), 'Root mode must remain off without explicit opt-in'
a.screens.joinpath('tesla-root-settings.png').write_bytes(run('exec-out', 'screencap', '-p'))
print('TESLA_UI_OK native entry and Root disabled by default; no grant simulated')
run('shell', 'input', 'keyevent', '4')
for _ in range(7):
    row = find('com.dashplay.android:id/row_tls')
    if row is not None:
        click(row)
        break
    run('shell', 'input', 'swipe', '500', '1700', '500', '600', '250')
else:
    raise AssertionError('own TLS settings entry missing')
click(find('com.dashplay.android:id/tls_automatic'))
assert find('com.dashplay.android:id/auto_tls_enabled') is not None
assert find('com.dashplay.android:id/auto_tls_domain') is not None
a.screens.joinpath('automatic-tls-settings.png').write_bytes(run('exec-out', 'screencap', '-p'))
for _ in range(6):
    if find('com.dashplay.android:id/auto_tls_save') is not None:
        break
    run('shell', 'input', 'swipe', '500', '1700', '500', '600', '250')
else:
    raise AssertionError('automatic TLS save action missing')
a.screens.joinpath('automatic-tls-actions.png').write_bytes(run('exec-out', 'screencap', '-p'))
print('AUTO_TLS_UI_OK native settings and reachable controls; no public configuration enabled')
run('shell', 'input', 'keyevent', '4')
assert find('com.dashplay.android:id/tls_chain') is not None
for _ in range(5):
    if find('com.dashplay.android:id/tls_import') is not None:
        break
    run('shell', 'input', 'swipe', '500', '1400', '500', '600', '250')
else:
    raise AssertionError('own TLS import action missing')
a.screens.joinpath('own-tls-settings.png').write_bytes(run('exec-out', 'screencap', '-p'))
print('OWN_TLS_UI_OK native file import and enable/disable entry')
run('shell', 'input', 'keyevent', '4')
for _ in range(7):
    row = find('com.dashplay.android:id/row_phones')
    if row is not None:
        click(row)
        break
    run('shell', 'input', 'swipe', '500', '1700', '500', '600', '250')
else:
    raise AssertionError('phone management entry missing')
assert find('com.dashplay.android:id/add_iphone') is not None
texts = [n.get('text', '') for n in tree().iter('node')]
expected = '尚未添加 iPhone'
if int(run('shell', 'getprop', 'ro.build.version.sdk').strip()) == 21:
    expected = expected.encode('ascii', errors='replace').decode()
assert expected in texts, repr(texts)
a.screens.joinpath('phones-empty.png').write_bytes(run('exec-out', 'screencap', '-p'))
print('PHONES_UI_OK empty state and add-device entry')
run('shell', 'input', 'keyevent', '4')
for _ in range(7):
    row = find('com.dashplay.android:id/row_usb')
    if row is not None:
        click(row)
        break
    run('shell', 'input', 'swipe', '500', '1700', '500', '600', '250')
else:
    raise AssertionError('USB settings entry missing')
button = find('com.dashplay.android:id/usb_enable')
assert button is not None
assert not pref(), 'default USB option must be off'
a.screens.joinpath('usb-settings.png').write_bytes(run('exec-out', 'screencap', '-p'))
if button.get('enabled') == 'false':
    texts = [n.get('text', '') for n in tree().iter('node')]
    expected = '此设备不支持 USB Host，请使用无线连接。'
    # API 21's uiautomator process serializes Chinese as '?', although the app renders it correctly.
    # Compare its exact encoded output; retain the screenshot for the actual Chinese rendering.
    if int(run('shell', 'getprop', 'ro.build.version.sdk').strip()) == 21:
        expected = expected.encode('ascii', errors='replace').decode()
    assert expected in texts, repr(texts)
    print('USB_UI_OK unsupported host: disabled control and explanation')
else:
    click(button)
    cancel = find('android:id/button2')
    assert cancel is not None, 'expected system VPN consent before enabling'
    click(cancel)
    assert not pref(), 'denying VPN consent changed USB preference'
    assert any('取消了 VPN 授权' in n.get('text', '') for n in tree().iter('node'))
    a.screens.joinpath('usb-consent-cancelled.png').write_bytes(run('exec-out', 'screencap', '-p'))
    click(find('com.dashplay.android:id/usb_enable'))
    assert find('android:id/button2') is not None, 'retry must reopen consent'
    click(find('android:id/button2'))
    assert not pref()
    print('USB_UI_OK VPN cancelled twice: no setting change, retry available')
assert not pref('usb_internet'), 'Internet sharing must be off by default'
for _ in range(4):
    internet = find('com.dashplay.android:id/usb_internet')
    if internet is not None:
        assert internet.get('checked') == 'false' and internet.get('enabled') == 'false'
        break
    run('shell', 'input', 'swipe', '500', '1600', '500', '650', '250')
else:
    raise AssertionError('USB Internet setting missing')
a.screens.joinpath('usb-internet-settings.png').write_bytes(run('exec-out', 'screencap', '-p'))
print('USB_INTERNET_UI_OK default off; requires local USB/VPN opt-in')
assert not pref('usb_hotspot_share'), 'Root hotspot sharing must be off by default'
for _ in range(6):
    shared = find('com.dashplay.android:id/usb_hotspot_share')
    if shared is not None:
        assert shared.get('checked') == 'false' and shared.get('enabled') == 'false'
        break
    run('shell', 'input', 'swipe', '500', '1600', '500', '650', '250')
else:
    raise AssertionError('Root hotspot share setting missing')
a.screens.joinpath('usb-hotspot-share-settings.png').write_bytes(run('exec-out', 'screencap', '-p'))
print('USB_HOTSPOT_SHARE_UI_OK default off; local opt-in required')
run('shell', 'input', 'keyevent', '4')
