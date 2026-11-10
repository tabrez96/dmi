"""The gate: the only thing an agent microVM can talk to.

Agent VMs have no internet access. Everything they need goes through this
small proxy on the host, which holds the real secrets:

  /llm/<provider>/...   model APIs (nvidia, openrouter, anthropic). The VM sends a
                        short-lived run token; the gate swaps in the real key.
  /tools/search?q=      Brave web search (only for runs granted `web`).
  /tools/fetch?url=     public web page as text (only `web`; private addresses refused).
  CONNECT host:443      package registries (pypi, npm) for runs granted `packages`.

A run token is issued when HQ starts an agent VM and revoked when it exits. It
only works for the providers/tools that run was granted, expires, and has a
request cap, so a hijacked agent can't leak a real key or run up a bill.
"""

import base64
import ipaddress
import json
import os
import secrets
import select
import socket
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http import client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UPSTREAMS = {
    "nvidia": "https://integrate.api.nvidia.com",
    "openrouter": "https://openrouter.ai/api",
    "anthropic": "https://api.anthropic.com",
}
PACKAGE_HOSTS = {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length", "authorization", "x-api-key"}
IP_FREEBIND = getattr(socket, "IP_FREEBIND", 15)    # Linux: bind an address the host doesn't have yet


@dataclass
class Grant:
    label: str
    providers: set
    web: bool = False
    packages: bool = False
    expires: float = 0.0
    max_requests: int = 600
    used: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self):
        """Count one request against the cap; False once the token has expired or used them all."""
        with self.lock:
            if time.time() > self.expires or self.used >= self.max_requests:
                return False
            self.used += 1
            return True


CONNECT_TIMEOUT = 8   # per address; the read timeout for long model responses is separate


class NotPublic(OSError):
    """A host that resolves to a private, loopback or otherwise non-public address."""


def _connect(host, port, public_only=False):
    """IPv4 first, a short timeout per address: a host whose IPv6 route silently drops packets
    (common on home/VM networks) would otherwise stall every request for ~2 minutes.
    public_only: refuse unless every address the host resolves to is public; the addresses checked are the ones
    connected to (one lookup), so a second lookup can't swap in a private one."""
    addresses = sorted(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                       key=lambda info: info[0] != socket.AF_INET)
    if public_only and not all(ipaddress.ip_address(info[4][0]).is_global for info in addresses):
        raise NotPublic(f"{host} isn't a public address")
    last_error = None
    for family, kind, proto, _canonical_name, address in addresses:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(CONNECT_TIMEOUT)
        try:
            sock.connect(address)
            return sock
        except OSError as e:
            sock.close()
            last_error = e
    raise last_error or OSError(f"can't connect to {host}")


class _V4FirstHTTPS(client.HTTPSConnection):
    def connect(self):
        sock = _connect(self.host, self.port)
        sock.settimeout(self.timeout)
        self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)


class _V4FirstHTTP(client.HTTPConnection):
    def connect(self):
        self.sock = _connect(self.host, self.port)
        self.sock.settimeout(self.timeout)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # The gate listens only on the agents' bridge, which can get its address after HQ starts (at boot).
        try:
            self.socket.setsockopt(socket.IPPROTO_IP, IP_FREEBIND, 1)
        except OSError:
            pass
        super().server_bind()


class Gate:
    def __init__(self, host="127.0.0.1", port=8787, log_path=None, env=None, upstreams=None):
        self.host, self.port = host, port
        self.upstreams = dict(upstreams or UPSTREAMS)
        self.env = env if env is not None else os.environ
        self.grants = {}
        self.log_path = Path(log_path) if log_path else None
        self._lock = threading.Lock()
        self.server = None

    # -- tokens --

    def issue(self, label, providers, web=False, packages=False, ttl=3600, max_requests=600):
        token = "dmi-run-" + secrets.token_urlsafe(24)
        with self._lock:
            self.grants[token] = Grant(label, set(providers), web, packages, time.time() + ttl, max_requests)
        return token

    def revoke(self, token):
        with self._lock:
            self.grants.pop(token, None)

    def grant(self, token):
        with self._lock:
            return self.grants.get(token)

    # -- upstream credentials (never leave this process) --

    def upstream_auth(self, provider, headers):
        env = self.env
        if provider == "nvidia" and env.get("NVIDIA_API_KEY"):
            return {"Authorization": f"Bearer {env['NVIDIA_API_KEY']}"}
        if provider == "openrouter" and env.get("OPENROUTER_API_KEY"):
            return {"Authorization": f"Bearer {env['OPENROUTER_API_KEY']}"}
        if provider == "anthropic":
            if env.get("ANTHROPIC_API_KEY"):
                return {"x-api-key": env["ANTHROPIC_API_KEY"]}
            if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
                betas = [beta.strip() for beta in (headers.get("anthropic-beta") or "").split(",") if beta.strip()]
                if "oauth-2025-04-20" not in betas:
                    betas.append("oauth-2025-04-20")
                return {"Authorization": f"Bearer {env['CLAUDE_CODE_OAUTH_TOKEN']}",
                        "anthropic-beta": ",".join(betas)}
        return None

    def brave_key(self):
        return self.env.get("BRAVE_API_KEY")

    # -- server --

    def start(self):
        class Handler(_Handler):
            gate = self
        self.server = _Server((self.host, self.port), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="gate").start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    sink = None      # called with ("api_call", **fields) for every request, to feed the activity log

    def log(self, label, method, target, status, provider):
        """One line per request: who (the run's label), what (method and target), which service, and the result."""
        if self.sink:
            try:
                ref, _, role = str(label).partition(" ")
                self.sink("api_call", label=label, ref=ref, role=role or None, method=method,
                          target=target.split("?")[0], provider=provider, status=str(status))
            except Exception:  # noqa: BLE001, S110 - the log must never break a request
                pass
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as log_file:
                log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {label} {method} {target} {status}\n")


class _Handler(BaseHTTPRequestHandler):
    gate = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet; the gate keeps its own log
        pass

    # -- helpers --

    def _token(self):
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if self.headers.get("x-api-key"):
            return self.headers["x-api-key"].strip()
        proxy_auth = self.headers.get("Proxy-Authorization") or ""
        if proxy_auth.lower().startswith("basic "):
            try:
                return base64.b64decode(proxy_auth[6:]).decode().split(":", 1)[1]
            except (ValueError, IndexError):      # not base64, not text, or no "user:token"
                return ""
        return ""

    def _service(self):
        """What a request is for, as the log names it: a model provider, search, web or registry."""
        if self.command == "CONNECT":
            return "registry"
        parts = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        if parts[0] == "llm" and len(parts) > 1:
            return parts[1]
        return {"tools/search": "search", "tools/fetch": "web"}.get("/".join(parts[:2]), "-")

    def _deny(self, code, msg, label="-"):
        self.gate.log(label, self.command, self.path[:120], code, self._service())
        body = json.dumps({"error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            out = b""
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else None

    def _authorized(self, allows, refusal="this run isn't allowed to use that"):
        """The run's grant if its token is live, may use this and is under its cap; otherwise refuse and None.
        A refused request doesn't count against the cap."""
        grant = self.gate.grant(self._token())
        if grant is None:
            self._deny(401, "unknown or revoked run token")
            return None
        if not allows(grant):
            self._deny(403, refusal, grant.label)
            return None
        if not grant.take():
            self._deny(429, "run token expired or over its request cap", grant.label)
            return None
        return grant

    # -- routes --

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def _route(self):
        path = urllib.parse.urlsplit(self.path)
        parts = path.path.lstrip("/").split("/", 2)
        if parts[0] == "llm" and len(parts) >= 2:
            return self._llm(parts[1], "/" + (parts[2] if len(parts) > 2 else ""), path.query)
        if path.path == "/tools/search":
            return self._search(urllib.parse.parse_qs(path.query))
        if path.path == "/tools/fetch":
            return self._fetch(urllib.parse.parse_qs(path.query))
        if path.path == "/health":
            return self._deny(200, "ok")
        self._deny(404, "no such route")

    def _llm(self, provider, rest, query):
        if provider not in self.gate.upstreams:
            return self._deny(404, f"unknown provider {provider}")
        grant = self._authorized(lambda grant: provider in grant.providers)
        if not grant:
            return
        auth = self.gate.upstream_auth(provider, self.headers)
        if auth is None:
            return self._deny(503, f"HQ has no credentials for {provider}", grant.label)
        base = urllib.parse.urlsplit(self.gate.upstreams[provider])
        target = base.path.rstrip("/") + rest + (f"?{query}" if query else "")
        headers = {name: value for name, value in self.headers.items() if name.lower() not in HOP}
        headers.update(auth)
        body = self._body()
        conn_cls = _V4FirstHTTPS if base.scheme == "https" else _V4FirstHTTP
        conn = conn_cls(base.hostname, base.port, timeout=600)
        replying = False
        try:
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            replying = True
            self.send_response(resp.status)
            for name, value in resp.getheaders():
                if name.lower() not in HOP:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            self.gate.log(grant.label, self.command, f"{provider}{rest[:60]}", resp.status, provider)
        except (OSError, client.HTTPException) as e:     # unreachable, cut off, a bad status line or a bad path
            if not replying:            # nothing sent yet: tell the agent instead of dropping the connection
                return self._deny(502, f"can't reach {provider}: {e}", grant.label)
            self.gate.log(grant.label, self.command, f"{provider}{rest[:60]}", f"error {e!r}", provider)
        finally:
            conn.close()
            self.close_connection = True

    def _send_text(self, text, label, target):
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True
        self.gate.log(label, self.command, target, 200, self._service())

    @staticmethod
    def _number(params, name, default):
        """A whole-number query parameter, or None if it isn't one."""
        try:
            return int((params.get(name) or [str(default)])[0])
        except ValueError:
            return None

    def _search(self, params):
        grant = self._authorized(lambda grant: grant.web)
        if not grant:
            return
        from . import webtools
        key = self.gate.brave_key()
        if not key:
            return self._deny(503, "HQ has no BRAVE_API_KEY", grant.label)
        query = (params.get("q") or [""])[0]
        count = self._number(params, "count", 10)
        if count is None:
            return self._deny(400, "count must be a whole number", grant.label)
        try:
            text = webtools.search(query, count, key)
        except webtools.WebError as e:
            return self._deny(502, str(e), grant.label)
        self._send_text(text, grant.label, f"search {query[:60]!r}")

    def _fetch(self, params):
        grant = self._authorized(lambda grant: grant.web)
        if not grant:
            return
        from . import webtools
        url = (params.get("url") or [""])[0]
        max_chars = self._number(params, "max", 15000)
        if max_chars is None:
            return self._deny(400, "max must be a whole number", grant.label)
        try:
            text = webtools.fetch(url, max_chars)
        except webtools.WebError as e:
            return self._deny(502, str(e), grant.label)
        self._send_text(text, grant.label, f"fetch {url[:80]!r}")   # repr: no newlines into the log

    def do_CONNECT(self):
        host, _, port = self.path.partition(":")
        registry = host in PACKAGE_HOSTS and port == "443"
        grant = self._authorized(lambda grant: grant.packages and registry,
                                 f"{host} isn't an allowed package registry" if not registry
                                 else "this run has no package access")
        if not grant:
            return
        try:
            upstream = _connect(host, 443, public_only=True)
            upstream.settimeout(None)
        except NotPublic as e:
            return self._deny(403, str(e), grant.label)
        except OSError as e:
            return self._deny(502, str(e), grant.label)
        self.send_response(200, "Connection established")
        self.end_headers()
        self.gate.log(grant.label, "CONNECT", host, 200, "registry")
        sockets = [self.connection, upstream]
        try:
            while True:
                readable, _writable, broken = select.select(sockets, [], sockets, 120)
                if broken or not readable:
                    break
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()
            self.close_connection = True
