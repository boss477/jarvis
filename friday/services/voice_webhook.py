"""Minimal Twilio voice webhook for conversational agent calls."""
from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time


class VoiceSessionStore:
    """Short-lived file-backed context shared by the composer and agent host."""

    def __init__(self, path: str | Path | None = None, ttl_seconds: int = 7200,
                 max_sessions: int = 200):
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        if path is None:
            configured_path = os.getenv("VOICE_SESSIONS_PATH", "").strip()
            if configured_path:
                path = configured_path
            elif os.getenv("LOCALAPPDATA"):
                path = Path(os.environ["LOCALAPPDATA"]) / "Friday" / "voice-sessions.json"
            else:
                task_path = Path(os.getenv("AGENT_TASKS_PATH", "agent-state/tasks.jsonl"))
                path = task_path.with_name("voice-sessions.json")
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()

    @contextmanager
    def _locked_sessions(self):
        """Serialize session updates across threads and independent app processes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        with self._lock, self.path.open("r+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                handle.seek(0)
                if handle.read(1) == b"":
                    handle.seek(0)
                    handle.write(b"\0")
                    handle.flush()
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(1)
                raw = handle.read()
                try:
                    sessions = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    sessions = {}
                if not isinstance(sessions, dict):
                    sessions = {}
                yield sessions
                encoded = json.dumps(sessions, ensure_ascii=False).encode("utf-8")
                handle.seek(1)
                handle.truncate(1)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def create(self, opener: str) -> str:
        now = time.time()
        with self._locked_sessions() as sessions:
            expired = [key for key, value in sessions.items()
                       if now - value.get("created_at", 0) >= self.ttl_seconds]
            for key in expired:
                del sessions[key]
            while len(sessions) >= self.max_sessions:
                oldest = min(sessions, key=lambda key: sessions[key].get("created_at", 0))
                del sessions[oldest]
            session_id = secrets.token_urlsafe(24)
            sessions[session_id] = {
                "opener": opener[:2000], "turns": [], "created_at": now,
            }
        return session_id

    def discard(self, session_id: str) -> None:
        if not session_id:
            return
        with self._locked_sessions() as sessions:
            sessions.pop(session_id, None)

    def cleanup_expired(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        with self._locked_sessions() as sessions:
            expired = [key for key, value in sessions.items()
                       if value.get("created_at", 0) <= cutoff]
            for key in expired:
                del sessions[key]
        return len(expired)

    def reply(self, session_id: str, speech: str, generator) -> tuple[str, bool]:
        with self._locked_sessions() as sessions:
            session = sessions.get(session_id)
            if not session or time.time() - session.get("created_at", 0) >= self.ttl_seconds:
                sessions.pop(session_id, None)
                return "I'm sorry, this call session has expired. Goodbye.", True
            opener = session["opener"]
            turns = list(session["turns"])
        if not speech.strip():
            return opener, False
        generated = generator(opener, turns, speech[:4000])
        if isinstance(generated, tuple):
            response, end_call = generated
        else:
            response, end_call = str(generated), False
        response = str(response)[:1000]
        with self._locked_sessions() as sessions:
            session = sessions.get(session_id)
            if session:
                session["turns"].extend((("caller", speech[:4000]), ("agent", response)))
                session["turns"] = session["turns"][-20:]
        return response, bool(end_call)


VOICE_SESSIONS = VoiceSessionStore()


def is_public_https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if parsed.fragment or host.lower() == "localhost" or host.lower().endswith((".localhost", ".local")):
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
        try:
            if not ip_address(host).is_global:
                return False
        except ValueError:
            if "." not in host:
                return False
        return True
    except ValueError:
        return False


def _voice_callback_url(path: str, session_id: str) -> str:
    public_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip()
    if not public_url:
        return path + "?" + urlencode({"session": session_id})
    parsed = urlsplit(public_url)
    query = dict(parse_qs(parsed.query))
    query["session"] = [session_id]
    return urlunsplit((parsed.scheme, parsed.netloc, path,
                       urlencode({key: values[-1] for key, values in query.items()}), ""))


def twiml_gather(prompt: str, action: str = "/voice/respond") -> str:
    safe_prompt = escape(prompt)
    safe_action = escape(action, quote=True)
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<Response><Gather input=\"speech\" action=\"{safe_action}\" "
            "method=\"POST\" speechTimeout=\"auto\">"
            f"<Say>{safe_prompt}</Say></Gather></Response>")


def twiml_say(text: str, gather: bool = True) -> str:
    if gather:
        return twiml_gather(text)
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say>{escape(text)}</Say></Response>"


def twiml_hangup(text: str) -> str:
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<Response><Say>{escape(text)}</Say><Hangup/></Response>")


class VoiceWebhookHandler(BaseHTTPRequestHandler):
    callback = None

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "invalid content length")
            return
        if size < 0 or size > 65536:
            self.send_error(413, "request body too large")
            return
        try:
            body = self.rfile.read(size).decode("utf-8")
        except UnicodeDecodeError:
            self.send_error(400, "invalid request encoding")
            return
        form = {key: values[0] for key, values in parse_qs(body, keep_blank_values=True).items()}
        try:
            if not self._valid_signature(form):
                self.send_error(403, "invalid Twilio signature")
                return
            query = parse_qs(urlsplit(self.path).query)
            session_id = query.get("session", [""])[0]
            if urlsplit(self.path).path.rstrip("/").endswith("/voice/status"):
                VOICE_SESSIONS.discard(session_id)
                self.send_response(204)
                self.end_headers()
                return
            speech = form.get("SpeechResult", "")
            if self.callback:
                result = self.callback(session_id, speech) if session_id else self.callback(form)
            else:
                result = "Voice agent is not configured."
            if isinstance(result, tuple) and len(result) == 2:
                reply, end_call = str(result[0]), bool(result[1])
            else:
                reply, end_call = str(result), False
            if end_call:
                if session_id:
                    VOICE_SESSIONS.discard(session_id)
                xml = twiml_hangup(reply)
            elif session_id:
                xml = twiml_gather(reply, _voice_callback_url("/voice/respond", session_id))
            else:
                xml = twiml_say(reply, gather=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(xml.encode())))
            self.end_headers()
            self.wfile.write(xml.encode())
        except Exception:
            xml = twiml_say("I'm sorry, I'm having trouble right now. Goodbye.", gather=False)
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.end_headers()
            self.wfile.write(xml.encode())

    def log_message(self, *_args): pass

    def _valid_signature(self, form: dict[str, str]) -> bool:
        token = os.getenv("TWILIO_AUTH_TOKEN", "")
        if not token:
            return not bool(os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "").strip())
        signature = self.headers.get("X-Twilio-Signature", "")
        public_url = os.getenv("PUBLIC_VOICE_WEBHOOK_URL", "")
        if not signature or not public_url:
            return False
        configured = urlsplit(public_url)
        incoming = urlsplit(self.path)
        signed_url = urlunsplit((configured.scheme, configured.netloc, incoming.path,
                                 incoming.query, ""))
        value = signed_url + "".join(key + form[key] for key in sorted(form))
        digest = base64.b64encode(hmac.new(token.encode(), value.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(signature, digest)


def create_voice_webhook_server(callback, host="127.0.0.1", port=0):
    handler = type("ConfiguredVoiceWebhookHandler", (VoiceWebhookHandler,),
                   {"callback": staticmethod(callback)})
    return ThreadingHTTPServer((host, port), handler)
