"""Friday screen-vision: shared state, key parsing, and image injection.

A vision turn captures the screen on a dedicated hotkey and attaches it to that
turn's user message so the vision model answers about what's on screen.
"""

import asyncio
from dataclasses import dataclass

from loguru import logger
from pynput import keyboard

from pipecat.frames.frames import (
    Frame,
    LLMContextFrame,
    StartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from friday.services.vision_capture import capture_screen_data_url


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
