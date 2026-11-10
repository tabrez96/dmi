"""The web tools: fetch reaches public addresses only, on every hop, and every failure is a WebError."""

import importlib.util
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("web_under_test", TOOLS / "_web.py")
web = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web)


def serve(address, handler):
    server = ThreadingHTTPServer((address, 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Page(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<html><body><p>hello from the page</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def only_127_0_0_1_is_public(monkeypatch):
    """Stand-in for the internet: 127.0.0.1 counts as public, every other address (127.0.0.2 too) as private."""
    monkeypatch.setattr(web, "_is_public", lambda ip: ip == "127.0.0.1")


def test_a_public_page_is_fetched_as_text(only_127_0_0_1_is_public):
    server = serve("127.0.0.1", Page)
    try:
        assert web.fetch(f"http://127.0.0.1:{server.server_address[1]}/") == "hello from the page"
    finally:
        server.shutdown()


def test_a_private_address_is_refused(only_127_0_0_1_is_public):
    server = serve("127.0.0.2", Page)
    try:
        with pytest.raises(web.WebError, match="non-public"):
            web.fetch(f"http://127.0.0.2:{server.server_address[1]}/")
    finally:
        server.shutdown()


def test_a_redirect_to_a_private_address_is_refused(only_127_0_0_1_is_public):
    private = serve("127.0.0.2", Page)

    class Redirect(Page):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.2:{private.server_address[1]}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
    public = serve("127.0.0.1", Redirect)
    try:
        with pytest.raises(web.WebError, match="non-public"):
            web.fetch(f"http://127.0.0.1:{public.server_address[1]}/")
    finally:
        public.shutdown()
        private.shutdown()


def test_the_address_checked_is_the_address_connected_to(only_127_0_0_1_is_public, monkeypatch):
    """A rebinding DNS server answers the first lookup with a public address and later ones with a private one."""
    server = serve("127.0.0.1", Page)
    real_lookup = socket.getaddrinfo
    lookups = []

    def rebinding(host, port, *args, **kwargs):
        if host != "rebind.test":
            return real_lookup(host, port, *args, **kwargs)
        lookups.append(host)
        return real_lookup("127.0.0.1" if len(lookups) == 1 else "127.0.0.2", port, *args, **kwargs)
    monkeypatch.setattr(web.socket, "getaddrinfo", rebinding)
    try:
        assert web.fetch(f"http://rebind.test:{server.server_address[1]}/") == "hello from the page"
        assert lookups == ["rebind.test"]                # one lookup: nothing to rebind between check and connect
    finally:
        server.shutdown()


def test_failures_are_web_errors(only_127_0_0_1_is_public, monkeypatch):
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        port = unused.getsockname()[1]                   # nothing listening there
    with pytest.raises(web.WebError, match="fetch failed"):
        web.fetch(f"http://127.0.0.1:{port}/")
    with pytest.raises(web.WebError, match="only http"):
        web.fetch("file:///etc/passwd")
    monkeypatch.setenv("DMI_GATE", f"http://127.0.0.1:{port}")
    with pytest.raises(web.WebError, match="gate unreachable"):
        web.via_gate("fetch", {"url": "https://example.com"})


@pytest.mark.parametrize("tool", ["webfetch", "websearch"])
def test_a_bad_number_prints_usage_not_a_traceback(tool):
    done = subprocess.run([sys.executable, str(TOOLS / tool), "x", "abc"], capture_output=True, text=True,
                          check=False)
    assert done.returncode != 0 and "must be a whole number" in done.stderr and "Traceback" not in done.stderr
