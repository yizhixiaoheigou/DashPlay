"""Drive diagnostics that survive a power cut: one JSON line every 10 s in state/diag/, and the
full logs of each boot in state/logs/ (LogMirror).

In the car the box is simply switched off, and Armbian keeps /var/log in RAM, so the journal of a
drive was lost (2026-10-11: the boots of a laggy test drive kept 3 s each). This log is written
straight to the eMMC and fsynced. Each line holds the box side (Wi-Fi link, USB dongle resets, load,
temperature, memory) and the noteworthy receiver log lines since the previous line, including the
viewer's own playback numbers ("viewer stats"). Two files of at most MAX_BYTES each are kept.
"""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import threading
import time

import update

MAX_BYTES = 1_000_000
INTERVAL = 10
MAX_LINES = 40
KEEP = re.compile(r"viewer stats|Receive: (video|audio)|session (active|ended)|display changed|wireless bootstrap"
                  r"|accepted by the iPhone|CarPlay is live|rejecting|WARN|ERROR|^boot ")


class DiagLog:
    def __init__(self, directory: Path, max_bytes=MAX_BYTES):
        self.directory, self.max_bytes = directory, max_bytes
        self.current, self.previous = directory / "diag.jsonl", directory / "diag.1.jsonl"

    def append(self, record):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.current.exists() and self.current.stat().st_size >= self.max_bytes:
            self.current.replace(self.previous)
        fd = os.open(self.current, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    def text(self):
        parts = [path.read_text(errors="replace") for path in (self.previous, self.current) if path.exists()]
        return "".join(parts)


class LogTail:
    """New lines of a log that may be truncated or recreated (each receiver start rewrites it)."""

    def __init__(self, path: Path):
        self.path, self.offset, self.inode = path, 0, None

    def read(self):
        try:
            stat = self.path.stat()
        except OSError:
            return []
        if stat.st_ino != self.inode or stat.st_size < self.offset:
            self.inode, self.offset = stat.st_ino, 0
        with self.path.open("rb") as log:
            log.seek(self.offset)
            data = log.read(2_000_000)
        end = data.rfind(b"\n") + 1   # keep a partial last line for the next read
        self.offset += end
        return data[:end].decode(errors="replace").splitlines()


LOG_BOOTS = 8                 # boots whose full logs are kept
LOG_FILE_MAX = 40_000_000     # per mirrored file and boot
MIRROR_INTERVAL = 5


class LogMirror:
    """Full logs that survive a power cut: every /run/dashplay/*.log and this boot's journal (kernel,
    hostapd, wpa_supplicant, bluetoothd, the supervisor) appended to root/<boot>/ and fsynced every few
    seconds. Offsets and the journal cursor are kept beside them, so a supervisor restart within the
    same boot neither repeats nor skips lines (2026-10-12: every drive's journal was lost on power-off).
    """

    def __init__(self, root: Path, run: Path, boot_id: str, journal=True):
        """journal: also this boot's systemd journal (OpenWrt has none: only run/*.log are kept)."""
        self.root, self.run, self.journal = root, run, journal
        self.directory = root / boot_id[:8]
        self.state_file = self.directory / ".offsets.json"
        self.cursor = self.directory / ".journal-cursor"

    def prune(self):
        boots = sorted((d for d in self.root.iterdir() if d.is_dir() and d != self.directory),
                       key=lambda d: d.stat().st_mtime)
        for old in boots[:max(0, len(boots) - (LOG_BOOTS - 1))]:
            shutil.rmtree(old, ignore_errors=True)

    def load(self):
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def append(self, name, data, handles):
        target = self.directory / name
        try:
            size = target.stat().st_size
        except OSError:
            size = 0
        if size >= LOG_FILE_MAX:
            return
        if size + len(data) > LOG_FILE_MAX:
            data = data[:LOG_FILE_MAX - size] + b"\n[log mirror: size limit reached]\n"
        fd = handles.get(name)
        if fd is None:
            fd = handles[name] = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.write(fd, data)

    def cycle(self, offsets, handles):
        for path in sorted(self.run.glob("*.log")):
            key = path.name
            try:
                stat = path.stat()
            except OSError:
                continue
            inode, offset = offsets.get(key, (None, 0))
            if inode != stat.st_ino or stat.st_size < offset:
                if inode is not None:
                    self.append(key, f"[log mirror: {key} restarted]\n".encode(), handles)
                offset = 0
            if stat.st_size > offset:
                with path.open("rb") as log:
                    log.seek(offset)
                    data = log.read(4_000_000)
                end = data.rfind(b"\n") + 1
                if end:
                    self.append(key, data[:end], handles)
                    offset += end
            offsets[key] = (stat.st_ino, offset)
        journal = self.journal and subprocess.run(["journalctl", "-b", "-o", "short-monotonic", "--no-pager", "-q",
                                                   f"--cursor-file={self.cursor}"], capture_output=True, timeout=20).stdout
        if journal:
            self.append("journal.log", journal, handles)
        for fd in handles.values():
            os.fsync(fd)
        self.state_file.write_text(json.dumps(offsets))

    def start(self, stopped, interval=MIRROR_INTERVAL):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.utime(self.directory)
        self.prune()
        offsets = {key: tuple(value) for key, value in self.load().items()}
        handles = {}

        def loop():
            while True:
                try:
                    self.cycle(offsets, handles)
                except Exception as error:   # logging must never take the service down
                    print(f"log mirror skipped: {type(error).__name__}", flush=True)
                if stopped.wait(interval):
                    break
            try:
                self.cycle(offsets, handles)   # what the shutdown itself logged
            except Exception:
                pass

        threading.Thread(target=loop, name="log-mirror", daemon=True).start()


def net_counters():
    """TCP retransmissions and UDP receive drops, cumulative since boot (/proc/net/snmp)."""
    try:
        lines = Path("/proc/net/snmp").read_text().splitlines()
    except OSError:
        return {}
    values = {}
    for header, row in zip(lines[::2], lines[1::2]):
        names, numbers = header.split()[1:], row.split()[1:]
        values[header.split(":")[0]] = dict(zip(names, numbers))
    tcp, udp = values.get("Tcp", {}), values.get("Udp", {})
    return {key: int(value) for key, value in (("tcp_out", tcp.get("OutSegs")), ("tcp_retrans", tcp.get("RetransSegs")),
            ("udp_in", udp.get("InDatagrams")), ("udp_rcvbuf_err", udp.get("RcvbufErrors"))) if value is not None}


def ping_stats(address, count=5):
    """A short burst: {min, avg, max, loss %}. Gateway versus phone shows whether the car hotspot's
    forwarding between its clients (or the phone's power save) adds the delay, or the radio link itself."""
    try:
        out = subprocess.run(["ping", "-c", str(count), "-i", "0.1", "-W", "1", "-q", address],
                             capture_output=True, text=True, timeout=count + 3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    loss = re.search(r"([\d.]+)% packet loss", out)
    times = re.search(r"= ([\d.]+)/([\d.]+)/([\d.]+)/", out)
    result = {"loss": float(loss.group(1))} if loss else {}
    if times:
        result.update(min=float(times.group(1)), avg=float(times.group(2)), max=float(times.group(3)))
    return result or None


def tcp_stats(address):
    """The receiver's TCP connections from the phone (AirPlay control, video, iAP2 tunnel), from
    `ss -tin`: smoothed RTT, retransmissions and bytes received per connection."""
    try:
        out = subprocess.run(["ss", "-tinH", "dst", address], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    connections = []
    for header, info in zip(out.splitlines()[::2], out.splitlines()[1::2]):
        fields = header.split()
        entry = {"local": fields[3].rsplit(":", 1)[-1] if len(fields) > 3 else "?"}
        for key, pattern in (("rtt", r"\brtt:([\d.]+)"), ("rcv_rtt", r"rcv_rtt:([\d.]+)"),
                             ("retrans", r"retrans:\d+/(\d+)"), ("rx_bytes", r"bytes_received:(\d+)"),
                             ("lost", r"\blost:(\d+)")):
            match = re.search(pattern, info)
            if match:
                entry[key] = float(match.group(1))
        connections.append(entry)
    return connections


def local_networks():
    import ipaddress
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ipaddress.ip_interface(line.split()[3]).network for line in out.splitlines()
            if len(line.split()) > 3 and " lo " not in line]


def ping_targets(state: Path, networks=None):
    """The default gateway (the car hotspot in station mode) and the phone's address on a network the
    box is on now: the phones remember several (box hotspot, car hotspot), and the first test drive
    (2026-10-12) pinged the box-hotspot address while the iPhone was on the car hotspot."""
    import ipaddress
    networks = local_networks() if networks is None else networks
    targets = {}
    try:
        route = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=3).stdout
        match = re.search(r"via (\S+)", route)
        if match:
            targets["gateway"] = match.group(1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        presence = json.loads((state / "receiver/phone-presence.json").read_text())
        for phone in presence.get("phones", {}).values():
            for network in phone.get("networks", {}).values():
                address = network.get("ip")
                if address and any(ipaddress.ip_address(address) in net for net in networks):
                    targets.setdefault("phone", address)
    except (OSError, ValueError, AttributeError):
        pass
    return targets


GAP_BUCKETS_MS = (10, 50, 150, 500)   # <10, 10-50, 50-150, 150-500, >=500 ms


def phone_traffic(interface, address, seconds=4.0):
    """What arrives from the phone in a few seconds, when the car hotspot relays it: packets, kbit/s,
    repeated TCP segments (the phone retransmitting: the hotspot lost them or our ACKs) and the gaps
    between packets. Arrivals bunched every ~100 ms point at power-save buffering (the phone's or the
    hotspot's), steady but few at a rate limit, many repeats at losses."""
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0800))
        sock.bind((interface, 0))
    except OSError:
        return None
    sock.settimeout(0.2)
    source = socket.inet_aton(address)
    arrivals, total, repeats, seen = [], 0, 0, set()
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            try:
                frame = sock.recv(4096)
            except socket.timeout:
                continue
            if len(frame) < 34 or frame[26:30] != source:
                continue
            arrivals.append(time.monotonic())
            total += len(frame)
            header = (frame[14] & 0x0F) * 4
            if frame[23] == 6 and len(frame) >= 14 + header + 20:
                tcp = frame[14 + header:]
                payload = struct.unpack("!H", frame[16:18])[0] - header - (tcp[12] >> 4) * 4
                if payload > 0:
                    key = (tcp[:4], tcp[4:8])
                    repeats += key in seen
                    seen.add(key)
    finally:
        sock.close()
    gaps = [round((b - a) * 1000) for a, b in zip(arrivals, arrivals[1:])]
    buckets = [0] * (len(GAP_BUCKETS_MS) + 1)
    for gap in gaps:
        buckets[sum(gap >= limit for limit in GAP_BUCKETS_MS)] += 1
    return {"pkts": len(arrivals), "kbps": round(total * 8 / seconds / 1000), "tcp_repeats": repeats,
            "max_gap_ms": max(gaps, default=None), "gaps": buckets}


def keep(lines, limit=MAX_LINES):
    kept = [line.strip()[:300] for line in lines if KEEP.search(line)]
    return kept[-limit:]


def start(stopped, log: DiagLog, receiver_log: Path, platform, status, interval=INTERVAL, state: Path = None,
          program: Path = Path(__file__).resolve().parent):
    tail = LogTail(receiver_log)
    # The release this box runs opens every start, so a user's diagnostics say it (2026-10-20: a box still on the
    # image's 2026.10.15 program was only told apart by the viewer's build stamp).
    log.append({"event": "start", "wall": round(time.time()), "uptime": round(time.monotonic(), 1),
                "boot": platform.boot_id()[:8], "version": update.bundled_version(program)})

    def loop():
        while not stopped.wait(interval):
            try:
                record = {"uptime": round(time.monotonic(), 1), "mode": status.get("mode")}
                record.update({key: status[key] for key in ("uplink", "tesla_offline", "tesla_internet",
                               "tesla_dns_log", "direct_phone", "phone_interface", "tesla_lan_stable",
                               "tesla_upstream", "uplink_link", "uplink_error") if key in status})
                record.update(platform.diagnostics())
                record["net"] = net_counters()
                if state is not None:
                    # Car-hotspot round trips explain lag when the iPhone and the box share it.
                    targets = ping_targets(state)
                    record["ping"] = {name: ping_stats(address) for name, address in targets.items()}
                    if targets.get("phone"):
                        record["tcp"] = tcp_stats(targets["phone"])
                        # mifi: a wireless iPhone's video is relayed by the portable Wi-Fi.
                        relayed = status.get("mode") == "sta"
                        phone_interface = status.get('phone_interface') or getattr(platform, 'interface', None)
                        if relayed and int(time.monotonic()) % 30 < interval and phone_interface:
                            record["phone_rx"] = phone_traffic(phone_interface, targets["phone"])
                lines = keep(tail.read())
                if lines:
                    record["log"] = lines
                log.append(record)
            except Exception as error:   # diagnostics must never take the service down
                print(f"diagnostics skipped: {type(error).__name__}", flush=True)

    threading.Thread(target=loop, name="diagnostics", daemon=True).start()
