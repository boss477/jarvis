import pytest
from friday.mcp_server import input as inp
from friday.mcp_server import safety


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    safety._reset()
    # Record hotkey execution instead of really pressing keys.
    pressed = []
    monkeypatch.setattr(inp, "_do_hotkey", lambda parts: pressed.append(parts) or "ok")
    inp._test_pressed = pressed
    yield
    safety._reset()


def test_type_text_strips_enter(monkeypatch):
    typed = {}
    monkeypatch.setattr(inp.kbd, "type", lambda s: typed.setdefault("s", s))
    inp.type_text("hello\nworld\r\n")
    assert "\n" not in typed["s"]
    assert "\r" not in typed["s"]
    assert typed["s"] == "helloworld"


def test_press_hotkey_safe_combo_runs_direct():
    msg = inp.press_hotkey("ctrl+c")
    assert msg == "ok"
    assert inp._test_pressed == [["ctrl", "c"]]


def test_press_hotkey_danger_combo_stages():
    msg = inp.press_hotkey("win+r")
    assert msg.startswith("CONFIRM_REQUIRED")
    assert inp._test_pressed == []  # not executed


def test_press_hotkey_danger_match_is_order_insensitive():
    msg = inp.press_hotkey("r+win")
    assert msg.startswith("CONFIRM_REQUIRED")


def test_press_hotkey_empty_rejected():
    assert "ctrl+c" in inp.press_hotkey("  ")


def test_press_hotkey_danger_confirm_runs():
    inp.press_hotkey("alt+f4")
    safety.confirm_action(safety._pending.id)
    assert inp._test_pressed and set(inp._test_pressed[0]) == {"alt", "f4"}
