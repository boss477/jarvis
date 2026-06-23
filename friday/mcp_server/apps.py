"""Launch apps / open URLs / close apps."""

import subprocess
import webbrowser
from urllib.parse import quote_plus

# Friendly spoken name -> launchable command. Unknown names pass through as-is.
APP_MAP = {
    "chrome": "chrome",
    "google chrome": "chrome",
    # Store Spotify installs a `Spotify.exe` execution alias in WindowsApps
    # (on PATH); plain "spotify" isn't found, so `start spotify` fails.
    "spotify": "Spotify.exe",
    "discord": "discord",
    "vs code": "code",
    "vscode": "code",
    "code": "code",
    "notepad": "notepad",
    "explorer": "explorer",
    "file explorer": "explorer",
    "files": "explorer",
    "calculator": "calc",
    "calc": "calc",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "settings": "ms-settings:",
    "terminal": "wt",
    "task manager": "taskmgr",
}


def open_app(name: str) -> str:
    """Launch an application by friendly name, e.g. 'chrome' or 'vs code'."""
    key = name.strip().lower()
    target = APP_MAP.get(key, key)
    # `start` resolves PATH apps and registered app aliases; empty "" is the title arg.
    subprocess.Popen(["cmd", "/c", "start", "", target])
    return f"Opening {name}."


def open_url(url: str) -> str:
    """Open a URL in the default browser."""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    webbrowser.open(url)
    return f"Opening {url}."


def web_search(query: str) -> str:
    """Open the default browser to a web search for the query."""
    webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
    return f"Searching for {query}."


def close_app(name: str) -> str:
    """Terminate an application by name (e.g. 'chrome' or 'notepad.exe')."""
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    proc = subprocess.run(["taskkill", "/IM", exe, "/F"], capture_output=True, text=True)
    return f"Closed {name}." if proc.returncode == 0 else f"Could not close {name}."
