"""Offline domain compatibility and local DNS/HTTPS checks for program upgrades.

Run from the candidate program before installation, then from the installed program after restart.
No provider API or certificate issuance is triggered; credentials never leave the state directory.
"""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import struct

import own_domain
import setup_wizard
import viewer_port


def inspect(root):
    state = Path(root) / "state"
    path = state / "ddns.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    config = own_domain.load(state)
    if not config or config.get("provider", "duckdns") != str(raw.get("provider") or "duckdns").lower():
        raise ValueError("新程序无法读取设备已有的域名配置")
    name = own_domain.name_of(config)
    expected = raw["domain"].strip().lower().rstrip(".")
    if config.get("provider") not in ("desec", "dnspod"):
        expected = expected.removesuffix(".duckdns.org") + ".duckdns.org"
    if name != expected:
        raise ValueError("新程序解释出的域名与原配置不一致")
    hook = {"desec": "dns_desec.sh", "dnspod": "dns_dp.sh"}.get(config.get("provider"), "dns_duckdns.sh")
    if not (Path(__file__).parent / "acme/dnsapi" / hook).is_file():
        raise ValueError("升级包缺少设备所需的 DNS 验证脚本")
    return config


def _skip_name(packet, offset):
    while True:
        size = packet[offset]
        offset += 1
        if size & 0xc0 == 0xc0:
            return offset + 1
        if size == 0:
            return offset
        if size > 63:
            raise ValueError("invalid DNS name")
        offset += size


def dns_answers(name, server, port=53):
    transaction = os.urandom(2)
    query = transaction + struct.pack("!5H", 0x0100, 1, 0, 0, 0)
    query += b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split("."))
    query += b"\0" + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(2)
        client.connect((server, port))
        client.send(query)
        packet = client.recv(4096)
    if packet[:2] != transaction or len(packet) < 12:
        raise ValueError("invalid DNS response")
    flags, questions, answers = struct.unpack_from("!HHH", packet, 2)
    if not flags & 0x8000 or flags & 0x020f or questions != 1:
        raise ValueError("DNS query failed")
    offset = _skip_name(packet, 12) + 4
    found = set()
    for _ in range(answers):
        offset = _skip_name(packet, offset)
        kind, family, _, size = struct.unpack_from("!HHIH", packet, offset)
        offset += 10
        if kind == 1 and family == 1 and size == 4:
            found.add(socket.inet_ntoa(packet[offset:offset + size]))
        offset += size
    return found


def https_ready(state, name):
    chain = Path(state) / "tls/own/fullchain.pem"
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(chain))
    # Check the certificate actually loaded by the receiver, including hostname and validity.
    installed = ssl.PEM_cert_to_DER_cert(chain.read_text().split("-----END CERTIFICATE-----")[0]
                                       + "-----END CERTIFICATE-----\n")
    # PlayPort binds its Wi-Fi address; it does not listen on loopback on a real box.
    address = str(ipaddress.ip_address(own_domain.read_status(state).get("address") or "127.0.0.1"))
    with socket.create_connection((address, viewer_port.resolve(state)), timeout=3) as connection:
        with context.wrap_socket(connection, server_hostname=name) as tls:
            if tls.getpeercert(binary_form=True) != installed:
                raise ValueError("接收端尚未加载设备证书")
            tls.sendall(f"GET / HTTP/1.0\r\nHost: {name}\r\nConnection: close\r\n\r\n".encode("ascii"))
            with tls.makefile("rb") as response:
                reply = response.readline(1024).split()
            if len(reply) < 2 or reply[1] != b"200":
                raise ValueError("域名 HTTPS 页面未正常响应")


def check(root, health=False, require_https=False):
    config = inspect(root)
    if config is None:
        if require_https:
            raise ValueError("升级后设备的域名配置丢失")
        return {"https": False}
    state = Path(root) / "state"
    name = own_domain.name_of(config)
    certificate = own_domain.certificate_for(state, name)
    if health:
        if require_https and not certificate:
            raise ValueError("升级后设备的有效域名证书不可用")
        setup = setup_wizard.load(state)
        if config.get("provider") == "desec" and setup.get("mode") == "box_hotspot":
            settings = json.loads((Path(root) / "service.json").read_text())
            address, _ = setup_wizard.box_hotspot_address(
                ipaddress.IPv4Interface(settings["address"]), setup_wizard.options(state).get("tesla"), [])
            host = str(address.ip)
            status = own_domain.read_status(state)
            if status.get("name") == name and status.get("record") == "local":
                host = str(ipaddress.IPv4Address(status.get("address", host)))
            if host not in dns_answers(name, host):
                raise ValueError("盒子热点未正确解析设备域名")
        if require_https:
            https_ready(state, name)
    identity = Path(root) / "identity/offline-mfi"
    receiver_enabled = all((identity / name).is_file() and (identity / name).stat().st_size
                           for name in ("identity.pk8", "certificate.p7b"))
    # A box still awaiting its identity legitimately runs only the settings page.
    return {"https": bool(certificate and receiver_enabled)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--require-https", action="store_true")
    args = parser.parse_args()
    try:
        result = check(args.root, args.health, args.require_https)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, struct.error):
        # Deliberately omit exception values: malformed configuration can contain a token.
        raise SystemExit("设备域名、验证脚本、本地 DNS 或 HTTPS 检查失败")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
