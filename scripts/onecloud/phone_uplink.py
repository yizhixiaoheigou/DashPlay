"""Own the daily phone-uplink lifecycle; the box AP is always the fallback.

Only Debian uses this controller. F50's current driver cannot do STA + AP.
Switching networks is explicit: stop the old AP, re-associate clients for DHCP,
keep the AP MAC, and undo our NAT/forwarding before returning to offline mode.
"""
import json
from pathlib import Path
import socket
import subprocess
import time
import own_domain
import setup_wizard

HOTSPOT = 'uplink-hotspot.json'
# Direct iPhone AP sharing the car's internet: the station's default route can come a moment after its address.
GATEWAY_WAIT = 10
WIRED_RECHECK = 30   # s: look again whether the cable still carries the uplink
AP_READY_MISSES = 3  # hostapd checks (2 s apart) before the AP is rebuilt: one miss is not a dead AP
UPLINK_SCAN_TIMEOUT = 10


def wired_uplink(platform, resolve=None):
    """True when the box already has internet through something other than its Wi-Fi: the Ethernet cable at home.
    Then the phone hotspot is not needed, and joining it would take the access point and local DNS down for
    nothing (2026-10-26: the box kept rebuilding its AP while the cable was already online)."""
    resolve = resolve or socket.getaddrinfo   # looked up at call time, so a test can replace it
    try:
        devices = platform.default_route_devices()
    except (OSError, subprocess.SubprocessError):
        return False
    if not any(device not in (platform.interface, platform.ap_interface, 'lo') for device in devices):
        return False
    try:
        resolve('www.duckdns.org', 443, type=socket.SOCK_STREAM)   # a route without a working resolver is not enough
    except OSError:
        return False
    return True


def hotspot(state):
    import debug_uplink
    return debug_uplink.hotspot(state, filename=HOTSPOT)


def station_mac(permanent):
    octets = bytearray.fromhex(permanent.replace(':', ''))
    if len(octets) != 6 or octets[0] & 1:
        raise ValueError('Invalid Wi-Fi permanent address')
    octets[0] |= 2
    result = ':'.join(f'{b:02x}' for b in octets)
    if result == permanent.lower():
        octets[-1] ^= 0x80
    return ':'.join(f'{b:02x}' for b in octets)


def start_shared_ap(platform, address, box, channel, width, wait_for, names=(), upstream=None,
                    query_log=None, concurrent=False, router=False):
    """One AP path for phone uplinks and joined car/MiFi uplinks (design 5.2a)."""
    platform.configure_hotspot(channel, width, box['ssid'], box['passphrase'])
    if concurrent:
        platform.n_only_ap()
    platform.set_ap_address(address)
    for _ in range(2):
        if platform.start_ap(wait_for):
            break
        platform.stop_child('hostapd')
    else:
        raise RuntimeError('hotspot failed twice')
    platform.start_dhcp(address, False, names, upstream=upstream, query_log=query_log, router=router)


class UnsuitableChannel(ValueError):
    """The upstream's channel does not suit a direct hotspot beside it; the text says which and what to change."""


def direct_channel_problem(freq, blocked, label='车机热点'):
    """None when the box can open its hotspot on the upstream's channel (it must use the same one), else why not.
    blocked: what the radio says of that channel for an AP (platform.ap_channel_problem), or None."""
    channel = (freq - 2407) // 5 if freq < 3000 else (freq - 5000) // 5
    fix = f'把{label}改成 5 GHz 的 36–48 或 149–165 信道，就能无线直连'
    if freq < 3000:
        # N1 at home 2026-10-17: on 2.4 GHz the picture came 9-12 s late with many frames lost
        return f'{label}在 2.4 GHz（信道 {channel}），直连会很卡（实测延迟 9–12 秒、丢帧）。{fix}'
    if blocked:
        return f'{label}在 5 GHz 信道 {channel}，盒子不能在这个信道开热点（{blocked}）。{fix}'
    return None


class JoinedPhoneAP:
    """Opt-in device validation of a direct iPhone AP beside a car/MiFi station.

    Prepare before joining so the station and AP never share a MAC. The supervisor
    owns reconnect/fallback; a changed upstream channel rebuilds this whole lifecycle.
    No Tesla checks or own-name overrides are applied on the joined network.

    share_internet (state/options.json "direct_phone_internet", off by default; the user, 2026-10-17: some cars come
    with a data plan): the iPhone gets the box as router and DNS, and the box masquerades it out through the car
    hotspot, so the iPhone's own internet check passes for real. Off, the iPhone gets no router and stays on its
    cellular data. Its traffic shares the box's one Wi-Fi chip and channel with the CarPlay picture.
    """
    def __init__(self, platform, address, box, wait_for, share_internet=False, label='车机热点'):
        self.platform, self.address, self.box, self.wait_for = platform, address, box, wait_for
        self.share_internet, self.label = share_internet, label
        self.prepared = self.active = self.sharing = False
        self.frequency = None

    def prepare(self):
        if not self.platform.concurrent_ap():
            return False
        self.prepared = True  # cleanup even if journaling/preparation fails partway
        self.platform.prepare_phone_uplink()
        return True

    def start(self):
        p = self.platform
        if not self.prepared:
            raise RuntimeError('direct AP was not prepared before association')
        if any(a.version == 4 and a.network.overlaps(self.address.network) for a in p.local_addresses()):
            raise ValueError('upstream overlaps direct phone subnet')
        self.frequency = p.frequency()
        if not self.frequency:
            raise RuntimeError('upstream has no channel')
        problem = direct_channel_problem(int(self.frequency), p.ap_channel_problem(int(self.frequency)), self.label)
        if problem:
            raise UnsuitableChannel(problem)
        upstream = None
        if self.share_internet:
            seen = [None]

            def routed():
                seen[0] = p.uplink_gateway()
                return seen[0]
            try:
                self.wait_for(routed, GATEWAY_WAIT, interval=0.5)
            except TimeoutError:
                pass   # no router on the car hotspot: the iPhone stays on its cellular data
            upstream = seen[0] or p.uplink_gateway()
        self.sharing = bool(upstream)
        if not p.add_phone_ap():
            raise RuntimeError('direct AP interface failed')
        if upstream:
            p.enable_phone_nat(self.address.network)
        freq = int(self.frequency)
        channel = (freq - 2407) // 5 if freq < 3000 else (freq - 5000) // 5
        start_shared_ap(p, self.address, self.box, channel, 20, self.wait_for,
                        upstream=upstream, concurrent=True)
        self.active = True
        return channel

    def changed(self):
        return self.active and (not self.platform.connected() or
                               self.platform.frequency() != self.frequency or not self.platform.ap_ready())

    def close(self):
        if self.prepared:
            for child in ('dnsmasq', 'hostapd'):
                self.platform.stop_child(child)
            self.platform.cleanup_phone_uplink()
        self.prepared = self.active = self.sharing = False


class PhoneUplink:
    def __init__(self, platform, state, address, box, offline_channel, offline_width,
                 wait_for, start_receiver, status, say=print, clock=time.monotonic):
        self.platform, self.state, self.address, self.box = platform, Path(state), address, box
        self.channel, self.width = offline_channel, offline_width
        self.wait_for, self.start_receiver, self.status, self.say, self.clock = wait_for, start_receiver, status, say, clock
        self.phase, self.frequency = 'offline', None
        self.next_try, self.backoff = clock() + 5, 15
        self.ap_misses = 0
        self.saved = hotspot(state)
        self.set_status('offline', '先提供盒子热点，再寻找手机热点')

    def set_status(self, phase, reason):
        self.phase = phase
        self.status['uplink'] = {'phase': phase, 'ssid': self.saved['ssid'] if self.saved else '', 'reason': reason}
        # "wired" has the internet too, so the Tesla checks do not need to be answered locally.
        self.status['tesla_offline'] = bool(self.status.get('tesla') and phase in ('offline', 'connecting'))
        # No credentials in logs; diagnostics also records this dict.
        self.say(f'phone uplink {phase}: {reason}')

    def stop_network(self):
        for child in ('receiver', 'dnsmasq', 'hostapd'):
            self.platform.stop_child(child)

    def start_ap(self, channel, width, names, upstream=None):
        start_shared_ap(self.platform, self.address, self.box, channel, width, self.wait_for, names, upstream,
                        query_log=self.state / 'diag/tesla-dns.log' if self.status.get('tesla') else None,
                        concurrent=bool(upstream), router=bool(self.status.get('tesla')))   # supervisor.hotspot_router
        self.start_receiver(channel)
        self.status['channel'] = channel

    def own_names(self):
        """Keep the same local-name policy as the initial box AP, regardless of uplink state.

        deSEC names have no public A/AAAA records. Tesla and optional local DNS also
        keep their own name local; only unrelated names should use the uplink.
        """
        config = own_domain.load(self.state)
        local_only = bool(config and config.get('provider') == own_domain.DESEC)
        name = self.status.get('own_name')
        enabled = local_only or self.status.get('tesla') or setup_wizard.options(self.state).get('local_dns')
        return [name] if name and enabled else []

    def fallback(self, reason):
        self.stop_network()
        self.platform.cleanup_phone_uplink()
        names = list(setup_wizard.TESLA_CHECK_NAMES) if self.status.get('tesla') else []
        names.extend(self.own_names())
        self.start_ap(self.channel, self.width, names)
        self.set_status('offline', reason)
        self.next_try = self.clock() + self.backoff
        self.backoff = min(self.backoff * 2, 300)

    def tick(self):
        p = self.platform
        if self.phase == 'online':
            if not p.connected():
                self.ap_misses = 0
                self.fallback('手机热点掉线，已退回不上网')
            elif p.frequency() != self.frequency:
                self.ap_misses = 0
                self.fallback('手机热点信道改变，已退回不上网')
            elif not p.ap_ready():
                self.ap_misses += 1
                if self.ap_misses >= AP_READY_MISSES:
                    self.ap_misses = 0
                    self.fallback('共享热点停止，已退回不上网')
            else:
                self.ap_misses = 0
            return
        if not self.saved or self.clock() < self.next_try:
            return
        self.next_try = self.clock() + self.backoff
        network_changed = False
        try:
            if wired_uplink(p):
                # The cable is already the uplink: keep the AP (and local DNS) as it is, look again later.
                self.set_status('wired', '已接有线网络，不连手机热点')
                self.next_try = self.clock() + WIRED_RECHECK
                return
            if not p.concurrent_ap():
                self.set_status('offline', '无线驱动不支持同时连接和开热点')
                self.next_try = self.clock() + 300
                return
            supported, found, _ = p.find_hotspot(self.saved['ssid'], timeout=UPLINK_SCAN_TIMEOUT)
            if not supported or not found:
                self.set_status('offline', '没找到手机热点，保持不上网')
                self.backoff = min(self.backoff * 2, 300)
                return
            self.set_status('connecting', '已找到手机热点，正在连接')
            network_changed = True
            self.stop_network()
            p.prepare_phone_uplink()
            if not p.join_wifi(self.saved, {}, self.wait_for, 20):
                raise RuntimeError('手机热点连接失败')
            if any(a.version == 4 and a.network.overlaps(self.address.network) for a in p.local_addresses()):
                raise ValueError('手机热点与盒子网段冲突')
            self.frequency = p.frequency()
            if not self.frequency:
                raise RuntimeError('手机热点没有可用信道')
            upstream = p.uplink_gateway()
            if not upstream:
                raise RuntimeError('手机热点没有网关')
            if not p.add_phone_ap():
                raise RuntimeError('无法创建共享热点')
            p.enable_phone_nat(self.address.network)
            freq = int(self.frequency)
            channel = (freq - 2407) // 5 if freq < 3000 else (freq - 5000) // 5
            # Keep our own name local. Tesla checks and other websites use the real upstream.
            self.start_ap(channel, 20, self.own_names(), upstream)
            self.backoff = 15
            self.set_status('online', '已连上手机热点，车机可上网')
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
            # Exception strings from commands can contain credentials; only report the category.
            if network_changed:
                self.fallback('连接失败（' + type(error).__name__ + '），已退回不上网')
            else:
                # A failed scan has not changed the AP. Keep it and the receiver running until the next retry.
                self.set_status('offline', '手机热点检查暂不可用（' + type(error).__name__ + '），稍后重试')
                self.backoff = min(self.backoff * 2, 300)

    def close(self):
        self.platform.cleanup_phone_uplink()
