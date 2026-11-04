'use strict';
const el = id => document.getElementById(id);
const storageKey = 'dashplay-admin';
let token = '', state = null, working = false;
let securityNetwork = 'THIS_HOTSPOT', securityDrafts = {};
function remember(value) { token = value; try { value ? sessionStorage.setItem(storageKey, value) : sessionStorage.removeItem(storageKey); } catch {} }
function credential(value) {
  value = value.trim();
  if (/^[0-9a-f]{32}$/.test(value)) return value;
  try { return new URLSearchParams(new URL(value).hash.slice(1)).get('token') || ''; } catch { return ''; }
}
const fragment = new URLSearchParams(location.hash.slice(1)).get('token');
try { token = sessionStorage.getItem(storageKey) || ''; } catch {}
if (fragment && /^[0-9a-f]{32}$/.test(fragment)) remember(fragment);
if (location.hash) history.replaceState(null, '', location.pathname);
function notice(message, error = false) { el('notice').textContent = message; el('notice').classList.toggle('error', error); }
function busy(value) { working = value; document.querySelectorAll('button').forEach(button => { button.disabled = value || button.dataset.unavailable === 'true'; }); }
async function api(path, body, raw = false) {
  const headers = {'X-DashPlay-Admin': token};
  if (body !== undefined) headers['Content-Type'] = raw ? 'application/octet-stream' : 'application/json';
  const response = await fetch('/api/android/' + path, {method: body === undefined ? 'GET' : 'POST', headers,
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body), cache: 'no-store', signal: typeof AbortSignal !== 'undefined' && AbortSignal.timeout ? AbortSignal.timeout(65000) : undefined});
  const data = await response.json();
  if (response.status === 403) notice('只接受来自本机页面的设置请求，请用同一台设备上的地址打开。', true);
  if (!response.ok) throw new Error(data.error || '操作未完成，请重试。');
  return data;
}
function rows(id, entries) {
  const list = el(id); list.replaceChildren();
  for (const [key,value] of entries) { const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = key; dd.textContent = String(value); list.append(dt,dd); }
}
function render(value, fill = false) {
  state = value;
  el('login').hidden = true; el('content').hidden = false;
  const s = value.settings, c = value.capabilities;
  if (value.viewerUrl) el('viewer-link').href = value.viewerUrl;
  el('summary').textContent = s.deviceName + ' · ' + c.model;
  const status = {STARTING:'正在启动',WAITING_NETWORK:'等待网络',NEEDS_IDENTITY:'请导入身份',RUNNING:'接收服务运行中',FAILED:'启动失败，请下载诊断或重试',STOPPED:'服务已停止'};
  rows('status', [['接收服务',status[value.status] || value.status],['iPhone',value.sessionActive ? 'CarPlay 已连接' : value.wirelessProblem || '正在自动连接'],[phoneOnly() ? 'USB 上网通道' : '数据线连接',value.usb?.activeMode ? value.usb.message : '未启用'],['通过数据线使用 iPhone 网络',value.usb?.internetMessage || '未启用'],['热点共享',value.usbShare?.message || '未启用'],['画面浏览器',value.viewers],['设备地址',c.address || '等待网络'],['本地热点',value.localHotspot?.message || '未启用'],['热点名称',value.localHotspot?.ssid || '—']]);
  rows('capabilities', [['系统','Android ' + c.android],['Wi-Fi',c.wifi ? '硬件可用' : '未检测到硬件'],['应用本地热点',c.managedHotspotMessage || (c.managedHotspot ? '可尝试自动创建；首次需在安卓上授权' : '不满足应用热点条件；系统热点需另外检查')],['系统热点',c.systemHotspotMessage || '系统入口、开启与客户端接入仍需验证'],['蓝牙',c.bluetooth === false ? (phoneOnly() ? '未检测到蓝牙；安卓手机无法使用无线 CarPlay' : '未检测到蓝牙；有 USB Host 时仍可用数据线') : c.bluetooth !== true ? '尚未确认蓝牙硬件' : !c.bluetoothPermission ? '请在安卓设备上授予附近设备权限' : c.bluetoothEnabled ? '已开启' : '已关闭'],['USB Host',c.usbHost ? '系统声明支持；当前连接 ' + c.usbDevices + ' 个设备' : '系统未声明支持'],['特斯拉 Root 访问',value.teslaAccess?.message || '未启用；需在安卓设备上授权']]);
  el('identity-state').textContent = value.identity ? '身份已导入。再次导入会先校验，再替换。' : '尚未导入身份。';
  el('usbInternet').disabled = (!value.usb?.internetAllowed || !value.usb?.enabled) && !s.usbInternet;
  el('usb-internet-state').textContent = el('usbInternet').disabled ? '请先在安卓设备的 USB 设置页（手机为“USB 上网”，盒子为“数据线连接”）启用并允许使用 iPhone 网络。' : value.usb?.internetMessage || '未启用';
  el('usbHotspotShare').disabled = (!value.usbShare?.allowed || !value.usb?.enabled || !s.usbInternet || s.network !== 'THIS_HOTSPOT') && !s.usbHotspotShare;
  el('usb-share-state').textContent = el('usbHotspotShare').disabled ? '请先在安卓设备的 USB 设置页（手机为“USB 上网”，盒子为“数据线连接”）允许 Root 共享，并开启本机热点及“通过数据线使用 iPhone 网络”。' : value.usbShare?.message || '未启用';
  el('version').textContent = 'DashPlay ' + value.version;
  el('tesla-access-state').textContent = value.teslaAccess?.message || '未启用。';
  el('tesla-checks').disabled = !value.teslaAccess?.enabled && !value.teslaAccess?.checksEnabled;
  el('tesla-checks-save').dataset.unavailable = String(el('tesla-checks').disabled);
  el('tesla-checks-save').disabled = working || el('tesla-checks').disabled;
  if (fill) el('tesla-checks').checked = !!value.teslaAccess?.checksEnabled;
  const rootOff = !value.teslaAccess?.enabled && value.teslaAccess?.state === 'OFF';
  el('tesla-access-disable').dataset.unavailable = String(rootOff);
  el('tesla-access-disable').disabled = working || rootOff;
  const tls = value.ownTls;
  automaticStatus(tls?.automatic);
  const upd = value.update;
  if (upd) {
    const checked = upd.checkedAt ? '（上次检查：' + new Date(upd.checkedAt).toLocaleString() + '）' : '（尚未检查）';
    el('update-state').textContent = (upd.error ? '检查更新未成功：' + upd.error
      : upd.available ? '发现新版本 ' + upd.latest + '（当前 ' + upd.current + '）'
      : upd.latest ? '已是最新版本 ' + upd.latest
      : '当前版本 ' + (upd.current || value.version)) + ' ' + checked;
  }
  if (tls) {
    el('tls-state').textContent = tls.message + (tls.expires ? ' 有效期至 ' + new Date(tls.expires).toLocaleDateString() : '') +
      (tls.servingDomain ? ' 当前使用：' + tls.servingDomain : ' 当前使用本地证书。');
    el('tls-target').textContent = '域名应解析到：' + (tls.dnsTarget || '等待网络') +
      (tls.directAddress ? '。本地 HTTP 管理入口：http://' + tls.directAddress + '/manage' : '');
    for (const id of ['tls-enable','tls-disable']) {
      el(id).dataset.unavailable = String(!tls.domain);
      el(id).disabled = working || !tls.domain;
    }
  }
  if (!fill) { wifiFields(); return; }
  el('tls-domain').value = tls?.domain || '';
  fillAutomatic(tls?.automatic);
  for (const key of ['deviceName','car','network','iphoneLink','ssid','uplinkSsid','externalWifiChoice','wifiSecurity','wifiChannel','idleReleaseSeconds']) el(key).value = s[key] ?? '';
  securityNetwork = s.network === 'THIS_HOTSPOT' ? 'THIS_HOTSPOT' : 'MIFI';
  securityDrafts = {THIS_HOTSPOT: s.localWifiSecurity || (s.hasPassword ? 'WPA2' : 'OPEN'),
    MIFI: s.uplinkWifiSecurity || (s.hasUplinkPassword ? 'WPA2' : 'OPEN')};
  securityDrafts[securityNetwork] = s.wifiSecurity;
  el('audioOutsideCarPlay').value = String(s.audioOutsideCarPlay);
  el('usbInternet').checked = !!(s.usbInternetRequested ?? s.usbInternet);
  if (s.usbInternetRequested && !s.usbInternet && !value.usb?.internetAllowed) el('usb-internet-state').textContent = '已选择通过数据线使用 iPhone 网络，等待安卓设备授权；当前尚未连接。';
  el('usbHotspotShare').checked = !!s.usbHotspotShare;
  el('ownNameOnHotspot').checked = s.ownNameOnHotspot !== false;
  el('updateCheck').checked = s.updateCheck !== false;
  el('updateMirror').value = s.updateMirror || '';
  el('autoHotspot').checked = s.autoHotspot; wifiFields(); el('startAtBoot').checked = s.startAtBoot; el('setupDone').checked = s.setupDone;
  el('compatHttp').checked = !!s.compatHttp;
  el('browserPort').value = String(s.browserPort || s.compatHttpPort || 18743);
  const address = (value.teslaAccess?.endpoints || []).find(e => e.scheme === 'address');
  el('address-state').textContent = !address ? '' : address.available ? `免 Root 地址：${address.message}`
    : `免 Root 地址不可用：${address.message}`;
  const compat = (value.teslaAccess?.endpoints || []).find(e => e.scheme === 'http');
  el('compat-state').textContent = !compat ? '' : !compat.enabled ? '未启用。'
    : compat.available ? `运行中：${compat.url}` : `不可用：${compat.reason || '未知原因'}`;
  el('compat-url').textContent = compat?.available ? `车机浏览器打开：${compat.url}` : '';
  el('passphrase').value = '';
  el('uplinkPassphrase').value = '';
  el('password-state').textContent = s.hasPassword ? '已保存密码，留空则保留；密码不会回传到此页面。' : '尚未保存密码。';
  el('current-wifi-state').textContent = s.currentWifiSsid ? `安卓当前 Wi-Fi：${s.currentWifiSsid}。${value.wirelessProblem || ''}` : '暂时无法读取当前 Wi-Fi 名称；可在 App 中允许读取，或选择其他 Wi-Fi 手动填写。';
  el('uplink-password-state').textContent = s.hasUplinkPassword ? '已保存密码，留空则保留。' : '尚未保存密码。';
}
function automaticStatus(value) {
  if (!value) return;
  el('auto-tls-state').textContent = value.message + (value.phase === 'retry' && value.nextAttempt ?
    ' 下次尝试：' + new Date(value.nextAttempt).toLocaleString() : '');
}
function fillAutomatic(value) {
  if (!value || value.phase === 'storage_error') return;
  el('auto-tls-enabled').checked = value.enabled;
  el('auto-tls-provider').value = value.provider;
  el('auto-tls-domain').value = value.domain;
  el('auto-tls-zone').value = value.zone;
  el('auto-tls-token').value = '';
  el('auto-tls-terms').checked = value.termsAccepted;
  automaticFields();
}
function automaticFields() {
  const deSEC = el('auto-tls-provider').value === 'desec';
  const enabled = el('auto-tls-enabled').checked;
  for (const id of ['auto-tls-provider','auto-tls-domain','auto-tls-zone','auto-tls-token','auto-tls-terms']) el(id).disabled = !enabled;
  el('auto-tls-zone-box').hidden = !deSEC;
  el('auto-tls-zone').disabled = !enabled || !deSEC;
  el('auto-tls-domain').required = enabled;
  el('auto-tls-terms').required = enabled;
}
el('auto-tls-provider').onchange = automaticFields;
el('auto-tls-enabled').onchange = automaticFields;
el('auto-tls-form').onsubmit = event => { event.preventDefault(); void action(async () => {
  el('auto-tls-feedback').textContent = '正在保存……';
  try {
    const result = await api('tls/automatic', {enabled:el('auto-tls-enabled').checked,provider:el('auto-tls-provider').value,
      domain:el('auto-tls-domain').value,zone:el('auto-tls-provider').value === 'desec' ? el('auto-tls-zone').value : '',
      token:el('auto-tls-token').value || null,termsAccepted:el('auto-tls-terms').checked});
    el('auto-tls-token').value = ''; automaticStatus(result);
    el('auto-tls-feedback').textContent = '已保存，后台会按网络状态继续；当前画面不会因此立即重启。';
  } catch (error) { el('auto-tls-feedback').textContent = error.message || '保存未完成，请重试。'; throw error; }
}); };
el('auto-tls-retry').onclick = () => { void action(async () => {
  automaticStatus(await api('tls/automatic/retry', {}));
  el('auto-tls-feedback').textContent = '已请求重试；服务商要求的等待时间仍会保留。';
}); };
function manualWifi() {
  return state?.capabilities?.bluetooth !== false && el('network').value !== 'CAR_HOTSPOT' && el('iphoneLink').value !== 'WIRED' &&
    !(el('autoHotspot').checked && el('network').value === 'THIS_HOTSPOT');
}
function phoneOnly() { return state?.settings?.deviceKind === 'PHONE'; }
function connectionFields() {
  const c = state?.capabilities || {}, select = el('iphoneLink'), netSelect = el('network');
  const tesla = el('car').value === 'TESLA';
  const carHotspotOpt = netSelect.querySelector('[value="CAR_HOTSPOT"]');
  if (carHotspotOpt) { carHotspotOpt.disabled = tesla || phoneOnly(); carHotspotOpt.hidden = phoneOnly(); }
  netSelect.setCustomValidity((tesla || phoneOnly()) && netSelect.value === 'CAR_HOTSPOT' ? '当前设备类型或车型不提供车机热点路径，请选择本机热点或外部网络。' : '');
  const car = netSelect.value === 'CAR_HOTSPOT';
  const noBluetooth = c.bluetooth === false, noUsb = c.usbHost === false;
  const unavailable = {WIRED:phoneOnly() || noUsb, WIRELESS:car || noBluetooth, ANY:phoneOnly() || noUsb && (car || noBluetooth)};
  for (const value of ['WIRED','ANY']) select.querySelector('[value="' + value + '"]').hidden = phoneOnly();
  el('wired-consent-help').hidden = phoneOnly();
  el('device-policy').textContent = phoneOnly() ? '设备类型：安卓手机。仅支持无线 CarPlay；USB 上网是独立选项，默认关闭，不用于连接 CarPlay。请使用本机热点或其他设备提供的 Wi-Fi，当前不支持由运行 CarPlay 的同一部 iPhone 提供 Wi-Fi。' : '设备类型：安卓盒子。按硬件条件提供有线、无线和自动选择。设备类型可在安卓 App 设置中修改。';
  for (const [value, disabled] of Object.entries(unavailable)) select.querySelector('[value="' + value + '"]').disabled = disabled;
  // Keep an existing choice visible, but explain why it cannot be saved; do not silently switch transports.
  select.setCustomValidity(unavailable[select.value] ? '当前设备或网络不支持此连接方式，请选择可用的连接方式。' : '');
  el('connection-capability').textContent = phoneOnly() ? (noBluetooth ?
    '未检测到蓝牙，无法使用无线 CarPlay。安卓手机不提供有线连接，请更换具备蓝牙的设备或支持 USB Host 的安卓盒子。' : car ?
    '安卓手机仅支持无线 CarPlay，请改用本机热点或其他设备提供的外部 Wi-Fi。' : !c.bluetoothPermission ?
    '安卓手机仅支持无线 CarPlay；请在安卓设备授予蓝牙或附近设备权限，再添加 iPhone。' : !c.bluetoothEnabled ?
    '安卓手机仅支持无线 CarPlay；请打开蓝牙，再添加 iPhone。' :
    '安卓手机仅支持无线 CarPlay，请完成蓝牙配对和 Wi-Fi 设置。') : noBluetooth && noUsb ?
    '未检测到蓝牙和 USB Host，目前没有可用的 iPhone 连接方式。' : noBluetooth ?
    '未检测到蓝牙，不影响数据线 CarPlay；自动选择将仅使用数据线。无线连接不可用。' : car && noUsb ?
    '车机热点方案需要 USB Host 数据线连接；请更换设备或网络方案。' : car ?
    '车机热点方案使用数据线连接 iPhone，不需要蓝牙。' : c.bluetooth !== true ?
    '尚未确认蓝牙能力；数据线连接不依赖蓝牙。无线能力以安卓设备检测结果为准。' : !c.bluetoothPermission ?
    '检测到蓝牙；无线连接需先在安卓设备授予附近设备权限。数据线连接不需要蓝牙权限。' : !c.bluetoothEnabled ?
    '检测到蓝牙，但当前已关闭；无线连接需先打开蓝牙，数据线连接不受影响。' : noUsb ?
    '检测到蓝牙，未检测到 USB Host；可选择无线连接，仍需完成配对和网络设置。' :
    '已检测到蓝牙和 USB Host，可选择无线或数据线连接；无线仍需完成配对和网络设置。';
}
function wifiFields() {
  const nextNetwork = el('network').value === 'THIS_HOTSPOT' ? 'THIS_HOTSPOT' : 'MIFI';
  if (nextNetwork !== securityNetwork) {
    securityDrafts[securityNetwork] = el('wifiSecurity').value;
    el('wifiSecurity').value = securityDrafts[nextNetwork] || 'WPA2';
    securityNetwork = nextNetwork;
  }
  const car = el('network').value === 'CAR_HOTSPOT';
  connectionFields();
  const manual = manualWifi();
  for (const key of ['ssid','passphrase','uplinkSsid','uplinkPassphrase','wifiSecurity','wifiChannel']) el(key).disabled = !manual;
  const local = nextNetwork === 'THIS_HOTSPOT';
  for (const key of ['ssid','passphrase']) el(key).disabled = !manual || !local;
  for (const key of ['uplinkSsid','uplinkPassphrase']) el(key).disabled = !manual || local;
  el('uplinkSsid').required = manual && !local;
  el('external-wifi-choice').hidden = !manual || local;
  el('externalWifiChoice').disabled = !manual || local;
  el('externalWifiChoice').required = manual && !local;
  el('uplinkSsid').readOnly = el('externalWifiChoice').value === 'CURRENT';
  el('wifi-fields').hidden = !manual;
  el('wifi-fields-status').textContent = manual ? '' : phoneOnly() && state?.capabilities?.bluetooth === false ? '当前缺少蓝牙，无法使用无线 CarPlay；已保存的网络参数保留。' : car || el('iphoneLink').value === 'WIRED' || state?.capabilities?.bluetooth === false ?
    '数据线连接无需填写 iPhone 的 Wi-Fi 参数；已有参数会保留。安卓与车机仍需网络互通。' :
    '应用本地热点会自动提供连接参数；手填参数保留作备用。';
}
el('autoHotspot').onchange = wifiFields; el('car').onchange = wifiFields; el('network').onchange = wifiFields; el('iphoneLink').onchange = wifiFields;
let phonesSignature = '';
async function refreshPhones() {
  const value = await api('phones'), signature = JSON.stringify(value);
  if (signature === phonesSignature) return;
  phonesSignature = signature;
  const list = el('phones'); list.replaceChildren();
  if (value.wiredLive) {
    const block = document.createElement('div'), name = document.createElement('h3'), detail = document.createElement('p');
    name.textContent = '有线 · ' + (value.wiredName || 'iPhone');
    detail.textContent = 'CarPlay 连接中 · 数据线连接';
    block.append(name, detail); list.append(block);
  }
  if (!value.phones.length && !value.wiredLive) { list.textContent = '尚未添加 iPhone。'; return; }
  // Pairing needs Root: Android asks the person at this device to confirm, and a browser here cannot answer.
  const rootUsable = !!(state && state.capabilities && state.capabilities.rootUsable);
  for (const phone of value.phones) {
    const block = document.createElement('div'), name = document.createElement('h3'), detail = document.createElement('p'), actions = document.createElement('div');
    name.textContent = phone.name;
    detail.textContent = (phone.active ? 'CarPlay 连接中' : phone.paused ? '已暂停自动连接' : !phone.bonded ? '未配对' : '等待连接') + ' · 蓝牙 …' + phone.address.slice(-5);
    if (!phone.bonded) detail.textContent += rootUsable ? ' · 可在本页配对' : ' · 需在盒子屏幕上用遥控器确认配对';
    actions.className = 'actions';
    for (const [op,label] of [['connect','连接'],['disconnect','断开并暂停'],['remove','从 DashPlay 移除']]) {
      if (op === 'disconnect' && phone.paused) continue;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary'; button.textContent = label;
      button.dataset.unavailable = String(op === 'connect' && !phone.bonded); button.disabled = working || button.dataset.unavailable === 'true';
      button.onclick = () => {
        if (op === 'remove' && !confirm('从 DashPlay 移除这部 iPhone？系统蓝牙配对会保留。')) return;
        void action(async () => {
          await api('phones/action', {address:phone.address,action:op});
          await refreshPhones();
          notice(op === 'connect' ? '正在邀请 iPhone。' : op === 'disconnect' ? '已暂停自动连接，选择连接可恢复。' : '已从 DashPlay 移除，系统配对保留。');
        });
      };
      actions.append(button);
    }
    if (!phone.bonded && rootUsable) {
      const pair = document.createElement('button'); pair.type = 'button'; pair.className = 'secondary'; pair.textContent = '配对';
      pair.disabled = working;
      pair.onclick = () => void action(async () => {
        await api('phones/pair', {address:phone.address});
        notice('已在盒子后台发起配对，几秒后刷新查看。');
      });
      actions.append(pair);
    }
    block.append(name, detail, actions); list.append(block);
  }
}
async function refresh(fill = false) { render(await api('settings'), fill); await refreshPhones(); }
async function action(work) { if (working) return; busy(true); try { await work(); } catch (error) { notice(error.message || '暂时无法连接，请稍后刷新。', true); } finally { busy(false); } }
el('login-form').onsubmit = event => { event.preventDefault(); remember(credential(el('admin-link').value)); el('admin-link').value = ''; void action(async () => { await refresh(true); notice('已连接。'); }); };
el('settings-form').onsubmit = event => { event.preventDefault(); void action(async () => {
  const update = {};
  for (const key of ['deviceName','car','network','iphoneLink']) update[key] = el(key).value;
  update.idleReleaseSeconds = Number(el('idleReleaseSeconds').value);
  const needsWifi = manualWifi();
  if (needsWifi) {
    update.wifiSecurity = el('wifiSecurity').value;
    update.wifiChannel = Number(el('wifiChannel').value);
    // One field used to serve both meanings: the box's own hotspot and the external network the iPhone is told to
    // join. They are separate now, so changing the topology cannot leave a stale name behind.
    if (el('network').value === 'THIS_HOTSPOT') update.ssid = el('ssid').value;
    else {
      update.externalWifiChoice = el('externalWifiChoice').value;
      update.uplinkSsid = el('uplinkSsid').value;
      if (update.wifiSecurity === 'OPEN') update.uplinkPassphrase = '';
      else if (el('uplinkPassphrase').value) update.uplinkPassphrase = el('uplinkPassphrase').value;
    }
  }
  update.audioOutsideCarPlay = el('audioOutsideCarPlay').value === 'true';
  update.autoHotspot = el('autoHotspot').checked;
  if (!el('usbInternet').disabled) update.usbInternet = el('usbInternet').checked;
  if (!el('usbHotspotShare').disabled) update.usbHotspotShare = el('usbHotspotShare').checked && el('usbInternet').checked && el('network').value === 'THIS_HOTSPOT';
  update.ownNameOnHotspot = el('ownNameOnHotspot').checked;
  update.updateCheck = el('updateCheck').checked;
  update.updateMirror = el('updateMirror').value.trim();
  update.compatHttp = el('compatHttp').checked;
  update.browserPort = Number(el('browserPort').value);
  update.startAtBoot = el('startAtBoot').checked; update.setupDone = el('setupDone').checked;
  if (needsWifi && el('network').value === 'THIS_HOTSPOT') { if (update.wifiSecurity === 'OPEN') update.passphrase = ''; else if (el('passphrase').value) update.passphrase = el('passphrase').value; }
  render(await api('settings',update),true);
  const next = new URL(location.href);
  next.port = String(update.browserPort);
  const changedPort = next.origin !== location.origin;
  const link = el('saved-address');
  link.hidden = !changedPort;
  if (changedPort) {
    next.protocol = 'http:';
    next.pathname = '/manage'; next.search = ''; next.hash = '';
    link.href = next.href;
    link.textContent = '服务重启后打开：' + next.href;
  }
  notice(changedPort ? '已保存。端口已更改，请稍后打开下方新地址，保留 http:// 开头。' : '已保存。接收服务正在重新连接。');
}); };
el('identity-form').onsubmit = event => { event.preventDefault(); void action(async () => {
  const file = el('identity-file').files[0]; if (!file) return;
  if (file.size > 32*1024*1024) throw new Error('身份文件应在 32 MB 以内。');
  notice('正在导入身份……'); await api('identity',file,true); el('identity-file').value = ''; notice('身份已导入，接收服务正在重新连接。');
}); };
el('tls-form').onsubmit = event => { event.preventDefault(); void action(async () => {
  const chain = el('tls-chain').files[0], key = el('tls-key').files[0];
  if (!chain || !key) throw new Error('请选择证书链和私钥文件。');
  if (chain.size > 65536 || key.size > 16384) throw new Error('证书链或私钥文件过大。');
  const result = await api('tls/import', {domain:el('tls-domain').value,chain:await chain.text(),key:await key.text()});
  el('tls-chain').value = ''; el('tls-key').value = '';
  notice('证书已导入，接收服务正在重启。请通过域名 ' + result.domain + ' 访问；域名需解析到本机。');
}); };
for (const [id,enabled] of [['tls-enable',true],['tls-disable',false]]) el(id).onclick = () => { void action(async () => {
  await api('tls/enable',{enabled}); notice('证书设置已保存，接收服务正在重启。');
}); };
el('tls-download-ca').onclick = () => { void action(async () => {
  const response = await fetch('/api/android/ca.crt', {headers:{'X-DashPlay-Admin':token}, cache:'no-store'});
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || '无法下载证书。');
  }
  const text = await response.text(), url = URL.createObjectURL(new Blob([text],{type:'application/x-pem-file'}));
  const link = document.createElement('a'); link.href = url; link.download = 'dashplay-ca.crt'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  notice('证书已下载。');
}); };
// A press while the 5 s auto-refresh is in flight used to be dropped by action()'s working guard:
// the button said nothing and the page did not move. Wait for that refresh instead of ignoring the owner.
let refreshQueued = false;
el('refresh').onclick = () => {
  if (working) { refreshQueued = true; return; }
  void action(async () => { await refresh(); notice('状态已刷新。'); });
};
setInterval(() => {
  if (refreshQueued && !working && token) { refreshQueued = false; void action(async () => { await refresh(); notice('状态已刷新。'); }); }
}, 800);
el('restart').onclick = () => { void action(async () => { await api('restart',{}); notice('接收服务正在重启。'); }); };
el('diagnostics').onclick = () => { void action(async () => {
  const report = await api('diagnostics'), url = URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
  const link = document.createElement('a'); link.href = url; link.download = 'DashPlay-diagnostics.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
}); };
el('logs').onclick = () => { void action(async () => {
  // Plain text, not JSON: fetched directly instead of through api().
  const response = await fetch('/api/android/logs', {headers: {'X-DashPlay-Admin': token}, cache: 'no-store'});
  if (response.status === 403) { remember(''); el('login').hidden = false; el('content').hidden = true; }
  if (!response.ok) throw new Error('日志未能下载，请重试。');
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement('a'); link.href = url; link.download = 'DashPlay-log.txt'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  notice('运行日志已下载；其中的凭据、密码和私钥已去除。');
}); };
el('logout').onclick = () => { remember(''); phonesSignature = ''; el('phones').replaceChildren(); el('content').hidden = true; el('login').hidden = false; el('summary').textContent = '已退出管理。'; notice(''); };
el('tesla-access-disable').onclick = () => { void action(async () => { await api('tesla/disable', {}); notice('正在关闭，请稍后查看访问规则状态。'); }); };
el('update-check-btn').onclick = () => { void action(async () => {
  await api('update/check', {});
  await refresh();
  notice('已完成在线版本检查。');
}); };
// No management key: the page loads as soon as it opens (owner, 2026-11-03). Pasting a long token into a car
// browser was the whole problem this removes.
void action(() => refresh(true));
setInterval(() => { if (!working && !document.hidden) void refresh().catch(() => {}); },5000);

el('tesla-checks-form').onsubmit = event => { event.preventDefault(); void action(async () => { await api('tesla/checks', {enabled: el('tesla-checks').checked}); notice('已保存联网检查设置，正在应用。'); }); };

for (const id of ['usbInternet','network']) el(id).addEventListener('change', () => {
  if (!el('usbInternet').checked || el('network').value !== 'THIS_HOTSPOT') el('usbHotspotShare').checked = false;
});

el('externalWifiChoice').onchange = () => {
  if (el('externalWifiChoice').value === 'CURRENT') {
    const current = state?.settings?.currentWifiSsid;
    if (!current) { el('externalWifiChoice').value = ''; notice('无法读取当前 Wi-Fi 名称，请在 App 中允许读取，或选择其他 Wi-Fi 手动填写。', true); return; }
    el('uplinkSsid').value = current;
    el('uplinkPassphrase').value = '';
    el('uplink-password-state').textContent = state.settings.currentWifiPasswordSaved ? '已保存该网络密码，可直接保存。' : '请输入该 Wi-Fi 的密码；不会沿用其他网络的密码。';
  }
  el('uplinkSsid').readOnly = el('externalWifiChoice').value === 'CURRENT';
};
el('uplinkSsid').addEventListener('input', () => {
  el('uplinkPassphrase').value = '';
  el('uplink-password-state').textContent = '网络名称已修改，请填写该网络的密码。';
});
