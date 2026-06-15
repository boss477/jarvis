"""UI automation by element NAME, not coordinates (pywinauto, UIA backend).

The "API not GUI" rule, applied to clicking: instead of vision-finding a button
and clicking pixels, we ask the accessibility tree for a control by its name and
invoke it. click_element prefers the UIA Invoke pattern (no mouse movement —
fastest, most reliable) and only falls back to a real click when Invoke isn't
supported. Operates on the foreground window the user is looking at.
"""

import ctypes

from pywinauto import Application

_MAX_LIST = 60  # cap list_elements so a huge tree never floods the LLM context


def _foreground():
    """The active (foreground) window as a pywinauto UIA wrapper."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    app = Application(backend="uia").connect(handle=hwnd)
    return app.window(handle=hwnd)


def click_element(name: str) -> str:
    """Click the control whose name contains `name` in the active window."""
    needle = name.strip().lower()
    if not needle:
        return "name must not be empty."
    for el in _foreground().descendants():
        try:
            text = el.window_text().strip()
        except Exception:
            continue
        if text and needle in text.lower():
            try:
                el.invoke()  # UIA Invoke — no mouse move, fastest path
            except Exception:
                el.click_input()  # control doesn't support Invoke; real click
            return f"Clicked '{text}'."
    return f"No element named '{name}' in the active window."


def list_elements() -> str:
    """List named, clickable elements in the active window (for discovery)."""
    names: list[str] = []
    for el in _foreground().descendants():
        try:
            text = el.window_text().strip()
        except Exception:
            continue
        if text and text not in names:
            names.append(text)
            if len(names) >= _MAX_LIST:
                break
    return "\n".join(names) if names else "No named elements in the active window."
