"""Optional: a DuckDNS name with a Let's Encrypt certificate, for car browsers that refuse the box's local CA
and offer no way past the warning (2026-10-13: a head unit's Chromium showed ERR_CERT_AUTHORITY_INVALID with
no "continue"). Off unless the owner enters a DuckDNS sub-domain and token: on the box in the settings page
(state/ddns.json), on OpenWrt in LuCI (uci dashplay.main.duckdns_domain / duckdns_token).

- Preconfigured deSEC names use the same runtime and upgrade package. state/ddns.json selects provider=desec,
  a full domain name and a per-device token. Only DNS-01 TXT is public; the device's hotspot answers A locally.
  Factory provisioning tools and actual credentials are separate from the public program.
- The public DuckDNS record points the name at the address the head unit opens: the box on the car hotspot or
  portable Wi-Fi, 192.168.78.1 on the box hotspot, the router's own address on OpenWrt. DuckDNS takes a
  private address; a DNS server that filters those ("rebind protection") will not resolve the name.
- The certificate comes from Let's Encrypt through acme.sh (bundled in acme/) with a DNS-01 challenge: only
  outgoing HTTPS, nothing has to reach the box, so it works behind the car hotspot. It is installed in
  state/tls/own/, which boxnet serves while valid; a new one loads on the next start, never mid-drive.
  Renewed when less than RENEW_DAYS remain, whenever the box is online (box hotspot: no internet in the car,
  so at home on the Ethernet cable).
- The token reaches acme.sh by its environment only (not its command line); it is never logged or shown.
- An attempt starts only when the box has a default route and both DuckDNS and the CA resolve; without that it
  waits 15 s and tries again (no penalty). A failed attempt is retried after 60/120/300/900 s, then hourly; the
  settings page can ask for one now, and a certificate can be imported there. acme.sh 3.1.6 sends its requests
  without any timeout, so a curl shim in state/acme/bin bounds each one (the pinned acme.sh itself is untouched).
- Optional, on the car hotspot or portable Wi-Fi ("ipv6": true): the record also carries the box's own public
  IPv6 address, for car browsers that refuse a LAN address (the picture still comes straight over the Wi-Fi). The
  head unit asks that hotspot, which asks DuckDNS, so the address is public: it is published only behind an IPv6
  firewall (nftables, guard_rules) that lets in nothing but the Wi-Fi's own prefix and link-local addresses; a
  box where it cannot be loaded never publishes it. OpenWrt does this differently (uci public_ipv6): the router answers the
  name itself, so nothing is published.
"""
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SUFFIX = ".duckdns.org"
DESEC = "desec"
DOMAIN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
TOKEN = re.compile(r"[A-Za-z0-9-]{16,64}")   # DuckDNS shows a UUID
DESEC_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,64}")   # deSEC shows 28 URL-safe characters
RENEW_DAYS = 30            # Let's Encrypt certificates last 90 days
RECORD_REFRESH = 1800      # seconds between DuckDNS updates once one succeeded
RECORD_RETRY = 60
DESEC_TTL = 900            # the lowest deSEC takes for this zone (dns_desec.sh); a changed address can take
                           # up to this long to reach a car that asked before
DESEC_DENIED_RETRY = 6 * 3600   # a token without A permission, or a record deSEC refuses (TTL/zone): look again
                                # now and then; retrying every minute cannot fix either and only hits the rate limit
CERT_CHECK = 12 * 3600
CERT_RETRY = 3600          # once the quick attempts below are used up
CERT_RETRY_STEPS = (60, 120, 300, 900)   # first failures: the authorization is still valid, a retry is cheap
READY_RETRY = 15           # no default route or DNS yet: not a failure, just look again soon
ACME_TIMEOUT = 240         # the whole acme.sh run; the curl shim below caps every single request
ACME_CONNECT_TIMEOUT = 10
ACME_REQUEST_TIMEOUT = 90
ACME_RETRY = 2
DNS_SLEEP = 60             # wait for the TXT record instead of acme.sh's DoH check (blocked on some networks)
ACME_SERVER = os.environ.get("DASHPLAY_ACME_SERVER", "letsencrypt")   # tests: letsencrypt_test
ACME_HOSTS = {"letsencrypt": "acme-v02.api.letsencrypt.org",
              "letsencrypt_test": "acme-staging-v02.api.letsencrypt.org"}


def sub_domain(text):
    """'MyCar', 'mycar.duckdns.org' -> 'mycar'; ValueError when not a DuckDNS sub-domain."""
    domain = (text or "").strip().lower().removesuffix(SUFFIX)
    if not DOMAIN.fullmatch(domain):
        raise ValueError("DuckDNS 子域名只能包含小写字母、数字和连字符。")
    return domain


def check_token(token):
    token = (token or "").strip()
    if not TOKEN.fullmatch(token):
        raise ValueError("DuckDNS token 格式不对：在 duckdns.org 登录后页面上方的 token 一栏复制。")
    return token


def domain_name(text):
    """A full DNS name, normalized; ValueError when invalid."""
    name = (text or "").strip().lower().rstrip(".")
    if not name or len(name) > 253 or "." not in name:
        raise ValueError("域名要填完整名称，如 box01.car.example.com。")
    if any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")):
        raise ValueError("域名只能包含小写字母、数字、连字符和点，每段不能以连字符开头或结尾。")
    return name


def check_desec_token(token):
    token = (token or "").strip()
    if not DESEC_TOKEN.fullmatch(token):
        raise ValueError("deSEC token 格式不对：在 deSEC 的 Tokens 页面新建后复制。")
    return token


def load(state):
    """state/ddns.json -> DuckDNS {"domain": sub-domain, "token": ..., ["ipv6": True]} or deSEC
    {"provider": "desec", "domain": full-name, "token": ...}; None when off or unreadable."""
    try:
        config = json.loads((Path(state) / "ddns.json").read_text())
        provider = str(config.get("provider") or "duckdns").lower()
        if provider == DESEC:
            return {"provider": DESEC, "domain": domain_name(config["domain"]),
                    "token": check_desec_token(config["token"])}
        if provider != "duckdns":
            return None
        loaded = {"domain": sub_domain(config["domain"]), "token": check_token(config["token"])}
        if config.get("ipv6") is True:
            loaded["ipv6"] = True
        return loaded
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def name_of(config):
    if not config:
        return None
    return config["domain"] if config.get("provider") == DESEC else config["domain"] + SUFFIX


def directory_host():
    """The ACME server's host name, for the readiness check (acme.sh takes a name or a full directory URL)."""
    if "://" in ACME_SERVER:
        return urllib.parse.urlsplit(ACME_SERVER).hostname or ""
    return ACME_HOSTS.get(ACME_SERVER, ACME_HOSTS["letsencrypt"])


def default_route_families(run=subprocess.run, proc=Path("/proc")):
    """[4] or [4, 6] when the box has a default route, [] when it has none (early in boot, link down).

    `ip -j` is the Debian way; OpenWrt's BusyBox ip has no JSON, so /proc is the fallback there (without it the
    readiness check would wait forever and never renew)."""
    families = set()
    for flag in ("-4", "-6"):
        try:
            out = run(["ip", flag, "-j", "route", "show", "default"], capture_output=True, text=True, timeout=5)
            routes = json.loads(out.stdout or "[]")
            if out.returncode == 0 and isinstance(routes, list) and any(
                    isinstance(route, dict) and route.get("type", "unicast") == "unicast"
                    and route.get("dev") not in (None, "", "lo") for route in routes):
                families.add(int(flag[1]))
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    if 4 not in families:
        try:   # Iface Destination Gateway Flags ... Mask ...: /0, up, not a reject route
            for line in (Path(proc) / "net/route").read_text().splitlines()[1:]:
                fields = line.split()
                if (len(fields) >= 11 and fields[0] != "lo" and fields[1] == fields[7] == "00000000"
                        and int(fields[3], 16) & 1 and not int(fields[3], 16) & 0x200):
                    families.add(4)
                    break
        except (OSError, ValueError):
            pass
    if 6 not in families:
        try:   # ::/0 must be UP and not REJECT; Linux also lists an unreachable ::/0 on lo
            for line in (Path(proc) / "net/ipv6_route").read_text().splitlines():
                fields = line.split()
                if (len(fields) == 10 and fields[0] == "0" * 32 and fields[1] == "00" and fields[9] != "lo"
                        and int(fields[8], 16) & 1 and not int(fields[8], 16) & 0x200):
                    families.add(6)
                    break
        except (OSError, ValueError):
            pass
    return sorted(families)


def network_ready(run=subprocess.run, resolve=socket.getaddrinfo, provider=None):
    """None when the box can reach its DNS provider and the CA, else 'route' or 'dns' for the settings page.

    Checked before acme.sh runs: at boot the first attempt used to land while the access point was still
    being rebuilt and the Wi-Fi uplink was down, which cost an hour of retry (2026-10-26, N1 mode 3)."""
    if not default_route_families(run):
        return "route"
    for host in (("desec.io" if provider == DESEC else "www.duckdns.org"), directory_host()):
        try:
            resolve(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            return "dns"
    return None


def retry_delay(failures):
    """Seconds to the next attempt: 60, 120, 300, 900, then hourly."""
    return CERT_RETRY_STEPS[failures - 1] if 1 <= failures <= len(CERT_RETRY_STEPS) else CERT_RETRY


CURL_FAILURE = re.compile(r"error code: (\d{1,3})")


def curl_failure(log):
    """The curl exit code from the last acme.sh log ('6' name lookup, '28' timeout), digits only."""
    try:
        found = CURL_FAILURE.findall(Path(log).read_text(errors="replace"))
    except OSError:
        return None
    return found[-1] if found else None


def failure_reason(error, log=None):
    """A short code for state/own-domain.json: never a message, which may carry the token."""
    code = curl_failure(log) if log else None
    if code:
        return "curl" + code
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ValueError):
        return "certificate"
    if isinstance(error, OSError):
        return "network"
    return "acme"


def reason_text(reason):
    """The settings page's words for failure_reason (and the readiness codes)."""
    if reason == "route":
        return "盒子还没有默认路由（没连上网络）"
    if reason == "dns":
        return "域名解析失败"
    if reason == "timeout":
        return "acme.sh 超时"
    if reason == "busy":
        return "上一轮证书申请仍在结束，稍后继续"
    if reason and reason.startswith("curl") and reason[4:].isdigit():
        return f"连接 Let's Encrypt 失败（curl {reason[4:]}）"
    if reason == "network":
        return "网络中断"
    if reason == "certificate":
        return "证书内容不对"
    return "acme.sh 申请没有成功"


def forget_certificate(state):
    """When the name changes or the feature is turned off: the certificate is for the old name."""
    shutil.rmtree(Path(state) / "tls" / "own", ignore_errors=True)
    (Path(state) / "own-domain.json").unlink(missing_ok=True)


def update_desec(config, address, timeout=15):
    """The deSEC name's public A record -> address, so that a car asking its own hotspot or a MiFi finds the box
    too (the box still answers the name on its own hotspot). The zone is the name without its first label, as
    the device provider names one label per box. "ok"; "denied" (403, or 404 for a domain the token may not write:
    a token made before 2026-10-29 has the ACME TXT only, or the name's zone is not the one deSEC holds); "invalid"
    (400/422: deSEC refuses this TTL or record, which retrying cannot fix); "failed" (no internet, throttled;
    retried every minute)."""
    sub, zone = config["domain"].split(".", 1)
    body = json.dumps([{"subname": sub, "type": "A", "ttl": DESEC_TTL, "records": [address]}]).encode()
    request = urllib.request.Request(f"https://desec.io/api/v1/domains/{zone}/rrsets/", data=body, method="PUT",
                                     headers={"Authorization": "Token " + config["token"],
                                              "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(16)
        return "ok"
    except urllib.error.HTTPError as error:
        if error.code in (403, 404):
            return "denied"
        return "invalid" if error.code in (400, 422) else "failed"
    except (OSError, ValueError):   # no internet yet; URLError is an OSError
        return "failed"


def update_record(config, address, ipv6=None, clear=False, timeout=15):
    """Point the name at address (private addresses allowed) and, when given, its AAAA at ipv6. clear: drop both
    records first (DuckDNS has no way to drop the AAAA alone). True when DuckDNS answered OK. A deSEC name: its A
    record only, and the result of update_desec."""
    if config.get("provider") == DESEC:
        return update_desec(config, address, timeout)
    def call(fields):
        query = urllib.parse.urlencode(dict(fields, domains=config["domain"], token=config["token"]))
        with urllib.request.urlopen("https://www.duckdns.org/update?" + query, timeout=timeout) as response:
            return response.read(16).strip() == b"OK"
    if clear and not call({"clear": "true"}):
        return False
    return call(dict({"ip": address}, **({"ipv6": ipv6} if ipv6 else {})))


NFT_TABLE = "dashplay_v6"


def public_ipv6(interface, run=subprocess.run):
    """(address, prefix length) of the interface's public IPv6 address, a stable one before a temporary one;
    None when it has none (the hotspot hands out no IPv6, or only ULA)."""
    try:
        out = run(["ip", "-6", "-j", "addr", "show", "dev", interface, "scope", "global"],
                  capture_output=True, text=True, timeout=5)
        links = json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    found = []
    for link in links if isinstance(links, list) else []:
        for info in link.get("addr_info", []):
            try:
                address = ipaddress.IPv6Address(info.get("local", ""))
            except ValueError:
                continue
            if info.get("family") != "inet6" or not address.is_global or any(
                    info.get(flag) for flag in ("deprecated", "tentative", "dadfailed")):
                continue
            found.append((bool(info.get("temporary")), str(address), int(info.get("prefixlen", 64))))
    return min(found)[1:] if found else None


def guard_rules(address, prefix_length):
    """nftables: IPv6 into the box only from its own Wi-Fi (the address's prefix) and link-local addresses, plus
    ICMPv6 and DHCPv6 replies; IPv4 is left alone. Replaces the table as one transaction."""
    network = ipaddress.IPv6Network(f"{address}/{min(max(prefix_length, 48), 128)}", strict=False)
    return (f"table inet {NFT_TABLE}\ndelete table inet {NFT_TABLE}\n"
            f"table inet {NFT_TABLE} {{\n  chain input {{\n    type filter hook input priority filter; policy accept;\n"
            "    meta nfproto ipv4 accept\n    iifname \"lo\" accept\n    ct state established,related accept\n"
            "    meta l4proto ipv6-icmp accept\n    ip6 saddr fe80::/10 accept\n"
            f"    ip6 saddr {network} accept\n    udp dport 546 accept\n    counter drop\n  }}\n}}\n")


def nft(commands, run=subprocess.run):
    """Run nftables commands: with the nft program, or else through libnftables, which the box images carry (as a
    library of their other packages) without the program. False when neither is there or they were refused."""
    if shutil.which("nft"):
        try:
            return run(["nft", "-f", "-"], input=commands, capture_output=True, text=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        import ctypes
        lib = ctypes.CDLL("libnftables.so.1")
    except OSError:
        return False
    lib.nft_ctx_new.restype = ctypes.c_void_p
    lib.nft_ctx_free.argtypes = [ctypes.c_void_p]
    lib.nft_ctx_buffer_output.argtypes = lib.nft_ctx_buffer_error.argtypes = [ctypes.c_void_p]
    lib.nft_run_cmd_from_buffer.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    ctx = lib.nft_ctx_new(0)
    if not ctx:
        return False
    try:
        lib.nft_ctx_buffer_output(ctx); lib.nft_ctx_buffer_error(ctx)   # quiet: nothing on the service's output
        return lib.nft_run_cmd_from_buffer(ctx, commands.encode()) == 0
    finally:
        lib.nft_ctx_free(ctx)


def guard(address, prefix_length, run=subprocess.run):
    """Load the IPv6 firewall. False when it could not be loaded: then the address is not published."""
    return nft(guard_rules(address, prefix_length), run)


def unguard(run=subprocess.run):
    """Drop the IPv6 firewall (the option turned off); nothing when it is not there."""
    nft(f"table inet {NFT_TABLE}\ndelete table inet {NFT_TABLE}\n", run)


def _openssl(*args):
    return subprocess.run(["openssl", *args], capture_output=True, text=True, timeout=30)


def certificate_for(state, name, days=0):
    """The installed certificate is for name and stays valid `days` more."""
    chain = Path(state) / "tls" / "own" / "fullchain.pem"
    if not chain.exists():
        return False
    host = _openssl("x509", "-noout", "-checkhost", name, "-in", str(chain))
    return "does match" in host.stdout and _openssl(
        "x509", "-noout", "-checkend", str(days * 86400), "-in", str(chain)).returncode == 0


def end_date(state):
    """'2027-01-11' for the installed certificate, or None."""
    out = _openssl("x509", "-noout", "-enddate", "-in", str(Path(state) / "tls" / "own" / "fullchain.pem"))
    if out.returncode:
        return None
    try:
        return time.strftime("%Y-%m-%d", time.strptime(out.stdout.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z"))
    except (IndexError, ValueError):
        return None


def install_certificate(state, name, chain, key):
    """Replace only with a matching, already valid certificate that boxnet can use for at least one more day."""
    pub = lambda *args: _openssl(*args).stdout
    if "does match" not in pub("x509", "-noout", "-checkhost", name, "-in", str(chain)) or (
            pub("x509", "-noout", "-pubkey", "-in", str(chain)) != pub("pkey", "-pubout", "-in", str(key))):
        raise ValueError("certificate does not match")
    validity = _openssl("x509", "-noout", "-dates", "-in", str(chain))
    try:
        dates = dict(line.split("=", 1) for line in validity.stdout.splitlines())
        starts = ssl.cert_time_to_seconds(dates["notBefore"])
        ends = ssl.cert_time_to_seconds(dates["notAfter"])
    except (KeyError, ValueError):
        raise ValueError("invalid certificate dates") from None
    if validity.returncode or not starts <= time.time() < ends - 86400:
        raise ValueError("certificate is not valid for the next day")
    own = Path(state) / "tls" / "own"
    own.mkdir(mode=0o700, parents=True, exist_ok=True)
    key.chmod(0o600)
    key.replace(own / "key.pem")
    chain.replace(own / "fullchain.pem")


def import_certificate(state, name, chain_text, key_text):
    """A certificate brought from another machine (PEM text from the settings page), validated like acme.sh's own
    output. The service must restart to load it (boxnet reads it once at start)."""
    if not name:
        raise ValueError("先设置自己的域名，再导入证书。")
    if not 0 < len(chain_text) <= 65536 or not 0 < len(key_text) <= 16384:
        raise ValueError("证书或私钥为空，或者太长。")
    if "BEGIN CERTIFICATE" not in chain_text or "PRIVATE KEY" not in key_text:
        raise ValueError("需要 PEM 格式的证书链（fullchain.pem）和私钥（key.pem）。")
    with tempfile.TemporaryDirectory(dir=state) as scratch:
        chain, key = Path(scratch) / "fullchain.pem", Path(scratch) / "key.pem"
        chain.write_text(chain_text.strip() + "\n")
        key.write_text(key_text.strip() + "\n")
        try:
            install_certificate(state, name, chain, key)
        except ValueError:
            raise ValueError("证书域名或私钥不匹配，或证书尚未生效、已过期、剩余有效期不足一天。") from None
    write_status(Path(state), name=name, phase="ready", reason="")


def curl_shim(home):
    """A curl in front of acme.sh's: acme.sh 3.1.6 sends its requests without any timeout, so one dead connection
    holds the whole run (and the old 600 s cap made it look stuck). PATH only for the acme.sh process; the bundled
    acme.sh stays byte-identical to its pinned hash. None when curl is not there."""
    real = shutil.which("curl")
    if not real:
        return None
    directory = Path(home) / "bin"
    path = directory / "curl"
    text = ("#!/bin/sh\n"
            f'exec "{real}" --connect-timeout {ACME_CONNECT_TIMEOUT} --max-time {ACME_REQUEST_TIMEOUT} '
            f'--retry {ACME_RETRY} --retry-delay 2 "$@"\n')
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != text:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
            with os.fdopen(fd, "w") as out:
                out.write(text)
            path.chmod(0o700)
    except OSError:
        return None
    return str(directory)


def write_status(state, **fields):
    """Fixed, public fields only (phase, record, address, reason): never an error text, which may carry the token.
    reason is one of the codes from failure_reason()/network_ready(), never a message."""
    path = Path(state) / "own-domain.json"
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = {}
    value = dict(old if isinstance(old, dict) else {}, **fields)
    fd, temporary = tempfile.mkstemp(prefix=".own-domain.", dir=state)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out)
    Path(temporary).replace(path)


def read_status(state):
    try:
        value = json.loads((Path(state) / "own-domain.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Maintainer(threading.Thread):
    """Keeps the DuckDNS record on `address` (None: leave it) and the certificate current, until stopped.
    interface: the Wi-Fi joined on the car hotspot or portable Wi-Fi, whose public IPv6 address the record also
    carries when the owner asked for it (config "ipv6") and the firewall is up; looked at every minute, so a new
    prefix (the hotspot reconnected) is followed."""

    def __init__(self, root, config, address, stopped, say=print, interface=None, tesla=False):
        super().__init__(name="own-domain", daemon=True)
        self.root, self.state = Path(root), Path(root) / "state"
        self.config, self.name, self.address = config, name_of(config), address
        self.local_only = bool(config and config.get("provider") == DESEC)   # deSEC: the box answers the name
        if tesla and address:
            from setup_wizard import tesla_reaches
            if not tesla_reaches(address):
                self.address = None
                write_status(self.state, name=self.name, record="blocked", warning="特斯拉无法打开当前地址，已保留原域名记录")
                say("DuckDNS record kept: Tesla cannot reach the current address")
        self.stopped, self.say, self.interface = stopped, say, interface
        self.sent, self.sent_at = None, 0.0   # (address, ipv6) last accepted by DuckDNS, and when
        self.failures = 0                     # consecutive obtain() failures, for the retry ladder
        self.retry = threading.Event()        # "重新申请证书" from the settings page, or a new uplink

    def request_retry(self):
        """Ask for an attempt now: the waiting loop is cut short and the certificate is looked at again."""
        self.retry.set()

    def ipv6(self):
        """(address or None, state for the settings page): ok, none (no public IPv6), firewall (no nft), off."""
        if not (self.interface and self.config.get("ipv6")):
            return None, "off"
        found = public_ipv6(self.interface)
        if not found:
            return None, "none"
        return (found[0], "ok") if guard(*found) else (None, "firewall")

    def run(self):
        next_record = next_cert = time.monotonic()
        published = read_status(self.state).get("ipv6") or None   # an AAAA left from before must go
        while not self.stopped.is_set():
            now = time.monotonic()
            if self.address and now >= next_record:
                if self.local_only:
                    # deSEC: the box's own dnsmasq answers the name on its hotspot (record "local", read by
                    # upgrade_checks); the public A record ("published") only on a joined car hotspot or MiFi
                    # (interface), where the car asks that network. On the box hotspot nothing is published, so
                    # its 100.96 address never goes public. No AAAA, no IPv6 guard.
                    if not self.interface:
                        public = ""
                    elif self.address == self.sent and now < self.sent_at + RECORD_REFRESH:
                        public = "ok"
                    else:
                        public = update_record(self.config, self.address)
                        if public == "ok":
                            self.sent, self.sent_at = self.address, now
                        self.say(f"desec {self.name} -> {self.address}: {public}")
                    write_status(self.state, name=self.name, record="local", address=self.address,
                                 ipv6="", ipv6_state="off", published=public)
                    next_record = (float("inf") if not public else
                                   now + {"ok": RECORD_REFRESH, "denied": DESEC_DENIED_RETRY,
                                          "invalid": DESEC_DENIED_RETRY}.get(public, RECORD_RETRY))
                else:
                    ipv6, v6state = self.ipv6()
                    if (self.address, ipv6) == self.sent and now < self.sent_at + RECORD_REFRESH:
                        ok = True   # nothing changed since DuckDNS took it
                    else:
                        try:
                            ok = update_record(self.config, self.address, ipv6=ipv6, clear=bool(published and not ipv6))
                        except (OSError, ValueError):   # no internet yet; URLError is an OSError
                            ok = False
                        if ok:
                            self.sent, self.sent_at, published = (self.address, ipv6), now, ipv6
                        self.say(f"duckdns {self.name} -> {self.address}{' ' + ipv6 if ipv6 else ''}: "
                                 f"{'OK' if ok else 'failed'}")
                    write_status(self.state, name=self.name, record="ok" if ok else "failed", address=self.address,
                                 ipv6=published or "", ipv6_state=v6state)
                    next_record = now + (RECORD_RETRY if not ok or v6state != "off" else RECORD_REFRESH)
            if now >= next_cert:
                delay = self.check_certificate()
                next_cert = time.monotonic() + delay   # a slow attempt must not consume its retry interval
            if not self.address:
                next_record = float("inf")
            if self.wait_until(min(next_record, next_cert)):
                next_cert = time.monotonic()   # asked to try again now

    def wait_until(self, deadline):
        """Wait for the deadline; True when a retry was requested. Checks stopped every second so that a stop or a
        request never waits out the 12-hour check interval."""
        while not self.stopped.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            if self.retry.wait(min(left, 1.0)):
                self.retry.clear()
                return True
        return False

    def check_certificate(self):
        """Seconds until the next look."""
        if certificate_for(self.state, self.name, RENEW_DAYS):
            write_status(self.state, name=self.name, phase="ready", reason="")
            return CERT_CHECK
        try:
            missing = network_ready(provider=DESEC) if self.local_only else network_ready()
        except (OSError, ValueError, subprocess.SubprocessError, TypeError):   # never kill the worker here
            missing = "route"
        if missing:   # boot, access point rebuilding, uplink not up yet: not a failure, look again shortly
            write_status(self.state, name=self.name, phase="waiting", reason=missing)
            return READY_RETRY
        renewing = certificate_for(self.state, self.name)
        write_status(self.state, name=self.name, phase="renewing" if renewing else "issuing", reason="")
        log = self.state / "acme" / "acme.log"
        before = log.stat().st_mtime_ns if log.exists() else None
        try:
            self.obtain()
        except InterruptedError:
            return CERT_CHECK
        except BlockingIOError:
            write_status(self.state, name=self.name, phase="waiting", reason="busy")
            return READY_RETRY
        except Exception as error:   # no internet, DuckDNS or Let's Encrypt refused; texts may carry the token
            self.failures += 1
            after = log.stat().st_mtime_ns if log.exists() else None
            reason = failure_reason(error, log if after != before else None)   # this run's log, not the last one
            write_status(self.state, name=self.name, phase="retry", reason=reason)
            delay = retry_delay(self.failures)
            self.say(f"certificate for {self.name} deferred ({reason}), next try in {delay} s")
            return delay
        self.failures = 0
        write_status(self.state, name=self.name, phase="ready", reason="")
        self.say(f"certificate for {self.name} installed until {end_date(self.state)}; used from the next start")
        return CERT_CHECK

    def obtain(self):
        home = self.state / "acme"
        home.mkdir(mode=0o700, exist_ok=True)
        log = home / "acme.log"
        # One worker at a time, also across a supervisor restart while acme.sh is still running.
        fd = os.open(home / "dashplay.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if log.exists():
                log.replace(home / "acme.log.1")
            # 2: acme.sh still has a certificate it considers current; install that one.
            dns = "dns_desec" if self.local_only else "dns_duckdns"
            if self.acme(["--issue", "--dns", dns, "--dnssleep", str(DNS_SLEEP), "-d", self.name,
                          "--keylength", "ec-256"], log, lock.fileno()) not in (0, 2):
                raise RuntimeError("acme.sh could not obtain the certificate")
            with tempfile.TemporaryDirectory(dir=home) as scratch:
                chain, key = Path(scratch) / "fullchain.pem", Path(scratch) / "key.pem"
                if self.acme(["--install-cert", "-d", self.name, "--ecc", "--fullchain-file", str(chain),
                              "--key-file", str(key)], log, lock.fileno()) != 0:
                    raise RuntimeError("acme.sh could not export the certificate")
                self.install(chain, key)

    def install(self, chain, key):
        install_certificate(self.state, self.name, chain, key)

    def acme(self, arguments, log, lock_fd=None):
        """acme.sh in its own process group, cancelled when the service stops; the token only in its environment."""
        home = self.state / "acme"
        command = [str(self.root / "acme" / "acme.sh"), "--home", str(self.root / "acme"), "--config-home", str(home),
                   "--cert-home", str(home / "certs"), *arguments, "--server", ACME_SERVER, "--no-color"]
        env = dict(os.environ, **({"DEDYN_TOKEN": self.config["token"]} if self.local_only
                                  else {"DuckDNS_Token": self.config["token"]}))
        shim = curl_shim(home)
        if shim:   # acme.sh asks for "curl"; its requests get bounded by the shim, the script stays untouched
            env["PATH"] = shim + os.pathsep + env.get("PATH", "")
        if 4 in default_route_families():
            # Where there is IPv4, don't let a broken IPv6 path to the CA hold or reset the request (2026-10-26).
            env.setdefault("ACME_USE_IPV4_REQUESTS", "1")
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "ab") as out, subprocess.Popen(
                command, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                start_new_session=True, pass_fds=(() if lock_fd is None else (lock_fd,))) as process:
            deadline = time.monotonic() + ACME_TIMEOUT
            try:
                while True:
                    if self.stopped.is_set():
                        raise InterruptedError("service stopping")
                    if time.monotonic() > deadline:
                        raise TimeoutError("acme.sh took too long")
                    try:
                        return process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        pass
            finally:
                # Reap descendants even when the shell itself already exited. The inherited lock also
                # survives an abrupt supervisor death until its signer has finished.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()


def message(state, name, box_hotspot=False, retry=False):
    """The settings page's line about the name and its certificate. retry: the page also offers a retry button."""
    if not name:
        return ""
    status = read_status(state)
    if status.get("name") != name:
        status = {}
    until = end_date(state) if certificate_for(state, name) else None
    if until:
        text = f"正式证书有效期至 {until}。"
        if not serving(state):
            text += "新证书在下次开机时启用。"
        if box_hotspot and not certificate_for(state, name, RENEW_DAYS):
            text += "车上没有网络无法续签：请回家插上网线开机一次，盒子会自动续签。"
    elif status.get("phase") == "retry":
        detail = reason_text(status.get("reason")) if status.get("reason") else "请确认盒子能上网、子域名和 token 正确"
        text = f"正式证书暂未申请到：{detail}。盒子会自动重试（1 分钟内开始，逐步放慢）。"
        if retry:
            text += "也可以点下面的「重新申请证书」立即再试一次。"
    elif status.get("phase") == "waiting":
        detail = reason_text(status.get("reason")) if status.get("reason") else "等待盒子联网"
        text = f"等待盒子联网：{detail}；联网后自动申请正式证书。"
    elif status.get("phase") == "issuing":
        text = "正在申请正式证书，通常几分钟；完成后下次开机启用。"
    else:
        text = "等待申请正式证书：盒子联网后自动开始。"
    if not until and box_hotspot:
        text += "盒子热点方式在车上没有网络：请在家插上网线开机申请。"
    if status.get("record") == "blocked":
        text += status.get("warning", "已保留原域名记录。")
    if status.get("record") == "failed":
        text += f"更新 {name} 的解析失败，盒子会继续重试。"
    # deSEC's public A record (Maintainer); on the box hotspot the box answers the name itself, so it does not matter.
    if not box_hotspot and status.get("published") == "denied":
        text += ("这个域名的钥匙还不能在网上登记盒子的地址，连车机热点或随身 WiFi 时用域名打不开："
                 "请先用设置页显示的 IP 地址，并联系设备提供方开通。")
    elif not box_hotspot and status.get("published") == "failed":
        text += "盒子的地址还没登记到网上，盒子会继续重试；登记前请用设置页显示的 IP 地址。"
    elif not box_hotspot and status.get("published") == "invalid":
        text += ("服务商不接受这个域名的登记（域名的分段或缓存时间不对），重试也修不好，盒子隔几小时再看一次："
                 "连车机热点或随身 WiFi 时先用设置页显示的 IP 地址，并联系设备提供方处理。")
    text += {"ok": f"公网 IPv6 {status.get('ipv6')} 已登记，防火墙只让同一个 WiFi 里的设备连进盒子。",
             "none": "连着的热点没有给盒子分配公网 IPv6，暂时只登记了局域网地址。",
             "firewall": "盒子的防火墙没能启用，为安全起见没有登记公网 IPv6；请把诊断记录交给 AI 助手或开发者。",
             }.get(status.get("ipv6_state"), "")
    return text


def serving(state):
    """The web server's keystore holds the installed certificate of one's own (boxnet's marker: its hash)."""
    try:
        marker = json.loads((Path(state) / "tls" / "leaf.san.json").read_text())
        chain = (Path(state) / "tls" / "own" / "fullchain.pem").read_bytes()
        return marker.get("own") == hashlib.sha256(chain).hexdigest()[:16]
    except (OSError, ValueError, TypeError, AttributeError):
        return False
