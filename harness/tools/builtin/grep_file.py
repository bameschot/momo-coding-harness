"""grep_file: regex search in one file, numbered hit lines."""
from __future__ import annotations

import json
from pathlib import Path

from ...paths import split_lines
from ..common import MAX_GREP_RESULTS, capped, clip_hit, grep_one, index_target, literal
from ..registry import ALL_ROLES, Tool, tool_schema


def grep_file(pattern: str, path: str, *, workdir: Path) -> str:
    got = grep_one(pattern, path, workdir)
    if isinstance(got, str):
        return got
    text, rx = got
    hits, total = [], 0
    for i, line in enumerate(split_lines(text), 1):
        if m := rx.search(line):
            total += 1
            if total <= MAX_GREP_RESULTS:
                hits.append(f"{i:4}: {clip_hit(line, m)}")
    return capped(hits, total, "narrow the pattern, or read_file a line range")


def _index_hint(result: str, args: dict, workdir: Path, index) -> str:
    """With the code index on: index_text names the definition each hit is in."""
    if index is None or result.startswith("ERROR"):
        return result
    got = index_target(args.get("path"), workdir, index)
    if got is None or not index.has_file(got[1]):
        return result
    rel = got[1]
    text = literal(str(args.get("pattern", ""))) or "<text>"
    return result + (f"\n(code index: index_text({json.dumps(text)}, path=\"{rel}\") names the "
                     f"definition each hit is in; index_file(\"{rel}\") shows its outline and users)")


TOOL = Tool(
    tool_schema("grep_file",
        "Single-file regex search. Search for a regex pattern in one file, returns matching "
        f"lines with line numbers. Results are capped at {MAX_GREP_RESULTS} matches.",
        {"pattern": {"type": "string", "description": "Regular expression to search for. It matches the file's own text — the 'N:' line numbers shown in results are not part of the file, so never put them in the pattern"},
         "path": {"type": "string"}},
        ["pattern", "path"]),
    run=grep_file, order=140, group="files", modes=ALL_ROLES, keeps_green=True,
    example={"pattern": "def ", "path": "main.py"}, after=_index_hint)
