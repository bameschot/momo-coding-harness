"""The harness side of MCP: which MCP tools a role gets, running a call (with
the confirm policy), the system-prompt section and the status fields.

The servers themselves live in `harness.mcp` (mcp/manager.py).  Their state
changes arrive on background threads, which only emit status; the system
prompt is rebuilt at the start of the next turn (`_mcp_before_turn`), so a
running turn's tool set never changes under it.
"""
from __future__ import annotations

import json

from . import net as net_mod
from . import session as session_mod
from .events import ChatEvent
from .mcp import RESOURCE_TOOLS, MCPManager
from .mcp.convert import clean

# The first turn waits this long for servers still starting (npx/uvx can be slow).
MCP_START_WAIT_S = 10.0
_DECLINED = ("ERROR: the user declined this call, so it was never sent to the MCP "
             "server. Nothing is wrong with the server. Do not retry — ask the user "
             "what they would like to do instead.")


class MCPMixin:
    def _init_mcp(self) -> None:
        # /mcp on|off (prefs).  Servers start only in start_mcp() (main.py), so a
        # Harness built by a test or an eval never launches anything.
        self.mcp_enabled: bool = True
        # /mcp-confirm: ask before calls the server's confirm policy covers.
        # Session-only, like net_confirm: security toggles reset to safe defaults.
        self.mcp_confirm: bool = True
        self.mcp = MCPManager(self.workdir, on_change=self._on_mcp_change,
                              disabled=set(session_mod.load_prefs().get("mcp_disabled") or ()))
        self._mcp_started = False
        self._mcp_waited = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start_mcp(self, extra=()) -> str:
        """Read the config files and start the servers (in the background).
        Returns a notice, '' when nothing is configured."""
        self.mcp.extra = tuple(extra)
        self._mcp_started = True
        if not self.mcp_enabled:
            return ""
        note = self.mcp.reload(self.workdir)
        return note if self.mcp.clients or self.mcp.config_errors else ""

    def shutdown_mcp(self) -> None:
        self.mcp.shutdown()

    def mcp_reload(self) -> str:
        """/mcp reload, and a workdir change: re-read the config for the current workdir."""
        if not self._mcp_started or not self.mcp_enabled:
            self.mcp.workdir = self.workdir
            return ""
        note = self.mcp.reload(self.workdir)
        self._mcp_waited = False
        self.rebuild_system_prompt()
        self.emit_status()
        return note if self.mcp.clients or self.mcp.config_errors else ""

    def set_mcp_enabled(self, on: bool) -> str:
        self.mcp_enabled = on
        session_mod.save_prefs(mcp=on)
        if on:
            note = self.mcp.reload(self.workdir) if self._mcp_started else ""
            self._mcp_waited = False
        else:
            self.mcp.shutdown()
            note = ""
        self.rebuild_system_prompt()
        self.emit_status()
        return f"MCP tools: {'on' if on else 'off'}" + (f"\n{note}" if note else "")

    def save_mcp_disabled(self) -> None:
        session_mod.save_prefs(mcp_disabled=sorted(self.mcp.disabled))

    def _on_mcp_change(self) -> None:
        # A server thread: status only (cheap, no network); the prompt waits for the turn.
        self.emit_status()

    def _mcp_before_turn(self) -> None:
        """At the start of send(): give servers that are still starting a moment
        on the first turn, then pick up tool-list changes."""
        if not self.mcp_enabled:
            return
        if not self._mcp_waited:
            self._mcp_waited = True
            waiting = [c.name for c in self.mcp.clients.values() if c.state == "connecting"]
            if waiting:
                self.event_queue.put(ChatEvent(
                    "system", f"Waiting for MCP server{'s' if len(waiting) > 1 else ''} "
                              f"{', '.join(waiting)} to start…"))
                late = self.mcp.wait_ready(MCP_START_WAIT_S)
                if late:
                    self.event_queue.put(ChatEvent(
                        "system", f"MCP {', '.join(late)} still starting; its tools join "
                                  "from the next message."))
        if self.mcp.consume_dirty():
            self.rebuild_system_prompt()

    # ── tools ────────────────────────────────────────────────────────────────

    def _mcp_read_only_role(self) -> bool:
        """Roles that don't change things get only read-only MCP tools
        (readOnlyHint), which keeps design/chat/plan investigation read-only."""
        return self.mode != "coding" and not self._plan_executing()

    def _mcp_tools(self) -> list[dict]:
        if not self.mcp_enabled:
            return []
        return self.mcp.tool_schemas(read_only_only=self._mcp_read_only_role())

    def _is_mcp_tool(self, name: str) -> bool:
        return self.mcp_enabled and (self.mcp.is_mcp(name) or name in RESOURCE_TOOLS)

    def _mcp_confirm_prompt(self, name: str, args: dict) -> str | None:
        if (name in RESOURCE_TOOLS or not self.mcp.is_mcp(name) or not self.mcp_confirm
                or name in self.mcp.allowed):
            return None
        mode, read_only = self.mcp.policy(name)
        if mode == "never" or (mode == "writes" and read_only):
            return None
        server = self.mcp.server_of(name)
        tool = name.split("__", 2)[-1]
        shown = net_mod.CONTROL_CHARS.sub("", json.dumps(args, ensure_ascii=False))
        if len(shown) > 600:
            shown = shown[:600] + f"… (+{len(shown) - 600} chars)"
        return (f"Run this MCP tool? Reply 'y' to allow, 'a' to always allow {name} this "
                f"session, anything else to decline.\n  {server} › {net_mod.safe_line(tool, 100)}"
                f"\n  args: {shown}")

    def _mcp_call(self, name: str, args: dict) -> str:
        if name == "mcp_list_resources":
            return self.mcp.list_resources(str(args.get("server") or "") or None)
        if name == "mcp_read_resource":
            return self.mcp.read_resource(str(args.get("server") or ""),
                                          str(args.get("uri") or ""), self._cancel)
        if self._mcp_read_only_role() and self.mcp.is_mcp(name) and not self.mcp.policy(name)[1]:
            return (f"ERROR: {name} can change things, so it is only offered in coding mode; "
                    f"this is {self.mode} mode.")
        if (q := self._mcp_confirm_prompt(name, args)):
            answer = self._ask_user(q).strip().lower()
            if answer in ("a", "always"):
                self.mcp.allowed.add(name)
            elif answer not in ("y", "yes"):
                return _DECLINED
        return self.mcp.call(name, args, self._cancel)

    def _mcp_keeps_green(self, name: str) -> bool:
        """A read-only MCP tool can't have changed the files a green run tested."""
        if name in RESOURCE_TOOLS:
            return True
        return self.mcp.is_mcp(name) and self.mcp.policy(name)[1]

    # ── prompt and status ────────────────────────────────────────────────────

    def _mcp_prompt_section(self, names: set[str]) -> str:
        if not self.mcp_enabled:
            return ""
        offered = {self.mcp.server_of(n) for n in names if self.mcp.is_mcp(n)}
        if not offered and not (names & set(RESOURCE_TOOLS)):
            return ""
        lines = ["## MCP servers", "",
                 "Tools named `mcp__<server>__<tool>` come from external MCP servers the user "
                 "connected. Use them when they fit the task better than the built-in tools."]
        if names & set(RESOURCE_TOOLS):
            lines.append("Servers that offer resources (documents, records) can be browsed with "
                         "mcp_list_resources and read with mcp_read_resource.")
        for server, text in self.mcp.instructions():
            if server in offered:
                lines += ["", f"### {server}", "", text]
        return "\n".join(lines)

    def _mcp_status_fields(self) -> dict:
        ready, total, attention = self.mcp.counts()
        return {"mcp_enabled": self.mcp_enabled, "mcp_confirm": self.mcp_confirm,
                "mcp_ready": ready, "mcp_total": total, "mcp_attention": attention,
                "mcp": [{"name": r["name"], "state": r["state"], "tools": r["tools"],
                         "error": r["error"]} for r in self.mcp.status_rows()]}

    def mcp_view(self) -> dict:
        """GET /api/mcp and /mcp: every server with its tools; never touches the network."""
        from .mcp.config import PROJECT_FILE, global_path
        from .mcp.manager import tilde
        rows = self.mcp.status_rows()
        for r in rows:
            r["tool_list"] = self.mcp.tool_rows(r["name"])
            r["log"] = self.mcp.clients[r["name"]].log_tail(20) if r["state"] == "error" else []
        return {"enabled": self.mcp_enabled, "confirm": self.mcp_confirm, "servers": rows,
                "errors": [clean(e, 500) for e in self.mcp.config_errors],
                "files": {"global": tilde(str(global_path())),
                          "project": tilde(str(self.workdir / PROJECT_FILE))},
                "schema_tokens": self._mcp_schema_tokens(),
                "context_limit": self.context_limit}

    def _mcp_schema_tokens(self) -> int:
        from .compaction import text_tokens
        tools = self._mcp_tools()
        return text_tokens(json.dumps(tools)) if tools else 0
