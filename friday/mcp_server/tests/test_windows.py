import pytest
from friday.mcp_server import windows


class FakeU:
    """Fake user32 recording the action calls."""
    def __init__(self, fg_result=1):
        self.calls = []
        self._fg = fg_result
    def ShowWindow(self, hwnd, cmd):
        self.calls.append(("ShowWindow", hwnd, cmd))
        return True
    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))
        return self._fg


@pytest.fixture
def fake_enum(monkeypatch):
    monkeypatch.setattr(
        windows, "_enumerate",
        lambda: [(101, "Google Chrome"), (202, "main.py - VS Code")],
    )


def test_find_case_insensitive_substring(fake_enum):
    assert windows._find("chrome") == (101, "Google Chrome")
    assert windows._find("VS CODE") == (202, "main.py - VS Code")


def test_find_no_match(fake_enum):
    assert windows._find("spotify") == (None, None)


def test_list_windows(fake_enum):
    out = windows.list_windows()
    assert "Google Chrome" in out and "VS Code" in out


def test_list_windows_empty(monkeypatch):
    monkeypatch.setattr(windows, "_enumerate", lambda: [])
    assert windows.list_windows() == "No visible windows."


def test_focus_success(fake_enum, monkeypatch):
    fu = FakeU(fg_result=1)
    monkeypatch.setattr(windows, "_u", fu)
    assert windows.focus_window("chrome") == "Focused 'Google Chrome'."
    assert ("ShowWindow", 101, windows.SW_RESTORE) in fu.calls
    assert ("SetForegroundWindow", 101) in fu.calls


def test_focus_blocked_is_honest(fake_enum, monkeypatch):
    fu = FakeU(fg_result=0)
    monkeypatch.setattr(windows, "_u", fu)
    assert "may need to click" in windows.focus_window("chrome")


def test_focus_no_match(fake_enum):
    assert windows.focus_window("spotify") == "No window matching 'spotify'."


def test_minimize(fake_enum, monkeypatch):
    fu = FakeU()
    monkeypatch.setattr(windows, "_u", fu)
    assert windows.minimize_window("chrome") == "Minimized 'Google Chrome'."
    assert ("ShowWindow", 101, windows.SW_MINIMIZE) in fu.calls


def test_maximize(fake_enum, monkeypatch):
    fu = FakeU()
    monkeypatch.setattr(windows, "_u", fu)
    assert windows.maximize_window("chrome") == "Maximized 'Google Chrome'."
    assert ("ShowWindow", 101, windows.SW_MAXIMIZE) in fu.calls
