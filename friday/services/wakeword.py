"""Hands-free wake word ("hey jarvis"), alongside SPACE push-to-talk.

Sits AFTER the PTT processor so it sees PTT's speaking frames and stays dormant
while SPACE owns a turn — no double-triggering. On detecting the wake word it
emits the same start frames PTT does, then hands turn-ending to Silero VAD:
when you stop talking, it emits the stop frames. SPACE path stays VAD-free.

openWakeWord runs a tiny ONNX model on CPU (near-zero load). Audio is tapped
from the frames already flowing through the pipeline — no second mic stream.
"""

import asyncio
import time

from loguru import logger

from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    StartFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

_OWW_CHUNK = 1280  # samples openWakeWord expects per predict() call (80ms @ 16kHz)
_COOLDOWN_S = 1.5  # ignore re-detections right after a turn (stops repeat triggers)


class WakeWordProcessor(FrameProcessor):
    def __init__(self, *, vad_analyzer, threshold: float = 0.5, model_name: str = "hey_jarvis", **kwargs):
        super().__init__(**kwargs)
        self._vad = vad_analyzer
        self._threshold = threshold
        self._model_name = model_name
        self._model = None
        self._loop = None
        self._buf = bytearray()
        self._active = False       # wake-word turn in progress
        self._ptt_busy = False     # SPACE turn in progress (observed from PTT frames)
        self._spoke = False        # have we heard speech since activation (for VAD end)
        self._cooldown_until = 0.0  # monotonic time before which wake detection is muted

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._loop = self.get_event_loop()
            self._init_model()
        elif isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)) and not self._active:
            self._ptt_busy = True
        elif isinstance(frame, (UserStoppedSpeakingFrame, VADUserStoppedSpeakingFrame)) and not self._active:
            self._ptt_busy = False
        elif isinstance(frame, InputAudioRawFrame):
            self._on_audio(frame.audio)
        await self.push_frame(frame, direction)

    def _init_model(self):
        from openwakeword.model import Model

        self._model = Model(wakeword_models=[self._model_name], inference_framework="onnx")
        logger.info(f"Wake word ready — say '{self._model_name.replace('_', ' ')}'.")

    def _on_audio(self, audio: bytes):
        if self._model is None or self._ptt_busy:
            return
        if self._active:
            self._listen_for_end(audio)
        else:
            self._listen_for_wake(audio)

    def _listen_for_wake(self, audio: bytes):
        import numpy as np

        if time.monotonic() < self._cooldown_until:
            self._buf.clear()  # don't let stale audio carry into the next window
            return
        self._buf.extend(audio)
        step = _OWW_CHUNK * 2  # int16 = 2 bytes/sample
        while len(self._buf) >= step:
            chunk = bytes(self._buf[:step])
            del self._buf[:step]
            scores = self._model.predict(np.frombuffer(chunk, dtype=np.int16))
            if scores.get(self._model_name, 0.0) >= self._threshold:
                self._activate()
                break

    def _activate(self):
        self._active = True
        self._spoke = False
        self._buf.clear()
        logger.debug("Wake word detected — listening.")
        self._emit(UserStartedSpeakingFrame())
        self._emit(VADUserStartedSpeakingFrame())

    def _listen_for_end(self, audio: bytes):
        state = self._vad.analyze_audio(audio)
        if state == VADState.SPEAKING:
            self._spoke = True
        elif state == VADState.QUIET and self._spoke:
            self._deactivate()

    def _deactivate(self):
        self._active = False
        self._spoke = False
        self._buf.clear()
        self._cooldown_until = time.monotonic() + _COOLDOWN_S
        logger.debug("Wake-word turn ended (silence).")
        self._emit(VADUserStoppedSpeakingFrame())
        self._emit(UserStoppedSpeakingFrame())

    def _emit(self, frame: Frame):
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self.push_frame(frame, FrameDirection.DOWNSTREAM), self._loop
            )
