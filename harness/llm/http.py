"""Minimal stdlib HTTP client for the LLM adapters (Ollama and llama.cpp).

Both backends speak plain JSON over HTTP — a request/response call, or a stream
of lines (Ollama: one JSON object per line; llama.cpp: Server-Sent Events).  That
is all this module does, with http.client, so momo needs no HTTP library.

* A connection per request; every open one is tracked so abort() — called from
  another thread — can shut it down and make a blocked read in the worker raise
  at once.
* Connecting times out after 30 s; reading a stream never does (generation can
  take minutes).  Short JSON calls get a 30 s read timeout.
* Redirects are followed (up to 5).  A POST only follows 307/308, which keep the
  method and body.  Authorization is dropped when a redirect changes scheme, host
  or port, so a token is never replayed to another server.
* HTTPS verifies certificates against the system store (``ssl`` defaults).
  Proxy environment variables are not used: model servers are local or on a LAN.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
from urllib.parse import urljoin, urlsplit

CONNECT_TIMEOUT_S = 30.0
JSON_TIMEOUT_S = 30.0
_MAX_REDIRECTS = 5


class LLMHTTPError(OSError):
    """A backend answered with an HTTP error, or could not be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalize_base_url(host: str, default_port: int) -> str:
    """``scheme://host:port[/path]`` from a user-typed host, the way the ollama SDK
    did it: no scheme means http and ``default_port``; a scheme without a port
    means its standard port; a trailing slash is dropped."""
    host = (host or "").strip()
    scheme, sep, rest = host.partition("://")
    port = default_port
    if not sep:
        scheme, rest = "http", host
    elif scheme == "http":
        port = 80
    elif scheme == "https":
        port = 443
    else:
        raise ValueError(f"unsupported scheme in host {host!r} (use http:// or https://)")
    split = urlsplit(f"{scheme}://{rest}")
    name = split.hostname or "127.0.0.1"
    port = split.port or port
    try:
        if ipaddress.ip_address(name).version == 6:
            name = f"[{name}]"
    except ValueError:
        pass
    path = split.path.strip("/")
    return f"{scheme}://{name}:{port}" + (f"/{path}" if path else "")


def _error_message(status: int, reason: str, body: bytes) -> str:
    """The server's own error text when it sent one (Ollama: {"error": "..."},
    OpenAI-style: {"error": {"message": "..."}}), else the start of the body."""
    text = body.decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err = err.get("message") or err.get("type")
        if err:
            text = str(err)
    return f"HTTP {status} {reason}".rstrip() + (f": {text[:300]}" if text else "")


class HTTPClient:
    def __init__(self, base_url: str, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self._lock = threading.Lock()
        # connection -> its socket, captured at connect: http.client drops
        # conn.sock once a response that ends at connection close is read, but
        # close() still has to be able to shut that socket down.
        self._open: dict[http.client.HTTPConnection, socket.socket | None] = {}
        self._closed = False

    # ── connections ──────────────────────────────────────────────────────────

    def _connect(self, url: str, read_timeout: float | None) -> tuple[http.client.HTTPConnection, str]:
        split = urlsplit(url)
        if split.scheme == "https":
            conn = http.client.HTTPSConnection(split.hostname, split.port or 443,
                                               timeout=CONNECT_TIMEOUT_S,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(split.hostname, split.port or 80,
                                              timeout=CONNECT_TIMEOUT_S)
        with self._lock:
            if self._closed:
                raise LLMHTTPError("request aborted")
            self._open[conn] = None
        try:
            conn.connect()
        except OSError as e:
            self._forget(conn)
            raise LLMHTTPError(f"cannot connect to {split.scheme}://{split.netloc}: {e}") from None
        conn.sock.settimeout(read_timeout)
        with self._lock:
            aborted = self._closed
            if conn in self._open:
                self._open[conn] = conn.sock
        if aborted:                       # close() ran while we were connecting
            self._forget(conn)
            raise LLMHTTPError("request aborted")
        path = split.path or "/"
        if split.query:
            path += "?" + split.query
        return conn, path

    def _forget(self, conn) -> None:
        with self._lock:
            self._open.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass

    def close(self) -> None:
        """Abort every in-flight request (safe to call from another thread).
        The client cannot be used afterwards.

        Only the sockets are shut down here: the worker thread's blocked read
        then returns or raises, and the worker closes its own connection.
        Closing the connection objects from this thread would pull the response
        out from under that read."""
        with self._lock:
            self._closed = True
            socks = [sock for sock in self._open.values() if sock is not None]
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _raise_if_aborted(self) -> None:
        if self._closed:
            raise LLMHTTPError("request aborted")

    # ── requests ─────────────────────────────────────────────────────────────

    def _send(self, method: str, path: str, body, read_timeout: float | None):
        """Send the request, following redirects; returns (conn, response) for a
        2xx answer, raises LLMHTTPError otherwise."""
        url = self.base_url + path
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json", "User-Agent": "momo-coding-harness", **self.headers}
        if data is not None:
            headers["Content-Type"] = "application/json"
        origin = urlsplit(url)[:2]
        for _ in range(_MAX_REDIRECTS + 1):
            conn, target = self._connect(url, read_timeout)
            try:
                conn.request(method, target, body=data, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                self._forget(conn)
                raise LLMHTTPError(f"{method} {url}: {e}") from None
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                location = urljoin(url, resp.getheader("Location"))
                resp.read()
                self._forget(conn)
                if method != "GET" and resp.status not in (307, 308):
                    raise LLMHTTPError(f"{method} {url} was redirected to {location} "
                                       f"with HTTP {resp.status}; set the host to that address",
                                       resp.status)
                if urlsplit(location)[:2] != origin:     # (scheme, host:port)
                    headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                url = location
                continue
            if resp.status >= 400:
                try:
                    raw = resp.read(64_000)
                except OSError:
                    raw = b""
                self._forget(conn)
                raise LLMHTTPError(_error_message(resp.status, resp.reason, raw), resp.status)
            return conn, resp
        raise LLMHTTPError(f"{method} {self.base_url + path}: too many redirects")

    def request_json(self, method: str, path: str, body=None,
                     timeout: float | None = JSON_TIMEOUT_S):
        """One request, parsed JSON reply. ``timeout=None`` waits indefinitely
        (a non-streamed chat can take as long as the generation)."""
        conn, resp = self._send(method, path, body, timeout)
        try:
            raw = resp.read()
        except (OSError, ValueError, http.client.HTTPException) as e:
            self._raise_if_aborted()
            raise LLMHTTPError(f"{method} {path}: {e}") from None
        finally:
            self._forget(conn)
        self._raise_if_aborted()
        try:
            return json.loads(raw)
        except ValueError:
            raise LLMHTTPError(f"{method} {path}: the server did not return JSON") from None

    def stream_lines(self, method: str, path: str, body=None):
        """Yield the response body line by line (decoded, without the newline)
        as it arrives. Reading never times out; abort with close()."""
        conn, resp = self._send(method, path, body, None)
        try:
            while True:
                try:
                    line = resp.readline()
                except (OSError, ValueError, http.client.HTTPException) as e:
                    # ValueError: the connection was closed under us by close().
                    self._raise_if_aborted()
                    raise LLMHTTPError(f"{method} {path}: stream interrupted ({e})") from None
                if not line:
                    # A shut-down socket reads as a clean end of stream; the caller
                    # must not mistake an aborted reply for a finished one.
                    self._raise_if_aborted()
                    return
                yield line.decode("utf-8", "replace").rstrip("\r\n")
        finally:
            self._forget(conn)
