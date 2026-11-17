"""momo-box gateway: the only way in or out of a locked or pull box.

    forward.py [LISTEN=HOST:PORT …] [--filter LISTEN=HOST:PORT --paths /a,/b]
               [--proxy PORT --allow-file F] [--log F]

* Each LISTEN=HOST:PORT relays TCP from 0.0.0.0:LISTEN to HOST:PORT, e.g.
  `9000=host.docker.internal:9931` (the inference server, for momo) and
  `8765=momo:8765` (the web UI, published to the host).  The target is resolved
  per connection, so it may start later than the gateway.
* --filter LISTEN=HOST:PORT --paths … is the model server, inference requests
  only: an HTTP forward that lets GET and POST through to the listed paths (the
  ones momo uses) and answers 403 to the rest of the server's API (pulling,
  deleting or pushing models).  One request per connection; the Host header is
  rewritten to the server's own name.
* --proxy PORT runs the pull-mode allowlist proxy: `CONNECT host:443` and plain
  HTTP GET/HEAD, only to hosts in the allow file (one per line; `.example.org`
  or `*.example.org` also match subdomains).  Re-read when the file changes.
  Everything else gets a 403 that says how to allow it.  There is no TLS
  interception: an allowed CONNECT is an opaque tunnel.
* --log F appends one JSON line per proxy decision (momo-box netlog).

Standard library only; runs on the momo image's own Python.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

_BUF = 65536
_HEAD_MAX = 16384
_CONNECT_PORTS = {443}
_HTTP_PORTS = {80}
_TIMEOUT = 30.0
_LOG_MAX = 5 * 1024 * 1024          # net.log is rotated to net.log.1 at this size
_BODY_MAX = 512 * 1024 * 1024       # a model request (images included) is far below this
_HOST_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?")


# ── relaying ──────────────────────────────────────────────────────────────────

def _pump(src: socket.socket, dst: socket.socket):
    """src → dst until EOF, then half-close dst (the other direction keeps going)."""
    try:
        while True:
            data = src.recv(_BUF)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def relay(a: socket.socket, b: socket.socket, initial: bytes = b""):
    """Both directions until both are done; closes both sockets."""
    try:
        if initial:
            b.sendall(initial)
        t = threading.Thread(target=_pump, args=(b, a), daemon=True)
        t.start()
        _pump(a, b)
        t.join()
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


def _connect(host: str, port: int) -> socket.socket:
    s = socket.create_connection((host, port), timeout=_TIMEOUT)
    s.settimeout(None)
    return s


def serve(port: int, handler, host: str = "0.0.0.0") -> socket.socket:
    """Listen on host:port and run handler(conn, addr) per connection on a thread."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(128)

    def loop():
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn, addr), daemon=True).start()
    threading.Thread(target=loop, daemon=True, name=f"listen-{port}").start()
    return srv


def forwarder(target_host: str, target_port: int):
    def handle(conn: socket.socket, _addr):
        try:
            up = _connect(target_host, target_port)
        except OSError:
            conn.close()
            return
        relay(conn, up)
    return handle


# ── the model server, inference only ─────────────────────────────────────────

_HOP_HEADERS = ("connection:", "keep-alive:", "proxy-", "host:", "expect:", "upgrade:")


class NetLog:
    """net.log: one JSON line per decision of the proxy and the model filter,
    rotated to net.log.1 at _LOG_MAX.  The host's syncer follows it (nettail)."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()

    def write(self, **entry):
        if self.path is None:
            return
        entry["ts"] = round(time.time(), 3)
        with self._lock:
            try:
                if self.path.stat().st_size > _LOG_MAX:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            except OSError:
                pass
            try:
                with open(self.path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass


def _status_of(first: bytes) -> int | None:
    """The status code of an HTTP response's first bytes."""
    parts = first.split(b"\r\n", 1)[0].split(b" ", 2)
    if len(parts) >= 2 and parts[0].startswith(b"HTTP/") and parts[1].isdigit():
        return int(parts[1])
    return None


class LlmFilter:
    """HTTP forward to the model server that only lets inference requests through."""

    def __init__(self, host: str, port: int, paths: list[str], log: NetLog | None = None):
        self.host, self.port = host, port
        self.paths = {p.rstrip("/") or "/" for p in paths}
        self.netlog = log or NetLog(None)

    def allowed(self, method: str, target: str) -> bool:
        path = target.split("?", 1)[0].rstrip("/") or "/"
        return method in ("GET", "POST") and path in self.paths

    def handle(self, conn: socket.socket, _addr):
        up = None
        try:
            got = _read_head(conn)
            if got is None:
                return
            head, rest = got
            lines = head.decode("latin-1").split("\r\n")
            try:
                method, target, version = lines[0].split(" ", 2)
            except ValueError:
                _reply(conn, "400 Bad Request")
                return
            path = target.split("?", 1)[0][:200]
            if not self.allowed(method, target):
                print(f"momo-box gw: refused {method} {path}", file=sys.stderr, flush=True)
                self.netlog.write(kind="llm", client=_addr[0] if _addr else "", method=method[:10],
                                  path=path, allowed=False)
                _reply(conn, "403 Forbidden",
                       f"momo-box: {method} {target.split('?', 1)[0]} is not an inference "
                       "request; the box reaches the model server for chat only "
                       "(momo-box --llm-filter off lifts this).")
                return
            headers = [ln for ln in lines[1:] if ln]
            names = {ln.split(":", 1)[0].strip().lower(): ln.split(":", 1)[-1].strip()
                     for ln in headers}
            if "transfer-encoding" in names:
                _reply(conn, "411 Length Required",
                       "momo-box: the gateway needs a Content-Length request body")
                return
            try:
                length = int(names.get("content-length", "0"))
            except ValueError:
                length = -1
            if not 0 <= length <= _BODY_MAX:
                _reply(conn, "400 Bad Request", "momo-box: bad Content-Length")
                return
            kept = [ln for ln in headers if not ln.lower().startswith(_HOP_HEADERS)]
            hostport = self.host if self.port == 80 else f"{self.host}:{self.port}"
            out = "\r\n".join([f"{method} {target} {version}", f"Host: {hostport}", *kept,
                                "Connection: close", "", ""])
            try:
                up = _connect(self.host, self.port)
                up.sendall(out.encode("latin-1"))
                body = rest[:length]
                up.sendall(body)
                left = length - len(body)
                while left > 0:
                    chunk = conn.recv(min(_BUF, left))
                    if not chunk:
                        return
                    up.sendall(chunk)
                    left -= len(chunk)
            except OSError as e:
                self.netlog.write(kind="llm", client=_addr[0] if _addr else "", method=method[:10],
                                  path=path, allowed=True, error=str(e)[:200])
                _reply(conn, "502 Bad Gateway", f"momo-box: {self.host}:{self.port}: {e}")
                return
            # Only the response flows from here on: whatever else the client sends (a
            # second request on the same connection) is dropped, not forwarded.  A
            # client that hangs up (a cancelled turn) closes the upstream so the
            # server stops generating.  No half-close: llama.cpp reads it as a cancel.
            server = up

            def watch():
                try:
                    while conn.recv(_BUF):
                        pass
                except OSError:
                    pass
                try:
                    server.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            threading.Thread(target=watch, daemon=True).start()
            try:
                first = up.recv(_BUF)
            except OSError:
                first = b""
            self.netlog.write(kind="llm", client=_addr[0] if _addr else "", method=method[:10],
                              path=path, allowed=True, status=_status_of(first),
                              **({} if first else {"error": "no response"}))
            if first:
                conn.sendall(first)
                _pump(up, conn)
        except OSError:
            pass
        finally:
            for s in (up, conn):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass


# ── allowlist proxy ───────────────────────────────────────────────────────────

class _Rule:
    """One allowlist line: `host`, `*.domain` or `.domain`, optionally `:port`,
    prefixed with `!` (a deny) or `+` (the user's own allow, which beats a deny),
    with an optional `# reason`.  Without a port it covers the default ports."""
    __slots__ = ("kind", "host", "port", "reason", "text")

    def __init__(self, kind: str, host: str, port: int | None, reason: str, text: str):
        self.kind, self.host, self.port, self.reason, self.text = kind, host, port, reason, text

    @classmethod
    def parse(cls, line: str) -> "_Rule | None":
        body, _, reason = line.partition("#")
        body = body.strip()
        kind = body[:1] if body[:1] in "!+" and body else ""
        body = body[len(kind):].strip()
        if not body:
            return None
        if body.startswith("[") or body.count(":") == 1:    # host:port, [v6] or [v6]:port
            host, port = _split_hostport(body, 0)
        else:                                               # a host, or a bare IPv6 literal
            host, port = body, 0
        host = _norm_host(host)
        try:
            host = ipaddress.ip_address(host.strip("[]")).compressed
        except ValueError:
            if not _HOST_RE.fullmatch(host.lstrip(".")):
                return None
        return cls(kind, host, port or None, " ".join(reason.split()), body)

    def covers(self, host: str, port: int, defaults: set[int], ip: bool) -> bool:
        if (port not in defaults) if self.port is None else (port != self.port):
            return False
        if ip or not self.host.startswith("."):      # an IP literal only matches itself
            return host == self.host
        return host == self.host[1:] or host.endswith(self.host)


class Allowlist:
    """Rules from a file, reloaded when its mtime changes (see _Rule).  The user's
    own allows win, then denies, then allows; nothing matching means refused."""

    def __init__(self, path: Path | None, hosts: list[str] | None = None):
        self.path = path
        self._mtime = None
        self._rules: list[_Rule] = []
        self._lock = threading.Lock()
        if hosts:
            self._rules = [r for r in map(_Rule.parse, hosts) if r]

    def _reload(self):
        if self.path is None:
            return
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            return
        if mtime == self._mtime:
            return
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        self._rules = [r for r in map(_Rule.parse, lines) if r]
        self._mtime = mtime

    def _target(self, host: str) -> tuple[str, bool] | None:
        """(normalised host, is an IP literal), or None for a non-host."""
        host = _norm_host(host)
        try:
            return ipaddress.ip_address(host.strip("[]")).compressed, True
        except ValueError:
            return (host, False) if _HOST_RE.fullmatch(host) else None   # "a@b", spaces…

    def _current(self) -> list[_Rule]:
        with self._lock:
            self._reload()
            return self._rules

    def match(self, host: str, port: int, defaults: set[int] | None = None
              ) -> tuple[bool, _Rule | None]:
        """(allowed, the deciding rule or None).  `defaults`: the ports a rule
        without one covers (443 for CONNECT, 80 for plain HTTP)."""
        defaults = _CONNECT_PORTS if defaults is None else defaults
        target, rules = self._target(host), self._current()
        if target is None:
            return False, None
        host, ip = target
        hits = [r for r in rules if r.covers(host, port, defaults, ip)]
        for kind in ("+", "!", ""):
            for r in hits:
                if r.kind == kind:
                    return kind != "!", r
        return False, None

    def allows(self, host: str, port: int = 443) -> bool:
        return self.match(host, port, {443})[0]

    def names(self, host: str) -> bool:
        """Whether an allow rule covers this host on some port."""
        target = self._target(host)
        return target is not None and any(
            r.kind != "!" and r.covers(target[0], r.port or 0, {0}, target[1])
            for r in self._current())


def _norm_host(h: str) -> str:
    h = h.strip().lower().rstrip(".")
    return "." + h[2:] if h.startswith("*.") else h


def _rule_fields(rule: "_Rule | None") -> dict:
    """The deciding rule, for net.log."""
    if rule is None:
        return {}
    out = {"rule": ("!" if rule.kind == "!" else rule.kind) + rule.text}
    if rule.reason:
        out["reason"] = rule.reason
    return out


def _read_head(conn: socket.socket) -> tuple[bytes, bytes] | None:
    """(request head incl. the blank line, bytes read past it), or None."""
    conn.settimeout(_TIMEOUT)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        buf += chunk
        if len(buf) > _HEAD_MAX:
            return None
    conn.settimeout(None)
    i = buf.index(b"\r\n\r\n") + 4
    return buf[:i], buf[i:]


def _reply(conn: socket.socket, status: str, text: str = ""):
    body = (text + "\n").encode() if text else b""
    try:
        conn.sendall(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                     f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    except OSError:
        pass


def _split_hostport(s: str, default: int) -> tuple[str, int]:
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default
    host, sep, port = s.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return s, default


class Proxy:
    def __init__(self, allow: Allowlist, log_path: Path | None = None,
                 box: str = "this box"):
        self.allow = allow
        self.log_path = log_path
        self.box = box
        self.netlog = log_path if isinstance(log_path, NetLog) else NetLog(log_path)

    def log(self, **entry):
        self.netlog.write(**entry)

    def refused(self, host: str, port: int = 443, rule: "_Rule | None" = None,
                defaults: set[int] | None = None) -> str:
        """The 403 text: why, and how to allow it."""
        if rule is not None and rule.kind == "!":
            why = f": {rule.reason}" if rule.reason else ""
            return (f"momo-box: {host} is denied by this repository's .momo/network.json "
                    f"({rule.text}){why}. The user can override it with "
                    f"`momo-box allow {self.box} {host}` on the host.")
        defaults = _CONNECT_PORTS if defaults is None else defaults
        usual = "/".join(str(p) for p in sorted(defaults))
        if port not in defaults and self.allow.names(host):
            return (f"momo-box: only port {usual} can be tunnelled to {host}, not {port}, "
                    f"unless .momo/network.json \"allow\" names {host}:{port}.")
        if port not in defaults:
            return (f"momo-box: only port {usual} can be tunnelled, not {port}. Allow {host}:{port} "
                    f"in .momo/network.json \"allow\" or with `momo-box allow {self.box} "
                    f"{host}:{port}` on the host.")
        return (f"momo-box: {host} is not on this box's allowlist (pull mode allows package "
                f"registries only). Add it to .momo/network.json \"allow\" or run "
                f"`momo-box allow {self.box} {host}` on the host.")

    def handle(self, conn: socket.socket, addr):
        try:
            self._handle(conn, addr)
        except Exception as e:           # a malformed request never leaks its socket
            _reply(conn, "400 Bad Request", f"momo-box: bad proxy request ({e})")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle(self, conn: socket.socket, addr):
        try:
            got = _read_head(conn)
        except OSError:
            got = None
        if got is None:
            conn.close()
            return
        head, rest = got
        try:
            line = head.split(b"\r\n", 1)[0].decode("latin-1")
            method, target, _version = line.split(" ", 2)
        except ValueError:
            _reply(conn, "400 Bad Request")
            conn.close()
            return
        client = addr[0] if addr else ""
        if method == "CONNECT":
            host, port = _split_hostport(target, 443)
            ok, rule = self.allow.match(host, port, _CONNECT_PORTS)
            self.log(client=client, method=method, host=host, port=port, allowed=ok,
                     **_rule_fields(rule))
            if not ok:
                _reply(conn, "403 Forbidden", self.refused(host, port, rule, _CONNECT_PORTS))
                conn.close()
                return
            try:
                up = _connect(host, port)
            except OSError as e:
                _reply(conn, "502 Bad Gateway", f"momo-box: {host}:{port}: {e}")
                conn.close()
                return
            try:
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            except OSError:
                up.close()
                conn.close()
                return
            relay(conn, up, rest)
            return
        # Plain HTTP: an absolute URI, GET/HEAD only, one request per connection.
        if not target.lower().startswith("http://"):
            _reply(conn, "400 Bad Request", "momo-box: expected a proxy request (absolute URI)")
            conn.close()
            return
        hostport, _, path = target[7:].partition("/")
        host, port = _split_hostport(hostport, 80)
        ok, rule = self.allow.match(host, port, _HTTP_PORTS)
        ok = ok and method in ("GET", "HEAD")
        self.log(client=client, method=method, host=host, port=port, allowed=ok,
                 **_rule_fields(rule))
        if not ok:
            if method not in ("GET", "HEAD"):
                _reply(conn, "405 Method Not Allowed",
                       f"momo-box: pull mode only lets GET and HEAD through, not {method}")
            else:
                _reply(conn, "403 Forbidden", self.refused(host, port, rule, _HTTP_PORTS))
            conn.close()
            return
        lines = head.decode("latin-1").split("\r\n")[1:]
        kept = [ln for ln in lines if ln and not ln.lower().startswith(
            ("proxy-", "connection:", "keep-alive:"))]
        out = "\r\n".join([f"{method} /{path} HTTP/1.1", *kept, "Connection: close", "", ""])
        try:
            up = _connect(host, port)
            up.sendall(out.encode("latin-1"))
        except OSError as e:
            _reply(conn, "502 Bad Gateway", f"momo-box: {host}:{port}: {e}")
            conn.close()
            return
        # Only the response flows back: further bytes from the client (a second,
        # pipelined request) never reach the upstream unchecked.
        try:
            up.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        _pump(up, conn)
        for s in (up, conn):
            try:
                s.close()
            except OSError:
                pass


# ── main ──────────────────────────────────────────────────────────────────────

def parse_args(argv: list[str]) -> dict:
    opts = {"maps": [], "filters": [], "paths": [], "proxy": None, "allow_file": None,
            "log": None, "box": os.environ.get("MOMO_BOX_NAME", "NAME")}

    def mapping(a: str) -> tuple[int, str, int]:
        listen, _, target = a.partition("=")
        host, port = _split_hostport(target, 0)
        if not listen.isdigit() or not port:
            raise SystemExit(f"forward.py: expected LISTEN=HOST:PORT, got {a!r}")
        return int(listen), host, port
    it = iter(argv)
    for a in it:
        if a in ("--proxy", "--allow-file", "--log", "--box", "--filter", "--paths"):
            try:
                v = next(it)
            except StopIteration:
                raise SystemExit(f"forward.py: {a} needs a value") from None
            key = a[2:].replace("-", "_")
            if key == "filter":
                opts["filters"].append(mapping(v))
            elif key == "paths":
                opts["paths"] = [x for x in v.split(",") if x]
            else:
                opts[key] = int(v) if key == "proxy" else v
        elif "=" in a:
            opts["maps"].append(mapping(a))
        else:
            raise SystemExit(f"forward.py: unexpected argument {a!r}")
    if opts["filters"] and not opts["paths"]:
        raise SystemExit("forward.py: --filter needs --paths")
    return opts


def nettail(path: Path, out=None, interval: float = 0.5, heartbeat: float = 5.0,
            rounds: int | None = None) -> int:
    """Print net.log from its start, then each new line as it is written, for
    the host's syncer (one `exec`, like entrypoint.py watch).  Follows the file
    across rotation; a blank line every `heartbeat` seconds shows it is alive."""
    out = out or sys.stdout
    f, ino, buf = None, None, b""
    beat = time.monotonic()
    try:
        while rounds is None or rounds > 0:
            if rounds is not None:
                rounds -= 1
            try:
                st = path.stat()
            except OSError:
                st = None
            wrote = False
            if st is not None:
                if f is None or st.st_ino != ino or st.st_size < f.tell():
                    if f is not None:
                        f.close()
                    f, ino, buf = open(path, "rb"), st.st_ino, b""
                buf += f.read()
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    if ln.strip():
                        out.write(ln.decode("utf-8", "replace") + "\n")
                        wrote = True
            now = time.monotonic()
            if wrote or now - beat >= heartbeat:
                if not wrote:
                    out.write("\n")
                out.flush()
                beat = now
            if rounds != 0:
                time.sleep(interval)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        if f is not None:
            f.close()
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["nettail"]:
        return nettail(Path(argv[1] if len(argv) > 1 else "/gw/net.log"))
    opts = parse_args(argv)
    netlog = NetLog(Path(opts["log"]) if opts["log"] else None)
    servers = []
    for listen, host, port in opts["maps"]:
        servers.append(serve(listen, forwarder(host, port)))
        print(f"momo-box gw: :{listen} → {host}:{port}", file=sys.stderr, flush=True)
    for listen, host, port in opts["filters"]:
        servers.append(serve(listen, LlmFilter(host, port, opts["paths"], netlog).handle))
        print(f"momo-box gw: :{listen} → {host}:{port} (inference requests only)",
              file=sys.stderr, flush=True)
    if opts["proxy"]:
        allow = Allowlist(Path(opts["allow_file"]) if opts["allow_file"] else None)
        proxy = Proxy(allow, netlog, opts["box"])
        servers.append(serve(opts["proxy"], proxy.handle))
        print(f"momo-box gw: allowlist proxy on :{opts['proxy']}", file=sys.stderr, flush=True)
    if not servers:
        print("forward.py: nothing to do", file=sys.stderr)
        return 2
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
