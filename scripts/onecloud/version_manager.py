#!/usr/bin/env python3
"""Authenticated version selection, retained under state/ when the main program is downgraded.

Installed only when the owner opens version selection. No release installs automatically.
The independent systemd/procd service survives restarting, downgrading and rebooting the main app.
"""
import argparse
import fcntl
import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import update

PORT = 8091
SERVICE = 'dashplay-versions'


def service_text(root, directory, port, openwrt=False):
    argv = [sys.executable, str(directory / 'version_manager.py'), 'serve', '--root', str(root), '--port', str(port)]
    command = ' '.join(shlex.quote(a) for a in argv)
    if openwrt:
        return (f'#!/bin/sh /etc/rc.common\nSTART=99\nUSE_PROCD=1\nstart_service() {{\n'
                f' procd_open_instance\n procd_set_param command {command}\n'
                ' procd_set_param respawn 3600 5 5\n procd_close_instance\n}\n')
    return ('[Unit]\nDescription=DashPlay version selection\nAfter=network.target\n'
            f'[Service]\nExecStart={command}\nRestart=on-failure\nUMask=0077\n'
            '[Install]\nWantedBy=multi-user.target\n')


def descriptor(root):
    return update.read(Path(root) / 'state/version-manager/access.json')


def manager_state(info):
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{info["port"]}/api/status',
                                         headers={'Authorization': 'Bearer ' + info['token']})
        with urllib.request.urlopen(request, timeout=1) as response:
            return json.load(response)
    except (OSError, ValueError, KeyError):
        return {}


def ready(info):
    return manager_state(info).get('manager') == 1


def recover_previous(root):
    """The updater before 11.02 did not write previous.json; its existing backup still identifies the release."""
    root = Path(root)
    target = root / 'state/version-manager/previous.json'
    current = update.current_version(root, update.is_openwrt())
    saved = update.read(target)
    if saved.get('version') and saved.get('installed') == current and saved['version'] != current:
        return
    backups = ((root.parent / 'dashplay-previous.tar', ('version.txt', './version.txt'), False),
               (root.parent / update.BACKUP, ('state/update-check.json',), True))
    for archive, names, is_json in backups:
        try:
            with tarfile.open(archive) as tar:
                for name in names:
                    try:
                        member = tar.getmember(name)
                    except KeyError:
                        continue
                    if not member.isfile() or member.size > 4096:
                        continue
                    data = tar.extractfile(member).read().decode()
                    version = json.loads(data).get('current') if is_json else data.strip()
                    key = update.version_key(version)
                    if key and key >= update.HISTORY_MIN and version != current:
                        update.save(target, {'version': version, 'installed': current})
                        return
        except (OSError, tarfile.TarError, ValueError, AttributeError, TypeError):
            continue


def ensure(root):
    directory = Path(root).resolve() / 'state/version-manager'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / 'setup.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _ensure(root)


def manager_compatible(state, openwrt):
    if openwrt:
        return state.get('openwrt_dns_compatibility') == 1
    return bool(state.get('wired_debian_update') and state.get('durable_program_backup')
                and state.get('platform_upgrade_check'))


def _ensure(root):
    """Keep a verified local copy, separate from the program files the upgrader replaces."""
    root = Path(root).resolve()
    directory = root / 'state/version-manager'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    recover_previous(root)
    info = descriptor(root)
    current = manager_state(info) if info else {}
    openwrt = update.is_openwrt()
    if current.get('manager') == 1:
        if not manager_compatible(current, openwrt):
            # Older versions intentionally keep this service across upgrades/downgrades. Refresh it when
            # reopening from the new settings page, retaining the port/token used by existing bookmarks.
            with open(root / 'state/update.lock', 'a') as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('正在安装，请完成后再打开版本选择') from None
                if manager_state(info).get('busy') or update.running(root):
                    raise ValueError('正在安装，请完成后再打开版本选择')
                copy_retained(directory)
                command = ['/etc/init.d/' + SERVICE, 'restart'] if openwrt else ['systemctl', 'restart', SERVICE + '.service']
                result = subprocess.run(command,
                                        capture_output=True, text=True, timeout=15)
                if result.returncode:
                    raise ValueError('版本选择入口刷新失败，请从设置页重试')
                for _ in range(20):
                    refreshed = manager_state(info)
                    if refreshed.get('manager') == 1 and manager_compatible(refreshed, openwrt):
                        return info
                    time.sleep(.1)
                raise ValueError('版本选择入口尚未恢复，请从设置页重试')
        return info
    port = info.get('port', PORT)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError('版本选择入口的端口设置不正确')
    with socket.socket() as probe:
        try:
            probe.bind(('0.0.0.0', port))
        except OSError:
            raise ValueError(f'版本选择端口 {port} 已被占用，未改动当前程序') from None
    copy_retained(directory)
    info = {'port': port, 'token': info.get('token') or secrets.token_urlsafe(32)}
    update.save(directory / 'access.json', info)
    (directory / 'access.json').chmod(0o600)
    openwrt = update.is_openwrt()
    unit = Path('/etc/init.d') / SERVICE if openwrt else Path('/etc/systemd/system') / (SERVICE + '.service')
    unit.write_text(service_text(root, directory, port, openwrt))
    unit.chmod(0o755 if openwrt else 0o644)
    commands = ([str(unit), 'enable'], [str(unit), 'start']) if openwrt else (
        ['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', SERVICE + '.service'])
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ValueError('版本选择入口没有启动，当前程序未改动')
    for _ in range(20):
        if ready(info):
            return info
        time.sleep(.1)
    raise ValueError('版本选择入口暂时没有响应，当前程序未改动')


def copy_retained(directory):
    origin = Path(__file__).resolve().parent
    # Prepare every file before replacing any, so an unreadable source cannot leave half a refresh.
    with tempfile.TemporaryDirectory(prefix='refresh-', dir=directory) as staging:
        copies = []
        for name, source in (('version_manager.py', origin / 'version_manager.py'), ('update.py', origin / 'update.py'),
                             ('update-key.pem', origin / 'update-key.pem'), ('upgrade.py', origin / 'firmware/upgrade.py')):
            if not source.is_file():
                source = directory / name
            if source.resolve() != (directory / name).resolve():
                temporary = Path(staging) / name
                shutil.copyfile(source, temporary)
                temporary.chmod(0o600)
                copies.append((temporary, directory / name))
        for source, target in copies:
            os.replace(source, target)


PAGE = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>DashPlay · 版本与更新</title><style>
body{font:17px system-ui;background:#111;color:#eee;max-width:700px;margin:0 auto;padding:24px}button,input{font:inherit}
button{min-height:48px;border:1px solid #777;border-radius:8px;padding:10px 16px;background:#252525;color:white;margin:6px 8px 6px 0}
button:focus-visible,input:focus-visible{outline:3px solid #82baff}button:disabled{opacity:.5}.version{display:block;width:100%;text-align:left}
p{line-height:1.6}small{color:#bbb}input{padding:12px;max-width:90%}#status{white-space:pre-wrap}a{color:#82baff}</style>
<h1>版本与更新</h1><p id="current">正在读取…</p>
<p>选择一个版本，只替换程序，保留个人设置、配对和证书。操作期间热点和 CarPlay 会暂时断开，请停车后使用。</p>
<!-- wired-update-note -->
<p><small>可以收藏此页。回退到旧版后，这个入口仍然保留。历史版本不支持的新功能会暂时不可用。</small></p>
<button id="previous" disabled>恢复更新前版本</button><button id="refresh">查看其他版本</button>
<div id="versions"></div><details><summary>列表加载不了？输入完整版本号</summary><p><label>版本号 <input id="version" placeholder="例如 2026.11.01.1"></label><button id="choose">选择</button></p></details>
<div id="review" hidden><h2 id="target"></h2><p id="notes"></p><button id="install" disabled>安装这个版本，保留设置</button><button id="cancel">取消</button></div>
<p id="status" role="status"></p><a id="back">返回盒子设置</a>
<script>
const $=id=>document.getElementById(id), key='dashplay-version-token';
let token=location.hash.slice(1)||sessionStorage.getItem(key)||'', target='', busy=false, previous='';
if(token)sessionStorage.setItem(key,token);
// Keep the fragment for a bookmark; fragments are never sent in HTTP requests or referrers.
$('back').href='http://'+location.hostname+'/';
async function api(path,data){const r=await fetch('/api/'+path,{method:data?'POST':'GET',headers:{'Authorization':'Bearer '+token,...(data?{'Content-Type':'application/json'}:{})},body:data?JSON.stringify(data):undefined,cache:'no-store'});const v=await r.json();if(!r.ok)throw Error(v.error||'操作没有完成');return v;}
function message(e){$('status').textContent=e.message||e;}
async function status(){try{const s=await api('status');busy=s.busy;previous=s.previous||'';$('current').textContent='当前版本：'+(s.current||'未知');$('previous').textContent=previous?'恢复更新前版本 '+previous:'暂无更新前版本';$('previous').disabled=busy||!previous;$('install').disabled=busy||!target;if(busy||s.progress.phase==='done'||s.progress.phase==='failed')message(s.progress.message||'正在处理…');}catch(e){message(e);}}
async function choose(version){target='';$('review').hidden=false;$('install').disabled=true;$('target').textContent='正在核对 '+version;try{const s=await api('preview',{version});target=s.version;$('target').textContent='切换到 '+target;$('notes').textContent=s.notes||'该版本没有更新说明';$('install').disabled=busy;message('已核对版本签名和兼容性。');}catch(e){message(e);}}
$('refresh').onclick=async()=>{try{message('正在读取版本列表…');const s=await api('history');$('versions').replaceChildren(...s.versions.map(v=>{const b=document.createElement('button');b.type='button';b.className='version';b.textContent=v;b.onclick=()=>choose(v);return b;}));message(s.versions.length?'请选择版本':'没有可选择的历史版本');}catch(e){message(e);}};
$('choose').onclick=()=>choose($('version').value.trim());$('previous').onclick=()=>choose(previous);
$('cancel').onclick=()=>{target='';$('review').hidden=true;};
$('install').onclick=async()=>{if(!target||busy)return;const version=target;target='';$('install').disabled=true;try{message((await api('install',{version})).message);$('review').hidden=true;await status();}catch(e){message(e);}};
status();setInterval(status,3000);
</script></html>'''


def make_server(root, token, address):
    root = Path(root)
    operation = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, code, value, html=False):
            data = value.encode() if html else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            for name, value in {'Content-Type': 'text/html; charset=utf-8' if html else 'application/json',
                                'Content-Length': str(len(data)), 'Cache-Control': 'no-store',
                                'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                                'X-Content-Type-Options': 'nosniff'}.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            return bool(token) and hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token)

        def do_GET(self):
            if self.path == '/':
                note = ('' if update.is_openwrt() else '<p>有线 CarPlay 可保持连接，请勿拔线：先下载、校验并备份，'
                        '再自动断开 CarPlay 安装。下载失败不会主动断开；无线 CarPlay 请先断开。</p>')
                return self.send(200, PAGE.replace('<!-- wired-update-note -->', note), True)
            if not self.allowed():
                return self.send(403, {'error': '请从盒子设置重新打开版本选择，或使用之前收藏的完整链接。'})
            try:
                if self.path == '/api/status':
                    return self.send(200, {'manager': 1, 'wired_debian_update': True,
                        'openwrt_dns_compatibility': getattr(update, 'OPENWRT_DNS_COMPATIBILITY', 0),
                        'durable_program_backup': update.durable_program_backup(Path(__file__).resolve().parent / 'upgrade.py'),
                        'platform_upgrade_check': update.platform_upgrade_check(Path(__file__).resolve().parent / 'upgrade.py'),
                        'current': update.current_version(root, update.is_openwrt()),
                        'previous': update.read(root / 'state/version-manager/previous.json').get('version'),
                        'busy': operation.locked() or update.running(root),
                        'progress': update.read(root / 'state/update-status.json')})
                if self.path == '/api/history':
                    return self.send(200, {'versions': update.history(root, update.is_openwrt())})
                self.send(404, {'error': '没有这个页面'})
            except Exception as error:
                self.send(400, {'error': str(error)})

        def do_POST(self):
            if not self.allowed():
                return self.send(403, {'error': '没有操作权限'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096 or self.headers.get('Content-Type') != 'application/json':
                    return self.send(400, {'error': '请求格式不正确'})
                version = json.loads(self.rfile.read(length)).get('version', '')
                if self.path == '/api/preview':
                    manifest = update.latest_manifest(root, update.is_openwrt(), version=version)
                    update.check_target(root, update.is_openwrt(), manifest)
                    return self.send(200, {'version': manifest['version'], 'notes': manifest.get('notes', '')})
                if self.path != '/api/install':
                    return self.send(404, {'error': '没有这个操作'})
                if update.running(root) or update.carplay_blocks_update(root, update.is_openwrt()) or not operation.acquire(blocking=False):
                    return self.send(409, {'error': '正在使用 CarPlay 或正在安装，请稍后再试'})
                def work():
                    try:
                        update.install(root, version=version)  # Reverify; never trust a previous preview.
                    finally:
                        operation.release()
                threading.Thread(target=work, name='version-install', daemon=True).start()
                self.send(202, {'message': '正在核对并安装所选版本，请保持供电。'})
            except Exception as error:
                self.send(400, {'error': str(error)})
    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['open', 'serve'])
    parser.add_argument('--root', type=Path, default=Path('/opt/dashplay'))
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'open':
        print(json.dumps(ensure(args.root)))
    else:
        info = descriptor(args.root)
        if not info.get('token'):
            raise SystemExit('Version manager has no access token')
        make_server(args.root, info['token'], ('0.0.0.0', args.port)).serve_forever()


if __name__ == '__main__':
    main()
