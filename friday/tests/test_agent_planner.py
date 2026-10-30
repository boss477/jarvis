import pytest

from friday.services.agent_planner import PlannerError, parse_plan


def test_parse_plan_accepts_array_and_fence():
    steps = parse_plan('```json\n[{"tool":"send","args":{"text":"hi"}}]\n```')
    assert steps[0]["tool"] == "send"


def test_parse_plan_accepts_steps_object():
    assert parse_plan('{"steps":[{"tool":"send","args":{}}]}')[0]["tool"] == "send"


def test_parse_plan_rejects_invalid_shape():
    with pytest.raises(PlannerError):
        parse_plan('{"tool":"send"}')


def test_planner_never_guesses_another_whatsapp_recipient(monkeypatch):
    from friday.services.agent_planner import plan_with_openai_compatible_model

    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content":
                '[{"tool":"respond","args":{"text":"What is their phone number?"}}]'
            }}]}

    def fake_post(_url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("friday.services.agent_planner.httpx.post", fake_post)
    steps = plan_with_openai_compatible_model(
        "Channel: whatsapp\nSender ID: 15550000001\nCurrent message: send this to Mom"
    )

    system = captured["json"]["messages"][0]["content"]
    assert "never substitute the current sender or guess a recipient" in system
    assert steps == [{"tool": "respond", "args": {"text": "What is their phone number?"}}]


def test_generate_voice_reply_uses_fallback_for_null_model_content(monkeypatch):
    from friday.services.agent_planner import generate_voice_reply

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": None}}]}

    monkeypatch.setattr(
        "friday.services.agent_planner.httpx.post",
        lambda *_args, **_kwargs: Response(),
    )

    assert generate_voice_reply("approved purpose", [], "hello") == (
        "I'm sorry, could you please repeat that?",
        False,
    )
