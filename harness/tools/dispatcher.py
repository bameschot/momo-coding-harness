"""Running a tool call: argument checks with errors a model can act on,
coercion of stringly-typed numbers, injection of harness state, the content
guards for writes, and the hooks a tool declares (index routing, after)."""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from .. import editing, momo_dir, net
from .. import run_store as run_store_mod
from . import registry

# Harness state dispatch injects rather than taking it from model arguments: a
# tool names the keys it needs in Tool.inject.  net_access is deliberately not
# in the fetch_url schema: the model must not be able to ask for "local" and
# unblock the private network for itself.
STATE = ("net_access", "net_max_bytes", "net_max_chars", "index", "cancel", "read_budget",
         "run_mode", "run_store", "run_output_limit", "search_sources", "edit_features",
         "run_exit", "ctx")

_DEFAULT_STATE = {"net_access": "off", "net_max_bytes": 0, "net_max_chars": 0, "index": None,
                  "cancel": None, "read_budget": None, "run_mode": "classic", "run_store": None,
                  "run_output_limit": run_store_mod.DEFAULT_LIMIT, "search_sources": None,
                  "edit_features": editing.EDIT_FEATURES, "run_exit": None, "ctx": None}

_INT_RE = re.compile(r"\s*[-+]?\d+\s*")
_PLACEHOLDER_RE = re.compile(r"^\s*\[written to [^\]]*\]\s*$")
# Notes the harness splices into tool results (compaction trim, size cutoff).
# Content carrying one was rebuilt from a shortened result: writing it would
# silently replace the missing middle of the file with the note.
CUT_MARKER_RE = re.compile(r"\[… [\d,]+ chars removed by context compaction …\]"
                           r"|\.\.\. \(truncated after \d+ chars of \d+ — ")


def is_placeholder(text) -> bool:
    return isinstance(text, str) and bool(_PLACEHOLDER_RE.match(text))


def has_cut_marker(text) -> bool:
    return isinstance(text, str) and bool(CUT_MARKER_RE.search(text))


def required_args(name: str) -> list[str]:
    tool = registry.get(name)
    return list(tool.parameters["required"]) if tool else []


def known_args(name: str) -> set[str]:
    tool = registry.get(name)
    return set(tool.parameters["properties"]) if tool else set()


def _coerce_args(tool: "registry.Tool", args: dict) -> dict:
    """Small models often send numbers and booleans as strings ("40", "true"):
    convert those the schema types as integer / boolean, leave the rest alone."""
    types = {k: v["type"] for k, v in tool.parameters["properties"].items()
             if isinstance(v.get("type"), str)}
    out = dict(args)
    for k, v in args.items():
        t = types.get(k)
        if t == "integer":
            if isinstance(v, str) and _INT_RE.fullmatch(v):
                out[k] = int(v)
            elif isinstance(v, float) and v.is_integer():
                out[k] = int(v)
        elif t == "boolean" and isinstance(v, str) and v.strip().lower() in ("true", "false"):
            out[k] = v.strip().lower() == "true"
    return out


def written_paths(tool: "registry.Tool", args: dict) -> list[str]:
    paths = tool.written(args) if tool.written else [args.get(k) for k in tool.writes]
    return [p for p in paths if isinstance(p, str)]


def _ensure_momo_dir(tool: "registry.Tool", args: dict, workdir: Path) -> None:
    """A write tool aimed at .momo/ (write_file into the scratchpad in coding
    mode) gets the folder and its .gitignore first, or git would see the file."""
    if any(momo_dir.touches(p, workdir) for p in written_paths(tool, args)):
        momo_dir.ensure(workdir)


def dispatch(name: str, args: dict, workdir: Path, net_access: str = "off",
             net_max_bytes: int = net.DEFAULT_MAX_BYTES,
             net_max_chars: int = net.DEFAULT_MAX_CHARS,
             index=None, cancel=None,
             index_route: bool = True, read_budget: int | None = None,
             run_mode: str = "classic", run_store: "run_store_mod.RunStore | None" = None,
             run_output_limit: int = run_store_mod.DEFAULT_LIMIT,
             search_sources=None,
             edit_features=editing.EDIT_FEATURES, run_exit: dict | None = None,
             ctx: "registry.ToolRunContext | None" = None) -> str:
    """read_budget: characters one read_file may return (None = no limit).
    run_mode "new" with a run_store saves run_command output to a log (/run-mode).
    search_sources: the session's web_search sources (None = the default set).
    edit_features: what the edit tools add to a plain edit (editing.EDIT_FEATURES).
    run_exit: run_command puts the command's exit code here as "code" (None when
    it was stopped or did not start), for a caller that must not parse the text.
    ctx: the harness services a tool asks for with inject=("ctx",)."""
    state = {"net_access": net_access, "net_max_bytes": net_max_bytes,
             "net_max_chars": net_max_chars, "index": index, "cancel": cancel,
             "read_budget": read_budget, "run_mode": run_mode, "run_store": run_store,
             "run_output_limit": run_output_limit, "search_sources": search_sources,
             "edit_features": edit_features, "run_exit": run_exit,
             "ctx": ctx if ctx is not None else registry.ToolRunContext(workdir, cancel)}
    tool = _runnable(name)
    if tool is not None and index is not None and index_route and tool.index_route:
        routed = tool.index_route(args, workdir, index, cancel)
        if routed is not None:
            return routed
    if tool is not None:
        _ensure_momo_dir(tool, args, workdir)
    result = run(name, args, workdir, state)
    if tool is None:
        return result
    if tool.after is not None:
        result = tool.after(result, args, workdir, index)
    if index is not None and tool.writes:
        index.invalidate(written_paths(tool, args))
    return result


def _runnable(name: str) -> "registry.Tool | None":
    tool = registry.get(name)
    if tool is None or tool.harness or tool.run is None or not tool.available():
        return None
    return tool


def run(name: str, args: dict, workdir: Path, state: dict | None = None) -> str:
    """The call itself, without dispatch's hooks: what an index route falls
    back to for an answer from the disk.  state: dispatch.STATE values."""
    tool = _runnable(name)
    if tool is None:
        return f"ERROR: unknown tool '{name}'"
    state = {**_DEFAULT_STATE, **(state or {})}

    # Auto-route a common small-model confusion: write_file called with edit-style
    # old_string/new_string and no content clearly means an edit — run edit_file so
    # the model's intent succeeds instead of failing on an unexpected argument.
    if (name == "write_file" and "old_string" in args and "new_string" in args
            and "content" not in args and "path" in args):
        routed = {k: args[k] for k in ("path", "old_string", "new_string", "replace_all")
                  if k in args}
        # dispatch() invalidates the index for this path under write_file's name.
        return "(note: routed write_file to edit_file) " + run("edit_file", routed, workdir, state)

    # Sessions saved before the harness stopped eliding write content hold
    # "[written to <path>]" in place of past file bodies, and a model reading that
    # history copies it.  Never let the placeholder reach the disk.
    if tool.guard is not None and (refused := tool.guard(args)):
        return refused
    field = tool.body_arg
    body = args.get(field) if field else None
    if is_placeholder(body):
        return (f"ERROR: {field} is a history placeholder, not file content — nothing was "
                f"written. {args.get('path', 'The file')} still holds its previous contents. "
                f"Pass the complete text in '{field}'.")
    if isinstance(body, str) and (m := CUT_MARKER_RE.search(body)):
        return (f"ERROR: {field} contains a harness note ({m.group(0).strip()[:60]}…), "
                f"so it was copied from a shortened tool result and is missing text — "
                f"nothing was written. {args.get('path', 'The file')} still holds its previous "
                f"contents. Re-read the part you need with read_file (start_line/end_line), "
                f"or use edit_file to change only the lines that differ.")

    required = required_args(name)
    missing = [r for r in required if r not in args]
    if missing:
        return (
            f"ERROR: {name} called without required argument(s): {', '.join(missing)}. "
            f"Required: {', '.join(required)}. Retry the call with all required arguments."
        )

    # Reject arguments the tool does not declare, with a clear message and a
    # "did you mean" hint — rather than letting Python raise a TypeError that
    # leaks the internal function name (e.g. write_file given old_string).
    known = known_args(name)
    unexpected = [a for a in args if a not in known]
    if unexpected:
        hint = ""
        if {"old_string", "new_string"} & set(unexpected):
            hint = " To change part of an existing file, use edit_file (old_string/new_string)."
        elif "content" in unexpected and name in ("edit_file",):
            hint = " To create or overwrite a whole file, use write_file (path/content)."
        return (
            f"ERROR: {name} does not accept argument(s): {', '.join(unexpected)}. "
            f"Valid arguments: {', '.join(sorted(known))}.{hint}"
        )

    args = _coerce_args(tool, args)
    extra = {k: state.get(k) for k in tool.inject}
    # Check the call against the signature first, so only an argument mismatch
    # reads as "bad arguments" — a TypeError raised inside the tool is a bug and
    # propagates (the harness reports it as the tool failing).
    try:
        inspect.signature(tool.run).bind(**args, workdir=workdir, **extra)
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    return tool.run(**args, workdir=workdir, **extra)
