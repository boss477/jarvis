from pathlib import Path

from friday.services.compose_window import ComposeWindow, _configured_state_path
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


def test_refresh_setup_removes_deleted_dotenv_keys_and_preserves_external_values(
    monkeypatch, tmp_path
):
    import os

    env_path = tmp_path / ".env"
    env_path.write_text(
        "JARVIS_REMOVED_KEY=from-dotenv\nJARVIS_EXTERNAL_KEY=from-dotenv\n",
        encoding="utf-8",
    )
    for key in ("JARVIS_REMOVED_KEY", "JARVIS_EXTERNAL_KEY", "JARVIS_NEW_KEY"):
        # Register the pre-test state even when a key was originally absent, so
        # _refresh_setup's direct os.environ writes are undone at teardown.
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.setenv("JARVIS_EXTERNAL_KEY", "shell-value")

    window = ComposeWindow.__new__(ComposeWindow)
    window.env_path = env_path
    window._external_environment_keys = set(os.environ)
    window._dotenv_keys = {"JARVIS_REMOVED_KEY", "JARVIS_EXTERNAL_KEY"}
    # Simulate a key loaded from .env at startup after external keys were captured.
    monkeypatch.setenv("JARVIS_REMOVED_KEY", "old-dotenv-value")

    class Value:
        def __init__(self):
            self.value = ""

        def set(self, value):
            self.value = value

    window.readiness = Value()
    window.status = Value()
    monkeypatch.setattr(ComposeWindow, "_readiness_summary", staticmethod(lambda: "ready"))
    env_path.write_text(
        "JARVIS_EXTERNAL_KEY=from-dotenv\nJARVIS_NEW_KEY=new-value\n",
        encoding="utf-8",
    )

    window._refresh_setup()

    assert "JARVIS_REMOVED_KEY" not in os.environ
    assert os.environ["JARVIS_EXTERNAL_KEY"] == "shell-value"
    assert os.environ["JARVIS_NEW_KEY"] == "new-value"
    assert window._dotenv_keys == {"JARVIS_EXTERNAL_KEY", "JARVIS_NEW_KEY"}
    assert window.readiness.value.startswith("Saved configuration (restart composer to apply): ready")
    assert "Close and reopen the composer" in window.status.value
    assert "rebuild channel adapters" in window.status.value

@pytest.mark.parametrize(
    ("targets", "length", "valid"),
    [
        ([{"channel": "x_public", "recipient": "public"}], 280, True),
        ([{"channel": "x_public", "recipient": "public"}], 281, False),
        ([{"channel": "voice", "recipient": "+15551234567"}], 1200, True),
        ([{"channel": "voice", "recipient": "+15551234567"}], 1201, False),
        ([{"channel": "whatsapp", "recipient": "15551234567"}], 1600, True),
        ([{"channel": "whatsapp", "recipient": "15551234567"}], 1601, False),
    ],
)
def test_compose_text_must_fit_complete_approval_preview(targets, length, valid):
    text = "x" * length
    if valid:
        ComposeWindow.validate_compose_text(text, targets)
    else:
        with pytest.raises(ValueError, match="approval prompt must show the complete text"):
            ComposeWindow.validate_compose_text(text, targets)



def test_configured_state_path_matches_host_environment(monkeypatch):
    monkeypatch.setenv("AGENT_TASKS_PATH", "custom-state/tasks.jsonl")

    assert _configured_state_path() == (
        Path(__file__).resolve().parents[2] / "custom-state" / "tasks.jsonl"
    )
