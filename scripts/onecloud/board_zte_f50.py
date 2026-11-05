#!/usr/bin/env python3
"""ZTE F50 (MU300 OpenWrt) Bluetooth for the OpenWrt plugin: bring up the built-in Marlin3 controller
and run a private D-Bus and BlueZ, as DashPlay's earlier F50-only service did (2026-10-10).

The F50 runs a vendor 5.4 kernel; OpenWrt's bluez-daemon pulls in kmod-bluetooth for 6.12 (checked
2026-10-12), so BlueZ lives in its own root (--setup makes it with apk, scripts off, from the
official feeds) and is never installed into the system.
uci dashplay.main (supervisor_openwrt.py):
  option bluetooth_service '/usr/bin/python3 /opt/dashplay/board_zte_f50.py'
  option dbus_address 'unix:path=/var/run/dashplay/bluetooth/system_bus_socket'
It runs until a part exits; the supervisor then restarts everything.
"""
import argparse
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

RUNTIME = Path("/opt/dashplay-bluez")
RUN = Path("/var/run/dashplay")
BUS = RUN / "bluetooth"
TTY = Path("/dev/ttyBT0")


def local_address():
    """A stable locally administered unicast address for the controller (the chip has none)."""
    first = (secrets.randbits(8) | 0x02) & 0xFE
    return ":".join(f"{b:02X}" for b in [first] + [secrets.randbits(8) for _ in range(5)])


def initialised(argv):
    """mu300-bt-init uploads the vendor PSKey/RF blocks; False when the controller did not take them."""
    try:
        return subprocess.run(argv, timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def reset_controller(state, init, sleep=time.sleep):
    """Power-cycle the Bluetooth function of the combo chip (its rfkill) so the controller starts from its ROM
    defaults, then initialise it. The vendor's bluetooth-start waits 1 s blocked and 2 s after unblocking; the
    unblock itself returns once the function is powered, and on the F50 the init then took 0.07 s with no wait
    at all (2026-10-16, ten tries), so try that first and fall back to the vendor's waits."""
    state.write_text("0\n")
    sleep(0.2)
    state.write_text("1\n")
    if init():
        return "quick"
    state.write_text("0\n")
    sleep(1)
    state.write_text("1\n")
    sleep(2)
    if init():
        return "with the vendor's waits"
    raise SystemExit("Marlin3 Bluetooth init failed")


def unblock_rfkill(root=Path("/sys/class/rfkill")):
    """The controller can come up soft-blocked (F50/MU300, BlueZ 5.83, 2026-10-25): unblock the Bluetooth
    rfkill after hci0 exists. Match by type, never a fixed rfkill index; a hard block is an operator problem."""
    radio = next((p for p in Path(root).glob("rfkill*")
                  if (p / "type").read_text().strip() == "bluetooth"), None)
    if radio is None:
        raise SystemExit("Bluetooth rfkill not found after hci0")
    hard = (radio / "hard").read_text().strip()
    if hard != "0":
        raise SystemExit(f"Bluetooth is hard-blocked (rfkill hard={hard})")
    if (radio / "soft").read_text().strip() != "0":
        (radio / "soft").write_text("0\n")
    if (radio / "soft").read_text().strip() != "0":
        raise SystemExit("Bluetooth rfkill unblock failed (soft is still 1)")
    return radio


def stop_children(children, timeout=6):
    """Stop every child (each in its own process group) within one shared 6 s deadline: one after the
    other. Waiting 8 s per child outlasted the supervisor's wait for this helper, which was then killed
    with the children left running."""
    for _, child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    until = time.monotonic() + timeout
    for _, child in children:
        try:
            child.wait(timeout=max(0.0, until - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def kill_leftovers(runtime):
    """dbus-daemon, btattach and bluetoothd of this BlueZ left by a helper that was killed: they keep hci0."""
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if os.readlink(proc / "exe").startswith(str(runtime) + "/"):
                os.kill(int(proc.name), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def setup(runtime):
    """Private BlueZ + D-Bus root from the device's own (signed) package feeds; no package scripts run."""
    subprocess.run(["apk", "add", "--root", str(runtime), "--initdb", "--no-scripts", "--keys-dir", "/etc/apk/keys",
                    "--repositories-file", "/etc/apk/repositories.d/distfeeds.list",
                    "--arch", Path("/etc/apk/arch").read_text().split()[0],
                    "bluez-daemon", "bluez-utils", "dbus"], check=True)
    print(f"BlueZ runtime ready in {runtime}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, default=RUNTIME)
    parser.add_argument("--state", type=Path, default=Path("/opt/dashplay/state"))
    parser.add_argument("--setup", action="store_true", help="create the private BlueZ root, then exit")
    args = parser.parse_args()
    if args.setup:
        return setup(args.runtime)
    runtime = args.runtime
    for relative in ["usr/bin/dbus-daemon", "usr/bin/btattach", "usr/bin/bluetoothd", "usr/bin/bluetoothctl"]:
        if not (runtime / relative).is_file():
            raise SystemExit(f"missing {runtime / relative}: run {sys.argv[0]} --setup")
    libs = f"{runtime}/usr/lib:{runtime}/lib"
    env = dict(os.environ, LD_LIBRARY_PATH=libs, DBUS_SYSTEM_BUS_ADDRESS=f"unix:path={BUS}/system_bus_socket")
    BUS.mkdir(mode=0o700, parents=True, exist_ok=True)
    # bluetoothctl for the supervisor and the settings page (supervisor_openwrt.py puts RUN/bin first on PATH).
    bin_dir = RUN / "bin"
    bin_dir.mkdir(mode=0o755, exist_ok=True)
    wrapper = bin_dir / "bluetoothctl"
    wrapper.write_text(f'#!/bin/sh\nLD_LIBRARY_PATH="{libs}" exec "{runtime}/usr/bin/bluetoothctl" "$@"\n')
    wrapper.chmod(0o755)

    state = args.state
    address_file = state / "bdaddr"
    if not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address_file.read_text().strip() if address_file.exists() else ""):
        address_file.write_text(local_address() + "\n")
        address_file.chmod(0o600)
    storage = state / "bluetooth"
    storage.mkdir(mode=0o700, exist_ok=True)
    link = Path("/var/lib/bluetooth")
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() != storage.resolve():
        link.unlink()   # left by the earlier F50 service; pairings were copied into state/bluetooth
    if not link.exists() and not link.is_symlink():
        link.symlink_to(storage)
    if link.resolve() != storage.resolve():
        raise SystemExit("/var/lib/bluetooth belongs to something else")

    children = []
    stopped = []
    signal.signal(signal.SIGTERM, lambda *_: stopped.append(True))

    def wait_for(predicate, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until and not stopped:
            if predicate():
                return
            time.sleep(0.2)
        raise SystemExit("Bluetooth controller did not come up")

    def start(name, argv):
        children.append((name, subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, start_new_session=True)))

    try:
        subprocess.run(["modprobe", "sprdbt_tty"], check=True)
        wait_for(TTY.exists, 15)
        radios = [p for p in Path("/sys/class/rfkill").glob("rfkill*") if (p / "type").read_text().strip() == "bluetooth"]
        if len(radios) != 1:
            raise SystemExit("expected exactly one built-in Bluetooth rfkill")
        kill_leftovers(runtime)
        until = time.monotonic() + 5
        while list(Path("/sys/class/bluetooth").glob("hci*")) and time.monotonic() < until:
            time.sleep(0.2)
        if list(Path("/sys/class/bluetooth").glob("hci*")):
            raise SystemExit("an HCI controller already exists (is the old f50-carplay service still running?)")
        argv = ["/opt/mu300/bin/mu300-bt-init", "-d", str(TTY), "-p", "/lib/firmware/bt_configure_pskey.ini",
                "-r", "/lib/firmware/bt_configure_rf.ini", "-a", address_file.read_text().strip()]
        began = time.monotonic()
        way = reset_controller(radios[0] / "state", lambda: initialised(argv))
        print(f"Marlin3 Bluetooth reset ({way}) in {time.monotonic() - began:.2f} s", flush=True)
        (BUS / "dbus.conf").write_text(
            f'<busconfig><type>system</type><listen>unix:path={BUS}/system_bus_socket</listen><auth>EXTERNAL</auth>'
            '<policy user="root"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>\n')
        (BUS / "bluez.conf").write_text("[General]\nName=DashPlay\nControllerMode=dual\nDiscoverableTimeout=180\n"
                                        "PairableTimeout=180\n[Policy]\nAutoEnable=false\n")
        start("dbus", [str(runtime / "usr/bin/dbus-daemon"), "--nofork", f"--config-file={BUS / 'dbus.conf'}"])
        wait_for((BUS / "system_bus_socket").exists, 10)
        start("btattach", [str(runtime / "usr/bin/btattach"), "-B", str(TTY), "-P", "h4"])
        start("bluetoothd", [str(runtime / "usr/bin/bluetoothd"), "-n", "-f", str(BUS / "bluez.conf")])
        wait_for(Path("/sys/class/bluetooth/hci0").exists, 15)
        wait_for(lambda: any((p / "type").read_text().strip() == "bluetooth"
                             for p in Path("/sys/class/rfkill").glob("rfkill*")), 5)
        radio = unblock_rfkill()
        print(f"F50 hci0 rfkill unblocked ({radio.name})", flush=True)
        print("F50 Bluetooth ready", flush=True)
        while not stopped:
            for name, child in children:
                if child.poll() is not None:
                    raise SystemExit(f"{name} exited with {child.returncode}")
            time.sleep(2)
    finally:
        stop_children(list(reversed(children)))


if __name__ == "__main__":
    main()
