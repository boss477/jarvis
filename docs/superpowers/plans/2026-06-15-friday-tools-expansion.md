# Friday Tools Expansion (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add window control (list/focus/minimize/maximize) to Friday's MCP tool server, expand app mappings, and make `run_powershell` the explicit do-anything escape-hatch.

**Architecture:** New `friday/mcp_server/windows.py` uses stdlib `ctypes` + `user32.dll` (no deps; `pygetwindow`'s build is blocked by this machine's App Control policy). The 4 window tools register flat in `__main__.py` alongside the existing 21 (→ 25). Minor edits expand `APP_MAP`, bump the shell timeout, and add a prompt nudge.

**Tech Stack:** Python 3.11 stdlib `ctypes`/`wintypes`, FastMCP, pytest.

**Spec:** `docs/superpowers/specs/2026-06-15-friday-tools-expansion-design.md`

**Pre-verified (do not re-guess):** explicit Win64-safe signatures work —
`EnumWindows(argtypes=[WINFUNCTYPE(BOOL,HWND,LPARAM), LPARAM])`, `ShowWindow([HWND, c_int])`,
`SetForegroundWindow([HWND])→BOOL`, `IsWindowVisible([HWND])`, `GetWindowTextW([HWND,LPWSTR,c_int])`,
`GetWindowTextLengthW([HWND])`. Enumerated 13 real windows in testing.

---

## File Structure

**New:** `friday/mcp_server/windows.py`, `friday/mcp_server/tests/test_windows.py`
**Modified:** `friday/mcp_server/__main__.py`, `friday/mcp_server/tests/test_registration.py`,
`friday/mcp_server/apps.py`, `friday/mcp_server/tests/test_apps.py`,
`friday/mcp_server/shell.py`, `friday/mcp_server/tests/test_shell.py`, `friday/main.py`.

---

## Task 1: Window control module

**Files:**
- Create: `friday/mcp_server/windows.py`, `friday/mcp_server/tests/test_windows.py`

- [ ] **Step 1: Confirm Win64-safe window enumeration works**

Run:
```bash
uv run python -c "
import ctypes
from ctypes import wintypes
u = ctypes.windll.user32
EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
u.EnumWindows.argtypes = [EnumProc, wintypes.LPARAM]
out = []
def cb(h, l):
    if u.IsWindowVisible(h):
        out.append(int(h))
    return True
u.EnumWindows(EnumProc(cb), 0)
print('visible hwnds:', len(out))
"
```
Expected: prints a non-zero count (e.g. `visible hwnds: 13`). If it errors, STOP and re-check the ctypes signatures before continuing.

- [ ] **Step 2: Write the failing tests**

Create `friday/mcp_server/tests/test_windows.py`:
```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_windows.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.windows'`.

- [ ] **Step 4: Implement `windows.py`**

Create `friday/mcp_server/windows.py`:
```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_windows.py -q`
Expected: PASS (9 passed).

- [ ] **Step 6: Commit**

```bash
git add friday/mcp_server/windows.py friday/mcp_server/tests/test_windows.py
git commit -m "feat: window control tools via ctypes/user32"
```

---

## Task 2: Register the window tools

**Files:**
- Modify: `friday/mcp_server/__main__.py`, `friday/mcp_server/tests/test_registration.py`

- [ ] **Step 1: Update the registration test (count 21 → 25)**

In `friday/mcp_server/tests/test_registration.py`, change:
```python
    assert len(EXPECTED_TOOL_NAMES) == 21
```
to:
```python
    assert len(EXPECTED_TOOL_NAMES) == 25
    assert {"list_windows", "focus_window", "minimize_window", "maximize_window"} <= set(EXPECTED_TOOL_NAMES)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest friday/mcp_server/tests/test_registration.py -q`
Expected: FAIL — count is 21, not 25 (and the window names are missing).

- [ ] **Step 3: Register the window tools in `__main__.py`**

In `friday/mcp_server/__main__.py`, change the import line:
```python
from . import apps, files, shell, system
```
to:
```python
from . import apps, files, shell, system, windows
```

And in the `_TOOLS` list, after the `shell.run_powershell,` line, add:
```python
    windows.list_windows, windows.focus_window, windows.minimize_window, windows.maximize_window,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest friday/mcp_server/tests/test_registration.py -q`
Expected: PASS (1 passed) — 25 tools registered.

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/__main__.py friday/mcp_server/tests/test_registration.py
git commit -m "feat: register window tools (21 -> 25)"
```

---

## Task 3: Expand app mappings

**Files:**
- Modify: `friday/mcp_server/apps.py`, `friday/mcp_server/tests/test_apps.py`

- [ ] **Step 1: Write the failing test**

Append to `friday/mcp_server/tests/test_apps.py`:
```python
def test_open_app_new_mappings(monkeypatch):
    captured = {}
    monkeypatch.setattr(apps.subprocess, "Popen", lambda args, **kw: captured.setdefault("args", args))
    apps.open_app("word")
    assert "winword" in captured["args"]
    apps.open_app("task manager")
    assert "taskmgr" in captured["args"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest friday/mcp_server/tests/test_apps.py::test_open_app_new_mappings -q`
Expected: FAIL — "word" maps to "word" (not "winword") because the entry doesn't exist yet.

- [ ] **Step 3: Expand `APP_MAP`**

In `friday/mcp_server/apps.py`, replace the `APP_MAP` dict with:
```python
APP_MAP = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "spotify": "spotify",
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest friday/mcp_server/tests/test_apps.py -q`
Expected: PASS (all apps tests, including the new mappings).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/apps.py friday/mcp_server/tests/test_apps.py
git commit -m "feat: more app-name mappings"
```

---

## Task 4: Shell timeout bump + escape-hatch prompt + output filter

**Files:**
- Modify: `friday/mcp_server/shell.py`, `friday/mcp_server/tests/test_shell.py`, `friday/main.py`

- [ ] **Step 1: Write the failing test for the timeout**

Append to `friday/mcp_server/tests/test_shell.py`:
```python
def test_run_powershell_timeout_is_120(monkeypatch):
    captured = {}

    class FakeProc:
        stdout = "ok"
        stderr = ""
        returncode = 0

    def fake_run(*a, **k):
        captured.update(k)
        return FakeProc()

    monkeypatch.setattr(shell.subprocess, "run", fake_run)
    shell.run_powershell("echo ok")
    safety.confirm_action(safety._pending.id)
    assert captured.get("timeout") == 120
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest friday/mcp_server/tests/test_shell.py::test_run_powershell_timeout_is_120 -q`
Expected: FAIL — timeout is currently 60.

- [ ] **Step 3: Bump the timeout in `shell.py`**

In `friday/mcp_server/shell.py`, change:
```python
            capture_output=True, text=True, timeout=60,
```
to:
```python
            capture_output=True, text=True, timeout=120,
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest friday/mcp_server/tests/test_shell.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Add the escape-hatch sentence to SYSTEM_PROMPT**

In `friday/main.py`, find the end of `SYSTEM_PROMPT`:
```python
    "shown in brackets; if they decline, call cancel_action."
)
```
Replace with:
```python
    "shown in brackets; if they decline, call cancel_action. For any task that has no "
    "dedicated tool, accomplish it by writing one PowerShell command and calling "
    "run_powershell."
)
```

- [ ] **Step 6: Add `list_windows` to the output-truncation filters**

In `friday/main.py`, find:
```python
        tools_output_filters={"run_powershell": _trunc, "search_files": _trunc},
```
Replace with:
```python
        tools_output_filters={
            "run_powershell": _trunc,
            "search_files": _trunc,
            "list_windows": _trunc,
        },
```

- [ ] **Step 7: Verify app syntax + full suite**

Run:
```bash
uv run python -c "import ast; ast.parse(open('friday/main.py', encoding='utf-8').read()); print('syntax ok')"
uv run pytest -q
```
Expected: `syntax ok`; all tests pass (windows + registration(25) + apps + shell + existing).

- [ ] **Step 8: Commit**

```bash
git add friday/mcp_server/shell.py friday/mcp_server/tests/test_shell.py friday/main.py
git commit -m "feat: run_powershell escape-hatch (120s timeout + prompt); truncate list_windows output"
```

---

## Task 5: Manual end-to-end

- [ ] **Step 1: Run the app and exercise the new tools (logged → regression checklist)**

Start: `./run_friday.ps1` (confirm `Tool server connected — computer-control tools enabled.`)
Over voice, record pass/fail:
- "List my open windows" → reads back window titles.
- "Focus Chrome" → Chrome comes to front (or "you may need to click it").
- "Minimize VS Code" → minimizes; "Maximize Chrome" → maximizes.
- "Open Spotify" → launches (new mapping).
- A no-dedicated-tool task, e.g. "what's my IP address" → Friday writes a PowerShell command, asks to confirm, runs it.

---

## Self-Review

**Spec coverage:**
- §3 window control (`_enumerate`, `_find`, 4 tools, honest focus, SW constants, no-match strings) → Task 1. ✓
- §4 app mappings → Task 3. ✓
- §5 escape-hatch (timeout 60→120, prompt sentence) → Task 4. ✓
- §6 registration 21→25 + names → Task 2. ✓
- §3 `list_windows` client-truncation → Task 4 Step 6. ✓
- §7 testing (find/focus-honest/minimize/maximize/list/empty/no-match, registration count, app mappings, timeout) → Tasks 1–4. ✓
- §8 files / no deps → all tasks; ctypes is stdlib. ✓
- §9 Win64 argtypes must-verify → plan preamble + Task 1 Step 1 + implemented in Task 1 Step 4. ✓

**Placeholder scan:** No TBD/TODO; every code step is complete; every command has expected output. ✓

**Type/name consistency:** `_enumerate`, `_find`, `_u`, `SW_RESTORE/SW_MINIMIZE/SW_MAXIMIZE`, `list_windows`/`focus_window`/`minimize_window`/`maximize_window` consistent across Tasks 1–2 and the tests. Registration names in Task 2 match the function names in Task 1. ✓

**Fixed during review:** the registration test also asserts the 4 new names are present (not just the count), so a wrong-name registration can't pass by coincidence.
