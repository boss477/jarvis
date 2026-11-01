"""Provider-neutral inbound/outbound channel adapters.

Parsing is usable without credentials; network delivery is deliberately left to
provider-specific implementations.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any
import os
import re
import httpx
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit
from .remote_gateway import IncomingMessage, UnconfiguredChannel
from .communication_hub import DeliveryResult

@dataclass
class ChannelAdapter:
    name: str
    def parse(self, payload: dict[str, Any]) -> list[IncomingMessage]: raise NotImplementedError
    def sender(self, recipient: str, text: str) -> str | DeliveryResult: return UnconfiguredChannel(self.name).send(recipient, text)
    def poll(self) -> list: return []

class WhatsAppCloudAdapter(ChannelAdapter):
    def __init__(self): super().__init__("whatsapp")

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        message_type = message.get("type", "")
        if message_type == "text":
            return str(message.get("text", {}).get("body", "")).strip()
        if message_type == "interactive":
            interactive = message.get("interactive", {})
            for kind, label in (("button_reply", "Button reply"),
                                ("list_reply", "List reply")):
                reply = interactive.get(kind, {})
                title = str(reply.get("title", "")).strip()
                reply_id = str(reply.get("id", "")).strip()
                if title or reply_id:
                    selection = title or reply_id
                    if reply_id and title and reply_id != title:
                        selection += f" [id: {reply_id}]"
                    return f"{label}: {selection}"
        if message_type == "button":
            button = message.get("button", {})
            title = str(button.get("text", "")).strip()
            payload = str(button.get("payload", "")).strip()
            if title or payload:
                selection = title or payload
                if title and payload and payload != title:
                    selection += f" [id: {payload}]"
                return f"Button reply: {selection}"
        unsupported_media = {
            "image": "photo",
            "audio": "audio clip",
            "video": "video",
            "document": "document",
            "sticker": "sticker",
        }
        if message_type in unsupported_media:
            attachment = message.get(message_type, {})
            caption = str(attachment.get("caption", "")).strip()
            description = (f"WhatsApp {unsupported_media[message_type]} received; "
                           "its contents are not available to this agent.")
            return f"{description} Caption: {caption}" if caption else description
        return ""

    def parse(self, payload):
        messages = []
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                contact_names = {
                    str(contact.get("wa_id", "")): str(
                        (contact.get("profile") or {}).get("name", "")
                    ).strip()
                    for contact in (value.get("contacts", []) or [])
                    if isinstance(contact, dict)
                }
                for message in (value.get("messages", []) or []):
                    text = self._message_text(message)
                    sender = str(message.get("from", ""))
                    if text and sender:
                        metadata: dict[str, Any] = {}
                        display_name = contact_names.get(sender, "")
                        if display_name:
                            metadata["display_name"] = display_name[:120]
                        interactive = message.get("interactive") or {}
                        for key in ("button_reply", "list_reply"):
                            choice = interactive.get(key) or {}
                            choice_id = str(choice.get("id", "")).strip()
                            if choice_id:
                                metadata["interactive_reply_id"] = choice_id[:256]
                                break
                        if message.get("type") == "button":
                            choice_id = str((message.get("button") or {}).get("payload", "")).strip()
                            if choice_id:
                                metadata["interactive_reply_id"] = choice_id[:256]
                        context_id = str((message.get("context") or {}).get("id", "")).strip()
                        if context_id:
                            metadata["in_reply_to_message_id"] = context_id[:200]
                        messages.append(IncomingMessage("whatsapp", sender, text,
                            str(message.get("id", "")), sender, metadata))
        return messages

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        """Send a text reply through WhatsApp Cloud API when configured."""
        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        if not token or not phone_id:
            return UnconfiguredChannel(self.name).send(recipient, text)
        url = f"https://graph.facebook.com/v23.0/{phone_id}/messages"
        payload = {"messaging_product": "whatsapp", "to": recipient,
                   "type": "text", "text": {"body": text}}
        response = httpx.post(url, headers={"Authorization": f"Bearer {token}"},
                              json=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
        message_id = (data.get("messages") or [{}])[0].get("id", "")
        return f"sent:{message_id}" if message_id else "sent"

    def send_interactive_message(self, recipient: str, text: str,
                                 buttons: list[dict[str, str]]) -> str | DeliveryResult:
        """Send up to three WhatsApp reply buttons with opaque action IDs."""
        if len(text) > 1024:
            raise ValueError("WhatsApp interactive message body exceeds 1024 characters")
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp interactive messages require one to three reply buttons")
        validated = []
        for button in buttons:
            button_id = str(button.get("id", ""))
            title = str(button.get("title", ""))
            if not button_id or len(button_id) > 256:
                raise ValueError("WhatsApp reply button IDs must contain 1 to 256 characters")
            if not title or len(title) > 20:
                raise ValueError("WhatsApp reply button titles must contain 1 to 20 characters")
            validated.append({"type": "reply", "reply": {"id": button_id, "title": title}})

        token = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
        phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        if not token or not phone_id:
            return UnconfiguredChannel(self.name).send(recipient, text)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {"buttons": validated},
            },
        }
        response = httpx.post(
            f"https://graph.facebook.com/v23.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload, timeout=20,
        )
        response.raise_for_status()
        message_id = (response.json().get("messages") or [{}])[0].get("id", "")
        return f"sent:{message_id}" if message_id else "sent"


class WhatsAppWebAdapter(ChannelAdapter):
    """Use Friday's signed-in persistent Chrome profile for local WhatsApp sends."""

    def __init__(self):
        super().__init__("whatsapp")

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        from friday.mcp_server.chat import whatsapp_send

        result = whatsapp_send(recipient, text)
        if result.startswith("Sent to "):
            return result
        raise RuntimeError(result)

class TelegramAdapter(ChannelAdapter):
    def __init__(self): super().__init__("telegram")
    def parse(self, payload):
        message = payload.get("message", {})
        text = message.get("text", "")
        if not text: return []
        sender = message.get("from", {}).get("id", "")
        chat_id = str(message.get("chat", {}).get("id", sender))
        return [IncomingMessage("telegram", str(sender), text,
                                str(message.get("message_id", "")), chat_id)]

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return UnconfiguredChannel(self.name).send(recipient, text)
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": recipient, "text": text}, timeout=20,
        )
        response.raise_for_status()
        return "sent"


class SlackAdapter(ChannelAdapter):
    """Slack Events API parser and Web API sender."""

    def __init__(self): super().__init__("slack")

    def parse(self, payload):
        if payload.get("type") == "url_verification":
            return []
        event = payload.get("event", {})
        text = event.get("text", "")
        if not text or event.get("subtype") == "bot_message":
            return []
        return [IncomingMessage("slack", str(event.get("user", "")), text,
                                 str(event.get("event_ts", "")), str(event.get("channel", "")))]

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        token = os.getenv("SLACK_BOT_TOKEN", "")
        if not token:
            return UnconfiguredChannel(self.name).send(recipient, text)
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": recipient, "text": text}, timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "Slack send failed"))
        return str(data.get("ts", "sent"))


class DiscordAdapter(ChannelAdapter):
    """Send a message to a Discord channel using a bot token and channel ID."""

    def __init__(self): super().__init__("discord")

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        if not re.fullmatch(r"[0-9]{17,20}", recipient):
            raise ValueError("Discord channel ID must be a 17- to 20-digit numeric snowflake")
        token = os.getenv("DISCORD_BOT_TOKEN", "")
        if not token:
            return UnconfiguredChannel(self.name).send(recipient, text)
        response = httpx.post(
            f"https://discord.com/api/v10/channels/{recipient}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": text}, timeout=20,
        )
        response.raise_for_status()
        return str(response.json().get("id", "sent"))


class TeamsWebhookAdapter(ChannelAdapter):
    """Outbound Teams channel webhook adapter."""

    def __init__(self): super().__init__("teams")

    def parse(self, payload):
        text = payload.get("text", "")
        if not text: return []
        conversation = str(payload.get("conversation", {}).get("id", ""))
        sender = str(payload.get("from", {}).get("id", ""))
        return [IncomingMessage("teams", sender, text, str(payload.get("id", "")), conversation)]

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        url = os.getenv("TEAMS_WEBHOOK_URL", "")
        if not url:
            return UnconfiguredChannel(self.name).send(recipient, text)
        response = httpx.post(url, json={"text": text}, timeout=20)
        response.raise_for_status()
        return "sent"


class EmailAdapter(ChannelAdapter):
    """SMTP outbound adapter with a generic inbound webhook payload format."""

    def __init__(self): super().__init__("email")

    def parse(self, payload):
        sender = str(payload.get("from", payload.get("sender", "")))
        text = str(payload.get("text", payload.get("body", "")))
        if not sender or not text:
            return []
        subject = str(payload.get("subject", ""))
        if subject:
            text = f"Subject: {subject}\n\n{text}"
        return [IncomingMessage("email", sender, text,
                                str(payload.get("message_id", payload.get("id", ""))),
                                sender)]

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        host = os.getenv("SMTP_HOST", "")
        username = os.getenv("SMTP_USERNAME", "")
        password = os.getenv("SMTP_PASSWORD", "")
        sender = os.getenv("EMAIL_FROM", username)
        if not all((host, username, password, sender)):
            return UnconfiguredChannel(self.name).send(recipient, text)
        port = int(os.getenv("SMTP_PORT", "465"))
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = os.getenv("EMAIL_SUBJECT", "Message from Jarvis")
        message.set_content(text)
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                if os.getenv("SMTP_STARTTLS", "1").lower() not in {"0", "false", "no"}:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(message)
        return f"sent to {recipient}"


class XAdapter(ChannelAdapter):
    """X API v2 direct messaging and public posting adapter."""

    def __init__(self): super().__init__("x")

    def parse(self, payload):
        data = payload.get("data", {})
        text = data.get("text", "")
        if not text: return []
        author = str(data.get("author_id", ""))
        return [IncomingMessage("x", author, text, str(data.get("id", "")), author)]

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        token = os.getenv("X_USER_ACCESS_TOKEN", "")
        if not token:
            return UnconfiguredChannel(self.name).send(recipient, text)
        participant_id = recipient.strip()
        if not participant_id.isdigit():
            raise ValueError("X direct messages require the recipient's numeric user ID")
        response = httpx.post(
                              f"https://api.x.com/2/dm_conversations/with/{participant_id}/messages",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"text": text}, timeout=20)
        response.raise_for_status()
        data = response.json().get("data", {})
        return str(data.get("dm_event_id", data.get("id", "sent")))

    def create_post(self, text: str) -> str | DeliveryResult:
        token = os.getenv("X_USER_ACCESS_TOKEN", "")
        if not token:
            return UnconfiguredChannel(self.name).send("public", text)
        response = httpx.post("https://api.x.com/2/tweets",
                              headers={"Authorization": f"Bearer {token}"},
                              json={"text": text}, timeout=20)
        response.raise_for_status()
        return str(response.json().get("data", {}).get("id", "sent"))


class XPublicPostAdapter(ChannelAdapter):
    """Public X post destination, separate from the X direct-message channel."""

    def __init__(self):
        super().__init__("x_public")
        self._x = XAdapter()

    def parse(self, _payload):
        return []

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        if recipient.strip().lower() != "public":
            raise ValueError("X public posts require recipient 'public'")
        return self._x.create_post(text)


class LinkedInPublicPostAdapter(ChannelAdapter):
    """Publish a text-only LinkedIn member post through the Posts API."""

    def __init__(self): super().__init__("linkedin_public")

    def parse(self, _payload):
        return []

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        token = os.getenv("LINKEDIN_ACCESS_TOKEN", "")
        author = os.getenv("LINKEDIN_AUTHOR_URN", "")
        if not token or not author:
            return UnconfiguredChannel(self.name).send(recipient, text)
        if recipient.strip().lower() != "public":
            raise ValueError("LinkedIn public posts require recipient 'public'")
        if not re.fullmatch(r"urn:li:(?:person|organization):[A-Za-z0-9_-]+", author):
            raise ValueError("LINKEDIN_AUTHOR_URN must be a LinkedIn person or organization URN")
        version = os.getenv("LINKEDIN_API_VERSION", "202610")
        if not re.fullmatch(r"20\d{4}", version):
            raise ValueError("LINKEDIN_API_VERSION must use YYYYMM format")
        response = httpx.post(
            "https://api.linkedin.com/rest/posts",
            headers={
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": version,
                "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json",
            },
            json={
                "author": author,
                "commentary": text,
                "visibility": "PUBLIC",
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
            timeout=20,
        )
        response.raise_for_status()
        return str(response.headers.get("x-restli-id", "published"))


class InstagramAdapter(ChannelAdapter):
    """Instagram Graph API messaging adapter for approved business accounts."""

    def __init__(self): super().__init__("instagram")

    def parse(self, payload):
        messages = []
        for entry in payload.get("entry", []):
            for event in entry.get("messaging", []):
                message = event.get("message") or {}
                # Meta sends echoes of the account's own messages through the
                # messaging webhook. They are outbound events, not user input.
                if message.get("is_echo"):
                    continue
                text = message.get("text", "")
                if text:
                    sender = str(event.get("sender", {}).get("id", ""))
                    messages.append(IncomingMessage("instagram", sender, text,
                        str(message.get("mid", "")), sender))
        return messages

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
        account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")
        if not token or not account_id:
            return UnconfiguredChannel(self.name).send(recipient, text)
        response = httpx.post(
            f"https://graph.facebook.com/v23.0/{account_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"recipient": {"id": recipient}, "message": {"text": text}},
            timeout=20,
        )
        response.raise_for_status()
        return str(response.json().get("message_id", "sent"))


class InstagramPublicPostAdapter(ChannelAdapter):
    """Publish a captioned image to a professional Instagram account."""

    def __init__(self):
        super().__init__("instagram_public")

    def parse(self, _payload):
        return []

    def send_message(self, media_url: str, caption: str) -> str | DeliveryResult:
        token = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
        account_id = os.getenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "")
        if not token or not account_id:
            return UnconfiguredChannel(self.name).send(media_url, caption)
        parsed_url = urlsplit(media_url.strip())
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError("Instagram public posts require a publicly reachable HTTPS image URL")

        version = os.getenv("META_GRAPH_API_VERSION", "v25.0")
        base_url = f"https://graph.facebook.com/{version}/{account_id}"
        container_response = httpx.post(
            f"{base_url}/media",
            headers={"Authorization": f"Bearer {token}"},
            params={"image_url": media_url.strip(), "caption": caption},
            timeout=30,
        )
        container_response.raise_for_status()
        container_id = container_response.json().get("id")
        if not container_id:
            raise RuntimeError("Instagram did not return a media container ID")

        publish_response = httpx.post(
            f"{base_url}/media_publish",
            headers={"Authorization": f"Bearer {token}"},
            params={"creation_id": container_id},
            timeout=30,
        )
        publish_response.raise_for_status()
        media_id = publish_response.json().get("id")
        if not media_id:
            raise RuntimeError("Instagram did not return a published media ID")
        return str(media_id)


class TwilioVoiceAdapter(ChannelAdapter):
    """Place an approved Twilio call, optionally using the conversational webhook."""

    def __init__(self): super().__init__("voice")

    def parse(self, payload):
        return []

    def send_message(self, recipient: str, text: str) -> str | DeliveryResult:
        recipient = recipient.strip()
        text = text.strip()
        if not re.fullmatch(r"\+[1-9]\d{7,14}", recipient):
            raise ValueError("voice calls require an explicit E.164 phone number, e.g. +15551234567")
        if not text:
            raise ValueError("voice calls require an approved opening message and purpose")
        if len(text) > 2000:
            raise ValueError("voice call opener/purpose must be 2000 characters or fewer")
        sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        auth = os.getenv("TWILIO_AUTH_TOKEN", "")
        source = os.getenv("TWILIO_FROM_NUMBER", "")
        if not sid or not auth or not source:
            return UnconfiguredChannel(self.name).send(recipient, text)
        call_opener = "Hi, I'm an AI assistant calling on behalf of the account owner. " + text.strip()
        public_voice_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
        if public_voice_url:
            from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
            from .voice_webhook import (VOICE_SESSIONS, _voice_callback_url,
                                        is_public_https_url)

            parsed = urlsplit(public_voice_url)
            if not is_public_https_url(public_voice_url):
                raise ValueError("PUBLIC_VOICE_WEBHOOK_URL must be a public HTTPS URL")
            session_id = VOICE_SESSIONS.create(call_opener)
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            query["session"] = session_id
            call_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                   urlencode(query), ""))
            create_payload = {"To": recipient, "From": source, "Url": call_url,
                              "Method": "POST",
                              "StatusCallback": _voice_callback_url("/voice/status", session_id),
                              "StatusCallbackEvent": ["completed"],
                              "StatusCallbackMethod": "POST"}
        else:
            twiml = f"<Response><Say>{_xml_escape(call_opener)}</Say></Response>"
            create_payload = {"To": recipient, "From": source, "Twiml": twiml}
        try:
            response = httpx.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json",
                auth=(sid, auth), data=create_payload,
                timeout=20,
            )
            response.raise_for_status()
            call_sid = response.json().get("sid")
            if not call_sid:
                raise RuntimeError("Twilio accepted no call SID in its response")
        except httpx.HTTPStatusError:
            # An explicit HTTP rejection means the call was not created.
            if public_voice_url:
                VOICE_SESSIONS.discard(session_id)
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # Connection setup failed before Twilio could receive the request.
            if public_voice_url:
                VOICE_SESSIONS.discard(session_id)
            raise
        except Exception:
            # Read/write timeouts and response errors are ambiguous: Twilio may
            # have accepted the call before the client failed. Keep the session
            # so its webhook can still respond; TTL cleanup removes orphaned sessions.
            raise
        return f"call request accepted by Twilio; call SID {call_sid} (connection not yet confirmed)"


def _xml_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))
