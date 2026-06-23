from unittest.mock import AsyncMock

import pytest

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    TextFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from friday.services.barge_in import BargeInProcessor


def _make(monkeypatch):
    """A BargeInProcessor with the base-class pipeline machinery stubbed out, so we
    can drive process_frame without a running pipeline."""
    proc = BargeInProcessor()
    monkeypatch.setattr(FrameProcessor, "process_frame", AsyncMock())
    proc.push_frame = AsyncMock()
    proc.broadcast_interruption = AsyncMock()
    return proc


async def _send(proc, frame):
    await proc.process_frame(frame, FrameDirection.DOWNSTREAM)


@pytest.mark.asyncio
async def test_interrupts_when_bot_speaking(monkeypatch):
    proc = _make(monkeypatch)
    await _send(proc, BotStartedSpeakingFrame())
    await _send(proc, UserStartedSpeakingFrame())
    proc.broadcast_interruption.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_interrupt_when_idle(monkeypatch):
    proc = _make(monkeypatch)
    await _send(proc, UserStartedSpeakingFrame())
    proc.broadcast_interruption.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_stopped_clears_flag(monkeypatch):
    proc = _make(monkeypatch)
    await _send(proc, BotStartedSpeakingFrame())
    await _send(proc, BotStoppedSpeakingFrame())
    await _send(proc, UserStartedSpeakingFrame())
    proc.broadcast_interruption.assert_not_awaited()


@pytest.mark.asyncio
async def test_all_frames_forwarded(monkeypatch):
    proc = _make(monkeypatch)
    for frame in [BotStartedSpeakingFrame(), UserStartedSpeakingFrame(), TextFrame("hi")]:
        await _send(proc, frame)
    assert proc.push_frame.await_count == 3
