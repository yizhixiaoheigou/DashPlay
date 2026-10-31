"""Remote debugging from the car: the box leaves its own hotspot for a while and joins the owner's phone hotspot,
so that it is online (Tailscale) while the car has no internet (2026-10-15).

The owner saves the phone hotspot once on the settings page (state/debug-hotspot.json, 0600). Starting a window
writes RUN/debug-uplink.json with its end on CLOCK_MONOTONIC: it survives the service restarts that switch the
network, but not a reboot (/run is cleared and the clock starts again), so pulling the power always brings the
box back to its normal network. The window also ends by itself after MINUTES, or from the settings page.
"""
import json
from pathlib import Path
import shutil
import time

MINUTES = 30
FILE = "debug-uplink.json"   # in RUN
HOTSPOT = "debug-hotspot.json"   # in state/
REMOTE = ("tailscale", "tailscaled")   # not in the public images: without it nobody can log in over the phone hotspot
SEARCH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def reachable(search=SEARCH):
    """Whether someone helping can log in once the box is online: Tailscale is installed."""
    return any(shutil.which(name, path=search) for name in REMOTE)


def hotspot(state, filename=HOTSPOT):
    """The saved phone hotspot {"ssid", "passphrase"}, or None."""
    try:
        value = json.loads((Path(state) / filename).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("ssid"), str) or not isinstance(value.get("passphrase"), str):
        return None
    if not 1 <= len(value["ssid"].encode()) <= 32 or not 8 <= len(value["passphrase"]) <= 63:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value['ssid'] + value['passphrase']):
        return None
    return {"ssid": value["ssid"], "passphrase": value["passphrase"]}


def remaining(run, now=None):
    """Seconds left in the open window, or 0 (none, ended, or unreadable; an ended one is removed)."""
    path = Path(run) / FILE
    now = time.monotonic() if now is None else now
    try:
        until = float(json.loads(path.read_text())["until"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if until <= now:
        path.unlink(missing_ok=True)
        return 0
    return until - now


def active(state, run, now=None):
    """The phone hotspot to join now, or None: a window is open and a hotspot is saved."""
    return hotspot(state) if remaining(run, now) else None


def start(run, minutes=MINUTES, now=None):
    now = time.monotonic() if now is None else now
    path = Path(run) / FILE
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"until": now + minutes * 60}))
    temporary.replace(path)


def stop(run):
    (Path(run) / FILE).unlink(missing_ok=True)
