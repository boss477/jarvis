# Friday — Screen Vision (Design)

**Date:** 2026-06-15
**Status:** Approved design, pre-implementation
**Scope:** Let Friday capture the screen on demand and reason over it with the local
vision model (`qwen/qwen3.5-9b` via LM Studio). Triggered by a dedicated push-to-talk
hotkey; the screenshot is attached to that one turn and sent to the LLM in a single call.

This is the first of two planned phases (vision now; tools-expansion later, separate spec).

---

## 1. Goal & Non-Goals

**Goal:** Hold a dedicated key, ask a question, and Friday answers about what's currently
on screen — one LLM call, lowest latency, no effect on normal voice turns.

**Verified feasibility (2026-06-15):** a real screenshot sent to `qwen/qwen3.5-9b` via
LM Studio returned an accurate description (it identified the LM Studio UI and the model
on screen). Pipecat's `LLMContext.create_image_url_message` produces the exact OpenAI
multimodal format the endpoint accepts.

**Non-goals (deferred):**
- Multi-monitor capture / active-window-only capture (v1 = primary monitor full screen).
- Vision as an MCP tool (Pipecat's MCP layer has no image handling — vision is Pipecat-side).
- Continuous/always-on screen watching (on-demand only, for latency).
- Tools-expansion (separate phase/spec).

---

## 2. Architecture

A vision turn is a normal push-to-talk turn on its own key, with a screenshot attached
to that turn's user message.

```
transport.input()
  → ptt            (SPACE; also CLEARS the vision flag on press)
  → vision_ptt     (VISION key; on press: capture screen + SET flag, emit speaking frames)
  → stt
  → agg.user()
  → vision_injector (if flag set: attach image to the user message, CLEAR flag)
  → llm → tts → transport.output() → agg.assistant()
```

**Why two processors:** the key listener + speaking frames must run *before* STT (to drive
transcription), but the image must be attached *after* `agg.user()` finalizes the user
message. Different pipeline positions → two focused units sharing one small state object.

---

## 3. Components (each one responsibility)

**`friday/services/vision_capture.py`** — pure capture util.
- `async def capture_screen_data_url() -> str`: `ImageGrab.grab()` → `thumbnail((1024,1024))`
  → JPEG quality 70 → `f"data:image/jpeg;base64,{b64}"`. Pixel work runs via
  `asyncio.to_thread`. (~60 KB payload, proven against LM Studio.)
- Constants `MAX_DIM = 1024`, `JPEG_QUALITY = 70` (proven values; not env knobs — YAGNI).

**`friday/services/vision.py`** — three small pieces:
- `VisionState`: a dataclass with `pending_url: str | None = None`. Shared by all parties.
- `VisionPTTProcessor(FrameProcessor)`: pynput listener on the vision hotkey.
  - On press (not already active): `state.pending_url = None` (re-arm), then capture →
    `state.pending_url = <data url>`; emit `UserStartedSpeakingFrame` +
    `VADUserStartedSpeakingFrame` (mic opens, mirrors PTT).
  - On release: emit `VADUserStoppedSpeakingFrame` + `UserStoppedSpeakingFrame`.
  - Other keys ignored. Listener started on `StartFrame`, stopped in `cleanup()`.
- `VisionContextInjector(FrameProcessor)`: placed after `agg.user()`.
  - On the context frame that triggers the LLM: if `state.pending_url` is set **and** the
    last context message is a `role == "user"` message, replace that message with
    `LLMContext.create_image_url_message(url=state.pending_url, text=<original text>)`,
    then `state.pending_url = None`. Otherwise pass through untouched.

**Modify `friday/services/ptt.py`** — accept an optional `VisionState`; on SPACE press,
set `state.pending_url = None` (the stale-flag leak guard). One added line + constructor arg.

**Modify `friday/main.py`** — build one `VisionState`; pass it to `PushToTalkProcessor` and
both vision processors; insert `vision_ptt` after `ptt` (before `stt`) and `vision_injector`
after `agg.user()` (before `llm`); read `VISION_KEY` from env.

---

## 4. Capture timing — on PRESS (race fix)

The screenshot is captured on **key press**, not release. Rationale:
- At press time the screen already shows what the user is asking about.
- Press-capture gives the entire speaking duration for the off-loop capture to finish, so
  `pending_url` is reliably set before STT → aggregation → the injector runs. Capturing on
  *release* would race STT and could miss the turn (image dropped or attached to the next turn).

Trade-off (accepted): pixels are from press-time, not release. Fine — the user asks about
what they're looking at when they start, and they're speaking (not changing the screen) in between.

---

## 5. Stale-flag lifecycle (defined + tested)

`pending_url` must never leak a screenshot onto the wrong turn.

- **Set:** on vision-key press (after capture completes).
- **Cleared:**
  1. by `VisionContextInjector` immediately after attaching (consumed), **and**
  2. at the **start of every turn** — both `PushToTalkProcessor` (SPACE) and
     `VisionPTTProcessor` set `pending_url = None` on press before doing anything else.
- **Injector guard:** attaches only when `pending_url` is set *and* the last message is a
  user-role message; otherwise passes through.

Outcomes:
- Normal SPACE turn → flag cleared at press → injector sees `None` → unaffected.
- Vision turn → cleared then set at press → injector attaches the fresh image.
- Dead vision turn (empty STT, no context frame) → no injection happens, and the next press
  (vision or normal) wipes the stale flag before its turn.

---

## 6. Config & Error Handling

- `VISION_KEY` in `friday/.env` (default `ctrl_r` = Right Ctrl). Parsed to a
  `pynput.keyboard.Key`. A modifier is the default because a printable key would type into
  the focused window (pynput is global).
- **Capture failure** (locked screen, headless, etc.): log a warning, ensure
  `pending_url` is cleared, and let the turn proceed as a normal text turn. Vision never
  crashes a turn.

**Latency:** only vision turns pay the image cost (one LLM call, ~60 KB image). Normal
turns are byte-for-byte unchanged.

---

## 7. Testing

**Unit (no real screen — `ImageGrab` mocked):**
- `vision_capture`: returns `data:image/jpeg;base64,…`; the decoded bytes are a valid JPEG
  (magic `\xff\xd8`); payload under a size budget; downscale invoked.
- `VisionPTTProcessor`: vision-key press → capture called + `pending_url` set + speaking-start
  frames emitted; release → speaking-stop frames; unrelated keys ignored.
- `VisionContextInjector`: with `pending_url` and a trailing user text message → message
  rewritten to multimodal (text + image_url) and flag cleared; with no flag → untouched;
  with a non-user last message → untouched (flag left for the press-guard to clear).
- `ptt.py`: SPACE press sets `pending_url = None` (leak guard).
- **Format check:** the injected message equals
  `{"role":"user","content":[{"type":"text",...},{"type":"image_url",...}]}` — the schema
  LM Studio accepts.

**Manual e2e (logged → regression checklist):**
- Hold Right Ctrl, "what's on my screen?" → accurate description.
- A normal SPACE turn immediately after → no image attached.
- Hold Right Ctrl, release without speaking (empty STT) → next SPACE turn unaffected.

---

## 8. Files

**New:**
- `friday/services/vision_capture.py`
- `friday/services/vision.py` (`VisionState`, `VisionPTTProcessor`, `VisionContextInjector`)
- tests: `friday/mcp_server/tests/` is MCP-only; vision tests live in a new
  `friday/tests/` (with `__init__.py`), and `pyproject.toml` `testpaths` extended to include it.

**Modified:**
- `friday/services/ptt.py` — `VisionState` arg + clear-on-press.
- `friday/main.py` — wire processors + shared state + `VISION_KEY`.
- `friday/.env` — `VISION_KEY=ctrl_r`.
- `pyproject.toml` — add `friday/tests` to `testpaths`.

**Dependencies:** Pillow (already installed, used by Kokoro/pipecat). No new deps.

---

## 9. Open Items for the Implementation Plan
- Exact context-frame type emitted by `agg.user()` that the injector keys on (confirm the
  class in Pipecat 1.3 — `LLMContextFrame`/`OpenAILLMContextFrame` — during the plan).
- `VISION_KEY` string → `pynput.keyboard.Key` parsing (map a few friendly names; default `ctrl_r`).
- Whether `vision_ptt` and `ptt` running two separate pynput listeners is preferable to one
  shared listener (default: two, mirroring the existing per-processor pattern).
