from friday.services.agent_factory import build_persistent_agent
from friday.services.communication_hub import CommunicationHub, CommunicationMessage, DeliveryResult
import pytest


def test_factory_builds_stopped_runtime_with_composed_channels(tmp_path):
    runtime = build_persistent_agent(lambda _: [], state_path=tmp_path / "tasks.jsonl",
                                     hub=CommunicationHub())
    assert not runtime.running
    assert runtime.agent.hub.channels() == []

def test_factory_registers_send_tool(tmp_path):
    class Adapter:
        name = "whatsapp"
        def __init__(self): self.sent = []
        def poll(self): return []
        def send_message(self, recipient, text):
            self.sent.append((recipient, text))
            return DeliveryResult("whatsapp", recipient, True, detail="sent")
    hub = CommunicationHub(); adapter = Adapter(); hub.register(adapter)
    runtime = build_persistent_agent(
        lambda _: [{"tool": "send", "args": {"channel": "whatsapp", "recipient": "u1", "text": "hi"}}],
        state_path=tmp_path / "tasks.jsonl", hub=hub,
    )
    reply = runtime.controller.handle(
        CommunicationMessage("whatsapp", "c1", "u1", "say hi")
    )
    assert "needs approval" in reply
    task = runtime.tasks.list()[0]
    runtime.tasks.approve(task.id)
    assert "completed" in runtime.controller.advance(task.id)
    assert adapter.sent == [("u1", "hi")]


def test_whatsapp_memory_is_sender_private_and_returns_saved_facts(tmp_path):
    def planner(prompt):
        if "Current message: remember my favorite color is blue" in prompt:
            return [
                {"tool": "remember", "args": {"text": "Favorite color is blue"}},
                {"tool": "respond", "args": {"text": "I'll remember that."}},
            ]
        return [{"tool": "respond", "args": {"memory_query": "favorite color"}}]

    runtime = build_persistent_agent(planner, state_path=tmp_path / "tasks.jsonl",
                                     hub=CommunicationHub())
    sender_one = CommunicationMessage("whatsapp", "c1", "user-1",
                                      "remember my favorite color is blue")
    sender_two = CommunicationMessage("whatsapp", "c2", "user-2", "what is my favorite color?")
    lookup_one = CommunicationMessage("whatsapp", "c1", "user-1", "what is my favorite color?")

    assert runtime.controller.handle(sender_one) == "I'll remember that."
    assert runtime.controller.handle(sender_two) == "I don't have any saved memory about that."
    assert runtime.controller.handle(lookup_one) == "Here's what I remember: Favorite color is blue"


def test_planner_history_contains_only_prior_user_facing_turns(tmp_path):
    prompts = []

    def planner(prompt):
        prompts.append(prompt)
        return [{"tool": "respond", "args": {"text": "reply"}}]

    runtime = build_persistent_agent(planner, state_path=tmp_path / "tasks.jsonl",
                                     hub=CommunicationHub())
    first = CommunicationMessage("whatsapp", "chat-1", "user-1", "first question", "m1")
    second = CommunicationMessage("whatsapp", "chat-1", "user-1", "second question", "m2")

    runtime.webhook_gateway.message_handler(first)
    runtime.webhook_gateway.message_handler(second)

    assert prompts[0].count("first question") == 1
    assert prompts[0].count("Agent:") == 0
    assert "user-1: first question" in prompts[1]
    assert prompts[1].count("second question") == 1
    assert "Agent: reply" in prompts[1]
    assert "processed" not in prompts[1]
    assert "delivery_result" not in prompts[1]


def test_agent_file_tools_are_rooted_and_opening_requires_approval(tmp_path, monkeypatch):
    safe_root = tmp_path / "allowed"
    outside_root = tmp_path / "not-allowed"
    safe_root.mkdir()
    outside_root.mkdir()
    monkeypatch.setenv("AGENT_FILE_ROOTS", str(safe_root))
    runtime = build_persistent_agent(lambda _: [], state_path=tmp_path / "tasks.jsonl",
                                     hub=CommunicationHub())

    search = runtime.orchestrator.handlers["search_files"]
    open_path = runtime.orchestrator.handlers["open_path"]
    with pytest.raises(ValueError, match="outside the configured agent file roots"):
        search({"query": "secret", "directory": str(outside_root)})
    with pytest.raises(ValueError, match="outside the configured agent file roots"):
        open_path({"path": str(outside_root)})
    assert runtime.orchestrator.policies["open_path"].requires_approval
