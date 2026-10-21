"""grep_files: regex search across a folder; plain text is answered from the
code index while it is on (index_route)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ... import code_index, code_nav
from ...paths import BINARY_SNIFF_BYTES, MAX_SCAN_FILE_BYTES, safe_path, split_lines, walk_files
from .. import dispatcher
from ..common import INDEX_GREP_TIP, MAX_GREP_RESULTS, MAX_SCAN_MB, capped, clip_hit, literal
from ..registry import ALL_ROLES, Tool, tool_schema


def grep_files(pattern: str, directory: str = ".", *, workdir: Path, cancel=None) -> str:
    root = safe_path(directory, workdir)
    if isinstance(root, str):
        return root
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"ERROR: invalid regex: {e}"
    if not root.exists():
        return f"ERROR: directory not found: {directory}"
    base = workdir.resolve()             # root is resolved, so it is always under this
    results, total = [], 0
    for fpath in ([root] if root.is_file() else walk_files(root)):
        if cancel is not None and cancel.is_set():
            return "ERROR: search interrupted by the user"
        # Skip oversized files: scanning them is slow and rarely useful.
        try:
            if fpath.stat().st_size > MAX_SCAN_FILE_BYTES:
                continue
            raw = fpath.read_bytes()
        except OSError:
            continue
        # Skip binaries: a NUL byte in the first chunk is a reliable, cheap
        # signal, and avoids polluting results with garbage decoded matches.
        if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
            continue
        text = raw.decode("utf-8", errors="replace")
        rel = fpath.relative_to(base)
        for i, line in enumerate(split_lines(text), 1):
            if m := rx.search(line):
                total += 1
                if total <= MAX_GREP_RESULTS:      # past the cap only the count is kept
                    results.append(f"{rel}:{i}: {clip_hit(line, m)}")
    # With the code index on, point at the indexed search: it is faster and names
    # the definition each hit sits in.
    tip = INDEX_GREP_TIP if code_nav.index_active() else ""
    return capped(results, total, "narrow the pattern or specify a directory") + tip


# ── index-first search (/index-route, on by default) ─────────────────────────
# With the code index on, the 9B still reached for grep_files out of habit.  A
# plain-text grep is answered from the index instead — the same hits, plus the
# definition each one sits in — with a first line that says so and names the
# index tool to call next time.  A regex, and anything the index has no answer
# for (filtered out, binary, not indexed), still goes to the disk.

def route_to_index(args: dict, workdir: Path, index, cancel) -> str | None:
    """The index's answer to a grep_files call, or None to run the real tool
    (bad arguments, a regex)."""
    if not set(args) <= dispatcher.known_args("grep_files") or not isinstance(args.get("pattern"), str):
        return None
    root = safe_path(str(args.get("directory") or "."), workdir)
    if isinstance(root, str):
        return None
    rel = index.rel(root)
    if rel is None:
        return None
    path = None if rel == "." else rel
    text = literal(args["pattern"])
    if text is None:
        return None
    out = code_index.index_text(text, path=path, workdir=workdir, index=index, cancel=cancel)
    if out.startswith("ERROR"):
        return out if cancel is not None and cancel.is_set() else None
    if out.startswith("(no matches"):
        disk = dispatcher.run("grep_files", args, workdir, {"index": index, "cancel": cancel})
        return ("(the code index has no match for this text; searched the files on disk "
                "directly)\n" + disk.replace(INDEX_GREP_TIP, ""))
    return (f"(grep_files is not needed for plain text while the code index is on — this is "
            f"index_text({json.dumps(text)})'s answer: case-insensitive, each hit with the "
            f"definition it is in. Call index_text directly next time)\n" + out)


def _index_note(result: str, args: dict, workdir: Path, index) -> str:
    """A grep ran on disk (a regex, or any grep with /index-route off): lead
    with the index tools when its hits are in indexed files (a trailing tip
    was being skipped)."""
    if index is None or result.startswith("ERROR"):
        return result
    hits = [ln.split(":", 1)[0] for ln in result.splitlines()[:50] if ":" in ln]
    body = result.replace(INDEX_GREP_TIP, "")
    if not index.has_file(*hits):
        return body
    return ("(the code index is on: index_text searches text, index_search finds definitions, "
            "index_callers finds uses — each in one call)\n" + body)


# While the index is on, grep_files is the fallback, and its description says
# so first — a small model picks tools by their description.
_FALLBACK_NOTE = ("Do NOT use this for plain text while the code index is on — call index_text "
                  "(each hit names the definition it is in). Only for a regex, or after index_text "
                  "found nothing. ")


def _fallback_note(ctx, schema: dict) -> dict:
    if not ctx.index_on:
        return schema
    fn = schema["function"]
    return {**schema, "function": {**fn, "description": _FALLBACK_NOTE + fn["description"]}}


TOOL = Tool(
    tool_schema("grep_files",
        "Multi-file recursive search. Search for a regex pattern across all files in a directory, "
        "returns file:line:content hits. Noise directories (.git, .venv, node_modules, __pycache__, "
        f"dist, build, and similar), binary files, and files larger than {MAX_SCAN_MB} MB are "
        f"skipped and will never appear in the results. Results are capped at {MAX_GREP_RESULTS} "
        "matches. Given a file instead of a directory, it searches that one file.",
        {"pattern": {"type": "string", "description": "Regular expression to search for"},
         "directory": {"type": "string", "description": "Directory to search (default: .)"}},
        ["pattern"]),
    run=grep_files, order=150, group="files", modes=ALL_ROLES, keeps_green=True,
    inject=("cancel",), example={"pattern": "TODO"}, adapt=_fallback_note,
    index_route=route_to_index, after=_index_note)
