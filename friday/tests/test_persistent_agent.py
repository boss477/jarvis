import time
import threading
import pytest

from friday.services.communication_hub import (CommunicationHub, CommunicationMessage,
                                                DeliveryResult)
from friday.services.action_agent import PersistentActionAgent
from friday.services.persistent_agent import AgentRuntimeConfig, PersistentAgentRuntime


class FakeAdapter:
    name = "whatsapp"

    def __init__(self):
        self.inbox = [CommunicationMessage("whatsapp", "c1", "u1", "hello", "m1")]
        self.sent = []

    def poll(self):
        items, self.inbox = self.inbox, []
        return items

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return DeliveryResult("whatsapp", conversation_id, True)


def test_runtime_processes_one_message_and_stops():
    hub = CommunicationHub(); adapter = FakeAdapter(); hub.register(adapter)
    runtime = PersistentAgentRuntime(hub, lambda message: "done",
                                     AgentRuntimeConfig(poll_interval_seconds=0.01))
    runtime.start()
    deadline = time.time() + 1
    while not adapter.sent and time.time() < deadline:
        time.sleep(0.01)
    runtime.stop()
    assert adapter.sent == [("c1", "done")]
    assert not runtime.running


def test_runtime_stop_retains_live_thread_after_timeout():
    hub = CommunicationHub(); adapter = FakeAdapter(); hub.register(adapter)
    entered = threading.Event()
    release = threading.Event()

    def handler(_message):
        entered.set()
        release.wait(timeout=2)
        return "done"

    runtime = PersistentAgentRuntime(
        hub, handler, AgentRuntimeConfig(poll_interval_seconds=0.01)
    )
    first = None
    runtime.start()
    try:
        assert entered.wait(timeout=1)
        first = runtime._thread
        assert first is not None

        runtime.stop(timeout=0.01)
        assert runtime._thread is first
        assert first.is_alive()

        # A second start must not replace a still-running worker.
        runtime.start()
        assert runtime._thread is first
    finally:
        release.set()
        if first is not None:
            first.join(timeout=1)
        runtime.stop(timeout=1)

    assert runtime._thread is None


def test_webhook_retry_does_not_repeat_action_or_successful_reply(tmp_path):
    history_path = tmp_path / "conversations.jsonl"
    calls = []
    agent = PersistentActionAgent(CommunicationHub(),
                                  lambda message: calls.append(message.message_id) or "answer",
                                  history_path)
    inbound = CommunicationMessage("whatsapp", "c1", "u1", "question", "wamid.1")

    first = agent.process_message(inbound, deliver=False)
    agent.record_delivery(inbound, "sent:wamid.2", True)
    agent = PersistentActionAgent(CommunicationHub(),
                                  lambda message: calls.append(message.message_id) or "answer",
                                  history_path)
    retry = agent.process_message(inbound, deliver=False)

    assert first.reply == "answer"
    assert retry.reply == ""
    assert retry.delivery.detail == "duplicate already delivered"
    assert calls == ["wamid.1"]


def test_webhook_retry_redelivers_cached_reply_when_delivery_not_confirmed(tmp_path):
    calls = []
    agent = PersistentActionAgent(CommunicationHub(),
                                  lambda message: calls.append(message.message_id) or "answer",
                                  tmp_path / "conversations.jsonl")
    inbound = CommunicationMessage("whatsapp", "c1", "u1", "question", "wamid.1")

    first = agent.process_message(inbound, deliver=False)
    agent.record_delivery(inbound, "temporary send failure", False)
    retry = agent.process_message(inbound, deliver=False)

    assert first.reply == retry.reply == "answer"
    assert calls == ["wamid.1"]


def test_webhook_delivery_claim_allows_only_one_concurrent_send(tmp_path):
    agent = PersistentActionAgent(CommunicationHub(), lambda _message: "answer",
                                  tmp_path / "conversations.jsonl")
    inbound = CommunicationMessage("whatsapp", "c1", "u1", "question", "wamid.1")
    assert agent.claim_delivery(inbound)
    assert not agent.claim_delivery(inbound)

    agent.record_delivery(inbound, "temporary send failure", False)
    assert agent.claim_delivery(inbound)

    agent.record_delivery(inbound, "sent:wamid.2", True)
    assert not agent.claim_delivery(inbound)


def test_restart_quarantines_inbound_event_interrupted_during_handler(tmp_path):
    history_path = tmp_path / "conversations.jsonl"
    inbound = CommunicationMessage("whatsapp", "c1", "u1", "do it", "wamid.crash")

    def interrupted_handler(_message):
        raise RuntimeError("host stopped during action")

    agent = PersistentActionAgent(CommunicationHub(), interrupted_handler, history_path)
    with pytest.raises(RuntimeError, match="host stopped"):
        agent.process_message(inbound, deliver=False)

    calls = []
    recovered = PersistentActionAgent(
        CommunicationHub(), lambda message: calls.append(message.message_id) or "unsafe retry",
        history_path,
    )
    retry = recovered.process_message(inbound, deliver=False)
    assert "interrupted" in retry.reply
    assert "verify its destination" in retry.reply
    assert retry.delivery.detail == "duplicate; cached reply available for webhook redelivery"
    assert calls == []

    after_second_restart = PersistentActionAgent(
        CommunicationHub(), lambda _message: calls.append("ran") or "unsafe retry", history_path,
    )
    again = after_second_restart.process_message(inbound, deliver=False)
    assert again.reply == retry.reply
    assert calls == []

def test_recent_history_non_positive_limit_returns_empty():
    agent = PersistentActionAgent(CommunicationHub(), lambda _message: "answer")
    key = "whatsapp:c1"
    agent.history[key] = [
        CommunicationMessage("whatsapp", "c1", "u1", "first", "m1"),
        CommunicationMessage("whatsapp", "c1", "u1", "second", "m2"),
    ]

    assert agent.recent_history("whatsapp", "c1", limit=0) == []
    assert agent.recent_history("whatsapp", "c1", limit=-1) == []
    assert [message.text for message in agent.recent_history(
        "whatsapp", "c1", limit=1
    )] == ["second"]
