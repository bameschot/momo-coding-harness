"""Streamable HTTP transport (MCP 2025-03-26 and later).

Every message is POSTed to the one endpoint.  A request's answer comes back
either as a JSON body or as a Server-Sent Events stream that may carry server
requests/notifications before the response; a notification or response we send
gets 202 Accepted.  The ``Mcp-Session-Id`` the server hands out on initialize is
sent on every later request, and a DELETE ends the session on close.

Each POST runs on its own thread and delivers what it reads through
``on_message``, so `Peer.request` waits (and can be interrupted) the same way it
does for stdio.  A failed POST is turned into a JSON-RPC error response for that
request id, so the waiting call fails at once instead of timing out.

Not done: the optional GET stream for unsolicited server messages, OAuth, and
redirects (refused: a redirect is how an Authorization header leaks to another
host).  Plain http is only allowed to loopback/private addresses.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import threading
from urllib.parse import urlsplit

CONNECT_TIMEOUT_S = 30.0
_MAX_BODY = 64_000_000


def check_url(url: str) -> None:
    """Raise ConnectionError unless url is https, or http to a non-public address."""
    split = urlsplit(url)
    if split.scheme not in ("http", "https") or not split.hostname:
        raise ConnectionError(f"not an http(s) URL: {url}")
    if split.scheme == "https":
        return
    try:
        infos = socket.getaddrinfo(split.hostname, split.port or 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise ConnectionError(f"cannot resolve {split.hostname}: {e}") from None
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if addr.is_global:
            raise ConnectionError(f"{split.hostname} is a public address: use https "
                                  "(plain http is only allowed on this machine or the LAN)")


class HTTPTransport:
    def __init__(self, url: str, headers: dict[str, str], read_timeout: float = 300.0):
        self.url = url
        self.headers = dict(headers)
        self.read_timeout = read_timeout
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._lock = threading.Lock()
        self._open: set[http.client.HTTPConnection] = set()
        self._closed = False
        self._on_message = self._on_close = None

    def start(self, on_message, on_close) -> None:
        check_url(self.url)
        self._on_message, self._on_close = on_message, on_close

    # ── sending ──────────────────────────────────────────────────────────────

    def send(self, msg: dict) -> None:
        if self._closed:
            raise ConnectionError("connection closed")
        threading.Thread(target=self._post, args=(msg,), daemon=True, name="mcp-http").start()

    def _connect(self) -> tuple[http.client.HTTPConnection, str]:
        split = urlsplit(self.url)
        if split.scheme == "https":
            conn = http.client.HTTPSConnection(split.hostname, split.port or 443,
                                               timeout=CONNECT_TIMEOUT_S,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(split.hostname, split.port or 80,
                                              timeout=CONNECT_TIMEOUT_S)
        path = (split.path or "/") + (f"?{split.query}" if split.query else "")
        return conn, path

    def _request_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": "momo-coding-harness", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, msg: dict) -> None:
        rid = msg.get("id") if "method" in msg else None
        conn, path = self._connect()
        with self._lock:
            self._open.add(conn)
        try:
            conn.request("POST", path, body=json.dumps(msg).encode("utf-8"),
                         headers=self._request_headers())
            conn.sock.settimeout(self.read_timeout)
            resp = conn.getresponse()
            if sid := resp.getheader("Mcp-Session-Id"):
                self.session_id = sid
            if resp.status in (301, 302, 303, 307, 308):
                raise ConnectionError(f"HTTP {resp.status} redirect to "
                                      f"{resp.getheader('Location')} — not followed; "
                                      "put the final URL in the config")
            if resp.status == 404 and self.session_id and msg.get("method") != "initialize":
                raise ConnectionError("the server ended the MCP session (HTTP 404); "
                                      "restart the server with /mcp restart")
            if resp.status >= 400:
                body = resp.read(2000).decode("utf-8", "replace").strip()
                raise ConnectionError(f"HTTP {resp.status} {resp.reason}"
                                      + (f": {body[:300]}" if body else ""))
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if resp.status == 202 or rid is None and ctype != "text/event-stream":
                resp.read()
            elif ctype == "text/event-stream":
                self._read_sse(resp)
            else:
                data = resp.read(_MAX_BODY)
                if data.strip():
                    self._on_message(json.loads(data))
        except (OSError, ConnectionError, http.client.HTTPException, ValueError) as e:
            if rid is not None and not self._closed:
                self._on_message({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32000, "message": str(e) or type(e).__name__}})
        finally:
            with self._lock:
                self._open.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _read_sse(self, resp) -> None:
        data: list[str] = []
        event = "message"
        while True:
            raw = resp.readline(_MAX_BODY)
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if data and event == "message":
                    try:
                        self._on_message(json.loads("\n".join(data)))
                    except json.JSONDecodeError:
                        pass
                data, event = [], "message"
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event = value or "message"

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            conns = list(self._open)
        for conn in conns:
            if conn.sock is not None:
                try:
                    conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self.session_id:
            try:
                conn, path = self._connect()
                conn.timeout = 3
                conn.request("DELETE", path, headers=self._request_headers())
                conn.getresponse().read()
                conn.close()
            except (OSError, http.client.HTTPException):
                pass
        if self._on_close:
            self._on_close("closed")
