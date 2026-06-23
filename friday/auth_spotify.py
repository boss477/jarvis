"""One-time Spotify login: opens a browser, you approve, a refresh token is cached.

Run once:  python -m friday.auth_spotify
After this, the MCP `spotify_play` tool refreshes silently — no more browser.
Requires SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in friday/.env and the matching
Redirect URI (default http://127.0.0.1:8888/callback) added in the Spotify dashboard.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

load_dotenv(Path(__file__).resolve().parent / ".env")

auth = SpotifyOAuth(
    client_id=os.environ["SPOTIFY_CLIENT_ID"],
    client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
    redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
    scope="user-read-playback-state user-modify-playback-state",
    cache_path=str(Path(__file__).resolve().parent / ".spotify_cache"),
    open_browser=True,
)

token = auth.get_access_token(as_dict=False)
print("Spotify authorized — token cached. You can close this window.")
