"""Provider-neutral communication composition for persistent action agents.

Connectors implement this small contract; the action agent can then choose a
channel without embedding provider-specific logic in its task loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
import re
from typing import Any, Protocol


def _safe_delivery_detail(value: str) -> str:
    detail = str(value)
    for key, secret in os.environ.items():
        normalized = key.upper()
        if secret and any(part in normalized for part in
                          ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCOUNT_SID", "WEBHOOK")):
            detail = detail.replace(secret, "[redacted]")
    detail = re.sub(r"(https?://api\.telegram\.org/bot)[^/\s]+", r"\1[redacted]", detail)
    return detail[:500]


@dataclass
class CommunicationMessage:
    channel: str
    conversation_id: str
    sender_id: str
    text: str
    message_id: str = ""
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeliveryResult:
    channel: str
    conversation_id: str
    ok: bool
    message_id: str = ""
    detail: str = ""


class CommunicationAdapter(Protocol):
    name: str

    def send_message(self, conversation_id: str, text: str) -> DeliveryResult: ...

    def poll(self) -> list[CommunicationMessage]: ...


class UnavailableAdapter:
    """Explicit adapter placeholder until provider credentials are configured."""

    def __init__(self, name: str):
        self.name = name

    def send_message(self, conversation_id: str, text: str) -> DeliveryResult:
        return DeliveryResult(self.name, conversation_id, False,
                              detail=f"{self.name} is not configured")

    def poll(self) -> list[CommunicationMessage]:
        return []


class CallbackAdapter:
    """Adapter for an already-implemented provider function."""

    def __init__(self, name: str, sender, poller=None):
        self.name = name
        self._sender = sender
        self._poller = poller or (lambda: [])

    def send_message(self, conversation_id: str, text: str) -> DeliveryResult:
        try:
            detail = str(self._sender(conversation_id, text))
            return DeliveryResult(self.name, conversation_id, True, detail=detail)
        except Exception as exc:
            return DeliveryResult(self.name, conversation_id, False, detail=str(exc))

    def poll(self) -> list[CommunicationMessage]:
        return list(self._poller())


class CommunicationHub:
    """Routes messages across channels; durable processing owns deduplication."""

    def __init__(self):
        self._adapters: dict[str, CommunicationAdapter] = {}

    def register(self, adapter: CommunicationAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def channels(self) -> list[str]:
        return sorted(self._adapters)

    def send(self, channel: str, conversation_id: str, text: str) -> DeliveryResult:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return DeliveryResult(channel, conversation_id, False,
                                  detail=f"unknown channel: {channel}")
        try:
            result = adapter.send_message(conversation_id, text)
        except Exception as exc:
            return DeliveryResult(channel, conversation_id, False,
                                  detail=_safe_delivery_detail(str(exc)))
        if isinstance(result, DeliveryResult):
            return DeliveryResult(result.channel, result.conversation_id, result.ok,
                                  result.message_id, _safe_delivery_detail(result.detail))
        detail = _safe_delivery_detail(str(result))
        ok = "not configured" not in detail.lower()
        return DeliveryResult(channel, conversation_id, ok, detail=detail)

    def send_many(self, targets: list[tuple[str, str]], text: str) -> list[DeliveryResult]:
        """Send one composed message to explicit (channel, recipient) targets."""
        results = []
        seen = set()
        for channel, recipient in targets:
            channel, recipient = str(channel).strip().lower(), str(recipient).strip()
            if not channel or not recipient or (channel, recipient) in seen:
                continue
            seen.add((channel, recipient))
            results.append(self.send(channel, recipient, text))
        return results

    def poll(self) -> list[CommunicationMessage]:
        messages: list[CommunicationMessage] = []
        for adapter in self._adapters.values():
            messages.extend(adapter.poll())
        return messages


DEFAULT_CHANNELS = ("whatsapp", "x", "x_public", "linkedin_public", "instagram", "instagram_public", "slack", "discord", "teams",
                    "telegram", "email", "voice")


def build_default_hub(whatsapp_sender=None, whatsapp_poller=None) -> CommunicationHub:
    from .channel_adapters import (DiscordAdapter, EmailAdapter, InstagramAdapter, InstagramPublicPostAdapter, LinkedInPublicPostAdapter, SlackAdapter, TeamsWebhookAdapter,
                                   TelegramAdapter, TwilioVoiceAdapter, WhatsAppCloudAdapter,
                                   WhatsAppWebAdapter, XAdapter, XPublicPostAdapter)
    hub = CommunicationHub()
    for channel in DEFAULT_CHANNELS:
        if channel == "whatsapp" and whatsapp_sender is not None:
            hub.register(CallbackAdapter(channel, whatsapp_sender, whatsapp_poller))
        elif channel == "whatsapp":
            import os
            if os.getenv("WHATSAPP_ACCESS_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID"):
                hub.register(WhatsAppCloudAdapter())
            else:
                hub.register(WhatsAppWebAdapter())
        elif channel == "telegram":
            hub.register(TelegramAdapter())
        elif channel == "slack":
            hub.register(SlackAdapter())
        elif channel == "discord":
            hub.register(DiscordAdapter())
        elif channel == "teams":
            hub.register(TeamsWebhookAdapter())
        elif channel == "email":
            hub.register(EmailAdapter())
        elif channel == "x":
            hub.register(XAdapter())
        elif channel == "x_public":
            hub.register(XPublicPostAdapter())
        elif channel == "linkedin_public":
            hub.register(LinkedInPublicPostAdapter())
        elif channel == "instagram":
            hub.register(InstagramAdapter())
        elif channel == "instagram_public":
            hub.register(InstagramPublicPostAdapter())
        elif channel == "voice":
            hub.register(TwilioVoiceAdapter())
        else:
            hub.register(UnavailableAdapter(channel))
    return hub
