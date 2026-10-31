"""Own the daily phone-uplink lifecycle; the box AP is always the fallback.

Only Debian uses this controller. F50's current driver cannot do STA + AP.
Mode three keeps one AP, DHCP/DNS and receiver across uplink changes. Only a
channel change or a failed AP requires rebuilding the wireless downstream.
"""
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import own_domain
import setup_wizard

HOTSPOT = 'uplink-hotspot.json'
# Direct iPhone AP sharing the car's internet: the station's default route can come a moment after its address.
GATEWAY_WAIT = 10
WIRED_RECHECK = 30   # s: look again whether the cable still carries the uplink
AP_READY_MISSES = 3  # hostapd checks (2 s apart) before the AP is rebuilt: one miss is not a dead AP
#: brcmfmac stalls a scan for up to ~12 s while the AP is up (docs/交接-热点提速), so 5 s failed the whole
#: search and Mode B never found the hotspot (dp-0017 2026-10-31). A single-channel scan is ~0.1 s.
UPLINK_SCAN_TIMEOUT = 15
NETWORK_ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


class LanRecoveryFailed(RuntimeError):
    """Neither concurrent nor ordinary AP could recover; let the service restart."""

LINK_LOSS_GRACE = 15
ASSOCIATION_TIMEOUT = 30
# Looking for the phone hotspot (2026-10-30 request: people switch it on only after getting in, when the box has
# been up for 30-60 s). For FAST_WINDOW seconds after boot and after the uplink is lost, try every RETRY_FAST
# seconds; then wait longer each time, never more than RETRY_MAX. A miss costs one short scan, and the box
# hotspot and CarPlay stay up throughout.
RETRY_FAST = 10
FAST_WINDOW = 300
#: usb_tether: wait this long for the cable's lease before looking for the saved hotspot at startup.
SHARE_START_GRACE = 20
RETRY_MIN = 20
RETRY_MAX = 60
# Retries scan only the phone hotspot's last channel (a fraction of a second, the radio stays on its channel);
# every FULL_SCAN_EVERY-th try scans all channels in case the phone picked another one.
FULL_SCAN_EVERY = 3
LAST_HOTSPOT = 'uplink-last.json'   # {"ssid": sha256 prefix, "frequency": MHz}: never the name or password


class HotspotSearch:
    """When to look for the phone hotspot next and on which channel; shared by both uplink controllers."""
    def __init__(self, state, saved, clock):
        self.path, self.clock = Path(state) / LAST_HOTSPOT, clock
        self.key = hashlib.sha256(saved['ssid'].encode()).hexdigest()[:16] if saved else None
        self.frequency = None
        try:
            last = json.loads(self.path.read_text())
            if self.key and last.get('ssid') == self.key and int(last.get('frequency') or 0) > 0:
                self.frequency = int(last['frequency'])
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.tries = 0
        self.hurry()

    def hurry(self):
        """Start a new fast window: at boot and whenever a working uplink was lost."""
        self.fast_until, self.backoff = self.clock() + FAST_WINDOW, RETRY_MIN

    def found(self):
        self.backoff = RETRY_MIN

    def delay(self):
        """Seconds until the next try."""
        if self.clock() < self.fast_until:
            return RETRY_FAST
        delay, self.backoff = self.backoff, min(self.backoff * 2, RETRY_MAX)
        return delay

    def channel(self):
        """Frequency to scan on this try; None means all channels."""
        self.tries += 1
        if not self.frequency or self.tries % FULL_SCAN_EVERY == 1:
            return None
        return self.frequency

    def remember(self, frequency):
        if not self.key or not frequency or int(frequency) == self.frequency:
            return   # unchanged: no flash write
        self.frequency = int(frequency)
        try:
            setup_wizard.save_json(self.path, {'ssid': self.key, 'frequency': self.frequency})
        except OSError:
            pass   # only a speed-up


def wired_uplink(platform, resolve=None):
    """True when the box already has internet through something other than its Wi-Fi: the Ethernet cable at home.
    Then the phone hotspot is not needed, and joining it would take the access point and local DNS down for
    nothing (2026-10-26: the box kept rebuilding its AP while the cable was already online)."""
    try:
        devices = platform.default_route_devices()
    except (OSError, subprocess.SubprocessError):
        return False
    if not any(device not in (platform.interface, platform.ap_interface, 'lo') for device in devices):
        return False
    try:
        # libc DNS retries can block for tens of seconds. Never hold up the first
        # CarPlay invitation (or its health loop) waiting for a broken cable's DNS.
        if resolve is None:
            result = subprocess.run([sys.executable, '-c',
                "import socket; socket.getaddrinfo('www.duckdns.org', 443, type=socket.SOCK_STREAM)"],
                capture_output=True, timeout=2)
            return result.returncode == 0
        resolve('www.duckdns.org', 443, type=socket.SOCK_STREAM)
    except (OSError, subprocess.SubprocessError):
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
    """One persistent LAN, with a bounded, non-blocking station association beside it.

    start() runs before the first receiver launch. The station is frequency-limited,
    so an autonomous reconnect cannot silently move the AP to another channel.
    """
    def __init__(self, platform, state, address, box, offline_channel, offline_width,
                 wait_for, start_receiver, status, say=print, clock=time.monotonic, share=False):
        self.platform, self.state, self.address, self.box = platform, Path(state), address, box
        self.channel, self.width = offline_channel, offline_width
        # share: the box is the car's router and forwards whichever uplink is up. "USB 共享网络" uses it so the
        # car keeps the port when the cable is unplugged and the saved phone hotspot takes over (2026-10-31).
        self.share = share
        self.default_channel = offline_channel
        self.wait_for, self.start_receiver, self.status = wait_for, start_receiver, status
        self.say, self.clock = say, clock
        self.saved = hotspot(state)
        self.phase, self.frequency = 'offline', None
        self.prepared = self.started = self.station = False
        self.loss_since = self.connect_since = None
        self.ap_misses = 0
        self.search = HotspotSearch(state, self.saved, clock)
        self.next_try = clock()
        self.gateway = None

    def set_status(self, phase, reason):
        self.status.pop('uplink_error', None)
        changed = self.status.get('uplink', {}).get('reason') != reason or self.phase != phase
        self.phase = phase
        self.status['uplink'] = {'phase': phase, 'ssid': self.saved['ssid'] if self.saved else '',
                                 'reason': reason}
        # Historical field: it now means local Tesla check answers, not lack of internet.
        self.status['tesla_lan_stable'] = self.started
        self.status['tesla_offline'] = bool(self.status.get('tesla'))
        self.status['tesla_upstream'] = phase
        if changed:
            self.say(f'phone uplink {phase}: {reason}')

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

    def local_names(self):
        checks = list(setup_wizard.TESLA_CHECK_NAMES) if self.status.get('tesla') else []
        return checks + self.own_names()

    def scan(self):
        frequency = self.search.channel()
        if frequency:
            return self.platform.phone_hotspot_frequency(self.saved['ssid'], frequency=frequency,
                                                         timeout=UPLINK_SCAN_TIMEOUT)
        return self.platform.phone_hotspot_frequency(self.saved['ssid'], timeout=UPLINK_SCAN_TIMEOUT)

    def boot_channel(self, frequency):
        """Channel for the box hotspot at boot: the phone's, else its remembered 5 GHz one (then a phone switched
        on later joins without a channel change), else the quiet default. Never 2.4 GHz from memory alone."""
        if frequency:
            return self.channel_of(frequency)
        last = self.search.frequency
        if last and last > 5000 and self.usable(last):
            return self.channel_of(last)
        return self.channel

    def usable(self, frequency):
        return bool(frequency and not self.platform.ap_channel_problem(frequency))

    @staticmethod
    def channel_of(frequency):
        return 14 if frequency == 2484 else ((frequency - 2407) // 5 if frequency < 3000
                                            else (frequency - 5000) // 5)

    def configure_ap(self, channel):
        p = self.platform
        p.configure_hotspot(channel, 20, self.box['ssid'], self.box['passphrase'])
        p.n_only_ap()
        for _ in range(2):
            if p.start_ap(self.wait_for):
                self.channel = channel
                self.status['channel'] = channel
                return
            p.stop_child('hostapd')
        raise RuntimeError('shared hotspot failed twice')

    def start(self):
        """False means partial LAN ownership was cleared; supervisor can start an ordinary AP."""
        p = self.platform
        try:
            capable = bool(self.saved and p.concurrent_ap())
        except (OSError, ValueError, subprocess.SubprocessError):
            capable = False
        if not capable:
            self.set_status('offline', '未配置手机热点或无线驱动不支持并发；保留盒子热点')
            return False
        self.prepared = True
        try:
            p.prepare_phone_uplink()
            # usb_tether: the cable's default route says enough (no DNS probe, which alone cost up to 2 s
            # at boot when the USB lease was not up yet — C5 vs C1 cold-boot, 2026-10-31).
            # A wired cable counts as soon as it has carrier: its lease can be a few seconds late at boot,
            # and joining the hotspot meanwhile moved the AP to 2.4 GHz with the cable still the uplink.
            cable = (self.cable_present() if self.share else wired_uplink(p)) or p.wired_carrier()
            frequency = None
            # usb_tether: no scan at all at startup. It ran before the cable's lease existed, joined the saved
            # hotspot within half a second, moved the AP and restarted the receiver (dp-0017 2026-10-31: four
            # receiver starts on one boot). After SHARE_START_GRACE the first tick decides with the cable known.
            if not cable and not self.share:
                try:
                    frequency = self.scan()
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.say('boot uplink scan unavailable: ' + type(error).__name__)
            if not self.usable(frequency):
                frequency = None
            if not p.add_phone_ap():
                raise RuntimeError('cannot create persistent AP')
            p.set_ap_address(self.address)
            default_channel = self.channel
            chosen = default_channel if cable else self.boot_channel(frequency)
            try:
                self.configure_ap(chosen)
            except RuntimeError:
                if chosen == default_channel:
                    raise
                self.configure_ap(default_channel)
                frequency = None
            # Only Tesla uses this persistent router policy. Legacy cars retain their lifecycle.
            p.set_phone_dns(None)
            p.start_dhcp(self.address, False, self.local_names(), router=self.share or bool(self.status.get('tesla')),
                         query_log=self.state / 'diag/tesla-dns.log' if self.status.get('tesla') else None,
                         upstream_file=p.phone_resolv)
            p.enable_phone_nat(self.address.network)
            self.start_receiver(self.channel)
            p.check_lan_children()
        except NETWORK_ERRORS as error:
            self.clear_lan()
            self.set_status('offline', '持续热点启动失败（' + type(error).__name__ + '）；回退普通热点')
            return False
        self.started = True
        self.set_status('wired' if cable else 'offline',
                        '已接有线网络；局域网保持可用' if cable else '盒子热点就绪；后台寻找手机热点')
        # usb_tether: give the cable's lease a few seconds before chasing the hotspot. A service restart with
        # the cable plugged used to decide "no cable" before the address arrived, join the hotspot, then
        # switch back — each move rebuilt the AP and restarted the receiver (4 receiver starts in 15 s,
        # dp-0017 2026-10-31).
        self.next_try = self.clock() + (SHARE_START_GRACE if self.share and not cable else self.search.delay())
        if frequency:
            self.connect(frequency)
        return self.started

    def clear_lan(self):
        """Clear all partial LAN ownership before allowing the ordinary AP path to run."""
        self.started = False
        self.status['tesla_lan_stable'] = False
        self.status.pop('tesla_upstream', None)
        self.status.pop('uplink_link', None)
        errors = []
        for child in ('receiver', 'dnsmasq', 'hostapd'):
            try:
                self.platform.stop_child(child)
            except NETWORK_ERRORS as error:
                errors.append(type(error).__name__)
        try:
            if self.prepared:
                self.platform.cleanup_phone_uplink()
        except NETWORK_ERRORS as error:
            errors.append(type(error).__name__)
        if errors:
            raise LanRecoveryFailed('LAN cleanup failed: ' + ', '.join(errors))
        self.prepared = self.station = False
        self.gateway = self.loss_since = self.connect_since = None

    def restore_plain_ap(self):
        """Last local recovery. On success the supervisor resumes ordinary child monitoring."""
        try:
            self.clear_lan()
            start_shared_ap(self.platform, self.address, self.box, self.default_channel, 20,
                            self.wait_for, self.local_names(), router=self.share or bool(self.status.get('tesla')),
                            query_log=self.state / 'diag/tesla-dns.log' if self.status.get('tesla') else None)
            self.start_receiver(self.default_channel)
            self.platform.check_lan_children()
        except NETWORK_ERRORS as error:
            self.started = False
            self.set_status('failed', '热点恢复失败；等待服务重新启动')
            raise LanRecoveryFailed('ordinary hotspot recovery failed: ' + type(error).__name__) from None
        self.channel = self.default_channel
        self.status['channel'] = self.channel
        self.set_status('offline', '已恢复普通盒子热点；本次启动停止手机上联')

    def dns(self, gateway):
        if gateway != self.gateway:
            self.platform.set_phone_dns(gateway)
            self.gateway = gateway

    def disconnect_station(self):
        if self.station:
            self.platform.stop_phone_station()
        self.station = False
        self.loss_since = self.connect_since = None
        self.dns(None)

    def cable_present(self):
        """Whether something other than the Wi-Fi already has the default route.

        No DNS probe on purpose: over an Android or MiFi USB share that probe fails and succeeds in turn, and
        the answer must not oscillate — each flip moved the AP between channels and restarted the receiver
        (dp-0017, 2026-10-31).
        """
        try:
            devices = self.platform.default_route_devices()
        except (OSError, subprocess.SubprocessError):
            return False
        return any(device not in (self.platform.interface, self.platform.ap_interface, 'lo')
                   for device in devices)

    def defer(self, reason):
        self.disconnect_station()
        self.set_status('offline', reason)
        self.next_try = self.clock() + self.search.delay()

    def rebuild_ap(self, channel):
        """True if requested channel works; False if restored old channel or plain AP."""
        p = self.platform
        previous = self.channel
        self.status['tesla_lan_stable'] = False
        try:
            p.stop_child('receiver')
            p.stop_child('hostapd')
            try:
                self.configure_ap(channel)
                self.start_receiver(channel)
                p.check_lan_children()
            except NETWORK_ERRORS:
                p.stop_child('receiver')
                p.stop_child('hostapd')
                self.configure_ap(previous)
                self.start_receiver(previous)
                p.check_lan_children()
                self.status['tesla_lan_stable'] = True
                return False
        except NETWORK_ERRORS:
            self.restore_plain_ap()
            return False
        self.status['tesla_lan_stable'] = True
        return True

    def connect(self, frequency):
        if not self.usable(frequency):
            self.defer('上游信道不适合盒子热点；保持局域网')
            return
        try:
            channel = self.channel_of(frequency)
            if channel != self.channel:
                self.disconnect_station()
                self.say(f'phone uplink channel change: {self.channel} -> {channel}; rebuilding AP')
                if not self.rebuild_ap(channel):
                    if self.started:
                        self.defer('新信道不可用；已恢复原热点，稍后重试')
                    return
            # Mark ownership first so partially failed association commands can be cleaned up.
            self.station = True
            self.platform.start_phone_station(self.saved, frequency)
            self.frequency = frequency
            self.connect_since = self.clock()
            self.loss_since = None
            self.set_status('connecting', '正在连接手机热点；局域网 CarPlay 保持可用')
        except LanRecoveryFailed:
            raise
        except NETWORK_ERRORS as error:
            self.defer('上联连接失败（' + type(error).__name__ + '）；保持局域网')

    def tick(self):
        if not self.started:
            return
        p = self.platform
        # AP health is independent of station health. Only a confirmed AP failure rebuilds it.
        self.ap_misses = 0 if p.ap_ready() else self.ap_misses + 1
        if self.ap_misses >= AP_READY_MISSES:
            self.ap_misses = 0
            self.disconnect_station()
            self.rebuild_ap(self.channel)
            if not self.started:
                return
            self.set_status('offline', '盒子热点已恢复；后台重连上游')
            self.search.hurry()
            self.next_try = self.clock() + self.search.delay()
            return
        try:
            if self.station:
                if self.share and self.cable_present():
                    # USB 共享网络: the cable (or Ethernet) is the first choice, the joined hotspot only the
                    # fallback. Back on the cable: leave the phone hotspot and, if the AP had followed that
                    # hotspot onto 2.4 GHz, return it to its own (5 GHz) channel — nothing needs that channel
                    # any more and 2.4 GHz makes CarPlay stutter (dp-0017, 2026-10-31). An AP already on 5 GHz
                    # is left alone.
                    self.disconnect_station()
                    if self.channel <= 14:
                        self.rebuild_ap(self.default_channel)
                    self.set_status('wired', '已接 USB 共享网络；改用数据线供网')
                    self.next_try = self.clock() + self.search.delay()
                    return
                link = p.phone_link()
                self.status['uplink_link'] = link
                address = p.phone_station_address() if link['connected'] else None
                if address and address.network.overlaps(self.address.network):
                    self.defer('手机热点网段与盒子冲突；保持局域网')
                    return
                gateway = p.uplink_gateway() if address else None
                if link['connected'] and link['frequency'] != self.frequency:
                    # freq_list should prevent this; never chase a driver's unexpected move in a loop.
                    self.defer('上游信道改变；重新扫描，保持局域网')
                    return
                if link['connected'] and gateway:
                    self.dns(gateway)
                    self.loss_since = self.connect_since = None
                    if self.phase != 'online':
                        self.search.found()
                        self.search.remember(self.frequency)
                    self.set_status('online', '手机热点已连接；互联网可用性取决于手机网络')
                    return
                if self.phase == 'online':
                    self.loss_since = self.clock()
                    self.set_status('recovering', '上联暂时中断；保留热点和 CarPlay，等待恢复')
                since = self.loss_since if self.loss_since is not None else self.connect_since
                timeout = LINK_LOSS_GRACE if self.loss_since is not None else ASSOCIATION_TIMEOUT
                if since is not None and self.clock() - since >= timeout:
                    if self.loss_since is not None:
                        self.search.hurry()   # a working uplink was lost: the phone is probably near
                    self.defer('上联未恢复；保留热点和 CarPlay，稍后重试')
                return
            if self.clock() < self.next_try:
                return
            self.next_try = self.clock() + self.search.delay()
            # usb_tether: the cable decides with its default route alone (cable_present). wired_uplink's DNS
            # probe fails and succeeds in turn over a phone's USB share, so the controller kept leaving for
            # the hotspot and coming back, moving the AP each way and restarting the receiver (dp-0017,
            # 2026-10-31: CarPlay dropped about every half minute).
            if self.cable_present() if self.share else wired_uplink(p):
                # The cable is the uplink now. An AP left on the hotspot's 2.4 GHz channel makes CarPlay
                # stutter and nothing needs that channel any more (2026-10-31: plugging Ethernet after the
                # hotspot had already dropped kept the AP on channel 3).
                if self.share and self.channel <= 14:
                    self.rebuild_ap(self.default_channel)
                self.set_status('wired', '已接有线网络；局域网保持可用')
                self.next_try = self.clock() + WIRED_RECHECK
                return
            frequency = self.scan()
            if frequency:
                self.connect(frequency)
            else:
                self.set_status('offline', '没找到手机热点；局域网保持可用')
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # A failed status query is not proof of disassociation. Do not tear down the LAN.
            self.status['uplink_error'] = type(error).__name__
            self.next_try = max(self.next_try, self.clock() + RETRY_FAST)
            self.say('phone uplink check unavailable: ' + type(error).__name__)
        else:
            self.status.pop('uplink_error', None)

    def close(self):
        self.status['tesla_lan_stable'] = False
        if self.prepared:
            self.platform.cleanup_phone_uplink()
        self.prepared = self.started = False


class LegacyPhoneUplink:
    """Non-Tesla: preserve the previous online-router/offline-no-router policy."""
    def __init__(self, platform, state, address, box, offline_channel, offline_width,
                 wait_for, start_receiver, status, say=print, clock=time.monotonic):
        self.platform, self.state, self.address, self.box = platform, Path(state), address, box
        self.channel, self.width = offline_channel, offline_width
        self.wait_for, self.start_receiver, self.status, self.say, self.clock = wait_for, start_receiver, status, say, clock
        self.phase, self.frequency = 'offline', None
        self.ap_misses = 0
        self.saved = hotspot(state)
        self.search = HotspotSearch(state, self.saved, clock)
        self.next_try = clock() + 5
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
        """Keep the same local-name policy as the initial box AP (setup_wizard.answers_own_name), regardless of
        uplink state; only unrelated names use the uplink."""
        chosen = dict(setup_wizard.options(self.state), tesla=bool(self.status.get('tesla')))
        name = self.status.get('own_name')
        return [name] if name and setup_wizard.answers_own_name(chosen, own_domain.load(self.state)) else []

    def fallback(self, reason):
        was_online = self.phase == 'online'
        self.stop_network()
        self.platform.cleanup_phone_uplink()
        names = list(setup_wizard.TESLA_CHECK_NAMES) if self.status.get('tesla') else []
        names.extend(self.own_names())
        self.start_ap(self.channel, self.width, names)
        self.set_status('offline', reason)
        if was_online:
            self.search.hurry()   # the phone is probably still near: look again soon
        self.next_try = self.clock() + self.search.delay()

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
        self.next_try = self.clock() + self.search.delay()
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
            frequency = self.search.channel()
            if frequency:
                supported, found, _ = p.find_hotspot(self.saved['ssid'], frequency=frequency, timeout=10)
            else:
                supported, found, _ = p.find_hotspot(self.saved['ssid'], timeout=10)
            if not supported or not found:
                self.set_status('offline', '没找到手机热点，保持不上网')
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
            self.search.found()
            self.search.remember(freq)
            self.set_status('online', '已连上手机热点，车机可上网')
        except (OSError, ValueError, RuntimeError, TimeoutError, subprocess.SubprocessError) as error:
            # Exception strings from commands can contain credentials; only report the category.
            if network_changed:
                self.fallback('连接失败（' + type(error).__name__ + '），已退回不上网')
            else:
                # A failed scan has not changed the AP. Keep it and the receiver running until the next retry.
                self.set_status('offline', '手机热点检查暂不可用（' + type(error).__name__ + '），稍后重试')

    def close(self):
        self.platform.cleanup_phone_uplink()
