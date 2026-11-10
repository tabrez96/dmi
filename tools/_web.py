"""Web search (Brave) and page fetching, shared by the tool scripts and HQ's gate.

On the host these run directly. Inside an agent microVM there's no internet, so
the scripts call the gate (DMI_GATE + DMI_RUN_TOKEN) and the gate runs these.
"""

import http.client
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

BRAVE = "https://api.search.brave.com/res/v1/web/search"
MAX_BYTES = 2_000_000


class WebError(Exception):
    pass


def search(query, count=10, key=None):
    key = key or os.environ.get("BRAVE_API_KEY")
    if not key:
        raise WebError("BRAVE_API_KEY is not set")
    url = f"{BRAVE}?{urllib.parse.urlencode({'q': query, 'count': count})}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "X-Subscription-Token": key})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                data = json.load(response)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:   # free tier: 1 request/second
                time.sleep(1.5 * (attempt + 1))
                continue
            raise WebError(f"HTTP {e.code} from Brave") from e
        except (OSError, http.client.HTTPException, ValueError) as e:     # network, timeout or a reply that isn't JSON
            raise WebError(f"search failed: {e}") from e
    results = (data.get("web") or {}).get("results") or []
    if not results:
        return "No results."
    out = []
    for number, result in enumerate(results, 1):
        desc = result.get("description", "").replace("<strong>", "").replace("</strong>", "")
        out.append(f"{number}. {result.get('title')}\n   {result.get('url')}\n   {desc}\n")
    return "\n".join(out)


class _Text(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "svg", "head"})

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class NotPublic(OSError):
    """A host that resolves to a private, loopback or link-local address."""


def _is_public(ip):
    return ipaddress.ip_address(ip).is_global


def _public_socket(host, port, timeout):
    """Connect only if every address the host resolves to is public, and only to those addresses: one lookup, so
    a DNS server can't answer the check with a public address and the connection with a private one."""
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses or not all(_is_public(info[4][0]) for info in addresses):
        raise NotPublic(f"refusing non-public address for {host}")
    last_error = None
    for family, kind, proto, _canonical_name, address in addresses:
        sock = socket.socket(family, kind, proto)
        sock.settimeout(timeout)
        try:
            sock.connect(address)
            return sock
        except OSError as e:
            sock.close()
            last_error = e
    raise last_error


class _PublicHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = _public_socket(self.host, self.port, self.timeout)


class _PublicHTTPS(http.client.HTTPSConnection):
    def connect(self):          # the hostname still goes to TLS, so the certificate is checked against it
        self.sock = self._context.wrap_socket(_public_socket(self.host, self.port, self.timeout),
                                              server_hostname=self.host)


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PublicHTTP, req)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PublicHTTPS, req, context=self._context)


class _SafeRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
            raise WebError("redirected to a URL that isn't http(s)")
        return super().redirect_request(req, fp, code, msg, headers, newurl)   # the next hop is checked as it connects


# Every hop, redirects included, connects through the public-only handlers; no proxy, so the check is the connection.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _PublicHTTPHandler, _PublicHTTPSHandler,
                                      _SafeRedirects)


def fetch(url, max_chars=15000):
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebError("only http(s) URLs")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (DunderMifflinScout)"})
    try:
        with _opener.open(req, timeout=20) as response:
            raw = response.read(MAX_BYTES).decode(response.headers.get_content_charset() or "utf-8", errors="replace")
            is_html = "html" in (response.headers.get_content_type() or "")
    except urllib.error.URLError as e:
        if isinstance(e.reason, NotPublic):
            raise WebError(str(e.reason)) from e
        raise WebError(f"fetch failed: {e}") from e
    except NotPublic as e:
        raise WebError(str(e)) from e
    except (OSError, http.client.HTTPException, ValueError) as e:      # timeouts, resets, cut-off replies, bad URLs
        raise WebError(f"fetch failed: {e}") from e
    if is_html:
        parser = _Text()
        parser.feed(raw)
        raw = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:max_chars] + ("\n[truncated]" if len(text) > max_chars else "")


def via_gate(route, params):
    """Inside a microVM: ask the gate to do it."""
    gate, token = os.environ["DMI_GATE"], os.environ.get("DMI_RUN_TOKEN", "")
    req = urllib.request.Request(f"{gate}/tools/{route}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.read().decode()
    except urllib.error.HTTPError as e:
        raise WebError(f"gate refused ({e.code}): {e.read().decode()[:200]}") from e
    except (OSError, http.client.HTTPException) as e:
        raise WebError(f"gate unreachable: {e}") from e
