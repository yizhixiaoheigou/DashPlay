"""Reversible connectivity-check DNS, independent of the local CarPlay network.

No configuration, interface, DHCP or receiver restart on a health transition.
Probe the selected uplink directly, never the locally answered check names.
"""
import ipaddress
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import threading
import time

INTERVAL = 2.0
BUDGET = 2.0
RECOVERIES = 3
FAILURES = 2
# Keep the foreign check names local, without sending probes to them from mainland China.
# A domestic fallback can prove internet availability but cannot release a failed Tesla check.
CHECKS = {
    "connman.vn.cloud.tesla.cn": ("/online/status.html", "connman"),
    "connman.vn.tesla.services": ("/online/status.html", "connman"),
    "connectivitycheck.gstatic.com": ("/generate_204", "204"),
}
PROBES = {
    "connman.vn.cloud.tesla.cn": CHECKS["connman.vn.cloud.tesla.cn"],
    "connectivitycheck.platform.hicloud.com": ("/generate_204", "204"),
}
FALLBACK_AFTER = 0.5
MAX_RESPONSE = 2048


def ipv4_forwarding():
    try:
        return Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '1'
    except OSError:
        return False


def local_scope(previous, names):
    """Append owned local-only domains without changing existing DNS scope."""
    if previous and (not previous.startswith('/') or not previous.endswith('/')):
        raise ValueError('Unsupported dnsmasq local domain syntax')
    names = [name for name in dict.fromkeys(names) if name and name not in (previous or '').split('/')]
    if not names:
        return previous
    return (previous or '/') + '/'.join(names) + '/'


def dns_name(data, offset, depth=0):
    if depth > 16:
        raise ValueError("DNS compression loop")
    labels = []
    while True:
        size = data[offset]; offset += 1
        if size & 0xc0 == 0xc0:
            pointer = ((size & 63) << 8) | data[offset]
            suffix, _ = dns_name(data, pointer, depth + 1)
            return ".".join(labels + [suffix]).lower(), offset + 1
        if size & 0xc0:
            raise ValueError("DNS label")
        if not size:
            return ".".join(labels).lower(), offset
        label = data[offset:offset + size]
        if len(label) != size:
            raise ValueError("DNS truncation")
        labels.append(label.decode("ascii")); offset += size


def dns_answer(data, transaction, host):
    ident, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    if ident != transaction or flags & 0x820f != 0x8000 or questions != 1 or answers > 128:
        raise ValueError("DNS response")
    name, offset = dns_name(data, 12)
    if name != host or data[offset:offset + 4] != b"\0\1\0\1":
        raise ValueError("DNS question")
    offset += 4
    aliases, addresses = {}, {}
    for _ in range(answers):
        name, offset = dns_name(data, offset)
        kind, family, _, size = struct.unpack("!HHIH", data[offset:offset + 10]); offset += 10
        if offset + size > len(data):
            raise ValueError("DNS truncation")
        if family == 1 and kind == 5:
            aliases[name], _ = dns_name(data, offset)
        if family == 1 and kind == 1 and size == 4:
            address = ipaddress.IPv4Address(data[offset:offset + size])
            if address.is_global:
                addresses[name] = str(address)
        offset += size
    for _ in range(16):
        if host in addresses:
            return addresses[host]
        host = aliases.get(host)
        if not host:
            break
    raise ValueError("no public address for requested name")


def http_success(data, kind):
    head, separator, body = data.partition(b"\r\n\r\n")
    if not separator:
        return False
    try:
        lines = head.splitlines()
        if lines[0].split()[0] not in (b'HTTP/1.0', b'HTTP/1.1'):
            return False
        status = int(lines[0].split()[1])
        headers = dict((key.strip().lower(), value.strip()) for line in lines[1:] if b":" in line
                       for key, value in [line.split(b":", 1)])
        if kind == "connman":
            return status in (200, 403) and headers.get(b"x-connman-status") == b"online"
        if kind == "204":
            return status == 204
        return status == 200 and b"<TITLE>Success</TITLE>" in body and b"<BODY>Success</BODY>" in body
    except (ValueError, IndexError):
        return False


class Probe:
    def __init__(self, resolv, ap_interface, run=subprocess.run, servers=(), ready=None):
        self.resolv = Path(resolv) if resolv else None
        self.ap_interface, self.run, self.servers = ap_interface, run, tuple(servers)
        self.ready = ready

    def route(self):
        # Plain ip output also works with OpenWrt BusyBox; no libc DNS or proxy.
        result = self.run(["ip", "-4", "route", "get", "1.1.1.1"], capture_output=True,
                          text=True, timeout=0.3, check=True)
        fields = result.stdout.split()
        if any(word in fields for word in ("unreachable", "blackhole", "prohibit", "linkdown")):
            return None
        interface = fields[fields.index("dev") + 1]
        if interface in (self.ap_interface, "lo") or interface.startswith("tailscale"):
            return None
        servers = []
        try:
            lines = self.resolv.read_text().splitlines() if self.resolv else []
        except OSError:
            lines = []
        for line in [*(f"nameserver {server}" for server in self.servers), *lines]:
            parts = line.split()
            if len(parts) == 2 and parts[0] == "nameserver":
                try:
                    address = ipaddress.IPv4Address(parts[1])
                except ValueError:
                    continue
                if not address.is_loopback and not address.is_unspecified and str(address) not in servers:
                    servers.append(str(address))
        if not servers:
            # Link can be present while DNS is not ready: do not call that internet.
            servers = []
        # uid, cache expiry and PMTU are not changes of uplink. ip-full and
        # BusyBox format these differently, sometimes on every invocation.
        key = tuple((name, fields[fields.index(name) + 1]) for name in ('dev', 'via', 'src', 'table') if name in fields)
        return interface, servers[:3], (key, tuple(servers))

    @staticmethod
    def bound_socket(kind, interface):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0")
            return sock
        except BaseException:
            sock.close()
            raise

    def endpoint(self, host, path, kind, interface, servers, deadline):
        address = None
        for server in servers:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            try:
                transaction = secrets.randbelow(65536)
                question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split(".")) + b"\0\0\1\0\1"
                with self.bound_socket(socket.SOCK_DGRAM, interface) as sock:
                    sock.settimeout(min(0.4, left))
                    sock.connect((server, 53))
                    sock.settimeout(max(0.001, min(0.4, deadline - time.monotonic())))
                    sock.send(struct.pack("!6H", transaction, 0x100, 1, 0, 0, 0) + question)
                    sock.settimeout(max(0.001, min(0.4, deadline - time.monotonic())))
                    address = dns_answer(sock.recv(4096), transaction, host)
                break
            except (OSError, ValueError, IndexError, struct.error):
                continue
        if address is None or time.monotonic() >= deadline:
            return False
        try:
            with self.bound_socket(socket.SOCK_STREAM, interface) as sock:
                sock.settimeout(max(0.001, deadline - time.monotonic()))
                sock.connect((address, 80))
                sock.settimeout(max(0.001, deadline - time.monotonic()))
                sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\nAccept-Encoding: identity\r\n\r\n".encode())
                data = b""
                while time.monotonic() < deadline and len(data) < MAX_RESPONSE:
                    sock.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = sock.recv(min(512, MAX_RESPONSE - len(data)))
                    if not chunk:
                        break
                    data += chunk
                    if http_success(data, kind):
                        return True
                    if b"\r\n\r\n" in data and kind in ("connman", "204"):
                        return False  # no body required; do not download a login/error page
                return http_success(data, kind)
        except OSError:
            return False

    def __call__(self):
        began = time.monotonic(); deadline = began + BUDGET
        result = {"link": False, "internet": False, "checks": {}, "route": None}
        try:
            route = self.route()
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            route = None
        if self.ready is not None:
            result['forwarding_ready'] = bool(self.ready())
            if not result['forwarding_ready']:
                result['link'] = bool(route)
                route = None  # clear the health history and retain protection, even if the box is online
        if route:
            interface, servers, result["route"] = route
            result["link"] = True
            answers = {}
            def check(host, spec):
                try:
                    answers[host] = self.endpoint(host, *spec, interface, servers, deadline)
                except (OSError, ValueError, AttributeError):
                    answers[host] = False
            workers = [threading.Thread(target=check, args=(host, spec), daemon=True) for host, spec in PROBES.items()]
            workers[0].start()
            workers[0].join(min(FALLBACK_AFTER, max(0, deadline - time.monotonic())))
            # The China Tesla endpoint proves both general internet and its own
            # check in one small request. Hedge only on failure/slow response;
            # both attempts still share the original round deadline.
            if not answers.get(next(iter(PROBES)), False):
                workers[1].start()
            else:
                workers = workers[:1]
            # Every socket operation shares this deadline; join all before a new round.
            for worker in workers: worker.join()
            result["checks"] = {host: bool(answers.get(host)) for host in CHECKS}
            result["internet"] = any(answers.values())
        result["elapsed_ms"] = round((time.monotonic() - began) * 1000)
        return result


class Policy:
    def __init__(self, names):
        self.names = tuple(names)
        self.healthy = {key: False for key in ("internet", *CHECKS)}
        self.good = dict.fromkeys(self.healthy, 0)
        self.bad = dict.fromkeys(self.healthy, 0)
        self.route = None

    def update(self, sample):
        if sample["route"] != self.route or not sample["link"]:
            self.route = sample["route"]
            for key in self.healthy:
                self.healthy[key] = False; self.good[key] = self.bad[key] = 0
        retry = False
        for key in self.healthy:
            ok = sample["link"] and (sample["internet"] if key == "internet" else sample["checks"].get(key, False))
            if ok:
                self.good[key] += 1; self.bad[key] = 0
                if self.good[key] >= RECOVERIES: self.healthy[key] = True
            else:
                self.bad[key] += 1; self.good[key] = 0
                retry |= self.healthy[key] and self.bad[key] == 1
                if self.bad[key] >= FAILURES: self.healthy[key] = False
        local = tuple(name for name in self.names if not self.healthy.get(name if name in CHECKS else "internet", False))
        return local, retry


class DnsFiles:
    def __init__(self, directory, address, names):
        self.directory, self.address, self.names = Path(directory), str(address), tuple(names)
        self.hosts = self.directory / "checks.hosts"
        self.servers = self.directory / "checks.servers"
        self.applied = None
        self.dirty = False

    def write(self, names):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o755)
        for path, text in ((self.hosts, "".join(f"{self.address} {name}\n" for name in names)),
                           (self.servers, "".join(f"server=/{name}/\n" for name in names))):
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text); temporary.chmod(0o644); temporary.replace(path)

    def prepare(self):
        self.write(self.names)
        self.applied = self.names
        return [f"--addn-hosts={self.hosts}", f"--servers-file={self.servers}", "--local-ttl=0"]

    def apply(self, names, reload):
        if names == self.applied and not self.dirty:
            return
        self.dirty = True  # timeout may mean the signal arrived; reconcile even if policy returns to the old state
        self.write(names)
        reload()  # failed reload is retried next round; no service restart fallback
        self.applied = names
        self.dirty = False


class Monitor:
    def __init__(self, probe, files, reload, notify=None):
        self.probe, self.files, self.reload, self.notify = probe, files, reload, notify
        self.policy = Policy(files.names)
        self.stop = threading.Event(); self.wake = threading.Event()
        self.thread = self.events = None
        self.netlink = None
        self.revision = 0
        self.apply_lock = threading.RLock()
        self.status = {"link": None, "internet": None, "checks": {}, "local": list(files.names), "mode": "protect"}

    def changed(self):
        self.revision += 1
        self.wake.set()

    def step(self):
        revision = self.revision
        sample = self.probe()
        with self.apply_lock:
            if self.stop.is_set(): return False
            if revision != self.revision: return True  # obsolete result from the old uplink
            local, retry = self.policy.update(sample)
            error = None
            try:
                self.files.apply(local, self.reload)
            except (OSError, subprocess.SubprocessError) as failure:
                error = type(failure).__name__
            applied = self.files.applied
            self.status = {**sample, "local": list(applied), "error": error,
                           "mode": "protect" if len(applied) == len(self.files.names) else "public" if not applied else "partial"}
            if self.notify: self.notify(self.status)
            return retry

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        def events():
            while not self.stop.is_set():
                try:
                    self.netlink.recv(65536); self.changed()
                except socket.timeout: pass
                except OSError: break
        try:
            self.netlink = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
            self.netlink.bind((0, 1 | 0x10 | 0x40))  # link, IPv4 addresses and routes
            self.netlink.settimeout(0.3)
            self.events = threading.Thread(target=events, name="connectivity-events", daemon=True)
            self.events.start()
        except (OSError, AttributeError):
            if self.netlink: self.netlink.close()
            self.netlink = None  # timer remains a fallback on systems without route events
        def worker():
            while not self.stop.is_set():
                self.wake.clear(); began = time.monotonic()
                try:
                    retry = self.step()
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.status = {**self.status, "error": type(error).__name__}
                    if self.notify: self.notify(self.status)
                    retry = False
                self.wake.wait(0 if retry else max(0.05, INTERVAL - (time.monotonic() - began)))
        self.thread = threading.Thread(target=worker, name="connectivity", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set(); self.wake.set()
        # Drain any write/reload already committed to by step(). A probe still
        # in flight will see stop before touching files or the next DNS process.
        with self.apply_lock:
            pass
        if self.netlink: self.netlink.close()
        if self.thread: self.thread.join(BUDGET + 1)
        if self.events: self.events.join(0.5)


def description(status):
    if status.get("error"):
        if status.get("retry") is False:
            return "动态联网检测未启用，已保留代答；投屏域名保持本地解析。"
        return "联网检测或 DNS 更新暂不可用，正在重试；投屏域名保持本地解析。"
    if status.get('forwarding_ready') is False:
        return "共享网络尚未就绪，保留联网检测代答和本地投屏。"
    if status.get("internet") is None:
        return "正在检测真实外网，先保留联网检测代答。"
    if not status.get("internet"):
        return "上联已连接但外网未通过检测；保留本地投屏，正在确认并保护车机连接。" if status.get("link") else "上联未连接，已保留联网检测代答和本地投屏。"
    if status.get("mode") == "protect":
        return "外网已响应，连续确认后恢复网站解析；本地投屏保持运行。"
    if status.get("mode") == "partial":
        return "外网已响应，普通网站正常解析；部分联网检测仍由盒子代答。"
    return "外网和联网检测已通过，网站正常解析；投屏域名保持本地解析。"
