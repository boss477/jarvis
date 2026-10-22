"""Loopback webhook server for remote channel adapters."""
from __future__ import annotations
import json
import base64
import hashlib
import hmac
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import time
from .channel_adapters import (EmailAdapter, InstagramAdapter, SlackAdapter, TeamsWebhookAdapter,
                               TelegramAdapter, WhatsAppCloudAdapter, XAdapter)
from .communication_hub import CommunicationMessage

logger = logging.getLogger(__name__)
MAX_WEBHOOK_BODY_BYTES = 1_048_576


def _normalise_phone_number(value: str) -> str:
    compact = re.sub(r"[+\s().-]", "", str(value).strip())
    return compact if re.fullmatch(r"[0-9]{7,15}", compact) else ""


def _whatsapp_allowed_senders() -> set[str]:
    configured = os.getenv("WHATSAPP_ALLOWED_SENDERS", "")
    return {number for item in configured.split(",")
            if (number := _normalise_phone_number(item))}


def whatsapp_sender_allowlist_configured() -> bool:
    """Whether at least one syntactically valid trusted sender is configured."""
    return bool(_whatsapp_allowed_senders())

class WebhookHandler(BaseHTTPRequestHandler):
    gateway = None
    token = ""
    health_provider = None
    adapters = {"telegram": TelegramAdapter(), "whatsapp": WhatsAppCloudAdapter(),
                "slack": SlackAdapter(), "teams": TeamsWebhookAdapter(),
                "instagram": InstagramAdapter(), "x": XAdapter(), "email": EmailAdapter()}

    def _reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        endpoint = self.path.split("?", 1)[0].rstrip("/")
        if endpoint == "/healthz":
            status = self.health_provider() if self.health_provider else {"ok": True}
            return self._reply(200 if status.get("ok") else 503, status)
        if endpoint != "/whatsapp":
            return self._reply(404, {"ok": False, "error": "unknown endpoint"})
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(self.path).query)
        mode = query.get("hub.mode", [""])[0]
        token = query.get("hub.verify_token", [""])[0]
        challenge = query.get("hub.challenge", [""])[0]
        verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
        if mode == "subscribe" and verify_token and hmac.compare_digest(token, verify_token):
            body = challenge.encode()
            self.send_response(200); self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        return self._reply(403, {"ok": False, "error": "webhook verification failed"})

    def do_POST(self):
        expected = f"Bearer {self.token}" if self.token else ""
        bearer_ok = bool(expected) and hmac.compare_digest(
            self.headers.get("Authorization", ""), expected
        )
        provider_auth = ((channel_has_meta_secret(self.path) and bool(os.getenv("META_APP_SECRET")))
                         or (self.path.rstrip("/").endswith("/slack") and bool(os.getenv("SLACK_SIGNING_SECRET")))
                         or (self.path.rstrip("/").endswith("/telegram") and bool(os.getenv("TELEGRAM_WEBHOOK_SECRET"))))
        if not bearer_ok and not provider_auth:
            return self._reply(401, {"ok": False, "error": "unauthorized"})
        channel = self.path.strip("/").split("/")[-1]
        adapter = self.adapters.get(channel)
        if adapter is None or self.gateway is None:
            return self._reply(404, {"ok": False, "error": "unknown channel"})
        try:
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reply(400, {"ok": False, "error": "invalid content length"})
            if size <= 0:
                return self._reply(400, {"ok": False, "error": "empty request body"})
            if size > MAX_WEBHOOK_BODY_BYTES:
                return self._reply(413, {"ok": False, "error": "request body too large"})
            body = self.rfile.read(size)
            if len(body) != size:
                return self._reply(400, {"ok": False, "error": "incomplete request body"})
            if not self._valid_provider_signature(channel, body):
                return self._reply(401, {"ok": False, "error": "invalid provider signature"})
            payload = json.loads(body)
            if channel == "slack" and payload.get("type") == "url_verification":
                return self._reply(200, {"challenge": payload.get("challenge", "")})
            messages = adapter.parse(payload)
            results = []
            allowed_senders = _whatsapp_allowed_senders() if channel == "whatsapp" else set()
            for message in messages:
                if (channel == "whatsapp"
                        and _normalise_phone_number(message.sender_id) not in allowed_senders):
                    results.append({"ok": True, "ignored": "sender_not_allowlisted"})
                    continue
                result = self.gateway.route_command(message)
                results.append(result)
                if result.get("retry"):
                    return self._reply(503, {"ok": False,
                                             "error": "temporary processing failure"})
                reply = result.get("reply")
                if not reply and result.get("task"):
                    task = result["task"]
                    reply = f"Task {task['id']}: {task['status']}"
                    result["reply"] = reply
                if reply and hasattr(adapter, "send_message"):
                    recipient = message.conversation_id or message.sender_id
                    actions = result.get("actions", [])
                    delivery_message = CommunicationMessage(
                        message.channel, recipient, message.sender_id, message.text,
                        message.message_id,
                    )
                    if (self.gateway.delivery_claim_handler
                            and not self.gateway.delivery_claim_handler(delivery_message)):
                        result["delivery"] = "duplicate suppressed"
                        continue
                    try:
                        if (channel == "whatsapp" and actions
                                and len(reply) <= 1024
                                and hasattr(adapter, "send_interactive_message")):
                            delivery = adapter.send_interactive_message(recipient, reply, actions)
                        else:
                            delivery = adapter.send_message(recipient, reply)
                    except Exception:
                        if self.gateway.delivery_handler:
                            try:
                                self.gateway.delivery_handler(
                                    delivery_message, "provider send failed", False,
                                )
                            except Exception as exc:
                                logger.warning("delivery failure persistence failed (%s)",
                                               type(exc).__name__)
                        raise
                    result["delivery"] = delivery
                    if self.gateway.delivery_handler:
                        try:
                            self.gateway.delivery_handler(
                                delivery_message,
                                str(delivery), "not configured" not in str(delivery).lower(),
                            )
                        except Exception as exc:
                            logger.warning("delivery status persistence failed (%s)",
                                           type(exc).__name__)
            return self._reply(200, {"ok": True, "results": results})
        except (ValueError, json.JSONDecodeError) as exc:
            return self._reply(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            logger.warning("webhook request failed (%s)", type(exc).__name__)
            return self._reply(503, {"ok": False, "error": "temporary webhook failure"})

    def _valid_provider_signature(self, channel: str, body: bytes) -> bool:
        if channel == "telegram":
            secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
            return not secret or hmac.compare_digest(
                self.headers.get("X-Telegram-Bot-Api-Secret-Token", ""), secret
            )
        if channel in {"whatsapp", "instagram"}:
            secret = os.getenv("META_APP_SECRET", "")
            if not secret:
                # A generic bearer token is not a substitute for Meta's
                # request signature. Fail closed even when bearer auth passed.
                return False
            supplied = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            return bool(supplied) and hmac.compare_digest(supplied, expected)
        if channel == "slack":
            secret = os.getenv("SLACK_SIGNING_SECRET", "")
            if not secret:
                return True
            timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
            supplied = self.headers.get("X-Slack-Signature", "")
            try:
                if abs(time.time() - int(timestamp)) > 300:
                    return False
            except ValueError:
                return False
            base = b"v0:" + timestamp.encode() + b":" + body
            expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
            return bool(supplied) and hmac.compare_digest(supplied, expected)
        return True

    def log_message(self, *_args): pass

def create_webhook_server(gateway, host="127.0.0.1", port=0, token="", health_provider=None):
    handler = type("ConfiguredWebhookHandler", (WebhookHandler,), {
        "gateway": gateway, "token": token, "health_provider": health_provider,
    })
    return ThreadingHTTPServer((host, port), handler)


def channel_has_meta_secret(path: str) -> bool:
    endpoint = path.split("?", 1)[0].rstrip("/")
    return endpoint.endswith("/whatsapp") or endpoint.endswith("/instagram")
