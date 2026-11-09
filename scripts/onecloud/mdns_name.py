"""Answer mDNS A queries for one name (dashplay.local), so phones find the box after a network change.

The box has no avahi-daemon. The receiver's jmdns only advertises the AirPlay service, so this
answers the host name itself, with the address of whichever local subnet the query came from.
"""
import ipaddress
import socket
import struct
import time
from platform_api import get_platform
import threading

GROUP, PORT = "224.0.0.251", 5353
TTL = 120


def local_addresses():
    return get_platform().local_addresses()


def encode_name(name):
    return b"".join(bytes([len(label)]) + label.encode() for label in name.split(".")) + b"\0"


def read_name(packet, offset):
    labels = []
    while True:
        length = packet[offset]
        if length & 0xC0:   # compression pointer; questions rarely use it, but follow it safely
            pointer = struct.unpack("!H", packet[offset:offset + 2])[0] & 0x3FFF
            if pointer >= offset:
                raise ValueError("forward pointer")
            labels.append(read_name(packet, pointer)[0])
            return ".".join(labels), offset + 2
        offset += 1
        if length == 0:
            return ".".join(labels), offset
        labels.append(packet[offset:offset + length].decode("utf-8", "replace"))
        offset += length


def answer(packet, name, address, legacy):
    """Response bytes for a query packet, or None when it does not ask for our A record."""
    if len(packet) < 12:
        return None
    query_id, flags, questions = struct.unpack("!HHH", packet[:6])
    if flags & 0x8000:   # a response, not a query
        return None
    offset, wanted = 12, False
    for _ in range(questions):
        qname, offset = read_name(packet, offset)
        qtype, _qclass = struct.unpack("!HH", packet[offset:offset + 4])
        offset += 4
        if qname.lower().rstrip(".") == name and qtype in (1, 255):
            wanted = True
    if not wanted:
        return None
    # Legacy unicast queries (source port != 5353) need the id echoed and no cache-flush bit.
    header = struct.pack("!HHHHHH", query_id if legacy else 0, 0x8400, 0, 1, 0, 0)
    record = encode_name(name) + struct.pack("!HHIH", 1, 0x0001 if legacy else 0x8001, TTL, 4)
    return header + record + socket.inet_aton(str(address))


class NameResponder:
    def __init__(self, name, stopped):
        self.name, self.stopped = name.lower(), stopped

    def start(self):
        threading.Thread(target=self.run, name="mdns-name", daemon=True).start()

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", PORT))
        joined = set()
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(2)
        addresses, addresses_at = [], 0.0
        while not self.stopped.is_set():
            # The loop wakes every 2 s on the recv timeout; an `ip -4 -o addr` per wake was a fork
            # twice a second for ever. Membership joins and answers do not need 2 s fresh addresses.
            if time.monotonic() - addresses_at >= 30:
                addresses, addresses_at = local_addresses(), time.monotonic()
            for local in addresses:
                if local.ip not in joined:
                    try:
                        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                        socket.inet_aton(GROUP) + socket.inet_aton(str(local.ip)))
                        joined.add(local.ip)
                    except OSError:
                        pass
            try:
                packet, (source, port) = sock.recvfrom(9000)
            except socket.timeout:
                continue
            try:
                local = next((a for a in addresses if ipaddress.ip_address(source) in a.network), None)
                if local is None:
                    continue
                legacy = port != PORT
                response = answer(packet, self.name, local.ip, legacy)
                if response:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(str(local.ip)))
                    sock.sendto(response, (source, port) if legacy else (GROUP, PORT))
            except (ValueError, IndexError, struct.error, OSError):
                continue
