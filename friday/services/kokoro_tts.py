"""Kokoro TTS wrapped as a Pipecat TTSService (in-process, ONNX, GPU-accelerated).

Faster replacement for Supertonic: runs on the RTX 4060 via onnxruntime CUDA,
and natively outputs 24 kHz (matches our audio_out), so no resampling on the
happy path. Falls back to CPU if CUDA libs aren't loadable.
"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

# onnxruntime's CUDA provider needs CUDA 12 + cuDNN 9 DLLs on the search path.
# The torch (cu121) install already bundles them, so point onnxruntime at that
# folder before importing it. Degrades to CPU if torch isn't present.
try:
    import torch

    os.add_dll_directory(os.path.join(os.path.dirname(torch.__file__), "lib"))
except Exception:
    pass

import numpy as np
import onnxruntime as ort
from loguru import logger

from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

from kokoro_onnx import Kokoro

KOKORO_SAMPLE_RATE = 24000
BIN = Path(__file__).parent.parent / "bin"


class KokoroTTSService(TTSService):
    """Local text-to-speech via Kokoro ONNX. GPU if available, else CPU."""

    def __init__(
        self,
        *,
        voice: str = "am_michael",
        speed: float = 1.0,
        lang: str = "en-us",
        model_path: str | None = None,
        voices_path: str | None = None,
        **kwargs,
    ):
        super().__init__(
            push_start_frame=True,
            push_stop_frames=True,
            settings=TTSSettings(),
            **kwargs,
        )
        self._voice = voice
        self._speed = speed
        self._lang = lang

        model_path = model_path or str(BIN / "kokoro-v1.0.onnx")
        voices_path = voices_path or str(BIN / "voices-v1.0.bin")

        logger.debug("Loading Kokoro model...")
        session = ort.InferenceSession(
            model_path,
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        active = session.get_providers()
        if "CUDAExecutionProvider" in active:
            logger.debug("Kokoro running on GPU (CUDA)")
        else:
            logger.warning(f"Kokoro running on CPU (providers: {active})")
        self._kokoro = Kokoro.from_session(session, voices_path)
        self._resampler = create_stream_resampler()
        logger.debug("Kokoro model ready")

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating TTS [{text}]")
        try:
            await self.start_tts_usage_metrics(text)

            # create() is blocking/compute-bound -> off the event loop
            samples, sr = await asyncio.to_thread(
                self._kokoro.create,
                text,
                self._voice,
                speed=self._speed,
                lang=self._lang,
            )
            await self.stop_ttfb_metrics()

            samples = np.asarray(samples, dtype=np.float32).reshape(-1)
            audio_int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            if sr != self.sample_rate:
                audio_int16 = await self._resampler.resample(
                    audio_int16, sr, self.sample_rate
                )

            yield TTSAudioRawFrame(
                audio=audio_int16,
                sample_rate=self.sample_rate,
                num_channels=1,
                context_id=context_id,
            )
        except Exception as e:
            logger.exception("Kokoro TTS failed")
            yield ErrorFrame(error=f"Kokoro TTS error: {e}")
        finally:
            await self.stop_ttfb_metrics()
