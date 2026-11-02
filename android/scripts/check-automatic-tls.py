#!/usr/bin/env python3
"""Emulator-only management checks. Only an emulator with no default network may enable fixture credentials."""
import argparse, json, pathlib, ssl, subprocess, time, urllib.request, urllib.error, xml.etree.ElementTree as ET
p=argparse.ArgumentParser();p.add_argument('--adb',required=True);p.add_argument('--serial',required=True)
p.add_argument('--base',required=True);p.add_argument('--screens',type=pathlib.Path,required=True);p.add_argument('--offline',action='store_true')
a=p.parse_args();adb=[a.adb,'-P','5039','-s',a.serial]
def run(*args):return subprocess.check_output(adb+list(args))
assert a.serial.startswith('emulator-') and run('shell','getprop','ro.kernel.qemu').strip()==b'1'
prefs=ET.fromstring(run('exec-out','run-as','com.dashplay.android','cat','shared_prefs/dashplay.xml'))
values={n.get('name'):n.text for n in prefs if n.tag=='string'};token=values['admin_token'];base=a.base.rstrip('/')
def api(path='tls/automatic',body=None,credential=token,origin=base):
 req=urllib.request.Request(base+'/api/android/'+path,data=None if body is None else json.dumps(body).encode(),
  headers={'X-DashPlay-Admin':credential,'Origin':origin,'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(req,context=ssl._create_unverified_context(),timeout=5) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.load(e)
def wait(fn):
 end=time.monotonic()+45
 while time.monotonic()<end:
  try:
   if fn():return
  except (OSError,ValueError):pass
  time.sleep(.3)
 raise AssertionError('automatic certificate state did not settle')
for credential in ['',values['access_token'],'f'*32]:
 for path,body in [('tls/automatic',None),('tls/automatic',{'enabled':False}),('tls/automatic/retry',{})]:
  assert api(path,body,credential=credential)[0]==403
assert api(body={'enabled':False},origin='https://elsewhere.invalid')[0]==403
assert api(body={'enabled':True,'token':'x'*8193})[0]==413
fixture={'enabled':True,'provider':'duckdns','domain':'dashplay-fixture.duckdns.org','token':'fixture-token-1234567890','termsAccepted':False}
code,error=api(body=fixture);assert code==400 and fixture['token'] not in json.dumps(error)
assert api(body=fixture|{'domain':'invalid','termsAccepted':True})[0]==400
assert api()[1]['enabled'] is False
assert api('settings')[1]['teslaAccess']['checksEnabled'] is False
assert api('tesla/checks', {'enabled': True})[0] == 400
assert api('tesla/checks', {'enabled': False}, credential='')[0] == 403
assert api('tesla/checks', {'enabled': False}, origin='https://elsewhere.invalid')[0] == 403
assert api('tesla/checks', {'enabled': 'false'})[0] == 400
assert api('tesla/checks', {'enabled': False, 'padding': 'x'*200})[0] == 413
if a.offline:
 # API 21 smoke provides an isolated local LAN, with radio data disabled. Never send fixture tokens to real providers.
 assert b'Active default network: none' in run('shell','dumpsys','connectivity')
 assert api(body=fixture|{'termsAccepted':True})[0]==200
 wait(lambda:api()[1]['phase']=='waiting_network')
 record=json.loads(run('exec-out','run-as','com.dashplay.android','cat','files/automatic-tls/automation.json'))
 assert record.get('failures',0)==0 and record['profile']['dns']['token']==fixture['token']
 run('shell','run-as','com.dashplay.android','test','!','-e','files/automatic-tls/production-account/account.pk8')
 for path in ['tls/automatic','tls','settings','diagnostics']:
  code,data=api(path);assert code==200 and fixture['token'] not in json.dumps(data)
 assert api('tls/import',{'domain':'wrong.test','chain':'bad','key':'bad'})[0]==400
 assert api()[1]['enabled'] is True
 run('shell','am','force-stop','com.dashplay.android');run('shell','am','start','-n','com.dashplay.android/.MainActivity')
 wait(lambda:api()[1]['phase']=='waiting_network')
 assert api()[1]['enabled'] is True
 # The previous manual TLS check saved a disabled fixture certificate. Explicit manual choice takes precedence.
 assert api('tls/enable',{'enabled':False})[0]==200
 wait(lambda:api()[1]['enabled'] is False)
 print('AUTO_TLS_OFFLINE_OK no default network; zero issuance/failures; private config persists; manual choice stops automation',flush=True)
from playwright.sync_api import sync_playwright
a.screens.mkdir(parents=True,exist_ok=True)
with sync_playwright() as pw:
 browser=pw.chromium.launch(channel='chrome',headless=True)
 page=browser.new_page(ignore_https_errors=True,viewport={'width':375,'height':812},reduced_motion='reduce')
 errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
 page.goto(base+'/manage');page.locator('#admin-link').fill(token);page.get_by_role('button',name='连接',exact=True).click()
 page.locator('#content').wait_for(state='visible')
 assert page.locator('#tesla-checks').is_disabled()
 assert not page.locator('#tesla-checks').is_checked()
 page.locator('#tesla-checks-form').locator('..').screenshot(path=str(a.screens/'tesla-checks-browser.png'))
 page.emulate_media(color_scheme='dark')
 page.locator('#tesla-checks-form').locator('..').screenshot(path=str(a.screens/'tesla-checks-browser-dark.png'))
 page.emulate_media(color_scheme='light')
 assert page.locator('#auto-tls-domain').is_disabled()
 page.locator('#auto-tls-enabled').check() # Unsaved draft only; never enables the device worker.
 page.locator('#auto-tls-domain').fill('draft.example.test');page.locator('#auto-tls-token').fill('fixture-unsaved-secret')
 page.locator('#auto-tls-provider').select_option('desec');page.locator('#auto-tls-zone').fill('example.test')
 page.locator('#refresh').click();page.wait_for_function("() => document.getElementById('notice').textContent === '状态已刷新。'")
 assert page.locator('#auto-tls-domain').input_value()=='draft.example.test'
 assert page.locator('#auto-tls-token').input_value()=='fixture-unsaved-secret'
 assert page.locator('#auto-tls-zone').is_visible()
 assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
 page.locator('#auto-tls-token').fill('')
 for dark in [False,True]:
  page.emulate_media(color_scheme='dark' if dark else 'light')
  page.locator('#auto-tls-form').locator('..').screenshot(path=str(a.screens/('automatic-tls-browser-dark.png' if dark else 'automatic-tls-browser.png')))
 page.set_viewport_size({'width':812,'height':375});assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
 # Save disabled settings through actual controls: no DNS/account writes, even on the online API 34 fixture.
 page.locator('#auto-tls-enabled').uncheck()
 with page.expect_response(lambda r:r.url.endswith('/api/android/tls/automatic') and r.request.method=='POST') as response:
  page.locator('#auto-tls-form button[type=submit]').click()
 assert response.value.status==200 and response.value.json()['enabled'] is False
 assert not errors
 browser.close()
print('AUTO_TLS_MANAGEMENT_OK auth/origin/bounds; browser fields and drafts; narrow/dark/landscape; no real DNS mutation or issuance',flush=True)
