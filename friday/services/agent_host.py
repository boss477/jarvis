"""Run the persistent action agent and its provider webhook listener."""
from __future__ import annotations

import os
import argparse
import ipaddress
import threading
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .agent_factory import build_persistent_agent
from .agent_planner import plan_with_openai_compatible_model
from .remote_http import create_webhook_server, whatsapp_sender_allowlist_configured


class AgentHostLock:
    """Keep one host process responsible for recovering and serving a task store."""

    def __init__(self, state_path: str | Path, suffix: str = ".host.lock"):
        self.path = Path(f"{state_path}{suffix}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        try:
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise RuntimeError("another persistent-agent host already owns this task store") from exc

    def close(self):
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def _whatsapp_inbound_missing() -> list[str]:
    required = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID",
                "WHATSAPP_VERIFY_TOKEN", "META_APP_SECRET")
    missing = [key for key in required if not os.getenv(key)]
    if not whatsapp_sender_allowlist_configured():
        missing.append("WHATSAPP_ALLOWED_SENDERS")
    return missing


def _whatsapp_cloud_send_missing() -> list[str]:
    return [key for key in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")
            if not os.getenv(key)]


def _loopback_bind_host(setting: str, default: str = "127.0.0.1") -> str:
    """Resolve a configured listener host while refusing non-loopback exposure."""
    host = os.getenv(setting, "").strip() or default
    if host.lower() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"{setting} must be localhost or a loopback IP address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
        raise ValueError(f"{setting} must be localhost or a loopback IPv4 address")
    return host


def _setup_status() -> dict[str, object]:
    """Return a credential-safe setup summary shared by startup and health checks."""
    from .voice_webhook import is_public_https_url

    inbound_missing = _whatsapp_inbound_missing()
    send_missing = _whatsapp_cloud_send_missing()
    voice_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
    voice_credentials = all(os.getenv(key) for key in
                            ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"))
    try:
        _loopback_bind_host("AGENT_WEBHOOK_HOST")
        _loopback_bind_host("VOICE_WEBHOOK_HOST")
        loopback_bindings_ok = True
    except ValueError:
        loopback_bindings_ok = False
    return {
        "whatsapp_cloud_send_configured": not send_missing,
        "whatsapp_cloud_send_missing": send_missing,
        "whatsapp_inbound_configured": not inbound_missing,
        "whatsapp_inbound_missing": inbound_missing,
        "whatsapp_web_fallback_supported": os.name == "nt",
        "voice_credentials_configured": voice_credentials,
        "voice_webhook_configured": bool(voice_credentials and is_public_https_url(voice_url)),
        "voice_webhook_url_valid": is_public_https_url(voice_url),
        "loopback_bindings_ok": loopback_bindings_ok,
    }


def _print_setup_status() -> None:
    import os
    from .voice_webhook import is_public_https_url

    status = _setup_status()
    inbound_missing = status["whatsapp_inbound_missing"]
    if inbound_missing:
        print("WhatsApp inbound setup incomplete; missing: " + ", ".join(inbound_missing))
    else:
        print("WhatsApp webhook credentials are present; public HTTPS callback setup is still required.")

    send_missing = status["whatsapp_cloud_send_missing"]
    if send_missing:
        print("WhatsApp Cloud API outbound is not configured; missing: " + ", ".join(send_missing))
        print("The Windows composer can use WhatsApp Web for outbound sends when Chrome is signed in.")
    else:
        print("WhatsApp Cloud API outbound credentials are present.")

    voice_required = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"]
    voice_missing = [key for key in voice_required if not os.getenv(key)]
    voice_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
    if voice_missing:
        print("Twilio voice setup incomplete; missing: " + ", ".join(voice_missing))
    elif not voice_url:
        print("Twilio voice is ready for one-way calls; set PUBLIC_VOICE_WEBHOOK_URL for AI conversation.")
    elif not is_public_https_url(voice_url):
        print("Conversational voice is disabled: PUBLIC_VOICE_WEBHOOK_URL must be a public HTTPS URL.")
    else:
        print("Conversational voice callback enabled; confirm public HTTPS routing and LLM availability.")


def _check_local_llm() -> bool:
    """Probe only loopback model servers; never send configured credentials remotely."""
    import httpx

    base_url = os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1").strip().rstrip("/")
    model = os.getenv("LLM_MODEL", "").strip()
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        print("LLM endpoint is invalid; set LLM_BASE_URL to an OpenAI-compatible /v1 endpoint.")
        return False
    if parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}:
        print("LLM endpoint is remote; active probing was skipped to avoid sending credentials.")
        print("LLM_MODEL configured: " + ("yes" if model else "no"))
        return False
    if not model:
        print("LLM_MODEL is missing from friday/.env.")
        return False
    try:
        response = httpx.get(
            f"{base_url}/models",
            headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY', 'local')}"},
            timeout=httpx.Timeout(3.0, connect=1.0),
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"Local LLM endpoint is unreachable ({type(exc).__name__}); start its server at {base_url}.")
        return False
    try:
        data = response.json().get("data", [])
        model_ids = {str(item.get("id", "")) for item in data if isinstance(item, dict)}
    except (ValueError, AttributeError):
        model_ids = set()
    if model_ids and model not in model_ids:
        print("Local LLM endpoint is reachable, but LLM_MODEL does not match a listed model ID.")
        return False
    print("Local LLM endpoint is reachable; LLM_MODEL is configured.")
    return True


def _check_setup() -> int:
    _print_setup_status()
    llm_ok = _check_local_llm()
    whatsapp_ok = not _whatsapp_inbound_missing()
    bindings_ok = bool(_setup_status()["loopback_bindings_ok"])
    if not bindings_ok:
        print("Webhook listeners must bind only to localhost or a loopback IPv4 address.")
    if not whatsapp_ok:
        print("WhatsApp inbound is not ready; add the listed values to friday/.env before connecting Meta.")
    if llm_ok and whatsapp_ok and bindings_ok:
        print("Persistent WhatsApp agent prerequisites are configured; start it manually for a local health check.")
        return 0
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Persistent WhatsApp and channel agent host")
    parser.add_argument("--check", action="store_true",
                        help="check WhatsApp and local LLM setup without starting the host")
    args = parser.parse_args()
    env_file = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_file)
    if args.check:
        raise SystemExit(_check_setup())
    state_path = Path(os.getenv("AGENT_TASKS_PATH", "agent-state/tasks.jsonl"))
    host_lock = AgentHostLock(state_path)
    try:
        _run_agent_host(state_path)
    finally:
        host_lock.close()


def _run_agent_host(state_path: Path) -> None:
    from .voice_webhook import is_public_https_url

    webhook_host = _loopback_bind_host("AGENT_WEBHOOK_HOST")
    voice_host = _loopback_bind_host("VOICE_WEBHOOK_HOST")
    runtime = build_persistent_agent(plan_with_openai_compatible_model, state_path=state_path)
    recovered = runtime.tasks.recover_interrupted_tasks()
    if recovered:
        print(f"Recovered {len(recovered)} interrupted task(s); check destination state before retrying.")
    voice_server = None
    voice_thread = None
    server = create_webhook_server(
        runtime.webhook_gateway,
        host=webhook_host,
        port=int(os.getenv("AGENT_WEBHOOK_PORT", "8767")),
        token=os.getenv("AGENT_WEBHOOK_TOKEN", ""),
        health_provider=lambda: {
            "ok": runtime.running,
            "agent_running": runtime.running,
            "consecutive_worker_errors": runtime.consecutive_errors,
            "last_error_present": bool(runtime.last_error),
            **_setup_status(),
            "voice_webhook_running": bool(voice_server and voice_thread and voice_thread.is_alive()),
        },
    )
    voice_cleanup_stop = threading.Event()
    voice_cleanup_thread = None
    if os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip():
        from .agent_planner import generate_voice_reply
        from .voice_webhook import VOICE_SESSIONS, create_voice_webhook_server

        if is_public_https_url(os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "")):
            voice_server = create_voice_webhook_server(
                lambda session_id, speech: VOICE_SESSIONS.reply(
                    session_id, speech, generate_voice_reply
                ),
                host=voice_host,
                port=int(os.getenv("VOICE_WEBHOOK_PORT", "8768")),
            )
    runtime.start()
    if voice_server:
        voice_thread = threading.Thread(target=voice_server.serve_forever,
                                        name="voice-webhook", daemon=True)
        voice_thread.start()
        VOICE_SESSIONS.cleanup_expired()

        def cleanup_voice_sessions():
            while not voice_cleanup_stop.wait(300):
                try:
                    VOICE_SESSIONS.cleanup_expired()
                except OSError:
                    pass

        voice_cleanup_thread = threading.Thread(target=cleanup_voice_sessions,
                                               name="voice-session-cleanup", daemon=True)
        voice_cleanup_thread.start()
        print(f"Conversational voice webhook listening on "
              f"{voice_server.server_address[0]}:{voice_server.server_address[1]}")
    print(f"Persistent agent running; webhook listener on {server.server_address[0]}:{server.server_port}")
    print(f"Health: http://{server.server_address[0]}:{server.server_port}/healthz")
    _print_setup_status()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        voice_cleanup_stop.set()
        if voice_cleanup_thread:
            voice_cleanup_thread.join(timeout=5)
        if voice_server:
            voice_server.shutdown()
            voice_server.server_close()
        if voice_thread:
            voice_thread.join(timeout=5)
        server.server_close()
        runtime.stop()


if __name__ == "__main__":
    main()
