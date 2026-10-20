"""One MCP server connection: lifecycle, tools and resources."""
from __future__ import annotations

import fnmatch
import os
import threading
import time
from pathlib import Path

from ..paths import momo_home
from . import config as config_mod
from .convert import clean
from .http import HTTPTransport
from .jsonrpc import Cancelled, Peer, RPCError
from .stdio import StdioTransport

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
# npx/uvx download the server on first start, so initialize gets a long budget.
INIT_TIMEOUT_S = 90.0
LIST_TIMEOUT_S = 30.0
MAX_PAGES = 50

# disabled → (needs-approval | denied) → connecting → ready | error
STATES = ("disabled", "needs-approval", "denied", "connecting", "ready", "error")


def log_path(name: str) -> Path:
    return momo_home() / "mcp-logs" / f"{name}.log"


class MCPClient:
    def __init__(self, cfg: config_mod.ServerConfig, workdir: Path, on_change=None):
        self.cfg = cfg
        self.workdir = Path(workdir)
        self.on_change = on_change or (lambda dirty=False: None)
        self.state = "disabled"
        self.error = ""
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self.instructions = ""
        self.protocol_version = ""
        self.tools: list[dict] = []
        # Resources listed at start (and on list_changed).  SDK servers declare the
        # capability even with nothing to offer, and two resource tools for an
        # empty list would only cost a small model's context.
        self.resource_count = 0
        self.peer: Peer | None = None
        self.transport = None
        self.started_at = 0.0
        self.crashes = 0              # consecutive exits after being ready (manager restarts)
        self._lock = threading.Lock()
        self._closing = False

    @property
    def name(self) -> str:
        return self.cfg.name

    def _set(self, state: str, error: str = "", dirty: bool = True) -> None:
        self.state, self.error = state, clean(error, 500)
        self.on_change(dirty)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Start the server and complete the handshake (blocking; run it on a
        thread).  Ends in state ready or error."""
        with self._lock:
            self._closing = False
            self.tools = []
            self._set("connecting", dirty=False)
            try:
                cfg = config_mod.expanded(self.cfg, dict(os.environ))
                if cfg.transport == "stdio":
                    cwd = cfg.cwd or str(self.workdir)
                    if cfg.cwd and not os.path.isabs(cfg.cwd):
                        cwd = str(self.workdir / cfg.cwd)
                    self.transport = StdioTransport(cfg.command, cfg.args, cfg.env, cwd,
                                                    log_path(self.name))
                else:
                    self.transport = HTTPTransport(cfg.url, cfg.headers,
                                                   read_timeout=max(cfg.timeout, 30.0) + 30)
                self.peer = Peer(self.transport,
                                 handlers={"ping": lambda p: {},
                                           "roots/list": self._roots},
                                 on_notification=self._on_notification,
                                 on_close=self._on_close)
                self.peer.start()
                init = self.peer.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": True}},
                    "clientInfo": {"name": "momo-coding-harness", "version": "1"},
                }, timeout=INIT_TIMEOUT_S)
                version = str(init.get("protocolVersion") or "")
                if version not in SUPPORTED_VERSIONS:
                    raise ConnectionError(f"unsupported MCP protocol version {version!r}")
                self.protocol_version = version
                if isinstance(self.transport, HTTPTransport):
                    self.transport.protocol_version = version
                self.server_info = init.get("serverInfo") if isinstance(init.get("serverInfo"), dict) else {}
                self.capabilities = init.get("capabilities") if isinstance(init.get("capabilities"), dict) else {}
                self.instructions = clean(init.get("instructions") or "", 1500).strip()
                self.peer.notify("notifications/initialized")
                self.tools = self._list_tools() if "tools" in self.capabilities else []
                self.resource_count = self._count_resources()
                self.started_at = time.monotonic()
            except (OSError, ConnectionError, RPCError, TimeoutError, Cancelled,
                    config_mod.ConfigError, ValueError) as e:
                self._shutdown_transport()
                self._set("error", _describe(e))
                return
            self._set("ready")

    def _shutdown_transport(self) -> None:
        self._closing = True
        if self.transport is not None:
            try:
                self.transport.close()
            except OSError:
                pass

    def close(self, state: str = "disabled") -> None:
        self._shutdown_transport()
        self.tools = []
        self._set(state, dirty=True)

    def _on_close(self, reason: str) -> None:
        if self._closing or self.state != "ready":
            return
        self.crashes += 1
        self.tools = []
        self._set("error", reason)

    def _roots(self, params: dict) -> dict:
        return {"roots": [{"uri": self.workdir.resolve().as_uri(), "name": self.workdir.name}]}

    def set_workdir(self, workdir: Path) -> None:
        self.workdir = Path(workdir)
        if self.state == "ready" and self.peer is not None:
            try:
                self.peer.notify("notifications/roots/list_changed")
            except (OSError, ConnectionError):
                pass

    def _on_notification(self, method: str, params: dict) -> None:
        if method == "notifications/tools/list_changed" and self.state == "ready":
            threading.Thread(target=self._refresh_tools, daemon=True, name="mcp-tools").start()
        elif method == "notifications/resources/list_changed" and self.state == "ready":
            threading.Thread(target=self._refresh_resources, daemon=True, name="mcp-res").start()
        elif method == "notifications/message" and isinstance(self.transport, StdioTransport):
            self.transport._log(f"[{clean(params.get('level'), 20)}] {clean(params.get('data'), 500)}")

    def _refresh_tools(self) -> None:
        try:
            self.tools = self._list_tools()
        except (OSError, ConnectionError, RPCError, TimeoutError):
            return
        self.on_change(True)

    def _count_resources(self) -> int:
        if "resources" not in self.capabilities:
            return 0
        try:
            return len(self._paged("resources/list", "resources"))
        except (OSError, ConnectionError, RPCError, TimeoutError):
            return 0

    def _refresh_resources(self) -> None:
        self.resource_count = self._count_resources()
        self.on_change(True)

    # ── requests ─────────────────────────────────────────────────────────────

    def _paged(self, method: str, key: str) -> list[dict]:
        items, cursor = [], None
        for _ in range(MAX_PAGES):
            res = self.peer.request(method, {"cursor": cursor} if cursor else {},
                                    timeout=LIST_TIMEOUT_S)
            items += [t for t in res.get(key) or () if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return items

    def _list_tools(self) -> list[dict]:
        tools = [t for t in self._paged("tools/list", "tools") if isinstance(t.get("name"), str)]
        if self.cfg.include is not None:
            tools = [t for t in tools if any(fnmatch.fnmatchcase(t["name"], p) for p in self.cfg.include)]
        return [t for t in tools if not any(fnmatch.fnmatchcase(t["name"], p) for p in self.cfg.exclude)]

    def _require_ready(self) -> Peer:
        if self.state != "ready" or self.peer is None:
            raise ConnectionError(f"MCP server '{self.name}' is {self.state}"
                                  + (f": {self.error}" if self.error else ""))
        return self.peer

    def call_tool(self, tool: str, args: dict, cancel: threading.Event | None = None) -> dict:
        return self._require_ready().request("tools/call", {"name": tool, "arguments": args},
                                             timeout=self.cfg.timeout, cancel=cancel)

    @property
    def has_resources(self) -> bool:
        return self.state == "ready" and self.resource_count > 0

    def list_resources(self) -> list[dict]:
        self._require_ready()
        if "resources" not in self.capabilities:
            return []
        return [r for r in self._paged("resources/list", "resources") if isinstance(r.get("uri"), str)]

    def read_resource(self, uri: str, cancel: threading.Event | None = None) -> dict:
        return self._require_ready().request("resources/read", {"uri": uri},
                                             timeout=self.cfg.timeout, cancel=cancel)

    def log_tail(self, n: int = 40) -> list[str]:
        if isinstance(self.transport, StdioTransport):
            return [clean(line, 1000) for line in list(self.transport.tail)[-n:]]
        return []


def _describe(e: Exception) -> str:
    if isinstance(e, RPCError):
        return f"server error {e.code}: {e.message}"
    if isinstance(e, TimeoutError):
        return str(e) or "timed out"
    return str(e) or type(e).__name__
