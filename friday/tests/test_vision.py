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
