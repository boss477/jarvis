from friday.services.action_agent import PersistentActionAgent
from friday.services.remote_gateway import UnconfiguredChannel
from friday.services.communication_hub import (
    CallbackAdapter, CommunicationHub, CommunicationMessage, DeliveryResult, build_default_hub,
)


class FakeAdapter:
    name = "whatsapp"

    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    def poll(self):
        messages, self.messages = self.messages, []
        return messages

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return DeliveryResult(self.name, conversation_id, True, "out-1")


def test_hub_preserves_events_for_durable_agent_deduplication():
    message = CommunicationMessage("whatsapp", "chat-1", "u-1", "hello", "m-1")
    adapter = FakeAdapter([message, message])
    hub = CommunicationHub(); hub.register(adapter)
    assert len(hub.poll()) == 2


def test_agent_replies_on_same_channel_and_keeps_history():
    message = CommunicationMessage("whatsapp", "chat-1", "u-1", "hello", "m-1")
    adapter = FakeAdapter([message])
    hub = CommunicationHub(); hub.register(adapter)
    agent = PersistentActionAgent(hub, lambda incoming: "Hi back")
    replies = agent.process_once()
    assert replies[0].delivery.ok
    assert adapter.sent == [("chat-1", "Hi back")]
    assert [item.sender_id for item in agent.history["whatsapp:chat-1"]] == [
        "u-1", "agent", "agent",
    ]


def test_agent_deduplicates_duplicate_polled_events():
    message = CommunicationMessage("whatsapp", "chat-1", "u-1", "hello", "m-1")
    adapter = FakeAdapter([message, message])
    hub = CommunicationHub(); hub.register(adapter)
    handled = []
    agent = PersistentActionAgent(hub, lambda incoming: handled.append(incoming.message_id) or "Hi back")

    replies = agent.process_once()

    assert len(replies) == 1
    assert handled == ["m-1"]
    assert adapter.sent == [("chat-1", "Hi back")]


def test_default_hub_advertises_composed_channels():
    hub = build_default_hub()
    assert {"whatsapp", "x", "linkedin_public", "instagram", "slack", "discord", "teams", "telegram", "email", "voice"} <= set(hub.channels())


def test_default_hub_can_activate_existing_whatsapp_sender():
    sent = []
    hub = build_default_hub(whatsapp_sender=lambda contact, text: sent.append((contact, text)) or "sent")
    result = hub.send("whatsapp", "contact-1", "hello")
    assert result.ok and sent == [("contact-1", "hello")]


def test_callback_adapter_treats_unconfigured_sender_result_as_failed_delivery():
    hub = CommunicationHub()
    hub.register(CallbackAdapter("whatsapp", lambda _recipient, _text: "WhatsApp is NOT CONFIGURED"))

    result = hub.send("whatsapp", "chat-1", "hello")

    assert not result.ok
    assert result.detail == "WhatsApp is NOT CONFIGURED"


def test_default_webhook_adapters_can_be_polled_without_provider_workers():
    hub = build_default_hub()
    assert hub.poll() == []


def test_string_provider_result_is_normalized():
    class Adapter:
        name = "slack"
        def poll(self): return []
        def send_message(self, recipient, text): return "sent:123"
    hub = CommunicationHub(); hub.register(Adapter())
    result = hub.send("slack", "C1", "hello")
    assert result.ok and result.detail == "sent:123"



def test_delivery_errors_redact_webhook_urls(monkeypatch):
    webhook = "https://example.webhook.office.com/secret/path"
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", webhook)

    class Adapter:
        name = "teams"

        def poll(self):
            return []

        def send_message(self, recipient, text):
            raise RuntimeError(f"request failed for {webhook}")

    hub = CommunicationHub()
    hub.register(Adapter())

    result = hub.send("teams", "room-1", "hello")

    assert not result.ok
    assert webhook not in result.detail
    assert "[redacted]" in result.detail


def test_unconfigured_channel_returns_structured_failure():
    result = UnconfiguredChannel("whatsapp").send("chat-1", "hello")

    assert isinstance(result, DeliveryResult)
    assert result.channel == "whatsapp"
    assert result.conversation_id == "chat-1"
    assert result.ok is False
    assert result.detail == "whatsapp is not configured; message not sent."
