"""System info: battery, CPU, RAM — read-only stats via psutil.

A dedicated tool so Friday answers "what's my battery?" reliably instead of
falling back to the flaky run_powershell escape-hatch. Output is one short,
voice-friendly line (low latency, small LLM context).
"""

import ctypes
import time

import psutil

from .windows import active_window

CF_UNICODETEXT = 13
_CLIP_CAP = 200  # keep clipboard from bloating the LLM context


def _clipboard_text() -> str:
    """Current clipboard text via user32 (no third-party deps); '' if empty
    or non-text."""
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    # Win64-safe: handles are 64-bit pointers; the default c_int restype would
    # truncate them and GlobalLock would then read a garbage address (crash).
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(0):
        return ""
    try:
        handle = u.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = k.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            k.GlobalUnlock(handle)
    finally:
        u.CloseClipboard()


def system_info() -> str:
    """Report battery %, charging state, CPU load, and RAM usage."""
    parts = []

    batt = psutil.sensors_battery()
    if batt is None:
        parts.append("No battery (desktop or unknown)")
    else:
        state = "charging" if batt.power_plugged else "on battery"
        parts.append(f"Battery {int(batt.percent)}% ({state})")

    # interval=0.1: short blocking sample so the reading is real, not 0.0 (the
    # first non-blocking call always returns 0.0). Negligible against turn latency.
    parts.append(f"CPU {psutil.cpu_percent(interval=0.1):.0f}%")

    mem = psutil.virtual_memory()
    parts.append(f"RAM {mem.percent:.0f}% ({mem.used / 1e9:.1f} of {mem.total / 1e9:.1f} GB)")

    return ", ".join(parts) + "."


def get_context() -> str:
    """Snapshot of full system state in one call: focused app, time, battery,
    CPU, and clipboard — so Friday can act with context before doing anything."""
    parts = []

    title, proc = active_window()
    if title or proc:
        app = f" ({proc})" if proc else ""
        parts.append(f"Active window: '{title}'{app}")
    else:
        parts.append("Active window: none")

    parts.append(f"Time: {time.strftime('%a %H:%M')}")

    batt = psutil.sensors_battery()
    if batt is None:
        parts.append("Battery: none")
    else:
        state = "charging" if batt.power_plugged else "on battery"
        parts.append(f"Battery {int(batt.percent)}% ({state})")

    parts.append(f"CPU {psutil.cpu_percent(interval=0.1):.0f}%")

    clip = _clipboard_text().strip()
    if clip:
        clip = clip[:_CLIP_CAP] + ("…" if len(clip) > _CLIP_CAP else "")
        parts.append(f"Clipboard: \"{clip}\"")
    else:
        parts.append("Clipboard: empty")

    return ". ".join(parts) + "."
