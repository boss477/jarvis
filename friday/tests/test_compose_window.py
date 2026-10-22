from friday.services.compose_window import ComposeWindow
import pytest


def test_compose_destination_requires_registered_channel_and_recipient():
    assert ComposeWindow.validate_target(" Slack ", " C123 ", {"slack", "teams"}) == {
        "channel": "slack", "recipient": "C123",
    }
    with pytest.raises(ValueError, match="Unknown channel"):
        ComposeWindow.validate_target("linkedin", "public", {"linkedin_public"})
    with pytest.raises(ValueError, match="recipient"):
        ComposeWindow.validate_target("slack", "  ", {"slack"})


def test_readiness_guides_whatsapp_sender_allowlist_setup(monkeypatch):
    for key in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                "WHATSAPP_VERIFY_TOKEN", "META_APP_SECRET", "WHATSAPP_ALLOWED_SENDERS"):
        monkeypatch.delenv(key, raising=False)

    status = ComposeWindow._readiness_summary()

    assert "WHATSAPP_ALLOWED_SENDERS" in status
    assert "+<countrycode><number>" in status
    assert "restart the persistent agent" in status
