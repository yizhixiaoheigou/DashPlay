#!/usr/bin/env python3
"""Real management-page interactions against explicit local API fixtures; no receiver/device access."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

assets = Path(__file__).resolve().parents[1] / 'app/src/main/assets/manage'
fixture = {'status': 'STOPPED', 'sessionActive': False, 'viewers': 0, 'version': 'test',
           'capabilities': {'model': 'fixture', 'android': 'test'}, 'settings': {
    'deviceName': 'Wi-Fi test', 'car': 'OTHER', 'network': 'MIFI', 'iphoneLink': 'WIRELESS',
    'externalWifiChoice': 'OTHER', 'currentWifiSsid': 'Current fixture', 'currentWifiPasswordSaved': False,
    'ssid': '', 'uplinkSsid': 'External fixture', 'hasPassword': False,
    'hasUplinkPassword': True, 'wifiSecurity': 'WPA3', 'localWifiSecurity': 'OPEN',
    'uplinkWifiSecurity': 'WPA3', 'wifiChannel': 0, 'idleReleaseSeconds': 300,
    'autoHotspot': False, 'audioOutsideCarPlay': False, 'setupDone': True, 'startAtBoot': True}}
posts = []
errors = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel='chrome', headless=True)
    page = browser.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    def serve(route):
        path = route.request.url.split('https://dashplay.test', 1)[1]
        if path.startswith('/api/android/'):
            if route.request.method == 'POST':
                posts.append(route.request.post_data_json)
            route.fulfill(json=fixture if path.endswith('/settings') else {'phones': []})
        else:
            name = 'index.html' if path == '/manage' else path.rsplit('/', 1)[-1]
            file = assets / name
            if file.is_file(): route.fulfill(path=str(file))
            else: route.fulfill(status=404, body='')
    page.route('https://dashplay.test/**', serve)
    page.goto('https://dashplay.test/manage')
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    assert page.locator('#browserPort').input_value() == '18743'
    assert page.locator('#wifiSecurity').input_value() == 'WPA3'
    assert page.locator('#wifiChannel').input_value() == '0'
    assert page.locator('#ssid').is_disabled()
    page.locator('#network').select_option('THIS_HOTSPOT')
    assert page.locator('#wifiSecurity').input_value() == 'OPEN'
    page.locator('#wifiSecurity').select_option('WPA2')
    page.locator('#network').select_option('MIFI')
    assert page.locator('#wifiSecurity').input_value() == 'WPA3'
    page.locator('#network').select_option('THIS_HOTSPOT')
    assert page.locator('#wifiSecurity').input_value() == 'WPA2'
    page.locator('#network').select_option('MIFI')
    # Refreshing status cannot erase the user's unsaved security choice.
    page.locator('#wifiSecurity').select_option('OPEN')
    page.evaluate('(fixture) => render(fixture, false)', fixture)
    assert page.locator('#wifiSecurity').input_value() == 'OPEN'
    page.locator('#settings-form').evaluate('(form) => form.requestSubmit()')
    page.wait_for_function('!working')
    assert len(posts) == 1, 'form did not submit'
    assert posts[0]['uplinkSsid'] == 'External fixture' and posts[0]['uplinkPassphrase'] == ''
    assert posts[0]['wifiSecurity'] == 'OPEN' and 'ssid' not in posts[0] and 'passphrase' not in posts[0]
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    page.locator('#uplinkPassphrase').fill('old-password')
    page.locator('#externalWifiChoice').select_option('CURRENT')
    assert page.locator('#uplinkSsid').input_value() == 'Current fixture'
    assert page.locator('#uplinkSsid').evaluate('(e) => e.readOnly')
    assert page.locator('#uplinkPassphrase').input_value() == ''
    page.locator('#externalWifiChoice').select_option('OTHER')
    page.locator('#uplinkPassphrase').fill('old-password')
    page.locator('#uplinkSsid').fill('Different fixture')
    assert page.locator('#uplinkPassphrase').input_value() == ''
    pending = json.loads(json.dumps(fixture))
    pending['settings']['usbInternet'] = False
    pending['settings']['usbInternetRequested'] = True
    page.evaluate('(fixture) => render(fixture, true)', pending)
    assert page.locator('#usbInternet').is_checked() and page.locator('#usbInternet').is_disabled()
    assert '等待' in page.locator('#usb-internet-state').inner_text()
    # Hardware absence blocks only the unsupported transport. Power/permission are separate.
    fixture['settings']['iphoneLink']='ANY'
    fixture['capabilities'].update(usbHost=True, bluetooth=False, bluetoothPermission=False, bluetoothEnabled=False)
    page.evaluate('(fixture) => render(fixture, true)',fixture)
    assert page.locator('#iphoneLink option[value="WIRELESS"]').is_disabled(), 'no Bluetooth must disable wireless'
    assert page.locator('#iphoneLink option[value="WIRED"]').is_enabled(), 'no Bluetooth must preserve wired'
    assert page.locator('#iphoneLink option[value="ANY"]').is_enabled()
    assert page.locator('#wifi-fields').is_hidden(), 'wired-only AUTO must not ask for wireless credentials'
    assert '数据线' in page.locator('#connection-capability').inner_text()
    fixture['settings']['iphoneLink']='WIRELESS'
    page.evaluate('(fixture) => render(fixture, true)',fixture)
    assert page.locator('#iphoneLink').input_value()=='WIRELESS', 'hardware observation must not rewrite a saved choice'
    assert page.locator('#iphoneLink').evaluate('(e)=>!e.checkValidity()')
    page.locator('#iphoneLink').select_option('WIRED')
    page.locator('#settings-form').evaluate('(form) => form.requestSubmit()')
    page.wait_for_function('!working')
    assert len(posts)==2 and posts[-1]['iphoneLink']=='WIRED', 'no-Bluetooth wired configuration must submit'
    fixture['settings']['iphoneLink']='ANY'
    fixture['capabilities']['bluetooth']=True
    page.evaluate('(fixture) => render(fixture, true)',fixture)
    assert page.locator('#iphoneLink option[value="WIRELESS"]').is_enabled(), 'permission denial is not hardware absence'
    assert '权限' in page.locator('#connection-capability').inner_text()
    fixture['capabilities']['bluetoothPermission']=True
    page.evaluate('(fixture) => render(fixture, true)',fixture)
    assert page.locator('#iphoneLink option[value="WIRELESS"]').is_enabled(), 'Bluetooth off is not hardware absence'
    assert '关闭' in page.locator('#connection-capability').inner_text()
    fixture['capabilities'].update(usbHost=False,bluetooth=False)
    page.evaluate('(fixture) => render(fixture, true)',fixture)
    for choice in ['ANY','WIRELESS','WIRED']: assert page.locator('#iphoneLink option[value="'+choice+'"]').is_disabled()
    assert page.locator('#iphoneLink').evaluate('(e)=>!e.checkValidity()')
    fixture['capabilities'].pop('bluetooth');fixture['capabilities']['usbHost']=True
    page.evaluate('(fixture) => render(fixture, true)',fixture)
    assert page.locator('#iphoneLink option[value="WIRED"]').is_enabled()
    assert '尚未确认' in page.locator('#connection-capability').inner_text()
    assert '无蓝牙硬件' not in page.locator('#capabilities').inner_text()
    # Tesla mode must disable car hotspot in the browser form too.
    page.locator('#car').select_option('TESLA')
    assert page.locator('#network option[value="CAR_HOTSPOT"]').is_disabled()
    page.locator('#car').select_option('OTHER')
    assert page.locator('#network option[value="CAR_HOTSPOT"]').is_enabled()
    assert '实验性' in page.content()
    assert not errors, errors
    fixture['ownTls'] = {'message': 'fixture certificate state', 'directAddress': '192.0.2.20:18743', 'dnsTarget': '192.0.2.20'}
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    assert 'http://192.0.2.20:18743/manage' in page.locator('#tls-target').inner_text(), 'certificate recovery must use HTTP management'
    fixture['settings']['browserPort'] = 18743
    fixture['settings']['iphoneLink'] = 'WIRED'
    fixture['capabilities']['usbHost'] = True
    fixture['viewerUrl'] = 'http://dashplay.test:18743/'
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    assert page.locator('#viewer-link').get_attribute('href') == fixture['viewerUrl']
    page.locator('#browserPort').fill('19443')
    page.locator('#settings-form').evaluate('(form) => form.requestSubmit()')
    page.wait_for_function("!document.getElementById('saved-address').hidden")
    assert posts[-1]['browserPort'] == 19443
    assert page.locator('#saved-address').get_attribute('href') == 'http://dashplay.test:19443/manage'
    assert 'http://' in page.locator('#notice').inner_text()
    # Phone product category is wireless-only even when USB Host is present.
    fixture['settings'].update(deviceKind='PHONE', iphoneLink='WIRELESS', network='MIFI')
    fixture['capabilities'].update(bluetooth=True, bluetoothPermission=True, bluetoothEnabled=True, usbHost=True)
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    for choice in ['ANY', 'WIRED']:
        option = page.locator('#iphoneLink option[value="'+choice+'"]')
        assert option.is_disabled() and option.get_attribute('hidden') is not None
    assert page.locator('#iphoneLink option[value="WIRELESS"]').is_enabled()
    assert page.locator('#network option[value="CAR_HOTSPOT"]').is_disabled()
    assert page.locator('#wired-consent-help').is_hidden()
    assert '仅支持无线' in page.locator('#device-policy').inner_text()
    assert page.locator('#wifi-fields').is_visible()
    # Browser rejects stale/forged wired selection without sending a settings request.
    count = len(posts)
    page.locator('#iphoneLink').evaluate("e => { e.value = 'WIRED'; e.dispatchEvent(new Event('change')); }")
    page.locator('#settings-form').evaluate('(form) => form.requestSubmit()')
    assert len(posts) == count and page.locator('#iphoneLink').evaluate('(e)=>!e.checkValidity()')
    fixture['capabilities']['bluetooth'] = False
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    assert page.locator('#iphoneLink option[value="WIRELESS"]').is_disabled()
    assert '安卓手机不提供有线' in page.locator('#connection-capability').inner_text()
    # Reclassifying as a box restores its supported wired choices, including no-Bluetooth boxes.
    fixture['settings'].update(deviceKind='BOX', iphoneLink='WIRED')
    page.evaluate('(fixture) => render(fixture, true)', fixture)
    assert page.locator('#iphoneLink option[value="WIRED"]').is_enabled()
    assert page.locator('#iphoneLink option[value="WIRED"]').get_attribute('hidden') is None
    assert page.locator('#network option[value="CAR_HOTSPOT"]').is_enabled()
    assert not errors, errors
    browser.close()
print('WIFI_MANAGEMENT_OK: separate security drafts, refresh preservation, open external clears only its password')
