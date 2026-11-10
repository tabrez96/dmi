"""The gate: key swapping, streaming, and everything it must refuse."""

import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hq.gate import Gate

SEEN = []


class FakeProvider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"),
                     "beta": self.headers.get("anthropic-beta"), "body": body})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for part in (b"data: one\n\n", b"data: two\n\n"):
            self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")


@pytest.fixture
def gate():
    provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{provider.server_address[1]}"
    started = Gate("127.0.0.1", 0, env={"NVIDIA_API_KEY": "REAL-NV", "CLAUDE_CODE_OAUTH_TOKEN": "REAL-OAUTH",
                                        "BRAVE_API_KEY": "REAL-BRAVE"},
                   upstreams={"nvidia": base, "anthropic": base, "openrouter": base}).start()
    SEEN.clear()
    yield started
    started.stop()
    provider.shutdown()


def call(gate, path, token, data=b'{"x":1}', header="Authorization"):
    req = urllib.request.Request(f"http://127.0.0.1:{gate.port}{path}", data=data, method="POST" if data else "GET",
                                 headers={header: f"Bearer {token}" if header == "Authorization" else token,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def connect(gate, target, token=None):
    """Send a CONNECT (with the token as proxy auth, if given) and return the start of the reply."""
    request = f"CONNECT {target} HTTP/1.1\r\nHost: x\r\n"
    if token:
        request += f"Proxy-Authorization: Basic {base64.b64encode(f'dmi:{token}'.encode()).decode()}\r\n"
    with socket.create_connection(("127.0.0.1", gate.port)) as sock:
        sock.sendall((request + "\r\n").encode())
        return sock.recv(200)


def test_swaps_run_token_for_real_key_and_streams(gate):
    token = gate.issue("T-1 engineer", ["nvidia"])
    status, body = call(gate, "/llm/nvidia/v1/chat/completions", token)
    assert status == 200 and body == b"data: one\n\ndata: two\n\n"
    assert SEEN[0]["path"] == "/v1/chat/completions" and SEEN[0]["auth"] == "Bearer REAL-NV"
    assert token not in json.dumps([seen["auth"] for seen in SEEN])


def test_anthropic_oauth_gets_bearer_and_beta(gate):
    token = gate.issue("T-1 qa", ["anthropic"])
    status, _ = call(gate, "/llm/anthropic/v1/messages", token, header="x-api-key")
    assert status == 200 and SEEN[0]["auth"] == "Bearer REAL-OAUTH" and "oauth-2025-04-20" in SEEN[0]["beta"]


def test_refusals(gate):
    token = gate.issue("T-1 engineer", ["nvidia"], max_requests=1)
    assert call(gate, "/llm/nvidia/v1/x", "made-up")[0] == 401           # unknown token
    assert call(gate, "/llm/anthropic/v1/x", token)[0] == 403            # provider not granted (not counted)
    assert call(gate, "/llm/nvidia/v1/x", token)[0] == 200               # the one request it's allowed
    assert call(gate, "/llm/nvidia/v1/x", token)[0] == 429               # over its request cap
    revoked = gate.issue("x", ["nvidia"])
    gate.revoke(revoked)
    assert call(gate, "/llm/nvidia/v1/x", revoked)[0] == 401             # revoked after the run
    assert call(gate, "/tools/search?q=x", gate.issue("x", ["nvidia"]), data=None)[0] == 403   # no web grant
    assert call(gate, "/llm/openrouter/v1/x", gate.issue("x", ["openrouter"]))[0] == 503  # no key held
    assert all(seen["auth"] != "Bearer made-up" for seen in SEEN)


def test_connect_only_to_package_registries(gate):
    token = gate.issue("x", ["nvidia"], packages=True, max_requests=1)
    for _ in range(3):                                                   # refusals don't use up the cap
        assert b" 403 " in connect(gate, "evil.example.com:443", token)
    assert b" 403 " in connect(gate, "pypi.org:443", gate.issue("x", ["nvidia"]))   # no package grant
    assert b" 401 " in connect(gate, "pypi.org:443")                    # no token
    assert gate.grant(token).used == 0


def test_an_unreachable_provider_gets_a_502(gate):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        gate.upstreams["nvidia"] = f"http://127.0.0.1:{unused.getsockname()[1]}"    # nothing listening there
    status, body = call(gate, "/llm/nvidia/v1/x", gate.issue("x", ["nvidia"]))
    assert status == 502 and b"can't reach nvidia" in body


def test_a_bad_number_gets_a_400(gate):
    token = gate.issue("x", [], web=True)
    assert call(gate, "/tools/search?q=x&count=abc", token, data=None)[0] == 400
    assert call(gate, "/tools/fetch?url=https://example.com&max=lots", token, data=None)[0] == 400


def test_the_log_names_the_service(gate):
    logged = []
    gate.sink = lambda kind, **fields: logged.append(fields)
    call(gate, "/llm/nvidia/v1/chat", gate.issue("T-1 engineer", ["nvidia"]))
    call(gate, "/tools/search?q=x", gate.issue("T-1 scout", ["nvidia"]), data=None)
    assert [(entry["method"], entry["provider"], entry["status"]) for entry in logged] == \
        [("POST", "nvidia", "200"), ("GET", "search", "403")]


def test_a_garbled_provider_reply_gets_a_502(gate):
    garbler = socket.socket()
    garbler.bind(("127.0.0.1", 0))
    garbler.listen()

    def reply_garbage():
        conn, _ = garbler.accept()
        conn.recv(65536)
        conn.sendall(b"this is not http\r\n\r\n")
        conn.close()
    threading.Thread(target=reply_garbage, daemon=True).start()
    gate.upstreams["nvidia"] = f"http://127.0.0.1:{garbler.getsockname()[1]}"
    status, body = call(gate, "/llm/nvidia/v1/x", gate.issue("x", ["nvidia"]))
    garbler.close()
    assert status == 502 and b"can't reach nvidia" in body


def test_a_fetch_url_cant_forge_log_lines(gate, monkeypatch, tmp_path):
    from hq import webtools
    monkeypatch.setattr(webtools, "fetch", lambda url, max_chars: "page")
    gate.log_path = tmp_path / "gate.log"
    forged = urllib.parse.quote("https://x.example/\n2026-01-01 00:00:00 T-9 qa POST nvidia/v1 200")
    assert call(gate, f"/tools/fetch?url={forged}", gate.issue("T-1 scout", [], web=True), data=None)[0] == 200
    for _ in range(50):                      # the line is written just after the reply goes out
        if gate.log_path.exists():
            break
        time.sleep(0.02)
    assert len(gate.log_path.read_text().splitlines()) == 1
