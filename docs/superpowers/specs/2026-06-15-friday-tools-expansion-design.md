# Friday — Tools Expansion (Phase 2) Design

**Date:** 2026-06-15
**Status:** Approved design, pre-implementation
**Scope:** Add window control + more app mappings to Friday's existing FastMCP tool
server, and make `run_powershell` the explicit "do anything" escape-hatch. All additions
to the existing server; **no new dependencies** (window control uses stdlib `ctypes`).

This is the second of the two planned tool phases (vision shipped; this is tools-expansion).

---

## 1. Goal & Non-Goals

**Goal:** Friday can manage open windows (list/focus/minimize/maximize) by voice, launch
more apps by friendly name, and accomplish arbitrary tasks through the existing gated shell.

**Decision (from brainstorming):** **No Open Interpreter.** The existing gated
`run_powershell` already lets the LLM run any command, so an OI second-agent loop would be
redundant, heavy, and slow on this 8 GB / high-latency setup. The escape-hatch is
`run_powershell` itself.

**Non-goals (deferred):**
- `close_window` (the existing `close_app` already terminates apps).
- Moving/resizing/positioning windows, virtual desktops.
- Open Interpreter / any code-writing agent loop.
- Multi-monitor-aware window targeting.

**Verified feasibility (2026-06-15):** `ctypes` + `user32.dll` enumerated 13 real visible
windows (VS Code, Chrome, LM Studio, …) with no dependency. `pygetwindow` was rejected: it
has no wheel and the source build is blocked by this machine's Windows Application Control
policy (os error 4551) — so a stdlib `ctypes` approach is required, not just preferred.

---

## 2. Architecture

All changes live inside the existing `friday/mcp_server/` package; tools are registered
flat in `__main__.py` exactly like the current ones, and reach the LLM through the same
Pipecat `MCPClient`. No transport, port, or pipeline changes.

```
friday/mcp_server/
  windows.py   (NEW)  list/focus/minimize/maximize via ctypes+user32
  apps.py      (EDIT) expand APP_MAP
  shell.py     (EDIT) run_powershell timeout 60 -> 120s
  __main__.py  (EDIT) register the 4 new window tools  (21 -> 25 tools)
friday/main.py (EDIT) SYSTEM_PROMPT: note run_powershell is the do-anything escape-hatch
```

---

## 3. Window control — `friday/mcp_server/windows.py`

Pure stdlib `ctypes`. Window-handle (`hwnd`) lookups are case-insensitive substring matches
on the visible window title; the first match wins.

**Internal helpers:**
- `_enumerate() -> list[tuple[int, str]]`: `EnumWindows` callback collecting `(hwnd, title)`
  for windows where `IsWindowVisible(hwnd)` and the title is non-empty.
- `_find(title: str) -> tuple[int | None, str | None]`: first `(hwnd, title)` whose title
  contains `title` (case-insensitive); `(None, None)` if no match.

**Tools (all non-destructive → no confirm gate):**
| Tool | Behaviour | user32 call |
|---|---|---|
| `list_windows()` | Newline-joined visible window titles (or "No visible windows.") | enumerate |
| `focus_window(title)` | `ShowWindow(hwnd, SW_RESTORE)` then `SetForegroundWindow(hwnd)`; **honest about the result** (see note) | restore + foreground |
| `minimize_window(title)` | `ShowWindow(hwnd, SW_MINIMIZE)` | minimize |
| `maximize_window(title)` | `ShowWindow(hwnd, SW_MAXIMIZE)` | maximize |

Constants: `SW_MAXIMIZE = 3`, `SW_MINIMIZE = 6`, `SW_RESTORE = 9`.
No-match → returns `"No window matching '<title>'."` (clean string, never raises).

**`focus_window` honesty:** Windows often blocks foreground-steal, so
`SetForegroundWindow` can return 0 (failure) even though the call "ran". `focus_window`
checks the return value: success → `"Focused '<title>'."`; failure → `"Brought '<title>'
forward; you may need to click it."` — never reports a focus that didn't happen. (The
common `AttachThreadInput` workaround is deferred; the honest message is enough for v1.)
`list_windows` output is truncated client-side by the existing `_trunc` filter (added to
`tools_output_filters` in `main.py`).

---

## 4. App mappings — `friday/mcp_server/apps.py`

Expand `APP_MAP` with more common spoken names (e.g. `discord`, `spotify` (present),
`word`→`winword`, `excel`→`excel`, `settings`→`ms-settings:`, `terminal`→`wt`,
`task manager`→`taskmgr`, `files`/`explorer` (present)). `open_app` already falls back to
`cmd /c start "" <name>` for unmapped names, so this is convenience only — no behaviour
change, no new tool.

---

## 5. Escape-hatch — `shell.py` + `SYSTEM_PROMPT`

- `shell.py`: raise the `subprocess.run(..., timeout=60)` to `timeout=120` so longer
  one-off tasks (installs, multi-step commands) don't time out. Still gated, still
  full-shell, output still client-truncated.
- `friday/main.py` `SYSTEM_PROMPT`: append one sentence — that for any task without a
  dedicated tool, Friday can accomplish it by writing a single PowerShell command and
  calling `run_powershell` (which will ask the user to confirm before running).

---

## 6. Registration & tool count

`__main__.py` imports `windows` and adds the 4 tools to the flat `_TOOLS` list. Expected
schema count goes from **21 to 25** (23 action + 2 safety). The existing
`test_registration.py` count assertion is updated to 25 and the 4 names added to the
expected set.

---

## 7. Testing

**Unit (no real windows — `_enumerate` and `user32` mocked):**
- `_find`: case-insensitive substring match returns the right `(hwnd, title)`; no match → `(None, None)`.
- `focus_window`: on a match, calls `ShowWindow(hwnd, SW_RESTORE)` and `SetForegroundWindow(hwnd)`; returns `"Focused '<title>'."`. No match → `"No window matching ..."`.
- `minimize_window` / `maximize_window`: call `ShowWindow` with `SW_MINIMIZE` / `SW_MAXIMIZE` on the matched hwnd.
- `list_windows`: joins titles from `_enumerate`; empty → "No visible windows."
- All tools: missing/empty title or no match returns a clean string, never raises.

**Registration:** `__main__` registers exactly 25 tools; names match the expected fixed set (existing 21 + `list_windows`, `focus_window`, `minimize_window`, `maximize_window`).

**App mappings:** a couple of new `APP_MAP` entries resolve to the expected command (mock `subprocess.Popen`, assert the target).

**Manual e2e (logged → regression checklist):**
- "List my open windows" → reads back titles.
- "Focus Chrome" → Chrome comes to front.
- "Minimize VS Code" / "Maximize Chrome" → correct window state.
- "Open Spotify" (new mapping) → launches.

---

## 8. Files

**New:**
- `friday/mcp_server/windows.py`
- `friday/mcp_server/tests/test_windows.py`

**Modified:**
- `friday/mcp_server/apps.py` — expand `APP_MAP`.
- `friday/mcp_server/shell.py` — timeout 60 → 120.
- `friday/mcp_server/__main__.py` — register 4 window tools.
- `friday/mcp_server/tests/test_registration.py` — count 21 → 25 + new names.
- `friday/main.py` — `SYSTEM_PROMPT` escape-hatch sentence; add `list_windows` to `_trunc` output filters.

**Dependencies:** none (stdlib `ctypes`).

---

## 9. Open Items for the Implementation Plan
- Exact `ctypes` signatures/argtypes for `EnumWindows`/`GetWindowTextW` on 64-bit Python
  (set `restype`/`argtypes` to avoid pointer-truncation of `hwnd` on Win64) — verify in the
  plan's first task.
- Final `APP_MAP` additions list.
- Substring match ambiguity: "chrome" may match several Chrome windows and first-match
  wins, while `list_windows` output is client-truncated so the user can't always see the
  full candidate list. Accepted for v1 (first-match); revisit if it proves annoying.
