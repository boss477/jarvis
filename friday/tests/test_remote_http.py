import json
import hashlib
import hmac
import http.client
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from friday.services.remote_http import MAX_WEBHOOK_BODY_BYTES, create_webhook_server
from friday.services.remote_gateway import RemoteGateway
from friday.services.task_commands import TaskCommandService
from friday.services.task_manager import TaskManager
from friday.services.action_agent import PersistentActionAgent
from friday.services.communication_hub import CommunicationHub, CommunicationMessage

def test_loopback_webhook_routes_authenticated_command(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("demo", [{"tool": "x"}], origin_channel="telegram",
                          origin_sender_id="1", origin_conversation_id="1")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)), token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        body = json.dumps({"message": {"from": {"id": 1}, "text": f"/check {task.id}"}}).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}/telegram", data=body,
                          headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
        result = json.loads(urlopen(request, timeout=2).read())
        assert result["ok"] and result["results"][0]["ok"]
    finally:
        server.shutdown(); server.server_close()



def test_telegram_webhook_routes_query_string_when_secret_auth_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-secret")
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create("demo", [{"tool": "x"}], origin_channel="telegram",
                          origin_sender_id="1", origin_conversation_id="1")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps({"message": {"from": {"id": 1}, "text": f"/check {task.id}"}}).encode()
        request = Request(
            f"http://127.0.0.1:{server.server_port}/telegram?source=meta",
            data=body,
            headers={
                "X-Telegram-Bot-Api-Secret-Token": "telegram-secret",
                "Content-Type": "application/json",
            },
        )
        result = json.loads(urlopen(request, timeout=2).read())
        assert result["ok"] and result["results"][0]["ok"]
    finally:
        server.shutdown()
        server.server_close()

def test_webhook_rejects_oversized_body_before_reading_it(tmp_path):
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)), token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.putrequest("POST", "/telegram")
        connection.putheader("Authorization", "Bearer secret")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_WEBHOOK_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 413
        connection.close()
    finally:
        server.shutdown(); server.server_close()


def test_whatsapp_subscription_verification_returns_exact_challenge(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret")
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/whatsapp"
            "?hub.mode=subscribe&hub.verify_token=verify-secret&hub.challenge=challenge%2Bvalue"
        )
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "text/plain"
            assert response.read().decode() == "challenge+value"
    finally:
        server.shutdown(); server.server_close()


def test_whatsapp_subscription_verification_rejects_wrong_token(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret")
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/whatsapp"
            "?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=do-not-echo"
        )
        try:
            urlopen(request, timeout=2)
            assert False, "incorrect verification token must be rejected"
        except HTTPError as exc:
            assert exc.code == 403
            assert b"do-not-echo" not in exc.read()
    finally:
        server.shutdown(); server.server_close()


def test_whatsapp_rejects_unsigned_request_even_with_bearer_token(tmp_path, monkeypatch):
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)), token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        request = Request(f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
                          headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
        try:
            urlopen(request, timeout=2)
            assert False, "unsigned WhatsApp webhook must be rejected"
        except HTTPError as exc:
            assert exc.code == 401
    finally:
        server.shutdown(); server.server_close()


def test_whatsapp_accepts_valid_meta_signature(tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = Request(f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
                          headers={"X-Hub-Signature-256": signature,
                                   "Content-Type": "application/json"})
        result = json.loads(urlopen(request, timeout=2).read())
        assert result["ok"]
    finally:
        server.shutdown(); server.server_close()


def test_whatsapp_ignores_non_allowlisted_sender(tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", "+1 (555) 987-6543")
    seen = []
    manager = TaskManager(tmp_path / "tasks.jsonl")
    gateway = RemoteGateway(TaskCommandService(manager),
                            message_handler=lambda message: seen.append(message.sender_id) or "")
    server = create_webhook_server(gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "15551234567", "id": "wamid.1", "type": "text",
             "text": {"body": "show me my files"}},
            {"from": "15559876543", "id": "wamid.2", "type": "text",
             "text": {"body": "hello"}},
        ]}}]}]}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = Request(f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
                          headers={"X-Hub-Signature-256": signature,
                                   "Content-Type": "application/json"})
        result = json.loads(urlopen(request, timeout=2).read())
        assert result["ok"]
        assert result["results"] == [
            {"ok": True, "ignored": "sender_not_allowlisted"},
            {"ok": True, "reply": ""},
        ]
        assert seen == ["15559876543"]
    finally:
        server.shutdown(); server.server_close()


def test_whatsapp_allowlist_accepts_normalized_e164_number(tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    monkeypatch.setenv("META_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", "+1 (555) 123-4567")
    seen = []
    manager = TaskManager(tmp_path / "tasks.jsonl")
    gateway = RemoteGateway(TaskCommandService(manager),
                            message_handler=lambda message: seen.append(message.sender_id) or "")
    server = create_webhook_server(gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        payload = {"entry": [{"changes": [{"value": {"messages": [{
            "from": "15551234567", "id": "wamid.2", "type": "text",
            "text": {"body": "hello"},
        }]}}]}]}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = Request(f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
                          headers={"X-Hub-Signature-256": signature,
                                   "Content-Type": "application/json"})
        result = json.loads(urlopen(request, timeout=2).read())
        assert result["results"] == [{"ok": True, "reply": ""}]
        assert seen == ["15551234567"]
    finally:
        server.shutdown(); server.server_close()


def test_signed_whatsapp_button_webhook_approves_only_allowlisted_owner_task(
        tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    sender = "15551234567"
    monkeypatch.setenv("META_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", "+1 (555) 123-4567")
    manager = TaskManager(tmp_path / "tasks.jsonl")
    task = manager.create(
        "send the approved report", [{"tool": "send"}],
        origin_channel="whatsapp", origin_sender_id=sender,
        origin_conversation_id=sender,
    )
    manager.require_approval(task.id, "send via communication")
    resumed = []
    gateway = RemoteGateway(
        TaskCommandService(manager), resume_handler=lambda task_id: resumed.append(task_id) or "Resumed",
    )

    class CapturingWhatsAppAdapter:
        def __init__(self):
            self.sent = []

        def parse(self, payload):
            from friday.services.channel_adapters import WhatsAppCloudAdapter
            return WhatsAppCloudAdapter().parse(payload)

        def send_message(self, recipient, text):
            self.sent.append(("text", recipient, text))
            return "sent:text"

        def send_interactive_message(self, recipient, text, buttons):
            self.sent.append(("interactive", recipient, text, buttons))
            return "sent:interactive"

    adapter = CapturingWhatsAppAdapter()
    server = create_webhook_server(gateway)
    server.RequestHandlerClass.adapters = {"whatsapp": adapter}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = {"entry": [{"changes": [{"value": {"messages": [{
            "from": sender, "id": "wamid.button.1", "type": "interactive",
            "interactive": {"button_reply": {
                "id": f"task:approve:{task.id}", "title": "Approve",
            }},
        }]}}]}]}
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        request = Request(f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
                          headers={"X-Hub-Signature-256": signature,
                                   "Content-Type": "application/json"})
        result = json.loads(urlopen(request, timeout=2).read())

        assert result["results"][0]["ok"]
        assert result["results"][0]["reply"] == "Resumed"
        assert result["results"][0]["delivery"] == "sent:text"
        assert resumed == [task.id]
        assert manager.get(task.id).status.value == "running"
        assert adapter.sent == [("text", sender, "Resumed")]
    finally:
        server.shutdown()
        server.server_close()


def test_concurrent_whatsapp_webhook_redelivery_sends_reply_once(tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    sender = "15551234567"
    monkeypatch.setenv("META_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", sender)
    send_started = threading.Event()
    release_send = threading.Event()
    deliveries = []

    class BlockingWhatsAppAdapter:
        def parse(self, payload):
            from friday.services.channel_adapters import WhatsAppCloudAdapter
            return WhatsAppCloudAdapter().parse(payload)

        def send_message(self, recipient, text):
            deliveries.append((recipient, text))
            send_started.set()
            if not release_send.wait(timeout=3):
                raise TimeoutError("test delivery was not released")
            return "sent:wamid.reply"

    agent = PersistentActionAgent(
        CommunicationHub(), lambda _message: "answer",
        tmp_path / "conversations.jsonl",
    )
    gateway = RemoteGateway(
        TaskCommandService(TaskManager(tmp_path / "tasks.jsonl")),
        message_handler=lambda message: agent.process_message(CommunicationMessage(
            message.channel, message.conversation_id, message.sender_id,
            message.text, message.message_id, metadata=message.metadata,
        ), deliver=False).reply,
        delivery_handler=agent.record_delivery,
        delivery_claim_handler=agent.claim_delivery,
    )
    server = create_webhook_server(gateway)
    server.RequestHandlerClass.adapters = {"whatsapp": BlockingWhatsAppAdapter()}
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": sender, "id": "wamid.concurrent.1", "type": "text",
        "text": {"body": "hello"},
    }]}}]}]}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    url = f"http://127.0.0.1:{server.server_port}/whatsapp"
    headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}
    first_result = []

    def send_first_request():
        request = Request(url, data=body, headers=headers)
        with urlopen(request, timeout=4) as response:
            first_result.append(json.loads(response.read()))

    first_thread = threading.Thread(target=send_first_request, daemon=True)
    first_thread.start()
    try:
        assert send_started.wait(timeout=2)
        duplicate_request = Request(url, data=body, headers=headers)
        with urlopen(duplicate_request, timeout=2) as response:
            duplicate_result = json.loads(response.read())
        assert duplicate_result["results"][0]["delivery"] == "duplicate suppressed"
        assert deliveries == [(sender, "answer")]
        release_send.set()
        first_thread.join(timeout=3)
        assert not first_thread.is_alive()
        assert first_result[0]["results"][0]["delivery"] == "sent:wamid.reply"
        assert deliveries == [(sender, "answer")]
    finally:
        release_send.set()
        first_thread.join(timeout=3)
        server.shutdown()
        server.server_close()


def test_whatsapp_retry_after_lost_webhook_ack_does_not_resend_reply(
        tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    sender = "15551234567"
    monkeypatch.setenv("META_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", sender)
    deliveries = []

    class CapturingWhatsAppAdapter:
        def parse(self, payload):
            from friday.services.channel_adapters import WhatsAppCloudAdapter
            return WhatsAppCloudAdapter().parse(payload)

        def send_message(self, recipient, text):
            deliveries.append((recipient, text))
            return "sent:wamid.reply"

    agent = PersistentActionAgent(
        CommunicationHub(), lambda _message: "answer",
        tmp_path / "conversations.jsonl",
    )
    gateway = RemoteGateway(
        TaskCommandService(TaskManager(tmp_path / "tasks.jsonl")),
        message_handler=lambda message: agent.process_message(CommunicationMessage(
            message.channel, message.conversation_id, message.sender_id,
            message.text, message.message_id, metadata=message.metadata,
        ), deliver=False).reply,
        delivery_handler=agent.record_delivery,
        delivery_claim_handler=agent.claim_delivery,
    )
    server = create_webhook_server(gateway)
    server.RequestHandlerClass.adapters = {"whatsapp": CapturingWhatsAppAdapter()}
    handler_class = server.RequestHandlerClass
    original_reply = handler_class._reply
    lost_ack = {"once": True}

    def fail_first_success_ack(self, status, payload):
        if status == 200 and lost_ack["once"]:
            lost_ack["once"] = False
            raise ConnectionResetError("simulated lost webhook acknowledgement")
        return original_reply(self, status, payload)

    handler_class._reply = fail_first_success_ack
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": sender, "id": "wamid.ack-lost.1", "type": "text",
        "text": {"body": "hello"},
    }]}}]}]}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    url = f"http://127.0.0.1:{server.server_port}/whatsapp"
    headers = {"X-Hub-Signature-256": signature, "Content-Type": "application/json"}

    def post():
        return json.loads(urlopen(Request(url, data=body, headers=headers), timeout=2).read())

    try:
        try:
            post()
            assert False, "the simulated lost acknowledgement should cause a retry"
        except HTTPError as exc:
            assert exc.code == 503
        assert deliveries == [(sender, "answer")]

        result = post()
        assert result["ok"]
        assert deliveries == [(sender, "answer")]
    finally:
        server.shutdown()
        server.server_close()


def test_whatsapp_batch_delivers_completed_message_before_retrying_later_failure(
        tmp_path, monkeypatch):
    secret = "test-meta-app-secret"
    sender = "15551234567"
    monkeypatch.setenv("META_APP_SECRET", secret)
    monkeypatch.setenv("WHATSAPP_ALLOWED_SENDERS", sender)
    attempts = {"wamid.batch.2": 0}

    def handle(message):
        if message.message_id == "wamid.batch.2":
            attempts[message.message_id] += 1
            if attempts[message.message_id] == 1:
                raise RuntimeError("temporary planner failure")
        return f"reply:{message.message_id}"

    agent = PersistentActionAgent(
        CommunicationHub(), handle, tmp_path / "conversations.jsonl",
    )

    class CapturingWhatsAppAdapter:
        def __init__(self):
            self.sent = []

        def parse(self, payload):
            from friday.services.channel_adapters import WhatsAppCloudAdapter
            return WhatsAppCloudAdapter().parse(payload)

        def send_message(self, recipient, text):
            self.sent.append((recipient, text))
            return f"sent:out-{len(self.sent)}"

    adapter = CapturingWhatsAppAdapter()
    gateway = RemoteGateway(
        TaskCommandService(TaskManager(tmp_path / "tasks.jsonl")),
        message_handler=lambda message: agent.process_message(CommunicationMessage(
            message.channel, message.conversation_id, message.sender_id,
            message.text, message.message_id, metadata=message.metadata,
        ), deliver=False).reply,
        delivery_handler=agent.record_delivery,
        delivery_claim_handler=agent.claim_delivery,
    )
    server = create_webhook_server(gateway)
    server.RequestHandlerClass.adapters = {"whatsapp": adapter}
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "id": "wamid.batch.1", "type": "text",
         "text": {"body": "first"}},
        {"from": sender, "id": "wamid.batch.2", "type": "text",
         "text": {"body": "second"}},
    ]}}]}]}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = Request(
        f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
        headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
    )
    try:
        try:
            urlopen(request, timeout=2)
            assert False, "the transient second-message failure should request a retry"
        except HTTPError as exc:
            assert exc.code == 503
        assert adapter.sent == [(sender, "reply:wamid.batch.1")]

        retry = Request(
            f"http://127.0.0.1:{server.server_port}/whatsapp", data=body,
            headers={"X-Hub-Signature-256": signature, "Content-Type": "application/json"},
        )
        result = json.loads(urlopen(retry, timeout=2).read())
        assert result["ok"]
        assert adapter.sent == [
            (sender, "reply:wamid.batch.1"),
            (sender, "reply:wamid.batch.2"),
        ]
        assert attempts["wamid.batch.2"] == 2
    finally:
        server.shutdown()
        server.server_close()


def test_whatsapp_subscription_rejects_non_ascii_token(tmp_path, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret")
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_port}/whatsapp"
            "?hub.mode=subscribe&hub.verify_token=caf%C3%A9&hub.challenge=do-not-echo"
        )
        try:
            urlopen(request, timeout=2)
            assert False, "non-matching verification token must be rejected"
        except HTTPError as exc:
            assert exc.code == 403
            assert b"do-not-echo" not in exc.read()
    finally:
        server.shutdown()
        server.server_close()


def test_webhook_rejects_non_ascii_bearer_header_without_dropping_connection(
    tmp_path, monkeypatch
):
    for name in ("META_APP_SECRET", "SLACK_SIGNING_SECRET", "TELEGRAM_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    manager = TaskManager(tmp_path / "tasks.jsonl")
    server = create_webhook_server(RemoteGateway(TaskCommandService(manager)), token="secret")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST", "/telegram", body=b"{}",
            headers={
                "Authorization": "Bearer café",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
        )
        response = connection.getresponse()
        assert response.status == 401
        response.read()
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
