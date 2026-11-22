"""Exercise the real transport against a test-only loopback HTTP server."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from apps.agent_debug.errors import DebugError
from apps.agent_debug.security import get_endpoint, serialize_request
from apps.agent_debug.transport import send


@pytest.fixture
def loopback(settings):
    requests = []
    response = {"status": 200, "body": b'{"model":"debug-model","choices":[{"message":{"content":"{}"}}]}'}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(response["status"])
            self.send_header("Content-Length", str(len(response["body"])))
            if response["status"] == 307:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            try:
                if response.get("trickle"):
                    for byte in response["body"]:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(response["body"])
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            settings.OPS_DEBUG_ENDPOINTS = [
                {"id": "transport-test", "url": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                 "models": ["debug-model"], "auth_mode": "none", "target": "local"},
            ]
            yield get_endpoint("transport-test"), response, requests
        finally:
            server.shutdown()
            thread.join()


def packet():
    return serialize_request("debug-model", {"messages": [{"role": "user", "content": "合成测试"}]})


def test_wire_matches_preview_and_response_is_parsed(loopback, monkeypatch):
    endpoint, _, requests = loopback
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    body = packet()
    result = send(endpoint, body)
    assert requests == [("/v1/chat/completions", body)]
    assert json.loads(body)["response_format"] == {"type": "json_object"}
    assert result.model == "debug-model" and result.text == "{}"
    assert result.usage is None and result.provider_request_id is None


@pytest.mark.parametrize(("status", "body", "code", "truncated"), [
    (307, b"{}", "PROVIDER_REJECTED", False),
    (401, b"provider-only error", "AUTH_REJECTED", False),
    (429, b"provider-only error", "PROVIDER_LIMIT", False),
    (200, b"\xff", "OUTPUT_SCHEMA_INVALID", False),
    (200, b'{"model":"different","choices":[{"message":{"content":"{}"}}]}', "OUTPUT_SCHEMA_INVALID", False),
    (200, b"a" * 65537, "RESPONSE_TOO_LARGE", True),
])
def test_single_attempt_and_categorized_failure(loopback, status, body, code, truncated):
    endpoint, response, requests = loopback
    response.update(status=status, body=body)
    with pytest.raises(DebugError) as caught:
        send(endpoint, packet())
    assert (caught.value.code, caught.value.http_status, caught.value.received, caught.value.truncated) == (code, status, True, truncated)
    assert len(requests) == 1


def test_slow_stream_is_bounded_by_wall_clock(loopback, settings):
    endpoint, response, requests = loopback
    settings.OPS_DEBUG_TIMEOUT_SECONDS = 0.4
    response.update(body=b"a" * 150, trickle=True)
    started = time.monotonic()
    with pytest.raises(DebugError) as caught:
        send(endpoint, packet())
    assert time.monotonic() - started < 2
    assert caught.value.code == "TIMEOUT" and caught.value.received
    assert len(requests) == 1
