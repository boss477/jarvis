import pytest

from friday.services.channel_adapters import (DiscordAdapter, InstagramAdapter, InstagramPublicPostAdapter,
                                               LinkedInPublicPostAdapter, SlackAdapter, TelegramAdapter,
                                               TeamsWebhookAdapter, TwilioVoiceAdapter,
                                               WhatsAppCloudAdapter, XAdapter)

def test_whatsapp_cloud_payload_parses_text():
    payload = {"entry": [{"changes": [{"value": {
        "contacts": [{"wa_id": "u1", "profile": {"name": "Fawaz"}}],
        "messages": [{"id": "m1", "from": "u1", "type": "text",
                      "text": {"body": "/check t1"}, "context": {"id": "m0"}}],
    }}]}]}
    message = WhatsAppCloudAdapter().parse(payload)[0]
    assert (message.channel, message.sender_id, message.text) == ("whatsapp", "u1", "/check t1")
    assert message.metadata == {"display_name": "Fawaz", "in_reply_to_message_id": "m0"}

def test_whatsapp_interactive_reply_preserves_machine_id():
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "id": "m1", "from": "u1", "type": "interactive",
        "interactive": {"button_reply": {"id": "confirm_send", "title": "Confirm"}},
    }]}}]}]}
    message = WhatsAppCloudAdapter().parse(payload)[0]
    assert message.text == "Button reply: Confirm [id: confirm_send]"

def test_whatsapp_button_reply_preserves_payload_id():
    message = WhatsAppCloudAdapter().parse({"entry": [{"changes": [{"value": {"messages": [{
        "id": "m1", "from": "u1", "type": "button",
        "button": {"text": "Yes", "payload": "approve_42"},
    }]}}]}]})[0]
    assert message.text == "Button reply: Yes [id: approve_42]"
    assert message.metadata["interactive_reply_id"] == "approve_42"


def test_whatsapp_sends_structured_reply_buttons(monkeypatch):
    import friday.services.channel_adapters as adapters

    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "phone-id")
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"messages": [{"id": "wamid.outbound"}]}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(adapters.httpx, "post", post)
    result = WhatsAppCloudAdapter().send_interactive_message(
        "15551234567", "Approve task?", [
            {"id": "task:approve:abcdef012345", "title": "Approve"},
            {"id": "task:deny:abcdef012345", "title": "Deny"},
        ],
    )

    assert result == "sent:wamid.outbound"
    assert captured["json"]["type"] == "interactive"
    assert captured["json"]["interactive"]["body"] == {"text": "Approve task?"}
    assert captured["json"]["interactive"]["action"]["buttons"][0] == {
        "type": "reply", "reply": {"id": "task:approve:abcdef012345", "title": "Approve"},
    }


def test_whatsapp_interactive_buttons_validate_provider_limits(monkeypatch):
    adapter = WhatsAppCloudAdapter()
    with pytest.raises(ValueError, match="1024"):
        adapter.send_interactive_message("u1", "x" * 1025, [{"id": "ok", "title": "OK"}])
    with pytest.raises(ValueError, match="titles"):
        adapter.send_interactive_message("u1", "body", [{"id": "ok", "title": "A" * 21}])

def test_whatsapp_cloud_without_credentials_is_explicitly_unconfigured(monkeypatch):
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    assert "not configured" in WhatsAppCloudAdapter().send_message("u1", "hello")

def test_slack_events_parse_text():
    payload = {"event": {"type": "message", "channel": "C1", "text": "hello", "event_ts": "1"}}
    parsed = SlackAdapter().parse(payload)[0]
    assert parsed.conversation_id == "C1"

def test_discord_without_credentials_is_explicitly_unconfigured(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    result = DiscordAdapter().send_message("123456789012345678", "hello")
    assert "not configured" in result

def test_discord_sends_to_explicit_channel_id(monkeypatch):
    import friday.services.channel_adapters as adapters

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-token")
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"id": "message-1"}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(adapters.httpx, "post", post)
    result = DiscordAdapter().send_message("123456789012345678", "hello")
    assert result == "message-1"
    assert seen["url"].endswith("/channels/123456789012345678/messages")
    assert seen["headers"]["Authorization"] == "Bot test-token"
    assert seen["json"] == {"content": "hello"}

def test_linkedin_text_post_uses_configured_author_and_version(monkeypatch):
    import friday.services.channel_adapters as adapters

    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "urn:li:person:member-123")
    monkeypatch.setenv("LINKEDIN_API_VERSION", "202610")
    seen = {}

    class Response:
        headers = {"x-restli-id": "urn:li:share:post-1"}
        def raise_for_status(self): pass

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(adapters.httpx, "post", post)
    result = LinkedInPublicPostAdapter().send_message("public", "Hello LinkedIn")
    assert result == "urn:li:share:post-1"
    assert seen["url"] == "https://api.linkedin.com/rest/posts"
    assert seen["headers"]["Linkedin-Version"] == "202610"
    assert seen["headers"]["X-Restli-Protocol-Version"] == "2.0.0"
    assert seen["json"]["author"] == "urn:li:person:member-123"
    assert seen["json"]["commentary"] == "Hello LinkedIn"

def test_linkedin_public_post_requires_public_target_and_valid_author(monkeypatch):
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "bad-author")
    with pytest.raises(ValueError, match="AUTHOR_URN"):
        LinkedInPublicPostAdapter().send_message("public", "hello")
    monkeypatch.setenv("LINKEDIN_AUTHOR_URN", "urn:li:person:123")
    with pytest.raises(ValueError, match="recipient 'public'"):
        LinkedInPublicPostAdapter().send_message("someone", "hello")

def test_teams_payload_parses_text():
    message = TeamsWebhookAdapter().parse({"id": "m1", "text": "hello", "conversation": {"id": "c1"}})[0]
    assert message.conversation_id == "c1"

def test_telegram_without_credentials_is_explicitly_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert "not configured" in TelegramAdapter().send_message("u1", "hello")

def test_telegram_uses_group_chat_as_reply_conversation():
    message = TelegramAdapter().parse({"message": {
        "from": {"id": 7}, "chat": {"id": -1001}, "message_id": 2, "text": "hello"
    }})[0]
    assert message.sender_id == "7" and message.conversation_id == "-1001"

def test_x_without_credentials_is_explicitly_unconfigured(monkeypatch):
    monkeypatch.delenv("X_USER_ACCESS_TOKEN", raising=False)
    assert "not configured" in XAdapter().send_message("u1", "hello")

def test_instagram_payload_parses_text():
    payload = {"entry": [{"messaging": [{"sender": {"id": "u1"},
                "message": {"mid": "m1", "text": "hello"}}]}]}
    assert InstagramAdapter().parse(payload)[0].sender_id == "u1"

def test_instagram_message_uses_bearer_header(monkeypatch):
    import friday.services.channel_adapters as adapters

    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "account-123")
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"message_id": "message-1"}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(adapters.httpx, "post", post)
    assert InstagramAdapter().send_message("recipient-1", "hello") == "message-1"
    assert seen["headers"]["Authorization"] == "Bearer test-token"
    assert "access_token" not in seen["url"]
    assert "access_token" not in seen.get("params", {})
    assert seen["json"] == {"recipient": {"id": "recipient-1"}, "message": {"text": "hello"}}


def test_instagram_public_post_uses_bearer_header_for_both_requests(monkeypatch):
    import friday.services.channel_adapters as adapters

    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("INSTAGRAM_BUSINESS_ACCOUNT_ID", "account-123")
    seen = []

    class Response:
        def __init__(self, payload): self.payload = payload
        def raise_for_status(self): pass
        def json(self): return self.payload

    def post(url, **kwargs):
        seen.append((url, kwargs))
        if url.endswith("/media"):
            return Response({"id": "container-1"})
        if url.endswith("/media_publish"):
            return Response({"id": "published-1"})
        raise AssertionError(f"unexpected Instagram endpoint: {url}")

    monkeypatch.setattr(adapters.httpx, "post", post)
    result = InstagramPublicPostAdapter().send_message("https://example.test/photo.jpg", "caption")

    assert result == "published-1"
    assert len(seen) == 2
    for url, kwargs in seen:
        assert kwargs["headers"]["Authorization"] == "Bearer test-token"
        assert "access_token" not in url
        assert "access_token" not in kwargs.get("params", {})
    assert seen[0][1]["params"] == {
        "image_url": "https://example.test/photo.jpg", "caption": "caption",
    }
    assert seen[1][1]["params"] == {"creation_id": "container-1"}

def test_voice_without_credentials_is_explicitly_unconfigured(monkeypatch):
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        monkeypatch.delenv(key, raising=False)
    assert "not configured" in TwilioVoiceAdapter().send_message("+15550000000", "hello")

def test_telegram_update_parses_text():
    message = TelegramAdapter().parse({"message": {"message_id": 2, "from": {"id": 7}, "text": "hello"}})[0]
    assert message.sender_id == "7" and message.text == "hello"

def test_non_text_payloads_are_ignored():
    assert WhatsAppCloudAdapter().parse({"entry": []}) == []
    assert TelegramAdapter().parse({"message": {}}) == []
