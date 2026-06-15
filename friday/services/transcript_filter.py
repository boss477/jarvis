"""Drop Whisper silence-hallucinations before they pollute the LLM context.

faster-whisper invents stock phrases ("Thank you.", "Thanks for watching") when
fed near-silent audio — which the always-listening wake word produces. Sitting
between STT and the user aggregator, this swallows those junk transcripts so they
never become conversation turns.
"""

import re

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Normalized (lowercased, letters-only) phrases Whisper emits on silence/noise.
_JUNK = {
    "", "you", "thank you", "thanks", "thank you so much", "thanks for watching",
    "thank you for watching", "please subscribe", "bye", "okay", "ok", "so", "the",
}


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


class TranscriptFilter(FrameProcessor):
    """Swallow transcription frames whose text is a known silence-hallucination."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            if _normalize(frame.text) in _JUNK:
                logger.debug(f"Dropped junk transcript: {frame.text!r}")
                return  # swallow — never reaches the aggregator/LLM
        await self.push_frame(frame, direction)
