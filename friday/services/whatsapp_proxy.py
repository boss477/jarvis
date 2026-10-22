"""Loopback-only, WhatsApp-path-filtering reverse proxy for Cloudflare Tunnel."""
from __future__ import annotations

import http.client
import ipaddress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .remote_http import MAX_WEBHOOK_BODY_BYTES


def _loopback_host(value: str, setting: str) -> str:
    host = value.strip() or "127.0.0.1"
    if host.lower() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError(f"{setting} must be localhost or a loopback IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
        raise ValueError(f"{setting} must be localhost or a loopback IPv4 address")
    return host


def _port(value: str, setting: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise ValueError(f"{setting} must be a valid TCP port") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{setting} must be between 1 and 65535")
    return port


class WhatsAppProxyHandler(BaseHTTPRequestHandler):
    upstream_host = "127.0.0.1"
    upstream_port = 8767

    def do_GET(self):
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_HEAD(self):
        self._reply(405, b"method not allowed")

    def do_PUT(self):
        self._reply(405, b"method not allowed")

    def do_DELETE(self):
        self._reply(405, b"method not allowed")

    def _proxy(self):
        parsed = urlsplit(self.path)
        if parsed.path != "/whatsapp":
            return self._reply(404, b"not found")

        body = b""
        headers = {"Host": f"{self.upstream_host}:{self.upstream_port}",
                   "Connection": "close"}
        if self.command == "POST":
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._reply(400, b"invalid content length")
            if size <= 0:
                return self._reply(400, b"empty request body")
            if size > MAX_WEBHOOK_BODY_BYTES:
                return self._reply(413, b"request body too large")
            body = self.rfile.read(size)
            if len(body) != size:
                return self._reply(400, b"incomplete request body")
            for name in ("Content-Type", "X-Hub-Signature-256", "Authorization"):
                value = self.headers.get(name)
                if value:
                    headers[name] = value
            headers["Content-Length"] = str(size)

        connection = http.client.HTTPConnection(
            self.upstream_host, self.upstream_port, timeout=10,
        )
        try:
            target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read(MAX_WEBHOOK_BODY_BYTES + 1)
            if len(response_body) > MAX_WEBHOOK_BODY_BYTES:
                return self._reply(502, b"upstream response too large")
            self.send_response(response.status)
            for name, value in response.getheaders():
                if name.lower() in {"content-type", "content-length"}:
                    self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response_body)
        except (OSError, http.client.HTTPException):
            self._reply(502, b"WhatsApp webhook service unavailable")
        finally:
            connection.close()

    def _reply(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def create_whatsapp_proxy_server(*, host: str = "127.0.0.1", port: int = 8769,
                                upstream_host: str = "127.0.0.1",
                                upstream_port: int = 8767) -> ThreadingHTTPServer:
    host = _loopback_host(host, "WHATSAPP_PROXY_HOST")
    upstream_host = _loopback_host(upstream_host, "AGENT_WEBHOOK_HOST")
    if not 1 <= int(port) <= 65535:
        raise ValueError("WHATSAPP_PROXY_PORT must be between 1 and 65535")
    if not 1 <= int(upstream_port) <= 65535:
        raise ValueError("AGENT_WEBHOOK_PORT must be between 1 and 65535")
    handler = type("ConfiguredWhatsAppProxyHandler", (WhatsAppProxyHandler,), {
        "upstream_host": upstream_host, "upstream_port": int(upstream_port),
    })
    return ThreadingHTTPServer((host, int(port)), handler)


def main() -> None:
    env_file = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_file)
    server = create_whatsapp_proxy_server(
        host=os.getenv("WHATSAPP_PROXY_HOST", "127.0.0.1"),
        port=_port(os.getenv("WHATSAPP_PROXY_PORT", "8769"), "WHATSAPP_PROXY_PORT"),
        upstream_host=os.getenv("AGENT_WEBHOOK_HOST", "127.0.0.1"),
        upstream_port=_port(os.getenv("AGENT_WEBHOOK_PORT", "8767"), "AGENT_WEBHOOK_PORT"),
    )
    print(f"WhatsApp-only loopback proxy listening on "
          f"{server.server_address[0]}:{server.server_port}; upstream "
          f"{os.getenv('AGENT_WEBHOOK_HOST', '127.0.0.1')}:{os.getenv('AGENT_WEBHOOK_PORT', '8767')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
