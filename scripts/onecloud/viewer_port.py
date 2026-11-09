"""The port the CarPlay viewer (PlayPort's web server) listens on.

18443 unless the owner set one. The settings page (OneCloud/N1) keeps it in state/options.json
"viewer_port"; the OpenWrt LuCI keeps dashplay.main.viewer_port in /etc/config/dashplay. An install that
only edited state/receiver/config.json keeps that value too: run-receiver.py used to write 18443 back on
every start and an update's restart dropped the owner's choice (2026-10-23).
"""
import json
from pathlib import Path
import time

DEFAULT = 18443
# The box settings page and its plain HTTP, and AirPlay; a viewer on one of them would collide.
RESERVED = (80, 443, 7000)

_CACHE = {}   # (path, mtime_ns, size) -> parsed value: the settings page asks resolve() several
              # times per render (every 3 s while pairing) and each look read two state files


def _read(path):
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    hit = _CACHE.get(key)
    if hit is None:
        try:
            hit = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if len(_CACHE) > 8:
            _CACHE.clear()
        _CACHE[key] = hit
    return hit


def valid(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    return 1024 <= port <= 65535 and port not in RESERVED


def resolve(state):
    """The settings page's choice, else the receiver's saved httpPort, else 18443."""
    state = Path(state)
    options = _read(state / "options.json") or {}
    if isinstance(options, dict) and valid(options.get("viewer_port")):
        return int(options["viewer_port"])
    saved = _read(state / "receiver" / "config.json") or {}
    if isinstance(saved, dict) and valid(saved.get("httpPort")):
        return int(saved["httpPort"])
    return DEFAULT
