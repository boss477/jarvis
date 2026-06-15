"""Wake-word logic tests with fake model/VAD — no audio device, no real ONNX."""

from pipecat.audio.vad.vad_analyzer import VADState
from pipecat.frames.frames import (
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from friday.services.wakeword import WakeWordProcessor, _OWW_CHUNK


class FakeModel:
    def __init__(self, score):
        self.score = score
    def predict(self, samples):
        return {"hey_jarvis": self.score}


class FakeVAD:
    def __init__(self, states):
        self._states = list(states)
    def analyze_audio(self, audio):
        return self._states.pop(0) if self._states else VADState.QUIET


def _proc(model, vad):
    p = WakeWordProcessor(vad_analyzer=vad)
    p._model = model
    p.push_frame = lambda *a, **k: None  # swallow emitted frames
    emitted = []
    p._emit = lambda frame: emitted.append(type(frame))
    return p, emitted


def _silence(n_chunks):
    return (b"\x00\x00" * _OWW_CHUNK) * n_chunks


def test_high_score_activates_and_emits_start():
    p, emitted = _proc(FakeModel(0.9), FakeVAD([]))
    p._on_audio(_silence(1))
    assert p._active
    assert UserStartedSpeakingFrame in emitted


def test_low_score_does_not_activate():
    p, emitted = _proc(FakeModel(0.1), FakeVAD([]))
    p._on_audio(_silence(2))
    assert not p._active
    assert emitted == []


def test_no_trigger_while_ptt_busy():
    p, emitted = _proc(FakeModel(0.9), FakeVAD([]))
    p._ptt_busy = True
    p._on_audio(_silence(1))
    assert not p._active
    assert emitted == []


def test_vad_silence_after_speech_ends_turn():
    p, emitted = _proc(FakeModel(0.9), FakeVAD([VADState.SPEAKING, VADState.QUIET]))
    p._on_audio(_silence(1))   # activate
    p._on_audio(b"\x00\x00" * 100)  # SPEAKING -> _spoke=True
    p._on_audio(b"\x00\x00" * 100)  # QUIET after speech -> end
    assert not p._active
    assert UserStoppedSpeakingFrame in emitted


def test_quiet_before_any_speech_does_not_end():
    p, _ = _proc(FakeModel(0.9), FakeVAD([VADState.QUIET, VADState.QUIET]))
    p._on_audio(_silence(1))   # activate
    p._on_audio(b"\x00\x00" * 100)  # QUIET but never spoke -> stay active
    assert p._active


def test_cooldown_blocks_immediate_retrigger():
    import time
    p, _ = _proc(FakeModel(0.9), FakeVAD([VADState.SPEAKING, VADState.QUIET]))
    p._on_audio(_silence(1))                 # activate
    p._on_audio(b"\x00\x00" * 100)           # SPEAKING
    p._on_audio(b"\x00\x00" * 100)           # QUIET -> end + set cooldown
    assert not p._active
    p._on_audio(_silence(1))                 # high score, but within cooldown
    assert not p._active                     # must NOT retrigger
    p._cooldown_until = time.monotonic() - 1  # cooldown elapsed
    p._on_audio(_silence(1))
    assert p._active                         # now it can trigger again
