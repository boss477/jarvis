from friday.services.transcript_filter import _normalize, _JUNK


def test_whisper_hallucinations_classified_junk():
    for phrase in ["Thank you.", "thank you", "Thanks for watching!", "You", "  bye ", ""]:
        assert _normalize(phrase) in _JUNK


def test_real_speech_not_junk():
    for phrase in ["open chrome", "what's my battery", "thank you for opening notepad"]:
        assert _normalize(phrase) not in _JUNK


def test_normalize_strips_punctuation_and_case():
    assert _normalize("Thank You!!!") == "thank you"
