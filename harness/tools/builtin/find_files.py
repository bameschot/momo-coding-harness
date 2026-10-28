"""find_files: files matching a glob, noise folders skipped; answered from the
code index while it is on (index_route)."""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from ... import code_index
from ...paths import SKIP_DIRS, safe_path, walk_files
from .. import dispatcher
from ..common import MAX_FIND_RESULTS
from ..registry import ALL_ROLES, Tool, tool_schema


def find_files(pattern: str, directory: str = ".", *, workdir: Path) -> str:
    root = safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    if not root.is_dir():
        return f"ERROR: not a directory: {directory}"
    base = workdir.resolve()
    # A bare filename pattern ("*.py" or "**/*.py") is matched against every
    # file name under root.  walk_files prunes SKIP_DIRS as it goes; a "**"
    # glob would walk all of .venv / node_modules first and filter afterwards.
    bare = pattern[3:] if pattern.startswith("**/") else pattern
    if bare and "/" not in bare and "**" not in bare:
        gen = (p for p in walk_files(root) if fnmatch.fnmatchcase(p.name, bare))
    else:
        try:
            gen = root.glob(pattern)
        except (ValueError, NotImplementedError) as e:
            return f"ERROR: invalid pattern: {e} (use a pattern relative to directory)"
    matches = []
    for p in gen:
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        matches.append(str(p.relative_to(base)))
    if not matches:
        return "(no matches)"
    total = len(matches)
    matches = sorted(matches)
    if total > MAX_FIND_RESULTS:
        return "\n".join(matches[:MAX_FIND_RESULTS]) + f"\n... (first {MAX_FIND_RESULTS} of {total} files — use a more specific pattern or directory)"
    return "\n".join(matches)


# ── index-first search (/index-route, on by default) ─────────────────────────
# With the code index on, the 9B still reached for grep_files / find_files out
# of habit.  A file-name glob is answered from the index instead, with a first
# line that says so and names the index tool to call next time.

def route_to_index(args: dict, workdir: Path, index, cancel) -> str | None:
    """The index's answer to a find_files call, or None to run the real tool
    (bad arguments, a pattern with a directory part)."""
    if not set(args) <= dispatcher.known_args("find_files") or not isinstance(args.get("pattern"), str):
        return None
    root = safe_path(str(args.get("directory") or "."), workdir)
    if isinstance(root, str):
        return None
    rel = index.rel(root)
    if rel is None:
        return None
    path = None if rel == "." else rel
    pattern = args["pattern"]
    glob = pattern[3:] if pattern.startswith("**/") else pattern
    if "/" in glob or "**" in glob:
        return None
    out = code_index.index_find_files(glob, path, index=index, cancel=cancel)
    if out is None:
        disk = dispatcher.run("find_files", args, workdir, {"index": index, "cancel": cancel})
        return "(no file in the code index matches; searched the disk)\n" + disk
    if out.startswith("ERROR"):
        return out
    return (f"(find_files is not needed while the code index is on — this is "
            f"index_search(query={json.dumps(glob)}, kind=\"file\")'s answer; call that directly "
            f"next time, and index_file to describe one)\n" + out)


# While the index is on, find_files is the fallback, and its description says
# so first — a small model picks tools by their description.
_FALLBACK_NOTE = ("Do NOT use this for source or config files while the code index is on — "
                  "call index_search(query, kind=\"file\") or index_map. Only for files the index "
                  "does not cover: images, binaries, git-ignored paths. ")


def _fallback_note(ctx, schema: dict) -> dict:
    if not ctx.index_on:
        return schema
    fn = schema["function"]
    return {**schema, "function": {**fn, "description": _FALLBACK_NOTE + fn["description"]}}


TOOL = Tool(
    tool_schema("find_files",
        "Find files matching a glob pattern under a directory. A pattern without '/' ('*.py') "
        "searches every subdirectory; 'src/*.py' only that one. Noise directories (.git, .venv, "
        f"node_modules, build, …) are skipped. Results are capped at {MAX_FIND_RESULTS} files.",
        {"pattern": {"type": "string", "description": "Glob pattern, e.g. '*.py' (recursive) or 'src/*.py' (one level)"},
         "directory": {"type": "string", "description": "Directory to search (default: .)"}},
        ["pattern"]),
    run=find_files, order=120, group="files", modes=ALL_ROLES, keeps_green=True,
    example={"pattern": "*.py"}, adapt=_fallback_note, index_route=route_to_index)
