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


def test_refresh_pending_prefers_newly_prepared_task_over_previous_selection():
    from types import SimpleNamespace

    class Value:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    old_task = SimpleNamespace(
        id="old-task", status=SimpleNamespace(value="waiting_approval"),
        steps=[SimpleNamespace(tool="compose_send")], updated_at="2026-10-29T10:00:00Z",
        prompt="old message",
    )
    new_task = SimpleNamespace(
        id="new-task", status=SimpleNamespace(value="waiting_approval"),
        steps=[SimpleNamespace(tool="compose_send")], updated_at="2026-10-30T10:00:00Z",
        prompt="new message",
    )
    window = ComposeWindow.__new__(ComposeWindow)
    window.runtime = SimpleNamespace(tasks=SimpleNamespace(list=lambda: [new_task, old_task]))
    window.pending_task = Value("old-task | waiting_approval | old message")
    window.pending_picker = {}
    window._show_selected_task = lambda: None

    window._refresh_pending(preferred_task_id="new-task")

    assert window.pending_task.get().startswith("new-task | waiting_approval | ")
