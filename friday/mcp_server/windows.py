"""Window control via stdlib ctypes + user32.dll (no third-party deps).

pygetwindow has no wheel and its source build is blocked by this machine's
Windows Application Control policy, so we call user32 directly.
"""

import ctypes
from ctypes import wintypes

_u = ctypes.windll.user32

SW_MAXIMIZE = 3
SW_MINIMIZE = 6
SW_RESTORE = 9

# Win64-safe signatures (HWND is a 64-bit pointer; default c_int would truncate it).
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_u.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
_u.EnumWindows.restype = wintypes.BOOL
_u.IsWindowVisible.argtypes = [wintypes.HWND]
_u.IsWindowVisible.restype = wintypes.BOOL
_u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u.GetWindowTextLengthW.restype = ctypes.c_int
_u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u.GetWindowTextW.restype = ctypes.c_int
_u.SetForegroundWindow.argtypes = [wintypes.HWND]
_u.SetForegroundWindow.restype = wintypes.BOOL
_u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u.ShowWindow.restype = wintypes.BOOL


def _enumerate() -> list[tuple[int, str]]:
    """Return (hwnd, title) for every visible window with a non-empty title."""
    out: list[tuple[int, str]] = []

    def _cb(hwnd, _lparam):
        if _u.IsWindowVisible(hwnd):
            n = _u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _u.GetWindowTextW(hwnd, buf, n + 1)
                title = buf.value.strip()
                if title:
                    out.append((int(hwnd), title))
        return True

    _u.EnumWindows(_EnumProc(_cb), 0)
    return out


def _find(title: str):
    """First (hwnd, title) whose title contains `title` (case-insensitive)."""
    needle = title.strip().lower()
    for hwnd, t in _enumerate():
        if needle in t.lower():
            return hwnd, t
    return None, None


def list_windows() -> str:
    """List the titles of all visible windows."""
    titles = [t for _, t in _enumerate()]
    return "\n".join(titles) if titles else "No visible windows."


def focus_window(title: str) -> str:
    """Bring the window matching `title` to the foreground."""
    hwnd, t = _find(title)
    if hwnd is None:
        return f"No window matching '{title}'."
    _u.ShowWindow(hwnd, SW_RESTORE)
    if _u.SetForegroundWindow(hwnd):
        return f"Focused '{t}'."
    return f"Brought '{t}' forward; you may need to click it."


def minimize_window(title: str) -> str:
    """Minimize the window matching `title`."""
    hwnd, t = _find(title)
    if hwnd is None:
        return f"No window matching '{title}'."
    _u.ShowWindow(hwnd, SW_MINIMIZE)
    return f"Minimized '{t}'."


def maximize_window(title: str) -> str:
    """Maximize the window matching `title`."""
    hwnd, t = _find(title)
    if hwnd is None:
        return f"No window matching '{title}'."
    _u.ShowWindow(hwnd, SW_MAXIMIZE)
    return f"Maximized '{t}'."
