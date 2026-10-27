"""All configured MCP servers for one harness.

Servers connect on background threads, so momo never waits for one at start-up
(the first `send()` waits briefly, see Harness).  State changes arrive on those
threads too: they only mark the manager dirty and call `on_change`, and the
harness rebuilds its system prompt at the next turn — never mid-turn.
`status_rows()` and the tool lookups never touch the network.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from . import approval
from . import config as config_mod
from . import convert
from .client import MCPClient, _describe
from .jsonrpc import Cancelled, RPCError

RESOURCE_TOOLS = ("mcp_list_resources", "mcp_read_resource")
MAX_RESTARTS = 5
_RESTART_BASE_S = 15.0


def tilde(text: str) -> str:
    """Paths under the home directory shown as ~/… (display only)."""
    home = str(Path.home()).rstrip(os.sep)
    return text.replace(home + os.sep, "~" + os.sep) if home else text


class MCPManager:
    def __init__(self, workdir: Path, *, on_change=None, extra: tuple[Path, ...] = (),
                 disabled: set[str] | None = None, max_chars: int = convert.DEFAULT_MAX_CHARS):
        self.workdir = Path(workdir)
        self.extra = tuple(Path(p) for p in extra)
        self.disabled: set[str] = set(disabled or ())
        self.max_chars = max_chars
        self.on_change = on_change or (lambda: None)
        self.clients: dict[str, MCPClient] = {}
        self.config_errors: list[str] = []
        self.allowed: set[str] = set()      # tools the user said "always" to (this session)
        self._names: dict[str, tuple[str, dict, dict]] = {}   # exposed -> (server, tool, schema)
        self._lock = threading.RLock()
        self._dirty = False
        self._next_restart: dict[str, float] = {}

    # ── state changes ────────────────────────────────────────────────────────

    def _changed(self, dirty: bool = True) -> None:
        if dirty:
            with self._lock:
                self._rebuild_names()
                self._dirty = True
        try:
            self.on_change()
        except Exception:           # a frontend hiccup must not kill a server thread
            pass

    def consume_dirty(self) -> bool:
        with self._lock:
            dirty, self._dirty = self._dirty, False
        return dirty

    def _rebuild_names(self) -> None:
        names: dict[str, tuple[str, dict, dict]] = {}
        for c in self.clients.values():
            if c.state != "ready":
                continue
            for t in c.tools:
                exposed = convert.tool_name(c.name, t["name"], set(names))
                names[exposed] = (c.name, t, convert.to_schema(exposed, c.name, t))
        self._names = names

    # ── lifecycle ────────────────────────────────────────────────────────────

    def _activate(self, client: MCPClient) -> None:
        cfg = client.cfg
        if not cfg.enabled or cfg.name in self.disabled:
            client.state = "disabled"
            return
        verdict = approval.status(self.workdir, cfg)
        if verdict != "approved":
            client.state = "needs-approval" if verdict == "pending" else "denied"
            return
        client.state = "connecting"
        threading.Thread(target=client.connect, daemon=True, name=f"mcp-{cfg.name}").start()

    def reload(self, workdir: Path | None = None) -> str:
        """(Re)read the config files: start new servers, restart changed ones,
        stop removed ones.  Returns a one-line summary."""
        moved = workdir is not None and Path(workdir) != self.workdir
        if workdir is not None:
            self.workdir = Path(workdir)
        configs, self.config_errors = config_mod.load(self.workdir, self.extra)
        started, stopped = [], []
        with self._lock:
            old = self.clients
            new: dict[str, MCPClient] = {}
            for cfg in configs:
                prev = old.pop(cfg.name, None)
                same = (prev is not None and prev.cfg.raw == cfg.raw and prev.cfg.source == cfg.source
                        and not (moved and cfg.source == "project"))
                if same:
                    prev.cfg = cfg
                    if moved:
                        prev.set_workdir(self.workdir)
                    new[cfg.name] = prev
                    continue
                if prev is not None:
                    stopped.append(prev)
                client = MCPClient(cfg, self.workdir, on_change=self._changed)
                new[cfg.name] = client
                self._activate(client)
                if client.state == "connecting":
                    started.append(cfg.name)
            stopped += old.values()
            self.clients = new
            self._rebuild_names()
            self._dirty = True
        for c in stopped:
            threading.Thread(target=c.close, daemon=True).start()
        self._changed(False)
        parts = [f"{len(self.clients)} MCP server(s) configured"]
        if started:
            parts.append("starting " + ", ".join(started))
        if stopped:
            parts.append("stopped " + ", ".join(c.name for c in stopped))
        pending = [c.name for c in self.clients.values() if c.state == "needs-approval"]
        if pending:
            parts.append("waiting for approval: " + ", ".join(pending) + " (/mcp approve <server>)")
        out = "; ".join(parts)
        if self.config_errors:
            out += "\n" + "\n".join("ERROR: " + e for e in self.config_errors)
        return out

    def get(self, name: str) -> MCPClient:
        client = self.clients.get(name)
        if client is None:
            known = ", ".join(self.clients) or "none configured"
            raise KeyError(f"no MCP server '{name}' (servers: {known})")
        return client

    def restart(self, name: str) -> str:
        client = self.get(name)
        client.close("disabled")
        client.crashes = 0
        self._activate(client)
        self._changed(True)
        return f"{name}: {client.state}"

    def set_enabled(self, name: str, on: bool) -> str:
        client = self.get(name)
        if on:
            self.disabled.discard(name)
            if not client.cfg.enabled:
                return (f"ERROR: {name} has \"enabled\": false in {client.cfg.source} config; "
                        "change it there")
            if client.state in ("disabled", "error"):
                client.crashes = 0
                self._activate(client)
        else:
            self.disabled.add(name)
            client.close("disabled")
        self._changed(True)
        return f"{name}: {client.state}"

    def decide(self, name: str, approve: bool) -> str:
        client = self.get(name)
        if client.cfg.source != "project":
            return f"{name} comes from the {client.cfg.source} config and needs no approval"
        approval.decide(self.workdir, client.cfg, approve)
        if approve:
            if client.state in ("needs-approval", "denied"):
                self._activate(client)
        else:
            client.close("denied")
        self._changed(True)
        return f"{name}: {'approved' if approve else 'denied'} — {client.state}"

    def wait_ready(self, timeout: float) -> list[str]:
        """Wait until no server is connecting (or timeout); returns the ones still connecting."""
        deadline = time.monotonic() + timeout
        while True:
            waiting = [c.name for c in self.clients.values() if c.state == "connecting"]
            if not waiting or time.monotonic() >= deadline:
                return waiting
            time.sleep(0.1)

    def tick(self) -> None:
        """Restart servers that crashed after having worked, with backoff."""
        now = time.monotonic()
        for c in list(self.clients.values()):
            if c.state != "error" or not 0 < c.crashes <= MAX_RESTARTS:
                continue
            due = self._next_restart.setdefault(c.name, now + _RESTART_BASE_S * 2 ** (c.crashes - 1))
            if now >= due:
                self._next_restart.pop(c.name, None)
                self._activate(c)
                self._changed(False)

    def shutdown(self) -> None:
        """Stop every server and forget them (a later reload() starts afresh)."""
        with self._lock:
            clients, self.clients = list(self.clients.values()), {}
            self._rebuild_names()
            self._dirty = True
        threads = [threading.Thread(target=c.close, daemon=True) for c in clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)

    # ── tools ────────────────────────────────────────────────────────────────

    def is_mcp(self, name: str) -> bool:
        return name in self._names

    def tool_schemas(self, read_only_only: bool = False) -> list[dict]:
        names = self._names
        out = [schema for server, tool, schema in names.values()
               if not read_only_only or convert.read_only(tool)]
        out += self.resource_tool_schemas()
        return out

    def server_of(self, name: str) -> str | None:
        hit = self._names.get(name)
        return hit[0] if hit else None

    def exposure(self, name: str) -> str | None:
        """The exposure of an exposed tool's server ("discover" | "direct")."""
        hit = self._names.get(name)
        return self.clients[hit[0]].cfg.exposure if hit else None

    def policy(self, name: str) -> tuple[str, bool]:
        """(confirm mode, read-only?) for an exposed tool name."""
        server, tool, _ = self._names[name]
        return self.clients[server].cfg.confirm, convert.read_only(tool)

    def call(self, name: str, args: dict, cancel: threading.Event | None = None) -> str:
        hit = self._names.get(name)
        if hit is None:
            return f"ERROR: the MCP tool {name} is not available any more (its server stopped)"
        server, tool, _ = hit
        client = self.clients[server]
        try:
            result = client.call_tool(tool["name"], args, cancel)
        except Cancelled:
            return "ERROR: interrupted by the user"
        except TimeoutError:
            return (f"ERROR: the MCP server '{server}' did not answer within "
                    f"{client.cfg.timeout:.0f} s")
        except RPCError as e:
            hint = " — check the arguments against the tool's parameters" if e.code == -32602 else ""
            return f"ERROR: the MCP server '{server}' rejected the call: {convert.clean(e.message, 500)}{hint}"
        except (OSError, ConnectionError) as e:
            return f"ERROR: MCP server '{server}': {convert.clean(_describe(e), 500)}"
        return convert.render_result(result, server=server, untrusted=client.cfg.untrusted,
                                     max_chars=self.max_chars)

    def tool_rows(self, server: str | None = None) -> list[dict]:
        rows = []
        for exposed, (srv, tool, schema) in self._names.items():
            if server is not None and srv != server:
                continue
            rows.append({"name": exposed, "server": srv, "tool": convert.clean(tool["name"], 100),
                         "title": convert.clean(tool.get("title"), 100),
                         "desc": schema["function"]["description"],
                         "read_only": convert.read_only(tool),
                         "destructive": convert.destructive(tool),
                         "params": list(schema["function"]["parameters"]["properties"])})
        return rows

    def instructions(self) -> list[tuple[str, str]]:
        return [(c.name, c.instructions) for c in self.clients.values()
                if c.state == "ready" and c.instructions]

    # ── resources ────────────────────────────────────────────────────────────

    def resource_servers(self) -> list[str]:
        return [c.name for c in self.clients.values() if c.has_resources]

    def resource_tool_schemas(self) -> list[dict]:
        servers = self.resource_servers()
        if not servers:
            return []
        server = {"type": "string", "enum": servers,
                  "description": "MCP server name: " + ", ".join(servers)}
        return [
            {"type": "function", "function": {
                "name": "mcp_list_resources",
                "description": ("List the resources (documents, files, records) that the "
                                "connected MCP servers offer, with their URIs. Read one with "
                                "mcp_read_resource."),
                "parameters": {"type": "object", "properties": {"server": server}, "required": []}}},
            {"type": "function", "function": {
                "name": "mcp_read_resource",
                "description": "Read one MCP resource by its URI (from mcp_list_resources).",
                "parameters": {"type": "object", "properties": {
                    "server": server,
                    "uri": {"type": "string", "description": "The resource URI, e.g. file:///notes.md"}},
                    "required": ["server", "uri"]}}},
        ]

    def list_resources(self, server: str | None = None) -> str:
        names = [server] if server else self.resource_servers()
        if not names:
            return "No connected MCP server offers resources."
        lines = []
        for name in names:
            try:
                client = self.get(name)
                items = client.list_resources()
            except (KeyError, OSError, ConnectionError, RPCError, TimeoutError) as e:
                lines.append(f"ERROR: {name}: {convert.clean(_describe(e) if not isinstance(e, KeyError) else e.args[0], 300)}")
                continue
            lines.append(f"{name}: {len(items)} resource(s)")
            for r in items[:200]:
                desc = convert.clean(r.get("description") or r.get("title") or r.get("name"), 120)
                mime = convert.clean(r.get("mimeType"), 60)
                lines.append(f"  {convert.clean(r['uri'], 300)}"
                             + (f"  ({mime})" if mime else "") + (f" — {desc}" if desc else ""))
            if len(items) > 200:
                lines.append(f"  … {len(items) - 200} more")
        return convert.cap("\n".join(lines), self.max_chars, "list one server at a time")

    def resource_rows(self, server: str) -> list[dict]:
        """One server's resources for the web UI's Attach list (raises ValueError)."""
        try:
            items = self.get(server).list_resources()
        except KeyError as e:
            raise ValueError(e.args[0]) from None
        except (OSError, ConnectionError, RPCError, TimeoutError) as e:
            raise ValueError(f"{server}: {_describe(e)}") from None
        return [{"uri": convert.clean(r["uri"], 500),
                 "name": convert.clean(r.get("title") or r.get("name") or r["uri"], 200),
                 "mime": convert.clean(r.get("mimeType"), 60),
                 "desc": convert.clean(r.get("description"), 300)} for r in items[:500]]

    def _read(self, server: str, uri: str, cancel=None) -> tuple[MCPClient, dict]:
        client = self.get(server)
        return client, client.read_resource(uri, cancel)

    def read_resource(self, server: str, uri: str, cancel=None) -> str:
        """For the model: rendered, capped and (for untrusted servers) fenced."""
        try:
            client, res = self._read(server, uri, cancel)
        except KeyError as e:
            return f"ERROR: {e.args[0]}"
        except Cancelled:
            return "ERROR: interrupted by the user"
        except (OSError, ConnectionError, RPCError, TimeoutError) as e:
            return f"ERROR: MCP server '{server}': {convert.clean(_describe(e), 500)}"
        return convert.render_resource(res, server=server, untrusted=client.cfg.untrusted,
                                       max_chars=self.max_chars)

    def resource_text(self, server: str, uri: str) -> tuple[str, str]:
        """(name, text) for attaching a resource to a message — the user picked
        it, so it is not fenced; raises ValueError with a message."""
        try:
            _, res = self._read(server, uri)
        except KeyError as e:
            raise ValueError(e.args[0]) from None
        except (OSError, ConnectionError, RPCError, TimeoutError) as e:
            raise ValueError(f"{server}: {_describe(e)}") from None
        texts = [c.get("text") or "" for c in res.get("contents") or ()
                 if isinstance(c, dict) and "text" in c]
        if not texts:
            raise ValueError(f"{uri} has no text content")
        name = uri.rstrip("/").rsplit("/", 1)[-1] or uri
        return convert.clean(name, 120), convert.clean("\n\n".join(texts))

    # ── status ───────────────────────────────────────────────────────────────

    def status_rows(self) -> list[dict]:
        rows = []
        for c in self.clients.values():
            tools = [t for t in self._names.values() if t[0] == c.name]
            info = c.server_info or {}
            rows.append({"name": c.name, "source": c.cfg.source, "transport": c.cfg.transport,
                         "target": convert.clean(tilde(c.cfg.describe()), 300), "state": c.state,
                         "error": c.error, "tools": len(tools),
                         "read_only": sum(convert.read_only(t[1]) for t in tools),
                         "resources": c.has_resources,
                         "resource_count": c.resource_count if c.state == "ready" else 0,
                         "confirm": c.cfg.confirm,
                         "exposure": c.cfg.exposure,
                         "untrusted": c.cfg.untrusted,
                         "server": convert.clean(" ".join(str(info.get(k, "")) for k in ("name", "version")).strip(), 100)})
        return rows

    def counts(self) -> tuple[int, int, bool]:
        """(ready, configured, attention?) — attention when one failed or waits for approval."""
        states = [c.state for c in self.clients.values()]
        return (states.count("ready"), len(states),
                any(s in ("error", "needs-approval") for s in states) or bool(self.config_errors))
