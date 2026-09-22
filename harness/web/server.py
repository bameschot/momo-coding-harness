"""Local web UI: a stdlib HTTP server exposing the shared Controller to a browser.

Routes
  GET  /              index.html (+ /app.css, /app.js)
  GET  /api/events    Server-Sent Events: backlog replay, then live harness events
  GET  /api/state     snapshot for (re)initialising the page
  POST /api/submit    {"text": ...} → same path as typing in the TUI input box
  POST /api/cancel    interrupt the running LLM call
  POST /api/mode      {"mode": ...}
  POST /api/upload    raw file bytes (X-Filename header) → {"name", "text", ...}
  POST /api/retry     re-send the last user message
  POST /api/edit      {"text": ...} → replace the last user message and re-send
  GET  /api/last-user the last user message's typed text + attachment names
  GET  /api/sessions  recent sessions for the session drawer
  GET  /api/models    models available on the backend
  GET  /api/context   context usage broken down per category (system, tools, …)
  GET  /api/export    the conversation as a Markdown download
  GET  /api/files     ?path= → directory listing (read-only, confined to the workspace)
  GET  /api/file      ?path= → file contents as text (same conversion as uploads)
  GET  /api/files/search ?q= → fuzzy path search for @-mentions

Security: bound to loopback by default, with Host-header checks against DNS
rebinding.  On a non-loopback bind an access token is required (``?token=`` once,
then an HttpOnly SameSite=Strict cookie).  POSTs must be JSON and same-origin.

Nothing here may write to stdout/stderr while the curses TUI owns the terminal,
so request logging and error tracebacks are silenced.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import queue
import socket
import os
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import attachments as attach_mod
from .. import companion
from ..file_search import fuzzy_search, workspace_files
from .. import session as session_mod
from ..tools import _SKIP_DIRS, _safe_path
from ..commands import help_commands, _render_markdown
from ..controller import Controller
from ..events import event_to_json
from ..harness import ErrorEvent

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_FILES = {
    "/":         ("index.html", "text/html; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/app.css":  ("app.css",    "text/css; charset=utf-8"),
    "/app.js":   ("app.js",     "text/javascript; charset=utf-8"),
    "/markdown.js":  ("markdown.js",  "text/javascript; charset=utf-8"),
    "/highlight.js": ("highlight.js", "text/javascript; charset=utf-8"),
}
_MODES = ["design", "chat", "plan", "coding", "momo"]
_MAX_BODY = 16_000_000  # JSON bodies; a submit carries attachment text
_KEEPALIVE_S = 15.0
_COOKIE = "momo_token"
_MAX_PREVIEW_BYTES = 5_000_000


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _pdf_support() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def _companion_data() -> dict:
    return {
        "walk_right": companion._MOMO_WR,
        "walk_left": companion._MOMO_WL,
        "sit": companion._MOMO_SIT,
        "sit_left": companion._MOMO_SIT_L,
        "walk_right_blink": companion._MOMO_WR_BLINK,
        "walk_left_blink": companion._MOMO_WL_BLINK,
        "speech": {f"{mode}|{int(busy)}": lines
                   for (mode, busy), lines in companion._SPEECH_TEXTS.items()},
        "speech_default": companion._SPEECH_TEXTS_DEFAULT,
        "bubble_max": companion.BUBBLE_MAX,
        "cat_w": companion.CAT_W,
    }


# ── sessions / workspace helpers ────────────────────────────────────────────

_session_cache: dict[Path, tuple[float, dict]] = {}


def _session_info(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _session_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    msgs = data.get("messages") or []
    first = next((m.get("content") or "" for m in msgs if m.get("role") == "user"), "")
    preview = " ".join(attach_mod.summarize(str(first)).split())
    info = {
        "name": path.stem, "mtime": mtime,
        "mode": data.get("mode", "?"), "model": data.get("model", "?"),
        "provider": data.get("provider") or "", "workdir": data.get("workdir", ""),
        "messages": sum(1 for m in msgs if m.get("role") in ("user", "assistant")),
        "preview": preview[:80] + ("…" if len(preview) > 80 else ""),
    }
    _session_cache[path] = (mtime, info)
    return info


def _list_workspace(root: Path, rel: str, hidden: bool) -> list[dict] | str:
    target = _safe_path(rel or ".", root)
    if isinstance(target, str):
        return target
    if not target.is_dir():
        return f"ERROR: not a directory: {rel}"
    out = []
    try:
        entries = list(os.scandir(target))
    except OSError as e:
        return f"ERROR: {e}"
    for e in entries:
        if (not hidden and e.name.startswith(".")) or e.name in _SKIP_DIRS:
            continue
        is_dir = e.is_dir(follow_symlinks=False)
        try:
            size = 0 if is_dir else e.stat().st_size
        except OSError:
            size = 0
        out.append({"name": e.name, "type": "dir" if is_dir else "file", "size": size})
    out.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    return out


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        # The default prints a traceback to stderr, which would corrupt the TUI.
        pass


class WebServer:
    def __init__(self, controller: Controller, host: str, port: int, token: str | None):
        self.controller = controller
        self.host = host
        self.token = token
        self._stopping = threading.Event()
        server_cls = _Server
        if ":" in host.strip("[]"):
            server_cls = type("_Server6", (_Server,), {"address_family": socket.AF_INET6})
        self._httpd = server_cls((host.strip("[]"), port), self._make_handler())
        self.port = self._httpd.server_address[1]
        url_host = f"[{host.strip('[]')}]" if ":" in host else host
        if url_host in ("0.0.0.0", "[::]"):
            url_host = socket.gethostname()
        self.url = f"http://{url_host}:{self.port}/"
        if token:
            self.url += f"?token={token}"
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="momo-web", daemon=True)

    def start(self):
        self._thread.start()

    def close(self):
        self._stopping.set()
        self._httpd.shutdown()
        self._httpd.server_close()

    # ── request handling ──────────────────────────────────────────────────────

    def _state(self) -> dict:
        c, h = self.controller, self.controller.harness
        return {
            "status": asdict(h.status_event()),
            "busy": c.busy,
            "waiting": c.waiting,
            "think": h.think,
            "context_limit": h.context_limit,
            "pdf_support": _pdf_support(),
            "modes": _MODES,
            "commands": help_commands(),
            "skills": {"available": h.list_available_skills(), "active": list(h.active_skills)},
            "plan": h.plan.to_markdown() if h.plan is not None else None,
            "plan_phase": h.plan_phase if h.plan is not None else None,
            "history": list(h.input_history[-200:]),
            "companion": _companion_data(),
            "can_switch_model": h.client.can_switch_model,
            "session": h.session_path().stem,
        }

    def _make_handler(self):
        web = self
        allowed_hosts = None
        if not self.token:
            # Loopback bind: only accept requests addressed to a loopback name,
            # which defeats DNS-rebinding pages that resolve to 127.0.0.1.
            allowed_hosts = {"localhost", "127.0.0.1", "[::1]", self.host}

        class Handler(BaseHTTPRequestHandler):
            server_version = "momo-web"

            def log_message(self, format, *args):  # noqa: A002 — silence stderr logging
                pass

            # ── helpers ──────────────────────────────────────────────────────

            def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
                      headers: dict | None = None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if body and self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, obj, status: int = 200):
                self._send(status, json.dumps(obj, default=str).encode(), "application/json")

            def _host_ok(self) -> bool:
                if allowed_hosts is None:
                    return True
                host = (self.headers.get("Host") or "").lower()
                name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
                return name in allowed_hosts

            def _authed(self) -> bool:
                if not web.token:
                    return True
                cookie = SimpleCookie(self.headers.get("Cookie") or "")
                got = cookie[_COOKIE].value if _COOKIE in cookie else ""
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("Bearer "):
                    got = got or auth[7:]
                return bool(got) and hmac.compare_digest(got.encode(), web.token.encode())

            def _guard(self) -> bool:
                if not self._host_ok():
                    self._send(HTTPStatus.FORBIDDEN, b"Forbidden: unexpected Host header\n")
                    return False
                return True

            # ── GET ──────────────────────────────────────────────────────────

            def do_GET(self):
                if not self._guard():
                    return
                parts = urlsplit(self.path)
                path = parts.path

                if path == "/" and web.token:
                    qs_token = (parse_qs(parts.query).get("token") or [""])[0]
                    if qs_token and hmac.compare_digest(qs_token.encode(), web.token.encode()):
                        # Trade the URL token for a cookie and drop it from the address bar.
                        self._send(HTTPStatus.SEE_OTHER, headers={
                            "Location": "/",
                            "Set-Cookie": f"{_COOKIE}={web.token}; HttpOnly; SameSite=Strict; Path=/",
                        })
                        return

                if not self._authed():
                    self._send(HTTPStatus.UNAUTHORIZED,
                               b"Unauthorized: open the URL printed by momo (it contains ?token=...)\n")
                    return

                if path in _STATIC_FILES:
                    name, ctype = _STATIC_FILES[path]
                    try:
                        body = (_STATIC_DIR / name).read_bytes()
                    except OSError:
                        self._send(HTTPStatus.NOT_FOUND, b"Not found\n")
                        return
                    self._send(HTTPStatus.OK, body, ctype)
                elif path == "/api/state":
                    self._json(web._state())
                elif path == "/api/events":
                    self._stream_events()
                else:
                    self._get_api(path, parse_qs(parts.query))

            def _get_api(self, path: str, qs: dict):
                h = web.controller.harness
                arg = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731
                if path == "/api/sessions":
                    infos = [i for p in session_mod.list_sessions()[:50] if (i := _session_info(p))]
                    self._json({"current": h.session_path().stem, "sessions": infos})
                elif path == "/api/models":
                    self._json({"current": h.client.model, "models": h.client.list_models(),
                                "can_switch": h.client.can_switch_model,
                                "provider": h.client.provider_name})
                elif path == "/api/context":
                    self._json(h.context_breakdown())
                elif path == "/api/last-user":
                    self._json(web.controller.last_user_message() or {})
                elif path == "/api/export":
                    body = _render_markdown(h.messages).encode("utf-8")
                    self._send(HTTPStatus.OK, body, "text/markdown; charset=utf-8", headers={
                        "Content-Disposition": f'attachment; filename="momo-{h.session_path().stem}.md"'})
                elif path == "/api/files":
                    res = _list_workspace(h.workdir, arg("path"), arg("hidden") == "1")
                    if isinstance(res, str):
                        self._json({"error": res}, HTTPStatus.BAD_REQUEST)
                    else:
                        self._json({"path": arg("path"), "entries": res})
                elif path == "/api/file":
                    self._file(h.workdir, arg("path"))
                elif path == "/api/files/search":
                    self._json({"results": fuzzy_search(workspace_files(h.workdir), arg("q"))})
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"Not found\n")

            def _file(self, root: Path, rel: str):
                target = _safe_path(rel, root)
                if isinstance(target, str) or not rel:
                    self._json({"error": target if isinstance(target, str) else "missing path"},
                               HTTPStatus.BAD_REQUEST)
                    return
                if not target.is_file():
                    self._json({"error": f"not a file: {rel}"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    if target.stat().st_size > _MAX_PREVIEW_BYTES:
                        self._json({"error": f"{rel} is larger than {_MAX_PREVIEW_BYTES // 1_000_000} MB"},
                                   HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                        return
                    data = target.read_bytes()
                    ext = attach_mod.extract(target.name, data)
                except OSError as e:
                    self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
                    return
                except attach_mod.AttachmentError as e:
                    self._json({"error": str(e)}, HTTPStatus.UNPROCESSABLE_ENTITY)
                    return
                self._json({"name": ext.name, "path": rel, "text": ext.text, "kind": ext.kind,
                            "chars": len(ext.text), "pages": ext.pages, "truncated": ext.truncated})

            def _stream_events(self):
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                sub = web.controller.bus.subscribe(replay=True)
                try:
                    while not web._stopping.is_set():
                        try:
                            ev = sub.get(timeout=_KEEPALIVE_S)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        chunks = [ev]
                        # Coalesce whatever else is queued (e.g. the backlog replay).
                        try:
                            while len(chunks) < 500:
                                chunks.append(sub.get_nowait())
                        except queue.Empty:
                            pass
                        payload = "".join(
                            "data: " + json.dumps(event_to_json(e), default=str) + "\n\n"
                            for e in chunks)
                        self.wfile.write(payload.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    sub.close()
                    self.close_connection = True

            # ── POST ─────────────────────────────────────────────────────────

            def do_POST(self):
                if not self._guard():
                    return
                if not self._authed():
                    self._send(HTTPStatus.UNAUTHORIZED, b"Unauthorized\n")
                    return
                # Same-origin only: browsers always send Origin on cross-site POSTs,
                # and requiring JSON forces a CORS preflight we never answer.
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                    self._send(HTTPStatus.FORBIDDEN, b"Forbidden: cross-origin request\n")
                    return
                path = urlsplit(self.path).path
                if path == "/api/upload":
                    self._upload()
                    return
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"Expected application/json\n")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > _MAX_BODY:
                    self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"Body too large\n")
                    return
                try:
                    data = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError
                except ValueError:
                    self._send(HTTPStatus.BAD_REQUEST, b"Invalid JSON\n")
                    return

                c = web.controller
                if path == "/api/submit":
                    text = data.get("text")
                    atts = data.get("attachments") or []
                    if not isinstance(text, str) or not isinstance(atts, list) or not all(
                            isinstance(a, dict) and isinstance(a.get("name"), str)
                            and isinstance(a.get("text"), str) for a in atts):
                        self._send(HTTPStatus.BAD_REQUEST, b"Expected {text, attachments: [{name, text}]}\n")
                        return
                    try:
                        outcome = c.submit(text, source="web",
                                           attachments=[{"name": a["name"], "text": a["text"]} for a in atts])
                    except Exception as e:  # never let a command crash the server thread silently
                        c.bus.put(ErrorEvent(str(e)))
                        self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                        return
                    self._json({"view": outcome.view})
                elif path == "/api/cancel":
                    self._json({"cancelled": c.cancel()})
                elif path == "/api/mode":
                    mode = data.get("mode")
                    if mode not in _MODES:
                        self._send(HTTPStatus.BAD_REQUEST, b"Unknown mode\n")
                        return
                    c.set_mode(mode)
                    self._json({"mode": mode})
                elif path == "/api/retry":
                    self._json({"ok": c.retry_last()})
                elif path == "/api/edit":
                    text = data.get("text")
                    if not isinstance(text, str):
                        self._send(HTTPStatus.BAD_REQUEST, b"Missing 'text'\n")
                        return
                    self._json({"ok": c.edit_last(text)})
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"Not found\n")

            def _upload(self):
                # Raw bytes, not multipart: application/octet-stream plus the custom
                # X-Filename header force a CORS preflight, which we never answer.
                if (self.headers.get("Content-Type") or "") != "application/octet-stream":
                    self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, b"Expected application/octet-stream\n")
                    return
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > attach_mod.MAX_UPLOAD_BYTES:
                    self._json({"error": f"file too large (max {attach_mod.MAX_UPLOAD_BYTES // 1_000_000} MB)"},
                               HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                name = unquote(self.headers.get("X-Filename") or "file").replace("/", "_").replace("\\", "_")[:200]
                data = self.rfile.read(length)
                try:
                    ext = attach_mod.extract(name, data)
                except attach_mod.AttachmentError as e:
                    self._json({"error": str(e)}, HTTPStatus.UNPROCESSABLE_ENTITY)
                    return
                self._json({"name": ext.name, "text": ext.text, "kind": ext.kind,
                            "chars": len(ext.text), "pages": ext.pages, "truncated": ext.truncated})

        return Handler


def start_web_server(controller: Controller, host: str, port: int,
                     token: str | None) -> WebServer:
    """Bind and start serving in a daemon thread. Raises OSError if the port is taken."""
    server = WebServer(controller, host, port, token)
    server.start()
    return server
