"""Local web UI: a stdlib HTTP server exposing the shared Controller to a browser.

Routes
  GET  /              index.html (+ /app.css and the ES modules in _STATIC_FILES)
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
  POST /api/sessions/delete {"names": [...]} → delete saved sessions (never the current one)
  GET  /api/models    models available on the backend
  GET  /api/context   context usage broken down per category (system, tools, …)
  GET  /api/index     code index memory broken down per category and language
  GET  /api/tools     the current mode's tools and whether each is on (/tools), and
                      the workspace's tools with their approval state
  GET  /api/skills    every skill: title, summary, token estimate, active (/load-skill)
  GET  /api/system-prompt the composed system prompt in its parts (/system-prompt)
  GET  /api/mcp       MCP servers: state, tools, config files (/mcp)
  GET  /api/mcp/resources ?server= → that server's resources
  GET  /api/mcp/resource  ?server=&uri= → {name, text} to attach to a message
  POST /api/mcp/servers   {"action": "add"|"remove", "name", "entry"} → edit ~/.momo-harness/mcp.json
  GET  /api/export    the conversation as a Markdown download (?full=1: the full log)
  GET  /api/files     ?path= → directory listing (read-only, confined to the workspace)
  GET  /api/file      ?path= → file contents as text (same conversion as uploads)
  GET  /api/files/search ?q= → fuzzy path search for @-mentions
  GET  /api/complete  ?text=&cursor= → input-box completion: /command words and @ paths
  GET  /api/changes   the file checkpoints since the last /keep: per turn, and net per file
  GET  /api/changes/diff ?path=&checkpoint= → one file's diff (no checkpoint: since keep)
  GET  /api/changes/preview ?checkpoint= → what undoing back to it would restore / skip
  POST /api/undo      {"checkpoint": n|null, "force": bool} → like /undo [force] [n]
  POST /api/changes/revert {"path", "force"} → put one file back as at the last /keep
  POST /api/keep      accept every file change (/keep)

Security: bound to loopback by default, with Host-header checks against DNS
rebinding.  On a non-loopback bind an access token is required (``?token=`` once,
then an HttpOnly SameSite=Strict cookie).  POSTs must be JSON and same-origin.
``--web-insecure`` drops the token on purpose; the Host check then admits only this
machine's own names, so DNS rebinding stays blocked.

HTTPS: given a certificate (``--web-cert`` or momo's own CA, see tls.py) the
listening socket is wrapped with TLS 1.2+, the token cookie is marked Secure, and
in auto mode the CA certificate — public by nature — is served at /momo-ca.pem
without a token so other devices can download and trust it.

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
import ssl
import threading
from dataclasses import asdict
from functools import cache
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import attachments as attach_mod
from .. import checkpoints as checkpoints_mod
from .. import companion
from .. import complete as complete_mod
from .. import ignore_rules
from ..diff import build_diff_body
from ..file_search import fuzzy_search, workspace_files
from .. import session as session_mod
from ..momo_dir import MOMO_DIR
from ..paths import SKIP_DIRS, safe_path
from ..commands import export_markdown, help_commands
from ..controller import CONFIRM_SUFFIX, Controller
from ..harness import DIFF_TOOLS
from ..events import ErrorEvent, event_to_json
from ..mcp import config as mcp_config
from ..prompts import MODES

_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_FILES = {
    "/":         ("index.html", "text/html; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/app.css":  ("app.css",    "text/css; charset=utf-8"),
    **{f"/{name}": (name, "text/javascript; charset=utf-8")
       for name in ("app.js", "dom.js", "markdown.js", "highlight.js", "notify.js",
                    "companion.js", "sessions.js", "files.js", "chips.js", "palette.js", "find.js",
                    "mcp.js", "skills.js", "diff.js", "changes.js", "planview.js", "todos.js")},
}
_MODES = list(MODES)
_MAX_BODY = 16_000_000  # JSON bodies; a submit carries attachment text
_KEEPALIVE_S = 15.0
_COOKIE = "momo_token"
_MAX_PREVIEW_BYTES = 5_000_000


def _host_form(host: str) -> str:
    """A name as it appears in a Host header (without port): lowercase, IPv6 bracketed."""
    h = host.strip().lower()
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        return h
    return f"[{ip}]" if ip.version == 6 else str(ip)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@cache
def _pdf_support() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


@cache
def _companion_data() -> dict:
    return {
        "walk_right": companion.MOMO_WR,
        "walk_left": companion.MOMO_WL,
        "sit": companion.MOMO_SIT,
        "sit_left": companion.MOMO_SIT_L,
        "walk_right_blink": companion.MOMO_WR_BLINK,
        "walk_left_blink": companion.MOMO_WL_BLINK,
        "speech": {f"{mode}|{int(busy)}": lines
                   for (mode, busy), lines in companion.SPEECH_TEXTS.items()},
        "speech_default": companion.SPEECH_TEXTS_DEFAULT,
        "bubble_max": companion.BUBBLE_MAX,
        "cat_w": companion.CAT_W,
        # companion.RecapPicker's rules, which app.js mirrors.
        "recap": {"repeat_secs": companion.RECAP_REPEAT_SECS,
                  "reuse_chance": companion.RECAP_REUSE_CHANCE,
                  "reuse_pool": companion.RECAP_REUSE_POOL,
                  "max_new": companion.MAX_RECAP_LINES},
    }


# ── sessions / workspace helpers ────────────────────────────────────────────

_session_cache: dict[Path, tuple[float, dict]] = {}
_session_lock = threading.Lock()   # request threads share the cache


def _session_infos(paths: list[Path]) -> list[dict]:
    """Drawer rows for these session files; the cache keeps only these."""
    with _session_lock:
        for gone in _session_cache.keys() - set(paths):   # deleted or aged out
            del _session_cache[gone]
    return [i for p in paths if (i := _session_info(p))]


def _session_info(path: Path) -> dict | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    with _session_lock:
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
        "title": str(data.get("title") or ""),
    }
    with _session_lock:
        _session_cache[path] = (mtime, info)
    return info


def _list_workspace(root: Path, rel: str, hidden: bool) -> list[dict] | str:
    target = safe_path(rel or ".", root)
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
        # momo's own folder (scratchpad, run logs) shows with the hidden files.
        if (not hidden and e.name.startswith(".")) or (e.name in SKIP_DIRS and e.name != MOMO_DIR):
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
    def __init__(self, controller: Controller, host: str, port: int, token: str | None,
                 *, cert: str | Path | None = None, key: str | Path | None = None,
                 ca_pem: str | Path | None = None, extra_hosts=()):
        self.controller = controller
        self.host = host
        self.token = token
        self.tls = cert is not None
        self.ca_pem = Path(ca_pem) if ca_pem else None
        self.extra_hosts = {_host_form(h) for h in extra_hosts}
        context = None
        if cert is not None:
            # Load before binding, so a bad certificate fails without holding the port.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
        self._stopping = threading.Event()
        server_cls = _Server
        if ":" in host.strip("[]"):
            server_cls = type("_Server6", (_Server,), {"address_family": socket.AF_INET6})
        self._httpd = server_cls((host.strip("[]"), port), self._make_handler())
        if context is not None:
            # The handshake runs on the first read, in the request's own thread, so a
            # client that stalls mid-handshake cannot block accept() for everyone else.
            self._httpd.socket = context.wrap_socket(
                self._httpd.socket, server_side=True, do_handshake_on_connect=False)
        self.port = self._httpd.server_address[1]
        url_host = f"[{host.strip('[]')}]" if ":" in host else host
        if url_host in ("0.0.0.0", "[::]"):
            url_host = socket.gethostname()
        self.url = f"{'https' if self.tls else 'http'}://{url_host}:{self.port}/"
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
            "context_limit": h.context_limit,
            "pdf_support": _pdf_support(),
            "max_upload": attach_mod.MAX_UPLOAD_BYTES,
            "modes": _MODES,
            "commands": help_commands(),
            "skills": {"available": h.list_available_skills(), "active": list(h.active_skills)},
            "plan": h.plan.to_markdown() if h.plan is not None else None,
            "plan_phase": h.plan_phase if h.plan is not None else None,
            "plan_kind": h._plan_kind_shown(),
            "plan_view": h.plan_view(),
            "history": list(h.input_history[-200:]),
            "companion": _companion_data(),
            "can_switch_model": h.client.can_switch_model,
            "diff_tools": sorted(DIFF_TOOLS),
            "confirm_suffix": CONFIRM_SUFFIX,
            "session": h.session_path().stem,
            "mcp_servers": list(h.mcp.clients),
        }

    def _make_handler(self):
        web = self
        allowed_hosts = None
        if not self.token:
            # Loopback bind: only accept requests addressed to a loopback name,
            # which defeats DNS-rebinding pages that resolve to 127.0.0.1.
            # With --web-insecure off loopback, also this machine's own names.
            allowed_hosts = {"localhost", "127.0.0.1", "[::1]", _host_form(self.host)} | self.extra_hosts

        class Handler(BaseHTTPRequestHandler):
            server_version = "momo-web"

            def log_message(self, format, *args):  # noqa: A002 — silence stderr logging
                pass

            # ── helpers ──────────────────────────────────────────────────────

            def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
                      headers: dict | None = None, cache: str = "no-store"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", cache)
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

                if path == "/momo-ca.pem" and web.ca_pem is not None:
                    # A CA certificate is public; devices need it before they can log in.
                    try:
                        body = web.ca_pem.read_bytes()
                    except OSError:
                        self._send(HTTPStatus.NOT_FOUND, b"Not found\n")
                        return
                    # The x509-ca-cert type and a .crt name make phones offer to install it.
                    self._send(HTTPStatus.OK, body, "application/x-x509-ca-cert", headers={
                        "Content-Disposition": 'attachment; filename="momo-ca.crt"'})
                    return

                if path == "/" and web.token:
                    qs_token = (parse_qs(parts.query).get("token") or [""])[0]
                    if qs_token and hmac.compare_digest(qs_token.encode(), web.token.encode()):
                        # Trade the URL token for a cookie and drop it from the address bar.
                        self._send(HTTPStatus.SEE_OTHER, headers={
                            "Location": "/",
                            "Set-Cookie": f"{_COOKIE}={web.token}; HttpOnly; SameSite=Strict; Path=/"
                                          + ("; Secure" if web.tls else ""),
                        })
                        return

                if not self._authed():
                    self._send(HTTPStatus.UNAUTHORIZED,
                               b"Unauthorized: open the URL printed by momo (it contains ?token=...)\n")
                    return

                if path in _STATIC_FILES:
                    self._static(*_STATIC_FILES[path])
                elif path == "/api/state":
                    self._json(web._state())
                elif path == "/api/events":
                    self._stream_events()
                else:
                    self._get_api(path, parse_qs(parts.query))

            def _static(self, name: str, ctype: str):
                # Revalidated on every load (no-cache) but only re-sent when the file
                # changed: the browser keeps it and asks with If-None-Match.
                f = _STATIC_DIR / name
                try:
                    st = f.stat()
                    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
                    if self.headers.get("If-None-Match") == etag:
                        self._send(HTTPStatus.NOT_MODIFIED, headers={"ETag": etag}, cache="no-cache")
                        return
                    body = f.read_bytes()
                except OSError:
                    self._send(HTTPStatus.NOT_FOUND, b"Not found\n")
                    return
                self._send(HTTPStatus.OK, body, ctype, headers={"ETag": etag}, cache="no-cache")

            def do_HEAD(self):
                # Static files only: HEAD on the event stream would hold the connection.
                if urlsplit(self.path).path in _STATIC_FILES:
                    self.do_GET()
                else:
                    self._send(HTTPStatus.METHOD_NOT_ALLOWED, headers={"Allow": "GET, POST"})

            def _get_api(self, path: str, qs: dict):
                h = web.controller.harness
                arg = lambda k, d="": (qs.get(k) or [d])[0]  # noqa: E731
                if path == "/api/sessions":
                    infos = _session_infos(session_mod.list_sessions()[:50])
                    self._json({"current": h.session_path().stem, "sessions": infos})
                elif path == "/api/models":
                    self._json({"current": h.client.model, "models": h.client.list_models(),
                                "can_switch": h.client.can_switch_model,
                                "provider": h.client.provider_name})
                elif path == "/api/context":
                    self._json(h.context_breakdown())
                elif path == "/api/index":
                    self._json(h.index_breakdown())
                elif path == "/api/system-prompt":
                    self._json(h.system_prompt_view())
                elif path == "/api/mcp":
                    self._json(h.mcp_view())
                elif path in ("/api/mcp/resources", "/api/mcp/resource"):
                    try:
                        if path == "/api/mcp/resources":
                            self._json({"resources": h.mcp.resource_rows(arg("server"))})
                        else:
                            name, text = h.mcp.resource_text(arg("server"), arg("uri"))
                            self._json({"name": name, "text": text, "chars": len(text)})
                    except ValueError as e:
                        self._json({"error": str(e)}, HTTPStatus.BAD_GATEWAY)
                elif path == "/api/skills":
                    self._json({"skills": h.skill_choices()})
                elif path == "/api/tools":
                    self._json({"mode": h.mode, "tools_enabled": h.tools_enabled,
                                "tools_discover": h.tools_discover,
                                "tools": h.tool_choices(), "workspace": h.workspace_choices()})
                elif path == "/api/index-filter":
                    text, fpath = h.index_filter()
                    self._json({"path": fpath, "text": text,
                                "rules": len(ignore_rules.Rules.parse(text))})
                elif path == "/api/last-user":
                    self._json(web.controller.last_user_message() or {})
                elif path == "/api/export":
                    full = arg("full") == "1"
                    body = export_markdown(h, full).encode("utf-8")
                    name = f"momo-{h.session_path().stem}{'-full' if full else ''}.md"
                    self._send(HTTPStatus.OK, body, "text/markdown; charset=utf-8", headers={
                        "Content-Disposition": f'attachment; filename="{name}"'})
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
                elif path == "/api/complete":
                    text = arg("text")
                    try:
                        cursor = max(0, min(len(text), int(arg("cursor", str(len(text))))))
                    except ValueError:
                        cursor = len(text)
                    got = complete_mod.complete(text, cursor, complete_mod.Sources(h, help_commands()),
                                                wait=True)
                    self._json(got.to_dict() if got else {"start": cursor, "items": [], "waiting": False})
                elif path == "/api/changes":
                    store = h.checkpoints
                    self._json({"turns": store.turns(), "since_keep": store.cumulative(),
                                "undo_max": h.undo_max, "workdir": str(h.workdir)})
                elif path in ("/api/changes/diff", "/api/changes/preview"):
                    cp = arg("checkpoint")
                    if cp and not cp.isdigit():
                        self._send(HTTPStatus.BAD_REQUEST, b"checkpoint must be a number\n")
                        return
                    cp = int(cp) if cp else None
                    if path == "/api/changes/preview":
                        plan = h.checkpoints.preview(cp)
                        self._json({**plan, "text": checkpoints_mod.describe(plan),
                                    "plan_progress": h.plan_progress_undone(
                                        # The UI forces an undo with conflicts only.
                                        plan["ids"], partial=bool(plan["conflicts"] and plan["restore"]))})
                        return
                    # Only a path the store tracks: never a way to read any file.
                    diff = h.checkpoints.diff(arg("path"), cp)
                    if diff is None:
                        self._json({"error": "not a changed file"}, HTTPStatus.NOT_FOUND)
                    else:
                        body, _, _ = build_diff_body(diff.pop("old"), diff.pop("new"))
                        self._json({**diff, "body": body})
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"Not found\n")

            def _file(self, root: Path, rel: str):
                target = safe_path(rel, root)
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
                            # A named event, not an SSE comment: the page sees it
                            # and can tell a stalled stream from a quiet one.
                            self.wfile.write(b"event: ping\ndata: \n\n")
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
                    self._json({"mode": mode, "ok": c.set_mode(mode)})
                elif path == "/api/sessions/delete":
                    names = data.get("names")
                    if (not isinstance(names, list) or not names or len(names) > 1000
                            or not all(isinstance(n, str) for n in names)):
                        self._send(HTTPStatus.BAD_REQUEST, b"Expected {names: [session names]}\n")
                        return
                    deleted, skipped = session_mod.delete_sessions(
                        names, current=c.harness.session_path().stem)
                    self._json({"deleted": deleted, "skipped": skipped})
                elif path == "/api/retry":
                    self._json({"ok": c.retry_last()})
                elif path == "/api/index-filter":
                    text = data.get("text")
                    if not isinstance(text, str):
                        self._send(HTTPStatus.BAD_REQUEST, b"Expected {text}\n")
                        return
                    msg = c.harness.set_index_filter(text)
                    if msg.startswith("ERROR"):
                        self._json({"error": msg}, HTTPStatus.INTERNAL_SERVER_ERROR)
                        return
                    c._system(msg)      # the change shows in every frontend's transcript
                    self._json({"ok": True, "message": msg})
                elif path == "/api/mcp/servers":
                    # Adding a stdio server means momo will run that command, so
                    # unlike the other POSTs a missing Origin is refused too: only
                    # this page (a browser always sends Origin on a fetch POST) may.
                    if not self.headers.get("Origin"):
                        self._send(HTTPStatus.FORBIDDEN, b"Forbidden: Origin header required\n")
                        return
                    name, action, entry = data.get("name"), data.get("action"), data.get("entry")
                    if (not isinstance(name, str) or action not in ("add", "remove")
                            or (action == "add" and not isinstance(entry, dict))):
                        self._send(HTTPStatus.BAD_REQUEST, b"Expected {action: add|remove, name, entry}\n")
                        return
                    if c.busy:
                        self._json({"error": "Busy: finish or interrupt the turn first"}, HTTPStatus.CONFLICT)
                        return
                    try:
                        msg = (mcp_config.add_server(name, entry) if action == "add"
                               else mcp_config.remove_server(name))
                    except mcp_config.ConfigError as e:
                        self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
                        return
                    if (note := c.harness.mcp_reload()):
                        msg += "\n" + note
                    c._system(msg)
                    self._json({"ok": True, "message": msg})
                elif path in ("/api/undo", "/api/changes/revert", "/api/keep"):
                    cp, force, fpath = data.get("checkpoint"), data.get("force") is True, data.get("path")
                    if path == "/api/undo" and cp is not None and (
                            not isinstance(cp, int) or isinstance(cp, bool)):
                        self._send(HTTPStatus.BAD_REQUEST, b"Expected {checkpoint: n|null, force}\n")
                        return
                    if path == "/api/changes/revert" and not isinstance(fpath, str):
                        self._send(HTTPStatus.BAD_REQUEST, b"Expected {path, force}\n")
                        return
                    h = c.harness
                    action = (lambda: h.undo_files(cp, force)) if path == "/api/undo" else \
                        (lambda: h.revert_file(fpath, force)) if path == "/api/changes/revert" else \
                        h.keep_files
                    res = c.file_action(action)
                    self._json(res, HTTPStatus.OK if res["ok"] else HTTPStatus.CONFLICT)
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
                     token: str | None, *, cert=None, key=None, ca_pem=None,
                     extra_hosts=()) -> WebServer:
    """Bind and start serving in a daemon thread. Raises OSError if the port is
    taken or the certificate cannot be loaded (ssl.SSLError is an OSError)."""
    server = WebServer(controller, host, port, token, cert=cert, key=key,
                       ca_pem=ca_pem, extra_hosts=extra_hosts)
    server.start()
    return server
