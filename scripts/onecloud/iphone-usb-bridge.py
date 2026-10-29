#!/usr/bin/env python3
"""USB side of wired CarPlay (the receiver's --usb-bridge; WiredBootstrap in the receiver).

Waits for an iPhone on a USB port (the OTG port away from the network jack is put in host role),
switches it to its CarPlay configuration as the Android WheelPlay/LIVI code does (vendor request
IN 0xc0/0x52/0/4, then configuration 6: USBMUX ff/fe/02 + NCM 02/0d), brings the NCM interface up
(relayed here to a TAP interface, see below; CarPlay uses fe80::2 on it), reports "usb waiting" (no
iPhone yet), "usb iphone <serial>" (one plugged in; the receiver then pauses wireless invitations) and
"ncm <ifname> <number> <mac>" on stderr, claims the USBMUX interface and relays its bulk transfers over stdio as frames of
u32 big-endian length + bytes. Exits when the iPhone goes away; the receiver starts it again.
Verified step by step on the box with wired-host-probe.py (2026-10-12).
"""
import ctypes
import errno
import fcntl
import glob
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

ROLE = Path("/sys/class/usb_role/c9040000.usb-role-switch/role")
CARPLAY_CONFIGURATION = 6
READ_CHUNK = 16384
NCM_READ_CHUNK = 65536  # NTB16; iPhone sends ~32 KB transfers (drive F9).


def log(text):
    print(text, file=sys.stderr, flush=True)


def ioc(direction, number, size):
    return (direction << 30) | (size << 16) | (ord("U") << 8) | number


# struct usbdevfs_ctrltransfer / usbdevfs_bulktransfer end in a user pointer, so they are packed in the
# CPU's native layout: 16 bytes on the OneCloud's armv7, 24 on the N1's aarch64 (2026-10-12).
CONTROL_STRUCT = "@BBHHHIP"
BULK_STRUCT = "@IIIP"
USBDEVFS_CONTROL = ioc(3, 0, struct.calcsize(CONTROL_STRUCT))
USBDEVFS_BULK = ioc(3, 2, struct.calcsize(BULK_STRUCT))
USBDEVFS_SETINTERFACE = ioc(2, 4, 8)
USBDEVFS_CLAIMINTERFACE = ioc(2, 15, 4)
TUNSETIFF = 0x400454CA
IFF_TAP, IFF_NO_PI = 0x0002, 0x1000
TAP_NAME = "dpncm"
LINK_LOCAL = "fe80::2/64"


def apple_device():
    for device in sorted(glob.glob("/sys/bus/usb/devices/[0-9]-[0-9]*")):
        if ":" in Path(device).name:
            continue
        path = Path(device)
        try:
            if path.joinpath("idVendor").read_text().strip() != "05ac":
                continue
            name = path.joinpath("product").read_text().strip().lower()
        except OSError:
            continue
        if name.startswith("iphone"):
            return path
    return None


def wait_device(timeout=None):
    end = None if timeout is None else time.monotonic() + timeout
    while end is None or time.monotonic() < end:
        device = apple_device()
        if device is not None:
            return device
        time.sleep(0.3)
    return None


def node(device):
    return f"/dev/bus/usb/{int(Path(device, 'busnum').read_text()):03d}/{int(Path(device, 'devnum').read_text()):03d}"


def control_in(fd, request_type, request, value, index, length):
    buffer = ctypes.create_string_buffer(length)
    setup = struct.pack(CONTROL_STRUCT, request_type, request, value, index, length, 1000, ctypes.addressof(buffer))
    fcntl.ioctl(fd, USBDEVFS_CONTROL, setup)
    return buffer.raw


def bulk(fd, endpoint, data=None, length=READ_CHUNK, timeout=1000):
    buffer = ctypes.create_string_buffer(data if data is not None else length, len(data) if data is not None else length)
    request = struct.pack(BULK_STRUCT, endpoint, len(buffer), timeout, ctypes.addressof(buffer))
    count = fcntl.ioctl(fd, USBDEVFS_BULK, bytearray(request))
    return buffer.raw[:count] if data is None else count


def interface_values(path):
    return {name: (path / name).read_text().strip() for name in
            ("bInterfaceNumber", "bInterfaceClass", "bInterfaceSubClass", "bInterfaceProtocol")}


def carplay_configuration(device):
    """Vendor request 0x52 adds the CarPlay configurations; then select configuration 6."""
    if int(Path(device, "bNumConfigurations").read_text()) < CARPLAY_CONFIGURATION:
        fd = os.open(node(device), os.O_RDWR)
        try:
            log(f"vendor request 0x52 answered {control_in(fd, 0xC0, 0x52, 0, 4, 1).hex()}")
        except OSError as error:
            log(f"vendor request 0x52: {error.strerror}")
        finally:
            os.close(fd)
        end = time.monotonic() + 8
        while time.monotonic() < end:
            time.sleep(0.1)
            device = apple_device()
            if device is not None and int(Path(device, "bNumConfigurations").read_text()) >= CARPLAY_CONFIGURATION:
                break
        else:
            raise RuntimeError("the iPhone did not offer its CarPlay configuration")
    if Path(device, "bConfigurationValue").read_text().strip() != str(CARPLAY_CONFIGURATION):
        Path(device, "bConfigurationValue").write_text(f"{CARPLAY_CONFIGURATION}\n")
        # Go on as soon as the configuration's interfaces exist (was a fixed 1.5 s); a kernel driver
        # still binding to one is handled when claiming it (claim()).
        end = time.monotonic() + 3
        while time.monotonic() < end and not list(device.parent.glob(f"{device.name}:{CARPLAY_CONFIGURATION}.*")):
            time.sleep(0.05)
    return device


def claim(fd, device, number):
    """Claim an interface; a kernel driver (cdc_ncm, ipheth) that bound after we looked is unbound first."""
    for attempt in range(20):
        try:
            fcntl.ioctl(fd, USBDEVFS_CLAIMINTERFACE, struct.pack("I", number))
            return
        except OSError as error:
            if error.errno != errno.EBUSY or attempt == 19:
                raise
        path = Path(device.parent, f"{device.name}:{CARPLAY_CONFIGURATION}.{number}")
        if (path / "driver").exists():
            ((path / "driver").resolve() / "unbind").write_text(path.name)
        time.sleep(0.05)


def release_tethering(device):
    """The CarPlay configuration also carries the iPhone's USB Personal Hotspot interface (ff/fd,
    kernel ipheth). Left bound, systemd-networkd took a DHCP lease and a default route through the
    iPhone's mobile data and the iPhone showed a hotspot client (2026-10-12). CarPlay needs only NCM."""
    for _ in range(10):
        for path in device.parent.glob(f"{device.name}:{CARPLAY_CONFIGURATION}.*"):
            driver = path / "driver"
            if interface_values(path)["bInterfaceSubClass"] == "fd" and driver.exists() and driver.resolve().name == "ipheth":
                (driver.resolve() / "unbind").write_text(path.name)
                log(f"released the iPhone's USB hotspot interface {path.name}")
                return
        time.sleep(0.2)


def ncm_functions(device):
    """CDC NCM functions of the CarPlay configuration from the raw descriptors: (control interface,
    data interface, iMACAddress string index, data bulk IN, data bulk OUT, status interrupt IN or None)."""
    raw = Path(device, "descriptors").read_bytes()
    offset, config, interface, found, current = 0, None, None, [], None
    while offset + 2 <= len(raw) and raw[offset]:
        length, kind = raw[offset], raw[offset + 1]
        d = raw[offset:offset + length]
        if kind == 2:
            config = d[5]
        elif config == CARPLAY_CONFIGURATION and kind == 4:
            interface = (d[2], d[3], d[5], d[6])
            if (d[5], d[6]) == (0x02, 0x0D):
                current = {"control": d[2], "status": None}
        elif config == CARPLAY_CONFIGURATION and kind == 0x24 and current is not None:
            if d[2] == 0x06:
                current["data"] = d[4]
            elif d[2] == 0x0F:
                current["mac"] = d[3]
        elif config == CARPLAY_CONFIGURATION and kind == 5 and current and interface[0] == current["control"]:
            if d[2] & 0x80 and d[3] & 3 == 3:
                current["status"] = d[2]
        elif config == CARPLAY_CONFIGURATION and kind == 5 and current and interface[0] == current.get("data") and interface[1] == 1:
            current["in" if d[2] & 0x80 else "out"] = d[2]
            if "in" in current and "out" in current:
                found.append((current["control"], current["data"], current["mac"], current["in"], current["out"],
                              current["status"]))
                current = None
        offset += length
    return found


def ntb_datagrams(block):
    """Ethernet frames in one NCM transfer block (NTB16: NTH16, then a chain of NDP16 tables)."""
    if len(block) < 12 or block[:4] != b"NCMH":
        return
    length = min(struct.unpack_from("<H", block, 8)[0] or len(block), len(block))
    ndp = struct.unpack_from("<H", block, 10)[0]
    seen = 0
    while 12 <= ndp < length - 8 and seen < 16:
        seen += 1
        if block[ndp:ndp + 3] != b"NCM":
            return
        table_length, next_ndp = struct.unpack_from("<HH", block, ndp + 4)
        for entry in range(ndp + 8, min(ndp + table_length, length) - 3, 4):
            index, size = struct.unpack_from("<HH", block, entry)
            if index == 0 or size == 0:
                break
            if index + size <= length:
                yield block[index:index + size]
        ndp = next_ndp


def ntb_block(frame, sequence):
    """One frame in its own NTB16: NTH16 at 0, NDP16 at 12, the frame 8-byte aligned at 32."""
    total = 32 + len(frame)
    padding = 1 if total % 512 == 0 else 0   # no transfer may end on a full packet without a ZLP
    total += padding
    header = struct.pack("<4sHHHH", b"NCMH", 12, sequence & 0xFFFF, total, 12)
    table = struct.pack("<4sHHHHHH", b"NCM0", 16, 0, 32, len(frame), 0, 0)
    return header + table + b"\0" * 4 + frame + b"\0" * padding


def open_tap(mac):
    fd = os.open("/dev/net/tun", os.O_RDWR)
    fcntl.ioctl(fd, TUNSETIFF, struct.pack("16sH", TAP_NAME.encode(), IFF_TAP | IFF_NO_PI))
    subprocess.run(["sysctl", "-qw", f"net.ipv6.conf.{TAP_NAME}.disable_ipv6=0"], capture_output=True)
    subprocess.run(["ip", "link", "set", TAP_NAME, "address", mac, "up"], check=True)
    subprocess.run(["ip", "-6", "addr", "replace", LINK_LOCAL, "dev", TAP_NAME, "nodad"], check=True)
    return fd


def main():
    if ROLE.exists() and ROLE.read_text().strip() != "host":
        ROLE.write_text("none\n")
        time.sleep(1)
        ROLE.write_text("host\n")
        log("OTG port switched to USB host")
    if apple_device() is None:
        log("usb waiting")
    device = wait_device()
    try:
        serial = Path(device, "serial").read_text().strip()
    except OSError:
        serial = ""
    log(f"usb iphone {serial}".strip())   # the serial keys the receiver's per-iPhone Lockdown record
    device = carplay_configuration(device)
    release_tethering(device)
    mux = None
    for path in sorted(device.parent.glob(f"{device.name}:{CARPLAY_CONFIGURATION}.*")):
        values = interface_values(path)
        if (values["bInterfaceClass"], values["bInterfaceSubClass"]) == ("ff", "fe"):
            endpoints = [int((e / "bEndpointAddress").read_text(), 16) for e in path.glob("ep_*")
                         if (e / "type").read_text().strip() == "Bulk"]
            mux = (int(values["bInterfaceNumber"], 16), next(e for e in endpoints if not e & 0x80),
                   next(e for e in endpoints if e & 0x80))
    functions = ncm_functions(device)
    if mux is None or not functions:
        raise RuntimeError(f"CarPlay configuration incomplete: usbmux={mux} ncm={functions}")
    # Two NCM functions; the iPhone picks one for CarPlay ("USB DirectLink", its en2): it connected over
    # the one we were not bridging and timed out (2026-10-12). Bridge both onto the TAP, learning which
    # function each peer MAC is behind; broadcast and multicast go out on both.
    log(f"NCM functions {[(f[0], f[1]) for f in functions]}")
    for control, data, *_ in functions:
        for number in (control, data):
            path = Path(device.parent, f"{device.name}:{CARPLAY_CONFIGURATION}.{number}")
            if (path / "driver").exists():
                ((path / "driver").resolve() / "unbind").write_text(path.name)
    fd = os.open(node(device), os.O_RDWR)
    claim(fd, device, mux[0])
    for control, data, *_ in functions:
        for number in (control, data):
            claim(fd, device, number)
        fcntl.ioctl(fd, USBDEVFS_SETINTERFACE, struct.pack("II", data, 1))
    # This side's MAC: the descriptor of the function CarPlay is reported on (the first; see below).
    raw = control_in(fd, 0x80, 0x06, 0x0300 | functions[0][2], 0x0409, 255)
    mac_hex = raw[2:raw[0]].decode("utf-16-le")
    mac = ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2)).lower()
    tap = open_tap(mac)
    log(f"ncm {TAP_NAME} {functions[0][0]} {mac}")
    behind = {}            # peer MAC -> index of the NCM function it was seen on
    sequences = [0] * len(functions)

    def ncm_to_tap(index, endpoint):
        while True:
            try:
                block = bulk(fd, endpoint, length=NCM_READ_CHUNK, timeout=1000)
            except OSError as error:
                if error.errno == errno.ETIMEDOUT:
                    continue
                log(f"NCM read ended: {error.strerror}")
                os._exit(0)
            for frame in ntb_datagrams(block):
                if len(frame) >= 12 and behind.get(frame[6:12]) != index:
                    behind[frame[6:12]] = index
                    log(f"NCM peer {frame[6:12].hex(':')} on function {functions[index][0]}")
                os.write(tap, frame)

    def tap_to_ncm():
        sent, timed_out = [0] * len(functions), [0] * len(functions)
        while True:
            frame = os.read(tap, 2048)
            target = behind.get(frame[:6]) if not frame[0] & 1 else None
            for index in ([target] if target is not None else range(len(functions))):
                try:
                    bulk(fd, functions[index][4], ntb_block(frame, sequences[index]), timeout=1000)
                    sent[index] += 1
                    if sent[index] in (1, 10, 100, 1000):
                        log(f"NCM function {functions[index][0]}: {sent[index]} frames sent, {timed_out[index]} timed out")
                except OSError as error:
                    if error.errno != errno.ETIMEDOUT:
                        log(f"NCM write ended: {error.strerror}")
                        os._exit(0)
                    # Before CarPlay starts the iPhone NAKs the NCM data path (Android app note).
                    timed_out[index] += 1
                    if timed_out[index] in (1, 10, 100):
                        log(f"NCM function {functions[index][0]}: write timed out ({timed_out[index]}x), {sent[index]} sent")
                sequences[index] += 1

    def status(index, endpoint):
        """CDC notifications (NetworkConnection, ConnectionSpeedChange). The Android app polled this
        endpoint; on the function without it the iPhone took our frames, on this one it NAKed them."""
        while True:
            try:
                note = bulk(fd, endpoint, length=64, timeout=1000)
            except OSError as error:
                if error.errno == errno.ETIMEDOUT:
                    continue
                return
            if note:
                log(f"NCM function {functions[index][0]} notification {note.hex(' ')}")

    for index, function in enumerate(functions):
        threading.Thread(target=ncm_to_tap, args=(index, function[3]), daemon=True).start()
        if function[5] is not None:
            threading.Thread(target=status, args=(index, function[5]), daemon=True).start()
    threading.Thread(target=tap_to_ncm, daemon=True).start()

    number, out_ep, in_ep = mux
    stdout = sys.stdout.buffer
    stdout_lock = threading.Lock()

    def from_phone():
        while True:
            try:
                data = bulk(fd, in_ep, timeout=1000)
            except OSError as error:
                if error.errno == errno.ETIMEDOUT:
                    continue
                log(f"USBMUX read ended: {error.strerror}")
                os._exit(0)
            if data:
                with stdout_lock:
                    stdout.write(struct.pack(">I", len(data)) + data)
                    stdout.flush()

    threading.Thread(target=from_phone, daemon=True).start()
    stdin = sys.stdin.buffer
    while True:
        header = stdin.read(4)
        if len(header) < 4:
            return 0
        (length,) = struct.unpack(">I", header)
        data = stdin.read(length)
        try:
            bulk(fd, out_ep, data, timeout=2000)
        except OSError as error:
            log(f"USBMUX write failed: {error.strerror}")
            return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, StopIteration) as error:
        log(f"USB bridge stopped: {type(error).__name__}: {error}")
        sys.exit(1)
