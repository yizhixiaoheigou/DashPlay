"""Plain-HTTP settings page on port 80: first-run setup, phones, hotspots, status, CA, own DuckDNS name.

Reachable in both modes (box hotspot: http://192.168.78.1/, in Tesla mode http://100.96.0.1/, car hotspot: http://<box address>/, and
http://dashplay.local/ in either). Only clients on the box's own LAN subnets may use it. Secrets are
written to 0600 files under state/ and never sent back to the browser; leaving a password field
empty keeps the stored value. Until setup is done, the page is the step-by-step setup flow.
On OpenWrt (managed=...) the router's admin owns the network: no setup flow, only iPhones and status.
Markup lives in settings_ui.py; this module holds the behaviour.
"""
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import audio_routes
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.parse

import debug_uplink
import own_domain
from pairing_window import PairingWindow
import settings_ui as ui
import viewer_port
import setup_wizard
import setup_flow
from setup_flow import FirstSetup, iphone_choices, limitations
import diagnostics
import update

MAX_BODY = 4096
CERT_BODY = 65536   # /cert only: a fullchain.pem and key.pem pasted as PEM text
# A phone shown the setup portal gets its connectivity checks answered "online" for this long (first-run setup).
PORTAL_SEEN_SECONDS = 3600
PIXEL = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,"
         b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")
APPLY_DELAY = 2.0   # let the browser receive the reply before the service restarts its network
# Receiver options only (supervisor.receiver_options): the CarPlay receiver restarts, the hotspot stays.
RECEIVER_ONLY = "已保存。CarPlay 几秒后重连一下，WiFi 不断。"
IDLE_RELEASE_MAX = 86400
CHECK_LOG_EVERY = 60   # s: one diag line per car connectivity check and reply, repeats counted into the next


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


_save = setup_wizard.save_json   # one save for the settings and the setup state


def _hotspot_credentials(form, old):
    ssid = (form.get("ssid") or "").strip()
    if not 1 <= len(ssid.encode()) <= 32 or any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("热点名称为 1–32 字节，不能含换行等控制字符。")
    password = form.get("password", "")
    if not password:
        password = old.get("passphrase", "")
    # WPA: 8–63 printable ASCII characters. A newline also became its own hostapd line (review 2026-10-12).
    if not (8 <= len(password) <= 63 and all(32 <= ord(c) <= 126 for c in password)):
        raise ValueError("热点密码为 8–63 个英文字母、数字或符号（可含空格）。")
    return ssid, password


def _car_credentials(form, old):
    ssid = (form.get("ssid") or "").strip()
    if not ssid or ssid == "__manual__":
        ssid = form.get("ssid_manual", "").strip()
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("请选择热点，或输入 1–32 字节的热点名称。")
    password = form.get("password", "")
    if not password and old.get("ssid") == ssid:
        password = old.get("passphrase", "")
    if not 8 <= len(password) <= 63:
        raise ValueError("热点密码为 8–63 个字符。")
    return ssid, password


MODE_TITLES = {"car_hotspot": "方式一：车机热点", "mifi": "方式二：随身 WiFi", "box_hotspot": "方式三：车机连盒子热点"}


def network_label(mode):
    """What the box joins in this way of use."""
    return "随身 WiFi" if mode == "mifi" else "车机热点"


def read_wired(path):
    """Phase of wired CarPlay written by the receiver (WiredBootstrap): cable/trust/starting/live, or None."""
    value = _load(path) if path else None
    return value.get("phase") if isinstance(value, dict) else None


# One sentence for one situation: shown where the add button would be, and returned when a POST slips
# through anyway (2026-10-31, owner's XS). CarPlay on the cable pauses every wireless invitation.
def internet_label(chosen, saved, current, tesla_lan_stable=False):
    """The 车机上网 banner: what the car is getting right now, in the owner's words."""
    phase, reason = current.get('phase'), current.get('reason')
    if tesla_lan_stable and reason:
        return reason
    if phase == 'online':
        return '已连上 ' + current.get('ssid', '') + '，车机可上网'
    if phase == 'wired':
        return ('已接 USB 共享网络，车机可上网' if chosen['car_internet'] == 'usb_tether'
                else '已接有线网络，车机可上网')
    if chosen['car_internet'] == 'usb_tether':
        if phase in ('connecting', 'recovering'):
            return '已找到 ' + saved.get('ssid', '手机热点') + '，正在连接'
        return '没插 USB 共享网络，也没找到之前的热点，车机暂时没网'
    if chosen['car_internet'] == 'phone_hotspot':
        # 2026-10-31: the owner asked for one short line here, not a list of causes.
        return ('没找到或无法连接 ' + saved.get('ssid', '手机热点') + '，已退回不上网。'
                '若该热点正常开启，可能与盒子不兼容：请重启盒子后重试；仍无法连接时，请更换其他热点设备。')
    return '不上网（特斯拉保持连接，自带音乐、地图不能用）' if chosen.get('tesla') else '不上网'


CABLE_BLOCKS_ADDING = ("现在用数据线连着 CarPlay，无线连接是暂停的，所以暂时不能添加手机。"
                       "要改用另一部已配对的手机，在下面点它的「切换到这部手机」（会自动改成无线）；"
                       "要添加新手机，拔掉数据线再来。数据线照常充电，选了 USB 共享网络也照常上网。")


class SettingsPage:
    def __init__(self, state: Path, status, on_change, allowed_networks, scanner=None, wired_status=None,
                 managed=None, run=None, retry_cert=None):
        """status() -> dict for display; on_change(reason) re-applies the configuration;
        wired_status: the receiver's wired CarPlay status file; managed: {"by": "OpenWrt", "admin": URL of
        its Wi-Fi settings} when the system, not this page, sets up the network; run: the service's runtime
        directory, which holds the remote-debugging window (debug_uplink.py; offered only with it);
        retry_cert() asks the own-domain worker to try the certificate now, without restarting the service."""
        self.managed = managed
        self.retry_cert = retry_cert
        self.run = run
        self.state = state
        self.pairing = PairingWindow(state.parent)
        self.car_file = state / "car-hotspot.json"
        self.wired_status = wired_status
        self.hotspot_file = state / "hotspot.json"
        self.ddns_file = state / "ddns.json"
        self.audio_routes_lock = threading.RLock()
        self.options_file = state / "options.json"   # also read by supervisor.py (idle_release, car_music, car_voice)
        self.ca_file = state / "tls" / "ca.crt"
        self.status, self.on_change, self.allowed = status, on_change, allowed_networks
        self.scanner = scanner
        self.server = None
        self.checks_seen, self.checks_lock = {}, threading.Lock()
        self.portal_seen = {}   # phone address -> when it was shown the setup portal

    def setup(self):
        return setup_wizard.load(self.state)

    def direct_capable(self):
        """The box can keep a joined hotspot and its own one for the iPhone at once (supervisor.direct_capable)."""
        return bool(self.status().get('direct_capable'))

    def wired_capable(self):
        """The box has a USB port for the iPhone (status wired_capable, absent on older status: yes)."""
        return self.status().get('wired_capable') is not False

    def band_note(self, label):
        """Under a hotspot list when the radio has no 5 GHz (status only_24ghz, Snapdragon 410 sticks): such hotspots
        never show up in it."""
        if not self.status().get('only_24ghz'):
            return ''
        return f'这台盒子的无线网卡只支持 2.4 GHz，5 GHz 的{label}不会出现在列表里；要连它请先把它改成 2.4 GHz（画面可能偏卡）。'

    def networks(self):
        """The scanned hotspots, or None while a scan runs (the page then shows "scanning" and reloads)."""
        if not self.scanner:
            return []
        return self.scanner.listing() if hasattr(self.scanner, 'listing') else self.scanner.results()

    def rescan(self):
        if self.scanner:
            self.scanner.refresh(fresh=True) if hasattr(self.scanner, 'refresh') else self.scanner.results(fresh=True)

    def apply_later(self, reason):
        threading.Timer(APPLY_DELAY, self.on_change, args=(reason,)).start()

    def start(self, port=80):
        page = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):   # keep tokens/passwords out of the journal
                pass

            def _allowed(self):
                client = ipaddress.ip_address(self.client_address[0].removeprefix("::ffff:"))
                return client.is_loopback or any(client in net for net in page.allowed())

            def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                host = self.headers.get("Host", "")
                reply = page.tesla_check(host, self.path)
                page.note_check(host, self.path, reply[0] if reply else "off")
                if reply:
                    return self._send(reply[0], reply[1], "text/html", reply[2])
                if not self._allowed():
                    return self._send(403, "forbidden")
                portal = page.portal_reply(host, self.path, self.client_address[0].removeprefix("::ffff:"))
                if portal:
                    return self._send(portal[0], portal[1], portal[2], portal[3])
                if self.path == "/ca.crt" and page.ca_file.exists():
                    return self._send(200, page.ca_file.read_bytes(), "application/x-x509-ca-cert",
                                      {"Content-Disposition": 'attachment; filename="dashplay-ca.crt"'})
                if self.path == "/audio-routes":
                    try:
                        return self._send(200, json.dumps({'routes': page.sound_routes(), 'nonce': page.pairing.nonce}),
                                          "application/json")
                    except (ValueError, OSError):
                        return self._send(503, '{"error":"settings unavailable"}', "application/json")
                if self.path == "/diag.txt":
                    return self._send(200, diagnostics.DiagLog(page.state / "diag").text(), "text/plain; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="dashplay-diag.txt"'})
                if self.path.split("?")[0] != "/":
                    return self._send(404, "not found")
                self._send(200, page.render(self.path.partition("?")[2]))

            def do_POST(self):
                if not self._allowed():
                    return self._send(403, "forbidden")
                length = int(self.headers.get("Content-Length") or 0)
                if length > (CERT_BODY if self.path == "/cert" else MAX_BODY):
                    return self._send(413, "too large")
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode(),
                                                                 keep_blank_values=True).items()}
                if self.path == "/audio-routes":
                    try:
                        routes = page.apply(self.path, form)
                        return self._send(200, json.dumps({'routes': routes}), "application/json")
                    except (ValueError, OSError) as error:
                        return self._send(400, json.dumps({'error': str(error)}), "application/json")
                back = {"/setup-flow": "/?view=setup&", "/car": "/?view=car&", "/hotspot": "/?view=hotspot&"}.get(self.path, "/?")
                try:
                    result = page.apply(self.path, form)
                except ValueError as error:
                    result = "错误：" + str(error)
                if isinstance(result, tuple) and result[0] == "go":
                    return self._send(303, "", extra={"Location": result[1]})
                if isinstance(result, tuple):   # a full page: the network is about to change
                    return self._send(200, result[1])
                self._send(303, "", extra={"Location": back + urllib.parse.urlencode({"m": result})})

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, name="settings-page", daemon=True).start()

    def stop(self):
        self.pairing.stop()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def home(self):
        return self.status().get("home", "/")

    def tesla_check(self, host, path="/"):
        status = self.status()
        enabled = status.get("tesla_offline") if self.managed or status.get("tesla_lan_stable") else setup_wizard.tesla_offline(
            self.setup(), setup_wizard.options(self.state),
            status.get("uplink", {}).get("phase") in ("online", "wired"))
        return setup_wizard.check_response(host, path) if enabled and status.get("mode") in ("ap", "openwrt") else None

    def note_check(self, host, path, reply):
        """A car's request for a check name into state/diag/ (host, path, our reply or "off"), so a user's diagnostics
        show which checks the car makes and what it got. Only these names, never the settings page's own requests."""
        name = setup_wizard.check_name(host)
        if not name:
            return
        key, now = (name, path.split("?")[0][:100], reply), time.monotonic()
        with self.checks_lock:
            last, repeats = self.checks_seen.get(key, (None, 0))
            if last is not None and now - last < CHECK_LOG_EVERY:
                self.checks_seen[key] = (last, repeats + 1)
                return
            self.checks_seen[key] = (now, 0)
        record = {"uptime": round(now, 1), "event": "tesla_check", "host": name, "path": key[1], "reply": reply}
        if repeats:
            record["repeats"] = repeats
        try:
            diagnostics.DiagLog(self.state / "diag").append(record)
        except OSError:
            pass

    def portal_active(self):
        """The box hotspot during first-run setup: the open hotspot's first questions, and the whole one-question
        setup until its summary is confirmed."""
        setup, mode = self.setup(), self.status().get("mode")
        return not self.managed and (setup_wizard.captive(setup, mode) or mode == "ap" and setup.get("wizard_version") == 2
                                     and not setup.get("confirmed") and not setup.get("done"))

    def portal_reply(self, host, path, client):
        """(status, body, content type, headers) for a request that is not the setup page itself during first-run setup,
        or None. Two users (2026-10-17): "next" often did nothing until they left and joined the Wi-Fi again - the setup
        ran in the phone's captive-portal window, which the phone closes, leaving the Wi-Fi, once the user switches away.
        So the window (any other name or path: the phones' checks) only shows the address to open in a browser, and
        once it drew that page (its pixel), that phone's own checks pass: it keeps the Wi-Fi when the window closes."""
        if not self.portal_active():
            return None
        if path.split("?")[0] == "/portal-seen.gif":
            with self.checks_lock:
                self.portal_seen[client] = time.monotonic()
            return 200, PIXEL, "image/gif", {}
        own = urllib.parse.urlsplit(self.home()).hostname
        name = host.split(":")[0].lower().rstrip(".")
        if name in (own, setup_wizard.MDNS_NAME) and path.split("?")[0] in ("/", "/ca.crt"):
            return None
        with self.checks_lock:
            seen = self.portal_seen.get(client)
        if seen is not None and time.monotonic() - seen < PORTAL_SEEN_SECONDS:
            online = setup_wizard.online_reply(name, path)
            if online:
                return online
        return 200, ui.portal_page(self.home()), "text/html; charset=utf-8", {}

    def check_nonce(self, form):
        if not secrets.compare_digest(form.get("nonce", ""), self.pairing.nonce):
            raise ValueError("页面已过期，请刷新后重试。")

    # Behaviour -------------------------------------------------------------------------------

    def apply(self, path, form):
        if self.managed and path not in ("/pair", "/update", "/audio-routes"):
            raise ValueError(f"网络在 {self.managed['by']} 后台设置。")
        if path == "/audio-routes":
            self.check_nonce(form)
            # One switch (audio_routes.resolve): "navigation" is the CarPlay-or-not choice; an older viewer that still
            # offers media/Siri/calls must be reloaded rather than appear to save something that does nothing.
            name, value = form.get('category'), form.get('destination')
            if name != 'navigation' or value not in audio_routes.DESTINATIONS:
                raise ValueError('请刷新画面页后重新选择声音输出。')
            with self.audio_routes_lock:
                routes = self.sound_routes()
                if routes['navigation'] != value:
                    routes = self.output_routes(value)
                    self.save_sound_routes(routes)
            return routes
        if path == "/pair":
            self.check_nonce(form)
            action = form.get("action")
            if action == "start":
                # CarPlay on the cable pauses every wireless invitation (WiredBootstrap -> setCablePhone):
                # pairing now would succeed and then wait forever. Stop before the phone is touched, with the
                # same sentence the page shows where the button would be (2026-10-31, owner's XS).
                if self.wired_status and read_wired(self.wired_status) == "live":
                    raise ValueError(CABLE_BLOCKS_ADDING)
                self.pairing.start()
                return ""
            if action == "cancel":
                self.pairing.stop()
                return "已取消添加。"
            if action == "pick":
                self.pairing.pick(form.get("phone", ""))
                return ""
            if action == "disconnect":
                self.pairing.disconnect(form.get("phone", ""))
                return "已断开 CarPlay，配对已保留。这部手机暂停自动连接；点击「接入 CarPlay」即可恢复。"
            if action == "remove":
                self.pairing.remove(form.get("phone", ""))
                return "已从盒子上删除。这部手机以后要用，需要重新添加；也可以在它的「设置 → 通用 → CarPlay」里忘记此车辆。"
            if action == "use":
                self.pairing.use(form.get("phone", ""))
                return "已邀请这部手机接入，约 1 分钟内有效。请保持它的蓝牙打开。"
            if action == "switch":
                if not setup_wizard.wireless(self.setup(), setup_wizard.options(self.state), self.direct_capable()):
                    raise ValueError('这台盒子只用有线 CarPlay，没有可以切换的无线手机。')
                self.pairing.switch_to(form.get("phone", ""))
                return "已请求切换，十秒左右把 CarPlay 交给这部手机；它保持蓝牙和 Wi-Fi 打开即可。"
            if action in ("yes", "no"):
                self.pairing.confirm(form.get("request_id", ""), action == "yes")
                return "" if action == "yes" else "已拒绝这次配对。"
            raise ValueError("未知操作。")
        if path == "/update":
            self.check_nonce(form)
            if form.get("action") == "check":
                threading.Thread(target=update.check, args=(self.state.parent,), name="update-check", daemon=True).start()
                return "正在检查更新，几秒后刷新页面就能看到结果。"
            if form.get("action") == "install":
                return update.launch(self.state.parent)
            raise ValueError("未知操作。")
        if path == "/setup-flow":
            self.check_nonce(form)
            return FirstSetup(self).apply(form)
        if path in ("/internet", "/iphone"):
            self.check_nonce(form)
            return self.save_connection(path, form)
        if path == "/direct":
            self.check_nonce(form)
            if self.setup().get('mode') not in setup_wizard.JOINS or not self.direct_capable():
                raise ValueError('只有连车机热点或随身 WiFi、且盒子网卡支持时才能直连。')
            chosen = setup_wizard.options(self.state)
            chosen['direct_phone'] = form.get('direct_phone') == '1'
            chosen['direct_phone_internet'] = form.get('direct_phone_internet') == '1'
            _save(self.options_file, chosen)
            self.apply_later('direct phone changed')
            return '已保存，约 1 分钟后生效，期间 CarPlay 会断一下。'
        if path == "/setup":
            self.check_nonce(form)
            return self.apply_setup(form)
        if path == "/reset":
            self.check_nonce(form)
            self.pairing.reset_all()
            # The Lockdown pair record too: the next iPhone on the cable asks "Trust this computer?" again.
            for name in ("car-hotspot.json", "car-lease.json", "network-mode.json"):
                (self.state / name).unlink(missing_ok=True)
            for record in (self.state / "receiver").glob("lockdown-pair-record*.properties"):   # one per iPhone
                record.unlink(missing_ok=True)
            # And the receiver's CarPlay pairings, else the box still knew the old iPhones after a reset.
            (self.state / "receiver/pairings.properties").unlink(missing_ok=True)
            (self.state / "setup-draft.json").unlink(missing_ok=True)
            setup_wizard.save(self.state, {"done": False, "wizard_version": 2})
            self.apply_later("setup reset")
            return ("page", self.notice_page(
                "正在恢复首次设置", ("off", "热点", "已清除"), ("off", "iPhone", "已清除"),
                ["盒子约 1 分钟后打开自己的热点。",
                 f"手机连接「{self.status().get('box_ssid', '')}」，不需要密码，会自动弹出首次设置。",
                 f"没有弹出时，用浏览器打开 http://{setup_wizard.MDNS_NAME}/ 。"]))
        if path == "/car":
            self.check_nonce(form)
            action = form.get("action")
            if action == "rescan":
                self.rescan()
                return ""
            ssid, password = _car_credentials(form, _load(self.car_file) or {})
            _save(self.car_file, {"ssid": ssid, "passphrase": password})
            self.apply_later("car hotspot saved")
            return ("page", self.switching_page(ssid))
        if path == "/hotspot":
            self.check_nonce(form)
            ssid = self.save_hotspot(form)
            if self.status().get("mode") != "ap":
                return f"已保存。盒子下次打开自己的热点时使用「{ssid}」。"
            self.apply_later("box hotspot changed")
            return ("page", self.reconnect_page(ssid, "正在更改盒子热点"))
        if path == "/ddns":
            self.check_nonce(form)
            return self.save_ddns(form)
        if path == "/cert":
            self.check_nonce(form)
            return self.apply_certificate(form)
        if path == "/audio":
            self.check_nonce(form)
            return self.save_car_audio(form)
        if path == "/viewer-port" and not self.managed:
            self.check_nonce(form)
            return self.save_viewer_port(form)
        if path == "/idle-release" and not self.managed:
            self.check_nonce(form)
            return self.save_idle_release(form)
        if path == "/debug" and self.run:
            self.check_nonce(form)
            return self.apply_debug(form)
        if path == "/retry":
            self.check_nonce(form)
            self.on_change("retry requested")
            return f"正在重新连接{network_label(self.setup().get('mode'))}。"
        raise ValueError("未知操作。")

    def apply_debug(self, form):
        """Remote debugging (debug_uplink.py): save the owner's phone hotspot, open or end the window."""
        action = form.get("action")
        saved = debug_uplink.hotspot(self.state) or debug_uplink.hotspot(self.state, filename="uplink-hotspot.json")
        if action == "save":
            ssid, password = _car_credentials(form, saved or {})
            _save(self.state / debug_uplink.HOTSPOT, {"ssid": ssid, "passphrase": password})
            return f"已保存手机热点「{ssid}」。要远程调试时再点「连手机热点调试」。"
        if action == "forget":
            (self.state / debug_uplink.HOTSPOT).unlink(missing_ok=True)
            return "已删除保存的手机热点。"
        if action == "start":
            if not debug_uplink.reachable():
                raise ValueError("盒子没有装 Tailscale，连上手机热点也没人能远程登录。")
            if not saved:
                raise ValueError("先保存手机热点的名称和密码。")
            _save(self.state / debug_uplink.HOTSPOT, saved)  # independent copy, may later use another phone
            debug_uplink.start(self.run)
            self.apply_later("remote debugging started")
            box = self.status().get("box_ssid", "")
            return ("page", self.notice_page(
                "正在连接手机热点", ("wait", saved["ssid"], "约 1 分钟"), ("off", "车机画面", "调试期间暂停"),
                [f"打开手机热点「{saved['ssid']}」，盒子约 1 分钟后连上它，并关掉自己的热点「{box}」。",
                 "告诉帮你调试的人可以开始了；iPhone 插数据线时有线 CarPlay 照常可用。",
                 f"调试中想恢复：给盒子拔电再插上，就回到原来的网络；或等 {debug_uplink.MINUTES} 分钟自动结束。"],
                f"连不上手机热点时，盒子约 1 分钟后重新打开自己的热点「{box}」，并一直找手机热点，直到 {debug_uplink.MINUTES} 分钟到。"))
        if action == "stop":
            debug_uplink.stop(self.run)
            self.apply_later("remote debugging stopped")
            return "已结束远程调试，盒子约 1 分钟后回到原来的网络。"
        raise ValueError("未知操作。")

    def update_block(self, nonce):
        """Program update (update.py): what the daily check found, the last outcome and the button. The update itself
        runs apart from this service, which it restarts."""
        found = update.read(self.state / "update-check.json")
        progress = update.read(self.state / "update-status.json")
        if update.running(self.state.parent):
            return (f'<div class=cell><span class=main><b>正在升级</b><span>{escape(progress.get("message", ""))}</span>'
                    '</span></div><p class=note>约 2–5 分钟，期间 CarPlay 会断开，页面会自己刷新。</p>')
        version = update.bundled_version(self.state.parent)
        cells = (f'<div class=cell><span class=main><b>本机程序</b></span><span class=value>{escape(version)}</span></div>'
                 if version else "")
        # A failure no longer matters once a later check finds the box up to date (updated by hand, say).
        settled = progress.get("phase") == "failed" and found.get("latest") and not found.get("available") \
            and not found.get("error") and found.get("checked_at", 0) > progress.get("at", 0)
        if progress.get("phase") in ("done", "failed") and time.time() - progress.get("at", 0) < 86400 and not settled:
            cells += (f'<div class=cell><span class=main><b>{"上次升级" if progress["phase"] == "done" else "上次升级没有完成"}</b>'
                      f'<span>{escape(progress.get("message", ""))}</span></span></div>')
        if found.get("error"):
            cells += f'<div class=cell><span class=main><b>检查更新没有成功</b><span>{escape(found["error"])}</span></span></div>'
        elif found.get("available"):
            notes = escape(found.get("notes", "")).replace("\n", "<br>")
            cells += (f'<div class=cell><span class=main><b>有新版本 {escape(found["latest"])}</b><span>{notes}</span></span>'
                      f'<span class=value>当前 {escape(found.get("current") or "较旧版本")}</span></div>')
            cells += (f'<form method=post action=/update>{ui.hidden(nonce)}<button class="cell action" name=action '
                      'value=install>立即升级（约 2–5 分钟，期间 CarPlay 会断开）</button></form>' if found.get("online") else
                      f'<p class=note>{escape(found.get("reason") or "这一版改了系统")}，不能在线升级：请按装机说明用刷机包升级。</p>')
        elif found.get("latest"):
            cells += (f'<div class=cell><span class=main><b>已是最新</b></span>'
                      f'<span class=value>{escape(found["latest"])}</span></div>')
        checked = (time.strftime("上次检查 %m-%d %H:%M", time.localtime(found["checked_at"]))
                   if found.get("checked_at") else "还没有检查过")
        return cells + (f'<form method=post action=/update>{ui.hidden(nonce)}<button class="cell action" name=action '
                        f'value=check>检查更新</button></form><p class=note>{checked}</p>')

    def debug_block(self, nonce):
        """Advanced: remote debugging over the owner's phone hotspot."""
        saved = debug_uplink.hotspot(self.state) or debug_uplink.hotspot(self.state, filename="uplink-hotspot.json")
        left = debug_uplink.remaining(self.run)
        if left:
            return (f'<div class=cell><span class=main><b>远程调试中</b><span>已连手机热点「{escape(saved["ssid"] if saved else "")}」</span>'
                    f'</span><span class=value>还剩 {max(1, round(left / 60))} 分钟</span></div>'
                    f'<form method=post action=/debug>{ui.hidden(nonce)}<button class="cell action" name=action value=stop>'
                    '结束调试，回到原来的网络</button></form><p class=note>调试中想恢复：给盒子拔电再插上，就回到原来的网络；或等 30 分钟自动结束。</p>')
        form = (f'<form method=post action=/debug>{ui.hidden(nonce)}<div class=group>'
                '<label class=cell><span class=main><b>手机热点名称</b></span><input type=text name=ssid_manual '
                f'value="{escape(saved["ssid"] if saved else "")}" required maxlength=32 autocapitalize=off '
                'style="text-align:right"></label>'
                '<label class=cell><span class=main><b>密码</b></span><input type=password name=password '
                f'placeholder="{"留空表示不修改" if saved else "8–63 个字符"}" autocomplete=off style="text-align:right"></label>'
                '</div><button class="btn plain" name=action value=save>保存</button></form>')
        actions = ((f'<form method=post action=/debug>{ui.hidden(nonce)}<button class="cell action" name=action value=start '
                    f'onclick="return confirm(\'盒子会关掉自己的热点 {debug_uplink.MINUTES} 分钟，车机画面暂停，确定？\')">'
                    f'连手机热点调试 {debug_uplink.MINUTES} 分钟</button></form>'
                    f'<form method=post action=/debug>{ui.hidden(nonce)}<button class="btn plain" name=action value=forget>'
                    '删除保存的手机热点</button></form>') if saved else "")
        return (f'<details><summary class=cell><span class=main><b>远程调试</b><span>车上出问题时，让盒子临时连你的手机热点上网</span></span>'
                f'<span class=value>{escape(saved["ssid"] if saved else "未设置")}</span><span class=chev></span></summary>'
                f'<div class=inner style="padding:0 0 12px">{form}<p class=note>调试中想恢复：给盒子拔电再插上，就回到原来的网络；或等 30 分钟自动结束。</p></div></details>{actions}')

    def save_ddns(self, form):
        """Own DuckDNS name (own_domain.py); a new name or turning it off drops the old name's certificate.
        A deSEC name is configured when the box is prepared (state/ddns.json), not from this page."""
        old = own_domain.load(self.state)
        if old and old.get("provider") == own_domain.DESEC:
            raise ValueError("这个域名是刷机时配置的，不能在这里修改。")
        if form.get("action") == "clear":
            self.ddns_file.unlink(missing_ok=True)
            own_domain.forget_certificate(self.state)
            self.apply_later("own domain cleared")
            return "已关闭自己的域名，盒子约 1 分钟后改回自带证书。"
        domain = own_domain.sub_domain(form.get("domain"))
        token = (form.get("token") or "").strip()
        if not token and old and old["domain"] == domain:
            token = old["token"]   # left empty: keep the saved one
        token = own_domain.check_token(token)
        if not old or old["domain"] != domain:
            own_domain.forget_certificate(self.state)
        # Public IPv6 only where the box joins a hotspot: on its own hotspot it has no public address.
        ipv6 = form.get("ipv6") == "on" and self.setup().get("mode") in setup_wizard.JOINS
        _save(self.ddns_file, {"domain": domain, "token": token, **({"ipv6": True} if ipv6 else {})})
        self.apply_later("own domain saved")
        return (f"已保存 {domain}{own_domain.SUFFIX}。盒子约 1 分钟后重启服务，联网时自动更新解析并申请正式证书"
                "（几分钟），下次开机起用。")

    def apply_certificate(self, form):
        """Try the certificate now, or import one from elsewhere (own_domain.py). A new certificate is loaded when
        the service starts, so an import restarts it after the reply has gone out."""
        config = own_domain.load(self.state)
        if not config:
            raise ValueError("先保存自己的域名，再申请或导入证书。")
        action = form.get("action")
        if action == "retry":
            if not self.retry_cert:
                raise ValueError("当前页面不能触发申请。")
            self.retry_cert()
            return "已请求重新申请正式证书：盒子联网时会立即开始，通常几分钟；新证书下次开机时启用。"
        if action == "import":
            own_domain.import_certificate(self.state, own_domain.name_of(config),
                                          form.get("chain", ""), form.get("key", ""))
            self.apply_later("certificate imported")
            return "证书已导入，服务约 2 秒后重启并启用；之后打开 https://域名 不再有证书警告。"
        raise ValueError("未知操作。")

    def certificate_actions(self, nonce, ddns, name):
        """The same retry/import controls for owner-configured and supplied domains."""
        actions = ""
        if ddns and self.retry_cert and not own_domain.certificate_for(self.state, name, own_domain.RENEW_DAYS):
            actions += (f'<form method=post action=/cert>{ui.hidden(nonce)}'
                        '<button class="btn plain" name=action value=retry>重新申请证书</button></form>')
        if ddns:
            actions += (
                '<details><summary class=cell><span class=main><b>手动导入证书</b>'
                '<span>申请一直不成功时，把别处拿到的证书放进来</span></span><span class=chev></span></summary>'
                f'<div class=inner style="padding:0 0 12px"><form method=post action=/cert>{ui.hidden(nonce)}'
                '<label class=cell><span class=main><b>证书链 fullchain.pem</b></span></label>'
                '<textarea name=chain rows=5 required spellcheck=false style="width:100%" '
                'placeholder="-----BEGIN CERTIFICATE-----"></textarea>'
                '<label class=cell><span class=main><b>私钥 key.pem</b></span></label>'
                '<textarea name=key rows=4 required spellcheck=false style="width:100%" '
                'placeholder="-----BEGIN PRIVATE KEY-----"></textarea>'
                '<button class="btn plain" name=action value=import '
                'onclick="return confirm(\'导入后服务会重启一下，确定？\')">导入并重启服务</button></form>'
                '<p class=foot>两段都要是 PEM 文本，证书必须是这个子域名的（名字对不上会被拒绝），私钥要和证书成对。'
                '证书只写到盒子本地，不会上传；服务重启后才生效。</p></div></details>')
        return actions

    def ddns_block(self, nonce, mode):
        """Advanced: the own DuckDNS name. On OpenWrt LuCI sets it; this page only tells how it is going."""
        name = self.status().get("own_name")
        if self.managed:
            return (f'<div class=cell><span class=main><b>自己的域名</b><span>{escape(own_domain.message(self.state, name))}'
                    f'</span></span><span class=value>{escape(name)}</span></div>') if name else ""
        ddns = own_domain.load(self.state) or {}
        name = name or own_domain.name_of(ddns)
        if ddns.get("provider") == own_domain.DESEC:
            return (f'<div class=cell><span class=main><b>自己的域名</b><span>{escape(own_domain.message(self.state, name, mode == "box_hotspot"))}</span></span><span class=value>{escape(name)}</span></div>'
                    '<p class=foot>这个域名已由设备提供方配置。证书到期前，盒子联网会自动续期。'
                    '更换域名请联系设备提供方。</p>'
                    + self.certificate_actions(nonce, ddns, name))
        form = (f'<form method=post action=/ddns>{ui.hidden(nonce)}<div class=group>'
                f'<label class=cell><span class=main><b>子域名</b></span><input type=text name=domain '
                f'value="{escape(ddns.get("domain", ""))}" placeholder="如 mycar" required autocapitalize=off '
                'style="text-align:right"></label>'
                '<label class=cell><span class=main><b>Token</b></span><input type=password name=token '
                f'placeholder="{"留空表示不修改" if ddns else "必填"}" autocomplete=off style="text-align:right"></label>'
                + ('<label class=cell><span class=main><b>同时登记公网 IPv6</b><span>车机不让打开 192.168.x.x 这类局域网地址时才勾选</span>'
                   f'</span><input type=checkbox name=ipv6 value=on{" checked" if ddns.get("ipv6") else ""}></label>'
                   if mode in setup_wizard.JOINS else "") + '</div>'
                '<button class="btn plain" name=action value=save>保存</button></form>'
                + (f'<form method=post action=/ddns>{ui.hidden(nonce)}<button class="btn plain" name=action value=clear '
                   'onclick="return confirm(\'关闭后车机改用盒子自带证书的地址，确定？\')">关闭</button></form>' if ddns else ""))
        state = (own_domain.message(self.state, name, mode == "box_hotspot", retry=bool(self.retry_cert))
                 if ddns else "")
        # Only where the car asks the box (its hotspot) and only when it can be turned off (not Tesla mode).
        if ddns and mode == "box_hotspot" and not setup_wizard.options(self.state).get("tesla"):
            form += f'<div class=group style="margin-top:12px">{self.car_audio_block(nonce, "own_name")}</div>'
        actions = self.certificate_actions(nonce, ddns, name)
        ipv6 = ('勾选「同时登记公网 IPv6」后，盒子把自己的公网 IPv6 地址也登记到这个域名：车机拿到的是公网地址，'
                '画面仍在同一个 WiFi 里直接传。需要车机热点或随身 WiFi 给设备分配 IPv6；这个地址在 DuckDNS 上谁都查得到，'
                '所以盒子同时开启防火墙，只让同一个 WiFi 里的设备连进来。' if mode in setup_wizard.JOINS else
                '盒子热点方式没有公网地址，车机不让打开局域网地址的问题无法用这个办法解决。')
        return (f'<details><summary class=cell><span class=main><b>自己的域名</b><span>证书警告点不过去，或不让打开局域网地址</span></span>'
                f'<span class=value>{escape(name or "未设置")}</span><span class=chev></span></summary>'
                f'<div class=inner style="padding:0 0 12px">{form}'
                + (f'<p class=foot>{escape(state)}</p>' if state else "") + actions +
                '<p class=foot>在 duckdns.org 免费注册并建一个子域名，把子域名和页面上的 token 填在这里。盒子会把这个域名指向自己，'
                f'并向 Let’s Encrypt 申请免费的正式证书、到期前自动续签；车机改用 https://子域名.duckdns.org:{viewer_port.resolve(self.state)}/ 打开，'
                f'不再有证书警告。不需要开放任何端口。{ipv6}'
                + ('盒子热点方式在车上没有网络，申请和续签要在家插上网线开机。' if mode == "box_hotspot" else "") + '</p></div></details>')

    # What goes over the car's own Bluetooth instead of the CarPlay page (found in a Tesla, 2026-10-14; supervisor.py
    # car_audio_args). On OpenWrt LuCI sets the same (dashplay.main.car_music / car_voice / tesla_mode).
    CAR_AUDIO = (("car_music", "音乐走车机蓝牙",
                  "iPhone 用蓝牙连车机，并在车机里把媒体来源选成这部 iPhone：方向盘切歌、媒体卡片、音量就和普通手机一样用。"
                  "CarPlay 网页负责画面和导航播报（播报会放大）。偶尔放歌没声音时，在 iPhone 控制中心把音频输出选成车机。"),
                 ("car_voice", "通话与 Siri 走车机蓝牙",
                  "通话和 Siri 用车机自己的麦克风和喇叭（iPhone 要先用蓝牙连上车机），不经过 CarPlay 网页。网页通话有回声时可以试试。"))
    # Box-hotspot mode with one's own domain (supervisor.py hotspot_names, 2026-10-15; on by default since 2026-10-29).
    OWN_NAME = ("own_name_on_hotspot", "由盒子告诉车机域名在哪",
                "车机连盒子热点时，由盒子直接回答「自己的域名就在这里」，车机没网也能用域名打开。"
                "一般保持打开；连着盒子时车机自带的应用上不了网，就关掉试试。")

    def tesla_text(self, mode):
        """Tesla mode (supervisor.py tesla_mode): what it does in this way of use."""
        shared = setup_wizard.SHARED_LAN.ip
        common = ("iPhone 用蓝牙连特斯拉并选为媒体来源，音乐、Siri 和通话在 iPhone 控制中心选特斯拉输出，导航播报留在网页"
                  "（会放大）；想让所有声音都不走 CarPlay，在本页「声音」或 CarPlay 画面里的「盒子设置」改。"
                  "全屏按钮经腾讯视频、1905 的跳转页去掉地址栏（特斯拉要能上网）。特斯拉的 WiFi 设置里要打开「行驶中保持连接」，"
                  "否则一出 P 挡画面就停。")
        if mode in setup_wizard.JOINS:
            label = network_label(mode)
            return (f"特斯拉浏览器不让开 192.168.x.x 这类局域网地址：要在{label}自己的后台把局域网地址改成 {shared}"
                    f"（100.64–100.127 开头的都行），盒子改不了{label}的地址。" + common)
        return (f"盒子热点的网段改成 {shared}（特斯拉浏览器不让开 192.168.x.x 这类局域网地址），车机打开 "
                f"https://{shared}:{viewer_port.resolve(self.state)}/ ；改完车机和手机重新连一次盒子热点。" + common)

    def sound_options(self):
        chosen = audio_routes.read_uci() if self.managed else (_load(self.options_file) or {})
        return chosen, audio_routes.enabled(chosen.get('tesla_mode', chosen.get('tesla')))

    def sound_routes(self):
        return audio_routes.resolve(*self.sound_options())

    def output_routes(self, output):
        chosen, tesla = self.sound_options()
        return audio_routes.with_output(chosen, output, tesla)

    def save_sound_routes(self, routes):
        audio_routes.validate(routes)
        with self.audio_routes_lock:
            if self.managed:
                audio_routes.write_uci(routes)
            else:
                chosen = _load(self.options_file) or {}
                # Only the owner's switch is stored (audio_routes.resolve); older per-category keys go.
                for name in ('main', 'media', 'alert', 'siri', 'calls'):
                    chosen.pop('audio_route_' + name, None)
                chosen[audio_routes.OUTPUT_KEY] = routes['navigation']
                _save(self.options_file, chosen)
        self.apply_later('receiver options changed')

    def save_car_audio(self, form):
        if form.get('scope') == 'sound' and audio_routes.OUTPUT_KEY in form:
            self.save_sound_routes(self.output_routes(form[audio_routes.OUTPUT_KEY]))
            return RECEIVER_ONLY
        chosen = _load(self.options_file)
        chosen = dict(chosen) if isinstance(chosen, dict) else {}
        was_tesla = chosen.get("tesla") is True
        scope = form.get("scope")
        keys = (("tesla",) if scope == "car" else ("car_music", "car_voice") if scope == "sound" else
                (self.OWN_NAME[0],) if scope == "own_name" else ("tesla", "car_music", "car_voice"))
        for key in keys:
            chosen[key] = form.get(key) == "on"
        if chosen.get("tesla") and self.setup().get("mode") == "car_hotspot":
            raise ValueError("特斯拉没有车机热点，请先在网络里改用其他方式。")
        if scope in (None, "car") and chosen.get("tesla") and not was_tesla:
            # The page's car-music behaviour (prompt boost, no media session); where the sound plays is the iPhone's
            # Control Center, or none through CarPlay with the sound switch (2026-10-31).
            chosen.update(car_music=True, car_voice=True)
        _save(self.options_file, chosen)
        if scope == "sound":
            # Receiver options only (supervisor.receiver_options): no network rebuild (2026-10-29).
            self.apply_later("receiver options changed")
            return RECEIVER_ONLY
        self.apply_later("car audio changed")
        if chosen.get("tesla", False) != was_tesla and self.setup().get("mode") == "box_hotspot":
            address = setup_wizard.SHARED_LAN.ip if chosen["tesla"] else "盒子原来的地址"
            return (f"已保存。盒子约 1 分钟后重启服务，热点网段改为 {address}：车机和手机重新连一次盒子热点，"
                    f"设置页用 http://{setup_wizard.MDNS_NAME}/ 打开。")
        return "已保存，盒子约 1 分钟后重启服务生效。"

    def car_audio_block(self, nonce, scope=None):
        if scope == 'sound':
            output = self.sound_routes()['navigation']
            choices = ''.join(f'<option value="{value}"{" selected" if output == value else ""}>{label}</option>'
                              for value, label in (('carplay', '留在 CarPlay'), ('bluetooth', '不走 CarPlay')))
            return (f'<form method=post action=/audio>{ui.hidden(nonce, scope="sound")}'
                    '<label class=cell><span class=main><b>声音输出</b><span>与 CarPlay 画面里的盒子设置同步</span></span>'
                    f'<select name="{audio_routes.OUTPUT_KEY}">{choices}</select></label>'
                    '<p class=foot>留在 CarPlay：音乐、Siri 和通话从哪出，在 iPhone 控制中心（音乐卡片右上角的输出图标）选，'
                    '导航播报一直在网页。不走 CarPlay：所有声音（含导航）都由 iPhone 自己或它连的蓝牙播放，'
                    '控制中心里不会再有 CarPlay。保存后 CarPlay 会短暂重连，WiFi 不断。</p>'
                    '<button class="cell action">保存声音输出</button></form>')
        chosen = _load(self.options_file)
        chosen = chosen if isinstance(chosen, dict) else {}
        vehicle = (("tesla", "特斯拉模式", self.tesla_text(self.setup().get("mode"))),)
        rows = (vehicle if scope == "car" else self.CAR_AUDIO if scope == "sound" else (self.OWN_NAME,) if scope == "own_name"
                else vehicle + self.CAR_AUDIO)
        # The own-name switch is on unless turned off (setup_wizard.answers_own_name); the others are off until ticked.
        on = lambda key: chosen.get(key) is not False if key == self.OWN_NAME[0] else chosen.get(key) is True
        cells = "".join(f'<label class=cell><span class=main><b>{title}</b><span>{escape(text)}</span></span>'
                        f'<input type=checkbox name={key} value=on{" checked" if on(key) else ""}></label>'
                        for key, title, text in rows)
        return f'<form method=post action=/audio>{ui.hidden(nonce, **({'scope': scope} if scope else {}))}{cells}<button class="cell action">保存</button></form>'

    def save_connection(self, path, form):
        chosen = setup_wizard.options(self.state)
        setup = self.setup()
        if path == '/iphone':
            value = form.get('iphone')
            draft = dict(chosen, **setup)
            if value not in [key for key, *_ in iphone_choices(draft, self.direct_capable(), self.wired_capable())]:
                raise ValueError('前面的需求只能使用数据线；请先修改网络来源。')
            setup_wizard.save(self.state, dict(setup, iphone=value))
        else:
            if setup.get('mode') != 'box_hotspot':
                raise ValueError('只有盒子热点方式可设置车机上网。')
            value = form.get('car_internet')
            if value not in ('none', 'phone_hotspot') + (('usb_tether',) if self.wired_capable() else ()):
                raise ValueError('请选择车机上网方式。')
            chosen['car_internet'] = value
            if value == 'phone_hotspot':
                old = _load(self.state / 'uplink-hotspot.json') or {}
                # A new SSID must not silently reuse another hotspot's password.
                if old.get('ssid') != form.get('ssid', '').strip():
                    old = {}
                ssid, password = _hotspot_credentials(form, old)
                _save(self.state / 'uplink-hotspot.json', {'ssid': ssid, 'passphrase': password})
                setup_wizard.save(self.state, setup)
            _save(self.options_file, chosen)
        self.apply_later('connection preference changed')
        return '已保存，约 1 分钟后生效，期间 CarPlay 会断一下。'

    def internet_block(self, nonce):
        if self.setup().get('mode') != 'box_hotspot' or self.managed:
            return ''
        chosen = setup_wizard.options(self.state)
        saved = _load(self.state / 'uplink-hotspot.json') or {}
        current = self.status().get('uplink', {})
        phase = current.get('phase')
        label = internet_label(chosen, saved, current, self.status().get('tesla_lan_stable'))
        rows = ui.banner(label, 'ok' if phase in ('online', 'wired') else 'wait')
        rows += f'<form method=post action=/internet>{ui.hidden(nonce)}'
        choices = [('none', '不上网'), ('phone_hotspot', '连接 WiFi 热点（手机或随身 WiFi）')]
        if self.wired_capable():
            choices.append(('usb_tether', 'USB 共享网络（手机或随身 WiFi）'))
        for value, text in choices:
            rows += f'<label class=cell><input type=radio name=car_internet value={value}{" checked" if chosen["car_internet"]==value else ""}>{text}</label>'
        rows += (f'<label class=cell><span class=main><b>热点名称</b></span><input type=text name=ssid value="{escape(saved.get("ssid", ""))}" '
                 'maxlength=32 placeholder=手机热点名称 autocapitalize=off style="text-align:right" aria-label=手机热点名称></label>'
                 '<label class=cell><span class=main><b>热点密码</b></span><input type=password name=password minlength=8 maxlength=63 '
                 f'placeholder="{"留空沿用已保存的密码" if saved.get("passphrase") else "至少 8 位"}" autocomplete=new-password '
                 'style="text-align:right" aria-label=手机热点密码></label>')
        rows += '<button class="cell action">保存</button></form>'
        return ui.group('车机上网', rows, '密码只写不读，留空沿用同名热点密码。热点可以是手机（跑 CarPlay 的同一部 iPhone 或另一部手机），也可以是随身 WiFi，要一直开着。'
                       'iPhone 开热点时把「最大兼容性」关掉（热点走 5 GHz，画面才不卡）；'
                       '只有盒子网卡只支持 2.4 GHz 时才需要打开它。'
                       + setup_flow.DATA_USE + '找不到或掉线时退回不上网；特斯拉自带音乐、地图将不能联网，CarPlay 正常。'
                       '「USB 共享网络」：手机或随身 WiFi 用数据线插到盒子的 USB 口。iPhone 打开「个人热点」'
                       '（可以就是做有线 CarPlay 的那根线）；安卓手机打开「USB 网络共享」；随身 WiFi 一般插上即用。'
                       '联网顺序是：先用 USB；USB 没插或连不上时，自动改连上面填过的热点；热点也连不上时保持无外网'
                       '（盒子热点和 CarPlay 不受影响）。插回数据线再切回 USB。'
                       '和 CarPlay 走有线还是无线无关，有线、无线在 CarPlay 画面的「盒子设置」里随时切换。')

    def iphone_preference(self, nonce):
        if self.managed:
            return ''
        setup = self.setup()
        chosen = setup_wizard.options(self.state)
        draft = dict(chosen, **setup)
        capable, mode = self.direct_capable(), setup.get('mode')
        direct_status = self.status().get('direct_phone')
        rows = ui.banner(direct_status, 'ok' if direct_status.startswith('iPhone 直连') else 'wait') if direct_status else ''
        rows += f'<form method=post action=/iphone>{ui.hidden(nonce)}'
        for key, title, text in iphone_choices(draft, capable, self.wired_capable()):
            rows += (f'<label class=cell><input type=radio name=iphone value={key}{" checked" if setup.get("iphone")==key else ""}>'
                     f'<span class=main><b>{title}</b><span>{text}</span></span></label>')
        rows += '<button class="cell action">保存</button></form>'
        if mode in setup_wizard.JOINS and capable:
            on = setup_wizard.direct_phone(chosen, capable, mode)
            other = '数据线' if mode == 'car_hotspot' else '经随身 WiFi 连接'
            rows += (f'<form method=post action=/direct>{ui.hidden(nonce)}'
                     f'<label class=cell><input type=checkbox name=direct_phone value=1{" checked" if on else ""}>'
                     '<span class=main><b>无线时 iPhone 直连盒子热点</b>'
                     f'<span>不经{network_label(mode)}转发，画面更顺。{network_label(mode)}要在 5 GHz 的 36–48 或 149–165 信道；'
                     f'2.4 GHz 或其他信道时自动改用{other}，上面会写明原因。</span></span></label>'
                     f'<label class=cell><input type=checkbox name=direct_phone_internet value=1'
                     f'{" checked" if chosen.get("direct_phone_internet") is True else ""}>'
                     f'<span class=main><b>iPhone 用车机流量上网</b><span>只在直连时有效，默认关（关着时 iPhone 用自己的流量）。'
                     f'iPhone 上网的数据和 CarPlay 画面走盒子同一块无线网卡、同一信道，会抢带宽；{network_label(mode)}没网或限速时，'
                     'iPhone 上网会变慢，要等它自己切回蜂窝。</span></span></label>'
                     '<button class="cell action">保存</button></form>')
        if not setup_wizard.wireless(dict(setup, iphone='any'), chosen, capable) and not direct_status:
            rows += ui.banner('这种方式只能用数据线。' + ('打开上面的「无线时 iPhone 直连盒子热点」就能用无线。'
                                                    if mode == 'car_hotspot' and capable else '要用无线，请修改网络来源。'), 'wait')
        return rows


    def save_hotspot(self, form):
        old = _load(self.hotspot_file) or {}
        ssid, password = _hotspot_credentials(form, old)
        _save(self.hotspot_file, dict(old, ssid=ssid, passphrase=password))
        return ssid

    def apply_setup(self, form):
        action = form.get("action")
        setup = self.setup()
        if setup.get("wizard_version") == 2 and not setup.get("confirmed"):
            raise ValueError("请使用一步一问的首次设置并确认配置总结。")
        if action == "rescan":
            self.rescan()
            return ""
        step = setup_wizard.step(setup, self.status().get("mode"))
        if action == "car" and step == "car":
            ssid, password = _car_credentials(form, _load(self.car_file) or {})
            box = _load(self.hotspot_file) or {}
            if not box.get("passphrase"):
                box_password = form.get("box_password", "")
                if not 8 <= len(box_password) <= 63:
                    raise ValueError("请为盒子自己的热点设置 8–63 位密码。")
                _save(self.hotspot_file, dict(box, passphrase=box_password))
            _save(self.car_file, {"ssid": ssid, "passphrase": password})
            # Keep first-run setup's answers (wizard_version, confirmed, iphone): only the hotspot is new.
            setup_wizard.save(self.state, dict({k: v for k, v in setup.items() if k != "failed"}, done=False, car=ssid))
            self.apply_later("setup car hotspot chosen")
            return ("page", self.switching_page(ssid, first=True))
        if action == "finish" and step == "phone":
            if setup.get("wizard_version") == 2 and setup_wizard.wireless(setup, setup_wizard.options(self.state), self.direct_capable()) and not self.pairing.devices():
                raise ValueError("选择了无线或都行，请先添加 iPhone；只用线可返回改为有线。")
            setup_wizard.save(self.state, dict(setup, done=True))
            return "设置完成。"
        raise ValueError("未知操作。")

    # Pages -----------------------------------------------------------------------------------

    def notice_page(self, title, hotspot, phone, steps, warn=""):
        items = "".join(f"<li>{escape(step)}</li>" for step in steps)
        return ui.page(title, f'<h1>{escape(title)}</h1>{ui.chain(hotspot, phone)}'
                              f'<p class=group-title>接下来</p><ol class=steps>{items}</ol>{ui.banner(warn, "wait")}')

    def reconnect_page(self, ssid, title, first=False):
        steps = ["盒子约 1 分钟后用新的设置重新打开热点，手机会先断开。",
                 f"在手机和车机的「无线局域网」里连接「{ssid}」，输入你设置的密码。",
                 f"用浏览器打开 http://{setup_wizard.MDNS_NAME}/" + ("，继续连接 iPhone。" if first else "。")]
        return self.notice_page(title, ("wait", ssid, "约 1 分钟"), ("off", "iPhone", "稍后"), steps)

    def switching_page(self, ssid, first=False):
        box = self.status().get("box_ssid", "")
        label = network_label(self.setup().get("mode"))
        address = f"http://{setup_wizard.MDNS_NAME}/"
        steps = [f"盒子约 1 分钟后连上{label}「{ssid}」，并关掉自己的热点，手机会先断开。",
                 f"手机也连接「{ssid}」。",
                 f"用浏览器打开 {address}" + ("，继续连接 iPhone。" if first else "。")]
        return self.notice_page(f"正在连接「{ssid}」", ("wait", ssid, "约 1 分钟"), ("off", "iPhone", "稍后"), steps,
                                f"2 分钟后仍打不开：盒子没连上「{ssid}」，会重新打开自己的热点「{box}」，"
                                "手机连它后打开上面的地址，页面会写明原因。")

    def phone_node(self, devices, active, phase, wired=None):
        if wired == "live":
            return ("on", "iPhone", "有线连接")
        if active and active in devices:
            return ("on", devices[active], "正在使用")
        if phase not in ("closed", "paired"):
            return ("wait", "iPhone", "正在添加")
        if wired in ("trust", "starting"):
            return ("wait", "iPhone", "数据线已插上")
        # Car hotspot: wireless only through the direct hotspot, and only while it is up (supervisor direct_phone).
        direct = self.status().get("direct_phone", "")
        if self.setup().get("mode") == "car_hotspot" and (
                not setup_wizard.wireless(self.setup(), setup_wizard.options(self.state), self.direct_capable())
                or direct and not direct.startswith("iPhone 直连")):
            return ("off", "iPhone", "插数据线使用")
        return ("off", "iPhone", f"已添加 {len(devices)} 部" if devices else "未添加")

    def hotspot_node(self):
        status = self.status()
        if self.managed:
            return ("on", status.get("ssid", ""), "路由器 WiFi") if status.get("ssid") else ("wait", "WiFi", "等待 WiFi")
        if status.get("mode") == "sta":
            return ("on", status.get("ssid", ""), network_label(self.setup().get("mode")))
        if status.get("mode") == "ap":
            return ("on", status.get("box_ssid", ""), "盒子热点")
        return ("wait", "热点", "正在启动")

    def chain(self, devices, presence, phase, wired):
        mode = self.setup().get("mode")
        first = "car" if mode == "car_hotspot" and self.status().get("mode") == "sta" else "hotspot"
        return ui.chain(self.hotspot_node(), self.phone_node(devices, presence.get("active"), phase, wired), first)

    def phones_block(self, nonce, first=False):
        """Keep paired/active phones visible while adding another phone."""
        pair = self.pairing.status()
        panel, refresh = ui.pairing_panel(pair, nonce)
        presence = self.pairing.presence()
        devices = self.pairing.devices()
        active = presence.get("active")
        if active and active not in devices:
            devices[active] = f"iPhone（蓝牙 …{active[-5:]}）"
        paused = presence.get("paused", [])
        # A switch from this page can be under way while the old phone still counts as active: the receiver ends
        # that session first. Either request marks the target as connecting, which keeps the page reloading every
        # 3 s until it is (2026-10-31: the page sat still until a manual reload).
        pending = None if panel else (self.pairing.pending_phone() or self.pairing.switch_pending())
        connecting = (pending if pending in devices and pending not in paused and pending != active else None)
        # CarPlay on the cable counts as "in use" here too: another phone's row says 切换, not 接入.
        wired_live = bool(self.wired_status) and read_wired(self.wired_status) == "live"
        rows = ui.phone_rows(devices, active, presence.get("last_phone"), nonce, not active and not panel and not wired_live,
                             paused=paused, connecting=connecting)
        if panel:
            return ui.group("我的手机", rows) + panel, refresh, devices
        # The old paired flag survives a completed session; only a live invitation means waiting.
        notice = (ui.banner(f"「{devices[connecting]}」蓝牙已配对，正在尝试连接 CarPlay。"
                            "连接可能需要几十秒，请保持蓝牙打开，暂时不要重复配对。") if connecting else "")
        # One place for "adding is not possible now", both reasons (2026-10-31): a phone is using CarPlay
        # over Wi-Fi, or CarPlay is on the cable (which pauses every wireless invitation). The add button is
        # replaced by the reason, so nothing has to be tapped to find out why.
        blocked = "" if panel or connecting else self.add_phone_blocked()
        # The explanation takes the button's own place (below the list, where the button is), not the space
        # above it: one spot for "add iPhone", whether it is a button or the reason it is missing.
        add = "" if blocked or connecting else (
            f'<form method=post action=/pair>{ui.hidden(nonce)}'
            f'<button class="{"btn plain" if devices else "btn"}" name=action value=start>添加 iPhone</button></form>')
        foot = ("配对一次后，手机连上同一个热点就能接入；有人正在使用时，其他手机不会把它挤掉。"
                if not first else "第一次使用需要用蓝牙配对一次，之后连上同一个热点就能接入。")
        return notice + ui.group("我的手机", rows, foot) + (blocked or add), bool(connecting), devices

    def add_phone_blocked(self):
        """Why no phone can be added right now, or "": the same wording a POST comes back with."""
        in_use = self.pairing.in_use_message()
        if in_use:
            return ui.banner(in_use, "wait")
        if self.wired_status and read_wired(self.wired_status) == "live":
            return ui.banner(CABLE_BLOCKS_ADDING, "wait")
        return ""

    def render(self, query):
        params = urllib.parse.parse_qs(query)
        notice = ui.flash(params.get("m", [""])[0])
        if self.status().get("identity") is False:
            host = urllib.parse.urlsplit(self.home()).hostname or ""
            notice = ui.banner(f"还缺 CarPlay 认证文件。把「AI 安装说明」交给你的 AI 助手，并告诉它：盒子地址 {host}，"
                               "用户名 root，密码 1234。文件放好后盒子会自动开始工作，网络和添加手机可以先设置。", "wait") + notice
        if self.managed:
            internet = self.status().get("tesla_internet", "")
            if internet:
                notice += ui.banner(internet, "wait" if ("不可用" in internet or "检测" in internet) else "ok")
            error = self.status().get("tesla_error", "")
            if error:
                notice += ui.banner("特斯拉模式局域网设置失败：" + error + "；CarPlay 不受影响。", "bad")
            return self.render_full(notice)
        setup = self.setup()
        current = setup_wizard.step(setup, self.status().get("mode"))
        # The older three-step setup's first pages (way of use, box hotspot) are gone (2026-10-29): a box left there
        # starts first-run setup again; the car and iPhone pages are where first-run setup leads after its summary.
        if (params.get("view", [""])[0] == "setup" or current in ("vehicle", "mode", "box")
                or setup.get("wizard_version") == 2 and not setup.get("confirmed") and not setup.get("done")):
            return FirstSetup(self).render(notice)
        if current == "car":
            return self.render_car_step(setup, notice)
        if current == "phone":
            return self.render_phone_step(notice)
        view = params.get("view", [""])[0]
        if view == "car" and setup.get("mode") in setup_wizard.JOINS:
            return self.render_car_change(notice)
        if view == "hotspot":
            return self.render_hotspot_change(notice)
        return self.render_full(notice)

    def hotspot_foot(self):
        mode = self.setup().get("mode")
        if mode == "box_hotspot":
            address = self.status().get("ap_ip", "192.168.78.1")
            return ("车机和 iPhone 都连这个热点。车机连上后不能上网（盒子热点没有互联网）；"
                    f"CarPlay 画面地址为 https://{address}:{viewer_port.resolve(self.state)}/ 。")
        return f"连不上{network_label(mode)}时，盒子临时打开这个热点，手机连它进入设置。"

    def render_hotspot_change(self, notice):
        nonce = self.pairing.nonce
        box = _load(self.hotspot_file) or {}
        body = (f'<a class=back href="/">‹ 盒子设置</a><h1>盒子热点</h1>{notice}'
                + ui.hotspot_form(nonce, "/hotspot", box.get("ssid", ""), box.get("passphrase", ""), "保存", self.hotspot_foot()))
        return ui.page("盒子热点", body)

    def render_car_step(self, setup, notice):
        nonce = self.pairing.nonce
        label = network_label(setup.get("mode"))
        failure = ""
        trying = setup.get("car") not in (None, "skip")
        if trying:
            reason = setup_wizard.FAILURES.get(setup.get("failed"), setup_wizard.FAILURES["not_found"])
            failure = ui.banner(f"没有连上「{setup['car']}」。{reason}", "bad")
        networks = self.networks()
        box = _load(self.hotspot_file) or {}
        extra = "" if box.get("passphrase") else ui.group(
            "盒子自己的热点", '<label class=cell>' + ui.LOCK + '<input type=password name=box_password placeholder="设置密码，至少 8 位" '
            'autocomplete=new-password required minlength=8 maxlength=63 aria-label=盒子热点密码></label>',
            f"找不到{label}时，盒子临时打开自己的热点「{escape(box.get('ssid', ''))}」，手机连它进入设置。")
        foot = ("盒子连车机热点，车机浏览器经它打开 CarPlay 画面。请先在车机上打开它的热点。"
                if setup.get("mode") == "car_hotspot" else
                "盒子、车机和 iPhone 都连这个随身 WiFi。请先打开随身 WiFi。") + self.band_note(label)
        picker = ui.scanning(label) if networks is None else ui.network_picker(
            networks, setup.get("car") if trying else None, nonce, "car", "/setup", "连接", foot, extra, label)
        body = (f'<a class=back href="/?view=setup">‹ 修改使用方式</a><h1>连接{label}</h1><p class=sub>首次设置</p>{notice}'
                f'{ui.chain(("wait", label, "请选择"), ("off", "iPhone", "下一步"), "car" if label == "车机热点" else "hotspot")}'
                f'{failure}{picker}')
        return ui.page("首次设置", body, ui.SCANNING_RELOAD if networks is None else False)

    def wired_phase(self):
        return read_wired(self.wired_status)

    def iphone_blocks(self, nonce, first=False):
        """Cable first; Bluetooth pairing for wireless where this way of use has it.
        Returns (html, refresh, devices, wired phase)."""
        mode = self.setup().get("mode")
        wired = self.wired_phase()
        waiting = wired in ("trust", "starting") or first and mode == "car_hotspot" and wired != "live"
        if not setup_wizard.wireless(self.setup(), setup_wizard.options(self.state), self.direct_capable()):
            devices = self.pairing.devices()
            error = self.status().get("bluetooth_error", "")
            notice = ui.banner("无线蓝牙没有启动：" + error + "；已配对的手机会保留，修好后重启盒子即可恢复无线。",
                               "bad") if error else ""
            paired = "".join(f'<div class=cell><span class=main><b>{escape(name)}</b>'
                             f'<span>{escape(address)}</span></span></div>'
                             for address, name in devices.items()) if devices else ""
            return (ui.wired_block(wired, "iPhone 用数据线", "这种使用方式只能用数据线（有线 CarPlay）。")
                    + notice + paired, waiting, devices, wired)
        foot = ("插着数据线时用有线；拔掉后改用下面的无线。"
                + ("无线卡顿或连不上时请用数据线：有的随身 WiFi 不让设备互相访问。" if mode == "mifi" else ""))
        block, refresh, devices = self.phones_block(nonce, first)
        # Pairing is done on another screen (the head unit): the iPhone stays on its Bluetooth page.
        intro = ui.other_screen(self.home()) if first and not devices and not refresh else ""
        # #phones is the anchor the CarPlay page's 添加 iPhone entry opens (2026-10-31).
        return (ui.wired_block(wired, "用数据线（推荐）", foot) + intro
                + f'<div id="phones">{block}</div>'), refresh or waiting, devices, wired

    def entries_block(self, status, refresh):
        entries = "".join(f'<a class=cell href="{escape(u)}"><span class=main><b>{escape(u)}</b></span><span class=chev></span></a>'
                          for u in status.get("entries", []) if u.startswith("https"))
        if not entries or refresh:
            return ""
        mode = self.setup().get("mode")
        if self.managed:
            foot = f"车机连接 WiFi「{escape(status.get('ssid', ''))}」后，在车机浏览器打开上面的地址；画面端口在 LuCI 的 DashPlay 页设置。"
        elif status.get("mode") == "ap" and mode == "box_hotspot":
            foot = f"车机连接盒子热点「{escape(status.get('box_ssid', ''))}」后，在车机浏览器打开上面的地址。是否能上网请看「车机上网」当前状态。"
        elif status.get("mode") == "ap":
            foot = "当前通过盒子热点连接。在连着盒子热点的设备上打开上面的地址。"
        else:
            foot = f"当前通过{network_label(mode)}连接。在车机浏览器打开上面的地址。"
        return ui.group("CarPlay 画面", entries, foot)

    def viewer_port_form(self, nonce):
        """Advanced: the HTTPS port of the CarPlay page (viewer_port.py) and when an unwatched iPhone is let go
        (supervisor.idle_release); OpenWrt sets both in LuCI."""
        if self.managed:
            return ""
        from supervisor import idle_release
        return (f'<form method=post action=/viewer-port>{ui.hidden(nonce)}'
                f'<label class=cell><span class=main><b>画面端口</b>'
                f'<span>车机浏览器打开的 HTTPS 端口；默认 18443，一般不用改。改完约 1 分钟后生效</span></span>'
                f'<input type=number name=viewer_port min=1024 max=65535 value={viewer_port.resolve(self.state)}></label>'
                '<button class="cell action">保存端口</button></form>'
                f'<form method=post action=/idle-release>{ui.hidden(nonce)}'
                f'<label class=cell><span class=main><b>没人看画面时释放 iPhone（秒）</b>'
                '<span>没有网页在看画面超过这么久，就结束无线 CarPlay，iPhone 不再发热；再打开画面约 7 秒出图。'
                '默认 300，0 为永不释放</span></span>'
                f'<input type=number name=idle_release min=0 max={IDLE_RELEASE_MAX} value={idle_release(self.state)}></label>'
                '<button class="cell action">保存</button></form>')

    def save_viewer_port(self, form):
        port = form.get("viewer_port", "").strip()
        if not port.isdigit() or not viewer_port.valid(port):
            raise ValueError(f"端口要在 1024–65535 之间，且不能是 {'、'.join(map(str, viewer_port.RESERVED))}。")
        self._update_options(viewer_port=int(port))
        self.apply_later("viewer port changed")
        return f"已保存。约 1 分钟后生效，期间 CarPlay 会断一下；之后车机改用 :{int(port)} 打开。"

    def save_idle_release(self, form):
        value = form.get("idle_release", "").strip()
        if not value.isdigit() or int(value) > IDLE_RELEASE_MAX:
            raise ValueError(f"请填 0–{IDLE_RELEASE_MAX} 之间的秒数（0 为永不释放）。")
        self._update_options(idle_release=int(value))
        self.apply_later("receiver options changed")
        return RECEIVER_ONLY

    def _update_options(self, **values):
        chosen = _load(self.options_file)
        _save(self.options_file, dict(chosen if isinstance(chosen, dict) else {}, **values))

    def render_phone_step(self, notice):
        nonce = self.pairing.nonce
        block, refresh, devices, wired = self.iphone_blocks(nonce, first=True)
        presence = self.pairing.presence()
        phase = self.pairing.status().get("phase", "closed")
        needs_pair = setup_wizard.wireless(self.setup(), setup_wizard.options(self.state), self.direct_capable())
        ready = bool(devices) if needs_pair else True
        finish = (f'<form method=post action=/setup>{ui.hidden(nonce)}'
                  f'<button class="{"btn" if ready else "btn plain"}" name=action value=finish>'
                  f'{"完成设置" if ready else "稍后连接，先完成设置"}</button></form>')
        pairing = phase != "closed"
        body = (f'<h1>连接 iPhone</h1><p class=sub>首次设置：最后一步</p>{notice}'
                f'{self.chain(devices, presence, phase, wired)}'
                f'{block}'
                + self.entries_block(self.status(), pairing)
                + (finish if ready else "")
                + (ui.banner("请先添加 iPhone，或者在设置中选择只用有线。", "wait") if not ready else ""))
        return ui.page("首次设置", body, refresh)

    def render_car_change(self, notice):
        nonce = self.pairing.nonce
        car = _load(self.car_file) or {}
        label = network_label(self.setup().get("mode"))
        networks = self.networks()
        if networks is None:
            return ui.page(label, f'<a class=back href="/">‹ 盒子设置</a><h1>{label}</h1>{notice}{ui.scanning(label)}',
                           ui.SCANNING_RELOAD)
        picker = ui.network_picker(networks, car.get("ssid"), nonce, "save", "/car", "连接",
                                   "保存后盒子会切换过去；连不上时临时打开盒子自己的热点。" +
                                   ("密码留空表示沿用已保存的密码。" if car else "") + self.band_note(label), label=label)
        body = f'<a class=back href="/">‹ 盒子设置</a><h1>{label}</h1>{notice}{picker}'
        page = ui.page(label, body)
        # A saved password may be kept, so the field is optional on this page only.
        return page.replace(" required minlength=8", " minlength=8") if car else page

    def render_full(self, notice):
        nonce = self.pairing.nonce
        status = self.status()
        mode = self.setup().get("mode")
        label = network_label(mode)
        car = _load(self.car_file) or {}
        block, refresh, devices, wired = self.iphone_blocks(nonce)
        presence = self.pairing.presence()
        phase = self.pairing.status().get("phase", "closed")
        joins = mode in setup_wizard.JOINS
        fallback = (ui.banner(f"没有连上{label}「{car.get('ssid', '')}」，暂时打开了盒子自己的热点。{label}打开后会自动切换。", "wait")
                    if status.get("mode") == "ap" and joins and car else "")
        network = (f'<div class=cell><span class=main><b>使用方式</b></span><span class=value>{escape(MODE_TITLES.get(mode, ""))}</span></div>')
        if joins:
            network += (f'<a class=cell href="/?view=car"><span class=main><b>{label}</b></span>'
                        f'<span class=value>{escape(car.get("ssid", "未设置"))}</span><span class=chev></span></a>')
        network += (f'<a class=cell href="/?view=hotspot"><span class=main><b>盒子热点</b></span>'
                    f'<span class=value>{escape(status.get("box_ssid", ""))}</span><span class=chev></span></a>'
                    f'<div class=cell><span class=main><b>设置页</b></span><span class=value>http://{setup_wizard.MDNS_NAME}/</span></div>')
        if status.get("mode") == "ap" and joins and car:
            network += (f'<form method=post action=/retry>{ui.hidden(nonce)}'
                        f'<button class="cell action">立即重试{label}</button></form>')
        if self.managed:
            admin = self.managed["admin"]
            if admin.startswith("/"):
                # On the router's own web server (port 80), not on this page's port (a 404 there).
                host = urllib.parse.urlsplit(status.get("home", "")).hostname
                admin = f"http://{host}{admin}" if host else admin
            network = (f'<div class=cell><span class=main><b>交给 iPhone 的 WiFi</b></span>'
                       f'<span class=value>{escape(status.get("ssid", "") or "等待 WiFi")}</span></div>'
                       f'<a class=cell href="{escape(admin)}"><span class=main><b>在 {self.managed["by"]} 后台设置 WiFi</b>'
                       '<span>WiFi 名称、密码或信道改了以后，DashPlay 会自动跟着改</span></span><span class=chev></span></a>')
            if status.get("warning"):
                fallback = ui.banner(escape(status["warning"]), "bad")
        # Why the head unit may not open the picture, and what to do, in the order to try (2026-10-13).
        reach = (('<a class=cell href="/ca.crt"><span class=main><b>下载盒子 CA 证书</b><span>装到车机上，证书警告就没了</span></span><span class=chev></span></a>'
                  if self.ca_file.exists() else "") + self.ddns_block(nonce, mode))
        reach_foot = ("" if self.managed else
                      "一般用不到。按顺序试：① 车机提示证书不安全，点「继续访问」即可。② 没有「继续访问」：在车机上装盒子 CA 证书；"
                      "车机不让装，就设置「自己的域名」。③ 车机不让打开 192.168.x.x 这类局域网地址：设置「自己的域名」并勾选"
                      "「同时登记公网 IPv6」（方式一、方式二）。")
        # The box answers its own name on its hotspot by default (2026-10-29). A car that keeps its own LTE can then
        # fail to resolve anything else: say where to turn that off, not only inside the folded "own domain" group.
        own = own_domain.load(self.state) or {}
        if (own and mode == "box_hotspot" and not self.managed and own.get("provider") != own_domain.DESEC
                and not setup_wizard.options(self.state).get("tesla")):
            reach_foot += ("车机连盒子热点时，盒子默认替车机回答自己的域名；如果车机自带的应用因此上不了网，"
                           "在下面「自己的域名」里关掉「由盒子告诉车机域名在哪」。")
        advanced = ('<a class=cell href="/diag.txt"><span class=main><b>下载诊断记录</b><span>画面卡顿、断线时，把它交给 AI 助手或开发者分析</span></span><span class=chev></span></a>'
                    + self.viewer_port_form(nonce)
                    + ("" if self.managed else
                    f'<form method=post action=/reset>{ui.hidden(nonce)}<button class="cell destructive" '
                    'onclick="return confirm(\'清除使用方式、热点和所有已添加的手机，回到首次设置？\')">重新进行首次设置</button></form>'))
        notice += (ui.banner("特斯拉建议回家插网线后设置自己的网址。", "wait")
                   if setup_wizard.options(self.state).get("tesla") and not own_domain.serving(self.state) else "")
        body = (f'<h1 class=span>盒子设置</h1>{notice}{self.chain(devices, presence, phase, wired)}'
                f'{fallback}<div>'
                + self.entries_block(status, False)
                + ("" if self.managed else ui.group("车", self.car_audio_block(nonce, "car")))
                + ui.group("网络", network + ("" if self.managed else '<a class=cell href="/?view=setup">修改使用方式（保留已添加的手机）</a>'))
                + self.internet_block(nonce)
                + ui.group("iPhone", self.iphone_preference(nonce) + block) + '</div><div>'
                + ("" if self.managed else ui.group("声音", self.car_audio_block(nonce, "sound"), "改完几秒后 CarPlay 重连一下，WiFi 不断。"))
                + (ui.group("自己的网址", reach, reach_foot) if reach else "")
                + ("" if self.managed else ui.banner(status.get("tesla_warning", ""), "bad"))
                + (ui.group("远程调试", self.debug_block(nonce),
                            "盒子装了 Tailscale 时，连上手机热点后帮你调试的人就能远程登录。调试期间盒子不开自己的热点，"
                            f"{debug_uplink.MINUTES} 分钟后或拔电重启后自动回到原来的网络。")
                   if self.run and not self.managed and (debug_uplink.reachable() or debug_uplink.remaining(self.run))
                   else "")
                + ui.group("程序更新", self.update_block(nonce),
                           "每天自动检查一次，只提示不自动安装。升级前会备份配对、证书和设置，新版本起不来会自动退回。")
                + ui.group("高级", advanced, "" if self.managed else "重新进行首次设置可以更换使用方式，会保留盒子证书、自己的域名和盒子热点密码。")
                + '</div>')
        return ui.page("盒子设置", body, refresh or update.running(self.state.parent), wide=True)
