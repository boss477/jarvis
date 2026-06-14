# Friday — Computer Control via MCP Tools (Design)

**Date:** 2026-06-15
**Status:** Approved design, pre-implementation
**Scope:** Give Friday (local push-to-talk voice assistant) the ability to control the
laptop — open apps/websites, system actions, file operations, raw shell, and keyboard
input — through a standalone FastMCP tool server that Pipecat connects to as an MCP client.

---

## 1. Goal & Non-Goals

**Goal:** Friday can perform real actions on the Windows machine by voice, with a
server-side confirmation gate protecting destructive operations.

**Non-goals (explicitly deferred to a later phase):**
- Mouse move/click automation.
- Screenshot + vision (requires a multimodal LLM; current free text model cannot see).
- Open Interpreter / arbitrary code-writing escape-hatch tool.
- Authentication on the tool server (local-only, loopback-bound).

---

## 2. Architecture

```
┌─ Voice app (existing) ───────────────┐        ┌─ Tool server (NEW) ──────────┐
│ mic → PTT → Whisper → LLM ──tools──► │  HTTP  │ friday/mcp_server/           │
│                   ▲          MCPClient│◄──────►│ FastMCP @ 127.0.0.1:8000/mcp/│
│                   └── Kokoro ◄─ result │        │  21 tools, flat-registered  │
└──────────────────────────────────────┘        └──────────────────────────────┘
   run: uv run python -m friday.main                run: uv run python -m friday.mcp_server
```

- **Two processes.** The tool server runs independently so it can be hot-reloaded
  while the voice app keeps running (valuable while iterating on the tool set). localhost
  HTTP latency is negligible.
- **Transport:** streamable HTTP, bound to `127.0.0.1` only (**never `0.0.0.0`**),
  no auth (local single-machine use). `shell.py` exposes a full shell, so the loopback
  bind is a hard requirement, not a preference.
- **Tool registration:** one `FastMCP` instance; tool functions imported from four
  domain modules and registered flat in `__main__.py`. No `mcp.mount()` / sub-servers
  yet — add only if domains grow.

---

## 3. Tool Catalog (19 action tools + 2 safety = 21)

| Module | Tool | Behaviour | Gate? |
|---|---|---|---|
| **apps.py** | `open_app(name)` | Launch app by friendly name ("chrome", "spotify", "vs code") | — |
| | `open_url(url)` | Open URL in default browser | — |
| | `web_search(query)` | Open browser to a search for the query | — |
| | `close_app(name)` | Terminate app by name | — |
| **system.py** | `set_volume(level)` | Set system volume 0–100 | — |
| | `toggle_mute()` | Mute / unmute | — |
| | `set_brightness(level)` | Set display brightness 0–100 | — |
| | `media_control(action)` | play_pause / next / previous | — |
| | `lock_screen()` | Lock Windows | — |
| | `sleep_pc()` | Sleep | — |
| | `power_action(mode)` | mode = restart \| shutdown | ⚠️ |
| **files.py** | `open_path(path)` | Open file/folder in Explorer/default app | — |
| | `search_files(query, dir)` | Find files by name (output truncated) | — |
| | `create_folder(path)` | Make a directory (within allowed roots) | — |
| | `delete_path(path)` | Delete file/folder (within allowed roots) | ⚠️ |
| | `move_path(src, dst)` | Move file/folder (within allowed roots) | ⚠️ |
| **shell.py** | `run_powershell(command)` | Run arbitrary PowerShell (output truncated ~2000 chars) | ⚠️ |
| **input.py** | `type_text(text)` | Simulate typing — Enter/newlines stripped, can't submit a command alone (pynput) | — |
| | `press_hotkey(combo)` | Simulate a hotkey, e.g. "ctrl+c" (pynput) | ⚠️ danger combos only |
| **(safety)** | `confirm_action(action_id)` | Execute a staged risky action | — |
| | `cancel_action()` | Discard the pending staged action | — |

`⚠️` tools route through the safety gate (§4) and **never execute on first call**.

---

## 4. Safety / Confirmation Gate (server-side)

The gate lives entirely in the tool server, independent of the LLM. Even a misbehaving
or hallucinating LLM cannot run a destructive op without an explicit, correctly-targeted
second call.

**State:** a single pending slot:
```
PendingAction = { id: str, summary: str, fn: Callable[[], str], created_at: float }
```
Only one pending action at a time. Staging a new action **replaces** any existing one.

**Flow:**
1. A `⚠️` tool validates its args (see §5 sandbox rules), then calls
   `_stage(summary, fn)` instead of executing. `_stage` stores the pending slot and
   returns `"CONFIRM_REQUIRED [id=<id>]: <summary>"`.
2. The LLM relays the summary aloud — *"You want me to delete report.pdf — say confirm or cancel."*
3. User says "confirm" → LLM calls `confirm_action(action_id=<id>)`. The server:
   - rejects cleanly if no pending action, if `action_id` ≠ pending id, or if expired;
   - otherwise pops the slot, runs `fn()`, returns its result.
4. User says "cancel" → `cancel_action()` clears the slot.

**Lifecycle rules (defined, and tested):**
- **Targeted confirm:** `confirm_action` requires the `action_id` returned at stage time.
  "Confirm the latest" is *not* allowed — removes voice ambiguity (stage delete, say
  something unrelated, then "yes" → "yes" only fires if it carries the right id).
- **Stays armed:** an unrelated command does **not** clear the pending slot. It remains
  armed until explicit `confirm_action` / `cancel_action` / expiry.
- **Expiry:** 90 seconds from `created_at`. A confirm at >90s is rejected. (90, not 60,
  because the voice round-trip between stage and confirm — Kokoro speaks the summary, user
  thinks, Whisper transcribes "confirm", LLM emits the call — can eat 10–20s; 60s left too
  little slack for a brief pause.)

---

## 5. Argument Safety (the wall the gate can't be)

The confirm gate protects *intent*, not *bad arguments* — a misheard path or a
hallucinated arg passes the gate. Argument validation is therefore a separate wall,
applied **before** staging.

- **File ops (`delete_path`, `move_path`, `create_folder`):** every path is resolved to
  its real absolute form (`Path.resolve()`, resolving symlinks) and must fall within a
  configurable **allowed-roots** list (default: the user's home directory, which already
  covers Desktop/Documents/Downloads). Anything outside — `../../../` traversal, absolute
  path outside roots, symlink escape — is rejected before staging.
- **`run_powershell`:** **decision — full shell, no sanitization.** PowerShell cannot be
  meaningfully sanitized (it is a complete language; `;`/`&&`/`|`/`Invoke-Expression`/
  encoded commands all chain). We do not pretend to. The protection is the gate:
  `run_powershell` always stages + reads back the exact command, and only the explicit
  `confirm_action(id)` runs it.
- **Input tools (`press_hotkey`, `type_text`):** **decision — gate danger combos only.**
  Input simulation can otherwise walk around the whole gate (`win+r` then type a command
  into the Run box; typing + Enter into a focused terminal). Two walls:
  - `press_hotkey` checks the combo against a **danger denylist** — `win+r`, `alt+f4`,
    `ctrl+w`, `win+e` (extendable). A denylisted combo routes through the confirm gate
    (stage + read-back + `confirm_action`); everyday combos (`ctrl+c`/`v`/`s`, etc.) run
    direct to preserve the fast feel.
  - `type_text` **strips Enter / newline / carriage-return** from its input, so it types
    literal text only and can never submit a command or Run-box entry on its own.
  - Residual risk (accepted): typing literal text into whatever window happens to be
    focused. Bounded because submission still requires a separate, denylist-checked key.
- **All tools:** malformed / out-of-range / garbage args are rejected with a clean error
  string, never an unhandled exception (a crash would kill the tool call mid-conversation).

---

## 6. Voice-app Wiring (`friday/main.py`)

```python
from mcp.client.session_group import StreamableHttpParameters
from pipecat.services.mcp_service import MCPClient

# ... inside async def main(), before building the pipeline:
mcp = MCPClient(
    StreamableHttpParameters(url=os.environ["MCP_URL"]),  # ".../mcp/" trailing slash
    tools_filter=[...],                                   # whitelist of registered tools
    tools_output_filters={                               # truncate noisy output
        "run_powershell": _trunc,
        "search_files": _trunc,
    },
)
await mcp.start()                       # raises loudly if the server is cold
tools = await mcp.register_tools(llm)   # registers handlers on llm, returns ToolsSchema
context = LLMContext(messages=[{"role": "system", "content": SYSTEM_PROMPT}], tools=tools)
# ... build pipeline / run task ...
# in finally / shutdown:
await mcp.close()                       # session must live the whole app; closing early kills tools mid-convo
```

Verified against installed Pipecat 1.3.0:
- `MCPClient(server_params, tools_filter=..., tools_output_filters=...)` — mcp_service.py:54.
- `register_tools(llm)` registers handlers **and** returns `ToolsSchema` — mcp_service.py:146.
- `LLMContext(messages=..., tools=ToolsSchema, ...)` — llm_context.py:101.
- `StreamableHttpParameters` import path — mcp_service.py:25.

`MCP_URL` in `friday/.env` changes from `http://127.0.0.1:8000/sse` →
`http://127.0.0.1:8000/mcp/` (trailing slash required).

---

## 7. Startup Ordering — `run_friday.ps1`

The MCPClient connect fails if the server is cold, so order matters:
1. Start the tool server in the background.
2. **Poll the TCP port, not an HTTP GET.** The streamable-HTTP `/mcp/` endpoint rejects a
   bare GET (it requires `Accept: application/json, text/event-stream` + the MCP
   `initialize` handshake), so `Invoke-WebRequest http://127.0.0.1:8000/mcp/` returns
   400/406 even when the server is healthy — an HTTP-200 readiness check would never fire.
   Readiness probe = **port open**:
   ```powershell
   do { Start-Sleep -Milliseconds 300 }
   until ((Test-NetConnection 127.0.0.1 -Port 8000 -InformationLevel Quiet))
   ```
   (Add a timeout so a failed server doesn't loop forever.) The port being bound proves the
   server is up; `MCPClient.start()` then performs the real MCP handshake — and the §8
   failure-path test asserts that a genuinely-cold server makes `start()` raise cleanly
   rather than hang.
3. Then launch the voice app.
4. Ctrl+C tears down both. The server can be left running across voice-app restarts to
   hot-reload tools.

---

## 8. Testing

**Tool unit tests (pure Python, no machine side effects):**
- Each tool's happy path; destructive ops run against temp dirs or mocks — never the real machine.
- `⚠️` tools assert they **stage** (return `CONFIRM_REQUIRED`) rather than execute on first call.

**Argument-safety / security tests (the biggest area):**
- `delete_path` / `move_path` / `create_folder`: assert sandbox-escape rejection for
  `../../../`, absolute path outside roots, and symlink escape.
- `run_powershell`: assert it always stages (full-shell decision) — the gate is the wall.
- `press_hotkey`: denylisted combos (`win+r`, `alt+f4`, `ctrl+w`, `win+e`) **stage**;
  everyday combos (`ctrl+c`) run direct. Case/spacing/order-insensitive match
  (`Win+R`, `r+win`) still caught.
- `type_text`: Enter/newline/carriage-return stripped from input (the stripped result
  contains no `\n`/`\r`), so it cannot submit on its own.
- Every tool: malformed/garbage args rejected with a clean error, no crash.

**Safety-gate tests:**
- stage → `confirm_action(correct id)` runs it; result returned.
- stage → `cancel_action()` discards; later confirm rejected.
- `confirm_action(wrong id)` rejected; pending slot untouched.
- unrelated call between stage and confirm leaves slot **armed** (defined behaviour).
- second stage **replaces** the first (single-slot).
- **expiry boundary:** confirm at 89s accepted, 90s/91s rejected (off-by-one guard).

**Integration:**
- start server → connect client → assert tool list and one safe round-trip (`set_volume`).
- **failure path:** `MCPClient.start()` against a cold server raises a clean error
  (asserted), not a silent hang.
- **schema stability:** registered tool count == expected (19 action + 2 safety = 21) and
  names match a fixed list — catches accidental unregister on module refactor.
- **filter test:** `_trunc` caps long output AND passes short input through unchanged
  (no index error on short strings).

**Manual end-to-end (logged → becomes a regression checklist):**
- One tool per module exercised live over PTT; record pass/fail per tool.

---

## 9. Dependencies & Files

**New files:**
- `friday/mcp_server/__init__.py`
- `friday/mcp_server/__main__.py` — FastMCP instance, flat registration, HTTP serve.
- `friday/mcp_server/apps.py`, `system.py`, `files.py`, `shell.py`, `input.py`
- `friday/mcp_server/safety.py` — pending-slot gate, `confirm_action`, `cancel_action`.
- `run_friday.ps1`
- tests under `friday/mcp_server/tests/` (or project test dir).

**Modified files:**
- `friday/main.py` — MCPClient start/register/close wiring.
- `friday/.env` — `MCP_URL` trailing-slash + transport.

**Dependencies:** `fastmcp` (installed). System-control helpers may need a small lib for
volume/brightness on Windows (e.g. `pycaw` for volume, `screen-brightness-control` for
brightness) or pure PowerShell shell-outs — to be settled in the implementation plan.
`pynput` (installed) covers `type_text` / `press_hotkey`.

---

## 10. Open Items for the Implementation Plan
- Exact app-name → executable map for `open_app` (and a sane fallback via `start`).
- Whether system controls use helper libs (`pycaw`, `screen-brightness-control`) or
  PowerShell shell-outs — pick the lib only where PowerShell is unreliable.
- Final `tools_filter` whitelist contents.
- Default allowed-roots list value.
```
