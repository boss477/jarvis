import json
import time

import pytest

from friday.services.voice_webhook import (
    VoiceSessionStore,
    create_voice_webhook_server,
    twiml_gather,
)


def test_twiml_gather_escapes_prompt():
    xml = twiml_gather("Say <hello>")
    assert "&lt;hello&gt;" in xml and "input=\"speech\"" in xml


def test_voice_webhook_accepts_speech_result(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PUBLIC_VOICE_WEBHOOK_URL", raising=False)
    server = create_voice_webhook_server(lambda form: f"heard: {form.get('SpeechResult')}")
    try:
        import http.client
        host, port = server.server_address
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection(host, port)
        conn.request("POST", "/voice/respond", "SpeechResult=hello+agent",
                     {"Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        body = response.read().decode()
        assert response.status == 200 and "heard: hello agent" in body
    finally:
        server.shutdown(); server.server_close()


def test_voice_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "secret")
    monkeypatch.setenv("PUBLIC_VOICE_WEBHOOK_URL", "https://example.test/voice/respond")
    received = []
    server = create_voice_webhook_server(lambda form: received.append(form) or "should not run")
    try:
        import http.client, threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port)
        conn.request("POST", "/voice/respond", "SpeechResult=hello",
                     {"Content-Type": "application/x-www-form-urlencoded",
                      "X-Twilio-Signature": "bad"})
        response = conn.getresponse()
        assert response.status == 403
        assert received == []
    finally:
        server.shutdown(); server.server_close()


def test_voice_webhook_accepts_signed_blank_form_parameter(monkeypatch):
    import base64
    import hashlib
    import hmac
    import http.client
    import threading

    token = "test-twilio-token"
    public_url = "https://example.test/voice/respond"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", token)
    monkeypatch.setenv("PUBLIC_VOICE_WEBHOOK_URL", public_url)
    form = {"From": "", "SpeechResult": "hello"}
    signed_value = public_url + "".join(key + form[key] for key in sorted(form))
    signature = base64.b64encode(
        hmac.new(token.encode(), signed_value.encode(), hashlib.sha1).digest()
    ).decode()
    received = []
    server = create_voice_webhook_server(
        lambda values: (received.append(values) or "heard: " + values.get("SpeechResult", ""))
    )
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=2)
        conn.request(
            "POST",
            "/voice/respond",
            "From=&SpeechResult=hello",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature,
            },
        )
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()

        assert response.status == 200
        assert "heard: hello" in body
        assert received == [form]
    finally:
        server.shutdown()
        server.server_close()




@pytest.mark.parametrize("contents", [b"\0{broken", b"\0[]", b"{broken"])
def test_voice_session_store_preserves_unreadable_data(tmp_path, contents):
    path = tmp_path / "voice-sessions.json"
    path.write_bytes(contents)
    store = VoiceSessionStore(path)

    with pytest.raises(ValueError, match="corrupt voice session store"):
        store.create("new call")

    assert path.read_bytes() == contents
    assert path.with_name(path.name + ".corrupt").read_bytes() == contents


def test_voice_session_store_serializes_replies_per_session(tmp_path):
    import threading

    store = VoiceSessionStore(tmp_path / "voice-sessions.json")
    session_id = store.create("opener")
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []
    results = {}

    def generator(opener, turns, speech):
        calls.append((speech, tuple(turns)))
        if speech == "first":
            first_started.set()
            release_first.wait(timeout=2)
        return f"reply to {speech}"

    errors = {}

    def run_reply(key, speech):
        try:
            results[key] = store.reply(session_id, speech, generator)
        except Exception as exc:
            errors[key] = exc

    first = threading.Thread(target=run_reply, args=("first", "first"))
    second = threading.Thread(target=run_reply, args=("second", "second"))
    first.start()
    assert first_started.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    assert [speech for speech, _turns in calls] == ["first"]

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert errors == {}
    assert results == {
        "first": ("reply to first", False),
        "second": ("reply to second", False),
    }
    assert [speech for speech, _turns in calls] == ["first", "second"]
    assert calls[1][1] == (("caller", "first"), ("agent", "reply to first"))

    written = json.loads((tmp_path / "voice-sessions.json").read_bytes()[1:].decode("utf-8"))
    assert written[session_id]["turns"] == [
        ["caller", "first"], ["agent", "reply to first"],
        ["caller", "second"], ["agent", "reply to second"],
    ]


def test_voice_session_store_reuses_matching_corrupt_backup(tmp_path):
    path = tmp_path / "voice-sessions.json"
    original = b"\\0{broken"
    path.write_bytes(original)
    first_backup = path.with_name(path.name + ".corrupt")
    first_backup.write_bytes(b"older corrupt contents")
    store = VoiceSessionStore(path)

    with pytest.raises(ValueError, match="corrupt voice session store"):
        store.create("new call")
    matching_backup = path.with_name(path.name + ".corrupt.1")
    assert matching_backup.read_bytes() == original

    with pytest.raises(ValueError, match="corrupt voice session store"):
        store.cleanup_expired()

    backups = sorted(path.parent.glob(path.name + ".corrupt*"))
    assert backups == [first_backup, matching_backup]
    assert matching_backup.read_bytes() == original


def test_voice_session_store_updates_legacy_file_format(tmp_path):
    path = tmp_path / "voice-sessions.json"
    existing = {
        "existing-session": {
            "opener": "keep this call",
            "turns": [],
            "created_at": time.time(),
        }
    }
    path.write_bytes(b"\0" + json.dumps(existing).encode("utf-8"))
    store = VoiceSessionStore(path)

    new_session_id = store.create("new call")

    written = path.read_bytes()
    assert written.startswith(b"\0")
    sessions = json.loads(written[1:].decode("utf-8"))
    assert sessions["existing-session"]["opener"] == "keep this call"
    assert new_session_id in sessions


def test_voice_session_store_failed_atomic_replace_preserves_original(
    tmp_path, monkeypatch
):
    path = tmp_path / "voice-sessions.json"
    existing = {
        "existing-session": {
            "opener": "keep this call",
            "turns": [],
            "created_at": time.time(),
        }
    }
    original = b"\0" + json.dumps(existing).encode("utf-8")
    path.write_bytes(original)
    store = VoiceSessionStore(path)

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr("friday.services.voice_webhook.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.create("new call")

    assert path.read_bytes() == original
