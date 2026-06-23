"""Barge-in: cut off an in-progress bot reply when a new user turn starts.

Jarvis uses ExternalUserTurnStrategies, which Pipecat documents as NOT generating
interruptions — our PTT/wake/vision processors emit the speaking frames, but nothing
triggers an interruption, so a reply always plays to completion. This processor closes
that gap: it watches for the start of a user turn and, only if the bot is currently
speaking, broadcasts an InterruptionFrame (flushing in-progress TTS).

Since all three input modes (SPACE, "hey jarvis", vision key) emit
UserStartedSpeakingFrame, this one processor covers them all. No-op on the normal path
(you wait for Jarvis to finish before talking), so it adds no latency there.
"""

from loguru import logger

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class BargeInProcessor(FrameProcessor):
    """Interrupt the bot when the user starts a new turn while it's speaking."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._bot_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
        elif isinstance(frame, UserStartedSpeakingFrame) and self._bot_speaking:
            logger.debug("Barge-in: interrupting bot reply.")
            await self.broadcast_interruption()
            self._bot_speaking = False
        await self.push_frame(frame, direction)
