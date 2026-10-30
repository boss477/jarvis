"""Persistent action-agent loop built on the communication hub and task store."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Callable

from .communication_hub import CommunicationHub, CommunicationMessage, DeliveryResult
from .jsonl_store import append_jsonl_record, read_jsonl_records


def _message_dedupe_key(channel: str, conversation_id: str, message_id: str) -> str:
    """Build a stable idempotency key scoped to one provider conversation."""
    if not message_id:
        return ""
    return json.dumps([channel, conversation_id, message_id], ensure_ascii=False, separators=(",", ":"))


@dataclass
class AgentReply:
    message: CommunicationMessage
    reply: str
    delivery: DeliveryResult


class PersistentActionAgent:
    """Owns cross-channel conversation memory and reply policy.

    The handler receives an inbound message and returns a response. A later
    task orchestrator can replace the handler with autonomous task execution;
    channel-specific code remains outside this class.
    """

    def __init__(self, hub: CommunicationHub,
                 handler: Callable[[CommunicationMessage], str],
                 history_path: str | Path | None = None):
        self.hub = hub
        self.handler = handler
        self.history: dict[str, list[CommunicationMessage]] = {}
        self._seen_message_ids: set[str] = set()
        self._processing_message_ids: set[str] = set()
        self._cached_replies: dict[str, str] = {}
        self._delivery_results: dict[str, bool] = {}
        self._delivery_in_progress: set[str] = set()
        self.history_path = Path(history_path) if history_path else None
        self._history_lock = threading.RLock()
        interrupted: dict[str, CommunicationMessage] = {}
        if self.history_path and self.history_path.exists():
            for row in read_jsonl_records(self.history_path):
                message = CommunicationMessage(**row["message"])
                self.history.setdefault(row["conversation_key"], []).append(message)
                metadata = message.metadata or {}
                if (message.message_id and message.sender_id != "agent"
                        and metadata.get("processing_state") == "pending"):
                    interrupted[_message_dedupe_key(message.channel, message.conversation_id, message.message_id)] = message
                if (message.message_id and message.sender_id != "agent"
                        and metadata.get("processing_state") != "pending"):
                    self._seen_message_ids.add(_message_dedupe_key(message.channel, message.conversation_id, message.message_id))
                if metadata.get("event") == "processed":
                    inbound_id = str(metadata.get("inbound_message_id", ""))
                    if inbound_id:
                        dedupe_key = _message_dedupe_key(message.channel, message.conversation_id, inbound_id)
                        interrupted.pop(dedupe_key, None)
                        self._seen_message_ids.add(dedupe_key)
                        self._cached_replies[dedupe_key] = str(metadata.get("reply", ""))
                elif metadata.get("event") == "delivery_result":
                    inbound_id = str(metadata.get("inbound_message_id", ""))
                    if inbound_id:
                        self._delivery_results[_message_dedupe_key(message.channel, message.conversation_id, inbound_id)] = bool(
                            metadata.get("delivered", False)
                        )
        for dedupe_key, inbound in interrupted.items():
            warning = (
                "I was interrupted while handling that request. Check /pending or /check "
                "for any task already started, verify its destination, then tell me whether to retry."
            )
            self._seen_message_ids.add(dedupe_key)
            self._cached_replies[dedupe_key] = warning
            conversation_key = f"{inbound.channel}:{inbound.conversation_id}"
            self._remember(conversation_key, CommunicationMessage(
                channel=inbound.channel, conversation_id=inbound.conversation_id,
                sender_id="agent", text="", metadata={
                    "event": "processed", "inbound_message_id": inbound.message_id,
                    "reply": warning, "recovered_after_restart": True,
                },
            ))

    def process_once(self) -> list[AgentReply]:
        replies: list[AgentReply] = []
        for message in self.hub.poll():
            result = self.process_message(message, deliver=True)
            if result.reply:
                replies.append(result)
        return replies

    def process_message(self, message: CommunicationMessage, *, deliver: bool) -> AgentReply:
        """Process either a polled event or an HTTP webhook event once."""
        key = f"{message.channel}:{message.conversation_id}"
        dedupe_key = _message_dedupe_key(message.channel, message.conversation_id, message.message_id)
        duplicate_reply = None
        with self._history_lock:
            if dedupe_key and dedupe_key in self._seen_message_ids:
                duplicate_reply = self._cached_replies.get(dedupe_key, "")
            elif dedupe_key and dedupe_key in self._processing_message_ids:
                return AgentReply(message, "", DeliveryResult(message.channel,
                    message.conversation_id, True, detail="duplicate is already processing"))
            elif dedupe_key:
                self._processing_message_ids.add(dedupe_key)
        if duplicate_reply is not None:
            already_delivered = self._delivery_results.get(dedupe_key, False)
            if deliver or already_delivered:
                return AgentReply(message, "", DeliveryResult(
                    message.channel, message.conversation_id, True,
                    detail=("duplicate ignored" if deliver else "duplicate already delivered")))
            return AgentReply(message, duplicate_reply, DeliveryResult(
                message.channel, message.conversation_id, True,
                detail="duplicate; cached reply available for webhook redelivery"))
        if dedupe_key:
            message.metadata = {**(message.metadata or {}), "processing_state": "pending"}
        try:
            self._remember(key, message)
            response = self.handler(message)
            delivery = (self.hub.send(message.channel, message.conversation_id, response)
                        if deliver and response else DeliveryResult(
                            message.channel, message.conversation_id, True,
                            detail="delivery pending webhook" if response else "no reply"))
            if response:
                reply_message = CommunicationMessage(
                    channel=message.channel, conversation_id=message.conversation_id,
                    sender_id="agent", text=response, message_id=delivery.message_id,
                    metadata={"direction": "outbound", "delivered": deliver and delivery.ok,
                              "detail": delivery.detail},
                )
                self._remember(key, reply_message)
            if dedupe_key:
                self._remember(key, CommunicationMessage(
                    channel=message.channel, conversation_id=message.conversation_id,
                    sender_id="agent", text="", metadata={
                        "event": "processed", "inbound_message_id": message.message_id,
                        "reply": response,
                    },
                ))
                with self._history_lock:
                    self._seen_message_ids.add(dedupe_key)
                    self._cached_replies[dedupe_key] = response
            return AgentReply(message, response, delivery)
        finally:
            if dedupe_key:
                with self._history_lock:
                    self._processing_message_ids.discard(dedupe_key)

    def record_delivery(self, message: CommunicationMessage, detail: str, ok: bool) -> None:
        """Persist delivery confirmation for the latest reply to a webhook event."""
        if message.message_id:
            with self._history_lock:
                dedupe_key = _message_dedupe_key(message.channel, message.conversation_id, message.message_id)
                self._delivery_results[dedupe_key] = ok
                self._delivery_in_progress.discard(dedupe_key)
        key = f"{message.channel}:{message.conversation_id}"
        event = CommunicationMessage(
            channel=message.channel, conversation_id=message.conversation_id,
            sender_id="agent", text="", metadata={"event": "delivery_result",
            "inbound_message_id": message.message_id, "delivered": ok, "detail": detail},
        )
        self._remember(key, event)

    def claim_delivery(self, message: CommunicationMessage) -> bool:
        """Reserve one webhook reply send for this inbound provider message."""
        if not message.message_id:
            return True
        dedupe_key = _message_dedupe_key(message.channel, message.conversation_id, message.message_id)
        with self._history_lock:
            if self._delivery_results.get(dedupe_key, False):
                return False
            if dedupe_key in self._delivery_in_progress:
                return False
            self._delivery_in_progress.add(dedupe_key)
            return True

    def recent_history(self, channel: str, conversation_id: str, *, limit: int = 12,
                       exclude_message_id: str = "") -> list[CommunicationMessage]:
        """Return recent user-facing turns, excluding internal journal events."""
        key = f"{channel}:{conversation_id}"
        with self._history_lock:
            messages = self.history.get(key, ())
            turns = [
                message for message in messages
                if message.text.strip()
                and not (message.metadata or {}).get("event")
                and not (exclude_message_id and message.message_id == exclude_message_id
                         and message.sender_id != "agent")
            ]
            return list(turns[-limit:]) if limit > 0 else []

    def _remember(self, key: str, message: CommunicationMessage) -> None:
        with self._history_lock:
            self.history.setdefault(key, []).append(message)
            if self.history_path:
                append_jsonl_record(self.history_path, {
                    "conversation_key": key, "message": message.__dict__,
                })
