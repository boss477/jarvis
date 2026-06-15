"""Push-to-talk: hold SPACE to talk, release to send.

Synthesizes the speaking frames that a VAD would normally emit, from the
spacebar instead. On press -> user/VAD started speaking (STT starts buffering).
On release -> VAD/user stopped speaking (STT flushes the buffer to Whisper and
the LLM turn fires).
"""

import asyncio

from loguru import logger
from pynput import keyboard

from pipecat.frames.frames import (
    Frame,
    StartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class PushToTalkProcessor(FrameProcessor):
    """Hold SPACE = mic open; release = end of turn."""

    def __init__(self, *, vision_state=None, **kwargs):
        super().__init__(**kwargs)
        self._loop = None
        self._listener = None
        self._talking = False
        self._vision_state = vision_state

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
        logger.info("PTT ready — hold SPACE to talk, release to send.")

    def _on_press(self, key):
        if key == keyboard.Key.space and not self._talking:
            self._talking = True
            if self._vision_state is not None:
                self._vision_state.pending_url = None  # drop any stale vision image
            logger.debug("PTT: listening...")
            self._emit(UserStartedSpeakingFrame())
            self._emit(VADUserStartedSpeakingFrame())

    def _on_release(self, key):
        if key == keyboard.Key.space and self._talking:
            self._talking = False
            logger.debug("PTT: sent.")
            self._emit(VADUserStoppedSpeakingFrame())
            self._emit(UserStoppedSpeakingFrame())

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
