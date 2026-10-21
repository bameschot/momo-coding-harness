"""Custom tools: Python files in ~/.momo-harness/tools/, each declaring a
`TOOL = Tool(...)` like the built-in ones (see builtin/ for examples, and
docs/examples/tools/word_count.py for a complete one).

A tool file is code that runs inside momo, so it is only ever loaded from the
user's own folder — never from the workdir, where a cloned repository could
bring one along — and only when that folder and the file are the user's and
not writable by others (paths.untrusted_reason, the index pickle's rule).

A file that fails to load is reported, never fatal: momo starts without it.
Files starting with "_" are helpers for the others and are not loaded as tools.
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import momo_home, untrusted_reason
from . import dispatcher, registry

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
# Fields that only make sense for a built-in tool: the harness loop answers
# harness tools itself, locked tools keep plan mode working, diffs and index
# routes rely on harness internals.
_BUILTIN_ONLY = ("harness", "locked", "diff", "index_route")
_MODULE_PREFIX = "momo_custom_tools."


def tools_dir() -> Path:
    """Where custom tool files live; read per call (tests point HOME elsewhere)."""
    return momo_home() / "tools"


@dataclass
class LoadReport:
    folder: Path
    loaded: list[str] = field(default_factory=list)            # tool names
    errors: list[tuple[str, str]] = field(default_factory=list)  # (file, reason)
    details: dict[str, str] = field(default_factory=dict)      # file -> traceback

    def summary(self) -> str:
        """A notice for the chat ('' when there is nothing to say)."""
        lines = []
        if self.loaded:
            lines.append(f"Custom tools: {', '.join(self.loaded)} (from {self.folder})")
        for file, why in self.errors:
            lines.append(f"Custom tool {file} not loaded: {why}")
        if self.details:
            lines.append("/tools errors shows the tracebacks.")
        return "\n".join(lines)


def _folder_problem(folder: Path) -> str:
    try:
        st = os.stat(folder)
    except OSError as e:
        return f"cannot read {folder}: {e.strerror or e}"
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return f"{folder} is not owned by you"
    if st.st_mode & 0o022:
        return f"{folder} is writable by other users (chmod 700 it)"
    return ""


def _import(path: Path):
    name = _MODULE_PREFIX + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module           # dataclasses and pickling look it up
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True       # no __pycache__ in the user's folder
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.dont_write_bytecode = dont_write
    return module


def check(tool: registry.Tool, taken: set[str]) -> str:
    """Why a custom tool can't be registered ('' when it can)."""
    name = tool.name
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return f"name {name!r} must be 2-64 characters of a-z, 0-9 and _, starting with a letter"
    if name.startswith("mcp_"):
        return f"name {name!r}: the mcp_ prefix is reserved for MCP servers"
    if registry.is_builtin(name):
        return f"name {name!r} is a built-in tool"
    if name in taken:
        return f"name {name!r} is defined twice"
    for f in _BUILTIN_ONLY:
        if getattr(tool, f):
            return f"{f} is only for built-in tools"
    if not callable(tool.run):
        return "run is missing: a custom tool needs a function to call"
    params = inspect.signature(tool.run).parameters
    if "workdir" not in params and not any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return "its run function must take a workdir keyword argument"
    unknown = [k for k in tool.inject if k not in dispatcher.STATE]
    if unknown:
        return f"inject names unknown state {unknown} (available: {', '.join(dispatcher.STATE)})"
    roles = set(tool.modes) - registry.ALL_ROLES
    if roles:
        return f"modes {sorted(roles)} are not roles (roles: {', '.join(registry.ROLES)})"
    fn = tool.schema.get("function", {})
    if not isinstance(fn.get("description"), str) or not fn["description"].strip():
        return "the schema needs a description: it is how the model learns what the tool is for"
    return ""


def load_custom_tools(folder: Path | None = None) -> LoadReport:
    """Load every tool file in folder (default tools_dir()) and make them the
    registry's custom tools, replacing the ones loaded before."""
    folder = tools_dir() if folder is None else folder
    report = LoadReport(folder)
    accepted: list[registry.Tool] = []
    if not folder.is_dir():
        registry.set_custom([])
        return report
    if (why := _folder_problem(folder)):
        report.errors.append((str(folder), why))
        registry.set_custom([])
        return report
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        try:
            why = untrusted_reason(path, os.lstat(path))
        except OSError as e:
            why = f"cannot read it: {e.strerror or e}"
        if why:
            report.errors.append((path.name, why))
            continue
        try:
            module = _import(path)
        except KeyboardInterrupt:
            raise
        except BaseException as e:          # SystemExit too: a tool file must never stop momo
            report.errors.append((path.name, f"{type(e).__name__}: {e}"))
            report.details[path.name] = traceback.format_exc()
            continue
        tools = registry.module_tools(module)
        if not tools:
            report.errors.append((path.name, "it defines no TOOL = Tool(...)"))
            continue
        taken = {t.name for t in accepted}
        for tool in tools:
            if (why := check(tool, taken)):
                report.errors.append((path.name, why))
                continue
            tool.source = str(path)
            accepted.append(tool)
            taken.add(tool.name)
            report.loaded.append(tool.name)
    registry.set_custom(accepted)
    return report
