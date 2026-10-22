from friday.services.voice_webhook import create_voice_webhook_server, twiml_gather


def test_twiml_gather_escapes_prompt():
    xml = twiml_gather("Say <hello>")
    assert "&lt;hello&gt;" in xml and "input=\"speech\"" in xml


def test_voice_webhook_accepts_speech_result():
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
    server = create_voice_webhook_server(lambda form: "should not run")
    try:
        import http.client, threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port)
        conn.request("POST", "/voice/respond", "SpeechResult=hello",
                     {"Content-Type": "application/x-www-form-urlencoded",
                      "X-Twilio-Signature": "bad"})
        assert conn.getresponse().status == 403
    finally:
        server.shutdown(); server.server_close()
