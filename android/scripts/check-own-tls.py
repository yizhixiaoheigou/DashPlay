#!/usr/bin/env python3
"""Real Android TLS with generated test CA; never publishes DNS or claims public CA trust."""
import argparse,hashlib,json,pathlib,socket,ssl,subprocess,sys,time,urllib.request,urllib.error,urllib.parse,xml.etree.ElementTree as ET
p=argparse.ArgumentParser();p.add_argument('--adb',required=True);p.add_argument('--serial',required=True)
p.add_argument('--base',required=True);p.add_argument('--fixtures',type=pathlib.Path,required=True)
p.add_argument('--screens',type=pathlib.Path)
args=p.parse_args();adb=[args.adb,'-P','5039','-s',args.serial]
def run(*a):return subprocess.check_output(adb+list(a))
assert args.serial.startswith('emulator-') and run('shell','getprop','ro.kernel.qemu').strip()==b'1'
prefs=ET.fromstring(run('exec-out','run-as','com.dashplay.android','cat','shared_prefs/dashplay.xml'))
values={n.get('name'):n.text for n in prefs if n.tag=='string'};token=values['admin_token'];viewer=values['access_token']
base=args.base.rstrip('/');address=urllib.parse.urlsplit(base);port=address.port
unverified=ssl._create_unverified_context()
def api(path='tls',body=None,credential=token,origin=base):
 request=urllib.request.Request(base+'/api/android/'+path,data=None if body is None else json.dumps(body).encode(),
  headers={'Origin':origin,'X-DashPlay-Admin':credential,'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(request,context=unverified,timeout=4) as r:return r.status,json.load(r)
 except urllib.error.HTTPError as e:return e.code,json.load(e)
def cert():
 with socket.create_connection((address.hostname,port),timeout=3) as raw:
  with unverified.wrap_socket(raw,server_hostname='localhost') as tls:
   return hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
def wait(predicate):
 end=time.monotonic()+45
 while time.monotonic()<end:
  try:
   if predicate():return
  except (OSError,ValueError):pass
  time.sleep(.3)
 raise AssertionError('TLS transition did not settle')
# The preceding settings test schedules a receiver restart; require a stable real listener, not its old RUNNING flag.
baseline=None;stable_since=time.monotonic()
def baseline_ready():
 global baseline,stable_since
 try:
  current=cert();code,state=api()
  if code!=200 or state['enabled']:return False
 except OSError:
  stable_since=time.monotonic();return False
 if baseline!=current:baseline=current;stable_since=time.monotonic()
 return time.monotonic()-stable_since>=3
wait(baseline_ready)
for credential in ['',viewer,'f'*32]:
 assert api(credential=credential)[0]==403
 assert api('tls/import',{'domain':'forbidden.test','chain':'secret','key':'secret'},credential=credential)[0]==403
 assert api('tls/enable',{'enabled':True},credential=credential)[0]==403
assert api('tls/import',{},origin='https://elsewhere.invalid')[0]==403
assert api('tls/import',{'key':'secret-invalid'})[0]==400
assert api('tls/import',{'domain':'dashplay.test','chain':'x'*131073,'key':'secret'})[0]==413
trusted=ssl.create_default_context(cafile=str(args.fixtures/'root.pem'))
def verified_http(expected):
 with socket.create_connection((address.hostname,port),timeout=3) as raw:
  with trusted.wrap_socket(raw,server_hostname='dashplay.test') as tls:
   if hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()!=expected:return False
   tls.sendall(b'GET /api/status HTTP/1.1\r\nHost: dashplay.test\r\nConnection: close\r\n\r\n')
   body=b''
   while len(body)<65536:
    chunk=tls.recv(4096)
    if not chunk:break
    body+=chunk
   return body.startswith(b'HTTP/1.1 200') and b'deviceName' in body
installed=False
try:
 for algorithm in ['rsa','ec']:
  payload={'domain':'dashplay.test','chain':(args.fixtures/(algorithm+'-chain.pem')).read_text(),
   'key':(args.fixtures/(algorithm+'-key.pem')).read_text()}
  code,state=api('tls/import',payload)
  assert code==200 and state['usable'] and not state['systemTrusted'], f'fixture import failed: HTTP {code}; {state.get("error", "unusable certificate")}'
  installed=True
  assert 'key' not in state and 'chain' not in state
  fingerprint=state['fingerprint'];wait(lambda:verified_http(fingerprint))
  assert api()[1]['servingDomain']=='dashplay.test'
  private_path=['exec-out','run-as','com.dashplay.android','cat','files/own-tls/certificate.json']
  before=hashlib.sha256(run(*private_path)).digest()
  bad=payload|{'domain':'wrong.test'}
  code,error=api('tls/import',bad);assert code==400 and 'BEGIN' not in json.dumps(error)
  assert before==hashlib.sha256(run(*private_path)).digest()
  assert cert()==fingerprint
  print('OWN_TLS_'+algorithm.upper()+'_OK hostname/CA verified HTTPS; failed replacement retained old key and certificate',flush=True)
 assert api('tls/enable',{'enabled':False})[0]==200
 wait(lambda:cert()==baseline)
 assert api()[1]['servingDomain']==''
 assert api('tls/enable',{'enabled':True})[0]==200
 wait(lambda:verified_http(fingerprint))
 # Recreate the actual Android process: the enabled certificate must survive disk reload.
 run('shell','am','force-stop','com.dashplay.android')
 run('shell','am','start','-n','com.dashplay.android/.MainActivity')
 wait(lambda:verified_http(fingerprint))
 code,diagnostic=api('diagnostics');assert code==200 and 'PRIVATE KEY' not in json.dumps(diagnostic)
 print('OWN_TLS_PERSISTENCE_OK disable/re-enable, process restart, redacted diagnostics',flush=True)
 if args.screens:
  from playwright.sync_api import sync_playwright
  args.screens.mkdir(parents=True,exist_ok=True)
  with sync_playwright() as pw:
   browser=pw.chromium.launch(channel='chrome',headless=True)
   page=browser.new_page(ignore_https_errors=True,viewport={'width':375,'height':812})
   errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
   page.goto(base+'/manage')
   page.locator('#admin-link').fill(token)
   page.get_by_role('button',name='连接',exact=True).click()
   page.locator('#content').wait_for(state='visible')
   page.locator('#tls-domain').fill('dashplay.test')
   page.locator('#tls-chain').set_input_files(args.fixtures/'rsa-chain.pem')
   page.locator('#tls-key').set_input_files(args.fixtures/'rsa-key.pem')
   with page.expect_response(lambda response:response.url.endswith('/api/android/tls/import')) as response:
    page.locator('#tls-form button').click()
   assert response.value.status==200
   fingerprint=response.value.json()['fingerprint']
   wait(lambda:verified_http(fingerprint))
   page.wait_for_function("document.getElementById('tls-key').value === '' && document.getElementById('tls-chain').value === ''")
   page.locator('#tls-domain').fill('unsaved.example.test')
   page.locator('#refresh').click()
   page.wait_for_function("document.getElementById('tls-state').textContent.includes('当前使用：dashplay.test')")
   assert page.locator('#tls-domain').input_value()=='unsaved.example.test'
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
   page.locator('#tls-form').locator('..').screenshot(path=str(args.screens/'own-tls-browser.png'))
   page.emulate_media(color_scheme='dark')
   page.locator('#tls-form').locator('..').screenshot(path=str(args.screens/'own-tls-browser-dark.png'))
   page.locator('#tls-disable').click()
   wait(lambda:cert()==baseline)
   page.wait_for_function("!document.getElementById('tls-enable').disabled")
   page.locator('#tls-enable').click()
   wait(lambda:verified_http(fingerprint))
   assert not errors,'TLS management JavaScript error'
   browser.close()
  print('OWN_TLS_BROWSER_OK real PEM file upload, draft preservation, narrow/dark layout, disable/re-enable',flush=True)
finally:
 if installed:
  failed=sys.exception() is not None
  try:
   wait(lambda:api('tls/enable',{'enabled':False})[0]==200)
   wait(lambda:cert()==baseline)
  except Exception:
   if not failed:raise
   print('Cleanup unavailable; disposable emulator will be removed by smoke harness',flush=True)
print('OWN_TLS_LOCAL_FALLBACK_OK original local certificate restored',flush=True)
