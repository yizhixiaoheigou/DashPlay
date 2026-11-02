#!/usr/bin/env python3
"""Actual system picker and installer on disposable emulators; no user-device installation."""
import argparse, hashlib, json, pathlib, re, subprocess, time, xml.etree.ElementTree as ET
# Version codes that build-update-fixtures.sh pins for the two APKs this flow walks through.
BASE_CODE, NEWER_CODE = 3, 4
p=argparse.ArgumentParser()
p.add_argument('--adb',required=True);p.add_argument('--serial',required=True);p.add_argument('--output',type=pathlib.Path,required=True);p.add_argument('--api',type=int,required=True)
a=p.parse_args();assert a.serial.startswith('emulator-')
adb=[a.adb,'-P','5039','-s',a.serial];a.output.mkdir(parents=True,exist_ok=True)
def run(*parts): return subprocess.check_output(adb+list(parts))
ui_count=0
def tree():
    global ui_count
    for _ in range(5):
        run('shell','rm','-f','/data/local/tmp/update-ui.xml')
        diagnostic=run('shell','uiautomator','dump','/data/local/tmp/update-ui.xml')
        data=run('exec-out','cat','/data/local/tmp/update-ui.xml')
        if data.startswith(b'<?xml'):
            ui_count+=1
            (a.output/'ui').mkdir(exist_ok=True)
            (a.output/'ui'/f'{ui_count:03d}.xml').write_bytes(data)
            (a.output/'last-ui.xml').write_bytes(data)
            return ET.fromstring(data)
        time.sleep(.5)
    raise AssertionError('UI hierarchy unavailable: '+repr(diagnostic))
def find(rid):return next((n for n in tree().iter('node') if n.get('resource-id')==rid),None)
def click(n):
    assert n is not None,'control missing'
    x1,y1,x2,y2=map(int,re.findall(r'\d+',n.get('bounds')))
    run('shell','input','tap',str((x1+x2)//2),str((y1+y2)//2));time.sleep(.5)
def control(rid, tries=6):
    """A control on the update page, scrolled into view first (the page is taller than one viewport)."""
    return scroll_to(rid, tries)


def visible_bounds():
    """Pixel extent the UI actually occupies now.

    `wm size` keeps reporting the portrait size after a rotation, so a stroke built from it lands outside the
    landscape display and silently does nothing. The root node's bounds are the real, current extent.
    """
    root = next(iter(tree().iter('node')), None)
    assert root is not None, 'empty UI hierarchy'
    x1, y1, x2, y2 = map(int, re.findall(r'-?\d+', root.get('bounds')))
    return x2 - x1, y2 - y1


def scroll_to(rid, tries=6):
    """Bring a control into the viewport (uiautomator only reports what is visible) and return its node."""
    node = find('com.dashplay.android:id/'+rid)
    for _ in range(tries):
        if node is not None:
            return node
        # Swipe up inside the display: portrait y=1600 is off-screen in landscape, which silently did nothing.
        width, height = visible_bounds()
        # A short, fast stroke is treated as a fling and can be dropped as a tap; drag slowly across most of the screen.
        run('shell','input','swipe',str(width//2),str(int(height*0.80)),str(width//2),str(int(height*0.20)),'600')
        time.sleep(.6)
        node = find('com.dashplay.android:id/'+rid)
    raise AssertionError('control not reachable by scrolling: '+rid)


def scroll_page(times=1):
    """Drag the page up by about half a screen, using the size the UI really has now."""
    for _ in range(times):
        width, height = visible_bounds()
        run('shell','input','swipe',str(width//2),str(int(height*0.80)),str(width//2),str(int(height*0.30)),'500')
        time.sleep(.4)


def tap(rid):
    for _ in range(6):
        n=find('com.dashplay.android:id/'+rid)
        if n is not None:click(n);return
        scroll_page()
    raise AssertionError('control missing: '+rid)
def shot(name):(a.output/(name+'.png')).write_bytes(run('exec-out','screencap','-p'))
def current_version():
    data=run('shell','dumpsys','package','com.dashplay.android').decode()
    return int(re.search(r'versionCode=(\d+)',data)[1])
def native_page():
    run('shell','am','start','-f','0x04000000','-n','com.dashplay.android/.MainActivity');time.sleep(1)
    if find('com.dashplay.android:id/settings_button') is None:
        run('shell','input','keyevent','4');time.sleep(.5)
    tap('settings_button')
    for _ in range(8):
        row=find('com.dashplay.android:id/row_update')
        if row is not None:click(row);break
        scroll_page()
    else:raise AssertionError('update settings entry missing')
    for _ in range(6):
        if find('com.dashplay.android:id/update_choose') is not None and find('com.dashplay.android:id/update_install') is not None:break
        scroll_page()
    assert find('com.dashplay.android:id/update_choose') is not None
def pick(filename):
    tap('update_choose')
    if a.api == 21:
        # Old DownloadsProvider lists download records, not files copied into Download.
        # Expose the real storage root in the stock picker before opening that folder.
        nodes=list(tree().iter('node'))
        if any(n.get('text')=='Open from' for n in nodes):
            click(next(n for n in nodes if n.get('text')=='Recent' and n.get('resource-id')=='android:id/title'));nodes=list(tree().iter('node'))
        menu=next((n for n in nodes if n.get('content-desc')=='More options'),None)
        if menu is not None:
            click(menu); nodes=list(tree().iter('node'))
            show=next((n for n in nodes if n.get('text') in ['Show SD card','Show internal storage']),None)
            if show is not None:click(show)
            else:run('shell','input','keyevent','4')
        nodes=list(tree().iter('node'))
        drawer=next((n for n in nodes if n.get('content-desc')=='Show roots'),None)
        if drawer is not None:click(drawer)
        nodes=list(tree().iter('node'))
        storage=next((n for n in nodes if n.get('text') in ['USB storage','SD card','SDCARD','Internal storage']),None)
        assert storage is not None, [(n.get('text'),n.get('content-desc')) for n in nodes]
        click(storage)
    for attempt in range(10):
        nodes=list(tree().iter('node'))
        target=next((n for n in nodes if n.get('text')==filename),None)
        if target is not None:click(target);break
        # DocumentsUI navigation drawer -> Downloads, then device-storage fallback.
        if attempt==0 and a.api != 21:
            drawer=next((n for n in nodes if n.get('content-desc') in ['Show roots','Open navigation drawer','Navigate up']),None)
            if drawer is not None:click(drawer);continue
        downloads=next((n for n in nodes if n.get('text') in ['Downloads','Download']),None)
        if downloads is not None:click(downloads);continue
        raise AssertionError('picker file missing: '+filename+' '+str([(n.get('text'),n.get('content-desc')) for n in nodes]))
    else:raise AssertionError('picker exhausted')
    for _ in range(30):
        choose=find('com.dashplay.android:id/update_choose')
        if choose is not None and choose.get('enabled')=='true':return
        time.sleep(.3)
    raise AssertionError('package check did not finish')
assert current_version()==BASE_CODE
native_page();assert find('com.dashplay.android:id/update_install').get('enabled')=='false';shot('01-update-empty')
pick('Broken.apk');assert find('com.dashplay.android:id/update_install').get('enabled')=='false';shot('02-broken-rejected')
pick('Other-app.apk');assert find('com.dashplay.android:id/update_install').get('enabled')=='false';shot('02-other-rejected')
pick('Same-version.apk');assert find('com.dashplay.android:id/update_install').get('enabled')=='false';shot('03-same-rejected')
pick('New-version.apk');assert find('com.dashplay.android:id/update_install').get('enabled')=='true';shot('04-ready')
# Configuration change must retain the verified private copy, not ask for another source URI grant.
run('shell','settings','put','system','accelerometer_rotation','0');run('shell','settings','put','system','user_rotation','1');time.sleep(1)
# The page scrolled back to the top on rotation (the update screen grew past one viewport when the online
# section arrived), so the install button has to be scrolled into view before it can be read.
landscape_install = scroll_to('update_install')
assert landscape_install.get('enabled')=='true','the verified package must survive the rotation';shot('05-landscape')
run('shell','settings','put','system','user_rotation','0');time.sleep(1)

def system_button(action):
    nodes=list(tree().iter('node'))
    words={'cancel':['Cancel','CANCEL'], 'install':['Install','INSTALL','Update','UPDATE','Next','NEXT']}[action]
    ids={'cancel':['android:id/button2','com.android.packageinstaller:id/cancel_button'],
         'install':['android:id/button1','com.android.packageinstaller:id/ok_button']}[action]
    chosen=next((n for n in nodes if n.get('text') in words and n.get('enabled')=='true'),None)
    return chosen if chosen is not None else next((n for n in nodes if n.get('resource-id') in ids and n.get('enabled')=='true'),None)
def wait_installer():
    for _ in range(10):
        if any(n.get('package','').endswith('packageinstaller') for n in tree().iter('node')):return
        time.sleep(.5)
    raise AssertionError('system installer did not appear')
def data_snapshot():
    names=['shared_prefs/dashplay.xml','files/update-test-sentinel']
    for name in run('shell','run-as','com.dashplay.android','ls','files/state').decode().split():
        if re.fullmatch(r'[A-Za-z0-9_.-]+',name):names.append('files/state/'+name)
    result={}
    for name in names:
        data=subprocess.run(adb+['exec-out','run-as','com.dashplay.android','cat',name],capture_output=True)
        if data.returncode==0:result[name]=hashlib.sha256(data.stdout).hexdigest()
    assert 'shared_prefs/dashplay.xml' in result and 'files/update-test-sentinel' in result
    assert any('p12' in n or 'jks' in n or 'keystore' in n for n in result),result.keys()
    return result
run('shell',"run-as com.dashplay.android sh -c 'echo upgrade-data-sentinel > files/update-test-sentinel'")
before=data_snapshot()
tap('update_install');shot('06-system-step')
if a.api>=26:
    # First decline the per-app source permission; no package has been installed.
    assert any(n.get('package')=='com.android.settings' and n.get('checkable')=='true' for n in tree().iter('node'))
    run('shell','input','keyevent','4');time.sleep(.5)
    assert current_version()==BASE_CODE and find('com.dashplay.android:id/update_install').get('enabled')=='true'
    tap('update_install')
    switch=next(n for n in tree().iter('node') if n.get('package')=='com.android.settings' and n.get('checkable')=='true')
    if switch.get('checked')!='true':click(switch)
    run('shell','input','keyevent','4');time.sleep(.5);tap('update_install')
else:
    # Legacy Android has a device-wide Unknown sources setting; leave it to system UI.
    wait_installer()
    if system_button('install') is None:
        nodes=list(tree().iter('node'));settings=next((n for n in nodes if n.get('text') in ['Settings','SETTINGS']),None)
        click(settings)
        for _ in range(8):
            nodes=list(tree().iter('node'));unknown=next((n for n in nodes if n.get('text')=='Unknown sources'),None)
            if unknown is not None:click(unknown);break
            scroll_page()
        click(next(n for n in tree().iter('node') if n.get('resource-id')=='android:id/button1'))
        run('shell','input','keyevent','4');time.sleep(.5);tap('update_install')
wait_installer();shot('07-installer-confirmation');click(system_button('cancel'));time.sleep(1)
assert current_version()==BASE_CODE and data_snapshot()==before
assert find('com.dashplay.android:id/update_install').get('enabled')=='true'
shot('08-cancel-preserves-data')
tap('update_install');wait_installer();click(system_button('install'))
for _ in range(60):
    if current_version()==NEWER_CODE:break
    time.sleep(.5)
else:raise AssertionError('system installer did not install newer version')
assert data_snapshot()==before
native_page();shot('09-updated')
(a.output/'result.json').write_text(json.dumps({'success':True,'api':a.api,'from_code':BASE_CODE,'to_code':NEWER_CODE,'data_sha256':before,
 'checks':['real system document picker','other package rejected','same version rejected','rotation retains candidate','system confirmation canceled with data unchanged','actual system installer upgraded same signed APK without data loss'],
 'limits':['debug-signature test build only','no real identity or iPhone was used','not an online update service']},indent=2)+'\n')
print(f'UPDATE_INSTALL_OK versionCode {BASE_CODE} -> {NEWER_CODE}; real system picker/installer; configuration and TLS files retained')
