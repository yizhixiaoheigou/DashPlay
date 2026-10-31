#!/usr/bin/env python3
"""Run the DashPlay receiver (OneCloud, N1, OpenWrt); keep Wi-Fi credentials and tokens out of logs.

Reads the Wi-Fi handed to the iPhone from a JSON file (state/hotspot.json, or --hotspot: the car
hotspot, or on OpenWrt the network read from ubus) and uses the system Python and BlueZ/D-Bus.
"""
import argparse
import audio_routes
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import time


MILESTONES = [("RTSP listening", "receiver listening"), ("wireless bootstrap: iPhone", "invitation sent"),
              ("accepted by the iPhone", "invitation accepted"), ("CarPlay session active", "session active"),
              ("CarPlay is live", "CarPlay live")]


def durable_write(path, text):
    """Replace a file so that a power cut leaves either the old or the new content, never an empty file."""
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_browser_access(root, token, settings_port=80, car_music=False, tesla=False, tesla_browser=False, car_voice=False, routes=None):
    """Authorize viewers on the receiver LAN; admin login remains separate. car_music: the music goes over the car's
    own Bluetooth (the page leaves the media card and keys to the car, prompts louder); tesla: also the Tesla
    browser's ways (fullscreen through a listed video site); car_voice: calls and Siri go over the car's
    Bluetooth (the page greys out their levels)."""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Unexpected browser token format")
    dist = root / "playport/web/dist"
    # Served by the receiver to the box's own page only (WebHub.ownPage): a script any site could include
    # gave the token away (audit 2026-10-13, A2). The player links "Add iPhone / network" to this box's
    # settings page (settings_page.py; port 80, or another one on OpenWrt, where LuCI has 80).
    (dist / "receiver-access.js").unlink(missing_ok=True)
    path = dist / "receiver-access.json"
    flags = {**({"carMusic": bool(car_music or tesla)} if car_music or tesla or tesla_browser else {}), **({"tesla": True} if tesla or tesla_browser else {}),
             **({"carVoice": True} if car_voice else {})}
    if routes is not None:
        audio_routes.validate(routes)
        flags.update(audioRoutes=routes, carMusic=routes['media'] == 'bluetooth',
                     carVoice=routes['siri'] == routes['calls'] == 'bluetooth')
    content = json.dumps({"token": token, "settingsPort": int(settings_port), **flags}) + "\n"
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(content)
    temporary.chmod(0o644)
    temporary.replace(path)
    assert path.read_text() == content


# The iAP2 trace's Wi-Fi configuration field, a quoted string with " and \ escaped.
PASSPHRASE_FIELD = re.compile(r'(passphrase:\s*)"(?:[^"\\]|\\.)*"')


def heap_limit(meminfo):
    """The JVM heap ceiling for this board, from /proc/meminfo. 384 MB on the 1-2 GB boxes (F50 measured ~180 MB
    resident). A Snapdragon 410 stick has 512 MB, of which the modem and Wi-Fi firmware keep ~100 MB: a 384 MB
    ceiling there would let the heap outgrow the board, so 224 MB (not yet measured on a stick)."""
    found = re.search(r"^MemTotal:\s+(\d+) kB", meminfo, re.M)
    return "-Xmx224m" if found and int(found.group(1)) < 640 * 1024 else "-Xmx384m"


def redact(line, token, password):
    """A receiver log line without the viewer token or the hotspot password."""
    line = line.replace(token, "[REDACTED]")
    if line.lstrip().startswith("raw-body="):
        # The iAP2 trace's hex preview of a frame: for the Wi-Fi configuration it held the first
        # bytes of the password, which the plain-text redaction cannot see (2026-10-12).
        return line[:line.index("raw-body=")] + "raw-body=[not logged]\n"
    # The trace escapes " and \ in the password, so a plain replace missed such passwords (review 2026-10-12).
    line = PASSPHRASE_FIELD.sub(r'\1"[REDACTED]"', line)
    return line.replace(password, "[REDACTED]") if password else line


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--phone", default="", help="legacy single phone; migrated into the phone list")
    parser.add_argument("--interface", required=True)
    parser.add_argument("--channel", type=int, required=True, help="AP channel; may not be up yet")
    parser.add_argument("--hotspot", type=Path, help="Wi-Fi handed to the iPhone (default state/hotspot.json); "
                        "e.g. the car's own hotspot when the box joins it as a client")
    parser.add_argument("--bind-wait", type=int, default=0,
                        help="seconds the server may wait for --bind to be configured (started before the network)")
    parser.add_argument("--security", default="wpa2", choices=["open", "wpa2", "wpa3-transition", "wpa3"],
                        help="how that Wi-Fi is secured, as handed to the iPhone")
    parser.add_argument("--web-host", default="",
                        help="address the viewer listens on, e.g. 0.0.0.0 when the head unit reaches the box "
                             "through the car hotspot while AirPlay stays on --bind")
    parser.add_argument("--wired", action="store_true",
                        help="offer wired CarPlay: an iPhone on the USB cable (iphone-usb-bridge.py)")
    parser.add_argument("--wired-status", type=Path, help="where the receiver writes the wired CarPlay phase")
    parser.add_argument("--cable-off-file", type=Path,
                        help="marker meaning no CarPlay on the cable (the viewer's wireless row, or wireless only); "
                             "read by iphone-usb-bridge.py")
    parser.add_argument("--cable-carplay", default="on", choices=["on", "off"],
                        help="off: wireless only, the cable never carries CarPlay (it may still feed the box)")
    parser.add_argument("--java", default="/usr/bin/java", help="Java 21 (OpenWrt: the dashplay-jre package)")
    parser.add_argument("--settings-port", type=int, default=80, help="port of the box settings page")
    parser.add_argument("--viewer-port", type=int, default=0,
                        help="port of the viewer page (default: the saved config's, else 18443)")
    # Any car whose own Bluetooth is paired with the iPhone (found in a Tesla, 2026-10-14); --tesla is all of it.
    parser.add_argument("--audio-routes", type=audio_routes.parse)
    parser.add_argument("--car-music", action="store_true",
                        help="music over the car's own Bluetooth: the page leaves the media card and keys to the car")
    parser.add_argument("--car-voice", action="store_true",
                        help="calls and Siri over the car's own Bluetooth: the receiver offers no microphone")
    parser.add_argument("--tesla-browser", action="store_true", help="Tesla fullscreen without changing audio preferences")
    parser.add_argument("--tesla", action="store_true",
                        help="Tesla mode: --car-music, --car-voice and the Tesla browser's fullscreen")
    parser.add_argument("--idle-release", type=int, default=300,
                        help="end wireless CarPlay after this many seconds without a viewer page (0: never)")
    parser.add_argument("--no-wireless", action="store_true",
                        help="no Bluetooth/Wi-Fi handoff (car_hotspot mode: the iPhone uses the cable only)")
    args = parser.parse_args()
    root = args.root.resolve()
    wired = args.wired
    os.umask(0o077)
    state = root / "state" / "receiver"
    state.mkdir(parents=True, exist_ok=True)
    config_path = state / "config.json"
    try:
        saved = json.loads(config_path.read_text())
    except (OSError, ValueError):
        # Missing, or cut to 0 bytes by a power loss while it was being saved (2026-10-11: the
        # receiver then failed every start); fall back to the default display.
        saved = {}
    hotspot = json.loads((args.hotspot or root / "state/hotspot.json").read_text())
    ssid = hotspot["ssid"]
    password = "" if args.security == "open" else hotspot["passphrase"]
    viewer = args.viewer_port if 1024 <= args.viewer_port <= 65535 else 0
    config = {
        "deviceName": "DashPlay", "model": "DashPlay", "manufacturer": "DashPlay",
        "airPlayPort": 7000, "httpPort": viewer or saved.get("httpPort", 18443),
        "display": saved.get("display", {"width": 1280, "height": 720, "fps": 30, "uiScale": 100, "hevc": False}),
        # While an iPhone is on the cable the receiver pauses wireless invitations (WiredBootstrap).
        "wireless": not args.no_wireless, "wifiSsid": ssid, "wifiPassphrase": password,
        "wifiChannel": args.channel, "wifiSecurity": args.security,
        "bluetoothDeviceAddress": args.phone or None, "mdns": "jmdns",
    }
    durable_write(config_path, json.dumps(config, indent=2) + "\n")
    assert json.loads(config_path.read_text()) == config
    token_path = state / "browser-token"
    try:
        token = token_path.read_text().strip()
    except OSError:
        token = ""
    if not re.fullmatch(r"[0-9a-f]{32}", token):   # missing, or emptied by a power cut
        token = secrets.token_hex(16)
        durable_write(token_path, token + "\n")
    write_browser_access(root, token, args.settings_port, args.car_music, args.tesla, args.tesla_browser,
                         car_voice=bool(args.car_voice or args.tesla), routes=args.audio_routes)
    env = dict(os.environ, BT_BRIDGE_PYTHON="/usr/bin/python3", BT_BRIDGE_READY_WAIT="15", BT_BRIDGE_CONNECT_TIMEOUT="8",
               BT_BRIDGE_RECOVERY_STATE=str(state / "bluetooth-recovery.stamp"),
               CARPLAY_PRESENCE_HELPER=str(root / "phone-presence.py"),
               CARPLAY_PRESENCE_INTERFACE=args.interface, CARPLAY_DEFAULT_PHONE=args.phone,
               CARPLAY_PRESENCE_STATE=str(state / "phone-presence.json"))
    if args.web_host:
        env["CARPLAY_WEB_HOST"] = args.web_host
    if args.wired_status:
        env["CARPLAY_WIRED_STATUS"] = str(args.wired_status)
    # The cable's CarPlay (the viewer's rows) is separate from the box's internet over it (2026-10-31).
    if args.cable_off_file:
        env["CARPLAY_CABLE_OFF_FILE"] = str(args.cable_off_file)
        env["CARPLAY_CABLE_CARPLAY"] = args.cable_carplay
    if args.bind_wait:
        env["CARPLAY_BIND_WAIT_MS"] = str(args.bind_wait * 1000)
    env["CARPLAY_IDLE_RELEASE_SECONDS"] = str(max(0, args.idle_release))
    # AppCDS keeps working after jar updates only if the archive is rebuilt; this JDK mapped a stale
    # archive, logged "shared class paths mismatch" and neither used nor re-dumped it (2026-10-10),
    # costing ~3 s per start. Drop the archive whenever any classpath jar changes.
    # The JVM's own archive too: after a Java update the old one failed its checksum and AppCDS was off for that start.
    jvm = Path(args.java).resolve().parent.parent / "lib/server"
    jars = [root / "launcher/onecloud-launcher.jar",
            *sorted((root / "playport/server/build/install/server/lib").glob("*.jar")),
            *(path for path in (jvm / "libjvm.so", jvm / "classes.jsa") if path.exists())]
    fingerprint = "\n".join(f"{jar.name} {jar.stat().st_size} {jar.stat().st_mtime_ns}" for jar in jars)
    stamp = state / "app-cds.classpath"
    if not stamp.exists() or stamp.read_text() != fingerprint:
        (state / "app-cds.jsa").unlink(missing_ok=True)
        stamp.write_text(fingerprint)
    # A dump interrupted by a restart leaves a truncated archive that the JVM then refuses ("bad magic
    # number", "invalid archive") and never replaces, costing ~3 s on every start (dp-0017, 2026-10-31:
    # 3.9 MB instead of ~39 MB). The last start's log says so; drop it and the JVM dumps a fresh one on exit.
    cds_log = state / "cds.log"
    try:
        if cds_log.exists() and "invalid archive" in cds_log.read_text(errors="replace"):
            (state / "app-cds.jsa").unlink(missing_ok=True)
    except OSError:
        pass
    options = [
        "-Xms64m", heap_limit(Path("/proc/meminfo").read_text()),
        # iAP2 bootstrap steps (message ids only, no credentials) for diagnosing a stalled Wi-Fi handoff.
        "-Dorg.slf4j.simpleLogger.log.wireless=debug",
        # Milliseconds since JVM start on every line, for boot-time profiling.
        "-Dorg.slf4j.simpleLogger.showDateTime=true",
        # Prefer ChaCha20 for the browser TLS link; see tls.security for the measured reason.
        "-Djava.security.properties=" + str(root / "tls.security"),
        # AppCDS: archive loaded application classes on first clean exit, map them on later starts.
        "-Xlog:cds=info:file=" + str(state / "cds.log"),
        "-Xlog:gc:file=" + str(state / "gc.log"),
        "-XX:+AutoCreateSharedArchive", "-XX:SharedArchiveFile=" + str(state / "app-cds.jsa"), "-cp",
        # launcher/onecloud-launcher.jar (source in launcher/) warms the handshake crypto in parallel.
        str(root / "launcher/onecloud-launcher.jar") + ":" + str(root / "playport/server/build/install/server/lib/*"),
        "onecloud.Launcher", "--bind", args.bind, *(["--car-voice"] if args.car_voice or args.tesla else []),
        "--identity-dir", str(root / "identity"), "--state-dir", str(state),
        "--bt-bridge", str(root / "linux-bt-bridge/bt-bridge"), "--token", token,
    ] + (["--usb-bridge", str(root / "iphone-usb-bridge.py")] if wired else [])
    if args.audio_routes is not None:
        options += ['--audio-routes', audio_routes.serialize(args.audio_routes)]
    env['CARPLAY_AUDIO_ROUTES_HELPER'] = str(root / 'audio_routes.py')
    env['CARPLAY_AUDIO_SETTINGS_PORT'] = str(args.settings_port)
    argfile = state / "java.args"
    argfile.write_text("\n".join(json.dumps(value) for value in options) + "\n")
    # The supervisor signals the whole process group; outlive the JVM so it can finish writing the
    # AppCDS archive on exit (exiting first let systemd SIGKILL it mid-dump). A Python function
    # handler, unlike SIG_IGN, is reset to default in the exec'd JVM, which keeps its own handling.
    signal.signal(signal.SIGTERM, lambda *_: None)
    with subprocess.Popen(
        [args.java, "@" + str(argfile)], cwd=root / "playport", env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace",
    ) as process:
        (state / "java.pid").write_text(str(process.pid) + "\n")
        for line in process.stdout:
            line = redact(line, token, password)
            print(line, end="", flush=True)
            # The JVM's timestamps follow the wall clock, which jumps when time is synced during boot
            # (~27 s on 2026-10-11); mark milestones on the boot clock, comparable with "boot" lines.
            milestone = next((name for key, name in MILESTONES if key in line), None)
            if milestone:
                print(f"boot {time.monotonic():.2f}s {milestone}", flush=True)
        raise SystemExit(process.wait())


if __name__ == "__main__":
    main()
