import os
import pytest
import httpx

from friday.services import agent_host
from friday.services.agent_host import (AgentHostLock, _loopback_bind_host,
                                        _setup_status)


def test_only_one_host_can_own_a_task_store(tmp_path):
    state_path = tmp_path / "tasks.jsonl"
    first = AgentHostLock(state_path)
    try:
        with pytest.raises(RuntimeError, match="another persistent-agent host"):
            AgentHostLock(state_path)
    finally:
        first.close()

    second = AgentHostLock(state_path)
    second.close()


def test_setup_status_distinguishes_whatsapp_cloud_send_from_inbound(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "configured")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "configured")
    for key in ("WHATSAPP_VERIFY_TOKEN", "META_APP_SECRET", "WHATSAPP_ALLOWED_SENDERS"):
        monkeypatch.delenv(key, raising=False)

    status = _setup_status()

    assert status["whatsapp_cloud_send_configured"]
    assert not status["whatsapp_inbound_configured"]
    assert status["whatsapp_inbound_missing"] == [
        "WHATSAPP_VERIFY_TOKEN", "META_APP_SECRET", "WHATSAPP_ALLOWED_SENDERS",
    ]
    assert status["whatsapp_web_fallback_supported"] == (os.name == "nt")


def test_setup_status_never_returns_configured_secret_values(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "secret-token-value")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "phone-id")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret")
    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", "+15551234567")

    status = _setup_status()

    assert status["whatsapp_cloud_send_configured"]
    assert status["whatsapp_inbound_configured"]
    assert "secret-token-value" not in repr(status)
    assert "verify-secret" not in repr(status)
    assert "app-secret" not in repr(status)


def test_listener_bind_hosts_allow_only_loopback(monkeypatch):
    monkeypatch.delenv("AGENT_WEBHOOK_HOST", raising=False)
    assert _loopback_bind_host("AGENT_WEBHOOK_HOST") == "127.0.0.1"
    monkeypatch.setenv("AGENT_WEBHOOK_HOST", "  ")
    assert _loopback_bind_host("AGENT_WEBHOOK_HOST") == "127.0.0.1"

    monkeypatch.setenv("AGENT_WEBHOOK_HOST", "127.0.0.2")
    assert _loopback_bind_host("AGENT_WEBHOOK_HOST") == "127.0.0.2"
    monkeypatch.setenv("AGENT_WEBHOOK_HOST", "localhost")
    assert _loopback_bind_host("AGENT_WEBHOOK_HOST") == "localhost"

    for host in ("0.0.0.0", "192.168.1.20", "::1", "example.com"):
        monkeypatch.setenv("AGENT_WEBHOOK_HOST", host)
        with pytest.raises(ValueError, match="loopback"):
            _loopback_bind_host("AGENT_WEBHOOK_HOST")


def test_setup_status_flags_non_loopback_listener_configuration(monkeypatch):
    monkeypatch.setenv("VOICE_WEBHOOK_HOST", "0.0.0.0")

    assert not _setup_status()["loopback_bindings_ok"]


def test_local_llm_check_probes_models_without_printing_api_key(monkeypatch, capsys):
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "local-model")
    monkeypatch.setenv("LLM_API_KEY", "do-not-print-this")
    captured = {}

    class Response:
        def raise_for_status(self): pass
        def json(self): return {"data": [{"id": "local-model"}]}

    def get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return Response()

    monkeypatch.setattr(httpx, "get", get)

    assert agent_host._check_local_llm()
    assert captured["url"] == "http://127.0.0.1:1234/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer do-not-print-this"
    assert "do-not-print-this" not in capsys.readouterr().out


def test_local_llm_check_skips_remote_endpoint_without_sending_credentials(monkeypatch, capsys):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "remote-model")
    monkeypatch.setenv("LLM_API_KEY", "do-not-send-this")
    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: pytest.fail("remote probe attempted"))

    assert not agent_host._check_local_llm()
    output = capsys.readouterr().out
    assert "probing was skipped" in output
    assert "do-not-send-this" not in output
