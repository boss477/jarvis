"""System control: volume, brightness, media, lock/sleep/power."""

import subprocess

import comtypes
from pynput.keyboard import Key
from pycaw.pycaw import AudioUtilities

from . import safety
from ._keyboard import kbd


def _endpoint():
    """Return the default audio endpoint volume interface (pycaw).

    FastMCP runs each tool in a worker thread, and COM must be initialized on
    the calling thread before pycaw can use it — otherwise CoCreateInstance
    fails with 'CoInitialize has not been called'. CoInitialize is idempotent
    (ref-counted), so calling it per use is safe. pycaw's AudioDevice exposes
    the IAudioEndpointVolume interface directly via .EndpointVolume.
    """
    comtypes.CoInitialize()
    return AudioUtilities.GetSpeakers().EndpointVolume


def set_volume(level: int) -> str:
    """Set system master volume, level 0-100."""
    if not isinstance(level, int) or not 0 <= level <= 100:
        return "level must be an integer 0-100."
    _endpoint().SetMasterVolumeLevelScalar(level / 100, None)
    return f"Volume set to {level}."


def toggle_mute() -> str:
    """Mute or unmute system audio."""
    ep = _endpoint()
    was_muted = ep.GetMute()
    ep.SetMute(0 if was_muted else 1, None)
    return "Unmuted." if was_muted else "Muted."


def set_brightness(level: int) -> str:
    """Set display brightness 0-100 (internal display, via WMI)."""
    if not isinstance(level, int) or not 0 <= level <= 100:
        return "level must be an integer 0-100."
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)."
         f"WmiSetBrightness(1,{level})"],
        check=False,
    )
    return f"Brightness set to {level}."


def media_control(action: str) -> str:
    """Media transport: 'play_pause', 'next', or 'previous'."""
    keymap = {
        "play_pause": Key.media_play_pause,
        "next": Key.media_next,
        "previous": Key.media_previous,
    }
    if action not in keymap:
        return "action must be play_pause, next, or previous."
    kbd.press(keymap[action])
    kbd.release(keymap[action])
    return f"Media: {action}."


def lock_screen() -> str:
    """Lock the Windows session."""
    subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"], check=False)
    return "Locked."


def sleep_pc() -> str:
    """Put the PC to sleep."""
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], check=False)
    return "Sleeping."


def power_action(mode: str) -> str:
    """Restart or shut down the PC. mode = 'restart' | 'shutdown'. GATED."""
    if mode not in ("restart", "shutdown"):
        return "mode must be 'restart' or 'shutdown'."
    flag = "/r" if mode == "restart" else "/s"

    def _do() -> str:
        subprocess.run(["shutdown", flag, "/t", "0"], check=False)
        return f"{mode} initiated."

    return safety.stage(f"{mode} the PC now", _do)
