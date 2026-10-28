"""The tool registry: what a tool is (Tool), which ones a role is offered
(offered / universe), and where they come from.

Every built-in tool is one file in harness/tools/builtin/ that ends with
`TOOL = Tool(...)`; load_builtins() imports them all, so dropping a file in is
all it takes to register a tool.  Custom tools (custom.py) use the same class.

A Tool carries everything the harness needs to know about it: its schema (and
schema variants), its executor, the harness state dispatch injects, the
example in the tool reference, when it is offered, and how it is treated
(index invalidation, diffs, the TDD green state, the /tools picker group).
The tables that used to be keyed by tool name are derived from these fields.
"""
from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace

# Roles a tool can be offered in: the five modes, plus an approved plan while
# it executes (it runs with the coding tools and the step-tracking tools).
ROLES = ("design", "chat", "plan", "coding", "momo", "plan-exec")
# /tools discover: which tools are listed in the prompt and declared only once
# loaded.  "off" (the default): none, every tool is declared; "extensions":
# custom, workspace and MCP tools (every built-in stays declared); "all": also
# the built-ins marked discoverable.
DISCOVER_MODES = ("off", "extensions", "all")
DEFAULT_DISCOVER = "off"


def discover_mode(value) -> str:
    """A saved or given /tools discover setting as a mode: true/false were saved
    before the modes, when on listed the discoverable built-ins too."""
    if isinstance(value, bool):
        return "all" if value else "off"
    return value if value in DISCOVER_MODES else DEFAULT_DISCOVER
ALL_ROLES = frozenset(ROLES)
WRITE_ROLES = frozenset({"coding", "momo", "plan-exec"})


def tool_schema(name: str, description: str, properties: dict, required) -> dict:
    """A function-calling schema, as the model receives it."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            # "required" only when something is: an empty list is tokens for nothing.
            "parameters": {"type": "object", "properties": properties,
                           **({"required": list(required)} if required else {})},
        },
    }


@dataclass(frozen=True)
class ToolContext:
    """What decides which tools a role is offered, and in which variant."""
    role: str = "coding"
    run_mode: str = "new"
    edit_features: frozenset = frozenset()
    index_on: bool = False
    net_on: bool = False
    tdd: bool = False
    search_sources: object = None    # search.SourceRegistry, for web_search's source list
    # /tools discover (DISCOVER_MODES): on-demand tools are offered only once
    # loaded (find_tools).
    discover: str = "off"
    loaded: frozenset = frozenset()


@dataclass(frozen=True)
class ToolRunContext:
    """Harness services a tool can ask for with inject=("ctx",): its own
    small interface instead of the Harness object."""
    workdir: object
    cancel: object = None
    ask_user: Callable[[str], str] | None = None     # blocks until the user answers
    note: Callable[[str], None] | None = None        # a system line in the chat


@dataclass(eq=False)
class Tool:
    schema: dict                     # tool_schema(...): what the model sees
    run: Callable | None = None      # fn(**args, workdir=..., **injected) -> str
    order: int = 9000                # place in every tool list (builtins are spaced by 10)
    group: str = "custom"            # /tools picker section
    modes: frozenset = WRITE_ROLES   # roles that offer it
    inject: tuple = ()               # harness state passed as keyword arguments (dispatcher.STATE)
    example: dict | None = None      # tool-reference example (None: built from required params)
    # Schema variants, picked by select(ctx) -> "" (the main schema) or a key;
    # examples holds a variant's own example.
    variants: dict = field(default_factory=dict)
    examples: dict = field(default_factory=dict)
    select: Callable[[ToolContext], str] | None = None
    adapt: Callable[[ToolContext, dict], dict] | None = None   # last touch to the schema
    # When it is offered.
    needs_index: bool = False        # only while the code index is on
    hidden_by_index: bool = False    # not while the code index is on (an index tool replaces it)
    needs_net: bool = False          # only while internet access is on
    needs_run_mode: str | None = None    # only in this /run-mode
    needs_edit_feature: str | None = None
    requires: Callable[[], bool] | None = None    # e.g. tree-sitter installed
    # With /tools discover: listed in the prompt, offered once loaded.  None is
    # the default by kind: a built-in only with discover "all" when True; a
    # custom or workspace tool on demand unless False (see on_demand).
    discoverable: bool | None = None
    needs_discover: bool = False     # only while /tools discover is on (find_tools)
    # How it is treated.
    writes: tuple = ()               # path arguments it writes (index invalidation, .momo/)
    written: Callable[[dict], list] | None = None  # the written paths, when not plain arguments
    body_arg: str | None = None      # argument carrying file text (placeholder / cut guards)
    guard: Callable[[dict], str | None] | None = None   # refuses a call up front: ERROR or None
    diff: bool = False               # its result is shown as a diff of what changed
    keeps_green: bool = False        # a TDD plan's passing state survives a call
    harness: bool = False            # answered by the harness loop, not dispatch
    locked: bool = False             # /tools can't turn it off
    confirm: bool = False            # the user approves each call
    windows_itself: Callable[[ToolContext], bool] | None = None   # exempt from /tool-result
    index_route: Callable | None = None   # (args, workdir, index, cancel) -> answer or None
    after: Callable | None = None         # (result, args, workdir, index) -> result
    suggests: tuple = ()             # tools its results may name: loaded when named (/tools discover)
    source: str = "builtin"          # or the custom tool file

    @property
    def name(self) -> str:
        return self.schema["function"]["name"]

    @property
    def parameters(self) -> dict:
        return self.schema["function"]["parameters"]

    def available(self) -> bool:
        return self.requires is None or self.requires()

    def on_demand(self, mode: str) -> bool:
        """Whether /tools discover mode lists this tool instead of declaring it."""
        if mode == "off" or self.needs_discover:
            return False
        if self.source == "builtin":
            return mode == "all" and self.discoverable is True
        return self.discoverable is not False

    def schema_for(self, ctx: ToolContext) -> dict | None:
        """The schema this tool shows in ctx, or None when it is not offered."""
        if ctx.role not in self.modes or not self.available():
            return None
        if self.needs_index and not ctx.index_on or self.hidden_by_index and ctx.index_on:
            return None
        if self.needs_net and not ctx.net_on:
            return None
        if self.needs_run_mode is not None and ctx.run_mode != self.needs_run_mode:
            return None
        if self.needs_edit_feature is not None and self.needs_edit_feature not in ctx.edit_features:
            return None
        if self.needs_discover and ctx.discover == "off":
            return None
        if self.on_demand(ctx.discover) and self.name not in ctx.loaded:
            return None
        key = self.select(ctx) if self.select else ""
        schema = self.variants[key] if key else self.schema
        return self.adapt(ctx, schema) if self.adapt else schema


_BUILTIN: dict[str, Tool] = {}
_CUSTOM: dict[str, Tool] = {}        # ~/.momo-harness/tools/ (custom.py)
_WORKSPACE: dict[str, Tool] = {}     # <workdir>/.momo/tools/, approved (workspace.py)
# id(schema) -> example, for every schema and variant a tool registered: the
# tool reference shows the example of the variant actually offered.
_EXAMPLES: dict[int, dict] = {}


def _register(tool: Tool, table: dict[str, Tool]) -> None:
    _EXAMPLES[id(tool.schema)] = tool.example
    for key, schema in tool.variants.items():
        _EXAMPLES[id(schema)] = tool.examples.get(key, tool.example)
    table[tool.name] = tool


def module_tools(module) -> list[Tool]:
    """The tools a tool file declares: TOOL, or a TOOLS list."""
    found = getattr(module, "TOOLS", None)
    if found is None:
        found = [module.TOOL] if hasattr(module, "TOOL") else []
    return [t for t in found if isinstance(t, Tool)]


def load_builtins() -> None:
    """Import every file in harness/tools/builtin/ and register its tools."""
    from . import builtin
    for info in sorted(pkgutil.iter_modules(builtin.__path__), key=lambda i: i.name):
        module = importlib.import_module(f"{builtin.__name__}.{info.name}")
        for tool in module_tools(module):
            if tool.name in _BUILTIN:
                raise RuntimeError(f"tool {tool.name!r} is defined twice ({info.name}.py)")
            _register(tool, _BUILTIN)


def _replace(table: dict[str, Tool], tools: list[Tool]) -> None:
    for t in table.values():
        _EXAMPLES.pop(id(t.schema), None)
    table.clear()
    for t in tools:
        _register(t, table)


def set_custom(tools: list[Tool]) -> None:
    """Replace the custom tools (custom.load_custom_tools)."""
    _replace(_CUSTOM, tools)


def set_workspace(tools: list[Tool]) -> None:
    """Replace the workspace tools (workspace.load_workspace_tools)."""
    _replace(_WORKSPACE, tools)


def builtins() -> list[Tool]:
    return sorted(_BUILTIN.values(), key=lambda t: t.order)


_KIND_RANK = {"builtin": 0, "custom": 1, "workspace": 2}


def all_tools() -> list[Tool]:
    """Every registered tool, built-in first, then custom, then workspace
    tools, each in list order."""
    return sorted([*_BUILTIN.values(), *_CUSTOM.values(), *_WORKSPACE.values()],
                  key=lambda t: (_KIND_RANK[kind_of(t.name)], t.order))


def get(name: str) -> Tool | None:
    return _BUILTIN.get(name) or _CUSTOM.get(name) or _WORKSPACE.get(name)


def kind_of(name: str) -> str | None:
    """"builtin" | "custom" | "workspace", or None for an unknown name."""
    for kind, table in (("builtin", _BUILTIN), ("custom", _CUSTOM), ("workspace", _WORKSPACE)):
        if name in table:
            return kind
    return None


def is_builtin(name: str) -> bool:
    return name in _BUILTIN


def offered(ctx: ToolContext) -> list[dict]:
    """The schemas ctx.role is offered, in order."""
    return [s for t in all_tools() if (s := t.schema_for(ctx)) is not None]


def catalog(ctx: ToolContext) -> list[dict]:
    """The discoverable tools ctx.role could load (/tools discover), loaded or
    not, in order: the schemas they have once loaded."""
    full = replace(ctx, discover="off")
    return [s for t in all_tools()
            if t.on_demand(ctx.discover) and (s := t.schema_for(full)) is not None]


def universe(ctx: ToolContext) -> list[dict]:
    """Every tool ctx.role can be offered, whatever the internet and code index
    toggles (the /tools pickers): those tools in their plain schema, the others
    as with both toggles off.  Discoverable tools count as loaded; find_tools
    is listed only while /tools discover is on."""
    off = replace(ctx, index_on=False, net_on=False, discover="off")
    out = []
    for t in all_tools():
        if t.needs_discover:
            if ctx.discover == "off":
                continue
            if (s := t.schema_for(replace(off, discover=ctx.discover))) is not None:
                out.append(s)
        elif t.needs_index or t.needs_net:
            if ctx.role in t.modes and t.available():
                out.append(t.schema)
        elif (s := t.schema_for(off)) is not None:
            out.append(s)
    return out


def example_for(schema: dict) -> dict | None:
    """The curated example for a schema the registry built; else the tool's
    own (an adapted copy), else None."""
    if id(schema) in _EXAMPLES:
        return _EXAMPLES[id(schema)]
    tool = get(schema["function"]["name"])
    return tool.example if tool is not None else None


def names(pred: Callable[[Tool], bool]) -> frozenset[str]:
    """Names of the built-in tools pred accepts (the derived tool tables)."""
    return frozenset(t.name for t in _BUILTIN.values() if pred(t))
