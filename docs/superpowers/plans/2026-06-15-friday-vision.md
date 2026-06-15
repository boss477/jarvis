# Friday Screen Vision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Friday capture the screen on a dedicated push-to-talk hotkey and answer about it with the local vision model (`qwen/qwen3.5-9b`), attaching the screenshot to that one turn.

**Architecture:** A `VisionPTTProcessor` (own pynput key, before STT) emits speaking frames immediately on press, then captures the screen in the background into a shared `VisionState`. A `VisionContextInjector` (after the user aggregator) rewrites that turn's user message to multimodal via `LLMContext.create_image_url_message` when an image is pending. The existing `PushToTalkProcessor` clears the pending flag on SPACE press so images never leak onto normal turns.

**Tech Stack:** Python 3.11, Pipecat 1.3 (`FrameProcessor`, `LLMContextFrame`, `LLMContext`), Pillow `ImageGrab`, pynput, pytest.

**Spec:** `docs/superpowers/specs/2026-06-15-friday-vision-design.md`

**Pre-verified load-bearing APIs (do not re-guess):**
- Trigger frame: `from pipecat.frames.frames import LLMContextFrame`; constructed as `LLMContextFrame(context=...)`; the injector reads `frame.context`.
- Multimodal builder: `LLMContext.create_image_url_message(*, role="user", url, text=None)` → `{"role":..,"content":[{"type":"text",..},{"type":"image_url","image_url":{"url":..}}]}` (static method).

---

## File Structure

**New:**
- `friday/services/vision_capture.py` — screen → `data:` URL util (one job: pixels).
- `friday/services/vision.py` — `VisionState`, `resolve_vision_key`, `inject_pending_image`, `VisionPTTProcessor`, `VisionContextInjector`.
- `friday/tests/__init__.py`, `friday/tests/test_vision_capture.py`, `friday/tests/test_vision.py`.

**Modified:**
- `friday/services/ptt.py` — optional `VisionState`, clear on SPACE press.
- `friday/main.py` — build `VisionState`, wire both processors + `VISION_KEY`.
- `friday/.env` — `VISION_KEY=ctrl_r`.
- `pyproject.toml` — add `friday/tests` to `testpaths`.

---

## Task 1: Test infrastructure + screen capture util

**Files:**
- Create: `friday/tests/__init__.py`, `friday/services/vision_capture.py`, `friday/tests/test_vision_capture.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Confirm the two load-bearing APIs still resolve**

Run:
```bash
uv run python -c "from pipecat.frames.frames import LLMContextFrame; from pipecat.processors.aggregators.llm_context import LLMContext; print(LLMContextFrame.__name__); print(LLMContext.create_image_url_message(url='data:,x', text='hi'))"
```
Expected: prints `LLMContextFrame` and `{'role': 'user', 'content': [{'type': 'text', 'text': 'hi'}, {'type': 'image_url', 'image_url': {'url': 'data:,x'}}]}`. If either import fails, STOP and re-check the spec's §9 before continuing.

- [ ] **Step 2: Add the vision test dir to pytest testpaths**

In `pyproject.toml`, change:
```toml
testpaths = ["friday/mcp_server/tests"]
```
to:
```toml
testpaths = ["friday/mcp_server/tests", "friday/tests"]
```

- [ ] **Step 3: Create the test package marker**

Create `friday/tests/__init__.py`:
```python
```

- [ ] **Step 4: Write the failing test for the capture util**

Create `friday/tests/test_vision_capture.py`:
```python
import base64
import pytest
from PIL import Image
from friday.services import vision_capture


@pytest.mark.asyncio
async def test_capture_returns_jpeg_data_url(monkeypatch):
    # Fake screen: a 4000x3000 solid image (forces downscale).
    fake = Image.new("RGB", (4000, 3000), (10, 20, 30))
    monkeypatch.setattr(vision_capture.ImageGrab, "grab", lambda: fake)

    url = await vision_capture.capture_screen_data_url()

    assert url.startswith("data:image/jpeg;base64,")
    raw = base64.b64decode(url.split(",", 1)[1])
    assert raw[:2] == b"\xff\xd8"          # JPEG magic
    assert len(raw) < 400_000              # downscaled payload is small


def test_grab_downscales_to_max_dim(monkeypatch):
    fake = Image.new("RGB", (4000, 3000), (0, 0, 0))
    monkeypatch.setattr(vision_capture.ImageGrab, "grab", lambda: fake)
    url = vision_capture._grab_to_data_url()
    assert url.startswith("data:image/jpeg;base64,")
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `uv run pytest friday/tests/test_vision_capture.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.services.vision_capture'`.

- [ ] **Step 6: Implement the capture util**

Create `friday/services/vision_capture.py`:
```python
"""Capture the primary screen as a small JPEG data URL for the vision model."""

import asyncio
import base64
import io

from PIL import ImageGrab

MAX_DIM = 1024
JPEG_QUALITY = 70


def _grab_to_data_url() -> str:
    """Grab the primary screen, downscale, JPEG-encode, return a data: URL."""
    img = ImageGrab.grab()
    img.thumbnail((MAX_DIM, MAX_DIM))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{b64}"


async def capture_screen_data_url() -> str:
    """Async wrapper — runs the blocking grab/encode off the event loop."""
    return await asyncio.to_thread(_grab_to_data_url)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest friday/tests/test_vision_capture.py -q`
Expected: PASS (2 passed).

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml friday/tests/__init__.py friday/services/vision_capture.py friday/tests/test_vision_capture.py
git commit -m "feat: screen capture util for Friday vision"
```

---

## Task 2: VisionState + image injection logic

The pure, heavily-tested core: the function that rewrites a turn's user message to include the image.

**Files:**
- Create: `friday/services/vision.py`
- Test: `friday/tests/test_vision.py`

- [ ] **Step 1: Write the failing tests**

Create `friday/tests/test_vision.py`:
```python
import pytest
from pipecat.processors.aggregators.llm_context import LLMContext
from friday.services import vision


def _ctx(messages):
    return LLMContext(messages=list(messages))


def test_inject_rewrites_last_user_message():
    state = vision.VisionState(pending_url="data:image/jpeg;base64,AAA")
    ctx = _ctx([
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "what is this?"},
    ])
    injected = vision.inject_pending_image(ctx, state)
    assert injected is True
    last = ctx._messages[-1]
    assert last["role"] == "user"
    assert last["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}},
    ]
    assert state.pending_url is None  # consumed


def test_inject_noop_when_no_pending():
    state = vision.VisionState(pending_url=None)
    ctx = _ctx([{"role": "user", "content": "hello"}])
    assert vision.inject_pending_image(ctx, state) is False
    assert ctx._messages[-1]["content"] == "hello"  # untouched


def test_inject_noop_when_last_message_not_user():
    state = vision.VisionState(pending_url="data:image/jpeg;base64,AAA")
    ctx = _ctx([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ])
    assert vision.inject_pending_image(ctx, state) is False
    assert state.pending_url == "data:image/jpeg;base64,AAA"  # left for press-guard


def test_inject_noop_when_no_messages():
    state = vision.VisionState(pending_url="data:image/jpeg;base64,AAA")
    ctx = _ctx([])
    assert vision.inject_pending_image(ctx, state) is False


def test_resolve_vision_key_modifier():
    from pynput import keyboard
    assert vision.resolve_vision_key("ctrl_r") == keyboard.Key.ctrl_r
    assert vision.resolve_vision_key("alt_r") == keyboard.Key.alt_r


def test_resolve_vision_key_single_char():
    from pynput import keyboard
    assert vision.resolve_vision_key("v") == keyboard.KeyCode.from_char("v")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/tests/test_vision.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'friday.services.vision'`.

- [ ] **Step 3: Implement `VisionState`, `resolve_vision_key`, `inject_pending_image`**

Create `friday/services/vision.py`:
```python
"""Friday screen-vision: shared state, key parsing, and image injection.

A vision turn captures the screen on a dedicated hotkey and attaches it to that
turn's user message so the vision model answers about what's on screen.
"""

from dataclasses import dataclass

from pynput import keyboard

from pipecat.processors.aggregators.llm_context import LLMContext


@dataclass
class VisionState:
    """Shared between the vision hotkey processor and the context injector."""

    pending_url: str | None = None


def resolve_vision_key(name: str):
    """Map a VISION_KEY string to a pynput key. Modifier names (ctrl_r, alt_r,
    ...) map to keyboard.Key.<name>; anything else is treated as a character."""
    key = getattr(keyboard.Key, name, None)
    if key is not None:
        return key
    return keyboard.KeyCode.from_char(name)


def inject_pending_image(context: LLMContext, state: VisionState) -> bool:
    """If an image is pending and the last context message is a plain user
    message, rewrite that message to multimodal (text + image) and clear the
    flag. Returns True if an image was injected."""
    if not state.pending_url:
        return False
    messages = context._messages
    if not messages or messages[-1].get("role") != "user":
        return False
    content = messages[-1].get("content")
    text = content if isinstance(content, str) else None
    messages[-1] = LLMContext.create_image_url_message(url=state.pending_url, text=text)
    state.pending_url = None
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest friday/tests/test_vision.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add friday/services/vision.py friday/tests/test_vision.py
git commit -m "feat: vision state, key parsing, and image-injection logic"
```

---

## Task 3: Vision hotkey processor + injector processor + PTT leak guard

**Files:**
- Modify: `friday/services/vision.py` (append the two processors)
- Modify: `friday/services/ptt.py`
- Test: `friday/tests/test_vision.py` (append), `friday/tests/test_ptt_guard.py`

- [ ] **Step 1: Write failing tests for the capture-store coroutine and PTT guard**

Append to `friday/tests/test_vision.py`:
```python
@pytest.mark.asyncio
async def test_capture_and_store_sets_url(monkeypatch):
    state = vision.VisionState()
    proc = vision.VisionPTTProcessor(state=state, key_name="ctrl_r")
    monkeypatch.setattr(vision, "capture_screen_data_url",
                        lambda: _async_return("data:image/jpeg;base64,ZZZ"))
    await proc._capture_and_store()
    assert state.pending_url == "data:image/jpeg;base64,ZZZ"


@pytest.mark.asyncio
async def test_capture_and_store_failure_clears(monkeypatch):
    state = vision.VisionState(pending_url="stale")

    async def boom():
        raise RuntimeError("no screen")

    proc = vision.VisionPTTProcessor(state=state, key_name="ctrl_r")
    monkeypatch.setattr(vision, "capture_screen_data_url", boom)
    await proc._capture_and_store()
    assert state.pending_url is None  # cleared, no raise


def _async_return(value):
    async def _coro():
        return value
    return _coro()
```

Create `friday/tests/test_ptt_guard.py`:
```python
from pynput import keyboard
from friday.services.ptt import PushToTalkProcessor
from friday.services.vision import VisionState


def test_space_press_clears_pending_image():
    state = VisionState(pending_url="data:image/jpeg;base64,AAA")
    ptt = PushToTalkProcessor(vision_state=state)
    # No event loop set, so _emit is a no-op; we only assert the guard.
    ptt._on_press(keyboard.Key.space)
    assert state.pending_url is None


def test_space_press_without_state_is_safe():
    ptt = PushToTalkProcessor()
    ptt._on_press(keyboard.Key.space)  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest friday/tests/test_vision.py friday/tests/test_ptt_guard.py -q`
Expected: FAIL — `AttributeError: module 'friday.services.vision' has no attribute 'VisionPTTProcessor'` and `TypeError` for the unexpected `vision_state` kwarg.

- [ ] **Step 3: Append the two processors to `friday/services/vision.py`**

Add these imports to the top of `friday/services/vision.py` (merge with existing imports):
```python
import asyncio

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    StartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from friday.services.vision_capture import capture_screen_data_url
```

Append to the end of `friday/services/vision.py`:
```python
class VisionPTTProcessor(FrameProcessor):
    """Hold the vision key = look at the screen + talk. Emits the same speaking
    frames as PTT (so STT runs), and captures the screen in the background."""

    def __init__(self, *, state: VisionState, key_name: str = "ctrl_r", **kwargs):
        super().__init__(**kwargs)
        self._state = state
        self._key = resolve_vision_key(key_name)
        self._loop = None
        self._listener = None
        self._active = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._loop = self.get_event_loop()
            self._start_listener()
        await self.push_frame(frame, direction)

    def _start_listener(self):
        if self._listener:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()
        logger.info("Vision ready — hold the vision key to look + talk.")

    def _on_press(self, key):
        if key == self._key and not self._active:
            self._active = True
            self._state.pending_url = None          # re-arm
            logger.debug("Vision: listening + capturing...")
            # 1) open the mic immediately (no lag behind the capture)
            self._emit(UserStartedSpeakingFrame())
            self._emit(VADUserStartedSpeakingFrame())
            # 2) capture in the background; sets pending_url when done
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._capture_and_store(), self._loop)

    def _on_release(self, key):
        if key == self._key and self._active:
            self._active = False
            logger.debug("Vision: sent.")
            self._emit(VADUserStoppedSpeakingFrame())
            self._emit(UserStoppedSpeakingFrame())

    async def _capture_and_store(self):
        try:
            self._state.pending_url = await capture_screen_data_url()
        except Exception:
            logger.exception("Vision capture failed; proceeding without image")
            self._state.pending_url = None

    def _emit(self, frame: Frame):
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.push_frame(frame, FrameDirection.DOWNSTREAM), self._loop
            )

    async def cleanup(self):
        if self._listener:
            self._listener.stop()
            self._listener = None
        await super().cleanup()


class VisionContextInjector(FrameProcessor):
    """Attaches a pending screenshot to the current turn's user message."""

    def __init__(self, *, state: VisionState, **kwargs):
        super().__init__(**kwargs)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            if inject_pending_image(frame.context, self._state):
                logger.debug("Vision: image attached to this turn.")
        await self.push_frame(frame, direction)
```

- [ ] **Step 4: Add the leak guard to `friday/services/ptt.py`**

In `friday/services/ptt.py`, change the constructor:
```python
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._loop = None
        self._listener = None
        self._talking = False
```
to:
```python
    def __init__(self, *, vision_state=None, **kwargs):
        super().__init__(**kwargs)
        self._loop = None
        self._listener = None
        self._talking = False
        self._vision_state = vision_state
```

And change `_on_press`:
```python
    def _on_press(self, key):
        if key == keyboard.Key.space and not self._talking:
            self._talking = True
            logger.debug("PTT: listening...")
            self._emit(UserStartedSpeakingFrame())
            self._emit(VADUserStartedSpeakingFrame())
```
to:
```python
    def _on_press(self, key):
        if key == keyboard.Key.space and not self._talking:
            self._talking = True
            if self._vision_state is not None:
                self._vision_state.pending_url = None  # drop any stale vision image
            logger.debug("PTT: listening...")
            self._emit(UserStartedSpeakingFrame())
            self._emit(VADUserStartedSpeakingFrame())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest friday/tests/test_vision.py friday/tests/test_ptt_guard.py -q`
Expected: PASS (10 passed total in test_vision.py + 2 in test_ptt_guard.py).

- [ ] **Step 6: Commit**

```bash
git add friday/services/vision.py friday/services/ptt.py friday/tests/test_vision.py friday/tests/test_ptt_guard.py
git commit -m "feat: vision hotkey + injector processors and PTT leak guard"
```

---

## Task 4: Wire into the voice app + manual e2e

**Files:**
- Modify: `friday/main.py`, `friday/.env`

- [ ] **Step 1: Add the import to `friday/main.py`**

In `friday/main.py`, after `from services.ptt import PushToTalkProcessor`, add:
```python
from services.vision import VisionState, VisionPTTProcessor, VisionContextInjector
```

- [ ] **Step 2: Build the shared state and processors in `main()`**

In `friday/main.py`, find:
```python
    ptt = PushToTalkProcessor()
```
Replace with:
```python
    vision_state = VisionState()
    ptt = PushToTalkProcessor(vision_state=vision_state)
    vision_ptt = VisionPTTProcessor(state=vision_state, key_name=os.getenv("VISION_KEY", "ctrl_r"))
    vision_injector = VisionContextInjector(state=vision_state)
```

- [ ] **Step 3: Insert the processors into the pipeline**

In `friday/main.py`, find the pipeline list:
```python
    pipeline = Pipeline(
        [
            transport.input(),
            ptt,
            stt,
            agg.user(),
            llm,
            tts,
            transport.output(),
            agg.assistant(),
        ]
    )
```
Replace with:
```python
    pipeline = Pipeline(
        [
            transport.input(),
            ptt,
            vision_ptt,
            stt,
            agg.user(),
            vision_injector,
            llm,
            tts,
            transport.output(),
            agg.assistant(),
        ]
    )
```

- [ ] **Step 4: Add VISION_KEY to `friday/.env`**

In `friday/.env`, under the existing config, add:
```
# Vision: hold this key to capture the screen and ask about it (pynput key name)
VISION_KEY=ctrl_r
```

- [ ] **Step 5: Verify the app imports and the full suite passes**

Run:
```bash
uv run python -c "import ast; ast.parse(open('friday/main.py', encoding='utf-8').read()); print('syntax ok')"
uv run pytest -q
```
Expected: `syntax ok`, then all tests pass (vision + capture + ptt guard + existing mcp_server suite).

- [ ] **Step 6: Manual end-to-end (logged → regression checklist)**

Start the app (server + voice): `./run_friday.ps1`
- Hold **Right Ctrl**, ask "what's on my screen?", release → Friday describes the screen.
- Immediately do a normal **SPACE** turn ("say hello") → reply has NO image (check logs: no "image attached").
- Hold **Right Ctrl**, release WITHOUT speaking → then a SPACE turn → unaffected (stale flag cleared).

Record pass/fail for each.

- [ ] **Step 7: Commit**

```bash
git add friday/main.py
git commit -m "feat: wire screen-vision hotkey into the voice app"
```
Note: `friday/.env` is gitignored — apply the Step 4 edit manually; the commit records only `main.py`.

---

## Self-Review

**Spec coverage:**
- §2 architecture (two processors, shared state, pipeline placement) → Tasks 2–4. ✓
- §3 components (`vision_capture`, `VisionState`, `VisionPTTProcessor`, `VisionContextInjector`, ptt change) → Tasks 1–4. ✓
- §3/§4 emit-speaking-frames-before-capture ordering → Task 3 Step 3 (`_on_press` emits frames, then schedules `_capture_and_store`). ✓
- §4 capture on press → Task 3 (`_on_press` triggers capture). ✓
- §5 stale-flag lifecycle (set on press, cleared by injector + every turn start, injector guard) → Task 2 (`inject_pending_image` clears + guards), Task 3 (vision re-arm on press, PTT clears on press). ✓
- §6 config (`VISION_KEY`, default `ctrl_r`) + capture-failure degradation → Task 4 Step 4, Task 3 (`_capture_and_store` try/except). ✓
- §7 testing (capture, processors, injector, ptt guard, format) → Tasks 1–3 tests + Task 4 e2e. ✓
- §8 files / no new deps → all tasks; Pillow already present. ✓
- §9 must-verify-first (frame class) → plan preamble + Task 1 Step 1. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code; every command has expected output. ✓

**Type/name consistency:** `VisionState.pending_url`, `inject_pending_image(context, state)`, `resolve_vision_key(name)`, `VisionPTTProcessor(state=, key_name=)`, `VisionContextInjector(state=)`, `capture_screen_data_url()`, `_grab_to_data_url()`, `_capture_and_store()` used consistently across Tasks 1–4. `PushToTalkProcessor(vision_state=)` matches its new signature and the main.py call. The injector reads `frame.context` (matches `LLMContextFrame(context=...)`). ✓

**Fixed during review:** test for `_capture_and_store` monkeypatches `vision.capture_screen_data_url` (the name imported into `vision.py`), not the capture module — matches how the processor calls it.
