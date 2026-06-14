# Friday Computer-Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Friday a standalone FastMCP tool server that lets it control the Windows laptop (apps, system, files, shell, keyboard input) by voice, with a server-side confirmation gate on destructive actions, connected to the existing Pipecat voice app over streamable HTTP.

**Architecture:** A new `friday/mcp_server/` package exposes ~21 tools across domain modules (`apps`, `system`, `files`, `shell`, `input`) plus a `safety` gate, all registered flat on one `FastMCP` instance served at `http://127.0.0.1:8000/mcp/` (loopback only). The voice app (`friday/main.py`) connects via Pipecat's `MCPClient`, registers the tools on the LLM, and closes the session on shutdown. Destructive tools stage a closure that only runs after an explicit, id-targeted `confirm_action`.

**Tech Stack:** Python 3.11, FastMCP 3.4.2, Pipecat 1.3.0 (`MCPClient`), `pycaw` (volume), `pynput` (keyboard/media), PowerShell shell-outs (brightness/lock/sleep/power/shell), pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-06-15-friday-computer-control-design.md`

---

## File Structure

**New package `friday/mcp_server/`:**
- `__init__.py` — marks the package.
- `_keyboard.py` — single shared `pynput` `Controller` (used by `input` + `system` media keys). One responsibility: own the keyboard handle.
- `safety.py` — the confirmation gate: single pending-slot state, `stage()`, `confirm_action()`, `cancel_action()`, `_reset()` test helper.
- `apps.py` — `open_app`, `open_url`, `web_search`, `close_app`.
- `system.py` — `set_volume`, `toggle_mute`, `set_brightness`, `media_control`, `lock_screen`, `sleep_pc`, `power_action` (gated).
- `files.py` — `_safe_path()` sandbox helper + `open_path`, `search_files`, `create_folder`, `delete_path` (gated), `move_path` (gated).
- `shell.py` — `run_powershell` (gated).
- `input.py` — `type_text` (strips Enter), `press_hotkey` (danger combos gated).
- `__main__.py` — builds the `FastMCP` instance, registers every tool flat, serves HTTP.
- `tests/` — one test module per source module.

**Modified:**
- `friday/main.py` — MCPClient start/register/close wiring + `_trunc` filter + SYSTEM_PROMPT confirmation note.
- `friday/.env` — `MCP_URL` → `http://127.0.0.1:8000/mcp/`.
- `pyproject.toml` — add `pycaw`; add `pytest`, `pytest-asyncio` dev deps.

**Repo root:**
- `conftest.py` — ensures project root on `sys.path` so `from friday.mcp_server import ...` resolves under pytest.
- `run_friday.ps1` — start server → poll port → launch voice app → teardown.

---

## Task 1: Project scaffolding & dependencies

**Files:**
- Create: `friday/mcp_server/__init__.py`, `friday/mcp_server/tests/__init__.py`, `conftest.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependencies**

Run:
```bash
uv add pycaw
uv add --dev pytest pytest-asyncio
```
Expected: resolves and installs; `pyproject.toml` gains `pycaw` under dependencies and a `[dependency-groups]`/dev section with `pytest`, `pytest-asyncio`.

- [ ] **Step 2: Create the package marker files**

Create `friday/mcp_server/__init__.py`:
```python
"""Friday computer-control tools, served as a FastMCP server."""
```

Create `friday/mcp_server/tests/__init__.py`:
```python
```

- [ ] **Step 3: Create root conftest so tests import the package**

Create `conftest.py` (project root):
```python
import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 4: Verify pytest collects nothing yet (clean baseline)**

Run: `uv run pytest -q`
Expected: `no tests ran` (exit code 5) — confirms pytest is installed and configured.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock friday/mcp_server/__init__.py friday/mcp_server/tests/__init__.py conftest.py
git commit -m "chore: scaffold mcp_server package and test deps"
```

---

## Task 2: Safety gate (`safety.py`)

The core of the whole feature. Single pending slot, id-targeted confirm, 90s expiry, stays armed until explicit confirm/cancel.

**Files:**
- Create: `friday/mcp_server/safety.py`
- Test: `friday/mcp_server/tests/test_safety.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/mcp_server/tests/test_safety.py`:
```python
import time
import pytest
from friday.mcp_server import safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


def test_stage_returns_confirm_required_with_id():
    msg = safety.stage("delete foo.txt", lambda: "did it")
    assert msg.startswith("CONFIRM_REQUIRED [id=")
    assert "delete foo.txt" in msg


def test_confirm_with_correct_id_runs_fn():
    calls = []
    safety.stage("do thing", lambda: calls.append(1) or "ran")
    action_id = safety._pending.id
    result = safety.confirm_action(action_id)
    assert result == "ran"
    assert calls == [1]
    assert safety._pending is None  # slot cleared after run


def test_confirm_with_wrong_id_rejected_and_slot_kept():
    safety.stage("do thing", lambda: "ran")
    result = safety.confirm_action("zzzzzz")
    assert "mismatch" in result.lower()
    assert safety._pending is not None  # still armed


def test_confirm_with_no_pending_is_clean():
    result = safety.confirm_action("anything")
    assert "no action" in result.lower()


def test_cancel_clears_slot():
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    assert "cancel" in safety.cancel_action().lower()
    assert safety._pending is None
    assert "no action" in safety.confirm_action(action_id).lower()


def test_second_stage_replaces_first():
    safety.stage("first", lambda: "first-ran")
    first_id = safety._pending.id
    safety.stage("second", lambda: "second-ran")
    assert safety._pending.id != first_id
    assert "mismatch" in safety.confirm_action(first_id).lower()


def test_unrelated_call_leaves_slot_armed():
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    # Simulate an unrelated command happening (no gate interaction).
    assert safety._pending is not None
    assert safety.confirm_action(action_id) == "ran"


def test_expiry_boundary():
    # 89s elapsed -> accepted
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    safety._pending.created_at = time.monotonic() - 89
    assert safety.confirm_action(action_id) == "ran"

    # 90s elapsed -> rejected
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    safety._pending.created_at = time.monotonic() - 90
    assert "expired" in safety.confirm_action(action_id).lower()

    # 91s elapsed -> rejected
    safety.stage("do thing", lambda: "ran")
    action_id = safety._pending.id
    safety._pending.created_at = time.monotonic() - 91
    assert "expired" in safety.confirm_action(action_id).lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_safety.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.safety'`.

- [ ] **Step 3: Implement `safety.py`**

Create `friday/mcp_server/safety.py`:
```python
"""Server-side confirmation gate for destructive tool actions.

A single pending action is staged by a risky tool and only runs when the LLM
calls confirm_action() with the matching id, within the expiry window. The gate
is independent of the LLM: nothing destructive runs without the explicit,
id-targeted second call.
"""

import time
import uuid
from dataclasses import dataclass
from typing import Callable

EXPIRY_SECONDS = 90


@dataclass
class PendingAction:
    id: str
    summary: str
    fn: Callable[[], str]
    created_at: float


_pending: PendingAction | None = None


def stage(summary: str, fn: Callable[[], str]) -> str:
    """Store a single pending action, replacing any existing one. Returns the
    CONFIRM_REQUIRED message the LLM should read back to the user."""
    global _pending
    action_id = uuid.uuid4().hex[:6]
    _pending = PendingAction(id=action_id, summary=summary, fn=fn, created_at=time.monotonic())
    return f"CONFIRM_REQUIRED [id={action_id}]: {summary}"


def confirm_action(action_id: str) -> str:
    """Execute the staged action if action_id matches the pending slot and it
    has not expired. The LLM must pass the id from the CONFIRM_REQUIRED message."""
    global _pending
    if _pending is None:
        return "No action is pending."
    if action_id != _pending.id:
        return f"action_id mismatch; the pending action id is {_pending.id}."
    if time.monotonic() - _pending.created_at >= EXPIRY_SECONDS:
        _pending = None
        return "Pending action expired; please ask again."
    action = _pending
    _pending = None
    return action.fn()


def cancel_action() -> str:
    """Discard the pending action, if any."""
    global _pending
    if _pending is None:
        return "Nothing to cancel."
    _pending = None
    return "Cancelled."


def _reset() -> None:
    """Test helper: clear the pending slot."""
    global _pending
    _pending = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_safety.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/safety.py friday/mcp_server/tests/test_safety.py
git commit -m "feat: server-side confirmation gate for risky tools"
```

---

## Task 3: Shared keyboard handle (`_keyboard.py`)

**Files:**
- Create: `friday/mcp_server/_keyboard.py`

- [ ] **Step 1: Implement the shared controller**

Create `friday/mcp_server/_keyboard.py`:
```python
"""Single shared pynput keyboard Controller, reused by input + media tools."""

from pynput.keyboard import Controller

kbd = Controller()
```

- [ ] **Step 2: Verify it imports**

Run: `uv run python -c "from friday.mcp_server._keyboard import kbd; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add friday/mcp_server/_keyboard.py
git commit -m "feat: shared pynput keyboard controller"
```

---

## Task 4: App control (`apps.py`)

**Files:**
- Create: `friday/mcp_server/apps.py`
- Test: `friday/mcp_server/tests/test_apps.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/mcp_server/tests/test_apps.py`:
```python
from friday.mcp_server import apps


def test_open_app_maps_friendly_name(monkeypatch):
    calls = {}
    monkeypatch.setattr(apps.subprocess, "Popen", lambda args, **kw: calls.setdefault("args", args))
    msg = apps.open_app("vs code")
    assert "code" in calls["args"]      # mapped from "vs code"
    assert "Opening" in msg


def test_open_app_unknown_name_passes_through(monkeypatch):
    calls = {}
    monkeypatch.setattr(apps.subprocess, "Popen", lambda args, **kw: calls.setdefault("args", args))
    apps.open_app("someapp")
    assert "someapp" in calls["args"]


def test_open_url_adds_scheme(monkeypatch):
    opened = {}
    monkeypatch.setattr(apps.webbrowser, "open", lambda url: opened.setdefault("url", url))
    apps.open_url("example.com")
    assert opened["url"] == "https://example.com"


def test_web_search_builds_query_url(monkeypatch):
    opened = {}
    monkeypatch.setattr(apps.webbrowser, "open", lambda url: opened.setdefault("url", url))
    apps.web_search("hello world")
    assert "q=hello+world" in opened["url"]


def test_close_app_reports_failure_cleanly(monkeypatch):
    class FakeProc:
        returncode = 1
    monkeypatch.setattr(apps.subprocess, "run", lambda *a, **k: FakeProc())
    msg = apps.close_app("nothere")
    assert "could not close" in msg.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_apps.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.apps'`.

- [ ] **Step 3: Implement `apps.py`**

Create `friday/mcp_server/apps.py`:
```python
"""Launch apps / open URLs / close apps."""

import subprocess
import webbrowser
from urllib.parse import quote_plus

# Friendly spoken name -> launchable command. Unknown names pass through as-is.
APP_MAP = {
    "chrome": "chrome",
    "google chrome": "chrome",
    "spotify": "spotify",
    "vs code": "code",
    "vscode": "code",
    "code": "code",
    "notepad": "notepad",
    "explorer": "explorer",
    "file explorer": "explorer",
    "calculator": "calc",
    "calc": "calc",
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_apps.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/apps.py friday/mcp_server/tests/test_apps.py
git commit -m "feat: app control tools (open/url/search/close)"
```

---

## Task 5: System control (`system.py`)

Volume via `pycaw`, brightness via PowerShell WMI, media via shared `pynput`, lock/sleep via `rundll32`, `power_action` gated.

**Files:**
- Create: `friday/mcp_server/system.py`
- Test: `friday/mcp_server/tests/test_system.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/mcp_server/tests/test_system.py`:
```python
import pytest
from friday.mcp_server import system, safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


class FakeEndpoint:
    def __init__(self):
        self.scalar = None
        self.muted = 0
    def SetMasterVolumeLevelScalar(self, v, _):
        self.scalar = v
    def GetMute(self):
        return self.muted
    def SetMute(self, v, _):
        self.muted = v


def test_set_volume_valid(monkeypatch):
    fake = FakeEndpoint()
    monkeypatch.setattr(system, "_endpoint", lambda: fake)
    msg = system.set_volume(50)
    assert fake.scalar == 0.5
    assert "50" in msg


def test_set_volume_out_of_range_rejected():
    assert "0-100" in system.set_volume(150)
    assert "0-100" in system.set_volume(-1)


def test_toggle_mute_flips(monkeypatch):
    fake = FakeEndpoint()
    monkeypatch.setattr(system, "_endpoint", lambda: fake)
    system.toggle_mute()
    assert fake.muted == 1


def test_set_brightness_invokes_powershell(monkeypatch):
    calls = {}
    monkeypatch.setattr(system.subprocess, "run", lambda args, **k: calls.setdefault("args", args))
    msg = system.set_brightness(70)
    assert any("WmiSetBrightness(1,70)" in str(a) for a in calls["args"])
    assert "70" in msg


def test_media_control_invalid_action():
    assert "play_pause" in system.media_control("rewind")


def test_power_action_stages_not_runs(monkeypatch):
    ran = {}
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: ran.setdefault("ran", True))
    msg = system.power_action("shutdown")
    assert msg.startswith("CONFIRM_REQUIRED")
    assert "ran" not in ran  # nothing executed on first call


def test_power_action_invalid_mode():
    assert "restart" in system.power_action("explode")


def test_power_action_confirm_runs(monkeypatch):
    ran = {}
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: ran.setdefault("ran", True))
    system.power_action("restart")
    safety.confirm_action(safety._pending.id)
    assert ran.get("ran") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_system.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.system'`.

- [ ] **Step 3: Implement `system.py`**

Create `friday/mcp_server/system.py`:
```python
"""System control: volume, brightness, media, lock/sleep/power."""

import subprocess
from ctypes import POINTER, cast

from comtypes import CLSCTX_ALL
from pynput.keyboard import Key
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

from . import safety
from ._keyboard import kbd


def _endpoint():
    """Return the default audio endpoint volume interface (pycaw)."""
    speakers = AudioUtilities.GetSpeakers()
    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_system.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/system.py friday/mcp_server/tests/test_system.py
git commit -m "feat: system control tools (volume/brightness/media/lock/sleep/power)"
```

---

## Task 6: File operations (`files.py`)

Sandbox helper `_safe_path` confines destructive ops to allowed roots; `delete_path`/`move_path` are gated.

**Files:**
- Create: `friday/mcp_server/files.py`
- Test: `friday/mcp_server/tests/test_files.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/mcp_server/tests/test_files.py`:
```python
import os
import pytest
from pathlib import Path
from friday.mcp_server import files, safety


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    # Confine all destructive ops to tmp_path for the duration of the test.
    monkeypatch.setattr(files, "ALLOWED_ROOTS", [tmp_path.resolve()])
    safety._reset()
    yield
    safety._reset()


def test_safe_path_accepts_inside_root(tmp_path):
    p = files._safe_path(str(tmp_path / "sub" / "x.txt"))
    assert str(p).startswith(str(tmp_path.resolve()))


def test_safe_path_rejects_traversal():
    with pytest.raises(ValueError):
        files._safe_path("../../../etc/passwd")


def test_safe_path_rejects_absolute_outside(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError):
        files._safe_path(str(outside))


def test_safe_path_rejects_symlink_escape(tmp_path):
    target_outside = tmp_path.parent / "secret"
    target_outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        os.symlink(target_outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")
    with pytest.raises(ValueError):
        files._safe_path(str(link / "f.txt"))


def test_create_folder_inside_root(tmp_path):
    msg = files.create_folder(str(tmp_path / "newdir"))
    assert (tmp_path / "newdir").is_dir()
    assert "Created" in msg


def test_create_folder_outside_root_rejected(tmp_path):
    msg = files.create_folder(str(tmp_path.parent / "evil"))
    assert "outside allowed roots" in msg
    assert not (tmp_path.parent / "evil").exists()


def test_delete_path_stages_not_runs(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    msg = files.delete_path(str(f))
    assert msg.startswith("CONFIRM_REQUIRED")
    assert f.exists()  # not deleted yet


def test_delete_path_confirm_runs(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    files.delete_path(str(f))
    safety.confirm_action(safety._pending.id)
    assert not f.exists()


def test_delete_path_outside_root_rejected(tmp_path):
    msg = files.delete_path(str(tmp_path.parent / "x.txt"))
    assert "outside allowed roots" in msg
    assert safety._pending is None  # never staged


def test_move_path_stages_and_confirm_runs(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    msg = files.move_path(str(src), str(dst))
    assert msg.startswith("CONFIRM_REQUIRED")
    safety.confirm_action(safety._pending.id)
    assert dst.exists() and not src.exists()


def test_search_files_finds_match(tmp_path):
    (tmp_path / "report.txt").write_text("x")
    out = files.search_files("report", str(tmp_path))
    assert "report.txt" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_files.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.files'`.

- [ ] **Step 3: Implement `files.py`**

Create `friday/mcp_server/files.py`:
```python
"""File operations. Destructive ops are sandboxed to ALLOWED_ROOTS and gated."""

import os
import shutil
from pathlib import Path

from . import safety

# Destructive ops may only touch paths inside these roots. Default: user home.
ALLOWED_ROOTS = [Path.home().resolve()]


def _safe_path(raw: str) -> Path:
    """Resolve raw to a real absolute path (following symlinks) and require it to
    be inside an allowed root. Raises ValueError otherwise."""
    p = Path(raw).expanduser().resolve()
    for root in ALLOWED_ROOTS:
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise ValueError(f"path outside allowed roots: {p}")


def open_path(path: str) -> str:
    """Open a file or folder in Explorer / its default app (not sandboxed; read-only effect)."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"Path does not exist: {p}"
    os.startfile(str(p))
    return f"Opened {p}."


def search_files(query: str, directory: str | None = None) -> str:
    """Find files whose name contains query, under directory (default: home)."""
    base = Path(directory).expanduser() if directory else Path.home()
    if not base.exists():
        return f"Directory does not exist: {base}"
    matches = []
    for p in base.rglob(f"*{query}*"):
        matches.append(str(p))
        if len(matches) >= 50:
            break
    return "\n".join(matches) if matches else "No matches."


def create_folder(path: str) -> str:
    """Create a directory (within allowed roots)."""
    try:
        p = _safe_path(path)
    except ValueError as e:
        return str(e)
    p.mkdir(parents=True, exist_ok=True)
    return f"Created {p}."


def delete_path(path: str) -> str:
    """Delete a file or folder (within allowed roots). GATED."""
    try:
        p = _safe_path(path)
    except ValueError as e:
        return str(e)
    if not p.exists():
        return f"Path does not exist: {p}"

    def _do() -> str:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return f"Deleted {p}."

    return safety.stage(f"delete {p}", _do)


def move_path(src: str, dst: str) -> str:
    """Move a file or folder (both within allowed roots). GATED."""
    try:
        s = _safe_path(src)
        d = _safe_path(dst)
    except ValueError as e:
        return str(e)
    if not s.exists():
        return f"Source does not exist: {s}"

    def _do() -> str:
        shutil.move(str(s), str(d))
        return f"Moved {s} -> {d}."

    return safety.stage(f"move {s} to {d}", _do)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_files.py -q`
Expected: PASS (11 passed, or 10 passed + 1 skipped if symlinks aren't permitted).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/files.py friday/mcp_server/tests/test_files.py
git commit -m "feat: sandboxed file tools with gated delete/move"
```

---

## Task 7: Shell (`shell.py`)

Full PowerShell, always gated — the gate is the only wall (per spec §5).

**Files:**
- Create: `friday/mcp_server/shell.py`
- Test: `friday/mcp_server/tests/test_shell.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/mcp_server/tests/test_shell.py`:
```python
import pytest
from friday.mcp_server import shell, safety


@pytest.fixture(autouse=True)
def reset():
    safety._reset()
    yield
    safety._reset()


def test_run_powershell_stages_not_runs(monkeypatch):
    ran = {}
    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: ran.setdefault("ran", True))
    msg = shell.run_powershell("Get-Date")
    assert msg.startswith("CONFIRM_REQUIRED")
    assert "Get-Date" in msg
    assert "ran" not in ran


def test_run_powershell_empty_rejected():
    assert "non-empty" in shell.run_powershell("   ")


def test_run_powershell_confirm_executes(monkeypatch):
    class FakeProc:
        stdout = "hello"
        stderr = ""
        returncode = 0
    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **k: FakeProc())
    shell.run_powershell("echo hello")
    out = safety.confirm_action(safety._pending.id)
    assert "hello" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_shell.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.shell'`.

- [ ] **Step 3: Implement `shell.py`**

Create `friday/mcp_server/shell.py`:
```python
"""Run arbitrary PowerShell. Always gated; full shell, no sanitization (spec §5)."""

import subprocess

from . import safety


def run_powershell(command: str) -> str:
    """Run a PowerShell command on the machine. GATED — stages, runs on confirm."""
    if not command or not command.strip():
        return "command must be a non-empty string."

    def _do() -> str:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=60,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return out.strip() or f"(exit {proc.returncode}, no output)"

    return safety.stage(f"run PowerShell: {command}", _do)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_shell.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/shell.py friday/mcp_server/tests/test_shell.py
git commit -m "feat: gated full-shell run_powershell tool"
```

---

## Task 8: Keyboard input (`input.py`)

`type_text` strips Enter; `press_hotkey` gates danger combos, runs everyday combos direct.

**Files:**
- Create: `friday/mcp_server/input.py`
- Test: `friday/mcp_server/tests/test_input.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/mcp_server/tests/test_input.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/mcp_server/tests/test_input.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.mcp_server.input'`.

- [ ] **Step 3: Implement `input.py`**

Create `friday/mcp_server/input.py`:
```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/mcp_server/tests/test_input.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add friday/mcp_server/input.py friday/mcp_server/tests/test_input.py
git commit -m "feat: keyboard input tools with gated danger hotkeys"
```

---

## Task 9: Server entrypoint (`__main__.py`)

Build the FastMCP instance, register all 21 tools flat, serve loopback HTTP at `/mcp/`.

**Files:**
- Create: `friday/mcp_server/__main__.py`
- Test: `friday/mcp_server/tests/test_registration.py`

- [ ] **Step 1: Write the failing test (schema stability)**

Create `friday/mcp_server/tests/test_registration.py`:
```python
import pytest
from friday.mcp_server.__main__ import mcp, EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_all_expected_tools_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOL_NAMES)
    assert len(EXPECTED_TOOL_NAMES) == 21
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest friday/mcp_server/tests/test_registration.py -q`
Expected: FAIL — `ModuleNotFoundError` / cannot import `mcp` from `__main__`.

- [ ] **Step 3: Implement `__main__.py`**

Create `friday/mcp_server/__main__.py`:
```python
"""Friday tool server: one FastMCP instance, all tools registered flat, HTTP @ /mcp/."""

from fastmcp import FastMCP

from . import apps, files, shell, system
from . import input as input_tools
from . import safety

mcp = FastMCP("friday-tools")

_TOOLS = [
    apps.open_app, apps.open_url, apps.web_search, apps.close_app,
    system.set_volume, system.toggle_mute, system.set_brightness, system.media_control,
    system.lock_screen, system.sleep_pc, system.power_action,
    files.open_path, files.search_files, files.create_folder, files.delete_path, files.move_path,
    shell.run_powershell,
    input_tools.type_text, input_tools.press_hotkey,
    safety.confirm_action, safety.cancel_action,
]

for _fn in _TOOLS:
    mcp.add_tool(_fn)

EXPECTED_TOOL_NAMES = [fn.__name__ for fn in _TOOLS]

if __name__ == "__main__":
    # Loopback only — shell.py exposes a full shell. NEVER bind 0.0.0.0.
    mcp.run(transport="http", host="127.0.0.1", port=8000, path="/mcp/")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest friday/mcp_server/tests/test_registration.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Manually verify the server boots**

Run (in one terminal): `uv run python -m friday.mcp_server`
Expected: FastMCP banner; server listening on `127.0.0.1:8000`. Then Ctrl+C.
In another terminal while it runs: `uv run python -c "import socket; s=socket.create_connection(('127.0.0.1',8000),2); print('port open'); s.close()"`
Expected: prints `port open`.

- [ ] **Step 6: Commit**

```bash
git add friday/mcp_server/__main__.py friday/mcp_server/tests/test_registration.py
git commit -m "feat: FastMCP server entrypoint with flat tool registration"
```

---

## Task 10: MCP integration test (round-trip + cold-server failure)

**Files:**
- Test: `friday/mcp_server/tests/test_integration.py`

- [ ] **Step 1: Write the integration tests**

Create `friday/mcp_server/tests/test_integration.py`:
```python
import socket
import subprocess
import sys
import time

import pytest
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

URL = "http://127.0.0.1:8000/mcp/"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.3):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def server():
    proc = subprocess.Popen([sys.executable, "-m", "friday.mcp_server"])
    deadline = time.time() + 30
    while time.time() < deadline and not _port_open(8000):
        time.sleep(0.3)
    if not _port_open(8000):
        proc.terminate()
        pytest.fail("tool server did not start on port 8000")
    yield
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.asyncio
async def test_list_tools_over_http(server):
    client = MCPClient(StreamableHttpParameters(url=URL))
    await client.start()
    try:
        schema = await client.get_tools_schema()
        names = {t.name for t in schema.standard_tools}
        assert "cancel_action" in names
        assert len(schema.standard_tools) == 21
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_safe_tool_round_trip(server):
    # cancel_action has no side effects — safe to call end-to-end.
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    async with streamablehttp_client(url=URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("cancel_action", {})
            text = result.content[0].text
            assert "nothing to cancel" in text.lower()


@pytest.mark.asyncio
async def test_cold_server_start_raises_cleanly():
    # Nothing listening on 8099 -> start() must raise, not hang.
    client = MCPClient(StreamableHttpParameters(url="http://127.0.0.1:8099/mcp/"))
    with pytest.raises(Exception):
        await client.start()
```

- [ ] **Step 2: Run the integration tests**

Run: `uv run pytest friday/mcp_server/tests/test_integration.py -q`
Expected: PASS (3 passed). The server fixture boots a real process and tears it down.

- [ ] **Step 3: Run the whole suite**

Run: `uv run pytest -q`
Expected: all tests pass (safety, apps, system, files, shell, input, registration, integration).

- [ ] **Step 4: Commit**

```bash
git add friday/mcp_server/tests/test_integration.py
git commit -m "test: MCP HTTP integration (round-trip, schema, cold-server)"
```

---

## Task 11: Wire tools into the voice app (`main.py` + `.env`)

**Files:**
- Modify: `friday/main.py`
- Modify: `friday/.env`

- [ ] **Step 1: Update `MCP_URL` in `friday/.env`**

Change the line:
```
MCP_URL=http://127.0.0.1:8000/sse
```
to:
```
MCP_URL=http://127.0.0.1:8000/mcp/
```

- [ ] **Step 2: Add imports to `friday/main.py`**

In the import block of `friday/main.py`, after the existing pipecat imports, add:
```python
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient
```

- [ ] **Step 3: Add the output-truncation helper**

In `friday/main.py`, after `SYSTEM_PROMPT` is defined, add:
```python
def _trunc(result):
    """Cap noisy tool output before it reaches the LLM context."""
    s = result if isinstance(result, str) else str(result)
    return s if len(s) <= 2000 else s[:2000] + " …(truncated)"
```

- [ ] **Step 4: Extend SYSTEM_PROMPT with the confirmation convention**

Replace the `SYSTEM_PROMPT` assignment in `friday/main.py` with:
```python
SYSTEM_PROMPT = (
    "You are Friday, a local voice assistant. Reply in one short sentence only — "
    "never more. Be direct. You are spoken aloud, so never use markdown, bullet "
    "points, code blocks, or emoji. You can control this computer with tools. If a "
    "tool reply starts with CONFIRM_REQUIRED, read the action aloud and ask the user "
    "to say confirm or cancel; when they confirm, call confirm_action with the id "
    "shown in brackets; if they decline, call cancel_action."
)
```

- [ ] **Step 5: Wire MCPClient into `main()`**

In `friday/main.py`, the LLM is currently created and the context built like this:
```python
    llm = OpenRouterLLMService(
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=os.getenv("LLM_MODEL", "nex-agi/nex-n2-pro:free"),
    )

    tts = KokoroTTSService(voice="am_michael")

    context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}])
```
Replace that block with:
```python
    llm = OpenRouterLLMService(
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=os.getenv("LLM_MODEL", "nex-agi/nex-n2-pro:free"),
    )

    tts = KokoroTTSService(voice="am_michael")

    # Connect to the standalone tool server and register its tools on the LLM.
    # The server must already be running (run_friday.ps1 guarantees ordering).
    mcp = MCPClient(
        StreamableHttpParameters(url=os.environ["MCP_URL"]),
        tools_output_filters={"run_powershell": _trunc, "search_files": _trunc},
    )
    await mcp.start()
    tools = await mcp.register_tools(llm)

    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=tools,
    )
```

- [ ] **Step 6: Close the MCP session on shutdown**

In `friday/main.py`, the run currently looks like:
```python
    runner = PipelineRunner(handle_sigint=True)
    logger.info("Friday online. Hold SPACE, speak, release. Ctrl+C to quit.")
    await runner.run(task)
```
Replace with:
```python
    runner = PipelineRunner(handle_sigint=True)
    logger.info("Friday online. Hold SPACE, speak, release. Ctrl+C to quit.")
    try:
        await runner.run(task)
    finally:
        await mcp.close()  # session must live the whole app; close only on shutdown
```

- [ ] **Step 7: Verify the voice app imports cleanly**

Run: `uv run python -c "import ast; ast.parse(open('friday/main.py', encoding='utf-8').read()); print('syntax ok')"`
Expected: prints `syntax ok`.

- [ ] **Step 8: Commit**

```bash
git add friday/main.py friday/.env
git commit -m "feat: wire MCP tool server into the voice app"
```

Note: `friday/.env` is gitignored; the commit records only `main.py`. Apply the `.env` edit manually (Step 1).

---

## Task 12: Launch script (`run_friday.ps1`)

**Files:**
- Create: `run_friday.ps1`

- [ ] **Step 1: Implement the launcher**

Create `run_friday.ps1` (project root):
```powershell
# Start the Friday tool server, wait until it is listening, then launch the voice app.
$ErrorActionPreference = "Stop"

Write-Host "Starting Friday tool server..."
$server = Start-Process -FilePath "uv" `
    -ArgumentList "run", "python", "-m", "friday.mcp_server" `
    -PassThru -NoNewWindow

try {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 300
        $up = Test-NetConnection 127.0.0.1 -Port 8000 -InformationLevel Quiet -WarningAction SilentlyContinue
    } until ($up -or (Get-Date) -gt $deadline)

    if (-not $up) { throw "Tool server did not open port 8000 within 30s." }

    Write-Host "Tool server is up. Launching voice app (hold SPACE to talk)..."
    uv run python -m friday.main
}
finally {
    Write-Host "Shutting down tool server..."
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
    }
}
```

- [ ] **Step 2: Manual end-to-end smoke (logged → regression checklist)**

Run: `./run_friday.ps1`
Then hold SPACE and exercise one tool per module; record pass/fail:
- apps: "open notepad"
- system: "set volume to 30"
- files: "search for <a filename> in my documents"
- shell: "run power shell get-date" → Friday asks to confirm → say "confirm"
- input: "type hello" (focus a text field first)
- gate: "delete <a temp file>" → confirm flow → file gone

Expected: each acts correctly; risky ones ask first. Note any failures for follow-up.

- [ ] **Step 3: Commit**

```bash
git add run_friday.ps1
git commit -m "feat: run_friday.ps1 launches server (port-gated) then voice app"
```

---

## Self-Review

**Spec coverage:**
- §2 architecture (two processes, HTTP, loopback, flat registration) → Tasks 9, 12. ✓
- §3 tool catalog (21 tools) → Tasks 4–8 (19 action) + Task 2 (confirm/cancel) = 21, asserted in Task 9. ✓
- §4 safety gate (single slot, targeted confirm, armed, 90s expiry) → Task 2 tests. ✓
- §5 arg safety (file sandbox escape, full-shell-gated, input gating, clean errors) → Tasks 6, 7, 8. ✓
- §6 voice-app wiring (start/register/close, tools_output_filters, LLMContext tools) → Task 11. ✓
- §7 startup ordering (port poll, not HTTP GET) → Task 12. ✓
- §8 testing (unit, security, gate, integration round-trip, cold-server, schema count, filter) → Tasks 2–10. ✓
- §9 deps/files → Task 1 + each task. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every command has expected output. ✓

**Type/name consistency:** `safety.stage`/`confirm_action`/`cancel_action`/`_reset`/`_pending` used consistently across Tasks 2,5,6,7,8,9. `_safe_path`/`ALLOWED_ROOTS` consistent in Task 6. `_do_hotkey`/`kbd` consistent in Tasks 3,8. `EXPECTED_TOOL_NAMES`/`mcp` consistent in Tasks 9,10. `_trunc` defined and referenced in Task 11. ✓

**Gap fixed during review:** `_trunc` filter keys (`run_powershell`, `search_files`) match the tool names registered in Task 9. The integration round-trip uses `cancel_action` (side-effect-free) rather than `set_volume`, avoiding real volume changes during tests.
