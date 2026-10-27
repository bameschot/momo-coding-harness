"""grep_extract: the matching text (or a capture group) from one file."""
from __future__ import annotations

from pathlib import Path

from ...paths import split_lines
from ..common import MAX_GREP_LINE_CHARS, MAX_GREP_RESULTS, capped, grep_one
from ..registry import ALL_ROLES, Tool, tool_schema


def grep_extract(pattern: str, path: str, group: int = 0, *, workdir: Path) -> str:
    got = grep_one(pattern, path, workdir)
    if isinstance(got, str):
        return got
    text, rx = got
    if not 0 <= group <= rx.groups:
        return f"ERROR: group {group} does not exist in pattern (it has {rx.groups})"
    hits, total = [], 0
    for i, line in enumerate(split_lines(text), 1):
        for m in rx.finditer(line):
            extracted = m.group(group)
            if extracted is None:
                continue
            total += 1
            if total <= MAX_GREP_RESULTS:
                if len(extracted) > MAX_GREP_LINE_CHARS:
                    extracted = (extracted[:MAX_GREP_LINE_CHARS]
                                 + f"… [match is {len(extracted):,} chars]")
                hits.append(f"{i:4}: {extracted}")
    return capped(hits, total, "narrow the pattern")


TOOL = Tool(
    tool_schema("grep_extract",
        "Single-file regex extraction. Like grep_file, but returns only the matching text "
        "(or a specific capture group) rather than the whole line. Use to pull values out of "
        "structured text, e.g. extract version strings, URLs, or identifiers. Results are "
        f"capped at {MAX_GREP_RESULTS} matches.",
        {"pattern": {"type": "string", "description": "Regex; use a capture group to extract part of the match. It matches the file's own text — the 'N:' line numbers shown in results are not part of the file"},
         "path":    {"type": "string", "description": "File to search"},
         "group":   {"type": "integer", "description": "Capture group to return (default: 0 = whole match)"}},
        ["pattern", "path"]),
    discoverable=True,
    run=grep_extract, order=160, group="files", modes=ALL_ROLES, keeps_green=True,
    example={"pattern": "def (\\w+)", "path": "main.py", "group": 1})
