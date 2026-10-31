import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from friday.services.whatsapp_proxy import create_whatsapp_proxy_server


class EchoWebhookHandler(BaseHTTPRequestHandler):
    last_post_headers = {}

    def do_GET(self):
        body = self.path.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).last_post_headers = {name.lower(): value for name, value in self.headers.items()}
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def test_whatsapp_proxy_forwards_only_allowed_webhook_headers():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), EchoWebhookHandler)
    proxy = create_whatsapp_proxy_server(port=0, upstream_port=upstream.server_port)
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in (upstream, proxy)]
    for thread in threads:
        thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=2)
        connection.request("GET", "/whatsapp?hub.mode=subscribe&hub.challenge=ok")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == b"/whatsapp?hub.mode=subscribe&hub.challenge=ok"
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=2)
        connection.request("POST", "/whatsapp", body=b'{"test":true}', headers={
            "Content-Type": "application/json", "X-Hub-Signature-256": "sha256=test",
            "Authorization": "Bearer test-token", "X-Not-Forwarded": "private",
        })
        response = connection.getresponse()
        assert response.status == 202
        assert response.read() == b'{"test":true}'
        forwarded = EchoWebhookHandler.last_post_headers
        assert forwarded["content-type"] == "application/json"
        assert forwarded["x-hub-signature-256"] == "sha256=test"
        assert forwarded["authorization"] == "Bearer test-token"
        assert "x-not-forwarded" not in forwarded
        connection.close()
    finally:
        for server in (proxy, upstream):
            server.shutdown()
            server.server_close()


def test_whatsapp_proxy_never_exposes_health_or_other_paths():
    proxy = create_whatsapp_proxy_server(port=0, upstream_port=1)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=2)
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        assert response.status == 404
        assert response.read() == b"not found"
        connection.close()
    finally:
        proxy.shutdown()
        proxy.server_close()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com", "::1"])
def test_whatsapp_proxy_refuses_non_loopback_listeners(host):
    with pytest.raises(ValueError, match="loopback"):
        create_whatsapp_proxy_server(host=host)
