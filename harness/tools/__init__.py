"""The model's tools.

Each built-in tool is one file in builtin/ declaring everything the harness
needs to know about it (registry.Tool); custom tools are the same kind of file
in ~/.momo-harness/tools/ (custom.py).  This package puts them together:

  registry.py   Tool and ToolContext, which tools a role is offered (offered,
                universe), and the tables derived from the tools' fields
  dispatcher.py running a call: argument checks, coercion, injected state, guards
  reference.py  the tool reference in the system prompt
  common.py     helpers several tool files share; shell.py what the run tools share
  custom.py     loading custom tool files

The names below are the package's public API (harness.py, commands.py,
prompts.py, tdd.py and the tests import them from here).
"""
from __future__ import annotations

from ..editing import EDIT_FEATURES
from . import registry
from .common import edit_paths, read_text
from .dispatcher import dispatch
from .reference import TOOL_REF_STYLES, render_tool_reference
from .registry import ALL_ROLES, ROLES, WRITE_ROLES, Tool, ToolContext, ToolRunContext, tool_schema
from .shell import (MAX_COMMAND_TIMEOUT, command_env, exec_command, output_tail,
                    split_view_pipe)

registry.load_builtins()

RUN_MODES = ("new", "classic")


def tool_context(role: str, **kw) -> ToolContext:
    return ToolContext(role=role, **kw)


def offered(role: str, **kw) -> list[dict]:
    """The schemas a role is offered with these settings (ToolContext fields)."""
    return registry.offered(ToolContext(role=role, **kw))


# ── derived tables ───────────────────────────────────────────────────────────
# Built from the tools' own fields, so a new tool file is in them by declaring
# the field, never by editing a list here.

INDEX_TOOL_NAMES = registry.names(lambda t: t.needs_index and t.available())
INDEX_REPLACES = registry.names(lambda t: t.hidden_by_index)
NET_TOOL_NAMES = registry.names(lambda t: t.needs_net)
RUN_STORE_TOOLS = registry.names(lambda t: "run_store" in t.inject)
DIFF_TOOLS = registry.names(lambda t: t.diff)
KEEPS_GREEN = registry.names(lambda t: t.keeps_green)
LOCKED_TOOLS = registry.names(lambda t: t.locked)


# ── the tool lists by role ───────────────────────────────────────────────────
# Each role's built-in tools as the old per-mode lists held them: run_command
# and edit_file in their classic variants, every edit tool, no index or net
# tool.  The harness itself asks registry.offered() per turn; these serve the
# evals, the tests and prompts' list of modes.

def _base(role: str, tdd: bool = False) -> list[dict]:
    ctx = ToolContext(role=role, run_mode="classic", edit_features=frozenset({"symbol", "batch"}),
                      tdd=tdd)
    return [s for t in registry.builtins() if (s := t.schema_for(ctx)) is not None]


def _group(group: str) -> list[dict]:
    return [t.schema for t in registry.builtins() if t.group == group and t.available()]


ALL_TOOLS = _base("coding")
DESIGN_TOOLS = _base("design")
CHAT_TOOLS = _base("chat")
PLAN_INVESTIGATE_TOOLS = _base("plan")
PLAN_EXECUTE_TOOLS = _base("plan-exec")
PLAN_TDD_INVESTIGATE_TOOLS = _base("plan", tdd=True)
PLAN_TDD_EXECUTE_TOOLS = _base("plan-exec", tdd=True)
READ_ONLY_TOOLS = _group("files")
CODE_NAV_TOOLS = _group("code nav")
INDEX_TOOLS = _group("index")
NET_TOOLS = _group("net")
PLAN_TOOLS = [registry.get(n).schema for n in ("create_plan", "complete_step", "revise_plan")]

__all__ = [
    "ALL_ROLES", "WRITE_ROLES", "ALL_TOOLS", "CHAT_TOOLS", "CODE_NAV_TOOLS", "DESIGN_TOOLS", "DIFF_TOOLS", "EDIT_FEATURES",
    "INDEX_REPLACES", "INDEX_TOOLS", "INDEX_TOOL_NAMES", "KEEPS_GREEN", "LOCKED_TOOLS",
    "MAX_COMMAND_TIMEOUT", "NET_TOOLS", "NET_TOOL_NAMES", "PLAN_EXECUTE_TOOLS",
    "PLAN_INVESTIGATE_TOOLS", "PLAN_TDD_EXECUTE_TOOLS", "PLAN_TDD_INVESTIGATE_TOOLS",
    "PLAN_TOOLS", "READ_ONLY_TOOLS", "ROLES", "RUN_MODES", "RUN_STORE_TOOLS",
    "TOOL_REF_STYLES", "Tool", "ToolContext", "ToolRunContext", "command_env", "dispatch",
    "edit_paths", "exec_command", "offered", "output_tail", "read_text", "registry",
    "render_tool_reference", "split_view_pipe", "tool_context", "tool_schema",
]
