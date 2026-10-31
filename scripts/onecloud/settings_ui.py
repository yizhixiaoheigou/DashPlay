"""HTML for the box settings and first-run pages, in the visual language of iOS Settings.

People arrive from an iPhone (captive-portal sheet, Safari) or the head unit's browser next to CarPlay,
so the page borrows what they already know: large title, inset grouped lists, the Wi-Fi list with
checkmark/lock/signal, Bluetooth-style device rows, system colours that follow light/dark. The one
element of its own is the connection chain (car hotspot - box - iPhone) at the top. Self-contained:
the box hotspot has no internet, so no web fonts or external files.
"""
from html import escape
import json

STYLE = """
.summary-row b{flex:0 0 85px}.cell.summary-row .value{white-space:normal;overflow-wrap:anywhere;min-width:0}.role+.role{margin-top:12px}

:root{--bg:#F2F2F7;--cell:#FFFFFF;--sep:rgba(60,60,67,.18);--label:#000;--secondary:rgba(60,60,67,.6);
--tint:#007AFF;--green:#34C759;--orange:#FF9500;--red:#FF3B30;--fill:rgba(120,120,128,.12);--press:#D1D1D6;
color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#000;--cell:#1C1C1E;--sep:rgba(84,84,88,.6);--label:#FFF;
--secondary:rgba(235,235,245,.6);--tint:#0A84FF;--green:#30D158;--orange:#FF9F0A;--red:#FF453A;
--fill:rgba(120,120,128,.24);--press:#3A3A3C}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--label);
font:17px/1.35 -apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Noto Sans CJK SC","Microsoft YaHei",system-ui,sans-serif;
-webkit-text-size-adjust:100%}
main{max-width:680px;margin:0 auto;padding:max(12px,env(safe-area-inset-top)) 16px 48px}
h1{font-size:34px;line-height:1.2;font-weight:700;letter-spacing:-.02em;margin:12px 4px 4px}
.sub{color:var(--secondary);font-size:15px;margin:0 4px 8px}
.group-title{color:var(--secondary);font-size:13px;margin:24px 16px 6px}
.gap{height:20px}
.foot{color:var(--secondary);font-size:13px;line-height:1.4;margin:6px 16px 0}
.group{background:var(--cell);border-radius:12px;overflow:hidden}
.cell{display:flex;align-items:center;gap:12px;min-height:46px;padding:10px 16px;position:relative;width:100%;
background:none;border:0;color:inherit;font:inherit;text-align:left;text-decoration:none}
.cell+.cell,.cell+details,details+.cell,details+details,form+form>.cell,.cell+form>.cell,form+.cell{border-top:.5px solid var(--sep)}
.group>form+form,.group>form+details,.group>details+form{border-top:.5px solid var(--sep)}
.cell .main{flex:1;min-width:0}
.cell .main b{font-weight:400;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell .main span{display:block;color:var(--secondary);font-size:13px}
.cell .value{color:var(--secondary);white-space:nowrap}
.cell.on .value{color:var(--green)}
button.cell,a.cell,label.cell,summary.cell{cursor:pointer}
button.cell:active,a.cell:active,label.cell:active,summary.cell:active{background:var(--press)}
.cell.action{color:var(--tint);justify-content:center}
.cell.destructive{color:var(--red);justify-content:center}
.chev{flex:none;display:block}
.chev::after{content:"";display:block;width:8px;height:8px;border:solid var(--secondary);border-width:0 2px 2px 0;transform:rotate(-45deg);margin-right:4px;flex:none}
details[open]>summary .chev::after{transform:rotate(45deg)}
summary{list-style:none} summary::-webkit-details-marker{display:none}
details>.inner{border-top:.5px solid var(--sep);background:var(--bg)}
details>.inner .cell{background:var(--cell)}
.check{width:22px;flex:none;color:var(--tint);visibility:hidden}
input.pick{position:absolute;opacity:0;pointer-events:none}
input.pick:checked~.check{visibility:visible}
.icon{width:22px;height:22px;flex:none;color:var(--secondary)}
.icon.on{color:var(--label)}
input[type=text],input[type=password]{flex:1;min-width:0;border:0;background:none;color:var(--label);font:inherit;padding:0;outline:none}
input::placeholder{color:var(--secondary)}
.btn{display:block;width:100%;min-height:50px;margin:20px 0 0;padding:14px 16px;border:0;border-radius:12px;background:var(--tint);
color:#fff;font:inherit;font-weight:600;text-align:center;text-decoration:none;cursor:pointer;transition:transform .1s ease-out}
.btn:active{transform:scale(.97)}
.btn.plain{background:var(--fill);color:var(--tint)}
.btn+form .btn,form+form .btn.plain{margin-top:10px}
:focus-visible{outline:3px solid var(--tint);outline-offset:2px}
.banner{border-radius:12px;padding:12px 16px;margin:12px 0 0;background:var(--cell);font-size:15px}
.banner.ok{box-shadow:inset 4px 0 var(--green)} .banner.bad{box-shadow:inset 4px 0 var(--red)} .banner.wait{box-shadow:inset 4px 0 var(--orange)}
.chain{display:flex;align-items:flex-start;background:var(--cell);border-radius:12px;padding:18px 12px 14px;margin:16px 0 0}
.node{flex:0 0 27%;min-width:0;text-align:center}
.node .glyph{width:52px;height:52px;margin:0 auto 6px;border-radius:50%;display:grid;place-items:center;background:var(--fill);color:var(--secondary)}
.node .glyph svg{width:28px;height:28px}
.node.on .glyph{background:var(--green);color:#fff}
.node.wait .glyph{background:var(--orange);color:#fff}
.node .name{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.node .state{font-size:12px;color:var(--secondary)}
.link{flex:1;height:3px;border-radius:2px;margin-top:25px;background:var(--fill)}
.link.on{background:var(--green)}
.link.wait{background:repeating-linear-gradient(90deg,var(--orange) 0 6px,transparent 6px 12px);background-size:12px 3px;animation:flow .9s linear infinite}
@keyframes flow{to{background-position:12px 0}}
@media (prefers-reduced-motion:reduce){.link.wait{animation:none}.btn{transition:none}}
section+ol.steps{margin-top:10px}
ol.steps{margin:0;padding:12px 16px 12px 36px;background:var(--cell);border-radius:12px} ol.steps li{margin:4px 0}
.code{font:600 48px/1.1 -apple-system,"SF Pro Display",system-ui,sans-serif;letter-spacing:.12em;text-align:center;
font-variant-numeric:tabular-nums;padding:20px 0 6px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px} .pair .btn{margin-top:16px}
.back{display:inline-block;color:var(--tint);text-decoration:none;margin:6px 0 0;padding:6px 4px;font-size:17px}
.back:active{opacity:.5}
.cell a{color:var(--tint);text-decoration:none}
.roles{display:grid;gap:12px;margin:16px 0 0}
.role{display:flex;gap:14px;align-items:flex-start;padding:16px;border-radius:14px;background:var(--cell);cursor:pointer;
position:relative;box-shadow:inset 0 0 0 2px transparent;transition:box-shadow .15s ease-out,transform .1s ease-out}
.role:active{transform:scale(.985)}
.role:has(input:checked){box-shadow:inset 0 0 0 2px var(--tint)}
.role .glyph{width:44px;height:44px;flex:none;border-radius:11px;display:grid;place-items:center;background:var(--tint);color:#fff}
.role .glyph svg{width:26px;height:26px}
.role.car .glyph{background:var(--green)}
.role b{display:block;font-size:17px;font-weight:600;margin:1px 0 3px}
.role .main span{display:block;color:var(--secondary);font-size:15px;line-height:1.4}
.role .note{margin-top:6px;font-size:13px!important}
.role .check{position:absolute;top:14px;right:14px}
.role input:checked~.check{visibility:visible}
.role.off{opacity:.45;cursor:default}
.secret{font-variant-numeric:tabular-nums;letter-spacing:.02em}
.spinner{width:22px;height:22px;flex:none;border-radius:50%;border:2.5px solid var(--sep);border-top-color:var(--tint);animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.role{transition:none}.spinner{animation-duration:2.4s}}
@media (min-width:960px){main.wide{max-width:1100px;display:grid;grid-template-columns:1fr 1fr;column-gap:28px;align-items:start}
main.wide>.span{grid-column:1/-1}}
"""

ICONS = {
    "car": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
           'stroke-linejoin="round" aria-hidden="true"><path d="M5 15.5 6.6 9.8A2 2 0 0 1 8.5 8.4h7a2 2 0 0 1 1.9 1.4L19 15.5"/>'
           '<rect x="3.5" y="15.5" width="17" height="3.5" rx="1.2"/><path d="M6 19v1.5M18 19v1.5"/></svg>',
    "box": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
           'stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="7.5" rx="2"/>'
           '<path d="M8.5 7.6a5 5 0 0 1 7 0M10.6 9.4a2 2 0 0 1 2.8 0"/><circle cx="8" cy="14.8" r=".9" fill="currentColor"/></svg>',
    "hotspot": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
               'stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="13" r="1.6" fill="currentColor"/>'
               '<path d="M8.6 16.4a4.8 4.8 0 0 1 0-6.8M15.4 9.6a4.8 4.8 0 0 1 0 6.8M5.8 19.2a8.8 8.8 0 0 1 0-12.4'
               'M18.2 6.8a8.8 8.8 0 0 1 0 12.4"/></svg>',
    "phone": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true"><rect x="7" y="2.8" width="10" height="18.4" rx="2.6"/>'
             '<path d="M10.8 5.2h2.4"/></svg>',
}
CHECK = ('<svg class=check viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" '
         'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>')
LOCK = ('<svg class=icon viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 10V8a4 4 0 1 1 8 0v2h.5A1.5 1.5 0 0 1 18 '
        '11.5v7a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 18.5v-7A1.5 1.5 0 0 1 7.5 10zm1.8 0h4.4V8a2.2 2.2 0 1 0-4.4 0z"/></svg>')

SCRIPT = """<script>
document.addEventListener('focusin',function(e){var c=e.target.closest&&e.target.closest('.manual');
if(c){var r=c.querySelector('input.pick');if(r)r.checked=true;}});
</script>"""


SCANNING_RELOAD = 2   # seconds between reloads while a Wi-Fi scan runs (scanning)


def page(title, body, refresh=False, wide=False):
    """refresh: True reloads every 3 s, a number every that many seconds."""
    meta = f'<meta http-equiv=refresh content={3 if refresh is True else refresh}>' if refresh else ''
    return (f'<!doctype html><html lang=zh-CN><head><meta charset=utf-8>'
            f'<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">{meta}'
            f'<title>{escape(title)}</title><style>{STYLE}</style></head>'
            f'<body><main{" class=wide" if wide else ""}>{body}</main>{SCRIPT}</body></html>')


def portal_page(address):
    """What the phone's captive-portal window shows during first-run setup: only the address, to open in a browser
    (the window closes, and the phone may leave the Wi-Fi, once the user switches away). The pixel tells the box the
    window drew it (settings_page.portal_reply)."""
    value = escape(address)
    body = ('<h1>在浏览器里继续设置</h1>'
            '<p class=sub>这个自动弹出的窗口一切出去就会关掉，手机还可能断开 WiFi。请改用浏览器完成设置。</p>'
            f'<div class=group><label class=cell><input id=portal-address type=text value="{value}" readonly '
            'aria-label=设置页地址 style="font-size:20px;font-weight:600"></label></div>'
            '<button class=btn type=button id=portal-copy>复制地址</button>'
            '<p class=foot>1. 点「复制地址」。<br>2. iPhone 点右上角「完成」，安卓点返回；手机问要不要保持连接时，选保持。<br>'
            '3. 打开 Safari 或常用的浏览器，把地址粘贴到地址栏打开。手机提示这个 WiFi 不能上网是正常的。</p>'
            '<img src="/portal-seen.gif" width=1 height=1 alt="" style="position:absolute;opacity:0">'
            "<script>document.getElementById('portal-copy').onclick=function(){var i=document.getElementById('portal-address'),ok=false;"
            "i.readOnly=false;i.focus();i.select();i.setSelectionRange(0,i.value.length);try{ok=document.execCommand('copy');}catch(e){}"
            "i.readOnly=true;i.blur();if(!ok&&navigator.clipboard){navigator.clipboard.writeText(i.value).then(function(){},function(){});ok=true;}"
            "this.textContent=ok?'已复制，去浏览器粘贴打开':'请长按上面的地址复制';};</script>")
    return page('打开设置页', body)


def wifi_icon(signal):
    level = 3 if signal >= -60 else 2 if signal >= -72 else 1
    arcs = [("M5 9.5a10 10 0 0 1 14 0", 3), ("M7.6 12.4a6.3 6.3 0 0 1 8.8 0", 2), ("M10.2 15.2a2.6 2.6 0 0 1 3.6 0", 1)]
    paths = "".join(f'<path d="{d}" opacity="{1 if level >= need else .3}"/>' for d, need in arcs)
    return (f'<svg class="icon on" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
            f'stroke-linecap="round" role=img aria-label="信号 {level} 格">{paths}'
            '<circle cx="12" cy="18" r="1.4" fill="currentColor" stroke="none"/></svg>')


def chain(hotspot, phone, first="car"):
    """hotspot/phone: (state, name, caption) with state on/wait/off. The box itself is always on.
    first: icon of the left node, the car hotspot ("car") or the box's own hotspot ("hotspot")."""
    def node(kind, value):
        state, name, caption = value
        return (f'<div class="node {state}"><div class=glyph>{ICONS[kind]}</div>'
                f'<div class=name>{escape(name)}</div><div class=state>{escape(caption)}</div></div>')
    return (f'<div class="chain span" role=img aria-label="{escape(hotspot[1])}：{escape(hotspot[2])}；'
            f'{escape(phone[1])}：{escape(phone[2])}">{node(first, hotspot)}<div class="link {hotspot[0]}"></div>'
            f'{node("box", ("on", "盒子", "运行中"))}<div class="link {phone[0]}"></div>{node("phone", phone)}</div>')


def banner(text, kind="ok"):
    return f'<p class="banner {kind} span" role=status>{escape(text)}</p>' if text else ""


def flash(message):
    if not message:
        return ""
    if message.startswith("错误："):
        return banner(message.removeprefix("错误："), "bad")
    return banner(message)


def hidden(nonce, **values):
    fields = {"nonce": nonce, **values}
    return "".join(f'<input type=hidden name={k} value="{escape(v)}">' for k, v in fields.items())


def group(title, cells, foot=""):
    head = f'<p class=group-title>{escape(title)}</p>' if title else '<div class=gap></div>'
    tail = f'<p class=foot>{foot}</p>' if foot else ""
    return f'<section>{head}<div class=group>{cells}</div>{tail}</section>'


def scanning(label):
    """Shown while the box scans for hotspots on its own thread; the page reloads every SCANNING_RELOAD s."""
    return (f'<div class=group><div class="cell scanning" role=status><span class=spinner aria-hidden=true></span>'
            f'<span class=main><b>正在扫描附近的{escape(label)}…</b><span>几秒钟，扫完自动显示列表。请先在车上打开它。</span></span></div></div>')


def network_picker(networks, selected, nonce, action, path, submit, foot="", extra="", label="车机热点"):
    """Hotspot chooser shaped like the iOS Wi-Fi list, plus a manual name and the password.
    label: what the box joins ("车机热点" or "随身 WiFi")."""
    rows = "".join(
        f'<label class=cell><input class=pick type=radio name=ssid value="{escape(n["ssid"])}"'
        f'{" checked" if n["ssid"] == selected else ""}>{CHECK}<span class=main><b>{escape(n["ssid"])}</b>'
        f'<span>{"5 GHz" if n["band"].startswith("5") else "2.4 GHz"}</span></span>{LOCK}{wifi_icon(n["signal"])}</label>'
        for n in networks)
    known = {n["ssid"] for n in networks}
    manual = bool(selected) and selected not in known
    rows += (f'<label class="cell manual"><input class=pick type=radio name=ssid value=__manual__{" checked" if manual else ""}>'
             f'{CHECK}<input type=text name=ssid_manual value="{escape(selected) if manual else ""}" '
             'placeholder="其他热点…" autocomplete=off aria-label="其他热点名称"></label>')
    empty = f'<p class=foot>附近没有扫描到热点。先打开{escape(label)}，再点「重新扫描」。</p>' if not networks else ""
    return (f'<form method=post action={path}>{hidden(nonce)}'
            f'{group("选取" + label, rows)}{empty}'
            f'{group("", "<label class=cell>" + LOCK + "<input type=password name=password placeholder=密码 autocomplete=off required minlength=8 maxlength=63 aria-label=热点密码></label>", foot)}'
            f'{extra}<button class=btn name=action value={action}>{escape(submit)}</button></form>'
            f'<form method=post action={path}>{hidden(nonce)}<button class="btn plain" name=action value=rescan>重新扫描</button></form>')


def phone_rows(devices, active, last, nonce, idle, paused=(), connecting=None):
    """Bluetooth-style 'My devices': tap a phone to connect it or remove it from the box."""
    if not devices:
        return '<div class=cell><span class=main><span>还没有添加手机</span></span></div>'
    html = ""
    for phone, name in devices.items():
        state = ("正在使用" if phone == active else
                 "已断开" if phone in paused else
                 "等待接入" if phone == connecting else
                 "上次使用 · 未连接" if phone == last else "已配对 · 未连接")
        # Keep the right-hand status short on narrow screens; the explanation can wrap below the name.
        detail = ("已断开 · 自动连接已暂停" if phone != active and phone in paused else
                  "蓝牙已配对，等待接入 CarPlay" if phone != active and phone == connecting else "")
        detail = f'<span>{detail}</span>' if detail else ""
        summary = (f'<summary class="cell{" on" if phone == active else ""}"><span class=main><b>{escape(name)}</b>'
                   f'<span>蓝牙 …{escape(phone[-5:])}</span>{detail}</span><span class=value>{state}</span><span class=chev></span></summary>')
        actions = ""
        form = f'<form method=post action=/pair>{hidden(nonce, phone=phone)}'
        if phone == active:
            actions += form + '<button class="cell action" name=action value=disconnect>断开 CarPlay</button></form>'
            actions += '<p class=foot>断开会保留配对，并暂停这部手机自动连接；点击「接入 CarPlay」可恢复。</p>'
        elif phone != connecting:
            # One button for both cases (2026-10-31): with a phone in use it takes CarPlay from it, exactly like
            # tapping its row in the CarPlay box settings; idle, it is simply the invitation.
            label = '接入 CarPlay' if idle else '切换到这部手机'
            actions += form + f'<button class="cell action" name=action value=switch>{label}</button></form>'
            if not idle:
                actions += ('<p class=foot>会结束正在用的那部手机的 CarPlay；它保留配对和 Wi-Fi，随时可以切回来。</p>')
        confirmation = escape('return confirm(' + json.dumps(f'从盒子上删除「{name}」？当前连接会结束，以后要用需要重新配对。', ensure_ascii=False) + ')', quote=True)
        actions += (form + f'<button class="cell destructive" name=action value=remove '
                    f'onclick="{confirmation}">删除此手机</button></form>')
        html += f'<details>{summary}<div class=inner>{actions}</div></details>'
    return html


def pairing_panel(pair, nonce, address=""):
    """The add-phone flow as its own screen. Returns (html, refresh), or (None, False) when idle.

    iOS shows an incoming pairing request only while Settings > Bluetooth is open, so the iPhone stays
    there and this page is operated on another screen, normally the head unit's browser.
    """
    phase = pair.get("phase", "closed")
    cancel = (f'<form method=post action=/pair>{hidden(nonce)}'
              '<button class="btn plain" name=action value=cancel>取消</button></form>')
    if phase == "confirm":
        return (f'<p class=group-title>配对码</p><div class=group><p class=code>{escape(pair.get("passkey", ""))}</p>'
                f'<p class=foot style="margin:0 16px 16px;text-align:center">「{escape(pair.get("name", ""))}」的蓝牙页面会弹出同样的数字。'
                '两边一致时，手机上点「配对」，这里点「一致」。</p></div>'
                f'<form method=post action=/pair>{hidden(nonce, request_id=pair.get("request_id", ""))}<div class=pair>'
                '<button class="btn plain" name=action value=no>不一致</button>'
                '<button class=btn name=action value=yes>一致</button></div></form>', True)
    if phase == "waiting":
        found = "".join(
            f'<form method=post action=/pair>{hidden(nonce, phone=f["address"])}'
            f'<button class=cell name=action value=pick>{ICONS["phone"].replace("<svg ", "<svg class=icon ", 1)}'
            f'<span class=main><b>{escape(f["name"])}</b><span>蓝牙 …{escape(f["address"][-5:])}</span></span>'
            '<span class=value>配对</span><span class=chev></span></button></form>'
            for f in pair.get("found", []))
        found = found or '<div class=cell><span class=main><span>正在寻找附近的 iPhone…</span></span></div>'
        error = banner(pair["error"], "bad") if pair.get("error") else ""
        return ('<p class=group-title>添加 iPhone</p><ol class=steps>'
                '<li>在要添加的 iPhone 上打开「设置 → 蓝牙」，<b>一直停在这一页</b>，配对请求只会在那里弹出。</li>'
                '<li>在这一页点它的名字。</li><li>手机弹出配对请求后，核对两边的六位数字。</li></ol>'
                f'{error}{group("附近的 iPhone", found)}{cancel}', True)
    if phase == "pairing":
        return (group("添加 iPhone", f'<div class=cell><span class=main><b>正在连接「{escape(pair.get("name", ""))}」</b>'
                      '<span>请看 iPhone 蓝牙页面上的配对请求。</span></span></div>') + cancel, True)
    if phase in ("starting", "finishing"):
        return group("添加 iPhone", '<div class=cell><span class=main><span>请稍候…</span></span></div>'), True
    return None, False


WIRED_STATES = {
    None: ("off", "未启动", "CarPlay 接收服务启动后可用。"),
    "cable": ("off", "等待插线", "用数据线把 iPhone 插到盒子上。"),
    "trust": ("wait", "等待信任", "请在 iPhone 上点「信任」，并输入锁屏密码。"),
    "starting": ("wait", "正在连接", "iPhone 已插上，正在打开 CarPlay。"),
    "live": ("on", "已连接", "有线 CarPlay 正在使用。"),
}


def wired_block(phase, title="用数据线", foot=""):
    """Wired CarPlay: how to plug the iPhone in and what the cable is doing now."""
    state, value, detail = WIRED_STATES.get(phase, WIRED_STATES[None])
    status = (f'<div class="cell{" on" if state == "on" else ""}"><span class=main><b>有线 CarPlay</b>'
              f'<span>{escape(detail)}</span></span><span class=value>{escape(value)}</span></div>')
    steps = ('<ol class=steps><li>用 iPhone 的数据线（能传数据的线，不是只能充电的线）插到盒子的 USB 口：'
             '玩客云插<b>远离网口</b>的那个 USB 口，N1 两个 USB 口都行。</li>'
             '<li>第一次插上时，iPhone 会问「要信任此电脑吗？」，点「信任」并输入锁屏密码，以后插上即用。</li>'
             '<li>在车机浏览器打开 CarPlay 画面的地址。</li></ol>')
    return group(title, status, foot) + (steps if phase != "live" else "")


def other_screen(address):
    """Where to operate pairing: the head unit, since the iPhone must stay on its Bluetooth page."""
    if not address:
        return ""
    return (f'<p class=group-title>在车机屏幕上操作</p><div class=group><p class=code style="font-size:28px;letter-spacing:0">'
            f'{escape(address)}</p><p class=foot style="margin:0 16px 16px;text-align:center">'
            '添加 iPhone 时，手机要一直停在蓝牙页面，所以请在车机浏览器输入上面的地址，在车机上完成这一步。</p></div>')


def hotspot_form(nonce, path, ssid, password, submit, foot=""):
    """Name and password of the box's own hotspot; the stored password is shown on request."""
    reveal = (f'<details><summary class=cell><span class=main><b>当前密码</b></span>'
              f'<span class=value>轻点显示</span><span class=chev></span></summary>'
              f'<div class=inner><div class=cell><span class="main secret"><b>{escape(password)}</b></span></div></div></details>'
              if password else "")
    rows = (f'<label class=cell><span class=main><b>名称</b></span><input type=text name=ssid value="{escape(ssid)}" '
            'required maxlength=32 autocomplete=off style="text-align:right" aria-label=热点名称></label>'
            f'<label class=cell><span class=main><b>新密码</b></span><input type=password name=password '
            f'placeholder="{"留空表示不修改" if password else "至少 8 位"}" {"" if password else "required "}minlength=8 maxlength=63 '
            'autocomplete=new-password style="text-align:right" aria-label=热点密码></label>' + reveal)
    return (f'<form method=post action={path}>{hidden(nonce)}{group("盒子热点", rows, foot)}'
            f'<button class=btn name=action value=hotspot>{escape(submit)}</button></form>')
