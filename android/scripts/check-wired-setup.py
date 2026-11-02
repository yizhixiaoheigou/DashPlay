#!/usr/bin/env python3
"""Wired-only setup regression on a disposable emulator, never a user device."""
import argparse,json,pathlib,re,ssl,subprocess,time,urllib.request,urllib.error,xml.etree.ElementTree as ET
from urllib.parse import urlsplit

p=argparse.ArgumentParser()
p.add_argument('--adb',required=True);p.add_argument('--serial',required=True)
p.add_argument('--base',required=True);p.add_argument('--api-only',action='store_true')
p.add_argument('--screens',type=pathlib.Path)
args=p.parse_args();adb=[args.adb,'-P','5039','-s',args.serial]
def run(*a):return subprocess.check_output(adb+list(a))
assert args.serial.startswith('emulator-') and run('shell','getprop','ro.kernel.qemu').strip()==b'1'
def prefs():return ET.fromstring(run('exec-out','run-as','com.dashplay.android','cat','shared_prefs/dashplay.xml'))
# Create the admin token, without ever printing it.
context=ssl._create_unverified_context();base=args.base.rstrip('/')
try:urllib.request.urlopen(urllib.request.Request(base+'/api/android/settings',headers={'X-DashPlay-Admin':'0'*32}),context=context,timeout=5)
except urllib.error.HTTPError as e:assert e.code==403
token=next(n.text for n in prefs() if n.get('name')=='admin_token')
def api(body=None):
 req=urllib.request.Request(base+'/api/android/settings',data=None if body is None else json.dumps(body).encode(),
  headers={'Origin':base,'X-DashPlay-Admin':token,'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(req,context=context,timeout=3) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.load(e)
def ready():
 for _ in range(50):
  try:
   code,s=api()
   if code==200 and s['status']=='RUNNING':return s
  except OSError:pass
  time.sleep(.2)
 raise AssertionError('receiver not ready')
ready()
assert not any(n.get('name')=='hotspot_ssid' and n.text for n in prefs()), 'test requires no user-saved Wi-Fi'
# Android 5 may expose a default system hotspot configuration. External mode must not use it.
assert api({'network':'MIFI'})[0]==200
time.sleep(2)
original=ready()['settings']
assert original['ssid']=='' and not original['hasPassword'], 'test requires a fresh emulator setup'
code,result=api({'iphoneLink':'WIRED','network':'MIFI','setupDone':True})
assert code==200, 'wired setup rejected missing Wi-Fi credentials'
time.sleep(2);ready()
before=api()[1]['settings']
assert before['setupDone'] and before['ssid']==''
code,_=api({'iphoneLink':'WIRELESS'})
assert code==400, 'switching to wireless must require credentials'
assert api()[1]['settings']==before, 'failed wireless switch partially saved'
assert api({'network':'CAR_HOTSPOT','iphoneLink':'ANY','setupDone':True})[0]==200
time.sleep(2);ready()
assert api({'network':'MIFI','iphoneLink':'WIRED','setupDone':True})[0]==200
time.sleep(2);ready()
print('WIRED_SETUP_API_OK: no SSID required, wireless switch validated atomically, car hotspot follows wired plan',flush=True)
if args.api_only:raise SystemExit(0)

assert args.screens is not None
args.screens.mkdir(parents=True,exist_ok=True)
def tree():
 run('shell','uiautomator','dump','/data/local/tmp/dashplay-wired-ui.xml')
 return ET.fromstring(run('exec-out','cat','/data/local/tmp/dashplay-wired-ui.xml'))
def find(name,root=None):
 return next((n for n in (root if root is not None else tree()).iter('node') if n.get('resource-id')=='com.dashplay.android:id/'+name),None)
def tap(name):
 for _ in range(10):
  node=find(name)
  if node is not None:
   a,b,c,d=map(int,re.findall(r'\d+',node.get('bounds')))
   run('shell','input','tap',str((a+c)//2),str((b+d)//2));time.sleep(.25);return
  run('shell','input','swipe','500','1800','500','500','250')
 raise AssertionError('missing control '+name)
sdk=int(run('shell','getprop','ro.build.version.sdk').strip())
def has_text(expected):
 if sdk==21:expected=expected.encode('ascii',errors='replace').decode()
 return any(n.get('text')==expected for n in tree().iter('node'))
def shot(name):args.screens.joinpath(name+'.png').write_bytes(run('exec-out','screencap','-p'))

run('shell','am','start','-f','0x04000000','-n','com.dashplay.android/.MainActivity')
time.sleep(.5);tap('settings_button');tap('row_redo')
tap('choice_1');tap('primary') # 安卓手机 -> identity
tap('primary') # identity -> car
tap('choice_2');tap('primary') # other car -> network
tap('choice_3');tap('primary') # external LAN -> sound
tap('choice_1');tap('primary') # draft sound differs from stored default -> iPhone
tap('choice_3');tap('primary') # wired -> summary without a Wi-Fi step
assert has_text('确认一下') and find('field_ssid') is None
shot('wired-native-summary')
run('shell','input','keyevent','4')
assert has_text('iPhone 用数据线还是无线？'), 'back did not skip unused Wi-Fi step'
tap('choice_2');tap('primary') # wireless -> Wi-Fi
assert find('field_ssid') is not None
tap('primary') # blank SSID cannot advance
assert find('field_ssid') is not None and not has_text('确认一下')
run('shell','input','keyevent','4')
tap('choice_3');tap('primary')
assert has_text('确认一下')
# Recreation must retain the current draft and skipped step, including orientation changes.
run('shell','settings','put','system','accelerometer_rotation','0')
run('shell','settings','put','system','user_rotation','1');time.sleep(2)
assert has_text('确认一下') and find('field_ssid') is None
shot('wired-native-landscape')
run('shell','settings','put','system','user_rotation','0');time.sleep(2)
tap('primary');time.sleep(2)
s=ready()['settings']
assert s['network']=='MIFI' and s['iphoneLink']=='WIRED' and s['setupDone'] and s['ssid']==''
assert s['audioOutsideCarPlay'] and not original['audioOutsideCarPlay'], 'rotation lost unsaved draft answer'
assert not any(n.get('name')=='usb_enabled' and n.get('value')=='true' for n in prefs())
print('WIRED_SETUP_NATIVE_OK: skipped fields, back navigation, wireless validation, rotation draft, durable completion without USB consent',flush=True)

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
 browser=pw.chromium.launch(channel='chrome',headless=True)
 page=browser.new_page(ignore_https_errors=True,viewport={'width':375,'height':812})
 errors=[];page.on('pageerror',lambda e:errors.append(type(e).__name__))
 failed_requests=[]
 page.on('requestfailed',lambda r:failed_requests.append({'path':urlsplit(r.url).path,'method':r.method,'failure':r.failure}))
 page.goto(base+'/manage')
 page.locator('#admin-link').fill(token);page.locator('#login-form button').click()
 page.locator('#content').wait_for(state='visible')
 assert page.locator('#wifi-fields').is_hidden() and page.locator('#ssid').is_disabled()
 assert page.locator('#settings-form').evaluate('(form)=>form.checkValidity()')
 page.locator('#iphoneLink').select_option('WIRELESS')
 assert page.locator('#wifi-fields').is_visible() and page.locator('#ssid').is_enabled()
 assert not page.locator('#settings-form').evaluate('(form)=>form.checkValidity()')
 page.locator('#ssid').fill('DashPlay setup fixture')
 page.locator('#wifiSecurity').select_option('WPA2');page.locator('#passphrase').fill('fixture-password')
 def submit():
  try:
   with page.expect_response(lambda r:r.url==base+'/api/android/settings' and r.request.method=='POST') as response:
    page.locator('#settings-form button[type=submit]').click()
   assert response.value.status==200
   # HTTP headers alone do not prove the browser has consumed/rendered the saved response.
   page.wait_for_function("!document.querySelector('#settings-form button[type=submit]').disabled && document.querySelector('#notice').textContent.startsWith('已保存。')")
   ready()
  except Exception:
   observed=page.evaluate("({valid:document.querySelector('#settings-form').checkValidity(),link:document.querySelector('#iphoneLink').value,network:document.querySelector('#network').value,notice:document.querySelector('#notice').textContent,disabled:document.querySelector('#settings-form button[type=submit]').disabled})")
   (args.screens/'wired-submit-failure.json').write_text(json.dumps({'browser':observed,'failed_requests':failed_requests,'errors':errors},ensure_ascii=False,indent=2))
   page.screenshot(path=str(args.screens/'wired-submit-failure.png'),full_page=True)
   raise
 submit()
 saved=api()[1]['settings'];assert saved['ssid']=='DashPlay setup fixture' and saved['hasPassword']
 page.locator('#iphoneLink').select_option('WIRED')
 with page.expect_request(lambda r:r.url==base+'/api/android/settings' and r.method=='POST') as posted:
  submit()
 assert all(k not in posted.value.post_data_json for k in ['ssid','passphrase','wifiSecurity','wifiChannel'])
 s=api()[1]['settings'];assert s['iphoneLink']=='WIRED' and s['ssid']==saved['ssid'] and s['hasPassword']
 assert page.locator('#wifi-fields').is_hidden()
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
 page.screenshot(path=str(args.screens/'wired-browser-phone.png'),full_page=True)
 page.set_viewport_size({'width':1180,'height':820});page.emulate_media(color_scheme='dark',reduced_motion='reduce')
 page.screenshot(path=str(args.screens/'wired-browser-dark.png'),full_page=True)
 assert not errors
 browser.close()
# The fixture password remains private in app storage; changing modes must not remove it.
assert next(n.text for n in prefs() if n.get('name')=='hotspot_passphrase')=='fixture-password'
print('WIRED_SETUP_BROWSER_OK: conditional required fields, real saves, stored Wi-Fi retained, narrow/dark layout',flush=True)
