"""Play a specific track on Spotify by name — Web API, not GUI (see [[friday-api-not-gui]]).

Playback control needs Spotify Premium + a one-time user login (Authorization
Code flow). Run `python -m friday.auth_spotify` ONCE to cache a refresh token;
after that this tool refreshes silently and never reopens a browser.

The Web API doesn't emit audio itself — it tells an already-open Spotify device
to play. So the desktop app (or web player) must be running. Pause/next/prev are
already covered by media keys (system.media_control), so this only adds *play X*.
"""

import os
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

# The MCP server is its own process; main.py's load_dotenv doesn't reach it.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

_SCOPE = "user-read-playback-state user-modify-playback-state"
_CACHE = str(Path(__file__).resolve().parents[1] / ".spotify_cache")
_REDIRECT = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

_sp = None
_lock = threading.Lock()


def _client():
    """Lazily build one authenticated Spotify client from the cached token."""
    global _sp
    with _lock:
        if _sp is not None:
            return _sp
        import spotipy
        from spotipy.oauth2 import SpotifyOAuth

        auth = SpotifyOAuth(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=_REDIRECT,
            scope=_SCOPE,
            cache_path=_CACHE,
            open_browser=False,  # never block the server on a browser prompt
        )
        _sp = spotipy.Spotify(auth_manager=auth)
        return _sp


def _pick_device(sp):
    """Return (device_id, was_active). Prefer the active device, else a Computer, else any."""
    devices = sp.devices().get("devices", [])
    if not devices:
        return None, False
    for d in devices:
        if d.get("is_active"):
            return d["id"], True
    for d in devices:  # prefer a desktop/web player over a phone when none is active
        if d.get("type") == "Computer":
            return d["id"], False
    return devices[0]["id"], False


def _launch_and_wait_for_device(sp, timeout=12.0):
    """Open the desktop Spotify app and poll until it registers as a device."""
    import subprocess

    subprocess.Popen(["cmd", "/c", "start", "", "Spotify.exe"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        device_id, was_active = _pick_device(sp)
        if device_id is not None:
            return device_id, was_active
    return None, False


def spotify_play(query: str) -> str:
    """Search Spotify for `query` and play the top track (Premium; Spotify must be open)."""
    if not Path(_CACHE).exists():
        return "Spotify isn't authorized yet — run `python -m friday.auth_spotify` once."
    try:
        sp = _client()
        # limit=1 returns a less-relevant top hit than the app shows; ask for a few
        # and take the first — that order matches Spotify's in-app ranking.
        hits = sp.search(q=query, type="track", limit=5).get("tracks", {}).get("items", [])
        if not hits:
            return f"No track found for '{query}'."
        track = hits[0]
        name = track["name"]
        artist = track["artists"][0]["name"] if track["artists"] else "unknown"

        device_id, was_active = _pick_device(sp)
        if device_id is None:
            # No device yet — launch the desktop app and wait for it to appear,
            # so "play X" works in one shot without the user opening Spotify.
            device_id, was_active = _launch_and_wait_for_device(sp)
        if device_id is None:
            return "Opened Spotify — give it a moment to load, then ask again."

        # An idle device 404s on a direct play; transferring wakes it up first.
        if not was_active:
            sp.transfer_playback(device_id, force_play=False)
            time.sleep(1.0)
        sp.start_playback(device_id=device_id, uris=[track["uri"]])
        return f"Playing '{name}' by {artist}."
    except Exception as e:
        msg = str(e)
        if "Premium required" in msg or "403" in msg:
            return "Playback needs Spotify Premium on the logged-in account."
        return f"Spotify play failed: {msg}"
