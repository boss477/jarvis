"""Keyboard input. type_text can't submit (Enter stripped); danger hotkeys gated."""

from pynput.keyboard import Key

from . import safety
from ._keyboard import kbd

# Combos that can escape the gate (open Run box, close window, etc.) -> require confirm.
DANGER_COMBOS = {
    frozenset({"win", "r"}),
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "w"}),
    frozenset({"win", "e"}),
}

_KEYMAP = {
    "win": Key.cmd, "cmd": Key.cmd, "ctrl": Key.ctrl, "control": Key.ctrl,
    "alt": Key.alt, "shift": Key.shift, "enter": Key.enter, "return": Key.enter,
    "tab": Key.tab, "esc": Key.esc, "escape": Key.esc, "space": Key.space,
    "delete": Key.delete, "backspace": Key.backspace, "home": Key.home, "end": Key.end,
}


def type_text(text: str) -> str:
    """Type literal text. Enter/newlines are stripped so it can never submit alone."""
    clean = text.replace("\r", "").replace("\n", "")
    kbd.type(clean)
    return f"Typed {len(clean)} characters."


def press_hotkey(combo: str) -> str:
    """Press a hotkey like 'ctrl+c'. Danger combos (win+r, alt+f4, ...) are gated."""
    parts = [k.strip().lower() for k in combo.split("+") if k.strip()]
    if not parts:
        return "combo must look like 'ctrl+c'."
    if frozenset(parts) in DANGER_COMBOS:
        return safety.stage(f"press hotkey {combo}", lambda: _do_hotkey(parts))
    return _do_hotkey(parts)


def _do_hotkey(parts: list[str]) -> str:
    """Press all keys down (in order) then release in reverse."""
    resolved = [_KEYMAP.get(p, p) for p in parts]
    for k in resolved:
        kbd.press(k)
    for k in reversed(resolved):
        kbd.release(k)
    return f"Pressed {'+'.join(parts)}."
